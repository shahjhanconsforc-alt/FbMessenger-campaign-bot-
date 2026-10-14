"""Panel 3 - Scraping.

The home timeline of every account picked for the run, walked without stopping.
Gated behind an open browser window, because the feed is behind the login wall
entirely - a sweep run without one does not fail, it collects nothing and reports
that as the answer.

SEVERAL ACCOUNTS AT ONCE, not one. Three windows open and two of them picked
means two feeds being walked side by side, and the third left alone - which is
the point of having three windows. Each account walks in its own thread against
its own lane in `automation`, so they are genuinely simultaneous rather than
taking turns; what makes that safe is that no two of them ever touch the same
Chrome.

They collect into ONE roster. Two home feeds overlap - the same account is
followed by both - and a handle is a handle whichever window saw it first, so
the run keeps one row per handle and remembers every account that walked for it.

The page is always Home: there is no target to pick and no list to choose
between, so nothing on the panel asks. And every handle not yet met is hovered
off the chat list, because a thread row carries a name and
nothing else - without the hover the roster is a column of handles with no way
to triage it.

The panel's live state (the rows so far, the lanes walking, the runs it has
finished) is one object, and every task frame carries the whole of it. The run
owns the roster: it decides what it found, and the console paints what it is
handed rather than reassembling a table on its side from a percentage.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import APIRouter, Body

from .. import busy, tasks
from ..automation import facebook, firecrawl, messenger
from ..automation.browser import automation
from ..events import log
from ..storage import object_name, objects
from ..store import now, store
from ._common import as_at, fail, open_accounts, working_account

router = APIRouter()

# The one thing a sweep walks. Kept in the shape a target has because everything
# downstream - the roster key, the per-account provenance the Data panel filters
# on - is written against that shape, and "Chat list" is a perfectly honest
# answer to "where did this handle come from".
HOME_TARGET = {"key": "inbox", "name": "Chat list", "mode": "inbox",
               "note": "the conversations the account it runs as already has"}
# The other page a sweep walks: the account's home feed on facebook.com. A
# stream of strangers rather than a list of acquaintances, and the reason the
# panel has a page to pick at all - the chat list is finite and already known,
# the feed is where new people come from. Each post's author comes off as a
# handle - a username, or the bare numeric id for a profile that has none.
FEED_TARGET = {"key": "feed", "name": "Home feed", "mode": "feed",
               "note": "whoever posted in the account's home feed"}
# The third: every group the account is in, one by one, each group's People
# tab read to the end. Group members are strangers who share an interest,
# which is the reason to want them.
GROUPS_TARGET = {"key": "groups", "name": "FB groups", "mode": "groups",
                 "note": "the members of every group the account is in"}
SOURCES = {"feed": FEED_TARGET, "inbox": HOME_TARGET, "groups": GROUPS_TARGET}
# What a press with nothing said walks. The feed, because that is what a
# scrape is FOR: the chat list is who the account already talks to.
DEFAULT_SOURCE = "feed"
MODE = DEFAULT_SOURCE

# A run that does not stop collects more rows than a table can paint. The frame
# carries the most recent slice and the true count beside it; the roster on disk
# keeps every one of them.
FRAME_ROWS = 500

# The roster is written as the walk goes rather than at the end, because there is
# no end - a run is finished by the operator pressing Stop, and everything up to
# that press has to already be saved.
SAVE_EVERY = 25

# The open run. One at a time by design - not because of the browser any more,
# but because there is one roster and one board, and two sweeps writing both
# would each be reporting half of what the panel showed.
#
# `seen` is the roster itself, keyed by handle and in the order the accounts came
# off the feeds - so the table is append-only and reads as the run's own
# chronology rather than being regrouped every time a lane reports.
_sweep: dict[str, Any] = {"target": None, "mode": MODE, "seen": {}, "lanes": [], "done": [],
                          "task": None, "saved_at": 0, "started_at": "", "ended_at": ""}

# A lane whose walk died is started again - after this long, and this many
# times in a row before the lane is given up as broken. The count resets every
# time the lane finds something new, so a walk that runs for an hour and then
# trips is not charged for the trips it took yesterday. Twenty at ten seconds
# is a few minutes of a window that is signed out or closed, which is long
# enough for the operator to read the reason on the lane and fix it.
RESTART_WAIT = 10.0
MAX_RESTARTS = 20
_lock = threading.Lock()


def _state(**extra: Any) -> dict[str, Any]:
    with _lock:
        rows = list(_sweep["seen"].values())
        # Each lane's OWN rows travel with it, as well as the merged roster.
        # Three windows walking are three feeds, and the board shows each on a
        # tab of its own beside the merged one - a handle two accounts both
        # met is on both tabs and once in the roster.
        lanes = [{"id": lane["id"], "account": lane["account"],
                  "count": lane["count"], "running": lane["running"],
                  "error": lane["error"],
                  # The clocks. `last_new_at` is when this lane last added a
                  # handle - the stall clock on the board counts from it - and
                  # `restarts` is how often the watchdog has had to reload or
                  # restart it. `status` is what it is doing right now.
                  "last_new_at": lane.get("last_new_at") or "",
                  "restarts": int(lane.get("restarts") or 0),
                  "status": lane.get("status") or "",
                  # The group this lane is inside right now: its name, how many
                  # people have been read out of it, and how many the group says
                  # it has. Gone the moment the lane leaves the group.
                  "group": lane.get("group") or None,
                  # With more than one tab, what each tab is reading.
                  "tabs": list(lane.get("tabs") or []),
                  "rows": list(lane.get("rows") or [])[-FRAME_ROWS:]}
                 for lane in _sweep["lanes"]]
        frame = {
            "target": _sweep["target"],
            "mode": _sweep["mode"],
            "started_at": _sweep["started_at"],
            "ended_at": _sweep["ended_at"],
            "rows": rows[-FRAME_ROWS:],
            "total": len(rows),
            "lanes": lanes,
            "done": list(_sweep["done"]),
        }
    frame.update(extra)
    return frame


def _rows() -> list[dict]:
    with _lock:
        return list(_sweep["seen"].values())


def _running() -> Any:
    task = tasks.get(_sweep.get("task") or "")
    return task if task is not None and task.state in ("running", "paused") else None


def _absorb(lane: dict, rows: list[dict]) -> None:
    """Fold one lane's roster into the run's, and count what that lane has.

    A handle both feeds carry is ONE row. Which lane saw it first decides where
    it sits in the table; every read after that fills in whatever the first was
    missing, because a row whose card had not come back yet when it was written
    down should not stay blank for the rest of the run just because it was
    written down early.
    """
    with _lock:
        seen = _sweep["seen"]
        lane["count"] = len(rows)
        lane["rows"] = [dict(row) for row in rows]
        for row in rows:
            handle = row.get("handle")
            if not handle:
                continue
            found = seen.get(handle)
            if found is None:
                seen[handle] = dict(row)
                continue
            for field in ("name", "bio"):
                if not found.get(field) and row.get(field):
                    found[field] = row[field]
            for field in ("followers", "dm_open"):
                if found.get(field) is None and row.get(field) is not None:
                    found[field] = row[field]


def _keep(by: str) -> None:
    """Write the run's roster to the store. One roster, whoever walked for it."""
    rows = _rows()
    if not rows:
        return
    with _lock:
        target, mode = dict(_sweep["target"] or HOME_TARGET), _sweep["mode"]
    store.put_roster(target, mode, rows, by)


