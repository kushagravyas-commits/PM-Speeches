import csv
import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


CSV_PATH = Path("speech_sheet_rows.csv")
JSON_PATH = Path("speech_sheet_rows.json")
CACHE_PATH = Path("recent_speech_details_cache.json")
START = int(os.getenv("START", "0"))
LIMIT = int(os.getenv("LIMIT", "10"))

HEADERS = [
    "Serial Number",
    "Date",
    "Speech Description",
    "Link of Transcript",
    "YouTube Link of Speech",
]

EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const titleEl = document.querySelector('#article_title h1 a') ||
                  document.querySelector('#article_title h1') ||
                  document.querySelector('.newsUpCaptionDetailWBanner h1') ||
                  document.querySelector('h1');
  const article = document.querySelector('article.main_article_content') ||
                  document.querySelector('div#printable') ||
                  document.querySelector('.news-bg') ||
                  document;
  const urls = [
    ...Array.from(article.querySelectorAll('iframe')).map(e => e.getAttribute('src') || e.src || ''),
    ...Array.from(article.querySelectorAll('a[href]')).map(e => e.getAttribute('href') || e.href || '')
  ].map(u => (u || '').trim()).filter(Boolean);
  const youtube = urls.find(u => /(?:youtube\.com\/embed|youtube\.com\/watch|youtu\.be\/)/i.test(u)) || '';
  return {title: clean(titleEl ? titleEl.textContent : ''), youtube};
}
"""


def norm(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def write_rows(rows):
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    JSON_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
    missing = [
        r for r in rows
        if r["Date"] >= "2026-02-22"
        and (not r["Speech Description"] or not r["YouTube Link of Speech"])
    ]
    batch = missing[START:START + LIMIT]
    cache = load_cache()

    print(f"missing={len(missing)} batch={len(batch)} start={START} limit={LIMIT}", flush=True)
    if not batch:
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1365, "height": 768},
        )
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}
            else route.continue_(),
        )
        for index, item in enumerate(batch, start=1):
            url = item["Link of Transcript"]
            data = cache.get(url)
            if not data or data.get("error"):
                page = context.new_page()
                page.set_default_timeout(10_000)
                data = None
                last_error = ""
                for attempt in range(1, 4):
                    try:
                        page.goto(url, wait_until="commit", timeout=10_000)
                        page.wait_for_selector("#article_title h1, h1", timeout=10_000)
                        data = page.evaluate(EXTRACT_JS)
                        data["youtube"] = norm(data.get("youtube", ""))
                        break
                    except Exception as exc:
                        last_error = str(exc).splitlines()[0]
                        time.sleep(attempt)
                if data is None:
                    data = {"title": "", "youtube": "", "error": last_error}
                cache[url] = data
                save_cache(cache)
                page.close()

            item["Speech Description"] = item["Speech Description"] or data.get("title", "")
            item["YouTube Link of Speech"] = item["YouTube Link of Speech"] or data.get("youtube", "")
            write_rows(rows)
            print(f"{index}/{len(batch)} title={bool(item['Speech Description'])} youtube={bool(item['YouTube Link of Speech'])}", flush=True)

        browser.close()

    write_rows(rows)
    recent = [r for r in rows if r["Date"] >= "2026-02-22"]
    print(
        "after recent_missing_title="
        + str(sum(1 for r in recent if not r["Speech Description"]))
        + " recent_missing_youtube="
        + str(sum(1 for r in recent if not r["YouTube Link of Speech"])),
        flush=True,
    )


if __name__ == "__main__":
    main()
