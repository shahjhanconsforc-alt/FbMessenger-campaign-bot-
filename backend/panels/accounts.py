"""Panel 2 - Accounts.

One card per Messenger account: the handle, the email Facebook sends its verification
challenges to, the password behind an eye. A blue card is a stored session.

Two words that look alike and are not:

  * `active`  - a browser window is open on that profile, right now.
  * `logged_in` - that profile's cookie jar was signed in when it was last read.

An account can be logged in with no window open (the usual state between runs)
and open without being signed in (the state during a login that stalled on a
challenge). The Scraping gate reads the first; the campaign checklist reads the
second.
"""

from __future__ import annotations

import random
import time
from typing import Any, Optional

from fastapi import APIRouter, Body

from .. import busy, tasks
from ..automation import messenger
from ..automation.browser import automation
from ..events import log
from ..store import now, store
from ._common import fail, find_account

router = APIRouter()


def _session(db: dict, account_id: str) -> dict[str, Any]:
    return db["sessions"].setdefault(
        str(account_id), {"logged_in": False, "active": False, "detail": "no session"}
    )


def _record(account_id: str, login: str, found: dict[str, Any]) -> dict[str, Any]:
    """Write what a look at the window found, and say it the one way.

    Two things read a window and store the answer - the sign-in watcher and
    Check sessions - and for a while they wrote the same six fields in two
    places with two different sentences for the same state. The card only ever
    shows one of them at a time, so the difference read as the console changing
    its mind.
    """
    signed_in = bool(found.get("logged_in"))
    as_handle = found.get("handle")
    # The handle on the card and the handle in the browser disagreeing is the
    # failure worth shouting about: a run against it sends from the wrong
    # account and never errors once.
    mismatch = bool(as_handle and as_handle.lower() != login.lower())

    with store.edit() as db:
        session = _session(db, account_id)
        session["logged_in"] = signed_in
        session["active"] = automation.is_open(account_id)
        session["as_handle"] = as_handle
        session["mismatch"] = mismatch
        session["detail"] = (
            f"signed in as @{as_handle} - NOT @{login}" if mismatch
            else f"signed in as @{as_handle}" if as_handle
            else "signed in" if signed_in
            else "signed out"
        )
        # Facebook's own session cookie is the authority on this; 30 days is what it
        # issues, and it is shown as an expectation, not a promise.
        session["expires_at"] = _in_30_days() if signed_in else None
        # WHEN this was found out, which is a different question from what was
        # found. `logged_in` is only ever as good as the last look at a window,
        # and a closed profile is never looked at - so without a timestamp a
        # card cannot tell an answer from an hour ago apart from one from six
        # weeks ago, and says "signed in" with equal confidence about both.
        session["checked_at"] = now()
        return dict(session)


@router.get("/api/accounts/busy")
def busy_accounts() -> dict[str, Any]:
    """Which accounts a run is using right now, and which service has them.

    Polled by every panel with an account picker, so an account that Scraping
    is walking is greyed out on FB Campaign and Campaign, and the other way
    round. The refusal itself lives in `busy.claim`; this is only the picture.
    """
    return {"busy": busy.holders()}


@router.get("/api/accounts")
def list_accounts() -> dict[str, Any]:
    db = store.read()
    return {
        "accounts": [
            {
                "id": a["id"],
                "label": a.get("label", ""),
                "login": a["login"],
                "email": a.get("email", ""),
                # The password itself is never in a list response. It is one
                # request away, behind the eye, so a screenshot of this panel is
                # not a screenshot of the passwords.
                "has_password": bool(db["secrets"].get(str(a["id"]))),
            }
            for a in db["accounts"]
        ]
    }


