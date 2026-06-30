from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from openai import OpenAI


# =========================
# CONFIG
# =========================
STEP2_INPUT_JSON_PATH = "outputs/middle_class_2025-02-06_2025-02-08_contexts_step1_v2_1.json"
STEP2_OUTPUT_JSON_PATH = "outputs/middle_class_2025-02-06_2025-02-08_contexts_step2_v2_ai.json"

RESUME = os.getenv("STEP2_RESUME", "true").lower() in {"1", "true", "yes"}
SAVE_EVERY_N = int(os.getenv("STEP2_SAVE_EVERY_N", "10"))
SLEEP_S = float(os.getenv("STEP2_SLEEP_S", "0.2"))
MAX_RETRIES = int(os.getenv("STEP2_MAX_RETRIES", "2"))
TEMPERATURE = float(os.getenv("STEP2_TEMPERATURE", "0.2"))
MAX_CONTEXT_CHARS = int(os.getenv("STEP2_MAX_CONTEXT_CHARS", "2400"))

MAX_OCCURRENCES = os.getenv("STEP2_MAX_OCCURRENCES", "").strip()
MAX_OCCURRENCES = int(MAX_OCCURRENCES) if MAX_OCCURRENCES.isdigit() else None

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite").strip()

HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
X_TITLE = os.getenv("OPENROUTER_APP_TITLE", "").strip()

PROMPT_FILE_PATH = os.getenv("STEP2_PROMPT_FILE", "").strip()


CONTEXT_CATEGORIES = [
    "Budget", "Tax relief", "Housing", "Healthcare", "Infrastructure", "Employment",
    "Welfare schemes", "Education/Skills", "Inflation/Cost of living",
    "Business/Entrepreneurship", "Political statement", "National development narrative", "Other",
]

BENEFICIARIES = [
    "Salaried employees", "Taxpayers", "Small businesses/MSMEs", "Entrepreneurs/Startups", "Youth",
    "Women", "Senior citizens", "Urban households", "Rural households", "Homebuyers", "Workers/Labour",
    "Farmers", "Students", "Middle-income families", "Other",
]

TONES = ["supportive", "promise", "reform", "critique", "reassurance", "credit-claim", "warning", "neutral"]


