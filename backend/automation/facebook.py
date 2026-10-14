"""Facebook itself — the feed, and walking down it.

A file of its own for the same reason `messenger.py` and `x.py` are files of
their own: a surface is its selectors, and facebook.com's are not
messenger.com's. The two share a browser window and a login, and nothing else.

What is here is deliberately small. This is not the sweep — it reads nothing off
the feed and saves nothing — it is the scroll: put the feed on screen, and then
move down it at a human speed for as long as it is asked to. `panels/fb.py` is
what decides how long.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional

from ..events import log
from .browser import AutomationError, goto

HOME_URL = "https://www.facebook.com/"

# What being ON the feed looks like, cheapest first. `[role="feed"]` is
# Facebook's own label for the column of posts and is the one worth having; the
# composer is the fallback, because a feed with nothing in it yet still has the
# "What's on your mind?" box at the top of it.
FEED = ('div[role="feed"], '
        '[role="main"] [aria-posinset], '
        '[role="main"] [role="article"], '
        '[role="main"] [aria-label*="Create a post" i]')

# The login wall, which is what an unsigned-in window answers with instead. Told
# apart from a slow feed on purpose: they look identical from a poll - no feed
# on screen - and they need opposite things from the operator.
LOGIN = ('form[action*="login" i], '
         'input[name="pass"], '
         '[data-testid="royal_login_form"]')

# The captcha Facebook serves INSIDE an otherwise ordinary-looking page. It has
# no address of its own to give it away - which is why the URL half of
# `challenge_showing` cannot catch it and this exists.
#
# Deliberately iframes and labels only. An `[id*="captcha" i]` sweep was the
# obvious third clause and is left out twice over: it walks the whole of
# Facebook's DOM on every poll, and it matches the hidden scaffolding that sits
# on pages with no captcha on them at all.
CAPTCHA = ('iframe[src*="arkose" i], iframe[src*="funcaptcha" i], '
           'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], '
           'iframe[title*="captcha" i], [aria-label*="captcha" i]')


def signed_out(page) -> bool:
    try:
        return page.locator(LOGIN).count() > 0
    except Exception:                                # noqa: BLE001 - mid-render
        return False


def challenge_showing(page) -> Optional[str]:
    """Name what Facebook is asking for, or None if it is asking for nothing.

    `messenger.py` has had this since the pooled campaign needed it; this file
    never got it, and that is the whole of the bug it fixes. `settle` knew two
    endings - the feed, or the login wall - and a checkpoint is neither: it has
    no `role="feed"` and no password box, so it fell through both checks and was
    reported as "the feed has not appeared" after the full wait. The operator
    was told the page was slow when what was on screen was a captcha waiting for
    them.

    Read off the URL first for the same reason `messenger.py` does: the address
    is the one piece of evidence about this screen that no Facebook build can
    rename, and it costs nothing to look at.
    """
    try:
        url = page.url or ""
        if "/checkpoint" in url:
            return "the account is checkpointed - clear it in this window"
        if "two_factor" in url or "/authenticate" in url:
            return "Facebook is asking for a two-factor code"
        if "/recover" in url or "/disabled" in url:
            return "Facebook has put this account into recovery"
        if page.locator(CAPTCHA).count() > 0:
            return "Facebook is showing a captcha - solve it in this window"
    except Exception:                                # noqa: BLE001 - a page mid-navigation
        return None
    return None


def feed_showing(page) -> bool:
    try:
        return page.locator(FEED).count() > 0
    except Exception:                                # noqa: BLE001 - mid-render
        return False


def settle(page, *, timeout: int = 15_000) -> str:
    """Wait for whatever this window is going to become, and name it.

    Separate from `open_home` because the two callers arrive differently. The
    panel's Open button gets here through `open_account`, which has ALREADY
    navigated - calling `open_home` there would load facebook.com a second time
    for nothing, and the operator would watch the page they asked for be thrown
    away and fetched again.

    Answers rather than raises when the window is signed out, or challenged.
    Opening Facebook on a profile that has not logged in yet is a perfectly
    ordinary thing to do - it is how you log in - and so is being handed a
    captcha. Both are reported, not failed.

    Polled rather than one blocking `wait_for_selector(FEED)`, which is what
    made a challenged window look like a hung one. That call can only wait for
    the feed, so a checkpoint sat behind the WHOLE timeout before anything was
    said about it - the operator watched a spinner for fifteen seconds with the
    captcha already on screen in front of them. Now every ending is checked on
    the same tick and the first one to be true is the answer, so a captcha is
    named in well under a second.
    """
    deadline = time.monotonic() + timeout / 1000.0
    while True:
        # Challenge first: it is the most specific reading of a page with no
        # feed on it, and the only one the operator has to act on themselves.
        found = challenge_showing(page)
        if found:
            return found
        if feed_showing(page):
            return "the feed is up"
        if signed_out(page):
            return "facebook is asking this profile to log in"
        if time.monotonic() >= deadline:
            return "facebook opened, but the feed has not appeared"
        page.wait_for_timeout(250)


def open_home(page, *, timeout: int = 60_000) -> str:
    """Navigate this window to facebook.com, then `settle`.

    For a caller that does not know where the window is pointing - the walk,
    which may be starting on a tab left on Messenger.
    """
    goto(page, HOME_URL, timeout=timeout)
    return settle(page)


# Facebook scrolls the WINDOW, not an inner div - which is the opposite of
# Messenger's chat list, and the reason this is a different few lines rather
# than an import. `scrollingElement` is the honest way to say "whatever this
# document actually scrolls", and it is right on both a normal feed and the
# rare layout that puts the column in a container of its own.
#
# The scroller lives IN THE PAGE. It used to be a slice: one blocking
# `evaluate` that held a constant velocity for 900ms and handed back, and the
# walk was the loop around it. That was fine for one tab and wrong for five:
# all of an account's tabs share one browser thread, so five tabs meant five
# slices in turn - one tab moving, four standing still - which is not five
# tabs scrolling, it is one tab scrolling five feeds slowly.
#
# So the loop is in the page now. One call installs it, and after that the tab
# keeps moving on its own; every later call is a control message - speed,
# stop, go, a nudge - and a reading back of how far it has come. Five tabs
# each carry their own, and all five move at once while the Python side does
# nothing but look for posts and comment.
#
# Three things make it smooth rather than merely small jumps:
#
#   It is driven by `requestAnimationFrame` while the tab is in front, so a
#   step lands once per frame, in step with the compositor. A timer at some
#   interval of our own choosing beats against the refresh rate and judders.
#
#   The distance is `speed * dt` from the clock rather than a fixed amount per
#   frame. A frame the browser was late delivering therefore moves further,
#   which is what keeps the SPEED constant instead of the frame rate. Facebook
#   drops frames constantly as it renders posts in behind you.
#
#   The position is carried in a float. `scrollTop` reports back rounded to
#   device pixels, so a read-add-write cycle at anything under ~1px/frame
#   rounds its own increment away every frame and the page never moves at all.
#   `carried` accumulates the true position and only resyncs when the page
#   genuinely disagrees.
#
# A tab that is NOT in front gets no animation frames at all - the browser
# stops them for hidden documents - so there the loop runs on a timer instead,
# and swaps back to frames when the tab comes forward. Nobody is watching a
# hidden tab, so its steps may be coarse; what matters is that it moves. The
# switch is watched two ways: `visibilitychange` re-arms a pending frame as a
# timer, and every control call checks the loop is alive and restarts it if
# the swap was missed. Chrome's own timer throttling for hidden tabs is off -
# Playwright launches with `--disable-background-timer-throttling`.
#
# Facebook scrolls the WINDOW, not an inner div - which is the opposite of
# Messenger's chat list, and the reason this is a different few lines rather
# than an import. `scrollingElement` is the honest way to say "whatever this
# document actually scrolls".
SCROLLER_JS = r"""
(opts) => {
  const target = document.scrollingElement || document.documentElement;
  let s = window.__xbotScroll;
  if (!s) {
    s = window.__xbotScroll = {
      speed: 0, running: false, carried: target.scrollTop, last: performance.now(),
      nudge: 0, from: target.scrollTop, raf: 0, timer: 0,
      steps: 0, restarts: 0,     // for the probe: is the loop actually running?
    };
    const step = (now) => {
      // Whichever of the two fired, the other is cancelled - see `schedule`.
      if (s.raf) cancelAnimationFrame(s.raf);
      if (s.timer) clearTimeout(s.timer);
      s.raf = 0; s.timer = 0; s.steps++;
      now = (typeof now === 'number') ? now : performance.now();
      // dt is clamped, but loosely: three seconds. Facebook's main thread is
      // busy laying posts in behind the scroll, and in a tab at the back it is
      // starved outright, so steps arrive late - measured at 10 a second in a
      // hidden tab against 60 in front, with the odd two-second hole. A tight
      // clamp threw that time away and a hidden tab fell to two thirds of its
      // speed; a loose one catches up in the next step. The cost is a jump
      // after a stall, in a tab nobody was looking at - or a tab in front that
      // had visibly frozen for those seconds anyway.
      const dt = Math.min((now - s.last) / 1000, 3.0);
      s.last = now;
      if (s.running) {
        // The page moved under us - Facebook spliced a post in above, or the
        // bottom was hit. The float is now lying about where we are.
        if (Math.abs(target.scrollTop - s.carried) > 2) s.carried = target.scrollTop;
        if (s.nudge > 0) {
          // A nudge: back up a little, at three times the speed so it is
          // over quickly, then carry on down. What a person does at the
          // bottom of a feed that has not loaded - and the scroll events are
          // what wake the loader.
          const up = Math.min(s.nudge, s.speed * 3 * dt);
          s.nudge -= up;
          s.carried -= up;
        } else {
          s.carried += s.speed * dt;
        }
        s.carried = Math.max(0, s.carried);
        target.scrollTop = s.carried;
      }
      schedule();
    };
    const schedule = () => {
      // BOTH a frame and a timer, every step; the first to fire wins and the
      // step cancels the other. In a tab in front the frame comes first
      // (16ms at 60Hz) and the motion is frame-locked and smooth; in a tab at
      // the back - or a window Chrome has decided is covered, where it hands
      // out one frame a second and `document.hidden` still reads false, as
      // measured - the timer carries it at 25 steps a second instead. No
      // guessing about visibility, and nothing to miss when it changes.
      if (s.raf || s.timer) return;
      s.raf = requestAnimationFrame(step);
      s.timer = setTimeout(step, 40);
    };
    s.schedule = schedule;
    schedule();
  }

  if (typeof opts.speed === 'number') s.speed = opts.speed;
  if (typeof opts.running === 'boolean') {
    // Resuming from where the page IS, not where the float left off: the
    // comment that paused it may have scrolled the page, or a modal closed.
    if (opts.running && !s.running) { s.carried = target.scrollTop; s.last = performance.now(); }
    s.running = opts.running;
  }
  if (opts.nudge) s.nudge += opts.nudge;

  // Alive? A loop whose last step is long past has lost its frame and timer
  // both - or its renderer has been starved that long. Re-arm it. `last` is
  // deliberately NOT reset: the next step charges the stall to the clock, up
  // to the clamp, instead of writing the time off.
  if (performance.now() - s.last > 1500) {
    s.restarts++; s.raf = 0; s.timer = 0; s.schedule();
  }

  const out = {
    moved: Math.round(target.scrollTop - s.from),
    at: Math.round(target.scrollTop),
    height: target.scrollHeight,
    posts: document.querySelectorAll(opts.post).length,
    running: s.running,
    steps: s.steps, restarts: s.restarts, hidden: document.hidden,
  };
  s.from = target.scrollTop;
  return out;
}
"""


# -- Commenting ----------------------------------------------------
# A post. The same selector `posts_on_screen` and the scroll count use, kept as
# one name so the thing that is counted and the thing that is commented on
# cannot drift.
#
# `aria-posinset` first, because that is what a feed post IS on the current
# build: each unit in the home feed is a `div[aria-posinset="N"]` with an
# `aria-labelledby`, and there is no `role="feed"` column around them any more
# and no `role="article"` on them. Measured on 2026-09-10 - a page with nine
# posts on it had exactly two `role="article"` elements, both 286px blocks far
# below the fold, which is why every walk reported "0 eligible ... 2 on the
# page: 2 not on screen" and never got as far as the comment icon.
#
# The article spellings are kept behind it for the builds and surfaces (groups,
# pages, the older feed) that still use them. A post carrying both is handled by
# the nested filter in `CLAIM_POST_JS`: the inner one is dropped.
POST = ('[role="main"] [aria-posinset], '
        '[role="feed"] [role="article"], [role="main"] [role="article"]')

# Facebook labels these by what they DO, in the viewer's language, and the label
# is the only stable handle on them - the class names are generated per build.
# `aria-label` is matched case-insensitively and by substring because the real
# values vary ("Comment", "Leave a comment", "Write a comment").
# Facebook's own marker on the Comment control, and the one worth having: it is
# a data attribute the build puts there deliberately, where the class names next
# to it (`x9f619 x1n2onr6 x1ja2u2z …`) are generated per deploy and worthless.
#
# It marks an EMPTY div INSIDE the button rather than the button:
#
#   <div class="x9f619 …">                                  <- the real button
#     <i data-visualcompletion="css-img" …></i>              <- the bubble icon
#     <div data-ad-rendering-role="comment_button" …></div>  <- this, empty
#   </div>
#
# So it is a handle to find, not a thing to click, and `_comment_button` walks
# up from it. This is why matching on `aria-label` found nothing: the control
# carries no label at all, and the icon is a sprite background rather than text,
# so there is no accessible name on it to match either.
COMMENT_MARKER = '[data-ad-rendering-role="comment_button"]'

# The comment bubble ITSELF, and the handle that actually works on an ordinary
# post. Read the name of the attribute above again: `data-AD-rendering-role`.
# Facebook puts it on the posts it renders as ads, and an ordinary post in your
# own feed has no such attribute anywhere on it - so keying on it found the
# button on sponsored posts and nothing at all on the rest of the feed, which is
# a walk that scrolls past every real post in silence.
#
# The icon is a CSS sprite: one image for the whole action bar, positioned to
# show one glyph. Like, comment and share are the same file at different
# offsets, so the OFFSET is the identity:
#
#   <i data-visualcompletion="css-img"
#      style="background-image: url(.../I_rhPL68mZl.webp);
#             background-position: 0px -231px;      <- the comment bubble
#             width: 20px; height: 20px">
#
# Matched on the inline style, which is where Facebook writes it. The offsets
# move when Facebook reissues the sprite sheet, so this is a list and a setting
# rather than one hardcoded number - and `survey` reports every offset it saw on
# the page, so when it does move the new value is in the probe output rather
# than something to go hunting for in devtools.
COMMENT_ICON = 'i[data-visualcompletion="css-img"]'
COMMENT_ICON_OFFSETS = ("0px -231px",)

# The labelled spelling, for the builds and locales that do give it a name.
# Kept as the fallback rather than the route - see above.
COMMENT_LABELLED = ('[role="button"][aria-label*="comment" i], '
                    '[role="button"][aria-label*="Kommentar" i]')

# The Like button, for the like that goes before the comment. Three handles,
# tried in the order of how often they are there: Facebook labels the feed's
# Like control by name (and relabels it "Remove Like" once pressed, which is
# how "already liked" is read); its ad marker is `like_button`; and the thumb
# is the first glyph on the same sprite strip as the comment bubble. The
# offset moves when Facebook reissues the sheet, the same as the bubble's, so
# it is a list and a setting rather than one number.
LIKE_LABELLED = ('[role="button"][aria-label="Like" i], '
                 '[role="button"][aria-label="Remove Like" i], '
                 '[role="button"][aria-label="Gefällt mir" i]')
LIKE_MARKER = '[data-ad-rendering-role="like_button"]'
LIKE_ICON_OFFSETS = ("0px -441px",)

COMMENT_BOX = ('[role="textbox"][contenteditable="true"], '
               'div[contenteditable="true"][aria-label*="comment" i]')

# Where the composer ends up when Facebook opens comments in a modal rather than
# expanding them under the post.
DIALOG = '[role="dialog"]'

# Claim the first post on screen that has not been done yet, and STAMP it.
#
# Stamped rather than returned as an index into a NodeList, which is what this
# wanted to be and would have been wrong: the feed re-renders between the call
# that picks a post and the call that clicks into it, and an index picked before
# a re-render addresses a different post afterwards - which would mean commenting
# on a post nobody chose. An attribute travels WITH the element through any
# amount of re-rendering, so what Playwright goes on to click is the post that
# was actually picked, or nothing at all.
#
# `data-xbot-done` is the other half: it is what stops the walk commenting twice
# on a post it scrolls past twice, which the back-to-top reset guarantees it will.
# It ALWAYS returns a tally of what it looked at and why each candidate was
# turned down, claim or no claim. That is not instrumentation bolted on after
# the fact - it is the difference between "the walk scrolled past everything"
# and a usable answer. This returned a bare `null` for every rejection at first,
# and a run where nothing was ever claimed was indistinguishable from a run with
# commenting switched off: no post stopped on, no error, nothing in the log.
CLAIM_POST_JS = r"""
(opts) => {
  document.querySelectorAll('[data-xbot-target]')
          .forEach(el => el.removeAttribute('data-xbot-target'));

  const vh = window.innerHeight;
  const why = { total: 0, nested: 0, seen: 0, short: 0, offscreen: 0,
                nokey: 0, nobutton: 0, barhidden: 0, eligible: 0, withButton: 0 };
  let claim = null;

  for (const el of document.querySelectorAll(opts.post)) {
    why.total++;

    // Facebook marks COMMENTS as role="article" too, nested inside the post's
    // own article. Without this the walk claims a comment, finds no comment
    // button on it, marks it failed and moves to the next comment - burning
    // the whole cap on one post's replies without ever commenting on a post.
    if (el.parentElement && el.parentElement.closest(opts.post)) { why.nested++; continue; }

    // Commented on, or tried and failed. Both are "do not pick this again":
    // a post that could not be commented on will not become commentable by
    // being met a second time, and retrying it forever is how the walk would
    // stop making progress down the feed.
    if (el.getAttribute('data-xbot-done') || el.getAttribute('data-xbot-skip')) {
      why.seen++; continue;
    }

    const r = el.getBoundingClientRect();
    // A box too short to be a post is a placeholder Facebook has not filled in
    // yet.
    if (r.height < 120) { why.short++; continue; }

    // Overlapping the viewport at all, rather than sitting inside it. Feed
    // posts are routinely taller than the window - a photo post can run to two
    // or three screens - so "fits on screen" would reject the big ones forever
    // and the walk would sail past every one of them.
    if (r.bottom < 60 || r.top > vh - 60) { why.offscreen++; continue; }

    // Identity, for the run's own record. The permalink is the real one; the
    // opening text is a fallback for the posts that do not carry one, and it
    // only has to be good enough to tell two posts on one screen apart.
    const link = el.querySelector(
      'a[href*="/posts/"], a[href*="story_fbid"], a[href*="/permalink/"], ' +
      'a[href*="/videos/"], a[href*="/photo"], a[href*="/reel/"]');
    // The query string is dropped EXCEPT for the id, because on a photo post
    // the id is the whole address: `/photo/?fbid=123` with the query gone is
    // `/photo/`, which every photo post on the page then shares, and the log
    // says "commented on /photo/" five times about five different posts.
    let key = '';
    if (link && link.href) {
      try {
        const u = new URL(link.href);
        const id = u.searchParams.get('fbid') || u.searchParams.get('story_fbid') ||
                   u.searchParams.get('v');
        key = u.origin + u.pathname + (id ? '?id=' + id : '');
      } catch (e) { key = link.href.split('?')[0]; }
    }
    if (!key) key = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 140);
    if (!key) { why.nokey++; continue; }

    // Done in an earlier life of this page. The stamps above are attributes
    // on the DOM and a reload wipes them, so the walk's own list of what it
    // has commented on is the memory that survives one - without it the
    // reload that recovers a stuck run would hand it the same posts again.
    if (opts.done && opts.done.indexOf(key) >= 0) { why.seen++; continue; }

    // No comment control anywhere on it: not something to comment on. The
    // "What's on your mind?" composer and the stories tray are feed units
    // too - they carry `aria-posinset` like a post does - and claiming one
    // costs a failed attempt and a warning in the log for nothing. Any sprite
    // icon counts as evidence of an action bar, on top of the marker and the
    // labelled button, so a locale that names the button something else is
    // still let through.
    const control = el.querySelector(opts.marker) ||
                    el.querySelector(opts.labelled) ||
                    (opts.icon && el.querySelector(opts.icon));
    if (!control) { why.nobutton++; continue; }

    // And the comment BAR has to be on screen already - not just the post.
    // This is what keeps the scroll smooth: a post is claimed only once its
    // action bar has scrolled into the window on its own, so clicking the
    // comment button never has to move the page to reach it. Before this,
    // claiming a two-screen post the moment its top edge appeared meant
    // scrolling up to its bar, commenting, and scrolling back - the lurch
    // that read as "it goes down and comes back up". A bar still below the
    // fold is left for a later slice; the scroll is heading there anyway.
    const c = control.getBoundingClientRect();
    if (c.top < 60 || c.bottom > vh - 40) { why.barhidden++; continue; }

    why.eligible++;
    why.withButton++;

    if (!claim) {
      el.setAttribute('data-xbot-target', '1');
      claim = { key: key, top: Math.round(r.top), height: Math.round(r.height),
                hasButton: true };
    }
  }
  return { claim: claim, why: why, vh: vh };
}
"""

# Retire the claim, commented or not. Called on BOTH paths on purpose: a post
# that could not be commented on must still be marked, or the walk meets it
# again on the next pass, fails on it again, and never gets any further.
# Three outcomes, not two. "done" and "skip" both mean never pick this again;
# `null` means simply let it go - which is what a LOOK needs. The probe runs the
# picker to see what it would choose, and a probe that marked its answer as
# skipped would quietly poison the next walk against the very post it reported.
RELEASE_POST_JS = r"""
(done) => {
  const el = document.querySelector('[data-xbot-target]');
  if (!el) return false;
  if (done === true) el.setAttribute('data-xbot-done', '1');
  else if (done === false) el.setAttribute('data-xbot-skip', '1');
  el.removeAttribute('data-xbot-target');
  return true;
}
"""

# Find the comment control inside the claimed post and STAMP it, so Playwright
# can then click it for real. Deliberately not clicked here: a JS `.click()`
# fires an untrusted event with no pointer behind it, and Playwright's click
# moves an actual cursor, waits for the element to be stable and hittable, and
# fails loudly when something is covering it.
#
# It returns every icon offset it found either way. That is what makes the next
# sprite change a five-second fix instead of an afternoon: the probe prints the
# offsets that ARE on the post, and one of them is the new comment bubble.
FIND_COMMENT_BUTTON_JS = r"""
(opts) => {
  document.querySelectorAll('[data-xbot-cbtn]')
          .forEach(el => el.removeAttribute('data-xbot-cbtn'));

  const post = document.querySelector('[data-xbot-target="1"]');
  if (!post) return { found: false, why: 'the post is no longer on the page', seen: [] };

  // Compared as NUMBERS, never as text. The page writes `0 -231px` and
  // devtools shows you `0px -231px` - the same position, two spellings, and an
  // exact string match on the second one never matches the first. That is the
  // whole reason the icon was never found. Anything that reads as the same two
  // lengths now matches: `0 -231px`, `0px -231px`, `0.0px -231.0px`.
  //
  // The Y alone decides it. Facebook's sprite is a vertical strip, so X is 0
  // for every glyph on it and carries no information - insisting on it only
  // adds a second thing that can be spelled differently.
  const yOf = (text) => {
    const parts = String(text || '').trim().split(/\s+/);
    if (!parts.length) return null;
    const raw = parts.length > 1 ? parts[1] : parts[0];
    const m = /^(-?\d*\.?\d+)/.exec(raw);
    return m ? Math.round(parseFloat(m[1])) : null;
  };
  const wanted = (opts.offsets || []).map(yOf).filter(v => v !== null);

  const seen = [];
  let icon = null;
  for (const i of post.querySelectorAll(opts.icon)) {
    // The inline style is where Facebook writes it; the computed style is the
    // fallback for the builds that put it in a class instead.
    let bp = (i.style && i.style.backgroundPosition) || '';
    if (!bp) { try { bp = getComputedStyle(i).backgroundPosition || ''; } catch (e) {} }
    bp = bp.trim();
    if (bp && seen.indexOf(bp) < 0) seen.push(bp);
    const y = yOf(bp);
    if (icon === null && y !== null && wanted.indexOf(y) >= 0) icon = i;
  }

  // The icon, then Facebook's ad marker, then a labelled button. In that order
  // because that is the order of how often they are actually present.
  let from = 'icon';
  let node = icon;
  if (!node) { node = post.querySelector(opts.marker); from = 'marker'; }
  if (!node) { node = post.querySelector(opts.labelled); from = 'label'; }
  if (!node) return { found: false, why: 'no comment icon, marker or labelled button',
                      seen: seen, wanted: wanted };

  // Up to whatever actually takes the click. The icon is a 20x20 decoration
  // inside the control, not the control - clicking it can work by bubbling and
  // can equally land on padding that handles nothing.
  //
  // The last resort walks up until it finds an ancestor big enough to BE the
  // button, because the markup here has neither a role nor a tabindex on any
  // of the wrappers around the icon, and `parentElement` alone is a bare div
  // that handles nothing.
  let button = node.closest('[role="button"]') || node.closest('[tabindex]');
  if (!button) {
    let up = node.parentElement, hops = 0;
    while (up && hops < 5) {
      const b = up.getBoundingClientRect();
      if (b.width >= 40 && b.height >= 24) { button = up; break; }
      up = up.parentElement; hops++;
    }
    button = button || node.parentElement;
  }
  if (!button) return { found: false, why: 'the icon has no clickable ancestor',
                        seen: seen, wanted: wanted };

  const r = button.getBoundingClientRect();
  // The ICON's own box too. It is what a person aims at, and it is what the
  // coordinate click falls back to when clicking the element does nothing.
  const ir = (icon || node).getBoundingClientRect();
  button.setAttribute('data-xbot-cbtn', '1');
  return { found: true, via: from, seen: seen, wanted: wanted,
           role: button.getAttribute('role') || '',
           tag: button.tagName.toLowerCase(),
           width: Math.round(r.width), height: Math.round(r.height),
           top: Math.round(r.top),
           iconX: Math.round(ir.left + ir.width / 2),
           iconY: Math.round(ir.top + ir.height / 2) };
}
"""


def guard(page) -> None:
    """Raise if this window has stopped being a feed we can walk.

    Both readings, because they are different mornings: a signed-out window
    wants a login and a checkpointed one wants a captcha solved, and "it stopped
    scrolling" tells the operator neither.
    """
    found = challenge_showing(page)
    if found:
        raise AutomationError(f"Stopped scrolling: {found}.")
    if signed_out(page):
        raise AutomationError(
            "This window is signed out of Facebook - log in inside it, then start again.")


def _delay_for(wpm: int) -> float:
    """Milliseconds between keystrokes for a words-per-minute figure.

    Five characters to a word is the standard the number is defined against, so
    240wpm is 1200 characters a minute, or 50ms a key. The same arithmetic as
    `messenger.py` and `x.py`, and the same reason: a composer that receives its
    whole value in one call has fired no keydown events at all.
    """
    wpm = max(40, min(900, int(wpm or 240)))
    return 60_000 / (wpm * 5)


def survey(page, *, done: Optional[list[str]] = None) -> dict:
    """What is on screen, and what the picker makes of it.

    Always returns both halves: `claim` (the post stamped, or None) and `why`
    (the tally). The tally is what makes "nothing was claimed" answerable
    instead of merely true.

    `done` is the keys of posts already commented on this run; they are never
    claimed again, reload or no reload.
    """
    return page.evaluate(CLAIM_POST_JS, {
        "post": POST, "marker": COMMENT_MARKER, "labelled": COMMENT_LABELLED,
        "icon": COMMENT_ICON, "done": list(done or []),
    }) or {}


def refresh_feed(page) -> str:
    """Reload facebook.com and wait for the feed. Returns what came up.

    The recovery for a walk that has stopped landing comments: a composer that
    never cleared, a modal that would not close, a feed that stopped
    rendering. Whatever it was, a fresh page has none of it. A reload that
    itself fails - a dead tab, a navigation that hangs - falls back to a plain
    navigation to the home URL, which is the same page by another road.
    """
    close_dialog(page)
    try:
        page.reload(wait_until="domcontentloaded", timeout=45_000)
        found = settle(page)
    except Exception:                                # noqa: BLE001 - try the other road
        found = open_home(page)
    guard(page)
    return found


def explain(why: dict, *, total_hint: str = "") -> str:
    """The tally, as a sentence for the log.

    Written out rather than dumped as a dict because the person reading it is
    looking at a feed that is scrolling past everything and wants to know which
    filter is eating their posts, not to parse JSON in a log line.
    """
    if not why or not why.get("total"):
        return ("no posts matched at all - the feed selector has stopped "
                "matching Facebook's markup" + (f" ({total_hint})" if total_hint else ""))
    bits = []
    for key, said in (("nested", "were comments, not posts"),
                      ("seen", "already done"),
                      ("short", "too short to be a post"),
                      ("offscreen", "not on screen"),
                      ("nokey", "had nothing to identify them by"),
                      ("nobutton", "had no comment control on them"),
                      ("barhidden", "with the comment bar not on screen yet")):
        if why.get(key):
            bits.append(f"{why[key]} {said}")
    tail = f" of {why['total']} on the page: " + ", ".join(bits) if bits else ""
    return (f"{why.get('eligible', 0)} eligible, "
            f"{why.get('withButton', 0)} with a comment button{tail}")


def claim_post(page) -> Optional[dict]:
    """Pick the next post on screen to comment on, or None if there isn't one.

    None is an ordinary answer much of the time - the walk is between posts, or
    everything on screen is done - which is exactly why this used to swallow
    every exception and return None for real failures too. A broken selector, a
    JS error, a detached frame: all of them read as "nothing to comment on", and
    the walk scrolled the whole feed in silence. Failures are raised now, and
    the caller decides what to say about them.
    """
    found = survey(page)
    return found.get("claim") or None


def release_post(page, *, done: Optional[bool]) -> None:
    """Give up the claim. `True` commented, `False` tried and failed, `None` a
    look that is putting the post back untouched."""
    try:
        page.evaluate(RELEASE_POST_JS, done)
    except Exception:                                # noqa: BLE001 - already gone
        pass


def _visible_box(root):
    """The first comment composer under `root` that is actually on screen.

    `.first` on its own is not enough: a post carries hidden composers for
    replies to comments it has not expanded, and typing into one of those posts
    a reply to somebody else's comment rather than a comment on the post.
    """
    try:
        boxes = root.locator(COMMENT_BOX)
        for i in range(min(boxes.count(), 8)):
            box = boxes.nth(i)
            if box.is_visible():
                return box
    except Exception:                                # noqa: BLE001 - mid-render
        return None
    return None


# The Like control on the claimed post, stamped for Playwright the same way
# the comment control is. `liked` is read off the button itself: Facebook
# relabels it "Remove Like" and sets aria-pressed once it has been pressed,
# which is the difference between a like to make and a like to leave alone -
# pressing a pressed Like button UNlikes the post.
FIND_LIKE_BUTTON_JS = r"""
(opts) => {
  document.querySelectorAll('[data-xbot-lbtn]')
          .forEach(el => el.removeAttribute('data-xbot-lbtn'));
  const post = document.querySelector('[data-xbot-target="1"]');
  if (!post) return { found: false, why: 'the post is no longer on the page' };

  const yOf = (text) => {
    const parts = String(text || '').trim().split(/\s+/);
    if (!parts.length) return null;
    const raw = parts.length > 1 ? parts[1] : parts[0];
    const m = /^(-?\d*\.?\d+)/.exec(raw);
    return m ? Math.round(parseFloat(m[1])) : null;
  };
  const wanted = (opts.offsets || []).map(yOf).filter(v => v !== null);

  // The labelled button first: it is the control itself, and it carries the
  // pressed state. The marker and the thumb are handles to walk up from.
  let node = post.querySelector(opts.labelled), from = 'label';
  if (!node) { node = post.querySelector(opts.marker); from = 'marker'; }
  if (!node) {
    for (const i of post.querySelectorAll(opts.icon)) {
      let bp = (i.style && i.style.backgroundPosition) || '';
      if (!bp) { try { bp = getComputedStyle(i).backgroundPosition || ''; } catch (e) {} }
      const y = yOf(bp);
      if (y !== null && wanted.indexOf(y) >= 0) { node = i; from = 'icon'; break; }
    }
  }
  if (!node) return { found: false, why: 'no like button, marker or thumb on this post' };

  let button = node.closest('[role="button"]') || node.closest('[tabindex]');
  if (!button) {
    let up = node.parentElement, hops = 0;
    while (up && hops < 5) {
      const b = up.getBoundingClientRect();
      if (b.width >= 40 && b.height >= 24) { button = up; break; }
      up = up.parentElement; hops++;
    }
    button = button || node.parentElement;
  }
  if (!button) return { found: false, why: 'the thumb has no clickable ancestor' };

  const label = (button.getAttribute('aria-label') || '').toLowerCase();
  const liked = button.getAttribute('aria-pressed') === 'true' ||
                label.indexOf('remove') >= 0 || label.indexOf('unlike') >= 0;
  button.setAttribute('data-xbot-lbtn', '1');
  const r = button.getBoundingClientRect();
  return { found: true, via: from, liked: liked, label: label,
           width: Math.round(r.width), height: Math.round(r.height) };
}
"""


def find_like_button(page, *, offsets: tuple[str, ...] = LIKE_ICON_OFFSETS) -> dict:
    """Locate the claimed post's Like control and stamp it. Reports what it saw."""
    return page.evaluate(FIND_LIKE_BUTTON_JS, {
        "icon": COMMENT_ICON, "offsets": list(offsets),
        "marker": LIKE_MARKER, "labelled": LIKE_LABELLED,
    }) or {"found": False, "why": "the page answered nothing"}


