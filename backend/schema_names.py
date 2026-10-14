"""Words a scraper can read off a profile link that are not a person's name.

Facebook labels some links with its own words rather than the name - a post by
a Page or a boosted post heads its author link with "See owner profile", and a
member row carries "Add friend" and "Message" as links beside the name. Taken
as names they made dozens of different accounts look like one account listed
again and again. Anything matching here is treated as no name at all.
"""

from __future__ import annotations

import re

NOT_A_NAME = re.compile(
    r"^(see owner profile|see profile|view profile|visit profile|add friend|"
    r"message|send message|follow|following|unfollow|see all|see more|"
    r"admin|moderator|you|sponsored|suggested for you)$",
    re.I,
)


def is_not_a_name(text: str) -> bool:
    return bool(NOT_A_NAME.match(" ".join(str(text or "").split())))
