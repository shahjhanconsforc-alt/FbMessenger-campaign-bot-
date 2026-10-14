"""Panel 1 - Campaign, and the two stores the wizard fills itself from.

The wizard's five steps are accounts, recipients, drafts, media, status, so the
drafts and the media library are answered here rather than in a panel of their
own: they exist to be picked in step three and step four.

Two things about the send that are decisions, not implementation details:

  * The PAIRING is decided before anything is sent. Which account writes to whom
    is fixed when the run starts and the lane table is that decision, not a
    guess reassembled on the UI side from a percentage.
  * The pacing is per ACCOUNT-DAY. Messenger counts messages per account, so the run holds a
    daily tally per account and stops that lane when it is spent - a run that
    only knew its own total would let three runs in one afternoon spend the same
    budget three times over.

There are only five statuses the console can draw: pending, processing, sent,
typed, failed. A recipient that was passed over is therefore `failed` with the
reason on the row, which is honest - it was not messaged - and visible, which
"skipped" quietly dropped from the table would not be.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import FileResponse

from .. import busy, config, tasks
from ..automation import facebook, messenger
from ..automation.browser import automation, AutomationError
from ..automation.messenger import DmClosed
from ..events import log
from ..storage import objects
from ..store import now, store
from ._common import fail, find_account
from .actions import MESSENGER_ACTIONS

# What the Messenger-actions turn presses, by the rail's own words.
_ACTION_LABELS = [messenger.NAV_ACTIONS[k]["label"] for k in MESSENGER_ACTIONS]

router = APIRouter()

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE = 15 * 1024 * 1024

_campaign: dict[str, Any] = {"lanes": [], "state": "", "note": "", "task": None,
                             "restarts": 0, "run": {}}
_lock = threading.Lock()

# The run going right now, and the settings it was started with. Read by the
# persister for the counters a restart needs, and by a restart for the settings
# - the ones the run began with, so a knob turned mid-run does not change what
# a restart does.
_current: dict[str, Any] = {"run": None, "settings": None}

# Where the run lives on disk, so a server that dies mid-campaign comes back
# knowing what was sent and what was not. Rewritten as the run goes, at most
# every couple of seconds while rows are landing and at once on every change
# of state.
CAMPAIGN_FILE = config.DATA / "campaign.json"
_persist_at = {"when": 0.0}


def _state() -> dict[str, Any]:
    with _lock:
        out = {
            "lanes": [{"account": lane["account"], "rows": [dict(r) for r in lane["rows"]]}
                      for lane in _campaign["lanes"]],
            "state": _campaign["state"],
            "note": _campaign["note"],
            "task": _campaign.get("task") or "",
            "restarts": int(_campaign.get("restarts") or 0),
        }
        full = {
            "lanes": [{"account": lane["account"], "id": str(lane["id"]),
                       "rows": [dict(r) for r in lane["rows"]]}
                      for lane in _campaign["lanes"]],
            "state": _campaign["state"],
            "note": _campaign["note"],
            "task": _campaign.get("task") or "",
            "restarts": int(_campaign.get("restarts") or 0),
        }
    _persist(full)
    return out


def _persist(full: Optional[dict] = None, *, force: bool = False) -> None:
    """Write the run to disk. Throttled unless `force`, and never raising.

    Called from `_state()`, which everything that changes a row calls, so the
    file is never more than a moment behind the table. Not called under
    `_lock` - it takes the lock itself when it has to build the snapshot.
    """
    now = time.monotonic()
    if not force and now - _persist_at["when"] < 2.0:
        return
    if full is None:
        with _lock:
            full = {
                "lanes": [{"account": lane["account"], "id": str(lane["id"]),
                           "rows": [dict(r) for r in lane["rows"]]}
                          for lane in _campaign["lanes"]],
                "state": _campaign["state"],
                "note": _campaign["note"],
                "task": _campaign.get("task") or "",
                "restarts": int(_campaign.get("restarts") or 0),
            }
    run = _current.get("run")
    with _lock:
        if run is not None:
            _campaign["run"] = run.snapshot()
        full["run"] = dict(_campaign.get("run") or {})
    full["settings"] = _current.get("settings") or {}
    full["saved_at"] = now_iso()
    _persist_at["when"] = now
    try:
        tmp = CAMPAIGN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CAMPAIGN_FILE)
    except Exception as err:                         # noqa: BLE001 - the run matters more than the copy
        if not _persist_at.get("complained"):
            _persist_at["complained"] = True
            log("campaign", f"could not save the run to {CAMPAIGN_FILE.name}: {err}", "warn")


def reset_idle() -> None:
    """No campaign at all: not in memory, not in campaign.json.

    campaign.json is what a server start restarts a campaign from, so a reset
    that left the old rows on disk would bring the campaign back the next time
    the server came up. A restart that is only waiting to begin sees the state
    is no longer "restarting" and does not.
    """
    with _lock:
        _campaign.update(lanes=[], state="", note="", task=None, restarts=0, run={})
    _current.update(run=None, settings=None)
    try:
        CAMPAIGN_FILE.unlink(missing_ok=True)
    except OSError as err:
        log("campaign", f"could not remove {CAMPAIGN_FILE.name}: {err}", "warn")


def now_iso() -> str:
    return now()


def _load_saved() -> Optional[dict]:
    try:
        if not CAMPAIGN_FILE.exists():
            return None
        data = json.loads(CAMPAIGN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("lanes") else None
    except Exception as err:                         # noqa: BLE001 - a bad file is no campaign
        log("campaign", f"could not read {CAMPAIGN_FILE.name}: {err}", "warn")
        return None


def _recover_rows(lanes: list[dict]) -> int:
    """Make saved rows fit to run again. Returns how many recipients are left.

    A recipient the run was ON when it died is not known to have been reached
    - the message may or may not have gone - so it goes back to pending and is
    tried again; a second copy of one message is the lesser harm against a
    recipient never reached. A break or an actions turn caught mid-way is
    written off as interrupted; the next planned one still stands.
    """
    left = 0
    for lane in lanes:
        for row in lane["rows"]:
            if row.get("kind"):
                if row.get("status") == "processing":
                    row["status"] = "failed"
                    row["why"] = "interrupted - " + str(row.get("why") or "")
                continue
            if row.get("status") == "processing":
                row["status"] = "pending"
                row["why"] = ""
                row["chat"] = ""
            if row.get("status") == "pending":
                left += 1
    return left


def _pending_left() -> int:
    with _lock:
        return sum(1 for lane in _campaign["lanes"] for r in lane["rows"]
                   if not r.get("kind") and r.get("status") == "pending")


def recover() -> None:
    """At server start: pick the last campaign back up off disk.

    A run that was going when the server went down comes back as
    `restarting` and is resumed once the windows are up again; one that had
    finished is only loaded, so the live panel still shows it.
    """
    data = _load_saved()
    if not data:
        return
    with _lock:
        _campaign.update(lanes=data["lanes"], state=str(data.get("state") or ""),
                         note=str(data.get("note") or ""), task=None,
                         restarts=int(data.get("restarts") or 0),
                         run=dict(data.get("run") or {}))
    _current["settings"] = data.get("settings") or None
    if _campaign["state"] not in ("running", "paused", "restarting"):
        return
    with _lock:
        left = _recover_rows(_campaign["lanes"])
    if not left:
        with _lock:
            _campaign["state"] = "done"
            _campaign["note"] = "Finished - the server restarted after the last recipient."
        _persist(force=True)
        return
    settings = _current["settings"] or store.settings()
    if not settings.get("campaign_auto_restart", True):
        with _lock:
            _campaign["state"] = "interrupted"
            _campaign["note"] = (f"The server restarted mid-campaign with {left} to go. "
                                 "Press Resume to carry on from where it was.")
        _persist(force=True)
        log("campaign", f"interrupted by a server restart with {left} to go - waiting for Resume", "warn")
        return
    with _lock:
        _campaign["state"] = "restarting"
        _campaign["note"] = (f"The server restarted mid-campaign with {left} to go - "
                             "carrying on from where it was once the windows are up.")
    _persist(force=True)
    log("campaign", f"server restarted mid-campaign with {left} to go - resuming", "warn")
    _schedule_restart("the server restarted", immediate=True, delay=8.0)


def _schedule_restart(reason: str, *, wait_for: Optional[tasks.Task] = None,
                      immediate: bool = False, delay: float = 0.0) -> None:
    """Start the run again from its saved rows, on a thread, after a moment.

    `immediate` is a restart the operator or the server asked for: it does not
    count against the limit and resets it. Anything else is the run picking
    itself up, which is counted, and backs off a little more each time so a
    Messenger that is refusing everyone is not hammered every few seconds.
    """
    def go() -> None:
        deadline = time.monotonic() + 120
        while (wait_for is not None and wait_for.state == "running"
               and time.monotonic() < deadline):
            time.sleep(0.5)
        settings = _current.get("settings") or store.settings()
        if immediate:
            with _lock:
                _campaign["restarts"] = 0
            pause = delay
        else:
            with _lock:
                _campaign["restarts"] = int(_campaign.get("restarts") or 0) + 1
                n = _campaign["restarts"]
            most = max(1, int(float(settings.get("campaign_max_restarts") or 5)))
            if n > most:
                with _lock:
                    _campaign["state"] = "failed"
                    _campaign["note"] = (f"Restarted {most} times and it kept going wrong "
                                         f"({reason}). Press Resume to try again.")
                _persist(force=True)
                log("campaign", f"gave up after {most} restarts - {reason}", "error")
                return
            pause = max(delay, min(120.0, 15.0 * n))
            with _lock:
                _campaign["note"] = f"Restart {n} of {most} ({reason}) in {int(pause)} sec."
            _persist(force=True)
        time.sleep(pause)
        # Ended by the operator while this was waiting: their call stands.
        if _campaign["state"] != "restarting":
            return
        try:
            _resume_run(reason, settings)
        except Exception as err:                     # noqa: BLE001 - said on the panel
            detail = getattr(err, "detail", None) or str(err) or err.__class__.__name__
            with _lock:
                _campaign["state"] = "failed"
                _campaign["note"] = f"Could not restart: {detail} Press Resume to try again."
            _persist(force=True)
            log("campaign", f"could not restart - {detail}", "error")

    with _lock:
        _campaign["state"] = "restarting"
    threading.Thread(target=go, name="campaign-restart", daemon=True).start()


def _resume_run(reason: str, settings: dict[str, Any]) -> tasks.Task:
    """Carry the saved campaign on from its first pending row.

    Opens any window that is not up - a server restart takes them all - claims
    the accounts, and starts a run on the SAME lanes with the counters it had,
    so the day's tallies, the break plan and the actions plan carry on rather
    than starting over.
    """
    with _lock:
        left = _recover_rows(_campaign["lanes"])
        lanes = list(_campaign["lanes"])
        saved = dict(_campaign.get("run") or {})
    if not left:
        with _lock:
            _campaign["state"] = "done"
            _campaign["note"] = "Finished - nothing was left to send."
        _persist(force=True)
        raise fail("Nothing is left to send.")

    ids = [str(lane["id"]) for lane in lanes]
    names = {str(lane["id"]): lane["account"] for lane in lanes}
    for account_id in ids:
        if automation.is_open(account_id):
            continue
        with _lock:
            _campaign["note"] = f"Opening the window on {names[account_id]}."
        _persist(force=True)
        try:
            automation.open_account(account_id, messenger.HOME_URL, timeout=30_000)
        except Exception as err:                     # noqa: BLE001 - a slow load is not a closed window
            if "Timeout" not in str(err):
                raise
    busy.claim(ids, "campaign", names=names)

    with _lock:
        _campaign["state"] = "running"
        _campaign["note"] = f"Resumed ({reason}) - {left} to go."
    _current["settings"] = settings
    log("campaign", f"resumed ({reason}) - {left} to go", "warn")
    task = tasks.start("campaign", lambda t: _run(t, settings, saved=saved), source="campaign")
    busy.bind(ids, task.id)
    with _lock:
        _campaign["task"] = task.id
    task.set(result=_state())
    _persist(force=True)
    return task


def _watchdog(task: tasks.Task, run: "_Run", stall: float) -> None:
    """Notice a run that has stopped moving, and restart it from where it is.

    Progress is a row starting, ending or moving in the pool; a break, a rest
    and the gap are waits the run chose, and are not counted against it. A run
    stuck inside a page call cannot see a stop, so the windows are closed under
    it - the call fails, the run unwinds, and the restart opens fresh ones.
    """
    minutes = max(1, int(round(stall / 60)))
    while task.state in ("running", "paused"):
        time.sleep(5)
        if task.state != "running" or run.restarting:
            continue
        now = time.monotonic()
        if now < max(run.waiting_until, run._next_send) + 30:
            run.last_progress = max(run.last_progress, now - 1)
            continue
        if now - run.last_progress < stall:
            continue
        run.restarting = True
        log("campaign", f"no progress for {minutes} min - restarting from where it was", "warn")
        with _lock:
            _campaign["state"] = "restarting"
            _campaign["note"] = f"Stuck for {minutes} min - restarting from where it was."
        _persist(force=True)
        task.stop()
        deadline = time.monotonic() + 30
        while task.state == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
        if task.state == "running":
            with _lock:
                ids = [str(lane["id"]) for lane in _campaign["lanes"]]
            for account_id in ids:
                try:
                    automation.close_account(account_id)
                except Exception:                    # noqa: BLE001 - going anyway
                    pass
        _schedule_restart("stalled", wait_for=task)
        return


# -- Drafts --------------------------------------------------------
@router.get("/api/messages")
def messages() -> dict[str, Any]:
    return {"drafts": store.read()["drafts"]}


@router.post("/api/messages")
def save_messages(body: dict = Body(default={})) -> dict[str, Any]:
    drafts = [
        {
            "name": str(d.get("name") or "draft"),
            "text": str(d.get("text") or ""),
            "enabled": d.get("enabled") is not False,
        }
        for d in (body.get("drafts") or [])
    ]
    with store.edit() as db:
        db["drafts"] = drafts
    log("data", f"saved {len(drafts)} drafts")
    return {"drafts": drafts}


@router.get("/api/messages/minio")
def messages_in_bucket() -> dict[str, Any]:
    """The drafts on file in the messages bucket - `sms_drafts.txt`, the file
    the insta bot writes, read straight off MinIO. Nothing is stored here: the
    Database option on the Messages step shows what is in the bucket, and Save
    drafts is what puts a copy in the store."""
    found = objects.read_drafts()
    if not found.get("ok"):
        raise fail(found.get("reason") or "MinIO refused the read.", 502)
    return found


@router.post("/api/messages/minio")
def push_messages(body: dict = Body(default={})) -> dict[str, Any]:
    """Put drafts back into the bucket - the ones sent, or what is stored.

    The other half of the round trip. Without it the bucket is a thing this
    console reads from and never writes to, and the drafts an operator edits here
    would be lost the next time it read.
    """
    drafts = body.get("drafts")
    if not drafts:
        drafts = store.read()["drafts"]
    sent = objects.write_drafts(drafts)
    if not sent.get("ok"):
        raise fail(sent.get("reason") or "MinIO refused the write.", 502)
    log("data", f"sent {len(drafts)} drafts to {sent.get('bucket')}")
    return {**sent, "count": len(drafts)}


@router.post("/api/messages/parse")
async def parse_messages(file: UploadFile = File(...)) -> dict[str, Any]:
    """Blank-line separated, because a DM has line breaks in it.

    One-per-line would cut every multi-paragraph message into pieces, and the
    drafts worth importing in bulk are exactly the long ones.
    """
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    drafts = [
        {"name": f"imported {i + 1}", "text": chunk.strip(), "enabled": True}
        for i, chunk in enumerate(re.split(r"\r?\n\s*\r?\n", text))
        if chunk.strip()
    ]
    return {"drafts": drafts}


# -- Media ---------------------------------------------------------
@router.get("/api/media")
def media() -> dict[str, Any]:
    return {"media": store.read()["media"]}


@router.post("/api/media")
async def upload_media(files: list[UploadFile] = File(...)) -> dict[str, int]:
    config.ensure_dirs()
    added = 0

    for upload in files:
        name = Path(upload.filename or "image").name
        if Path(name).suffix.lower() not in IMAGE_TYPES:
            raise fail(f"{name} is not an image Messenger will accept.")
        raw = await upload.read()
        if len(raw) > MAX_IMAGE:
            raise fail(f"{name} is larger than Messenger will take on a message.")

        # Written to disk under its own name rather than kept as a data URI: the
        # composer is fed a FILE, so an image that only existed as base64 in a
        # JSON store would have to be written out at send time anyway.
        target = config.MEDIA / name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            target = config.MEDIA / f"{stem}-{n}{suffix}"
            n += 1
        target.write_bytes(raw)

        with store.edit() as db:
            db["media"] = [m for m in db["media"] if m["name"] != target.name]
            db["media"].append({
                "name": target.name,
                "size": len(raw),
                "url": f"/api/media/file/{target.name}",
                "at": now(),
            })
        added += 1

    log("data", f"uploaded {added} image(s)")
    return {"added": added}


@router.get("/api/media/minio")
def media_in_bucket() -> dict[str, Any]:
    """What pictures the media bucket holds, and which are already in the library."""
    found = objects.list_media()
    if not found.get("ok"):
        raise fail(found.get("reason") or "MinIO refused the read.", 502)
    have = {m["name"] for m in store.read()["media"]}
    for item in found["media"]:
        item["in_library"] = item["name"] in have
    return found


@router.post("/api/media/minio/pull")
def pull_media(body: dict = Body(default={})) -> dict[str, Any]:
    """Copy the bucket's pictures into the library - all of them, or the names given.

    Copied rather than streamed at send time: the composer is fed a FILE, so a
    picture has to be on disk before a campaign can attach it, and a bucket
    that is down mid-run must not be what stops a send.
    """
    found = objects.list_media()
    if not found.get("ok"):
        raise fail(found.get("reason") or "MinIO refused the read.", 502)
    wanted = {str(n) for n in (body.get("names") or [])}
    items = [m for m in found["media"] if not wanted or m["name"] in wanted]
    if not items:
        raise fail("Nothing in the bucket to pull.")

    config.ensure_dirs()
    pulled, skipped = 0, 0
    for item in items:
        name = Path(item["name"]).name
        if Path(name).suffix.lower() not in IMAGE_TYPES:
            skipped += 1
            continue
        raw = objects.get_media(item["name"])
        if raw is None or len(raw) > MAX_IMAGE:
            skipped += 1
            continue
        target = config.MEDIA / name
        target.write_bytes(raw)
        with store.edit() as db:
            db["media"] = [m for m in db["media"] if m["name"] != name]
            db["media"].append({
                "name": name, "size": len(raw),
                "url": f"/api/media/file/{name}", "at": now(),
                "source": found.get("bucket"),
            })
        pulled += 1
    log("data", f"pulled {pulled} image(s) from {found.get('bucket')}"
                + (f", {skipped} skipped" if skipped else ""))
    return {"pulled": pulled, "skipped": skipped, "bucket": found.get("bucket")}


@router.get("/api/media/file/{name}")
def media_file(name: str) -> FileResponse:
    """Serve one stored image back to the library grid.

    `Path(name).name` and the existence check together are the whole of the path
    handling: the name arrives from a URL, and `../../` in it must resolve to a
    404 rather than to a file.
    """
    path = config.MEDIA / Path(name).name
    if not path.is_file():
        raise fail(f"{name} is not in the library.", 404)
    return FileResponse(path)


# -- Planning the send ---------------------------------------------
def _dealer(items: list) -> Any:
    """Draw at random, but from a bag that empties before it refills.

    Plain `random.choice` per recipient is random and nothing else: over a
    hundred recipients one draft lands forty times and another twice. That
    matters here for two reasons - the drafts are ALTERNATIVES the sender
    rotates between, so an uneven split is a worse test of which one works, and
    four hundred DMs weighted onto one text is the pattern the rotation exists
    to avoid.

    So: shuffle, deal the whole set, shuffle again. Every draft is used the same
    number of times give or take one, and the ORDER is unpredictable - which is
    the half of "random" that actually matters against a machine watching for a
    repeating pattern.
    """
    bag: list = []

    def draw():
        nonlocal bag
        if not items:
            return None
        if not bag:
            bag = list(items)
            random.shuffle(bag)
        return bag.pop()

    return draw


def _room_today(accounts: list[dict], settings: dict, counters: dict) -> dict[str, int]:
    """How many more DMs each account may send today.

    The cap is per account and Messenger counts it per account, so this is the real
    limit on a lane - not a preference. An account that has already sent 38 of
    its 40 can take two more recipients, however many are waiting.
    """
    cap = int(settings["per_account_dm_cap"])
    spent = counters.get("per_account") or {}
    return {str(a["id"]): max(0, cap - int(spent.get(str(a["id"]), 0)))
            for a in accounts}


def _assign_lanes(lanes: list[dict], count: int, room: dict[str, int],
                  rng: Optional[random.Random] = None) -> list[int]:
    """Which lane each recipient in turn belongs to.

    By ROOM LEFT, not in equal shares. Equal shares look fairer and are worse:
    the run enforces the per-account cap at send time, so a hundred recipients
    split 34/33/33 across three accounts - one of which has five sends left
    today - queues twenty-nine DMs that can only come back "at its cap for
    today". Dealing by room spends the capacity that exists.

    With nothing sent yet the rooms are equal and this IS round-robin, one each
    in turn, which is what it should be: the lanes only pull apart once the
    accounts actually differ.

    Recipients past the total room left are still dealt, round-robin, rather than
    dropped. They will fail at send time with a reason on the row - and a
    recipient visibly failing for a stated reason is better than one that
    silently never appeared on the board at all.
    """
    left = [room.get(lane["id"], 0) for lane in lanes]
    out: list[int] = []
    for index in range(count):
        # Most room first; a tie goes at random between the lanes that share
        # it, so three accounts with the same room left get the list dealt
        # between them in no fixed order - the `rng` is seeded from the
        # request, so the preview and the run still deal alike.
        if rng is not None:
            best = max(range(len(lanes)), key=lambda j: (left[j], rng.random()))
        else:
            best = max(range(len(lanes)), key=lambda j: (left[j], -j))
        if left[best] <= 0:
            out.append(rng.randrange(len(lanes)) if rng is not None else index % len(lanes))
            continue
        left[best] -= 1
        out.append(best)
    return out


def _plan_lanes(body: dict, db: dict) -> tuple[list[dict], list[dict]]:
    """Who gets what, from whom - without sending anything.

    Shared by `/plan` and `/start` so the run cannot disagree with the preview
    that was approved: the same code deals the same rows, and the only thing
    `/start` adds is a task to walk them.

    Returns the lanes and the accounts they belong to.
    """
    account_ids = [str(i) for i in (body.get("accounts") or [])]
    # Through the same parser the upload uses, so a numeric id or a pasted
    # profile link typed straight into the recipient list is addressed the
    # same way a stored handle is.
    recipients = [messenger.handle_of(str(h)) for h in (body.get("recipients") or []) if str(h).strip()]
    recipients = [h for h in recipients if h]
    if not account_ids:
        raise fail("Pick an account to send from.")
    if not recipients:
        raise fail("No recipients.")

    accounts = [find_account(i) for i in account_ids]

    # The drafts the console is showing, TEXT AND ALL. They used to come as
    # names looked up in the store, and a draft that lived only in the MinIO
    # bucket - which is where most of them live - was quietly dropped by that
    # lookup, leaving whatever the store happened to hold to be dealt to every
    # recipient. A bare name is still accepted and still looked up.
    drafts_by_name = {d["name"]: d for d in db["drafts"]}
    wanted: list[dict] = []
    if not body.get("skip_messages"):
        for given in body.get("drafts") or []:
            if isinstance(given, dict):
                name = str(given.get("name") or "").strip()
                text = str(given.get("text") or "")
                if not text and name in drafts_by_name:
                    text = str(drafts_by_name[name].get("text") or "")
                if name and text.strip():
                    wanted.append({"name": name, "text": text})
            elif given in drafts_by_name:
                wanted.append(drafts_by_name[given])
    if not body.get("skip_messages") and not wanted:
        raise fail("No draft selected - tick Skip messages to send without one.")

    media_names = [] if body.get("skip_media") else list(body.get("media") or [])
    library = {m["name"]: m for m in db["media"]}
    missing = [n for n in media_names if n not in library]
    if missing:
        raise fail(f"{missing[0]} is no longer in the media library.")

    store_by_handle = {row["handle"]: row for row in db["store"]}
    draw_draft = _dealer(wanted)
    draw_media = _dealer(media_names)

    lanes = [{"account": "@" + a["login"], "id": str(a["id"]), "rows": []} for a in accounts]
    # Dealt at RANDOM across the accounts, within the room each has left in
    # the day - see `_assign_lanes`. The recipients are shuffled first, so
    # which account a given handle lands on, and the order each account works
    # through, are both unpredictable. Seeded from the request rather than
    # the clock, because `/plan` and `/start` must deal the same rows: the
    # preview the operator approved has to be the run that goes out.
    rng = random.Random("|".join(account_ids) + "#" + "|".join(recipients))
    recipients = list(recipients)
    rng.shuffle(recipients)
    room = _room_today(accounts, store.settings(), store.counters())
    where = _assign_lanes(lanes, len(recipients), room, rng)

    for index, handle in enumerate(recipients):
        known = store_by_handle.get(handle, {})
        draft = draw_draft()
        lane = lanes[where[index]]
        lane["rows"].append({
            "handle": handle,
            "name": known.get("name") or "",
            # The NAME rides on the row and the text with it: the console
            # shows which draft a recipient got, and the rendered text is
            # what the tooltip holds.
            "draft": draft["name"] if draft else "",
            "text": messenger.render(draft["text"], {"handle": handle, "name": known.get("name")})
                    if draft else "",
            "media": draw_media() or "",
            "status": "pending",
            # Whether this profile turned out to have a chat at all. Empty until
            # the run reaches it, then "open" or "none" - the Chat column reads
            # this, so the table says which handles are reachable and which are
            # not without the operator opening a single profile by hand.
            "chat": "",
            "why": "",
        })
    return lanes, accounts


@router.post("/api/campaign/plan")
def plan(body: dict = Body(default={})) -> dict[str, Any]:
    """The whole send, dealt out and shown, with nothing sent.

    What the Campaign live panel opens on. Every recipient with the draft and
    the picture they would get and a status of `pending` - which is the last
    chance to see that four hundred DMs are about to go out with the wrong text
    attached, at the point where the answer is still to close the panel.

    It does not touch `_campaign`, so opening it during a run shows the plan
    without disturbing the run - and `/status` is what the panel reads once
    something IS running.
    """
    if _campaign["state"] == "running":
        # A run owns the panel while it lasts: its rows have real statuses on
        # them, and replacing those with a fresh set of `pending` would be
        # throwing away the only record of what has already gone out.
        return _state()

    lanes, accounts = _plan_lanes(body, store.read())
    total = sum(len(lane["rows"]) for lane in lanes)

    # What today actually has room for. Said here rather than discovered one
    # failed row at a time once the run is under way.
    settings = store.settings()
    counters = store.counters()
    room = sum(_room_today(accounts, settings, counters).values())
    room = min(room, max(0, int(settings["daily_dm_cap"]) - int(counters.get("total", 0))))
    over = max(0, total - room)

    note = (f"{total} recipients dealt across {len(accounts)} account"
            f"{'' if len(accounts) == 1 else 's'} - nothing sent yet.")
    if over:
        note += (f" {over} of them are past today's cap and will be marked "
                 f"failed rather than sent.")
    return {"state": "planned", "lanes": lanes, "note": note}


# -- The send ------------------------------------------------------
@router.post("/api/campaign/start")
def start(body: dict = Body(default={})) -> dict[str, Any]:
    if _campaign["state"] == "running":
        raise fail("A campaign is already running.")

    db = store.read()
    settings = store.settings()
    lanes, accounts = _plan_lanes(body, db)

    # Checked here as well as on the wizard's checklist. The checklist is a
    # courtesy; this is the thing that cannot be got past, and a send that
    # started against a closed window would fail on every single recipient.
    closed = [a for a in accounts if not automation.is_open(str(a["id"]))]
    if closed:
        raise fail("No browser window is open on @" + closed[0]["login"] + ".")
    account_ids = [str(a["id"]) for a in accounts]
    busy.claim(account_ids, "campaign", names={str(a["id"]): "@" + str(a["login"]) for a in accounts})

    total = sum(len(lane["rows"]) for lane in lanes)
    with _lock:
        _campaign["lanes"] = lanes
        _campaign["state"] = "running"
        _campaign["note"] = f"{total} recipients queued."
        _campaign["restarts"] = 0
        _campaign["run"] = {}
    _current["run"] = None
    _current["settings"] = settings

    log("campaign", f"started - {total} recipients across {len(accounts)} account(s)")
    task = tasks.start("campaign", lambda t: _run(t, settings), source="campaign")
    busy.bind(account_ids, task.id)
    _campaign["task"] = task.id
    task.set(result=_state())
    _persist(force=True)
    return {"task": task.id, "lanes": _state()["lanes"]}


def _order() -> list[tuple[dict, dict]]:
    """Round-robin across the lanes: lane 0 row 0, lane 1 row 0, lane 0 row 1...

    Lane by lane would run one account's whole day's worth back to back and only
    then start the second. Interleaving spreads the same work across the
    accounts, which is both faster in wall-clock and the less peculiar pattern of
    the two.
    """
    with _lock:
        lanes = list(_campaign["lanes"])
    # Recipients only. On a resumed run the lanes already carry the planned
    # breaks and actions turns, and those are not things to send to.
    people = [[r for r in lane["rows"] if not r.get("kind")] for lane in lanes]
    out: list[tuple[dict, dict]] = []
    depth = max((len(rows) for rows in people), default=0)
    for index in range(depth):
        for lane, rows in zip(lanes, people):
            if index < len(rows):
                out.append((lane, rows[index]))
    return out


class _Halt(Exception):
    """The run has to end here, and the reason is already on the campaign.

    Raised by `_Run.trouble` for a challenge with Stop On Challenge set. It
    exists so the loops do not each have to unwind themselves: whichever one is
    running simply stops, its `finally` closes whatever tabs it was holding, and
    `_run` writes the ending once.
    """


class _Run:
    """One campaign's bookkeeping, shared by both send loops.

    There are two loops because a pool of one tab and a pool of five want
    genuinely different things from Messenger - at one tab the fast path is
    clicking the thread in the list that is already on screen, and a freshly
    opened tab has no list in it to click. What the two must NOT differ on is
    what a finished row MEANS: the day's caps, the tallies, what gets written
    back to the store, what the table says. All of that is here and is written
    once, so a change to the pool cannot quietly give the serial run a different
    idea of how many messages have gone out today.
    """

    def __init__(self, task: tasks.Task, queue: list[tuple[dict, dict]],
                 settings: dict[str, Any], saved: Optional[dict] = None) -> None:
        self.task = task
        self.queue = queue
        self.total = len(queue)
        self.counters = store.counters()
        self.sent = 0
        # For the watchdog: when something last happened, how long the run has
        # chosen to wait, and whether a restart is already under way.
        self.last_progress = time.monotonic()
        self.waiting_until = 0.0
        self.restarting = False

        self.daily_cap = int(settings["daily_dm_cap"])
        self.per_account_cap = int(settings["per_account_dm_cap"])
        self.batch_size = int(settings["batch_size"])
        self.batch_pause = float(settings["batch_pause_minutes"]) * 60
        # What a break is spent on: each account on the run joining this many
        # groups from Discover before the wait, if any wait is left.
        self.break_groups = max(0, int(float(settings.get("break_groups") or 0)))
        self.breaks = 0
        self.gap = tuple(sorted((float(settings["gap_min_seconds"]),
                                 float(settings["gap_max_seconds"]))))
        # Every so many messages - rolled from this range, and rolled again each
        # time - every account presses its Messenger rail items before sending
        # goes on. (0, 0) never does. `_actions_at` is the send count it next
        # happens at.
        self.actions_every = tuple(sorted((
            max(0, int(float(settings.get("actions_min_messages") or 0))),
            max(0, int(float(settings.get("actions_max_messages") or 0))))))
        # What one turn is: this many of the rail items, picked at random, and
        # then this long a rest before the next message goes.
        most = len(MESSENGER_ACTIONS)
        self.actions_items = tuple(sorted((
            min(most, max(1, int(float(settings.get("actions_min_items") or 2)))),
            min(most, max(1, int(float(settings.get("actions_max_items") or 3)))))))
        self.actions_rest = tuple(sorted((
            max(0.0, float(settings.get("actions_min_rest_minutes") or 0)) * 60,
            max(0.0, float(settings.get("actions_max_rest_minutes") or 0)) * 60)))
        self.actions_done = 0
        if saved:
            # A resumed run: the counters it had, the plan it rolled - the
            # rows on the lanes already carry the planned turns and breaks.
            self.sent = int(saved.get("sent") or 0)
            self.breaks = int(saved.get("breaks") or 0)
            self.actions_done = int(saved.get("actions_done") or 0)
            self.actions_plan = [dict(t) for t in (saved.get("actions_plan") or [])
                                 if isinstance(t, dict) and t.get("at")]
            self._actions_at = self._next_turn_at()
        else:
            # Rolled ONCE, up front - where each turn falls, which items it
            # presses and how long it rests - so the table can show the whole
            # plan before the run gets there. `_actions_at` is the next one's
            # send count.
            self.actions_plan = self._plan_actions()
            self._actions_at = self.actions_plan[0]["at"] if self.actions_plan else 0
            self._plan_rows()
        self.typing_wpm = int(settings["typing_wpm"])
        self.click_send = bool(settings["click_send"])
        self.chat_settle = float(settings["chat_open_seconds"])
        self.stop_on_challenge = bool(settings["stop_on_challenge"])
        self.template = str(settings["profile_url"])
        self.parallel = max(1, int(settings.get("parallel_tabs") or 1))
        # Off by default, and the default is the one that cannot lose anything:
        # a tab left open can always be closed afterwards, and a chat closed on
        # a wrong verdict cannot be got back.
        self.close_after_send = bool(settings.get("close_tab_after_send"))

        # When the next message may leave. The gap is spent HERE rather than
        # between recipients, and that is the whole of what keeps a pooled run
        # honest: five tabs load at once, but they still send one at a time and
        # still wait out the gap between two sends. Raising the pool makes the
        # run finish sooner; it does not make the account send faster.
        self._next_send = 0.0

    # -- What the table says --------------------------------------
    def done_count(self) -> int:
        return sum(1 for _, row in self.queue
                   if row["status"] in ("sent", "typed", "failed"))

    def remaining(self) -> bool:
        return self.done_count() < self.total

    def snapshot(self) -> dict[str, Any]:
        """What a restart needs to carry on: the counters and the plan."""
        return {"sent": self.sent, "breaks": self.breaks, "actions_done": self.actions_done,
                "actions_plan": [dict(t) for t in self.actions_plan]}

    def touch(self) -> None:
        self.last_progress = time.monotonic()

    def wait(self, seconds: float) -> None:
        """A wait the run chose - a break, a rest - which the watchdog must not count."""
        self.waiting_until = time.monotonic() + seconds
        try:
            self.task.checkpoint(seconds)
        finally:
            self.waiting_until = 0.0
            self.touch()

    def report(self) -> None:
        self.touch()
        finished = self.done_count()
        self.task.set(
            progress=finished / self.total if self.total else 1.0,
            detail=f"{finished} of {self.total}",
            result=_state(),
        )

    # -- The day's budget -----------------------------------------
    def blocked(self, lane: dict, row: dict) -> bool:
        """Is this recipient past a cap? Marks the row and answers.

        A cap of 0 sends nothing. It is read literally rather than as
        "unlimited", because the two readings differ by a whole campaign and
        only one of them is safe to get wrong.
        """
        account_id = str(lane["id"])
        if self.counters["total"] >= self.daily_cap:
            row["status"] = "failed"
            row["why"] = "the day's cap was reached"
            self.report()
            return True
        if self.counters["per_account"].get(account_id, 0) >= self.per_account_cap:
            row["status"] = "failed"
            row["why"] = f"{lane['account']} is at its cap for today"
            self.report()
            return True
        return False

    # -- How a row ends -------------------------------------------
    def won(self, lane: dict, row: dict, outcome: str) -> None:
        """A chat that opened, and a message that went into it."""
        account_id = str(lane["id"])
        handle = row["handle"]
        row["chat"] = "open"
        row["status"] = outcome                # "sent", or "typed" with Click Send off
        row["why"] = "chat available" if self.click_send else "chat available - not sent"
        self.sent += 1

        if outcome == "sent":
            # Only a real send spends the day's budget. Messenger counts messages
            # it delivered, and a dry first run with Click Send off would
            # otherwise burn the cap it exists to protect - leaving the operator
            # to discover at the real run that the day is gone.
            spent_today = self.counters["per_account"].get(account_id, 0)
            self.counters["total"] += 1
            self.counters["per_account"][account_id] = spent_today + 1
            store.count_dm(account_id)

        with store.edit() as db:
            for stored in db["store"]:
                if stored["handle"] == handle:
                    # A tally, not a flag: the recipients picker filters on
                    # "already messaged twice", and a boolean cannot answer
                    # that. Read BEFORE the flag below is set - the seed is what
                    # was true on the way in, and reading it after would score a
                    # first send as a second one. Seeded off `messaged` so a
                    # handle marked before sends were counted reads as one send
                    # rather than none.
                    seen = int(stored.get("msg_count") or 0) or (
                        1 if stored.get("messaged") else 0)
                    # Only a real send marks the handle. With Click Send off
                    # nothing was delivered, and a store that said otherwise
                    # would quietly exclude them from the next campaign.
                    stored["messaged"] = stored.get("messaged") or self.click_send
                    if self.click_send:
                        stored["msg_count"] = seen + 1
                    stored["last_at"] = now()
                    break

        log("campaign", f"{outcome} -> @{handle}")
        self.report()

    def no_chat(self, row: dict, why: str = "no chat available") -> None:
        """The thread came up and there was nothing to type into.

        Not an error and not worth a slot out of the day's budget - it is
        written on the row and the run moves straight on.

        `why` is the EVIDENCE, not a category. A run that reported every one of
        these as "no chat available" said the same sentence about a handle
        Messenger had never heard of, an account that had walled the sender, and
        a thread that was merely slow - which left nothing in the log to act on.
        """
        row["chat"] = "none"
        row["status"] = "failed"
        row["why"] = why
        log("campaign", f"@{row['handle']}: {why}", "warn")
        # NOT written back to the store as `dm_open: False`. A composer that was
        # not found is not the same as an inbox that is shut - a slow render says
        # the same thing as a closed inbox - and a wrong False here is permanent:
        # it would drop the handle from every future campaign without opening it
        # again. The row says what this run saw; the store keeps what a profile
        # read actually established.
        self.report()

    def trouble(self, row: dict, err: Exception) -> None:
        """Whatever went wrong, on the row and in the log. Never swallowed.

        Raises `_Halt` for a challenge with Stop On Challenge set, because
        carrying on through a captcha means submitting actions that all silently
        fail, which is what escalates a challenge into a lock.
        """
        handle = row["handle"]
        row["status"] = "failed"
        row["why"] = str(err) or err.__class__.__name__

        if isinstance(err, DmClosed):
            row["chat"] = "none"
            log("campaign", f"@{handle}: {err}", "warn")
            # Learned the hard way, so the next campaign does not spend a slot
            # finding it out again.
            with store.edit() as db:
                for stored in db["store"]:
                    if stored["handle"] == handle:
                        stored["dm_open"] = False
                        break
        else:
            log("campaign", f"@{handle}: {row['why']}", "error")

        self.report()
        if (isinstance(err, AutomationError) and self.stop_on_challenge
                and _is_challenge(err)):
            raise _Halt(str(err))

    # -- Pacing ---------------------------------------------------
    def due(self) -> bool:
        """May a message go out yet?"""
        return time.monotonic() >= self._next_send

    def pace(self) -> None:
        """Spend the gap, or the batch pause when one is owed.

        The serial loop calls this between recipients and blocks in it. The
        pooled loop calls the same thing and blocks in it too - the tabs it is
        holding simply sit on their loaded threads while it does, which is what
        a pause is for.
        """
        if not self.remaining():
            return
        if self._actions_at and self.sent >= self._actions_at:
            self.take_actions()
            self._actions_at = self._next_turn_at()
        # Counted by breaks TAKEN rather than `sent % batch_size`, so a resumed
        # run whose break was due at the very count it stopped on still takes
        # it, instead of sailing past to the next multiple.
        if (self.batch_size and self.sent >= self.batch_size * (self.breaks + 1)
                and (self.batch_pause > 0 or self.break_groups > 0)):
            self.take_break()
            self._next_send = time.monotonic()
            return
        self._next_send = time.monotonic() + random.uniform(*self.gap)

    # -- The break ------------------------------------------------
    def take_break(self) -> None:
        """A batch is done: join some groups on every account, then wait.

        The joining happens INSIDE the pause, not on top of it. A twelve-minute
        break with five groups to join is twelve minutes long: the groups take
        what they take, and whatever is left is waited out. The campaign's
        windows are not disturbed - each account joins from a tab of its own,
        opened for the purpose and closed after, so the chats it was writing
        to are exactly where it left them when the run comes back.

        Every account joins at once, each on its own lane, because that is the
        shape of the run itself. One account's tab that will not open, or a
        Discover page with nothing on it, is logged against that account and
        the break goes on for the others.
        """
        self.breaks += 1
        minutes = int(round(self.batch_pause / 60))
        started = time.monotonic()
        doing = f"break {self.breaks}"
        if self.break_groups:
            doing += f": joining {self.break_groups} groups on each account"
        if minutes:
            doing += f", then back in {minutes} min"
        what = f"Batch of {self.batch_size} done - " + doing
        with _lock:
            ids = [(str(lane["id"]), lane["account"]) for lane in _campaign["lanes"]]
            # The row planned for this break, now the break it is.
            rows = {str(lane["id"]): self._claim_row(lane, "break", self.sent)
                    for lane in _campaign["lanes"]}
            for row in rows.values():
                row["status"] = "processing"
                row["why"] = doing
            _campaign["note"] = what + "."
        self.task.set(detail=f"break {self.breaks}", result=_state())
        log("campaign", what.lower())

        tally: dict[str, int] = {}
        try:
            if self.break_groups:
                tally = self._join_groups(ids)
                # Stop pressed during the joining: the threads saw it and came
                # back; this is where the run itself hears it.
                self.task.checkpoint(0)

            rest = max(0.0, self.batch_pause - (time.monotonic() - started))
            if rest > 1:
                back = max(1, int(round(rest / 60)))
                with _lock:
                    _campaign["note"] = f"Break {self.breaks} - back in {back} min."
                    for row in rows.values():
                        row["why"] = f"resting - back in {back} min"
                self.task.set(detail=f"paused for {back} min", result=_state())
                self.wait(rest)
        except tasks.Stopped:
            with _lock:
                for row in rows.values():
                    row["status"] = "failed"
                    row["why"] = "stopped part-way"
            raise
        with _lock:
            for lane_id, row in rows.items():
                said = []
                if self.break_groups:
                    said.append(f"joined {tally.get(lane_id, 0)} of {self.break_groups} groups")
                if minutes:
                    said.append(f"rested {minutes} min")
                row["status"] = "sent"
                row["why"] = "; ".join(said) or "done"
            _campaign["note"] = f"Back from break {self.breaks} - sending."
        self.task.set(result=_state())
        log("campaign", f"back from break {self.breaks}")

    def _join_groups(self, ids: list[tuple[str, str]]) -> dict[str, int]:
        """Join the break's groups on every account at once. Returns how many each did."""
        want = self.break_groups
        tally: dict[str, int] = {}

        def one(account_id: str, who: str) -> None:
            def on_join(done: dict) -> None:
                tally[account_id] = int(done.get("joined") or 0)
                log("campaign",
                    f'{who}: {"joined" if done["ok"] else "could not join"} '
                    f'"{done["name"]}" ({done["joined"]} of {done["want"]}) - {done["note"]}',
                    "info" if done["ok"] else "warn")
                with _lock:
                    _campaign["note"] = (f"Break {self.breaks}: " + ", ".join(
                        f"{w} {tally.get(i, 0)}/{want}" for i, w in ids) + " groups joined.")
                self.task.set(result=_state())

            def in_own_tab(page):
                # A tab of its own, so the chats on this window are untouched.
                extra = page.context.new_page()
                try:
                    return facebook.join_groups(
                        extra, want=want, on_join=on_join,
                        checkpoint=self.task.checkpoint)
                finally:
                    try:
                        extra.close()
                    except Exception:                # noqa: BLE001 - already gone
                        pass
                    try:
                        page.bring_to_front()
                    except Exception:                # noqa: BLE001 - window gone
                        pass

            try:
                automation.with_page(account_id, in_own_tab, timeout=600)
            except tasks.Stopped:
                pass
            except Exception as err:                 # noqa: BLE001 - one account's break, not the run
                log("campaign", f"{who}: could not join groups on the break - {err}", "warn")

        threads = [threading.Thread(target=one, args=(aid, who), name=f"break-{aid}",
                                    daemon=True) for aid, who in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log("campaign", "break: " + ", ".join(
            f"{who} joined {tally.get(aid, 0)} of {want}" for aid, who in ids))
        return tally

    # -- The Messenger actions ------------------------------------
    def _roll_actions(self, base: int) -> int:
        """The send count the rail items are next pressed at, or 0 for never."""
        low, high = self.actions_every
        if high <= 0:
            return 0
        return base + random.randint(max(1, low), high)

    def _roll_turn(self, at: int) -> dict[str, Any]:
        """One turn, decided now: which items, in rail order, and how long a rest."""
        count = random.randint(*self.actions_items)
        picked = set(random.sample(list(MESSENGER_ACTIONS), count))
        keys = [k for k in MESSENGER_ACTIONS if k in picked]
        low, high = self.actions_rest
        rest = int(round(random.uniform(low, high))) if high > 0 else 0
        return {"at": at, "keys": keys, "rest": rest}

    def _plan_actions(self) -> list[dict[str, Any]]:
        """Every turn the run will take, rolled now."""
        out: list[dict[str, Any]] = []
        at = self._roll_actions(0)
        while at and at < self.total:
            out.append(self._roll_turn(at))
            at = self._roll_actions(at)
        return out

    def _next_turn_at(self) -> int:
        """The send count of the next turn not yet taken, or 0 when none is left.

        By turns TAKEN, not by the send count: a resumed run whose turn was due
        at the very count it stopped on still owes that turn.
        """
        if self.actions_done < len(self.actions_plan):
            return int(self.actions_plan[self.actions_done]["at"])
        return 0

    def _turn_at(self, at: int) -> dict[str, Any]:
        """The planned turn for this send count - or a fresh one if none was."""
        for turn in self.actions_plan:
            if turn["at"] == at:
                return turn
        turn = self._roll_turn(at)
        self.actions_plan.append(turn)
        return turn

    @staticmethod
    def _rest_words(seconds: int) -> str:
        minutes = int(round(seconds / 60))
        return f"{minutes} min" if minutes else f"{int(seconds)} sec"

    def _planned_row(self, kind: str, at: int) -> dict:
        """A break or an actions turn as a row of the table, before it happens."""
        if kind == "action":
            turn = self._turn_at(at)
            name = "Messenger actions"
            doing = ", ".join(messenger.NAV_ACTIONS[k]["label"] for k in turn["keys"])
            if turn["rest"] > 0:
                doing += f", then rest {self._rest_words(turn['rest'])}"
        else:
            name = "Break"
            parts = []
            if self.break_groups:
                parts.append(f"join {self.break_groups} groups")
            minutes = int(round(self.batch_pause / 60))
            if minutes:
                parts.append(f"rest {minutes} min")
            doing = ", ".join(parts) or "pause"
        return {"kind": kind, "at": at, "handle": "", "name": name,
                "draft": "", "text": "", "media": "", "chat": "",
                "status": "pending", "why": f"after {at} messages: {doing}"}

    def _plan_rows(self) -> None:
        """Write every coming break and actions turn INTO the lanes, up front.

        The run counts sends across all accounts, and the queue is round-robin,
        so the N-th send is queue[N-1]. On each account's tab the event is put
        after the last of that account's rows at or before it - which is where
        it will fall if every message goes. A failed row shifts the real moment
        later, and `_claim_row` moves the planned row to where it actually
        happened when it does.
        """
        breaks: list[int] = []
        if self.batch_size and (self.batch_pause > 0 or self.break_groups > 0):
            breaks = list(range(self.batch_size, self.total, self.batch_size))
        events = sorted([(t["at"], "action") for t in self.actions_plan]
                        + [(n, "break") for n in breaks],
                        key=lambda e: (e[0], 0 if e[1] == "action" else 1))
        if not events:
            return
        with _lock:
            lanes = list(_campaign["lanes"])
            position = {id(row): i for lane in lanes for i, row in enumerate(lane["rows"])}
            # Per lane: (index of the row it follows, or -1 for the top; the row).
            after: dict[str, list[tuple[int, dict]]] = {str(l["id"]): [] for l in lanes}
            for n, kind in events:
                last: dict[str, int] = {}
                for lane, row in self.queue[:n]:
                    last[str(lane["id"])] = position[id(row)]
                for lane in lanes:
                    lid = str(lane["id"])
                    after[lid].append((last.get(lid, -1), self._planned_row(kind, n)))
            for lane in lanes:
                lid = str(lane["id"])
                rebuilt = [row for i, row in after[lid] if i < 0]
                for i, row in enumerate(lane["rows"]):
                    rebuilt.append(row)
                    rebuilt.extend(r for j, r in after[lid] if j == i)
                lane["rows"] = rebuilt
        log("campaign", "planned: " + ", ".join(
            f"{kind if kind == 'break' else 'actions'} after {n}" for n, kind in events))

    def _claim_row(self, lane: dict, kind: str, at: int) -> dict:
        """The planned row for this event, moved to where the event actually fell.

        Called under `_lock`. Planned for send N but happening after a later
        row (a failure in between does not count as a send), the row is lifted
        out and put back just after the last row this lane has dealt with. A
        turn with no planned row - the settings changed under a run, say - gets
        one made on the spot.
        """
        rows = lane["rows"]
        found = None
        for i, r in enumerate(rows):
            if r.get("kind") == kind and r.get("at") == at and r.get("status") == "pending":
                found = rows.pop(i)
                break
        if found is None:
            found = self._planned_row(kind, at)
        done = [i for i, r in enumerate(rows) if r.get("status") != "pending"]
        rows.insert((done[-1] + 1) if done else 0, found)
        return found

    def take_actions(self) -> None:
        """So many messages are out: press some of the Messenger rail, then rest.

        The turn was rolled when the run was planned - which items, how long a
        rest - so what happens is what the table has been showing. The items
        are pressed on each account's own window, all accounts at once, like
        the break's group joining; the campaign's chats live in their own tabs,
        so the rail is pressed on the page behind them. Then the run rests for
        the turn's time before the next message goes. The planned row on each
        account's tab is where the turn is reported, step by step.
        """
        self.actions_done += 1
        turn_no = self.actions_done
        turn = self._turn_at(self._actions_at)
        keys = list(turn["keys"])
        labels = [messenger.NAV_ACTIONS[k]["label"] for k in keys]
        rest = int(turn["rest"] or 0)
        with _lock:
            lanes = list(_campaign["lanes"])
            rows: dict[str, dict] = {}
            for lane in lanes:
                # The row planned for this turn, moved to where the turn fell.
                row = self._claim_row(lane, "action", self._actions_at)
                row["status"] = "processing"
                row["why"] = "pressing " + labels[0]
                rows[str(lane["id"])] = row
            names = ", ".join(lane["account"] for lane in lanes)
            what = (f"{self.sent} sent - Messenger actions {turn_no}: " + ", ".join(labels)
                    + f" on {names}")
            if rest > 0:
                what += f", then rest {self._rest_words(rest)}"
            _campaign["note"] = what + "."
        self.task.set(detail=f"messenger actions {turn_no}", result=_state())
        log("campaign", what.lower())

        said: dict[str, str] = {}
        ok: dict[str, bool] = {}

        def one(lane: dict) -> None:
            account_id = str(lane["id"])
            who = lane["account"]
            row = rows[account_id]
            pressed: list[str] = []
            failed: list[str] = []

            def press_all(page) -> None:
                for index, (key, label) in enumerate(zip(keys, labels)):
                    self.task.checkpoint()
                    row["why"] = f"pressing {label} ({index + 1} of {len(keys)})"
                    self.task.set(result=_state())
                    try:
                        result = messenger.click_action(page, key)
                        pressed.append(label)
                        log("campaign", f"{who}: {result} {label}")
                    except Exception as err:         # noqa: BLE001 - one item, not the set
                        failed.append(label)
                        log("campaign", f"{who}: could not press {label} - {err}", "warn")
                    if index + 1 < len(keys):
                        self.task.checkpoint(random.uniform(1.2, 3.0))
                # Leave the window on the chat list, where the run expects it.
                try:
                    messenger.close_overlay(page)
                except Exception:                    # noqa: BLE001 - tidying, not the run
                    pass

            try:
                automation.with_page(account_id, press_all, timeout=600)
            except tasks.Stopped:
                said[account_id], ok[account_id] = "stopped part-way", False
                return
            except Exception as err:                 # noqa: BLE001 - one account's turn, not the run
                log("campaign", f"{who}: Messenger actions failed - {err}", "warn")
                said[account_id] = str(err) or err.__class__.__name__
                ok[account_id] = False
                return
            words = f"pressed {', '.join(pressed)}" if pressed else "pressed nothing"
            if failed:
                words += f"; could not press {', '.join(failed)}"
            said[account_id], ok[account_id] = words, not failed

        threads = [threading.Thread(target=one, args=(lane,), name=f"actions-{lane['id']}",
                                    daemon=True) for lane in lanes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            # Stop pressed during the pressing: the threads saw it and came
            # back; this is where the run itself hears it.
            self.task.checkpoint(0)
            if rest > 1:
                back = self._rest_words(rest)
                with _lock:
                    for lane_id, row in rows.items():
                        row["why"] = f"{said.get(lane_id, 'pressed')} - resting, back in {back}"
                    _campaign["note"] = f"Messenger actions {turn_no} pressed - back in {back}."
                self.task.set(detail=f"resting {back}", result=_state())
                log("campaign", f"messenger actions {turn_no}: resting {back}")
                self.wait(rest)
        except tasks.Stopped:
            with _lock:
                for row in rows.values():
                    row["status"] = "failed"
                    row["why"] = "stopped part-way"
            raise

        with _lock:
            for lane_id, row in rows.items():
                row["status"] = "sent" if ok.get(lane_id) else "failed"
                row["why"] = said.get(lane_id, "pressed") + (
                    f"; rested {self._rest_words(rest)}" if rest > 1 else "")
            _campaign["note"] = f"Messenger actions {turn_no} done - sending."
        self.task.set(result=_state())
        log("campaign", f"messenger actions {turn_no} done")

    def files_for(self, row: dict) -> Optional[list[str]]:
        return [str(config.MEDIA / row["media"])] if row["media"] else None


# -- One tab at a time ---------------------------------------------
def _serial(run: _Run) -> None:
    """The original run, and what `parallel_tabs` at 1 still gets.

    Kept rather than folded into the pool because it has something the pool
    cannot have: `open_chat` CLICKS the thread's row in the list that is already
    on screen, which is one SPA route change instead of a page load. That is
    only available in a tab that has the list in it, and every tab the pool
    opens is blank.
    """
    for lane, row in run.queue:
        run.task.checkpoint()
        account_id = str(lane["id"])
        handle = row["handle"]

        # Already dealt with - a resumed run's rows from before it stopped.
        if row["status"] in ("sent", "typed", "failed"):
            continue
        if run.blocked(lane, row):
            continue

        # WHAT WAS PICKED IS WHAT IS TRIED. There used to be two gates here -
        # "this handle's DMs were closed last time" and "this handle is
        # protected" - and both failed a recipient before the profile was ever
        # opened, on a fact the store had recorded on some earlier run. That is
        # exactly backwards for a list the operator has just chosen by hand:
        # picking a handle IS the instruction to try it. Whether the chat is
        # reachable is now ANSWERED rather than assumed - the Chat column says
        # what was found, freshly, every time.
        row["status"] = "processing"
        run.report()

        text = row["text"] or ""
        files = run.files_for(row)

        def one(page, h=handle, t=text, f=files):
            """Open the chat if there is one, then write into it.

            Two answers, not one: `None` means the thread had no composer to
            open, which is a fact about the recipient and is what the Chat
            column shows. Anything else is what became of the message.
            """
            if not messenger.open_chat(page, h, template=run.template,
                                       settle=run.chat_settle):
                return None
            if not t and not f:
                # Nothing to write. The chat was opened, which was the whole of
                # the work asked for, so that is the outcome.
                return "typed"
            return messenger.write_dm(page, t, media=f, typing_wpm=run.typing_wpm,
                                      click_send=run.click_send)

        try:
            outcome = automation.with_page(account_id, one, timeout=900)
        except Exception as err:                     # noqa: BLE001 - one bad row is not the run
            run.trouble(row, err)
        else:
            if outcome is None:
                run.no_chat(row)
                continue
            run.won(lane, row, outcome)

        run.pace()


# -- Five tabs at a time -------------------------------------------
class _Slot:
    """One open tab, and the recipient it was opened for.

    `page` travels between the campaign's thread and the account's, and that is
    only safe because nothing here ever touches it directly: every call on it
    goes back through `automation.on_lane`, onto the one thread Playwright will
    accept it from.
    """

    __slots__ = ("lane", "row", "account", "handle", "page", "ready", "deadline",
                 "retried", "why", "walls", "pins", "text", "at", "written", "seen",
                 "bounced_at")

    def __init__(self, lane: dict, row: dict, page: Any, deadline: float) -> None:
        self.lane = lane
        self.row = row
        self.account = str(lane["id"])
        self.handle = row["handle"]
        self.page = page
        # False while the thread is still booting; True once its composer is up
        # and the only thing left is this tab's turn to send.
        self.ready = False
        self.deadline = deadline
        # Whether this tab has already been sent round the second route, and
        # what the last poll made of it. The reason is carried so that a tab
        # which ends up unreachable can say WHY on the row rather than joining
        # everything else under "no chat available".
        self.retried = False
        self.why = ""
        # How many "Continue" walls this tab has been shown. Counted HERE
        # rather than in `chat_state`, because the poll runs several times a
        # second and something that acts on every reading presses the same
        # button a hundred times while waiting for the first press to land.
        self.walls = 0
        # How many times this tab has declined the PIN prompt.
        self.pins = 0
        # The draft, and how much of it has been typed into this tab so far.
        # None until the first slice, which is also what says "the caret has not
        # been taken in this tab yet". `written` is the whole draft being in the
        # box - the tab is then done typing and waiting only for its turn to
        # send, which is the state that lets five tabs fill up at once.
        self.text = None
        self.at = 0
        self.written = False
        # The last thing the poll made of this tab. Kept so that "the reading
        # changed" can be told from "the same screen for a minute", which is the
        # difference between a chat that is booting slowly and one that has
        # stopped moving - and the ONLY thing that is now allowed to end a tab
        # on time rather than on evidence. See `progress`.
        self.seen = None
        # When this tab FIRST read as having been sent somewhere that is not the
        # chat. None whenever it is reading as anything else. A bounce is only
        # acted on once it has been the answer for `BOUNCE_GRACE` - see there.
        self.bounced_at = None

    def progress(self, state, why) -> None:
        """Something happened in this tab. Give it its full patience back.

        The deadline used to be set once, at open, and never touched again -
        so the seventy-five seconds covered the page load AND every Continue
        press AND the PIN prompt AND the second route, and a tab that was
        visibly working its way in ran out of time and was written off as
        unreachable. Now the clock only runs while the tab is showing the same
        thing it showed before.
        """
        reading = (state, why)
        if reading != self.seen:
            self.seen = reading
            self.deadline = time.monotonic() + OPEN_TIMEOUT


# How long a tab may show the SAME screen before the run gives up on it. Not
# how long it has altogether: `_Slot.progress` puts the whole of it back every
# time the reading changes, so a tab that is loading, then walled, then walled
# again, then loading is never out of time. Only a tab that has stopped moving
# runs this down - which is what the number is for, and is not what it used to
# mean.
OPEN_TIMEOUT = 75.0

# How long the same "messenger sent this tab somewhere else" reading has to keep
# coming back before it is believed. A bounce is read off the ADDRESS, and the
# address of a tab mid-redirect is not where it is going to end up - so the
# first one or two are ignored on principle. This is the second half of the fix
# in `chat_state`: that one refuses to call a still-loading tab bounced, and
# this one refuses to act on a settled reading until it has settled.
BOUNCE_GRACE = 6.0

# How many times one tab may be shown a "Continue" and press it. Raised from
# four, and the four was covering for a clicker that missed: `press_continue`
# reported success on buttons it had not pressed and failure on buttons that
# were only mid-render, so a handful of presses was a handful of guesses. With
# the visible-only match and the three ways in it now has, a press that reports
# success has landed - and a wall that keeps coming back is Messenger stacking
# them, which is a thing to keep pressing through rather than to give up on.
MAX_WALLS = 12

# How many times one tab may decline the PIN prompt. Messenger re-offers it, so
# once is not always enough; a prompt still there after several goes is one this
# run cannot get past, and the row says so rather than the tab spinning on it.
MAX_PINS = 3


def _release(slots: list, slot) -> None:
    """This tab is no longer the pool's, AND IT STAYS OPEN.

    Every one of these used to be `automation.close_tab`, and that is what the
    operator watched: tabs vanishing off the window one after another, several
    of them while the chat inside was still on its way up. Two things were
    wrong with it and only one of them was the closing.

    The other was WHEN. A tab was closed for being slow, for a Continue that
    would not clear, for an address read mid-redirect - all three of which the
    tab would have got past given another poll. Those judgements are fixed
    above; this makes the remaining ones cheap, because a tab that is released
    on a wrong judgement is now still sitting there to be looked at.

    Freeing the SLOT is what refills the pool - `_fill` counts what is in
    `slots`, not what is in the window - so the run keeps its parallel_tabs
    chats going without the window ever losing one.
    """
    if slot in slots:
        slots.remove(slot)


def _pooled(run: _Run) -> None:
    """Keep `parallel_tabs` chats open per account and send out of them.

    The shape is a pool, not a batch: a slot is refilled the moment it is done
    rather than at the end of a round of five, so a recipient whose thread comes
    up at once does not sit waiting on the one that is still booting.

    A SLOT, not a tab, and that is the whole of the change here. The pool counts
    the conversations it is driving, and freeing a slot is what refills it - so
    what happens to the tab afterwards is a separate question with its own
    setting, `close_tab_after_send`, off by default.

    What is NOT a setting is the rest of it: a tab is never closed for being
    slow, for showing a Continue that would not clear, or for an address read
    mid-redirect. That is what was happening - chats were being swept off the
    window while they were still loading, and the recipient was marked
    unreachable on the strength of a page nobody had seen.

    Everything happens on THIS thread. The tabs are driven through short calls
    onto each account's own thread, none of which blocks for longer than a poll,
    and that is what lets one loop hold five conversations at once without ever
    being inside a page load.
    """
    # A resumed run's finished rows stay finished.
    pending = [(lane, row) for lane, row in run.queue if row["status"] == "pending"]
    slots: list[_Slot] = []
    try:
        while pending or slots:
            run.task.checkpoint()
            moved = _fill(run, pending, slots)
            if _poll_slots(run, slots):
                moved = True
            if _write_all(run, slots):
                moved = True
            if _send_finished(run, slots):
                moved = True
            if moved:
                run.touch()
            if not moved:
                # Nothing to do but wait on a thread that is still loading.
                # Through `checkpoint` so Stop still means now.
                run.task.checkpoint(0.25)
    finally:
        # Whatever the run was holding when it ended - stopped, halted or
        # finished - is let go of, and LEFT OPEN. A stop in particular is the
        # operator wanting to look at what is on screen, and a stop that swept
        # five half-loaded chats off the window took the evidence with it.
        for slot in list(slots):
            _release(slots, slot)


def _fill(run: _Run, pending: list, slots: list[_Slot]) -> bool:
    """Open tabs until every account is holding its share. Returns whether any did.

    The count is per ACCOUNT, not per run: five tabs means five tabs in one
    window, and two accounts sending at once are two windows of five. Counting
    globally would let one account's slow threads starve another account of the
    pool entirely.
    """
    moved = False
    while pending:
        held: dict[str, int] = {}
        for slot in slots:
            held[slot.account] = held.get(slot.account, 0) + 1
        index = next((i for i, (lane, _) in enumerate(pending)
                      if held.get(str(lane["id"]), 0) < run.parallel), None)
        if index is None:
            break
        lane, row = pending.pop(index)
        moved = True
        if run.blocked(lane, row):
            continue
        slot = _open_tab(run, lane, row)
        if slot is not None:
            slots.append(slot)
    return moved


def _open_tab(run: _Run, lane: dict, row: dict) -> Optional[_Slot]:
    """A tab of its own for this recipient, with the thread already loading."""
    account_id = str(lane["id"])
    handle = row["handle"]
    row["status"] = "processing"
    run.report()

    page = None
    try:
        page = automation.new_tab(account_id, timeout=120)
        automation.on_lane(account_id,
                           lambda: messenger.begin_chat(page, handle),
                           timeout=120)
    except Exception as err:                         # noqa: BLE001 - one bad row is not the run
        # The tab stays even here. It is the one place a close would be tidy -
        # nothing ever loaded in it - but "no tab is closed" is worth more than
        # tidy: whatever `begin_chat` hit is on that tab's screen, and the row
        # only carries the sentence Python raised.
        run.trouble(row, err)
        return None
    return _Slot(lane, row, page, time.monotonic() + OPEN_TIMEOUT)


def _poll_slots(run: _Run, slots: list[_Slot]) -> bool:
    """Ask every still-loading tab what it has become. Returns whether any answered.

    NOTHING HERE CLOSES A TAB. Every ending below goes through `_release`, which
    hands the slot back to the pool and leaves the page on the window. What used
    to happen was the opposite of what was wanted, twice over: the tab went, and
    it went early - on a slow load, on an address read mid-redirect, on a
    Continue the clicker had failed to press. Two of those three are no longer
    endings at all, and the third now leaves something on screen to look at.
    """
    moved = False
    for slot in list(slots):
        if slot.ready:
            continue
        try:
            state, why = automation.on_lane(
                slot.account,
                # Once this tab has pressed Continue as often as it is allowed
                # to, stop being told about it. A wall that will not clear
                # otherwise hides every other reading behind it - and the
                # screen behind THAT is usually the new-message one, which the
                # run can get past.
                lambda s=slot: messenger.chat_state(
                    s.page, s.handle, skip_wall=s.walls >= MAX_WALLS),
                timeout=180,
            )
        except Exception as err:                     # noqa: BLE001 - one bad row is not the run
            _release(slots, slot)
            run.trouble(slot.row, err)
            moved = True
            continue

        # Whatever it said: if it is not what it said last time, this tab is
        # getting somewhere and its clock starts again. Done before any of the
        # branches, so a tab that alternates between loading and walled - which
        # is what getting in through a stack of notices LOOKS like - is never
        # given up on in the middle of doing it.
        slot.progress(state, why)
        if why:
            slot.why = why
        if state != "bounced":
            slot.bounced_at = None

        if state == "open":
            slot.ready = True
            slot.row["chat"] = "open"
            moved = True
            continue

        if state == "restore" and slot.pins < MAX_PINS:
            # Decline it and carry on. The PIN is in the operator's head and
            # this run is never going to have it - but it does not need it: a
            # profile that cannot unseal its HISTORY can still send, so the
            # prompt is put away rather than treated as the end of the road.
            slot.pins += 1
            log("campaign", f"@{slot.handle}: declining the PIN prompt")
            try:
                automation.on_lane(
                    slot.account,
                    lambda s=slot: messenger.dismiss_restore(s.page),
                    timeout=120,
                )
            except Exception as err:                 # noqa: BLE001 - one bad row is not the run
                _release(slots, slot)
                run.trouble(slot.row, err)
                moved = True
                continue
            slot.deadline = time.monotonic() + OPEN_TIMEOUT
            moved = True
            continue

        if state == "wall" and slot.walls < MAX_WALLS:
            # Press it and go back to polling. The composer is usually the very
            # next thing this tab becomes - which is exactly what the run could
            # not do before: it watched the wall until the deadline and then
            # called the recipient unreachable.
            slot.walls += 1
            try:
                pressed = automation.on_lane(
                    slot.account,
                    lambda s=slot: messenger.clear_wall(s.page),
                    timeout=120,
                )
                if pressed:
                    log("campaign", f"@{slot.handle}: {pressed} "
                                    f"({slot.walls} of {MAX_WALLS})")
            except Exception as err:                 # noqa: BLE001 - one bad row is not the run
                _release(slots, slot)
                run.trouble(slot.row, err)
                moved = True
                continue
            if not pressed:
                # It was there when it was read and gone when it was pressed -
                # which means something else cleared it. Nothing to do but look
                # again.
                slot.walls -= 1
            # PRESSING IS PROGRESS, whether or not this particular press landed:
            # the screen behind a stack of notices is moving either way, and a
            # tab must never be written off for having spent its time getting
            # through walls. `progress` cannot see this - the reading is "wall"
            # both before and after - so it is said here.
            slot.deadline = time.monotonic() + OPEN_TIMEOUT
            moved = True
            continue

        give_up = state in ("none", "picker") or (
            state == "restore" and slot.pins >= MAX_PINS)

        if state == "bounced":
            # NOT BELIEVED ON SIGHT. This is the reading that was ending
            # recipients before their chat had finished arriving: it comes off
            # the address bar, and a tab still following Messenger's redirects
            # has an address that means nothing yet. `chat_state` already
            # refuses to say "bounced" about a document that is still loading;
            # this waits, on top of that, for the settled answer to stay settled.
            if slot.bounced_at is None:
                slot.bounced_at = time.monotonic()
            elif time.monotonic() - slot.bounced_at >= BOUNCE_GRACE:
                if not slot.retried:
                    # messenger.com would not resolve this handle. Facebook's
                    # copy of the same inbox resolves it through the profile
                    # lookup instead, so the tab goes round once more by that
                    # road before the recipient is written off - which is the
                    # difference between "this account cannot be messaged" and
                    # "one of the two addresses for it did not work".
                    slot.retried = True
                    slot.bounced_at = None
                    slot.seen = None
                    slot.deadline = time.monotonic() + OPEN_TIMEOUT
                    log("campaign", f"@{slot.handle}: {why} - trying facebook.com")
                    try:
                        automation.on_lane(
                            slot.account,
                            lambda s=slot: messenger.begin_chat(
                                s.page, s.handle, on_facebook=True),
                            timeout=120,
                        )
                    except Exception as err:         # noqa: BLE001 - one bad row is not the run
                        _release(slots, slot)
                        run.trouble(slot.row, err)
                    moved = True
                    continue
                # Both addresses, both settled, and both somewhere else.
                give_up = True

        # The clock - and it is a STALL clock now rather than a stopwatch. Every
        # branch above puts the whole of `OPEN_TIMEOUT` back, so reaching this
        # means the tab has shown the same thing, done nothing, and got nowhere
        # for the entire length of it.
        timed_out = time.monotonic() > slot.deadline
        if give_up or timed_out:
            # A wall that said so, a handle neither host would resolve, or a
            # thread that never arrived. The row says which - they are three
            # different mornings and they used to read identically. The TAB is
            # left open on whichever of them it is, which is the only way the
            # verdict can be checked against the screen it was made from.
            _release(slots, slot)
            if give_up:
                # The page answered. Whatever it said is the reason.
                reason = slot.why or "no chat available"
            else:
                # It never answered - and `slot.why` here would be a stale line
                # from the route that was already retried, which would read as
                # an explanation for a wait that had a different cause.
                reason = (f"the chat did not change for {int(OPEN_TIMEOUT)}s "
                          f"- its tab is still open")
            run.no_chat(slot.row, reason)
            moved = True
    return moved


# How many characters one tab is given before the pool moves to the next. At
# the default 240wpm a character is 50ms, so a slice is roughly half a second
# of typing - long enough that the cost of the call is not the bulk of the work,
# short enough that four other tabs are never waiting long for their turn.
SLICE = 10


def _write_all(run: _Run, slots: list[_Slot]) -> bool:
    """Type a little into EVERY tab that is ready. Returns whether any did.

    The pool exists to overlap waiting, and typing is waiting: at a human speed
    a 130-character draft is half a minute, and it used to be spent with the
    account's one thread held by a single tab. Five tabs then wrote strictly one
    after another - open five, write one, send it, only then start the next -
    which is the run the operator watched and described as tabs being missed.

    So a pass gives each tab a slice and comes back. All five fill up together,
    at the same typing speed as before; what changed is that none of them is
    waiting behind another.

    SENDING is still one at a time and still spaced by the gap. That is a
    different question from typing - five composers filling at once must not
    become five messages leaving at once - and `_send_finished` answers it.
    """
    moved = False
    for slot in list(slots):
        if not slot.ready or slot.written:
            continue

        if slot.text is None:
            # First slice for this tab: settle, attach, take the caret. Done
            # once, here, rather than re-established on every slice.
            text = slot.row["text"] or ""
            files = run.files_for(slot.row)
            if not text and not files:
                # Nothing to write. The chat was opened, which was the whole of
                # the work asked for, so that is the outcome.
                slot.text, slot.at, slot.written = "", 0, True
                return True
            try:
                automation.on_lane(
                    slot.account,
                    lambda s=slot, f=files: _start_writing(run, s, f),
                    timeout=300,
                )
            except Exception as err:                 # noqa: BLE001 - one bad row is not the run
                _release(slots, slot)
                run.trouble(slot.row, err)
                return True
            slot.text, slot.at = text, 0
            moved = True

        try:
            slot.at = automation.on_lane(
                slot.account,
                lambda s=slot: messenger.type_slice(
                    s.page, s.text, s.at, SLICE, run.typing_wpm),
                timeout=300,
            )
        except Exception as err:                     # noqa: BLE001 - one bad row is not the run
            _release(slots, slot)
            run.trouble(slot.row, err)
            return True

        moved = True
        if slot.at >= len(slot.text):
            slot.written = True
            log("campaign", f"@{slot.handle}: written, waiting its turn to send")
    return moved


def _start_writing(run: _Run, slot: _Slot, files) -> None:
    """The settle and the caret, on the account's thread. One call per tab."""
    # The settle the serial run spends inside `open_chat`, spent here instead:
    # this is the moment the thread is on screen with nothing typed into it,
    # which is what there was to see.
    if run.chat_settle > 0:
        slot.page.wait_for_timeout(int(run.chat_settle * 300))
    log("campaign", f"@{slot.handle}: {messenger.begin_writing(slot.page, media=files)}")


def _send_finished(run: _Run, slots: list[_Slot]) -> bool:
    """Send ONE tab that is fully written, if the gap allows it.

    The tab stays open afterwards unless `close_tab_after_send` is on - see the
    `finally` at the bottom, which is the run's only remaining close.

    One, deliberately. Five composers filling at the same moment must not become
    five messages leaving at the same moment: the pool overlaps the WRITING, and
    the gap spaces the SENDING. So a pass sends at most once, and `pace` decides
    when the next pass may.
    """
    if not run.due():
        return False
    slot = next((s for s in slots if s.written), None)
    if slot is None:
        return False
    slots.remove(slot)

    # The cap, AGAIN, and this is the check that counts. `_fill` tested it too,
    # but that was up to four sends ago - a pool of five opens five tabs while
    # the day has room for one, and every one of them would then send. The tally
    # only moves when a message actually goes out, so the reading that decides
    # whether THIS one may is the one taken here, with the draft already typed.
    if run.blocked(slot.lane, slot.row):
        _release(slots, slot)
        return True

    try:
        if slot.text:
            outcome = automation.on_lane(
                slot.account,
                lambda s=slot: messenger.finish_writing(s.page, click_send=run.click_send),
                timeout=300,
            )
        else:
            outcome = "typed"                        # there was nothing to send
        run.won(slot.lane, slot.row, outcome)
    except Exception as err:                         # noqa: BLE001 - one bad row is not the run
        run.trouble(slot.row, err)
    finally:
        # The SLOT goes, and that is the refill: the next pass finds room in
        # this account and opens the next recipient. Whether the TAB goes with
        # it is `close_tab_after_send`, and this is the only place in the run
        # that asks - every other ending leaves the tab standing whatever the
        # setting says, because every other ending is a verdict rather than a
        # send, and a verdict is the thing worth being able to check against
        # the screen it was made from.
        #
        # Off, which is the default, a finished run is a window of the chats it
        # wrote to, in order. On, the window stays the size of the pool.
        _release(slots, slot)
        if run.close_after_send:
            automation.close_tab(slot.account, slot.page)
    run.pace()
    return True


def _run(task: tasks.Task, settings: dict[str, Any],
         saved: Optional[dict] = None) -> None:
    run = _Run(task, _order(), settings, saved=saved)
    _current["run"] = run
    _current["settings"] = settings
    if run.parallel > 1:
        log("campaign", f"{run.parallel} chats open at a time per account")
    stall = max(0.0, float(settings.get("campaign_stall_minutes") or 0)) * 60
    if stall > 0 and settings.get("campaign_auto_restart", True):
        threading.Thread(target=_watchdog, args=(task, run, stall),
                         name="campaign-watchdog", daemon=True).start()
    try:
        if run.parallel > 1:
            _pooled(run)
        else:
            _serial(run)

        failed = sum(1 for _, row in run.queue if row["status"] == "failed")
        with _lock:
            _campaign["state"] = "done"
            _campaign["note"] = f"Finished - {failed} failed."
        task.set(progress=1.0, result=_state())
        _persist(force=True)
        log("campaign", f"finished - {run.total - failed} of {run.total} reached")

    except _Halt as err:
        # A challenge is a reason to stop, not a fault to restart through.
        with _lock:
            _campaign["state"] = "stopped"
            _campaign["note"] = f"Stopped: {err}"
        task.set(result=_state())
        _persist(force=True)
        log("campaign", "stopped on a challenge", "error")

    except tasks.Stopped:
        if not run.restarting:
            with _lock:
                _campaign["state"] = "stopped"
                _campaign["note"] = "Ended. The recipients it did not reach stay unmessaged."
        task.set(result=_state())
        _persist(force=True)
        raise

    except Exception as err:                         # noqa: BLE001 - reported, then picked up again
        detail = str(err) or err.__class__.__name__
        task.set(result=_state())
        if settings.get("campaign_auto_restart", True) and _pending_left():
            with _lock:
                _campaign["state"] = "restarting"
                _campaign["note"] = f"Went wrong ({detail}) - restarting from where it was."
            _persist(force=True)
            log("campaign", f"went wrong - {detail} - restarting from where it was", "error")
            _schedule_restart(f"went wrong: {detail}", wait_for=task)
        else:
            with _lock:
                _campaign["state"] = "failed"
                _campaign["note"] = f"Failed: {detail}"
            _persist(force=True)
        raise


def _is_challenge(err: Exception) -> bool:
    return bool(re.search(r"captcha|verification|locked|login wall", str(err), re.I))


@router.get("/api/campaign/status")
def status() -> dict[str, Any]:
    return _state()


@router.post("/api/campaign/pause")
def pause() -> dict[str, Any]:
    task = tasks.get(_campaign.get("task") or "")
    if task:
        task.pause()
    with _lock:
        _campaign["state"] = "paused"
        # It finishes the recipient it is on. Cutting a DM off mid-composer
        # would leave a half-typed message in a real inbox.
        _campaign["note"] = "Paused. The recipient it was on finished first."
    log("campaign", "paused", "warn")
    _persist(force=True)
    return _state()


@router.post("/api/campaign/resume")
def resume() -> dict[str, Any]:
    """Carry on. A paused run wakes up; a run that is not going any more -
    interrupted, failed, given up on - is started again from its saved rows."""
    task = tasks.get(_campaign.get("task") or "")
    if task and task.state == "paused":
        task.resume()
        with _lock:
            _campaign["state"] = "running"
            _campaign["note"] = "Running."
        log("campaign", "resumed")
        _persist(force=True)
        return _state()
    if task and task.state == "running":
        return _state()
    if not _pending_left():
        raise fail("There is nothing left to send.")
    with _lock:
        _campaign["note"] = "Resuming from where it was."
    _schedule_restart("resumed by the operator", immediate=True)
    _persist(force=True)
    log("campaign", "resumed by the operator from the saved rows")
    return _state()


@router.post("/api/campaign/stop")
def stop() -> dict[str, Any]:
    task = tasks.get(_campaign.get("task") or "")
    if task:
        task.stop()
    with _lock:
        _campaign["state"] = "stopped"
        _campaign["note"] = "Ended. The recipients it did not reach stay unmessaged."
    log("campaign", "ended", "warn")
    _persist(force=True)
    return _state()