def like_claimed(page, *, timeout: int = 6_000,
                 offsets: tuple[str, ...] = LIKE_ICON_OFFSETS,
                 on_step: Optional[Callable[[str], None]] = None) -> str:
    """Press Like on the claimed post. Returns what happened, in words.

    Never raises: the like is the warm-up to the comment, not the point of the
    visit, and a post whose Like button could not be found is still a post to
    comment on. The comment is what the run is counted by; this is reported
    beside it.

    A post already liked is left alone. Facebook's Like button is a toggle,
    and pressing it on a post this account has already liked takes the like
    away - the one outcome worse than not pressing it.

    Verified rather than assumed: after the click the button is read back and
    has to say "Remove Like" (or carry aria-pressed) before this counts it. A
    click that landed on the button's padding, or one Facebook swallowed, is
    reported as such rather than as a like.
    """
    found = find_like_button(page, offsets=offsets)
    if not found.get("found"):
        return f"like skipped - {found.get('why') or 'no like button'}"
    if found.get("liked"):
        return "already liked"
    button = page.locator('[data-xbot-lbtn="1"]').first
    try:
        button.click(timeout=timeout)
    except Exception as err:                         # noqa: BLE001 - reported, not raised
        return f"like click failed - {str(err).splitlines()[0][:120]}"
    if on_step:
        on_step(f"clicked Like (via {found.get('via')})")

    # Read it back. The relabel is near-instant when it lands; three seconds
    # is generous.
    deadline = time.monotonic() + 3.0
    while True:
        try:
            again = find_like_button(page, offsets=offsets)
        except Exception:                            # noqa: BLE001 - mid-render
            again = {}
        if again.get("liked"):
            return "liked"
        if time.monotonic() >= deadline:
            return "like did not register"
        page.wait_for_timeout(250)


