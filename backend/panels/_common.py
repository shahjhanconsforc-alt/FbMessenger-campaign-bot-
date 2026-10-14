"""The few things every panel needs.

Not a panel itself - it is here rather than a level up because everything in it
is about answering a request, and the panels are where requests are answered.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from ..automation.browser import automation
from ..store import store


def fail(message: str, status: int = 400) -> HTTPException:
    """A 4xx the console can read.

    `app.js` surfaces `detail` from the body whatever the status is, so the text
    is written for the toast it is about to become: what happened, and what the
    operator can do about it.
    """
    return HTTPException(status_code=status, detail=message)


def find_account(account_id: str) -> dict[str, Any]:
    for account in store.read()["accounts"]:
        if str(account["id"]) == str(account_id):
            return account
    raise fail(f"There is no account {account_id} on the list.", 404)


def open_accounts() -> list[dict[str, Any]]:
    """The accounts with a browser window up, in the order they were added."""
    return [a for a in store.read()["accounts"] if automation.is_open(str(a["id"]))]


def working_account(preferred: Optional[str] = None) -> dict[str, Any]:
    """The account a scrape runs as.

    This is the ONLY thing that decides whether a sweep can run. The console used
    to decide it as well, off its own reading of which windows were up, and a
    console that answers this on its own is a console that can answer it wrongly
    - it greys out the button over a window that is open. So it presses, and this
    is what says no.

    Saying no matters for a reason worth restating: the feed is behind the login
    wall entirely, and a sweep started without a window does not fail - it
    collects an empty list and reports it as a fact.
    """
    open_ones = open_accounts()
    if not open_ones:
        raise fail("No browser window is open - open an account from the "
                   "Accounts panel first.")
    if preferred:
        for account in open_ones:
            if str(account["id"]) == str(preferred):
                return account
    return open_ones[0]


def as_at(account: dict[str, Any]) -> str:
    return "@" + str(account.get("login") or account.get("id"))
