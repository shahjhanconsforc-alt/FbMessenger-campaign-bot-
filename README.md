# x-bot

A local operator console for X: scroll your own home timeline without stopping,
collect every account it shows — each one hovered for the profile card X pops up
— and run targeted DM outreach from your own accounts.

**Console and server.** Five panels, no build step, and a Python backend that
drives Chrome through Playwright. The frontend still runs on its own against
`mock.js`, which answers the same contract out of `localStorage` — useful for
working on a panel without a browser session open.

> **Scope.** This drives *your own* X accounts against X's own surface. It stores
> other people's profile text and messages them, so the two things that matter
> most are the per-account caps in Settings and leaving **Click Send** off for the
> first run. There is no unsend.

## Why the backend is new code

meetup-bot's could not be ported. Its automation layer is one file of Meetup
selectors (`backend/automation/meetup.py`), its stores are Meetup's tables, and
its whole pipeline is *category → event → attendees* — a shape X does not have.
Lifting it across would have meant a server that looked right and worked nowhere.

So the console was built first, against an HTTP contract, `mock.js` answered that
contract while there was nothing else to, and the server was then written to the
same one. The shape survived: a sweep is still a task you poll, a roster is still
per source *and* per mode, and `app.js` never learned which of the two is
answering.

## Architecture

```
x-bot/
├─ frontend/
│  ├─ index.html   the six panels, the icon sprite, the dialogs
│  ├─ style.css    meetup-bot's console, recoloured — see below
│  ├─ app.js       the whole app: panels, wizard, tables, polling
│  └─ mock.js      the stand-in backend (localStorage, no automation)
└─ backend/
   ├─ run.py       the entry point — serves the API and the console
   ├─ app.py       routes mounted, errors shaped, index.html served
   ├─ tasks.py     a long run behind an id: progress, pause, resume, stop
   ├─ store.py     one JSON file, one lock
   ├─ schema.py    the settings the panel is generated from
   ├─ automation/  Chrome (browser.py) and every x.com selector (x.py)
   └─ panels/      one module per panel on the screen
```

`backend/README.md` is the tour of that side.

`app.js` never branches on which backend is answering. Every read and write goes
through one `api()` function, and that function hands the call to `mock.js` only
if it is loaded:

```js
async function api(path, options = {}) {
  if (window.XBOT_BACKEND) return window.XBOT_BACKEND.handle(path, options);
  const res = await fetch(path, { ... });
  ...
}
```

**Deleting `<script src="/mock.js"></script>` from `index.html` is the entire
migration**, and the server does exactly that on the way out: `index()` in
`backend/app.py` strips the line as it serves the page, so the same file drives
the mock from a static server and the real backend from this one. Nothing else
changed when the server landed.

Requesting `/mock.js` from the backend is a deliberate 404. A page cached from
before the server existed would otherwise load the stand-in over the top of it
and write every change to `localStorage` while the console looked perfectly
healthy.

### The UI is meetup-bot's, deliberately

Same grid, same cards, same wizard rail, same dense tables, same activity feed.
The two consoles are operated by the same person on the same desk, and a scrape
table that reads differently in each is one to relearn every time you switch.

What changed:

| | meetup-bot | x-bot |
|---|---|---|
| accent | `#F44C71` pink | `#1D9BF0` X blue |
| `--info` | `#58a6ff` | `#a371f7` — blue had become the accent |
| the scrape's unit | category → event → attendees | home timeline → accounts |
| where it stops | the end of the attendee list | nowhere — a feed has no end, so **Stop** is the end |
| roster columns | member | account · followers · **DMs open** |
| the store | MinIO buckets | `store.json`, plus MinIO for the finished rosters |

`style.css` is meetup-bot's file with the palette swapped; everything x-bot
added is in one block at the end, under **"What x-bot has that meetup-bot did
not"**, so the diff between the two stylesheets stays readable.

### The panels

1. **Campaign** — the five-step wizard, and the panel everything else feeds.
   Accounts → recipients → drafts → media → status. Nothing sends until every
   mandatory line on the checklist is ticked, and the line beside the disabled
   button names what is missing.

   Step 2 is where a run is *aimed*. The stored handles sit in a table with a
   filter rail down its right: category (what the bio says the account is,
   derived by `classify.py`), target (which scrape it came off), how many DMs it
   has already had out of here, open inboxes only, and a row range. **Categorised
   only** is on by default — this step exists to point a draft at a kind of
   account, and a handle whose bio said nothing is one nothing can be said about;
   the switch is there because a file of bare handles has no bios to read. None
   of it takes effect until **OK**: a category, a send count and a range are one
   question, and a list that re-cut itself between them would make the row
   numbers the range counts in a moving target. What is on screen after OK is
   exactly who the run sends to.
