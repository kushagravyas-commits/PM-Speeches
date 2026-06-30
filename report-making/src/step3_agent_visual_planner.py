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

USE_LLM = os.getenv("STEP3_VIS_USE_LLM", "false").lower() in {"1", "true", "yes"}
try:
    from openai import OpenAI 
except Exception:
    OpenAI = None

STEP3_VIS_DATA_ANALYSIS_JSON = os.getenv("STEP3_VIS_DATA_ANALYSIS_JSON", "outputs/step3_data_analysis.json")
STEP3_VIS_OCCASION_JSON = os.getenv("STEP3_VIS_OCCASION_JSON", "outputs/step3_occasion_classification.json")
STEP3_VIS_COOCC_JSON = os.getenv("STEP3_VIS_COOCC_JSON", "outputs/step3_cooccurrence.json")
STEP3_VIS_THEME_JSON = os.getenv("STEP3_VIS_THEME_JSON", "outputs/step3_theme_synthesis.json")

STEP3_VIS_OUTPUT_JSON = os.getenv("STEP3_VIS_OUTPUT_JSON", "outputs/step3_visual_plan.json")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite").strip()

HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
X_TITLE = os.getenv("OPENROUTER_APP_TITLE", "").strip()

TEMPERATURE = float(os.getenv("STEP3_VIS_TEMPERATURE", "0.2"))
MAX_RETRIES = int(os.getenv("STEP3_VIS_MAX_RETRIES", "2"))
SLEEP_S = float(os.getenv("STEP3_VIS_SLEEP_S", "0.3"))

TOP_N_TOP_SPEECHES = int(os.getenv("STEP3_VIS_TOP_N_TOP_SPEECHES", "10"))
TOP_N_COOCC = int(os.getenv("STEP3_VIS_TOP_N_COOCC", "20"))
TOP_N_NGRAMS = int(os.getenv("STEP3_VIS_TOP_N_NGRAMS", "20"))


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


def ensure_output_path_is_file(out_path: Path) -> None:
    if str(out_path).strip() in {".", ""}:
        raise ValueError(f"STEP3_VIS_OUTPUT_JSON must be a file path, got: {out_path}")
    if out_path.exists() and out_path.is_dir():
        raise ValueError(f"STEP3_VIS_OUTPUT_JSON points to a directory, must be a file: {out_path}")


def json_ref(file_path: str, json_path: str, note: str = "") -> Dict[str, Any]:
    ref = {"file": file_path.replace("\\", "/"), "json_path": json_path}
    if note:
        ref["note"] = note
    return ref


