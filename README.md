# PM Speeches

Working scraper used to collect Prime Minister speech transcript URLs from narendramodi.in into MongoDB.

## Main Workflow

- `extract_speech_url_range_playwright.py` collects transcript URLs for a date range and stores them in MongoDB.

## Local Setup

Install dependencies:

```powershell
pip install -r requirements.txt
playwright install chromium
```

Create a local `.env` with MongoDB settings:

```text
MONGODB_URI=...
MONGODB_DB=pm_speeches
URLS_COLLECTION=en_urls
```

Credential files, OAuth tokens, caches, generated CSV/JSON data, and report outputs are intentionally ignored by Git.
