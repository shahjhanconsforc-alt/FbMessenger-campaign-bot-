"""The server.

It answers the contract `frontend/mock.js` was written against and serves the
console itself, so the whole thing is one process on one port and there is no
static server to start beside it.

One deliberate piece of sleight of hand, in `index()` below: the page is served
with the `<script src="/mock.js">` line stripped out. The README says deleting
that line is the entire migration from the stand-in to a real backend - this is
the server doing exactly that, on the way out, without editing the file. Open
`frontend/index.html` from a plain static server and the mock still answers;
open it from here and this does.
"""

from __future__ import annotations

import atexit
import re
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, tasks
from .automation.browser import automation
from .events import log
from .panels import ROUTERS, accounts
from .store import store

MOCK_TAG = re.compile(r"^\s*<script[^>]+src=[\"']/?mock\.js[\"'][^>]*>\s*</script>\s*$\r?\n?",
                      re.I | re.M)

# The console's own script and stylesheet, as the page asks for them.
ASSET_TAG = re.compile(r"(?P<attr>src|href)=\"/(?P<file>app\.js|style\.css)\"", re.I)


def _stamped(page: str) -> str:
    """The same page, with a version on each asset URL.

    `index()` is `no-store`, so the markup is always today's. The assets are a
    different question: give a browser the same URL twice and it is entitled to
    reuse what it already has, and it does - which leaves today's page running
    yesterday's `app.js`. That is not a cosmetic mismatch. `app.js` is one
    script with its wiring at the top level all the way down, so the first id
    the new markup no longer has throws, the script STOPS, and every listener
    below that line is silently never attached. The page renders perfectly and
    half its buttons do nothing.

    A stamp off the file's own mtime ends it: edit the file and the URL changes,
    so there is nothing to reuse. Leave it alone and the URL does not, so the
    cache still works. `Cache-Control: no-store` on the mount below says the
    same thing to a browser that is listening; this one does not need it to.
    """

    def stamp(match: re.Match[str]) -> str:
        target = config.FRONTEND / match.group("file")
        try:
            version = int(target.stat().st_mtime)
        except OSError:
            # The mount will 404 it in a moment and say so properly. Rewriting
            # the tag to something cleverer here would only hide that.
            return match.group(0)
        return f'{match.group("attr")}="/{match.group("file")}?v={version}"'

    return ASSET_TAG.sub(stamp, page)

