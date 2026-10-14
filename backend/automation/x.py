"""Everything that knows what X's pages look like.

meetup-bot kept its selectors in one file for a reason worth repeating here: the
selectors are the part that rots. X ships a new build and a `data-testid`
changes, and when that happens the fix should be one file with a list of strings
in it, not a search across a server.

So the rule for this module is that it is the only place allowed to know a CSS
selector or a URL shape. Everything above it deals in handles, rows and counts.

The `data-testid` attributes below are X's own, and they are the most stable
thing on the page - far steadier than class names, which are generated. Where
there is no testid the code reads visible text instead, which at least fails
loudly and legibly rather than silently matching the wrong element.
"""

from __future__ import annotations

import random
import re
import time
import weakref
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

from . import firecrawl
from ..events import log
from .browser import AutomationError, goto

# -- Selectors ----------------------------------------------------
NAV_ACCOUNT = '[data-testid="SideNav_AccountSwitcher_Button"]'
USER_CELL = '[data-testid="UserCell"]'
DM_BUTTON = '[data-testid="sendDMFromProfile"]'
DM_INPUT = '[data-testid="dmComposerTextInput"]'
DM_SEND = '[data-testid="dmComposerSendButton"]'
DM_FILE = 'input[data-testid="fileInput"]'

# -- The Messages compose flow ------------------------------------
# The other way into a composer, and the one an operator watches for: open
# Messages, press New message, type the handle, pick them, press Next. The
# profile Message button opens the same box from the other side, but on current
# builds it is the flakier of the two - it is often missing, or clicks without
# the box ever appearing - so this is tried first and the button is the standby.
DM_INBOX_URL = "https://x.com/messages"
DM_NEW_BUTTON = ('[data-testid="NewDM_Button"], '
                 '[aria-label="New message" i][role="button"], '
                 'a[href="/messages/compose"]')
# The search box in the "Direct Messages" dialog. `searchPeople` is its testid;
# the aria fallback catches a build that drops it.
DM_SEARCH_INPUT = ('[data-testid="searchPeople"], '
                   '[data-testid="messagesSearchInput"], '
                   'input[placeholder*="Search people" i]')
# One result row in that dialog.
DM_TYPEAHEAD = '[data-testid="TypeaheadUser"]'
# The button that turns a picked person into an open conversation.
DM_NEXT = ('[data-testid="nextButton"], '
           '[role="button"][aria-label="Next" i]')

# How long the composer flow waits at each gate before it gives up on a recipient
# and moves to the next. Short on purpose: an unreachable inbox is the common
# case in a cold list, and the old twenty-second waits turned a run of a few
# hundred into an afternoon of mostly waiting on accounts that were never going
# to answer. A control that is coming appears in a second or two; one that is not
# there in seven was not going to come.
DM_STEP_TIMEOUT = 7_000       # New message, the search box - a step of the flow
DM_COMPOSER_TIMEOUT = 8_000   # the text box, after the whole flow has run

# "The profile has rendered." Any one of these means X's script has drawn the
# header, which is the point at which asking whether there is a chat button is a
# question with an answer - before it, the answer is always no. The follow
# button and the action row arrive with the chat button; the name arrives with
# the profile whether it is reachable or not, so it is last.
PROFILE_READY = ('[data-testid="userActions"], '
                 '[data-testid$="-follow"], [data-testid$="-unfollow"], '
                 '[data-testid="UserName"]')

# The Message button on a profile, when the testid above is not there.
#
# X ships builds where the profile action row carries no testid at all - the
# button is a `div[role="button"]` of obfuscated `css-*`/`r-*` classes wrapping
# one SVG. Nothing in those class names is stable enough to select on; they are
# generated per build. Two things about that button ARE stable:
#
#   * its accessible name is "Message";
#   * its icon is the chat bubble, and the `d` of an SVG path is a drawing, not
#     a name - it only changes when the icon is redrawn.
#
# So the fallbacks are the accessible name first, and the drawing second.
DM_BUTTON_ARIA = ('[role="button"][aria-label="Message" i], '
                  'button[aria-label="Message" i], '
                  '[role="button"][data-testid="sendDMFromProfile"]')

# The first characters of the chat-bubble path. A prefix, not the whole `d`:
# X re-emits the same icon with different trailing sub-paths between builds.
DM_ICON_PATH = "M12 4c-4.418 0-8 3.582-8 8"

# Find the Message button by its icon, and hand back the thing that is actually
# clickable - the path is inside an <svg> inside a label div inside the button.
#
# The same chat bubble is drawn twice more on a profile page: once in the left
# navigation rail, and once on every post in the timeline below. Both are
# excluded by where they sit rather than by how they look, and of whatever is
# left the topmost wins, because the profile action row is above the posts.
DM_BUTTON_JS = r"""
(prefix) => {
  const hits = [];
  for (const path of document.querySelectorAll('svg path')) {
    if (!(path.getAttribute('d') || '').startsWith(prefix)) continue;
    const button = path.closest('[role="button"], button, a[href]');
    if (!button) continue;
    if (button.closest('nav')) continue;                        // the left rail
    if (button.closest('[data-testid="cellInnerDiv"]')) continue;  // a post
    if (button.closest('[data-testid="primaryColumn"]') === null) continue;
    const box = button.getBoundingClientRect();
    if (box.width < 12 || box.height < 12) continue;            // not drawn
    hits.push([box.top, button]);
  }
  hits.sort((a, b) => a[0] - b[0]);
  return hits.length ? hits[0][1] : null;
}
"""

# The composer, once it is open. The testid is the one that has actually been
# seen to go missing, so this accepts any of the shapes the message box takes:
# X sometimes opens the DM in place on the profile and sometimes navigates to
# /messages/<id>, and the two are not always the same markup.
DM_INPUT_ANY = ('[data-testid="dmComposerTextInput"], '
                '[data-testid="dmComposerTextInput"] [contenteditable="true"], '
                '[role="textbox"][contenteditable="true"][aria-label*="message" i], '
                '[data-testid="dmDrawer"] [role="textbox"][contenteditable="true"]')
LOGIN_USERNAME = 'input[autocomplete="username"]'
LOGIN_PASSWORD = 'input[name="password"]'
LOGIN_SUBMIT = '[data-testid="LoginForm_Login_Button"]'
LOGIN_NEXT = '//button[.//span[text()="Next"]]'
# X asks for the email or phone on the account when the login looks unusual. It
# is the same input it uses for a confirmation code, hence the vague testid.
CHALLENGE_INPUT = '[data-testid="ocfEnterTextTextInput"]'
CHALLENGE_SUBMIT = '[data-testid="ocfEnterTextNextButton"]'
EMPTY_STATE = '[data-testid="empty_state_header_text"]'

# A logged-out or rate-limited page can look like an empty follower list. These
# are the phrases that mean "not what you asked for", checked before a sweep
# reports zero accounts as a fact.
BLOCKED_TEXT = re.compile(
    r"(something went wrong|rate limit|try again|caught up|sign in to X|log in to X)",
    re.I,
)

# The one page a sweep walks. Everything the console collects comes off the
# signed-in account's own feed, so there is no list URL to build and no mode to
# pick - the target IS the home timeline.
HOME_URL = "https://x.com/home"

# X's own left-hand navigation, keyed the way the Actions panel addresses it.
# Each entry is a click target FIRST and a URL second: the panel's whole point is
# pressing the real sidebar item - so a "More" opens its menu and a "Post" opens
# the composer exactly as a hand on the mouse would - and the URL is the fallback
# for the three items X ships without a data-testid (Grok, History, Creator
# Studio) and for a window not sitting on a page that carries the sidebar at all.
# The order is the order they appear down the rail, which is the order `All`
# walks them in.
NAV_ACTIONS: dict[str, dict[str, Any]] = {
    "home":          {"label": "Home",           "testid": "AppTabBar_Home_Link",          "url": "https://x.com/home"},
    "explore":       {"label": "Explore",        "testid": "AppTabBar_Explore_Link",       "url": "https://x.com/explore"},
    "notifications": {"label": "Notifications",  "testid": "AppTabBar_Notifications_Link", "url": "https://x.com/notifications"},
    "follow":        {"label": "Follow",         "testid": "AppTabBar_Follow_Link",        "url": "https://x.com/i/connect_people"},
    "chat":          {"label": "Chat",           "testid": "AppTabBar_DirectMessage_Link", "url": "https://x.com/messages"},
    "grok":          {"label": "Grok",           "aria": "Grok",                           "url": "https://x.com/i/grok"},
    "history":       {"label": "History",        "aria": "History",                        "url": "https://x.com/i/history"},
    "studio":        {"label": "Creator Studio", "aria": "Creator Studio",                 "url": "https://x.com/i/jf/creators/studio"},
    # `closes` says this item opens something that covers the page. X's Premium
    # sign-up is a full-screen panel over the timeline, and while it is up every
    # other item on the rail is behind it - so `All` would press Premium and then
    # fail on everything after it. Marked here rather than special-cased in the
    # caller, so the next item that behaves this way is one flag.
    "premium":       {"label": "Premium",        "testid": "premium-signup-tab",           "url": "https://x.com/i/premium_sign_up", "closes": True},
    "profile":       {"label": "Profile",        "testid": "AppTabBar_Profile_Link",       "url": None},
    "more":          {"label": "More",           "testid": "AppTabBar_More_Menu",          "url": None},
    "post":          {"label": "Post",           "testid": "SideNav_NewTweet_Button",      "url": "https://x.com/compose/post"},
}


