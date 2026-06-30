import os
import time
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Dict, Tuple, Optional, List, Any

from dateutil import parser as date_parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ============================================================
# Combined Text-Speeches URL Collector (narendramodi.in only)
#
# Strategy A (primary): Crawl the site's JS loader endpoint:
#   /speech/loadspeeche?language=<lang>&page=<n>
#
# Strategy B (fallback): Use the visible Search UI on:
#   /category/text-speeches  (and /hi/category/text-speeches)
# with From/To + Language + GO, in date windows, to backfill ranges
# when the loader stalls/repeats before reaching END_DATE_STR.
#
# Mongo insertion logic is preserved from your existing script:
#   - collection: db["urls"]
#   - unique index on "url"
#   - bulk insert ordered=False, duplicates skipped
# ============================================================

# -------------------------
# ENV / CONFIG
# -------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")

# Safety floor (earliest date you care about)
END_DATE_STR = os.getenv("END_DATE_STR", "2014-01-01")

# Incremental vs full backfill
FULL_BACKFILL = os.getenv("FULL_BACKFILL", "true").lower() in ("1", "true", "yes")

# Languages to crawl (comma-separated). Practical: en, hi
LANGUAGES = [x.strip() for x in os.getenv("LANGUAGES", "en,hi").split(",") if x.strip()]

# Search fallback chunk size (days, inclusive windows)
CHUNK_DAYS = int(os.getenv("CHUNK_DAYS", "90"))

# Playwright run mode
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

# Navigation / timeouts
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "120000"))
RESTART_CONTEXT_EVERY = int(os.getenv("RESTART_CONTEXT_EVERY", "12"))

# Loader stall detection
NO_NEW_URL_STREAK_LIMIT = int(os.getenv("NO_NEW_URL_STREAK_LIMIT", "6"))
REPEAT_SIGNATURE_LIMIT = int(os.getenv("REPEAT_SIGNATURE_LIMIT", "6"))

# Search fallback robustness
WINDOW_RETRIES = int(os.getenv("WINDOW_RETRIES", "4"))
GO_RETRIES = int(os.getenv("GO_RETRIES", "3"))
SEARCH_NO_NEW_URL_LIMIT = int(os.getenv("SEARCH_NO_NEW_URL_LIMIT", "3"))

# Request retries and cooldowns
MAX_RETRIES_PER_PAGE = int(os.getenv("MAX_RETRIES_PER_PAGE", "6"))
BASE_COOLDOWN_SEC = int(os.getenv("BASE_COOLDOWN_SEC", "20"))

# Throttling
SLEEP_BETWEEN_PAGES_MIN = float(os.getenv("SLEEP_BETWEEN_PAGES_MIN", "1.2"))
SLEEP_BETWEEN_PAGES_MAX = float(os.getenv("SLEEP_BETWEEN_PAGES_MAX", "3.0"))

# Browser recovery
HEADFUL_FALLBACK = os.getenv("HEADFUL_FALLBACK", "true").lower() in ("1", "true", "yes")
WAF_HITS_BEFORE_HEADFUL = int(os.getenv("WAF_HITS_BEFORE_HEADFUL", "2"))

# Safety caps to prevent infinite loops
MAX_PAGES_HARD_CAP = int(os.getenv("MAX_PAGES_HARD_CAP", "5000"))
MAX_SEARCH_LOAD_MORE_LOOPS = int(os.getenv("MAX_SEARCH_LOAD_MORE_LOOPS", "500"))

# Mongo insert chunk size
INSERT_CHUNK_SIZE = int(os.getenv("INSERT_CHUNK_SIZE", "1000"))

CATEGORY_URL_EN = "https://www.narendramodi.in/category/text-speeches"
CATEGORY_URL_HI = "https://www.narendramodi.in/hi/category/text-speeches"

# Loader endpoint (Strategy A)
LOAD_URL_TMPL = "https://www.narendramodi.in/speech/loadspeeche?language={lang}&page={page}"

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

# -------------------------
# Mongo init (collection name is 'urls')
# -------------------------
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
urls_col = db["all_urls"]  # <-- collection name

urls_col.create_index("url", unique=True)
urls_col.create_index([("published_date", DESCENDING), ("url", 1)])


