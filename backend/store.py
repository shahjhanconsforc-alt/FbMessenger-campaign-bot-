"""One JSON file, one lock, one process.

`mock.js` kept the whole console in a single localStorage key. This is the same
shape in a file, and deliberately not a database: everything here is small
(hundreds of accounts, not millions), it is read far more often than written,
and a file can be opened in an editor when a run has gone wrong at midnight.

The one rule: every mutation goes through `edit()`, which holds the lock for the
whole block and writes once at the end. Writing inside a loop would leave the
file half-updated if the process died mid-run, and a half-written roster is
worse than no roster - it reads as a complete one.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from . import config
from .schema import defaults, known

# The three targets `mock.js` seeded. Kept because an empty console cannot be
# judged: every table looks correct when it has nothing in it. Unlike the mock's,
# nothing else is seeded - the rosters and the account store start empty, because
# inventing accounts that were never scraped is exactly the lie a real server
# must not tell.
SEED_TARGETS = [
    {"key": "inbox", "name": "Chat list", "mode": "inbox",
     "note": "the conversations this account already has"},
]

# No sample draft. A fresh store used to come with an "intro" message written
# for a different product, and it sat in the Stored drafts list looking like
# something the operator had saved. The drafts are what the operator writes or
# what the bucket holds - nothing else.
SEED_DRAFTS: list[dict] = []


# How hard to try when Windows will not let the store be replaced. Five tries
# over about three quarters of a second: long enough to outlast a scanner
# holding the file, short enough that a genuinely stuck write still fails while
# someone is watching the run rather than minutes later.
WRITE_TRIES = 5
WRITE_BACKOFF = 0.05


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def fresh() -> dict[str, Any]:
    return {
        "accounts": [],
        # Passwords, kept out of every list response and revealed only by the eye
        # on the account card. They are in this file in the clear: the file is
        # gitignored, and so is `profiles/`, which holds live session cookies
        # that are worth more than the password anyway. Encrypting one and not
        # the other would be theatre.
        "secrets": {},
        "sessions": {},
        "targets": copy.deepcopy(SEED_TARGETS),
        "rosters": [],
        "store": [],
        "drafts": copy.deepcopy(SEED_DRAFTS),
        "media": [],
        "settings": defaults(),
        "logs": [],
        # DMs sent per day, per account and in total. Per DAY because that is the
        # window X counts in; a counter that only knew about this run would let
        # three runs in one afternoon spend the day's budget three times over.
        "counters": {},
        "next_account": 1,
    }


def _with_bio(row: dict[str, Any]) -> dict[str, Any]:
    """A row with a bio that is actually a bio.

    Two repairs, both on the way IN to the store so nothing downstream has to
    know about either.

    What is thrown out: the follow button's own accessibility label, which an
    older read of the list row wrote into the bio field. "Click to Follow
    someone" is worse than an empty bio - it is confidently wrong, it is what a
    category would then be guessed from, and it would go into the bucket.

    What is recovered: the bio out of the hover card, when the row kept the card
    but no bio was parsed from it. The card is stored verbatim, so a row that has
    one has the bio too, parsed or not - which makes this the belt to the
    parser's braces. A build that stops finding the bio on the live page still
    writes a roster the bio can be read back out of.
    """
    from .automation.x import bio_from_card, is_junk_bio  # noqa: PLC0415 - avoids a cycle

    handle = row.get("handle", "")
    bio = (row.get("bio") or "").strip()
    if is_junk_bio(bio, handle):
        bio = ""

    if not bio and row.get("card"):
        found = bio_from_card(row["card"], handle)
        if found and not is_junk_bio(found, handle):
            bio = found

    return row if bio == row.get("bio") else {**row, "bio": bio}


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._db = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                db = json.load(handle)
        except (OSError, ValueError):
            return fresh()
        # A store written before a key existed: fill what is missing rather than
        # handing a panel an undefined to render.
        for key, value in fresh().items():
            db.setdefault(key, value)
        db["settings"] = {**defaults(), **(db.get("settings") or {})}
        return db

    def _write(self) -> None:
        """Write the store, atomically, and put up with Windows.

        `os.replace` is atomic everywhere, but on Windows it is REFUSED outright
        while any other process holds the destination open - an editor, a backup
        agent, a virus scanner that decided to read the file this millisecond. The
        lock is measured in milliseconds; the failure it caused was a whole scrape
        marked failed, because a log line at the end of a run goes through here
        and the error unwound the task with it.

        So it retries briefly. Anything still locked after a second is a real
        problem - a file gone read-only, a directory that vanished - and raises
        the way it did before, because a store that cannot be written is not
        something to swallow.
        """
        config.ensure_dirs()
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._db, handle, indent=2, ensure_ascii=False)

        last: Optional[OSError] = None
        for attempt in range(WRITE_TRIES):
            try:
                os.replace(tmp, self.path)  # atomic: no reader sees half a file
                return
            except PermissionError as err:           # Windows, and only Windows
                last = err
                time.sleep(WRITE_BACKOFF * (attempt + 1))
        raise last if last else OSError("could not write the store")

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def read(self) -> dict[str, Any]:
        """A snapshot. Deep-copied, so a caller iterating it cannot be tripped by
        a run mutating the same list underneath."""
        with self._lock:
            return copy.deepcopy(self._db)

    def edit(self) -> "_Edit":
        """`with store.edit() as db:` - mutate freely, saved once on the way out."""
        return _Edit(self)

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._db["settings"])

    def save(self) -> None:
        with self._lock:
            self._write()

    # -- The two tables that overlap on purpose --------------------
    def put_roster(self, target: dict, mode: str, rows: list[dict], by: str) -> dict:
        """A roster is per target AND per mode.

        The followers of an account and the likers of its post are two different
        lists, and merging them under one key would make the Data panel lie about
        where a handle came from.
        """
        key = f"{target['key']}-{mode}"
        rows = [_with_bio(row) for row in rows]
        roster = {
            "key": key,
            "target": target["name"],
            "mode": mode,
            "at": now(),
            "by": by,
            "accounts": copy.deepcopy(rows),
        }
        with self.edit() as db:
            db["rosters"] = [roster] + [r for r in db["rosters"] if r["key"] != key]

            # The account store overlaps the rosters BY DESIGN. The roster says
            # who came off one target on one day; the store says who has ever
            # been seen, which is what a campaign draws from. Purging one into
            # the other would make the first an unreliable record of what had
            # been scraped.
            index = {row["handle"]: row for row in db["store"]}
            for account in rows:
                existing = index.get(account["handle"])
                if existing:
                    if target["name"] not in existing["targets"]:
                        existing["targets"].append(target["name"])
                    existing["name"] = existing.get("name") or account.get("name") or ""
                    # The bio is REFRESHED, not kept: it is what the account says
                    # about itself today, it is edited, and a stored one that is
                    # wrong - a bad read from an older build, say - would
                    # otherwise be permanent, because nothing else ever writes it.
                    # Only when the new read has one, so a row seen before its
                    # card came back does not blank a bio already on file.
                    if account.get("bio"):
                        existing["bio"] = account["bio"]
                    existing.setdefault("bio", "")
                    # Counts, inbox state and the hovered profile card are
                    # refreshed for the same reason: they are facts about the
                    # account today, not about the day it was first seen.
                    if account.get("followers") is not None:
                        existing["followers"] = account["followers"]
                    if account.get("card"):
                        existing["card"] = account["card"]
                    existing["dm_open"] = account.get("dm_open", existing.get("dm_open", True))
                    existing["protected"] = account.get("protected", existing.get("protected", False))
                else:
                    row = copy.deepcopy(account)
                    row.update({"targets": [target["name"]], "messaged": False,
                                "msg_count": 0, "at": now()})
                    db["store"].append(row)
                    index[row["handle"]] = row
        return roster

    # -- Repair ----------------------------------------------------
    def migrate_settings(self) -> list[str]:
        """Carry a store written by the X console over to the Messenger one.

        `settings()` hands back what is IN the file, with no merge against the
        defaults - which is right, because a value the operator set must outlive
        a change to what it would have been. The cost is that a key whose
        default moved keeps the old value for ever on an existing install, and
        after the platform change one of those values is actively wrong:
        `profile_url` still building `x.com/{handle}` means the campaign opens X
        profiles for Messenger handles and finds nobody.

        So the ones that are wrong rather than merely customised are moved, and
        only when they are still sitting on the OLD DEFAULT - a template the
        operator has edited to something of their own is theirs, and this does
        not touch it. Keys the schema no longer has go too; nothing reads them,
        but a settings block listing knobs that do not exist is a file that
        cannot be understood by reading it.

        Idempotent, and cheap on a store with nothing to fix.
        """
        moved: list[str] = []
        with self.edit() as db:
            settings = db.setdefault("settings", {})

            stale_url = str(settings.get("profile_url") or "")
            if "x.com/" in stale_url or "twitter.com/" in stale_url:
                settings["profile_url"] = defaults()["profile_url"]
                moved.append("profile_url")

            for key in [k for k in settings if not known(k)]:
                del settings[key]
                moved.append(key)
        return moved

    def clean_names(self) -> int:
        """Blank every stored name that is really a link's label.

        The feed scraper read the words on a post's header link, and on some
        posts those words are Facebook's own - "See owner profile" - not the
        person's name. Dozens of rows then carried the same name and read as
        the same account listed over and over. A blank name shows the handle
        instead, which is at least true. Run at startup; idempotent.
        """
        from .schema_names import is_not_a_name   # noqa: PLC0415 - small, and only here
        fixed = 0
        with self.edit() as db:
            groups = [db["store"]] + [r.get("accounts") or [] for r in db["rosters"]]
            for rows in groups:
                for row in rows:
                    if row.get("name") and is_not_a_name(row["name"]):
                        row["name"] = ""
                        fixed += 1
        return fixed

    def recover_bios(self) -> int:
        """Repair every stored bio: throw out what was never one, recover what was lost.

        Run at startup, and idempotent - it only touches rows that have a card
        and no bio, and a card with no bio in it stays that way. It is here
        rather than as a one-off script because the fix belongs to the data: the
        console kept the whole popup from the beginning, so every roster written
        while the parser was looking in the wrong place is recoverable without
        walking the feed again, on any machine, whenever the server next starts.

        Not called from `_load`: this needs `automation.x`, and x imports events
        which imports this module - so it runs once the package is fully
        imported, from the server's own startup.
        """
        fixed = 0
        with self.edit() as db:
            groups = [db["store"]] + [r.get("accounts") or [] for r in db["rosters"]]
            for rows in groups:
                for i, row in enumerate(rows):
                    repaired = _with_bio(row)
                    if repaired is not row:
                        rows[i] = repaired
                        fixed += 1
        return fixed

    # -- The day's budget ------------------------------------------
    def counters(self) -> dict[str, Any]:
        with self.edit() as db:
            day = db["counters"].get(today())
            if day is None:
                # Only today is kept. A month of per-account tallies is a record
                # of who was messaged and when, and nothing in the console reads
                # yesterday's.
                day = {"total": 0, "per_account": {}}
                db["counters"] = {today(): day}
            return copy.deepcopy(day)

    def count_dm(self, account_id: str) -> None:
        with self.edit() as db:
            day = db["counters"].setdefault(today(), {"total": 0, "per_account": {}})
            day["total"] = day.get("total", 0) + 1
            day["per_account"][account_id] = day["per_account"].get(account_id, 0) + 1


class _Edit:
    def __init__(self, store: Store):
        self._store = store

    def __enter__(self) -> dict[str, Any]:
        self._store._lock.acquire()
        return self._store._db

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            # Saved even when the block raised: whatever it managed to change is
            # already true in memory, and a crash that silently un-does half a
            # sweep is harder to reason about than one that keeps it.
            self._store._write()
        finally:
            self._store._lock.release()
        return False


store = Store(config.STORE_FILE)
