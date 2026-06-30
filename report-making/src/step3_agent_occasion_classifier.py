from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional .env loading
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from openai import OpenAI


# =========================
# CONFIG
# =========================
STEP3_OCC_INPUT_JSON_PATH = os.getenv(
    "STEP3_OCC_INPUT_JSON_PATH",
    "outputs/middle_class_2025-02-06_2025-02-08_contexts_step2_v2_ai.json",
)
STEP3_OCC_OUTPUT_JSON_PATH = os.getenv(
    "STEP3_OCC_OUTPUT_JSON_PATH",
    "outputs/step3_occasion_classification.json",
)

RESUME = os.getenv("STEP3_OCC_RESUME", "true").lower() in {"1", "true", "yes"}
SAVE_EVERY_N = int(os.getenv("STEP3_OCC_SAVE_EVERY_N", "10"))
SLEEP_S = float(os.getenv("STEP3_OCC_SLEEP_S", "0.2"))
MAX_RETRIES = int(os.getenv("STEP3_OCC_MAX_RETRIES", "2"))
TEMPERATURE = float(os.getenv("STEP3_OCC_TEMPERATURE", "0.0"))  # keep low for consistency

MAX_SPEECHES = os.getenv("STEP3_OCC_MAX_SPEECHES", "").strip()
MAX_SPEECHES = int(MAX_SPEECHES) if MAX_SPEECHES.isdigit() else None

# OpenRouter via OpenAI SDK
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite").strip()

HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
X_TITLE = os.getenv("OPENROUTER_APP_TITLE", "").strip()

PROMPT_FILE_PATH = os.getenv("STEP3_OCC_PROMPT_FILE", "").strip()


# =========================
# OCCASION TAXONOMY
# (Edit freely, but keep stable across runs)
# =========================
OCCASION_LABELS = [
    "Budget",
    "Parliament",
    "National Day Address",          # e.g., Independence Day / Republic Day style addresses
    "Summit/Conference",
    "Inauguration/Foundation",       # inaugurations, foundation stones, project launches
    "International/Foreign Visit",   # overseas address / foreign visit events
    "Programme/Ceremony",            # award ceremonies, commemorations, special programmes
    "Media/Interview/Podcast",
    "Mann Ki Baat",
    "Election/Rally/Campaign",
    "Other",
]

# Confidence threshold for "low confidence" list
LOW_CONF_THRESHOLD = float(os.getenv("STEP3_OCC_LOW_CONF_THRESHOLD", "0.65"))


DEFAULT_PROMPT_TEMPLATE = """You are classifying the OCCASION / TYPE of a speech.

Choose EXACTLY ONE occasion_label from this allowed list:
{labels}

Optionally provide occasion_subtype (short, specific), e.g. "Rajya Sabha – Motion of Thanks", "Independence Day address", "Foundation stone laying".

Use ONLY the provided speech metadata (title, url, date, speaker, speech_type). Do not invent details.

Return ONLY valid JSON (no markdown, no extra commentary) in this schema:
{{
  "occasion_label": "...",
  "occasion_subtype": "...",
  "confidence": 0.0,
  "evidence_signals": ["...","..."],
  "notes_if_uncertain": ""
}}

Speech metadata:
- title: {title}
- url: {url}
- published_date: {published_date}
- speaker: {speaker}
- speech_type: {speech_type}
"""


# =========================
# Utilities
# =========================
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
        f = 0.0
    return max(0.0, min(1.0, f))


