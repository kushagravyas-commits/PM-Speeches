import os
import re
import csv
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from typing import Tuple

START_DATE = "2022-06-01"   # inclusive (YYYY-MM-DD)
END_DATE   = "2024-06-01"   # inclusive (YYYY-MM-DD)

# Put words/phrases you want to search:
# 1) English only  -> fill ENGLISH_TERMS, keep HINDI_TERMS = []
# 2) Hindi only    -> fill HINDI_TERMS, keep ENGLISH_TERMS = []
# 3) Both          -> fill both lists
ENGLISH_TERMS = ["middle class"]          # ["reform", "deregulation", "middle class"] or []
HINDI_TERMS   = []           # ["विकसित भारत", "सुधार"] or []

TOP_N = 50  
OUTPUT_JSON = "word_search_results"
OUTPUT_CSV  = "word_search_results"
if ENGLISH_TERMS:
    for word in ENGLISH_TERMS:
        OUTPUT_JSON = OUTPUT_JSON + "_" + word
        OUTPUT_CSV  = OUTPUT_CSV + "_" + word
if HINDI_TERMS:
    for word in HINDI_TERMS:
        OUTPUT_JSON = OUTPUT_JSON + "_" + word
        OUTPUT_CSV  = OUTPUT_CSV + "_" + word
OUTPUT_JSON = OUTPUT_JSON + START_DATE + "_" + END_DATE + ".json"
OUTPUT_CSV  = OUTPUT_CSV + START_DATE + "_" + END_DATE + ".csv"

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI")
MONGO_DB = os.getenv("MONGODB_DB", "test")
COLL = os.getenv("SPEECHES_COLLECTION", "speeches")

if not MONGO_URI:
    raise RuntimeError("MONGODB_URI missing in .env")


def _parse_date_utc_naive(d: str) -> datetime:
    # Mongo dates in your DB are stored as naive UTC (from datetime.utcnow()).
    # We treat input date as UTC midnight.
    return datetime.strptime(d, "%Y-%m-%d")


def _is_phrase(term: str) -> bool:
    return bool(re.search(r"\s", term.strip()))


def _build_english_regex(term: str) -> Tuple[str, str]:
    t = term.strip()
    if not t:
        return r"\B", "i"  # Invalid/empty term; non-matching pattern

    # Split into tokens, removing any empty strings from extra spaces
    tokens = [tok for tok in t.split() if tok]
    if not tokens:
        return r"\B", "i"

    # Escape all tokens first
    escaped_tokens = [re.escape(tok) for tok in tokens]

    # Apply fuzzy plural handling to the *last* token only
    last_raw = tokens[-1]
    last_lower = last_raw.lower()
    last_escaped = escaped_tokens[-1]

    if last_lower.endswith("y") and len(last_lower) > 1 and last_lower[-2] not in "aeiou":
        # consonant + y → optional "y" or "ies" (e.g., baby → baby/babies)
        base_raw = last_raw[:-1]
        base_escaped = re.escape(base_raw)
        plural_part = r"(?:y|ies)?"
        last_escaped = base_escaped + plural_part
    else:
        # Otherwise optional "s" or "es" (covers most regular nouns with minimal false positives)
        plural_part = r"(?:s|es)?"
        last_escaped += plural_part

    # Replace the last escaped token with the fuzzy-plural version
    escaped_tokens[-1] = last_escaped

    # Build pattern
    if len(tokens) >= 2:
        # Flexible separator: hyphen, underscore, whitespace (zero or more → allows concatenation)
        sep = r"[-_\s]*"
        pattern = r"\b" + sep.join(escaped_tokens) + r"\b"
    else:
        # Single word: just the (optionally pluralized) token
        pattern = r"\b" + escaped_tokens[0] + r"\b"

    return pattern, "i"

def _build_hindi_regex(term: str) -> tuple[str, None]:
    """
    Hindi:
      - phrase => substring
      - single token => Devanagari-aware boundaries using PCRE2-safe \\x{....}
    """
    t = term.strip()
    esc = re.escape(t)

    # Phrase: substring match
    if _is_phrase(t):
        return esc, None

    # Single token: "word-ish" match using Devanagari boundaries
    # NOTE: Mongo PCRE2 does NOT allow \u0900, so we use \x{0900}
    dev = r"[\x{0900}-\x{097F}]"
    pattern = r"(?<!"+dev+r")" + esc + r"(?!"+dev+r")"
    return pattern, None



def _count_expr(regex: str, options: str | None):
    expr = {"input": "$content.full_text", "regex": regex}
    if options:
        expr["options"] = options
    return {"$size": {"$ifNull": [{"$regexFindAll": expr}, []]}}


