# PM Speeches URL Scraper

This repository contains the working scraper used to collect Prime Minister speech transcript URLs from `narendramodi.in` and save them into MongoDB.

The current production script is:

```text
extract_speech_url_range_playwright.py
```

Older scraper experiments and report/sheet helper scripts are intentionally not tracked in this GitHub repository because they were either not part of the working scrape flow or were one-off local utilities.

## What This Scraper Does

`extract_speech_url_range_playwright.py`:

- Opens `https://www.narendramodi.in/category/text-speeches` with Playwright/Chrome to establish a browser session.
- Calls the site search endpoint:

```text
https://www.narendramodi.in/speech/searchspeeche
```

- Searches speech transcript URLs over a configurable date range.
- Splits large ranges into smaller windows using `CHUNK_DAYS`.
- Handles pagination with `page=1`, `page=2`, etc.
- Filters results so only speeches inside the requested date range are saved.
- Writes local checkpoint JSON files.
- Inserts URL/date records into MongoDB.

MongoDB documents are inserted into the configured URL collection with this shape:

```json
{
  "url": "https://www.narendramodi.in/...",
  "published_date": "MongoDB datetime",
  "added_at": "MongoDB datetime",
  "status": "pending"
}
```

Important: this script collects transcript URLs and dates only. It does not extract speech text, descriptions, or YouTube links.

## Required Local Files

Create a local `.env` file in the project root. Do not commit it.

```text
MONGODB_URI=mongodb+srv://...
MONGODB_DB=pm_speeches
URLS_COLLECTION=en_urls
```

`MONGODB_URI` is required. The script will stop immediately if it is missing.

## Setup

Use Python 3.10+.

Install dependencies:

```powershell
pip install -r requirements.txt
```

Install Playwright browser support:

```powershell
playwright install chromium
```

The script launches Chrome through Playwright:

```python
p.chromium.launch(channel="chrome", headless=HEADLESS)
```

So Google Chrome should be installed on the machine.

## Running The Scraper

Run a date range by setting environment variables before the command.

Example, the range used to scrape the missing recent speeches:

```powershell
$env:FROM_DATE='2026-02-22'
$env:TO_DATE='2026-06-08'
$env:LANGUAGE='en'
python .\extract_speech_url_range_playwright.py
```

Full historical example:

```powershell
$env:FROM_DATE='2014-05-26'
$env:TO_DATE='2026-06-08'
$env:LANGUAGE='en'
python .\extract_speech_url_range_playwright.py
```

Optional settings:

```powershell
$env:HEADLESS='true'
$env:CHUNK_DAYS='90'
$env:KEYWORD=''
$env:URLS_COLLECTION='en_urls'
```

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `FROM_DATE` | `2014-01-01` | Start date in `YYYY-MM-DD` format |
| `TO_DATE` | Current UTC date | End date in `YYYY-MM-DD` format |
| `LANGUAGE` | `en` | Use `en`, `hi`, or `all` |
| `KEYWORD` | empty | Optional search keyword |
| `HEADLESS` | `true` | Browser headless mode |
| `CHUNK_DAYS` | `90` | Number of days per scrape window |
| `MONGODB_URI` | none | Required MongoDB connection string |
| `MONGODB_DB` | `test` | Mongo database name |
| `URLS_COLLECTION` | `en_urls` | Mongo collection for URL records |

## Output Files

The script writes these local files during/after a run:

```text
speech_urls_searchspeeche_checkpoint_sorted.json
speech_urls_searchspeeche_raw_sorted.json
```

These files are ignored by Git because they are generated scrape outputs.

## MongoDB Behavior

The script creates these indexes on the URL collection:

```text
url unique
published_date descending + url ascending
```

Before inserting, it checks which URLs already exist. Existing URLs are skipped, so rerunning an overlapping date range is safe.

Expected final log lines look like:

```text
[MAIN] Total in-range unique URLs collected: 105
[INSERT] candidates=105 existing_in_db=2 to_insert=103
[MAIN] Mongo before=3323 after=3426 inserted=103 dup_errors=0
[MAIN] DONE.
```

## Troubleshooting

If Playwright cannot launch Chrome, confirm Chrome is installed and try:

```powershell
playwright install chromium
```

If MongoDB fails with DNS or SRV errors, verify the host inside `MONGODB_URI`.

If the site blocks or returns a WAF page, the script retries each date window and cools down before restarting the browser session.

If a long historical run is needed, prefer smaller windows:

```powershell
$env:CHUNK_DAYS='30'
```

## Git Hygiene

Never commit:

- `.env`
- credentials or token JSON files
- generated scrape outputs
- local caches
- virtual environments

The `.gitignore` is set up to keep those out of Git.
