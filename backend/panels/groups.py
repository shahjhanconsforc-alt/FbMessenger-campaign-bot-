"""Panel - Group Posting.

The first thing on it: **Groups joined**. It opens Facebook's Groups page in
the account's window - launching one if none is up - and goes down the list
of every group this account is a member of, OPENING EACH ONE AS IT IS FOUND
rather than reading every name first: a name turns up, it is pressed, its
welcome and rules sheets are closed, and then the list is picked back up
where it was. The list is kept in the store per account, with which were
opened, so the panel shows the last walk the moment it opens and a run only
refreshes it.

Give it words - and a picture from the media library if you want one - and it
posts them into each group as it goes: the composer is opened, the post is
typed, the file attached, and Post pressed. Leave the box empty and it only
looks, which is what it did before posting existed.

Like the Actions panel's Groups joining it drives the live browser window, and
picks the account the same way: the one named, else whichever window is open,
else the first on the list - launched for the purpose.
"""

from __future__ import annotations

import copy
import random
import re
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body

from .. import busy, config, tasks
from ..automation import facebook
from ..automation.browser import automation
from ..events import log
from ..storage import GROUP_FIELDS, objects
from ..store import now, store
from ._common import as_at, fail, open_accounts
from .fb import _account_for_open

router = APIRouter()

_lock = threading.Lock()
_run: dict[str, Any] = {
    "task": "",
    "state": "idle",         # idle | running | stopped | done
    "account": "",
    "account_id": "",
    "note": "",
    "count": 0,
    "visited": 0,
    "posted": 0,
    "text": "",
    "media": [],
    "groups": [],            # [{name, url, visited}] as they are found and opened
}
_RUN_IDLE = copy.deepcopy(_run)


def _state(**over: Any) -> dict[str, Any]:
    with _lock:
        out = dict(_run)
        out["groups"] = list(_run["groups"])
    out.update(over)
    return out


def _running() -> Optional[tasks.Task]:
    task = tasks.get(_run.get("task") or "")
    return task if task and task.snapshot().get("state") == "running" else None


def _saved() -> dict[str, Any]:
    """Every account's last list, keyed by account id."""
    db = store.read()
    kept = db.get("groups") or {}
    labels = {str(a["id"]): as_at(a) for a in db["accounts"]}
    return {aid: {"label": labels.get(aid, f"account {aid}"),
                  "at": entry.get("at", ""), "groups": list(entry.get("groups") or [])}
            for aid, entry in kept.items() if aid in labels}


# -- The groups registry, and fb-groups.txt ------------------------
# Every group each account is in, with how many times it has been posted in.
# Kept in the store so posting never waits on MinIO, and mirrored to
# fb-groups.txt in the bucket so the counts survive a new machine and can be
# read by anything else that shares the bucket. The two are merged rather than
# one overwriting the other: the larger count wins, the later post, the
# earlier join.
_reg_lock = threading.Lock()
_push_lock = threading.Lock()
_push = {"running": False, "again": False}


def _int(raw: Any) -> int:
    try:
        return max(0, int(float(str(raw).strip() or 0)))
    except (TypeError, ValueError):
        return 0


def _gid(url: str, name: str = "") -> str:
    """A group's key: the segment after /groups/ in its address, lower-cased -
    the same key the walk reads off the list. A group joined before its address
    was known is keyed on its name until the walk finds it."""
    m = re.search(r"/groups/([^/?#]+)", url or "", re.I)
    if m:
        return m.group(1).lower()
    name = " ".join((name or "").split()).lower()
    return f"name:{name}" if name else ""


def _reg_find(rows: list, account_id: str, gid: str) -> Optional[dict]:
    for r in rows:
        if str(r.get("account_id")) == account_id and str(r.get("group_id")) == gid:
            return r
    return None


def _reg_entry(db: dict, account_id: str, label: str, name: str, url: str) -> Optional[dict]:
    """The registry row for this group on this account, made if it is not there."""
    rows = db.setdefault("fb_groups", [])
    gid = _gid(url, name)
    if not gid:
        return None
    found = _reg_find(rows, account_id, gid)
    if found is None and not gid.startswith("name:"):
        # Joined here before its address was known, and now found with one:
        # the same group, so its row takes the address rather than a second
        # row starting from nothing.
        found = _reg_find(rows, account_id, _gid("", name))
        if found is not None:
            found["group_id"] = gid
    if found is None:
        found = {"account_id": account_id, "account": label, "group_id": gid,
                 "name": name, "url": url, "used_count": 0,
                 "last_posted": "", "joined_at": "",
                 "scraped_at": "", "scraped_members": 0, "members_total": 0}
        rows.append(found)
    if name:
        found["name"] = name
    if url:
        found["url"] = url
    if label:
        found["account"] = label
    return found


