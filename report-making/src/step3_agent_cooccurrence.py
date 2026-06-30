# src/step3_agent_cooccurrence.py
"""
STEP 3 — Co-occurrence Agent (Deterministic, No AI)

Input : Step 2 output JSON (Step1 v2.1 + Step2 ai_analysis appended)
        Structure expected: speeches -> paragraphs (keyword paragraphs) -> occurrences

This agent uses ORIGINAL paragraph text (NOT AI rewrites) to compute:
1) Global co-occurring unigrams (freq + TF-IDF)
2) Global co-occurring bigrams/trigrams (freq + TF-IDF)
3) Per-speech top terms (freq + TF-IDF) + top ngrams (freq)
4) Per-paragraph top terms (freq + TF-IDF) + top ngrams (freq)
5) Visual-ready datasets + chart metadata stubs for the Visual Planner

Exclusions:
- Stopwords (embedded list)
- Keyword tokens (from meta.keyword) and simple plural forms (token+"s")

Output: a single JSON file with all computed signals + chart seeds.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import nltk
from nltk.corpus import stopwords


# =========================
# CONFIG
# =========================
STEP3_COOCC_INPUT_JSON_PATH = os.getenv(
    "STEP3_COOCC_INPUT_JSON_PATH",
    "outputs/middle_class_2025-02-06_2025-02-08_contexts_step2_v2_ai.json",
)
STEP3_COOCC_OUTPUT_JSON_PATH = os.getenv(
    "STEP3_COOCC_OUTPUT_JSON_PATH",
    "outputs/step3_cooccurrence.json",
)

# Top-K controls
TOP_K_GLOBAL = int(os.getenv("STEP3_COOCC_TOP_K_GLOBAL", "40"))
TOP_K_SPEECH = int(os.getenv("STEP3_COOCC_TOP_K_SPEECH", "25"))
TOP_K_PARAGRAPH = int(os.getenv("STEP3_COOCC_TOP_K_PARAGRAPH", "12"))
TOP_K_NGRAM_GLOBAL = int(os.getenv("STEP3_COOCC_TOP_K_NGRAM_GLOBAL", "40"))
TOP_K_NGRAM_SPEECH = int(os.getenv("STEP3_COOCC_TOP_K_NGRAM_SPEECH", "20"))
TOP_K_NGRAM_PARAGRAPH = int(os.getenv("STEP3_COOCC_TOP_K_NGRAM_PARAGRAPH", "8"))

# Token rules
MIN_TOKEN_LEN = int(os.getenv("STEP3_COOCC_MIN_TOKEN_LEN", "2"))
INCLUDE_NUMBERS = os.getenv("STEP3_COOCC_INCLUDE_NUMBERS", "false").lower() in {"1", "true", "yes"}

# TF-IDF + n-grams
ENABLE_TFIDF = True
ENABLE_NGRAMS = True
NGRAMS = (2, 3)  # bigrams + trigrams

# Optional: add extra stopwords via env (comma-separated)
# Example: STEP3_COOCC_EXTRA_STOPWORDS=government,country,people
EXTRA_STOPWORDS_ENV = os.getenv("STEP3_COOCC_EXTRA_STOPWORDS", "").strip()


# =========================
# Custom “speech filler” stopwords (added on top of NLTK)
# =========================
CUSTOM_SPEECH_FILLERS: Set[str] = {
    "hon", "honble", "honourable", "sir", "madam", "mr", "mrs", "ms", "chairman", "speaker",
    "shri", "smt", "dr", "prof",
    "ji", "hon’ble", "honble", "honourable",
}


# =========================
# Tokenization
# =========================
TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|[0-9]+")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def get_stopwords_english() -> Set[str]:
    """
    Uses NLTK stopwords and auto-downloads if missing.
    Adds custom speech fillers + optional env extra stopwords.
    """
    try:
        sw = set(stopwords.words("english"))
    except LookupError:
        # Auto-download stopwords if not present
        try:
            nltk.download("stopwords", quiet=True)
            sw = set(stopwords.words("english"))
        except Exception:
            # Last-resort fallback: minimal set to avoid crash
            sw = {"the", "and", "to", "of", "in", "a", "is", "for", "on", "with", "as", "by"}

    # Add custom fillers
    sw |= {w.lower() for w in CUSTOM_SPEECH_FILLERS}

    # Add env extras
    if EXTRA_STOPWORDS_ENV:
        extras = [x.strip().lower() for x in EXTRA_STOPWORDS_ENV.split(",") if x.strip()]
        sw |= set(extras)

    return sw


def tokenize(text: str) -> List[str]:
    """
    Lowercase tokenization:
    - words (letters + optional apostrophe)
    - numbers (optional via INCLUDE_NUMBERS)
    """
    if not text:
        return []
    tokens: List[str] = []
    for m in TOKEN_RE.finditer(text):
        t = m.group(0).lower()
        if t.isdigit() and not INCLUDE_NUMBERS:
            continue
        if len(t) < MIN_TOKEN_LEN and not t.isdigit():
            continue
        tokens.append(t)
    return tokens


def build_keyword_exclude_tokens(keyword: str) -> Set[str]:
    """
    Exclude only the search keyword tokens (per your instruction),
    plus simple plural forms (token+"s") as a practical normalization.
    Example: "middle class" -> {"middle","class","classes"}
    """
    toks = tokenize(keyword)
    ex: Set[str] = set()
    for t in toks:
        ex.add(t)
        if not t.endswith("s"):
            ex.add(t + "s")
    if "class" in ex:
        ex.add("classes")
    return ex


def filter_tokens(tokens: List[str], stopwords_set: Set[str], exclude: Set[str]) -> List[str]:
    out = []
    for t in tokens:
        if t in stopwords_set:
            continue
        if t in exclude:
            continue
        out.append(t)
    return out


def make_ngrams(tokens: List[str], n: int) -> List[str]:
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


# =========================
# TF-IDF helpers
# =========================
def compute_idf(df: Dict[str, int], n_docs: int) -> Dict[str, float]:
    # Smooth IDF: log((N+1)/(df+1)) + 1
    return {term: (math.log((n_docs + 1.0) / (d + 1.0)) + 1.0) for term, d in df.items()}


def doc_tfidf_scores(counts: Counter, idf: Dict[str, float]) -> Dict[str, float]:
    total = sum(counts.values()) or 1
    return {term: (c / total) * idf.get(term, 0.0) for term, c in counts.items()}


def topk_from_counter(c: Counter, k: int) -> List[Dict[str, Any]]:
    return [{"term": t, "count": int(n)} for t, n in c.most_common(k)]


def topk_from_scores(scores: Dict[str, float], k: int) -> List[Dict[str, Any]]:
    items = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [{"term": t, "score": round(float(s), 8)} for t, s in items[:k]]


# =========================
# Core agent logic
# =========================
def count_mentions_in_speech(speech: Dict[str, Any]) -> int:
    paragraphs = speech.get("paragraphs", [])
    if not isinstance(paragraphs, list):
        return 0
    total = 0
    for p in paragraphs:
        occs = p.get("occurrences", [])
        if isinstance(occs, list):
            total += len(occs)
    return total


def iter_documents(step2: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    speeches = step2.get("speeches", [])
    if not isinstance(speeches, list):
        return [], {}

    speech_meta: Dict[str, Dict[str, Any]] = {}
    docs: List[Dict[str, Any]] = []

    for sp in speeches:
        if not isinstance(sp, dict):
            continue
        speech_id = str(sp.get("speech_id", "") or "")
        url = str(sp.get("url", "") or "")
        title = str(sp.get("title", "") or "")
        published_date = str(sp.get("published_date", "") or "")
        speaker = str(sp.get("speaker", "") or "")
        speech_type = str(sp.get("speech_type", "") or "")

        paragraphs = sp.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            paragraphs = []

        mentions = count_mentions_in_speech(sp)
        speech_meta[speech_id] = {
            "speech_id": speech_id,
            "url": url,
            "title": title,
            "published_date": published_date,
            "speaker": speaker,
            "speech_type": speech_type,
            "mentions": mentions,
            "paragraphs_with_keyword": len(paragraphs),
        }

        for p in paragraphs:
            if not isinstance(p, dict):
                continue
            occs = p.get("occurrences", [])
            if not isinstance(occs, list) or len(occs) == 0:
                continue

            paragraph_id = str(p.get("paragraph_id", "") or "")
            paragraph_index = int(p.get("paragraph_index", 0) or 0)
            char_start = int(p.get("char_start", 0) or 0)
            char_end = int(p.get("char_end", 0) or 0)
            text = str(p.get("text", "") or "")

            docs.append(
                {
                    "doc_id": paragraph_id or f"{speech_id}:p{paragraph_index:04d}",
                    "speech_id": speech_id,
                    "paragraph_id": paragraph_id or f"{speech_id}:p{paragraph_index:04d}",
                    "paragraph_index": paragraph_index,
                    "char_start": char_start,
                    "char_end": char_end,
                    "text": text,
                }
            )

    return docs, speech_meta


def build_outputs(step2: Dict[str, Any]) -> Dict[str, Any]:
    keyword = (step2.get("meta", {}) or {}).get("keyword") or ""
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    stopwords_set = get_stopwords_english()
    exclude_tokens = build_keyword_exclude_tokens(keyword)

    docs, speech_meta = iter_documents(step2)
    n_docs = len(docs)
    speeches_count = len(speech_meta)

    removed_stopwords = 0
    removed_keyword_tokens = 0
    kept_tokens_total = 0

    for d in docs:
        toks_all = tokenize(d["text"])
        toks_filtered = []
        for t in toks_all:
            if t in stopwords_set:
                removed_stopwords += 1
                continue
            if t in exclude_tokens:
                removed_keyword_tokens += 1
                continue
            toks_filtered.append(t)
        kept_tokens_total += len(toks_filtered)
        d["tokens"] = toks_filtered
        d["counts"] = Counter(toks_filtered)

    # Global unigram stats
    term_tf = Counter()
    term_df: Dict[str, int] = defaultdict(int)
    term_speech_set: Dict[str, Set[str]] = defaultdict(set)

    for d in docs:
        counts: Counter = d["counts"]
        term_tf.update(counts)
        for term in counts.keys():
            term_df[term] += 1
            term_speech_set[term].add(d["speech_id"])

    term_speech_df = {t: len(sids) for t, sids in term_speech_set.items()}

    # TF-IDF unigrams
    idf = compute_idf(term_df, n_docs) if ENABLE_TFIDF else {}
    global_tfidf_sum: Dict[str, float] = defaultdict(float)

    # Per paragraph outputs
    per_paragraph: List[Dict[str, Any]] = []
    for d in docs:
        counts: Counter = d["counts"]
        para_out: Dict[str, Any] = {
            "paragraph_id": d["paragraph_id"],
            "speech_id": d["speech_id"],
            "paragraph_index": d["paragraph_index"],
            "char_start": d["char_start"],
            "char_end": d["char_end"],
            "top_terms_freq": topk_from_counter(counts, TOP_K_PARAGRAPH),
        }

        if ENABLE_TFIDF:
            scores = doc_tfidf_scores(counts, idf)
            for term, sc in scores.items():
                global_tfidf_sum[term] += sc
            para_out["top_terms_tfidf"] = topk_from_scores(scores, TOP_K_PARAGRAPH)

        if ENABLE_NGRAMS:
            toks = d["tokens"]
            bi = Counter(make_ngrams(toks, 2))
            tri = Counter(make_ngrams(toks, 3))
            para_out["top_bigrams_freq"] = [{"ngram": k, "count": int(v)} for k, v in bi.most_common(TOP_K_NGRAM_PARAGRAPH)]
            para_out["top_trigrams_freq"] = [{"ngram": k, "count": int(v)} for k, v in tri.most_common(TOP_K_NGRAM_PARAGRAPH)]

        per_paragraph.append(para_out)

    global_top_freq = [
        {
            "term": term,
            "count": int(cnt),
            "paragraph_df": int(term_df.get(term, 0)),
            "speech_df": int(term_speech_df.get(term, 0)),
        }
        for term, cnt in term_tf.most_common(TOP_K_GLOBAL)
    ]

    global_top_tfidf: List[Dict[str, Any]] = []
    if ENABLE_TFIDF:
        for term, score in sorted(global_tfidf_sum.items(), key=lambda x: (-x[1], x[0]))[:TOP_K_GLOBAL]:
            global_top_tfidf.append(
                {
                    "term": term,
                    "tfidf_sum": round(float(score), 8),
                    "paragraph_df": int(term_df.get(term, 0)),
                    "speech_df": int(term_speech_df.get(term, 0)),
                }
            )

    # Speech-level aggregations
    speech_term_counts: Dict[str, Counter] = defaultdict(Counter)
    for d in docs:
        speech_term_counts[d["speech_id"]].update(d["counts"])

    speech_df_term: Dict[str, int] = {t: len(sids) for t, sids in term_speech_set.items()}
    idf_speech = compute_idf(speech_df_term, speeches_count) if ENABLE_TFIDF else {}

    per_speech: List[Dict[str, Any]] = []
    for sid, meta in speech_meta.items():
        c = speech_term_counts.get(sid, Counter())
        speech_out = {**meta, "top_terms_freq": topk_from_counter(c, TOP_K_SPEECH)}
        if ENABLE_TFIDF:
            speech_out["top_terms_tfidf"] = topk_from_scores(doc_tfidf_scores(c, idf_speech), TOP_K_SPEECH)

        if ENABLE_NGRAMS:
            bi = Counter()
            tri = Counter()
            for d in docs:
                if d["speech_id"] != sid:
                    continue
                toks = d["tokens"]
                bi.update(make_ngrams(toks, 2))
                tri.update(make_ngrams(toks, 3))
            speech_out["top_bigrams_freq"] = [{"ngram": k, "count": int(v)} for k, v in bi.most_common(TOP_K_NGRAM_SPEECH)]
            speech_out["top_trigrams_freq"] = [{"ngram": k, "count": int(v)} for k, v in tri.most_common(TOP_K_NGRAM_SPEECH)]

        per_speech.append(speech_out)

    # Global n-grams (freq + TF-IDF)
    global_bi_tf = Counter()
    global_tri_tf = Counter()
    bi_df: Dict[str, int] = defaultdict(int)
    tri_df: Dict[str, int] = defaultdict(int)

    bi_doc_counts: List[Counter] = []
    tri_doc_counts: List[Counter] = []

    if ENABLE_NGRAMS:
        for d in docs:
            toks = d["tokens"]
            bi_c = Counter(make_ngrams(toks, 2))
            tri_c = Counter(make_ngrams(toks, 3))
            bi_doc_counts.append(bi_c)
            tri_doc_counts.append(tri_c)

            global_bi_tf.update(bi_c)
            global_tri_tf.update(tri_c)

            for bg in bi_c.keys():
                bi_df[bg] += 1
            for tg in tri_c.keys():
                tri_df[tg] += 1

        global_bi_top_freq = [{"ngram": k, "count": int(v), "paragraph_df": int(bi_df.get(k, 0))} for k, v in global_bi_tf.most_common(TOP_K_NGRAM_GLOBAL)]
        global_tri_top_freq = [{"ngram": k, "count": int(v), "paragraph_df": int(tri_df.get(k, 0))} for k, v in global_tri_tf.most_common(TOP_K_NGRAM_GLOBAL)]

        global_bi_top_tfidf: List[Dict[str, Any]] = []
        global_tri_top_tfidf: List[Dict[str, Any]] = []

        if ENABLE_TFIDF and n_docs > 0:
            bi_idf = compute_idf(bi_df, n_docs)
            tri_idf = compute_idf(tri_df, n_docs)

            bi_tfidf_sum: Dict[str, float] = defaultdict(float)
            tri_tfidf_sum: Dict[str, float] = defaultdict(float)

            for bi_c in bi_doc_counts:
                scores = doc_tfidf_scores(bi_c, bi_idf)
                for k, sc in scores.items():
                    bi_tfidf_sum[k] += sc

            for tri_c in tri_doc_counts:
                scores = doc_tfidf_scores(tri_c, tri_idf)
                for k, sc in scores.items():
                    tri_tfidf_sum[k] += sc

            for k, sc in sorted(bi_tfidf_sum.items(), key=lambda x: (-x[1], x[0]))[:TOP_K_NGRAM_GLOBAL]:
                global_bi_top_tfidf.append({"ngram": k, "tfidf_sum": round(float(sc), 8), "paragraph_df": int(bi_df.get(k, 0))})
            for k, sc in sorted(tri_tfidf_sum.items(), key=lambda x: (-x[1], x[0]))[:TOP_K_NGRAM_GLOBAL]:
                global_tri_top_tfidf.append({"ngram": k, "tfidf_sum": round(float(sc), 8), "paragraph_df": int(tri_df.get(k, 0))})
    else:
        global_bi_top_freq = []
        global_tri_top_freq = []
        global_bi_top_tfidf = []
        global_tri_top_tfidf = []

    visuals_seed = {
        "datasets": {
            "global_top_terms_freq": global_top_freq,
            "global_top_terms_tfidf": global_top_tfidf,
            "global_top_bigrams_freq": global_bi_top_freq,
            "global_top_bigrams_tfidf": global_bi_top_tfidf,
            "global_top_trigrams_freq": global_tri_top_freq,
            "global_top_trigrams_tfidf": global_tri_top_tfidf,
        },
        "chart_suggestions": [
            {
                "graph_id": "cooccurrence_unigrams_tfidf",
                "title": "Top Co-occurring Terms (TF-IDF)",
                "recommended_chart": "horizontal_bar",
                "data_ref": "datasets.global_top_terms_tfidf",
                "x_field": "tfidf_sum",
                "y_field": "term",
                "top_n": TOP_K_GLOBAL,
                "filename_suggestion": "cooccurrence_terms_tfidf.png",
            },
            {
                "graph_id": "cooccurrence_unigrams_freq",
                "title": "Top Co-occurring Terms (Frequency)",
                "recommended_chart": "horizontal_bar",
                "data_ref": "datasets.global_top_terms_freq",
                "x_field": "count",
                "y_field": "term",
                "top_n": TOP_K_GLOBAL,
                "filename_suggestion": "cooccurrence_terms_freq.png",
            },
            {
                "graph_id": "cooccurrence_bigrams_tfidf",
                "title": "Top Co-occurring Bigrams (TF-IDF)",
                "recommended_chart": "horizontal_bar",
                "data_ref": "datasets.global_top_bigrams_tfidf",
                "x_field": "tfidf_sum",
                "y_field": "ngram",
                "top_n": TOP_K_NGRAM_GLOBAL,
                "filename_suggestion": "cooccurrence_bigrams_tfidf.png",
            },
            {
                "graph_id": "cooccurrence_trigrams_tfidf",
                "title": "Top Co-occurring Trigrams (TF-IDF)",
                "recommended_chart": "horizontal_bar",
                "data_ref": "datasets.global_top_trigrams_tfidf",
                "x_field": "tfidf_sum",
                "y_field": "ngram",
                "top_n": TOP_K_NGRAM_GLOBAL,
                "filename_suggestion": "cooccurrence_trigrams_tfidf.png",
            },
        ],
    }

    qc = {
        "paragraph_docs_analyzed": n_docs,
        "speeches_analyzed": speeches_count,
        "keyword": keyword,
        "excluded_keyword_tokens": sorted(list(exclude_tokens)),
        "stopwords_source": "nltk.corpus.stopwords (english) + custom fillers + env extras",
        "stopwords_count": len(stopwords_set),
        "removed_stopwords_tokens": removed_stopwords,
        "removed_keyword_tokens": removed_keyword_tokens,
        "kept_tokens_total": kept_tokens_total,
        "unique_terms_kept": len(term_tf),
        "extra_stopwords_env": EXTRA_STOPWORDS_ENV,
    }

    out = {
        "meta": {
            "agent": "step3_cooccurrence_agent",
            "generated_at": now_iso(),
            "input_file": str(Path(STEP3_COOCC_INPUT_JSON_PATH)).replace("\\", "/"),
            "version": "step3_cooccurrence_v2_nltk_stopwords",
            "keyword": keyword,
            "search_window": search_window,
            "settings": {
                "enable_tfidf": ENABLE_TFIDF,
                "enable_ngrams": ENABLE_NGRAMS,
                "ngrams": list(NGRAMS),
                "min_token_len": MIN_TOKEN_LEN,
                "include_numbers": INCLUDE_NUMBERS,
                "top_k_global": TOP_K_GLOBAL,
                "top_k_speech": TOP_K_SPEECH,
                "top_k_paragraph": TOP_K_PARAGRAPH,
            },
        },
        "qc": qc,
        "global": {
            "unigrams": {"top_by_freq": global_top_freq, "top_by_tfidf": global_top_tfidf},
            "bigrams": {"top_by_freq": global_bi_top_freq, "top_by_tfidf": global_bi_top_tfidf},
            "trigrams": {"top_by_freq": global_tri_top_freq, "top_by_tfidf": global_tri_top_tfidf},
        },
        "per_speech": per_speech,
        "per_paragraph": per_paragraph,
        "visuals_seed": visuals_seed,
    }
    return out


def main() -> None:
    in_path = Path(STEP3_COOCC_INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {in_path.resolve()}")

    step2 = load_json(in_path)
    out = build_outputs(step2)

    out_path = Path(STEP3_COOCC_OUTPUT_JSON_PATH)
    if str(out_path).strip() in {".", ""} or out_path.is_dir():
        raise ValueError(f"STEP3_COOCC_OUTPUT_JSON_PATH must be a file path, got: {out_path}")

    atomic_write_json(out_path, out)

    print("[DONE] Co-occurrence Agent finished.")
    print("Output:", out_path.resolve())
    print("Docs analyzed:", out["qc"]["paragraph_docs_analyzed"])
    print("Unique terms kept:", out["qc"]["unique_terms_kept"])
    print("Stopwords count:", out["qc"]["stopwords_count"])


if __name__ == "__main__":
    main()
