"""The browser, and the one thread allowed to touch each account's window.

Playwright's sync API belongs to the thread that created it. Tasks run on their
own threads and HTTP handlers on someone else's, so every call into a window is
posted to that window's owner thread and waited on.

ONE THREAD PER ACCOUNT, not one thread for all of them. Each account is its own
Chrome, launched out of its own profile directory - separate processes sharing
nothing - and a single owner thread made them take turns for no reason but the
shape of this file. It also made "scrape two accounts at once" unwritable: the
second walk would sit in the queue behind a first walk that never ends, and a
walk of the home feed has no end. So each account gets a LANE - a thread, a
Playwright instance and a browser context of its own - and lanes run alongside
each other.

What is still true, and is why a lane is per ACCOUNT rather than per call: there
is one Chrome per account, and two runs typing into one of them would interleave
their keystrokes. Everything aimed at one account is still single file.

What the rest of the server sees, unchanged:

    automation.open_account(id)           -> launches (or reuses) that profile
    automation.with_page(id, fn)          -> runs fn(page) on that account's lane
    automation.status()                   -> read WITHOUT touching Playwright

That last one matters. The status pill polls every five seconds; if it queued
behind a sweep it would freeze for the whole run, so it reads a plain-Python
mirror the lanes keep up to date instead.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from concurrent.futures import Future
from queue import Queue
from typing import Any, Callable, Optional

from .. import config
from ..events import log


def goto(page, url: str, *, timeout: int = 45_000, attempts: int = 3,
         wait_until: str = "domcontentloaded") -> None:
    """Navigate, allowing for the browser navigating itself.

    A freshly created profile does its own first-run navigation a moment after
    launch, and it lands on top of ours: Playwright reports the goto as
    "interrupted by another navigation" and the whole call fails. It is not an
    error about the page - the second navigation is Chrome settling in - so it is
    waited out and retried rather than raised at the operator, who can do nothing
    with it.

    `wait_until="commit"` is what the pooled campaign passes. It returns as soon
    as the navigation is the page's own - before a byte of the document has been
    parsed - which is the whole trick behind having five chats loading at once:
    this call has to give the lane's thread back straight away, or the next tab
    cannot be started until this one has finished booting. The caller then polls
    for what it actually needs. Nobody else wants it: for a single page,
    "committed" is not a state anything can be read out of.
    """
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as err:                     # noqa: BLE001 - re-raised below
            # Two spellings of the same thing, and both are Chrome settling
            # rather than a page that will not load:
            #   "interrupted by another navigation" - a first-run profile
            #       navigating itself a moment after launch;
            #   ERR_ABORTED - our navigation CANCELLED by one starting on top
            #       of it, which is what session restore does on the first goto
            #       after a launch. Measured: the first open fails in 0.2s, the
            #       next one succeeds in 2.4s against the same window.
            # Neither is anything the operator can act on, so both are waited
            # out and retried rather than raised at them.
            text = str(err)
            interrupted = ("interrupted by another navigation" in text
                           or "ERR_ABORTED" in text)
            if not interrupted or attempt == attempts - 1:
                raise
            page.wait_for_timeout(1500)


# Playwright's own --disable-features value, verbatim. Ours has to CONTAIN it,
# because Chromium keeps switches in a map: a second --disable-features does not
# add to the first, it replaces it. Passing only our own names would silently
# switch Playwright's list back on.
_PLAYWRIGHT_DISABLES = (
    "ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,"
    "DestroyProfileOnBrowserClose,MediaRouter,DialMediaRouteProvider,AcceptCHFrame,"
    "AutoExpandDetailsElement,CertificateTransparencyComponentUpdater,"
    "AvoidUnnecessaryBeforeUnloadCheckSync,Translate,HttpsUpgrades,PaintHolding,"
    "ThirdPartyStoragePartitioning,LensOverlay,PlzDedicatedWorker"
)

# Chrome's on-device AI model, and what pulls it down. It arrives per PROFILE,
# and a profile here is per account: four gigabytes in `profiles/2` alone, on a
# disk that is already full, for a model nothing in this console can use. Off.
_OFF = _PLAYWRIGHT_DISABLES + (
    ",OptimizationGuideOnDeviceModel,OptimizationGuideModelDownloading,"
    "TextSafetyClassifier,OptimizationHints"
)


# The directories Chrome fills with that model. Named here because they also
# have to be cleared out of the profiles that already have one - the switch only
# stops the next download, it does not undo the last.
_MODEL_STORES = ("OptGuideOnDeviceModel", "optimization_guide_model_store")


def _drop_model_store(profile) -> None:
    """Delete Chrome's on-device AI model out of this profile before launching.

    Safe to lose: Chrome treats it as a component and would fetch it again if it
    were ever wanted, and none of the session is in it - cookies and storage sit
    under `Default/`, which is not touched. Doing it at launch rather than in a
    script means the profiles already carrying four gigabytes clear themselves
    the first time they are opened after this change, without anybody being told
    to go and find them.
    """
    for name in _MODEL_STORES:
        target = profile / name
        if not target.exists():
            continue
        try:
            freed = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        except OSError:
            freed = 0
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            log("browser", f"cleared {freed / 1e9:.1f}GB of Chrome's on-device "
                           f"model out of profile {profile.name}")


# -- Making the session outlive the window ------------------------
# Facebook issues part of its login as a SESSION cookie: one with no expiry,
# which Chrome keeps in memory and drops the moment the browser closes. That is
# the whole reason a profile signed in yesterday asks for a password today - the
# jar on disk was never missing anything, because the half that mattered was
# never written to disk at all.
#
# `restore_on_startup: 1` is Chrome's "continue where you left off", and it is
# the one setting that makes Chrome persist those cookies across a restart
# instead of discarding them. It has to be in the profile BEFORE the launch that
# creates the cookies, which is why this runs at launch and not in a script
# somebody has to remember.
_PREFS: dict[str, Any] = {
    # NOTE: `session.restore_on_startup` is NOT here, and must not be added.
    # It is a PROTECTED preference - Chrome MAC-validates it through
    # `Default/Secure Preferences`, whose `protection.macs` lists
    # `restore_on_startup` and `restore_on_startup_encrypted_hash`. A value
    # written into plain Preferences without a matching MAC is treated as
    # tampering and silently reset, which is what happened: it read back as
    # absent on every launch and the "keep the session cookies" behaviour was
    # never actually on. The switch below (`--restore-last-session`) is the
    # mechanism that works; the MAC is keyed to this machine and profile and is
    # not something to forge.
    # How Chrome decides whether it crashed. It is killed by design here - at
    # shutdown, and by anyone who closes the window - and a profile Chrome
    # believes crashed reopens with a "Restore pages?" bubble sitting over
    # whatever the run just asked for, which a run cannot dismiss and does not
    # expect. Marking the last exit clean is what keeps that bubble away.
    "profile": {"exit_type": "Normal", "exited_cleanly": True},
}


def _seed_prefs(profile) -> None:
    """Merge `_PREFS` into this profile's Preferences file before launch.

    Merged, never written over the top: Preferences is where Chrome keeps
    everything else about the profile too, and replacing the file to set two
    keys would throw the rest away. A profile that has not launched yet has no
    file, and an empty dict is the right starting point for one.
    """
    path = profile / "Default" / "Preferences"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):             # a truncated write, last time
            current = {}
    except (OSError, ValueError):
        # Unreadable Preferences is not worth failing a launch over - Chrome
        # rebuilds it from defaults - so the seed is written on its own.
        current = {}

    changed = False
    for section, values in _PREFS.items():
        block = current.setdefault(section, {})
        if not isinstance(block, dict):
            block = current[section] = {}
        for key, value in values.items():
            if block.get(key) != value:
                block[key] = value
                changed = True
    if not changed:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current), encoding="utf-8")
    except OSError as err:
        log("browser", f"could not seed Chrome preferences for profile "
                       f"{profile.name}: {err}", "warn")


# What "signed in" means in a Facebook cookie jar: `c_user` is the account id,
# `xs` is the session token. Everything else in there survives a sign-out.
_LOGIN_COOKIES = frozenset({"c_user", "xs"})


def _backup_path(account_id: str):
    return config.SESSIONS / f"{account_id}.json"


class AutomationError(RuntimeError):
    """Anything the operator has to fix: no browser, no session, a dead profile.

    The message goes straight to a toast, so it is written as a sentence
    addressed to a person.
    """


def _running_as_root() -> bool:
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as err:                       # pragma: no cover - env-dependent
        raise AutomationError(
            "Playwright is not installed. Run: pip install -r backend/requirements.txt "
            "and then: python -m playwright install chromium"
        ) from err
    return sync_playwright


class _Lane:
    """One account's window, its Playwright, and the thread that owns both.

    The thread starts on the first call and lives until the window is closed or
    the server shuts down. It is not started with the lane: an account nobody has
    opened should not cost a thread and a driver process.
    """

    def __init__(self, account_id: str,
                 mirror: Callable[[str, Optional[int]], None]) -> None:
        self.account_id = account_id
        self._mirror = mirror
        self._jobs: "Queue[tuple[Optional[Callable[[_Lane], Any]], Future]]" = Queue()
        self._thread: Optional[threading.Thread] = None
        self._boot = threading.Lock()
        self._pw = None
        self._context = None
        # True from the moment a context is built until someone asks. Only the
        # tabs Chrome RESTORED belong to the launch; everything opened after it
        # belongs to the operator - see `open_account`.
        self._fresh = False
        # Set once, on the way out. A retired lane will not build a window, and
        # will not start a thread to be asked to - see `retire`.
        self._closing = False

    # -- The owner thread -----------------------------------------
    def _ensure_thread(self) -> None:
        with self._boot:
            if self._thread and self._thread.is_alive():
                return
            ready: "Future[bool]" = Future()
            self._thread = threading.Thread(
                target=self._serve, args=(ready,),
                name=f"automation-{self.account_id}", daemon=True,
            )
            self._thread.start()
            ready.result(timeout=60)     # raises here if Playwright will not start

    def _serve(self, ready: "Future[bool]") -> None:
        sync_playwright = _playwright()
        try:
            with sync_playwright() as pw:
                self._pw = pw
                ready.set_result(True)
                while True:
                    job, future = self._jobs.get()
                    if job is None:
                        future.set_result(None)
                        break
                    try:
                        future.set_result(job(self))
                    except Exception as err:         # noqa: BLE001 - handed to the caller
                        future.set_exception(err)
        except Exception as err:                     # noqa: BLE001
            if not ready.done():
                ready.set_exception(err)
        finally:
            self._pw = None

    def call(self, job: Callable[["_Lane"], Any], timeout: Optional[float] = None) -> Any:
        """Run `job(self)` on this lane's thread and wait for it.

        Called from inside that thread it just runs - a job that reaches for
        another job would otherwise post work to a queue only it can drain, and
        wait for itself for ever. A job that reaches into ANOTHER account's lane
        is a different matter and is fine: that is a different thread, and this
        one simply waits on it.
        """
        if threading.current_thread() is self._thread:
            return job(self)
        if self._closing and not (self._thread and self._thread.is_alive()):
            # Shutting down. `_ensure_thread` would start a thread and a driver
            # here, which is the opposite of what is being asked for.
            raise AutomationError("The browser is shutting down.")
        self._ensure_thread()
        future: Future = Future()
        self._jobs.put((job, future))
        return future.result(timeout=timeout if timeout is not None else config.AUTOMATION_TIMEOUT)

    def end(self) -> None:
        """Let the thread finish, which is what shuts this lane's driver down."""
        if not (self._thread and self._thread.is_alive()):
            return
        if threading.current_thread() is self._thread:
            return
        try:
            future: Future = Future()
            self._jobs.put((None, future))
            future.result(timeout=30)
        except Exception:                            # noqa: BLE001 - shutting down anyway
            pass

    # -- The window (this lane's thread only) ----------------------
    def context(self, create: bool = True):
        if self._context is not None:
            return self._context
        if not create or self._closing:
            return None

        config.ensure_dirs()
        profile = config.PROFILES / str(self.account_id)
        profile.mkdir(parents=True, exist_ok=True)
        _drop_model_store(profile)
        _seed_prefs(profile)

        launch: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": config.HEADLESS,
            "args": [
                # The one flag worth setting. Chrome under automation announces
                # itself in `navigator.webdriver`, and X reads it.
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                # The flag form of `session.restore_on_startup`, and the one
                # that actually survives. Seeding the pref alone is not enough:
                # on a PROFILE THAT DOES NOT EXIST YET, Chrome regenerates
                # Preferences during first-run initialisation and the seeded key
                # is gone by the time the profile is usable - verified on a
                # freshly deleted profile, where `restore_on_startup` read back
                # as absent. A switch cannot be rewritten by the browser, so the
                # behaviour that keeps Facebook's session cookies is asked for
                # here and merely reinforced by `_seed_prefs`.
                # Chrome persists Facebook's SESSION cookies - the ones with
                # no expiry, which are otherwise dropped the moment the browser
                # closes - only when it is restoring the last session. The
                # preference for it is MAC-protected and cannot be seeded (see
                # `_PREFS`), so the switch is the only way to ask.
                #
                # It costs one thing: session restore fires a navigation of its
                # own at startup that cancels the first `goto` with ERR_ABORTED.
                # That is handled in `goto`, which retries it - measured, the
                # first Open used to fail in 0.2s and now succeeds.
                "--restore-last-session",
                #
                # The real window's size. `--window-size` is the only thing that
                # sets it; Playwright's `viewport` does not - see below.
                "--window-size=1280,900",
                f"--disable-features={_OFF}",
                # In a container the server runs as root, and Chrome refuses
                # to start its sandbox there ("Chromium sandboxing failed").
                # On a desktop, as a normal user, the sandbox stays on.
                *(["--no-sandbox"] if _running_as_root() else []),
            ],
            # Playwright adds `--enable-automation` to every launch, and Google
            # refuses to sign anybody in to a browser carrying it - the account
            # chooser on "Continue with Google" comes up empty, or the page says
            # the browser may not be secure. That single flag is the difference
            # between an operator being able to use SSO in this window and not,
            # and nothing here needs it: it exists to put an infobar on the
            # window and to change a few defaults, not to make automation work.
            # Two switches, and the second is ours. `--enable-automation` is
            # dropped outright; the --disable-features Playwright would pass is
            # named so it is dropped rather than duplicated - and if a Playwright
            # release changes that string, the one we append still lands last and
            # still wins.
            "ignore_default_args": [
                "--enable-automation",
                f"--disable-features={_PLAYWRIGHT_DISABLES}",
            ],
        }
        # THE VIEWPORT, and why a visible window must not have one set.
        #
        # `viewport={"width":1280,"height":900}` does NOT resize a headful
        # window. It installs an emulation override, so the page is laid out as
        # if it had 900px of height inside a window whose real content area is
        # about 730px. Measured on this machine:
        #
        #     viewport set : inner 1280x900, outer 1298x882, dpr forced to 1.0
        #     no_viewport  : inner 1036x647, outer 1051x798, dpr 1.25 (real)
        #
        # The bottom ~170px of every page is then rendered, present in the DOM,
        # and never painted on screen - with no scrollbar to reach it, because
        # the page believes it already fits. Messenger puts the message composer
        # at the bottom of the thread, which is exactly what went missing: the
        # composer's DOM node was byte-identical to real Chrome's, and invisible.
        #
        # Headless has no window, so there the viewport is the only thing that
        # gives the page a size and it stays.
        if config.HEADLESS:
            launch["viewport"] = {"width": 1280, "height": 900}
        else:
            launch["no_viewport"] = True

        if config.BROWSER_CHANNEL:
            launch["channel"] = config.BROWSER_CHANNEL

        try:
            context = self._pw.chromium.launch_persistent_context(**launch)
        except Exception as err:                     # noqa: BLE001 - retried without the channel
            if not config.BROWSER_CHANNEL:
                raise
            # The channel names a browser INSTALLED on this machine, and the
            # store travels between machines. Falling back to the bundled
            # Chromium keeps a run working on a box where Chrome is not present,
            # and says so rather than quietly changing what it is driving.
            log("browser",
                f"{config.BROWSER_CHANNEL} would not start ({err}); "
                "falling back to the bundled Chromium", "warn")
            launch.pop("channel", None)
            context = self._pw.chromium.launch_persistent_context(**launch)

        context.set_default_timeout(30_000)

        # The window can be closed from outside the app - by the operator, or by
        # a crash - and the console must find out. Without this a run would keep
        # driving something that is not there.
        def gone(_ctx=None):
            self._context = None
            self._mirror(self.account_id, None)
            log("browser", f"the window on account {self.account_id} closed", "warn")

        context.on("close", gone)
        self._context = context
        self._fresh = True
        self._restore_cookies(context)
        self.touch()
        return context

    def _restore_cookies(self, context) -> None:
        """Put the backed-up session into a profile that has lost its own.

        Cookies AND localStorage. Restoring only the cookies is what made
        Facebook demand a verification on every single sign-in: a session token
        arriving in a browser that holds none of the local state that browser is
        supposed to have is the exact shape of a session lifted onto a new
        device, and a challenge is the correct response to it. `storage_state()`
        captures 18 localStorage keys per origin here and they were being read
        off disk and thrown away.

        localStorage cannot be written before a page exists on the origin, so it
        goes in through an init script - see `_seed_local_storage` - rather than
        being pushed at the context the way cookies are.

        What still does NOT come back, and cannot: Messenger is end-to-end
        encrypted and those keys live in IndexedDB, which `storage_state()` does
        not capture. The login returns; the message history does not, and
        Messenger will ask to restore the chats from the PIN. That is Facebook's
        design, not a gap here.
        """
        backup = _backup_path(self.account_id)
        if not backup.exists():
            return
        try:
            have = {c.get("name") for c in context.cookies()}
            # The test is "has this profile lost the LOGIN", not "is the jar
            # empty". It was the second, and that was wrong: Chrome keeps the
            # long-lived device cookies (`datr`, `sb`) after dropping the
            # session ones, so the jar is never actually empty and a backup
            # taken minutes earlier sat unused while the card read "signed out"
            # and the operator was asked to sign in again.
            #
            # `c_user` is who, `xs` is the session itself. Both present means
            # this profile has its own live login and must be left alone - it is
            # always newer than the backup.
            if _LOGIN_COOKIES <= have:
                return
            state = json.loads(backup.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if not cookies:
                return
            context.add_cookies(cookies)
            keys = self._seed_local_storage(context, state.get("origins") or [])
        except Exception as err:                     # noqa: BLE001 - never fatal
            log("browser", f"could not restore the saved session for account "
                           f"{self.account_id}: {err}", "warn")
            return
        log("browser", f"restored {len(cookies)} cookies and {keys} localStorage "
                       f"keys into profile {self.account_id} from the saved session")

    @staticmethod
    def _seed_local_storage(context, origins: list) -> int:
        """Put the saved localStorage back, on the origins it came from.

        Through an init script because there is nowhere else to put it: a
        context has no localStorage of its own, the store belongs to an origin,
        and an origin only exists once a page is on it. The script runs before
        any of the page's own scripts, so Facebook's code sees the restored
        state as if it had always been there.

        Existing keys are never overwritten. A profile that already has state
        for the origin is the live one and is newer than any backup; the seed is
        only there to fill a store that is empty.
        """
        data = {o.get("origin"): {i["name"]: i["value"]
                                  for i in (o.get("localStorage") or [])}
                for o in origins if o.get("origin")}
        data = {k: v for k, v in data.items() if v}
        if not data:
            return 0

        context.add_init_script(
            "(() => { try {"
            "  const all = " + json.dumps(data) + ";"
            "  const mine = all[location.origin];"
            "  if (!mine) return;"
            "  for (const k in mine) {"
            "    if (window.localStorage.getItem(k) === null)"
            "      window.localStorage.setItem(k, mine[k]);"
            "  }"
            "} catch (e) {} })();"
        )
        return sum(len(v) for v in data.values())

    def write_state(self) -> int:
        """Copy this window's cookie jar out to `data/sessions/<id>.json`.

        Written through a temporary file and moved into place: the reader is a
        launch, and a launch that catches a half-written jar would restore a
        broken session over a profile that had just been wiped - the one moment
        there is nothing left to fall back to.
        """
        context = self.context(create=False)
        if context is None:
            raise AutomationError("There is no window open on this account to save.")
        state = context.storage_state()
        cookies = state.get("cookies") or []

        config.ensure_dirs()
        target = _backup_path(self.account_id)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(state), encoding="utf-8")
        temp.replace(target)
        return len(cookies)

    def page(self):
        context = self.context()
        if context is None:                          # retired mid-run
            raise AutomationError("The window on this account is closed.")
        page = context.pages[0] if context.pages else context.new_page()
        self.touch()
        return page

    def take_fresh(self) -> bool:
        """Was this window built since the last time anybody asked? Clears it."""
        fresh, self._fresh = self._fresh, False
        return fresh

    def touch(self) -> None:
        self._mirror(self.account_id,
                     len(self._context.pages) if self._context is not None else 0)

    def retire(self) -> None:
        """Refuse new work, close the window, end the thread - in that order.

        The order is the whole of it. Closing the window first leaves a lane
        that will happily open another one the next time anything asks it for a
        page, and at shutdown something always does: a run one poll behind the
        server it is running in. So the refusal comes first, and by the time the
        window goes there is nothing left that could bring it back.
        """
        self._closing = True
        if not (self._thread and self._thread.is_alive()):
            # Never opened, or the thread has already gone. There is no window
            # to close and nothing to wait for; just stop calling it open.
            self._context = None
            self._mirror(self.account_id, None)
            return
        try:
            self.call(lambda l: l.close_window(), timeout=30)
        except Exception:                            # noqa: BLE001 - going anyway
            self._mirror(self.account_id, None)
        # And the thread with it, so this profile's driver process goes too.
        self.end()

    def close_window(self) -> None:
        context, self._context = self._context, None
        self._mirror(self.account_id, None)
        if context is not None:
            self._refresh_state(context)
            try:
                context.close()
            except Exception:                        # noqa: BLE001 - already gone
                pass

    def _refresh_state(self, context) -> None:
        """Re-take the backup on the way out, while the window is still alive.

        The copy taken at sign-in is the thinnest one there will ever be:
        `save_session` snapshots the moment the login is detected, before the
        app has written most of its local state. Measured on this profile - 10
        localStorage keys on one origin at sign-in, 18 on each of two once the
        session had actually been used. That state is what keeps Facebook from
        challenging the next launch, so the copy worth keeping is the LAST one.

        Two guards, and both matter:

          * Only refreshes a backup that already EXISTS. `logout` deletes the
            file and then closes the window; re-creating it here would put back
            the session the operator just cleared.
          * Only writes a jar that is still signed in. A window sitting on the
            login wall would otherwise overwrite a good backup with an empty
            one - losing the session at exactly the moment it is needed.
        """
        if not _backup_path(self.account_id).exists():
            return
        try:
            state = context.storage_state()
            names = {c.get("name") for c in state.get("cookies") or []}
            if not _LOGIN_COOKIES <= names:
                return
            target = _backup_path(self.account_id)
            temp = target.with_suffix(".json.tmp")
            temp.write_text(json.dumps(state), encoding="utf-8")
            temp.replace(target)
            keys = sum(len(o.get("localStorage") or [])
                       for o in state.get("origins") or [])
            log("browser", f"refreshed the saved session for account "
                           f"{self.account_id} on the way out "
                           f"({len(state.get('cookies') or [])} cookies, {keys} keys)")
        except Exception as err:                     # noqa: BLE001 - shutting down anyway
            log("browser", f"could not refresh the saved session for account "
                           f"{self.account_id}: {err}", "warn")


