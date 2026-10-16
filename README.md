# messenger-bot

An operator console for Facebook Messenger outreach: run your own Facebook
accounts in their own Chrome windows, walk the feed and groups to collect
people, keep a roster, and send targeted Messenger campaigns with drafts and
pictures. One Python process serves the API and the console on one port and
drives Google Chrome through Playwright. No build step.

> **Scope.** This drives *your own* accounts against Facebook's own surface.
> It stores other people's profile text and messages them, so the two things
> that matter most are the per-account caps in Settings and leaving
> **Click Send** off for a first run. There is no unsend.

## Panels

| Panel | What it does |
|---|---|
| Campaign | The wizard: accounts → people → drafts → media → send, with the live run view |
| FB Campaign | Open the feed in an account's window and walk down it |
| Group Posting | The groups each account is in; post into them |
| Accounts | The Messenger accounts, their Chrome profiles and sessions |
| Scraping | The home-timeline walk and the roster it produces |
| Actions | Group joining and the Messenger rail actions |
| Data | Rosters, the account store, the activity stream |
| Settings | Every knob the operator changes between runs |

## Layout

```
messenger-bot/
├─ run.py                     start here: python run.py
├─ run.bat                    the same, for a Windows double-click
├─ Dockerfile                 console + Playwright + Google Chrome (production image)
├─ docker-entrypoint.sh       waits for the shared display in headed mode, starts the server
├─ docker-compose.yml         the console on the VM (headed, code bind-mounted)
├─ DEPLOY.md                  how it runs on the VM
├─ deploy/
│  ├─ desktop/docker-compose.yml   the web desktop the Chrome windows show on
│  └─ host/                        KVM-host port forwards (DNAT script + unit)
├─ frontend/                  the console: index.html, app.js, style.css, mock.js
└─ backend/
   ├─ run.py / app.py         entry point; routes mounted, console served
   ├─ config.py               environment: paths, port, headless, MinIO
   ├─ schema.py               the Settings panel's schema and defaults
   ├─ store.py                the JSON store (accounts, rosters, settings)
   ├─ storage.py              the shared MinIO object store (handles, drafts, media)
   ├─ tasks.py / busy.py      background runs, one per account at a time
   ├─ events.py               the activity feed
   ├─ panels/                 one module per panel
   ├─ automation/             browser.py (Chrome lanes), facebook.py, messenger.py
   ├─ .env.example            environment template → copy to backend/.env
   └─ data/                   runtime state (gitignored): store.json, profiles/, sessions/, media/
```

`backend/data/` holds the live Facebook sessions. It is a credential store and
must never be committed, copied around, or left on a shared disk.

## Run on a desktop

```bash
pip install -r backend/requirements.txt
python -m playwright install chromium         # or use the installed Google Chrome
cp backend/.env.example backend/.env           # Windows: Copy-Item
python run.py                                 # → http://127.0.0.1:4300
```

Settings live in `backend/.env` (environment: port, headed/headless, browser
channel, MinIO) and in the Settings panel (policy: caps, delays, selectors).
A real environment variable always beats the file.

## Run in production (VM)

See **DEPLOY.md**. In short: a web desktop container exports its X display,
the console container shows its Chrome windows there, the code is a git
checkout bind-mounted into the container, and a deploy is `git pull` plus a
restart. The console is headed by default so logins, verification codes and
every automation window can be watched and used from a browser.

## Data it shares with the other bots

The object store on the databases VM holds one bucket per bot for handles and
two shared buckets for message drafts and pictures. This bot writes its handles
to `messenger-bot-usernames` and reads drafts and media from the Instagram
bot's buckets, so one set of drafts serves every campaign. Bucket names are
exactly what `backend/.env` says.