2. **FB Campaign** — facebook.com itself, in the same real window the campaign
   sends from. Two buttons, and they are deliberately not the same *kind* of
   thing. **Open Facebook** launches Chrome on an account's own profile and puts
   the feed on screen; it is the one control here that does not need a window
   already open, because opening one is what it does. **Start** then walks down
   that feed until **Stop** — the distance scrolled and the pause between are
   both rolled (`scroll_pause_min`/`max` in Settings, the same two the Messenger
   sweep uses), because a fixed 600px every 1.00s is a metronome.

   It is not the scrape. It reads nothing off the feed and saves nothing; the
   count under the buttons is passes made and posts rendered, which is evidence
   the walk is *doing* something rather than a tally of anything kept. A feed
   that stops growing is gone back to the top of rather than sat at — a walker
   parked at the bottom of a page that will not grow only looks busy. A window
   that is signed out is told apart from a slow feed and says so, rather than
   scrolling a login form.
3. **Accounts** — one card per X account: handle, the email X sends its
   verification challenges to, the password behind an eye. A blue card is a
   stored session.
4. **Scraping** — Home → accounts. A strip of the open browser windows, a
   button, a count and the table. The only thing to pick is *which* windows:
   every open account is a chip, every chip is a tick, and the run walks each
   ticked feed at the same time as the others — three open and two ticked is two
   feeds walked side by side and the third left alone. The page is always the
   signed-in account's own feed, every new handle is hovered for its profile
   card, and the walk runs until **Stop**. Gated behind an open browser window,
   because the feed is behind the login wall entirely.

   Several feeds collect into **one** roster. Two home timelines overlap, and a
   handle is a handle whichever window saw it first, so the run keeps one row per
   handle and records every account that walked for it.
5. **Data** — one row per **account**: the handle, the bio read off its hover
   card, and a category derived from that bio. Filter by scrape, by category or
   by text; the download and the MinIO send both act on whatever the filters
   leave standing, so an export can be one category out of one scrape. Download
   is tab-separated, not JSON — a roster is grepped and dropped into a
   spreadsheet far more often than it is parsed.

   The category is worked out in `backend/classify.py` and derived on read, never
   stored: retuning the words there re-labels everything ever collected, with no
   re-scrape.
6. **Settings** — generated from whatever schema the backend describes, so the
   panel grows on its own when the server gets longer. **Click Send** has a
   switch of its own and saves on the press.

### The bucket

A finished roster goes to MinIO as a text file of handles and bios, one account
per line, tab-separated — which is the form something that is not this console
can actually use. It is sent when a scrape ends, and again whenever the ↑ button
on a roster row in **Data** is pressed.

Configure it in `backend/.env` (`MINIO_URL`, `MINIO_USER`, `MINIO_PASSWORD`,
`MINIO_BUCKET`) — the same four names the Instagram and Threads bots on this
machine read. With none of them set the console runs exactly as before and the
pill stays hidden.

**A handle goes in once.** Before each upload the bucket's own ledger
(`_handles.txt`) is read and anything already in it is left out — so a week of
runs over a home feed that keeps showing the same accounts is one line per
account, not one per sighting. The bucket is the authority, not a list kept on
this side: emptying the bucket, or writing to it from another machine, changes
what gets sent next time. Delete the ledger and it is rebuilt from the run files.
A run with nothing new writes no object at all and says so.

Every bucket this console writes to is prefixed **`x-`**, so a name left over
from another bot cannot have X handles written into it: `MINIO_BUCKET=usernames`
is `x-usernames`, and so is `x_usernames`. It is a hyphen and not an underscore
because a bucket name is a DNS label — MinIO allows lowercase letters, digits,
hyphens and dots, and `make_bucket` refuses anything else before a request is
sent.

```
x-usernames/
  _handles.txt                     # every handle sent, so none is sent twice
  home-home-20260822-1330.txt      # x-bot - Home timeline
  home-home-20260822-0915.txt      # collected 2026-08-22T09:15:00+00:00
                                   # handle	bio
                                   @NFL	football is in the air
                                   @recostseg	We help real estate investors defer taxes
```

## Running it

```bash
pip install -r backend/requirements.txt
python -m playwright install chromium     # once — the browser itself
python backend/run.py
# http://127.0.0.1:4300
```

One process serves both the API and the console, so there is no static server to
start beside it.

