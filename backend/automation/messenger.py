"""Everything that knows what Messenger's pages look like.

The sibling of `x.py`, and a drop-in replacement for it: every name the panels
reach for - `whoami`, `peek`, `login`, `walk_home`, `read_profile`, `open_chat`,
`write_dm`, `render`, `click_action`, `NAV_ACTIONS`, `DmClosed` - exists here
with the same signature and the same return shape. The panels changed their
import and nothing else.

Where this differs from X, and why the shapes still line up:

  * A HANDLE here is a Facebook username or numeric id - what sits after
    `/t/` in a thread URL and after `facebook.com/` on a profile. The console is
    handle-keyed from the store up, so keeping that word (rather than renaming
    everything to "contact id") is what let the panels stay as they are.
  * The HOME WALK is the chat list, not a feed. Messenger has no public
    follower list and no timeline of strangers; what it has is the conversations
    this account already has, so that is what a sweep collects.
  * There is no GOOGLE SSO, and so no `google_account_beside`. Facebook is its
    own identity provider: the session in the profile IS a Facebook session,
    and the account it belongs to is what `whoami` already reports.

On selectors. Messenger ships no `data-testid`; its class names are generated
per build and are worthless as handles. What it does have, because the whole
product is built on a screen-reader-visible tree, is ROLES and ARIA LABELS -
`[role="textbox"]`, `[aria-label="Chats"]`, `a[href*="/t/"]`. Those are what is
used here, always in a comma-separated list of two or three candidates, because
an aria-label is a human-readable string and human-readable strings get
reworded. When Messenger ships a build that breaks this, the repair is this file
and only this file.

  ! The selector strings below are written against Messenger's current DOM
  ! conventions, and they have NOT been run against a signed-in session - there
  ! was no Messenger account to sign in to. The structure, the flows and the
  ! contract are right; expect to adjust one or two strings on first contact,
  ! and expect them to be in the block at the top of this file when you do.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable, Iterable, Optional

from ..events import log
from .browser import AutomationError, goto

# -- URLs ---------------------------------------------------------
# The inbox IS the home page: messenger.com signed in is the chat list, which is
# the page every walk starts from and every action returns to.
HOME_URL = "https://www.messenger.com/"
LOGIN_URL = "https://www.messenger.com/login/"

# A thread by the person in it. Messenger accepts a username or a numeric id in
# the same position, which is why one template covers both.
THREAD_TEMPLATE = "https://www.messenger.com/t/{handle}"
# The profile behind a thread lives on Facebook, not on Messenger. This is the
# `template` default the panels pass around, so it keeps the `{handle}` shape.
PROFILE_TEMPLATE = "https://www.facebook.com/{handle}"


# -- Selectors ----------------------------------------------------
# Signed in. Any one of these three is enough: the chat grid, the rail beside
# it, or the button that starts a new message. Three rather than one because
# Messenger renders a different shell on a narrow window and on a cold load, and
# a session check that only knew the widest one would call a real session dead.
SIGNED_IN = (
    '[role="grid"][aria-label*="Chat" i]',
    '[role="navigation"][aria-label*="Chat" i]',
    '[aria-label="New message" i]',
    '[role="main"] [role="textbox"]',
)
SIGNED_IN_ANY = ", ".join(SIGNED_IN)

# The login wall, and the form on it.
LOGIN_EMAIL = 'input[name="email"], input[id="email"], input[type="email"]'
LOGIN_PASSWORD = 'input[name="pass"], input[id="pass"], input[type="password"]'
LOGIN_SUBMIT = ('button[name="login"], button[id="loginbutton"], '
                'div[role="button"][aria-label*="Log in" i], button[type="submit"]')

# One row of the chat list. The link is the load-bearing part - it carries the
# thread id - and the row around it carries the name.
# Scoped to the chat list, because the RAIL carries `/t/` links as well: the
# survey found `/marketplace/t/<id>`, `/requests/t/<id>` and `/archived/t/<id>`
# beside the real row, all pointing at the same conversation. Unscoped, a sweep
# counts one thread five times and calls four of them separate people.
CHAT_LIST = '[role="grid"][aria-label*="Chat" i], [role="navigation"][aria-label*="Chat" i]'
# Written out per branch rather than f-strung onto CHAT_LIST: a selector list is
# comma-separated, so `f"{CHAT_LIST} a[...]"` binds the descendant to the LAST
# branch only and leaves the first matching the grid itself instead of its links.
THREAD_LINK = ('[role="grid"][aria-label*="Chat" i] a[href*="/t/"], '
               '[role="navigation"][aria-label*="Chat" i] a[href*="/t/"]')


def thread_row_selector(handle: str) -> str:
    """One thread's row in the chat list, by handle.

    Scoped to the grid for the same reason THREAD_LINK is: the rail carries
    `/t/` links too, and clicking one of those opens a SECTION of the app
    rather than a conversation.
    """
    return f'[role="grid"][aria-label*="Chat" i] a[href*="/t/{handle}"]'
# The rail prefixes, for the walk's own filter. A real thread is `/t/<id>` or
# `/e2ee/t/<id>`; anything else in front of `/t/` is a section of the app.
RAIL_PREFIXES = ("/marketplace/", "/requests/", "/archived/", "/groups/", "/calls/", "/people/")
THREAD_ROW = '[role="row"], [role="listitem"], [role="gridcell"]'

# The composer, and what sends it. Messenger's message box is a contenteditable
# with a role, never an <input>.
# Every textbox on the page that is NOT the message box. The "To:" line on the
# New message screen and the chat search are both contenteditable
# `[role="textbox"]` - the same shape as the composer - and the widest branch of
# COMPOSER used to match them.
#
# Why this cannot be fixed by ordering the selector: `Locator.first` resolves in
# DOM order, not selector order, so listing the labelled composer branch first
# changes nothing. On the New message screen the recipient line comes first in
# the document and wins, `open_chat` reports a composer that is really the "To:"
# field, and `write_dm` types the draft into the recipient line. The exclusion
# has to be in the query itself.
NOT_THE_COMPOSER = (
    ':not([role="combobox"])'
    ':not([aria-label*="search" i])'
    ':not([aria-label*="recipient" i])'
    ':not([aria-label^="To" i])'
)

# SURVEYED against a signed-in session on 2026-09-06. The message box is:
#     <div role="textbox" contenteditable="true" aria-label="Write to <Name>">
# "Write to", not "Message" - which is why every `aria-label*="message"` branch
# found nothing and the composer looked absent. The name is in the label, so the
# match has to be on the PREFIX, never the whole string.
#
# INSPECTED on a stuck run, 2026-09-08. The box the operator can type in
# contains this, and nothing else, when it is empty:
#     <p class="xat24cr xdj266r" dir="auto"><br data-lexical-managed-linebreak="true"></p>
# Two things follow from it, and they are why the branches below were added.
#
# The composer is a LEXICAL editor. `data-lexical-editor="true"` sits on the
# editable root that `<p>` lives in, and `data-lexical-managed-linebreak` on the
# break inside it. Those attributes are the framework's own, so they survive the
# builds that rename everything else - unlike `xat24cr`, which is generated per
# build and means nothing tomorrow.
#
# The aria-label cannot be relied on alone. The 2026-09-06 survey found "Write
# to <Name>"; this build renders a box the earlier branches did not match at
# all, which is exactly how a tab with a perfectly good composer in it came to
# be reported as having none - and then went looking for a New message screen.
COMPOSER = ('div[role="textbox"][contenteditable="true"][aria-label^="Write to" i], '
            'div[role="textbox"][contenteditable="true"][aria-label*="message" i], '
            'div[contenteditable="true"][aria-label*="message" i], '
            'div[role="textbox"][aria-label*="message" i], '
            f'[role="main"] [contenteditable="true"][data-lexical-editor="true"]{NOT_THE_COMPOSER}, '
            f'[contenteditable="true"][data-lexical-editor="true"][role="textbox"]{NOT_THE_COMPOSER}, '
            # DESCENDANT, not child. The first go at this asked for
            # `:has(> p > br[...])`, which requires the inspected paragraph to
            # be a direct child of the editable root - true of a bare Lexical
            # editor, and false the moment Messenger wraps its content in one
            # more div. A composer that is one wrapper deep is still a composer.
            f'[contenteditable="true"]:has(p br[data-lexical-managed-linebreak]){NOT_THE_COMPOSER}, '
            f'[role="main"] [role="textbox"][contenteditable="true"]{NOT_THE_COMPOSER}, '
            # The widest branch, and the one that does not care what this build
            # calls anything: an editable region inside the conversation that is
            # not a search box and not a recipient line. No role, no framework
            # attribute, no label. Everything above it is an attempt to be
            # SPECIFIC; this is the one that keeps working when they all rot,
            # and `composer_box` taking the last match is what makes it safe.
            f'[role="main"] [contenteditable="true"]{NOT_THE_COMPOSER}')
# EXACT labels only, and that is deliberate. The composer sits in a row of
# buttons that a `*=` match would happily hit: surveyed on a live thread, the
# visible ones include "Send a Like" (fires a thumbs-up immediately, with no
# draft and no undo) and "Send a voice clip". A loosened selector here does not
# fail a run - it sends the wrong thing to every recipient in it.
#
# Messenger renders the real Send button only once the composer has text, so
# not matching is NORMAL on an empty box; `write_dm` falls back to Enter, which
# is what Messenger sends on anyway.
SEND_BUTTON = ('[aria-label="Send" i], '
               '[role="button"][aria-label*="Press enter to send" i], '
               '[aria-label="Send message" i]')
# -- What stands between a fresh tab and its composer --------------
#
# REPORTED from a live run, and neither of these is a failure: a tab that has
# just opened a thread often comes up with something in front of the message
# box, and the two things that do it need OPPOSITE treatment.
#
#   A "Continue" wall. One button, and behind it the composer. The run used to
#   sit and poll at this until the deadline ran out, then call the recipient
#   unreachable - while the wall it needed to press was on screen the whole time.
#
#   The New message screen. Messenger answers some handles by opening the
#   composer's cousin: a "To:" line with a search under it, waiting for a name.
#   That line is a `[role="textbox"]` exactly like the composer, which is why
#   NOT_THE_COMPOSER excludes it - typing a DRAFT into it puts the message in a
#   recipient box, and the first Enter would send it to whoever it had guessed
#   at. So it is excluded from the composer AND driven deliberately here: the
#   HANDLE goes in, a suggestion is picked, and that is what turns the screen
#   into a real thread with a real message box.
CONTINUE_WALL = ('[role="dialog"] [role="button"][aria-label="Continue" i], '
                 '[role="button"][aria-label="Continue" i], '
                 '[aria-label="Continue" i]')

# Messenger keeps its file input hidden behind an "Attach a file" button; the
# input itself is what `set_input_files` needs, so it is addressed directly.
FILE_INPUT = 'input[type="file"]'
ATTACHED = ('[aria-label*="Remove" i][role="button"], '
            '[role="img"][aria-label*="attachment" i], '
            'img[src^="blob:"]')

# What "this account cannot be messaged" looks like. Messenger does not error;
# it simply renders the thread without a composer, or with a wall in place of
# one, which is why this is checked as TEXT rather than trusted to raise.
UNREACHABLE_TEXT = re.compile(
    r"(can't reply to this conversation|you can't message this account|"
    r"this person isn't available|content isn't available|"
    r"message request|unavailable right now)",
    re.I,
)

# A page that has stopped being the page we asked for. Checked before a sweep is
# allowed to report an empty chat list as a fact.
BLOCKED_TEXT = re.compile(
    r"(something went wrong|try again later|temporarily blocked|"
    r"you're temporarily blocked|we limit how often|log in to continue)",
    re.I,
)

# Messenger is end-to-end encrypted, and the keys live on the DEVICE. A profile
# that is new - or was wiped - is a new device, so a perfectly good login lands
# on a PIN prompt with an empty chat list behind it. That state used to read as
# "not signed in", which sent the operator off to fix a login that was never
# broken, so it is named separately and reported as itself.
RESTORE_TEXT = re.compile(
    r"(restore your chats|restore chats|secure storage|enter your pin|"
    r"create a pin|your messages are end-to-end encrypted|"
    r"unlock your chats|restore from backup)",
    re.I,
)

# What is covering the page. Messenger's dialogs are modal and trap the rail
# behind them exactly as X's do.
OVERLAY = '[role="dialog"], [aria-modal="true"]'
CLOSE_BUTTONS = (
    '[aria-label="Close" i]',
    '[aria-label="Dismiss" i]',
    '[aria-label="Back" i]',
    '[role="dialog"] [role="button"][aria-label*="close" i]',
)


# -- URLs, from a handle ------------------------------------------
def handle_of(target: str) -> str:
    """The handle inside whatever was typed into the target box.

    Every spelling of one person comes out the same: a bare username, an @ in
    front of it, a bare numeric id, a facebook.com profile URL, a messenger.com
    thread URL. A numeric id survives this unchanged, which is the point: plenty
    of Facebook accounts have no vanity username at all, and for those the
    profile page shows `profile.php?id=<number>` or `/people/<Name>/<number>/`
    and nothing else. `messenger.com/t/<number>` opens the same thread that
    `messenger.com/t/<username>` does, so the number is the handle.

    The id-style URLs come with baggage a person does not think to strip -
    `&mibextid=...`, `?sk=about`, a trailing slash - and every one of those
    used to end up inside the handle, which made the thread address a page that
    did not exist.
    """
    raw = (target or "").strip()
    # profile.php?id=<number>, on any facebook host, with anything after it.
    match = re.search(r"profile\.php\?(?:[^#\s]*&)?id=(\d+)", raw)
    if match:
        return match.group(1)
    # /people/<Name>/<number>/ - what a profile without a username is linked
    # as from search results and from the mobile app's share sheet.
    match = re.search(r"facebook\.com/people/[^/?#\s]+/(\d+)", raw)
    if match:
        return match.group(1)
    # A thread on either host: /t/<x> and /e2ee/t/<x> on messenger.com,
    # /messages/t/<x> and /messages/e2ee/t/<x> on facebook.com.
    match = re.search(r"(?:messenger|facebook)\.com/(?:messages/)?(?:e2ee/)?t/([^/?#\s]+)", raw)
    if match:
        return match.group(1)
    # m.me/<handle>, the short link Messenger hands out for a profile.
    match = re.search(r"(?:^|/)m\.me/([^/?#\s]+)", raw)
    if match:
        return match.group(1)
    # A profile URL on any facebook host, vanity username or bare number.
    match = re.search(r"(?:facebook|fb)\.com/([^/?#\s]+)", raw)
    if match:
        return match.group(1)
    # `id=<number>` on its own, which is what survives a hasty copy of the
    # address bar.
    match = re.fullmatch(r"(?:id=)?(\d+)", raw)
    if match:
        return match.group(1)
    return raw.lstrip("@").split("/")[0].split("?")[0]


def profile_url(handle: str, template: str = PROFILE_TEMPLATE) -> str:
    return template.replace("{handle}", handle.lstrip("@"))


def thread_url(handle: str) -> str:
    return THREAD_TEMPLATE.replace("{handle}", handle.lstrip("@"))


# The second way in. messenger.com/t/<handle> is the direct route and is what a
# run tries first, but it answers a username it does not recognise by bouncing
# the tab to the inbox - silently, with no error and no wall to read. Facebook's
# own copy of the same inbox resolves the SAME handle through the profile
# lookup, so a handle that bounces off messenger.com is worth one go here before
# the run calls the recipient unreachable.
FB_THREAD_TEMPLATE = "https://www.facebook.com/messages/t/{handle}"


def fb_thread_url(handle: str) -> str:
    return FB_THREAD_TEMPLATE.replace("{handle}", handle.lstrip("@"))


def on_a_thread(url: str) -> bool:
    """Is this address a conversation? `/t/<x>` or `/e2ee/t/<x>`, on either host."""
    return "/t/" in (url or "")


def on_new_message(url: str) -> bool:
    """Is this the New message screen?

    OBSERVED, 2026-09-08: `messenger.com/t/<handle>` for a handle Messenger will
    not resolve does not land on the inbox - it lands on `messenger.com/new`,
    and the facebook.com route lands on `facebook.com/messages/new`. The run
    read both as a bounce and gave up on the recipient, when what was on screen
    was a "To:" line waiting for a name.

    Read off the URL rather than off a selector, and that is the point of it
    being here at all: the address is the one piece of evidence about this
    screen that no build can rename. The selector for the To: line was a guess
    and was wrong; `/new` was in the log the whole time.
    """
    return (url or "").split("?")[0].rstrip("/").endswith("/new")


# -- Is this page still the page we asked for ----------------------
def challenge_showing(page, *, quick: bool = False) -> Optional[str]:
    """A checkpoint, a two-factor prompt, or a bounce back to the login wall.

    Named rather than merely detected: "Facebook wants a code" and "the session
    dropped" are different mornings, and the run bar is the only place the
    operator reads it.

    `quick` skips the body read and checks the URL only. MEASURED cost, not a
    guess at one: reading `body.inner_text` extracts the text of the whole
    application, and the pooled run called this - and two more like it - on
    every tab on every poll. Five tabs share one thread, so three full-page
    extractions each turned a quarter-second poll into seconds, and a Continue
    button that appeared instantly was not pressed for eight of them.

    The URL half catches everything that MOVES the page, which is what a
    checkpoint, a login bounce and a two-factor prompt all do. Only the
    rate-limit banner needs the text, and that one is not urgent: it is caught
    on the slower paths - opening a chat, typing, sending - which run once per
    recipient rather than four times a second.
    """
    try:
        url = page.url or ""
        if "/checkpoint" in url:
            return "the account is checkpointed - Facebook wants it cleared in a browser"
        if "/login" in url or "/recover" in url:
            return "the session dropped back to the login wall"
        if "two_factor" in url or "/authenticate" in url:
            return "Facebook is asking for a two-factor code"
        if quick:
            return None
        body = (page.locator("body").inner_text(timeout=2000) or "")[:4000]
        if BLOCKED_TEXT.search(body):
            return "Facebook is rate-limiting or blocking this session"
    except Exception:                                # noqa: BLE001 - a page mid-navigation
        return None
    return None


def guard(page, when: str, *, quick: bool = False) -> None:
    """Raise if the page has stopped being the page we asked for.

    `quick` is for the poll loop - see `challenge_showing`. Everything that
    acts on a page keeps the full check.
    """
    found = challenge_showing(page, quick=quick)
    if found:
        raise AutomationError(f"Stopped {when}: {found}.")


# -- Getting out of whatever just opened --------------------------
def overlay_showing(page) -> bool:
    try:
        return page.locator(OVERLAY).count() > 0
    except Exception:                                # noqa: BLE001
        return False


def close_overlay(page, *, tries: int = 3) -> bool:
    """Dismiss whatever is covering the page. Returns whether anything closed.

    Escape first, because Messenger's dialogs all honour it and it cannot click
    the wrong thing. The labelled buttons are the fallback for the ones that do
    not - a media viewer, mostly.
    """
    closed = False
    for _ in range(tries):
        if not overlay_showing(page):
            return closed
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:                            # noqa: BLE001
            pass
        if not overlay_showing(page):
            return True
        for selector in CLOSE_BUTTONS:
            try:
                button = page.locator(selector).first
                if button.count() > 0:
                    button.click(timeout=2500)
                    closed = True
                    page.wait_for_timeout(400)
                    break
            except Exception:                        # noqa: BLE001 - try the next shape
                pass
    return closed or not overlay_showing(page)


# -- Who is signed in ---------------------------------------------
# Messenger does not put the signed-in account's username anywhere in the DOM -
# there is no equivalent of X's `SideNav_AccountSwitcher_Button` carrying an
# @handle. What it does carry is the account's NAME, on the settings/profile
# control at the top of the rail, and that is the most a card can honestly show.
WHOAMI_JS = r"""
() => {
  const pick = sel => {
    const el = document.querySelector(sel);
    if (!el) return '';
    const label = el.getAttribute('aria-label') || '';
    return (label || el.innerText || '').trim();
  };
  // Most specific first. The profile control names the account; the generic
  // "Settings" one does not, so it is only read for the name it sometimes
  // carries after a comma ("Settings, help and more" gives nothing, and an
  // empty string here is the correct answer).
  let name = pick('[role="navigation"] [aria-label*="profile" i]')
          || pick('[aria-label*="Your profile" i]')
          || pick('[role="banner"] [role="button"][aria-label]');
  // Messenger writes this label three ways round and the name is the only
  // part worth keeping: "Your profile, Jane Doe", "Jane Doe, profile",
  // "Profile: Jane Doe" all have to come out as "Jane Doe".
  name = name.replace(/^(your\s+)?(profile|account)\s*[:,-]\s*/i, '')
             .replace(/\s*[,-]\s*(your\s+)?(profile|account|settings).*$/i, '')
             .trim();
  // The last selector above is a generic "a labelled button in the banner",
  // which on some builds is the settings menu rather than the account. Its
  // label is not a name, and a card showing "Settings, help and more" where a
  // person's name goes is worse than a card showing nothing.
  if (/^(settings|help|menu|notifications)\b/i.test(name)) return '';
  return name;
}
"""


# -- Waiting for the app, rather than for the clock ----------------
# Every flat `wait_for_timeout` in this file is a guess about how long Messenger
# takes to paint, and every one of them is wrong on a cold profile: `goto`
# returns at domcontentloaded, which on a React app is the empty shell, so a
# guess that expires early hands the caller a page with nothing in it and the
# caller calls that "signed out". This waits for the page to become one of the
# things it can actually BE instead.
_READY_JS = r"""
() => {
  const shell = !!document.querySelector('%s');
  const wall  = !!document.querySelector('input[name="pass"], input[type="password"]');
  const gate  = /login|checkpoint|two_factor|authenticate|recover/.test(location.pathname);
  const rows  = document.querySelectorAll('a[href*="/t/"]').length;
  const modal = !!document.querySelector('[role="dialog"], [aria-modal="true"]');
  // A wall or a gate is a final answer on its own. A signed-in shell is only an
  // answer once something is IN it - a thread, or a dialog sitting over it -
  // because the shell paints long before the chat list arrives, and the gap
  // between the two is exactly where the old flat waits landed.
  return wall || gate || (shell && (rows > 0 || modal));
}
""" % SIGNED_IN_ANY


def _poll(page, js: str, *, timeout: int, interval: float = 0.25) -> bool:
    """Poll a JS predicate with `evaluate`. Returns whether it ever came true.

    Why this exists instead of `page.wait_for_function`, which is the obvious
    tool and is used nowhere in this file:

        Playwright implements `wait_for_function` by evaluating a STRING as
        JavaScript in the page. Messenger serves
            script-src *.facebook.com *.fbcdn.net ... (no 'unsafe-eval')
        so that string evaluation is refused by the Content Security Policy and
        the call fails - in about a tenth of a second, with EvalError, NOT with
        a timeout. Every caller read that as "the page never settled", which is
        indistinguishable from "signed out" unless you know to look.

    `page.evaluate` reaches the page through a different CDP path and is not
    affected, so the waiting is done on this side instead. An exception inside
    the loop is a navigation destroying the context mid-poll - expected on a
    site that redirects "/" to the last thread - and is looked past rather than
    raised.
    """
    deadline = time.monotonic() + timeout / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.evaluate(js):
                return True
        except Exception:                            # noqa: BLE001 - mid-navigation
            pass
        try:
            page.wait_for_timeout(int(interval * 1000))
        except Exception:                            # noqa: BLE001 - page went away
            return False
    return False


def wait_ready(page, *, timeout: int = 45_000) -> dict[str, Any]:
    """Block until the page is definitively something, and report which.

    `settled` false means "no answer inside the timeout" - NOT "signed out".
    Every caller has to keep those apart: the second is worth writing to a card,
    and the first is worth retrying.
    """
    settled = _poll(page, _READY_JS, timeout=timeout)

    state: dict[str, Any] = {"settled": settled, "threads": 0, "restore": False}
    try:
        state["threads"] = page.locator(THREAD_LINK).count()
        body = (page.locator("body").inner_text(timeout=2000) or "")[:4000]
        state["restore"] = bool(RESTORE_TEXT.search(body))
    except Exception:                                # noqa: BLE001 - mid-navigation
        pass
    state["signed_in"] = _signed_in(page)
    return state


def wait_for_chats(page, *, timeout: int = 45_000, quiet: float = 1.4) -> int:
    """Wait for the chat list to appear AND stop growing. Returns the count.

    Two conditions, not one. The first link appearing is not the list being
    ready: Messenger streams the rest in behind it, and a walk that started at
    the first row would scroll a list that was still filling underneath it and
    call the result the whole inbox. So the count has to hold still for `quiet`
    seconds before this returns.

    Zero is a legitimate answer - a real account with no conversations, or a new
    device that has not restored its chats yet - so it is RETURNED rather than
    raised on. `wait_ready` is what tells those two apart.
    """
    deadline = time.monotonic() + timeout / 1000.0
    seen, steady_since = -1, 0.0

    while time.monotonic() < deadline:
        try:
            count = page.locator(THREAD_LINK).count()
        except Exception:                            # noqa: BLE001 - mid-navigation
            count = seen

        if count != seen:
            seen, steady_since = count, time.monotonic()
        elif count > 0 and time.monotonic() - steady_since >= quiet:
            return count

        page.wait_for_timeout(250)

    return max(seen, 0)


def _signed_in(page) -> bool:
    try:
        return page.locator(SIGNED_IN_ANY).count() > 0
    except Exception:                                # noqa: BLE001
        return False


def whoami(page) -> dict[str, Any]:
    """Whose session is this - if anyone's, and under which name.

    Read off the page rather than off a cookie: a cookie jar can hold a token
    Facebook has already invalidated, and the difference only shows up as a
    redirect to the login wall halfway through a run.
    """
    try:
        goto(page, HOME_URL)
        # Was a flat 1.8s. On a cold profile that expired while the shell was
        # still painting, and an empty shell reads as signed out - which then
        # gets WRITTEN to the card and clears a session that was fine.
        state = wait_ready(page)
        url = page.url or ""
        if "/login" in url or "/checkpoint" in url:
            return {"logged_in": False, "handle": None}
        if not state["signed_in"]:
            return {"logged_in": False, "handle": None}
        return {"logged_in": True, "handle": (page.evaluate(WHOAMI_JS) or "") or None}
    except Exception:                                # noqa: BLE001 - offline counts as no
        return {"logged_in": False, "handle": None}


def is_logged_in(page) -> bool:
    return bool(whoami(page)["logged_in"])


def peek(page) -> dict[str, Any]:
    """Who this window is signed in as, WITHOUT navigating it anywhere.

    `whoami` finds out by going to messenger.com, which is right when nobody is
    looking at the window and destructive when somebody is: the sign-in watcher
    polls every few seconds, and a person typing a password into a form that
    keeps being navigated out from under them cannot finish. So this reads
    whatever is on screen already and says "not yet" for anything that is not a
    signed-in Messenger page - the login wall, a checkpoint, a blank tab.
    """
    try:
        url = page.url or ""
        if not ("messenger.com" in url or "facebook.com" in url):
            return {"logged_in": False, "handle": None}
        if "/login" in url or "/checkpoint" in url or "/recover" in url:
            return {"logged_in": False, "handle": None}
        if not _signed_in(page):
            return {"logged_in": False, "handle": None}
        return {"logged_in": True, "handle": (page.evaluate(WHOAMI_JS) or "") or None}
    except Exception:                                # noqa: BLE001 - mid-navigation counts as no
        return {"logged_in": False, "handle": None}


# Either a signed-in shell or the login wall. One of the two is what "the page
# has finished deciding" looks like.
_SETTLED_JS = r"""
() => !!document.querySelector('%s')
      || !!document.querySelector('input[name="pass"], input[type="password"]')
      || /\/login|\/checkpoint/.test(location.pathname)