# -- URLs ---------------------------------------------------------
def handle_of(target: str) -> str:
    """The handle inside whatever was typed into the target box."""
    raw = (target or "").strip()
    match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)", raw)
    if match:
        return match.group(1)
    return raw.lstrip("@").split("/")[0]


def profile_url(handle: str, template: str = "https://x.com/{handle}") -> str:
    return template.replace("{handle}", handle.lstrip("@"))


# -- Reading the home timeline ------------------------------------
# One evaluate per pass rather than a Playwright query per element: the timeline
# is virtualised, and forty round trips across the CDP bridge take longer than
# the feed takes to recycle the nodes underneath them.
#
# Three things in the feed carry an account, and all three are worth having: the
# author of a post, the account named in a "reposted"/"liked" header above one,
# and the UserCells X drops inline as "Who to follow". Read in one pass and
# de-duplicated here, so the walk above only ever sees handles it has not met.
HOME_JS = r"""
() => {
  const skip = /^(i|home|explore|notifications|messages|search|settings|compose)$/;
  const labels = /^(Follow|Following|Pending|Follow back|Follows you|Blocked|Unblock|Subscribe|Ad|Promoted)$/i;
  // The age of the post, which sits in the same byline block as the name and
  // the handle. Without filtering it, an account whose display name renders as
  // an image gets "17h" written down as its name - and a DM opens "hey 17h".
  const stamp = /^(\d+[smhd]|[A-Z][a-z]{2} \d{1,2}(, \d{4})?)$/;
  const found = new Map();

  // Where the handle sits in the PAGE, not the window - so it stays comparable
  // after the walk has scrolled on. This is what lets the hover walk go down
  // the screen in the order a reader would meet the accounts, instead of doing
  // every post first and then jumping back up for the follow rails between them.
  const topOf = node => {
    if (!node) return 0;
    const box = node.getBoundingClientRect();
    return Math.round(box.top + window.scrollY);
  };

  const add = (handle, name, bio, locked, node) => {
    handle = (handle || '').trim();
    if (!handle || skip.test(handle)) return;
    const seen = found.get(handle);
    if (seen) {
      // A handle appears several times down one screen. Keep the richest read of
      // it rather than whichever came last: a bare repost header would otherwise
      // blank the name the post itself carried.
      if (!seen.name && name) seen.name = name;
      if (!seen.bio && bio) seen.bio = bio;
      // And the HIGHEST of its positions, because that is the one the walk will
      // reach first on its way down.
      const top = topOf(node);
      if (top && top < seen.top) seen.top = top;
      return;
    }
    found.set(handle, {
      handle, name: name || '', bio: bio || '', protected: !!locked, top: topOf(node),
    });
  };

  const handleOf = link =>
    (link.getAttribute('href') || '').replace(/^\//, '').split('/')[0];

  // Every post on screen: whoever wrote it, and whoever put it in this feed.
  document.querySelectorAll('[data-testid="tweet"]').forEach(tweet => {
    tweet.querySelectorAll('[data-testid="User-Name"]').forEach(block => {
      const link = block.querySelector('a[role="link"][href^="/"]');
      if (!link) return;
      const handle = handleOf(link);
      // The block reads "Name\n@handle\n·\n4h". The name is the first line that
      // is neither the handle nor the timestamp separator.
      // The separator is stripped off each line before it is judged, not
      // matched as a line of its own: depending on how X lays the byline out
      // it arrives either as its own line or glued to the age beside it, and
      // only one of those two is caught by comparing whole lines.
      const lines = (block.innerText || '').split('\n')
        .map(s => s.replace(/^[\s\u00b7]+|[\s\u00b7]+$/g, '').trim())
        .filter(s => s && !labels.test(s) && !stamp.test(s));
      const name = lines.find(line => !line.startsWith('@')) || '';
      add(handle, name, '',
          block.querySelector('svg[aria-label*="rotected"], [data-testid="icon-lock"]'),
          block);
    });

    // "@someone reposted" above the post - a second account, and one that has
    // never posted into this feed itself.
    const social = tweet.querySelector('[data-testid="socialContext"] a[role="link"][href^="/"]');
    if (social) add(handleOf(social), '', '', false, social);
  });

  // The follow rails X seeds into the feed. These are UserCells, laid out as a
  // run of `div[dir]` blocks: the display name, then the @handle, then the bio.
  document.querySelectorAll('[data-testid="UserCell"]').forEach(cell => {
    const link = cell.querySelector('a[role="link"][href^="/"]');
    if (!link) return;
    const handle = handleOf(link);
    const blocks = Array.from(cell.querySelectorAll('div[dir]'))
      .map(node => (node.innerText || '').trim())
      .filter(text => text && !labels.test(text));

    // The handle block is matched against THIS cell's handle, and from the end:
    // an account whose display name is its own handle (@levelsio) puts the same
    // text in two blocks, and anchoring on the first makes the name the handle.
    const wanted = '@' + handle.toLowerCase();
    let at = -1;
    for (let i = blocks.length - 1; i >= 0; i--) {
      if (blocks[i].toLowerCase() === wanted) { at = i; break; }
    }
    if (at < 0) at = blocks.findIndex(text => /^@[A-Za-z0-9_]{1,15}$/.test(text));

    let name = at > 0 ? blocks[at - 1] : '';
    const bio = at >= 0 ? blocks.slice(at + 1).join(' ') : blocks.slice(1).join(' ');
    if (!name) {
      const lines = (cell.innerText || '').split('\n')
        .map(s => s.trim()).filter(s => s && !labels.test(s));
      name = lines.find(line => !line.startsWith('@')) || '';
    }
    // A name that is the handle WRITTEN AS ONE is no name at all - carrying it
    // would make a DM open "hey @jackbutcher". Tested on the leading @, not on
    // the text: plenty of display names match their handle (Naval, @naval).
    if (name.startsWith('@') && name.slice(1).toLowerCase() === handle.toLowerCase()) name = '';

    add(handle, name, bio,
        cell.querySelector('svg[aria-label*="rotected"], [data-testid="icon-lock"]'),
        cell);
  });

  // Top of the page first. The caller hovers them in exactly this order, so the
  // feed only ever has to travel FORWARD to reach the next one.
  return Array.from(found.values()).sort((a, b) => a.top - b.top);
}
"""


# Which handle the profile is signed in as. The profile link in the tab bar is
# the best source on the page: its href IS the handle, so there is no display
# name to parse and nothing that changes with the language X renders in.
WHOAMI_JS = """
() => {
  const link = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
  if (link) {
    const handle = (link.getAttribute('href') || '').replace(/^\\//, '').split('/')[0];
    if (handle) return handle;
  }
  const swap = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  const found = swap && (swap.innerText || '').match(/@([A-Za-z0-9_]{1,15})/);
  return found ? found[1] : '';
}
"""


def whoami(page) -> dict[str, Any]:
    """Whose session is this - if anyone's, and under which handle.

    Read off the page rather than off a cookie: a cookie jar can hold a token X
    has already invalidated, and the difference only shows up as a redirect to
    the login wall halfway through a run.

    The handle matters as much as the yes/no. A profile signed in as someone
    other than the account on the card will send from the wrong account without
    ever failing, and that is not a mistake anything downstream can catch.
    """
    try:
        goto(page, "https://x.com/home")
        page.wait_for_timeout(1800)
        if "/i/flow/login" in page.url or "/login" in page.url:
            return {"logged_in": False, "handle": None}
        if page.locator(NAV_ACCOUNT).count() == 0:
            return {"logged_in": False, "handle": None}
        return {"logged_in": True, "handle": (page.evaluate(WHOAMI_JS) or "") or None}
    except Exception:                                # noqa: BLE001 - offline counts as no
        return {"logged_in": False, "handle": None}


def is_logged_in(page) -> bool:
    return bool(whoami(page)["logged_in"])


# Either the account switcher (signed in) or the login wall (signed out). One of
# the two is what "the page has finished deciding" looks like.
_SETTLED_JS = r"""
() => !!document.querySelector('%s')
      || /\/i\/flow\/login|\/login/.test(location.pathname)
""" % NAV_ACCOUNT


def whoami_when_settled(page, timeout: int = 20_000) -> dict[str, Any]:
    """`whoami`, but it waits for the page to become one thing or the other.

    `whoami` navigates and then waits a flat 1.8 seconds. On a warm profile that
    is plenty; on a cold one - a window opened seconds ago, a slow morning - the
    timeline is still painting and has no account switcher in it yet, which
    reads as signed out.

    That answer is not merely wrong, it gets WRITTEN: the caller records it
    against the card, and a saved session is cleared because the network was
    slow. So this waits for a definite answer instead, and says which kind it
    got. `settled` false means "no answer", not "signed out", and a caller must
    leave the stored session alone when it sees one - the profile on disk is
    untouched either way, and a card that still says "signed in" is closer to
    the truth than one wiped by a timeout.
    """
    settled = False
    try:
        goto(page, "https://x.com/home")
        page.wait_for_function(_SETTLED_JS, timeout=timeout)
        settled = True
    except Exception:                                # noqa: BLE001 - reported as unsettled
        pass
    found = peek(page)
    found["settled"] = settled
    return found


