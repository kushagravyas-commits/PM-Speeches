import os
import time
import json
import random
from datetime import datetime, timedelta, date

from dateutil import parser as date_parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

load_dotenv()

# -------------------------
# CONFIG / ENV
# -------------------------
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")

URLS_COLLECTION = os.getenv("URLS_COLLECTION", "urls")  # allow en_urls, hi_urls etc.

END_DATE_STR = os.getenv("END_DATE_STR", "2014-01-01")   # safety floor
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

# set LANGUAGES="en" or "en,hi" or even "" (All) if you want to test
LANGUAGES = [x.strip() for x in os.getenv("LANGUAGES", "").split(",")]

FULL_BACKFILL = os.getenv("FULL_BACKFILL", "true").lower() in ("1", "true", "yes")

CATEGORY_URL = "https://www.narendramodi.in/category/text-speeches"
LOAD_URL_TMPL = "https://www.narendramodi.in/speech/loadspeeche?page={page}&language={lang}"

NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "120000"))
RESTART_CONTEXT_EVERY = int(os.getenv("RESTART_CONTEXT_EVERY", "12"))

# Retry knobs
MAX_RETRIES_PER_PAGE = int(os.getenv("MAX_RETRIES_PER_PAGE", "6"))
BASE_COOLDOWN_SEC = int(os.getenv("BASE_COOLDOWN_SEC", "20"))

# Stop knobs (important)
NO_NEW_URL_STREAK_LIMIT = int(os.getenv("NO_NEW_URL_STREAK_LIMIT", "6"))
REPEAT_SIGNATURE_LIMIT = int(os.getenv("REPEAT_SIGNATURE_LIMIT", "6"))
MAX_PAGES_HARD_CAP = int(os.getenv("MAX_PAGES_HARD_CAP", "5000"))  # safety

SLEEP_MIN = float(os.getenv("SLEEP_BETWEEN_PAGES_MIN", "1.2"))
SLEEP_MAX = float(os.getenv("SLEEP_BETWEEN_PAGES_MAX", "3.0"))

INSERT_CHUNK_SIZE = int(os.getenv("INSERT_CHUNK_SIZE", "1000"))

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
urls_col = db[URLS_COLLECTION]

urls_col.create_index("url", unique=True)
urls_col.create_index([("published_date", DESCENDING), ("url", 1)])


def parse_floor_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def to_mongo_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def get_latest_db_date() -> date | None:
    doc = urls_col.find_one(
        {"published_date": {"$exists": True}},
        sort=[("published_date", DESCENDING), ("url", 1)],
        projection={"published_date": 1},
    )
    if not doc:
        return None
    pd = doc.get("published_date")
    return pd.date() if isinstance(pd, datetime) else None


def get_urls_on_date(d: date) -> set[str]:
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return set(
        x["url"]
        for x in urls_col.find(
            {"published_date": {"$gte": start, "$lt": end}},
            {"url": 1, "_id": 0},
        )
    )


def normalize_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.narendramodi.in" + href
    return "https://www.narendramodi.in/" + href


def jitter():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


# Robust extractor: first try speechesBox; if not found, fall back to anchor+date-in-parent
EXTRACT_ITEMS_JS = r"""
() => {
  const out = [];

  // Primary structure (when present)
  const boxes = Array.from(document.querySelectorAll("#maincontainer .speechesBox, .speechesBox"));
  for (const box of boxes) {
    const a = box.querySelector(".speechesItemLink a") || box.querySelector("a[href]");
    const d = box.querySelector(".pwdBy");
    const href = a ? (a.getAttribute("href") || a.href || "").trim() : "";
    const dateText = d ? (d.textContent || "").trim() : "";
    if (href && dateText) out.push({ href, dateText });
  }

  // Fallback: scan anchors and find a date-like string in the same card
  if (out.length === 0) {
    const monthRe = /(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}/i;
    const cards = Array.from(document.querySelectorAll("#maincontainer *"));
    for (const card of cards) {
      const a = card.querySelector && card.querySelector("a[href]");
      if (!a) continue;
      const href = (a.getAttribute("href") || "").trim();
      const txt = (card.innerText || "");
      const m = txt.match(monthRe);
      if (href && m) out.push({ href, dateText: m[0] });
    }
  }

  const txt = document.body ? (document.body.innerText || "") : "";
  const challenged = /please wait|access denied|captcha|unusual traffic|akamai|reference\s*#/i.test(txt);

  // signature for repeat detection
  const hrefs = out.map(x => x.href).slice(0, 3).join("|");
  return { items: out, challenged, signature: hrefs, count: out.length };
}
"""