# -- The sweep -----------------------------------------------------
def _groups_for(account_id: str, wanted: Optional[set]) -> list[dict]:
    """The groups a tabbed read goes through for this account, off the registry.

    `wanted` is the set of group ids the panel asked for, or None for every
    group on record for the account that has not been scraped yet.
    """
    rows = store.read().get("fb_groups") or []
    scraped = {str(r.get("group_id")) for r in rows if r.get("scraped_at")}
    out = []
    for row in rows:
        if str(row.get("account_id")) != str(account_id):
            continue
        gid = str(row.get("group_id") or "")
        if not gid or gid.startswith("name:"):
            continue
        if wanted is None and gid in scraped:
            continue
        if wanted is not None and gid.lower() not in wanted:
            continue
        out.append({"key": gid, "name": row.get("name") or gid,
                    "url": row.get("url") or f"https://www.facebook.com/groups/{gid}/"})
    return out


def _picked(body: dict) -> list[dict[str, Any]]:
    """The accounts this run walks: the ones asked for that have a window up.

    `sessions` is the list the panel sends. `session` is the single-account
    shape the panel used to send and the Actions panel still does. Neither given
    means every open window, which is what pressing Scrape with nothing ticked
    should do rather than nothing at all.
    """
    wanted = body.get("sessions")
    if wanted is None and body.get("session") is not None:
        wanted = [body["session"]]

    live = open_accounts()
    if not live:
        raise fail("No browser window is open - open an account from the "
                   "Accounts panel first.")
    if not wanted:
        return live

    keys = {str(one) for one in wanted}
    picked = [a for a in live if str(a["id"]) in keys]
    if not picked:
        raise fail("None of the accounts you picked have a browser window open.")
    return picked


# Refresh on the FB Groups columns: MinIO's scrape dates first, then each
# picked account's "Your groups" list read off Facebook, so a group joined since
# the last walk shows up under "Unscraped". One at a time for the whole panel.
_refresh: dict[str, Any] = {"task": None, "state": "", "note": "", "found": {}}


def _group_lists() -> dict[str, dict]:
    """Per account: the group keys Facebook listed, and when they were read.

    Kept in the store rather than in memory, so a tab still says when it last
    synced after the server has been restarted. While an account has a list,
    its Unscraped column is exactly that list - not every group ever recorded
    against it, which includes groups a join attempt failed on.
    """
    return store.read().get("group_lists") or {}


def _group_list_save(account_id: str, keys: set) -> None:
    with store.edit() as db:
        lists = db.setdefault("group_lists", {})
        lists[str(account_id)] = {"at": now(), "keys": sorted(k for k in keys if k)}


def _refresh_state() -> dict[str, Any]:
    with _lock:
        return {"task": _refresh["task"], "state": _refresh["state"],
                "note": _refresh["note"], "found": dict(_refresh["found"])}