def _reg_upsert(account_id: str, label: str, items: list[dict], *, joined: bool = False) -> None:
    stamp = now()
    with _reg_lock, store.edit() as db:
        for item in items:
            entry = _reg_entry(db, account_id, label, str(item.get("name") or "").strip(),
                               str(item.get("url") or "").strip())
            if entry is not None and joined and not entry.get("joined_at"):
                entry["joined_at"] = stamp


def _reg_count_post(account_id: str, label: str, name: str, url: str) -> int:
    """One more post in this group. Returns the new count."""
    with _reg_lock, store.edit() as db:
        entry = _reg_entry(db, account_id, label, name, url)
        if entry is None:
            return 0
        entry["used_count"] = _int(entry.get("used_count")) + 1
        entry["last_posted"] = now()
        return entry["used_count"]


def _reg_mark_scraped(account_id: str, label: str, name: str, url: str, members: int,
                      total: int = 0) -> None:
    """This group's members have just been read by the Scraping panel.

    `total` is what the group says its membership is. Kept when it is known and
    left alone when it is not, so a read that could not find the number does
    not wipe one an earlier read did.
    """
    with _reg_lock, store.edit() as db:
        entry = _reg_entry(db, account_id, label, name, url)
        if entry is not None:
            entry["scraped_at"] = now()
            entry["scraped_members"] = int(members)
            if int(total or 0) > 0:
                entry["members_total"] = int(total)


def _reg_merge(local: list, remote: list) -> list:
    """Local and bucket rows as one list, keyed on account and group."""
    merged: dict[tuple, dict] = {}
    for r in local:
        merged[(str(r.get("account_id")), str(r.get("group_id")))] = dict(r)
    for r in remote:
        key = (str(r.get("account_id") or ""), str(r.get("group_id") or ""))
        if not key[0] or not key[1]:
            continue
        mine = merged.get(key)
        if mine is None:
            merged[key] = {f: r.get(f, "") for f in GROUP_FIELDS}
            merged[key]["used_count"] = _int(r.get("used_count"))
            merged[key]["scraped_members"] = _int(r.get("scraped_members"))
            merged[key]["members_total"] = _int(r.get("members_total"))
            continue
        mine["used_count"] = max(_int(mine.get("used_count")), _int(r.get("used_count")))
        if str(r.get("last_posted") or "") > str(mine.get("last_posted") or ""):
            mine["last_posted"] = r.get("last_posted")
        # The later scrape wins, and brings its member count with it.
        if str(r.get("scraped_at") or "") > str(mine.get("scraped_at") or ""):
            mine["scraped_at"] = r.get("scraped_at")
            mine["scraped_members"] = _int(r.get("scraped_members"))
        # The membership is the group's, not the read's: whichever side knows
        # it, keeps it.
        mine["members_total"] = max(_int(mine.get("members_total")),
                                    _int(r.get("members_total")))
        joins = [j for j in (mine.get("joined_at"), r.get("joined_at")) if j]
        mine["joined_at"] = min(joins) if joins else ""
        for f in ("name", "url", "account"):
            if not mine.get(f) and r.get(f):
                mine[f] = r[f]
    return list(merged.values())


def _reg_pull() -> dict[str, Any]:
    """Bring fb-groups.txt's counts into the store. Skipped, and said so, when
    MinIO is not up - opening the chooser must never wait on a dead bucket."""
    if not objects.configured:
        return {"ok": False, "reason": "MinIO is not configured"}
    if not objects.status().get("up"):
        return {"ok": False, "reason": "MinIO is not reachable right now"}
    remote = objects.read_fb_groups()
    if not remote.get("ok"):
        return {"ok": False, "reason": remote.get("reason") or "could not read fb-groups.txt"}
    with _reg_lock, store.edit() as db:
        db["fb_groups"] = _reg_merge(db.get("fb_groups") or [], remote.get("rows") or [])
    return {"ok": True, "bucket": remote.get("bucket"), "object": remote.get("object")}