def peek(page) -> dict[str, Any]:
    """Who this window is signed in as, WITHOUT navigating it anywhere.

    `whoami` finds out by going to x.com/home, which is right when nobody is
    looking at the window and destructive when somebody is: the sign-in watcher
    polls every few seconds, and a person typing a password into a form that
    keeps being navigated out from under them cannot finish. So this reads
    whatever is on screen already and says "not yet" for anything that is not a
    signed-in X page - a Google chooser, the login wall, a blank tab.
    """
    try:
        url = page.url or ""
        if not ("x.com" in url or "twitter.com" in url):
            return {"logged_in": False, "handle": None}
        if "/i/flow/login" in url or "/login" in url:
            return {"logged_in": False, "handle": None}
        if page.locator(NAV_ACCOUNT).count() == 0:
            return {"logged_in": False, "handle": None}
        return {"logged_in": True, "handle": (page.evaluate(WHOAMI_JS) or "") or None}
    except Exception:                                # noqa: BLE001 - mid-navigation counts as no
        return {"logged_in": False, "handle": None}


# Google's own account page states the address in a handful of places. The
# aria-label on the account button is the one that carries it in full, and it is
# read only when a session check finds an X login it does not yet have a Google
# address for.
GOOGLE_JS = """
() => {
  const label = document.querySelector('a[aria-label*="@"], [aria-label*="Google Account"]');
  const from = label ? label.getAttribute('aria-label') : '';
  const text = from + ' ' + (document.body.innerText || '').slice(0, 4000);
  const found = text.match(/[\\w.+-]+@[\\w-]+\\.[\\w.-]+/);
  return found ? found[0] : '';
}
"""


def google_account(page) -> Optional[str]:
    """Which Google address this profile's SSO session belongs to.

    Best effort, and deliberately quiet about failing. Signing in to X through
    Google leaves a google.com session in the same profile, and the address on it
    is the thing an operator needs to know when the card says only "signed in" -
    but it is one extra page load on somebody else's site, so it is read once,
    stored, and never asked for again.
    """
    try:
        goto(page, "https://accounts.google.com/", timeout=30_000)
        page.wait_for_timeout(2000)
        if "signin" in (page.url or "") or "ServiceLogin" in (page.url or ""):
            return None                              # no Google session in this profile
        found = (page.evaluate(GOOGLE_JS) or "").strip()
        return found or None
    except Exception:                                # noqa: BLE001 - never fails a check
        return None


def google_account_beside(context) -> Optional[str]:
    """The same read, in a tab of its own.

    `google_account` navigates the page it is handed, and at the end of a
    sign-in that page is the operator's window, still showing the timeline they
    just landed on. This opens a second tab, reads it, and closes it, so the
    answer costs them nothing they can see.
    """
    page = None
    try:
        page = context.new_page()
        return google_account(page)
    except Exception:                                # noqa: BLE001 - never fails a check
        return None
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:                        # noqa: BLE001 - already gone
                pass


def challenge_showing(page) -> Optional[str]:
    """A captcha, a verification prompt, or a bounce back to the login wall.

    Named, not just detected: "X asked for a verification code" and "X showed a
    captcha" are different mornings, and the run bar is the only place the
    operator will read it.
    """
    try:
        url = page.url or ""
        if "/account/access" in url:
            return "the account is locked - X wants it unlocked in a browser"
        if "/i/flow/login" in url or "/login" in url:
            return "the session dropped back to the login wall"
        if page.locator(CHALLENGE_INPUT).count() > 0:
            return "X is asking for a verification code"
        if page.locator('iframe[src*="arkose"], iframe[title*="challenge" i]').count() > 0:
            return "X showed a captcha"
    except Exception:                                # noqa: BLE001
        return None
    return None


def guard(page, when: str) -> None:
    """Raise if the page has stopped being the page we asked for."""
    found = challenge_showing(page)
    if found:
        raise AutomationError(f"Stopped {when}: {found}.")


# -- Getting out of whatever just opened --------------------------
# X's panels - Premium's sign-up above all - sit OVER the page rather than beside
# it, and while one is up nothing else on the rail can be clicked. So every press
# clears the way first, and the items known to open one close it again after.

# What "something is covering the page" looks like.
OVERLAY = '[role="dialog"], [aria-modal="true"]'

# The close control, most specific first. The testid is X's own and the surest;
# the aria-label is the next surest and survives a testid rename.
#
# The last two match the cross ITSELF, by the start of its path data - the shape
# in the SVG X draws its close button with. That is the shape the operator will
# recognise, and it is what is left when a build ships the button with neither a
# testid nor a label. It goes last on purpose: the same cross is drawn on dismiss
# buttons all over X, and matching it first would close the wrong thing.
CLOSE_BUTTONS = (
    '[data-testid="app-bar-close"]',
    '[aria-label="Close"]',
    '[aria-label="Dismiss"]',
    '[aria-label="Back"]',
    'button:has(path[d^="M10.59 12L4.54 5.96"])',
    '[role="button"]:has(path[d^="M10.59 12L4.54 5.96"])',
)


def overlay_showing(page) -> bool:
    """Is a panel covering the page right now."""
    try:
        return page.locator(OVERLAY).count() > 0
    except Exception:                                # noqa: BLE001 - a page mid-navigation
        return False


def close_overlay(page, *, tries: int = 3) -> bool:
    """Dismiss whatever is covering the page. Returns whether anything closed.

    Tried in order: the close button inside the panel, then the same button
    anywhere on the page, then Escape - which is what actually dismisses most of
    X's modals and costs nothing when there is nothing to dismiss.

    Never raises. This is called before every press to clear the way, and a page
    with nothing open is the normal case rather than a failure.
    """
    closed = False
    for _ in range(max(1, tries)):
        if not overlay_showing(page):
            break

        pressed = False
        # Inside the panel first: the cross belonging to the thing that is up,
        # rather than one belonging to a card behind it.
        for scope in (f"{OVERLAY} ", ""):
            for selector in CLOSE_BUTTONS:
                try:
                    found = page.locator(scope + selector).first
                    if found.count() == 0:
                        continue
                    found.click(timeout=3000)
                    pressed = True
                    break
                except Exception:                    # noqa: BLE001 - try the next shape
                    continue
            if pressed:
                break

        if not pressed:
            try:
                page.keyboard.press("Escape")
            except Exception:                        # noqa: BLE001
                return closed

        page.wait_for_timeout(700)
        if not overlay_showing(page):
            closed = True
            break
        closed = True                                # something moved; go round again

    return closed


# -- The sidebar nav ----------------------------------------------
def _settle(page, entry: dict[str, Any], result: str) -> str:
    """What to do once an item has been pressed.

    For most of the rail, nothing: the click IS the action. For an item marked
    `closes` the panel it just opened is dismissed again, because leaving it up
    blocks every press after it - and a button that works once and then jams the
    panel is worse than one that does not work at all.
    """
    if not entry.get("closes"):
        return result
    return f"{result} and closed" if close_overlay(page) else result


def click_action(page, action: str) -> str:
    """Press one item in X's left-hand navigation.

    The real sidebar element, clicked - not merely a navigation to its URL - so
    "More" opens its menu and "Post" opens the composer the way a click does.
    Returns "clicked" when the element was pressed, "opened" when it fell back to
    the item's URL. The URL is the fallback for the handful of items X ships with
    no data-testid, and for a window that is not on a page carrying the sidebar.
    """
    # The one key that is not a menu item: close whatever is covering the page.
    # It lives here rather than in NAV_ACTIONS because it is not on X's rail -
    # it is how you get back TO the rail.
    if action == "close":
        return "closed" if close_overlay(page) else "nothing open"

    entry = NAV_ACTIONS.get(action)
    if entry is None:
        raise AutomationError(f"{action} is not a menu item this can press.")

    # The sidebar only exists on a signed-in x.com page. A window sitting on a
    # login wall, a settings sub-page or a blank tab may not carry the rail, so
    # it is sent home first and the click made from there.
    if "x.com/" not in (page.url or ""):
        goto(page, "https://x.com/home")
        page.wait_for_timeout(1500)

    # Clear the way. A panel left open by the LAST press covers the rail, and
    # every click after it fails on an element that is there but not reachable -
    # which is the failure that reads as "the Actions panel stopped working".
    close_overlay(page)

    selectors = []
    if entry.get("testid"):
        selectors.append(f'[data-testid="{entry["testid"]}"]')
    if entry.get("aria"):
        selectors.append(f'[aria-label="{entry["aria"]}"]')
    # The label as a last on-page handle: the three testid-less items still carry
    # their word in the nav, and reading it fails loudly rather than silently
    # matching nothing.
    selectors.append(f'nav[role="navigation"] a[role="link"]:has-text("{entry["label"]}")')

    for selector in selectors:
        try:
            found = page.locator(selector).first
            if found.count() > 0:
                found.click(timeout=8000)
                page.wait_for_timeout(1200)
                guard(page, f"opening {entry['label']}")
                return _settle(page, entry, "clicked")
        except Exception:                            # noqa: BLE001 - fall through to the URL
            pass

    url = entry.get("url")
    if url:
        goto(page, url)
        page.wait_for_timeout(1000)
        return _settle(page, entry, "opened")

    # Profile with no click target found: its link is the signed-in handle's own,
    # so read who this is and go there rather than reporting a dead button.
    handle = (page.evaluate(WHOAMI_JS) or "").strip()
    if handle:
        goto(page, profile_url(handle))
        page.wait_for_timeout(1000)
        return "opened"
    raise AutomationError(f"Could not find the {entry['label']} button on the page.")