@router.post("/api/scrape/groups/refresh")
def groups_refresh(body: dict = Body(default={})) -> dict[str, Any]:
    """Scraped groups from fb-groups.txt now; new groups from Facebook as they are read.

    Returns straight away with the MinIO answer. The Facebook half runs as a
    task and puts every group it finds into the registry the moment it is seen,
    so the panel's next look at /api/scrape/groups already has it.
    """
    from . import groups as registry                 # noqa: PLC0415 - the registry's home

    with _lock:
        going = _refresh["state"] == "running"
    if going:
        raise fail("Groups are already being read from Facebook - wait for that one.")
    if _running() is not None:
        raise fail("A scrape is running - stop it before refreshing the groups.")
    accounts = _picked(body)
    account_ids = [str(a["id"]) for a in accounts]
    minio = registry._reg_pull()
    busy.claim(account_ids, "scraping", names={str(a["id"]): as_at(a) for a in accounts})

    def run(task: tasks.Task) -> dict[str, Any]:
        total = 0
        try:
            for account in accounts:
                account_id, label = str(account["id"]), as_at(account)
                pending: list[dict] = []

                def flush() -> None:
                    if pending:
                        registry._reg_upsert(account_id, label, list(pending))
                        pending.clear()

                def on_group(group: dict, account_id=account_id, pending=pending, flush=flush) -> None:
                    pending.append(group)
                    with _lock:
                        _refresh["found"][account_id] = _refresh["found"].get(account_id, 0) + 1
                    if len(pending) >= 5:
                        flush()

                def on_step(said: str, label=label) -> None:
                    with _lock:
                        _refresh["note"] = f"{label}: {said}"
                    task.set(detail=f"{label}: {said}", result=_refresh_state())

                try:
                    automation.open_account(account_id, facebook.JOINED_URL, timeout=20_000)
                except Exception as err:             # noqa: BLE001 - a slow load is still read
                    if "Timeout" not in str(err):
                        raise
                found = automation.with_page(
                    account_id,
                    lambda page, on_group=on_group, on_step=on_step: facebook.list_joined_groups(
                        page, on_group=on_group, on_step=on_step, checkpoint=task.checkpoint),
                    timeout=None) or []
                flush()
                keys = {registry._gid(g.get("url") or "", g.get("name") or "") for g in found}
                if keys:
                    _group_list_save(account_id, keys)
                total += len(found)
                log("scrape", f"{label}: {len(found)} group(s) on Facebook")
            with _lock:
                _refresh.update(state="done", note=f"{total} group(s) read from Facebook")
        except tasks.Stopped:
            with _lock:
                _refresh.update(state="stopped", note="Stopped - the groups read so far are kept.")
            raise
        except Exception as err:                     # noqa: BLE001 - reported to the panel
            with _lock:
                _refresh.update(state="error", note=str(err) or err.__class__.__name__)
            log("scrape", f"reading the groups off Facebook stopped: {err}", "error")
            raise
        finally:
            registry._reg_push_later()
        task.set(result=_refresh_state())
        return _refresh_state()

    with _lock:
        _refresh.update(state="running", note="reading the groups off Facebook", found={})
    try:
        task = tasks.start("reading groups to scrape", run, source="scraping")
    except Exception:
        busy.release(account_ids)
        with _lock:
            _refresh.update(state="", note="")
        raise
    busy.bind(account_ids, task.id)
    with _lock:
        _refresh["task"] = task.id
    return {"task": task.id, "minio": minio}


@router.get("/api/scrape/groups")
def groups_to_scrape(sessions: str = "", pull: int = 1) -> dict[str, Any]:
    """Every known group of the picked accounts, and whether it has been scraped.

    Known means in the groups registry - every group a Group Posting walk,
    Groups joining or an earlier scrape has seen - and mirrored to
    fb-groups.txt, whose scrape dates are brought in first.
    """
    from . import groups as registry                 # noqa: PLC0415 - the registry's home

    live = open_accounts()
    wanted = {s.strip() for s in sessions.split(",") if s.strip()}
    accounts = [a for a in live if not wanted or str(a["id"]) in wanted]
    # pull=0 while Refresh is filling the columns: it already read the bucket,
    # and a MinIO read every poll would be the slowest thing on the panel.
    minio = registry._reg_pull() if pull else {"ok": True, "skipped": True}
    db = store.read()
    for account in accounts:
        account_id = str(account["id"])
        saved = ((db.get("groups") or {}).get(account_id) or {}).get("groups") or []
        if saved:
            registry._reg_upsert(account_id, as_at(account),
                                 [{"name": g.get("name"), "url": g.get("url")} for g in saved])
    rows = store.read().get("fb_groups") or []
    # Scraped: every group any account has scraped - one row per group, the
    # latest scrape. A group scraped from one account is not unscraped on
    # another just because that account is in it too.
    scraped: dict[str, dict] = {}
    for r in rows:
        if not r.get("scraped_at"):
            continue
        gid = str(r.get("group_id"))
        if gid not in scraped or str(r["scraped_at"]) > scraped[gid]["scraped_at"]:
            scraped[gid] = {"group_id": gid, "name": r.get("name") or "", "url": r.get("url") or "",
                            "account_id": str(r.get("account_id")), "account": r.get("account") or "",
                            "scraped_at": str(r["scraped_at"]),
                            "scraped_members": registry._int(r.get("scraped_members")),
                            "members_total": registry._int(r.get("members_total"))}
    lists = _group_lists()
    out = []
    for account in accounts:
        account_id = str(account["id"])
        synced = lists.get(account_id) or {}
        on_facebook = set(synced.get("keys") or []) if synced else None
        out.append({
            "checked": on_facebook is not None,
            "synced_at": synced.get("at") or "",
            "id": account_id,
            "label": as_at(account),
            "rows": [{"group_id": str(r.get("group_id")), "name": r.get("name") or "",
                      "url": r.get("url") or "", "scraped_at": r.get("scraped_at") or "",
                      "scraped_members": registry._int(r.get("scraped_members")),
                      "members_total": registry._int(r.get("members_total"))}
                     for r in rows if str(r.get("account_id")) == account_id
                     and (on_facebook is None or str(r.get("group_id")) in on_facebook)],
        })
    return {"accounts": out,
            "scraped": sorted(scraped.values(), key=lambda g: g["scraped_at"], reverse=True),
            "minio": minio, "refresh": _refresh_state()}