def _reg_delete(group_ids: list[str]) -> dict[str, Any]:
    """Remove these groups from the registry and from fb-groups.txt.

    Done in one go rather than through the usual push: a push MERGES with what
    is in the bucket, so a row deleted here and still there would come straight
    back. The bucket is read, merged, the rows dropped, and the result written.

    The group itself is untouched on Facebook - the next sync of an account
    that is in it puts it back as a group not scraped yet.
    """
    wanted = {str(g).strip().lower() for g in group_ids if str(g).strip()}
    if not wanted:
        return {"ok": True, "removed": 0}
    remote = objects.read_fb_groups() if objects.configured else {"ok": False}
    with _reg_lock, store.edit() as db:
        rows = db.get("fb_groups") or []
        if remote.get("ok"):
            rows = _reg_merge(rows, remote.get("rows") or [])
        keep = [r for r in rows if str(r.get("group_id") or "").lower() not in wanted]
        removed = len(rows) - len(keep)
        db["fb_groups"] = keep
        out = [dict(r) for r in keep]
    if not objects.configured:
        return {"ok": True, "removed": removed, "reason": "MinIO is not configured"}
    if not remote.get("ok"):
        # The bucket could not be read, so it cannot be rewritten safely - the
        # rows are gone here and the file still has them. Said, not hidden.
        log("groups", f"fb-groups.txt not updated: {remote.get('reason')}", "warn")
        return {"ok": True, "removed": removed, "reason": remote.get("reason")}
    sent = objects.write_fb_groups(out)
    if not sent.get("ok"):
        log("groups", f"fb-groups.txt not updated: {sent.get('reason')}", "warn")
        return {"ok": True, "removed": removed, "reason": sent.get("reason")}
    log("groups", f"{sent['bucket']}/{sent['object']}: {len(out)} group(s) "
                  f"after removing {removed}")
    return {"ok": True, "removed": removed, "bucket": sent.get("bucket"),
            "object": sent.get("object")}


def _reg_push_later() -> None:
    """Merge with the bucket and write fb-groups.txt, on a thread of its own.

    Written back only when the read worked: a bucket that could not be read is
    not a bucket that is empty, and writing the local rows over it would wipe
    every row another machine had put there.
    """
    with _push_lock:
        if _push["running"]:
            _push["again"] = True
            return
        _push["running"] = True

    def go() -> None:
        try:
            while True:
                with _push_lock:
                    _push["again"] = False
                if not objects.configured:
                    return
                remote = objects.read_fb_groups()
                if not remote.get("ok"):
                    log("groups", f"fb-groups.txt not updated: {remote.get('reason')}", "warn")
                    return
                with _reg_lock, store.edit() as db:
                    db["fb_groups"] = _reg_merge(db.get("fb_groups") or [],
                                                 remote.get("rows") or [])
                    rows = [dict(r) for r in db["fb_groups"]]
                sent = objects.write_fb_groups(rows)
                if sent.get("ok"):
                    log("groups", f"{sent['bucket']}/{sent['object']}: {len(rows)} group(s)")
                else:
                    log("groups", f"fb-groups.txt not updated: {sent.get('reason')}", "warn")
                with _push_lock:
                    if not _push["again"]:
                        return
        finally:
            with _push_lock:
                _push["running"] = False

    threading.Thread(target=go, name="fb-groups-push", daemon=True).start()


@router.get("/api/groups/list")
def group_list(account: str = "") -> dict[str, Any]:
    """The groups an account is in, with their post counts, for the chooser."""
    chosen = _account_for_open({"account": account} if account else {})
    account_id = str(chosen["id"])
    label = as_at(chosen)
    # The walk's saved list counts: groups read before the registry existed are
    # still groups this account is in.
    saved = ((store.read().get("groups") or {}).get(account_id) or {}).get("groups") or []
    if saved:
        _reg_upsert(account_id, label, [{"name": g.get("name"), "url": g.get("url")}
                                        for g in saved])
    minio = _reg_pull()
    rows = [{"group_id": str(r.get("group_id")), "name": r.get("name") or "",
             "url": r.get("url") or "", "used_count": _int(r.get("used_count")),
             "last_posted": r.get("last_posted") or "", "joined_at": r.get("joined_at") or ""}
            for r in (store.read().get("fb_groups") or [])
            if str(r.get("account_id")) == account_id]
    return {"account": label, "account_id": account_id, "rows": rows, "minio": minio}


@router.get("/api/groups/status")
def status() -> dict[str, Any]:
    return {**_state(),
            "windows": [{"id": str(a["id"]), "label": as_at(a)} for a in open_accounts()],
            "saved": _saved()}


