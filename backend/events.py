"""The activity feed.

Every panel writes to one stream, and the feed down the right-hand side of the
console is that stream live. Two consumers, one source:

  * `GET /api/data/activity/stream` - an EventSource, pushed to as rows arrive.
  * `GET /api/store/logs` - the last few hundred rows, for the pill's dialog.

SSE rather than a websocket, because the stream is one-directional and
EventSource reconnects on its own when the server restarts, which during
development is constantly.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Iterator

from .store import now, store

# How much scrollback the store keeps. A run generates a row per recipient, so a
# few hundred is one campaign's worth - enough to answer "what did it just do",
# which is the only question the feed is asked.
KEEP = 400

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()

# Set once, at the very start of the server's shutdown, so the streams below let
# go instead of holding the process open. A never-ending stream is a connection
# uvicorn waits on, and without this the wait is the whole of a laggy Ctrl-C.
_closing = threading.Event()


def begin_shutdown() -> None:
    """Tell every open feed to close, and wake the ones parked on `get`.

    Called first thing in the server's shutdown hook, before the browsers are
    closed - the point is to release the connections uvicorn is about to wait
    for, not to wait for them here. Waking each listener with a sentinel turns a
    stream blocked for up to its keep-alive interval into one that ends now.
    """
    _closing.set()
    with _lock:
        listeners = list(_subscribers)
    for listener in listeners:
        try:
            listener.put_nowait(None)                # the sentinel `sse` breaks on
        except queue.Full:
            pass


def log(source: str, message: str, level: str = "info") -> dict[str, Any]:
    row = {"at": now(), "source": source, "message": message, "level": level}

    with store.edit() as db:
        db["logs"].append(row)
        if len(db["logs"]) > KEEP:
            del db["logs"][:-KEEP]

    with _lock:
        listeners = list(_subscribers)
    for listener in listeners:
        # Never block the caller. A browser tab that stopped reading its own
        # stream must not be able to stall a campaign mid-send, so a full queue
        # drops the row rather than waiting for room.
        try:
            listener.put_nowait(row)
        except queue.Full:
            pass
    return row


def subscribe() -> queue.Queue:
    listener: queue.Queue = queue.Queue(maxsize=1000)
    with _lock:
        _subscribers.append(listener)
    return listener


def unsubscribe(listener: queue.Queue) -> None:
    with _lock:
        if listener in _subscribers:
            _subscribers.remove(listener)


def recent(limit: int = 200) -> list[dict[str, Any]]:
    return store.read()["logs"][-limit:]


def sse() -> Iterator[str]:
    """The stream body.

    Opens with the tail of the log rather than with silence: a feed that is blank
    until the next thing happens looks broken, and the operator has usually just
    reloaded in the middle of something.
    """
    listener = subscribe()
    try:
        if _closing.is_set():                        # started mid-shutdown
            return
        for row in recent(60):
            yield _frame(row)
        # A keep-alive every fifteen seconds, but the block is only one second at
        # a time - long enough not to be busy, short enough that a shutdown told
        # to the stream (a `None` sentinel, or the flag) ends it within the
        # second rather than up to a keep-alive later. That one second is the
        # difference between Ctrl-C returning at once and hanging on this feed.
        idle = 0.0
        while True:
            try:
                row = listener.get(timeout=1)
            except queue.Empty:
                if _closing.is_set():
                    return
                idle += 1
                if idle >= 15:
                    # Idle proxies close a connection that has said nothing for a
                    # minute, and EventSource's reconnect would then replay the
                    # last 60 rows every time the console was quiet.
                    yield ": keep-alive\n\n"
                    idle = 0.0
                continue
            if row is None or _closing.is_set():     # the shutdown sentinel
                return
            idle = 0.0
            yield _frame(row)
    finally:
        unsubscribe(listener)


def _frame(row: dict[str, Any]) -> str:
    return f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
