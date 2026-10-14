"""Panel 4 - Data.

One row per scraped target, and behind it the account store the campaign draws
from. The two tables overlap on purpose:

  * a ROSTER says who came off one target, on one day, read by one account;
  * the STORE says who has ever been seen.

Purging the first into the second would make it an unreliable record of what had
been scraped, which is the one thing it is for.

The activity stream lives here too. It is rendered down the side of every panel,
but it is data about the runs, and this is the panel that owns the runs' output.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import StreamingResponse

from .. import events
from ..automation import messenger
from ..classify import LABELS, with_category
from ..events import log
from ..storage import object_name, objects
from ..store import now, store
from ._common import fail

router = APIRouter()

# Anything past this in a pasted file is not a handle list, it is a mistake -
# usually a JSON dump or a spreadsheet exported with every column.
MAX_UPLOAD = 5 * 1024 * 1024

HEADER_WORDS = {"handle", "username", "user", "account", "screen_name", "login"}


# -- Rosters -------------------------------------------------------
@router.get("/api/data/rosters")
def rosters() -> dict[str, Any]:
    """Every saved roster, each account labelled with what it looks like.

    The category is worked out here rather than read off the store: it is derived
    from the bio, and deriving it on the way out means retuning the words in
    `classify.py` re-labels everything ever collected without a re-scrape.
    """
    found = store.read()["rosters"]
    for roster in found:
        roster["accounts"] = [with_category(a) for a in roster.get("accounts") or []]
    return {"rosters": found}


@router.get("/api/data/categories")
def categories() -> dict[str, Any]:
    """The labels a profile can be given, in the order they are tried."""
    return {"categories": LABELS}


@router.delete("/api/data/rosters/{key}")
def delete_roster(key: str) -> dict[str, Any]:
    with store.edit() as db:
        db["rosters"] = [r for r in db["rosters"] if r["key"] != key]
    # The store keeps the handles. Deleting a roster deletes the RECORD of one
    # sweep, not everyone it ever found - a campaign mid-flight draws from the
    # store, and pulling its recipients out from under it is not what this button
    # says.
    log("data", f"deleted roster {key}", "warn")
    return {"ok": True}


@router.post("/api/data/rosters/{key}/minio")
def send_roster(key: str) -> dict[str, Any]:
    """Send one saved roster to the bucket, on the operator's say-so.

    A scrape already does this when it ends. This is the second half of that: a
    roster collected before MinIO was configured, one whose upload failed because
    the server was down, or simply one wanted in the bucket again under today's
    date. Cheap and repeatable, because each send is its own dated object rather
    than an overwrite.
    """
    roster = next((r for r in store.read()["rosters"] if r["key"] == key), None)
    if roster is None:
        raise fail("That roster is no longer saved.", 404)

    rows = roster.get("accounts") or []
    if not rows:
        raise fail("That roster has no accounts in it.")

    sent = objects.put_roster(
        rows,
        name=object_name(key),
        source=roster.get("target") or key,
        at=roster.get("at") or now(),
    )
    if not sent.get("ok"):
        # A 502, not a 500: the console is fine and the request was fine - what
        # failed is the thing on the other end, and the toast should say which.
        raise fail(sent.get("reason") or "MinIO refused the write.", 502)
    # `object` is None when every handle was already in the bucket. Not an error:
    # the bucket holds each handle once, and a second send of the same roster
    # having nothing to add is the feature working.
    return sent


@router.get("/api/minio/status")
def minio_status() -> dict[str, Any]:
    """Is the bucket reachable. Polled, so it stays cheap and never raises."""
    return objects.status()


# -- The activity stream -------------------------------------------
@router.get("/api/data/activity/stream")
def stream() -> StreamingResponse:
    return StreamingResponse(
        events.sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx in front of this in someone's setup would otherwise buffer
            # the whole stream and deliver it when the run ended.
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/api/data/activity")
def clear_activity() -> dict[str, Any]:
    with store.edit() as db:
        db["logs"] = []
    return {"ok": True}


# -- The account store ---------------------------------------------
@router.get("/api/store/accounts")
def stored_accounts() -> dict[str, Any]:
    """Every handle ever collected, labelled and with its send count settled.

    The category is derived here, not read off the store, for the same reason
    the rosters derive theirs: retuning `classify.py` should re-label the whole
    campaign picker without a re-scrape.

    `msg_count` is normalised on the way out because it did not always exist.
    Rows written before sends were counted know only yes/no, so a `messaged`
    row with no tally is worth one send - which is the truth as far as anything
    recorded it, and keeps "messaged once" from being an empty filter on every
    store that predates the field.
    """
    from ..schema_names import is_not_a_name   # noqa: PLC0415 - tiny module
    rows = []
    for row in store.read()["store"]:
        labelled = with_category(row)
        labelled["msg_count"] = _sent_count(row)
        # A link's label is not a name; shown as the handle instead. The store
        # itself is cleaned at the next start - this is for until then.
        if labelled.get("name") and is_not_a_name(labelled["name"]):
            labelled["name"] = ""
        rows.append(labelled)
    return {"rows": rows}


def _sent_count(row: dict) -> int:
    """How many times this handle has been DMed, however old the row is."""
    try:
        count = int(row.get("msg_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count or (1 if row.get("messaged") else 0)


@router.post("/api/store/accounts/delete")
def delete_accounts(body: dict = Body(default={})) -> dict[str, Any]:
    """Take handles OUT of the store for good.

    The other delete in this panel removes a sweep and leaves everyone it found
    collected. This is the opposite and the one the table row needs: a handle
    that should not be in the list at all - a group, a duplicate, someone who
    asked not to be written to again - goes, and stays gone.

    By handle rather than by index. The client is showing a filtered, paged view
    and its row numbers mean nothing here; the handle is what the store is keyed
    on, so what is named is what is removed even if the two lists have drifted.

    Takes a LIST because the table will grow a multi-select, and a route that
    only ever deletes one would have to be replaced rather than reused.
    """
    wanted = {str(h).lstrip("@").strip().lower()
              for h in (body.get("handles") or []) if str(h).strip()}
    if not wanted:
        raise fail("No handles to delete.")

    with store.edit() as db:
        keeping = [r for r in db["store"]
                   if str(r.get("handle", "")).strip().lower() not in wanted]
        removed = len(db["store"]) - len(keeping)
        db["store"] = keeping

    if not removed:
        # Said rather than swallowed: a delete that matched nothing means the
        # table and the store disagree, and the operator is about to press it
        # again on a row that is already gone.
        raise fail("Nothing in the store matched that.", 404)
    log("data", f"deleted {removed} handle{'' if removed == 1 else 's'} from the store", "warn")
    return {"ok": True, "removed": removed}


@router.get("/api/store/accounts/minio")
def stored_in_bucket() -> dict[str, Any]:
    """The handles and bios in the bucket's `usernames_bio.txt`, read live."""
    found = objects.read_handles()
    if not found.get("ok"):
        raise fail(found.get("reason") or "MinIO refused the read.", 502)
    have = {str(r.get("handle", "")).lower() for r in store.read()["store"]}
    for row in found["rows"]:
        row["in_store"] = row["handle"].lower() in have
    return found