def _post_from(body: dict) -> Optional[dict[str, Any]]:
    """What to post, or None when this run is only looking.

    The pictures are named, not uploaded here: they are already in the media
    library the campaign fills, so this checks the names are real and hands
    over the paths on disk. A name that is not there is refused rather than
    quietly dropped - a post that was meant to carry a picture and went
    without one is not the post anybody asked for.
    """
    text = str(body.get("text") or "").strip()
    names = [str(n) for n in (body.get("media") or []) if str(n).strip()]
    if not text and not names:
        return None
    library = {m["name"]: m for m in store.read()["media"]}
    missing = [n for n in names if n not in library]
    if missing:
        raise fail(f"{missing[0]} is no longer in the media library.")
    paths = [str(config.MEDIA / n) for n in names]
    gone = [p for p in paths if not Path(p).exists()]
    if gone:
        raise fail(f"{Path(gone[0]).name} is in the library but not on disk.")
    return {"text": text, "media": paths, "names": names,
            "click_post": body.get("click_post") is not False,
            "wpm": int(store.settings().get("typing_wpm") or 240)}


@router.post("/api/groups/joined")
def joined(body: dict = Body(default={})) -> dict[str, Any]:
    """Go down the account's joined groups, opening each one as it is found."""
    if _running() is not None:
        raise fail("The groups are already being read - stop that first.")

    post = _post_from(body)
    only: Optional[set] = None
    if body.get("only") is not None:
        only = {str(g).strip().lower() for g in (body.get("only") or []) if str(g).strip()}
        if not only:
            raise fail("Choose at least one group to post in.")

    account = _account_for_open(body)
    account_id = str(account["id"])
    busy.claim([account_id], "groups", names={account_id: as_at(account)})

    with _lock:
        _run.update(state="running", account=as_at(account), account_id=account_id,
                    count=0, visited=0, posted=0, groups=[],
                    text=(post or {}).get("text", ""),
                    media=list((post or {}).get("names") or []),
                    note="Opening Facebook.")

    settings = store.settings()

    def _gap() -> tuple[float, float]:
        """How long to rest between two groups, as the operator set it."""
        def num(raw: Any, default: float) -> float:
            try:
                return max(0.0, min(60.0, float(raw)))
            except (TypeError, ValueError):
                return default
        low = num(settings.get("groups_visit_gap_min"), 1.0)
        high = num(settings.get("groups_visit_gap_max"), 2.5)
        return (low, high) if low <= high else (high, low)

    def run(task: tasks.Task) -> None:
        def on_step(said: str) -> None:
            with _lock:
                _run["note"] = said
            task.set(detail=said, result=_state())

        def on_group(group: dict) -> None:
            """A name, the moment it is found - its row lands before it opens."""
            with _lock:
                _run["groups"].append({"name": group["name"], "url": group["url"],
                                       "visited": "", "title": "", "how": "", "closed": 0,
                                       "posted": "", "said": ""})
                _run["count"] = len(_run["groups"])
                count = _run["count"]
            _reg_upsert(account_id, as_at(account), [group])
            log("groups", f'found "{group["name"]}" ({count})')
            task.set(detail=f"{count} found", result=_state())

        def on_visit(visit: dict) -> None:
            with _lock:
                for entry in _run["groups"]:
                    if entry["url"] == visit["url"]:
                        entry["visited"] = "yes" if visit["ok"] else "no"
                        entry["title"] = visit.get("title") or ""
                        entry["closed"] = int(visit.get("closed") or 0)
                        entry["how"] = visit.get("how") or ""
                        # What became of the post. Left off at first, so the
                        # table's Posted column read a dash for every group
                        # whether the post had gone or not.
                        entry["posted"] = visit.get("posted") or ""
                        entry["said"] = visit.get("said") or ""
                        break
                if visit["ok"]:
                    _run["visited"] += 1
                if visit.get("posted") == "yes":
                    _run["posted"] += 1
                seen = _run["visited"]
            said = visit.get("said") or ""
            if visit.get("posted") == "yes":
                used = _reg_count_post(account_id, as_at(account), visit["name"], visit["url"])
                said = f"{said} (posted here {used} time{'' if used == 1 else 's'})"
            log("groups",
                f'{visit["how"]} "{visit["name"]}" ({visit["index"] + 1} of {visit["total"]})'
                + (f" - {said}" if said else ""),
                "info" if visit["ok"] and visit.get("posted") != "no" else "warn")
            # No denominator: the list's length is not known until the walk
            # reaches the end of it, so this counts rather than fractions.
            task.set(detail=f"{seen} of {visit['total']} opened", result=_state())
            _keep(account_id)

        try:
            try:
                # Straight onto the Groups page: loading the home feed first only for the walk to leave it was seconds of nothing.
                automation.open_account(account_id, facebook.JOINED_URL, timeout=20_000)
            except Exception as err:                 # noqa: BLE001 - re-raised unless slow
                if "Timeout" not in str(err):
                    raise
            found = automation.with_page(
                account_id,
                lambda page: facebook.walk_joined_groups(
                    page, gap=_gap(),
                    direct=settings.get("groups_visit_direct", True) is not False,
                    post=post, only=only,
                    on_step=on_step, on_group=on_group,
                    on_visit=on_visit, checkpoint=task.checkpoint),
                timeout=None) or []
        except tasks.Stopped:
            _keep(account_id)
            _reg_push_later()
            with _lock:
                _run.update(state="stopped", note="Stopped - what was read so far is kept.")
            task.set(result=_state())
            raise
        except Exception as err:                     # noqa: BLE001 - reported, not swallowed
            with _lock:
                _run.update(state="done", note=str(err) or err.__class__.__name__)
            task.set(result=_state())
            _reg_push_later()
            log("groups", f"reading the groups stopped: {err}", "error")
            raise
        with _lock:
            opened = _run["visited"]
            put = _run["posted"]
            _run.update(state="done",
                        note=f"Finished - {len(found)} group(s) this account is in, "
                             f"{opened} opened"
                             + (f", posted in {put}." if post else "."))
        _keep(account_id)
        _reg_push_later()
        task.set(result=_state())
        log("groups", f"{as_at(account)} is in {len(found)} group(s) - {opened} opened")

    task = tasks.start("reading the joined groups", run, source="groups")
    busy.bind([account_id], task.id)
    with _lock:
        _run["task"] = task.id
    log("groups", f"reading the groups {as_at(account)} has joined"
                  + (f" - posting in {len(only)} chosen group(s)" if only else ""))
    task.set(result=_state())
    return {"task": task.id, **_state()}


