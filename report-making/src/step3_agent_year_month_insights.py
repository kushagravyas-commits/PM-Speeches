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
STEP3_YM_INPUT_JSON_PATH = os.getenv(
    "STEP3_YM_INPUT_JSON_PATH",
    "outputs/middle_class_2025-02-06_2025-02-08_contexts_step2_v2_ai.json",
)
STEP3_YM_OUTPUT_JSON_PATH = os.getenv(
    "STEP3_YM_OUTPUT_JSON_PATH",
    "outputs/step3_year_month_insights.json",
)

TOP_K_CATEGORIES = int(os.getenv("STEP3_YM_TOP_K_CATEGORIES", "6"))
TOP_K_BENEFICIARIES = int(os.getenv("STEP3_YM_TOP_K_BENEFICIARIES", "6"))
TOP_K_TONES = int(os.getenv("STEP3_YM_TOP_K_TONES", "5"))

TOP_K_TERMS = int(os.getenv("STEP3_YM_TOP_K_TERMS", "15"))
TOP_K_BIGRAMS = int(os.getenv("STEP3_YM_TOP_K_BIGRAMS", "12"))
TOP_K_TRIGRAMS = int(os.getenv("STEP3_YM_TOP_K_TRIGRAMS", "10"))

MIN_TOKEN_LEN = int(os.getenv("STEP3_YM_MIN_TOKEN_LEN", "2"))
INCLUDE_NUMBERS = os.getenv("STEP3_YM_INCLUDE_NUMBERS", "false").lower() in {"1", "true", "yes"}

EXTRA_STOPWORDS_ENV = os.getenv("STEP3_YM_EXTRA_STOPWORDS", "").strip()

CUSTOM_SPEECH_FILLERS: Set[str] = {
    "hon", "honble", "honourable", "sir", "madam", "mr", "mrs", "ms", "chairman", "speaker",
    "shri", "smt", "dr", "prof", "ji"
}

TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|[0-9]+")


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