""" % SIGNED_IN_ANY


def whoami_when_settled(page, timeout: int = 20_000) -> dict[str, Any]:
    """`whoami`, but it waits for the page to become one thing or the other.

    `whoami` navigates and then waits a flat 1.8 seconds. On a warm profile that
    is plenty; on a cold one - a window opened seconds ago, a slow morning - the
    shell is still painting and has no chat grid in it yet, which reads as
    signed out.

    That answer is not merely wrong, it gets WRITTEN: the caller records it
    against the card, and a saved session is cleared because the network was
    slow. So this waits for a definite answer instead, and says which kind it
    got. `settled` false means "no answer", not "signed out", and a caller must
    leave the stored session alone when it sees one.
    """
    settled = False
    try:
        goto(page, HOME_URL)
        # Was `wait_for_function`, which cannot run here - see `_poll`.
        settled = _poll(page, _SETTLED_JS, timeout=timeout)
    except Exception:                                # noqa: BLE001 - reported as unsettled
        pass
    found = peek(page)
    found["settled"] = settled
    return found


# -- Messenger's own navigation -----------------------------------
# The rail down the left of messenger.com, keyed the way the Actions panel
# addresses it. Each entry is a click target FIRST and a URL second: the panel's
# point is pressing the real item, so "New message" opens the composer exactly
# as a hand on the mouse would, and the URL is the fallback for a window not
# sitting on a page that carries the rail at all.
NAV_ACTIONS: dict[str, dict[str, Any]] = {
    "chats":     {"label": "Chats",       "aria": "Chats",        "url": "https://www.messenger.com/"},
    "new":       {"label": "New message", "aria": "New message",  "url": None},
    "requests":  {"label": "Requests",    "aria": "Message requests",
                  "url": "https://www.messenger.com/requests/"},
    "archive":   {"label": "Archive",     "aria": "Archived chats",
                  "url": "https://www.messenger.com/archived/"},
    "groups":    {"label": "Groups",      "aria": "Groups",       "url": "https://www.messenger.com/groups/"},
    "calls":     {"label": "Calls",       "aria": "Calls",        "url": "https://www.messenger.com/calls/"},
    "people":    {"label": "People",      "aria": "People",       "url": "https://www.messenger.com/people/"},
    "marketplace": {"label": "Marketplace", "aria": "Marketplace",
                    "url": "https://www.messenger.com/marketplace/"},
    # `closes` says this item opens something that covers the page, so `All`
    # clears it before pressing the next one. Marked here rather than
    # special-cased in the caller.
    "settings":  {"label": "Settings",    "aria": "Settings",     "url": None, "closes": True},
    "profile":   {"label": "Profile",     "aria": "Your profile", "url": "https://www.facebook.com/me",
                  "closes": True},
    "notifications": {"label": "Notifications", "aria": "Notifications", "url": None, "closes": True},
    "facebook":  {"label": "Facebook",    "aria": "Facebook",     "url": "https://www.facebook.com/"},
    # The two at the bottom of the rail that TOGGLE rather than navigate, so a
    # press is two clicks a second apart: open the menu and close it again,
    # collapse the sidebar and expand it again. `twice` says so. The menu's label
    # is "<Name> Settings, help and more", so it is matched by its tail; the
    # sidebar's reads "Collapse inbox sidebar" or "Expand inbox sidebar"
    # depending on where it is, so it is matched by the part that does not change.
    "menu":      {"label": "Settings menu", "aria": "Settings, help and more", "url": None,
                  "selectors": ['[aria-controls="mw-inbox-settings-menu"]',
                                '[role="button"][aria-haspopup="menu"][aria-label*="Settings" i]'],
                  "twice": True},
    "sidebar":   {"label": "Inbox sidebar", "aria": "inbox sidebar", "url": None,
                  "twice": True},
}


def _settle(page, entry: dict[str, Any], result: str) -> str:
    """Close what this item opened, if it opens something."""
    if entry.get("closes"):
        page.wait_for_timeout(700)
        close_overlay(page)
    return result


def click_action(page, action: str) -> str:
    """Press one item in Messenger's left-hand navigation.

    The real element, clicked - not merely a navigation to its URL - so "New
    message" opens the composer the way a click does. Returns "clicked" when the
    element was pressed and "opened" when it fell back to the item's URL.
    """
    # The one key that is not a menu item: close whatever is covering the page.
    # It lives here rather than in NAV_ACTIONS because it is not on the rail -
    # it is how you get back TO the rail.
    if action == "close":
        return "closed" if close_overlay(page) else "nothing open"

    entry = NAV_ACTIONS.get(action)
    if entry is None:
        raise AutomationError(f"{action} is not a menu item this can press.")

    # The rail only exists on a signed-in messenger.com page. A window sitting
    # on a login wall, a Facebook profile or a blank tab may not carry it, so it
    # is sent home first and the click made from there.
    if "messenger.com" not in (page.url or ""):
        goto(page, HOME_URL)
        page.wait_for_timeout(1500)

    # Clear the way. A dialog left open by the LAST press covers the rail, and
    # every click after it fails on an element that is there but not reachable -
    # the failure that reads as "the Actions panel stopped working".
    close_overlay(page)

    label = entry["label"]
    # An item's own handles first, when it has ones steadier than a label.
    selectors = list(entry.get("selectors") or [])
    if entry.get("aria"):
        selectors.append(f'[aria-label="{entry["aria"]}" i]')
        selectors.append(f'[role="button"][aria-label*="{entry["aria"]}" i]')
        selectors.append(f'[role="link"][aria-label*="{entry["aria"]}" i]')
    # The visible word as a last on-page handle. It fails loudly and legibly
    # rather than silently matching nothing.
    selectors.append(f'[role="navigation"] [role="link"]:has-text("{label}")')

    for selector in selectors:
        try:
            found = page.locator(selector).first
            if found.count() > 0:
                found.click(timeout=8000)
                if entry.get("twice"):
                    # A toggle: pressed, left a second, pressed again. Looked up
                    # afresh rather than reused, because the sidebar button is a
                    # different element once the sidebar has moved.
                    page.wait_for_timeout(1000)
                    page.locator(selector).first.click(timeout=8000)
                page.wait_for_timeout(1200)
                guard(page, f"opening {label}")
                return _settle(page, entry, "clicked twice" if entry.get("twice") else "clicked")
        except Exception:                            # noqa: BLE001 - fall through to the URL
            pass

    url = entry.get("url")
    if url:
        goto(page, url)
        page.wait_for_timeout(1000)
        return _settle(page, entry, "opened")

    raise AutomationError(f"Could not find the {label} button on the page.")


# -- Logging in ---------------------------------------------------
def login(page, username: str, password: str, email: str = "") -> None:
    """The password flow, up to the point a human has to take over.

    Everything Facebook can ask for that is on the account - the address, the
    password - is answered here. A code sent to a phone or an authenticator is
    not: nothing in this repo reads mail or SMS, and a wrong code is how a
    checkpoint escalates into a lock. The run stops and says so, with the window
    left open on the prompt for the operator to finish by hand.

    `username` is the login identifier, which for Facebook is usually the email
    address; `email` is the address on the card and is used when the login field
    is the one that is empty. Either way one of the two reaches the form.
    """
    identifier = (username or email or "").strip()
    if not identifier:
        raise AutomationError("No login address on this account to sign in with.")

    goto(page, LOGIN_URL)
    page.wait_for_timeout(1500)

    try:
        page.wait_for_selector(LOGIN_EMAIL, timeout=20_000)
    except Exception as err:                         # noqa: BLE001
        raise AutomationError("Messenger did not show a login form.") from err

    _type_human(page, LOGIN_EMAIL, identifier)
    page.wait_for_timeout(random.uniform(300, 900))
    _type_human(page, LOGIN_PASSWORD, password)
    page.wait_for_timeout(random.uniform(400, 1100))

    submit = page.locator(LOGIN_SUBMIT).first
    if submit.count() == 0:
        raise AutomationError("Messenger did not show a log-in button.")
    submit.click()
    page.wait_for_timeout(5000)

    found = challenge_showing(page)
    if found:
        raise AutomationError(
            f"{found}. The window is open on it - finish there, and the session saves itself.")
    if not _signed_in(page):
        raise AutomationError(
            "The sign-in did not complete. The window is open where it stopped.")


# -- Walking the chat list ----------------------------------------
# One evaluate per pass rather than a Playwright query per row: the list is
# virtualised, and forty round trips across the CDP bridge take longer than the
# list takes to recycle the nodes underneath them.
LIST_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  // Same scope as THREAD_LINK, and for the same reason: the rail carries
  // /marketplace/t/<id>, /requests/t/<id> and /archived/t/<id> for the SAME
  // conversation, so an unscoped sweep reports one thread as five people.
  const list = document.querySelector(
      '[role="grid"][aria-label*="Chat" i]') ||
      document.querySelector('[role="navigation"][aria-label*="Chat" i]') ||
      document;
  const RAIL = /^\/(marketplace|requests|archived|groups|calls|people)\//;
  list.querySelectorAll('a[href*="/t/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (RAIL.test(href)) return;
    const m = href.match(/\/t\/([^/?#]+)/);
    if (!m) return;
    const id = decodeURIComponent(m[1]);
    if (!id || seen.has(id)) return;
    seen.add(id);
    // The row's accessible name is the conversation; its first line is the
    // person. The aria-label is preferred because innerText also carries the
    // message preview and the timestamp, which are not a name.
    const row = a.closest('[role="row"], [role="listitem"], [role="gridcell"]') || a;
    const label = (a.getAttribute('aria-label') || row.getAttribute('aria-label') || '').trim();
    const text = (row.innerText || '').trim();
    const name = (label || text).split('\n')[0].trim();
    // A group thread is not a person and cannot be messaged as one. Messenger
    // names them after their members, which is why the comma is the tell.
    const group = /(^|\s)(and \d+ others?|, )/i.test(label || '') && !/^[^,]+$/.test(name);
    out.push({ handle: id, name, group });
  });
  return out;
}
"""