def _keep(account_id: str) -> None:
    """Write the list read so far into the store, under this account."""
    with _lock:
        groups = list(_run["groups"])
    if not groups:
        return
    with store.edit() as db:
        db.setdefault("groups", {})[account_id] = {"at": now(), "groups": groups}


@router.post("/api/groups/stop")
def stop() -> dict[str, Any]:
    task = _running()
    if task is None:
        raise fail("The groups are not being read.")
    task.stop()
    with _lock:
        _run.update(state="stopped", note="Stopping.")
    log("groups", "reading the groups was ended by the operator", "warn")
    return _state()


# -- Joining groups ------------------------------------------------
# The other half of this panel, and where it came from: this used to be the
# Actions panel's one action. It opens facebook.com/groups/discover in the
# account's window - launching one if none is up - and joins a handful of the
# groups suggested there, answering whatever Facebook puts up on the way: a
# rules sheet to agree to, membership questions, a confirmation. A dialog that
# cannot be got through is closed and the next group is tried.
#
# It sits beside the walk rather than on another screen because the two are
# one job in two halves: join some groups, then go through the groups you are
# in. They share this panel's account picker and its claim on the window.
_join: dict[str, Any] = {
    "task": "",
    "state": "idle",         # idle | running | stopped | done
    "account": "",
    "note": "",
    "joined": 0,
    "tried": 0,
    "want": 0,
    "names": [],
    # Every group this run tried, joined or not, for the panel's table:
    # [{name, ok, note}] in the order they were pressed.
    "rows": [],
}
_JOIN_IDLE = copy.deepcopy(_join)


def reset_idle() -> None:
    """Both jobs on this panel as they were when the server started - the
    groups registry and fb-groups.txt are data, not a run, and are kept."""
    with _lock:
        _run.clear()
        _run.update(copy.deepcopy(_RUN_IDLE))
        _join.clear()
        _join.update(copy.deepcopy(_JOIN_IDLE))


def _join_state(**over: Any) -> dict[str, Any]:
    with _lock:
        out = dict(_join)
        out["names"] = list(_join["names"])
        out["rows"] = [dict(r) for r in _join["rows"]]
    out.update(over)
    return out


def _join_running() -> Optional[tasks.Task]:
    task = tasks.get(_join.get("task") or "")
    return task if task and task.snapshot().get("state") == "running" else None