# ==========================================================
# BASELINE VISUAL PLAN (deterministic)
#  - NO TF-IDF visuals
#  - Coocc: count-based freq charts only
#  - Pie chart included (percent + legend)
#  - Top speeches axis mapping fixed
# ==========================================================
def baseline_visual_plan(
    keyword: str,
    data_analysis_path: str,
    occasion_path: str,
    coocc_path: str,
    theme_path: str,
) -> Dict[str, Any]:
    global_style = {
        "theme_name": "clean_report_v2",
        "font_family": "Helvetica",
        "base_font_size": 11,
        "title_font_size": 16,
        "subtitle_font_size": 12,
        "grid": True,
        "grid_alpha": 0.25,
        "palette": "matplotlib_default",
        "background": "white",
        "figure_dpi": 200,
        "number_format": {"counts": "int", "percent": "0.0%"},
    }

    visuals: List[Dict[str, Any]] = []

    # II.A Monthly mentions (line) – keep it clean + bigger canvas
    visuals.append({
        "visual_id": "monthly_mentions_line_chart",
        "section_placement": "II.A",
        "title": f"Monthly Mentions of '{keyword.title()}'",
        "subtitle": "",
        "chart": {
            "type": "line",
            "x": {"field": "month", "label": "Month"},
            "y": {"field": "mentions", "label": "Number of mentions"},
            "options": {
                "marker": True,
                "x_tick_rotation": 45,
                "sort_x": "ascending",
                "grid": True,
            },
        },
        "data_ref": json_ref(data_analysis_path, "$.tables.monthly_mentions"),
        "output": {"filename": "monthly_mentions_line_chart.png", "width_px": 1600, "height_px": 1100},
    })

    # II.B Occasion – bar (pretty)
    visuals.append({
        "visual_id": "occasion_speeches_bar",
        "section_placement": "II.B",
        "title": "Speeches by occasion type",
        "subtitle": "",
        "chart": {
            "type": "horizontal_bar",
            "x": {"field": "speech_count", "label": "Speeches"},
            "y": {"field": "occasion_label", "label": "Occasion"},
            "options": {
                "sort_y_by": {"field": "speech_count", "order": "desc"},
                "grid": True,
                "wrap_labels": True,
                "bar_label": True,
            },
        },
        "data_ref": json_ref(occasion_path, "$.distribution"),
        "output": {"filename": "occasion_speeches_bar.png", "width_px": 1700, "height_px": 900},
    })

    # II.B Occasion – pie (exact style: % + legend)
    visuals.append({
        "visual_id": "speech_occasions_pie_chart",
        "section_placement": "II.B",
        "title": "Distribution of Speech Occasions",
        "subtitle": "",
        "chart": {
            "type": "pie",
            "x": {"field": "occasion_label", "label": "Occasion"},
            "y": {"field": "speech_count", "label": "Speeches"},
            "options": {"autopct": "%1.1f%%"},
        },
        "data_ref": json_ref(occasion_path, "$.distribution"),
        "output": {"filename": "speech_occasions_pie_chart.png", "width_px": 1700, "height_px": 1000},
    })

    # II.C Top speeches – IMPORTANT axis fix (y=title_short, x=mentions)
    visuals.append({
        "visual_id": "top_speeches_bar_chart",
        "section_placement": "II.C",
        "title": f"Top Speeches by '{keyword.title()}' Mentions",
        "subtitle": "",
        "chart": {
            "type": "horizontal_bar",
            "x": {"field": "mentions", "label": "Mentions"},
            "y": {"field": "title_short", "label": "Speech Title"},
            "options": {
                "top_n": TOP_N_TOP_SPEECHES,
                "sort_y_by": {"field": "mentions", "order": "desc"},
                "grid": True,
                "wrap_labels": True,
            },
        },
        "data_ref": json_ref(data_analysis_path, "$.tables.top_speeches_by_mentions"),
        "output": {"filename": "top_speeches_bar_chart.png", "width_px": 1700, "height_px": 1800},
    })

    # III Co-occurrence — UNIGRAMS by frequency (count)
    visuals.append({
        "visual_id": "coocc_unigrams_bar_chart",
        "section_placement": "III",
        "title": "Top Co-occurring Terms (Unigrams)",
        "subtitle": "",
        "chart": {
            "type": "horizontal_bar",
            "x": {"field": "count", "label": "Frequency"},
            "y": {"field": "term", "label": "Term"},
            "options": {
                "top_n": TOP_N_COOCC,
                "sort_y_by": {"field": "count", "order": "desc"},
                "grid": True,
                "wrap_labels": True,
            },
        },
        "data_ref": json_ref(coocc_path, "$.global.unigrams.top_by_freq"),
        "output": {"filename": "coocc_unigrams_bar_chart.png", "width_px": 1700, "height_px": 1200},
    })

    # III Co-occurrence — BIGRAMS by frequency (count)
    visuals.append({
        "visual_id": "coocc_bigrams_bar_chart",
        "section_placement": "III",
        "title": "Top Co-occurring Bigrams",
        "subtitle": "",
        "chart": {
            "type": "horizontal_bar",
            "x": {"field": "count", "label": "Frequency"},
            "y": {"field": "ngram", "label": "Bigram"},
            "options": {
                "top_n": TOP_N_NGRAMS,
                "sort_y_by": {"field": "count", "order": "desc"},
                "grid": True,
                "wrap_labels": True,
            },
        },
        "data_ref": json_ref(coocc_path, "$.global.bigrams.top_by_freq"),
        "output": {"filename": "coocc_bigrams_bar_chart.png", "width_px": 1700, "height_px": 1200},
    })

    # III Co-occurrence — TRIGRAMS by frequency (count)
    visuals.append({
        "visual_id": "coocc_trigrams_bar_chart",
        "section_placement": "III",
        "title": "Top Co-occurring Trigrams",
        "subtitle": "",
        "chart": {
            "type": "horizontal_bar",
            "x": {"field": "count", "label": "Frequency"},
            "y": {"field": "ngram", "label": "Trigram"},
            "options": {
                "top_n": TOP_N_NGRAMS,
                "sort_y_by": {"field": "count", "order": "desc"},
                "grid": True,
                "wrap_labels": True,
            },
        },
        "data_ref": json_ref(coocc_path, "$.global.trigrams.top_by_freq"),
        "output": {"filename": "coocc_trigrams_bar_chart.png", "width_px": 1700, "height_px": 1300},
    })

    # Optional slot
    visuals.append({
        "visual_id": "vis_thematic_optional_slot_1",
        "section_placement": "IV",
        "title": "Optional Theme Visual Slot (if needed)",
        "subtitle": "",
        "chart": {"type": "none", "options": {"note": "Reserved slot."}},
        "data_ref": json_ref(theme_path, "$.content_plan.thematic_insights", "Use only if needed."),
        "output": {"filename": "theme_slot_1.png", "width_px": 1400, "height_px": 700},
    })

    return {
        "meta": {
            "agent": "step3_visual_planner_agent",
            "generated_at": now_iso(),
            "version": "step3_visual_plan_v2_freq_only_pie",
            "keyword": keyword,
            "inputs": {
                "data_analysis": data_analysis_path.replace("\\", "/"),
                "occasion": occasion_path.replace("\\", "/"),
                "cooccurrence": coocc_path.replace("\\", "/"),
                "theme_synthesis": theme_path.replace("\\", "/"),
            },
            "mode": "deterministic_baseline",
        },
        "global_style": global_style,
        "visuals": visuals,
    }