# -- Logging in ---------------------------------------------------
def login(page, username: str, password: str, email: str = "") -> None:
    """The password flow, up to the point a human has to take over.

    Everything X can ask for that is on the account - the handle, the email - is
    answered here. A code sent to that email is not: nothing in this repo reads
    mail, and typing a wrong code is how a challenge escalates into a lock. The
    run stops and says so, with the window left open on the prompt.
    """
    if not password:
        raise AutomationError(f"No password stored for @{username}.")

    goto(page, "https://x.com/i/flow/login")
    page.wait_for_timeout(1500)

    if page.locator(NAV_ACCOUNT).count() > 0:
        return                                       # already signed in on this profile

    page.wait_for_selector(LOGIN_USERNAME, timeout=45_000)
    _type_human(page, LOGIN_USERNAME, username, wpm=280)
    page.wait_for_timeout(400)
    _click_text(page, "Next")
    page.wait_for_timeout(1800)

    # The "unusual login" step: X asks for the email or phone on the account
    # before it will show the password field.
    if page.locator(CHALLENGE_INPUT).count() > 0 and page.locator(LOGIN_PASSWORD).count() == 0:
        if not email:
            raise AutomationError(
                f"X asked @{username} for its email before the password, and none is "
                "stored on the account card."
            )
        _type_human(page, CHALLENGE_INPUT, email, wpm=280)
        page.wait_for_timeout(400)
        _click_text(page, "Next")
        page.wait_for_timeout(1800)

    page.wait_for_selector(LOGIN_PASSWORD, timeout=45_000)
    _type_human(page, LOGIN_PASSWORD, password, wpm=260)
    page.wait_for_timeout(500)
    if page.locator(LOGIN_SUBMIT).count() > 0:
        page.locator(LOGIN_SUBMIT).first.click()
    else:
        _click_text(page, "Log in")
    page.wait_for_timeout(4000)

    found = challenge_showing(page)
    if found:
        raise AutomationError(
            f"@{username}: {found}. The window is open on it - finish it by hand, "
            "then press Check sessions."
        )
    if page.locator(NAV_ACCOUNT).count() == 0:
        page.wait_for_timeout(4000)
    if page.locator(NAV_ACCOUNT).count() == 0:
        raise AutomationError(f"@{username}: the login did not land on a signed-in page.")


# -- The hover card -----------------------------------------------
# X pops a profile preview when the cursor rests on a handle. It carries the bio
# and the follower/following counts a bare list row does not, and reading it is a
# hover rather than a page load - so a sweep can fill those in as it walks,
# without the per-profile navigation `Re-read accounts` does. This is the card's
# own testid; the counts are the same anchors the profile header uses.
HOVER_CARD_JS = r"""
(handle) => {
  const card = document.querySelector('[data-testid="HoverCard"]');
  if (!card) return null;
  const text = (card.innerText || '').trim();
  if (!text) return null;

  // WHOSE card this is. X leaves the last popup in the DOM for a moment after
  // the cursor moves off, so a card found here is not necessarily the one that
  // was just asked for - and reading it anyway writes one account's follower
  // count onto another's row, silently and unrecoverably. Checked against the
  // handle the caller hovered, and refused rather than guessed.
  const wanted = String(handle || '').toLowerCase();
  const links = Array.from(card.querySelectorAll('a[role="link"][href^="/"]'))
    .map(a => (a.getAttribute('href') || '').replace(/^\//, '').split('/')[0].toLowerCase());
  const named = /@([A-Za-z0-9_]{1,15})/.exec(text);
  const belongs = links.includes(wanted)
    || (named && named[1].toLowerCase() === wanted);
  if (!belongs) return null;

  const followers = card.querySelector('a[href$="/followers"], a[href$="/verified_followers"]');
  const following = card.querySelector('a[href$="/following"]');
  const bio_el = card.querySelector('[data-testid="UserDescription"]');
  const named_el = card.querySelector('[data-testid="UserName"]');

  // The display name off the popup. A timeline row does not always carry one -
  // a repost header names only the handle - and the card is the cheapest place
  // to recover it, so it is read here rather than left to a profile load.
  let name = named_el
    ? ((named_el.innerText || '').split('\n')[0] || '').trim()
    : ((text.split('\n').find(line => line.trim() && !line.trim().startsWith('@')) || '').trim());
  // X sometimes runs the display name and the @handle together in one block.
  // Trim the handle off the end rather than carrying it into a DM opening
  // "hey Naval@naval".
  name = name.replace(new RegExp('@' + wanted + '$', 'i'), '').trim();
  if (name.toLowerCase() === '@' + wanted) name = '';

  // The bio - the lines under the @handle in the popup.
  //
  // Read off the card's TEXT rather than off a testid, because the hover card
  // does not carry `UserDescription`; that testid belongs to the profile header,
  // and asking the card for it comes back empty on almost every account (and
  // occasionally with the follow button's own label, which is worse than empty).
  // The popup is laid out as: the follow button, the display name, the @handle,
  // the bio, then the counts. So the bio is everything between the handle line
  // and the first line that is a count, a "Followed by" line, or a button.
  const COUNT = /^[\d.,]+[KMB]?\s*(Followers?|Following)$/i;
  const AFTER = /^(Followed by|Follows you|Joined |Click to )/i;
  const BUTTON = /^(Follow|Following|Pending|Follow back|Blocked|Unblock|Subscribe|Subscribed|Message|Notify)$/i;

  let bio = '';
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean);
  let at = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].toLowerCase() === '@' + wanted) { at = i; break; }
  }
  if (at >= 0) {
    const parts = [];
    for (let i = at + 1; i < lines.length; i++) {
      const line = lines[i];
      if (COUNT.test(line) || AFTER.test(line) || BUTTON.test(line)) break;
      parts.push(line);
    }
    // A bio X wrapped over several lines is one bio, joined back up: the column
    // it lands in is one line high, and the newlines were the popup's, not the
    // author's.
    bio = parts.join(' ').trim();
  }
  // The testid is still preferred WHEN it is there and says something real - a
  // future build that adds it back should win over the text scrape.
  const stated = bio_el ? (bio_el.innerText || '').trim() : '';
  if (stated && !AFTER.test(stated) && !BUTTON.test(stated)) bio = stated;

  return {
    // The whole popup, verbatim - name, handle, bio and the counts, as X laid
    // them out. Kept alongside the parsed fields so nothing the card showed is
    // thrown away.
    text,
    name,
    bio,
    followers_text: followers ? (followers.innerText || '').trim() : '',
    following_text: following ? (following.innerText || '').trim() : '',
  };
}
"""


def hover_card(page, handle: str, *, settle: float = 0.30,
               timeout: int = 1600) -> Optional[dict[str, Any]]:
    """Hover a handle in the open feed and read the profile card X pops up.

    Returns the card's fields, or None when no card appeared - a row X recycled
    out of the DOM before the cursor reached it, or a hover it simply did not
    answer. Deliberately quiet about that: on a feed of thousands a few misses
    are the cost of not opening every profile, not a failure worth stopping the
    walk.

    The timeouts are short on purpose, and are what decides how far the bio
    column trails the handles beside it. This runs once per account on a walk
    with no end: the handles go up the moment they are read, and each bio arrives
    only when its hover lands, so the whole cost of this call is the gap the
    operator watches the bio fill across. X pops the card in well under a second
    or not at all, so waiting longer than that buys no bios - it only widens the
    gap and, on a MISS, spends the whole wait for nothing. Kept tight so the bio
    catches the handle rather than trailing a screen behind it; a card that is
    genuinely slow that day is what `Re-read accounts` is for.

    The handle is looked for in each of the places the timeline puts one, most
    specific first: a post's byline, a follow rail's cell, a repost header. The
    fallback is any link to it on the page, which is what catches the shapes X
    has not shipped yet.
    """
    where = (
        f'[data-testid="User-Name"] a[role="link"][href="/{handle}"]',
        f'[data-testid="UserCell"] a[role="link"][href="/{handle}"]',
        f'[data-testid="socialContext"] a[role="link"][href="/{handle}"]',
        f'a[role="link"][href="/{handle}"]',
    )
    link = None
    for selector in where:
        link = _pick(page, selector)
        if link is not None:
            break
    if link is None:
        return None

    at = _approach(page, link)
    if at is None:
        return None

    # The card is delayed on purpose by X - it waits out a cursor merely passing
    # over. So the hover is held, then the popup waited for.
    try:
        page.wait_for_selector('[data-testid="HoverCard"]', state="visible", timeout=timeout)
    except Exception:                                # noqa: BLE001
        _dismiss_card(page, at)
        return None
    page.wait_for_timeout(int(max(0.0, settle) * 1000))

    # The handle goes in with it: the script refuses a card belonging to anyone
    # else rather than handing back the last account's numbers.
    found = page.evaluate(HOVER_CARD_JS, handle)
    _dismiss_card(page, at)
    return found


# Where on screen a handle should be sitting when its card is read, as a share of
# the window: a band rather than a line, because a handle already somewhere a
# reader would be looking is one that costs no scroll at all. `HOVER_REST` is
# where a handle outside the band gets put.
HOVER_BAND = (0.20, 0.58)
HOVER_REST = 0.36

# A step smaller than this is not worth making - it is below the distance a hand
# could aim for anyway, and a page that twitches by twenty pixels between every
# account is doing something no reader does.
HOVER_SLACK = 48

# How fast the step BETWEEN two handles travels, in pixels per second. Slower
# than a reading pass on purpose: this is the short move from one account to the
# next one just below it, not a sweep down the feed.
STEP_SPEED = (700, 1400)