# ============================================================
# Utilities
# ============================================================
WAF_RE = re.compile(r"please wait|access denied|captcha|unusual traffic|akamai|reference\s*#",
                    re.IGNORECASE)


def jitter_sleep(min_s: float = SLEEP_BETWEEN_PAGES_MIN, max_s: float = SLEEP_BETWEEN_PAGES_MAX):
    time.sleep(random.uniform(min_s, max_s))


def parse_floor_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def to_mongo_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def normalize_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.narendramodi.in" + href
    return "https://www.narendramodi.in/" + href


def iter_date_windows(start_d: date, end_d: date, chunk_days: int):
    """
    Inclusive windows [from,to], advancing forward.
    """
    cur = start_d
    while cur <= end_d:
        to_d = min(cur + timedelta(days=chunk_days - 1), end_d)
        yield cur, to_d
        cur = to_d + timedelta(days=1)


def fmt_ui_date(d: date) -> str:
    # Website expects MM-DD-YYYY
    return d.strftime("%m-%d-%Y")



# ============================================================
# DB helpers (incremental/backfill logic)
# ============================================================
def get_latest_db_date() -> Optional[date]:
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


# ============================================================
# Mongo insertion logic (preserved)
# ============================================================
def chunked(items, size):
    buf = []
    for x in items:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def insert_candidates(candidates: Dict[str, date]) -> Tuple[int, int]:
    """
    Insert candidates into MongoDB urls collection.
    Returns (inserted_count, duplicate_errors_count).
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
            dup_total += len(docs)

    return inserted_total, dup_total


# ============================================================
# Playwright Browser + Context management
# ============================================================
class BrowserManager:
    def __init__(self, playwright, headless: bool):
        self.p = playwright
        self.headless = headless
        self.browser = self._launch(headless)

    def _launch(self, headless: bool):
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        channel = os.getenv("CHROMIUM_CHANNEL", "chrome")
        try:
            return self.p.chromium.launch(channel=channel, headless=headless, args=args)
        except Exception:
            return self.p.chromium.launch(headless=headless, args=args)

    def relaunch(self, headless: bool):
        try:
            self.browser.close()
        except Exception:
            pass
        self.headless = headless
        self.browser = self._launch(headless=headless)

    def close(self):
        try:
            self.browser.close()
        except Exception:
            pass


def new_context(browser):
    """
    New context with:
    - realistic UA
    - no webdriver
    - block heavy assets and third-party domains (narendramodi.in only)
    """
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

    def route_handler(route, request):
        url = request.url.lower()
        # Keep traffic confined to narendramodi.in and its subdomains
        if "narendramodi.in" not in url:
            return route.abort()
        # Do not block styles/scripts: the search UI can rely on CSS/JS
        if request.resource_type in {"image", "media", "font"}:
            return route.abort()
        return route.continue_()

    context.route("**/*", route_handler)

    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    return context, page


def safe_goto(page, url: str):
    """
    More resilient navigation for flaky pages:
    - try domcontentloaded
    - fall back to commit
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(800)


def category_url_for_lang(lang: str) -> str:
    return CATEGORY_URL_HI if lang.lower() == "hi" else CATEGORY_URL_EN