def new_context(browser):
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1365, "height": 768},
    )
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

    # block heavy assets
    def route_handler(route, request):
        if request.resource_type in {"image", "media", "font", "stylesheet"}:
            return route.abort()
        return route.continue_()
    context.route("**/*", route_handler)

    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    return context, page


def prime_total_pages(page) -> int | None:
    """
    totalspeechePages may be unreliable, so treat as hint only.
    """
    try:
        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        # this element might not exist / might be dynamic
        loc = page.locator("#totalspeechePages")
        if loc.count() == 0:
            return None
        loc.wait_for(state="attached", timeout=20_000)
        val = loc.get_attribute("value") or ""
        return int(val) if val.isdigit() else None
    except Exception:
        return None


def goto_loader(page, url: str):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(800)


def scrape_loader_page(page, page_num: int, lang: str):
    url = LOAD_URL_TMPL.format(page=page_num, lang=lang)
    goto_loader(page, url)
    return page.evaluate(EXTRACT_ITEMS_JS)


def chunked(items, size):
    buf = []
    for x in items:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def insert_candidates(candidates: dict[str, date]) -> tuple[int, int]:
    if not candidates:
        return 0, 0

    inserted_total = 0
    dup_total = 0
    now = datetime.utcnow()

    items = sorted(candidates.items(), key=lambda x: (-x[1].toordinal(), x[0]))

    for batch in chunked(items, INSERT_CHUNK_SIZE):
        docs = [{
            "url": url,
            "published_date": to_mongo_datetime(d),
            "added_at": now,
            "status": "pending",
        } for url, d in batch]

        try:
            r = urls_col.insert_many(docs, ordered=False)
            inserted_total += len(r.inserted_ids)
        except BulkWriteError as bwe:
            errs = bwe.details.get("writeErrors", [])
            dup_total += len(errs)
            inserted_total += len(docs) - len(errs)
        except DuplicateKeyError:
            dup_total += len(docs)

    return inserted_total, dup_total


def collect_until_stop_date(browser, lang: str, stop_date: date) -> dict[str, date]:
    """
    Keep pulling pages until:
    - oldest_date < stop_date, OR
    - the feed repeats / no new unique URLs for many pages
    """
    collected: dict[str, date] = {}

    context, page = new_context(browser)
    total_pages_hint = prime_total_pages(page)
    if total_pages_hint:
        print(f"[SCRAPER] total_pages_hint={total_pages_hint} (may be incomplete)")

    no_new_streak = 0
    repeat_sig_streak = 0
    last_sig = None

    page_num = 1
    pages_since_restart = 0

    while page_num <= MAX_PAGES_HARD_CAP:
        if pages_since_restart >= RESTART_CONTEXT_EVERY:
            context.close()
            context, page = new_context(browser)
            _ = prime_total_pages(page)
            pages_since_restart = 0

        ok = False
        for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
            try:
                data = scrape_loader_page(page, page_num, lang)
            except Exception as e:
                print(f"[SCRAPER] page={page_num} nav/error: {e}")
                data = {"count": 0, "challenged": True, "items": [], "signature": ""}

            if data.get("challenged") or data.get("count", 0) == 0:
                cooldown = BASE_COOLDOWN_SEC + attempt * 10
                print(f"[SCRAPER] page={page_num} challenged/empty -> cooldown {cooldown}s + restart")
                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(cooldown)
                context, page = new_context(browser)
                _ = prime_total_pages(page)
                continue

            sig = data.get("signature") or ""
            if sig and sig == last_sig:
                repeat_sig_streak += 1
            else:
                repeat_sig_streak = 0
                last_sig = sig

            dates_on_page = []
            before = len(collected)

            for it in data["items"]:
                u = normalize_url(it.get("href", ""))
                try:
                    d = date_parser.parse(it.get("dateText", ""), fuzzy=True).date()
                except Exception:
                    continue
                if u and u not in collected:
                    collected[u] = d
                dates_on_page.append(d)

            if not dates_on_page:
                print(f"[SCRAPER] page={page_num}: parsed 0 dates -> retry")
                time.sleep(3)
                continue

            oldest = min(dates_on_page)
            added = len(collected) - before

            if added == 0:
                no_new_streak += 1
            else:
                no_new_streak = 0

            if page_num % 10 == 0 or page_num == 1 or added == 0:
                print(f"[SCRAPER] lang={lang} page={page_num} oldest={oldest} added={added} total_unique={len(collected)} "
                      f"no_new_streak={no_new_streak} repeat_sig_streak={repeat_sig_streak}")

            ok = True
            page_num += 1
            pages_since_restart += 1
            jitter()

            # stop if reached floor
            if oldest < stop_date:
                print(f"[SCRAPER] Reached stop_date={stop_date} (oldest={oldest}).")
                context.close()
                return collected

            # stop if feed is clearly repeating / dead
            if no_new_streak >= NO_NEW_URL_STREAK_LIMIT or repeat_sig_streak >= REPEAT_SIGNATURE_LIMIT:
                print(f"[SCRAPER] Feed stopped producing new URLs. Likely end of this feed at page={page_num-1}, oldest≈{oldest}.")
                context.close()
                return collected

            break

        if not ok:
            print(f"[SCRAPER] page={page_num}: FAILED after retries. Stopping.")
            break

    context.close()
    return collected


