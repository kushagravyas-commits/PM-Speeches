from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================
# CONFIG
# =========================
STEP3_KEY_EVIDENCE_INPUT_JSON_PATH = os.getenv(
    "STEP3_KEY_EVIDENCE_INPUT_JSON_PATH",
    "outputs/step2_enriched_middle_class_2025-02-06_2026-02-16.json",
)
STEP3_KEY_EVIDENCE_OUTPUT_JSON_PATH = os.getenv(
    "STEP3_KEY_EVIDENCE_OUTPUT_JSON_PATH",
    "outputs/step3_key_evidence.json",
)

MAX_KEY_NUMBERS = int(os.getenv("STEP3_KEY_EVIDENCE_MAX_NUMBERS", "80"))
MAX_KEY_STATEMENTS = int(os.getenv("STEP3_KEY_EVIDENCE_MAX_STATEMENTS", "40"))

# Sentence window around a numeric match (0 = only containing sentence)
NUMBER_CONTEXT_WINDOW = int(os.getenv("STEP3_KEY_EVIDENCE_NUMBER_CONTEXT_WINDOW", "1"))

# If True, only analyze paragraphs that contain keyword (default True, matches your pipeline)
ONLY_KEYWORD_PARAGRAPHS = os.getenv("STEP3_KEY_EVIDENCE_ONLY_KEYWORD_PARAS", "true").lower() in {"1", "true", "yes"}


# =========================
# Regex patterns
# =========================
# Indian number words
INDIAN_UNIT_RE = re.compile(
    r"(?<![,])\b(\d[\d,]*(?:\.\d+)?)\s*(crore|crores|lakh|lakhs)\b",
    re.IGNORECASE
)

# Currency: ₹12,00,000 or Rs. 12 lakh or 12 lakh rupees
RUPEE_SYMBOL_RE = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
RUPEES_WORD_RE = re.compile(r"\b(?:rs\.?|rupees|inr)\s*([\d,]+(?:\.\d+)?)\b", re.IGNORECASE)

# Percent + Age + 4-digit year
PERCENT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*%\b")
YEARS_RE = re.compile(r"\b(\d{1,3})\s*(?:years?|yrs?)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")

# Generic number with commas (used only if we can label it safely)
PLAIN_NUMBER_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")

# Sentence splitting
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# Importance keywords
PROMISE_WORDS = [
    "will", "shall", "ensure", "guarantee", "commit", "committed", "resolve", "dedicated",
    "mission", "we have made", "we have extended", "we have launched", "we have brought",
]
POLICY_WORDS = [
    "scheme", "policy", "tax", "exemption", "budget", "ayushman", "free", "treatment", "benefit",
    "subsidy", "loan", "housing", "health", "insurance", "jobs", "employment",
]
IMPACT_WORDS = [
    "biggest", "historic", "transform", "unprecedented", "never before", "force multiplier", "golden period",
]


