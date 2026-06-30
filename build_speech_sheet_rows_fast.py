import asyncio
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from pymongo import ASCENDING, MongoClient


OUT_CSV = Path("speech_sheet_rows.csv")
OUT_JSON = Path("speech_sheet_rows.json")
PAGE_CACHE = Path("speech_page_details_fast.json")

CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))
BASE_URL = "https://www.narendramodi.in/category/text-speeches"


def ymd(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if value is None:
        return ""
    text = str(value)
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else text


def norm_youtube(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def first_video(media) -> str:
    if not isinstance(media, dict):
        return ""
    videos = media.get("videos") or []
    return norm_youtube(str(videos[0])) if videos else ""


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def mongo_data():
    load_dotenv(".env")
    client = MongoClient(os.environ["MONGODB_URI"])
    db = client[os.getenv("MONGODB_DB", "pm_speeches")]

    urls = list(
        db.en_urls.find(
            {"url": {"$exists": True, "$ne": ""}, "published_date": {"$exists": True, "$ne": None}},
            {"_id": 0, "url": 1, "published_date": 1},
        ).sort([("published_date", ASCENDING), ("url", ASCENDING)])
    )

    url_list = [d["url"] for d in urls]
    speeches = {}
    for s in db.speeches.find(
        {"url": {"$in": url_list}},
        {"_id": 0, "url": 1, "title": 1, "media.videos": 1},
    ):
        speeches[s["url"]] = {
            "title": s.get("title", "") or "",
            "youtube_link": first_video(s.get("media")),
        }
    return urls, speeches


async def extract_page(page, url: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    data = await page.evaluate(
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
          const urls = [
            ...Array.from(article.querySelectorAll("iframe")).map((el) => el.getAttribute("src") || el.src || ""),
            ...Array.from(article.querySelectorAll("a[href]")).map((el) => el.getAttribute("href") || el.href || ""),
          ].map((u) => (u || "").trim()).filter(Boolean);
          const youtube = urls.find((u) => /(?:youtube\.com\/embed|youtube\.com\/watch|youtu\.be\/)/i.test(u)) || "";
          return { title: clean(titleEl ? titleEl.textContent : ""), youtube_link: youtube };
        }
        """
    )
    data["youtube_link"] = norm_youtube(data.get("youtube_link", ""))
    return data


async def fetch_missing(urls):
    cache = load_json(PAGE_CACHE)
    pending = [u for u in urls if u not in cache or cache[u].get("error")]
    print(f"[FETCH] pending={len(pending)} cached={len(cache)}")
    if not pending:
        return cache

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1365, "height": 768},
        )

        async def route_handler(route):
            if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_handler)
        base = await context.new_page()
        await base.goto(BASE_URL, wait_until="domcontentloaded", timeout=120_000)
        await base.close()

        sem = asyncio.Semaphore(CONCURRENCY)
        done = 0

        async def worker(url):
            nonlocal done
            async with sem:
                page = await context.new_page()
                try:
                    cache[url] = await extract_page(page, url)
                    cache[url]["page_checked"] = True
                except Exception as e:
                    cache[url] = {"title": "", "youtube_link": "", "error": str(e), "page_checked": False}
                finally:
                    await page.close()
                done += 1
                if done % 25 == 0:
                    save_json(PAGE_CACHE, cache)
                    print(f"[FETCH] done={done}/{len(pending)}")

        await asyncio.gather(*(worker(u) for u in pending))
        await browser.close()

    save_json(PAGE_CACHE, cache)
    return cache


def write_outputs(rows):
    headers = ["Serial Number", "Date", "Speech Description", "Link of Transcript", "YouTube Link of Speech"]
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    save_json(OUT_JSON, [dict(zip(headers, row)) for row in rows])
    print(f"[OUT] wrote {len(rows)} rows to {OUT_CSV} and {OUT_JSON}")


async def main():
    docs, speeches = mongo_data()
    required_fetch = []
    for doc in docs:
        detail = speeches.get(doc["url"], {})
        if not detail.get("title") or not detail.get("youtube_link"):
            required_fetch.append(doc["url"])

    print(f"[MAIN] en_urls={len(docs)} speech_matches={len(speeches)} need_page_fetch={len(set(required_fetch))}")
    page_details = await fetch_missing(sorted(set(required_fetch)))

    rows = []
    for i, doc in enumerate(docs, start=1):
        url = doc["url"]
        detail = dict(speeches.get(url, {}))
        page_detail = page_details.get(url, {})
        if not detail.get("title"):
            detail["title"] = page_detail.get("title", "")
        if not detail.get("youtube_link"):
            detail["youtube_link"] = page_detail.get("youtube_link", "")
        rows.append([i, ymd(doc.get("published_date")), detail.get("title", ""), url, detail.get("youtube_link", "")])

    write_outputs(rows)
    print(f"[MAIN] missing_title={sum(1 for r in rows if not r[2])} missing_youtube={sum(1 for r in rows if not r[4])}")


if __name__ == "__main__":
    asyncio.run(main())