@router.post("/api/accounts")
def add_account(body: dict = Body(default={})) -> dict[str, Any]:
    login_name = str(body.get("login") or "").lstrip("@").strip()
    if not login_name:
        raise fail("A handle is required.")

    with store.edit() as db:
        if any(a["login"].lower() == login_name.lower() for a in db["accounts"]):
            raise fail(f"@{login_name} is already on the list.")
        account_id = str(db["next_account"])
        db["next_account"] += 1
        db["accounts"].append({
            "id": account_id,
            "label": str(body.get("label") or ""),
            "login": login_name,
            "email": str(body.get("email") or ""),
        })
        if body.get("password"):
            db["secrets"][account_id] = str(body["password"])
        _session(db, account_id)

    log("accounts", f"added @{login_name}")
    return {"id": account_id}


@router.delete("/api/accounts/{account_id}")
def remove_account(account_id: str) -> dict[str, Any]:
    automation.close_account(str(account_id))
    with store.edit() as db:
        db["accounts"] = [a for a in db["accounts"] if str(a["id"]) != str(account_id)]
        db["secrets"].pop(str(account_id), None)
        db["sessions"].pop(str(account_id), None)
    # The Chrome profile is left on disk. Removing a card is a change to this
    # list, and a directory holding a live session is not something to delete on
    # the strength of a click that said "remove from the list".
    log("accounts", f"removed account {account_id}", "warn")
    return {"ok": True}


@router.get("/api/accounts/{account_id}/secret")
def reveal(account_id: str) -> dict[str, str]:
    find_account(account_id)
    log("accounts", f"revealed the password for account {account_id}", "warn")
    return {"password": store.read()["secrets"].get(str(account_id), "")}


@router.post("/api/accounts/{account_id}/open")
def open_window(account_id: str) -> dict[str, str]:
    """Launch Chrome on that account's own profile directory.

    Per account, not per run: the profile IS the session, and two accounts
    sharing one would be one account as far as Facebook is concerned.
    """
    account = find_account(account_id)
    url = automation.open_account(str(account_id))

    with store.edit() as db:
        session = _session(db, account_id)
        session["active"] = True
        session["detail"] = "window open"

    log("browser", f"opened a window on @{account['login']}")
    return {"url": url}


# How long the watcher stays with a sign-in before it gives up. Long, because
# what it is waiting for is a person: an SSO round trip through Google, a code
# out of an email, a captcha. It gives up on the WATCHING, never on the window -
# whatever is signed in there is still there, and Check login status will find
# it.
SIGN_IN_WINDOW = 15 * 60