# The scrollable ancestor of the chat list. Messenger scrolls a div, not the
# window, so `window.scrollBy` moves nothing at all - which is the whole reason
# this is found rather than assumed.
SCROLLER_JS = r"""
() => {
  const first = document.querySelector('a[href*="/t/"]');
  if (!first) return false;
  let node = first.parentElement;
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    const scrolls = /(auto|scroll)/.test(style.overflowY);
    if (scrolls && node.scrollHeight > node.clientHeight + 40) {
      node.setAttribute('data-xbot-scroller', '1');
      return true;
    }
    node = node.parentElement;
  }
  return false;
}
"""

SCROLL_BY_JS = r"""
(distance) => {
  const el = document.querySelector('[data-xbot-scroller]');
  const target = el || document.scrollingElement || document.body;
  const before = target.scrollTop;
  target.scrollTop = before + distance;
  return target.scrollTop - before;
}
"""

TO_TOP_JS = r"""
() => {
  const el = document.querySelector('[data-xbot-scroller]');
  const target = el || document.scrollingElement || document.body;
  target.scrollTop = 0;
}
"""


def walk_home(page, *,
              pause: tuple[float, float] = (0.6, 2.4),
              limit: int = 0,
              refresh_after: int = 8,
              stall_seconds: float = 90.0,
              on_rows: Optional[Callable[[list[dict]], None]] = None,
              on_reload: Optional[Callable[[str], None]] = None,
              checkpoint: Optional[Callable[[float], None]] = None) -> list[dict]:
    """Scroll the signed-in account's chat list and read everyone on it.

    The X version of this walks a home timeline, which is a feed of strangers
    and has no end. Messenger has no such surface - there is no public follower
    list and no feed of accounts you do not know - so the sweep collects the
    conversations this account already HAS. That is a smaller, finite thing, and
    the walk reflects it: when several passes turn up nothing new the list is
    genuinely exhausted, and rather than spin at the bottom it goes back to the
    top and settles, so a long-running sweep picks up threads as they arrive.

    Group threads are read and marked rather than skipped. A group is not a
    person and cannot be DMed as one, but a roster that silently dropped them
    would look like it had missed accounts.

    `limit` is a ceiling for a caller that wants one - zero, the default, means
    there is none. `on_rows` is called with everything collected SO FAR, which
    is what puts the table on screen while the walk is still running.
    """
    goto(page, HOME_URL, timeout=60_000)
    state = wait_ready(page, timeout=60_000)
    guard(page, "walking the chat list")

    # An empty inbox has three causes and they need different answers from the
    # operator, so they are told apart here rather than all reported as the
    # chat list never appearing.
    if state["restore"]:
        raise AutomationError(
            "Messenger is asking to restore this account's chats - this profile is a "
            "new device, and the history is end-to-end encrypted. Enter the PIN in "
            "the window; the sweep can only see threads once they have restored.")
    if not state["signed_in"]:
        raise AutomationError(
            "The chat list never appeared. The window may not be signed in.")

    # The list exists; now let it finish arriving. Starting the scroll while
    # Messenger is still streaming rows in walks a list that is growing
    # underneath the walker, which reads as a short inbox rather than a slow one.
    if wait_for_chats(page, timeout=45_000) == 0 and not state["restore"]:
        log("messenger", "signed in, but the chat list came back empty", "warn")

    page.evaluate(SCROLLER_JS)

    rows: dict[str, dict] = {}
    barren = 0
    last_new = time.monotonic()

    while True:
        if checkpoint:
            checkpoint(0)

        # The watchdog. Going back to the top (below) is for a list that has
        # simply been read to the end; this is for a page that has stopped
        # answering at all - a list that will not scroll, a Messenger that
        # dropped its connection - and the fix for that is a fresh page load.
        quiet = time.monotonic() - last_new
        if stall_seconds and quiet > stall_seconds:
            why = f"no new conversation for {int(quiet)}s"
            log("messenger", f"{why} - reloading the chat list", "warn")
            if on_reload:
                on_reload(why)
            goto(page, HOME_URL, timeout=60_000)
            wait_ready(page, timeout=60_000)
            guard(page, "walking the chat list")
            wait_for_chats(page, timeout=45_000)
            page.evaluate(SCROLLER_JS)
            barren = 0
            last_new = time.monotonic()

        before = len(rows)
        for found in (page.evaluate(LIST_JS) or []):
            handle = str(found.get("handle") or "").strip()
            if not handle or handle in rows:
                continue
            rows[handle] = {
                "handle": handle,
                "name": str(found.get("name") or "").strip(),
                "bio": "",
                "group": bool(found.get("group")),
            }
            if limit and len(rows) >= limit:
                break

        if len(rows) > before:
            last_new = time.monotonic()
            if on_rows:
                on_rows(list(rows.values()))

        if limit and len(rows) >= limit:
            break

        barren = 0 if len(rows) > before else barren + 1
        if barren >= refresh_after:
            # The list is exhausted for now. Back to the top for whatever has
            # arrived since, rather than spinning at the bottom of a dead page.
            page.evaluate(TO_TOP_JS)
            page.wait_for_timeout(1200)
            barren = 0

        started = time.monotonic()
        page.evaluate(SCROLL_BY_JS, random.randint(420, 900))
        spent = time.monotonic() - started

        # Jittered, and the movement is paid for OUT of the pause rather than
        # added to it: a constant gap is a metronome, and a metronome is the
        # easiest thing to spot in a request log.
        wait = max(0.0, random.uniform(*sorted(pause)) - spent)
        if wait:
            if checkpoint:
                checkpoint(wait)
            else:
                time.sleep(wait)
        guard(page, "walking the chat list")

    return list(rows.values())