# =========================
# LLM mode (optional)
# - Must not produce TF-IDF visuals; use freq datasets only
# - Escaped braces in prompt to avoid .format() issues
# =========================
LLM_PROMPT_TEMPLATE = """You are a Visual Planner. Create a VISUAL BLUEPRINT for a report.
You should try to include pie charts and bar charts.
Rules:
- Output ONE valid JSON object only (no markdown, no extra text).
- Only use the provided dataset references. Do not invent data.
- IMPORTANT: Do NOT create TF-IDF visuals. Use FREQUENCY/COUNT datasets for unigram/bigram/trigram charts.
- Include BOTH: an occasion bar chart and an occasion pie chart (pie must use legend + percentages).
- For Top Speeches chart: y-axis must be title_short, x-axis must be mentions (NOT swapped).
- Specify chart type, x/y fields, sorting, top_n, axis labels, grid, and output filename/dimensions.
- Define a global_style section once.

Here is the planning packet (JSON). Use ONLY this:
{packet_json}

Return JSON with keys: meta, global_style, visuals (list)
Each visual must include:
- visual_id
- section_placement (e.g., "II.A", "II.B", "II.C", "III")
- title
- data_ref (file + json_path)
- chart: {{type, x, y, options}}
- output: {{filename, width_px, height_px}}
"""


def make_llm_client() -> OpenAI:
    if OpenAI is None:
        raise RuntimeError("openai package missing. Install openai.")
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing.")
    return OpenAI(base_url=BASE_URL, api_key=OPENROUTER_API_KEY)


def llm_call(client: OpenAI, prompt: str) -> Dict[str, Any]:
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


