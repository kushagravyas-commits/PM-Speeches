import json
import re
from copy import deepcopy
from typing import Any, Dict, List, Tuple


def _norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _split_data_landscape_blocks(blocks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns: (dataset_overview_blocks, trend_blocks)
    Heuristic: routes blocks under "B. Speech occasions" to dataset overview.
               routes "A. Monthly frequency" and "C. Top speeches" to trend.
    """
    dataset, trend = [], []
    bucket = None

    for b in blocks:
        if b.get("type") == "heading":
            t = _norm_title(b.get("text", ""))
            if "speech occasions" in t or t.startswith("b."):
                bucket = "dataset"
            elif "monthly frequency" in t or t.startswith("a.") or "top speeches" in t or t.startswith("c."):
                bucket = "trend"

        if bucket == "dataset":
            dataset.append(b)
        else:
            trend.append(b)

    # If heuristic never triggered, treat all as trend (safer)
    if not dataset:
        return [], blocks
    return dataset, trend


def normalize_blueprint(blueprint: Dict[str, Any]) -> Dict[str, Any]:
    sections = blueprint.get("sections", [])
    by_title = { _norm_title(s.get("title","")): s for s in sections }

    exec_sec = by_title.get("executive summary")
    data_landscape = by_title.get("data landscape")
    key_numbers = by_title.get("key numbers mentioned")
    key_quotes = by_title.get("key statements and quotes")
    coocc = by_title.get("co-occurrence analysis")
    yearwise = by_title.get("year-wise analysis")
    monthwise = by_title.get("month analysis")
    discussion = by_title.get("discussion")
    conclusion = by_title.get("conclusion")

    normalized = {
        "format": "research_brief_v1",
        "meta": blueprint.get("meta", {}),
        "cover": blueprint.get("cover", {}),
        "sections": [],
        "appendix": {"collapsed": True, "blocks": []}
    }

    def add_section(key: str, title: str, blocks: List[Dict[str, Any]] = None, collapsed: bool = False, extra: Dict[str, Any] = None):
        sec = {"key": key, "title": title, "collapsed": collapsed}
        if blocks is not None:
            sec["blocks"] = blocks
        if extra:
            sec.update(extra)
        normalized["sections"].append(sec)

    # 01 Executive Summary
    add_section("01", "Executive Summary", deepcopy(exec_sec.get("blocks", [])) if exec_sec else [], collapsed=False)

    # 02 + 03 split from Data Landscape
    dataset_blocks, trend_blocks = ([], [])
    if data_landscape:
        dataset_blocks, trend_blocks = _split_data_landscape_blocks(deepcopy(data_landscape.get("blocks", [])))

    add_section("02", "Dataset Overview", dataset_blocks, collapsed=False)
    add_section("03", "Trend & Concentration", trend_blocks, collapsed=False)

    # 04 Key Evidence (combine numbers + quotes)
    combined = []
    if key_numbers:
        combined.append({"type": "heading", "text": "A. Key Numbers", "level": 2})
        combined += deepcopy(key_numbers.get("blocks", []))
    if key_quotes:
        combined.append({"type": "heading", "text": "B. Key Statements & Quotes", "level": 2})
        combined += deepcopy(key_quotes.get("blocks", []))
    add_section("04", "Key Evidence", combined, collapsed=False)

    # 05 Association Landscape
    add_section("05", "Association Landscape", deepcopy(coocc.get("blocks", [])) if coocc else [], collapsed=False)

    # 06 Temporal Interpretation (collapsed)
    temporal = []
    if yearwise:
        temporal.append({"type": "heading", "text": "A. Year-wise", "level": 2})
        temporal += deepcopy(yearwise.get("blocks", []))
    if monthwise:
        temporal.append({"type": "heading", "text": "B. Month-wise", "level": 2})
        temporal += deepcopy(monthwise.get("blocks", []))
    add_section("06", "Temporal Interpretation", temporal, collapsed=True)

    # 07 Thematic deep dives = anything level-1 that isn't already mapped, excluding Discussion/Conclusion
    used_titles = {
        "executive summary", "data landscape", "key numbers mentioned", "key statements and quotes",
        "co-occurrence analysis", "year-wise analysis", "month analysis", "discussion", "conclusion"
    }
    themes = []
    for s in sections:
        t = _norm_title(s.get("title", ""))
        if t in used_titles:
            continue
        # keep only level-1 sections as themes
        if int(s.get("level", 1) or 1) == 1:
            themes.append({"title": s.get("title", "Theme"), "blocks": deepcopy(s.get("blocks", []))})

    add_section("07", "Thematic Deep Dives", collapsed=False, extra={"themes": themes})

    # 08 Discussion
    add_section("08", "Discussion", deepcopy(discussion.get("blocks", [])) if discussion else [], collapsed=False)

    # 09 Conclusion
    add_section("09", "Conclusion", deepcopy(conclusion.get("blocks", [])) if conclusion else [], collapsed=False)

    # 10 Methodology & Notes (from meta)
    meta = blueprint.get("meta", {})
    meth_blocks = [
        {"type": "paragraph", "text": "This report is generated automatically from a keyword-based speech analysis pipeline."},
        {"type": "table",
         "headers": ["Field", "Value"],
         "rows": [
             ["generated_at", str(meta.get("generated_at",""))],
             ["keyword", str(meta.get("keyword",""))],
             ["window.start_date", str(meta.get("search_window",{}).get("start_date",""))],
             ["window.end_date", str(meta.get("search_window",{}).get("end_date",""))],
             ["version", str(meta.get("version",""))],
         ]
        },
        {"type": "paragraph", "text": "Inputs / artifacts:"},
        {"type": "table",
         "headers": ["Name", "Path"],
         "rows": [[k, v] for k, v in (meta.get("inputs", {}) or {}).items()]
        }
    ]
    add_section("10", "Methodology & Notes", meth_blocks, collapsed=True)

    return normalized


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()

    with open(args.inp, "r", encoding="utf-8") as f:
        bp = json.load(f)

    out = normalize_blueprint(bp)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("Wrote:", args.out)