# -- Reading one profile ------------------------------------------
PROFILE_JS = r"""
() => {
  const text = sel => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || '').trim() : '';
  };
  const body = (document.body.innerText || '');
  return {
    name: text('h1') || text('[role="main"] h2'),
    bio: text('[data-overviewsection] , [role="main"] [dir="auto"] + [dir="auto"]').slice(0, 400),
    followers_text: (body.match(/([\d.,]+[KMB]?)\s*(followers|friends)/i) || [])[0] || '',
    dm_open: !!document.querySelector('[aria-label="Message" i], [role="button"][aria-label*="Message" i]'),
    missing: /(this content isn't available|page isn't available|content not found)/i.test(body),
  };
}
"""


def read_chat_list(page, *, limit: int = 0, settle_passes: int = 3,
                   pause: float = 0.8) -> list[dict[str, Any]]:
    """Every conversation in the chat list, once, and then RETURN.

    The bounded sibling of `walk_home`. That one is a run - it never ends on its
    own, because a sweep is something the operator stops - and so it cannot back
    a button that has to hand an answer to the page that pressed it. This scrolls
    the list until it stops producing anyone new, then returns what it has.

    `settle_passes` is how many scrolls in a row must turn up nobody new before
    the list counts as read. More than one because Messenger's list is
    virtualised and streams rows in behind the scroll: the first pass that adds
    nothing is often just the next batch not having arrived yet.
    """
    goto(page, HOME_URL, timeout=60_000)
    state = wait_ready(page, timeout=60_000)
    guard(page, "reading the chat list")

    if state["restore"]:
        raise AutomationError(
            "Messenger is asking to restore this account's chats - this profile is a "
            "new device and the history is end-to-end encrypted. Enter the PIN in the "
            "window; the chat list is empty until the threads have restored.")
    if not state["signed_in"]:
        raise AutomationError("That window is not signed in to Messenger.")

    wait_for_chats(page, timeout=45_000)
    page.evaluate(SCROLLER_JS)

    found: dict[str, dict[str, Any]] = {}
    quiet = 0
    while quiet < settle_passes:
        for row in page.evaluate(LIST_JS):
            handle = row.get("handle")
            if not handle:
                continue
            if handle in found:
                # A row seen again with a name this time is worth keeping: the
                # list renders the avatar before the label on a cold load.
                if row.get("name") and not found[handle].get("name"):
                    found[handle] = row
                continue
            found[handle] = row
            if limit and len(found) >= limit:
                return list(found.values())

        before = len(found)
        moved = page.evaluate(SCROLL_BY_JS, 900)
        page.wait_for_timeout(int(pause * 1000))
        for row in page.evaluate(LIST_JS):
            if row.get("handle") and row["handle"] not in found:
                found[row["handle"]] = row
        # Nobody new AND nowhere left to scroll is the end of the list. Either
        # on its own is not: a virtualised list can scroll without having
        # rendered the next batch yet, and a short list has nothing to scroll.
        quiet = quiet + 1 if (len(found) == before or not moved) else 0

    page.evaluate(TO_TOP_JS)
    return list(found.values())


