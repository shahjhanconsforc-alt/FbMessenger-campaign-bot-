"""The object store - MinIO, shared with the other bots on this machine.

Three buckets, and the layout is the one every other bot here already keeps,
so a file this console writes can be read by any of them and a file they write
can be read here:

    <MINIO_BUCKET>/usernames_bio.txt          handle + bio, one per line
    <MINIO_BUCKET>/fb-groups.txt              Facebook groups each account is in,
                                              and how often each was posted in
    <MINIO_BUCKET_MESSAGES>/sms_drafts.txt    the campaign's drafts
    <MINIO_BUCKET_MEDIA>/<image files>        the campaign's pictures

The handles file is THIS console's - `messenger-bot-usernames` by default,
beside `insta-bot-usernames` and `threads-usernames`. Facebook handles and ids
are not Instagram usernames, and writing them into the insta bot's file would
hand that bot people it cannot reach. The messages and media buckets point at
the insta bot's by default on purpose: the drafts and pictures ARE shared, and
the Campaign panel's Database option reads them from there.

Every file is tab-separated text with a header line, the shape the other bots'
`tsv` codec writes: fields are escaped so a bio with a line break in it stays
one record. `dumps`/`loads` below are that codec, kept byte-compatible.

Three rules this module keeps.

**Optional, always.** There may be no MinIO on this machine and a scrape must
not care. Every call here reports failure as a returned reason rather than an
exception, and the caller logs it and carries on.

**One file per kind, upserted.** The handles file holds each handle once
across every run: a walk of a feed that has no end meets the same people every
day, and a dated file per run would write them down a few hundred times.

**Bucket names are literal.** What `.env` says is what is used. An earlier
build forced an `x-` prefix on to whatever was configured; that made it
impossible to point at another bot's bucket, which is now the whole point.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config
from .events import log

# -- The files ------------------------------------------------------
HANDLES_OBJECT = "usernames_bio.txt"
HANDLE_FIELDS = ("username", "bio", "used_count")

# The Facebook groups each account has joined, one line per account and group,
# with how many times the group has been posted in and when it last was. One
# file for every account: `account_id` is part of the key, because two accounts
# can be in the same group and have posted in it a different number of times.
GROUPS_OBJECT = "fb-groups.txt"
GROUP_FIELDS = ("account_id", "account", "group_id", "name", "url",
                "used_count", "last_posted", "joined_at",
                # When the Scraping panel last read this group's members, and
                # how many it read. Empty on a group never scraped.
                "scraped_at", "scraped_members",
                # What the group says its membership is, off its People page.
                # Kept so the panel can say 320 of 4,532 after the run as well
                # as during it. Zero on a group that does not say.
                "members_total")

DRAFTS_OBJECT = "sms_drafts.txt"
DRAFT_FIELDS = ("id", "name", "text", "disabled")

# What the insta bot writes for a handle it collected without reading a bio.
UNKNOWN_BIO = "unknown"

# How long one call to MinIO may take. Short, and with no retries behind it:
# this talks to a server on the same machine or the same LAN, where the honest
# answers are "at once" and "not there".
TIMEOUT = 3.0

# How long a status answer is reused. The pill polls every five seconds and the
# reply cannot cost a network round trip each time - least of all when MinIO is
# down, which is exactly when the round trip is slowest.
STATUS_TTL = 20.0

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def bucket_name(raw: str) -> str:
    """The bucket a name asks for. Lower-cased, and otherwise exactly as given."""
    return (raw or "").strip().lower() or "messenger-bot-usernames"


def object_name(key: str, when: Optional[str] = None) -> str:
    """A dated file name for one run. Kept for callers that still pass one;
    the handles file has a fixed name and `put_roster` does not use this."""
    stamp = (when or datetime.now(timezone.utc).isoformat())[:16]
    stamp = stamp.replace(":", "").replace("-", "").replace("T", "-")
    safe = re.sub(r"[^a-z0-9._-]+", "-", (key or "roster").lower()).strip("-") or "roster"
    return f"{safe}-{stamp}.txt"


# -- The tab-separated codec ---------------------------------------
# Byte-compatible with the other bots' `services/storage/tsv.py`: a header line
# naming the columns, one record per line, and every field escaped so a
# backslash, newline, carriage return or tab inside a value cannot break the
# line/column shape. Backslash is escaped first on the way out and the unescape
# is one left-to-right pass, or `\\n` would turn into a real newline.
SEP = "\t"
_ESCAPE = ((chr(92), chr(92) * 2), ("\n", chr(92) + "n"),
           ("\r", chr(92) + "r"), ("\t", chr(92) + "t"))
_UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", chr(92): chr(92)}


def escape(value: Any) -> str:
    text = "" if value is None else str(value)
    for raw, coded in _ESCAPE:
        text = text.replace(raw, coded)
    return text


def unescape(text: str) -> str:
    if chr(92) not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == chr(92) and i + 1 < n and text[i + 1] in _UNESCAPE:
            out.append(_UNESCAPE[text[i + 1]])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def dumps(rows: list[dict], columns: tuple[str, ...]) -> bytes:
    lines = [SEP.join(columns)]
    for row in rows or []:
        lines.append(SEP.join(escape((row or {}).get(c)) for c in columns))
    return ("\n".join(lines) + "\n").encode("utf-8")


def loads(raw: bytes | str, columns: tuple[str, ...]) -> list[dict]:
    """A stored table back into dicts. The header decides the mapping; a file
    with no recognisable header is read positionally against `columns`."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    head = lines[0].split(SEP)
    if any(h.strip() in columns for h in head):
        names = [h.strip() for h in head]
        body = lines[1:]
    else:
        names = list(columns)
        body = lines
    out: list[dict] = []
    for line in body:
        if line.startswith("#"):
            continue
        cells = line.split(SEP)
        row = {names[i]: unescape(cells[i]) for i in range(min(len(names), len(cells)))}
        for c in columns:
            row.setdefault(c, "")
        out.append(row)
    return out


