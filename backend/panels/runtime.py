"""The chrome around the five panels.

The status pills across the top of the console, the store's own log, and the
task poller every long run is watched through. None of it belongs to a panel;
all of it is a route, so it sits with the routes.

The one rule these have to keep: they must answer FAST and always. `pollStatus()`
calls two of them every five seconds, and `watchTask()` calls the third every
900ms for the length of a run. A status endpoint that queued behind a scrape
would freeze the pills for the whole of it, which is why the browser's state is
read from a mirror rather than from Playwright.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import busy, config, tasks
from ..automation.browser import automation
from ..events import log, recent
from ._common import fail

router = APIRouter()


@router.get("/api/automation/status")
def automation_status() -> dict[str, Any]:
    return {
        "browser": automation.status(),
        "run": {"state": "running" if tasks.running() else "idle"},
    }


@router.get("/api/store/health")
def store_health() -> dict[str, Any]:
    """Is the store writable.

    Not "does the file exist" - an unwritable data directory is the failure that
    matters, and it presents at the end of a sweep when the roster cannot be
    saved. Checking it every five seconds means the pill goes red before the
    work is lost rather than after.
    """
    try:
        config.ensure_dirs()
        probe = config.DATA / ".health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        up = True
    except OSError:
        up = False
    return {"up": up, "kind": "json file"}


@router.post("/api/app/reset")
def app_reset() -> dict[str, Any]:
    """Stop everything that is running and put every panel back to a fresh start.

    Each run is ended through its own panel's Stop first, so it records that it
    was ended - which is what keeps a campaign from restarting itself. Then any
    other background job, paused ones included, is stopped and waited for; the
    accounts are let go; and each panel's run is put back to idle.

    Kept: the accounts, their browser windows and sessions, and every piece of
    saved data - drafts, media, collected handles, the groups registry. Fresh
    means nothing running, not nothing stored.
    """
    # Here rather than at the top: the panels import the store and the browser,
    # and this module is imported alongside them.
    from . import campaign, fb, groups, scraping

    going = tasks.live_titles()

    def quietly(fn) -> None:
        try:
            fn()
        except Exception:                            # noqa: BLE001 - not running is fine
            pass

    if campaign._campaign.get("state") in ("running", "paused", "restarting"):
        quietly(campaign.stop)
    quietly(fb.stop)
    quietly(scraping.stop)
    quietly(groups.stop)
    quietly(groups.join_stop)

    waited_on = tasks.stop_all(timeout=25.0)
    still = tasks.live_titles()

    busy.release_all()
    quietly(campaign.reset_idle)
    quietly(fb.reset_idle)
    quietly(groups.reset_idle)
    quietly(scraping.reset)

    log("server", "app reset - " + (f"stopped {', '.join(going)}" if going
                                     else "nothing was running"), "warn")
    return {"ok": True, "stopped": going, "waited_on": waited_on, "still_going": still}


@router.get("/api/store/logs")
def store_logs() -> dict[str, Any]:
    return {"logs": recent(200)}


@router.get("/api/tasks/{task_id}")
def task(task_id: str) -> dict[str, Any]:
    found = tasks.get(task_id)
    if found is None:
        # The wording is the point. A restart mid-run leaves the console polling
        # an id nothing knows about, and "that run is no longer on the server"
        # is what happened - not a 404 the operator has to interpret.
        raise fail("That run is no longer on the server.", 404)
    return found.snapshot()