@router.post("/api/scrape/groups/delete")
def groups_delete(body: dict = Body(default={})) -> dict[str, Any]:
    """Forget these groups: their rows go from the registry and fb-groups.txt.

    What is deleted is the RECORD of the group, not the people already
    collected from it - those are in the roster and in usernames_bio.txt, and
    a group's row going does not take them with it.
    """
    from . import groups as registry                 # noqa: PLC0415 - the registry's home

    ids = body.get("group_ids")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        raise fail("Nothing to delete - send the group ids to remove.")
    if _running() is not None:
        raise fail("A scrape is running - stop it before deleting groups.")
    out = registry._reg_delete([str(i) for i in ids])
    log("scrape", f"{out.get('removed', 0)} group(s) removed from the groups list", "warn")
    return out


@router.post("/api/scrape/sweep")
def sweep(body: dict = Body(default={})) -> dict[str, str]:
    """Walk every picked account's home timeline at once, until told to stop.

    Returns a task id straight away. This run has no natural end, and no HTTP
    request should be held open across one.
    """
    if _running() is not None:
        raise fail("A scrape is already running - stop that one first.")

    accounts = _picked(body)
    by = ", ".join(as_at(account) for account in accounts)
    # Spoken for from here until the task ends. Refused with the other
    # service's name if any of them is already driving its window elsewhere.
    account_ids = [str(a["id"]) for a in accounts]
    busy.claim(account_ids, "scraping", names={str(a["id"]): as_at(a) for a in accounts})

    # Which page. The feed unless the panel says the chat list; anything else
    # is a typo in a request and is said so rather than walked as the default.
    source = str(body.get("source") or DEFAULT_SOURCE).strip().lower()
    if source not in SOURCES:
        busy.release(account_ids)
        raise fail(f"Unknown page to scrape: {source!r}. Pick 'feed', 'inbox' or 'groups'.")
    target = dict(SOURCES[source])

    settings = store.settings()
    pause = (float(settings["scroll_pause_min"]), float(settings["scroll_pause_max"]))
    # The feed scrolls at the FB Campaign panel's speed, so the two walks of
    # the same page move the same way.
    feed_speed = float(settings.get("fb_scroll_speed") or 300)
    feed_tick = max(0.1, float(settings.get("fb_scroll_slice_ms") or 900) / 1000.0)
    stall = max(0.0, float(settings.get("scrape_stall_seconds") or 0))
    per_group = max(0, int(float(settings.get("scrape_group_members_max") or 0)))
    group_tabs = max(1, min(8, int(float(settings.get("scrape_group_tabs") or 1))))
    # How the People lists are scrolled, in every tab: smoothly or in jumps,
    # how far each turn, and how long between turns.
    smooth = settings.get("scrape_group_scroll_smooth", True) is not False
    scroll_wait = max(0.2, min(8.0, float(settings.get("scrape_group_scroll_seconds") or 1.2)))
    scroll_step = max(0.2, min(3.0, float(settings.get("scrape_group_scroll_step") or 0.9)))
    # FB groups: which groups to read, per account, as the chooser sent them.
    # None means every group on the account's Facebook list.
    chosen = body.get("only") if isinstance(body.get("only"), dict) else None

    with _lock:
        _sweep["target"] = target
        _sweep["mode"] = source
        _sweep["seen"] = {}
        _sweep["saved_at"] = 0
        _sweep["started_at"] = now()
        _sweep["ended_at"] = ""
        _sweep["lanes"] = [{
            "id": str(account["id"]),
            "account": as_at(account),
            "rows": [],
            "count": 0,
            "running": True,
            "error": "",
            "last_new_at": now(),
            "restarts": 0,
            "status": "opening",
        } for account in accounts]

    def total() -> int:
        with _lock:
            return len(_sweep["seen"])

    def push(task: tasks.Task) -> None:
        found = total()
        task.set(detail=f"{found} accounts off the {target['name'].lower()} of "
                        f"{len(accounts)} account{'' if len(accounts) == 1 else 's'}",
                 result=_state())

    def walk(task: tasks.Task, account: dict[str, Any], lane: dict[str, Any]) -> None:
        """One account's whole walk. Runs on its own thread, start to finish."""
        account_id = str(account["id"])

        failures = 0
        # FB groups: what this lane has already been through, kept OUTSIDE the
        # walker so a walk that dies and is started again goes on from the
        # next group instead of reading every group from the top.
        done_groups: set = set()
        members: dict[str, dict] = {}

        def progress(rows: list[dict]) -> None:
            nonlocal failures
            # Painted as they arrive. A sweep that shows nothing for four minutes
            # and then a full table is one nobody can tell from a hung one.
            if len(rows) > lane["count"]:
                lane["last_new_at"] = now()
                lane["status"] = "walking"
                failures = 0
            _absorb(lane, rows)
            # Saved by the run rather than by the lane: the roster is shared, so
            # the every-twenty-five is counted against the whole of it and not
            # once per account.
            with _lock:
                found = len(_sweep["seen"])
                due = found - _sweep["saved_at"] >= SAVE_EVERY
                if due:
                    _sweep["saved_at"] = found
            if due:
                _keep(by)
            push(task)

        def reloaded(why: str) -> None:
            # The watchdog inside the walk reloaded the page. Counted on the
            # lane and said on the board, and the stall clock starts again -
            # a reload is the answer to the stall, not more of it.
            lane["restarts"] = int(lane.get("restarts") or 0) + 1
            lane["status"] = f"reloaded: {why}"
            lane["last_new_at"] = now()
            log("scrape", f"{lane['account']}: {why} - page reloaded "
                          f"(reload {lane['restarts']})", "warn")
            push(task)

        def tab_state(i: int, state: dict) -> None:
            tabs = list(lane.get("tabs") or [])
            while len(tabs) <= i:
                tabs.append({"group": "", "read": 0, "total": 0, "state": "idle"})
            tabs[i] = state
            lane["tabs"] = tabs
            reading = [t for t in tabs if t.get("group")]
            lane["status"] = (f"{len(reading)} group(s) in {len(tabs)} tab(s)"
                              if reading else "tabs finished")
            push(task)

        def _tab_walk(task, lane, account_id, todo, members, progress):
            """Every group in `todo`, several at a time in tabs of their own."""
            from . import groups as registry          # noqa: PLC0415 - the registry's home
            log("scrape", f"{lane['account']}: reading {len(todo)} group(s) "
                          f"in {group_tabs} tabs")

            def rows_in(group: dict, rows: list[dict], total: int = 0) -> None:
                for row in rows:
                    handle = row.get("handle")
                    if handle and handle not in members:
                        members[handle] = row
                # The group counts as scraped from its first people on, the
                # same as the one-at-a-time read: a tab stopped half way
                # through a big group has still read what it has read.
                registry._reg_mark_scraped(account_id, lane["account"], group.get("name") or "",
                                           group.get("url") or "", len(rows), total)
                progress(list(members.values()))

            def group_done(group: dict, rows: list[dict], finished: bool, total: int = 0) -> None:
                registry._reg_mark_scraped(account_id, lane["account"], group.get("name") or "",
                                           group.get("url") or "", len(rows), total)
                registry._reg_push_later()
                log("scrape", f'{lane["account"]}: "{group.get("name")}" '
                              f'{"read" if finished else "stopped"} - {len(rows)} people, '
                              f'{len(members)} so far')

            try:
                automation.with_context(
                    account_id,
                    lambda ctx: facebook.scrape_groups_in_tabs(
                        ctx, todo, tabs=group_tabs, limit=per_group,
                        gap=scroll_wait, scroll_step=scroll_step, smooth=smooth,
                        on_rows=rows_in, on_tab=tab_state, on_done=group_done,
                        checkpoint=task.checkpoint),
                    timeout=None)
            finally:
                lane["tabs"] = []
            lane["status"] = f"every group read - {len(members)} people"
            return list(members.values())

        def walker(page):
            lane["status"] = "walking"
            if source == "groups":
                def one_group(pg, group: dict) -> None:
                    lane["status"] = f'people in "{group["name"]}"'
                    lane["group"] = {"name": group["name"], "url": group.get("url") or "",
                                     "read": 0, "total": 0}
                    push(task)

                    from . import groups as registry  # noqa: PLC0415
                    # How many people this group has given so far, and how many
                    # of those are already on its registry row. The group is
                    # recorded as scraped the moment its first people come in
                    # and its count kept up as more do - a group of tens of
                    # thousands read for half an hour and then stopped was
                    # otherwise never recorded, and stayed "unscraped" for ever.
                    here = {"read": 0, "marked": -1, "total": 0}

                    def mark() -> None:
                        registry._reg_mark_scraped(account_id, lane["account"], group["name"],
                                                   group["url"], here["read"], here["total"])
                        here["marked"] = here["read"]

                    def progressed(read: int, total: int) -> None:
                        here["total"] = int(total or 0) or here["total"]
                        lane["group"] = {"name": group["name"], "url": group.get("url") or "",
                                         "read": read, "total": total}
                        lane["status"] = (f'"{group["name"]}" - {read}'
                                          + (f" of {total}" if total else "") + " people")
                        push(task)

                    def got(rows: list[dict]) -> None:
                        for row in rows:
                            handle = row.get("handle")
                            if handle and handle not in members:
                                members[handle] = row
                        here["read"] = len(rows)
                        if here["marked"] < 0 or here["read"] - here["marked"] >= 100:
                            mark()
                        progress(list(members.values()))

                    finished = False
                    try:
                        facebook.scrape_group_members(
                            pg, group, limit=per_group, on_rows=got,
                            scroll_step=scroll_step, smooth=smooth,
                            gap=(scroll_wait * 0.7, scroll_wait * 1.3),
                            on_progress=progressed, checkpoint=task.checkpoint)
                        finished = True
                        done_groups.add(group["key"])
                    finally:
                        lane["group"] = None
                        # Read to the end: recorded even with nobody in it.
                        # Stopped part way: recorded if anyone was read.
                        if finished or here["read"]:
                            mark()
                            # Out to fb-groups.txt after every group, not only
                            # when the whole run ends.
                            registry._reg_push_later()
                    log("scrape", f'{lane["account"]}: "{group["name"]}" read - '
                                  f'{here["read"]} people, {len(members)} so far')

                # Several groups at once, a tab each. Only with a list of
                # groups to read: the tabs open a group by its own address, so
                # the list has to come from the registry rather than from the
                # Groups page the one-at-a-time walk reads as it goes.
                if group_tabs > 1:
                    wanted = None
                    if chosen is not None:
                        wanted = {str(g).strip().lower()
                                  for g in (chosen.get(account_id) or []) if str(g).strip()}
                        if not wanted:
                            lane["status"] = "every group read - nothing chosen for this account"
                            return []
                    todo = _groups_for(account_id, wanted)
                    if todo:
                        return _tab_walk(task, lane, account_id, todo, members, progress)
                    lane["status"] = ("no groups on record for this account - "
                                      "press Sync, or set the tabs back to 1")
                    log("scrape", f"{lane['account']}: nothing to read in tabs - "
                                  f"sync the account's groups first", "warn")
                    return []

                only = None
                if chosen is not None:
                    only = {str(g).strip().lower() for g in (chosen.get(account_id) or [])
                            if str(g).strip()}
                    if not only:
                        lane["status"] = "every group read - nothing chosen for this account"
                        return []

                # Each group reached by pressing its name in the Groups list,
                # the same walk Group Posting uses.
                facebook.walk_joined_groups(
                    page, gap=(0.4, 1.0), skip=done_groups, only=only, on_open=one_group,
                    checkpoint=task.checkpoint)
                lane["status"] = f"every group read - {len(members)} people"
                return list(members.values())
            if source == "feed":
                return facebook.walk_authors(
                    page, speed=feed_speed, tick=feed_tick, stall_seconds=stall,
                    on_rows=progress, on_reload=reloaded, checkpoint=task.checkpoint,
                )
            return messenger.walk_home(
                page, pause=pause, stall_seconds=stall,
                on_rows=progress, on_reload=reloaded, checkpoint=task.checkpoint,
            )

        try:
            # A walk that dies is started again, until Stop is pressed or it
            # has died too many times in a row to be worth another go. One
            # window thrown off the feed, closed by hand, or knocked over by a
            # page that would not load must not end this lane while the
            # reason is something a fresh page fixes - and must not take the
            # other lanes down either way.
            while True:
                try:
                    automation.with_page(account_id, walker, timeout=None)
                    break                            # the walk returned on its own (a limit)
                except tasks.Stopped:
                    # Stop was pressed. Not this lane's failure and not the
                    # run's - it is the run ending the way this run always ends.
                    break
                except Exception as err:             # noqa: BLE001 - one dead lane is not the run
                    failures += 1
                    reason = " ".join((str(err) or err.__class__.__name__).split())[:200]
                    lane["restarts"] = int(lane.get("restarts") or 0) + 1
                    if failures > MAX_RESTARTS:
                        lane["error"] = reason
                        lane["status"] = "gave up"
                        log("scrape", f"{lane['account']}: {reason} - gave up after "
                                      f"{failures - 1} restarts", "error")
                        break
                    lane["status"] = f"restarting ({failures}): {reason}"
                    lane["last_new_at"] = now()
                    log("scrape", f"{lane['account']}: {reason} - restarting the walk "
                                  f"in {int(RESTART_WAIT)}s (restart {failures})", "warn")
                    push(task)
                    try:
                        task.checkpoint(RESTART_WAIT)
                    except tasks.Stopped:
                        break
        finally:
            lane["running"] = False
            # A groups walk that got to the end of the list ended on its own,
            # and says so rather than looking as if Stop had been pressed.
            if not lane["error"] and not str(lane.get("status") or "").startswith("every group read"):
                lane["status"] = "stopped"
            push(task)

    def run(task: tasks.Task) -> None:
        log("scrape", f"walking the {target['name'].lower()} on {by}")
        task.set(detail=f"opening the {target['name'].lower()} on {len(accounts)} account"
                        f"{'' if len(accounts) == 1 else 's'}",
                 result=_state())

        walking = [
            threading.Thread(target=walk, args=(task, account, lane),
                             name=f"sweep-{account['id']}", daemon=True)
            for account, lane in zip(accounts, _sweep["lanes"])
        ]
        for thread in walking:
            thread.start()
        try:
            for thread in walking:
                thread.join()
        finally:
            # Stop unwinds through here, and so does a window that ended its own
            # lane. Either way the accounts already collected are the point of
            # having run at all.
            rows = _rows()
            _keep(by)
            if source == "groups":
                # Which groups were read, out to fb-groups.txt with the rest.
                from . import groups as registry     # noqa: PLC0415
                registry._reg_push_later()
            with _lock:
                _sweep["ended_at"] = now()
                _sweep["done"].insert(0, {
                    "key": target["key"], "name": target["name"], "mode": source,
                    "count": len(rows), "by": by,
                })
            log("scrape", f"{target['name'].lower()} on {by} -> {len(rows)} accounts",
                "info" if rows else "warn")

            # And out to the bucket. At the END, not as it goes: the object is
            # the record of one walk, and rewriting it every twenty-five accounts
            # would be the same file uploaded forty times to say what the last
            # one says. Failure here is a warning, never the run's - a harvest
            # that finished is not undone by a MinIO that was not up, and it is
            # still on disk and still re-sendable from the Data panel.
            sent = objects.put_roster(
                rows,
                name=object_name(f"{target['key']}-{source}"),
                source=target["name"],
                at=now(),
            ) if rows else {"ok": False, "reason": "Nothing collected to send."}
            # Why it ended, on the frame, because "stopped" and "Messenger threw us off
            # the feed" are different mornings and the board is where the
            # operator reads which one this was.
            how = "Stopped" if task.stopping else "Ended"
            # Three endings worth telling apart, because "sent" and "sent
            # nothing" are not the same answer and neither is a failure: the
            # bucket keeps each handle once, so a walk that met only accounts it
            # had met before is a normal, successful, empty upload.
            if not sent.get("ok"):
                where = ""
            elif not sent.get("object"):
                where = f" Nothing new for the bucket - all {sent['skipped']} were already in it."
            else:
                where = (f" Sent {sent['new']} new to {sent['bucket']}/{sent['object']}"
                         + (f" ({sent['skipped']} already there)." if sent.get("skipped") else "."))
            # A lane that died says so here rather than only in the feed: half a
            # run is not the same answer as a whole one.
            broke = [lane["account"] for lane in _sweep["lanes"] if lane["error"]]
            trouble = f" {', '.join(broke)} stopped early." if broke else ""
            task.set(result=_state(
                note=f"{how} — {len(rows)} accounts collected and saved "
                     f"across {len(accounts)} account"
                     f"{'' if len(accounts) == 1 else 's'}.{where}{trouble}",
                minio=sent,
            ))

        # After the frame is written, so the board is painted before the task
        # stops being `running` and the console stops polling it.
        if task.stopping:
            raise tasks.Stopped()

    task = tasks.start(f"scraping the {target['name'].lower()}", run, source="scrape")
    busy.bind(account_ids, task.id)
    with _lock:
        _sweep["task"] = task.id
    task.set(result=_state())
    return {"task": task.id}