def read_profile(page, handle: str, template: str = PROFILE_TEMPLATE) -> dict[str, Any]:
    """Open a profile for the facts a list row does not carry.

    The follower/friend count and whether the account can be messaged. Both are
    worth the extra page load: the count is what the roster is filtered on, and
    a closed inbox discovered only at send time has already spent a slot out of
    the day's budget.

    Unlike the X version this never routes through Firecrawl. Firecrawl reads a
    public page, and a Facebook profile shows almost nothing to a signed-out
    fetch - the name at best - so a Firecrawl read here would return a row that
    looks collected and is empty. Reading it off the window is the only way to
    get an answer worth storing.
    """
    if page is None:
        raise AutomationError("No browser window to read a profile with.")

    goto(page, profile_url(handle, template))
    page.wait_for_timeout(1800)
    guard(page, f"reading {handle}")

    # Facebook and Messenger are separate cookie DOMAINS, and signing in to
    # messenger.com does not sign in to facebook.com - a profile with `c_user`
    # and `xs` on messenger.com can carry only `datr`/`fr`/`sb` on facebook.com,
    # which is signed out. Profiles live on facebook.com, so every read then
    # lands on the login wall and comes back `missing` - indistinguishable, on
    # the row, from an account that has been deleted. It is not a fact about the
    # recipient and must not be written against them as one.
    if page.locator(LOGIN_PASSWORD).count() > 0 or "/login" in (page.url or ""):
        raise AutomationError(
            "This window is signed in to Messenger but NOT to facebook.com, and profiles "
            "live on facebook.com. Open facebook.com in the same window and sign in there; "
            "until then no profile can be read.")

    found = page.evaluate(PROFILE_JS) or {}
    if found.get("missing"):
        return {"handle": handle, "missing": True, "dm_open": False, "protected": False,
                "followers": None, "name": "", "bio": ""}
    return {
        "handle": handle,
        "name": found.get("name") or "",
        "bio": found.get("bio") or "",
        "followers": _count(found.get("followers_text") or ""),
        "dm_open": bool(found.get("dm_open")),
        # Facebook has no "protected" the way X does - a profile is either
        # visible to you or it is not, and an invisible one reads as missing.
        "protected": False,
        "missing": False,
    }


def open_profiles(context, handles: Iterable[str], template: str = PROFILE_TEMPLATE,
                  checkpoint: Optional[Callable[[float], None]] = None) -> int:
    """Open each handle in its own tab, for reading by eye.

    Deliberately not a scrape: this is the operator wanting to look at who is
    about to be messaged before anything is.
    """
    opened = 0
    for handle in handles:
        if checkpoint:
            checkpoint(0)
        page = context.new_page()
        try:
            goto(page, profile_url(handle, template), timeout=30_000)
        except Exception:                            # noqa: BLE001 - a dead handle is not fatal
            log("browser", f"could not open {handle}", "warn")
        opened += 1
        if checkpoint:
            checkpoint(random.uniform(0.4, 1.1))
    return opened


# -- Sending -------------------------------------------------------
class DmClosed(AutomationError):
    """The composer could not be opened at all.

    Its own type because it is the one failure worth naming on the row: it is
    the difference between "Messenger refused" and "this account was never
    reachable".
    """


def click_thread_in_list(page, handle: str, *, timeout: int = 8_000) -> bool:
    """Open a thread by CLICKING its row, not by navigating to it.

    Why this exists. `goto(thread_url(handle))` is a full page load: messenger.com
    is fetched again, the app boots again, and on a profile whose E2EE device is
    not verified that bootstrap is the slow path - seconds per recipient, all of
    it spent rebuilding a page that was already on screen. Clicking the row is
    what a person does, and it is one SPA route change.

    Returns False rather than raising when the row is not on screen. The list is
    VIRTUALISED - it renders a window of rows and recycles the nodes - so a
    handle further down than the walk has scrolled genuinely is not clickable
    yet, and that is a normal thing for the caller to fall back from, not an
    error about the recipient.

    The check after the click is the load-bearing part. A click that landed on
    the wrong row would open the wrong person's thread, and the next thing this
    run does is type a message into it - so the URL has to name the handle that
    was asked for before this can answer True.
    """
    row = page.locator(thread_row_selector(handle)).first
    try:
        if row.count() == 0:
            return False
        row.click(timeout=timeout)
    except Exception:                                # noqa: BLE001 - recycled mid-click
        return False

    # `/t/<handle>` or `/e2ee/t/<handle>` - both are this thread.
    deadline = time.monotonic() + timeout / 1000.0
    while time.monotonic() < deadline:
        if f"/t/{handle}" in (page.url or ""):
            return True
        page.wait_for_timeout(150)
    return False


def open_chat(page, handle: str, *, template: str = PROFILE_TEMPLATE,
              follow_first: bool = False, via_search: bool = False,
              settle: float = 2.0) -> bool:
    """Open the thread with this person, ready to type into.

    Returns True with the composer up, and False when the thread simply has no
    composer in it. That False is a FACT ABOUT THE RECIPIENT, not a failure of
    the run: plenty of accounts do not take messages from strangers, and the
    campaign table exists to show which - so this answers the question rather
    than raising, and the caller writes "no chat" on the row and moves on.

    Only a page that would not load at all raises, because that is the run's
    problem rather than this recipient's and the next one will not fix it.

    `follow_first` and `via_search` are part of the contract the X version set
    and are accepted so the campaign does not have to know which platform it is
    driving. Neither has a Messenger meaning - there is nothing to follow, and
    a thread is addressable by URL so there is no search to go through - so they
    are ignored rather than faked.
    """
    # Click the row if it is on screen; navigate only if it is not.
    #
    # `goto` here is a full page load - messenger.com fetched again, the app
    # booted again - and on a profile whose E2EE device is not verified that
    # bootstrap is seconds per recipient, spent rebuilding a page that was
    # already open. A run of forty spends most of its wall clock there. Clicking
    # the row is one SPA route change, and it is what a person does.
    #
    # The fallback is not a nicety: the list is virtualised, so a handle below
    # what has been scrolled to has no row to click, and one that came off an
    # uploaded file may not be in this account's list at all.
    if not click_thread_in_list(page, handle):
        goto(page, thread_url(handle))

    # Wait for the THREAD to be there, not for a fixed number of milliseconds. A
    # flat sleep is wrong in both directions: too short on the first few handles
    # of a run, when nothing is warm yet, and wasted time on every one after.
    try:
        page.wait_for_selector(f'{COMPOSER}, [role="main"]', timeout=15_000)
    except Exception:                                # noqa: BLE001 - the checks below still run
        pass
    page.wait_for_timeout(int(settle * 300))
    guard(page, f"opening the chat with {handle}")

    # A composer only means "ready to type" if this is actually a THREAD. The
    # selector above excludes the recipient picker by shape; this excludes it by
    # address, which is the part that cannot rot when Messenger rewords an
    # aria-label. A page that is not on /t/ has no message box worth finding,
    # and typing a draft into whatever it does have is the worst failure here.
    #
    # Off a thread means messenger.com moved this tab, which is how it answers a
    # handle it will not resolve. The second route gets the same go the pooled
    # run gives it, for the same reason: the two hosts resolve a handle by
    # different means, and one bouncing is not both refusing.
    if not on_a_thread(page.url or "") and not on_new_message(page.url or ""):
        log("dm", f"{handle}: messenger.com bounced the tab - trying facebook.com", "warn")
        goto(page, fb_thread_url(handle))
        try:
            page.wait_for_selector(f'{COMPOSER}, [role="main"]', timeout=20_000)
        except Exception:                            # noqa: BLE001 - the checks below still run
            pass
        page.wait_for_timeout(int(settle * 300))
        guard(page, f"opening the chat with {handle}")
        if not on_a_thread(page.url or "") and not on_new_message(page.url or ""):
            return False

    # The New message screen, which `/t/<handle>` lands on when there is no
    # thread with this person yet. Writable when it has its message box on it -
    # the recipient is already in the "To:" line, put there by the address this
    # run asked for. Nothing is typed into that line: the handle-lookup that
    # used to do so is gone, and `message_box_on_form` is what keeps the draft
    # out of it.
    if on_new_message(page.url or ""):
        close_suggestions(page)
        if page.locator(COMPOSER).count() > 0 and message_box_on_form(page):
            log("dm", f"{handle}: writing on the new-message form")
            return True
        log("dm", f"{handle}: the new-message form has no message box on it", "warn")
        return False

    if page.locator(COMPOSER).count() > 0:
        return True

    # No composer YET, which is not the same as no composer. Two things stand in
    # front of a fresh thread and both can be got past - the same two the pooled
    # run handles a poll at a time. Here there is one tab and one thread of
    # control, so they are simply done in order.
    for _ in range(4):                               # Messenger stacks its walls
        did = clear_wall(page)
        if not did:
            break
        log("dm", f"{handle}: {did}")
        try:
            page.wait_for_selector(COMPOSER, timeout=8_000)
            return True
        except Exception:                            # noqa: BLE001 - another wall, or none
            continue

    # Still nothing. Two reasons, and they read differently on the row: the
    # thread said why (a request wall, an unavailable account), or it simply did
    # not come up. Neither raises - both are answers about this recipient.
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "")[:4000]
    except Exception:                                # noqa: BLE001
        body = ""
    if UNREACHABLE_TEXT.search(body):
        return False
    return False