def find_comment_button(page, *, offsets: tuple[str, ...] = COMMENT_ICON_OFFSETS) -> dict:
    """Locate the claimed post's comment control and stamp it. Reports what it saw."""
    return page.evaluate(FIND_COMMENT_BUTTON_JS, {
        "icon": COMMENT_ICON, "offsets": list(offsets),
        "marker": COMMENT_MARKER, "labelled": COMMENT_LABELLED,
    }) or {"found": False, "why": "the page answered nothing", "seen": []}


def _comment_button(page, *, offsets: tuple[str, ...] = COMMENT_ICON_OFFSETS):
    """The clickable Comment control on the claimed post, and what was found.

    Found in JS (see `FIND_COMMENT_BUTTON_JS`) and returned as a Playwright
    locator over the stamp it left, so the click itself is a real one. The
    second half of the pair carries the icon's own coordinates, which is what
    the fallback click needs.
    """
    found = find_comment_button(page, offsets=offsets)
    if not found.get("found"):
        seen = found.get("seen") or []
        why = found.get("why") or "no comment button"
        if seen:
            # The offsets on the post are the answer to "why not", so they go in
            # the message rather than only into the probe. When Facebook moves
            # the sprite this line is the fix, sitting in the log.
            wanted = found.get("wanted") or []
            why += (f" (looking for y={wanted}; icon offsets on this post: "
                    f"{', '.join(seen[:8])})")
        raise AutomationError(why)
    return page.locator('[data-xbot-cbtn="1"]').first, found


def _comment_box(page, post, *, timeout: int = 6_000,
                 offsets: tuple[str, ...] = COMMENT_ICON_OFFSETS,
                 on_step: Optional[Callable[[str], None]] = None):
    """The composer for this post, opening it if it is not already open.

    Facebook shows the box outright on some posts and hides it behind the
    Comment button on others, so the button is a fallback rather than the route:
    clicking Comment on a post whose box is ALREADY open collapses it again,
    which is the opposite of what the click was for.
    """
    box = _visible_box(post)
    if box is not None:
        if on_step:
            on_step("the comment box was already open on this post")
        return box

    # Raises with what it saw rather than returning None - see `_comment_button`.
    button, found = _comment_button(page, offsets=offsets)
    if on_step:
        on_step(f"found the comment icon (via {found.get('via')}, "
                f"clicking a {found.get('width')}x{found.get('height')} "
                f"{found.get('tag') or 'element'}"
                f"{' role=' + found['role'] if found.get('role') else ''})")

    # Into view before the click. Playwright scrolls to an element itself, but
    # the action bar of a tall post can sit below the fold with a sticky header
    # over it, and a click that lands on the header is a click on nothing.
    try:
        button.scroll_into_view_if_needed(timeout=timeout)
    except Exception:                                # noqa: BLE001 - best effort
        pass

    # Two ways to click it, tried in order, because the element is not reliably
    # the thing that handles the click. Facebook's wrappers carry no role and no
    # tabindex, so what gets picked is a div chosen by size, and a div chosen by
    # size can be the wrong div.
    #
    #   1. the element, through Playwright - it waits for the thing to be
    #      stable and hittable and complains if something is covering it;
    #   2. the ICON's own centre, as raw mouse coordinates. That is the pixel a
    #      person aims at, and it does not care what the element tree looks
    #      like. It is second rather than first only because it skips all of
    #      Playwright's actionability checks.
    box = _click_then_wait(page, post, lambda: button.click(timeout=timeout),
                           timeout=timeout, on_step=on_step)
    if box is not None:
        if on_step:
            on_step("clicked the comment icon")
        return box

    x, y = found.get("iconX"), found.get("iconY")
    if x is None or y is None:
        raise AutomationError("the comment icon was clicked but no comment box appeared")
    if on_step:
        on_step(f"the element click did nothing - clicking the icon at ({x}, {y})")

    box = _click_then_wait(page, post, lambda: page.mouse.click(x, y), timeout=timeout,
                           on_step=on_step)
    if box is None:
        raise AutomationError(
            "the comment icon was clicked twice - as an element and at its own "
            "coordinates - and no comment box appeared either time")
    if on_step:
        on_step("clicked the comment icon by coordinates")
    return box


def _click_then_wait(page, post, do_click, *, timeout: int = 6_000,
                     on_step: Optional[Callable[[str], None]] = None):
    """Click, then wait for a composer. Returns it, or None if none appeared.

    None rather than raising: the caller has another way to click and wants to
    try it, and a failed first attempt is not an error until the second one has
    failed too.

    Scoped to the post first, NOT the page. A page-wide search finds the
    composer of whatever else is on screen - the next post down, or the one
    still open from last time - and types the comment into the wrong story.

    The dialog is the second look rather than the first for the same reason,
    and it is needed: Facebook opens some posts' comments in a modal instead of
    expanding them in place, and that modal is a child of <body>, nowhere
    inside the post element.
    """
    try:
        do_click()
    except Exception as err:                         # noqa: BLE001 - the other way may work
        # Said, not swallowed. "The click did nothing" and "the click could not
        # be made" are different problems, and this used to report both as the
        # first one.
        if on_step:
            on_step(f"the click could not be made: {str(err).splitlines()[0][:160]}")
        return None

    deadline = time.monotonic() + timeout / 1000.0
    while True:
        box = _visible_box(post) or _visible_box(page.locator(DIALOG))
        if box is not None:
            return box
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(200)


def comment_on_claimed(page, text: str, *, wpm: int = 240,
                       timeout: int = 6_000,
                       offsets: tuple[str, ...] = COMMENT_ICON_OFFSETS,
                       click_only: bool = False,
                       like_first: bool = False,
                       on_step: Optional[Callable[[str], None]] = None) -> str:
    """Type `text` into the claimed post's comment box and press Enter.

    Returns what happened, in words, because the panel puts it on screen:
    "commented", or "liked and commented" when `like_first` is set and the
    like landed too. Either is a success; anything else says what went wrong.

    `like_first` presses Like on the post before the comment box is opened -
    the order a person does it in. See `like_claimed` for what it does when
    it cannot: nothing that stops the comment.

    The post is addressed through the stamp `claim_post` left on it rather than
    by position, so a feed that re-rendered in between comments on the post that
    was chosen or on nothing at all - never on whatever slid into its place.

    `on_step` is called as each stage lands. A comment is four separate things
    that can each fail - find the icon, click it, get a composer, have Facebook
    accept the text - and "it did not work" is four different problems wearing
    one sentence. `click_only` stops once the composer is open, which is the
    Click comments button: it opens the comments and leaves them open.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)
    post = page.locator('[data-xbot-target="1"]').first
    if post.count() == 0:
        raise AutomationError("the post was claimed but is no longer on the page")

    # NOT scrolled into view here. The picker only claims a post whose comment
    # bar is already on screen (see CLAIM_POST_JS), so there is nothing to
    # scroll to - and scrolling a post taller than the window "into view"
    # meant jumping up to its top edge, which was the lurch the walk had.

    # Like first, comment second. The Like button is in the same action bar
    # as the comment bubble, so it is on screen for the same reason.
    liked = False
    if like_first and not click_only:
        said = like_claimed(page, timeout=timeout, on_step=step)
        liked = said in ("liked", "already liked")
        step(said)

    box = _comment_box(page, post, timeout=timeout, offsets=offsets, on_step=step)
    step("the comment box is open")
    if click_only:
        return "opened the comments"

    box.click(timeout=timeout)

    base = _delay_for(wpm)
    for char in text:
        page.keyboard.type(char, delay=0)
        # Jittered per key. A perfectly even cadence is not a typist.
        page.wait_for_timeout(base * random.uniform(0.55, 1.75))

    # A beat between the last key and Enter. Nobody types the final character of
    # a comment and submits it in the same millisecond.
    page.wait_for_timeout(random.uniform(220, 700))
    step(f'typed "{text}"')
    page.keyboard.press("Enter")
    step("pressed Enter")

    # Verify rather than assume. An emptied composer is the cheapest honest
    # signal that Facebook took the comment - and if it did not, the run has to
    # say so rather than counting a comment that was never posted.
    posted = False
    for _ in range(int(timeout / 250)):
        page.wait_for_timeout(250)
        try:
            if not (box.inner_text(timeout=1000) or "").strip():
                posted = True
                break
        except Exception:                            # noqa: BLE001 - box detached
            # The composer being gone entirely is the same evidence, arrived at
            # from the other direction: Facebook replaced it once it submitted.
            posted = True
            break

    # Shut the modal, if this was one of the posts that opens in a modal. Not
    # tidiness: a dialog left open covers the feed and takes the scroll with it,
    # so the walk would carry on "scrolling" a page that cannot move and report
    # passes against it until the stuck check gave up.
    close_dialog(page)

    if posted:
        return "liked and commented" if liked else "commented"
    return "typed, but the composer did not clear - the comment may not have posted"


def close_dialog(page) -> bool:
    """Escape out of an open modal. Returns whether one was there.

    Escape rather than hunting for the X: every Facebook dialog honours it, and
    it cannot click the wrong thing - which a guessed close-button selector on a
    page full of generated class names very much can.
    """
    try:
        if page.locator(DIALOG).count() == 0:
            return False
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        return True
    except Exception:                                # noqa: BLE001 - already gone
        return False


def posts_on_screen(page) -> int:
    """How many posts have rendered so far. The one number worth reporting.

    Not a count of anything collected - nothing is collected. It is the evidence
    that the scroll is DOING something: a feed that is loading more as it is
    walked has a number that climbs, and one that has stopped loading has a
    number that does not.
    """
    try:
        return page.locator(POST).count()
    except Exception:                                # noqa: BLE001 - mid-render
        return 0


# -- Who posted ------------------------------------------------------
# The author of every post on the page, read off the post's header link. This
# is what the Scraping panel collects when it walks the home feed: the feed is
# a stream of people, and each post's header is one profile link with a name
# on it - a vanity username after the slash, or `profile.php?id=<number>` for
# the accounts that never set one. Both come back as the bare handle, which is
# exactly what a Messenger thread is addressed by.
#
# The header is found through `aria-labelledby` first: a feed unit names its
# own heading with it, and the author link is the first profile link in that
# heading. The headline selectors behind it are for the surfaces that do not
# carry the attribute. Anything else in the post - a tagged friend, a shared
# post's original author, a Page in the body - comes after the header in
# document order and is never the first match.
#
# `RESERVED` keeps site sections out of the roster: `/groups/...`, `/watch`,
# `/marketplace` and the rest are one path segment too, and a link to one
# would otherwise read as a person called "watch".
AUTHORS_JS = r"""
(opts) => {
  const RESERVED = new Set(['groups','photo','photos','posts','watch','reel','reels',
    'hashtag','events','marketplace','stories','gaming','pages','people','videos',
    'video.php','share','sharer','sharer.php','login','help','policies','privacy',
    'settings','friends','memories','saved','bookmarks','notifications','messages',
    'search','ads','business','games','fundraisers','jobs','weather','dialog',
    'plugins','l.php','home.php','me','about','legal','terms','live','permalink.php',
    'story.php','profile.php','feeds','latest','favorites','places','pg','ad_center']);
  const parse = (href) => {
    let u;
    try { u = new URL(href, location.origin); } catch (e) { return null; }
    if (!/(^|\.)facebook\.com$/i.test(u.hostname)) return null;
    const path = u.pathname.replace(/\/+$/, '');
    let m;
    if (/^\/profile\.php$/i.test(path)) {
      const id = u.searchParams.get('id');
      return id && /^\d+$/.test(id) ? id : null;
    }
    if ((m = /^\/groups\/[^/]+\/user\/(\d+)/i.exec(path))) return m[1];
    if ((m = /^\/people\/[^/]+\/(\d+)/i.exec(path))) return m[1];
    if ((m = /^\/([A-Za-z0-9.\-_]+)$/.exec(path))) {
      const seg = m[1];
      if (RESERVED.has(seg.toLowerCase())) return null;
      return seg;
    }
    return null;
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const headerOf = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const found = ids.map(id => document.getElementById(id)).filter(Boolean);
    if (found.length) return found;
    const h = el.querySelector('h2, h3, h4');
    return h ? [h] : [el];
  };
  const authors = [];
  const why = { total: 0, nested: 0, noauthor: 0, found: 0 };
  for (const el of document.querySelectorAll(opts.post)) {
    why.total++;
    if (el.parentElement && el.parentElement.closest(opts.post)) { why.nested++; continue; }
    let author = null;
    for (const root of headerOf(el)) {
      for (const a of root.querySelectorAll('a[href]')) {
        const handle = parse(a.getAttribute('href') || a.href);
        if (!handle) continue;
        const name = clean(a.innerText) || clean(a.getAttribute('aria-label'));
        // A profile link with no words on it is the avatar; the same person's
        // name link is a sibling and comes next. Nor is a link labelled with
        // Facebook's own words - "See owner profile" heads some posts - a name.
        if (!name || /^(see owner profile|see profile|view profile|visit profile|add friend|message|follow|following|see all|sponsored)$/i.test(name)) continue;
        author = { handle: handle, name: name };
        break;
      }
      if (author) break;
    }
    if (!author) { why.noauthor++; continue; }
    why.found++;
    authors.push(author);
  }
  return { authors: authors, why: why };
}
"""


def read_authors(page) -> dict:
    """Everyone whose post is on the page right now, with the picker's tally."""
    try:
        return page.evaluate(AUTHORS_JS, {"post": POST}) or {}
    except Exception:                                # noqa: BLE001 - mid-render
        return {}


def walk_authors(page, *,
                 speed: float = 300.0,
                 tick: float = 0.9,
                 nudge_after: int = 6,
                 refresh_after: int = 0,
                 stall_seconds: float = 90.0,
                 limit: int = 0,
                 on_rows: Optional[Callable[[list[dict]], None]] = None,
                 on_reload: Optional[Callable[[str], None]] = None,
                 checkpoint: Optional[Callable[[float], None]] = None) -> list[dict]:
    """Scroll the home feed and collect the people who posted in it.

    `stall_seconds` is the watchdog: no new author for that long and the page
    is reloaded, whatever the reason - the feed stopped streaming, a modal
    took the page, the scroller lost its loop. `on_reload` hears about each
    one so the panel can count them. `refresh_after` is the older look-based
    version of the same thing; zero, the default, leaves it to the clock.

    The Facebook twin of `messenger.walk_home`, and the same contract: it runs
    until `checkpoint` raises `Stopped`, and `on_rows` is called with everything
    collected so far every time that grows, which is what puts the roster on
    screen while the walk is still going. `limit` is a ceiling for a caller that
    wants one; zero means none.

    The scrolling is the same in-page loop the commenting walk uses, at the
    speed the FB Campaign panel is set to, so the feed moves the way it does
    there. A feed that stops moving is nudged - a short scroll back up, which
    is what wakes Facebook's loader - and one that has given nothing new for
    a long while is reloaded, because a stale feed does not recover on its own.

    Each row is a handle and a name and nothing else. The feed shows no bio, no
    follower count and nothing about the inbox, and the walk does not open
    profiles to find out - so those are left unknown rather than invented.
    """
    found_page = open_home(page)
    guard(page)
    if not feed_showing(page):
        raise AutomationError(f"Cannot walk the feed: {found_page}.")

    speed = max(1.0, float(speed))
    tick = max(0.1, float(tick))

    def scroller(**flags) -> dict:
        return page.evaluate(SCROLLER_JS, {"post": POST, **flags}) or {}

    scroller(speed=speed, running=True)

    rows: dict[str, dict] = {}
    barren = 0
    stuck = 0
    seen_posts = 0
    said_empty = False
    last_new = time.monotonic()

    def reload(why: str) -> None:
        nonlocal barren, stuck, seen_posts, last_new
        log("facebook", f"{why} - reloading the feed", "warn")
        if on_reload:
            on_reload(why)
        found = refresh_feed(page)
        log("facebook", f"reloaded facebook - {found}")
        scroller(speed=speed, running=True)
        barren = stuck = seen_posts = 0
        last_new = time.monotonic()

    while True:
        if checkpoint:
            checkpoint(0)

        got = read_authors(page)
        before = len(rows)
        for author in got.get("authors") or []:
            handle = str(author.get("handle") or "").strip()
            if not handle or handle in rows:
                continue
            rows[handle] = {
                "handle": handle,
                "name": str(author.get("name") or "").strip(),
                "bio": "",
                "followers": None,
                # Unknown, not closed: nothing on the feed says whether this
                # person takes messages, and the campaign finds out by trying.
                "dm_open": None,
                "protected": False,
            }
            if limit and len(rows) >= limit:
                break

        if len(rows) > before:
            last_new = time.monotonic()
            if on_rows:
                on_rows(list(rows.values()))
        if limit and len(rows) >= limit:
            break

        # Said once, when the selector has stopped matching anything at all:
        # that is a Facebook rebuild, not a quiet feed, and the log is where
        # the operator finds out which.
        why = got.get("why") or {}
        if not why.get("total") and not said_empty and seen_posts == 0:
            log("facebook", "no feed posts matched on the page yet - the selector may "
                            "need a look if this does not change", "warn")
            said_empty = True

        moved = scroller(speed=speed, running=True)
        posts = int(moved.get("posts") or 0)
        stalled = not int(moved.get("moved") or 0) and posts <= seen_posts
        stuck = stuck + 1 if stalled else 0
        seen_posts = max(seen_posts, posts)
        barren = 0 if len(rows) > before else barren + 1

        if stuck >= nudge_after:
            scroller(nudge=random.randint(400, 800))
            stuck = 0
        quiet = time.monotonic() - last_new
        if stall_seconds and quiet > stall_seconds:
            reload(f"no new account for {int(quiet)}s")
        elif refresh_after and barren >= refresh_after:
            reload(f"nothing new in {barren} looks")

        wait = random.uniform(tick * 0.7, tick * 1.3)
        if checkpoint:
            checkpoint(wait)
        else:
            time.sleep(wait)
        guard(page)

    return list(rows.values())