# =========================
# Helpers
# =========================
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_date_best_effort(s: str) -> str:
    """
    Return YYYY-MM-DD if possible, else raw.
    """
    if not s:
        return ""
    s = str(s).strip()
    # common: 2025-02-06T00:00:00 or 2025-02-06 00:00:00
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt) if "T" in fmt or " " in fmt else datetime.strptime(s[:10], fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue
    # try fromisoformat
    try:
        dt = datetime.fromisoformat(s.replace("Z", ""))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return s[:10]


def split_sentences(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in SENT_SPLIT_RE.split(text) if p.strip()]
    return parts


def normalize_number_str(num: str) -> str:
    return num.replace(",", "").strip()


def to_float(num: str) -> Optional[float]:
    try:
        return float(normalize_number_str(num))
    except Exception:
        return None


def indian_multiplier(unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("lakh"):
        return 100000
    if unit.startswith("crore"):
        return 10000000
    return 1


def classify_number(match_text: str, unit: str, sentence: str) -> Tuple[str, str]:
    """
    Returns (label, unit_text)
    """
    s = sentence.lower()

    if unit in {"crore", "crores", "lakh", "lakhs"}:
        # could be people or money
        if "rupee" in s or "rs" in s or "₹" in sentence or "tax" in s or "income" in s:
            return ("money_amount", unit)
        if "people" in s or "indians" in s or "citizens" in s or "families" in s:
            return ("people_count", unit)
        return ("count_with_indian_unit", unit)

    if unit == "rupees" or unit == "₹":
        return ("money_amount", "rupees")

    if unit == "%":
        return ("percentage", "%")

    if unit == "years":
        return ("age_or_years", "years")

    # year label
    if len(match_text) == 4 and match_text.isdigit() and (match_text.startswith("19") or match_text.startswith("20")):
        return ("year_reference", "year")

    return ("number", unit)


def build_context(sentences: List[str], idx: int, window: int) -> str:
    lo = max(0, idx - window)
    hi = min(len(sentences), idx + window + 1)
    return " ".join(sentences[lo:hi]).strip()


def contains_any(s: str, words: List[str]) -> int:
    s = s.lower()
    score = 0
    for w in words:
        if w in s:
            score += 1
    return score


def score_statement(sentence: str, ai: Optional[Dict[str, Any]]) -> int:
    """
    Heuristic importance score.
    """
    s = sentence.lower()
    score = 0

    score += 2 * contains_any(s, PROMISE_WORDS)
    score += 1 * contains_any(s, POLICY_WORDS)
    score += 1 * contains_any(s, IMPACT_WORDS)

    # Numbers add salience
    if (INDIAN_UNIT_RE.search(sentence) or RUPEE_SYMBOL_RE.search(sentence) or RUPEES_WORD_RE.search(sentence)
            or PERCENT_RE.search(sentence) or YEARS_RE.search(sentence)):
        score += 2

    # AI tags (if available)
    if isinstance(ai, dict) and ai.get("_done") is True:
        tone = str(ai.get("tone", "") or "").lower()
        primary = str(ai.get("primary_category", "") or "")
        if tone in {"promise", "reform", "credit-claim"}:
            score += 2
        if primary in {"Tax relief", "Budget", "Welfare schemes", "Healthcare", "Housing", "Employment"}:
            score += 2

    return score


def short_quote(sentence: str, max_len: int = 220) -> str:
    s = " ".join(sentence.split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


# =========================
# Extraction
# =========================
def extract_numbers_from_paragraph(
    paragraph_text: str,
    speech_meta: Dict[str, Any],
    paragraph_id: str
) -> List[Dict[str, Any]]:
    sents = split_sentences(paragraph_text)
    out: List[Dict[str, Any]] = []

    # index sentences for scanning
    for i, sent in enumerate(sents):
        # Indian units
        for m in INDIAN_UNIT_RE.finditer(sent):
            num_raw = m.group(1)
            unit = m.group(2).lower()
            label, unit_text = classify_number(m.group(0), unit, sent)

            num_val = to_float(num_raw)
            norm_val = None
            if num_val is not None:
                norm_val = num_val * indian_multiplier(unit)

            ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
            out.append({
                "speech_id": speech_meta["speech_id"],
                "paragraph_id": paragraph_id,
                "title": speech_meta["title"],
                "url": speech_meta["url"],
                "published_date": speech_meta["published_date"],
                "label": label,
                "value": num_raw,
                "unit": unit_text,
                "value_normalized": norm_val,
                "context": ctx,
            })

        # Currency symbol ₹
        for m in RUPEE_SYMBOL_RE.finditer(sent):
            num_raw = m.group(1)
            label, unit_text = classify_number("₹", "₹", sent)
            ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
            out.append({
                "speech_id": speech_meta["speech_id"],
                "paragraph_id": paragraph_id,
                "title": speech_meta["title"],
                "url": speech_meta["url"],
                "published_date": speech_meta["published_date"],
                "label": label,
                "value": num_raw,
                "unit": unit_text,
                "value_normalized": to_float(num_raw),
                "context": ctx,
            })

        # Rupees word
        for m in RUPEES_WORD_RE.finditer(sent):
            num_raw = m.group(1)
            label, unit_text = classify_number("rupees", "rupees", sent)
            ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
            out.append({
                "speech_id": speech_meta["speech_id"],
                "paragraph_id": paragraph_id,
                "title": speech_meta["title"],
                "url": speech_meta["url"],
                "published_date": speech_meta["published_date"],
                "label": label,
                "value": num_raw,
                "unit": unit_text,
                "value_normalized": to_float(num_raw),
                "context": ctx,
            })

        # Percent
        for m in PERCENT_RE.finditer(sent):
            num_raw = m.group(1)
            label, unit_text = classify_number(m.group(0), "%", sent)
            ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
            out.append({
                "speech_id": speech_meta["speech_id"],
                "paragraph_id": paragraph_id,
                "title": speech_meta["title"],
                "url": speech_meta["url"],
                "published_date": speech_meta["published_date"],
                "label": label,
                "value": num_raw,
                "unit": unit_text,
                "value_normalized": to_float(num_raw),
                "context": ctx,
            })

        # Years (age)
        for m in YEARS_RE.finditer(sent):
            num_raw = m.group(1)
            label, unit_text = classify_number(m.group(0), "years", sent)
            ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
            out.append({
                "speech_id": speech_meta["speech_id"],
                "paragraph_id": paragraph_id,
                "title": speech_meta["title"],
                "url": speech_meta["url"],
                "published_date": speech_meta["published_date"],
                "label": label,
                "value": num_raw,
                "unit": unit_text,
                "value_normalized": to_float(num_raw),
                "context": ctx,
            })

        # Year references (only if appears with "by" / "in" / "till" / "until")
        for m in YEAR_RE.finditer(sent):
            year_txt = m.group(1)
            low = sent.lower()
            if any(x in low for x in ["by ", "in ", "till ", "until ", "to "]):
                label, unit_text = classify_number(year_txt, "year", sent)
                ctx = build_context(sents, i, NUMBER_CONTEXT_WINDOW)
                out.append({
                    "speech_id": speech_meta["speech_id"],
                    "paragraph_id": paragraph_id,
                    "title": speech_meta["title"],
                    "url": speech_meta["url"],
                    "published_date": speech_meta["published_date"],
                    "label": label,
                    "value": year_txt,
                    "unit": unit_text,
                    "value_normalized": to_float(year_txt),
                    "context": ctx,
                })

    return out


def extract_key_statements_from_occurrence(
    speech_meta: Dict[str, Any],
    paragraph_id: str,
    occ: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    ctx = (occ.get("context") or {})
    ctx_sents = ctx.get("context_sentences") if isinstance(ctx, dict) else None
    if not isinstance(ctx_sents, list) or not ctx_sents:
        return None

    ai = occ.get("ai_analysis") if isinstance(occ.get("ai_analysis"), dict) else None

    # pick best sentence in context by score
    best_sent = None
    best_score = -1
    for s in ctx_sents:
        s = str(s).strip()
        if not s:
            continue
        sc = score_statement(s, ai)
        if sc > best_score:
            best_score = sc
            best_sent = s

    if not best_sent:
        return None

    return {
        "speech_id": speech_meta["speech_id"],
        "paragraph_id": paragraph_id,
        "occurrence_id": occ.get("occurrence_id", ""),
        "title": speech_meta["title"],
        "url": speech_meta["url"],
        "published_date": speech_meta["published_date"],
        "quote": short_quote(best_sent),
        "score": best_score,
        "reason_tags": {
            "primary_category": (ai.get("primary_category") if isinstance(ai, dict) else ""),
            "tone": (ai.get("tone") if isinstance(ai, dict) else ""),
        }
    }


def main() -> None:
    in_path = Path(STEP3_KEY_EVIDENCE_INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Input not found: {in_path.resolve()}")

    step2 = load_json(in_path)
    keyword = (step2.get("meta", {}) or {}).get("keyword") or ""
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    speeches = step2.get("speeches", []) or []
    if not isinstance(speeches, list):
        raise ValueError("Invalid step2 JSON: speeches must be a list")

    key_numbers: List[Dict[str, Any]] = []
    key_statements: List[Dict[str, Any]] = []

    seen_num = set()
    seen_quote = set()

    for sp in speeches:
        if not isinstance(sp, dict):
            continue

        speech_meta = {
            "speech_id": sp.get("speech_id", ""),
            "title": sp.get("title", ""),
            "url": sp.get("url", ""),
            "published_date": parse_date_best_effort(str(sp.get("published_date", "") or "")),
        }

        paragraphs = sp.get("paragraphs", []) or []
        if not isinstance(paragraphs, list):
            continue

        for p in paragraphs:
            if not isinstance(p, dict):
                continue

            paragraph_id = str(p.get("paragraph_id", "") or "")
            occs = p.get("occurrences", []) or []

            if ONLY_KEYWORD_PARAGRAPHS and (not isinstance(occs, list) or not occs):
                continue

            para_text = str(p.get("text", "") or "")

            # ---- Numbers ----
            nums = extract_numbers_from_paragraph(para_text, speech_meta, paragraph_id)
            for n in nums:
                k = (n["speech_id"], n["paragraph_id"], n.get("label",""), str(n.get("value","")), n.get("unit",""), n.get("context","")[:120])
                if k in seen_num:
                    continue
                seen_num.add(k)
                # importance score for sorting
                imp = 0
                if n["label"] in {"money_amount", "people_count", "percentage"}:
                    imp += 3
                if n.get("value_normalized") is not None:
                    imp += 1
                if any(w in (n.get("context","").lower()) for w in POLICY_WORDS):
                    imp += 1
                n["importance_score"] = imp
                key_numbers.append(n)

            # ---- Key statements from occurrences ----
            if isinstance(occs, list):
                for occ in occs:
                    if not isinstance(occ, dict):
                        continue
                    st = extract_key_statements_from_occurrence(speech_meta, paragraph_id, occ)
                    if not st:
                        continue
                    q = st.get("quote", "")
                    if not q:
                        continue
                    kq = (st["speech_id"], q)
                    if kq in seen_quote:
                        continue
                    seen_quote.add(kq)
                    key_statements.append(st)

    # Sort outputs
    key_numbers.sort(key=lambda x: (-int(x.get("importance_score", 0)), str(x.get("published_date","")), str(x.get("label",""))))
    key_statements.sort(key=lambda x: (-int(x.get("score", 0)), str(x.get("published_date",""))))

    key_numbers = key_numbers[:MAX_KEY_NUMBERS]
    key_statements = key_statements[:MAX_KEY_STATEMENTS]

    out = {
        "meta": {
            "agent": "step3_key_evidence_extractor",
            "generated_at": now_iso(),
            "input_file": str(in_path).replace("\\", "/"),
            "keyword": keyword,
            "search_window": search_window,
            "version": "step3_key_evidence_v1",
            "settings": {
                "max_key_numbers": MAX_KEY_NUMBERS,
                "max_key_statements": MAX_KEY_STATEMENTS,
                "number_context_window": NUMBER_CONTEXT_WINDOW,
                "only_keyword_paragraphs": ONLY_KEYWORD_PARAGRAPHS,
            },
        },
        "key_numbers": key_numbers,
        "key_statements": key_statements,
        "summary": {
            "key_numbers_count": len(key_numbers),
            "key_statements_count": len(key_statements),
        }
    }

    out_path = Path(STEP3_KEY_EVIDENCE_OUTPUT_JSON_PATH)
    atomic_write_json(out_path, out)
    print("[DONE] Key evidence written:", out_path.resolve())


if __name__ == "__main__":
    main()
