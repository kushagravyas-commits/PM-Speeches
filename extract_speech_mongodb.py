import os
import time
import hashlib
import re
from datetime import datetime
from collections import Counter

from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError
from seleniumbase import SB
try:
    from selenium.common.exceptions import (
        TimeoutException,
        NoSuchWindowException,
        InvalidSessionIdException,
        WebDriverException,
        StaleElementReferenceException,
    )
except Exception:
    TimeoutException = Exception
    NoSuchWindowException = Exception
    InvalidSessionIdException = Exception
    WebDriverException = Exception
    StaleElementReferenceException = Exception

# ---------- Load env ----------
load_dotenv()
# Add near imports (top of file)
try:
    from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
except Exception:
    StaleElementReferenceException = Exception
    WebDriverException = Exception


def detect_languages_from_text(text: str):
    """
    Returns a list of language tags like ["en"], ["hi"], or ["en","hi"].
    Uses script presence (Latin vs Devanagari) so bilingual speeches are supported.
    """
    if not text:
        return ["und"]

    sample = text[:20000]  # plenty for detection, keeps it fast

    devanagari = len(re.findall(r"[\u0900-\u097F]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    total = devanagari + latin

    langs = []
    if total > 0:
        # Thresholds prevent tiny English acronyms inside Hindi text from marking as bilingual
        if devanagari >= 50 and (devanagari / total) >= 0.12:
            langs.append("hi")
        if latin >= 50 and (latin / total) >= 0.12:
            langs.append("en")

    # Optional fallback if nothing detected (keeps your requirement “use any library”)
    if not langs:
        try:
            from langdetect import detect_langs
            preds = detect_langs(sample)
            for p in preds:
                if p.prob >= 0.35:
                    langs.append(p.lang)
        except Exception:
            pass

    if not langs:
        langs = ["und"]

    # Stable order
    order = {"en": 0, "hi": 1}
    langs = sorted(set(langs), key=lambda x: order.get(x, 99))
    return langs


def _is_stale_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "stale element reference" in msg
        or "stale element" in msg
        or "detached from document" in msg
        or "is not attached to the page document" in msg
    )


def safe_extract_speech(driver, url, max_attempts=4):
    """
    Wrapper that retries ONLY on stale-element-type failures.
    Does NOT modify extract_speech() logic.
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            # Clearing page between retries reduces “stale element not found in current frame”
            if attempt > 1:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(0.6)

            return extract_speech(driver, url)

        except Exception as e:
            last_err = e
            if _is_stale_error(e):
                print(f"  [RETRY] stale element (attempt {attempt}/{max_attempts}) — retrying")
                time.sleep(1.2 * attempt)
                continue
            # Not a stale-element error -> let your existing handler deal with it
            raise

    print(f"  [ERROR] stale element persisted after {max_attempts} attempts: {last_err}")
    return None

MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
URLS_COL_NAME = os.getenv("URLS_COLLECTION", "en_urls")
urls_col = db[URLS_COL_NAME]
speeches_col = db["speeches"]

print(f"[INIT] Using URLs collection: '{URLS_COL_NAME}'")

speeches_col.create_index("url", unique=True)
speeches_col.create_index([("published_date", DESCENDING), ("url", 1)])


# ──────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────

def make_id(url):
    return hashlib.sha256(url.encode()).hexdigest()


def parse_caption_date(caption_text):
    """
    Extract date from captionDate text like:
      'Published By : Admin | February 8, 2026 | 08:35 IST'
    Returns (datetime_obj, date_string) or (None, caption_text).
    """
    if not caption_text:
        return None, ""
    parts = [p.strip() for p in caption_text.split("|")]
    for part in parts:
        try:
            parsed = dateutil_parser.parse(part, fuzzy=False)
            return parsed, part
        except (ValueError, OverflowError):
            continue
    return None, caption_text


# ──────────────────────────────────────────────
# Speaker detection (HTML-aware)
# ──────────────────────────────────────────────

def extract_speaker_from_p(p):
    """
    p: Selenium element for <p>
    Looks for <strong> containing a colon (:) or dash (- / -- / —) as speaker marker.
    Returns (speaker, spoken_text) or (None, None).
    """
    try:
        strongs = p.find_elements("tag name", "strong")
    except Exception:
        return None, None

    if not strongs:
        return None, None

    strong_text = strongs[0].text.strip()
    if not strong_text:
        return None, None

    # Try colon separator first (narendramodi.in pattern: "Prime Minister:")
    if re.search(r"[:：]", strong_text):
        speaker = re.split(r"[:：]", strong_text, maxsplit=1)[0].strip()
        full_text = p.text.strip()
        # Remove the speaker label (with colon) from the paragraph text
        spoken = re.sub(
            r"^\s*" + re.escape(speaker) + r"\s*[:：]\s*",
            "",
            full_text,
        ).strip()
        if speaker and spoken:
            return speaker, spoken

    # Try dash separator (pmindia.gov.in pattern: "Speaker –")
    if re.search(r"[-\u2013\u2014]", strong_text):
        speaker = re.split(r"[-\u2013\u2014]", strong_text, maxsplit=1)[0].strip()
        full_text = p.text.strip()
        spoken = full_text.replace(strong_text, "", 1).strip()
        spoken = re.sub(r"^[\s\-\u2013\u2014]+", "", spoken)
        if speaker and spoken:
            return speaker, spoken

    return None, None


# ──────────────────────────────────────────────
# Fallback plain-text dialogue parser
# ──────────────────────────────────────────────

def _is_plausible_speaker(name):
    if len(name) < 2:
        return False
    if len(name.split()) > 4:
        return False
    return True


def split_full_text_into_segments(full_text):
    """
    Fallback: convert full_text with 'Name: speech...' or 'Name-- speech...' lines
    into dialogue segments.  Returns list of segments or None.
    Uses 4-layer validation (regex + word count + repetition + density).
    """
    if not full_text:
        return None

    lines = full_text.splitlines()

    speaker_line_re = re.compile(
        r"^\s*([^\s.,;:!?*\"\'\(\)][^.,;:!?*\"\'\(\)]{0,38})\s*[-\u2013\u2014]\s*(.+)$"
    )

    candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = speaker_line_re.match(stripped)
        if m:
            speaker = m.group(1).strip()
            rest = m.group(2).strip()
            if _is_plausible_speaker(speaker):
                candidates.append((i, speaker, rest))

    if len(candidates) < 2:
        return None

    speaker_counts = Counter(spk for _, spk, _ in candidates)
    recurring = {spk for spk, cnt in speaker_counts.items() if cnt >= 2}
    if not recurring:
        return None
    if sum(1 for _, s, _ in candidates if s in recurring) < len(candidates) * 0.5:
        return None

    non_empty = sum(1 for l in lines if l.strip())
    if len(candidates) < max(3, non_empty * 0.1):
        return None

    segments = []
    current = None
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = speaker_line_re.match(line.strip())
        speaker_ok = False
        if m:
            speaker = m.group(1).strip()
            rest = m.group(2).strip()
            speaker_ok = _is_plausible_speaker(speaker)
        if m and speaker_ok:
            current = {"speaker": speaker, "text": rest}
            segments.append(current)
        else:
            if current:
                current["text"] += "\n\n" + line.strip()
            else:
                current = {"speaker": "Narration", "text": line.strip()}
                segments.append(current)

    for idx, seg in enumerate(segments, start=1):
        seg["index"] = idx

    return segments if segments else None


# ──────────────────────────────────────────────
# Smart skip: determine which URLs to scrape
# ──────────────────────────────────────────────
class FatalBrowserError(RuntimeError):
    """Signals that the Chrome session is broken and SB must be restarted."""
    pass


def _is_stale_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "stale element reference" in msg
        or "stale element" in msg
        or "detached from document" in msg
        or "is not attached to the page document" in msg
    )


def _is_fatal_browser_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "no such window" in msg
        or "target window already closed" in msg
        or "web view not found" in msg
        or "invalid session id" in msg
        or "chrome not reachable" in msg
        or "disconnected" in msg
        or "session deleted" in msg
        or "timed out receiving message from renderer" in msg
        or "renderer" in msg and "timeout" in msg
    )


def _try_recover_window(driver) -> bool:
    """If we somehow lost focus on the active tab, switch back to a valid handle."""
    try:
        handles = driver.window_handles
        if handles:
            driver.switch_to.window(handles[0])
            return True
    except Exception:
        pass
    return False


def safe_extract_speech(driver, url, max_attempts=4):
    """
    Wrapper retries on stale errors.
    If browser/session is broken, raises FatalBrowserError so main() can restart SB cleanly.
    DOES NOT modify extract_speech() logic.
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(0.5)

            return extract_speech(driver, url)

        except Exception as e:
            last_err = e

            # 1) Stale element -> retry in same session
            if _is_stale_error(e):
                print(f"  [RETRY] stale element (attempt {attempt}/{max_attempts}) — retrying")
                time.sleep(1.0 * attempt)
                continue

            # 2) Fatal browser/session -> try quick window recovery, else bubble up
            if _is_fatal_browser_error(e):
                recovered = _try_recover_window(driver)
                if recovered:
                    print(f"  [RECOVER] switched to a valid window handle — retrying")
                    time.sleep(1.0)
                    continue
                raise FatalBrowserError(str(e))

            # 3) Any other error -> let caller handle (your existing behavior)
            raise

    print(f"  [ERROR] stale element persisted after {max_attempts} attempts: {last_err}")
    return None

def get_urls_to_process():
    """
    Find all URLs that exist in `urls` but NOT in `speeches`.
    This fixes the "urls > speeches" problem (missed old URLs) because it doesn't
    depend on last processed date.
    Returns newest-first.
    """
    total_urls = urls_col.count_documents({})
    total_speeches = speeches_col.count_documents({})

    pipeline = [
        {
            "$lookup": {
                "from": "speeches",
                "localField": "url",
                "foreignField": "url",
                "as": "speech_match",
            }
        },
        # keep only those where no matching speech exists
        {"$match": {"speech_match.0": {"$exists": False}}},
        # keep only needed fields
        {"$project": {"url": 1, "published_date": 1}},
        # newest first
        {"$sort": {"published_date": -1, "url": 1}},
    ]

    to_process = list(urls_col.aggregate(pipeline, allowDiskUse=True))

    print(
        f"[SKIP] urls_total={total_urls}, speeches_total={total_speeches}, "
        f"missing_speeches={len(to_process)}"
    )
    return to_process


# ──────────────────────────────────────────────
# Extract speech content from a single page
# ──────────────────────────────────────────────

def extract_speech(driver, url):
    """
    Extract speech content from a narendramodi.in speech page.
    Returns a speech document dict, or None on failure.
    """
    driver.get(url)

    # Wait for the article content to load
    for _ in range(40):
        articles = driver.find_elements("css selector", "article.main_article_content")
        if articles:
            break
        time.sleep(0.5)
    else:
        print(f"  [WARN] No article found on {url[:60]}")
        return None

    # CRITICAL: only use the FIRST article (second is a different speech)
    article = articles[0]

    # --- Title ---
    title = ""
    try:
        h1s = driver.find_elements("css selector", "#article_title h1")
        if h1s:
            title = h1s[0].text.strip()
    except Exception:
        pass

    # --- Date ---
    date_obj = None
    date_str = ""
    try:
        cap_dates = driver.find_elements("css selector", ".captionDate")
        if cap_dates:
            date_obj, date_str = parse_caption_date(cap_dates[0].text.strip())
    except Exception:
        pass

    # --- Paragraphs (from first article only) ---
    p_elements = article.find_elements("css selector", "p")

    # --- Speaker detection (pass 1: count strong+separator hits) ---
    speaker_hits = 0
    for p in p_elements:
        spk, _ = extract_speaker_from_p(p)
        if spk:
            speaker_hits += 1

    # --- Build content ---
    if speaker_hits >= 2:
        # Dialogue path
        segments = []
        idx = 1
        for p in p_elements:
            text = p.text.strip()
            if not text:
                continue
            spk, spoken = extract_speaker_from_p(p)
            if spk:
                segments.append({"index": idx, "speaker": spk, "text": spoken})
                idx += 1
            else:
                if segments:
                    segments[-1]["text"] += "\n\n" + text
                else:
                    segments.append({"index": idx, "speaker": "Narration", "text": text})
                    idx += 1

        full_text = "\n\n".join(s["text"] for s in segments)
        content = {"full_text": full_text, "segments": segments}
        speech_type = "dialogue"
    else:
        # Monologue path
        full_text = "\n\n".join(
            p.text.strip() for p in p_elements if p.text and p.text.strip()
        )
        content = {"full_text": full_text, "segments": None}
        speech_type = "monologue"

        # Fallback: check plain text for dialogue patterns
        fallback = split_full_text_into_segments(full_text)
        if fallback:
            content["segments"] = fallback
            speech_type = "dialogue"

    # --- Media extraction (from first article only) ---
    images = list({
        img.get_attribute("src")
        for img in article.find_elements("css selector", "img")
        if img.get_attribute("src")
        and "cdn.narendramodi.in" in img.get_attribute("src")
        and "tiwtterlogo" not in img.get_attribute("src")  # exclude twitter icon
    })

    videos = list({
        iframe.get_attribute("src")
        for iframe in article.find_elements("css selector", "iframe")
        if iframe.get_attribute("src")
        and ("youtube.com" in iframe.get_attribute("src")
             or "youtu.be" in iframe.get_attribute("src"))
    })

    tweets = list({
        iframe.get_attribute("src")
        for iframe in article.find_elements("css selector", "iframe")
        if iframe.get_attribute("src")
        and "platform.twitter.com" in iframe.get_attribute("src")
    })

    return {
        "speech_id": make_id(url),
        "url": url,
        "title": title,
        "published_date": date_obj or datetime.utcnow(),
        "date_str": date_str,
        "speaker": "Narendra Modi",
        "speech_type": speech_type,
        "content": content,
        "media": {"images": images, "videos": videos, "tweets": tweets},
        "added_at": datetime.utcnow(),
    }


def main():
    to_process = get_urls_to_process()

    if not to_process:
        print("[MAIN] Nothing new to process. Done.")
        return

    print(f"[MAIN] Processing {len(to_process)} speeches...")

    # Proactive session recycling prevents renderer crashes on long runs
    RESTART_EVERY = int(os.getenv("RESTART_EVERY", "40"))
    FATAL_RETRIES_PER_URL = int(os.getenv("FATAL_RETRIES_PER_URL", "3"))

    failed = []
    fatal_counts = {}

    idx = 0
    while idx < len(to_process):
        # Start a fresh browser session
        with SB(uc=True, headless=HEADLESS) as sb:
            driver = sb.driver

            # Tighten timeouts so hung pages fail faster than Chrome's 300s renderer timeout
            try:
                driver.set_page_load_timeout(90)
                driver.set_script_timeout(90)
            except Exception:
                pass

            processed_in_session = 0

            while idx < len(to_process) and processed_in_session < RESTART_EVERY:
                url_doc = to_process[idx]
                url = url_doc["url"]
                pub_date = url_doc.get("published_date", "")

                print(f"[{idx+1}/{len(to_process)}] {pub_date} - {url[:70]}")

                try:
                    speech_doc = safe_extract_speech(driver, url, max_attempts=4)

                except FatalBrowserError as e:
                    # Count fatal retries for this URL
                    fatal_counts[url] = fatal_counts.get(url, 0) + 1
                    print(f"  [FATAL] Browser/session issue: {e}")
                    print(f"  [FATAL] Will restart browser (fatal_attempt={fatal_counts[url]}/{FATAL_RETRIES_PER_URL})")

                    # After N fatal restarts on same URL, give up on it and move on
                    if fatal_counts[url] >= FATAL_RETRIES_PER_URL:
                        print("  [GIVEUP] Too many fatal restarts for this URL; marking as failed and continuing.")
                        failed.append(url_doc)
                        idx += 1
                        processed_in_session += 1

                    # Break to exit SB context and start a new session
                    break

                except Exception as e:
                    print(f"  [ERROR] extraction failed: {e}")
                    failed.append(url_doc)
                    idx += 1
                    processed_in_session += 1
                    time.sleep(1)
                    continue

                if not speech_doc:
                    failed.append(url_doc)
                    idx += 1
                    processed_in_session += 1
                    time.sleep(1)
                    continue

                if not speech_doc["content"]["full_text"].strip():
                    print("  [WARN] Empty content, skipping.")
                    failed.append(url_doc)
                    idx += 1
                    processed_in_session += 1
                    time.sleep(1)
                    continue

                # Add language tag(s) (supports bilingual)
                speech_doc["language"] = detect_languages_from_text(speech_doc["content"]["full_text"])
                print(f"  [LANG] {speech_doc['language']}")

                # Insert into MongoDB
                try:
                    speeches_col.insert_one(speech_doc)
                    seg_count = len(speech_doc["content"]["segments"]) if speech_doc["content"]["segments"] else 0
                    print(f"  [OK] {speech_doc['speech_type']}, "
                          f"{len(speech_doc['content']['full_text'])} chars, "
                          f"{seg_count} segments")
                except DuplicateKeyError:
                    print("  [SKIP] Already exists in speeches collection.")
                except Exception as e:
                    print(f"  [ERROR] MongoDB insert failed: {e}")
                    failed.append(url_doc)

                idx += 1
                processed_in_session += 1
                time.sleep(1)

        # end SB context: browser closed; while loop continues and opens fresh session

    # Final report + write failures to file so you can rerun only failures if needed
    total = speeches_col.count_documents({})
    print(f"[MAIN] Done. Total speeches in collection: {total}")

    if failed:
        print(f"[MAIN] Failed URLs: {len(failed)} (saved to failed_speeches.json)")
        try:
            with open("failed_speeches.json", "w", encoding="utf-8") as f:
                json.dump(
                    [{"url": d.get("url"), "published_date": str(d.get("published_date", ""))} for d in failed],
                    f, ensure_ascii=False, indent=2
                )
        except Exception as e:
            print(f"[MAIN] Could not write failed_speeches.json: {e}")

if __name__ == "__main__":
    main()