def _approach(page, link) -> Optional[tuple[float, float]]:
    """Bring a handle under the cursor without the page jumping. Returns where.

    This is what `scroll_into_view_if_needed` is not. That call is a teleport -
    the element arrives at the edge of the window in a single frame, wherever it
    was - and on a walk that reads a screenful of accounts it teleports BACKWARDS
    as often as forwards, because the handles are not met in the order they are
    laid out. What it looks like on screen is the feed lurching to one post,
    lurching to another far away, then lurching back: nothing a person reading a
    timeline ever does.

    Here the feed is instead NUDGED, as one continuous gesture, only as far as it
    takes to put this handle where a reader would be looking - and only when it
    is not there already. A handle on screen costs nothing; the next one down
    costs the few hundred pixels between them. The caller hands the handles over
    in page order, so those nudges add up to one steady movement down the feed.

    The cursor then travels to it over a run of moves rather than appearing on
    it, because a pointer that materialises on a link is not what a hover is.
    """
    height = _window_height(page)

    # Twice: the first step aims at where the handle was, and X reflowing the
    # column underneath a scroll can leave it short. The second is the correction,
    # and by then it is almost always already inside the band.
    for _ in range(2):
        box = _box(link)
        if box is None:
            return None
        middle = box["y"] + box["height"] / 2
        if height * HOVER_BAND[0] <= middle <= height * HOVER_BAND[1]:
            break
        step = int(middle - height * HOVER_REST)
        if abs(step) < HOVER_SLACK:
            break
        _scroll_by(page, step, speed=random.randint(*STEP_SPEED))
        # Long enough for the column to settle under the gesture before the next
        # read of where the row ended up - and no longer, since this is paid once
        # per account and adds straight onto the gap the bio trails the handle by.
        page.wait_for_timeout(random.randint(70, 140))

    box = _box(link)
    if box is None:
        return None
    at = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    try:
        page.mouse.move(at[0], at[1], steps=random.randint(8, 18))
    except Exception:                                # noqa: BLE001 - a gone row is not an error
        return None
    return at


def _pick(page, selector: str):
    """The copy of a handle the walk should go to: the first one it has not passed.

    A handle is on screen twice often enough to matter - the post and the repost
    of it, the byline and the follow rail beside it. `.first` is whichever the
    DOM happens to hold first, and on a feed being read downwards that is usually
    the copy ALREADY BEHIND the cursor, so going to it drags the page back up.
    The one wanted is the first at or below where the feed is now.
    """
    try:
        found = page.locator(selector)
        total = found.count()
    except Exception:                                # noqa: BLE001 - a recycled row is not an error
        return None
    if total <= 0:
        return None
    if total == 1:
        return found.first

    # Capped: a handle with a dozen copies on screen is a rail of them, and the
    # first few are enough to find one ahead without measuring the lot.
    behind = None
    for index in range(min(total, 8)):
        candidate = found.nth(index)
        box = _box(candidate)
        if box is None:
            continue
        if box["y"] + box["height"] >= 0:            # at or below the top of the window
            return candidate
        behind = behind or candidate
    return behind or found.first


def _box(link) -> Optional[dict]:
    """The row's rectangle in the window, or None if X has recycled it away."""
    try:
        return link.bounding_box(timeout=1500)
    except Exception:                                # noqa: BLE001 - a gone row is not an error
        return None


def _dismiss_card(page, at: Optional[tuple[float, float]] = None) -> None:
    """Move the cursor off, so the next handle gets a fresh card.

    Always, including after a hover that produced nothing: a card left showing is
    the one that the next `wait_for_selector` would match instantly, and the
    script's own owner check is the second line of defence rather than the first.

    Off means sideways into the left gutter, at the height the cursor is already
    at - not up to the corner of the window. The corner was a long diagonal back
    across the column, over every byline between here and the top; straight out
    to the side leaves the timeline in the shortest line there is.
    """
    try:
        if at is None:
            page.mouse.move(4, 4)
        else:
            page.mouse.move(random.randint(4, 40),
                            at[1] + random.randint(-8, 8),
                            steps=random.randint(4, 9))
        # Wait for the popup to go, but not for long: the next handle's card is
        # refused by the owner check even if this one is still fading, so a
        # generous wait here only pads out the gap the bio trails the handle by.
        page.wait_for_selector('[data-testid="HoverCard"]', state="hidden", timeout=500)
    except Exception:                                # noqa: BLE001 - the owner check covers it
        pass


# The same three shapes `HOVER_CARD_JS` stops the bio at, in Python. Kept in step
# with it deliberately: one is what reads a card off a live page, the other reads
# one that was stored, and they have to agree about where a bio ends.
_CARD_COUNT = re.compile(r"^[\d.,]+[KMB]?\s*(Followers?|Following)$", re.I)
_CARD_AFTER = re.compile(r"^(Followed by|Follows you|Joined |Click to |Profile Summary)", re.I)
_CARD_BUTTON = re.compile(
    r"^(Follow|Following|Pending|Follow back|Blocked|Unblock|Subscribe|Subscribed"
    r"|Message|Notify)$", re.I)


# A "bio" that is really the follow button's own label. X puts `Click to Follow
# someone` on the button as an accessibility label, and an older read of the list
# row picked it up as the account's description - so there are rows on disk whose
# bio is a sentence about a button. Caught by shape rather than cleaned once,
# because a one-off script fixes today and this fixes whatever turns up.
_JUNK_BIO = re.compile(
    # The button's accessibility label, whole.
    r"^click to (un)?follow\b"
    # Or the button's own word, and nothing else. ANCHORED: "Follow for daily
    # doses of @Naval's wisdom" is a real bio that begins with the same word,
    # and an unanchored `follow\b` throws it away.
    r"|^(follow|following|follows you|pending)$"
    # Or a single bare word - which is either the handle again or a fragment,
    # and says nothing either way.
    r"|^@?[\w]+$",
    re.I)


def is_junk_bio(bio: str, handle: str = "") -> bool:
    """Is this text a bio, or something that got written into the bio field.

    A bare handle counts: a row whose bio is its own @name says nothing, and it
    reads on screen as though the account described itself when it did not.
    """
    text = (bio or "").strip()
    if not text:
        return False
    if text.lstrip("@").lower() == (handle or "").strip().lstrip("@").lower():
        return True
    return bool(_JUNK_BIO.match(text))


def bio_from_card(card: str, handle: str) -> str:
    """The bio out of a hover card's text, for a card that was stored rather than
    just read.

    The console kept the whole popup verbatim from the beginning, but for a while
    it looked for the bio under a testid the card does not carry - so there are
    rosters on disk holding the text with no bio parsed out of it. This is how
    those are recovered without walking the feed again, and it is the fallback
    whenever a row has a card but no bio.

    The layout is the popup's: the follow button, the display name, the @handle,
    the bio, then the counts. So the bio is everything between the handle line
    and the first line that is a count, a "Followed by", or a button.
    """
    wanted = "@" + (handle or "").strip().lstrip("@").lower()
    lines = [line.strip() for line in (card or "").splitlines()]
    lines = [line for line in lines if line]

    try:
        at = next(i for i, line in enumerate(lines) if line.lower() == wanted)
    except StopIteration:
        return ""

    parts: list[str] = []
    for line in lines[at + 1:]:
        if _CARD_COUNT.match(line) or _CARD_AFTER.match(line) or _CARD_BUTTON.match(line):
            break
        parts.append(line)
    # Joined back up: a bio X wrapped over several lines is one bio, and the
    # newlines were the popup's rather than the author's.
    return " ".join(" ".join(parts).split())


def _apply_card(row: dict, card: Optional[dict]) -> dict:
    """Fold a hover card's reading into a collected row.

    The full popup text is kept whole in `card`; the bio falls back to the list
    cell's when the card had none, and the follower count is parsed off the card
    so the roster can be filtered on it without a profile load.
    """
    if not card:
        return row
    row["card"] = card.get("text") or ""
    row["name"] = row.get("name") or card.get("name") or ""
    row["bio"] = card.get("bio") or row.get("bio") or ""
    followers = _count(card.get("followers_text") or "")
    if followers is not None:
        row["followers"] = followers
    following = _count(card.get("following_text") or "")
    if following is not None:
        row["following"] = following
    return row


# -- Walking the home feed ----------------------------------------
def _back_to_top(page) -> None:
    """Send the feed back to the top so the walk can carry on.

    Pressing X's own Home item is what a hand would do, and it is the one action
    that both scrolls up and pulls in the posts that arrived while we were
    scrolling. A reload is the fallback, and it costs the feed's position - which
    is exactly what is wanted at this point anyway.
    """
    try:
        home = page.locator(f'[data-testid="{NAV_ACTIONS["home"]["testid"]}"]').first
        if home.count() > 0:
            home.click(timeout=6000)
            page.wait_for_timeout(1500)
            page.evaluate("window.scrollTo(0, 0)")
            return
    except Exception:                                # noqa: BLE001 - fall through to the reload
        pass
    try:
        goto(page, HOME_URL, timeout=60_000)
        page.wait_for_timeout(2000)
    except Exception:                                # noqa: BLE001 - the next pass will find out
        pass


# How far one pass travels, as a share of the window. A fraction rather than a
# number of pixels: 2200px is most of a laptop window and a third of a tall one,
# so a fixed figure either skips posts on a short screen or crawls on a big one.
# Kept under 1.0 so consecutive passes overlap - a pass that lands exactly one
# window on is a pass that can step over a post between two reads of the page.
GLIDE_SHARE = (0.72, 0.92)