# How many looks of finding nothing to comment on before the walk says so.
# Six is about five seconds at the default tick: long enough that scrolling
# through the gap between two posts stays quiet, short enough that a run which
# is never going to stop on anything says so before the operator gives up on it.
DRY_BEFORE_SAYING = 6


class _Tab:
    """One tab's share of a walk: the counters that are its own, not the run's.

    The run owns what is shared - the comment total, the cap, the list of posts
    already commented on. A tab owns where IT is: whether its feed has stopped
    growing, how long since a comment landed in it, and when its comment gap
    is over. Kept apart so five tabs can be driven from one loop.
    """

    def __init__(self, page, index: int) -> None:
        self.page = page
        self.index = index          # 0-based; shown to people as index + 1
        self.looks = 0
        self.barren = 0
        self.seen = 0
        self.dry = 0
        self.landed_at = time.monotonic()
        self.misses = 0
        self.guarded_at = 0.0
        # Paused for the comment gap: the page is held still until `resume_at`
        # and this tab is not looked at for posts. The OTHER tabs carry on.
        self.paused = False
        self.resume_at = 0.0


def walk_feed(page, **kw) -> dict:
    """Scroll one page's feed. `walk_tabs` with one tab - see there."""
    return walk_tabs([page], **kw)


def walk_tabs(pages, *,
              speed: float = 90.0,
              slice_ms: int = 900,
              passes: int = 0,
              stuck_seconds: float = 15.0,
              comment_text: str = "",
              comment_wpm: int = 240,
              comment_gap: tuple[float, float] = (6.0, 18.0),
              comment_limit: int = 0,
              comment_offsets: tuple[str, ...] = COMMENT_ICON_OFFSETS,
              stall_seconds: float = 60.0,
              stall_misses: int = 2,
              like_first: bool = True,
              on_claim: Optional[Callable[[dict], None]] = None,
              on_note: Optional[Callable[[str], None]] = None,
              on_reload: Optional[Callable[[str], None]] = None,
              on_comment: Optional[Callable[[dict], None]] = None,
              on_pass: Optional[Callable[[dict], None]] = None,
              checkpoint: Optional[Callable[[float], None]] = None) -> dict:
    """Scroll the feed in every tab of `pages` AT ONCE, until told to stop.

    There is no natural end to this and it is not pretending there is one: a
    home feed is infinite, so the walk runs until `checkpoint` raises `Stopped`
    - which is the Stop button - or until `passes` looks if a caller sets one,
    or until `comment_limit` comments have landed across all tabs.

    Every tab scrolls continuously and simultaneously: the scrolling is done by
    a loop INSIDE each page (see `SCROLLER_JS`), so it does not need this thread
    at all. This loop wakes every `slice_ms`, and on each wake looks at each
    tab in turn - how far it has come, whether a post's comment bar is on
    screen - and comments where there is one. A comment holds THAT tab still
    for as long as it takes and then for the gap; the others keep moving
    through all of it. What cannot overlap is the comments themselves: all of
    an account's pages live on one browser thread, so two tabs' comments go
    one after the other. They take a few seconds each, and the scrolling never
    waits for them.

    Each tab is brought to the front for its comment - a click through
    Playwright waits on animation frames, and a tab at the back has none - and
    only then. The scrolling itself needs nothing brought forward.

    What is shared between tabs: the comment count and the cap, and the list
    of posts already commented on. Every tab is the same account on the same
    feed, and the same post shows up in more than one of them; without the
    shared list the second tab would comment on what the first just did.

    `speed` is pixels per second and is held CONSTANT for the whole walk. It is
    not rolled: re-rolling it is what would put the stepping back, one velocity
    change at a time, and a reader watching the screen sees a change of pace
    far more readily than a steady slow one.

    A feed that stops growing is nudged - a short scroll back up, then on down
    again - rather than sat at or sent back to the top. The nudge is what wakes
    Facebook's loader; a trip to the top is a restart the operator can see.

    `like_first` presses Like on each post before commenting on it, the way
    a person does. A like that cannot be made never stops the comment.

    `stall_seconds` is the watchdog, per tab. When commenting is on and no
    comment has LANDED in a tab for that long - stuck on a post, a composer
    that will not clear, a feed that stopped rendering, anything - that tab is
    reloaded and carries on from a fresh feed; `stall_misses` failed attempts
    in a row do the same without waiting for the clock. Posts already
    commented on are remembered by key across the reload. 0 turns it off.
    """
    tabs = [_Tab(page, i) for i, page in enumerate(pages)]
    if not tabs:
        raise AutomationError("There is no tab to scroll.")
    many = len(tabs) > 1

    speed = max(1.0, float(speed))
    tick = max(0.1, int(slice_ms) / 1000.0)
    # Expressed in seconds and converted, rather than counted in looks: the
    # tick is a responsiveness knob and the operator may well move it, and
    # "give up after 15 seconds of going nowhere" should not quietly become
    # thirty because the looks got closer together.
    stuck_looks = max(1, int(stuck_seconds / tick))

    def scroller(tab: _Tab, **flags) -> dict:
        """Talk to the loop in this tab's page: install, control, read back."""
        return tab.page.evaluate(SCROLLER_JS, {"post": POST, **flags}) or {}

    walked = 0
    commented = 0
    failed = 0
    keys: list[str] = []
    commenting = bool(comment_text)

    def capped() -> bool:
        return bool(comment_limit) and commented >= comment_limit

    def finished() -> bool:
        return capped() or (bool(passes) and walked >= passes)

    def tally() -> dict:
        return {"passes": walked, "posts": sum(tab.seen for tab in tabs),
                "commented": commented, "failed": failed, "tabs": len(tabs)}

    def front(tab: _Tab) -> None:
        if not many:
            return
        try:
            tab.page.bring_to_front()
        except Exception:                            # noqa: BLE001 - the click will say
            pass

    for tab in tabs:
        guard(tab.page)
        tab.seen = posts_on_screen(tab.page)
        scroller(tab, speed=speed, running=True)

    # How often each tab is checked for a captcha or a login wall, in seconds.
    # Not every look: the check costs a fifth of a second of the browser
    # thread on a Facebook page, and with five tabs that is a second a round
    # spent looking for a captcha instead of for posts. Every few seconds is
    # as prompt as anyone needs it, and it is ALSO run straight after every
    # comment, which is when a captcha actually arrives.
    guard_every = 5.0

    try:
        while not finished():
            if checkpoint:
                checkpoint(0)
            round_started = time.monotonic()

            for tab in tabs:
                if checkpoint:
                    checkpoint(0)
                page = tab.page
                now = time.monotonic()

                # Re-read as the walk goes, not just at the top. Facebook
                # interrupts a walk that is already going - that is WHEN a
                # checkpoint tends to arrive - and a walker that only looked
                # once carries on scrolling a captcha.
                if now - tab.guarded_at >= guard_every:
                    guard(page)
                    tab.guarded_at = now

                # Out of the gap? Let it move again.
                if tab.paused and tab.resume_at <= now:
                    tab.paused = False

                # One round-trip: control and reading together. This is also
                # what re-installs the loop after a reload took the page - and
                # its state - away.
                moved = scroller(tab, speed=speed, running=not tab.paused)
                tab.looks += 1
                posts = int(moved.get("posts") or 0)
                # Nowhere left to scroll AND nothing new rendered. Either alone
                # is not the end: a feed streams posts in behind the scroll, so
                # a look that finds no movement is often just the next batch
                # not having landed yet.
                stuck = (not tab.paused and not int(moved.get("moved") or 0)
                         and posts <= tab.seen)
                tab.barren = tab.barren + 1 if stuck else 0
                tab.seen = max(tab.seen, posts)

                if tab.paused or not commenting or capped():
                    pass
                else:
                    # The watchdog, checked BEFORE looking for a post rather
                    # than only after a failed one: a tab that finds nothing
                    # to claim for a minute is stuck just as surely as one
                    # that fails on what it finds, and only the clock catches
                    # that kind.
                    stalled = ""
                    if stall_seconds and now - tab.landed_at > stall_seconds:
                        stalled = f"no comment has landed for {int(stall_seconds)}s"
                    elif stall_misses and tab.misses >= stall_misses:
                        stalled = f"{tab.misses} comments in a row failed"
                    if stalled:
                        where = f"tab {tab.index + 1}: " if many else ""
                        if on_reload:
                            on_reload(where + stalled)
                        found_page = refresh_feed(page)
                        if on_reload:
                            on_reload(f"{where}reloaded facebook - {found_page}")
                        tab.landed_at = time.monotonic()
                        tab.misses = tab.dry = tab.barren = 0
                        tab.seen = posts_on_screen(page)
                        scroller(tab, speed=speed, running=True)
                        continue

                    found = survey(page, done=keys)
                    claimed = found.get("claim")

                    # Say something when a tab keeps finding nothing to comment
                    # on. Once, not every look - a feed genuinely between posts
                    # is normal and a line a second would bury the log - but a
                    # run that never stops on anything has to be able to say
                    # why, or it looks like commenting was never switched on.
                    if not claimed:
                        tab.dry += 1
                        if tab.dry == DRY_BEFORE_SAYING and on_note:
                            on_note(explain(found.get("why") or {}))
                    else:
                        tab.dry = 0

                    if claimed:
                        # Hold THIS page still first. A composer cannot be
                        # clicked or typed into while the page moves under it,
                        # and the button's position was read while it moved.
                        scroller(tab, running=False)
                        front(tab)
                        key = str(claimed.get("key") or "")
                        # Announced BEFORE the comment, not after: opening a
                        # composer, typing and waiting for Facebook to accept
                        # it takes seconds, and a panel that only reports
                        # finished comments looks frozen for all of them.
                        if on_claim:
                            on_claim({**tally(), "key": key, "tab": tab.index + 1})
                        try:
                            note = comment_on_claimed(page, comment_text, wpm=comment_wpm,
                                                      offsets=comment_offsets,
                                                      like_first=like_first)
                            ok = note in ("commented", "liked and commented")
                        except Exception as err:     # noqa: BLE001 - reported, not fatal
                            note, ok = str(err) or err.__class__.__name__, False

                        # Marked either way - see RELEASE_POST_JS. A post that
                        # could not be commented on is not retried, or the walk
                        # stops advancing.
                        release_post(page, done=ok)
                        if ok:
                            commented += 1
                            keys.append(key)
                            tab.landed_at = time.monotonic()
                            tab.misses = 0
                        else:
                            failed += 1
                            tab.misses += 1

                        if on_comment:
                            on_comment({**tally(), "key": key, "ok": ok, "note": note,
                                        "tab": tab.index + 1})

                        # A captcha most often arrives in answer to a comment,
                        # so this is the one place worth checking straight
                        # after acting rather than waiting for the next look.
                        guard(page)
                        tab.guarded_at = time.monotonic()

                        # This tab's gap: held still until it is over, while
                        # the others carry on. Not slept - nothing here sleeps
                        # for one tab.
                        gap = random.uniform(*sorted(comment_gap))
                        if gap > 0:
                            tab.paused = True
                            tab.resume_at = time.monotonic() + gap
                        else:
                            scroller(tab, running=True)

                if tab.barren >= stuck_looks:
                    # Nudged, never sent back to the top. Going back to the top
                    # was the "it goes down, then jumps up and starts again"
                    # the walk used to do every time it outran the loader; a
                    # feed that has truly ended is the watchdog's to reload.
                    scroller(tab, nudge=420)
                    tab.barren = 0

            walked += 1
            if on_pass:
                on_pass({**tally(), "at": 0, "height": 0})

            if finished():
                break
            # Between looks the pages scroll themselves. The wait is what is
            # LEFT of the tick after the round's own work: a look at a
            # Facebook tab costs a quarter of a second of this thread, so five
            # tabs would otherwise stretch the round to two seconds and a
            # comment bar could scroll through the window between looks. The
            # wait goes through `checkpoint` so Stop lands in the middle of it.
            wait = max(0.1, tick - (time.monotonic() - round_started))
            if checkpoint:
                checkpoint(wait)
            else:
                time.sleep(wait)
    finally:
        # The loops in the pages outlive this call unless told not to - a
        # Stop, a captcha, an error: the tabs must not carry on scrolling
        # after the walk that started them has gone.
        for tab in tabs:
            try:
                scroller(tab, running=False)
            except Exception:                        # noqa: BLE001 - page gone
                pass

    return {**tally(), "keys": keys}


# -- Joining groups ------------------------------------------------
# facebook.com/groups/discover: cards of suggested groups, each with a Join
# button. The run presses one, deals with whatever Facebook puts up in answer
# - a rules sheet to agree to, membership questions to answer, a plain
# confirmation - and moves to the next card, scrolling the page for more as
# the ones on screen are used up.
DISCOVER_URL = "https://www.facebook.com/groups/discover"

# The Join buttons on screen, and one of them claimed. Matched on what the
# button SAYS rather than on markup: the class names are generated per build,
# and "Join" is the one thing Facebook has to put on it for a person to press.
# Buttons inside a dialog are not cards, and buttons already pressed by this
# run are stamped and skipped - a card whose button was replaced by "Joined"
# no longer matches anyway.
FIND_JOIN_JS = r"""
(opts) => {
  const vh = window.innerHeight;
  // The button on a Discover card, as inspected: a div with role="button"
  // and aria-label="Join group, <name>" around the text "Join Group". The
  // label is the handle - it carries the group's name after the comma - and
  // the text is the fallback for a card whose label is missing or localised.
  const label = (el) => (el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const said = (el) => ((el.innerText || '') + ' ' + label(el)).replace(/\s+/g, ' ').trim().toLowerCase();
  const isJoin = (el) => {
    const l = label(el).toLowerCase();
    if (l.startsWith('join group') || l === 'join') return true;
    if (l.startsWith('hide ') || l.startsWith('leave') || l.startsWith('cancel')) return false;
    const t = said(el);
    return t === 'join' || t === 'join group' || t.startsWith('join ');
  };
  const out = { total: 0, done: 0, onScreen: 0, claimed: null };
  for (const el of document.querySelectorAll('[role="button"], button')) {
    if (el.closest('[role="dialog"]')) continue;
    if (!isJoin(el)) continue;
    out.total++;
    if (el.getAttribute('data-xbot-join') === '2') { out.done++; continue; }
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 16) continue;
    // On screen on BOTH axes. The "Friends' groups" row at the top is a
    // sideways carousel: its cards all sit at the same height, and most of
    // them are off to the right of the window where a click cannot land.
    if (r.top < 80 || r.bottom > vh - 20) continue;
    if (r.left < 0 || r.right > window.innerWidth) continue;
    // Never the same group twice in one run. A Join that is still loading
    // is re-rendered by Facebook without the stamp, and it used to be
    // pressed and counted again - three times over, as the log showed.
    const m0 = /^join group,\s*(.+)$/i.exec(label(el));
    const nm = m0 ? m0[1].trim() : '';
    if (nm && opts.skip && opts.skip.indexOf(nm) >= 0) { out.done++; continue; }
    out.onScreen++;
    if (!out.claimed) {
      document.querySelectorAll('[data-xbot-join="1"]').forEach(e => e.removeAttribute('data-xbot-join'));
      el.setAttribute('data-xbot-join', '1');
      // The group's name, for the log: from the label first ("Join group,
      // Askari XI Community"), else the nearest link into /groups/ above the
      // button, which on a card is the group's title.
      let name = '', up = el.parentElement, hops = 0;
      const m = /^join group,\s*(.+)$/i.exec(label(el));
      if (m) name = m[1].trim();
      while (up && hops < 10 && !name) {
        const a = up.querySelector('a[href*="/groups/"]');
        if (a) name = (a.innerText || a.getAttribute('aria-label') || '').trim().split('\n')[0];
        up = up.parentElement; hops++;
      }
      // And the group's address, from the same card: the first link on it
      // that points AT a group rather than into one. It is what lets a group
      // joined here be counted as the same group when the Group Posting
      // walk later finds it on the list.
      let url = '', up2 = el.parentElement, hops2 = 0;
      while (up2 && hops2 < 10 && !url) {
        for (const a of up2.querySelectorAll('a[href*="/groups/"]')) {
          let href = a.getAttribute('href') || '';
          try { href = new URL(href, location.origin).href; } catch (e) { continue; }
          const g = href.match(/facebook\.com\/groups\/([^/?#]+)/i);
          if (g && !/^(feed|discover|joins|create|notifications|search|your_groups|browse)$/i.test(g[1])) {
            url = 'https://www.facebook.com/groups/' + g[1] + '/';
            break;
          }
        }
        up2 = up2.parentElement; hops2++;
      }
      out.claimed = { name: name.slice(0, 90), url: url, label: said(el).slice(0, 40) };
    }
  }
  return out;
}
"""

