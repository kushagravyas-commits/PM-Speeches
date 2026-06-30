from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from pymongo import MongoClient

# Optional .env loading (safe if python-dotenv not installed)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


# =========================
# CONFIG (edit as needed)
# =========================
INPUT_JSON_PATH = "word_search_results_middle class2025-02-06_2025-02-08.json"
OUTPUT_JSON_PATH = "outputs/middle_class_2025-02-06_2025-02-08_contexts_step1_v2_1.json"

MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGODB_DB", "speechdb")
SPEECHES_COLLECTION = os.getenv("SPEECHES_COLLECTION", "speeches")

WINDOW_SENTENCES = 4  # ±4 sentences

DEFAULT_PHRASE_REGEX = re.compile(r"\bmiddle\W{0,10}class(es)?\b", re.IGNORECASE)

SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
PARA_DELIM_RE = re.compile(r"\n\s*\n+")


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _clean_text(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\u00A0", " ")  # NBSP -> space
    while "\n\n\n" in t:
        t = t.replace("\n\n\n", "\n\n")
    return t.strip()


def build_phrase_regex(searched_word: str) -> re.Pattern:
    if not searched_word or not isinstance(searched_word, str):
        return DEFAULT_PHRASE_REGEX

    words = [w.strip() for w in searched_word.split() if w.strip()]
    if len(words) < 2:
        safe = re.escape(searched_word.strip())
        return re.compile(rf"\b{safe}\b", re.IGNORECASE)

    last = words[-1].lower()
    if last == "class":
        last_pat = r"class(es)?"
        words_pat = [re.escape(w) for w in words[:-1]] + [last_pat]
    else:
        words_pat = [re.escape(w) for w in words]

    pat = r"\b" + r"\W{0,10}".join(words_pat) + r"\b"
    return re.compile(pat, re.IGNORECASE)


def sentence_spans(text: str) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    start = 0
    for m in SENT_SPLIT_RE.finditer(text):
        end = m.start()
        chunk = text[start:end].strip()
        if chunk:
            spans.append((start, end, chunk))
        start = m.end()

    tail = text[start:].strip()
    if tail:
        spans.append((start, len(text), tail))
    return spans


def find_sentence_index(spans: List[Tuple[int, int, str]], pos: int) -> int:
    for i, (s, e, _) in enumerate(spans):
        if s <= pos < e:
            return i
    return 0


def extract_context(spans: List[Tuple[int, int, str]], sentence_idx: int, window: int) -> Dict[str, Any]:
    lo = max(0, sentence_idx - window)
    hi = min(len(spans), sentence_idx + window + 1)
    sentences = [spans[i][2] for i in range(lo, hi)]
    return {
        "sentence_index": sentence_idx,
        "context_sentence_range": [lo, hi - 1],
        "context_sentences": sentences,
        "context_window_text": " ".join(sentences).strip(),
    }


def paragraph_spans(text: str) -> List[Tuple[int, int, str]]:
    spans: List[Tuple[int, int, str]] = []
    start = 0
    for m in PARA_DELIM_RE.finditer(text):
        end = m.start()
        block = text[start:end]
        if block.strip():
            spans.append((start, end, block))
        start = m.end()

    tail = text[start:]
    if tail.strip():
        spans.append((start, len(text), tail))
    return spans


def find_paragraph_index(paras: List[Tuple[int, int, str]], pos: int) -> int:
    for i, (s, e, _) in enumerate(paras):
        if s <= pos < e:
            return i
    return 0


def normalize_paragraph_text(raw_block: str) -> str:
    lines = [ln.strip() for ln in raw_block.split("\n")]
    lines = [ln for ln in lines if ln]
    return " ".join(lines).strip()


def resolve_full_text(speech_doc: Dict[str, Any]) -> str:
    content = speech_doc.get("content")
    if isinstance(content, dict):
        ft = content.get("full_text")
        if isinstance(ft, str) and ft.strip():
            return _clean_text(ft)

    for key in ("full_text", "text", "content"):
        val = speech_doc.get(key)
        if isinstance(val, str) and val.strip():
            return _clean_text(val)

    segs = speech_doc.get("segments")
    if isinstance(segs, list) and segs:
        parts: List[str] = []
        for seg in segs:
            if isinstance(seg, dict):
                speaker = seg.get("speaker")
                txt = seg.get("text") or ""
                parts.append(f"{speaker}: {txt}".strip() if speaker else str(txt).strip())
            else:
                parts.append(str(seg).strip())
        return _clean_text("\n".join([p for p in parts if p]))

    return ""


def iso_or_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main() -> None:
    in_path = Path(INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {in_path.resolve()}")

    data = json.loads(in_path.read_text(encoding="utf-8"))
    top_speeches = data.get("top_speeches") or []
    if not isinstance(top_speeches, list) or not top_speeches:
        raise ValueError("Input JSON has no top_speeches list (or it's empty).")

    # Global keyword (store ONCE in meta)
    keyword = ""
    if isinstance(top_speeches[0], dict):
        keyword = (top_speeches[0].get("searched_word") or "").strip()
    if not keyword:
        q = data.get("query", {}) or {}
        terms = q.get("english_terms") or []
        if isinstance(terms, list) and terms:
            keyword = str(terms[0]).strip()
    if not keyword:
        keyword = "middle class"

    # Global window (store ONCE in meta)
    q = data.get("query", {}) or {}
    window_start = (q.get("start_date") or "").strip()
    window_end = (q.get("end_date_inclusive") or q.get("end_date") or "").strip()
    if not window_start and isinstance(top_speeches[0], dict):
        window_start = (top_speeches[0].get("start_date") or "").strip()
    if not window_end and isinstance(top_speeches[0], dict):
        window_end = (top_speeches[0].get("end_date") or "").strip()

    # URLs dedup
    urls: List[str] = []
    seen = set()
    expected_total_from_input = 0
    for item in top_speeches:
        url = (item or {}).get("url")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
        expected_total_from_input += int((item or {}).get("total_count") or 0)

    print(f"[INFO] URLs in JSON: {len(urls)}")

    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    col = db[SPEECHES_COLLECTION]

    mongo_docs = list(
        col.find(
            {"url": {"$in": urls}},
            {
                "_id": 1,
                "speech_id": 1,
                "url": 1,
                "title": 1,
                "published_date": 1,
                "speaker": 1,
                "speech_type": 1,
                "language": 1,
                "content.full_text": 1,
                "segments": 1,
            },
        )
    )
    by_url: Dict[str, Dict[str, Any]] = {d.get("url"): d for d in mongo_docs if d.get("url")}
    print(f"[INFO] Speeches fetched from Mongo: {len(by_url)}")

    output: Dict[str, Any] = {
        "meta": {
            "version": "step1_v2_1",
            "generated_at": _now_iso(),
            "keyword": keyword,  # searched_word stored once here
            "search_window": {
                "start_date": window_start,
                "end_date": window_end,
            },
            "window_sentences": WINDOW_SENTENCES,
            "matching": {
                "regex_used": r"\bmiddle\W{0,10}class(es)?\b",
                "case_insensitive": True,
            },
            "text_source": {
                "collection": SPEECHES_COLLECTION,
                "field_path": "content.full_text",
            },
            "split_rules": {
                "paragraph_split": "blank-line / double newline heuristic",
                "sentence_split": "punctuation + newline heuristic",
            },
        },
        "input_summary": {
            "total_occurrences_expected": data.get("total_occurrences", expected_total_from_input),
            "total_en_occurrences": data.get("total_en_occurrences"),
            "total_hi_occurrences": data.get("total_hi_occurrences"),
            "speeches_matched_expected": data.get("speeches_matched", len(top_speeches)),
            "query": data.get("query", {}),
        },
        "extraction_summary": {
            "speeches_in_input": len(top_speeches),
            "speeches_fetched_from_mongo": len(by_url),
            "speeches_processed": 0,
            "speeches_with_matches": 0,
            "total_occurrences_actual": 0,
            "overall_delta_actual_minus_expected": 0,
        },
        "speeches": [],
    }

    speeches_processed = 0
    speeches_with_matches = 0
    total_occurrences_actual = 0
    total_expected_sum = 0

    # Phrase regex (global — do NOT repeat searched_word per speech)
    phrase_re = build_phrase_regex(keyword)

    for item in top_speeches:
        url = (item or {}).get("url")
        if not url:
            continue

        speech_doc = by_url.get(url)
        expected = int((item or {}).get("total_count") or 0)
        total_expected_sum += expected

        if not speech_doc:
            output["speeches"].append(
                {
                    "speech_id": (item or {}).get("speech_id", ""),
                    "mongo_object_id": "",
                    "url": url,
                    "title": (item or {}).get("title", ""),
                    "published_date": (item or {}).get("published_date", ""),
                    "speaker": "",
                    "speech_type": "",
                    "language": [],
                    "expected": {"total_count": expected, "en_count": int((item or {}).get("en_count") or 0), "hi_count": int((item or {}).get("hi_count") or 0)},
                    "actual": {"total_occurrences": 0, "unique_paragraphs_with_keyword": 0, "delta_actual_minus_expected": -expected},
                    "error": "Speech not found in MongoDB for this URL",
                    "paragraphs": [],
                }
            )
            continue

        speeches_processed += 1

        full_text = resolve_full_text(speech_doc)

        mongo_object_id = str(speech_doc.get("_id", ""))
        title = speech_doc.get("title") or (item or {}).get("title", "") or ""
        published_date = iso_or_str(speech_doc.get("published_date")) or (item or {}).get("published_date", "") or ""
        speaker = speech_doc.get("speaker") or ""
        speech_type = speech_doc.get("speech_type") or ""
        language = speech_doc.get("language") if isinstance(speech_doc.get("language"), list) else []

        speech_id_for_ids = (item or {}).get("speech_id", "") or str(speech_doc.get("speech_id", "")) or mongo_object_id

        if not full_text:
            output["speeches"].append(
                {
                    "speech_id": speech_id_for_ids,
                    "mongo_object_id": mongo_object_id,
                    "url": url,
                    "title": title,
                    "published_date": published_date,
                    "speaker": speaker,
                    "speech_type": speech_type,
                    "language": language,
                    "expected": {"total_count": expected, "en_count": int((item or {}).get("en_count") or 0), "hi_count": int((item or {}).get("hi_count") or 0)},
                    "actual": {"total_occurrences": 0, "unique_paragraphs_with_keyword": 0, "delta_actual_minus_expected": -expected},
                    "note": "Speech text empty (content.full_text missing/empty).",
                    "paragraphs": [],
                }
            )
            continue

        sent_spans = sentence_spans(full_text)
        para_blocks = paragraph_spans(full_text)

        paragraph_records: List[Dict[str, Any]] = []
        for p_idx, (ps, pe, raw_block) in enumerate(para_blocks):
            paragraph_records.append(
                {
                    "paragraph_id": f"{speech_id_for_ids}:p{p_idx:04d}",
                    "paragraph_index": p_idx,
                    "char_start": ps,
                    "char_end": pe,
                    "text": normalize_paragraph_text(raw_block),
                    "occurrences": [],
                }
            )

        occ_no_in_speech = 0
        occ_no_in_para: List[int] = [0] * len(paragraph_records)

        for m in phrase_re.finditer(full_text):
            occ_no_in_speech += 1
            match_start, match_end = m.start(), m.end()
            match_text = full_text[match_start:match_end]

            p_idx = find_paragraph_index(para_blocks, match_start)
            if p_idx < 0 or p_idx >= len(paragraph_records):
                p_idx = 0

            occ_no_in_para[p_idx] += 1
            occ_no_paragraph = occ_no_in_para[p_idx]

            sent_idx = find_sentence_index(sent_spans, match_start)
            ctx = extract_context(sent_spans, sent_idx, WINDOW_SENTENCES)

            paragraph_records[p_idx]["occurrences"].append(
                {
                    "occurrence_id": f"{speech_id_for_ids}:p{p_idx:04d}:o{occ_no_paragraph:04d}",
                    "occurrence_no_in_speech": occ_no_in_speech,
                    "occurrence_no_in_paragraph": occ_no_paragraph,
                    "matched_text": match_text,
                    "match_start": match_start,
                    "match_end": match_end,
                    "context": ctx,
                }
            )

        paragraphs_with_hits = [p for p in paragraph_records if p["occurrences"]]
        actual_total = occ_no_in_speech
        unique_paras = len(paragraphs_with_hits)
        delta = actual_total - expected

        if actual_total > 0:
            speeches_with_matches += 1
        total_occurrences_actual += actual_total

        output["speeches"].append(
            {
                "speech_id": speech_id_for_ids,
                "mongo_object_id": mongo_object_id,
                "url": url,
                "title": title,
                "published_date": published_date,
                "speaker": speaker,
                "speech_type": speech_type,
                "language": language,
                "expected": {
                    "total_count": expected,
                    "en_count": int((item or {}).get("en_count") or 0),
                    "hi_count": int((item or {}).get("hi_count") or 0),
                },
                "actual": {
                    "total_occurrences": actual_total,
                    "unique_paragraphs_with_keyword": unique_paras,
                    "delta_actual_minus_expected": delta,
                },
                "paragraphs": paragraphs_with_hits,
            }
        )

    output["extraction_summary"]["speeches_processed"] = speeches_processed
    output["extraction_summary"]["speeches_with_matches"] = speeches_with_matches
    output["extraction_summary"]["total_occurrences_actual"] = total_occurrences_actual
    output["extraction_summary"]["overall_delta_actual_minus_expected"] = total_occurrences_actual - total_expected_sum

    out_path = Path(OUTPUT_JSON_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] Saved: {out_path.resolve()}")
    print(f"[DONE] Speeches processed: {speeches_processed}")
    print(f"[DONE] Total occurrences found: {total_occurrences_actual}")


if __name__ == "__main__":
    main()
