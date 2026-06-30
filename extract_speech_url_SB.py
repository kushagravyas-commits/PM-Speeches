import os
import time
import json
import random
from datetime import datetime, timedelta, date

from dateutil import parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# =========================
# ENV / CONFIG
# =========================
load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")

# Safety floor (earliest date you care about)
END_DATE_STR = os.getenv("END_DATE_STR", "2014-01-01")

# Run headless
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

# Languages to scrape (comma-separated), e.g. "en" or "en,hi"
LANGUAGES = [x.strip() for x in os.getenv("LANGUAGES", "en").split(",") if x.strip()]

# If true: scrape down to END_DATE_STR and insert anything missing (skip duplicates)
FULL_BACKFILL = os.getenv("FULL_BACKFILL", "true").lower() in ("1", "true", "yes")

CATEGORY_URL = "https://www.narendramodi.in/category/text-speeches"
LOAD_URL_TMPL = "https://www.narendramodi.in/speech/loadspeeche?page={page}&language={lang}"

# Tuning knobs for WAF / stability
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "120000"))
MAX_RETRIES_PER_PAGE = int(os.getenv("MAX_RETRIES_PER_PAGE", "6"))
BASE_COOLDOWN_SEC = int(os.getenv("BASE_COOLDOWN_SEC", "20"))
SLEEP_MIN = float(os.getenv("SLEEP_BETWEEN_PAGES_MIN", "1.2"))
SLEEP_MAX = float(os.getenv("SLEEP_BETWEEN_PAGES_MAX", "3.0"))
RESTART_CONTEXT_EVERY = int(os.getenv("RESTART_CONTEXT_EVERY", "12"))

# Mongo insert chunk size
INSERT_CHUNK_SIZE = int(os.getenv("INSERT_CHUNK_SIZE", "1000"))

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
urls_col = db["urls"]

urls_col.create_index("url", unique=True)
urls_col.create_index([("published_date", DESCENDING), ("url", 1)])


# =========================
# DB helpers
# =========================
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
    if isinstance(pd, datetime):
        return pd.date()
    return None


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


# =========================
# Playwright page parsing
# =========================
EXTRACT_ITEMS_JS = r"""
() => {
  const boxes = Array.from(document.querySelectorAll("#maincontainer .speechesBox, .speechesBox"));
  const items = [];

  for (const box of boxes) {
    const a = box.querySelector(".speechesItemLink a");
    const d = box.querySelector(".pwdBy");
    const href = a ? (a.getAttribute("href") || "").trim() : "";
    const dateText = d ? (d.textContent || "").trim() : "";
    if (href && dateText) items.push({ href, dateText });
  }

  const htmlText = document.documentElement ? (document.documentElement.innerText || "") : "";
  const challenged = /please wait|access denied|captcha|unusual traffic|akamai|reference\s*#/i.test(htmlText);

  return { count: items.length, challenged, items };
}
"""


def normalize_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.narendramodi.in" + href
    return "https://www.narendramodi.in/" + href


def jitter():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


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

    # light stealth
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


def prime_and_get_total_pages(page) -> int:
    page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.locator("#totalspeechePages").wait_for(state="attached", timeout=NAV_TIMEOUT_MS)
    val = page.locator("#totalspeechePages").get_attribute("value") or "0"
    return int(val)


def goto_loader(page, url: str):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return
    except PWTimeoutError:
        page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(800)


def scrape_page_items(page, page_num: int, lang: str):
    url = LOAD_URL_TMPL.format(page=page_num, lang=lang)
    goto_loader(page, url)
    return page.evaluate(EXTRACT_ITEMS_JS)