@router.post("/api/accounts/{account_id}/session")
def save_session(account_id: str) -> dict[str, str]:
    """The one button on a card: open the window, keep whatever signs in.

    What this replaces. Signing an account in used to be three controls that had
    to be pressed in an order nothing stated: `Log in` (which does nothing for an
    account with no password, because there is nothing to type into Facebook's form),
    the `@` icon to put a Google session in the profile first, and then `Check
    login status` to notice any of it had happened. Miss the last one and a
    session that was signed in read as "no session" on the card.

    So: one button. It opens this account's own Chrome profile on messenger.com
    and then
    WATCHES, every few seconds, without touching the page - see `messenger.peek`, which
    reads what is on screen rather than navigating to find out, because the
    operator is typing into that window. The moment the window is signed in it
    records the handle, and the Google address behind it if the sign-in went
    through Google, against this card.

    The session, and this is the whole point: it lives in the same
    profile directory, so "saved" is not a copy of anything - it is that
    directory. Pressing the button again on a saved card reopens that profile
    with both sessions already in it, which is what makes the second sign-in no
    sign-in at all.
    """
    account = find_account(account_id)
    aid = str(account_id)

    def run(task: tasks.Task) -> None:
        task.set(detail="opening the window", progress=0.02)
        automation.open_account(aid, messenger.HOME_URL)

        with store.edit() as db:
            session = _session(db, aid)
            session["active"] = True
            session["detail"] = "window open - waiting for the sign-in"

        started = time.monotonic()
        found: dict[str, Any] = {"logged_in": False, "handle": None}

        while True:
            task.checkpoint(3.0)
            waited = time.monotonic() - started

            # Closed by hand. Not a failure - the operator is done, or has
            # decided not to - and whatever ended up in the profile is still in
            # the profile.
            if not automation.is_open(aid):
                with store.edit() as db:
                    _session(db, aid)["active"] = False
                task.set(detail="the window was closed", progress=1.0)
                log("accounts", f"@{account['login']}: the window closed before the sign-in", "warn")
                return

            try:
                found = automation.with_page(aid, messenger.peek, timeout=60)
            except Exception:                        # noqa: BLE001 - a page mid-navigation
                found = {"logged_in": False, "handle": None}

            if found.get("logged_in"):
                break

            if waited >= SIGN_IN_WINDOW:
                detail = "still not signed in - the window is open, finish there"
                with store.edit() as db:
                    _session(db, aid)["detail"] = detail
                task.set(detail=detail, progress=1.0)
                log("accounts", f"@{account['login']}: {detail}", "warn")
                return

            task.set(detail="waiting for the sign-in in the window",
                     progress=min(0.95, 0.02 + waited / SIGN_IN_WINDOW))

        task.set(detail="saving the session", progress=0.97)

        # The profile directory IS the live session, and always was. This is the
        # copy of it: taken here, at the one moment the jar is known to be
        # signed in, so a profile that is later wiped or corrupted has something
        # to come back from. It is never read while the profile still has
        # cookies of its own - see `_Lane._restore_cookies`.
        try:
            kept = automation.save_state(aid)
            log("accounts", f"backed up {kept} cookies for @{account['login']}")
        except Exception as err:                     # noqa: BLE001 - the session is still saved
            log("accounts", f"the session on @{account['login']} is live but could "
                            f"not be backed up: {err}", "warn")

        session = _record(aid, account["login"], found)
        task.set(detail=session["detail"], progress=1.0, result=session)

        if session["mismatch"]:
            log("accounts",
                f"the window on @{account['login']} is signed in as @{session['as_handle']}",
                "error")
        else:
            log("accounts", f"saved the session on @{account['login']}")

    task = tasks.start(f"signing in @{account['login']}", run, source="accounts")
    return {"task": task.id}


@router.post("/api/accounts/{account_id}/logout")
def logout(account_id: str) -> dict[str, Any]:
    """Drop the session, keep the profile.

    Cookies are cleared before the window closes; the profile directory stays.
    That is the difference between signing out and throwing away the browser,
    and only the first is what the button says.
    """
    account = find_account(account_id)

    def clear(context) -> None:
        context.clear_cookies()

    try:
        if automation.is_open(str(account_id)):
            automation.with_context(str(account_id), clear, timeout=120)
    finally:
        # Before the window goes, and unconditionally: a backup left behind
        # would be restored into the emptied profile on the next launch and
        # sign the account back in, which is the opposite of what was pressed.
        automation.drop_state(str(account_id))
        automation.close_account(str(account_id))

    with store.edit() as db:
        db["sessions"][str(account_id)] = {
            "logged_in": False, "active": False, "detail": "no session",
        }
    log("sessions", f"cleared the session on @{account['login']}", "warn")
    return {"ok": True}


@router.get("/api/accounts/sessions")
def sessions() -> dict[str, Any]:
    db = store.read()
    rows = []
    for account in db["accounts"]:
        account_id = str(account["id"])
        stored = db["sessions"].get(account_id, {})
        rows.append({
            "id": account["id"],
            "login": account["login"],
            "label": account.get("label", ""),
            "logged_in": bool(stored.get("logged_in")),
            # Read live off the browser rather than out of the store: a window
            # the operator closed by hand is closed, whatever the file says, and
            # the Scraping gate is built on this field.
            "active": automation.is_open(account_id),
            "detail": stored.get("detail", "no session"),
            "expires_at": stored.get("expires_at"),
            # How old the answer above is. The card says it out loud rather than
            # implying freshness it does not have.
            "checked_at": stored.get("checked_at"),
            # Who the profile actually belongs to. Facebook does not put a
            # username in the DOM, so this is the account's NAME - which is what
            # the card can honestly show and what a mismatch is caught on.
            "as_handle": stored.get("as_handle"),
            "mismatch": bool(stored.get("mismatch")),
        })
    return {"sessions": rows}