class Automation:
    """Every account's lane, and the one mirror the whole server reads."""

    def __init__(self) -> None:
        self._lanes: dict[str, _Lane] = {}
        self._lock = threading.Lock()
        # Set once, by `shutdown`, and never cleared. A retired LANE refuses to
        # open a window; this is what stops a run being handed a brand new lane
        # that has not heard the news.
        self._closing = False
        # The mirror. Written by the lanes, read by anyone, never Playwright.
        self._open: dict[str, int] = {}

    def _lane(self, account_id: str) -> _Lane:
        account_id = str(account_id)
        with self._lock:
            if self._closing:
                # Shutting down. Building a lane here is how a closed window
                # comes back: `shutdown` empties this dict, and the next poll
                # from a run that has not stopped yet would find nothing under
                # its account and be given a clean lane that opens Chrome again.
                raise AutomationError("The browser is shutting down.")
            lane = self._lanes.get(account_id)
            if lane is None:
                lane = _Lane(account_id, self._note_pages)
                self._lanes[account_id] = lane
            return lane

    def _note_pages(self, account_id: str, pages: Optional[int]) -> None:
        with self._lock:
            if pages is None:
                self._open.pop(account_id, None)
            else:
                self._open[account_id] = pages

    # -- What the panels call --------------------------------------
    def open_account(self, account_id: str,
                     url: str = "https://www.messenger.com/",
                     *, timeout: Optional[int] = None) -> str:
        """Launch (or reuse) this account's window and put `url` on screen.

        `timeout` is the navigation's, in milliseconds, and is passed through to
        `goto` - a caller that would rather look at a half-loaded page than wait
        for it can shorten it. Left alone it is `goto`'s own default, so every
        caller that does not care is unmoved.
        """
        def job(lane: _Lane) -> str:
            context = lane.context()
            if context is None:
                raise AutomationError("The window on this account is closed.")
            # `--restore-last-session` is what keeps Facebook's session
            # cookies across a restart, and the same switch asks Chrome to
            # reopen the last window's tabs. Harmless to a person, confusing to
            # a run: `page()` hands back pages[0], which after a restore is
            # whatever was open last time rather than the page being asked for.
            #
            # ONLY on the first open after a launch. This used to run on every
            # open, which closed the tabs the OPERATOR had opened: open Facebook
            # in a second tab, press Open, and the tab vanished. The restored
            # tabs are the launch's; anything after that is the operator's, and
            # this window is theirs to use.
            if lane.take_fresh():
                for extra in context.pages[1:]:
                    try:
                        extra.close()
                    except Exception:                # noqa: BLE001 - already gone
                        pass
            page = lane.page()
            if timeout is None:
                goto(page, url)
            else:
                goto(page, url, timeout=timeout)
            lane.touch()
            return page.url

        return self._lane(account_id).call(job, timeout=180)

    def close_account(self, account_id: str) -> None:
        account_id = str(account_id)
        with self._lock:
            lane = self._lanes.pop(account_id, None)
        if lane is None:
            self._note_pages(account_id, None)
            return
        # Reopening builds a fresh lane rather than reviving this one, so
        # retiring the one being dropped costs nothing and closes the window
        # even if a run is still holding it.
        lane.retire()

    def with_page(self, account_id: str, fn: Callable[[Any], Any],
                  timeout: Optional[float] = None) -> Any:
        """Hand `fn` the account's page, on that account's lane."""
        def job(lane: _Lane) -> Any:
            page = lane.page()
            try:
                return fn(page)
            finally:
                lane.touch()

        return self._lane(account_id).call(job, timeout=timeout)

    def with_context(self, account_id: str, fn: Callable[[Any], Any],
                     timeout: Optional[float] = None) -> Any:
        def job(lane: _Lane) -> Any:
            context = lane.context()
            try:
                return fn(context)
            finally:
                lane.touch()

        return self._lane(account_id).call(job, timeout=timeout)

    # -- Tabs a caller holds on to --------------------------------
    #
    # `with_page` hands out pages[0] and owns it for the length of one call,
    # which is the right shape for everything that does one thing to one page.
    # The pooled campaign is the other shape: it keeps five tabs alive across
    # hundreds of short calls, and each tab outlives the call that made it. So
    # these three hand the PAGE OBJECT back rather than taking a body to run
    # against it.
    #
    # What still holds is that every call ON that page happens on this
    # account's own thread. Playwright's sync API is bound to the thread its
    # driver was started on and raises if touched from another, so a page
    # travelling to the campaign thread is safe only because the campaign never
    # does more with it than pass it back through `on_lane`.
    def new_tab(self, account_id: str, timeout: Optional[float] = None):
        """Open a tab in this account's window and hand the page back."""
        def job(lane: _Lane):
            context = lane.context()
            if context is None:
                raise AutomationError("The window on this account is closed.")
            page = context.new_page()
            lane.touch()
            return page

        return self._lane(account_id).call(job, timeout=timeout)

    def on_lane(self, account_id: str, fn: Callable[[], Any],
                timeout: Optional[float] = None) -> Any:
        """Run `fn()` on this account's thread, with nothing handed to it.

        For a caller that already holds the page it means to touch and needs
        only to be somewhere it is allowed to touch it.
        """
        return self._lane(account_id).call(lambda lane: fn(), timeout=timeout)

    def close_tab(self, account_id: str, page) -> None:
        """Close one tab of this account's. Never raises.

        A tab that has already gone - the operator closed it, the window went -
        is the outcome being asked for, and a run must not fail a recipient it
        has already sent to over the tidying up afterwards.
        """
        def job(lane: _Lane) -> None:
            try:
                page.close()
            except Exception:                        # noqa: BLE001 - already gone
                pass
            lane.touch()

        try:
            self._lane(account_id).call(job, timeout=60)
        except Exception:                            # noqa: BLE001 - window gone
            pass

    def close_extra_tabs(self, account_id: str) -> int:
        """Leave one tab standing. `Open all profiles` can leave two dozen."""
        def job(lane: _Lane) -> int:
            context = lane.context(create=False)
            if context is None:
                return 0
            closed = 0
            for page in context.pages[1:]:
                try:
                    page.close()
                    closed += 1
                except Exception:                    # noqa: BLE001
                    pass
            lane.touch()
            return closed

        return self._lane(account_id).call(job, timeout=120)

    def save_state(self, account_id: str) -> int:
        """Back this account's cookie jar up outside its profile.

        Returns how many cookies were kept, so a caller can tell a real session
        from a window that happens to be open on a login wall.
        """
        return self._lane(account_id).call(lambda lane: lane.write_state(), timeout=120)

    def has_state(self, account_id: str) -> bool:
        return _backup_path(str(account_id)).exists()

    def drop_state(self, account_id: str) -> None:
        """Throw the backup away. Signing out has to reach this too.

        Otherwise `logout` clears the live jar, the next launch finds the
        profile empty, and the backup signs the account straight back in.
        """
        try:
            _backup_path(str(account_id)).unlink(missing_ok=True)
        except OSError as err:                       # noqa: BLE001
            log("browser", f"could not remove the saved session for account "
                           f"{account_id}: {err}", "warn")

    def shutdown(self) -> None:
        """Close every window this server opened.

        Called from the server's shutdown hook, which is the last thing here
        that runs. Chrome is a child process holding a live session: left behind
        it keeps the profile directory locked, and the next start finds the
        directory in use and refuses to open it.

        The runs are stopped before this is called, not by it - see the hook in
        `app.py`. That is not tidiness: a run still polling will ask its lane for
        a page a moment after the window closed, and asking for a page is what
        opens one.

        Every lane, even if one of them will not go. A single window wedged
        behind a run that ignored its stop must not keep the other five open.
        """
        with self._lock:
            self._closing = True
            lanes = list(self._lanes.values())
            self._lanes.clear()
        for lane in lanes:
            try:
                lane.retire()
            except Exception:                        # noqa: BLE001 - shutting down anyway
                pass

    # -- The mirror ------------------------------------------------
    def is_open(self, account_id: str) -> bool:
        with self._lock:
            return str(account_id) in self._open

    def open_ids(self) -> list[str]:
        with self._lock:
            return list(self._open)

    def status(self) -> dict[str, Any]:
        with self._lock:
            windows = len(self._open)
            pages = sum(self._open.values())
        return {"open": windows > 0, "count": windows, "pages": pages}


automation = Automation()