def looks_like_json(raw: bytes | str) -> bool:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else (raw or "")
    return text.lstrip()[:1] in ("[", "{")


# -- Drafts, in every shape on this MinIO ---------------------------
_TRUTHY = (True, 1, "1", "true", "True")


def drafts_from(text: str) -> list[dict[str, Any]]:
    """Whatever a messages file holds, as this console's drafts.

    Three shapes are in the wild on this MinIO and all three have to read:

      * tab-separated - `id  name  text  disabled`, which is what the insta and
        threads bots write to `sms_drafts.txt`;
      * JSON - `[{"label": ..., "text": ..., "disabled": false}]`, the meetup
        bot's `message_drafts.json`;
      * plain text - drafts separated by a blank line.

    Sniffed rather than configured: a bucket is a shared surface, and one bot
    changing its format should be a thing this notices rather than a setting
    somebody has to be told to flip.
    """
    body = (text or "").strip()
    if not body:
        return []

    if body[0] in "[{":
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("drafts") or data.get("messages") or []
        if isinstance(data, list):
            out = []
            for i, row in enumerate(data):
                if isinstance(row, str):
                    out.append({"name": f"sms{i + 1}", "text": row.strip(), "enabled": True})
                    continue
                if not isinstance(row, dict):
                    continue
                body_text = str(row.get("text") or row.get("message") or "").strip()
                if not body_text:
                    continue
                name = str(row.get("label") or row.get("name") or row.get("id")
                           or f"sms{i + 1}").strip()
                enabled = (row.get("disabled") not in _TRUTHY) if "disabled" in row \
                    else row.get("enabled") is not False
                out.append({"name": name or f"sms{i + 1}", "text": body_text,
                            "enabled": bool(enabled)})
            return out

    first = body.splitlines()[0]
    if SEP in first and ("text" in first.split(SEP) or first.count(SEP) >= 2):
        out = []
        for i, row in enumerate(loads(body, DRAFT_FIELDS)):
            body_text = str(row.get("text") or "").strip()
            if not body_text:
                continue
            name = str(row.get("name") or row.get("id") or f"sms{i + 1}").strip()
            out.append({"name": name or f"sms{i + 1}", "text": body_text,
                        "enabled": row.get("disabled") not in _TRUTHY})
        return out

    return [
        {"name": f"sms{i + 1}", "text": chunk.strip(), "enabled": True}
        for i, chunk in enumerate(re.split(r"\r?\n\s*\r?\n", body))
        if chunk.strip()
    ]


def drafts_to_tsv(drafts: list[dict[str, Any]]) -> bytes:
    """The drafts as `sms_drafts.txt` - the insta bot's shape, so the file
    written here reads back there and vice versa."""
    rows = []
    for i, d in enumerate(drafts):
        name = str(d.get("name") or f"sms{i + 1}")
        rows.append({"id": str(d.get("id") or f"sms{i + 1}"), "name": name,
                     "text": d.get("text") or "",
                     "disabled": "1" if d.get("enabled") is False else ""})
    return dumps(rows, DRAFT_FIELDS)