@router.post("/api/scrape/stop")
def stop() -> dict[str, Any]:
    """End the walk - every lane of it.

    They stop between rows, not mid-hover, and everything collected up to the
    press is already on disk - so this throws nothing away.
    """
    task = _running()
    if task is None:
        raise fail("No scrape is running.")
    task.stop()
    log("scrape", "scrape ended by the operator", "warn")
    return _state(note="Stopping - each account finishes the row it is on.")


@router.post("/api/scrape/members")
def members(body: dict = Body(default={})) -> dict[str, str]:
    """Open each collected profile and fill in what the hover card did not carry.

    Whether the inbox is open, above all: the card gives a bio and a follower
    count without a page load, but nothing short of the profile itself says
    whether a DM can reach the account. This is the expensive step - one page
    load each - which is why it is a call of its own rather than part of the
    walk: on five hundred accounts it is half an hour.

    One account does the reading, whoever walked for the rows. A profile page
    reads the same from any window, so there is nothing to gain from splitting it
    and a second window's worth of page loads to lose.

    Through Firecrawl when it is configured, which changes who does the reading:
    the public facts come off Firecrawl's proxy and no window is needed at all,
    so the re-read runs even with every account closed and spends none of them on
    five hundred navigations. The one thing it cannot bring back is `dm_open` -
    the inbox is only visible to a signed-in viewer - so a Firecrawl re-read
    leaves whatever the walk or an earlier browser re-read learned about the
    inbox standing rather than blanking it. To settle inboxes, turn Firecrawl off
    (or leave it off) and re-read off a window.
    """
    if _running() is not None:
        raise fail("The scrape is still running - stop it first.")

    with _lock:
        target = _sweep["target"]
    rows = _rows()
    if not target:
        raise fail("Nothing has been scraped yet.")
    if not rows:
        raise fail("Nothing collected to re-read.")

    template = str(store.settings()["profile_url"])
    # Firecrawl reads off no account; the browser reads off exactly one. Deciding
    # here keeps the loop from asking the question five hundred times, and means a
    # Firecrawl re-read never touches `working_account` - which would refuse the
    # run for want of a window the Firecrawl path does not use.
    via_firecrawl = firecrawl.enabled()
    if via_firecrawl:
        account = None
        account_id = None
        by = "Firecrawl"
    else:
        account = working_account(body.get("session"))
        account_id = str(account["id"])
        by = as_at(account)

    def read(handle: str) -> dict[str, Any]:
        if via_firecrawl:
            # No window, so no lane to post it to - the read is one HTTP call and
            # runs right here on the task thread.
            return messenger.read_profile(None, handle, template)
        return automation.with_page(
            account_id,
            lambda page, h=handle: messenger.read_profile(page, h, template),
            timeout=180,
        )

    def run(task: tasks.Task) -> None:
        for index, row in enumerate(rows):
            task.checkpoint()
            try:
                found = read(row["handle"])
            except Exception as err:                 # noqa: BLE001 - one dead profile is not a run
                log("scrape", f"@{row['handle']}: {err}", "warn")
                task.set(progress=(index + 1) / len(rows), result=_state())
                continue

            with _lock:
                kept = _sweep["seen"].get(row["handle"])
                if kept is not None:
                    # The row keeps its name and bio if the profile did not give
                    # better ones: the hover card often carries the bio the
                    # profile header truncates.
                    kept["name"] = found.get("name") or kept.get("name") or ""
                    kept["bio"] = found.get("bio") or kept.get("bio") or ""
                    kept["followers"] = found.get("followers")
                    # None means "this read could not tell" - a Firecrawl read
                    # never sees the inbox - so it leaves the last known answer
                    # standing rather than overwriting a real one with a blank.
                    if found.get("dm_open") is not None:
                        kept["dm_open"] = found.get("dm_open")
                    kept["protected"] = found.get("protected", kept.get("protected", False))
                    kept["missing"] = found.get("missing", False)

            task.set(
                progress=(index + 1) / len(rows),
                detail=f"{index + 1} of {len(rows)} profiles",
                result=_state(),
            )
            task.checkpoint(0.8)

        _keep(by)
        how = "via Firecrawl" if via_firecrawl else f"off {target['name']}"
        log("scrape", f"re-read {len(rows)} profiles {how}")
        task.set(result=_state())

    task = tasks.start("re-reading accounts", run, source="scrape")
    task.set(result=_state())
    return {"task": task.id}


