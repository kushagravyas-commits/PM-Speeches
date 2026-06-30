import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from playwright.sync_api import sync_playwright
from pymongo import MongoClient, ASCENDING


SPREADSHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
WORKSHEET_NAME = "Speeches"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BASE_URL = "https://www.narendramodi.in/category/text-speeches"
CACHE_PATH = Path("speech_sheet_extract_cache.json")
TOKEN_PATH = Path("token_sheets.json")
CREDS_PATH = Path("credentials.json")

SAVE_EVERY = 25


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with CACHE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache: dict):
    with CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def ymd(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if value is None:
        return ""
    s = str(value)
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s


def first_video(media) -> str:
    if not isinstance(media, dict):
        return ""
    videos = media.get("videos") or []
    if not videos:
        return ""
    return normalize_youtube_url(str(videos[0]))


def get_mongo_rows_and_speech_details():
    load_dotenv(".env")
    uri = os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB", "pm_speeches")
    collection_name = os.getenv("URLS_COLLECTION", "en_urls")
    if not uri:
        raise RuntimeError("MONGODB_URI missing in .env")

    client = MongoClient(uri)
    col = client[db_name][collection_name]
    docs = list(
        col.find(
            {"url": {"$exists": True, "$ne": ""}, "published_date": {"$exists": True, "$ne": None}},
            {"_id": 0, "url": 1, "published_date": 1},
        ).sort([("published_date", ASCENDING), ("url", ASCENDING)])
    )

    urls = [d["url"] for d in docs]
    speech_details = {}
    speeches_col = client[db_name]["speeches"]
    for speech in speeches_col.find(
        {"url": {"$in": urls}},
        {"_id": 0, "url": 1, "title": 1, "media": 1},
    ):
        url = speech.get("url")
        if not url:
            continue
        speech_details[url] = {
            "title": speech.get("title", "") or "",
            "youtube_link": first_video(speech.get("media")),
            "source": "mongo.speeches",
            "page_checked": False,
        }

    return docs, speech_details


def normalize_youtube_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def extract_current_page(page) -> dict:
    return page.evaluate(
        r"""
        () => {
          const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
          const titleEl =
            document.querySelector("#article_title h1 a") ||
            document.querySelector("#article_title h1") ||
            document.querySelector(".newsUpCaptionDetailWBanner h1") ||
            document.querySelector("h1");

          const article =
            document.querySelector("article.main_article_content") ||
            document.querySelector("div#printable") ||
            document.querySelector(".news-bg") ||
            document;

          const srcs = Array.from(article.querySelectorAll("iframe"))
            .map((el) => (el.getAttribute("src") || el.src || "").trim())
            .filter(Boolean);

          const hrefs = Array.from(article.querySelectorAll("a[href]"))
            .map((el) => (el.getAttribute("href") || el.href || "").trim())
            .filter(Boolean);

          const youtube = [...srcs, ...hrefs].find((u) =>
            /(?:youtube\.com\/embed|youtube\.com\/watch|youtu\.be\/)/i.test(u)
          ) || "";

          return { title: clean(titleEl ? titleEl.textContent : ""), youtube };
        }
        """
    )


def extract_details(docs, speech_details):
    cache = load_cache()
    for url, details in speech_details.items():
        current = cache.get(url, {})
        if not current.get("title"):
            current["title"] = details.get("title", "")
        if not current.get("youtube_link"):
            current["youtube_link"] = details.get("youtube_link", "")
        current.setdefault("source", details.get("source", "mongo.speeches"))
        current.setdefault("page_checked", details.get("page_checked", False))
        cache[url] = current

    needed = [
        d for d in docs
        if d["url"] not in cache
        or cache[d["url"]].get("error")
        or not cache[d["url"]].get("title")
        or (not cache[d["url"]].get("youtube_link") and not cache[d["url"]].get("page_checked"))
    ]
    print(f"[EXTRACT] mongo_urls={len(docs)} cached={len(cache)} to_fetch={len(needed)}")

    if not needed:
        return cache

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
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
        page = context.new_page()
        page.set_default_timeout(120_000)
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_(),
        )
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)

        for i, doc in enumerate(needed, start=1):
            url = doc["url"]
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                parsed = extract_current_page(page)
                cache[url] = {
                    "title": parsed.get("title", ""),
                    "youtube_link": normalize_youtube_url(parsed.get("youtube", "")),
                    "source": "page",
                    "page_checked": True,
                    "extracted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
            except Exception as e:
                cache[url] = {
                    "title": "",
                    "youtube_link": "",
                    "error": str(e),
                    "source": "page",
                    "page_checked": False,
                    "extracted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
                print(f"[WARN] failed {i}/{len(needed)} {url}: {e}")

            if i % SAVE_EVERY == 0:
                save_cache(cache)
                print(f"[EXTRACT] fetched={i}/{len(needed)} total_cached={len(cache)}")
                time.sleep(0.5)

        save_cache(cache)
        context.close()
        browser.close()

    return cache


def authorize_gspread():
    with CREDS_PATH.open("r", encoding="utf-8") as f:
        info = json.load(f)

    if info.get("type") == "service_account":
        creds = ServiceAccountCredentials.from_service_account_file(str(CREDS_PATH), scopes=SCOPES)
        return gspread.authorize(creds)

    creds = None
    if TOKEN_PATH.exists():
        creds = UserCredentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return gspread.authorize(creds)


def ensure_worksheet(sh, title: str, rows: int, cols: int):
    try:
        ws = sh.worksheet(title)
        if ws.row_count < rows or ws.col_count < cols:
            ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols))
        return ws
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_sheet(docs, cache):
    values = [["Serial Number", "Date", "Speech Description", "Link of Transcript", "YouTube Link of Speech"]]
    for idx, doc in enumerate(docs, start=1):
        url = doc["url"]
        details = cache.get(url, {})
        values.append([
            idx,
            ymd(doc.get("published_date")),
            details.get("title", ""),
            url,
            details.get("youtube_link", ""),
        ])

    gc = authorize_gspread()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = ensure_worksheet(sh, WORKSHEET_NAME, rows=max(len(values) + 10, 4000), cols=5)
    ws.batch_clear(["A:E"])
    ws.update("A1:E" + str(len(values)), values, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("A1:E1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})
    ws.columns_auto_resize(0, 5)
    print(f"[SHEET] wrote_rows={len(values) - 1} worksheet={WORKSHEET_NAME}")


def main():
    docs, speech_details = get_mongo_rows_and_speech_details()
    if not docs:
        raise SystemExit("[MAIN] No Mongo URL rows found.")

    print(f"[MAIN] Mongo rows: {len(docs)}")
    print(f"[MAIN] Date range: {ymd(docs[0].get('published_date'))} -> {ymd(docs[-1].get('published_date'))}")
    print(f"[MAIN] Details already available from speeches collection: {len(speech_details)}")
    cache = extract_details(docs, speech_details)
    write_sheet(docs, cache)
    print("[MAIN] DONE.")


if __name__ == "__main__":
    main()