def _bounds(settings: dict, body: dict) -> tuple[int, int]:
    """How many groups a run joins: rolled between these two, 1 to 50."""
    def num(raw: Any, default: int) -> int:
        try:
            return max(1, min(50, int(float(raw))))
        except (TypeError, ValueError):
            return default
    low = num(body.get("min", settings.get("groups_join_min")), 5)
    high = num(body.get("max", settings.get("groups_join_max")), 10)
    return (low, high) if low <= high else (high, low)


@router.get("/api/groups/join/status")
def join_status() -> dict[str, Any]:
    settings = store.settings()
    low, high = _bounds(settings, {})
    return {**_join_state(), "min": low, "max": high,
            "windows": [{"id": str(a["id"]), "label": as_at(a)}
                        for a in open_accounts()]}


@router.post("/api/groups/join")
def join(body: dict = Body(default={})) -> dict[str, Any]:
    """Start joining groups. Returns a task id straight away."""
    if _join_running() is not None:
        raise fail("Groups are already being joined - stop that first.")

    account = _account_for_open(body)
    account_id = str(account["id"])
    busy.claim([account_id], "groups", names={account_id: as_at(account)})
    settings = store.settings()
    low, high = _bounds(settings, body)
    if (low, high) != _bounds(settings, {}):
        with store.edit() as db:
            db["settings"]["groups_join_min"] = low
            db["settings"]["groups_join_max"] = high
    want = random.randint(low, high)

    with _lock:
        _join.update(state="running", account=as_at(account), joined=0, tried=0,
                    want=want, names=[], rows=[], note="Opening Facebook.")

    def run(task: tasks.Task) -> None:
        def on_step(said: str) -> None:
            with _lock:
                _join["note"] = said
            task.set(detail=said, result=_join_state())

        def on_join(done: dict) -> None:
            with _lock:
                _join["joined"] = done["joined"]
                _join["tried"] = done["tried"]
                if done["ok"]:
                    _join["names"].append(done["name"])
                _join["rows"].append({"name": done["name"], "ok": bool(done["ok"]),
                                      "url": str(done.get("url") or ""),
                                      "note": str(done.get("note") or "")})
            if done["ok"]:
                _reg_upsert(account_id, as_at(account),
                            [{"name": done["name"], "url": done.get("url") or ""}], joined=True)
            log("groups",
                f'{"joined" if done["ok"] else "could not join"} "{done["name"]}" '
                f'({done["joined"]} of {done["want"]}) - {done["note"]}',
                "info" if done["ok"] else "warn")
            task.set(progress=done["joined"] / max(1, done["want"]), result=_join_state())

        try:
            try:
                automation.open_account(account_id, facebook.HOME_URL, timeout=20_000)
            except Exception as err:                 # noqa: BLE001 - re-raised unless slow
                if "Timeout" not in str(err):
                    raise
            out = automation.with_page(
                account_id,
                lambda page: facebook.join_groups(
                    page, want=want, on_step=on_step, on_join=on_join,
                    checkpoint=task.checkpoint),
                timeout=None) or {}
        except tasks.Stopped:
            _reg_push_later()
            with _lock:
                _join.update(state="stopped", note="Stopped.")
            task.set(result=_join_state())
            raise
        except Exception as err:                     # noqa: BLE001 - reported, not swallowed
            with _lock:
                _join.update(state="done", note=str(err) or err.__class__.__name__)
            task.set(result=_join_state())
            _reg_push_later()
            log("groups", f"joining groups stopped: {err}", "error")
            raise
        with _lock:
            joined = int(out.get("joined") or 0)
            _join.update(state="done",
                        note=f"Finished - joined {joined} of {want} group(s)."
                             if joined >= want else
                             f"Finished - joined {joined} of {want}; the page ran out of groups.")
        _reg_push_later()
        task.set(result=_join_state())

    task = tasks.start("joining groups", run, source="groups")
    busy.bind([account_id], task.id)
    with _lock:
        _join["task"] = task.id
    log("groups", f"joining {want} groups from Discover on {as_at(account)}")
    task.set(result=_join_state())
    return {"task": task.id, **_join_state()}


@router.post("/api/groups/join/stop")
def join_stop() -> dict[str, Any]:
    task = _join_running()
    if task is None:
        raise fail("Groups are not being joined.")
    task.stop()
    with _lock:
        _join.update(state="stopped", note="Stopping - it finishes the group it is on.")
    log("groups", "joining groups was ended by the operator", "warn")
    return _join_state()