@router.post("/api/store/accounts/minio/import")
def import_from_bucket() -> dict[str, Any]:
    """Bring the bucket's handles and bios into the store, under the target `minio`.

    The Database option on the Recipients step. A handle already in the store
    keeps its row and gains the bucket's bio when it had none; a new one is
    added with the bio and nothing else, exactly like an upload.
    """
    found = objects.read_handles()
    if not found.get("ok"):
        raise fail(found.get("reason") or "MinIO refused the read.", 502)
    added, refreshed = 0, 0
    with store.edit() as db:
        index = {str(r.get("handle", "")).lower(): r for r in db["store"]}
        for row in found["rows"]:
            handle = messenger.handle_of(row["handle"])
            if not handle:
                continue
            existing = index.get(handle.lower())
            if existing:
                if row.get("bio") and not existing.get("bio"):
                    existing["bio"] = row["bio"]
                    refreshed += 1
                if "minio" not in (existing.get("targets") or []):
                    existing.setdefault("targets", []).append("minio")
                continue
            new_row = {
                "handle": handle, "name": "", "bio": row.get("bio") or "",
                "followers": None, "dm_open": True, "protected": False,
                "targets": ["minio"], "messaged": bool(row.get("used_count")),
                "msg_count": int(row.get("used_count") or 0), "at": now(),
            }
            db["store"].append(new_row)
            index[handle.lower()] = new_row
            added += 1
    log("data", f"imported {added} handles from {found.get('bucket')}"
                + (f", {refreshed} bios filled in" if refreshed else ""))
    return {"added": added, "refreshed": refreshed, "total": len(found["rows"]),
            "bucket": found.get("bucket")}