def collect_until_stop_date(browser, lang: str, stop_date: date, total_pages: int) -> dict[str, date]:
    """
    Collect URLs until we pass below stop_date.
    Stop condition: oldest_on_page < stop_date
    (this ensures we have fully covered stop_date items in a descending list).
    """
    collected: dict[str, date] = {}

    context, page = new_context(browser)
    _ = prime_and_get_total_pages(page)

    pages_since_restart = 0
    page_num = 1

    while page_num <= total_pages:
        if pages_since_restart >= RESTART_CONTEXT_EVERY:
            context.close()
            context, page = new_context(browser)
            _ = prime_and_get_total_pages(page)
            pages_since_restart = 0

        ok = False
        for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
            try:
                data = scrape_page_items(page, page_num, lang)
            except Exception as e:
                print(f"[SCRAPER] lang={lang} page={page_num}: nav/error: {e}")
                data = {"count": 0, "challenged": True, "items": []}

            if data.get("challenged") or data.get("count", 0) == 0:
                cooldown = BASE_COOLDOWN_SEC + (attempt * 10)
                print(f"[SCRAPER] lang={lang} page={page_num}: challenged/empty -> cooldown {cooldown}s + restart")
                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(cooldown)
                context, page = new_context(browser)
                _ = prime_and_get_total_pages(page)
                continue

            dates_on_page: list[date] = []
            for it in data["items"]:
                u = normalize_url(it["href"])
                try:
                    d = parser.parse(it["dateText"], fuzzy=True).date()
                except Exception:
                    continue
                if u not in collected:
                    collected[u] = d
                dates_on_page.append(d)

            if not dates_on_page:
                print(f"[SCRAPER] lang={lang} page={page_num}: parsed 0 dates -> retry")
                time.sleep(5)
                continue

            oldest = min(dates_on_page)

            if page_num % 10 == 0 or page_num == 1:
                print(f"[SCRAPER] lang={lang} page={page_num}/{total_pages}, collected={len(collected)}, oldest_on_page={oldest}")

            ok = True
            page_num += 1
            pages_since_restart += 1
            jitter()

            if oldest < stop_date:
                print(f"[SCRAPER] lang={lang}: passed below stop_date={stop_date} (oldest_on_page={oldest}). Stop.")
                context.close()
                return collected

            break

        if not ok:
            print(f"[SCRAPER] lang={lang} page={page_num}: FAILED after retries. Stopping.")
            break

    context.close()
    return collected


# =========================
# Insert helpers
# =========================
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
    """
    Insert candidates into MongoDB urls collection.
    Returns (inserted_count, duplicate_errors_count)
    """
    if not candidates:
        return 0, 0

    inserted_total = 0
    dup_total = 0
    now = datetime.utcnow()

    # Build docs in deterministic order
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
            # whole batch duplicates
            dup_total += len(docs)

    return inserted_total, dup_total


# =========================
# MAIN
# =========================
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

    # Stop date depends on FULL_BACKFILL
    stop_date = floor_date if FULL_BACKFILL else checkpoint_date

    print(f"[MAIN] MODE={mode}")
    print(f"[MAIN] FULL_BACKFILL={FULL_BACKFILL}")
    print(f"[MAIN] END_DATE_STR (floor) = {END_DATE_STR}")
    print(f"[MAIN] latest_db_date = {latest_db_date}")
    print(f"[MAIN] checkpoint_date = {checkpoint_date}")
    print(f"[MAIN] stop_date = {stop_date} (stop when oldest < stop_date)")
    if latest_db_date:
        print(f"[MAIN] existing urls on checkpoint_date ({checkpoint_date}): {len(existing_urls_on_checkpoint)}")

    # Playwright scrape
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=HEADLESS)
        except Exception:
            browser = p.chromium.launch(headless=HEADLESS)

        # total_pages
        ctx0, page0 = new_context(browser)
        total_pages = prime_and_get_total_pages(page0)
        ctx0.close()

        print(f"[MAIN] total_pages={total_pages}")

        raw: dict[str, date] = {}
        for lang in LANGUAGES:
            print(f"[SCRAPER] Start lang={lang}")
            part = collect_until_stop_date(browser, lang, stop_date, total_pages)
            for u, d in part.items():
                if u not in raw or d > raw[u]:
                    raw[u] = d

        browser.close()

    # Build candidates
    candidates: dict[str, date] = {}

    if FULL_BACKFILL or latest_db_date is None:
        # Backfill: include everything >= floor_date (duplicates will be skipped on insert)
        for url, d in raw.items():
            if d >= floor_date:
                candidates[url] = d
    else:
        # Incremental:
        # insert URLs newer than checkpoint_date,
        # plus same-date missing URLs
        for url, d in raw.items():
            if d > checkpoint_date:
                candidates[url] = d
            elif d == checkpoint_date and url not in existing_urls_on_checkpoint:
                candidates[url] = d

        # Always respect floor_date
        candidates = {u: d for u, d in candidates.items() if d >= floor_date}

    print(f"[MAIN] scraped_unique={len(raw)}, candidates_to_add={len(candidates)}")

    # Save candidates JSON
    try:
        with open("speech_urls_playwright_candidates.json", "w", encoding="utf-8") as f:
            json.dump({u: d.isoformat() for u, d in candidates.items()}, f, ensure_ascii=False, indent=2)
        print("[MAIN] Saved speech_urls_playwright_candidates.json")
    except Exception as e:
        print("[MAIN] JSON save failed:", e)

    if not candidates:
        print("[MAIN] Nothing to insert. DONE.")
        return

    inserted, dups = insert_candidates(candidates)
    print(f"[MAIN] MongoDB inserted={inserted}, dup_errors={dups}")
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()
