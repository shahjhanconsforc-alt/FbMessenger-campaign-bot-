"""Panel 5 - Settings.

The panel is generated from what this returns, not from a form written in
`app.js`. That is why the schema ships with the response: adding a row to
`schema.py` puts a control on the screen with its help text under it, and the
console needs no edit at all.

`click_send` is the one with a switch of its own, and it saves on the press
rather than with the rest of the form. A setting that decides whether real
messages leave the machine should not be sitting unsaved in a form somebody
navigated away from.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from ..events import log
from ..schema import SCHEMA, coerce, defaults, known
from ..store import store

router = APIRouter()


@router.get("/api/settings")
def read_settings() -> dict[str, Any]:
    return {"settings": store.settings(), "schema": SCHEMA}


@router.post("/api/settings")
def write_settings(body: dict = Body(default={})) -> dict[str, Any]:
    changed: list[str] = []
    with store.edit() as db:
        for key, value in (body or {}).items():
            if not known(key):
                # Silently ignored rather than rejected: a console left open
                # across a server restart may still be holding a key that has
                # since been removed, and one stale field should not fail the
                # save of the ten good ones beside it.
                continue
            db["settings"][key] = coerce(key, value)
            changed.append(key)

    log("config", f"settings saved ({', '.join(changed)})" if changed else "settings saved")
    return {"settings": store.settings()}


@router.post("/api/settings/reset")
def reset_settings() -> dict[str, Any]:
    with store.edit() as db:
        db["settings"] = defaults()
    # Warn, not info. Reset puts Click Send back ON, and that is the single
    # most consequential thing this panel can do.
    log("config", "settings reset to defaults", "warn")
    return {"settings": store.settings()}
