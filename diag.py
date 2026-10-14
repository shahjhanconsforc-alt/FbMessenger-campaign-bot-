"""Read-only: open account 1 on messenger.com and report what the page actually is.

    python diag.py            (run from the repo root)

Touches nothing. No clicks, no typing, no navigation beyond the home URL.
"""
import sys, json, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backend.automation.browser import automation, goto
from backend.automation import messenger as m

ACCOUNT = "1"

def probe(page):
    errors, failed = [], []
    page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text[:200]))
    page.on("requestfailed",
            lambda r: failed.append(f"{r.failure} {r.url[:110]}"))

    goto(page, m.HOME_URL)
    # Wait for a definite answer rather than a flat sleep.
    try:
        page.wait_for_function(m._SETTLED_JS, timeout=30_000)
        settled = True
    except Exception:
        settled = False

    page.wait_for_timeout(6000)          # let the thread list try to fill

    return {
        "url": page.url,
        "title": page.title(),
        "settled": settled,
        "signed_in": m.is_logged_in(page),
        "whoami": m.peek(page),
        "threads_in_dom": len(page.eval_on_selector_all('a[href*="/t/"]', "els => els")),
        "skeletons": page.eval_on_selector_all(
            '[role="main"] div', "els => els.filter(e => !e.innerText.trim()).length"),
        "service_worker": page.evaluate(
            "() => navigator.serviceWorker "
            "? navigator.serviceWorker.getRegistrations().then(r => r.length) : -1"),
        "console_errors": errors[:12],
        "failed_requests": failed[:12],
    }

if __name__ == "__main__":
    automation.open_account(ACCOUNT, m.HOME_URL)
    print(json.dumps(automation.with_page(ACCOUNT, probe, timeout=180),
                     indent=2, ensure_ascii=False))