# -- The same open, split in two so five can run at once -----------
#
# `open_chat` blocks from the first navigation until the composer is up, and on
# Messenger most of that is the end-to-end-encryption bootstrap: the app boots,
# the device unseals its keys, the thread backfills. Tens of seconds, and the
# lane's thread is inside all of it - which is the whole reason a run could only
# ever have one chat in flight, and why the wall clock of a campaign was mostly
# spent watching threads load one after another.
#
# These two are the same work with the waiting taken out of it. `begin_chat`
# commits a navigation and leaves; `chat_state` answers, without blocking, what
# that tab has become. Five tabs then boot at the same time, and the run's cost
# per recipient is the message rather than the message plus a page load.
#
# The one thing given up is the row-click fast path. A brand new tab has no chat
# list rendered in it to click a row in, so a pooled run navigates every time and
# pays the bootstrap on each tab - which is exactly the cost the pool exists to
# overlap. At a pool of one there is nothing to overlap, so the campaign keeps
# using `open_chat` there and keeps the fast path with it.
# Matched on the ACCESSIBLE NAME, not on an aria-label. Messenger draws this
# button as a `div[role="button"]` with the word inside it and no label
# attribute at all, so a selector that only reads `aria-label` sees no wall on
# the very page the operator is looking at one on. The regex is anchored, so
# "Continue" is pressed and "Continue with Facebook" - a login button - is not.
CONTINUE_NAME = re.compile(r"^\s*continue\s*$", re.I)


def _continue_targets(page) -> list:
    """Every shape the "Continue" wall comes in, cheapest first.

    ALL of them are visible-only, and that is the fix for a run that reported a
    wall it then could not press. `wall_showing` used to count nodes and
    `press_continue` used to click them, but the two were not asking the same
    question: a bare `count()` on `[aria-label="Continue"]` counts a button
    Messenger has rendered and hidden - a dialog kept in the tree for the next
    time it is needed - so the poll said "wall" forever while the click had
    nothing on screen to land on. The tab then spun on a button that was not
    there until its deadline ran out.

    So the same locators answer both questions, and every one of them ends in
    `:visible` or is filtered to visible below. If nothing is visible, there is
    no wall, and the tab is free to be read as whatever it actually is.
    """
    return [page.get_by_role("button", name=CONTINUE_NAME),
            page.locator(_visible(CONTINUE_WALL)),
            # The shape with no role and no label on it at all: Messenger
            # sometimes draws this as a plain clickable div inside its dialog.
            # Last, because it is the loosest - and still anchored on the whole
            # word by `has_text` being handed the same regex, so
            # "Continue with Facebook" is not a match.
            page.locator('[role="dialog"] div[tabindex="0"]:visible').filter(
                has_text=CONTINUE_NAME)]


def _visible(selector: str) -> str:
    """The same comma-list, every branch of it restricted to what is on screen."""
    return ", ".join(part.strip() + ":visible" for part in selector.split(","))


def _clickable(target, limit: int = 4) -> list:
    """The visible handles behind one locator, not just `.first`.

    `.first` is document order, and document order is not "the one in front of
    the operator": a page carrying a leftover dialog resolves `.first` to the
    stale copy, the click times out against it, and the wall that IS on screen
    is never touched. So every match gets its turn.
    """
    out = []
    try:
        found = target.count()
    except Exception:                                # noqa: BLE001 - mid-render
        return out
    for index in range(min(found, limit)):
        item = target.nth(index)
        try:
            if item.is_visible(timeout=500):
                out.append(item)
        except Exception:                            # noqa: BLE001 - detached
            continue
    return out


# The way OUT of the PIN prompt. Messenger's restore-your-chats screen is not a
# thing this run can answer - the PIN is in the operator's head - but it is also
# not a thing it has to answer: declining leaves the account perfectly able to
# SEND, which is all a campaign wants. Only the history stays sealed.
#
# Anchored like CONTINUE_NAME, and for a sharper reason: a `*=` match on "not
# now" is harmless, but one on "cancel" would find "Cancel account deletion" and
# suchlike. The whole label or nothing.
DISMISS_NAME = re.compile(
    r"^\s*(not now|no thanks|skip|maybe later|later|cancel|dismiss|close)\s*$", re.I)


def restore_showing(page) -> bool:
    """Is the PIN / restore-your-chats prompt on screen?"""
    try:
        body = (page.locator("body").inner_text(timeout=1500) or "")[:4000]
    except Exception:                                # noqa: BLE001 - mid-render
        return False
    return bool(RESTORE_TEXT.search(body))


def dismiss_restore(page, *, timeout: int = 4_000) -> bool:
    """Get the PIN prompt off the screen WITHOUT answering it.

    Never types a PIN and never presses "Continue" on this screen - "Continue"
    here is the way INTO the restore flow, not past it, which is the whole
    reason this is separate from `press_continue` and runs before it.

    Escape last, because Messenger's modals honour it and it cannot click the
    wrong thing; a labelled button first, because the full-page version of this
    screen is not a modal and has nothing for Escape to close.
    """
    targets = (
        page.get_by_role("button", name=DISMISS_NAME),
        page.locator('[role="dialog"] [aria-label="Close" i]'),
        page.locator('[aria-label="Not now" i], [aria-label="Skip" i], [aria-label="Cancel" i]'),
    )
    for target in targets:
        try:
            if target.count() == 0:
                continue
            target.first.click(timeout=timeout)
            page.wait_for_timeout(700)
            return True
        except Exception:                            # noqa: BLE001 - gone, or covered
            continue
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        return not restore_showing(page)
    except Exception:                                # noqa: BLE001
        return False


# The paragraph INSIDE the composer - the node that was inspected on a stuck
# run. Clicking it focuses the editor it belongs to, which is why it is worth
# having even though it is not the box itself: when every guess at the root is
# wrong, the thing the operator can see and point at is still clickable.
#
# Present only while the box is EMPTY - the managed linebreak is what Lexical
# puts in an empty paragraph - which is exactly when a run needs it.
COMPOSER_LINE = ('[role="main"] p:has(> br[data-lexical-managed-linebreak]), '
                 'p[dir="auto"]:has(> br[data-lexical-managed-linebreak])')


# The typeahead that drops out of the "To:" line. REPORTED from a run: after
# the Continue is pressed, this list is sitting open in front of the form.
#
# It matters for one reason beyond the view it blocks: while it is open, Enter
# belongs to IT. The send falls back to Enter when the button is not rendered,
# and an Enter into an open typeahead picks the highlighted suggestion instead
# of sending - which on this screen means choosing a recipient at random.
SUGGESTION_LIST = ('[role="listbox"], '
                   '[role="combobox"][aria-expanded="true"], '
                   '[aria-autocomplete] [role="option"]')


def suggestions_showing(page) -> bool:
    try:
        return page.locator(SUGGESTION_LIST).count() > 0
    except Exception:                                # noqa: BLE001 - mid-render
        return False


def close_suggestions(page) -> bool:
    """Shut the typeahead. Returns whether it was there and went.

    Escape first - it is the one gesture that cannot press anything - and a
    click in the message box second, which is the operator's own suggestion and
    is better than a click "somewhere outside": there is no blank space on this
    screen that is guaranteed not to be a button, and a run that clicks at a
    guessed coordinate to dismiss a menu will eventually click something.

    The message-box click is left to `focus_composer`, which is the next thing
    to run and needs the caret there anyway.
    """
    if not suggestions_showing(page):
        return False
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:                                # noqa: BLE001
        return False
    return not suggestions_showing(page)


def message_box_on_form(page) -> bool:
    """On the new-message form, is the box we would type into the LOWER one?

    The form has two editable lines and the difference between them is the whole
    ball game: the "To:" line at the top, the message box at the bottom. When
    both match, `composer_box` takes the last in document order and that is the
    message box. When only ONE matches, document order says nothing - and typing
    a draft into a recipient line is the worst thing this file can do.

    So the question is put to the SCREEN rather than to the markup: a message box
    sits in the bottom half of the window and a "To:" line sits at the top. That
    is a fact about the layout rather than about the class names, which is what
    makes it worth trusting on a build nobody has surveyed.
    """
    try:
        rect = composer_box(page).bounding_box()
        if not rect:
            return False
        height = (page.viewport_size or {}).get("height") or page.evaluate(
            "() => window.innerHeight")
        if not height:
            return False
        return rect["y"] > height * 0.5
    except Exception:                                # noqa: BLE001 - mid-render
        return False


def _editing(page) -> Optional[bool]:
    """Is the caret actually in an editable box right now? None if unanswerable.

    The question a click cannot answer about itself. A `click()` that resolves
    happily can still have landed on a wrapper, a covering element or a node
    that took the press and did nothing - and the next thing this run does is
    type, which goes to whatever has focus and is silently lost if that is the
    document body.

    Three-valued, deliberately. False is "the caret is somewhere else" and is
    worth another go; None is "this page could not be asked" - it was
    navigating, or the evaluate was refused - and that must NOT read as a failed
    click, or a run stops typing on pages where the click worked perfectly well.
    An unanswerable check falls back to trusting the click that resolved.
    """
    try:
        return bool(page.evaluate(
            "() => { const a = document.activeElement;"
            "        return !!(a && a.closest('[contenteditable=\"true\"]')); }"))
    except Exception:                                # noqa: BLE001 - mid-navigation, or no evaluate
        return None


def focus_composer(page) -> str:
    """Put the caret in the message box, and say how. "" if it could not.

    Three goes, and every one of them CHECKED, because "click the composer" is
    the step that has been reported failing and a click that reports success
    while the caret is elsewhere is indistinguishable from one that worked -
    until a run types four hundred characters into nothing.
    """
    # The typeahead first. It covers the box on the new-message form, and while
    # it is open the Enter that sends belongs to it.
    if close_suggestions(page):
        log("dm", "closed the To: suggestion list")

    box = composer_box(page)
    try:
        if page.locator(COMPOSER).count() > 0:
            box.click(timeout=5_000)
            landed = _editing(page)
            if landed is None:
                return "clicked the message box (focus not verifiable)"
            if landed:
                return "clicked the message box"
    except Exception:                                # noqa: BLE001 - covered, or re-rendered
        pass

    # The inspected paragraph. Inside the editor, so a click on it focuses the
    # editor - and it is matched by the framework's own attribute rather than by
    # any guess at what the root is called.
    try:
        line = page.locator(COMPOSER_LINE).last
        if line.count() > 0:
            line.click(timeout=3_000)
            if _editing(page) is not False:
                return "clicked the line inside the message box"
    except Exception:                                # noqa: BLE001
        pass

    # No click at all: ask the element to take focus. Last because it fires
    # none of the events a click does, and Messenger's composer does listen.
    try:
        if page.locator(COMPOSER).count() > 0:
            box.focus(timeout=3_000)
            if _editing(page) is not False:
                return "focused the message box"
    except Exception:                                # noqa: BLE001
        pass
    return ""