DEFAULT_PROMPT_TEMPLATE = """You are given a short context excerpt (±3 sentences) from a political speech that contains a keyword mention.

Tasks:
1) Rewrite each sentence ONE-BY-ONE (same order). Do NOT copy long phrases.
2) Provide:
   - rewrite_1line: 1 sentence summary
   - rewrite_2to3_sentences: clean paraphrase of the whole excerpt
3) Tag:
   - categories (multi-label) + primary_category (single best)
   - beneficiaries (multi-label)
   - tone (single)
4) policy_refs: only include if an explicit scheme/act/program is named in the excerpt, else [].

Rules:
- Use ONLY the excerpt; do not add facts.
- Output ONLY valid JSON. No markdown, no commentary.

Allowed categories: {categories}
Allowed beneficiaries: {beneficiaries}
Allowed tones: {tones}

Speech meta:
- title: {title}
- url: {url}
- published_date: {published_date}

Keyword: {keyword}
Matched variant: {matched_text}

Excerpt sentences (rewrite these one-by-one):
{sentences_block}

Return JSON exactly in this schema:
{{
  "rewritten_sentences": ["..."],
  "rewrite_1line": "...",
  "rewrite_2to3_sentences": "...",
  "categories": ["..."],
  "primary_category": "...",
  "beneficiaries": ["..."],
  "tone": "...",
  "policy_refs": ["..."],
  "confidence": 0.0,
  "evidence_keywords": ["..."],
  "notes_if_uncertain": ""
}}
"""


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_json_from_text(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    lb = text.find("{")
    rb = text.rfind("}")
    if lb != -1 and rb != -1 and rb > lb:
        return json.loads(text[lb : rb + 1])
    raise ValueError("Model output did not contain valid JSON.")


def clamp_conf(x: Any) -> float:
    try:
        f = float(x)
    except Exception:
        f = 0.5
    return max(0.0, min(1.0, f))


def dedup_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for it in items:
        if not isinstance(it, str):
            continue
        s = it.strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def validate_output(obj: Dict[str, Any]) -> Dict[str, Any]:
    rewritten_sentences = obj.get("rewritten_sentences", [])
    if not isinstance(rewritten_sentences, list):
        rewritten_sentences = []
    rewritten_sentences = [str(x).strip() for x in rewritten_sentences if str(x).strip()]

    categories = obj.get("categories", [])
    if not isinstance(categories, list):
        categories = []
    categories = [str(x).strip() for x in categories if str(x).strip()]
    categories = [c for c in categories if c in set(CONTEXT_CATEGORIES)]
    categories = dedup_keep_order(categories)

    primary = str(obj.get("primary_category", "")).strip()
    if primary not in CONTEXT_CATEGORIES:
        primary = categories[0] if categories else "Other"
    if primary not in categories:
        categories = [primary] + categories
    categories = categories[:8]

    beneficiaries = obj.get("beneficiaries", [])
    if not isinstance(beneficiaries, list):
        beneficiaries = []
    beneficiaries = [str(x).strip() for x in beneficiaries if str(x).strip()]
    beneficiaries = [b for b in beneficiaries if b in set(BENEFICIARIES)]
    beneficiaries = dedup_keep_order(beneficiaries)[:10]

    tone = str(obj.get("tone", "")).strip().lower()
    if tone not in set(TONES):
        tone = "neutral"

    policy_refs = obj.get("policy_refs", [])
    if not isinstance(policy_refs, list):
        policy_refs = []
    policy_refs = [str(x).strip() for x in policy_refs if str(x).strip()]
    policy_refs = dedup_keep_order(policy_refs)[:20]

    confidence = clamp_conf(obj.get("confidence", 0.5))

    evidence_keywords = obj.get("evidence_keywords", [])
    if not isinstance(evidence_keywords, list):
        evidence_keywords = []
    evidence_keywords = [str(x).strip() for x in evidence_keywords if str(x).strip()][:12]

    notes = str(obj.get("notes_if_uncertain", "") or "").strip()[:400]

    return {
        "rewritten_sentences": rewritten_sentences,
        "rewrite_1line": str(obj.get("rewrite_1line", "")).strip()[:400],
        "rewrite_2to3_sentences": str(obj.get("rewrite_2to3_sentences", "")).strip()[:1200],
        "categories": categories,
        "primary_category": primary,
        "beneficiaries": beneficiaries,
        "tone": tone,
        "policy_refs": policy_refs,
        "confidence": confidence,
        "evidence_keywords": evidence_keywords,
        "notes_if_uncertain": notes,
    }


def load_prompt_template() -> str:
    if PROMPT_FILE_PATH:
        p = Path(PROMPT_FILE_PATH)
        if not p.exists():
            raise FileNotFoundError(f"STEP2_PROMPT_FILE not found: {p.resolve()}")
        return p.read_text(encoding="utf-8")
    return DEFAULT_PROMPT_TEMPLATE


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


def main() -> None:
    in_path = Path(STEP2_INPUT_JSON_PATH)
    out_path = Path(STEP2_OUTPUT_JSON_PATH)

    if RESUME and out_path.exists():
        data = load_json(out_path)
        print(f"[INFO] Resuming from: {out_path}")
    else:
        if not in_path.exists():
            raise FileNotFoundError(f"Step2 input not found: {in_path.resolve()}")
        data = load_json(in_path)
        print(f"[INFO] Loaded input: {in_path}")

    client = make_client()
    prompt_template = load_prompt_template()

    keyword = (data.get("meta", {}) or {}).get("keyword") or "middle class"

    # Model stored ONCE here (no _model per occurrence)
    data.setdefault("step2_meta", {})
    data["step2_meta"].update(
        {
            "provider": "openrouter",
            "base_url": BASE_URL,
            "model": MODEL,
            "generated_at": now_iso(),
        }
    )

    speeches = data.get("speeches", [])
    if not isinstance(speeches, list):
        raise ValueError("Invalid input: speeches missing/not list (Step1 v2 expected).")

    processed = 0
    skipped = 0
    failed = 0

    for speech in speeches:
        title = speech.get("title", "") or ""
        url = speech.get("url", "") or ""
        published_date = speech.get("published_date", "") or ""

        paragraphs = speech.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            continue

        for para in paragraphs:
            occurrences = para.get("occurrences", [])
            if not isinstance(occurrences, list):
                continue

            for occ in occurrences:
                if MAX_OCCURRENCES is not None and processed >= MAX_OCCURRENCES:
                    data["step2_meta"]["summary"] = {
                        "processed_new": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "finished_at": now_iso(),
                        "stopped_early": True,
                    }
                    atomic_write_json(out_path, data)
                    print(f"[INFO] Stopped early at STEP2_MAX_OCCURRENCES={MAX_OCCURRENCES}. Saved: {out_path.resolve()}")
                    return

                ai_block = occ.get("ai_analysis")
                if isinstance(ai_block, dict) and ai_block.get("_done") is True:
                    skipped += 1
                    continue

                matched_text = occ.get("matched_text", "") or ""
                ctx = occ.get("context", {}) or {}
                ctx_sents = ctx.get("context_sentences", [])

                if not isinstance(ctx_sents, list) or not ctx_sents:
                    occ["ai_analysis"] = {"_done": False, "_error": "Missing context.context_sentences", "_at": now_iso()}
                    failed += 1
                    continue

                cleaned_sents = [str(s).strip() for s in ctx_sents if str(s).strip()]
                if not cleaned_sents:
                    occ["ai_analysis"] = {"_done": False, "_error": "Empty context sentences", "_at": now_iso()}
                    failed += 1
                    continue

                sentences_block = "\n".join([f"{i+1}. {s}" for i, s in enumerate(cleaned_sents)])
                sentences_block = sentences_block[:MAX_CONTEXT_CHARS]

                prompt = prompt_template.format(
                    categories=", ".join(CONTEXT_CATEGORIES),
                    beneficiaries=", ".join(BENEFICIARIES),
                    tones=", ".join(TONES),
                    title=title,
                    url=url,
                    published_date=published_date,
                    keyword=keyword,
                    matched_text=matched_text,
                    sentences_block=sentences_block,
                )

                last_err: Optional[str] = None
                for _ in range(MAX_RETRIES + 1):
                    try:
                        raw = call_model(client, prompt)
                        payload = validate_output(raw)
                        occ["ai_analysis"] = {**payload, "_done": True, "_at": now_iso()}
                        processed += 1
                        last_err = None
                        break
                    except Exception as e:
                        last_err = str(e)

                if last_err is not None:
                    occ["ai_analysis"] = {"_done": False, "_error": last_err[:2000], "_at": now_iso()}
                    failed += 1

                if (processed + failed) % SAVE_EVERY_N == 0:
                    atomic_write_json(out_path, data)
                    print(f"[INFO] Saved progress: processed={processed}, failed={failed}, skipped={skipped}")

                time.sleep(SLEEP_S)

    data["step2_meta"]["summary"] = {
        "processed_new": processed,
        "skipped": skipped,
        "failed": failed,
        "finished_at": now_iso(),
        "stopped_early": False,
    }
    atomic_write_json(out_path, data)

    print(f"[DONE] Saved: {out_path.resolve()}")
    print(f"[DONE] processed_new={processed}, skipped={skipped}, failed={failed}")


if __name__ == "__main__":
    main()
