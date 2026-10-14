"""Panel - FB Campaign.

Two buttons and one window. **Open Facebook** puts facebook.com on screen in a
real Chrome window running on an account's own profile; **Start** walks down the
feed in that window at a human speed until Stop is pressed.

It is deliberately not the scrape. The Scraping panel walks Messenger's chat
list and comes back with a roster; this reads nothing and saves nothing. What it
produces is the account having been ON Facebook, scrolling it the way the person
whose account it is would - which is a different job from collecting anything,
and one that wants its own screen rather than a checkbox on somebody else's.

Like Scraping and Actions it drives the live browser window. Unlike them, its
first button does not need one: opening Facebook is how a window gets opened, so
`open_facebook` falls back to the first account on the list when nothing is up.
`start` is the one that needs a window, and says so with the same sentence.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Optional

from fastapi import APIRouter, Body

from .. import busy, tasks
from ..automation import facebook
from ..automation.browser import automation
from ..events import log
from ..store import store
from ._common import as_at, fail, find_account, open_accounts, working_account

router = APIRouter()

# The walk's own state, mirrored here so `/api/fb/status` answers instantly.
# The task holds the truth about whether it is running; this holds what the
# panel puts on screen, which the poller asks for every 900ms and must never
# queue behind a page.
_lock = threading.Lock()
_walk: dict[str, Any] = {
    "task": "",
    "state": "idle",         # idle | running | stopped | done
    "account": "",           # every account on the run, comma-joined
    "note": "",
    "passes": 0,             # the sums across lanes - see _state
    "posts": 0,
    "commented": 0,
    "failed": 0,
    "comment": "",           # the text this run is commenting, for the locked box
    "tabs": 1,               # how many tabs each account scrolls in
    "lanes": {},             # account id -> that account's own walk, see _lane
}
_WALK_IDLE = copy.deepcopy(_walk)


def reset_idle() -> None:
    """The panel as it was when the server started. Only with nothing running."""
    with _lock:
        _walk.clear()
        _walk.update(copy.deepcopy(_WALK_IDLE))


def _lane(account: dict[str, Any]) -> dict[str, Any]:
    """One account's share of a run: its own state, note and counters.

    A run on three accounts is three feeds being scrolled and three comment
    counts, and the panel shows each on a tab of its own. The run-level fields
    above are the sums, kept so a page written for one account still reads.
    """
    return {"id": str(account["id"]), "account": as_at(account), "state": "idle",
            "note": "Opening Facebook.", "passes": 0, "posts": 0, "commented": 0,
            "failed": 0, "error": ""}


def _state(**over: Any) -> dict[str, Any]:
    with _lock:
        out = {k: v for k, v in _walk.items() if k != "lanes"}
        lanes = [dict(lane) for lane in _walk["lanes"].values()]
    out["lanes"] = lanes
    if lanes:
        for key in ("passes", "posts", "commented", "failed"):
            out[key] = sum(int(lane.get(key) or 0) for lane in lanes)
        out["account"] = ", ".join(lane["account"] for lane in lanes)
    out.update(over)
    return out


def _running() -> Optional[tasks.Task]:
    task = tasks.get(_walk.get("task") or "")
    return task if task and task.snapshot().get("state") == "running" else None


@router.get("/api/fb/status")
def status() -> dict[str, Any]:
    """What the panel paints: the walk, and whether there is a window to run in.

    `windows` is here so the panel can tell its two buttons apart without a
    second request - Open works with none, Start does not.
    """
    out = _state()
    # What the comment box shows: the running run's text while one is running
    # (the box is locked over it), otherwise the remembered setting.
    if out["state"] != "running" or not out.get("comment"):
        out["comment"] = str(store.settings().get("fb_comment_text") or "")
    if out["state"] != "running":
        out["tabs"] = _tabs(store.settings().get("fb_tabs"))
    return {**out,
            "windows": [{"id": str(a["id"]), "label": as_at(a)}
                        for a in open_accounts()]}


def _offsets(settings: dict) -> tuple[str, ...]:
    """The sprite offsets that mean "comment bubble", from Settings.

    Comma-separated so several builds can be covered at once, and falling back
    to the built-in default rather than to nothing: an empty setting that
    disabled commenting outright would be a confusing way to spell a typo.
    """
    raw = str(settings.get("fb_comment_icon_offsets") or "").strip()
    found = tuple(part.strip() for part in raw.split(",") if part.strip())
    return found or facebook.COMMENT_ICON_OFFSETS


def _tabs(raw: Any) -> int:
    """How many tabs a run scrolls in. 1 to 10; anything unreadable is 1."""
    try:
        return max(1, min(10, int(float(raw or 1))))
    except (TypeError, ValueError):
        return 1


def _account_for_open(body: dict) -> dict[str, Any]:
    """Whose window Facebook opens in.

    Three readings, narrowest first: the account the panel named, then whichever
    window is already up, then the first account on the list. The last is what
    makes this button the one that does not need a window - it launches one.
    """
    named = body.get("account") or body.get("session")
    if named:
        return find_account(str(named).lstrip("@"))
    live = open_accounts()
    if live:
        return live[0]
    accounts = store.read()["accounts"]
    if not accounts:
        raise fail("There are no accounts yet - add one on the Accounts panel first.")
    return accounts[0]


@router.post("/api/fb/open")
def open_facebook(body: dict = Body(default={})) -> dict[str, Any]:
    """Open facebook.com in that account's window, launching one if need be.

    Answered inline rather than as a task: it is one navigation, and the whole
    point of the button is that the window is there when it returns.
    """
    account = _account_for_open(body)
    account_id = str(account["id"])

    # `open_account` launches the window if there is not one and navigates it;
    # `settle` only waits and reports. Deliberately not `open_home` here - that
    # would navigate to the page we are already on, and the operator would watch
    # Facebook load twice.
    #
    # The navigation is given 20s rather than `goto`'s 45, and a timeout on it
    # is NOT the end of the button. This request is answered inline, so every
    # second spent here is a second the operator watches a spinning button with
    # nothing said - and worse, `domcontentloaded` on a challenged page can be
    # slow precisely BECAUSE of the captcha iframe holding it open. Raising
    # there threw away a page that was already on screen and told the operator
    # the load failed, when the thing they had to act on was sitting in front of
    # them. So: navigate, and then go and look at the window either way. `settle`
    # names a checkpoint in well under a second, and `url` is read back off the
    # page rather than assumed.
    #
    # Only a NAVIGATION timeout is walked past. A window that would not launch
    # at all is a different failure with nothing on screen to go and read, and
    # swallowing it here would turn "Chrome never started" into a puzzling
    # "the feed has not appeared" a few seconds later.
    try:
        url = automation.open_account(account_id, facebook.HOME_URL, timeout=20_000)
    except Exception as err:                         # noqa: BLE001 - re-raised unless slow
        if "Timeout" not in str(err):
            raise
        log("facebook", f"facebook was slow to load on {as_at(account)} - "
                        "reading the window anyway", "warn")
        url = ""
    found = automation.with_page(account_id, facebook.settle, timeout=180)
    if not url:
        url = automation.with_page(account_id, lambda page: page.url, timeout=30) or ""

    log("facebook", f"opened facebook on {as_at(account)} - {found}")
    with _lock:
        _walk["account"] = as_at(account)
        if _walk["state"] != "running":
            _walk["note"] = found
    return {"ok": True, "url": url, "account": as_at(account), "found": found,
            **_state()}


def _accounts_for_start(body: dict) -> list[dict[str, Any]]:
    """Whose windows the walk runs in - several, if the panel ticked several.

    `accounts` is the list the panel sends. The single-account spellings are
    kept for a caller written before there was a list, and with nothing named
    at all it falls back the way Open Facebook does: whichever window is up,
    else the first account on the list, launched.
    """
    named = body.get("accounts")
    if isinstance(named, list) and named:
        seen: list[dict[str, Any]] = []
        for one in named:
            account = find_account(str(one).lstrip("@"))
            if all(str(account["id"]) != str(x["id"]) for x in seen):
                seen.append(account)
        return seen
    return [_account_for_open(body)]


@router.post("/api/fb/start")
def start(body: dict = Body(default={})) -> dict[str, Any]:
    """Start scrolling the feed on every picked account at once.

    Returns a task id straight away. This run has no natural end - a home feed
    is infinite - so no HTTP request is held across it. It ends when Stop is
    pressed, or when every account has hit the comment cap.

    One task, one thread per account. Each account walks its own window on
    its own lane in `automation`, with its own counters on its own tab of the
    panel; the run is over when the last of them is.
    """
    if _running() is not None:
        raise fail("The feed is already being scrolled - stop that first.")

    accounts = _accounts_for_start(body)
    account_ids = [str(a["id"]) for a in accounts]
    # Spoken for until the task ends. Refused with the other service's name if
    # one of them is being walked by Scraping or written from by a campaign.
    busy.claim(account_ids, "fb", names={str(a["id"]): as_at(a) for a in accounts})

    settings = store.settings()
    # Not `scroll_pause_min/max` - those are the Messenger sweep's, and they
    # describe a rest between steps, which is the shape this walk no longer has.
    speed = float(settings.get("fb_scroll_speed") or 90)
    slice_ms = int(float(settings.get("fb_scroll_slice_ms") or 900))

    # `body` wins over the setting so a caller can send one comment run without
    # editing Settings; the setting is the default rather than the law.
    comment_text = body.get("comment")
    if comment_text is None:
        comment_text = settings.get("fb_comment_text") or ""
    comment_text = str(comment_text).strip()
    # Remembered. The box on the panel is the place the text is typed now, and
    # a text typed there should be there again after a reload - and be what the
    # Click comments buttons use too.
    if comment_text and comment_text != str(settings.get("fb_comment_text") or ""):
        with store.edit() as db:
            db["settings"]["fb_comment_text"] = comment_text
    comment_limit = int(float(settings.get("fb_comment_limit") or 0))
    comment_gap = (float(settings.get("fb_comment_gap_min") or 0),
                   float(settings.get("fb_comment_gap_max") or 0))
    comment_wpm = int(float(settings.get("typing_wpm") or 240))
    comment_offsets = _offsets(settings)
    # The watchdog: reload facebook when no comment has landed for this long.
    # Absent (a store written before the setting existed) means the default,
    # not off: `or 0` here would have silently disabled it on every old store.
    stall_seconds = float(settings.get("fb_refresh_after", 60) or 0)
    like_first = bool(settings.get("fb_like_first", True))

    # Tabs: the box wins over the setting, and is remembered as it, the same
    # way as the comment text. Each extra tab is another copy of facebook.com
    # in the same window, and the walk takes turns between them.
    tabs = _tabs(body.get("tabs") if body.get("tabs") is not None
                 else settings.get("fb_tabs"))
    if tabs != _tabs(settings.get("fb_tabs")):
        with store.edit() as db:
            db["settings"]["fb_tabs"] = tabs

    with _lock:
        _walk.update(state="running", account=", ".join(as_at(a) for a in accounts),
                     passes=0, posts=0, commented=0, failed=0, comment=comment_text,
                     tabs=tabs, note="Opening Facebook.",
                     lanes={str(a["id"]): _lane(a) for a in accounts})

    def walk_one(task: tasks.Task, account: dict[str, Any], lane: dict[str, Any]) -> None:
        """One account's whole walk, on its own thread, start to finish."""
        account_id = str(account["id"])
        who = lane["account"]

        def paint() -> None:
            done = sum(int(l.get("commented") or 0) for l in _walk["lanes"].values())
            posts = sum(int(l.get("posts") or 0) for l in _walk["lanes"].values())
            detail = f"{posts} posts on screen"
            if comment_text:
                detail += f", {done} commented"
            task.set(detail=detail, result=_state())

        def on_pass(seen: dict) -> None:
            with _lock:
                lane["state"] = "running"
                lane["passes"] = seen["passes"]
                lane["posts"] = seen["posts"]
                lane["commented"] = seen.get("commented", 0)
                lane["failed"] = seen.get("failed", 0)
            paint()

        def on_note(text: str) -> None:
            with _lock:
                lane["note"] = f"Not stopping on anything - {text}"
            log("facebook", f"{who}: nothing to comment on: {text}", "warn")
            paint()

        def on_reload(why: str) -> None:
            with _lock:
                lane["note"] = f"Reloading Facebook - {why}."
            log("facebook", f"{who}: reloading facebook: {why}", "warn")
            paint()

        def where(seen: dict) -> str:
            return f"Tab {seen['tab']}: " if tabs > 1 and seen.get("tab") else ""

        def on_claim(found: dict) -> None:
            with _lock:
                lane["note"] = (f'{where(found)}stopped on a post - opening '
                                f'comments and typing "{comment_text}".')
            paint()

        def on_comment(done: dict) -> None:
            with _lock:
                lane["commented"] = done["commented"]
                lane["failed"] = done["failed"]
                lane["note"] = (f'Scrolling and commenting "{comment_text}".'
                                if done["ok"] else
                                f'{where(done)}could not comment on that post - '
                                f'{done["note"]}')
            # Logged one line per comment, at the level the outcome deserves,
            # and with the account in front so three lanes read apart.
            log("facebook",
                f'{who}: {where(done)}{done["note"]} "{comment_text}" on {done["key"] or "a post"}'
                if done["ok"] else
                f'{who}: {where(done)}could not comment on {done["key"] or "a post"} - '
                f'{done["note"]}',
                "info" if done["ok"] else "warn")
            paint()

        try:
            with _lock:
                lane["state"] = "running"
            paint()
            # The feed first, then the walk. The window may be sitting on
            # Messenger, so the walk puts Facebook on screen itself. A slow
            # navigation is walked past the way Open Facebook walks past it.
            try:
                automation.open_account(account_id, facebook.HOME_URL, timeout=20_000)
            except Exception as err:                 # noqa: BLE001 - re-raised unless slow
                if "Timeout" not in str(err):
                    raise
            automation.with_page(account_id, facebook.open_home, timeout=180)

            def drive(page):
                """Open the extra tabs, walk them all, close the extras after.

                All on the lane's own thread, inside one call: the pages are
                Playwright objects and may only be touched from the thread
                that made them.
                """
                pages = [page]
                try:
                    for n in range(2, tabs + 1):
                        with _lock:
                            lane["note"] = f"Opening tab {n} of {tabs}."
                        paint()
                        extra = page.context.new_page()
                        pages.append(extra)
                        facebook.open_home(extra)
                    with _lock:
                        lane["note"] = (f'Scrolling and commenting "{comment_text}".'
                                        if comment_text else "Scrolling.")
                        if tabs > 1:
                            lane["note"] = f"{tabs} tabs: " + lane["note"]
                    paint()
                    return facebook.walk_tabs(
                        pages, speed=speed, slice_ms=slice_ms,
                        comment_text=comment_text, comment_wpm=comment_wpm,
                        comment_gap=comment_gap, comment_limit=comment_limit,
                        comment_offsets=comment_offsets,
                        stall_seconds=stall_seconds, like_first=like_first,
                        on_claim=on_claim, on_comment=on_comment, on_note=on_note,
                        on_reload=on_reload,
                        on_pass=on_pass, checkpoint=task.checkpoint)
                finally:
                    for extra in pages[1:]:
                        try:
                            extra.close()
                        except Exception:            # noqa: BLE001 - already gone
                            pass
                    try:
                        page.bring_to_front()
                    except Exception:                # noqa: BLE001 - window gone
                        pass

            automation.with_page(account_id, drive, timeout=None)
        except tasks.Stopped:
            with _lock:
                lane.update(state="stopped", note="Stopped.")
            paint()
            return
        except Exception as err:                     # noqa: BLE001 - one lane, not the run
            # One window that would not open, or was closed by hand, must not
            # take the other accounts' walks down with it.
            reason = str(err) or err.__class__.__name__
            with _lock:
                lane.update(state="done", note=reason, error=reason)
            log("facebook", f"{who}: the walk stopped: {reason}", "error")
            paint()
            return
        with _lock:
            done = lane.get("commented") or 0
            missed = lane.get("failed") or 0
            note = "Finished."
            if comment_text:
                note = f"Finished - {done} commented"
                if missed:
                    note += f", {missed} could not be"
                if comment_limit and done >= comment_limit:
                    note += f" (the {comment_limit}-comment cap)"
                note += "."
            lane.update(state="done", note=note)
        paint()

    def run(task: tasks.Task) -> None:
        with _lock:
            lanes = list(_walk["lanes"].values())
        threads = [
            threading.Thread(target=walk_one, args=(task, account, lane),
                             name=f"fb-{account['id']}", daemon=True)
            for account, lane in zip(accounts, lanes)
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                thread.join()
        finally:
            with _lock:
                if task.stopping:
                    _walk.update(state="stopped", note="Stopped.")
                else:
                    broke = [l for l in lanes if l.get("error")]
                    if len(lanes) == 1:
                        _walk.update(state="done", note=lanes[0]["note"])
                    else:
                        said = "; ".join(f"{l['account']}: {l['note']}" for l in lanes)
                        _walk.update(state="done", note=said)
                    if broke and len(broke) == len(lanes):
                        log("facebook", "every account's walk stopped early", "error")
            task.set(result=_state())
        if task.stopping:
            raise tasks.Stopped()

    task = tasks.start("scrolling facebook", run, source="facebook")
    busy.bind(account_ids, task.id)
    with _lock:
        _walk["task"] = task.id
    by = ", ".join(as_at(a) for a in accounts)
    log("facebook",
        f'scrolling the feed on {by} and commenting "{comment_text}"'
        f' on up to {comment_limit or "every"} post(s) per account'
        if comment_text else f"scrolling the feed on {by}")
    task.set(result=_state())
    return {"task": task.id, **_state()}


@router.post("/api/fb/comment")
def comment_once(body: dict = Body(default={})) -> dict[str, Any]:
    """Open the comments on the post on screen. **Nothing scrolls.**

    The walk and this share every line that matters - the same claim, the same
    icon lookup, the same click - so a fix here is a fix there. What it drops is
    the scrolling, the pacing and the cap, which is everything that makes the
    walk hard to watch: press it, and one post's comments open in front of you.

    `text` (or `fb_comment_text`) is typed and sent unless `click_only` is set,
    which stops with the composer open and types nothing.

    Answered inline. It is one post, and the whole point is that the result is
    the thing that happened rather than a task id to go and look up.
    """
    account = working_account(body.get("session") or body.get("account"))
    account_id = str(account["id"])
    settings = store.settings()

    click_only = bool(body.get("click_only"))
    text = body.get("text")
    if text is None:
        text = settings.get("fb_comment_text") or ""
    text = str(text).strip()
    offsets = _offsets(settings)
    wpm = int(float(settings.get("typing_wpm") or 240))

    steps: list[str] = []

    # Facebook has to be ON, but this must not throw the page away to get there.
    # Navigating unconditionally would reload the feed and lose the post the
    # operator is looking at - the one they pressed the button for.
    def ensure(page) -> str:
        if "facebook.com" not in (page.url or ""):
            return facebook.open_home(page)
        return facebook.settle(page, timeout=8_000)

    where = automation.with_page(account_id, ensure, timeout=180)
    steps.append(f"facebook: {where}")

    blocked = automation.with_page(account_id, facebook.challenge_showing, timeout=30)
    if blocked:
        raise fail(f"Cannot comment - {blocked}.")

    found = automation.with_page(account_id, facebook.survey, timeout=60) or {}
    claim = found.get("claim")
    if not claim:
        raise fail("Nothing to comment on — " + facebook.explain(found.get("why") or {}))
    steps.append(f"claimed a post: {claim.get('key') or 'unidentified'}")

    def act(page):
        return facebook.comment_on_claimed(
            page, text, wpm=wpm, offsets=offsets,
            click_only=click_only, on_step=steps.append,
            like_first=bool(settings.get("fb_like_first", True)))

    try:
        note = automation.with_page(account_id, act, timeout=180)
        ok = note in ("commented", "liked and commented", "opened the comments")
    except Exception as err:                         # noqa: BLE001 - reported, not raised
        note, ok = str(err) or err.__class__.__name__, False

    # Released as a LOOK when only opening: the post has not been commented on,
    # so a later walk should still be free to pick it up.
    automation.with_page(
        account_id,
        lambda page: facebook.release_post(page, done=None if click_only else ok),
        timeout=60)

    log("facebook", f"{note} on {as_at(account)}", "info" if ok else "warn")
    with _lock:
        _walk["note"] = note if ok else f"Could not comment — {note}"
    return {"ok": ok, "account": as_at(account), "note": note,
            "post": claim.get("key") or "", "steps": steps, **_state()}


@router.post("/api/fb/probe")
def probe(body: dict = Body(default={})) -> dict[str, Any]:
    """Ask the open window what it can see, without touching anything.

    The one thing worth having when the walk scrolls past every post and says
    nothing: it runs the picker's own filters against whatever is on screen now
    and reports the tally, so "it is not stopping on posts" becomes a specific
    answer - the selector matches nothing, or everything on screen is a comment
    rather than a post, or the posts are there but carry no comment button.

    Deliberately the picker's OWN code rather than a second copy of the filters.
    A probe that agrees with a walk it does not share logic with is worthless.
    """
    account = working_account(body.get("session") or body.get("account"))
    account_id = str(account["id"])

    offsets = _offsets(store.settings())
    found = automation.with_page(account_id, facebook.survey, timeout=60) or {}
    why = found.get("why") or {}
    claim = found.get("claim") or None

    # With a post claimed, go the second step too and ask what the comment
    # control looks like on it. This is the half that answers the actual
    # question - the walk finding posts but never clicking anything - and it
    # comes back with every icon offset on the post, which is the new value to
    # paste into Settings when Facebook reissues its sprite sheet.
    button: dict[str, Any] = {}
    if claim:
        button = automation.with_page(
            account_id,
            lambda page: facebook.find_comment_button(page, offsets=offsets),
            timeout=60) or {}

    # The claim is released with `None`: this is a look, not a turn. Leaving the
    # post stamped would hand the next walk a target it never picked, and
    # releasing it as failed would blacklist the very post being reported on.
    if claim:
        automation.with_page(
            account_id, lambda page: facebook.release_post(page, done=None), timeout=60)

    return {"ok": True, "account": as_at(account),
            "reading": facebook.explain(why),
            "would_claim": (claim or {}).get("key") or "",
            "viewport_height": found.get("vh") or 0,
            "counts": why,
            # The comment control on that post: found or not, how it was found,
            # and every sprite offset present. `looking_for` is echoed back so
            # the two can be compared without opening Settings in another tab.
            "comment_button": {
                "found": bool(button.get("found")),
                "via": button.get("via") or "",
                "why_not": button.get("why") or "",
                "looking_for": list(offsets),
                "offsets_on_post": button.get("seen") or [],
                "size": f'{button.get("width", 0)}x{button.get("height", 0)}',
            } if claim else {}}


@router.post("/api/fb/stop")
def stop() -> dict[str, Any]:
    """End the walk. It stops between passes, never mid-scroll."""
    task = _running()
    if task is None:
        raise fail("The feed is not being scrolled.")
    task.stop()
    with _lock:
        _walk.update(state="stopped", note="Stopping - it finishes the pass it is on.")
        for lane in _walk["lanes"].values():
            if lane.get("state") == "running":
                lane["note"] = "Stopping - it finishes the pass it is on."
    log("facebook", "the walk was ended by the operator", "warn")
    return _state()
