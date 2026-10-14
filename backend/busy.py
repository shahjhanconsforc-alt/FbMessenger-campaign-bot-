"""Which account is doing what, so two services never drive one window at once.

Every long run - a scrape, an FB Campaign walk, a Messenger campaign, a groups
join - claims the accounts it is about to use, and a second service asking for
one of them is refused with the name of the first. The panels read the same
answer to grey out an account that is spoken for, but the refusal here is the
thing that cannot be got past: a panel that was open before the other run
started still shows the account as free, and its press has to be the one that
learns otherwise.

Nothing releases a claim by hand. A claim carries the id of the task that owns
it, and it is live for exactly as long as that task is running - so a run that
ends any way at all, including by crashing, frees its accounts without every
`finally` in the codebase having to remember to. Between `claim` and `bind`
(the task id is only known once the task has started) a claim stands on its
own for two minutes, which is far longer than a start ever takes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Optional

from fastapi import HTTPException

from . import tasks

SERVICES = {
    "scraping": "Scraping",
    "fb": "FB Campaign",
    "campaign": "Campaign",
    "actions": "Actions",
    "groups": "Group Posting",
}

# How long an unbound claim stands. Long enough for any start to finish binding
# its task id; short enough that a start that raised without releasing does not
# lock an account for the rest of the day.
UNBOUND_TTL = 120.0

_lock = threading.Lock()
_held: dict[str, dict[str, Any]] = {}      # account id -> {service, task, since, label}


def _live(entry: dict[str, Any]) -> bool:
    task_id = entry.get("task")
    if task_id:
        task = tasks.get(task_id)
        return task is not None and task.state in ("running", "paused")
    return time.monotonic() - float(entry.get("since") or 0) < UNBOUND_TTL


def _sweep() -> None:
    for key in [k for k, v in _held.items() if not _live(v)]:
        del _held[key]


def holders() -> dict[str, dict[str, str]]:
    """Every account spoken for right now: id -> {service, label}."""
    with _lock:
        _sweep()
        return {aid: {"service": v["service"], "label": SERVICES.get(v["service"], v["service"])}
                for aid, v in _held.items()}


def of(account_id: str) -> Optional[str]:
    """The service holding this account, as its label, or None."""
    return (holders().get(str(account_id)) or {}).get("label")


def claim(account_ids: Iterable[str], service: str, *, names: Optional[dict[str, str]] = None) -> None:
    """Take these accounts for `service`, or refuse with the name of who has them.

    All or nothing: a run on three accounts of which one is busy does not start
    on two. The operator picked three, and two is a different run.
    """
    ids = [str(i) for i in account_ids]
    with _lock:
        _sweep()
        taken = [(aid, _held[aid]) for aid in ids if aid in _held]
        if taken:
            aid, entry = taken[0]
            who = (names or {}).get(aid) or f"account {aid}"
            what = SERVICES.get(entry["service"], entry["service"])
            more = f" (and {len(taken) - 1} more)" if len(taken) > 1 else ""
            raise HTTPException(
                status_code=409,
                detail=f"{who} is busy with {what}{more} - stop that first, or leave it out.")
        for aid in ids:
            _held[aid] = {"service": service, "task": None, "since": time.monotonic()}


def bind(account_ids: Iterable[str], task_id: str) -> None:
    """Tie a claim to the task that owns it. From here the task's life is the claim's."""
    with _lock:
        for aid in (str(i) for i in account_ids):
            entry = _held.get(aid)
            if entry is not None:
                entry["task"] = task_id


def release_all() -> None:
    """Every claim gone at once - for the app being reset, when nothing is running."""
    with _lock:
        _held.clear()


def release(account_ids: Iterable[str]) -> None:
    """Let go early - for a start that claimed and then could not start."""
    with _lock:
        for aid in (str(i) for i in account_ids):
            _held.pop(aid, None)
