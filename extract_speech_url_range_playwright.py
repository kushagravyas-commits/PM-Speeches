import os
import time
import json
import random
from datetime import datetime, timedelta, date
from urllib.parse import quote_plus

from dateutil import parser as date_parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError
from playwright.sync_api import sync_playwright

# ============================================================
# USER CONFIG
# ============================================================
FROM_DATE = os.getenv("FROM_DATE", "2014-01-01")      # YYYY-MM-DD
TO_DATE   = os.getenv("TO_DATE", datetime.utcnow().date().isoformat())

LANGUAGE = os.getenv("LANGUAGE", "en")               # "all" / "en" / "hi"
KEYWORD  = os.getenv("KEYWORD", "")

HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
CHUNK_DAYS = int(os.getenv("CHUNK_DAYS", "90"))

URLS_COLLECTION = os.getenv("URLS_COLLECTION", "en_urls")

CATEGORY_URL = "https://www.narendramodi.in/category/text-speeches"
SEARCH_URL_TMPL = "https://www.narendramodi.in/speech/searchspeeche?language={lang}&page={page}&keyword={kw}&fromdate={fd}&todate={td}&filtertag="

NAV_TIMEOUT_MS = 120_000
FETCH_TIMEOUT_MS = 45_000

WINDOW_RETRIES = 4
WAF_COOLDOWN_BASE = 15  # seconds
SLEEP_MIN, SLEEP_MAX = 0.6, 1.6

MAX_SEARCH_PAGES = 500   # safety cap
INSERT_CHUNK_SIZE = 1000

WAF_RE = ("please wait", "access denied", "captcha", "unusual traffic", "akamai", "reference #")

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
urls_col = db[URLS_COLLECTION]
urls_col.create_index("url", unique=True)
urls_col.create_index([("published_date", DESCENDING), ("url", 1)])


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def fmt_query_date(d: date) -> str:
    # ✅ This is what your network log shows: fromdate=01/01/2014&todate=03/31/2014
    return d.strftime("%m/%d/%Y")


def to_mongo_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def normalize_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.narendramodi.in" + href
    return "https://www.narendramodi.in/" + href


def jitter():
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))