# How fast the movement travels, in pixels per second. This is the "fast but
# smooth" dial: the gesture below is continuous whatever the number, and this
# only says how long it takes to cross the window.
GLIDE_SPEED = (1600, 2600)


def _cdp(page):
    """A DevTools session for this page, made once and kept.

    Cached in a weak map rather than on the page object: a page that closes
    should take its session with it, and nothing here should be the reason a
    closed page stays in memory.
    """
    found = _CDP_SESSIONS.get(page)
    if found is not None:
        return found
    session = page.context.new_cdp_session(page)
    _CDP_SESSIONS[page] = session
    return session


_CDP_SESSIONS: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


# The frame-by-frame fallback, for a page CDP will not open a session on. Driven
# by requestAnimationFrame so every frame gets its own position - which is what
# "smooth" means on screen - and eased at both ends so it starts and stops the
# way a hand does rather than snapping into motion.
SMOOTH_JS = r"""
([distance, duration]) => new Promise(resolve => {
  const from = window.scrollY;
  const start = performance.now();
  function frame(now) {
    const t = Math.min(1, (now - start) / duration);
    // ease-in-out cubic
    const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    window.scrollTo(0, from + distance * e);
    if (t < 1) requestAnimationFrame(frame);
    else resolve();
  }
  requestAnimationFrame(frame);
})
"""


def _window_height(page) -> int:
    """The window's height, with a sane figure for a page mid-navigation."""
    try:
        height = int(page.evaluate("window.innerHeight") or 0)
    except Exception:                                # noqa: BLE001 - a page mid-navigation
        height = 0
    return max(600, height or 900)


def _scroll_y(page) -> int:
    """How far down the page currently is. Zero when it cannot be asked."""
    try:
        return int(page.evaluate("window.scrollY") or 0)
    except Exception:                                # noqa: BLE001
        return 0


def _scroll_by(page, distance: int, *, speed: Optional[int] = None) -> float:
    """Move the feed `distance` pixels as ONE continuous movement.

    Returns roughly the seconds spent. Positive travels down the page.

    Chrome's own gesture synthesiser does the work: `Input.synthesizeScrollGesture`
    is what the browser uses to emulate a real scroll, and it emits a stream of
    events at the frame rate for the whole distance. That is the difference that
    matters here - a dozen `mouse.wheel` calls are a dozen JUMPS however small
    each one is, because each arrives as its own discrete event and the page
    repaints between them. One gesture is one movement.

    `speed` is the dial, in pixels per second, so "faster" does not mean "in
    bigger lurches" - the movement stays continuous and simply crosses the
    distance in less time.

    Falls back to a requestAnimationFrame scroll if no DevTools session can be
    had, which is smooth on screen but scripted rather than input-driven; and to
    a plain wheel if even that fails, which is the old jump and is better than
    not scrolling at all.
    """
    distance = int(distance)
    if not distance:
        return 0.0
    height = _window_height(page)
    speed = max(200, int(speed if speed is not None else random.randint(*GLIDE_SPEED)))
    seconds = abs(distance) / speed

    try:
        # Negative yDistance scrolls DOWN: the parameter is how far the content
        # travels, not how far the reader does.
        _cdp(page).send("Input.synthesizeScrollGesture", {
            "x": 400, "y": max(200, height // 2),
            "xDistance": 0, "yDistance": -distance,
            "speed": speed,
            # A mouse rather than a touch gesture: this is a desktop Chrome on a
            # desktop page, and a touch scroll on one is the odd thing to see.
            "gestureSourceType": "mouse",
            # No fling. A fling keeps travelling after the gesture ends, which
            # makes the distance something this cannot predict - and the walk
            # reads the page immediately afterwards.
            "preventFling": True,
        })
        return seconds
    except Exception:                                # noqa: BLE001 - fall through
        pass

    try:
        page.evaluate(SMOOTH_JS, [distance, int(seconds * 1000)])
        return seconds
    except Exception:                                # noqa: BLE001
        pass

    page.mouse.wheel(0, distance)
    return 0.0


def walk_home(page, *,
              pause: tuple[float, float] = (0.6, 2.4),
              limit: int = 0,
              refresh_after: int = 8,
              on_rows: Optional[Callable[[list[dict]], None]] = None,
              checkpoint: Optional[Callable[[float], None]] = None) -> list[dict]:
    """Scroll the signed-in account's home timeline and read every account on it.

    The walk does not stop on its own. A follower list has an end; a feed does
    not, and this is the console's only sweep - so it scrolls until the operator
    presses Stop, and `checkpoint` is what makes that press land between rows
    rather than at the end of some multi-minute wait.

    Every handle it has not met is hovered for the profile card X pops up, and
    the card's bio and follower count are folded into the row. That is not an
    option here: a timeline byline carries a name and nothing else, so without
    the hover the roster is a list of handles with no way to triage it.

    When several passes in a row turn up nothing new the feed is exhausted for
    now, so it goes back to the top for whatever has been posted since. That is
    what keeps "non-stop" from meaning "spinning at the bottom of a dead page".

    `limit` is a ceiling for a caller that wants one - zero, the default, means
    there is none. `on_rows` is called with everything collected SO FAR, which is
    what puts the table on screen while the walk is still running.
    """
    goto(page, HOME_URL, timeout=60_000)
    page.wait_for_timeout(2500)
    guard(page, "walking the home timeline")

    try:
        page.wait_for_selector('[data-testid="tweet"], [data-testid="UserCell"]', timeout=30_000)
    except Exception as err:                         # noqa: BLE001
        body = (page.inner_text("body") or "")[:400]
        raise AutomationError(
            f"Nothing loaded on {HOME_URL}. X answered with: "
            f"{' '.join(body.split())[:160] or 'an empty page'}"
        ) from err

    rows: dict[str, dict] = {}
    idle = 0

    def report() -> None:
        if on_rows:
            on_rows(list(rows.values()))

    while True:
        if checkpoint:
            checkpoint(0)
        before = len(rows)
        fresh: list[str] = []

        for row in page.evaluate(HOME_JS) or []:
            handle = (row.get("handle") or "").strip()
            if not handle or handle in rows:
                continue
            rows[handle] = {
                "handle": handle,
                "name": row.get("name") or "",
                "bio": row.get("bio") or "",
                # Neither of these is on a timeline row. The hover below fills
                # `followers` off the card; `dm_open` needs the profile itself,
                # so `Re-read accounts` is what answers it. Left unknown rather
                # than guessed - a fabricated "DMs open" is the one that costs a
                # send.
                "followers": None,
                "dm_open": None,
                "protected": bool(row.get("protected")),
            }
            fresh.append(handle)
            if limit and len(rows) >= limit:
                break

        # The handles go up FIRST, before a single card is read. The hover walk
        # is by far the slow part of a pass - seconds per account - and a panel
        # that shows nothing until it finishes reads exactly like a hung one.
        # The rows appear as bare handles and fill in underneath.
        report()

        # Now the cards, one row at a time and interruptibly: Stop has to bite
        # between accounts rather than at the end of the walk. Each is reported
        # as it lands, so the followers column fills in visibly rather than a
        # screenful at a time.
        #
        # `fresh` is in page order, top to bottom, and each hover nudges the feed
        # forward only as far as its own row - so this loop IS the movement down
        # the screen. That is the whole shape of the pass: hover, ease on to the
        # next one, hover. Nothing jumps and nothing doubles back.
        was = _scroll_y(page)
        for handle in fresh:
            if checkpoint:
                checkpoint(0)
            _apply_card(rows[handle], hover_card(page, handle))
            report()
        # How far reading the accounts carried the feed by itself.
        travelled = max(0, _scroll_y(page) - was)
        if limit and len(rows) >= limit:
            break

        idle = 0 if len(rows) > before else idle + 1
        if idle >= refresh_after:
            idle = 0
            log("scrape", "the feed stopped giving new accounts - going back to the top")
            _back_to_top(page)
            if checkpoint:
                checkpoint(2.0)
            else:
                time.sleep(2.0)
            guard(page, "walking the home timeline")
            continue

        # A pass covers most of a window, and the hover walk has already covered
        # part of it - so only the REMAINDER is scrolled here. Gliding a full
        # window on top of a screenful of hovering is what used to carry the feed
        # past posts nobody had read, which is then what made the walk look like
        # it was skipping ahead and coming back.
        if checkpoint:
            checkpoint(0)
        left = int(_window_height(page) * random.uniform(*GLIDE_SHARE)) - travelled
        spent = _scroll_by(page, left) if left > HOVER_SLACK else 0.0

        # The pause is rolled, not fixed. A scroll every 1.00s for four hundred
        # rows is a metronome, and a metronome is the easiest thing to spot.
        #
        # The movement is paid for OUT of that pause rather than added to it: it
        # is time the walk was going to spend waiting anyway, so smoothness costs
        # nothing and the pass ends when the setting says it should. A pause
        # shorter than the movement simply means no dead time left.
        wait = max(0.0, random.uniform(*sorted(pause)) - spent)
        if wait:
            if checkpoint:
                checkpoint(wait)
            else:
                time.sleep(wait)
        guard(page, "walking the home timeline")

    return list(rows.values())


# -- Reading one profile ------------------------------------------
PROFILE_JS = """
() => {
  const text = sel => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || '').trim() : '';
  };
  const followers = document.querySelector('a[href$="/followers"], a[href$="/verified_followers"]');
  return {
    name: (text('[data-testid="UserName"]').split('\\n')[0] || ''),
    bio: text('[data-testid="UserDescription"]'),
    followers_text: followers ? (followers.innerText || '').trim() : '',
    dm_open: !!document.querySelector('[data-testid="sendDMFromProfile"]'),
    protected: !!document.querySelector('svg[aria-label*="rotected"]')
      || /These posts are protected/i.test(document.body.innerText || ''),
    missing: /This account doesn.t exist|Account suspended/i.test(document.body.innerText || ''),
  };
}
"""


def read_profile(page, handle: str, template: str = "https://x.com/{handle}") -> dict[str, Any]:
    """Open a profile for the two facts a list row does not carry.

    The follower count and whether the inbox is open. Both are worth the extra
    page load: the count is what the roster is filtered on, and a closed inbox
    that is only discovered at send time has already spent a slot out of the
    day's budget.

    Through Firecrawl when it is configured, and off a window when it is not. A
    Firecrawl read reaches the public facts - name, bio, followers, protected,
    exists - without touching a signed-in session, which on five hundred profiles
    is five hundred navigations this console never has to make X watch one of its
    accounts perform. It cannot see the inbox, so `dm_open` comes back None from
    there and the caller keeps whatever it already knew. If Firecrawl is reachable
    but this one read failed, and there is a window to fall back to, the window
    reads it; with no window (the re-read runs off no account when Firecrawl is
    on) the failure is raised for the caller's own per-profile catch.
    """
    if firecrawl.enabled():
        try:
            return firecrawl.profile(handle, profile_url(handle, template))
        except Exception as err:                     # noqa: BLE001 - fall back to a window if there is one
            if page is None:
                raise
            log("firecrawl", f"@{handle} via Firecrawl failed ({err}); "
                             "reading it off the window instead", "warn")

    if page is None:                                 # nothing to read with
        raise AutomationError(
            "No browser window and no Firecrawl configured to read a profile.")

    goto(page, profile_url(handle, template))
    page.wait_for_timeout(1600)
    guard(page, f"reading @{handle}")

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
        "protected": bool(found.get("protected")),
        "missing": False,
    }


