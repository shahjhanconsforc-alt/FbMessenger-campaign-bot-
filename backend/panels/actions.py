"""Panel - Actions: the Messenger rail.

Six items down the left of messenger.com: Chats, Marketplace, Requests and
Archive, plus the settings menu and the sidebar collapse, which are toggles
and so are pressed twice a second apart. Each is pressed on its own in the
open window.

Which element each key maps to lives with every other Messenger selector in
`automation/messenger.py`; this file only decides which window the press lands
on, and refuses when that window is spoken for by a run.

Groups joining used to live here too. It is on the Group Posting panel now,
with the rest of what this app does with Facebook groups, and its code moved
with its screen - see `groups.py`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from .. import busy
from ..automation import messenger
from ..automation.browser import automation
from ..events import log
from ._common import as_at, fail, working_account

router = APIRouter()

# -- Messenger actions ---------------------------------------------
# The items down messenger.com's left rail, in the order they sit there.
# Keys are `messenger.NAV_ACTIONS` keys; the label shown is the rail's own word.
MESSENGER_ACTIONS: tuple[str, ...] = ("chats", "marketplace", "requests", "archive",
                                      "menu", "sidebar")


def _messenger_actions() -> list[dict[str, str]]:
    return [{"key": key, "label": messenger.NAV_ACTIONS[key]["label"]}
            for key in MESSENGER_ACTIONS]


@router.get("/api/actions/messenger")
def list_messenger_actions() -> dict[str, Any]:
    """The Messenger rail items this can press, in rail order, and the open windows."""
    return {"actions": _messenger_actions(),
            "windows": [{"id": str(a["id"]), "label": as_at(a)}
                        for a in open_accounts()]}


@router.post("/api/actions/messenger/click")
def click_messenger(body: dict = Body(default={})) -> dict[str, Any]:
    """Press one Messenger rail item in the open window.

    One click and a page settle, so it is answered inline rather than as a task.
    The window is whichever is open, or the one named in `session` when several
    are - and never one a campaign or a scrape is driving right now: a click in
    the middle of a send lands on the wrong page for both of them.
    """
    action = str(body.get("action") or "").strip()
    if action not in MESSENGER_ACTIONS:
        raise fail(f"{action or 'nothing'} is not a Messenger item this can press.")
    account = working_account(body.get("session"))
    account_id = str(account["id"])
    holder = busy.of(account_id)
    if holder:
        raise fail(f"{as_at(account)} is busy with {holder} - stop that first.", 409)

    label = messenger.NAV_ACTIONS[action]["label"]
    try:
        result = automation.with_page(
            account_id, lambda page: messenger.click_action(page, action), timeout=120)
    except Exception as err:                         # noqa: BLE001 - said in the feed, then the toast
        log("actions", f"could not press {label} on {as_at(account)}: {err}", "error")
        raise fail(f"Could not press {label}: {err}")
    log("actions", f"{result} {label} on {as_at(account)}")
    return {"ok": True, "action": action, "label": label, "result": result}