Port **4300** — 4000, 4100 and 4200 are the Instagram, Threads and Meetup bots
on the machine these run beside. Two servers can't share a port; the second to
start dies on bind. `XBOT_PORT` moves it, and `backend/.env.example` lists the
rest.

**The console on its own**, against the stand-in, with no Python and no browser
session — for working on a panel:

```bash
python -m http.server 4300 --directory frontend
```

The paths in `index.html` are absolute, so `frontend/` has to be the document
root.

There is sample data on first load — a draft — because an empty console cannot
be judged: every table looks correct when it has nothing in it. To wipe it: delete `backend/data/store.json` on the server, or clear the
`x-bot.db` key in the browser when running against the mock:

```js
localStorage.removeItem('x-bot.db')   // in the browser console, then reload
```

### The first run

1. **Accounts** — add an account, then **Open** it and sign in in the window that
   appears. Doing the login by hand once is the least eventful way through X's
   challenges; the profile in `backend/data/profiles/<id>` keeps the session
   afterwards.
2. **Settings** — turn **Click Send** *off*, and set `per_account_dm_cap` to
   something small.
3. **Scraping** — with that window still open, it appears as a chip in the
   strip, already ticked. Press **Scrape Home**. It walks the feed until you
   press **Stop**; everything it collected is already saved by then. Open a
   second account and it joins the strip — tick both and the next run walks both
   feeds at once.
4. **Campaign** — the wizard will not offer the button until the checklist is
   ticked, and the line beside it names what is missing.

With Click Send off, a campaign types each DM into the composer and leaves it.
Nothing is delivered, and nothing is marked messaged.

## The API

`mock.js` was the specification, and the server answers every route in it with
the same shapes — plus the uploads and the event stream the mock handled through
its own hooks. `/api/docs` is the generated reference.

| | |
|---|---|
| `/api/accounts…` | list, add, remove, reveal a password, open a window, log out |
| `/api/accounts/sessions` | are we logged in — read from the cookie jar |
| `/api/automation/status` | is a browser up, and how many windows |
| `/api/scrape/sweep` · `/members` · `/open-all` | the runs, each returning a task id |
| `/api/scrape/stop` · `/status` | end the walk; find it again after a reload |
| `/api/scrape/accounts` | which windows a sweep could walk — the panel's strip |
| `/api/data/rosters/:key/minio` | send one roster's handles and bios to the bucket |
| `/api/minio/status` | is the bucket reachable — the pill on the bar |
| `/api/tasks/:id` | poll a run — `{ state, progress, detail, result }` |
| `/api/data/rosters` | one per source *and* mode |
| `/api/store/accounts` | everyone ever collected — the campaign's pool |
| `/api/messages` · `/api/media` | drafts and images |
| `/api/campaign/start` · `/status` · `/pause` · `/resume` · `/stop` | the send |
| `/api/settings` | `{ settings, schema }` — the panel is generated from it |

Two conventions the server kept:

- **A long run is a task, not a held request.** A sweep has no end at all. POST
  starts it and returns an id; the UI polls, and `/api/scrape/status` is how the
  console finds a walk again after a reload rather than orphaning it.
  `watchTask()` in `app.js` is the only thing that knows this.
