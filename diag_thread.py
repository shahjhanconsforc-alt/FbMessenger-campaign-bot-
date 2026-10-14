"""Read-only: what does Messenger actually do with a handle?

    python diag_thread.py alihaider saman salman      (from the repo root)
    python diag_thread.py --account 2 alihaider

Opens each handle the way a campaign tab does - messenger.com/t/<handle> first,
facebook.com/messages/t/<handle> if that bounces - and reports what the tab
became. Types nothing and sends nothing; the only thing it does is look.

This exists because "no chat available" was the run's answer to three different
situations, and none of them could be told apart from the table.
"""
import sys, json, pathlib, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backend.automation.browser import automation
from backend.automation import messenger as m

WAIT = 25.0          # seconds a route is given before it is called a dead end


def _settle(page, label):
    """Poll one route until it is a chat, or out of time.

    Does exactly what a campaign tab does with what it finds - presses a
    "Continue", declines the PIN prompt - so that what this reports is what the
    run will get, not a snapshot of a screen the run would have got past. It
    types nothing at all: neither a draft, nor a handle into a search box.
    """
    deadline = time.monotonic() + WAIT
    steps, last, walls, pins = [], "", 0, 0
    while time.monotonic() < deadline:
        try:
            state, why = m.chat_state(page, label, skip_wall=walls >= 4)
        except Exception as err:                     # a challenge
            return {"state": "error", "why": str(err)[:200], "steps": steps}
        last = why or last

        if state == "restore" and pins < 3:
            pins += 1
            steps.append("declined the PIN prompt" if m.dismiss_restore(page)
                         else "could not dismiss the PIN prompt")
            continue
        if state == "wall" and walls < 4:
            walls += 1
            steps.append("pressed Continue" if m.press_continue(page) else "Continue vanished")
            continue
        if state:
            return {"state": state, "why": why, "steps": steps,
                    "seconds": round(WAIT - (deadline - time.monotonic()), 1)}
        page.wait_for_timeout(400)
    return {"state": "timed out", "why": last, "steps": steps, "seconds": WAIT}


def probe(page, handles):
    out = []
    for handle in handles:
        row = {"handle": handle}
        m.begin_chat(page, handle)
        row["messenger.com"] = _settle(page, handle)
        row["url_after"] = page.url

        if row["messenger.com"]["state"] != "open":
            m.begin_chat(page, handle, on_facebook=True)
            row["facebook.com"] = _settle(page, handle)
            row["url_after"] = page.url

        # What the page SAYS, which is the part no selector can guess at.
        try:
            row["text"] = " ".join((page.locator("body").inner_text(timeout=3000) or "").split())[:300]
        except Exception:
            row["text"] = ""
        out.append(row)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    account = "1"
    if args and args[0] == "--account":
        account, args = args[1], args[2:]
    if not args:
        print(__doc__)
        raise SystemExit(2)

    automation.open_account(account, m.HOME_URL)
    print(json.dumps(automation.with_page(account, lambda p: probe(p, args), timeout=600),
                     indent=2, ensure_ascii=False))