def dedup_keep_order(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    seen = set()
    out: List[str] = []
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


def validate_occ_output(obj: Dict[str, Any]) -> Dict[str, Any]:
    label = str(obj.get("occasion_label", "")).strip()
    if label not in OCCASION_LABELS:
        label = "Other"

    subtype = str(obj.get("occasion_subtype", "")).strip()[:120]

    conf = clamp_conf(obj.get("confidence", 0.0))

    evidence = dedup_keep_order(obj.get("evidence_signals", []))[:8]
    notes = str(obj.get("notes_if_uncertain", "") or "").strip()[:400]

    return {
        "occasion_label": label,
        "occasion_subtype": subtype,
        "confidence": conf,
        "evidence_signals": evidence,
        "notes_if_uncertain": notes,
    }


def load_prompt_template() -> str:
    if PROMPT_FILE_PATH:
        p = Path(PROMPT_FILE_PATH)
        if not p.exists():
            raise FileNotFoundError(f"STEP3_OCC_PROMPT_FILE not found: {p.resolve()}")
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


def count_mentions_in_speech(speech: Dict[str, Any]) -> int:
    """
    Count occurrences by traversing paragraphs->occurrences.
    """
    paragraphs = speech.get("paragraphs", [])
    if not isinstance(paragraphs, list):
        return 0
    total = 0
    for p in paragraphs:
        occs = p.get("occurrences", [])
        if isinstance(occs, list):
            total += len(occs)
    return total


def speech_min_meta(speech: Dict[str, Any]) -> Dict[str, str]:
    return {
        "speech_id": str(speech.get("speech_id", "") or ""),
        "url": str(speech.get("url", "") or ""),
        "title": str(speech.get("title", "") or ""),
        "published_date": str(speech.get("published_date", "") or ""),
        "speaker": str(speech.get("speaker", "") or ""),
        "speech_type": str(speech.get("speech_type", "") or ""),
    }


# =========================
# Main Agent
# =========================
def main() -> None:
    in_path = Path(STEP3_OCC_INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {in_path.resolve()}")

    out_path = Path(STEP3_OCC_OUTPUT_JSON_PATH)

    # Guard against bad output path like "."
    if str(out_path).strip() in {".", ""} or out_path.is_dir():
        raise ValueError(
            f"STEP3_OCC_OUTPUT_JSON_PATH must be a file path, got: {out_path}"
        )

    step2 = load_json(in_path)
    speeches = step2.get("speeches", [])
    if not isinstance(speeches, list):
        raise ValueError("Input JSON missing speeches[] list (Step2 v2.1 expected).")

    keyword = (step2.get("meta", {}) or {}).get("keyword") or ""
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    client = make_client()
    prompt_template = load_prompt_template()

    # Resume support
    already_done: set[str] = set()
    output_doc: Dict[str, Any]
    if RESUME and out_path.exists():
        output_doc = load_json(out_path)
        results_existing = output_doc.get("results", [])
        if isinstance(results_existing, list):
            for r in results_existing:
                sid = str((r or {}).get("speech_id", "") or "")
                if sid:
                    already_done.add(sid)
        print(f"[INFO] Resuming: found {len(already_done)} already-classified speeches.")
    else:
        output_doc = {
            "meta": {
                "agent": "step3_occasion_classifier_agent",
                "generated_at": now_iso(),
                "input_file": str(in_path).replace("\\", "/"),
                "keyword": keyword,
                "search_window": search_window,
                "provider": "openrouter",
                "base_url": BASE_URL,
                "model": MODEL,
                "taxonomy": {
                    "occasion_labels": OCCASION_LABELS,
                    "low_conf_threshold": LOW_CONF_THRESHOLD,
                },
                "version": "step3_occ_agent_v1",
            },
            "results": [],
            "distribution": {},
            "low_confidence": [],
            "run_summary": {
                "processed_new": 0,
                "skipped": 0,
                "failed": 0,
                "finished_at": None,
            },
        }

    processed = 0
    skipped = 0
    failed = 0

    for sp in speeches:
        if not isinstance(sp, dict):
            continue

        sid = str(sp.get("speech_id", "") or "")
        if not sid:
            # still attempt using url as identity if needed
            sid = str(sp.get("url", "") or "")

        if RESUME and sid in already_done:
            skipped += 1
            continue

        if MAX_SPEECHES is not None and processed >= MAX_SPEECHES:
            break

        meta = speech_min_meta(sp)
        mentions = count_mentions_in_speech(sp)

        # Build prompt
        prompt = prompt_template.format(
            labels=", ".join(OCCASION_LABELS),
            title=meta["title"],
            url=meta["url"],
            published_date=meta["published_date"],
            speaker=meta["speaker"],
            speech_type=meta["speech_type"],
        )

        last_err: Optional[str] = None
        payload: Optional[Dict[str, Any]] = None

        for _ in range(MAX_RETRIES + 1):
            try:
                raw = call_model(client, prompt)
                payload = validate_occ_output(raw)
                last_err = None
                break
            except Exception as e:
                last_err = str(e)

        if last_err is not None or payload is None:
            failed += 1
            # fallback record
            payload = {
                "occasion_label": "Other",
                "occasion_subtype": "",
                "confidence": 0.0,
                "evidence_signals": [],
                "notes_if_uncertain": f"LLM failure: {last_err[:300] if last_err else 'unknown'}",
            }

        # Store per-speech result
        result = {
            "speech_id": meta["speech_id"] or sid,
            "url": meta["url"],
            "title": meta["title"],
            "published_date": meta["published_date"],
            "mentions": mentions,
            **payload,
        }
        output_doc["results"].append(result)
        processed += 1 if payload.get("confidence", 0.0) >= 0 else 1  # always count as processed_new in this agent

        # periodic save
        if (len(output_doc["results"]) % SAVE_EVERY_N) == 0:
            atomic_write_json(out_path, output_doc)
            print(f"[INFO] Saved progress. total_results={len(output_doc['results'])}")

        time.sleep(SLEEP_S)

    # Build distribution summary
    dist: Dict[str, Dict[str, int]] = {}
    low_conf: List[Dict[str, Any]] = []

    for r in output_doc.get("results", []):
        if not isinstance(r, dict):
            continue
        label = str(r.get("occasion_label", "Other") or "Other")
        if label not in dist:
            dist[label] = {"speech_count": 0, "mentions_sum": 0}
        dist[label]["speech_count"] += 1
        dist[label]["mentions_sum"] += int(r.get("mentions", 0) or 0)

        conf = float(r.get("confidence", 0.0) or 0.0)
        if conf < LOW_CONF_THRESHOLD:
            low_conf.append(
                {
                    "speech_id": r.get("speech_id", ""),
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "occasion_label": r.get("occasion_label", ""),
                    "confidence": conf,
                    "notes_if_uncertain": r.get("notes_if_uncertain", ""),
                }
            )

    # Sort distribution by speech_count desc then mentions_sum desc
    dist_sorted = sorted(
        [{"occasion_label": k, **v} for k, v in dist.items()],
        key=lambda x: (-x["speech_count"], -x["mentions_sum"], x["occasion_label"]),
    )

    output_doc["distribution"] = dist_sorted
    output_doc["low_confidence"] = low_conf

    output_doc["run_summary"] = {
        "processed_new": len(output_doc.get("results", [])),
        "skipped": skipped,
        "failed": failed,
        "finished_at": now_iso(),
        "max_speeches_limit": MAX_SPEECHES,
    }

    atomic_write_json(out_path, output_doc)

    print("[DONE] Occasion Classifier Agent finished.")
    print("Output:", out_path.resolve())
    print("Total speeches classified:", len(output_doc.get("results", [])))
    print("Low-confidence speeches:", len(low_conf))


if __name__ == "__main__":
    main()