- **One thread per account, not one for the browser.** Playwright's sync API
  belongs to the thread that created it, so every call into a window is posted to
  that window's own thread and waited on — a *lane*, in `backend/automation/
  browser.py`. Per account rather than per process, because each account is its
  own Chrome out of its own profile directory: lanes never touch each other, so
  they run at the same time, which is what makes two feeds walkable at once.
  Everything aimed at one account is still single file, because two runs typing
  into one window would interleave their keystrokes.
- **The roster is written as it goes.** There is no end to write it at — a run
  finishes when the operator presses Stop — so everything up to that press is
  already on disk when it lands.
- **The object store is optional, always.** There may be no MinIO on the
  machine, and a scrape must not care. Every call in `backend/storage.py` reports
  failure as a returned reason rather than an exception; the run logs it and
  carries on, and the roster is still on disk and still re-sendable from Data.
- **Two tables that overlap on purpose.** A *roster* says who came off one
  source on one day. The *store* says who has ever been seen, which is what a
  campaign draws from. Purging one into the other would make the first an
  unreliable record of what had been scraped.

## On not looking like a bot

Enforced in `backend/panels/campaign.py` and `backend/automation/x.py`, from the
values in `backend/schema.py`:

- **Per-account caps.** X counts DMs per account per day, not per campaign.
  `per_account_dm_cap` is the one that keeps an account writable.
- **Randomised pacing.** `gap_min_seconds`/`gap_max_seconds` — a constant gap is
  a metronome, and a metronome is the easiest thing to spot in a request log.
  The gap is spent between two **sends**, not between two recipients, so it means
  the same thing however many chats are open at once.
- **The tabs type together.** Typing is deliberately key-by-key at a human
  speed, so a 130-character draft is about half a minute — and that used to be
  half a minute during which the account's single Playwright thread was held by
  one tab. Five tabs then wrote strictly one after another: open five, write
  one, send it, only then start the next. The pool now hands each tab a slice of
  ~10 characters and moves on, so all five composers fill at the same time, at
  the same typing speed as before. **Sending stays one at a time** and spaced by
  the gap — five composers filling at once must not become five messages leaving
  at once.
- **Chats open at once.** `parallel_tabs`, five by default. Each recipient gets
  its own tab, and the run drives five of them at a time. What this buys is the
  *waiting* — a Messenger thread takes tens of seconds to boot, and at 1 the run
  pays that before every single message — so a run finishes far sooner without
  the account sending any faster. Set it to 1 for the old one-tab run, which
  keeps the quicker path of clicking the thread in the list instead of loading it.
- **What the pool holds is a *slot*, not a tab.** A slot is let go of when its
  message has gone, or when the page has said in words that there is no chat to
  be had — and letting it go is what refills the pool, so the run keeps driving
  its `parallel_tabs` chats regardless of what becomes of the tab.
- **Nothing is closed for being *slow*.** A tab that is still loading, still
  pressing its way through Continue walls, or still following a redirect is a tab
  that is getting somewhere, and the run waits for it. This is not a setting.
- **`close_tab_after_send`, off by default.** Off, a chat stays open after its
  message goes, and a finished run leaves a window of the conversations it wrote
  to, in order, there to be read rather than taken on trust from the table. The
  cost is memory: one Chromium tab per recipient, which on a few hundred
  recipients is a lot of window. On, each tab closes the moment its message has
  gone. It applies to exactly one thing — a tab this run opened, after a real
  send. A chat that could not be opened, a recipient stopped by the day's cap,
  and a tab the run was still holding when it was stopped are all left where they
  are whatever it says, because those are *verdicts*, and a verdict is the thing
  worth being able to check against the screen it was made from. At
  `parallel_tabs` 1 nothing opens a tab at all, so it does nothing.
- **The clock only runs while nothing is happening.** A tab gets 75 seconds — but
  of *stillness*, not of life. Every time the poll reads something different, and
  every time a Continue is pressed or a PIN prompt declined, the 75 seconds start
  again. It used to be one stopwatch covering the page load *and* every wall *and*
  the second route, so a tab that was visibly working its way in ran out of time
  and the recipient was written off as unreachable.
- **Two ways into a chat.** A tab tries `messenger.com/t/<handle>` first. A
  handle that Messenger will not resolve is not answered with an error — the tab
  is quietly *redirected to the inbox* — so a tab that is no longer on a `/t/`
  address is sent round once more by `facebook.com/messages/t/<handle>`, which
  resolves the same handle through the profile lookup instead. Only when both
  bounce is the recipient written off, and the row then says which.

  A bounce is read off the *address bar*, though, and the address of a tab that
  is still following redirects means nothing yet — which is how chats were being
  abandoned before they had finished arriving. So it is now asked twice over: a
  document that has not finished loading is never called bounced, and a settled
  reading has to stay settled for six seconds before the run acts on it.
- **Continue is pressed, not merely noticed.** The poll and the click used to
  disagree about what a wall *was*: the poll counted `[aria-label="Continue"]`
  nodes including the hidden ones Messenger keeps in the tree, so it reported a
  wall on tabs that had none on screen, and the click then had nothing to land
  on. The tab span on a button that was not there until its time ran out. Both
  now ask the same visible-only question, every match gets a turn rather than
  just the first, and each is tried three ways — an ordinary click, a forced one,
  then a DOM click for the case where a fading overlay is still in front of it.
- **The poll is cheap, the acting is not.** A tab is asked what it has become
  four times a second, and five tabs share one thread — so the poll only does
  DOM *counts* and a URL read. It used to extract the text of the whole
  application three times per tab per poll (the challenge guard, the PIN check,
  the unreachable check), which is why a Continue button that appeared instantly
  was not pressed for eight seconds. The full-page reads now happen only where
  something is about to be decided: at the moment a wall is pressed, and just
  before a recipient is written off.
- **Clicking the composer is verified, not assumed.** A `click()` that resolves
  can still leave the caret on the document body, and then four hundred typed
  characters go nowhere. The run clicks the box, asks the page whether the caret
  landed in a `contenteditable`, and if not clicks the paragraph *inside* the
  editor, then falls back to `focus()`. If none of the three takes, it raises
  rather than typing blind. A page that cannot answer the question is trusted
  rather than failed — unanswerable is not the same as no.
- **One thing is typed: the draft, into the message box.** Never a handle, never
  into a search or recipient field. A version of this typed the handle into the
  New message form's "To:" line to look a recipient up, and on a live run that
  put text in a search box — the lookup is *gone*, not fixed, and a test asserts
  `_type_text` is called from exactly one place in the module.
- **Writing to someone new.** `/t/<handle>` for a person with no thread yet lands
  on the New message form with the recipient **already in the "To:" line** — put
  there by the address the run asked for. So the form is writable, and the run
  does what an operator would: shuts the typeahead that drops out of the To: line
  (Escape, then the click into the message box — never a click at a guessed
  coordinate, since nothing on that screen is guaranteed not to be a button),
  then types and sends. Shutting it is not cosmetic: while that list is open,
  **Enter belongs to it**, and the send falls back to Enter — so an open list
  turns a send into picking a suggestion.
- **Which of the two boxes.** That form has two editable lines, and the draft
  must never reach the top one. When both match the composer selector, the last
  in document order is the message box. When only one matches, document order
  proves nothing — so the question goes to the **screen**: the message box is in
  the bottom half of the window, the "To:" line is at the top. If that cannot be
  established, the run **refuses to type** rather than guessing.
- **What stands in front of a composer.** A fresh thread often does not come up
  ready to type in. A **"Continue"** wall gets pressed — up to `MAX_WALLS` times,
  since Messenger stacks them — and the composer behind it is what the next poll
  finds. The **New message screen** (a "To:" line instead of a message box) gets
  the *handle* typed into it and a suggestion picked. The draft is never typed
  there: the To: line sends on Enter exactly like the composer, so a draft in it
  would go to whoever Messenger guessed at. A suggestion is only taken when its
  name matches the handle once case, spacing and punctuation are removed
  (`Ali Haider` for `alihaider`), or when the search returned exactly one person.
  Several names and no clear match is **refused** and written on the row — there
  is no unsend, so "probably them" is not good enough to spend a stranger on.
- **The PIN prompt is declined, not answered.** A profile whose E2EE device is
  new comes up on *restore your chats*. The run cannot answer it and does not
  need to — a profile that cannot unseal its **history** can still **send** — so
  the prompt is dismissed ("Not now", or Escape) and the run carries on. It is
  looked for *before* the Continue wall, because that screen has a Continue of
  its own and pressing it goes further into the restore flow rather than past it.
- **A send has to prove it happened.** Messenger empties the composer when it
  commits a message, so an empty box is the send itself. The button is clicked,
  and if the box is still full the run presses Enter and looks again. A draft
  still sitting in the box after both is reported as **failed**, not sent —
  there is no worse row than one that says a message went out when it did not.
- **The composer is a Lexical editor.** Inspected 2026-09-08: an empty message
  box holds `<p class="xat24cr xdj266r"><br data-lexical-managed-linebreak="true"></p>`.
  The class names are generated per build and are matched on by nothing here;
  `data-lexical-editor` and `data-lexical-managed-linebreak` are the framework's
  own and are what `COMPOSER` looks for. Messenger builds its **"To:" line out of
  the same editor**, so every generic branch carries the recipient-box
  exclusions, the composer is taken as the **last** match rather than the first
  (the To: line comes first in the document; the message box is last on both
  screens), and `write_dm` refuses outright to type unless the tab is on a `/t/`
  address with no new-message search on it.
- **Real typing.** `typing_wpm`. Setting a 400-character DM in one call fires no
  keydown events at all.
- **Skip what cannot be reached.** `skip_no_dm`, `skip_protected` — opening a
  composer for a closed inbox fails *and* still spends the day's budget.
- **Stopping on a challenge.** `stop_on_challenge` is on by default. Continuing
  through a captcha means submitting actions that all silently fail, which is
  what escalates a challenge into a lock.

Two details the settings do not spell out. The day's tally is kept **in the
store, per account, per calendar day** — a counter that only knew about the
current run would let three runs in one afternoon spend the same budget three
times over. And a closed inbox discovered at send time is **written back** to the
account store, so the next campaign passes over it instead of spending another
slot learning the same thing.
