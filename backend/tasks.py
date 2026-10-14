"""A long run is a task, not a held request.

A sweep takes minutes. POST starts it and returns an id; the UI polls
`/api/tasks/:id` until the state stops being `running`. `watchTask()` in app.js
is the only thing on that side which knows this, and this is the only thing on
this side.

The states are the ones the frontend already reads: `running`, `paused`, `done`,
`failed`, `stopped`. Anything that is not `running` ends the poll loop.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable, Optional

from .events import log

# Finished tasks are kept so a poll that arrives just after the end still gets an
# answer - the UI needs that last frame to paint the completed table. Beyond a
# few dozen they are only ballast.
KEEP = 40


class Stopped(Exception):
    """Raised inside a task body when the operator ends the run.

    Not an error: it unwinds whatever the run was in the middle of, and the
    runner marks the task `stopped` rather than `failed`.
    """


class Task:
    def __init__(self, title: str):
        self.id = uuid.uuid4().hex[:8]
        self.title = title
        self.state = "running"
        self.progress = 0.0
        self.detail = ""
        self.result: Any = None
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()

    # -- What the run reports ------------------------------------
    def set(self, *, progress: Optional[float] = None, detail: Optional[str] = None,
            result: Any = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        if detail is not None:
            self.detail = detail
        if result is not None:
            self.result = result

    # -- What the operator does to it -----------------------------
    def pause(self) -> None:
        self._resume.clear()
        self.state = "paused"

    def resume(self) -> None:
        self._resume.set()
        self.state = "running"

    def stop(self) -> None:
        self._stop.set()
        # Cleared too: a run parked in a batch pause has to wake up before it can
        # notice it was asked to stop, and a paused-then-stopped task that never
        # woke would sit at "paused" for ever.
        self._resume.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def checkpoint(self, seconds: float = 0.0) -> None:
        """The one call a run makes between units of work.

        It is both the pause barrier and the stop check, and it doubles as the
        run's sleep, so a paced campaign spends its gap here and stays
        interruptible for the whole of it. A `time.sleep(90)` between two DMs
        would make Stop mean "in a minute and a half".
        """
        if self._stop.is_set():
            raise Stopped()
        self._resume.wait()
        if self._stop.is_set():
            raise Stopped()
        if seconds > 0:
            # In slices, for the same reason: the wait ends the moment Stop is
            # pressed instead of at the end of the gap.
            remaining = seconds
            while remaining > 0:
                if self._stop.wait(timeout=min(0.25, remaining)):
                    raise Stopped()
                remaining -= 0.25
                self._resume.wait()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "progress": self.progress,
            "detail": self.detail,
            "result": self.result,
        }


_tasks: "dict[str, Task]" = {}
_lock = threading.Lock()


def start(title: str, body: Callable[[Task], Any], *, source: str = "run") -> Task:
    """Run `body(task)` on a thread and hand back the task straight away."""
    task = Task(title)
    with _lock:
        _tasks[task.id] = task
        for dead in list(_tasks)[:-KEEP]:
            if _tasks[dead].state != "running":
                del _tasks[dead]

    def run() -> None:
        try:
            body(task)
            if task.state == "running":
                task.state = "done"
                task.progress = 1.0
        except Stopped:
            task.state = "stopped"
            log(source, f"{title}: ended by the operator", "warn")
        except Exception as err:                     # noqa: BLE001 - reported, not swallowed
            task.state = "failed"
            # The message reaches the run bar, so it has to read as a sentence
            # rather than as a class name.
            task.detail = str(err) or err.__class__.__name__
            log(source, f"{title}: {task.detail}", "error")
            traceback.print_exc()

    threading.Thread(target=run, name=f"task-{task.id}", daemon=True).start()
    return task


def get(task_id: str) -> Optional[Task]:
    with _lock:
        return _tasks.get(task_id)


def running() -> bool:
    with _lock:
        return any(t.state == "running" for t in _tasks.values())


def stop_all(timeout: float = 20.0) -> int:
    """End every run that is still going, and wait for them to unwind.

    The server's shutdown hook calls this before it closes the browsers, and the
    waiting is the point. `stop()` only sets a flag; a run notices it at its next
    `checkpoint()`, which is where it also does its sleeping. Closing the windows
    without giving it that moment means the run's next line reaches for a page
    that has just gone - and the lane, asked for a page, would open a new window
    to provide one.

    Bounded, because a run that will not come back must not hold the server open
    for ever. Past the deadline the windows close regardless, and the retired
    lanes are what stop the run in the end.
    """
    # PAUSED counts as going. A paused campaign is a run waiting to carry on,
    # and ending "everything" while leaving it parked would leave it to wake up
    # later and start sending again.
    going = ("running", "paused")
    with _lock:
        live = [t for t in _tasks.values() if t.state in going]
    for task in live:
        task.stop()

    deadline = time.monotonic() + timeout
    while live and time.monotonic() < deadline:
        if not any(t.state in going for t in live):
            break
        time.sleep(0.2)
    return len(live)


def live_titles() -> list[str]:
    """What is going right now, by title - running or paused."""
    with _lock:
        return [t.title for t in _tasks.values() if t.state in ("running", "paused")]
