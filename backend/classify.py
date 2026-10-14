"""What kind of account this is, read off its own bio.

A roster is triaged before it is messaged, and the question being asked of every
row is the same one: is this the kind of account this campaign is for. The bio
answers it, but only by being read - and nobody reads four hundred bios. So this
turns the bio into one word that a column can be sorted and filtered on.

Three rules it keeps.

**Derived, never stored.** The category is computed when a roster is read, not
written into the store. Retuning the words below then re-labels everything ever
collected, with no re-scrape - which matters, because these words WILL be
retuned: they are a guess about how people describe themselves, and the only way
to find out where the guess is wrong is to look at a few hundred real bios.

**Ordered, most specific first.** A bio saying "AI tools for real estate agents"
is a real-estate account that uses AI, not an AI account. Whichever pattern is
listed first wins, so the order below is the judgement, not the patterns.

**Honest about not knowing.** An account with no bio, or a bio that is a row of
emoji, gets no category rather than a guessed one. A column that is confidently
wrong is worse than a column with a dash in it - it gets filtered on.
"""

from __future__ import annotations

import re

# The label used when nothing matched, and when there was nothing to match on.
# One word, so the column stays narrow, and not "Other" - which reads like a
# category an account was PUT in rather than one it fell out of.
UNKNOWN = ""

# (label, pattern). First match wins, so the order is the argument: the narrow
# trades go above the broad ones, and "founder"/"investor" - which almost any
# account will say - go last so they only catch what nothing else did.
#
# Word boundaries throughout: without them "art" matches "startup", "vc" matches
# "service", and the column fills up with confident nonsense.
RULES: list[tuple[str, str]] = [
    ("Real estate", r"\b(real ?estate|realtor|realty|broker(age)?|cre\b|apartment|"
                    r"multifamily|multi-family|self storage|storage facilit|landlord|"
                    r"tenant|leasing|property manage|properties|mortgage|"
                    r"house flip|home ?buy|land ?lord|industrial park|strip ?mall)"),

    ("Crypto", r"\b(crypto|bitcoin|btc|ethereum|eth\b|web ?3|defi|nft|blockchain|"
               r"altcoin|solana|token|onchain|on-chain|dao\b|memecoin|hodl)"),

    ("Trading", r"\b(trader|trading|day ?trad|swing ?trad|forex|futures|options "
                r"trad|technical analysis|price action|charting|prop firm|"
                r"funded trader|pips?\b)"),

    ("Finance", r"\b(invest(or|ing|ment)|portfolio|hedge fund|private equity|"
                r"venture capital|\bvc\b|angel invest|wealth|financ(e|ial)|"
                r"accountant|\bcpa\b|\btax(es|ation)?\b|bookkeep|banking)"),

    ("AI", r"\b(a\.?i\.?\b|artificial intelligence|machine learning|\bml\b|\bllm\b|"
           r"gpt|prompt engineer|generative|neural|chatbot)"),

    ("Tech", r"\b(developer|engineer|software|programm(er|ing)|coder?|coding|"
             r"full ?stack|front ?end|back ?end|devops|\bsaas\b|\bapi\b|python|"
             r"javascript|typescript|react\b|rust\b|golang|open ?source|indie ?hack|"
             r"building in public|cto\b|sysadmin|cyber ?security|data scien)"),

    ("Marketing", r"\b(marketer|marketing|\bseo\b|\bsem\b|copywrit|growth hack|"
                  r"brand(ing| strategist)|social media manage|content creator|"
                  r"\bugc\b|advertis|\bppc\b|funnel|lead ?gen|agency owner)"),

    ("Ecommerce", r"\b(e-?commerce|shopify|amazon fba|dropship|print on demand|"
                  r"online store|d2c\b|dtc\b)"),

    ("Design", r"\b(designer|design(ing)?\b|\bux\b|\bui\b|graphic|illustrat|"
               r"typograph|3d artist|animator|photograph(y|er)|art director)"),

    ("News", r"\b(news|journalis|reporter|correspondent|editor|breaking|"
             r"headlines|press|media outlet|magazine|anchor)"),

    ("Sports", r"\b(football|soccer|\bnfl\b|\bnba\b|\bmlb\b|\bnhl\b|basketball|"
               r"baseball|cricket|tennis|\bufc\b|\bmma\b|boxing|athlete|"
               r"sports?\b|fantasy league|f1\b|formula 1)"),

    ("Gaming", r"\b(gamer|gaming|twitch|esports|speedrun|streamer|"
               r"video ?games?|game ?dev|playstation|xbox|nintendo|steam\b)"),

    ("Health", r"\b(fitness|workout|gym\b|nutrition|diet|health(y|care)?\b|"
               r"wellness|doctor|\bmd\b|nurse|therapis|mental health|"
               r"weight loss|bodybuild|yoga|physio)"),

    ("Education", r"\b(teacher|professor|educator|tutor|lectur|student|"
                   r"university|college|school\b|curriculum|learn(ing)? (to|how)|"
                   r"course creator|\bphd\b)"),

    ("Music", r"\b(musician|music\b|singer|songwriter|rapper|\bdj\b|producer of|"
              r"beats?\b|guitar|band\b|album|spotify)"),

    ("Politics", r"\b(politic|conservative|liberal|libertarian|maga\b|activis|"
                 r"campaign for|senator|congress|parliament|policy)"),

    ("Faith", r"\b(christ(ian)?|jesus|muslim|islam|quran|bible|god is|faith|"
              r"church|mosque|torah|jewish|spiritual)"),

    ("Memes", r"\b(meme|shitpost|funny|comedy|humou?r|satire|parody|clips?\b|"
              r"viral|entertainment)"),

    # Broad, and last on purpose. Almost every bio on X says one of these
    # somewhere, so they only get to label what nothing narrower claimed.
    ("Business", r"\b(founder|ceo\b|co-?founder|entrepreneur|business owner|"
                 r"startup|\bsmb\b|consultant|coach(ing)?\b|freelanc|"
                 r"solopreneur|\bb2b\b|acquisition)"),

    ("Writing", r"\b(writer|author|blogger|newsletter|substack|essay|poet|"
                r"storytell)"),
]

_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pattern, re.I)) for label, pattern in RULES
]

LABELS = [label for label, _ in RULES]


def categorize(bio: str = "", name: str = "") -> str:
    """One word for what this account is about, or "" when it does not say.

    The bio carries the claim; the display name is read too, because plenty of
    accounts put the whole of it there ("Bob Knakal | NYC Investment Sales") and
    leave the bio to the jokes.
    """
    text = f"{bio or ''} {name or ''}".strip()
    if not text:
        return UNKNOWN
    for label, pattern in _COMPILED:
        if pattern.search(text):
            return label
    return UNKNOWN


def with_category(row: dict) -> dict:
    """A copy of a collected row with its category filled in.

    A copy, not a mutation: the caller is usually handing out a slice of the
    store, and writing a derived field back into it would make the next read
    of that row disagree with a retuned RULES above.
    """
    return {**row, "category": categorize(row.get("bio", ""), row.get("name", ""))}