def main():
    floor_date = parse_floor_date(END_DATE_STR)
    latest_db_date = get_latest_db_date()

    if latest_db_date:
        checkpoint_date = max(latest_db_date, floor_date)
        existing_urls_on_checkpoint = get_urls_on_date(checkpoint_date)
        mode = "INCREMENTAL"
    else:
        checkpoint_date = floor_date
        existing_urls_on_checkpoint = set()
        mode = "BACKFILL (DB empty)"

    stop_date = floor_date if FULL_BACKFILL else checkpoint_date

    print(f"[MAIN] MODE={mode}")
    print(f"[MAIN] FULL_BACKFILL={FULL_BACKFILL}")
    print(f"[MAIN] floor_date={floor_date} latest_db_date={latest_db_date} checkpoint_date={checkpoint_date} stop_date={stop_date}")
    print(f"[MAIN] collection={MONGO_DB}.{URLS_COLLECTION}")
    if latest_db_date:
        print(f"[MAIN] existing urls on checkpoint_date {checkpoint_date}: {len(existing_urls_on_checkpoint)}")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        raw: dict[str, date] = {}
        for lang in LANGUAGES:
            # allow blank lang as "All"
            lang = lang.strip()
            print(f"[SCRAPER] Start lang='{lang or 'ALL'}'")
            part = collect_until_stop_date(browser, lang, stop_date)
            for u, d in part.items():
                if u not in raw or d > raw[u]:
                    raw[u] = d

        browser.close()

    # Build candidates (same logic as before)
    candidates: dict[str, date] = {}

    if FULL_BACKFILL or latest_db_date is None:
        for url, d in raw.items():
            if d >= floor_date:
                candidates[url] = d
    else:
        for url, d in raw.items():
            if d > checkpoint_date:
                candidates[url] = d
            elif d == checkpoint_date and url not in existing_urls_on_checkpoint:
                candidates[url] = d

    candidates = {u: d for u, d in candidates.items() if d >= floor_date}

    print(f"[MAIN] scraped_unique={len(raw)}, candidates_to_add={len(candidates)}")

    with open("speech_urls_loader_candidates.json", "w", encoding="utf-8") as f:
        json.dump({u: d.isoformat() for u, d in candidates.items()}, f, ensure_ascii=False, indent=2)
    print("[MAIN] Saved speech_urls_loader_candidates.json")

    if not candidates:
        print("[MAIN] Nothing to insert. DONE.")
        return

    inserted, dups = insert_candidates(candidates)
    print(f"[MAIN] MongoDB inserted={inserted}, dup_errors={dups}")
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()
