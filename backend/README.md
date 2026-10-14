# backend

The server the console was written against. `frontend/mock.js` answered that
contract out of localStorage; this answers it with a real browser, and serves the
console itself so there is one process on one port.

```bash
pip install -r backend/requirements.txt
python -m playwright install chromium     # once
python backend/run.py                     # http://127.0.0.1:4300
```

`python run.py` from inside this directory and `python -m backend.run` from the
root both work too.

## Where things are

```
backend/
├─ run.py            the entry point
├─ app.py            routes mounted, errors shaped, the console served
├─ config.py         paths, port, headless - environment, not policy
├─ schema.py         the settings schema the Settings panel is generated from
├─ store.py          the JSON store, and the roster/account-store write
├─ storage.py        MinIO: a finished roster as handles + bios, optional
├─ classify.py       what an account is, read off its bio - derived, not stored
├─ events.py         the activity feed, and the SSE endpoint's body
├─ tasks.py          a long run behind an id: progress, pause, resume, stop
├─ automation/
│  ├─ browser.py     Chrome, and the one thread allowed to touch it
│  ├─ firecrawl.py   the read-only profile fetch, off a proxy not a window
│  └─ x.py           every selector and every flow on x.com
└─ panels/           one module per panel on the screen
   ├─ campaign.py    the wizard, drafts, media, the send
   ├─ accounts.py    the X accounts and their sessions
   ├─ scraping.py    the home-timeline walk and the roster it produces
   ├─ data.py        accounts + bios + categories, the store, the activity stream
   ├─ settings.py    read, coerce, reset
   └─ runtime.py     status pills, store health, /api/tasks/:id
```

Finding the code behind a screen is finding the file with its name on it. The two
that are not panels are named for what they are: `runtime.py` is the chrome
around them, `_common.py` the handful of helpers they share.

## The decisions worth knowing before reading it

**A long run is a task.** A sweep is scrolling with no end. POST starts it and
returns an id; `watchTask()` in `app.js` polls `/api/tasks/:id` until the state
stops being `running`, and each frame carries the whole run state - the run owns
the roster and the console paints what it is handed. Nothing holds a request
open.

**One browser, one thread.** Playwright's sync API belongs to the thread that
created it, and there is one Chrome besides. Every call into it is posted to a
single owner thread. The status pill does *not* go through that queue - it reads
a plain-Python mirror, so it keeps answering in milliseconds while a sweep has
the browser for twenty minutes.

**One profile per account.** `data/profiles/<id>` is a persistent Chrome profile,
and it *is* the session. Two accounts sharing one would be one account as far as
X is concerned. Logging out clears the cookies and leaves the profile; removing
an account from the list leaves the directory alone entirely.

**Selectors live in one file.** `automation/x.py` is the part that rots. When X
ships a build that breaks a scrape, the repair is one file with a list of strings
at the top of it.

**Reads can leave the browser.** The home-feed walk is behind the login wall and
stays on the local Chrome. The profile *re-read* (the Data-step "read members")
does not have to: set `FIRECRAWL_API_KEY` and it goes out through Firecrawl's
proxy instead, so re-reading five hundred profiles spends no navigations on any
signed-in account and needs no window open. Firecrawl brings back the public
facts - name, bio, followers, protected, exists - but not `dm_open`, which only a
logged-in viewer can see; a Firecrawl re-read leaves the inbox answer standing
rather than blanking it. Off by default: no key, and the re-read reads off a
window exactly as before. See `automation/firecrawl.py` and the FIRECRAWL block
in `.env.example`.

## What it stores, and where

| | |
|---|---|
| `data/store.json` | accounts, sessions, rosters, the account store, drafts, settings, the feed |
| MinIO `x-*` buckets | a finished roster as handles + bios, one text file per run |
| `data/profiles/<id>/` | one Chrome profile per account - the live X sessions |
| `data/media/` | uploaded images, served back at `/api/media/file/<name>` |
| `data/screenshots/` | reserved |

All four are gitignored. The first two are credentials in all but name: passwords
are in `store.json` in the clear, and the profile directories hold session
cookies that are worth more than the passwords.

## The safety knobs are settings, not code

Everything in the README's *On not looking like a bot* section is enforced in
`panels/campaign.py`, from the values in `schema.py`:

- **Per-account caps.** Held per account *per day*, in the store, not per run - a
  counter that only knew about this run would let three runs in one afternoon
  spend the day's budget three times over. A cap of `0` sends nothing; it is read
  literally rather than as "unlimited", because the two readings differ by a
  whole campaign and only one is safe to get wrong.
- **Randomised pacing.** Every gap and every scroll pause is rolled from a range.
- **Real typing.** `typing_wpm`, key by key, jittered per keystroke, Shift+Enter
  for line breaks - a plain Enter in X's composer sends the message.
- **Skip what cannot be reached.** A closed inbox found at send time is written
  back to the store, so the next campaign does not spend a slot finding out
  again.
- **Stopping on a challenge.** A captcha or a verification prompt ends the run
  where it stands.

`click_send` off is the first run's shape: the DM is typed into the composer and
left there. Nothing is marked messaged, because nothing was delivered.

## Talking to it directly

`/api/docs` is the generated reference for all forty routes.

```bash
curl localhost:4300/api/store/health
curl localhost:4300/api/settings | python -m json.tool
curl -N localhost:4300/api/data/activity/stream      # the feed, live
```