@router.post("/api/scrape/open-all")
def open_all(body: dict = Body(default={})) -> dict[str, str]:
    """Open every collected profile in its own tab, to be read by eye."""
    rows = _rows()
    if not rows:
        raise fail("Nothing to open.")

    account = working_account(body.get("session"))
    account_id = str(account["id"])
    template = str(store.settings()["profile_url"])
    handles = [r["handle"] for r in rows]

    def run(task: tasks.Task) -> None:
        done = 0

        def one(context, handle: str) -> None:
            messenger.open_profiles(context, [handle], template, checkpoint=task.checkpoint)

        for handle in handles:
            task.checkpoint()
            automation.with_context(account_id, lambda c, h=handle: one(c, h), timeout=180)
            done += 1
            task.set(progress=done / len(handles), detail=f"{done} of {len(handles)}",
                     result=_state())
        log("browser", f"opened {done} profile tabs")

    task = tasks.start("opening profiles", run, source="browser")
    task.set(result=_state())
    return {"task": task.id}


@router.post("/api/scrape/close-tabs")
def close_tabs(body: dict = Body(default={})) -> dict[str, Any]:
    closed = 0
    for account in store.read()["accounts"]:
        account_id = str(account["id"])
        if automation.is_open(account_id):
            closed += automation.close_extra_tabs(account_id)
    log("browser", f"closed {closed} profile tabs")
    return {"ok": True, "closed": closed}


