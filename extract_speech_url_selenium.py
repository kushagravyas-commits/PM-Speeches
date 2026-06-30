import os
import time
import json
from datetime import datetime
from dateutil import parser
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import BulkWriteError, DuplicateKeyError

from seleniumbase import SB

# ---------- Load env ----------
load_dotenv()

# ---------- Config ----------
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")

END_DATE_STR = "2024-06-01"
HEADLESS = os.getenv("HEADLESS", "False").lower() in ("1", "true", "yes")

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]
urls_col = db["urls"]

urls_col.create_index("url", unique=True)
urls_col.create_index([("published_date", DESCENDING), ("url", 1)])


def collect_items(sb, collected):
    items = sb.find_elements("css selector", "#maincontainer .speechesBox")
    for item in items:
        try:
            link = item.find_element("css selector", ".speechesItemLink a").get_attribute("href")
            date_text = item.find_element("css selector", ".pwdBy").text.strip()
            parsed = parser.parse(date_text).date()
            if link not in collected:
                collected[link] = parsed
        except Exception:
            continue


def scroll_and_collect(sb, end_date_str):
    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    collected = {}
    idle_retries = 0

    sb.open("https://www.narendramodi.in/category/text-speeches")
    sb.wait_for_element("div.speechesBox", timeout=20)
    time.sleep(2)

    total_pages = int(sb.execute_script(
        'return document.getElementById("totalspeechePages").value'
    ))
    print(f"[SCRAPER] Starting with cutoff {end_date_str}, total_pages={total_pages}")

    collect_items(sb, collected)
    print(f"[SCRAPER] Initial items: {len(collected)}")

    while True:
        dates = list(collected.values())
        if dates and min(dates) <= end_date:
            print(f"[SCRAPER] Reached cutoff date {end_date_str}, stopping.")
            break

        current_page = int(sb.execute_script('return SpeechPage'))
        if current_page >= total_pages:
            print(f"[SCRAPER] All {total_pages} pages loaded, stopping.")
            break

        last_count = len(collected)

        items_before = len(sb.find_elements("css selector", "#maincontainer .speechesBox"))
        sb.execute_script('loadSpeeche(SpeechPage)')

        for _ in range(30):
            time.sleep(0.5)
            items_now = len(sb.find_elements("css selector", "#maincontainer .speechesBox"))
            if items_now > items_before:
                break

        time.sleep(0.5)

        collect_items(sb, collected)

        if len(collected) == last_count:
            idle_retries += 1
        else:
            idle_retries = 0

        if idle_retries >= 5:
            print(f"[SCRAPER] No new items after {idle_retries} retries, stopping.")
            break

        if current_page % 10 == 0:
            oldest = min(collected.values()) if collected else "N/A"
            print(f"[SCRAPER] page={current_page}/{total_pages}, collected={len(collected)}, oldest={oldest}")

    print(f"[SCRAPER] Total items collected: {len(collected)}")
    return collected


def to_mongo_datetime(d):
    return datetime(d.year, d.month, d.day)


def main():
    print(f"[MAIN] END_DATE_STR = {END_DATE_STR}")

    with SB(uc=True, headless=HEADLESS) as sb:
        raw = scroll_and_collect(sb, END_DATE_STR)

    end_date = datetime.strptime(END_DATE_STR, "%Y-%m-%d").date()
    filtered = {url: d for url, d in raw.items() if d >= end_date}

    print(f"[MAIN] scraped={len(raw)}, filtered≥{END_DATE_STR}={len(filtered)}")

    try:
        with open("speech_urls_selenium.json", "w", encoding="utf-8") as f:
            json.dump({u: d.isoformat() for u, d in filtered.items()}, f, ensure_ascii=False, indent=2)
        print("[MAIN] Saved speech_urls_selenium.json")
    except Exception as e:
        print("[MAIN] JSON save failed:", e)

    existing = set(doc["url"] for doc in urls_col.find({"url": {"$in": list(filtered.keys())}}, {"url": 1}))

    docs = []
    now = datetime.utcnow()
    for url, pub_date in sorted(filtered.items(), key=lambda x: (-x[1].toordinal(), x[0])):
        if url in existing:
            continue
        docs.append({
            "url": url,
            "published_date": to_mongo_datetime(pub_date),
            "added_at": now,
            "status": "pending"
        })

    inserted = 0
    if docs:
        try:
            r = urls_col.insert_many(docs, ordered=False)
            inserted = len(r.inserted_ids)
        except BulkWriteError as bwe:
            errs = bwe.details.get("writeErrors", [])
            inserted = len(docs) - len(errs)
        except DuplicateKeyError:
            pass

    print(f"[MAIN] MongoDB inserted={inserted}, skipped_existing={len(filtered) - inserted}")
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()