def composer_box(page):
    """The box to type the message into - the LAST match, not the first.

    `Locator.first` resolves in document order, and document order is exactly
    what is wrong here. Messenger builds its "To:" line out of the same Lexical
    editor as the composer, and on the New message screen that line comes first
    - so `.first` is the recipient box on the one screen where picking wrong
    puts the draft in front of a stranger. The message box is the last editable
    thing in the conversation on both screens, which makes `.last` the reading
    that is right in both.

    Everything that touches the composer goes through this, so the box that is
    typed into, the box that is checked for having emptied, and the box that
    Enter is pressed in are all the SAME element. Three separate `.first` calls
    could resolve to three different nodes on a screen that has two.
    """
    return page.locator(COMPOSER).last


def wall_showing(page) -> bool:
    """Is a "Continue" standing in front of this thread, and can it be pressed?

    VISIBLE, not merely present. See `_continue_targets`: a wall reported off a
    hidden node is a wall no click can ever clear, and the tab that believes in
    it waits out its whole deadline pressing nothing.
    """
    return any(_clickable(target, limit=1) for target in _continue_targets(page))


def press_continue(page, *, timeout: int = 4_000) -> bool:
    """Press it, and say whether anything was pressed.

    Deliberately narrow. This clicks a button whose accessible name is the whole
    word "Continue" and nothing else: a looser match on a page that is half
    login wall and half chat would be pressing whatever Facebook happened to put
    in front of the operator, which is not a thing an unattended run should do.
    `guard` has already refused a checkpoint or a login page before this runs.

    Three ways in, per button, because ONE was not enough. A plain `click` is
    refused by Playwright when the button is under a fading overlay or has not
    finished scrolling into view - and that refusal was being read as "there was
    no wall", which sent the tab back to polling the same screen. So the ordinary
    click is tried first, a forced one second (it skips the actionability wait
    rather than the button), and a DOM click last, which is what gets past an
    overlay that is on its way out. All three land on the SAME element, so none
    of them can press something the narrow match did not choose.
    """
    for target in _continue_targets(page):
        for button in _clickable(target):
            try:
                button.scroll_into_view_if_needed(timeout=1_000)
            except Exception:                        # noqa: BLE001 - already there
                pass
            for attempt in ("plain", "force", "dom"):
                try:
                    if attempt == "plain":
                        button.click(timeout=timeout)
                    elif attempt == "force":
                        button.click(timeout=timeout, force=True)
                    else:
                        button.evaluate("node => node.click()")
                except Exception:                    # noqa: BLE001 - gone, or covered
                    continue
                page.wait_for_timeout(600)
                return True
    return False


def clear_wall(page) -> str:
    """Get past the wall that is on screen, and say what was done.

    One entry point for both, because from the poll's side they look identical -
    a button saying Continue - and telling them apart costs a text read that
    must not happen four times a second on five tabs. So the poll reports the
    cheap fact ("a wall") and the expensive question is asked HERE, once, at the
    moment something is about to be pressed.

    Which matters, because the two walls want opposite treatment: the chat's
    Continue is the way through, and the PIN prompt's Continue is the way
    further in. Answers "" when there was nothing to do.
    """
    if restore_showing(page):
        return "declined the PIN prompt" if dismiss_restore(page) else ""
    return "pressed Continue" if press_continue(page) else ""


def begin_chat(page, handle: str, *, timeout: int = 30_000, on_facebook: bool = False) -> None:
    """Start this thread loading in this tab, and return without waiting.

    `on_facebook` picks the second route. Same conversation, different host, and
    it is only ever used after messenger.com has bounced the tab - see
    `FB_THREAD_TEMPLATE` for why the two do not resolve a handle alike.
    """
    url = fb_thread_url(handle) if on_facebook else thread_url(handle)
    goto(page, url, timeout=timeout, wait_until="commit")


def chat_state(page, handle: str, *, skip_wall: bool = False) -> tuple[Optional[str], str]:
    """What has this loading tab become, and how do we know?

    Returns one of four states with the evidence for it:

      ("open",    …)  a composer is up and `write_dm` can type into it.
      ("restore", …)  the PIN prompt. Declined rather than answered, and looked
                      for BEFORE the wall because it has a "Continue" of its own
                      that leads further into the flow rather than past it.
      ("wall",    …)  a "Continue" is in front of the chat. One press clears it,
                      and the caller does the pressing - see below.
      ("picker",  …)  the New message screen came up instead of the thread. The
                      handle has to be typed into its "To:" line and a
                      suggestion picked; `pick_recipient` does that.
      ("bounced", …)  the tab is no longer on a `/t/` address at all. Messenger
                      answers a handle it cannot resolve by REDIRECTING to the
                      inbox rather than erroring, so this is what "no such
                      conversation" looks like from here - and it is the caller's
                      cue to try the other route.
      ("none",    …)  a wall that says in words this account takes no message.
      (None,      …)  not yet. Ask again.

    `skip_wall` is how a caller that has already pressed Continue as many times
    as it is willing to says "stop telling me about that button". Without it a
    wall that will not clear hides every other reading behind it, and the tab
    waits out its deadline on a screen the run could have got past another way.

    This READS and does not act, including on the states that need acting
    on. It is polled every quarter second against five tabs at once, and a
    reader that clicked would press the same button over and over for as long as
    the button took to have an effect. The caller holds each tab's history, so
    the caller is the only thing that can press once.

    The reason travels WITH the state because of what the run used to say. Every
    one of those endings was reported as "no chat available", after the tab had
    sat out a 75-second deadline - so a bounced handle, a request wall and a slow
    thread were one indistinguishable outcome on the table, and nothing in the
    log said which. The string is what gets written on the row.

    Everything this does is a count, a URL read or one short text read, so the
    lane's thread comes straight back and the other four tabs get their turn.
    """
    # Same guard `open_chat` runs, and for the same reason: a tab that has
    # landed on a checkpoint is not a slow tab, and the run has to hear about it
    # rather than sit out the deadline on it.
    guard(page, f"opening the chat with {handle}", quick=True)

    url = page.url or ""
    if not on_a_thread(url):
        # NOT "still loading". `begin_chat` waits for the navigation to commit,
        # so this tab was ON `/t/<handle>` before the first poll ever ran - and
        # an address that is no longer a thread is one Messenger has moved off,
        # which it does when the handle resolves to nobody it can open a
        # conversation with. Answered now rather than waited out.
        if not url or url.startswith("about:"):
            return None, ""
        # Three of those addresses are not dead ends, and all three are on
        # screen rather than in the URL: the PIN prompt, a wall to press, or the
        # New message screen asking for a name. Checked BEFORE the bounce,
        # because a run that reads them as a bounce goes off to try the other
        # host while the way in is sitting in front of it.
        if not skip_wall and wall_showing(page):
            # Reported the INSTANT it is on screen. Whether it is the chat's
            # wall or the PIN prompt's is settled by `clear_wall` when it is
            # pressed - one text read, once, instead of one per tab per poll.
            return "wall", "a Continue wall is in front of the chat"
        # The ADDRESS, before any selector. `/new` is Messenger saying in the
        # one place it cannot rename that this is the New message screen - and
        # `picker_showing` below, which was a guess at the To: line, answered
        # False on exactly this screen and sent the run off calling handles
        # unreachable.
        if on_new_message(url):
            # Not automatically a dead end. Navigating to `/t/<handle>` for
            # someone with no thread yet lands HERE with the recipient already
            # in the "To:" line - put there by the address this run asked for,
            # not by anything it typed - and a message box underneath it. So the
            # question is whether that box is on screen, and the answer decides
            # between writing to a new person and giving up on them.
            try:
                has_box = page.locator(COMPOSER).count() > 0
            except Exception:                        # noqa: BLE001 - mid-render
                return None, ""
            if has_box and message_box_on_form(page):
                return "open", "the new-message form, with its message box"
            return "picker", f"messenger opened its new-message form at {_short_url(url)}"
        # The expensive read, and this is the one place it belongs: everything
        # cheap has been tried and the answer is about to be "bounced", which
        # ends the recipient. It runs once or twice per tab rather than on every
        # poll - the PIN prompt without a Continue on it would otherwise be
        # reported as a bounce, sending the operator to look at the handle when
        # the profile is what is locked.
        if restore_showing(page):
            return "restore", "messenger wants a PIN to restore the chats"
        # STILL LOADING IS NOT A BOUNCE, and this was the line that closed tabs
        # on chats that were about to open. `begin_chat` returns at `commit` -
        # before a byte of the document is parsed - and the first poll lands a
        # quarter of a second later, while Chrome is still following Messenger's
        # own redirect chain to the thread. The address at that instant is
        # whatever it is passing through, and reading it as "messenger sent the
        # tab somewhere else" ended the recipient before the page it asked for
        # had ever been on screen.
        #
        # So a document that has not finished loading is told to ask again. Only
        # a tab that has come to REST somewhere that is not a thread has actually
        # been sent somewhere else, and the caller waits that out too - see
        # `BOUNCE_GRACE` in the campaign, which will not act on this until the
        # same answer has come back for several seconds running.
        if _still_loading(page):
            return None, ""
        return "bounced", f"messenger sent the tab to {_short_url(url)} instead of the chat"

    # The composer being there is the answer, and it is the only one worth
    # having: it is what `write_dm` needs and what "this account takes messages"
    # actually means.
    #
    # Unless the New message screen is up as well. Messenger builds its "To:"
    # line out of the same Lexical editor as the composer, so on THAT screen a
    # composer match may be the recipient box - and it comes first in the
    # document, which is what `.first` resolves on. A screen showing both is
    # therefore reported as the picker, and driving the picker is what turns it
    # into a real thread where the only box on screen is the one to type in.
    try:
        if page.locator(COMPOSER).count() > 0:
            return "open", ""
    except Exception:                                # noqa: BLE001 - mid-render
        return None, ""

    # No composer. The two things that can be got past are counted first,
    # because a count is a DOM query and the text read below is an extraction of
    # the entire application - and the wall is the one thing here that the run
    # should act on within a poll rather than within seconds.
    if not skip_wall and wall_showing(page):
        return "wall", "a Continue wall is in front of the chat"

    # Nothing cheap explains this screen, so now the expensive question: is the
    # thread merely still backfilling, or is there a wall that says in words
    # that this account cannot be written to?
    try:
        body = (page.locator("body").inner_text(timeout=1500) or "")[:4000]
    except Exception:                                # noqa: BLE001 - mid-render
        return None, ""
    found = UNREACHABLE_TEXT.search(body)
    if found:
        return "none", f"messenger says: {found.group(0)}"
    # The E2EE lock, which is neither a slow thread nor a shut inbox: the chats
    # are there and this DEVICE cannot read them until a PIN is entered. It used
    # to run out the deadline and be written on the row as the recipient's fault,
    # which sent the operator looking at the wrong thing entirely.
    #
    # CHECKED BEFORE THE WALL, and that order is the whole point. This screen has
    # a "Continue" on it too, and that Continue is the way INTO the restore flow
    # - so a run that looks for a wall first presses it, lands another step
    # deeper in a flow it cannot finish, and never reaches the composer. The
    # caller declines this instead: the chats stay sealed and the account can
    # still send, which is all a campaign needs from it.
    if RESTORE_TEXT.search(body):
        return "restore", "messenger wants a PIN to restore the chats"
    # On a `/t/` address with no message box, the same two things get in the way
    # as on a bounced one - and this is the case that was actually reported: the
    # wall comes up, the composer is behind it, and polling alone never gets
    # there because nothing on this page changes until it is pressed.
    return None, ""