def iter_windows(start_d: date, end_d: date, chunk_days: int):
    cur = start_d
    while cur <= end_d:
        to_d = min(cur + timedelta(days=chunk_days - 1), end_d)
        yield cur, to_d
        cur = to_d + timedelta(days=1)


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
    Inserts candidates into MongoDB.
    Returns (inserted_count, dup_count).

    Fixes:
    - Pre-filters existing URLs so we don’t get massive duplicate writeErrors
    - Treats ONLY code 11000 as duplicates
    - If any NON-duplicate write error happens, prints sample and raises (so you see real cause)
    """

    if not candidates:
        return 0, 0

    # sanity: remove empty urls
    candidates = {u: d for u, d in candidates.items() if u and str(u).strip()}
    if not candidates:
        return 0, 0

    now = datetime.utcnow()

    # ---- 1) Pre-check existing URLs (avoid dup spam) ----
    all_urls = list(candidates.keys())
    existing = set()
    for batch_urls in chunked(all_urls, 2000):
        existing.update(
            doc["url"]
            for doc in urls_col.find({"url": {"$in": batch_urls}}, {"url": 1, "_id": 0})
        )

    to_insert_items = [(u, candidates[u]) for u in all_urls if u not in existing]

    print(f"[INSERT] candidates={len(candidates)} existing_in_db={len(existing)} to_insert={len(to_insert_items)}")

    if not to_insert_items:
        return 0, len(candidates)  # everything already existed

    # ---- 2) Insert in chunks ----
    inserted_total = 0
    dup_total = 0

    # deterministic order
    to_insert_items.sort(key=lambda x: (-x[1].toordinal(), x[0]))

    for batch in chunked(to_insert_items, INSERT_CHUNK_SIZE):
        docs = [{
            "url": u,
            "published_date": to_mongo_datetime(d),
            "added_at": now,
            "status": "pending",
        } for u, d in batch]

        try:
            r = urls_col.insert_many(docs, ordered=False)
            inserted_total += len(r.inserted_ids)

        except BulkWriteError as bwe:
            errs = bwe.details.get("writeErrors", []) or []

            # Count only real dup key errors
            non_dup_errs = []
            dup_errs = 0
            for e in errs:
                if e.get("code") == 11000:
                    dup_errs += 1
                else:
                    non_dup_errs.append({
                        "code": e.get("code"),
                        "errmsg": e.get("errmsg"),
                        "op": e.get("op"),
                    })

            dup_total += dup_errs
            inserted_total += len(docs) - len(errs)

            # If anything non-duplicate happened, stop and show it (this is the REAL reason inserts fail)
            if non_dup_errs:
                print("[INSERT][ERROR] Non-duplicate write errors found (showing first 3):")
                for x in non_dup_errs[:3]:
                    print("  ", x)
                raise RuntimeError("MongoDB insert failed due to non-duplicate write errors (see logs above).")

        except DuplicateKeyError:
            # whole batch dup (rare when pre-filtering, but safe)
            dup_total += len(docs)

    return inserted_total, dup_total


def build_context(browser):
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
    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    return context, page


PARSE_SEARCH_HTML_JS = r"""
(html) => {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const boxes = Array.from(doc.querySelectorAll(".speechesBox"));
  const out = [];

  for (const box of boxes) {
    const a = box.querySelector(".speechesItemLink a") || box.querySelector("a[href]");
    const href = a ? (a.getAttribute("href") || a.href || "").trim() : "";

    const d = box.querySelector(".pwdBy");
    let dateText = d ? (d.textContent || "").trim() : "";

    if (!dateText) {
      const txt = (box.innerText || "");
      const m = txt.match(/(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}/i)
             || txt.match(/\b\d{1,2}[-\/]\d{1,2}[-\/]\d{4}\b/);
      dateText = m ? m[0] : "";
    }

    if (href && dateText) out.push({ href, dateText });
  }

  return out;
}
"""


def fetch_html_in_page(page, url: str) -> str:
    """
    Fetch inside the browser context (keeps cookies/session).
    """
    return page.evaluate(
        """async ({url, timeoutMs}) => {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), timeoutMs);
            try {
                const r = await fetch(url, { credentials: "include", signal: ctrl.signal });
                return await r.text();
            } finally {
                clearTimeout(t);
            }
        }""",
        {"url": url, "timeoutMs": FETCH_TIMEOUT_MS},
    )


def is_waf_page(html: str) -> bool:
    h = (html or "").lower()
    return any(x in h for x in WAF_RE)


def search_window(page, lang: str, keyword: str, from_d: date, to_d: date) -> dict[str, date]:
    """
    ✅ Correct pagination for search:
    /speech/searchspeeche?...&page=1
    /speech/searchspeeche?...&page=2
    ...
    (NO loadspeeche here)
    """
    results: dict[str, date] = {}

    # IMPORTANT: language param:
    # UI sends language=en/hi; "all" can be empty string
    lang_param = "" if lang.lower() == "all" else lang.lower()

    kw_param = quote_plus(keyword or "")
    fd = fmt_query_date(from_d)
    td = fmt_query_date(to_d)

    last_sig = None
    repeat_sig = 0

    for page_num in range(1, MAX_SEARCH_PAGES + 1):
        url = SEARCH_URL_TMPL.format(lang=lang_param, page=page_num, kw=kw_param, fd=fd, td=td)

        html = fetch_html_in_page(page, url)
        if is_waf_page(html):
            raise RuntimeError("WAF/blocked page returned from search endpoint")

        items = page.evaluate(PARSE_SEARCH_HTML_JS, html) or []
        if not items:
            # no more pages
            break

        # signature: first/last href on this page
        sig = (items[0]["href"], items[-1]["href"])
        if sig == last_sig:
            repeat_sig += 1
        else:
            repeat_sig = 0
            last_sig = sig

        if repeat_sig >= 2:
            # server repeating same page => stop
            break

        added = 0
        for it in items:
            u = normalize_url(it.get("href", ""))
            try:
                d = date_parser.parse(it.get("dateText", ""), fuzzy=True).date()
            except Exception:
                continue

            # Hard filter: keep only inside window (prevents 2026 leakage)
            if not (from_d <= d <= to_d):
                continue

            if u and u not in results:
                results[u] = d
                added += 1

        print(f"    [SEARCHPAGE] {fd}->{td} page={page_num} items={len(items)} added_in_range={added} total_in_range={len(results)}")

        jitter()

    return results


def main():
    from_d = parse_ymd(FROM_DATE)
    to_d = parse_ymd(TO_DATE)
    if to_d < from_d:
        raise ValueError("TO_DATE must be >= FROM_DATE")

    print(f"[MAIN] Range: {FROM_DATE} -> {TO_DATE} (inclusive)")
    print(f"[MAIN] Language: {LANGUAGE} | Keyword: {'(blank)' if not KEYWORD else KEYWORD}")
    print(f"[MAIN] Mongo collection: {MONGO_DB}.{URLS_COLLECTION}")
    print(f"[MAIN] CHUNK_DAYS={CHUNK_DAYS} | SEARCH endpoint pagination (no loadspeeche)")

    all_found: dict[str, date] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=HEADLESS)

        context, page = build_context(browser)
        # open base page once to establish cookies
        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

        for w_from, w_to in iter_windows(from_d, to_d, CHUNK_DAYS):
            print(f"[WINDOW] {fmt_query_date(w_from)} -> {fmt_query_date(w_to)}")

            window_raw = {}
            last_err = None

            for attempt in range(1, WINDOW_RETRIES + 1):
                try:
                    t0 = time.time()
                    window_raw = search_window(page, LANGUAGE, KEYWORD, w_from, w_to)
                    print(f"  [WINDOW DONE] in_range_urls={len(window_raw)} elapsed={int(time.time()-t0)}s")
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    cooldown = WAF_COOLDOWN_BASE + attempt * 10
                    print(f"  [WARN] window failed (attempt {attempt}/{WINDOW_RETRIES}): {e} -> cooldown {cooldown}s")
                    time.sleep(cooldown)
                    # restart session
                    try:
                        context.close()
                    except Exception:
                        pass
                    context, page = build_context(browser)
                    page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            if last_err is not None and not window_raw:
                print(f"  [ERROR] skipping window: {last_err}")
                continue

            for u, d in window_raw.items():
                all_found.setdefault(u, d)

            # checkpoint sorted
            items_sorted = sorted(all_found.items(), key=lambda x: (x[1].toordinal(), x[0]))
            with open("speech_urls_searchspeeche_checkpoint_sorted.json", "w", encoding="utf-8") as f:
                json.dump([{"url": u, "published_date": d.isoformat()} for u, d in items_sorted], f, ensure_ascii=False, indent=2)

            jitter()

        try:
            context.close()
        except Exception:
            pass
        browser.close()

    # Save final sorted
    items_sorted = sorted(all_found.items(), key=lambda x: (x[1].toordinal(), x[0]))
    with open("speech_urls_searchspeeche_raw_sorted.json", "w", encoding="utf-8") as f:
        json.dump([{"url": u, "published_date": d.isoformat()} for u, d in items_sorted], f, ensure_ascii=False, indent=2)

    print(f"[MAIN] Total in-range unique URLs collected: {len(all_found)}")

    before = urls_col.count_documents({})
    inserted, dups = insert_candidates(all_found)
    after = urls_col.count_documents({})

    print(f"[MAIN] Mongo before={before} after={after} inserted={inserted} dup_errors={dups}")
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()
