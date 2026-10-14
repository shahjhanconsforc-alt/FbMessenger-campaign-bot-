"""The read-only way in - a profile fetched through Firecrawl, not a window.

Everything else in `automation/` drives a logged-in Chrome: it is how the feed
is walked and how a DM is sent, and there is no other way to do those things. A
profile RE-READ is different. It wants five facts off a public page - the name,
the bio, the follower count, whether the account is protected, whether it exists
- and driving one of the signed-in windows to read five hundred of them is five
hundred navigations charged against an account we would rather X saw doing
nothing. So when a key is set the re-read goes out through Firecrawl instead: the
request lands on Firecrawl's proxy, the console's sessions are never touched, and
the roster is filled in from HTTP.

The one fact this cannot reach is `dm_open`. Whether an inbox will take a message
is only visible to a browser that is signed in - the profile shows the Message
button to a logged-in viewer and not to Firecrawl - so a read from here leaves
that field unknown rather than guessing it, and the browser re-read stays the way
to settle it. See `x.read_profile`, which is where the two meet.

Dependency-free on purpose, the way `config` reads its own `.env`: this is one
POST and a page of parsing, and a client library to do it is a package to
install on every machine the bot runs on for no capability the standard library
withholds.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Optional

from .. import config
from ..events import log
from .browser import AutomationError


def enabled() -> bool:
    """Whether a profile re-read should go out through Firecrawl at all.

    Read live rather than captured at import, so a key added to `.env` and a
    reload is enough to turn it on without a code change.
    """
    return bool(config.FIRECRAWL_ENABLED)


def _endpoint() -> str:
    """The scrape URL, however the base was spelled.

    `FIRECRAWL_BASE_URL` may be the bare host, or already carry `/v1` or `/v2`
    from a copied example. Trim to the host and append the version this client
    speaks, so both spellings land on the same place.
    """
    base = config.FIRECRAWL_BASE_URL.rstrip("/")
    base = re.sub(r"/v[12]$", "", base)
    return f"{base}/v1/scrape"


def _scrape(url: str) -> dict[str, Any]:
    """POST one URL to Firecrawl and hand back its `data` block.

    Raises `AutomationError` for anything the caller cannot read past - no key,
    a refusal, a body that is not the success shape - because the message goes
    to the same toast every other automation failure does and is written for a
    person.
    """
    payload: dict[str, Any] = {
        "url": url,
        # Markdown for the follower line and any protected/suspended banner, the
        # metadata block for the name and bio X puts in its meta tags. `html` is
        # not asked for: nothing here reads the raw DOM, and it is the largest
        # part of the response.
        "formats": ["markdown"],
        # The profile header, the follower count and the banners are chrome, not
        # "main content"; stripping them is exactly the wrong move here.
        "onlyMainContent": False,
        "waitFor": 2500,
        "timeout": config.FIRECRAWL_TIMEOUT * 1000,
        "blockAds": True,
    }
    if config.FIRECRAWL_PROXY:
        payload["proxy"] = config.FIRECRAWL_PROXY

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(_endpoint(), data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if config.FIRECRAWL_API_KEY:
        request.add_header("Authorization", f"Bearer {config.FIRECRAWL_API_KEY}")

    try:
        with urllib.request.urlopen(request, timeout=config.FIRECRAWL_TIMEOUT + 10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8")[:300]
        except Exception:                            # noqa: BLE001 - best effort
            pass
        if err.code in (401, 403):
            raise AutomationError(
                "Firecrawl rejected the key - check FIRECRAWL_API_KEY in "
                "backend/.env.") from err
        if err.code == 402:
            raise AutomationError(
                "Firecrawl is out of credit for this key.") from err
        raise AutomationError(
            f"Firecrawl returned HTTP {err.code}. {detail}".strip()) from err
    except urllib.error.URLError as err:
        raise AutomationError(
            f"Could not reach Firecrawl at {_endpoint()} ({err.reason}).") from err
    except Exception as err:                         # noqa: BLE001 - json, timeouts
        raise AutomationError(f"Firecrawl read failed: {err}") from err

    if not parsed.get("success", True):
        raise AutomationError(
            f"Firecrawl could not scrape the page: {parsed.get('error') or 'unknown reason'}.")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise AutomationError("Firecrawl returned nothing for the page.")
    return data


# -- Turning a scrape into the row read_profile hands back ---------
_FOLLOWERS = re.compile(r"([\d.,]+\s*[KMB]?)\s*Followers", re.I)
_PROTECTED = re.compile(r"These posts are protected|Tweets are protected", re.I)
_MISSING = re.compile(
    r"This account doesn.?t exist|Account suspended|"
    r"page doesn.?t exist|Hmm.?this page doesn.?t exist", re.I)


def _name_from_title(title: str, handle: str) -> str:
    """"Name (@handle) / X" -> "Name". Falls back to the handle it was given.

    X titles a profile as the display name, the handle in parentheses, and a
    trailing site name. The display name is the first of those and the only part
    worth keeping; everything from the "(@" on is dropped, and so is a bare
    " / X" or " on X" when the parentheses are not there.
    """
    text = (title or "").strip()
    text = re.split(r"\s*\(@", text, maxsplit=1)[0]
    text = re.sub(r"\s*[/|]\s*X\s*$", "", text)
    text = re.sub(r"\s+on X\s*$", "", text)
    return text.strip()


def _meta(data: dict[str, Any], *names: str) -> str:
    """One value out of Firecrawl's metadata, trying each name in turn.

    The metadata block is flat but inconsistent across sites and versions - a
    description arrives as `description`, `ogDescription` or `og:description`
    depending on the page - so the caller lists the spellings it will accept and
    takes the first that is there.
    """
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return ""
    for name in names:
        value = meta.get(name)
        if isinstance(value, list):
            value = value[0] if value else ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _followers(markdown: str) -> Optional[int]:
    """"12.3K Followers" out of the page text -> 12300, or None if not there.

    The same reading `x._count` does, kept here rather than imported so this
    module leans on nothing in `x` - `x` imports Firecrawl, not the other way
    about, and a cycle is not worth saving eight lines.
    """
    match = _FOLLOWERS.search(markdown or "")
    if not match:
        return None
    raw = match.group(1)
    number = re.search(r"([\d.,]+)\s*([KMB])?", raw, re.I)
    if not number:
        return None
    try:
        value = float(number.group(1).replace(",", ""))
    except ValueError:
        return None
    value *= {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        (number.group(2) or "").upper(), 1)
    return int(value)


def profile(handle: str, url: str) -> dict[str, Any]:
    """Read one profile through Firecrawl, in the shape `x.read_profile` returns.

    `dm_open` is None, not False: a Firecrawl read cannot see the Message button,
    and the caller is trusted to keep whatever a browser last learned rather than
    reading None as "closed". Everything else is what the public page carries.
    """
    data = _scrape(url)
    markdown = data.get("markdown") or ""
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    status = metadata.get("statusCode") or metadata.get("status")

    missing = bool(_MISSING.search(markdown)) or status == 404
    if missing:
        log("firecrawl", f"@{handle} does not exist (Firecrawl)")
        return {"handle": handle, "missing": True, "dm_open": None,
                "protected": False, "followers": None, "name": "", "bio": ""}

    name = _meta(data, "ogTitle", "og:title", "title") or ""
    name = _name_from_title(name, handle) or handle.lstrip("@")
    bio = _meta(data, "description", "ogDescription", "og:description") or ""
    # X's meta description is often "<bio> ... The latest posts from @handle";
    # trim its boilerplate tail so the roster carries the bio and not the tagline.
    bio = re.sub(r"\s*The latest (posts|Tweets) from.*$", "", bio, flags=re.I).strip()

    return {
        "handle": handle,
        "name": name,
        "bio": bio,
        "followers": _followers(markdown),
        "dm_open": None,                             # unknowable without the session
        "protected": bool(_PROTECTED.search(markdown)),
        "missing": False,
    }