def _still_loading(page) -> bool:
    """Is this tab still fetching or parsing the page it was pointed at?

    One property read, which is what makes it cheap enough for a poll that runs
    several times a second against five tabs. `interactive` counts as loading:
    Messenger's redirect to the real thread is issued by a script that has not
    necessarily run at that point.
    """
    try:
        return page.evaluate("() => document.readyState") != "complete"
    except Exception:                                # noqa: BLE001 - mid-navigation
        # The evaluate itself fails while a navigation is in flight, which is
        # the strongest evidence there is that this tab is not done moving.
        return True


def _short_url(url: str) -> str:
    """A URL short enough to sit on a table row."""
    trimmed = re.sub(r"^https?://(www\.)?", "", url or "").split("?")[0]
    return trimmed[:60] or "a blank page"


# -- Writing, in three steps so five tabs can do it at once --------
#
# `write_dm` below is all three run back to back, and it is what the one-tab run
# uses. The pooled run cannot use it: typing is deliberately key-by-key at a
# human speed, so a 130-character draft holds its account's thread for half a
# minute - and that thread is the ONE thread every other tab of that account is
# also waiting on. Five tabs then type strictly one after another, which is what
# a run looked like: open five, write one, send it, write the next.
#
# Split up, the pool can give each tab a few characters at a time and come
# straight back for the next tab. The typing is the same typing at the same
# speed into the same box; what changes is that the other four tabs are not
# sitting behind it.
def begin_writing(page, *, media: Optional[list[str]] = None) -> str:
    """Everything before the first character: check WHERE, attach, take the caret.

    Returns how the caret was taken, for the log. Raises rather than returning
    a state, because every one of these failures means nothing may be typed.
    """
    if media:
        try:
            page.set_input_files(FILE_INPUT, media)
            # Attachments upload before the send will take them. The wait is on
            # the thumbnail, not a fixed sleep, because a 4MB image on a slow
            # line takes longer than any number picked in advance.
            page.wait_for_selector(ATTACHED, timeout=60_000)
        except Exception as err:                     # noqa: BLE001
            raise AutomationError(f"could not attach the image: {err}") from err

    # WHERE, before WHAT. Two screens can be typed into and no others: an open
    # conversation, and the new-message form that `/t/<handle>` lands on when
    # there is no thread yet.
    #
    # The form is allowed because of who chose the recipient. This run navigated
    # to that handle's own address and Messenger filled the "To:" line from it -
    # nothing here typed a name, searched for one, or picked one off a list. The
    # earlier version of this file did all three, and that is what put a handle
    # in a search box.
    #
    # `message_box_on_form` is what keeps it safe: that form has two editable
    # lines and the draft must reach the lower one. Refused rather than guessed
    # at, because the wrong answer types the draft into the recipient field.
    url = page.url or ""
    if on_new_message(url):
        if not message_box_on_form(page):
            raise AutomationError(
                "refusing to type: on the new-message form the message box "
                "could not be told apart from the To: line")
    elif not on_a_thread(url):
        raise AutomationError(
            f"refusing to type: {_short_url(url)} is not a conversation")

    how = focus_composer(page)
    if not how:
        raise AutomationError(
            "the message box would not take the caret - nothing was typed")
    return how


def type_slice(page, text: str, start: int, chars: int, wpm: int = 240) -> int:
    """Type up to `chars` more characters of `text`, and say how far it got.

    The unit the pool round-robins on. Small enough that no tab holds the
    account's thread for long, large enough that the per-call overhead is not
    the bulk of the work - a few hundred milliseconds of typing per call.

    The caret is CHECKED, not assumed, on every slice. Between two slices of
    this tab, four other tabs were driven and any of them may have taken the
    window's focus; typing into a page whose caret has moved puts the rest of a
    draft somewhere nobody asked for. Cheap enough to do every time: one
    evaluate against a page that is already loaded.
    """
    if start >= len(text):
        return start
    if _editing(page) is False and not focus_composer(page):
        raise AutomationError("the message box lost the caret part-way through")

    base = _delay_for(wpm)
    end = min(len(text), start + max(1, chars))
    index = start
    while index < end:
        char = text[index]
        if char == "\n":
            # A plain Enter in Messenger's composer SENDS. A two-paragraph draft
            # typed naively would send its first paragraph on its own.
            page.keyboard.press("Shift+Enter")
            page.wait_for_timeout(random.uniform(80, 220))
        else:
            page.keyboard.type(char, delay=0)
            # Jittered per key. A perfectly even 50ms cadence is not a typist.
            page.wait_for_timeout(base * random.uniform(0.55, 1.75))
        index += 1
    return index


def finish_writing(page, *, click_send: bool = True) -> str:
    """Send what has been typed, and prove it went. "sent" or "typed"."""
    page.wait_for_timeout(random.uniform(600, 1600))
    guard(page, "writing the message")

    if not click_send:
        return "typed"

    # Messenger sends on Enter, and its send button is only rendered once there
    # is something to send - so the button is tried first and the key is the
    # fallback, rather than the other way round.
    #
    # BOTH are tried, and that is the change. A send used to be one attempt
    # whose outcome was never read: the button was clicked, an empty composer
    # was waited for, and "sent" was returned either way - so a click that
    # landed on a button which was not the one that sends, or was covered by
    # something, reported a delivered message and left the draft sitting in the
    # box. Messenger CLEARS the composer when it commits a message, so an empty
    # box is the send itself rather than a guess about how long it takes - and
    # a box that is still full is proof the message has NOT gone, which makes
    # the second attempt safe rather than a risk of sending twice.
    for attempt, press in enumerate((_click_send, _press_enter, _click_send)):
        try:
            if not press(page):
                continue
        except Exception:                            # noqa: BLE001 - covered, or re-rendered
            continue
        if _wait_composer_clear(page, timeout=8_000):
            guard(page, "sending the message")
            return "sent"
        if attempt == 0:
            log("dm", "the send button did not clear the box - trying Enter", "warn")

    # The draft is typed and still in the box. Reported as failed, which is what
    # is true - "sent" here would be a message the operator believes went out
    # and did not.
    guard(page, "sending the message")
    raise AutomationError("the message was typed but Messenger would not send it")


def write_dm(page, text: str, *, media: Optional[list[str]] = None,
             typing_wpm: int = 240, click_send: bool = True) -> str:
    """Type into a composer that is ALREADY open, and send it if Click Send is on.

    The three steps above, run back to back. Used by the one-tab run, where
    there is nothing to interleave with and blocking is free.
    """
    log("dm", begin_writing(page, media=media))
    index = 0
    while index < len(text):
        index = type_slice(page, text, index, len(text), typing_wpm)
    return finish_writing(page, click_send=click_send)


def _click_send(page) -> bool:
    """Press the send button, if this build is rendering one. Says whether it did."""
    button = page.locator(SEND_BUTTON).first
    if button.count() == 0:
        return False
    button.click(timeout=5_000)
    return True


def _press_enter(page) -> bool:
    """What Messenger sends on anyway.

    The composer is clicked first: the send button click may have moved focus,
    and an Enter pressed at the page rather than at the box does nothing at all.
    """
    if not focus_composer(page):
        return False
    page.keyboard.press("Enter")
    return True


def _wait_composer_clear(page, *, timeout: int = 8_000) -> bool:
    """Wait until the composer is empty, which is Messenger saying it sent.

    Polled here rather than through `wait_for_selector`, because what is being
    waited on is the CONTENT of a box that stays on the page either way.
    """
    deadline = time.monotonic() + timeout / 1000.0
    while time.monotonic() < deadline:
        try:
            box = composer_box(page)
            if page.locator(COMPOSER).count() == 0 or not (
                    box.inner_text(timeout=1500) or "").strip():
                return True
        except Exception:                            # noqa: BLE001 - mid-render
            pass
        page.wait_for_timeout(120)
    return False


def send_dm(page, handle: str, text: str, *, media: Optional[list[str]] = None,
            typing_wpm: int = 240, click_send: bool = True, follow_first: bool = False,
            via_search: bool = True,
            template: str = PROFILE_TEMPLATE) -> str:
    """Open the thread and write into it. Returns "sent", "typed" or "no chat"."""
    if not open_chat(page, handle, template=template, follow_first=follow_first,
                     via_search=via_search):
        raise DmClosed(f"{handle} cannot be messaged from this account.")
    return write_dm(page, text, media=media, typing_wpm=typing_wpm, click_send=click_send)


# -- Drafts --------------------------------------------------------
def render(text: str, row: dict[str, Any]) -> str:
    """Fill a draft's placeholders.

    `{name}` falls back to the handle rather than leaving a hole: a message
    opening with "hey ," is worse than one that uses the id - it reads as a
    template that misfired, which is exactly what it is.
    """
    handle = str(row.get("handle") or "").lstrip("@")
    # A numeric id is not a name and never reads as one - "hey 100087123456789"
    # is worse than a hole - so an id-only recipient with no known name gets a
    # word that reads in the sentence instead of the number.
    fallback = "there" if handle.isdigit() else handle
    name = str(row.get("name") or "").strip() or fallback
    first = name.split(" ")[0] if name else fallback
    return (text
            .replace("{name}", name)
            .replace("{first}", first)
            .replace("{handle}", handle))


# -- Typing --------------------------------------------------------
def _delay_for(wpm: int) -> float:
    """Milliseconds between keystrokes for a words-per-minute figure.

    Five characters to a word is the standard the number is defined against, so
    240wpm is 1200 characters a minute, or 50ms a key.
    """
    wpm = max(40, min(900, int(wpm or 240)))
    return 60_000 / (wpm * 5)


# `_type_text` used to live here: the whole draft, key by key, in one blocking
# call. `type_slice` replaced it - same keys, same speed, done a few characters
# at a time so five tabs can be typing at once - and this was left behind
# unused. Deleted rather than kept "in case": a second copy of "type into
# whatever currently has focus" is precisely the shape of the bug that put a
# draft in a search box, and an unused one gets no exercise to prove it right.


def _type_human(page, selector: str, value: str, wpm: int = 260) -> None:
    field = page.locator(selector).first
    field.click()
    field.type(value, delay=_delay_for(wpm))


# -- Odds and ends -------------------------------------------------
def _count(text: str) -> Optional[int]:
    """"12.3K followers" -> 12300.

    Returns None rather than 0 when there is no number to read. Zero is a real
    count and the roster filters on it; "unknown" has to survive as something
    else.
    """
    match = re.search(r"([\d.,]+)\s*([KMB])?", text or "", re.I)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    suffix = (match.group(2) or "").upper()
    value *= {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(value)