# ============================================================
# In-page extraction (works for both loader HTML and category UI)
# ============================================================
EXTRACT_ITEMS_AND_STATE_JS = r"""
() => {
  const monthRe = /(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}/i;
  const shareRe = /\bShare\b|साझा करें/i;

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) return false;
    const st = window.getComputedStyle(el);
    if (!st) return false;
    return st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
  }

  const bodyTxt = document.body ? (document.body.innerText || "") : "";
  const challenged = /please wait|access denied|captcha|unusual traffic|akamai|reference\s*#/i.test(bodyTxt);

  // "No Records Found" can exist in DOM even when results exist; treat it as true only if visible.
  let noRecVisible = false;
  try {
    const els = Array.from(document.querySelectorAll("body *"))
      .filter(el => (el.textContent || "").trim() === "No Records Found");
    for (const el of els) {
      if (isVisible(el)) { noRecVisible = true; break; }
    }
  } catch (e) {}

  const container = document.querySelector("#maincontainer") || document.body || document.documentElement;
  const items = [];
  const seen = new Set();

  // 1) Structured boxes, if present
  const boxSelectors = [
    "#maincontainer .speechesBox",
    "#maincontainer .speechesBoxNew",
    "#maincontainer .speechesItem",
    ".speechesBox",
    ".speechesBoxNew",
  ];
  let boxes = [];
  for (const sel of boxSelectors) {
    const found = Array.from(document.querySelectorAll(sel));
    if (found.length) { boxes = found; break; }
  }

  function pushItem(href, dateText, hintText) {
    if (!href) return;
    const abs = href.trim();
    if (seen.has(abs)) return;
    if (!dateText) return;
    seen.add(abs);
    items.push({ href: abs, dateText, hintText: hintText || "" });
  }

  if (boxes.length) {
    for (const box of boxes) {
      const a = box.querySelector(".speechesItemLink a") || box.querySelector("a[href]");
      const href = a ? (a.getAttribute("href") || a.href || "").trim() : "";
      const dtEl = box.querySelector(".pwdBy") || box.querySelector(".date") || box.querySelector("time");
      let dateText = dtEl ? (dtEl.textContent || "").trim() : "";

      const t = (box.innerText || "").trim();
      if (!dateText) {
        const m = t.match(monthRe);
        if (m) dateText = m[0];
      }

      if (dateText && (shareRe.test(t) || monthRe.test(t))) {
        pushItem(href, dateText, t.slice(0, 2000));
      }
    }
  }

  // 2) Fallback: anchors with date-pattern in a nearby block
  if (items.length === 0) {
    const anchors = Array.from(container.querySelectorAll("a[href]"));
    for (const a of anchors) {
      const href = (a.getAttribute("href") || a.href || "").trim();
      const text = (a.textContent || "").trim();
      if (!href || text.length < 4) continue;
      if (text.toLowerCase() === "share") continue;

      const block = a.closest(".speechesBox, .speechesBoxNew, article, li, div") || a.parentElement;
      const bt = block ? ((block.innerText || "").trim()) : "";
      if (!bt) continue;

      const m = bt.match(monthRe);
      if (!m) continue;

      // Guard against nav/footer links: require "Share" marker in the same block
      if (!shareRe.test(bt)) continue;

      pushItem(href, m[0], bt.slice(0, 2000));
    }
  }

  const sig = items.slice(0, 5).map(x => x.href).join("|");
  return { challenged, noRecVisible, count: items.length, signature: sig, items };
}
"""


def parse_item_date(date_text: str) -> Optional[date]:
    if not date_text:
        return None
    try:
        return date_parser.parse(date_text, fuzzy=True).date()
    except Exception:
        return None


def evaluate_items(page) -> Dict[str, Any]:
    return page.evaluate(EXTRACT_ITEMS_AND_STATE_JS)


# ============================================================
# Strategy A: Loader crawl
# ============================================================
@dataclass
class CrawlMeta:
    strategy: str
    lang: str
    stop_reason: str
    pages_fetched: int
    oldest_date: Optional[date]
    newest_date: Optional[date]
    waf_hits: int


