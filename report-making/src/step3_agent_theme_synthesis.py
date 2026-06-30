from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from openai import OpenAI


# =========================
# INPUTS / OUTPUTS
# =========================
STEP3_THEME_STEP2_JSON = os.getenv(
    "STEP3_THEME_STEP2_JSON",
    "outputs/step2_enriched_middle_class_2025-02-06_2026-02-16.json",
)
STEP3_THEME_DATA_ANALYSIS_JSON = os.getenv(
    "STEP3_THEME_DATA_ANALYSIS_JSON",
    "outputs/step3_data_analysis.json",
)
STEP3_THEME_OCCASION_JSON = os.getenv(
    "STEP3_THEME_OCCASION_JSON",
    "outputs/step3_occasion_classification.json",
)
STEP3_THEME_COOCC_JSON = os.getenv(
    "STEP3_THEME_COOCC_JSON",
    "outputs/step3_cooccurrence.json",
)

STEP3_THEME_OUTPUT_JSON = os.getenv(
    "STEP3_THEME_OUTPUT_JSON",
    "outputs/step3_theme_synthesis.json",
)

# =========================
# OPENROUTER
# =========================
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite").strip()

HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
X_TITLE = os.getenv("OPENROUTER_APP_TITLE", "").strip()

TEMPERATURE = float(os.getenv("STEP3_THEME_TEMPERATURE", "0.3"))
MAX_RETRIES = int(os.getenv("STEP3_THEME_MAX_RETRIES", "2"))
SLEEP_S = float(os.getenv("STEP3_THEME_SLEEP_S", "2"))

# =========================
# HOW MUCH CONTEXT TO FEED THE MODEL
# (Raised defaults to enable more themes)
# =========================
TOP_K_EXAMPLES = int(os.getenv("STEP3_THEME_TOP_K_EXAMPLES", "40"))
TOP_K_COOCC_TERMS = int(os.getenv("STEP3_THEME_TOP_K_COOCC_TERMS", "40"))
TOP_K_NGRAMS = int(os.getenv("STEP3_THEME_TOP_K_NGRAMS", "30"))
TOP_K_TAGS = int(os.getenv("STEP3_THEME_TOP_K_TAGS", "15"))

# =========================
# THEME COUNT CONTROLS (NEW)
# =========================
MIN_THEMES = int(os.getenv("STEP3_THEME_MIN_THEMES", "3"))
MAX_THEMES = int(os.getenv("STEP3_THEME_MAX_THEMES", "10"))
MIN_SUBTHEMES = int(os.getenv("STEP3_THEME_MIN_SUBTHEMES", "2"))
MAX_SUBTHEMES = int(os.getenv("STEP3_THEME_MAX_SUBTHEMES", "4"))

PROMPT_FILE_PATH = os.getenv("STEP3_THEME_PROMPT_FILE", "").strip()


# =========================
# PROMPT (UPDATED: MORE THEMES)
# NOTE: keep only {briefing_json} placeholder (we use .replace)
# =========================
DEFAULT_PROMPT_TEMPLATE = """You are writing a structured REPORT BLUEPRINT for a keyword analysis report.

Output MUST be ONE valid JSON object only (no markdown, no extra text).
Use ONLY the provided briefing JSON.

You MUST return this schema (exact keys, no missing):
{
  "report_title": string,
  "executive_summary": {
    "headline_bullets": [string],
    "paragraph": string
  },
  "thematic_insights": {
    "themes": [
      {
        "theme_title": string,
        "content": [string],
        "subthemes": [
          { "title": string, "content": [string] }
        ],
        "evidence_refs": [
          { "speech_id": string, "paragraph_id": string, "occurrence_id": string }
        ]
      }
    ]
  },
  "discussion": {
    "bullets": [string],
    "text": string
  },
  "conclusion": {
    "bullets": [string]
  },
  "audit_notes": [string]
}

Critical requirements:
- Generate BETWEEN __MIN_THEMES__ AND __MAX_THEMES__ themes (avoid duplicates).
- Each theme should have BETWEEN __MIN_SUBTHEMES__ AND __MAX_SUBTHEMES__ subthemes (unless evidence is genuinely insufficient).
- Theme titles must be specific and interpretable (not just a single word).
- Every theme MUST include evidence_refs pointing to real (speech_id, paragraph_id, occurrence_id) from the provided examples.
- Use co-occurrence terms + bigrams/trigrams + tag distributions + examples to justify themes.
- Do NOT invent policies/schemes not present in signals/examples.
- Keep discussion data-led (beneficiaries/policy framing supported by provided evidence).

Here is the briefing packet (JSON):
{briefing_json}
"""


