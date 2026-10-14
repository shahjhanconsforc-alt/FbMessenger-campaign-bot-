"""One module per panel, in the order the console shows them.

The console is six panels and this is six files, plus `runtime.py` for the
pills and the task poller that belong to no panel and `_common.py` for the
handful of helpers they share. Finding the code behind a screen is finding the
file with its name on it.

    campaign.py   the wizard, the drafts, the media library, the send
    fb.py         facebook itself: open the feed, and walk down it
    groups.py     Group Posting: the groups each account is in
    accounts.py   the Messenger accounts and their sessions
    scraping.py   the home-timeline walk and the roster it produces
    actions.py    Groups joining, and the Messenger rail items
    data.py       rosters, the account store, the activity stream
    settings.py   the schema the panel is generated from
    runtime.py    status pills, store health, /api/tasks/:id
"""

from . import accounts, actions, campaign, data, fb, groups, runtime, scraping, settings

# Order matters only for the docs page. Every path is distinct.
ROUTERS = [
    campaign.router,
    fb.router,
    groups.router,
    accounts.router,
    scraping.router,
    actions.router,
    data.router,
    settings.router,
    runtime.router,
]

__all__ = ["ROUTERS", "accounts", "actions", "campaign", "data", "fb", "groups",
           "runtime", "scraping", "settings"]