# What the claimed button says now. Null when it is gone from the page, which
# after a Join is the usual outcome: Facebook replaces the button rather than
# relabelling it.
JOIN_STATE_JS = r"""
() => {
  const b = document.querySelector('[data-xbot-join="1"]');
  if (!b) return null;
  return ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim().toLowerCase();
}
"""

# What became of the card after Join was pressed, read by the group's NAME
# rather than through the button that was pressed - Facebook re-renders the
# card, and the pressed button is gone within a second whatever happened.
# Measured on the live page: the button disappears at once, and about three
# seconds later the card shows "Visit Group" (labelled "View <name>") for a
# group joined outright; a private group's card shows "Requested" or a
# "Cancel request" control instead. A card still offering "Join group, <name>"
# has not joined.
JOIN_RESULT_JS = r"""
(name) => {
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const n = norm(name);
  let stillJoin = false, visit = false, requested = false, hide = false;
  for (const el of document.querySelectorAll('[role="button"], button, a[role="link"]')) {
    const label = norm(el.getAttribute('aria-label'));
    if (!label || label.indexOf(n) < 0) continue;
    const text = norm(el.innerText);
    if (label.startsWith('join group')) stillJoin = true;
    else if (label.startsWith('view ') || text === 'visit group' || text === 'joined' || label.startsWith('leave')) visit = true;
    else if (label.startsWith('cancel') || text === 'requested' || text.startsWith('cancel') || label.indexOf('request') >= 0) requested = true;
    else if (label.startsWith('hide ')) hide = true;
  }
  return { stillJoin, visit, requested, cardThere: stillJoin || visit || requested || hide };
}
"""

# One look at the dialog Facebook put up, if any, and a plan for it: fields to
# fill, boxes to tick, the one button to press. The plan is carried out from
# Python because typing and clicking are Playwright's; this only decides.
#
# The button is chosen by what it says, most decisive first: Submit (the
# membership-questions sheet), Join / Join group (a confirmation), Agree (the
# rules sheet), then the generic yeses. Nothing that says cancel, close, not
# now, skip or back is ever picked - those are the way OUT, which is the last
# resort and is taken from Python when no way in is left.
DIALOG_PLAN_JS = r"""
() => {
  // Only a dialog that is actually up, and never Facebook's own flyouts.
  // The Notifications panel is a `role="dialog"` that sits in the DOM at
  // zero height while closed - and it holds Confirm buttons for friend
  // requests, which is the last thing a join should be pressing.
  const flyout = (d) => /^(notifications|chats|messenger|menu|account)/i
                          .test(((d.getAttribute('aria-label') || '') + ' ' + (d.innerText || '')).trim());
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                       .filter(d => d.getBoundingClientRect().height > 40 && !flyout(d));
  const dlg = dialogs[dialogs.length - 1];
  document.querySelectorAll('[data-xbot-fill], [data-xbot-tick], [data-xbot-press], [data-xbot-close]')
          .forEach(e => { for (const a of ['data-xbot-fill','data-xbot-tick','data-xbot-press','data-xbot-close']) e.removeAttribute(a); });
  if (!dlg) return { dialog: false };
  // What a button says, as the two things it can say - its text and its
  // label - kept APART. An X button labelled "Close" reads "x close" joined
  // up, and matches nothing; read separately, its label is "close".
  const says = (el) => [el.innerText || '', el.getAttribute('aria-label') || '', el.getAttribute('title') || '']
                         .map(t => t.replace(/\s+/g, ' ').trim().toLowerCase()).filter(Boolean);
  const text = (el) => says(el).join(' ');
  const saysOne = (el, w) => says(el).some(t => t === w || t.startsWith(w + ' ') || t.endsWith(' ' + w));
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  let fills = 0, ticks = 0;
  for (const f of dlg.querySelectorAll('textarea, input[type="text"], [contenteditable="true"][role="textbox"]')) {
    if (!visible(f)) continue;
    const empty = f.tagName === 'TEXTAREA' || f.tagName === 'INPUT' ? !f.value.trim() : !(f.innerText || '').trim();
    if (empty) { f.setAttribute('data-xbot-fill', '1'); fills++; }
  }
  for (const c of dlg.querySelectorAll('[role="checkbox"], input[type="checkbox"], [role="radio"]')) {
    if (!visible(c)) continue;
    const on = c.getAttribute('aria-checked') === 'true' || c.checked === true;
    // Radios: tick the first of each group only; checkboxes: every one.
    if (c.getAttribute('role') === 'radio') {
      const name = c.getAttribute('name') || c.parentElement;
      if (c.closest('[role="radiogroup"]') && c.closest('[role="radiogroup"]').querySelector('[data-xbot-tick]')) continue;
    }
    if (!on) { c.setAttribute('data-xbot-tick', '1'); ticks++; }
  }

  const wanted = ['submit', 'join group', 'join', 'agree', 'i agree', 'accept', 'confirm',
                  'continue', 'next', 'done', 'ok', 'okay', 'got it', 'yes', 'send'];
  const never = ['cancel', 'close', 'not now', 'skip', 'back', 'no thanks', 'leave', 'decline', 'report'];
  const buttons = Array.from(dlg.querySelectorAll('[role="button"], button')).filter(visible);
  let press = null, pressSaid = '';
  outer: for (const w of wanted) {
    for (const b of buttons) {
      if (!text(b) || never.some(n => saysOne(b, n))) continue;
      if (saysOne(b, w)) { press = b; pressSaid = w; break outer; }
    }
  }
  if (press) press.setAttribute('data-xbot-press', '1');
  const closer = buttons.find(b => ['close', 'cancel', 'not now'].some(n => saysOne(b, n)));
  if (closer) closer.setAttribute('data-xbot-close', '1');
  const title = (dlg.querySelector('h1, h2, h3, [role="heading"]') || {}).innerText || '';
  return { dialog: true, fills: fills, ticks: ticks, press: pressSaid, hasClose: !!closer,
           title: title.replace(/\s+/g, ' ').trim().slice(0, 80) };
}
"""

# Something to say to a membership question. Every question gets the same
# answer: it is the one thing that is true of every prospective member and
# reads as a person rather than a blank.
JOIN_ANSWER = "Yes, I agree to the group rules and I'm interested in this group."


def settle_dialog(page, *, rounds: int = 5,
                  on_step: Optional[Callable[[str], None]] = None) -> str:
    """Deal with whatever Facebook put up after a Join. Returns how it ended.

    "none" - there was no dialog. "done" - a way in was found and pressed and
    the dialog went away (rules agreed to, questions answered, request sent).
    "closed" - no way in was left after `rounds` tries and it was dismissed,
    which counts as a group not joined.

    Each round: fill the empty answer boxes, tick the unticked boxes, press
    the most decisive button on offer, wait a beat, look again. Several
    rounds because Facebook's sheets come in sequence - rules, then questions,
    then a confirmation - and each press brings the next.
    """
    for n in range(rounds):
        plan = page.evaluate(DIALOG_PLAN_JS) or {}
        if not plan.get("dialog"):
            return "none" if n == 0 else "done"
        if on_step:
            bits = [f'dialog "{plan.get("title")}"' if plan.get("title") else "a dialog"]
            if plan.get("fills"):
                bits.append(f"{plan['fills']} question(s) to answer")
            if plan.get("ticks"):
                bits.append(f"{plan['ticks']} box(es) to tick")
            bits.append(f'pressing "{plan["press"]}"' if plan.get("press") else "nothing to press")
            on_step(", ".join(bits))

        for i in range(int(plan.get("fills") or 0)):
            field = page.locator('[data-xbot-fill="1"]').nth(i)
            try:
                field.click(timeout=3_000)
                page.keyboard.type(JOIN_ANSWER, delay=random.uniform(20, 60))
            except Exception:                        # noqa: BLE001 - the press will tell
                pass
        for i in range(int(plan.get("ticks") or 0)):
            try:
                page.locator('[data-xbot-tick="1"]').nth(i).click(timeout=3_000)
            except Exception:                        # noqa: BLE001
                pass
        if not plan.get("press"):
            break
        try:
            page.locator('[data-xbot-press="1"]').first.click(timeout=5_000)
        except Exception:                            # noqa: BLE001 - try the way out
            break
        page.wait_for_timeout(random.uniform(1_200, 2_000))

    # No way in. Out, then: the X if it has one, Escape if not.
    plan = page.evaluate(DIALOG_PLAN_JS) or {}
    if not plan.get("dialog"):
        return "done"
    if plan.get("hasClose"):
        try:
            page.locator('[data-xbot-close="1"]').first.click(timeout=3_000)
            page.wait_for_timeout(600)
        except Exception:                            # noqa: BLE001
            pass
    if (page.evaluate(DIALOG_PLAN_JS) or {}).get("dialog"):
        close_dialog(page)
    return "closed"