@router.post("/api/store/accounts/minio")
def send_stored(body: dict = Body(default={})) -> dict[str, Any]:
    """Send stored handles to the bucket - the ones asked for, or all of them.

    `send_roster` above sends one saved sweep. This sends out of the STORE, which
    is a different and usually much larger set: a roster is replaced every time
    its target is walked again, while the store keeps everyone it has ever seen.
    With the Data panel reading the store, the button that says "send what is on
    screen" needs a route that can actually send what is on screen.

    Handles rather than a whole payload: the browser already has the rows, but
    what it has is a filtered VIEW, and letting a client post row contents into
    the archive would make the bucket a record of what some tab believed rather
    than of what was collected. So the client names who, and the server sends its
    own copy of them.
    """
    wanted = {str(h).lstrip("@").strip().lower()
              for h in (body.get("handles") or []) if str(h).strip()}
    rows = store.read()["store"]
    if wanted:
        rows = [r for r in rows if str(r.get("handle", "")).lower() in wanted]
    if not rows:
        raise fail("Nothing to send.")

    sent = objects.put_roster(
        rows,
        name=object_name(body.get("name") or "store"),
        source=body.get("source") or "account store",
        at=now(),
    )
    if not sent.get("ok"):
        # A 502 for the same reason `send_roster` uses one: the console and the
        # request are both fine, and it is the thing on the other end that said no.
        raise fail(sent.get("reason") or "MinIO refused the write.", 502)
    return sent


@router.post("/api/store/accounts/import")
def import_accounts(body: dict = Body(default={})) -> dict[str, int]:
    added = 0
    with store.edit() as db:
        seen = {row["handle"] for row in db["store"]}
        for row in body.get("rows") or []:
            handle = messenger.handle_of(str(row.get("handle") or ""))
            if not handle or handle in seen:
                continue
            db["store"].append({
                "handle": handle,
                "name": str(row.get("name") or ""),
                "bio": "",
                "followers": None,
                # Unknown, not closed. A file of handles says nothing about
                # inboxes, and marking them shut would hide every one of them
                # behind the Open DMs filter the moment it was ticked.
                "dm_open": True,
                "protected": False,
                "targets": ["uploaded"],
                "messaged": False,
                "msg_count": 0,
                "at": now(),
            })
            seen.add(handle)
            added += 1
    log("data", f"imported {added} handles")
    return {"added": added}


@router.post("/api/store/accounts/parse")
async def parse_accounts(file: UploadFile = File(...)) -> dict[str, Any]:
    """Read a file of handles, and show them before anything is stored.

    Parsed on the server rather than in the browser so the two upload paths -
    this one and a paste - end at the same code. The upload is not saved: this
    answers what it contains, and the Import button is what commits it.
    """
    text = await _text(file)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for line in csv.reader(io.StringIO(text), delimiter=_delimiter(text)):
        if not line:
            continue
        handle = (line[0] or "").strip().lstrip("@")
        if not handle or handle.lower() in HEADER_WORDS:
            continue
        # A pasted profile URL is a handle somebody did not strip, and a
        # profile with no username is only ever copied as one - so every row
        # goes through the same parser and comes out as the bare handle or the
        # bare numeric id.
        handle = messenger.handle_of(handle)
        if not handle:
            continue
        if handle in seen:
            continue
        seen.add(handle)
        rows.append({"handle": handle, "name": (line[1].strip() if len(line) > 1 else "")})

    return {"rows": rows}


@router.post("/api/store/accounts/names")
def fill_names() -> dict[str, int]:
    """Backfill display names from the rosters.

    An imported list is handles and nothing else, and a draft that opens with
    `{name}` renders the handle for every one of them. The rosters already know
    the names for anyone who was scraped rather than uploaded.
    """
    filled = 0
    with store.edit() as db:
        names: dict[str, str] = {}
        for roster in db["rosters"]:
            for account in roster["accounts"]:
                if account.get("name") and account["handle"] not in names:
                    names[account["handle"]] = account["name"]
        for row in db["store"]:
            if row.get("name"):
                continue
            found = names.get(row["handle"])
            if found:
                row["name"] = found
                filled += 1
    log("data", f"filled {filled} names")
    return {"filled": filled}


# -- Reading an upload ---------------------------------------------
async def _text(file: UploadFile) -> str:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        raise fail("That file is far too large to be a list of handles.")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise fail("That file is not text this can read.")


def _delimiter(text: str) -> str:
    """Tabs if the file has them, commas otherwise.

    Sniffed rather than configured: a roster downloaded out of this console is
    tab-separated, and it being re-uploadable without a setting to flip is worth
    the guess.
    """
    head = text[:4000]
    return "\t" if head.count("\t") > head.count(",") else ","
