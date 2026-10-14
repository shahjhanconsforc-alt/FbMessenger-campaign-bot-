"""Start the console.

    python backend/run.py           from the repo root
    python run.py                   from inside backend/
    python -m backend.run           either way

All three work, because a run script that only works from one directory is one
everybody gets wrong once.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The package is imported as `backend.*`, so the REPO ROOT has to be on the path
# - which it is not when this file is run directly from inside backend/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config                       # noqa: E402


def main() -> None:
    import uvicorn

    config.ensure_dirs()
    print(f"messenger-bot -> http://{config.HOST}:{config.PORT}")
    print(f"   store    {config.STORE_FILE}")
    print(f"   profiles {config.PROFILES}")
    print("   browser  " + ("headless" if config.HEADLESS else "visible"))
    print()

    uvicorn.run(
        "backend.app:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.RELOAD,
        # Off. uvicorn's access log prints a line per request, and `watchTask()`
        # polls every 900ms while a run is going - the log would be nothing but
        # the poller, and the feed is where a run is actually watched.
        access_log=False,
        # Ctrl-C should return the prompt, not hang. The activity feed is a
        # stream that never completes on its own, and uvicorn waits on every open
        # connection before it exits - with no cap, for ever, which is the pause
        # after Ctrl-C. Capped, uvicorn stops waiting and moves on to close the
        # browsers; the windows still close (that runs in the shutdown hook, not
        # under this timeout). A second Ctrl-C still forces an instant exit.
        timeout_graceful_shutdown=config.SHUTDOWN_GRACE,
    )


if __name__ == "__main__":
    main()