@router.post("/api/accounts/sessions/check")
def check_sessions() -> dict[str, int]:
    """Ask each open window who it is signed in as.

    Only the open ones. Checking a closed profile means launching Chrome on it,
    and a button captioned "check" that opens six browser windows is not the
    button anybody pressed.
    """
    db = store.read()
    live = 0
    checked = 0

    for account in db["accounts"]:
        account_id = str(account["id"])
        if not automation.is_open(account_id):
            with store.edit() as writable:
                session = _session(writable, account_id)
                session["active"] = False
                if not session.get("logged_in"):
                    session["detail"] = "no session"
            continue

        checked += 1
        try:
            found = automation.with_page(account_id, messenger.whoami, timeout=180)
        except Exception as err:                     # noqa: BLE001 - reported per account
            found = {"logged_in": False, "handle": None}
            log("sessions", f"@{account['login']}: {err}", "warn")

        session = _record(account_id, account["login"], found)

        if session["mismatch"]:
            log("sessions",
                f"the window on @{account['login']} is signed in as "
                f"@{session['as_handle']}", "error")
        elif session["logged_in"]:
            log("sessions", f"@{account['login']} signed in")
        live += 1 if session["logged_in"] else 0

    log("sessions", f"checked {checked}, {live} logged in")
    return {"checked": checked, "logged_in": live}


def _in_30_days() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")


# ── What the server does with saved sessions when it starts ───────────────

def reconcile_sessions() -> int:
    """Forget the windows the last run left behind.

    `active` and `detail` are written while a window is open - "window open",
    "window open - waiting for the sign-in" - and nothing clears them when the
    process ends. Chrome goes with the server, so on the next start every one of
    those sentences is describing a window that does not exist, and the card
    repeats it until something happens to overwrite it.

    Only the transient half is cleared. `logged_in`, `as_handle` and
    `checked_at` describe the PROFILE DIRECTORY, which is still there and still
    signed in - that is the whole point of it - so they survive the restart
    exactly as the session itself does.
    """
    stale = 0
    with store.edit() as db:
        for account in db["accounts"]:
            session = _session(db, str(account["id"]))
            if session.get("active"):
                stale += 1
            session["active"] = False
            if session.get("logged_in"):
                handle = session.get("as_handle")
                session["detail"] = f"signed in as @{handle}" if handle else "signed in"
            else:
                session["detail"] = "no session"
    return stale