class ObjectStore:
    """A MinIO connection made on demand and kept.

    Lazily, because the console must start with no MinIO on the network - and
    because building the client is where a bad endpoint is discovered, which is
    not something a server should die of at import time.
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._lock = threading.Lock()
        self._known: set[str] = set()          # buckets already created this run
        self._status: Optional[dict[str, Any]] = None
        self._status_at = 0.0
        self._probing = False

    # -- Connecting -------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(config.MINIO_URL and config.MINIO_USER and config.MINIO_PASSWORD)

    @property
    def handles_bucket(self) -> str:
        return bucket_name(config.MINIO_BUCKET)

    @property
    def groups_bucket(self) -> str:
        return bucket_name(config.MINIO_BUCKET_GROUPS or config.MINIO_BUCKET)

    @property
    def messages_bucket(self) -> str:
        return bucket_name(config.MINIO_BUCKET_MESSAGES)

    @property
    def media_bucket(self) -> str:
        return bucket_name(config.MINIO_BUCKET_MEDIA)

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from minio import Minio                 # noqa: PLC0415 - optional dependency
        except ImportError as err:                  # pragma: no cover - depends on the install
            raise RuntimeError(
                "the `minio` package is not installed - `pip install minio`"
            ) from err
        import urllib3                              # noqa: PLC0415 - a minio dependency

        endpoint = (config.MINIO_URL or "").strip()
        secure = config.MINIO_SECURE or endpoint.lower().startswith("https://")
        for prefix in ("https://", "http://"):
            if endpoint.lower().startswith(prefix):
                endpoint = endpoint[len(prefix):]
        pool = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=TIMEOUT, read=TIMEOUT),
            retries=urllib3.Retry(total=0, connect=0, read=0, redirect=1, status=0),
        )
        self._client = Minio(
            endpoint.rstrip("/"),
            access_key=config.MINIO_USER,
            secret_key=config.MINIO_PASSWORD,
            secure=secure,
            http_client=pool,
        )
        return self._client

    def _ensure_bucket(self, client: Any, bucket: str) -> None:
        if bucket in self._known:
            return
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            log("minio", f"created bucket {bucket}")
        self._known.add(bucket)

    def _read_bytes(self, client: Any, bucket: str, name: str) -> Optional[bytes]:
        """One object's bytes, or None when the bucket or object is not there."""
        try:
            if not client.bucket_exists(bucket):
                return None
            response = client.get_object(bucket, name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception as err:                     # noqa: BLE001 - a missing key is "None"
            code = getattr(err, "code", "")
            if code in ("NoSuchKey", "NoSuchBucket"):
                return None
            raise

    def _put_bytes(self, client: Any, bucket: str, name: str, body: bytes,
                   content_type: str) -> None:
        self._ensure_bucket(client, bucket)
        client.put_object(bucket, name, io.BytesIO(body), len(body), content_type=content_type)

    def _why(self, err: Exception, target: str = "") -> str:
        """One readable line out of whatever the SDK raised, and a reset so the
        next attempt reconnects: a MinIO restarted underneath us leaves a
        client that fails for ever."""
        self._client = None
        self._known.clear()
        self.forget_status()
        reason = " ".join((str(err) or err.__class__.__name__).split())[:160]
        log("minio", f"{target or 'minio'}: {reason}", "warn")
        return reason

    def _off(self) -> dict[str, Any]:
        return {"ok": False, "reason": "MinIO is not configured - see backend/.env"}

    # -- The handles file -------------------------------------------
    def _load_handles(self, client: Any, bucket: str) -> list[dict]:
        raw = self._read_bytes(client, bucket, HANDLES_OBJECT)
        if raw is None:
            return []
        if looks_like_json(raw):
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                return []
            rows = data if isinstance(data, list) else []
            return [{"username": str(r.get("username") or r.get("handle") or ""),
                     "bio": str(r.get("bio") or ""), "used_count": str(r.get("used_count") or 0)}
                    for r in rows if isinstance(r, dict)]
        return loads(raw, HANDLE_FIELDS)

    def read_handles(self) -> dict[str, Any]:
        """Every handle and bio in the bucket's file, as rows the store can take."""
        if not self.configured:
            return self._off()
        target = self.handles_bucket
        try:
            with self._lock:
                client = self._connect()
                stored = self._load_handles(client, target)
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "reason": self._why(err, target)}
        rows = []
        for r in stored:
            handle = str(r.get("username") or "").strip().lstrip("@")
            if not handle:
                continue
            bio = str(r.get("bio") or "").strip()
            try:
                used = int(str(r.get("used_count") or "0").strip() or 0)
            except ValueError:
                used = 0
            rows.append({"handle": handle, "bio": "" if bio == UNKNOWN_BIO else bio,
                         "used_count": used})
        return {"ok": True, "bucket": target, "object": HANDLES_OBJECT, "rows": rows}

    def put_roster(self, rows: list[dict], *, name: str = "", source: str = "",
                   at: str = "", bucket: Optional[str] = None) -> dict[str, Any]:
        """Merge a roster's handles and bios into the bucket's file.

        The file holds each handle once. A handle already there keeps its
        place and its use count, and its bio is refreshed only when the new
        row actually has one - a row collected off a feed, with no bio, must
        not blank a bio that was read off a profile last week.

        `name`, `source` and `at` are accepted so the callers written for the
        dated-file layout still work; the file is one file and has one name.
        """
        if not rows:
            return {"ok": False, "reason": "Nothing collected to send."}
        if not self.configured:
            return self._off()

        target = bucket_name(bucket) if bucket else self.handles_bucket
        try:
            with self._lock:
                client = self._connect()
                stored = self._load_handles(client, target)
                index: dict[str, dict] = {}
                for r in stored:
                    key = str(r.get("username") or "").strip().lstrip("@").lower()
                    if key and key not in index:
                        index[key] = {"username": str(r.get("username")).strip().lstrip("@"),
                                      "bio": str(r.get("bio") or "") or UNKNOWN_BIO,
                                      "used_count": str(r.get("used_count") or "0")}
                new = 0
                for row in rows:
                    handle = str(row.get("handle") or "").strip().lstrip("@")
                    if not handle:
                        continue
                    bio = " ".join(str(row.get("bio") or "").split())
                    key = handle.lower()
                    found = index.get(key)
                    if found is None:
                        index[key] = {"username": handle, "bio": bio or UNKNOWN_BIO,
                                      "used_count": str(int(row.get("msg_count") or 0))}
                        new += 1
                    elif bio:
                        found["bio"] = bio
                skipped = len(rows) - new
                body = dumps(list(index.values()), HANDLE_FIELDS)
                self._put_bytes(client, target, HANDLES_OBJECT, body,
                                "text/tab-separated-values; charset=utf-8")
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "object": HANDLES_OBJECT,
                    "reason": self._why(err, target)}

        self.forget_status()
        log("minio", f"{target}/{HANDLES_OBJECT}: {new} new"
                     + (f", {skipped} already there" if skipped else "")
                     + f" - {len(index)} in all")
        return {"ok": True, "bucket": target, "object": HANDLES_OBJECT if new else None,
                "new": new, "skipped": skipped, "total": len(index)}

    # -- The groups file --------------------------------------------
    def read_fb_groups(self) -> dict[str, Any]:
        """Every row of fb-groups.txt. A file that is not there yet is no rows."""
        if not self.configured:
            return self._off()
        target = self.groups_bucket
        try:
            with self._lock:
                client = self._connect()
                raw = self._read_bytes(client, target, GROUPS_OBJECT)
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "reason": self._why(err, target)}
        rows = loads(raw, GROUP_FIELDS) if raw else []
        return {"ok": True, "bucket": target, "object": GROUPS_OBJECT, "rows": rows}

    def write_fb_groups(self, rows: list[dict]) -> dict[str, Any]:
        """Overwrite fb-groups.txt with these rows."""
        if not self.configured:
            return self._off()
        target = self.groups_bucket
        body = dumps(rows, GROUP_FIELDS)
        try:
            with self._lock:
                client = self._connect()
                self._put_bytes(client, target, GROUPS_OBJECT, body,
                                "text/tab-separated-values; charset=utf-8")
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "object": GROUPS_OBJECT,
                    "reason": self._why(err, target)}
        self.forget_status()
        return {"ok": True, "bucket": target, "object": GROUPS_OBJECT, "rows": len(rows)}

    # -- The drafts file --------------------------------------------
    def read_drafts(self, *, seed: bool = False) -> dict[str, Any]:
        """The drafts in the messages bucket. `seed` is accepted and ignored."""
        if not self.configured:
            return self._off()
        target = self.messages_bucket
        try:
            with self._lock:
                client = self._connect()
                raw = self._read_bytes(client, target, DRAFTS_OBJECT)
                found = DRAFTS_OBJECT
                if raw is None:
                    # The meetup bot's name for the same file. Read if it is
                    # what the bucket holds, so a bucket that predates the
                    # text layout still reads.
                    raw = self._read_bytes(client, target, "message_drafts.json")
                    found = "message_drafts.json" if raw is not None else DRAFTS_OBJECT
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "reason": self._why(err, target)}
        drafts = drafts_from((raw or b"").decode("utf-8", "replace"))
        return {"ok": True, "bucket": target, "object": found, "drafts": drafts,
                "seeded_from": ""}

    def write_drafts(self, drafts: list[dict[str, Any]]) -> dict[str, Any]:
        """Put the drafts back, overwriting `sms_drafts.txt`."""
        if not drafts:
            return {"ok": False, "reason": "No drafts to send."}
        if not self.configured:
            return self._off()
        target = self.messages_bucket
        body = drafts_to_tsv(drafts)
        try:
            with self._lock:
                client = self._connect()
                self._put_bytes(client, target, DRAFTS_OBJECT, body,
                                "text/tab-separated-values; charset=utf-8")
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "object": DRAFTS_OBJECT,
                    "reason": self._why(err, target)}
        self.forget_status()
        log("minio", f"wrote {target}/{DRAFTS_OBJECT} ({len(body)} bytes)")
        return {"ok": True, "bucket": target, "object": DRAFTS_OBJECT, "bytes": len(body)}

    # -- The media bucket -------------------------------------------
    def list_media(self) -> dict[str, Any]:
        """Every image in the media bucket, as [{name, size}]."""
        if not self.configured:
            return self._off()
        target = self.media_bucket
        try:
            with self._lock:
                client = self._connect()
                if not client.bucket_exists(target):
                    return {"ok": True, "bucket": target, "media": []}
                items = [{"name": o.object_name, "size": int(o.size or 0)}
                         for o in client.list_objects(target, recursive=True)
                         if not str(o.object_name).endswith("/")]
        except Exception as err:                     # noqa: BLE001 - reported, never raised
            return {"ok": False, "bucket": target, "reason": self._why(err, target)}
        items = [m for m in items
                 if "." + m["name"].rsplit(".", 1)[-1].lower() in IMAGE_TYPES]
        return {"ok": True, "bucket": target, "media": items}

    def get_media(self, name: str) -> Optional[bytes]:
        """One image's bytes, or None. Raises nothing."""
        if not self.configured:
            return None
        target = self.media_bucket
        try:
            with self._lock:
                client = self._connect()
                return self._read_bytes(client, target, name)
        except Exception as err:                     # noqa: BLE001
            self._why(err, f"{target}/{name}")
            return None

    def put_media(self, name: str, body: bytes, content_type: str) -> dict[str, Any]:
        if not self.configured:
            return self._off()
        target = self.media_bucket
        try:
            with self._lock:
                client = self._connect()
                self._put_bytes(client, target, name, body, content_type)
        except Exception as err:                     # noqa: BLE001
            return {"ok": False, "bucket": target, "reason": self._why(err, target)}
        return {"ok": True, "bucket": target, "object": name, "bytes": len(body)}

    # -- Reporting --------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Whether the bucket is reachable, for the pill that says so.

        Deliberately cheap and deliberately quiet: it is polled, and a MinIO that
        is simply not running is a normal state for this console rather than an
        error to shout about.
        """
        if not self.configured:
            return {"configured": False, "up": False, "bucket": None,
                    "detail": "not configured"}
        target = self.handles_bucket
        cached = self._status
        fresh = cached is not None and time.monotonic() - self._status_at < STATUS_TTL
        if not fresh:
            self._probe_later(target)
        if cached is not None:
            return dict(cached)
        return {"configured": True, "up": False, "bucket": target, "detail": "checking"}

    def _probe_later(self, target: str) -> None:
        if self._probing:
            return
        self._probing = True

        def probe() -> None:
            try:
                with self._lock:
                    client = self._connect()
                    exists = client.bucket_exists(target)
                found = {"configured": True, "up": True, "bucket": target,
                         "detail": "ready" if exists else "not created yet",
                         "messages": self.messages_bucket, "media": self.media_bucket}
            except Exception as err:                 # noqa: BLE001 - a probe never raises
                self._client = None
                self._known.clear()
                reason = " ".join(str(err).split()) or err.__class__.__name__
                found = {"configured": True, "up": False, "bucket": target,
                         "detail": reason[:160]}
            finally:
                self._probing = False
            self._status = found
            self._status_at = time.monotonic()

        threading.Thread(target=probe, name="minio-probe", daemon=True).start()

    def forget_status(self) -> None:
        self._status = None


objects = ObjectStore()