def crawl_with_loader(manager: BrowserManager, lang: str, stop_date: date, checkpoint_path: str) -> Tuple[Dict[str, date], CrawlMeta]:
    """
    Crawl /speech/loadspeeche?language=...&page=...
    Stop when:
      - oldest_date_on_page < stop_date
      - no-new unique urls streak hits NO_NEW_URL_STREAK_LIMIT
      - signature repeats REPEAT_SIGNATURE_LIMIT times
      - MAX_PAGES_HARD_CAP reached
    """
    collected: Dict[str, date] = {}
    no_new_streak = 0
    repeat_sig_streak = 0
    last_sig = None
    waf_hits = 0

    context, page = new_context(manager.browser)

    # Prime category page (helps ensure loader is in the right listing context)
    safe_goto(page, category_url_for_lang(lang))

    pages_since_restart = 0
    page_num = 1

    newest_seen: Optional[date] = None
    oldest_seen: Optional[date] = None

    stop_reason = "unknown"

    while page_num <= MAX_PAGES_HARD_CAP:
        if pages_since_restart >= RESTART_CONTEXT_EVERY:
            print(f"[LOADER] lang={lang}: restarting context (every {RESTART_CONTEXT_EVERY} pages).")
            try:
                context.close()
            except Exception:
                pass
            context, page = new_context(manager.browser)
            safe_goto(page, category_url_for_lang(lang))
            pages_since_restart = 0

        ok = False
        for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
            url = LOAD_URL_TMPL.format(lang=lang, page=page_num)
            try:
                safe_goto(page, url)
                data = evaluate_items(page)
            except Exception as e:
                data = {"challenged": True, "count": 0, "items": [], "signature": ""}
                print(f"[LOADER] lang={lang} page={page_num}: nav/eval error: {e}")

            if data.get("challenged") or data.get("count", 0) == 0:
                waf_hits += 1
                cooldown = BASE_COOLDOWN_SEC + attempt * 10
                print(f"[LOADER] lang={lang} page={page_num}: challenged/empty -> cooldown {cooldown}s (waf_hits={waf_hits})")
                # Headful escalation if configured and we're currently headless
                if HEADFUL_FALLBACK and manager.headless and waf_hits >= WAF_HITS_BEFORE_HEADFUL:
                    print("[LOADER] WAF threshold reached. Relaunching browser headful for better success.")
                    try:
                        context.close()
                    except Exception:
                        pass
                    manager.relaunch(headless=False)
                    context, page = new_context(manager.browser)
                    safe_goto(page, category_url_for_lang(lang))
                    waf_hits = 0  # reset after escalation

                try:
                    context.close()
                except Exception:
                    pass
                time.sleep(cooldown)
                context, page = new_context(manager.browser)
                safe_goto(page, category_url_for_lang(lang))
                continue

            sig = data.get("signature") or ""
            if sig and sig == last_sig:
                repeat_sig_streak += 1
            else:
                repeat_sig_streak = 0
                last_sig = sig

            before = len(collected)
            dates_on_page: List[date] = []

            for it in data.get("items", []):
                u = normalize_url(it.get("href", ""))
                d = parse_item_date(it.get("dateText", ""))
                if not u or not d:
                    continue
                if u not in collected:
                    collected[u] = d
                dates_on_page.append(d)

            if not dates_on_page:
                print(f"[LOADER] lang={lang} page={page_num}: parsed 0 dates -> retry")
                time.sleep(3)
                continue

            oldest = min(dates_on_page)
            newest = max(dates_on_page)
            oldest_seen = oldest if oldest_seen is None else min(oldest_seen, oldest)
            newest_seen = newest if newest_seen is None else max(newest_seen, newest)

            added = len(collected) - before
            if added == 0:
                no_new_streak += 1
            else:
                no_new_streak = 0

            if page_num == 1 or page_num % 10 == 0 or added == 0:
                print(
                    f"[LOADER] lang={lang} page={page_num} "
                    f"added={added} total={len(collected)} oldest_on_page={oldest} "
                    f"no_new_streak={no_new_streak} repeat_sig_streak={repeat_sig_streak}"
                )

            # checkpoint save every ~10 pages or when stalled
            if page_num % 10 == 0 or added == 0:
                try:
                    with open(checkpoint_path, "w", encoding="utf-8") as f:
                        json.dump({u: d.isoformat() for u, d in collected.items()}, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            ok = True
            pages_since_restart += 1
            page_num += 1
            jitter_sleep()

            # Stop if we have passed below stop_date (descending list)
            if oldest < stop_date:
                stop_reason = "reached_stop_date"
                break

            # Stop if loader is clearly stuck/repeating
            if no_new_streak >= NO_NEW_URL_STREAK_LIMIT:
                stop_reason = "no_new_streak"
                break
            if repeat_sig_streak >= REPEAT_SIGNATURE_LIMIT:
                stop_reason = "repeat_signature"
                break

            break  # successful attempt

        if not ok:
            stop_reason = "failed_after_retries"
            break

        if stop_reason in {"reached_stop_date", "no_new_streak", "repeat_signature"}:
            break

    if page_num > MAX_PAGES_HARD_CAP:
        stop_reason = "max_pages_cap"

    try:
        context.close()
    except Exception:
        pass

    meta = CrawlMeta(
        strategy="loader",
        lang=lang,
        stop_reason=stop_reason,
        pages_fetched=max(0, page_num - 1),
        oldest_date=oldest_seen,
        newest_date=newest_seen,
        waf_hits=waf_hits,
    )
    return collected, meta


# ============================================================
# Strategy B: Search UI crawl (fallback)
# ============================================================
def locate_search_root(page):
    """
    Scope interactions to the Text Speeches search widget region when possible.
    The widget is commonly under containers like 'mediaSearch' / 'mediaSearchForm'.
    """
    candidates = [
        "div.mediaSearchForm",
        "div.mediaSearch",
        "div.specchMargin",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            root = loc.first
            # sanity check: should contain GO somewhere
            if root.get_by_text("GO").count() > 0:
                return root
            # if not, still return it; the page may render differently
            return root
    return page.locator("body")


def set_input_value(locator, value: str):
    """
    Fill an input robustly even if a readonly datepicker is used.
    """
    try:
        locator.evaluate("el => el.removeAttribute('readonly')")
    except Exception:
        pass
    locator.fill(value)
    try:
        locator.dispatch_event("input")
        locator.dispatch_event("change")
        locator.dispatch_event("blur")
    except Exception:
        pass


def set_search_language(search_root, lang: str):
    """
    Language control appears in two variants:
      - <select id="serachlanguage"> with values: "" (All), "en", "hi"
      - tab-like text controls: All / English / Hindi
    """
    lang = (lang or "all").lower()
    if lang not in {"all", "en", "hi"}:
        # Search UI only exposes All/English/Hindi; other values fall back to All.
        lang = "all"

    # Variant 1: dropdown select
    sel = search_root.locator("#serachlanguage")
    if sel.count() > 0:
        val = "" if lang == "all" else lang
        try:
            sel.select_option(val)
            return
        except Exception:
            pass

    # Variant 2: clickable tabs inside search root
    label = {"all": "All", "en": "English", "hi": "Hindi"}[lang]
    try:
        search_root.get_by_text(label, exact=True).click()
    except Exception:
        try:
            search_root.locator(f"text={label}").first.click()
        except Exception:
            pass


def wait_for_search_refresh(page, prev_sig: Dict[str, Any]):
    """
    Wait until:
      - challenge appears, OR
      - visible no-records, OR
      - signature changes, OR
      - count changes
    """
    page.wait_for_function(
        """(prev) => {
          const monthRe = /(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},\\s+\\d{4}/i;
          const shareRe = /\\bShare\\b|साझा करें/i;

          function isVisible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (!r || (r.width === 0 && r.height === 0)) return false;
            const st = window.getComputedStyle(el);
            if (!st) return false;
            return st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
          }

          const bodyTxt = document.body ? (document.body.innerText || "") : "";
          const challenged = /please wait|access denied|captcha|unusual traffic|akamai|reference\\s*#/i.test(bodyTxt);
          if (challenged) return true;

          let noRecVisible = false;
          try {
            const els = Array.from(document.querySelectorAll("body *"))
              .filter(el => (el.textContent || "").trim() === "No Records Found");
            for (const el of els) { if (isVisible(el)) { noRecVisible = true; break; } }
          } catch (e) {}
          if (noRecVisible) return true;

          const container = document.querySelector("#maincontainer") || document.body;
          const anchors = Array.from(container.querySelectorAll("a[href]"));
          const items = [];
          for (const a of anchors) {
            const href = (a.getAttribute("href") || a.href || "").trim();
            const t = (a.textContent || "").trim();
            if (!href || t.length < 4) continue;
            const block = a.closest(".speechesBox, .speechesBoxNew, article, li, div") || a.parentElement;
            const bt = block ? ((block.innerText || "").trim()) : "";
            if (!bt) continue;
            const m = bt.match(monthRe);
            if (!m) continue;
            if (!shareRe.test(bt)) continue;
            items.push(href);
            if (items.length >= 5) break;
          }
          const sig = items.join("|");
          const count = items.length;
          return sig !== prev.signature || count !== prev.count;
        }""",
        arg = prev_sig,
        timeout=NAV_TIMEOUT_MS,
    )


def apply_search_window_in_place(page, lang: str, from_d: date, to_d: date):
    """
    Fill the UI search form for a date window and click GO (with retries).
    Page must already be on the Text Speeches category URL.
    """
    search_root = locate_search_root(page)

    # keyword
    kw = search_root.locator("#searchkeyword")
    if kw.count() == 0:
        kw = search_root.locator("input.inputSpeech1")
    if kw.count() > 0:
        kw.first.fill("")  # blank keyword

    # dates
    from_in = search_root.locator("#fromdate")
    if from_in.count() == 0:
        from_in = search_root.locator("input[placeholder='From']")
    to_in = search_root.locator("#todate")
    if to_in.count() == 0:
        to_in = search_root.locator("input[placeholder='To']")

    if from_in.count() > 0:
        set_input_value(from_in.first, fmt_ui_date(from_d))
    if to_in.count() > 0:
        set_input_value(to_in.first, fmt_ui_date(to_d))

    set_search_language(search_root, lang)

    # GO button variants
    go = search_root.locator("#searchspeeches")
    if go.count() == 0:
        go = search_root.locator("button.buttonGo")
    if go.count() == 0:
        go = search_root.get_by_text("GO", exact=True)

    prev = evaluate_items(page)
    prev_sig = {"signature": prev.get("signature", ""), "count": prev.get("count", 0)}

    last_err: Optional[Exception] = None

    for go_attempt in range(1, GO_RETRIES + 1):
        try:
            print(f"  [SEARCH] GO click attempt {go_attempt}/{GO_RETRIES} for {fmt_ui_date(from_d)}->{fmt_ui_date(to_d)} lang={lang}")
            go.click()
            wait_for_search_refresh(page, prev_sig)
            return
        except Exception as e:
            last_err = e
            time.sleep(1.2 * go_attempt)
            continue

    raise RuntimeError(f"GO/search did not refresh after retries: {last_err}")


def collect_search_results_in_place(page) -> Dict[str, date]:
    """
    Collect all results currently visible, and try to load more results (JS loader or scroll),
    stopping when repeated loads add no new unique URLs.
    """
    collected: Dict[str, date] = {}

    # initial
    data = evaluate_items(page)
    if data.get("challenged"):
        raise RuntimeError("Challenge/WAF page detected on results page.")
    if data.get("noRecVisible") or data.get("count", 0) == 0:
        return collected

    for it in data.get("items", []):
        u = normalize_url(it.get("href", ""))
        d = parse_item_date(it.get("dateText", ""))
        if u and d and u not in collected:
            collected[u] = d

    no_new = 0

    for loop_i in range(1, MAX_SEARCH_LOAD_MORE_LOOPS + 1):
        before = len(collected)

        has_loader = False
        try:
            has_loader = bool(page.evaluate("() => (typeof loadSpeeche === 'function' && typeof SpeechPage !== 'undefined')"))
        except Exception:
            has_loader = False

        if has_loader:
            try:
                page.evaluate("() => loadSpeeche(SpeechPage)")
            except Exception:
                break
            page.wait_for_timeout(900)
        else:
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(900)

        data2 = evaluate_items(page)
        if data2.get("challenged"):
            raise RuntimeError("Challenge/WAF page detected during load-more.")
        if data2.get("noRecVisible"):
            break

        for it in data2.get("items", []):
            u = normalize_url(it.get("href", ""))
            d = parse_item_date(it.get("dateText", ""))
            if u and d and u not in collected:
                collected[u] = d

        added = len(collected) - before
        if added == 0:
            no_new += 1
        else:
            no_new = 0

        if loop_i % 10 == 0 or added == 0:
            print(f"    [SEARCH] load_more loop={loop_i} added={added} total={len(collected)} no_new={no_new}")

        if no_new >= SEARCH_NO_NEW_URL_LIMIT:
            break

        jitter_sleep(0.6, 1.4)

    return collected


def crawl_with_search_fallback(manager: BrowserManager, lang: str, start_d: date, end_d: date, checkpoint_path: str) -> Tuple[Dict[str, date], CrawlMeta]:
    """
    Crawl date windows using the Text Speeches UI (within the category page),
    reusing the same context/page for many windows to reduce WAF pressure.

    Intended fallback when loader stalls before reaching END_DATE_STR.
    """
    all_found: Dict[str, date] = {}
    waf_hits = 0
    windows_done = 0

    newest_seen: Optional[date] = None
    oldest_seen: Optional[date] = None

    context, page = new_context(manager.browser)
    safe_goto(page, category_url_for_lang(lang))

    windows_since_restart = 0

    try:
        for w_from, w_to in iter_date_windows(start_d, end_d, CHUNK_DAYS):
            if windows_since_restart >= RESTART_CONTEXT_EVERY:
                print(f"[SEARCH] lang={lang}: restarting context after {windows_since_restart} windows.")
                try:
                    context.close()
                except Exception:
                    pass
                context, page = new_context(manager.browser)
                safe_goto(page, category_url_for_lang(lang))
                windows_since_restart = 0

            window_ok = False
            last_err: Optional[Exception] = None

            for attempt in range(1, WINDOW_RETRIES + 1):
                try:
                    t0 = time.time()
                    apply_search_window_in_place(page, lang, w_from, w_to)
                    window = collect_search_results_in_place(page)
                    dt = int(time.time() - t0)

                    print(f"  [SEARCH] window done {fmt_ui_date(w_from)}->{fmt_ui_date(w_to)} urls={len(window)} elapsed={dt}s")

                    for u, d in window.items():
                        if u not in all_found or d > all_found[u]:
                            all_found[u] = d
                        newest_seen = d if newest_seen is None else max(newest_seen, d)
                        oldest_seen = d if oldest_seen is None else min(oldest_seen, d)

                    windows_done += 1
                    windows_since_restart += 1
                    window_ok = True

                    # checkpoint after each window
                    try:
                        with open(checkpoint_path, "w", encoding="utf-8") as f:
                            json.dump({u: d.isoformat() for u, d in all_found.items()}, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

                    jitter_sleep(0.8, 1.8)
                    break

                except Exception as e:
                    last_err = e
                    waf_hits += 1
                    cooldown = BASE_COOLDOWN_SEC + attempt * 10
                    print(f"  [SEARCH] window failed (attempt {attempt}/{WINDOW_RETRIES}): {e} -> cooldown {cooldown}s (waf_hits={waf_hits})")
                    time.sleep(cooldown)

                    # Headful escalation if configured and we're currently headless
                    if HEADFUL_FALLBACK and manager.headless and waf_hits >= WAF_HITS_BEFORE_HEADFUL:
                        print("[SEARCH] WAF threshold reached. Relaunching browser headful for better success.")
                        try:
                            context.close()
                        except Exception:
                            pass
                        manager.relaunch(headless=False)
                        context, page = new_context(manager.browser)
                        safe_goto(page, category_url_for_lang(lang))
                        waf_hits = 0
                        windows_since_restart = 0

                    # Soft reset context on repeated failure
                    try:
                        context.close()
                    except Exception:
                        pass
                    context, page = new_context(manager.browser)
                    safe_goto(page, category_url_for_lang(lang))
                    windows_since_restart = 0

            if not window_ok:
                print(f"  [SEARCH] giving up window {fmt_ui_date(w_from)}->{fmt_ui_date(w_to)} after retries: {last_err}")

    finally:
        try:
            context.close()
        except Exception:
            pass

    meta = CrawlMeta(
        strategy="search",
        lang=lang,
        stop_reason="finished_windows",
        pages_fetched=windows_done,
        oldest_date=oldest_seen,
        newest_date=newest_seen,
        waf_hits=waf_hits,
    )
    return all_found, meta


# ============================================================
# Orchestrator: loader first, then fallback if needed
# ============================================================
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
    print(f"[MAIN] END_DATE_STR (floor) = {END_DATE_STR}")
    print(f"[MAIN] latest_db_date = {latest_db_date}")
    print(f"[MAIN] checkpoint_date = {checkpoint_date}")
    print(f"[MAIN] stop_date = {stop_date}")
    print(f"[MAIN] LANGUAGES={LANGUAGES}")
    print(f"[MAIN] Mongo collection = {MONGO_DB}.all_urls")

    raw_all: Dict[str, date] = {}
    meta_all: List[CrawlMeta] = []

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        manager = BrowserManager(p, headless=HEADLESS)

        try:
            for lang in LANGUAGES:
                lang = lang.strip().lower() or "en"
                print(f"\n[LANG] {lang} starting...")

                # 1) Loader crawl
                ck_loader = f"checkpoint_{lang}_loader_{ts}.json"
                loader_urls, loader_meta = crawl_with_loader(manager, lang, stop_date, ck_loader)
                meta_all.append(loader_meta)

                print(f"[LANG] {lang} loader stop_reason={loader_meta.stop_reason} "
                      f"pages={loader_meta.pages_fetched} oldest={loader_meta.oldest_date} newest={loader_meta.newest_date} "
                      f"urls={len(loader_urls)}")

                lang_urls: Dict[str, date] = dict(loader_urls)

                # 2) If full backfill requested and loader did NOT reach floor_date, switch to UI search windows
                if FULL_BACKFILL:
                    if loader_meta.oldest_date is None:
                        need_search = True
                        search_end = date.today()
                    else:
                        need_search = loader_meta.oldest_date > stop_date
                        search_end = loader_meta.oldest_date

                    if need_search:
                        print(f"[LANG] {lang} switching to SEARCH fallback for {stop_date} -> {search_end} "
                              f"(CHUNK_DAYS={CHUNK_DAYS})")

                        ck_search = f"checkpoint_{lang}_search_{ts}.json"
                        search_urls, search_meta = crawl_with_search_fallback(manager, lang, stop_date, search_end, ck_search)
                        meta_all.append(search_meta)

                        for u, d in search_urls.items():
                            if u not in lang_urls or d > lang_urls[u]:
                                lang_urls[u] = d

                        print(f"[LANG] {lang} search urls={len(search_urls)} merged_total={len(lang_urls)}")

                # Merge language urls into global raw
                for u, d in lang_urls.items():
                    if u not in raw_all or d > raw_all[u]:
                        raw_all[u] = d

                try:
                    with open(f"checkpoint_{lang}_merged_{ts}.json", "w", encoding="utf-8") as f:
                        json.dump({u: d.isoformat() for u, d in lang_urls.items()}, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

        finally:
            manager.close()

    # Always respect floor_date
    raw_all = {u: d for u, d in raw_all.items() if d >= floor_date}

    # Build candidates (same logic as your previous script)
    candidates: Dict[str, date] = {}

    if FULL_BACKFILL or latest_db_date is None:
        for url, d in raw_all.items():
            if d >= floor_date:
                candidates[url] = d
    else:
        for url, d in raw_all.items():
            if d > checkpoint_date:
                candidates[url] = d
            elif d == checkpoint_date and url not in existing_urls_on_checkpoint:
                candidates[url] = d

    candidates = {u: d for u, d in candidates.items() if d >= floor_date}

    print(f"\n[MAIN] scraped_unique={len(raw_all)}, candidates_to_add={len(candidates)}")

    # Save outputs
    try:
        with open(f"speech_urls_combined_raw_{ts}.json", "w", encoding="utf-8") as f:
            json.dump({u: d.isoformat() for u, d in raw_all.items()}, f, ensure_ascii=False, indent=2)
        print(f"[MAIN] Saved speech_urls_combined_raw_{ts}.json")
    except Exception as e:
        print("[MAIN] raw JSON save failed:", e)

    try:
        with open(f"speech_urls_combined_candidates_{ts}.json", "w", encoding="utf-8") as f:
            json.dump({u: d.isoformat() for u, d in candidates.items()}, f, ensure_ascii=False, indent=2)
        print(f"[MAIN] Saved speech_urls_combined_candidates_{ts}.json")
    except Exception as e:
        print("[MAIN] candidates JSON save failed:", e)

    if not candidates:
        print("[MAIN] Nothing to insert. DONE.")
        return

    inserted, dups = insert_candidates(candidates)
    print(f"[MAIN] MongoDB inserted={inserted}, dup_errors={dups}")
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()