def reopen_saved_sessions() -> Optional[tasks.Task]:
    """Open a window on every account whose session was saved.

    The profile directory IS the session, so this is not a restore in any real
    sense - it is launching Chrome on a directory that has been signed in the
    whole time. What it buys is that the operator does not have to press Open on
    six cards after every restart.

    On a task rather than inline in the startup hook, for two reasons. Chrome
    takes seconds per profile and a startup hook that waits for six of them is a
    server that appears not to start. And a task is stoppable: the shutdown hook
    calls `tasks.stop_all()` before it closes the windows, so a Ctrl-C during
    the reopen unwinds instead of racing the thing that is closing what it opens.

    Each window is read once it is up, which is the only free session check
    there is - the window had to be opened anyway. A read that does not settle
    leaves the stored session alone; see `messenger.whoami_when_settled`.
    """
    db = store.read()
    saved = [a for a in db["accounts"]
             if db["sessions"].get(str(a["id"]), {}).get("logged_in")]
    if not saved:
        return None

    def run(task: tasks.Task) -> None:
        opened = 0
        confirmed = 0
        for index, account in enumerate(saved):
            task.checkpoint()
            aid = str(account["id"])
            task.set(detail=f"@{account['login']} ({index + 1} of {len(saved)})",
                     progress=index / len(saved))
            try:
                automation.open_account(aid, messenger.HOME_URL)
            except Exception as err:                 # noqa: BLE001 - one bad profile is not six
                log("accounts",
                    f"@{account['login']}: the saved session would not open ({err})", "warn")
                continue

            opened += 1
            with store.edit() as writable:
                _session(writable, aid)["active"] = True

            try:
                found = automation.with_page(aid, messenger.whoami_when_settled, timeout=180)
            except Exception:                        # noqa: BLE001 - unsettled, same as below
                found = {"settled": False}

            if not found.get("settled"):
                # No answer, which is not the same as "signed out". The window is
                # open and whatever is in the profile is in it; the card keeps
                # what it knew and keeps saying how old that is.
                log("accounts",
                    f"@{account['login']}: the window is open, but messenger.com did not "
                    "settle in time - the card still shows the last known session", "warn")
                continue

            session = _record(aid, account["login"], found)
            if session["logged_in"]:
                confirmed += 1
            else:
                log("accounts",
                    f"@{account['login']}: the saved session is no longer signed in", "warn")

        task.set(detail=f"{confirmed} of {len(saved)} still signed in", progress=1.0)
        log("accounts",
            f"reopened {opened} of {len(saved)} saved sessions, {confirmed} still signed in")

    return tasks.start("reopening saved sessions", run, source="accounts")


@router.post("/api/accounts/login")
def login(body: dict = Body(default={})) -> dict[str, str]:
    """Sign in to each account in turn.

    One at a time with a gap between them, because that is the shape the real
    thing has to take: a burst of simultaneous logins from one IP is the clearest
    signal that a set of accounts belongs together.
    """
    ids = [str(i) for i in (body.get("ids") or [])]
    if not ids:
        raise fail("No accounts selected.")

    def run(task: tasks.Task) -> None:
        db = store.read()
        for index, account_id in enumerate(ids):
            task.checkpoint()
            account = next((a for a in db["accounts"] if str(a["id"]) == account_id), None)
            if account is None:
                continue
            task.set(detail=f"@{account['login']}", progress=index / len(ids))

            password = db["secrets"].get(account_id, "")
            if not password:
                # An account signed in through Google has no password on this
                # side, and there is nothing to type into Facebook's own form. Saying
                # so beats failing at the password field with a message about a
                # missing secret, which reads as a bug rather than as the answer.
                automation.open_account(account_id, messenger.LOGIN_URL)
                detail = "no password stored - sign in in the window, then Check sessions"
                log("accounts", f"@{account['login']}: {detail}", "warn")
                with store.edit() as writable:
                    session = _session(writable, account_id)
                    session["active"] = True
                    session["detail"] = detail
                task.set(progress=(index + 1) / len(ids))
                continue

            try:
                automation.open_account(account_id, messenger.LOGIN_URL)
                automation.with_page(
                    account_id,
                    lambda page, a=account, p=password: messenger.login(
                        page, a["login"], p, a.get("email", "")
                    ),
                    timeout=600,
                )
                signed_in, detail = True, "signed in"
                log("accounts", f"logged in @{account['login']}")
            except Exception as err:                 # noqa: BLE001 - one bad login is not six
                signed_in, detail = False, str(err)
                log("accounts", f"@{account['login']}: {err}", "error")

            with store.edit() as writable:
                session = _session(writable, account_id)
                session["logged_in"] = signed_in
                session["active"] = automation.is_open(account_id)
                session["detail"] = detail
                session["expires_at"] = _in_30_days() if signed_in else None

            task.set(progress=(index + 1) / len(ids))
            if index + 1 < len(ids):
                task.checkpoint(random.uniform(8, 25))

    task = tasks.start("logging in", run, source="accounts")
    return {"task": task.id}
