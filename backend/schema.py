"""The settings schema — the same list `mock.js` carried, now authoritative.

It lives on the server rather than in `app.js` on purpose: the Settings panel is
generated from whatever the server describes, so adding a row here is the whole
job of putting a new knob on the screen.

`help` is not documentation of the field, it is the reason the field exists.
Someone reading the panel at 2am deciding whether to raise a cap gets the
consequence of raising it, on the row, at the moment they are about to.
"""

from typing import Any

SCHEMA: list[dict[str, Any]] = [
    {"key": "click_send", "type": "bool", "default": True,
     "help": "On, a run sends. Off, it types the message and leaves the composer."},

    {"key": "daily_dm_cap", "type": "number", "default": 120, "min": 0, "max": 1000,
     "help": "Messages per day across every account. The run stops at this number."},
    {"key": "per_account_dm_cap", "type": "number", "default": 40, "min": 0, "max": 500,
     "help": "Messages per day from one account. Messenger counts these per "
             "account, not per campaign, and going past its limit is what gets an "
             "account stopped from starting new conversations."},
    {"key": "batch_size", "type": "number", "default": 10, "min": 1, "max": 200,
     "help": "How many messages before the run takes a break."},
    {"key": "batch_pause_minutes", "type": "number", "default": 12, "min": 0, "max": 240,
     "help": "How long the break lasts. The group joining below happens inside "
             "it; whatever is left of the time after that is waited out."},
    {"key": "break_groups", "type": "number", "default": 5, "min": 0, "max": 50,
     "help": "During each break every account on the run joins this many "
             "Facebook groups from Discover, in a tab of its own, then comes "
             "back to the campaign. 0 joins none."},
    {"key": "actions_min_messages", "type": "number", "default": 5, "min": 0, "max": 500,
     "help": "After this many messages - rolled between the two, and rolled again "
             "each time - every account on the run presses its Messenger rail "
             "items (Chats, Marketplace, Requests, Archive, the settings menu and "
             "the sidebar) in its own window before sending goes on, and a row "
             "for it lands in the live table. 0 turns it off."},
    {"key": "actions_max_messages", "type": "number", "default": 10, "min": 0, "max": 500,
     "help": "The top of that range."},
    {"key": "actions_min_items", "type": "number", "default": 2, "min": 1, "max": 6,
     "help": "How many of the six rail items one turn presses, rolled between the "
             "two and picked at random each time. All six every time is a "
             "pattern; two or three of them is a person poking about."},
    {"key": "actions_max_items", "type": "number", "default": 3, "min": 1, "max": 6,
     "help": "The top of that range."},
    {"key": "actions_min_rest_minutes", "type": "number", "default": 2, "min": 0, "max": 240,
     "help": "After the items are pressed the run rests this long - rolled "
             "between the two - before the next message goes. 0 to 0 carries "
             "straight on."},
    {"key": "actions_max_rest_minutes", "type": "number", "default": 5, "min": 0, "max": 240,
     "help": "The top of that range."},

    {"key": "campaign_auto_restart", "type": "bool", "default": True,
     "help": "On, a campaign that crashes, stalls, or is cut off by the server "
             "going down picks itself up from where it was: the rows already "
             "sent stay sent, the one it was on is tried again, the plan of "
             "breaks and actions carries on. The table is written to disk as "
             "the run goes, which is what makes that possible. Off, it stays "
             "where it stopped until Resume is pressed."},
    {"key": "campaign_stall_minutes", "type": "number", "default": 12, "min": 0, "max": 180,
     "help": "No message sent, failed or started for this long - outside a "
             "break, a rest or the gap - and the run is treated as stuck: its "
             "windows are closed, reopened, and it carries on from the same "
             "row. 0 never does this."},
    {"key": "campaign_max_restarts", "type": "number", "default": 5, "min": 1, "max": 50,
     "help": "How many restarts one campaign gets before it stops trying and "
             "waits for Resume. Each one waits a little longer than the last."},

    {"key": "parallel_tabs", "type": "number", "default": 5, "min": 1, "max": 10,
     "help": "How many chats one account drives at once, each in its own tab. "
             "What this buys is the WAITING: a Messenger thread takes tens of "
             "seconds to boot, and at 1 the run pays that before every single "
             "message. It does not send any faster - the gap below still "
             "spaces the sends themselves - it stops the run spending its day "
             "loading pages. 1 is the old one-tab-at-a-time run, and keeps the "
             "faster path of clicking the thread in the list instead of "
             "loading it."},
    {"key": "close_tab_after_send", "type": "bool", "default": False,
     "help": "Off, a chat stays open after its message goes, and a finished run "
             "leaves a window of the conversations it wrote to, there to be "
             "read rather than taken on trust from the table. The run keeps "
             "driving its parallel_tabs chats either way - what refills the "
             "pool is the SLOT being freed, not the tab being shut - so the "
             "cost of leaving them is memory: one Chromium tab per recipient, "
             "which on a few hundred recipients is a lot of window. On, each "
             "tab closes the moment its message has gone. Only ever applies to "
             "a tab this run opened after a real send: a chat that could not be "
             "opened, or one the run was still holding when it stopped, is "
             "always left where it is. At parallel_tabs 1 nothing opens a tab "
             "at all, so this does nothing."},

    {"key": "gap_min_seconds", "type": "number", "default": 35, "min": 0, "max": 3600,
     "help": "The gap between two messages is rolled from this range. A constant gap "
             "is a metronome, and a metronome is the easiest thing to spot in a "
             "request log."},
    {"key": "gap_max_seconds", "type": "number", "default": 95, "min": 0, "max": 3600,
     "help": "The top of that range."},
    {"key": "typing_wpm", "type": "number", "default": 240, "min": 40, "max": 900,
     "help": "A message is typed key by key at roughly this speed. Setting a 400-"
             "character message in one call fires no keydown events at all."},

    {"key": "scroll_pause_min", "type": "number", "default": 0.25, "min": 0, "max": 30,
     "help": "How long the feed rests between passes, at the fastest. Most of a "
             "pass is spent easing from one account to the next to read its "
             "card, and that travelling is paid for out of this - so on a busy "
             "screenful there is often no pause left to take."},
    {"key": "scroll_pause_max", "type": "number", "default": 1.1, "min": 0, "max": 30,
     "help": "And at the slowest. The gap is rolled between the two rather than "
             "fixed: a pass every 1.00s for four hundred rows is a metronome, "
             "and a metronome is the easiest thing to spot in a request log."},

    {"key": "fb_scroll_speed", "type": "number", "default": 300, "min": 10, "max": 2000,
     "help": "How fast the Facebook feed scrolls, in pixels per second. The "
             "motion is continuous and this speed is held constant for the whole "
             "walk - it is not the two pause settings above, which belong to the "
             "Messenger sweep and step from one card to the next. 90 is an "
             "unhurried read; 300 is skimming."},
    {"key": "fb_scroll_slice_ms", "type": "number", "default": 900, "min": 100, "max": 5000,
     "help": "How often the run looks at its tabs, in milliseconds - for a post "
             "whose comment bar is on screen, for Stop, for a captcha. The "
             "scrolling itself runs inside each page and never pauses for this. "
             "Lower is a more responsive Stop and a sharper eye for posts; it "
             "does not change the speed."},

    {"key": "fb_comment_text", "type": "text", "default": "hi",
     "help": "What gets typed into each post's comment box as the feed is "
             "walked, key by key, followed by Enter. Blank turns commenting off "
             "and the walk goes back to scrolling and nothing else."},
    {"key": "fb_comment_limit", "type": "number", "default": 15, "min": 0, "max": 500,
     "help": "How many posts one run will comment on before it stops. 0 is no "
             "cap. This is the setting that matters most on this panel: the same "
             "short comment on post after post is what Facebook's spam detection "
             "looks for, and a checkpoint is how it answers. Start low."},
    {"key": "fb_comment_gap_min", "type": "number", "default": 3, "min": 0, "max": 600,
     "help": "The shortest wait after posting a comment before the next post is "
             "picked up, in seconds."},
    {"key": "fb_comment_icon_offsets", "type": "text", "default": "0px -231px",
     "help": "How the comment bubble is recognised. Facebook draws the like / "
             "comment / share icons from one sprite image and picks a glyph with "
             "`background-position`, so that offset IS the icon's identity. It "
             "changes when Facebook reissues the sprite - when it does, run "
             "Probe on the FB panel: it lists the offsets actually on the post, "
             "and one of them is the new bubble. Several may be given, "
             "comma-separated."},
    {"key": "fb_comment_gap_max", "type": "number", "default": 8, "min": 0, "max": 600,
     "help": "And the longest. Rolled between the two rather than fixed - a "
             "comment every n seconds exactly is a metronome, and a metronome is "
             "the easiest thing to spot in a request log."},
    {"key": "groups_visit_direct", "type": "bool", "default": False,
     "help": "Off - the default - every group is reached by PRESSING ITS NAME "
             "in the Groups list, which means going back to that list after "
             "each one. On, the walk instead reads every name the list is "
             "showing and then goes to those groups one after another by "
             "address, returning to the list only when it needs more names: "
             "less bouncing back to the Groups page, but only the first group "
             "of each run is an actual press on a row."},
    {"key": "groups_visit_gap_min", "type": "number", "default": 1.0, "min": 0, "max": 60,
     "help": "How long the Group Posting walk rests between one group and the "
             "next, rolled from this range. Lower is faster; a run that opens "
             "forty groups with no pause at all is forty page loads in a "
             "minute from one account, which is not what a person looks like."},
    {"key": "groups_visit_gap_max", "type": "number", "default": 2.5, "min": 0, "max": 60,
     "help": "The top of that range."},

    {"key": "groups_join_min", "type": "number", "default": 5, "min": 1, "max": 50,
     "help": "Groups joining, on the Actions panel: the fewest groups one run "
             "joins from facebook.com/groups/discover. The number is rolled "
             "between this and the next setting each run."},
    {"key": "groups_join_max", "type": "number", "default": 10, "min": 1, "max": 50,
     "help": "And the most. Both are set from the boxes on the Actions panel."},
    {"key": "fb_like_first", "type": "bool", "default": True,
     "help": "Press Like on each post before commenting on it - the order a "
             "person does it in. A post already liked is left alone (the button "
             "is a toggle, and pressing it again would take the like away), and "
             "a like that cannot be made never stops the comment."},
    {"key": "fb_tabs", "type": "number", "default": 1, "min": 1, "max": 10,
     "help": "How many tabs one run scrolls in. Each extra tab is another copy "
             "of facebook.com opened in the same window, and the walk takes "
             "turns between them - one slice of scrolling each, a comment when "
             "a post is in view. The comment cap and the memory of what has "
             "been commented on are shared, so two tabs never comment on the "
             "same post. Set from the box on the FB Campaign panel."},
    {"key": "scrape_group_tabs", "type": "number", "default": 1, "min": 1, "max": 8,
     "help": "FB Groups scraping: how many groups are read at the same time, a "
             "tab each in the account's own window. One group is mostly waiting "
             "for Facebook to send the next page of members, so four tabs read "
             "roughly four times as fast. Above four, Facebook sends each tab "
             "its pages more slowly. 1 reads one group at a time, by pressing "
             "each group's name in the Groups list."},
    {"key": "scrape_group_scroll_smooth", "type": "bool", "default": True,
     "help": "FB Groups scraping: scroll the People list smoothly, the way a "
             "person reading it would. Off jumps straight down, which is faster "
             "but is also the most obviously automated thing a page can do."},
    {"key": "scrape_group_scroll_seconds", "type": "number", "default": 1.2, "min": 0.2, "max": 8,
     "help": "FB Groups scraping: the wait between one scroll and the next, in "
             "every tab. Lower reads faster; too low and the tabs scroll past "
             "people Facebook has not sent yet, and a group stops short."},
    {"key": "scrape_group_scroll_step", "type": "number", "default": 0.9, "min": 0.2, "max": 3,
     "help": "FB Groups scraping: how far each scroll goes, as a share of the "
             "window - 1 is one screenful. Bigger covers a long list in fewer "
             "turns; past about 1.5 the list can outrun what has loaded."},
    {"key": "scrape_group_members_max", "type": "number", "default": 0, "min": 0, "max": 100000,
     "help": "FB Groups scraping: the most people read from one group before the "
             "walk moves on to the next group. 0 reads every member of every "
             "group, which on a group of tens of thousands is a long time on "
             "one group."},
    {"key": "scrape_stall_seconds", "type": "number", "default": 90, "min": 0, "max": 3600,
     "help": "The scraping watchdog. If an account's walk has added no new handle "
             "for this many seconds - a feed that stopped loading, a page that "
             "froze, a list that ran dry - its page is reloaded and the walk "
             "carries on. A walk that dies outright is restarted the same way. "
             "0 turns the watchdog off."},
    {"key": "fb_refresh_after", "type": "number", "default": 60, "min": 0, "max": 3600,
     "help": "The watchdog. If no comment has landed for this many seconds while "
             "the walk is commenting - stuck on a post, a comment box that will "
             "not clear, a feed that stopped loading - facebook.com is reloaded "
             "and the walk carries on from the fresh feed. Two failed comments "
             "in a row reload it straight away. Posts already commented on are "
             "not commented on again after a reload. 0 turns it off."},

    # `skip_no_dm` and `skip_protected` were here, and both failed a recipient
    # before the profile was opened, on a reading some earlier run had left in
    # the store. A picked handle is now always tried and the Chat column reports
    # what was actually found - a fresh answer instead of a stale assumption -
    # so the knobs would only be a way to reinstate the old, wrong behaviour.
    # Filtering unreachable handles belongs in step 2, where they are picked.
    {"key": "chat_open_seconds", "type": "number", "default": 2, "min": 0, "max": 30,
     "help": "How long an opened chat is left on screen before the run moves to "
             "the next handle. Long enough to see what opened; every second of "
             "it is paid once per recipient."},
    {"key": "stop_on_challenge", "type": "bool", "default": True,
     "help": "Stop the run on a captcha or a verification prompt. Carrying on "
             "means submitting actions that all silently fail, which is what "
             "escalates a challenge into a lock."},

    {"key": "profile_url", "type": "text", "default": "https://www.facebook.com/{handle}",
     "tokens": ["{handle}", "{id}"],
     "help": "How a handle becomes a profile link. A setting because an account "
             "with no vanity username is reached as "
             "facebook.com/profile.php?id={handle} instead, and a run should not "
             "be recompiled to switch."},
    {"key": "target_defaults", "type": "list", "default": "",
     "help": "Targets a fresh sweep starts from when the pool is empty."},
]

_BY_KEY = {row["key"]: row for row in SCHEMA}


def defaults() -> dict[str, Any]:
    return {row["key"]: row["default"] for row in SCHEMA}


def coerce(key: str, value: Any) -> Any:
    """Coerce on the way IN, not on the way out of the form.

    Everything arriving from an <input> is a string, and a cap stored as "40"
    compares wrong against a number for ever after.
    """
    row = _BY_KEY.get(key)
    if row is None:
        raise KeyError(key)
    kind = row["type"]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if kind == "number":
        number = float(value)
        if "min" in row:
            number = max(float(row["min"]), number)
        if "max" in row:
            number = min(float(row["max"]), number)
        # Whole numbers come back as ints so a cap renders as "40", not "40.0".
        return int(number) if float(number).is_integer() else number
    return str(value)


def known(key: str) -> bool:
    return key in _BY_KEY