def _sum_counts_for_terms(terms: list[str], lang: str):
    terms = [t.strip() for t in (terms or []) if t and t.strip()]
    if not terms:
        return {"$literal": 0}

    parts = []
    for t in terms:
        if lang == "en":
            rgx, opt = _build_english_regex(t)
        else:
            rgx, opt = _build_hindi_regex(t)
        parts.append(_count_expr(rgx, opt))

    if len(parts) == 1:
        return parts[0]
    return {"$add": parts}


def run_search():
    if not ENGLISH_TERMS and not HINDI_TERMS:
        raise ValueError("Please fill ENGLISH_TERMS and/or HINDI_TERMS in the CONFIG section.")

    start_dt = _parse_date_utc_naive(START_DATE)
    # END_DATE is inclusive → make end exclusive by adding 1 day
    end_dt = _parse_date_utc_naive(END_DATE) + timedelta(days=1)

    en_count_expr = _sum_counts_for_terms(ENGLISH_TERMS, "en")
    hi_count_expr = _sum_counts_for_terms(HINDI_TERMS, "hi")

    pipeline = [
        {
            "$match": {
                "published_date": {"$gte": start_dt, "$lt": end_dt},
                "content.full_text": {"$type": "string"},
            }
        },
        {
            "$project": {
                "url": 1,
                "title": 1,
                "published_date": 1,
                "en_count": en_count_expr,
                "hi_count": hi_count_expr,
                "_id": 1,
            }
        },
        {"$addFields": {"total_count": {"$add": ["$en_count", "$hi_count"]}}},
        {"$match": {"total_count": {"$gt": 0}}},
        {"$sort": {"total_count": -1, "published_date": -1}},
        {
            "$group": {
                "_id": None,
                "total_occurrences": {"$sum": "$total_count"},
                "total_en_occurrences": {"$sum": "$en_count"},
                "total_hi_occurrences": {"$sum": "$hi_count"},
                "speeches_matched": {"$sum": 1},
                "top_speeches": {
                    "$push": {
                        "title": "$title",
                        "url": "$url",
                        "published_date": "$published_date",
                        "total_count": "$total_count",
                        "en_count": "$en_count",
                        "hi_count": "$hi_count",
                        "speech_id": {"$toString": "$_id"},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "total_occurrences": 1,
                "total_en_occurrences": 1,
                "total_hi_occurrences": 1,
                "speeches_matched": 1,
                "top_speeches": "$top_speeches",

            }
        },
    ]

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    speeches = db[COLL]

    res = list(speeches.aggregate(pipeline, allowDiskUse=True))
    if not res:
        return {
            "query": {
                "start_date": START_DATE,
                "end_date_inclusive": END_DATE,
                "english_terms": ENGLISH_TERMS,
                "hindi_terms": HINDI_TERMS,
            },
            "total_occurrences": 0,
            "total_en_occurrences": 0,
            "total_hi_occurrences": 0,
            "speeches_matched": 0,
            "top_speeches": [],
        }

    out = res[0]
    out["query"] = {
        "start_date": START_DATE,
        "end_date_inclusive": END_DATE,
        "english_terms": ENGLISH_TERMS,
        "hindi_terms": HINDI_TERMS,
    }

    # Post-process to add static fields
    searched_word_str = ", ".join(ENGLISH_TERMS + HINDI_TERMS)
    for s in out.get("top_speeches", []):
        s["speech_id"] = s.get("speech_id")  # Already added in pipeline
        s["searched_word"] = searched_word_str
        s["start_date"] = START_DATE
        s["end_date"] = END_DATE

    return out


def save_outputs(result: dict):
    # JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str, indent=2)

    # CSV (top speeches)
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "speech_id", "searched_word", "start_date", "end_date", "published_date", "total_count", "en_count", "hi_count", "title", "url"])
        for i, s in enumerate(result.get("top_speeches", []), start=1):
            w.writerow([
                i,
                s.get("speech_id", ""),
                s.get("searched_word", ""),
                s.get("start_date", ""),
                s.get("end_date", ""),
                s.get("published_date"),
                s.get("total_count", 0),
                s.get("en_count", 0),
                s.get("hi_count", 0),
                s.get("title", ""),
                s.get("url", ""),
            ])


if __name__ == "__main__":
    result = run_search()

    print("=== RESULT ===")
    print("Date range:", START_DATE, "to", END_DATE, "(inclusive)")
    print("English terms:", ENGLISH_TERMS)
    print("Hindi terms:", HINDI_TERMS)
    print("Total occurrences:", result["total_occurrences"])
    print("  English occurrences:", result["total_en_occurrences"])
    print("  Hindi occurrences:", result["total_hi_occurrences"])
    print("Speeches matched:", result["speeches_matched"])
    print("Top speeches saved to:", OUTPUT_CSV)
    print("Full JSON saved to:", OUTPUT_JSON)

    save_outputs(result)