def open_profiles(context, handles: Iterable[str], template: str = "https://x.com/{handle}",
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
            log("browser", f"could not open @{handle}", "warn")
        opened += 1
        if checkpoint:
            checkpoint(random.uniform(0.4, 1.1))
    return opened


# -- Sending -------------------------------------------------------
class DmClosed(AutomationError):
    """The composer could not be opened at all.

    Its own type because it is the one failure worth naming on the row: it is the
    difference between "X refused" and "this account was never reachable".
    """


def _dm_button(page, *, timeout: int = 6_000):
    """The Message button on a profile, however this build of X draws it.

    Three goes, cheapest and most specific first. Each is a different KIND of
    evidence - a testid, an accessible name, a drawing - so a build that has
    dropped one is unlikely to have dropped all three.

    WAITED FOR, not glanced at. X renders the profile's action row with its own
    script a moment after the navigation settles, so a single look the instant
    the URL changes finds nothing on a profile that has the button perfectly well
    - and the caller reads that as "no chat" and skips a reachable recipient. The
    three shapes are retried until the button turns up or the timeout is spent;
    a profile that genuinely has no chat costs the whole timeout exactly once.
    """
    deadline = time.monotonic() + max(0, timeout) / 1000
    while True:
        try:
            found = page.locator(DM_BUTTON)
            if found.count():
                return found.first

            found = page.locator(DM_BUTTON_ARIA)
            if found.count():
                return found.first

            element = page.evaluate_handle(DM_BUTTON_JS, DM_ICON_PATH).as_element()
            if element is not None:
                return element
        except Exception:                            # noqa: BLE001 - a page mid-render
            pass

        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def _profile_loaded(page) -> bool:
    """Did the profile itself come up.

    The difference between "this account has DMs off" and "the page never
    rendered" is the difference between skipping one recipient and stopping the
    run - so it is worth a second look before blaming the account.
    """
    for selector in ('[data-testid$="-follow"]', '[data-testid$="-unfollow"]',
                     '[data-testid="userActions"]', '[data-testid="UserName"]'):
        try:
            if page.locator(selector).count():
                return True
        except Exception:                            # noqa: BLE001
            continue
    return False


def _pick_typeahead(page, handle: str):
    """The search result that is actually this handle, not one that starts like it.

    A search for `naval` returns `naval`, `navalny`, `naval_ravikant` in one
    list, and clicking the first would message the wrong person - silently, and
    with no unsend. So a row is taken only when its @handle matches exactly; the
    one exception is a search that returned a single row, which is unambiguous
    whatever its text says.
    """
    wanted = handle.lstrip("@").lower()
    rows = page.locator(DM_TYPEAHEAD)
    try:
        total = rows.count()
    except Exception:                                # noqa: BLE001
        return None
    if total <= 0:
        return None
    for index in range(min(total, 12)):
        row = rows.nth(index)
        try:
            text = (row.inner_text(timeout=1000) or "").lower()
        except Exception:                            # noqa: BLE001
            continue
        if re.search(r"(^|\s|@)" + re.escape(wanted) + r"\b", text):
            # The @handle is the exact word, not a prefix of a longer one.
            if re.search(r"@" + re.escape(wanted) + r"(\b|$)", text):
                return row
    return rows.first if total == 1 else None


def _open_composer_via_search(page, handle: str) -> None:
    """Open a DM from Messages: New message, search, pick, Next.

    Raises a plain `AutomationError` for anything that stops the flow, rather
    than `DmClosed` - the caller falls back to the profile button on a failure
    here, and the profile flow is what decides whether DMs are genuinely closed.
    A "could not find them in search" is not the same fact as "their inbox is
    shut", and only one of them is worth writing onto the row.
    """
    goto(page, DM_INBOX_URL, timeout=45_000)
    page.wait_for_timeout(800)
    guard(page, f"messaging @{handle}")

    opener = page.locator(DM_NEW_BUTTON)
    try:
        opener.first.wait_for(state="visible", timeout=DM_STEP_TIMEOUT)
    except Exception as err:                         # noqa: BLE001
        raise AutomationError("the New message button never appeared") from err
    opener.first.click()
    page.wait_for_timeout(500)

    box = page.locator(DM_SEARCH_INPUT)
    try:
        box.first.wait_for(state="visible", timeout=DM_STEP_TIMEOUT)
    except Exception as err:                         # noqa: BLE001
        raise AutomationError("the DM search box did not open") from err
    box.first.click()
    # Typed rather than filled: the typeahead is driven by keystrokes, and a
    # value set in one shot can leave the results empty.
    box.first.fill("")
    _type_human(page, DM_SEARCH_INPUT, handle.lstrip("@"))
    page.wait_for_timeout(1000)

    row = _pick_typeahead(page, handle)
    if row is None:
        raise AutomationError(f"@{handle} did not come up in the DM search")
    row.click()
    page.wait_for_timeout(400)

    # The Next button is there when starting a NEW conversation and absent when
    # the search jumped straight into an existing one - so it is pressed if
    # present and its absence is not an error.
    nxt = page.locator(DM_NEXT)
    try:
        if nxt.count() and nxt.first.is_visible():
            nxt.first.click()
            page.wait_for_timeout(400)
    except Exception:                                # noqa: BLE001 - the composer wait is the real test
        pass

    try:
        page.wait_for_selector(DM_INPUT_ANY, timeout=DM_COMPOSER_TIMEOUT)
    except Exception as err:                         # noqa: BLE001
        raise AutomationError("the composer did not open from search") from err


def _open_composer(page, handle: str, *, via_search: bool = True,
                   template: str = "https://x.com/{handle}") -> None:
    """Open a DM composer for `handle`, search flow first, profile button after.

    The search flow is the one an operator recognises and the steadier of the
    two on current X; the profile button is the fallback. A `DmClosed` out of the
    profile flow is final - both paths open the same inbox, so if the button says
    it is shut, searching would only find the same shut inbox - and is raised
    rather than retried.
    """
    try:
        if via_search:
            try:
                _open_composer_via_search(page, handle)
                return
            except DmClosed:
                raise
            except Exception as err:                 # noqa: BLE001 - the button is the standby
                log("dm", f"@{handle}: search DM flow did not open "
                          f"({err}); trying the profile button", "warn")
        _open_composer_via_profile(page, handle, template=template)
    except (DmClosed, AutomationError):
        # A "verify to continue" prompt, a "you can't message this account" note,
        # or a half-open menu can be left sitting over the page. Dismiss it here
        # so the next recipient's navigation is not blocked by this one's popup -
        # which is what turned one unreachable account into a run that jammed.
        try:
            close_overlay(page)
        except Exception:                            # noqa: BLE001 - best effort on the way out
            pass
        raise


def _open_composer_via_profile(page, handle: str,
                               template: str = "https://x.com/{handle}") -> None:
    """Press Message on the profile, and wait for the box to type into.

    Raises `DmClosed` for anything that is about this recipient - no Message
    button, or a composer that never appeared - because those are reasons to
    move on to the next handle rather than to stop.

    Assumes the page is already on the profile: the search flow above navigates
    to Messages, so on the fallback the profile has to be opened first. `send_dm`
    lands here on the profile; the direct caller does the same.
    """
    if "/messages" in (page.url or "") or f"/{handle.lstrip('@')}" not in (page.url or ""):
        # The search flow left the window in Messages, or nothing put it on the
        # profile at all. Open the profile the button lives on before looking for
        # the button.
        goto(page, profile_url(handle, template))
        page.wait_for_timeout(800)
        guard(page, f"messaging @{handle}")

    button = _dm_button(page)
    if button is None:
        if not _profile_loaded(page):
            # Not the account's fault, and not something the next recipient will
            # do any better, so it is worth saying plainly.
            raise AutomationError(f"@{handle}'s profile did not load")
        raise DmClosed("DMs are closed")

    button.click()
    page.wait_for_timeout(1000)

    try:
        page.wait_for_selector(DM_INPUT_ANY, timeout=DM_COMPOSER_TIMEOUT)
    except Exception as err:                         # noqa: BLE001
        # Say WHERE it got to. "The composer did not open" on its own is a dead
        # end for whoever reads the feed; the URL separates "the click did
        # nothing and we are still on the profile" from "the message view opened
        # and the box inside it is drawn some other way".
        where = ""
        try:
            where = page.url
        except Exception:                            # noqa: BLE001
            pass
        on_messages = "/messages" in (where or "")
        raise DmClosed(
            "the message view opened but the text box was not found"
            if on_messages else
            "the composer did not open"
        ) from err


# -- Opening a chat, and saying whether there was one --------------
def open_chat(page, handle: str, *, template: str = "https://x.com/{handle}",
              follow_first: bool = False, via_search: bool = False,
              settle: float = 2.0) -> bool:
    """Open the recipient's profile and, if it offers a chat, open it.

    Returns True with the composer up and ready to type into, and False when the
    profile simply has no chat button. That False is a FACT ABOUT THE RECIPIENT,
    not a failure of the run: half a cold list has DMs shut, and the campaign
    table exists to show which half - so this answers the question rather than
    raising, and the caller writes "no chat" on the row and moves straight on.

    Only a profile that would not load at all raises, because that is the run's
    problem rather than this account's and the next recipient will not fix it.
    """
    goto(page, profile_url(handle, template))
    # Wait for the PROFILE to be there, not for a fixed number of milliseconds.
    # A flat sleep is wrong in both directions: too short on the first few
    # handles of a run, when nothing is warm yet - which is what made a run skip
    # the accounts it opened first - and wasted time on every profile after that.
    try:
        page.wait_for_selector(PROFILE_READY, timeout=15_000)
    except Exception:                                # noqa: BLE001 - _dm_button still gets its go
        pass
    page.wait_for_timeout(300)
    guard(page, f"opening the chat with @{handle}")

    if follow_first:
        try:
            follow = page.locator('[data-testid$="-follow"]')
            if follow.count() > 0:
                follow.first.click()
                page.wait_for_timeout(random.uniform(900, 2200))
        except Exception:                            # noqa: BLE001 - not worth failing the send
            pass

    button = _dm_button(page)
    if button is None:
        if not _profile_loaded(page):
            raise AutomationError(f"@{handle}'s profile did not load")
        if via_search:
            # The profile shows no button, but the inbox may still take a message
            # - some builds only offer it from Messages. Tried only when asked
            # for, because it costs a second navigation on every shut inbox.
            try:
                _open_composer_via_search(page, handle)
                page.wait_for_timeout(int(max(0.0, settle) * 1000))
                return True
            except Exception:                        # noqa: BLE001 - then there is no chat
                pass
        return False

    button.click()
    page.wait_for_timeout(900)
    try:
        page.wait_for_selector(DM_INPUT_ANY, timeout=DM_COMPOSER_TIMEOUT)
    except Exception:                                # noqa: BLE001
        # The button was there and the box never came - a verify prompt, or a
        # "you can't message this account" in its place. Clear whatever popped up
        # so it cannot block the next recipient, and report it as no chat.
        try:
            close_overlay(page)
        except Exception:                            # noqa: BLE001
            pass
        return False

    # Left on screen a moment before the run moves on, so the chat that just
    # opened can actually be seen rather than flashing past.
    page.wait_for_timeout(int(max(0.0, settle) * 1000))
    return True


def write_dm(page, text: str, *, media: Optional[list[str]] = None,
             typing_wpm: int = 240, click_send: bool = True) -> str:
    """Type into a composer that is ALREADY open, and send it if Click Send is on.

    The other half of `open_chat`. Split from it because the campaign has to
    report "there was a chat" separately from "the message went into it" - and
    with no draft to write, opening the chat is the whole of the work.
    """
    if media:
        try:
            page.set_input_files(DM_FILE, media)
            # Attachments upload before the send button will take them. The wait
            # is on the thumbnail, not a fixed sleep, because a 4MB image on a
            # slow line takes longer than any number picked in advance.
            page.wait_for_selector('[data-testid="attachments"], [aria-label*="Remove media" i]',
                                   timeout=60_000)
        except Exception as err:                     # noqa: BLE001
            raise AutomationError(f"could not attach the image: {err}") from err

    page.locator(DM_INPUT_ANY).first.click()
    _type_text(page, text, typing_wpm)
    page.wait_for_timeout(random.uniform(600, 1600))
    guard(page, "writing the message")

    if not click_send:
        return "typed"

    if page.locator(DM_SEND).count() == 0:
        raise AutomationError("the send button never became available")
    page.locator(DM_SEND).first.click()
    page.wait_for_timeout(2200)
    guard(page, "sending the message")
    return "sent"


def send_dm(page, handle: str, text: str, *, media: Optional[list[str]] = None,
            typing_wpm: int = 240, click_send: bool = True, follow_first: bool = False,
            via_search: bool = True,
            template: str = "https://x.com/{handle}") -> str:
    """Type a DM into the composer, and send it if Click Send is on.

    Returns "sent" or "typed". With Click Send off the composer is left filled
    and unsent - that is the setting's whole purpose, and it is the first run's
    only safe shape, because there is no unsend.
    """
    # The profile is only opened first when something on it is needed: the follow
    # that has to happen before the DM, or the profile-button path that opens the
    # composer FROM the profile. When the composer is opened by search and there
    # is no follow to do, the run never visits the profile at all.
    if follow_first or not via_search:
        goto(page, profile_url(handle, template))
        page.wait_for_timeout(900)
        guard(page, f"messaging @{handle}")

        if follow_first:
            try:
                follow = page.locator('[data-testid$="-follow"]')
                if follow.count() > 0:
                    follow.first.click()
                    page.wait_for_timeout(random.uniform(900, 2200))
            except Exception:                        # noqa: BLE001 - not worth failing the send
                pass

    _open_composer(page, handle, via_search=via_search, template=template)

    if media:
        try:
            page.set_input_files(DM_FILE, media)
            # Attachments upload before the send button will take them. The wait
            # is on the thumbnail, not a fixed sleep, because a 4MB image on a
            # slow line takes longer than any number picked in advance.
            page.wait_for_selector('[data-testid="attachments"], [aria-label*="Remove media" i]',
                                   timeout=60_000)
        except Exception as err:                     # noqa: BLE001
            raise AutomationError(f"could not attach the image: {err}") from err

    page.locator(DM_INPUT_ANY).first.click()
    _type_text(page, text, typing_wpm)
    page.wait_for_timeout(random.uniform(600, 1600))
    guard(page, f"messaging @{handle}")

    if not click_send:
        return "typed"

    if page.locator(DM_SEND).count() == 0:
        raise AutomationError("the send button never became available")
    page.locator(DM_SEND).first.click()
    page.wait_for_timeout(2200)
    guard(page, f"messaging @{handle}")
    return "sent"


def render(text: str, row: dict[str, Any]) -> str:
    """Fill a draft's placeholders.

    `{name}` falls back to the handle rather than leaving a hole: a DM opening
    with "hey ," is worse than one that uses the @ - it reads as a template that
    misfired, which is exactly what it is.
    """
    handle = str(row.get("handle") or "").lstrip("@")
    name = str(row.get("name") or "").strip() or handle
    first = name.split(" ")[0] if name else handle
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


def _type_text(page, text: str, wpm: int) -> None:
    """Key by key, and Shift+Enter for a line break.

    A plain Enter in X's composer sends the message, so a two-paragraph draft
    typed naively would send its first paragraph on its own. `fill()` avoids
    both problems and creates a worse one: it sets the value without firing a
    single keydown, which no person has ever done.
    """
    base = _delay_for(wpm)
    for line_no, line in enumerate((text or "").split("\n")):
        if line_no:
            page.keyboard.press("Shift+Enter")
            page.wait_for_timeout(random.uniform(80, 220))
        for char in line:
            page.keyboard.type(char, delay=0)
            # Jittered per key. A perfectly even 50ms cadence is not a typist.
            page.wait_for_timeout(base * random.uniform(0.55, 1.75))


def _type_human(page, selector: str, value: str, wpm: int = 260) -> None:
    field = page.locator(selector).first
    field.click()
    field.type(value, delay=_delay_for(wpm))


def _click_text(page, label: str) -> None:
    """Click a button by what it says.

    The login flow's buttons have no testids, and their class names are
    generated. The visible word is the most stable handle on them.
    """
    button = page.locator(f'button:has-text("{label}"), div[role="button"]:has-text("{label}")')
    if button.count() == 0:
        raise AutomationError(f'X did not show a "{label}" button where one was expected.')
    button.first.click()


# -- Odds and ends -------------------------------------------------
def _count(text: str) -> Optional[int]:
    """"12.3K Followers" -> 12300.

    Returns None rather than 0 when there is no number to read. Zero is a real
    follower count and the roster filters on it; "unknown" has to survive as
    something else.
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