# =========================
# Utils
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


def extract_json_from_text(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    lb = text.find("{")
    rb = text.rfind("}")
    if lb != -1 and rb != -1 and rb > lb:
        return json.loads(text[lb: rb + 1])
    raise ValueError("Model output did not contain valid JSON.")


def make_client() -> OpenAI:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")
    return OpenAI(base_url=BASE_URL, api_key=OPENROUTER_API_KEY)


def call_model(client: OpenAI, prompt: str) -> Dict[str, Any]:
    extra_headers: Dict[str, str] = {}
    if HTTP_REFERER:
        extra_headers["HTTP-Referer"] = HTTP_REFERER
    if X_TITLE:
        extra_headers["X-Title"] = X_TITLE

    completion = client.chat.completions.create(
        extra_headers=extra_headers,
        extra_body={},
        model=MODEL,
        temperature=TEMPERATURE,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    )

    content = completion.choices[0].message.content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        content_text = "\n".join(parts)
    else:
        content_text = str(content or "")

    return extract_json_from_text(content_text)


def load_prompt_template() -> str:
    if PROMPT_FILE_PATH:
        p = Path(PROMPT_FILE_PATH)
        if not p.exists():
            raise FileNotFoundError(f"STEP3_THEME_PROMPT_FILE not found: {p.resolve()}")
        return p.read_text(encoding="utf-8")
    return DEFAULT_PROMPT_TEMPLATE


# =========================
# Step2 traversal
# =========================
def iter_occurrences(step2: Dict[str, Any]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]:
    speeches = step2.get("speeches", [])
    if not isinstance(speeches, list):
        return
    for sp in speeches:
        if not isinstance(sp, dict):
            continue
        paras = sp.get("paragraphs", [])
        if not isinstance(paras, list):
            continue
        for p in paras:
            if not isinstance(p, dict):
                continue
            occs = p.get("occurrences", [])
            if not isinstance(occs, list):
                continue
            for occ in occs:
                if isinstance(occ, dict):
                    yield sp, p, occ


def count_tag_distributions(step2: Dict[str, Any]) -> Dict[str, Any]:
    cat: Dict[str, int] = {}
    ben: Dict[str, int] = {}
    tone_counts: Dict[str, int] = {}

    total = 0
    with_ai = 0

    for _sp, _p, occ in iter_occurrences(step2):
        total += 1
        ai = occ.get("ai_analysis")
        if not isinstance(ai, dict) or ai.get("_done") is not True:
            continue
        with_ai += 1

        primary = str(ai.get("primary_category", "") or "").strip()
        if primary:
            cat[primary] = cat.get(primary, 0) + 1

        b_list = ai.get("beneficiaries", [])
        if isinstance(b_list, list):
            for b in b_list:
                b = str(b).strip()
                if b:
                    ben[b] = ben.get(b, 0) + 1

        t = str(ai.get("tone", "") or "").strip().lower()
        if t:
            tone_counts[t] = tone_counts.get(t, 0) + 1

    def topk(d: Dict[str, int], k: int) -> List[Dict[str, Any]]:
        return [{"label": k2, "count": v} for k2, v in sorted(d.items(), key=lambda x: (-x[1], x[0]))[:k]]

    return {
        "total_occurrences": total,
        "occurrences_with_ai": with_ai,
        "primary_category_top": topk(cat, TOP_K_TAGS),
        "beneficiaries_top": topk(ben, TOP_K_TAGS),
        "tone_top": topk(tone_counts, TOP_K_TAGS),
    }


def select_examples(step2: Dict[str, Any], max_examples: int) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    fallback: List[Dict[str, Any]] = []

    for sp, p, occ in iter_occurrences(step2):
        ai = occ.get("ai_analysis")
        if not isinstance(ai, dict) or ai.get("_done") is not True:
            continue

        primary = str(ai.get("primary_category", "") or "Other").strip() or "Other"
        ex = {
            "speech_id": sp.get("speech_id", ""),
            "paragraph_id": p.get("paragraph_id", ""),
            "occurrence_id": occ.get("occurrence_id", ""),
            "title": sp.get("title", ""),
            "url": sp.get("url", ""),
            "published_date": sp.get("published_date", ""),
            "primary_category": primary,
            "beneficiaries": ai.get("beneficiaries", []),
            "tone": ai.get("tone", ""),
            "rewrite_1line": ai.get("rewrite_1line", ""),
            # extra context to help topic diversity
            "evidence_keywords": ai.get("evidence_keywords", []),
            "rewrite_2to3_sentences": ai.get("rewrite_2to3_sentences", ""),
        }
        buckets.setdefault(primary, []).append(ex)
        fallback.append(ex)

    cats = sorted(buckets.keys())
    out: List[Dict[str, Any]] = []
    i = 0
    while len(out) < max_examples and cats:
        c = cats[i % len(cats)]
        if buckets[c]:
            out.append(buckets[c].pop(0))
        i += 1
        cats = [c2 for c2 in cats if buckets.get(c2)]

    if len(out) < max_examples:
        seen_ids = {x["occurrence_id"] for x in out}
        for ex in fallback:
            if ex["occurrence_id"] in seen_ids:
                continue
            out.append(ex)
            if len(out) >= max_examples:
                break

    return out[:max_examples]


# =========================
# Briefing packet
# =========================
def build_briefing_packet(step2: Dict[str, Any], data_analysis: Dict[str, Any], occasion: Dict[str, Any], coocc: Dict[str, Any]) -> Dict[str, Any]:
    keyword = (step2.get("meta", {}) or {}).get("keyword") or ""
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    numbers = data_analysis.get("numbers", {}) or {}
    tables = data_analysis.get("tables", {}) or {}
    text = data_analysis.get("text", {}) or {}

    monthly = tables.get("monthly_mentions", []) or []
    top_speeches = tables.get("top_speeches_by_mentions", []) or []

    occ_dist = occasion.get("distribution", []) or []
    low_conf = occasion.get("low_confidence", []) or []

    # coocc: include both tfidf and freq for terms + ngrams (NEW)
    global_terms_tfidf = (((coocc.get("global", {}) or {}).get("unigrams", {}) or {}).get("top_by_tfidf", []) or [])[:TOP_K_COOCC_TERMS]
    global_terms_freq = (((coocc.get("global", {}) or {}).get("unigrams", {}) or {}).get("top_by_freq", []) or [])[:TOP_K_COOCC_TERMS]

    bigrams_tfidf = (((coocc.get("global", {}) or {}).get("bigrams", {}) or {}).get("top_by_tfidf", []) or [])[:TOP_K_NGRAMS]
    trigrams_tfidf = (((coocc.get("global", {}) or {}).get("trigrams", {}) or {}).get("top_by_tfidf", []) or [])[:TOP_K_NGRAMS]

    bigrams_freq = (((coocc.get("global", {}) or {}).get("bigrams", {}) or {}).get("top_by_freq", []) or [])[:TOP_K_NGRAMS]
    trigrams_freq = (((coocc.get("global", {}) or {}).get("trigrams", {}) or {}).get("top_by_freq", []) or [])[:TOP_K_NGRAMS]

    tag_dist = count_tag_distributions(step2)
    examples = select_examples(step2, TOP_K_EXAMPLES)

    return {
        "keyword": keyword,
        "search_window": search_window,

        "data_analysis": {
            "numbers": numbers,
            "monthly_mentions": monthly,
            "top_speeches": top_speeches,
            "narrative": text,
        },

        "occasion": {
            "distribution": occ_dist,
            "low_confidence": low_conf[:10],
        },

        "cooccurrence": {
            "top_terms_tfidf": global_terms_tfidf,
            "top_terms_freq": global_terms_freq,
            "top_bigrams_tfidf": bigrams_tfidf,
            "top_trigrams_tfidf": trigrams_tfidf,
            "top_bigrams_freq": bigrams_freq,
            "top_trigrams_freq": trigrams_freq,
            "qc": coocc.get("qc", {}),
        },

        "tag_distributions_from_step2_ai": tag_dist,
        "examples": examples,
    }


# =========================
# Main
# =========================
def main() -> None:
    step2_path = Path(STEP3_THEME_STEP2_JSON)
    da_path = Path(STEP3_THEME_DATA_ANALYSIS_JSON)
    occ_path = Path(STEP3_THEME_OCCASION_JSON)
    co_path = Path(STEP3_THEME_COOCC_JSON)
    out_path = Path(STEP3_THEME_OUTPUT_JSON)

    for p in (step2_path, da_path, occ_path, co_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing required input: {p.resolve()}")

    if str(out_path).strip() in {".", ""} or out_path.is_dir():
        raise ValueError(f"STEP3_THEME_OUTPUT_JSON must be a file path, got: {out_path}")

    step2 = load_json(step2_path)
    data_analysis = load_json(da_path)
    occasion = load_json(occ_path)
    coocc = load_json(co_path)

    briefing = build_briefing_packet(step2, data_analysis, occasion, coocc)
    briefing_json = json.dumps(briefing, ensure_ascii=False, indent=2)

    prompt_template = load_prompt_template()

    # Replace numeric placeholders (NEW)
    prompt = prompt_template
    prompt = prompt.replace("__MIN_THEMES__", str(MIN_THEMES))
    prompt = prompt.replace("__MAX_THEMES__", str(MAX_THEMES))
    prompt = prompt.replace("__MIN_SUBTHEMES__", str(MIN_SUBTHEMES))
    prompt = prompt.replace("__MAX_SUBTHEMES__", str(MAX_SUBTHEMES))

    if "{briefing_json}" not in prompt:
        raise ValueError("Prompt template must contain {briefing_json}")
    prompt = prompt.replace("{briefing_json}", briefing_json)

    client = make_client()

    last_err: Optional[str] = None
    out_obj: Optional[Dict[str, Any]] = None

    for _ in range(MAX_RETRIES + 1):
        try:
            out_obj = call_model(client, prompt)
            last_err = None
            break
        except Exception as e:
            last_err = str(e)
            time.sleep(SLEEP_S)

    if out_obj is None:
        raise RuntimeError(f"Theme synthesis failed: {last_err}")

    final = {
        "meta": {
            "agent": "step3_theme_topic_synthesis_agent",
            "generated_at": now_iso(),
            "provider": "openrouter",
            "base_url": BASE_URL,
            "model": MODEL,
            "inputs": {
                "step2": str(step2_path).replace("\\", "/"),
                "data_analysis": str(da_path).replace("\\", "/"),
                "occasion": str(occ_path).replace("\\", "/"),
                "cooccurrence": str(co_path).replace("\\", "/"),
            },
            "version": "step3_theme_agent_v2_more_themes",
        },
        "briefing_qc": {
            "examples_used": TOP_K_EXAMPLES,
            "coocc_terms_used": TOP_K_COOCC_TERMS,
            "ngrams_used": TOP_K_NGRAMS,
            "min_themes": MIN_THEMES,
            "max_themes": MAX_THEMES,
            "min_subthemes": MIN_SUBTHEMES,
            "max_subthemes": MAX_SUBTHEMES,
        },
        "content_plan": out_obj,
    }

    atomic_write_json(out_path, final)

    print("[DONE] Theme/Topic Synthesis Agent finished.")
    print("Output:", out_path.resolve())


if __name__ == "__main__":
    main()
