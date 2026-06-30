# PM Speeches

Utilities for collecting Prime Minister speech transcript URLs from narendramodi.in, extracting speech text/media metadata, and searching/counting words across stored speeches.

## Main Workflows

- `extract_speech_url_range_playwright.py` collects transcript URLs for a date range and stores them in MongoDB.
- `extract_speech_mongodb.py` visits transcript URLs and stores speech title, date, text, images, videos, and tweets in MongoDB.
- `word_search.py` searches stored speeches for a keyword and exports JSON/CSV results.
- `scheduler.py` contains the older Prefect automation for scheduled extraction runs.

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
