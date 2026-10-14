"""Paths and the handful of knobs that are not settings.

The difference between this file and `schema.py`: a setting is something the
operator changes between runs from the Settings panel. What is here is what the
process needs before it can serve a page — where the store lives, which port to
bind, whether Chrome is allowed to be headless. Those are environment, not
policy, and putting them in the settings panel would invite changing them
halfway through a run.
"""

import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parent
FRONTEND = ROOT / "frontend"

# Everything the run writes. `backend/data/store.json`, `profiles/` and
# `screenshots/` are already in .gitignore — those directories ARE the live X
# sessions, which makes them credentials in all but name.
DATA = BACKEND / "data"
STORE_FILE = DATA / "store.json"
PROFILES = DATA / "profiles"
SCREENSHOTS = DATA / "screenshots"
MEDIA = DATA / "media"
# A copy of each signed-in cookie jar, kept OUTSIDE the profile it came from.
# The profile is still the live session; this is what survives the profile not
# doing so - a wipe, a corrupt jar, a Chrome that was killed mid-write. Same
# sensitivity as `profiles/`: these are credentials, and .gitignore covers them.
SESSIONS = DATA / "sessions"

ENV_FILE = BACKEND / ".env"


def _load_env(path: Path = ENV_FILE) -> None:
    """Read `backend/.env` before anything reads the environment.

    Hand-parsed rather than pulled in as a dependency: the file is a dozen
    `KEY=value` lines, and a package to read them is a package to install on
    every machine this runs on.

    A real environment variable always wins. `XBOT_PORT=4301 python run.py` has
    to beat the file, or overriding a setting for one run would mean editing the
    file and remembering to put it back.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env()


def _int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, "") or fallback)
    except ValueError:
        return fallback


def _bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}


HOST = os.environ.get("XBOT_HOST", "127.0.0.1")
# 4300 because 4000, 4100 and 4200 are the Instagram, Threads and Meetup bots on
# the machine these run beside. Two servers cannot share a port; the second to
# start dies on bind.
PORT = _int("XBOT_PORT", 4300)
RELOAD = _bool("XBOT_RELOAD", False)

# Headless is off by default and should stay off. A login that has to be handed
# a code from an email cannot be completed against a browser nobody can see, and
# X's challenge flows are harder on headless clients to begin with.
HEADLESS = _bool("XBOT_HEADLESS", False)

# The real Google Chrome installed on this machine, not Playwright's bundled
# Chromium. They are not the same client and X can tell: the bundled build ships
# without the proprietary codecs, reports a Chromium user agent, and is a browser
# almost nobody signs in to X with - which is exactly the profile a login
# challenge is looking for. Chrome is the ordinary one, so it is the default.
#
# `XBOT_BROWSER_CHANNEL=` (empty) goes back to the bundled Chromium, and
# `=msedge` / `=chrome-beta` name the other installed channels. Nothing breaks on
# a machine with no Chrome: the launch in `browser.py` catches it, says so, and
# falls back to the bundled build rather than refusing to open a window.
BROWSER_CHANNEL = os.environ.get("XBOT_BROWSER_CHANNEL", "chrome") or None

# How long one automation call may hold the browser thread. A follower sweep is
# minutes; the ceiling is here so a page that never resolves cannot wedge the
# queue for ever.
AUTOMATION_TIMEOUT = _int("XBOT_AUTOMATION_TIMEOUT", 60 * 60)

# How long Ctrl-C waits for open connections before the process exits anyway.
# The activity feed is a stream that never ends on its own, and uvicorn WAITS on
# every open connection before it exits - with no cap, for ever, which is why a
# plain Ctrl-C used to sit there until a second one forced it. This is that cap:
# past it uvicorn stops waiting and goes on to close the browsers. Kept short
# because nothing here holds a request open on purpose - the long runs are tasks
# behind an id, not held connections - so there is nothing worth waiting for.
SHUTDOWN_GRACE = _int("XBOT_SHUTDOWN_GRACE", 3)

# -- The object store ----------------------------------------------
# Where a finished roster is sent, so a harvest is reachable from something that
# is not this console. Optional in every sense: with no MinIO on the network the
# console runs exactly as before and the uploads report why they did not happen.
#
# These keep the names the other bots on this machine use - the Instagram and
# Threads consoles read the same ones - so one MinIO block in one .env serves
# all of them. Bucket names are used exactly as written: the handles bucket is
# this console's own (`messenger-bot-usernames`, beside `insta-bot-usernames`),
# and the messages and media buckets point at the insta bot's by default so the
# drafts and pictures are shared.
MINIO_URL = os.environ.get("MINIO_URL", "")
MINIO_USER = os.environ.get("MINIO_USER", "")
MINIO_PASSWORD = os.environ.get("MINIO_PASSWORD", "")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "messenger-bot-usernames")
MINIO_BUCKET_MESSAGES = os.environ.get("MINIO_BUCKET_MESSAGES", "insta-bot-messages")
MINIO_BUCKET_MEDIA = os.environ.get("MINIO_BUCKET_MEDIA", "insta-bot-media")
# Where fb-groups.txt lives. Empty means beside usernames_bio.txt, in this
# console's own bucket.
MINIO_BUCKET_GROUPS = os.environ.get("MINIO_BUCKET_GROUPS", "")
# HTTP unless told otherwise: this talks to a MinIO on the same machine or the
# same LAN, where a self-signed certificate is the worse failure of the two.
MINIO_SECURE = _bool("MINIO_SECURE", False)

# -- Firecrawl -----------------------------------------------------
# The read-only profile scrape can go out through Firecrawl instead of driving a
# local window, so that a re-read of five hundred profiles leaves no automation
# footprint on any of the logged-in accounts - the request comes off Firecrawl's
# proxy, not off a browser this console is signed in on.
#
# It is OFF unless a key (or a self-hosted base URL) is set: with neither, the
# members re-read behaves exactly as before and reads through the browser. What
# Firecrawl CANNOT tell us is whether an inbox is open - `dm_open` needs the
# logged-in session - so that stays the browser's job and is left untouched by a
# Firecrawl read rather than being blanked. The rest (name, bio, followers,
# protected, missing) is public and comes back over HTTP.
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
# The cloud unless pointed at a self-hosted instance. Trailing slash and any
# `/v1`/`/v2` suffix are trimmed in the client so either spelling works here.
FIRECRAWL_BASE_URL = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")
# `stealth` is the residential-proxy mode built to get past anti-bot, which is
# the whole reason to route X reads through here; `basic`/`auto` are the cheaper
# datacenter options for a self-hosted box that does not sell stealth.
FIRECRAWL_PROXY = os.environ.get("FIRECRAWL_PROXY", "stealth")
# One profile read's ceiling, in seconds. A stealth fetch with a wait is slower
# than a datacenter one, so this is generous.
FIRECRAWL_TIMEOUT = _int("FIRECRAWL_TIMEOUT", 60)
# Explicit override. Unset, Firecrawl is used whenever there is something to
# reach it with - a key, or a base URL that is not the cloud default (which is
# only useful with a key). `FIRECRAWL_ENABLED=0` forces it off with a key still
# in the file; `=1` forces it on for a self-hosted box that needs no key.
_FIRECRAWL_SELF_HOSTED = FIRECRAWL_BASE_URL.rstrip("/").split("/v")[0] not in (
    "https://api.firecrawl.dev", "http://api.firecrawl.dev")
FIRECRAWL_ENABLED = _bool("FIRECRAWL_ENABLED",
                          bool(FIRECRAWL_API_KEY) or _FIRECRAWL_SELF_HOSTED)


def ensure_dirs() -> None:
    for path in (DATA, PROFILES, SCREENSHOTS, MEDIA, SESSIONS):
        path.mkdir(parents=True, exist_ok=True)