def join_groups(page, *, want: int = 5, gap: tuple[float, float] = (2.0, 5.0),
                max_tries: int = 0,
                on_step: Optional[Callable[[str], None]] = None,
                on_join: Optional[Callable[[dict], None]] = None,
                checkpoint: Optional[Callable[[float], None]] = None) -> dict:
    """Join `want` groups from the Discover page. Returns the tally.

    Opens the page, then: find a Join button on screen, press it, settle
    whatever comes up, count it, wait a rolled gap, repeat. When no Join
    button is on screen the page is scrolled for more, and a page that stops
    producing them ends the run early with the count it reached.

    A press is counted as JOINED when the button is gone or no longer says
    Join afterwards - Facebook swaps it for "Joined" or "Requested" - and the
    dialog, if there was one, was got through rather than closed. Requests to
    private groups count: the join is made, the group's admins decide when.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)
    def wait(seconds: float) -> None:
        if checkpoint:
            checkpoint(seconds)
        else:
            time.sleep(seconds)

    goto(page, DISCOVER_URL)
    found = settle(page, timeout=20_000)
    if "log in" in found or "asking" in found:
        raise AutomationError("Facebook is asking this window to log in.")
    guard(page)
    page.wait_for_timeout(2_000)
    step("the Discover page is up")

    joined = 0
    tried = 0
    names: list[str] = []
    pressed: list[str] = []          # every name pressed, joined or not
    dry_scrolls = 0
    want = max(1, int(want))
    max_tries = max_tries or want * 4

    while joined < want and tried < max_tries:
        if checkpoint:
            checkpoint(0)
        look = page.evaluate(FIND_JOIN_JS, {"skip": pressed}) or {}
        claimed = look.get("claimed")
        if not claimed:
            # Nothing pressable on screen: scroll for more. Six scrolls that
            # turn up nothing new is the page having nothing more to offer.
            before = int(look.get("total") or 0)
            page.evaluate("() => window.scrollBy({ top: Math.round(window.innerHeight * 0.7), behavior: 'smooth' })")
            wait(random.uniform(1.2, 2.0))
            after = int((page.evaluate(FIND_JOIN_JS, {"skip": pressed}) or {}).get("total") or 0)
            dry_scrolls = dry_scrolls + 1 if after <= before else 0
            if dry_scrolls >= 6:
                step("no more groups to join on this page")
                break
            continue
        dry_scrolls = 0

        tried += 1
        name = claimed.get("name") or "a group"
        pressed.append(name)
        step(f'pressing Join on "{name}"')
        button = page.locator('[data-xbot-join="1"]').first
        try:
            button.click(timeout=6_000)
        except Exception as err:                     # noqa: BLE001 - next card
            step(f"could not press it - {str(err).splitlines()[0][:100]}")
            continue

        # Wait for the card to RESOLVE, up to twelve seconds. The button goes
        # at once and the outcome - Visit Group, Requested, or Join again -
        # takes a few seconds to land; reading it earlier reads nothing. A
        # dialog that comes up in the meantime is dealt with as it appears.
        ended = "none"
        result: dict = {}
        deadline = time.monotonic() + 12.0
        while True:
            plan = page.evaluate(DIALOG_PLAN_JS) or {}
            if plan.get("dialog"):
                got = settle_dialog(page, on_step=step)
                if got == "closed":
                    ended = "closed"
                    break
                ended = "done"
            result = page.evaluate(JOIN_RESULT_JS, name) or {}
            if result.get("visit") or result.get("requested"):
                break
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(700)

        ok = ended != "closed" and bool(result.get("visit") or result.get("requested"))
        if ok:
            joined += 1
            names.append(name)
        how = "joined" if result.get("visit") else "requested"
        note = (how if ok else
                "a dialog could not be got through and was closed" if ended == "closed" else
                "the card still says Join Group" if result.get("stillJoin") else
                "the card did not change" if result.get("cardThere") else
                "the card disappeared before it said anything")
        step(f'"{name}": {note}')
        if on_join:
            on_join({"name": name, "url": claimed.get("url") or "", "ok": ok,
                     "note": note, "joined": joined, "tried": tried, "want": want})
        guard(page)
        if joined < want:
            wait(random.uniform(*sorted(gap)))

    return {"joined": joined, "tried": tried, "want": want, "names": names}



# -- The groups this account is in ---------------------------------
# Facebook's own "Your groups" page: every group the account is a member of,
# as a list of cards that fills in as the page is scrolled.
JOINED_URL = "https://www.facebook.com/groups/joins/?nav_source=tab"

# The list of joined groups is the LEFT COLUMN of the Groups page - "Groups
# you've joined", one link per group with the name over a "Last active" line,
# loading more as that column is scrolled. The main column is a feed of posts
# from those groups, and reading links out of it yields a post's "9.7K
# members" or its author as if they were group names - which is exactly what
# the first cut of this did. So: the navigation column when it has any, the
# whole page otherwise, and only links that point AT a group (not into one),
# with the page's own items - Feed, Discover, Your groups, Create - skipped by
# name and anything that reads like a count or a date skipped by shape.
LIST_GROUPS_JS = r"""() => {
  const skip = /\/groups\/(feed|discover|joins|create|notifications|search|your_groups|browse)\b/i;
  const junk = /^(visit|view group|join|see all|\d[\d.,]*\s*k?\s*members?|last active\b.*)$/i;
  const read = root => {
    const out = [];
    const seen = new Set();
    for (const a of root.querySelectorAll('a[href*="/groups/"]')) {
      let href = a.getAttribute('href') || '';
      try { href = new URL(href, location.origin).href; } catch (e) { continue; }
      // At a group, not into one: /groups/<id>/ and nothing after it.
      const m = href.match(/facebook\.com\/groups\/([^/?#]+)\/?(?:[?#].*)?$/i);
      if (!m || skip.test(href)) continue;
      const key = m[1].toLowerCase();
      if (seen.has(key)) continue;
      let line = ((a.innerText || a.getAttribute('aria-label') || '').trim().split('\n')[0] || '').trim();
      // The name and its "Last active" line are separate blocks on the real
      // page; on a flattened one they share a line, so the tail is cut off.
      line = line.replace(/\s*last active\b.*$/i, '').trim();
      if (!line || junk.test(line)) continue;
      seen.add(key);
      out.push({ key, name: line, url: 'https://www.facebook.com/groups/' + m[1] + '/' });
    }
    return out;
  };
  const navs = [...document.querySelectorAll('[role="navigation"]')];
  for (const nav of navs) {
    const found = read(nav);
    if (found.length) return { from: 'nav', groups: found };
  }
  return { from: 'page', groups: read(document.body) };
}"""

# Scroll the list to its end and say how many groups are on it now.
#
# Two things in one call because the caller needs both to tell "the list is
# still loading" from "the list has ended", and asking twice lets the page
# change in between. Straight to the bottom rather than in 80% steps: after
# coming back from a group the column starts again near the top, and stepping
# down a list of forty groups a fifth of a screen at a time is most of a
# minute of doing nothing. The bottom is also where the loader lives.
SEEK_JS = r"""() => {
  const at = (a) => {
    let href = a.getAttribute('href') || '';
    try { href = new URL(href, location.origin).href; } catch (e) { return false; }
    return /facebook\.com\/groups\/[^/?#]+\/?$/i.test(href)
        && !/\/groups\/(feed|discover|joins|create|notifications|search|your_groups|browse)\b/i.test(href);
  };
  const links = [...document.querySelectorAll('[role="navigation"] a[href*="/groups/"]')].filter(at);
  const total = links.length;
  const last = links[links.length - 1];
  if (!last) {
    window.scrollTo({ top: document.body.scrollHeight });
    return { total: total, box: 'window' };
  }
  let box = last.parentElement;
  while (box && box !== document.body) {
    const cs = getComputedStyle(box);
    if (/(auto|scroll)/.test(cs.overflowY) && box.scrollHeight > box.clientHeight + 10) {
      box.scrollTop = box.scrollHeight;
      return { total: total, box: 'column' };
    }
    box = box.parentElement;
  }
  last.scrollIntoView({ block: 'end', behavior: 'instant' });
  window.scrollTo({ top: document.body.scrollHeight });
  return { total: total, box: 'window' };
}"""


# Scroll whatever holds the list. The left column is its own scroll box, so
# scrolling the window does nothing to it; the last group link is brought
# into view (which scrolls the box it is in) and the box is then pushed on
# by most of its height. Returns which box moved, for the log.
SCROLL_LIST_JS = r"""() => {
  const links = [...document.querySelectorAll('[role="navigation"] a[href*="/groups/"]')]
    .filter(a => /facebook\.com\/groups\/[^/?#]+\/?$/i.test(new URL(a.getAttribute('href') || '', location.origin).href));
  const last = links[links.length - 1];
  if (last) {
    last.scrollIntoView({ block: 'end', behavior: 'instant' });
    let box = last.parentElement;
    while (box && box !== document.body) {
      const cs = getComputedStyle(box);
      if (/(auto|scroll)/.test(cs.overflowY) && box.scrollHeight > box.clientHeight + 10) {
        box.scrollTop = box.scrollTop + Math.round(box.clientHeight * 0.8);
        return 'column';
      }
      box = box.parentElement;
    }
    window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: 'instant' });
    return 'window';
  }
  window.scrollBy({ top: Math.round(window.innerHeight * 0.8), behavior: 'instant' });
  return 'window';
}"""


def list_joined_groups(page, *,
                       on_step: Optional[Callable[[str], None]] = None,
                       on_group: Optional[Callable[[dict], None]] = None,
                       checkpoint: Optional[Callable[[float], None]] = None,
                       max_scrolls: int = 80) -> list[dict]:
    """Read every group this account is in, off its "Your groups" page.

    Opens the page, reads the cards on screen, scrolls for more, and stops
    when six scrolls in a row turn up nothing new - which is the list having
    run out. Each group is reported through `on_group` the moment it is first
    seen, so the panel fills as the page does. Returns the whole list, in the
    order Facebook showed it.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)

    def wait(seconds: float) -> None:
        if checkpoint:
            checkpoint(seconds)
        else:
            time.sleep(seconds)

    goto(page, JOINED_URL)
    found = settle(page, timeout=20_000)
    if "log in" in found or "asking" in found:
        raise AutomationError("Facebook is asking this window to log in.")
    guard(page)
    try:
        page.wait_for_selector('[role="main"]', timeout=15_000)
    except Exception:                                # noqa: BLE001 - read whatever is there
        pass
    page.wait_for_timeout(1_500)
    step("the Your groups page is up")

    groups: list[dict] = []
    seen: set[str] = set()
    dry = 0
    where = "page"
    for _ in range(max_scrolls):
        if checkpoint:
            checkpoint(0)
        fresh = 0
        look = page.evaluate(LIST_GROUPS_JS) or {}
        where = look.get("from") or where
        for group in look.get("groups") or []:
            if group["key"] in seen:
                continue
            seen.add(group["key"])
            groups.append({"name": group["name"], "url": group["url"]})
            fresh += 1
            if on_group:
                on_group(groups[-1])
        dry = 0 if fresh else dry + 1
        # Eight quiet scrolls, not six: the column fetches its next page from
        # the network and a slow one takes a few seconds to land.
        if dry >= 8:
            break
        step(f"{len(groups)} groups so far - scrolling the {'list' if where == 'nav' else 'page'} for more")
        page.evaluate(SCROLL_LIST_JS)
        wait(random.uniform(1.5, 2.5))
        guard(page)

    try:
        page.evaluate("() => window.scrollTo({ top: 0, behavior: 'smooth' })")
    except Exception:                                # noqa: BLE001 - tidying only
        pass
    step(f"{len(groups)} groups in all")
    return groups


# -- Visiting each group -------------------------------------------
# The title at the top of a group's page, read as proof the page is the group.
GROUP_TITLE_JS = r"""() => {
  const h = document.querySelector('[role="main"] h1, h1');
  return h ? (h.innerText || '').trim().split('\n')[0] : '';
}"""

# Facebook's own way of saying the group is not there any more, or not for
# this account. The page still sits at a /groups/ address, so the address
# alone cannot tell a group that opened from one that did not.
GONE_JS = r"""() => {
  const t = (document.body.innerText || '').slice(0, 3000);
  return /this content isn.t available|content isn.t available right now|page isn.t available|isn.t available right now|sorry, this content/i.test(t);
}"""


# A welcome sheet, a rules sheet, "you joined this group" - the panels a group
# puts up over itself the first time you open it. They are CLOSED, never
# pressed through: the buttons on them say things like Join, Agree and Invite,
# and a walk that is only looking at groups has no business pressing any of
# them. So this finds the way OUT and nothing else.
#
# The X in the top right corner, found two ways because Facebook ships both:
# by its label (it is an "Close" button on most sheets) and, when nothing is
# labelled, by where it sits - a small square button in the top-right quarter
# of the sheet. Flyouts are left alone for the same reason the join flow
# leaves them alone: the Notifications panel is a `role="dialog"` too.
POPUP_CLOSE_JS = r"""() => {
  const flyout = (d) => /^(notifications|chats|messenger|menu|account)/i
    .test(((d.getAttribute('aria-label') || '') + ' ' + (d.innerText || '')).trim());
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-xbot-x], [data-xbot-dlg]')
          .forEach(e => { e.removeAttribute('data-xbot-x'); e.removeAttribute('data-xbot-dlg'); });

  const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
    .filter(d => d.getBoundingClientRect().height > 40 && !flyout(d));
  const dlg = dialogs[dialogs.length - 1];
  if (!dlg) return { dialog: false };

  const box = dlg.getBoundingClientRect();
  const title = ((dlg.querySelector('h1, h2, h3, [role="heading"]') || {}).innerText || '')
    .replace(/\s+/g, ' ').trim().slice(0, 80);
  const buttons = Array.from(dlg.querySelectorAll('[role="button"], button, [aria-label]')).filter(visible);

  const labelled = (el) => [el.getAttribute('aria-label') || '', el.getAttribute('title') || '']
    .map(t => t.replace(/\s+/g, ' ').trim().toLowerCase())
    .some(t => t === 'close' || t.startsWith('close ') || t === 'dismiss');

  // By label first.
  let x = buttons.find(labelled);
  // Then by corner: small, square-ish, and in the top-right quarter of the sheet.
  if (!x) {
    x = buttons.filter(b => {
      const r = b.getBoundingClientRect();
      if (r.width > 64 || r.height > 64 || r.width < 12 || r.height < 12) return false;
      if (Math.abs(r.width - r.height) > 16) return false;
      return r.top < box.top + box.height * 0.25 && r.right > box.right - box.width * 0.28;
    }).sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0];
  }
  if (x) x.setAttribute('data-xbot-x', '1');
  // Marked so the caller can ask whether THIS sheet went, rather than whether
  // any dialog is left: closing a welcome panel often reveals the rules panel
  // behind it, and "another one is up" is not the same as "it would not go".
  dlg.setAttribute('data-xbot-dlg', '1');
  return { dialog: true, hasX: !!x, title: title };
}"""

# Is the sheet we just pressed the X on still up?
POPUP_STILL_JS = r"""() => {
  const d = document.querySelector('[data-xbot-dlg="1"]');
  if (!d || !d.isConnected) return false;
  const r = d.getBoundingClientRect();
  return r.height > 40 && r.width > 0;
}"""


def dismiss_popup(page, *, tries: int = 4,
                  on_step: Optional[Callable[[str], None]] = None) -> int:
    """Close whatever a group has put up over itself. Returns how many went.

    The X in the corner, pressed; Escape only when there is no X to press.
    Several rounds because one sheet often hides another behind it - a
    welcome panel with the rules waiting under it - and each close reveals
    the next.

    Nothing on the sheet is ever pressed except the way out. Agreeing to a
    group's rules is a decision, and this walk is not the thing that takes it.
    """
    gone = 0
    for _ in range(tries):
        try:
            found = page.evaluate(POPUP_CLOSE_JS) or {}
        except Exception:                            # noqa: BLE001 - nothing readable, nothing to close
            return gone
        if not found.get("dialog"):
            return gone
        what = found.get("title") or "a panel"
        if found.get("hasX"):
            try:
                page.locator('[data-xbot-x="1"]').first.click(timeout=3_000)
                page.wait_for_timeout(350)
            except Exception:                        # noqa: BLE001 - Escape below
                pass
        # THIS sheet still there, or there was no X: Escape, which every sheet
        # honours. Asked of the sheet itself, not of the page - a second panel
        # revealed behind the first is the next round's business, not a failure.
        if page.evaluate(POPUP_STILL_JS):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except Exception:                        # noqa: BLE001 - already gone
                pass
        if page.evaluate(POPUP_STILL_JS):
            # It will not go. Said out loud rather than looped over in silence.
            if on_step:
                on_step(f'"{what}" would not close')
            return gone
        gone += 1
        if on_step:
            on_step(f'closed "{what}"')
    return gone


def visit_groups(page, groups: list[dict], *,
                 gap: tuple[float, float] = (2.5, 6.0),
                 list_url: str = "",
                 on_step: Optional[Callable[[str], None]] = None,
                 on_visit: Optional[Callable[[dict], None]] = None,
                 checkpoint: Optional[Callable[[float], None]] = None) -> int:
    """Open every group in `groups`, one after another. Returns how many opened.

    Each is reached by PRESSING its link in the left-hand list, the way a hand
    would. That list only exists on the Groups page, and a group's own page
    swaps it for the group's menu - so between visits the window goes back to
    the list, and the next name is pressed from there. Only when the link
    cannot be found at all does it fall back to the group's address.

    A group that opens with a welcome or rules sheet over it has that sheet
    CLOSED by its X before anything is read - see `dismiss_popup`. Nothing on
    such a sheet is ever pressed: agreeing to a group's rules is a decision,
    and looking through the groups is not the thing that takes it.

    A visit counts as opened when the page settled on a group with a title on
    it and Facebook did not say the content is unavailable - the address alone
    is no proof, because a dead group keeps its /groups/ address.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)

    def wait(seconds: float) -> None:
        if checkpoint:
            checkpoint(seconds)
        else:
            time.sleep(seconds)

    home = list_url or (page.url if "/groups" in (page.url or "") else JOINED_URL)

    def link_for(key: str):
        found = page.locator(f'[role="navigation"] a[href*="/groups/{key}"]').first
        return found if key and found.count() > 0 else None

    opened = 0
    total = len(groups)
    for index, group in enumerate(groups):
        if checkpoint:
            checkpoint(0)
        name = group.get("name") or "a group"
        url = group.get("url") or ""
        key = url.rstrip("/").rsplit("/", 1)[-1]
        step(f'opening "{name}" ({index + 1} of {total})')
        how = ""
        title = ""
        ok = False
        shut = 0
        try:
            link = link_for(key)
            if link is None and key:
                # The list is not on screen - the last group's page replaced
                # it. Back to the list, then press the name there.
                goto(page, home)
                page.wait_for_timeout(1_500)
                link = link_for(key)
            if link is not None:
                link.scroll_into_view_if_needed(timeout=3_000)
                link.click(timeout=6_000)
                how = "clicked"
            else:
                goto(page, url)
                how = "opened by address"
            page.wait_for_timeout(1_500)
            guard(page)
            # A group often opens with a welcome or rules sheet over it. It is
            # closed BEFORE the title is read - the sheet's own heading would
            # otherwise be read as the group's name - and closing it is what
            # leaves the window on the group itself.
            shut = dismiss_popup(page, on_step=on_step)
            if shut:
                page.wait_for_timeout(600)
            for _ in range(8):
                title = page.evaluate(GROUP_TITLE_JS) or ""
                if title:
                    break
                page.wait_for_timeout(500)
            gone = bool(page.evaluate(GONE_JS))
            ok = bool(title) and not gone and "/groups/" in (page.url or "")
            if not ok:
                how = ("Facebook says this content is not available" if gone else
                       "the group did not come up")
        except Exception as err:                     # noqa: BLE001 - one group, not the walk
            ok = False
            how = f"could not open - {str(err).splitlines()[0][:100]}"
        if ok:
            opened += 1
            if shut:
                how += f", closed {shut} panel(s)"
        if on_visit:
            on_visit({"name": name, "url": url, "ok": ok, "title": title,
                      "how": how, "closed": shut, "index": index, "total": total})
        if index + 1 < total:
            wait(random.uniform(*gap))
    step(f"visited {opened} of {total} groups")
    return opened


def _tokens(group: dict) -> set:
    """How a chosen group can be recognised on the list: by the address segment
    it is keyed on, and by its name for a group whose address was never seen."""
    name = " ".join(str(group.get("name") or "").split()).lower()
    return {str(group.get("key") or "").lower(), "name:" + name}


def walk_joined_groups(page, *,
                       gap: tuple[float, float] = (1.0, 2.5),
                       limit: int = 0,
                       direct: bool = False,
                       post: Optional[dict] = None,
                       only: Optional[set] = None,
                       skip: Optional[set] = None,
                       on_open: Optional[Callable[[object, dict], None]] = None,
                       on_step: Optional[Callable[[str], None]] = None,
                       on_group: Optional[Callable[[dict], None]] = None,
                       on_visit: Optional[Callable[[dict], None]] = None,
                       checkpoint: Optional[Callable[[float], None]] = None) -> list[dict]:
    """Go through the joined groups, opening each one as it is found.

    One pass, not two. The earlier version read the WHOLE list first and only
    then started opening - which on an account in fifty groups meant minutes
    of scrolling before anything happened, and a stop halfway through meant a
    list of names and nothing visited. This finds a name and opens it, finds
    the next and opens that.

    The shape of each round: read EVERY name on the list that has not been
    opened yet, then go through that whole run of them one after another -
    the first pressed on the list where the window is standing, each one after
    it gone to straight from the group before. Only when the run is finished
    does the window come back to the list, scroll for the names below, and
    start another. Ten scrolls that grow the list by nothing is the list
    having ended.

    One tab for the whole walk, and the Groups page seen once per run rather
    than once per group. The trip back cannot be got rid of altogether: the
    list is the only place the walk can learn which groups exist, and a row
    that is not on screen cannot be pressed. What it can be is rare - a list
    that hands over twenty names costs one return, not twenty.

    `open_list` makes that trip as cheaply as it can be made - history, then
    the rail's own "Your groups", and a real load only when neither lands.

    Returns every group seen, in the order found, each with what became of it.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)

    def wait(seconds: float) -> None:
        if checkpoint:
            checkpoint(seconds)
        else:
            time.sleep(seconds)

    def on_list() -> bool:
        """Is the window on the FULL list of joined groups?

        `/groups/joins` and nothing else. The Groups landing page at
        `/groups` looks close enough - it has the same rail and a column of
        joined groups down the side - but that column is a preview with a
        "See all" under it, half a dozen names where the account has forty.
        Accepting it is how a walk ends after six groups and reports that as
        the lot, so the landing page is treated as somewhere to click OUT of.
        """
        url = (page.url or "").split("?")[0].rstrip("/")
        return url.endswith("/groups/joins")

    def press_your_groups() -> bool:
        """Press "Your groups" in the rail - a click, not a load."""
        for selector in ('[role="navigation"] a[href*="/groups/joins"]',
                         'a[href*="/groups/joins"]'):
            try:
                tab = page.locator(selector).first
                if tab.count() > 0:
                    tab.click(timeout=5_000)
                    for _ in range(16):
                        if on_list():
                            return True
                        page.wait_for_timeout(250)
            except Exception:                        # noqa: BLE001 - the next way
                pass
        return on_list()

    def open_list() -> bool:
        """Back to the full list, without reloading it if that can be avoided.

        Four ways, in the order they cost: already there; the back button,
        which an app of this kind answers with a route change; the rail's own
        "Your groups", still a click, which is also what gets off the landing
        page when back lands there. A real load is the last resort and says so
        in the log - it is the one that loses the column's place.
        """
        if on_list():
            return True
        # Back, as many times as the visit went deep: a group's People tab is
        # two steps from the list, not one.
        for _ in range(3):
            try:
                page.go_back(timeout=15_000)
                page.wait_for_timeout(900)
            except Exception:                        # noqa: BLE001 - try the rail
                break
            if on_list():
                return True
            # Only while still INSIDE a group - its page, its People tab.
            # Anywhere else (the Groups landing page, above all) another step
            # back leaves Facebook altogether and costs a whole page load; the
            # rail's "Your groups" below is the way on from there.
            parts = (page.url or "").split("?")[0].split("#")[0]                 .split("facebook.com", 1)[-1].strip("/").split("/")
            inside = (len(parts) >= 2 and parts[0] == "groups"
                      and parts[1] not in ("", "feed", "discover", "joins"))
            if not inside:
                break
        if press_your_groups():
            return True
        step("the list did not come back on its own - loading it")
        goto(page, JOINED_URL)
        page.wait_for_timeout(1_500)
        guard(page)
        return on_list()

    # Already there when the panel opened the window straight on this page -
    # which it does - so no second load of it.
    if not on_list():
        goto(page, JOINED_URL)
    # Wait for the ONE thing this walk needs, the column of group links, and
    # go the moment it is there.
    #
    # This used to call `settle`, which waits for a NEWS FEED. The Groups page
    # has no news feed, so every run sat out that wait's whole twenty seconds
    # before touching the first group - on a page that had been ready for most
    # of them. A sign-in wall and a challenge are still noticed on the same
    # quick poll, so neither is waited out either.
    deadline = time.monotonic() + 20
    while True:
        if (page.evaluate(LIST_GROUPS_JS) or {}).get("groups"):
            break
        if signed_out(page):
            raise AutomationError("Facebook is asking this window to log in.")
        guard(page)
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(200)
    step("the Groups page is up")

    walked: list[dict] = []
    seen: set[str] = set()
    opened = 0

    while True:
        if checkpoint:
            checkpoint(0)
        if limit and len(walked) >= limit:
            step(f"stopping at {limit} groups")
            break

        # Back to the list, and down it until a name turns up that has not
        # been opened yet.
        open_list()
        # The column comes back a moment after the route does. Waited for, so
        # a list that is still rendering is not read as a list that has ended.
        for _ in range(12):
            if (page.evaluate(LIST_GROUPS_JS) or {}).get("groups"):
                break
            page.wait_for_timeout(400)
        # EVERY name on the list that has not been opened yet, not just the
        # next one. What follows goes through the whole run of them without
        # coming back here - see below - so this is the only trip to the list
        # those groups cost between them.
        batch: list[dict] = []
        dry = 0
        while not batch:
            if checkpoint:
                checkpoint(0)
            look = page.evaluate(LIST_GROUPS_JS) or {}
            batch = [g for g in (look.get("groups") or []) if g["key"] not in seen]
            if only is not None or skip:
                # A group that was not chosen - or was already done by an
                # earlier go at this walk - is passed over WITHOUT a click, and
                # remembered as seen so the next look does not weigh it again.
                # The walk carries on down the list for the rest.
                keep = []
                for g in batch:
                    chosen = only is None or bool((_tokens(g) & only) - seen)
                    if chosen and not (skip and g["key"] in skip):
                        keep.append(g)
                    else:
                        seen.add(g["key"])
                batch = keep
                if only is not None and not (only - seen):
                    break
            if batch:
                break
            # Nothing new on the list as it stands. Scroll, and judge the
            # scroll by whether the list GREW - not by whether it turned up a
            # group that had not been opened yet.
            #
            # That distinction is the whole of it. Coming back from a group
            # leaves the column near the top, so the first however-many
            # scrolls only bring back names already visited; counting those
            # as failures made the walk give up after eight of them and call
            # a list of forty groups finished at about twenty. Now a scroll
            # only counts against the list when the list stops getting longer.
            before = int((page.evaluate(SEEK_JS) or {}).get("total") or 0)
            wait(random.uniform(0.7, 1.2))
            after = int((page.evaluate(SEEK_JS) or {}).get("total") or 0)
            if after > before:
                dry = 0
                continue
            dry += 1
            if dry >= 10:
                break
            # Give a slow fetch a moment longer before counting it against
            # the list - the column asks the network for its next page.
            wait(random.uniform(0.5, 0.9))
        if not batch:
            if only is not None and not (only - seen):
                step(f"every chosen group has been done - {opened} opened")
            else:
                step(f"no more groups on the list - {len(walked)} found, {opened} opened")
            break
        if limit:
            batch = batch[:max(0, limit - len(walked))]
        if not direct:
            # THE DEFAULT. One group per trip to the list, and every single one
            # of them reached by pressing its name on the list - which is what
            # was asked for, and it is also the only way every group is a real
            # click rather than an address the walk went to. The cost is the
            # Groups page between one group and the next; the consolation is
            # that each hop is a move inside the page Facebook already has
            # open, where going straight to the next group loads the whole of
            # Facebook again.
            batch = batch[:1]
        if len(batch) > 1:
            step(f"{len(batch)} names on the list - going through them one after "
                 f"another without coming back here")

        # One trip to the list, then the whole run of groups back to back.
        for at, target in enumerate(batch):
            if checkpoint:
                checkpoint(0)
            # Its name as well as its address: a group chosen by name - one
            # joined before its address was known - is done once, not once for
            # every group on the list that happens to share the name.
            seen.update(_tokens(target))
            row = {"name": target["name"], "url": target["url"], "visited": "",
                   "title": "", "how": "", "closed": 0, "posted": "", "said": ""}
            walked.append(row)
            step(f'found "{target["name"]}" ({len(walked)}) - opening it')
            if on_group:
                on_group({"name": target["name"], "url": target["url"],
                          "index": len(walked) - 1})

            # How this group is reached. The FIRST of a run is pressed on the
            # list, because that is where the window is standing. Every one after
            # it is gone to straight from the group before - no trip back to the
            # Groups page in between, which is the bouncing the operator was
            # watching: thirty groups used to mean thirty returns to a page they
            # did not want to look at.
            #
            # The list is where the walk finds out WHICH groups exist, and it
            # cannot press a row that is not on screen - so it comes back when it
            # runs out of names, and not before.
            how = ""
            title = ""
            shut = 0
            ok = False
            try:
                link = (page.locator(f'[role="navigation"] a[href*="/groups/{target["key"]}"]').first
                        if at == 0 and on_list() else None)
                if link is not None and link.count() > 0:
                    link.scroll_into_view_if_needed(timeout=3_000)
                    try:
                        link.click(timeout=6_000)
                    except Exception:                    # noqa: BLE001 - something over it
                        # A sticky header sitting on top of the row. Forced, which
                        # skips the "is it reachable" wait and is still a press on
                        # the real element.
                        link.click(timeout=4_000, force=True)
                    how = "clicked"
                    # A route change, not a load: waited for by the address
                    # changing rather than by a fixed pause.
                    for _ in range(20):
                        if f'/groups/{target["key"]}' in (page.url or ""):
                            break
                        page.wait_for_timeout(250)
                else:
                    goto(page, target["url"])
                    how = "opened"

                look = page
                # A short breath, not a flat second and a half. What the page
                # actually has to produce is its title, and the loop below waits
                # for THAT - so a group that is ready in 300ms costs 300ms
                # instead of costing the slowest case every time.
                look.wait_for_timeout(250)
                guard(look)
                # Whatever the group put up over itself, closed by its X first -
                # the sheet's own heading would otherwise be read as the group's.
                shut = dismiss_popup(look, on_step=on_step)
                if shut:
                    look.wait_for_timeout(250)
                # Polled quickly and for as long as it might take: twenty-five
                # looks at 200ms is the same five seconds of patience the old
                # eight-at-500ms had, spent in smaller pieces, so the common case
                # of a title already there returns at once.
                for _ in range(25):
                    title = look.evaluate(GROUP_TITLE_JS) or ""
                    if title:
                        break
                    look.wait_for_timeout(200)
                gone = bool(look.evaluate(GONE_JS))
                ok = bool(title) and not gone and "/groups/" in (look.url or "")
                if not ok:
                    # The press still happened - saying only what went wrong
                    # afterwards loses that, and "clicked" is the half of this
                    # line the operator is checking.
                    why = ("Facebook says this content is not available" if gone else
                           "the group did not come up")
                    how = f"{how} - {why}" if how else why
                elif shut:
                    how += f", closed {shut} panel(s)"
            except Exception as err:                     # noqa: BLE001 - one group, not the walk
                ok = False
                how = f"could not open - {str(err).splitlines()[0][:100]}"

            # And, if there is something to post, post it - but only into a
            # group that actually came up. Writing into a page that did not
            # open is how a post ends up somewhere nobody meant it to go.
            said = ""
            posted = ""
            if ok and post:
                try:
                    said = post_in_group(
                        page, str(post.get("text") or ""),
                        media=list(post.get("media") or []),
                        wpm=int(post.get("wpm") or 240),
                        click_post=post.get("click_post") is not False,
                        on_step=on_step)
                    posted = "yes"
                except Exception as err:             # noqa: BLE001 - one group, not the walk
                    said = str(err).splitlines()[0][:140] or err.__class__.__name__
                    posted = "no"
                step(f'"{target["name"]}": {said}')

            # And whatever else the caller came to this group for - the
            # Scraping panel reads its People tab. One group that goes wrong
            # is said and stepped over; Stop still stops.
            if ok and on_open:
                try:
                    on_open(page, {"name": target["name"], "url": target["url"],
                                   "key": target["key"]})
                except Exception as err:             # noqa: BLE001 - one group, not the walk
                    if type(err).__name__ == "Stopped":
                        raise
                    step(f'"{target["name"]}": {str(err).splitlines()[0][:140]}')

            row.update(visited="yes" if ok else "no", title=title, how=how,
                       closed=shut, posted=posted, said=said)
            if ok:
                opened += 1
            if on_visit:
                on_visit({"name": target["name"], "url": target["url"], "ok": ok,
                          "title": title, "how": how, "closed": shut,
                          "posted": posted, "said": said,
                          "index": len(walked) - 1, "total": len(walked)})
            wait(random.uniform(*gap))
        # The run is done. Round the outer loop goes, back to the list for the
        # names below the ones just walked.

    # And leave the window on the list rather than inside whichever group it
    # happened to finish on. The operator comes back to the page they started
    # from, which is also the page a second run wants to begin on.
    try:
        open_list()
    except Exception:                                # noqa: BLE001 - tidying, not the run
        pass
    step(f"{len(walked)} groups, {opened} opened")
    return walked


# -- Posting into a group ------------------------------------------
# The box at the top of a group that opens the composer. Facebook words it
# several ways and has changed it more than once, so it is matched on what it
# SAYS rather than on a class or a position, and only inside the group's own
# column - the same words appear in the left rail on some builds.
FIND_COMPOSER_JS = r"""() => {
  document.querySelectorAll('[data-xbot-composer]').forEach(e => e.removeAttribute('data-xbot-composer'));
  const says = /write something|what.?s on your mind|anything on your mind|start a discussion|create a post|write a post|say something/i;
  // The group's own column first, the whole page second. Some builds hang the
  // composer outside `[role="main"]`, and looking only inside it found nothing
  // on a page that plainly had a composer on it.
  const main = document.querySelector('[role="main"]');
  const root = (main && main.querySelector('[contenteditable], [role="button"]')) ? main : document.body;

  // Find the WORDS first, then the thing around them that can be clicked.
  //
  // The other way round does not work. Facebook puts the placeholder in a
  // bare <span>, and the row it sits in carries the avatar and the
  // Photo/video and Live video buttons as well - so looking for a
  // role="button" whose own text is "Write something..." finds nothing, and
  // a run against thirty-four groups reported thirty-four times that the
  // group would not let it post. It was the reading that was wrong, not the
  // groups.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node, best = null, bestTop = Infinity, bestSaid = '';
  while ((node = walker.nextNode())) {
    const said = (node.nodeValue || '').trim();
    if (!said || said.length > 120 || !says.test(said)) continue;
    const held = node.parentElement;
    if (!held) continue;
    const box = held.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;      // not on screen

    // Up to whatever actually takes the click. The span itself is the
    // fallback: a click on it bubbles to whichever ancestor is listening.
    let target = held;
    let step = held;
    for (let hop = 0; step && step !== root && hop < 8; step = step.parentElement, hop++) {
      const role = step.getAttribute('role');
      if (role === 'button' || role === 'textbox' || step.hasAttribute('tabindex')) {
        target = step;
        break;
      }
    }
    const where = target.getBoundingClientRect();
    // The topmost: the group's own composer sits above the feed, and a post
    // further down can carry the same words in a shared preview.
    if (where.top < bestTop) { bestTop = where.top; best = target; bestSaid = said; }
  }

  if (!best) {
    // What WAS there, so a failure can be read rather than guessed at.
    return { found: false,
             buttons: root.querySelectorAll('[role="button"]').length,
             editables: root.querySelectorAll('[contenteditable="true"]').length,
             sample: (root.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160) };
  }
  best.setAttribute('data-xbot-composer', '1');
  return { found: true, said: bestSaid.slice(0, 60) };
}"""


# Inside the composer dialog: where the words go, where a file goes, and the
# button that sends it. `post` is deliberately strict - a dialog carries other
# buttons whose labels begin with "Post", and pressing the wrong one either
# does nothing or changes where the post goes.
POST_DIALOG_JS = r"""() => {
  document.querySelectorAll('[data-xbot-box], [data-xbot-post], [data-xbot-photo]')
    .forEach(e => { e.removeAttribute('data-xbot-box'); e.removeAttribute('data-xbot-post');
                    e.removeAttribute('data-xbot-photo'); });
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .filter(d => d.getBoundingClientRect().height > 80);
  const dlg = dialogs[dialogs.length - 1];
  if (!dlg) return { dialog: false };
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const words = (el) => [el.innerText || '', el.getAttribute('aria-label') || '']
    .map(t => t.replace(/\s+/g, ' ').trim().toLowerCase()).filter(Boolean);

  // Facebook's composer is a Lexical editor: the editable root carries
  // `contenteditable` and usually role="textbox", and the paragraph the
  // caret lands in is a <p> INSIDE it. The root is what must be clicked -
  // typing into the <p> is typing into something Lexical owns and replaces.
  const box = [...dlg.querySelectorAll('[contenteditable="true"], [data-lexical-editor="true"]')]
    .filter(visible)[0];
  if (box) box.setAttribute('data-xbot-box', '1');

  const buttons = [...dlg.querySelectorAll('[role="button"], button')].filter(visible);
  // Exactly "post", not "post to", not "posting as", not "add to your post".
  // Matched on the button's OWN words and on its label, so a button whose
  // text lives in a span inside it still reads as "post".
  const post = buttons.find(b => words(b).some(t => t === 'post'));
  if (post) post.setAttribute('data-xbot-post', '1');
  const photo = buttons.find(b => words(b).some(t => /^photo\/video$|^photo$|add photos?\/videos?/.test(t)));
  if (photo) photo.setAttribute('data-xbot-photo', '1');

  const file = dlg.querySelector('input[type="file"]');
  const disabled = post ? (post.getAttribute('aria-disabled') === 'true' || post.disabled === true) : true;
  return { dialog: true, box: !!box, post: !!post, photo: !!photo,
           file: !!file, postDisabled: disabled,
           text: box ? (box.innerText || '').trim().length : 0 };
}"""

# When a group says outright that this account may not post in it. Checked so
# the wait for a composer that is coming can be a long one without a group
# that has no composer at all costing the same twenty seconds: thirty of those
# is ten minutes of a run spent waiting for something already ruled out.
NO_POST_JS = r"""() => {
  const t = ((document.querySelector('[role="main"]') || document.body).innerText || '').slice(0, 4000);
  return /only admins can post|admins only|you can.?t post|can.?t post in this group|posting is turned off|turned off posting|members can.?t post/i.test(t);
}"""

# What a group says when a post has gone in but an admin has to let it through.
PENDING_JS = r"""() => {
  const t = (document.body.innerText || '').slice(0, 4000);
  return /pending approval|waiting for approval|admins? (will|to) (review|approve)|your post is pending/i.test(t);
}"""


def post_in_group(page, text: str, *, media: Optional[list[str]] = None,
                  wpm: int = 240, click_post: bool = True,
                  wait_for_box: float = 20.0,
                  on_step: Optional[Callable[[str], None]] = None) -> str:
    """Write a post into the group that is on screen, and send it.

    Returns what happened, in words: "posted", "posted - waiting for an admin
    to approve it", or "written, not posted" when `click_post` is off. Raises
    `AutomationError` with the reason when it could not - a group that does not
    let this account post has no box to write in, which is a fact about the
    group rather than a fault, and is reported as such.

    `click_post` off is the dry run: it opens the composer, types the words,
    attaches the picture and stops with the dialog open for a person to look
    at. Same idea as the campaign's Click Send switch, and there for the same
    reason - the first run of anything that writes in public should be one you
    can read before it goes.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)

    if not (text or "").strip() and not media:
        raise AutomationError("nothing to post - no words and no picture")

    # Anything the group put up over itself, or the composer is behind it.
    # Closed again inside the wait below as well: a welcome sheet can arrive a
    # second or two after the page does, and it hides the box when it lands.
    dismiss_popup(page, on_step=on_step)

    # WAITED FOR, not glanced at. A group's page shows its heading long before
    # it has drawn the box under it - the heading is the first thing rendered
    # and the composer is several seconds behind it on a real connection. This
    # looked once, immediately, and so reported that all thirty-four groups
    # refused to let the account post when what had actually happened is that
    # none of them had finished loading.
    found: dict = {}
    for _ in range(int(wait_for_box / 0.5)):
        found = page.evaluate(FIND_COMPOSER_JS) or {}
        if found.get("found"):
            break
        # Said so in words: there is no composer coming, so stop waiting for one.
        if page.evaluate(NO_POST_JS):
            raise AutomationError("this group does not let this account post")
        page.wait_for_timeout(500)
        # A sheet that landed late is what is covering the box.
        if (page.evaluate(POPUP_CLOSE_JS) or {}).get("dialog"):
            dismiss_popup(page, on_step=on_step)
    if not found.get("found"):
        # What the page had on it, so the next failure of this kind can be
        # read off the log instead of guessed at from the outside.
        raise AutomationError(
            "this group has no box to write in - it may not let this account "
            f"post (after {int(wait_for_box)}s; buttons on the page: "
            f"{found.get('buttons')}, editable boxes: {found.get('editables')})")
    step('opening the composer')
    try:
        page.locator('[data-xbot-composer="1"]').first.click(timeout=8_000)
    except Exception as err:                         # noqa: BLE001 - said plainly
        raise AutomationError(f"the composer would not open: {err}") from err

    # The dialog, waited for rather than assumed.
    plan: dict = {}
    for _ in range(30):
        plan = page.evaluate(POST_DIALOG_JS) or {}
        if plan.get("dialog") and plan.get("box"):
            break
        page.wait_for_timeout(300)
    if not plan.get("dialog"):
        raise AutomationError("the composer did not open")
    if not plan.get("box"):
        raise AutomationError("the composer opened with nowhere to type")

    if (text or "").strip():
        step("typing the post")
        try:
            page.locator('[data-xbot-box="1"]').first.click(timeout=6_000)
            # `_delay_for` is already MILLISECONDS a key, which is what
            # Playwright's `delay` wants. Multiplying it by a thousand - as
            # this did at first - asks for fifty seconds a character, and a
            # short post takes the rest of the day.
            #
            # Typed key by key with the gap jittered, the way the comment flow
            # does it: an even cadence is not a typist, and a composer handed
            # its whole value at once has fired no keydown events at all.
            base = _delay_for(wpm)
            for char in text:
                page.keyboard.type(char, delay=0)
                page.wait_for_timeout(base * random.uniform(0.55, 1.75))
        except Exception as err:                     # noqa: BLE001
            raise AutomationError(f"could not type the post: {err}") from err

    if media:
        step(f"attaching {len(media)} file(s)")
        # The file input is often already in the dialog, hidden. When it is
        # not, the Photo/video button is what puts it there.
        if not (page.evaluate(POST_DIALOG_JS) or {}).get("file"):
            try:
                page.locator('[data-xbot-photo="1"]').first.click(timeout=6_000)
                page.wait_for_timeout(800)
            except Exception:                        # noqa: BLE001 - the input may be there anyway
                pass
        try:
            page.set_input_files('[role="dialog"] input[type="file"]', media, timeout=15_000)
        except Exception as err:                     # noqa: BLE001
            raise AutomationError(f"could not attach the picture: {err}") from err
        # Uploaded before Post will take it. Waited for by the button coming
        # alive rather than by a fixed sleep - a big picture on a slow line
        # takes longer than any number picked in advance.
        for _ in range(60):
            plan = page.evaluate(POST_DIALOG_JS) or {}
            if not plan.get("postDisabled"):
                break
            page.wait_for_timeout(500)

    if not click_post:
        return "written, not posted"

    plan = page.evaluate(POST_DIALOG_JS) or {}
    if not plan.get("post"):
        raise AutomationError("the composer has no Post button on it")
    if plan.get("postDisabled"):
        raise AutomationError("Post is greyed out - the picture may not have finished uploading")

    step("pressing Post")
    try:
        page.locator('[data-xbot-post="1"]').first.click(timeout=8_000)
    except Exception as err:                         # noqa: BLE001
        raise AutomationError(f"Post would not press: {err}") from err

    # The dialog closing is what says it went. A dialog still up after this
    # long is a post that did not.
    for _ in range(40):
        if not (page.evaluate(POST_DIALOG_JS) or {}).get("dialog"):
            break
        page.wait_for_timeout(500)
    if (page.evaluate(POST_DIALOG_JS) or {}).get("dialog"):
        raise AutomationError("the composer stayed open - the post did not go")

    page.wait_for_timeout(1_200)
    if bool(page.evaluate(PENDING_JS)):
        return "posted - waiting for an admin to approve it"
    return "posted"


# -- The people in a group -------------------------------------------
# The group's People tab: its link, found by where it points first (the
# group's /members/ page) and by what it says second, so a build that words
# the tab "Members" is still found.
PEOPLE_TAB_JS = r"""() => {
  document.querySelectorAll('[data-xbot-people]').forEach(e => e.removeAttribute('data-xbot-people'));
  const shown = (a) => { const r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const links = [...document.querySelectorAll('a[href]')].filter(shown);
  let hit = links.find(a => /\/groups\/[^/?#]+\/members\/?(?:[?#]|$)/i.test(a.getAttribute('href') || ''));
  if (!hit) hit = links.find(a => /^(people|members)$/i.test((a.innerText || '').trim()));
  if (!hit) return { found: false };
  hit.setAttribute('data-xbot-people', '1');
  return { found: true, said: (hit.innerText || '').trim().slice(0, 30) };
}"""

# Everyone listed on the People page right now. A member row is a list item
# with the person's profile link in it; a group also links each member as
# /groups/<group>/user/<id>/, which nothing but a member row carries. Handles
# come back the way the feed's authors do: the username after the slash, or
# the bare numeric id for a profile that has none.
MEMBERS_JS = r"""() => {
  const RESERVED = new Set(['groups','photo','photos','posts','watch','reel','reels',
    'hashtag','events','marketplace','stories','gaming','pages','people','videos',
    'video.php','share','sharer','sharer.php','login','help','policies','privacy',
    'settings','friends','memories','saved','bookmarks','notifications','messages',
    'search','ads','business','games','fundraisers','jobs','weather','dialog',
    'plugins','l.php','home.php','me','about','legal','terms','live','permalink.php',
    'story.php','profile.php','feeds','latest','favorites','places','pg','ad_center']);
  const parse = (href) => {
    let u;
    try { u = new URL(href, location.origin); } catch (e) { return null; }
    if (!/(^|\.)facebook\.com$/i.test(u.hostname)) return null;
    const path = u.pathname.replace(/\/+$/, '');
    let m;
    if (/^\/profile\.php$/i.test(path)) {
      const id = u.searchParams.get('id');
      return id && /^\d+$/.test(id) ? id : null;
    }
    if ((m = /^\/groups\/[^/]+\/user\/(\d+)/i.exec(path))) return m[1];
    if ((m = /^\/people\/[^/]+\/(\d+)/i.exec(path))) return m[1];
    if ((m = /^\/([A-Za-z0-9.\-_]+)$/.exec(path))) {
      const seg = m[1];
      if (RESERVED.has(seg.toLowerCase())) return null;
      return seg;
    }
    return null;
  };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const NOT_A_NAME = /^(see owner profile|see profile|view profile|visit profile|add friend|message|send message|follow|following|see all|see more|admin|moderator|you|sponsored)$/i;
  const root = document.querySelector('[role="main"]') || document.body;
  const out = {};
  const add = (a) => {
    const handle = parse(a.getAttribute('href') || a.href);
    if (!handle) return;
    const row = out[handle] || (out[handle] = { handle: handle, name: '' });
    const name = clean(a.innerText).split(' · ')[0] || clean(a.getAttribute('aria-label'));
    if (!row.name && name && !NOT_A_NAME.test(name) && name.length <= 80) row.name = name;
  };
  for (const a of root.querySelectorAll('[role="listitem"] a[href]')) add(a);
  for (const a of root.querySelectorAll('a[href*="/user/"]')) {
    if (/\/groups\/[^/]+\/user\/\d+/i.test(a.getAttribute('href') || '')) add(a);
  }
  return { members: Object.values(out),
           height: document.documentElement.scrollHeight };
}"""


# What the group says its membership is - "4,532 members", "12K members" -
# read off the People page so the panel can say 300 of 4,532 rather than just
# 300. Facebook writes it several ways and sometimes not at all; nothing is
# reported rather than a guess.
MEMBERS_TOTAL_JS = r"""() => {
  const root = document.querySelector('[role="main"]') || document.body;
  const text = (root.innerText || '').slice(0, 4000);
  const m = /([\d][\d.,]*)\s*([KM])?\s*(?:total\s+)?members?/i.exec(text);
  if (!m) return 0;
  const n = parseFloat(m[1].replace(/,/g, ''));
  if (isNaN(n)) return 0;
  const scale = (m[2] || '').toUpperCase() === 'M' ? 1e6
              : (m[2] || '').toUpperCase() === 'K' ? 1e3 : 1;
  return Math.round(n * scale);
}"""



def members_scroll_js(step: float = 0.9, smooth: bool = True) -> str:
    """How a People list is scrolled: how far, and whether it glides.

    Smooth looks like a person reading and lands Facebook's loader in the
    middle of the list rather than at the bottom of it; instant is the fastest
    the page can be driven. `step` is the fraction of the window scrolled each
    turn - past about 1.5 the list can outrun what Facebook has sent and the
    read stops early on a gap that was only not-yet-loaded.
    """
    far = max(0.1, min(3.0, float(step or 0.9)))
    how = "smooth" if smooth else "instant"
    return ("() => window.scrollBy({ top: Math.round(window.innerHeight * "
            f"{far:.2f}), behavior: '{how}' }})")


def scrape_groups_in_tabs(context, groups: list[dict], *,
                          tabs: int = 3,
                          limit: int = 0,
                          quiet_rounds: int = 12,
                          gap: float = 1.2,
                          scroll_step: float = 0.9,
                          smooth: bool = True,
                          on_rows: Optional[Callable[[dict, list[dict]], None]] = None,
                          on_tab: Optional[Callable[[int, dict], None]] = None,
                          on_done: Optional[Callable[[dict, list[dict], bool], None]] = None,
                          checkpoint: Optional[Callable[[float], None]] = None) -> int:
    """Read the people out of several groups at once, a tab each.

    One group at a time is mostly waiting: scroll, wait for Facebook to send
    the next page of members, scroll again. A group of four thousand takes
    twenty minutes of that, and ten such groups take an afternoon. This opens
    `tabs` tabs in the account's own window and goes round them - one scroll
    and one read per tab per turn - so the waiting happens in parallel. As a
    tab's group runs out it is marked done and the next group is loaded into
    that same tab, until there are none left.

    Every call here happens on the account's own thread: Playwright's sync API
    is bound to the thread its driver started on, so the whole round robin runs
    inside ONE `with_context` call rather than a thread per tab.

    `on_rows(group, rows, total)` is the people so far from that group as they
    come, with what the group says its membership is. `on_tab(i, state)` is
    what tab i is doing, for the panel. `on_done(group, rows, finished, total)`
    is one group read to its end (or to `limit`). Returns how many groups were
    read.
    """
    queue = list(groups)
    want = max(1, min(int(tabs or 1), 8, len(queue) or 1))
    scroll_js = members_scroll_js(scroll_step, smooth)

    def say(i: int, slot: Optional[dict]) -> None:
        if not on_tab:
            return
        if slot is None or slot.get("group") is None:
            on_tab(i, {"group": "", "read": 0, "total": 0, "state": "idle"})
            return
        on_tab(i, {"group": slot["group"].get("name") or slot["group"].get("key") or "",
                   "read": len(slot["rows"]), "total": slot.get("total") or 0,
                   "state": slot.get("state") or "reading"})

    def members_url(group: dict) -> str:
        base = str(group.get("url") or "").split("?")[0].rstrip("/")
        if not base:
            base = f"https://www.facebook.com/groups/{group.get('key') or ''}"
        return base + "/members/"

    def load(slot: dict, i: int) -> bool:
        """Put the next group in the queue into this tab. False when none left."""
        if not queue:
            slot["group"] = None
            say(i, slot)
            return False
        group = queue.pop(0)
        slot.update(group=group, rows={}, quiet=0, tallest=0, total=0, state="opening")
        say(i, slot)
        goto(slot["page"], members_url(group))
        dismiss_popup(slot["page"], on_step=None)
        try:
            slot["total"] = int(slot["page"].evaluate(MEMBERS_TOTAL_JS) or 0)
        except Exception:                            # noqa: BLE001 - a number, not the read
            slot["total"] = 0
        slot["state"] = "reading"
        say(i, slot)
        return True

    def finish(slot: dict, i: int, finished: bool) -> None:
        if slot.get("group") is not None and on_done:
            on_done(slot["group"], list(slot["rows"].values()), finished,
                    int(slot.get("total") or 0))

    read_count = 0
    slots: list[dict] = []
    try:
        for i in range(want):
            page = context.new_page()
            slot = {"page": page, "group": None, "rows": {}, "quiet": 0,
                    "tallest": 0, "total": 0, "state": "idle"}
            slots.append(slot)
            if not load(slot, i):
                break

        while any(slot.get("group") is not None for slot in slots):
            if checkpoint:
                checkpoint(0)
            for i, slot in enumerate(slots):
                if slot.get("group") is None:
                    continue
                page = slot["page"]
                try:
                    got = page.evaluate(MEMBERS_JS) or {}
                except Exception as err:             # noqa: BLE001 - one tab, not the run
                    slot["state"] = f"stopped: {str(err).splitlines()[0][:80]}"
                    say(i, slot)
                    finish(slot, i, False)
                    read_count += 1
                    load(slot, i)
                    continue
                before = len(slot["rows"])
                for member in got.get("members") or []:
                    handle = str(member.get("handle") or "").strip()
                    if not handle:
                        continue
                    found = slot["rows"].get(handle)
                    if found is None:
                        slot["rows"][handle] = {
                            "handle": handle,
                            "name": str(member.get("name") or "").strip(),
                            "bio": "", "followers": None, "dm_open": None,
                            "protected": False,
                            "group": slot["group"].get("name") or "",
                        }
                    elif not found["name"] and member.get("name"):
                        found["name"] = str(member["name"]).strip()
                    if limit and len(slot["rows"]) >= limit:
                        break
                grew = len(slot["rows"]) > before
                if grew and on_rows:
                    on_rows(slot["group"], list(slot["rows"].values()),
                            int(slot.get("total") or 0))
                height = int(got.get("height") or 0)
                slot["quiet"] = 0 if (grew or height > slot["tallest"]) else slot["quiet"] + 1
                slot["tallest"] = max(slot["tallest"], height)
                say(i, slot)
                done = (limit and len(slot["rows"]) >= limit) or slot["quiet"] >= quiet_rounds
                if done:
                    finish(slot, i, True)
                    read_count += 1
                    load(slot, i)
                    continue
                try:
                    page.evaluate(scroll_js)
                except Exception:                    # noqa: BLE001 - read again next turn
                    pass
            if checkpoint:
                checkpoint(gap)
            else:
                time.sleep(gap)
    finally:
        # Whatever stopped this - Stop, a window closed - the people read so
        # far belong to their groups, and the tabs are not left open.
        for i, slot in enumerate(slots):
            if slot.get("group") is not None:
                finish(slot, i, False)
            say(i, None)
            try:
                slot["page"].close()
            except Exception:                        # noqa: BLE001 - already gone
                pass
    return read_count

def scrape_group_members(page, group: dict, *,
                         limit: int = 0,
                         quiet_rounds: int = 12,
                         scroll_step: float = 0.9,
                         smooth: bool = True,
                         gap: tuple = (0.8, 1.4),
                         on_rows: Optional[Callable[[list[dict]], None]] = None,
                         on_progress: Optional[Callable[[int, int], None]] = None,
                         on_step: Optional[Callable[[str], None]] = None,
                         checkpoint: Optional[Callable[[float], None]] = None) -> list[dict]:
    """Every person listed on the group's People tab. The group is on screen.

    The tab is PRESSED when the group shows it, and gone to by address only
    when it does not - a group whose tabs are folded under "More" on a narrow
    window, say. Then the list is scrolled and read until it stops growing:
    `quiet_rounds` looks in a row with no new person and no more page to
    scroll is the end of the list. `limit` stops sooner, for a group of tens
    of thousands; zero reads the whole list.

    Each row is a handle and a name, and the group it came from. A People page
    shows no bio and nothing about the inbox, so those are left unknown.
    """
    def step(said: str) -> None:
        if on_step:
            on_step(said)

    def wait(seconds: float) -> None:
        if checkpoint:
            checkpoint(seconds)
        else:
            time.sleep(seconds)

    name = group.get("name") or "the group"
    pressed = False
    look = page.evaluate(PEOPLE_TAB_JS) or {}
    if look.get("found"):
        try:
            page.locator('[data-xbot-people="1"]').first.click(timeout=6_000)
            pressed = True
        except Exception:                            # noqa: BLE001 - by address instead
            pressed = False
    if pressed:
        for _ in range(25):
            if "/members" in (page.url or ""):
                break
            page.wait_for_timeout(200)
    if "/members" not in (page.url or ""):
        base = (group.get("url") or page.url or "").split("?")[0].rstrip("/")
        goto(page, base + "/members/")
    guard(page)
    dismiss_popup(page, on_step=on_step)
    step(f'reading the people in "{name}"')

    # How many the group says it has. Best effort: a group that does not say
    # leaves this at zero, and the panel then only counts what has been read.
    try:
        total = int(page.evaluate(MEMBERS_TOTAL_JS) or 0)
    except Exception:                                # noqa: BLE001 - a number, not the walk
        total = 0
    if on_progress:
        on_progress(0, total)

    rows: dict[str, dict] = {}
    quiet = 0
    tallest = 0
    while True:
        if checkpoint:
            checkpoint(0)
        got = page.evaluate(MEMBERS_JS) or {}
        before = len(rows)
        for member in got.get("members") or []:
            handle = str(member.get("handle") or "").strip()
            if not handle:
                continue
            found = rows.get(handle)
            if found is None:
                rows[handle] = {
                    "handle": handle,
                    "name": str(member.get("name") or "").strip(),
                    "bio": "",
                    "followers": None,
                    # Unknown, not closed: the People page says nothing about
                    # the inbox, and the campaign finds out by trying.
                    "dm_open": None,
                    "protected": False,
                    "group": name,
                }
            elif not found["name"] and member.get("name"):
                found["name"] = str(member["name"]).strip()
            if limit and len(rows) >= limit:
                break
        grew = len(rows) > before
        if grew and on_rows:
            on_rows(list(rows.values()))
        if grew and on_progress:
            on_progress(len(rows), total)
        if limit and len(rows) >= limit:
            break
        height = int(got.get("height") or 0)
        quiet = 0 if (grew or height > tallest) else quiet + 1
        tallest = max(tallest, height)
        if quiet >= quiet_rounds:
            break
        page.evaluate(members_scroll_js(scroll_step, smooth))
        wait(random.uniform(min(gap), max(gap)))
        # A sheet that lands part way down the list covers it.
        if quiet and quiet % 4 == 0:
            dismiss_popup(page, on_step=on_step)
        guard(page)

    step(f'"{name}": {len(rows)} people')
    return list(rows.values())