app = FastAPI(
    title="messenger-bot",
    description="Operator console for Facebook Messenger outreach. Everything it does, it does as you.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

for router in ROUTERS:
    app.include_router(router)


# -- Errors --------------------------------------------------------
@app.exception_handler(HTTPException)
def http_error(request: Request, err: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": err.detail}, status_code=err.status_code)


@app.exception_handler(Exception)
def unhandled(request: Request, err: Exception) -> JSONResponse:
    """Anything that got past a panel.

    `app.js` reads `detail` off the body whatever the status is, so a crash
    surfaces as a toast with the reason in it rather than as a bare 500 the
    console can only report as "500 Internal Server Error".
    """
    message = str(err) or err.__class__.__name__
    log("server", f"{request.method} {request.url.path}: {message}", "error")
    return JSONResponse({"detail": message}, status_code=500)


# -- Lifecycle -----------------------------------------------------
@app.on_event("startup")
def startup() -> None:
    config.ensure_dirs()
    # Rosters written while the bio was being looked for under the wrong testid
    # kept the whole hover card and no bio. The card is all that was needed, so
    # they are repaired here rather than left to be re-scraped - idempotent, and
    # cheap on a store with nothing to fix.
    recovered = store.recover_bios()
    if recovered:
        log("store", f"recovered {recovered} bios from stored profile cards")
    cleaned = store.clean_names()
    if cleaned:
        log("store", f"cleared {cleaned} names that were a link's label, not a name")

    # A store written by the X console carries settings whose defaults have
    # since moved - `profile_url` above all, which would send the campaign to
    # x.com looking for Messenger handles. Idempotent; see `migrate_settings`.
    moved = store.migrate_settings()
    if moved:
        log("config", f"carried {len(moved)} setting(s) over from the X console "
                      f"({', '.join(moved)})", "warn")

    # Chrome went with the last process, so every "window open" left in the
    # store is describing something that is not there. Cleared before anything
    # can read it, and before the reopen below writes the true answer over it.
    stale = accounts.reconcile_sessions()
    if stale:
        log("accounts", f"forgot {stale} window(s) the last run left open")

    log("server", f"messenger-bot server up on http://{config.HOST}:{config.PORT}")

    # And then bring the saved sessions back up. On its own thread inside a
    # task, so the server is answering requests while Chrome starts - see
    # `reopen_saved_sessions`. It returns None when there is nothing saved,
    # which is the ordinary case on a fresh install.
    if accounts.reopen_saved_sessions() is not None:
        log("accounts", "reopening the saved sessions")

    # And the campaign that was going when the last process died, if there
    # was one: its rows are on disk, and it carries on from the first one it
    # had not reached. Windows it needs are opened by the resume itself.
    from .panels import campaign as campaign_panel
    campaign_panel.recover()


@app.on_event("shutdown")
def shutdown() -> None:
    """Stop the runs, then close the windows. Both, and in that order.

    Chrome is a child process holding a live session. Left running it keeps the
    profile directory locked, and the next start finds it in use and refuses to
    open it - so Ctrl-C on the server has to take the browsers with it.

    The runs go first. A sweep or a sign-in watcher is a thread of its own, and
    it outlives the request that started it; closing the windows underneath one
    is not the end of it, because the next thing it does is ask its lane for a
    page, and a lane asked for a page OPENS one. Six windows would close and six
    would come back, owned by a server that is no longer there.
    """
    if getattr(shutdown, "_done", False):            # atexit after a clean stop
        return
    shutdown._done = True

    # First, so the activity-feed streams let go: uvicorn waits on open
    # connections before it will exit, and a stream that never ends is the one
    # that made Ctrl-C hang. Telling them to close here is what lets the wait be
    # a moment rather than a timeout.
    from .events import begin_shutdown
    begin_shutdown()

    still_going = tasks.stop_all()
    if still_going:
        log("server", f"ended {still_going} run(s) still going", "warn")
    automation.shutdown()
    log("server", "server stopped", "warn")


# The hook above runs when uvicorn stops the app, which is every ordinary exit -
# Ctrl-C included. This is for the ones that are not: an exception on the way up,
# or a stop that never reaches the ASGI lifespan at all. It is the same call, and
# it is guarded, so a clean shutdown does not do it twice.
@atexit.register
def _last_chance() -> None:
    try:
        shutdown()
    except Exception as err:                         # noqa: BLE001 - the exit is the story
        # Whatever is already going wrong is what the operator needs to read.
        # A traceback out of the closing routine, printed on top of it, is not.
        print(f"messenger-bot: could not close the browsers cleanly: {err}")


# -- The console ---------------------------------------------------
@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = (config.FRONTEND / "index.html").read_text(encoding="utf-8")
    served, count = MOCK_TAG.subn("", _stamped(page))
    if not count:
        # Worth a line in the feed rather than a silent pass: if the tag ever
        # stops matching, the console would boot with the stand-in installed and
        # answer every call out of localStorage while this server sat idle,
        # which looks exactly like a working app.
        log("server", "index.html had no mock.js tag to strip", "warn")
    return HTMLResponse(served, headers={"Cache-Control": "no-store"})


@app.get("/mock.js")
def no_mock() -> PlainTextResponse:
    """Refused on purpose.

    A page cached from before this server existed would otherwise load the
    stand-in over the top of a real backend, and every write would go to
    localStorage while the console looked entirely healthy. Failing here makes
    that a broken tag in the devtools console instead - one reload fixes it.
    """
    return PlainTextResponse(
        "// messenger-bot: the real backend is serving this console. mock.js is not loaded.\n",
        status_code=404,
        media_type="application/javascript",
    )


class _Console(StaticFiles):
    """The console's own files, served like the page that pulls them in.

    `index()` above is `no-store`, but this mount was not - it sent an ETag and
    nothing else, which leaves a browser free to reuse `app.js` out of its
    session cache without ever asking. Edit the console and the next load is a
    half-update: the markup is today's, because the page is never cached, and
    the script is yesterday's. Every id the edit removed is then a `null` at the
    end of a `$(...)`, and the console fails with a TypeError that has nothing
    to do with what changed.

    Same reasoning as `no_mock()` below-a stale asset over a live server looks
    like a broken app rather than a stale asset. This is a local console with
    the files on the same disk, so there is nothing to save by caching them.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        # The 304 path answers off the ETag before `file_response` is reached,
        # so the header alone is not enough - the revalidation has to actually
        # come back with the file.
        return False

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


# Last, and only for what is left: a mount at "/" swallows every path that got
# this far, so it has to be added after the API routes rather than before them.
app.mount("/", _Console(directory=str(config.FRONTEND), html=False), name="console")