@router.post("/api/scrape/reset")
def reset() -> dict[str, Any]:
    """Throw away the open run - not what it already saved.

    The rosters it wrote stay in the Data panel. This clears the board, and a
    button that also deleted the morning's collection would be one nobody could
    press without checking first.
    """
    if _running() is not None:
        raise fail("The scrape is still running - stop it first.")
    with _lock:
        _sweep["target"] = None
        _sweep["seen"] = {}
        _sweep["lanes"] = []
        _sweep["done"] = []
        _sweep["task"] = None
        _sweep["saved_at"] = 0
    log("scrape", "run reset", "warn")
    return {"ok": True}


@router.get("/api/scrape/status")
def status() -> dict[str, Any]:
    """The board, the lanes, and the walk if one is going.

    A run that does not stop on its own will outlive the page that started it -
    a reload, a second tab, a laptop lid. This is how the console finds the walk
    again and picks the watch back up, instead of showing an idle panel over
    browsers that are still scrolling.
    """
    task = _running()
    return _state(task=task.id if task else None, running=task is not None)


@router.post("/api/scrape/chat-list")
def chat_list(body: dict = Body(default={})) -> dict[str, Any]:
    """Read the chat list NOW and hand it back. Nothing is stored.

    The sweep at `/api/scrape/sweep` is a run: it walks until the operator
    presses Stop, writes what it finds into the account store, and answers with
    a task id. That is the right shape for collecting a roster over minutes and
    the wrong shape for a button that has to fill a recipient list on the press.

    So this is the other half - `messenger.read_chat_list` scrolls the list once,
    settles, and returns. The rows go straight to the caller and never touch the
    store, because the question being asked is "who is in this account's chat
    list right now", not "what has ever been collected".

    Answers per account, because two windows are two inboxes and merging them
    would tell the operator they have one list when they have two.
    """
    accounts = _picked(body)
    limit = int(body.get("limit") or 0)

    out: list[dict[str, Any]] = []
    problems: list[dict[str, str]] = []
    for account in accounts:
        aid = str(account["id"])
        try:
            rows = automation.with_page(
                aid, lambda page: messenger.read_chat_list(page, limit=limit),
                timeout=300)
        except Exception as err:                     # noqa: BLE001 - reported per account
            # One signed-out window must not sink the others: the panel shows
            # what worked and says which did not, rather than failing whole.
            problems.append({"account": as_at(account), "error": str(err)})
            log("scrape", f"could not read the chat list on {as_at(account)}: {err}", "warn")
            continue
        for row in rows:
            row["target"] = as_at(account)
            out.append(row)
        log("scrape", f"read {len(rows)} conversations off {as_at(account)}")

    return {"rows": out, "count": len(out), "problems": problems}


@router.get("/api/scrape/accounts")
def accounts() -> dict[str, Any]:
    """Which accounts a sweep could run on: the ones with a window up.

    Read live off the browser rather than out of the store - a window the
    operator closed by hand is closed, whatever the file says - and this is the
    list the panel ticks off.
    """
    open_ones = open_accounts()
    with _lock:
        walking = {lane["id"]: lane for lane in _sweep["lanes"]}
    return {"accounts": [{
        "id": str(account["id"]),
        "account": as_at(account),
        "label": account.get("label", ""),
        "count": walking.get(str(account["id"]), {}).get("count", 0),
        "running": bool(walking.get(str(account["id"]), {}).get("running")),
        "error": walking.get(str(account["id"]), {}).get("error", ""),
    } for account in open_ones]}