def parse_date_best_effort(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def month_key(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    return f"{dt.year:04d}-{dt.month:02d}"


def year_key(dt: Optional[datetime]) -> str:
    if dt is None:
        return "unknown"
    return f"{dt.year:04d}"


def get_stopwords_english() -> Set[str]:
    try:
        sw = set(stopwords.words("english"))
    except LookupError:
        nltk.download("stopwords", quiet=True)
        sw = set(stopwords.words("english"))
    sw |= {w.lower() for w in CUSTOM_SPEECH_FILLERS}
    if EXTRA_STOPWORDS_ENV:
        sw |= {x.strip().lower() for x in EXTRA_STOPWORDS_ENV.split(",") if x.strip()}
    return sw


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    for m in TOKEN_RE.finditer(text):
        t = m.group(0).lower()
        if t.isdigit() and not INCLUDE_NUMBERS:
            continue
        if len(t) < MIN_TOKEN_LEN and not t.isdigit():
            continue
        out.append(t)
    return out


def build_keyword_exclude_tokens(keyword: str) -> Set[str]:
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
    return [t for t in tokens if t not in stopwords_set and t not in exclude]


def make_ngrams(tokens: List[str], n: int) -> List[str]:
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


def topk_counter(c: Counter, k: int, key_name: str = "label") -> List[Dict[str, Any]]:
    return [{key_name: t, "count": int(n)} for t, n in c.most_common(k)]


def compute_idf(df: Dict[str, int], n_docs: int) -> Dict[str, float]:
    return {term: (math.log((n_docs + 1.0) / (d + 1.0)) + 1.0) for term, d in df.items()}


def sum_tfidf_over_docs(doc_counts: List[Counter]) -> Tuple[Dict[str, float], Dict[str, int]]:
    """
    Given list of doc term counters, return (tfidf_sum, df).
    """
    df: Dict[str, int] = defaultdict(int)
    for c in doc_counts:
        for term in c.keys():
            df[term] += 1
    n_docs = len(doc_counts)
    idf = compute_idf(df, n_docs) if n_docs else {}
    tfidf_sum: Dict[str, float] = defaultdict(float)

    for c in doc_counts:
        total = sum(c.values()) or 1
        for term, cnt in c.items():
            tfidf_sum[term] += (cnt / total) * idf.get(term, 0.0)
    return tfidf_sum, df


def topk_scores(score_map: Dict[str, float], k: int, key_name: str) -> List[Dict[str, Any]]:
    items = sorted(score_map.items(), key=lambda x: (-x[1], x[0]))
    return [{key_name: t, "tfidf_sum": round(float(s), 8)} for t, s in items[:k]]


def highlights_from_group(
    top_cats: List[Dict[str, Any]],
    top_bens: List[Dict[str, Any]],
    top_terms: List[Dict[str, Any]],
    top_bigrams: List[Dict[str, Any]],
) -> List[str]:
    bullets = []

    if top_cats:
        cats = ", ".join([f"{x['label']} ({x['count']})" for x in top_cats[:3]])
        bullets.append(f"Dominant framing: {cats}.")
    if top_bens:
        bens = ", ".join([f"{x['label']} ({x['count']})" for x in top_bens[:3]])
        bullets.append(f"Beneficiary focus: {bens}.")
    if top_bigrams:
        ph = ", ".join([x["ngram"] for x in top_bigrams[:4] if x.get("ngram")])
        if ph:
            bullets.append(f"Recurring phrases: {ph}.")
    elif top_terms:
        terms = ", ".join([x["term"] for x in top_terms[:5] if x.get("term")])
        if terms:
            bullets.append(f"Salient terms: {terms}.")
    return bullets[:5]


# =========================
# Core computation
# =========================
def iter_paragraph_docs(step2: Dict[str, Any]) -> List[Dict[str, Any]]:
    docs = []
    speeches = step2.get("speeches", []) or []
    for sp in speeches:
        if not isinstance(sp, dict):
            continue
        sid = str(sp.get("speech_id", "") or "")
        dt = parse_date_best_effort(str(sp.get("published_date", "") or ""))
        yk = year_key(dt)
        mk = month_key(dt)

        paras = sp.get("paragraphs", []) or []
        if not isinstance(paras, list):
            continue
        for p in paras:
            if not isinstance(p, dict):
                continue
            occs = p.get("occurrences", []) or []
            if not isinstance(occs, list) or not occs:
                continue
            docs.append({
                "speech_id": sid,
                "year": yk,
                "month": mk,
                "paragraph_id": str(p.get("paragraph_id", "") or ""),
                "text": str(p.get("text", "") or ""),
                "occurrences": occs,
            })
    return docs


def count_group_tags(docs: List[Dict[str, Any]]) -> Tuple[Counter, Counter, Counter, int, int]:
    """
    Returns (category_counter, beneficiary_counter, tone_counter, mentions_count, speeches_count)
    Mentions counted as number of occurrences.
    """
    cat = Counter()
    ben = Counter()
    tone = Counter()
    mentions = 0
    speeches = set()

    for d in docs:
        speeches.add(d["speech_id"])
        occs = d["occurrences"]
        mentions += len(occs)
        for occ in occs:
            ai = occ.get("ai_analysis")
            if not isinstance(ai, dict) or ai.get("_done") is not True:
                continue
            pc = str(ai.get("primary_category", "") or "").strip()
            if pc:
                cat[pc] += 1
            b_list = ai.get("beneficiaries", [])
            if isinstance(b_list, list):
                for b in b_list:
                    b = str(b).strip()
                    if b:
                        ben[b] += 1
            t = str(ai.get("tone", "") or "").strip().lower()
            if t:
                tone[t] += 1

    return cat, ben, tone, mentions, len(speeches)


def compute_group_text_signals(docs: List[Dict[str, Any]], stopwords_set: Set[str], exclude_tokens: Set[str]) -> Dict[str, Any]:
    """
    TF-IDF over:
      - unigrams
      - bigrams
      - trigrams
    using paragraph docs as documents.
    """
    unigram_doc_counts: List[Counter] = []
    bigram_doc_counts: List[Counter] = []
    trigram_doc_counts: List[Counter] = []

    for d in docs:
        toks = filter_tokens(tokenize(d["text"]), stopwords_set, exclude_tokens)
        unigram_doc_counts.append(Counter(toks))
        bigram_doc_counts.append(Counter(make_ngrams(toks, 2)))
        trigram_doc_counts.append(Counter(make_ngrams(toks, 3)))

    uni_scores, uni_df = sum_tfidf_over_docs(unigram_doc_counts)
    bi_scores, bi_df = sum_tfidf_over_docs(bigram_doc_counts)
    tri_scores, tri_df = sum_tfidf_over_docs(trigram_doc_counts)

    uni_top = topk_scores(uni_scores, TOP_K_TERMS, "term")
    bi_top = topk_scores(bi_scores, TOP_K_BIGRAMS, "ngram")
    tri_top = topk_scores(tri_scores, TOP_K_TRIGRAMS, "ngram")

    return {
        "top_terms_tfidf": uni_top,
        "top_bigrams_tfidf": bi_top,
        "top_trigrams_tfidf": tri_top,
        "docs": len(docs),
    }


def build_group_insight(group_name: str, group_value: str, docs: List[Dict[str, Any]], stopwords_set: Set[str], exclude_tokens: Set[str]) -> Dict[str, Any]:
    cat, ben, tone, mentions, speeches_count = count_group_tags(docs)
    text_sig = compute_group_text_signals(docs, stopwords_set, exclude_tokens)

    top_cats = [{"label": k, "count": int(v)} for k, v in cat.most_common(TOP_K_CATEGORIES)]
    top_bens = [{"label": k, "count": int(v)} for k, v in ben.most_common(TOP_K_BENEFICIARIES)]
    top_tones = [{"label": k, "count": int(v)} for k, v in tone.most_common(TOP_K_TONES)]

    highlights = highlights_from_group(
        top_cats=top_cats,
        top_bens=top_bens,
        top_terms=text_sig["top_terms_tfidf"],
        top_bigrams=text_sig["top_bigrams_tfidf"],
    )

    return {
        group_name: group_value,
        "mentions": mentions,
        "speeches_count": speeches_count,
        "paragraph_docs": text_sig["docs"],
        "top_primary_categories": top_cats,
        "top_beneficiaries": top_bens,
        "top_tones": top_tones,
        "top_terms_tfidf": text_sig["top_terms_tfidf"],
        "top_bigrams_tfidf": text_sig["top_bigrams_tfidf"],
        "top_trigrams_tfidf": text_sig["top_trigrams_tfidf"],
        "highlights": highlights,
    }


def main() -> None:
    in_path = Path(STEP3_YM_INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Input not found: {in_path.resolve()}")

    step2 = load_json(in_path)
    keyword = (step2.get("meta", {}) or {}).get("keyword") or "keyword"
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    stopwords_set = get_stopwords_english()
    exclude_tokens = build_keyword_exclude_tokens(keyword)

    docs_all = iter_paragraph_docs(step2)

    # group docs by year and month
    by_year: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_month: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for d in docs_all:
        by_year[d["year"]].append(d)
        by_month[d["month"]].append(d)

    years = sorted([y for y in by_year.keys() if y != "unknown"])
    if "unknown" in by_year:
        years.append("unknown")

    months = sorted([m for m in by_month.keys() if m != "unknown"])
    if "unknown" in by_month:
        months.append("unknown")

    overall = build_group_insight("group", "overall", docs_all, stopwords_set, exclude_tokens)

    year_insights = [build_group_insight("year", y, by_year[y], stopwords_set, exclude_tokens) for y in years]
    month_insights = [build_group_insight("month", m, by_month[m], stopwords_set, exclude_tokens) for m in months]

    out = {
        "meta": {
            "agent": "step3_year_month_insights_agent",
            "generated_at": now_iso(),
            "input_file": str(in_path).replace("\\", "/"),
            "keyword": keyword,
            "search_window": search_window,
            "version": "step3_ym_v1",
            "settings": {
                "tfidf": True,
                "ngrams": [2, 3],
                "top_k_terms": TOP_K_TERMS,
                "top_k_bigrams": TOP_K_BIGRAMS,
                "top_k_trigrams": TOP_K_TRIGRAMS,
                "top_k_categories": TOP_K_CATEGORIES,
                "top_k_beneficiaries": TOP_K_BENEFICIARIES,
                "top_k_tones": TOP_K_TONES,
                "min_token_len": MIN_TOKEN_LEN,
                "include_numbers": INCLUDE_NUMBERS,
                "extra_stopwords_env": EXTRA_STOPWORDS_ENV,
            },
            "excluded_keyword_tokens": sorted(list(exclude_tokens)),
        },
        "overall": overall,
        "years": year_insights,
        "months": month_insights,
    }

    out_path = Path(STEP3_YM_OUTPUT_JSON_PATH)
    atomic_write_json(out_path, out)
    print("[DONE] Year/Month Insights written:", out_path.resolve())


if __name__ == "__main__":
    main()