def build_packet_for_llm(
    data_analysis: Dict[str, Any],
    occasion: Dict[str, Any],
    coocc: Dict[str, Any],
    theme: Dict[str, Any],
    paths: Dict[str, str],
) -> Dict[str, Any]:
    keyword = (data_analysis.get("meta", {}) or {}).get("keyword") or (coocc.get("meta", {}) or {}).get("keyword") or ""
    return {
        "keyword": keyword,
        "dataset_refs": [
            {"name": "monthly_mentions", "file": paths["data_analysis"], "json_path": "$.tables.monthly_mentions"},
            {"name": "top_speeches_by_mentions", "file": paths["data_analysis"], "json_path": "$.tables.top_speeches_by_mentions"},
            {"name": "occasion_distribution", "file": paths["occasion"], "json_path": "$.distribution"},
            {"name": "coocc_unigrams_freq", "file": paths["coocc"], "json_path": "$.global.unigrams.top_by_freq"},
            {"name": "coocc_bigrams_freq", "file": paths["coocc"], "json_path": "$.global.bigrams.top_by_freq"},
            {"name": "coocc_trigrams_freq", "file": paths["coocc"], "json_path": "$.global.trigrams.top_by_freq"},
        ],
        "constraints": {
            "no_tfidf_visuals": True,
            "prefer_horizontal_bar": True,
            "include_pie_for_occasions": True,
            "top_speeches_n": TOP_N_TOP_SPEECHES,
            "coocc_top_n": TOP_N_COOCC,
            "ngrams_top_n": TOP_N_NGRAMS,
        }
    }


def main() -> None:
    da_path = Path(STEP3_VIS_DATA_ANALYSIS_JSON)
    oc_path = Path(STEP3_VIS_OCCASION_JSON)
    co_path = Path(STEP3_VIS_COOCC_JSON)
    th_path = Path(STEP3_VIS_THEME_JSON)
    out_path = Path(STEP3_VIS_OUTPUT_JSON)
    ensure_output_path_is_file(out_path)

    for p in (da_path, oc_path, co_path, th_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing required input: {p.resolve()}")

    data_analysis = load_json(da_path)
    occasion = load_json(oc_path)
    coocc = load_json(co_path)
    theme = load_json(th_path)

    keyword = (data_analysis.get("meta", {}) or {}).get("keyword") or (coocc.get("meta", {}) or {}).get("keyword") or "keyword"

    if USE_LLM:
        try:
            client = make_llm_client()
            paths = {
                "data_analysis": str(da_path).replace("\\", "/"),
                "occasion": str(oc_path).replace("\\", "/"),
                "coocc": str(co_path).replace("\\", "/"),
                "theme": str(th_path).replace("\\", "/"),
            }
            packet = build_packet_for_llm(data_analysis, occasion, coocc, theme, paths)
            packet_json = json.dumps(packet, ensure_ascii=False, indent=2)
            prompt = LLM_PROMPT_TEMPLATE.format(packet_json=packet_json)

            plan = None
            last_err = None
            for _ in range(MAX_RETRIES + 1):
                try:
                    plan = llm_call(client, prompt)
                    last_err = None
                    break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(SLEEP_S)

            if plan is None:
                raise RuntimeError(f"LLM plan failed: {last_err}")

            plan.setdefault("meta", {})
            plan["meta"].update({
                "agent": "step3_visual_planner_agent",
                "generated_at": now_iso(),
                "provider": "openrouter",
                "base_url": BASE_URL,
                "model": MODEL,
                "version": "step3_visual_plan_v2_llm_freq_only",
                "mode": "llm",
            })

            atomic_write_json(out_path, plan)
            print("[DONE] Visual Planner (LLM) finished.")
            print("Output:", out_path.resolve())
            return

        except Exception as e:
            print(f"[WARN] LLM visual planning failed, falling back to deterministic baseline. Reason: {e}")

    baseline = baseline_visual_plan(
        keyword=keyword,
        data_analysis_path=str(da_path),
        occasion_path=str(oc_path),
        coocc_path=str(co_path),
        theme_path=str(th_path),
    )
    atomic_write_json(out_path, baseline)
    print("[DONE] Visual Planner (baseline) finished.")
    print("Output:", out_path.resolve())


if __name__ == "__main__":
    main()
