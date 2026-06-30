from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================
# CONFIG
# =========================
STEP5_THEME_JSON = os.getenv("STEP5_THEME_JSON", "outputs/step3_theme_synthesis.json")
STEP5_DATA_ANALYSIS_JSON = os.getenv("STEP5_DATA_ANALYSIS_JSON", "outputs/step3_data_analysis.json")
STEP5_OCCASION_JSON = os.getenv("STEP5_OCCASION_JSON", "outputs/step3_occasion_classification.json")
STEP5_COOCC_JSON = os.getenv("STEP5_COOCC_JSON", "outputs/step3_cooccurrence.json")
STEP5_YEAR_MONTH_JSON = os.getenv("STEP5_YEAR_MONTH_JSON", "outputs/step3_year_month_insights.json")


STEP5_KEY_EVIDENCE_JSON = os.getenv("STEP5_KEY_EVIDENCE_JSON", "outputs/step3_key_evidence.json").strip()

STEP5_VISUAL_PLAN_JSON = os.getenv("STEP5_VISUAL_PLAN_JSON", "outputs/step3_visual_plan.json")
STEP5_VISUAL_MANIFEST_JSON = os.getenv("STEP5_VISUAL_MANIFEST_JSON", "")
STEP5_OUTPUT_JSON = os.getenv("STEP5_OUTPUT_JSON", "outputs/step5_report_blueprint.json")

TOP_SPEECHES_ROWS = int(os.getenv("STEP5_TOP_SPEECHES_ROWS", "10"))
OCCASION_ROWS = int(os.getenv("STEP5_OCCASION_ROWS", "12"))
COOCC_ROWS = int(os.getenv("STEP5_COOCC_ROWS", "20"))

KEY_NUMBERS_ROWS = int(os.getenv("STEP5_KEY_NUMBERS_ROWS", "20"))
KEY_STATEMENTS_ROWS = int(os.getenv("STEP5_KEY_STATEMENTS_ROWS", "12"))


# =========================
# Helpers
# =========================
def now_date_str() -> str:
    return datetime.now().strftime("%d %b %Y")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_path(p: str) -> str:
    return str(Path(p)).replace("\\", "/")


def find_latest_manifest(default_root: Path = Path("outputs/visuals")) -> Optional[Path]:
    manifests = list(default_root.glob("run_*/step4_visual_manifest.json"))
    if not manifests:
        return None
    manifests.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return manifests[0]


def safe_get(d: Dict[str, Any], path: List[str], default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def mk_paragraph(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "text": text}


def mk_bullets(items: List[str]) -> Dict[str, Any]:
    return {"type": "bullets", "items": items}


def mk_table(headers: List[str], rows: List[List[Any]]) -> Dict[str, Any]:
    return {"type": "table", "headers": headers, "rows": rows}


def mk_heading(text: str, level: int) -> Dict[str, Any]:
    return {"type": "heading", "text": text, "level": level}


def mk_image(path: str, caption: str = "", visual_id: str = "") -> Dict[str, Any]:
    return {"type": "image", "path": path.replace("\\", "/"), "caption": caption, "visual_id": visual_id}


def mk_metrics_strip(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    return {"type": "metrics_strip", "pairs": [[k, v] for k, v in pairs]}


_ROMANS = ["", "I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","XIII","XIV","XV","XVI","XVII","XVIII","XIX","XX"]
def roman(n: int) -> str:
    return _ROMANS[n] if 0 <= n < len(_ROMANS) else str(n)


# =========================
# Visual lookup
# =========================
def build_visual_lookup(visual_plan: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    plan_visuals = visual_plan.get("visuals", []) or []
    plan_by_id: Dict[str, Dict[str, Any]] = {}
    for v in plan_visuals:
        vid = v.get("visual_id")
        if vid:
            plan_by_id[vid] = v

    out_map: Dict[str, Dict[str, Any]] = {}
    for r in (manifest.get("results", []) or []):
        if not isinstance(r, dict) or r.get("status") != "ok":
            continue
        vid = r.get("visual_id")
        output_path = r.get("output")
        if not vid or not output_path:
            continue

        pv = plan_by_id.get(vid, {})
        placement = pv.get("section_placement") or pv.get("section") or ""
        cap = pv.get("title", "") or ""
        sub = pv.get("subtitle", "") or ""
        if sub:
            cap = f"{cap} — {sub}"

        out_map[vid] = {"path": normalize_path(output_path), "caption": cap, "section_placement": placement}
    return out_map


def images_for_section(visuals: Dict[str, Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
    imgs = []
    for vid, info in visuals.items():
        if str(info.get("section_placement", "")).startswith(prefix):
            imgs.append(mk_image(info["path"], caption=info.get("caption", ""), visual_id=vid))
    imgs.sort(key=lambda x: x.get("visual_id", ""))
    return imgs


# =========================
# Theme parsing
# =========================
def extract_content_as_blocks(obj: Any) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    if isinstance(obj, list):
        items = [str(x).strip() for x in obj if str(x).strip()]
        if items:
            blocks.append(mk_bullets(items))
        return blocks
    if isinstance(obj, str) and obj.strip():
        blocks.append(mk_paragraph(obj.strip()))
        return blocks
    if isinstance(obj, dict):
        v = obj.get("headline_bullets") or obj.get("bullets") or obj.get("points")
        if isinstance(v, list) and v:
            blocks.append(mk_bullets([str(x).strip() for x in v if str(x).strip()]))
        txt = obj.get("paragraph") or obj.get("text") or obj.get("summary")
        if isinstance(txt, str) and txt.strip():
            blocks.append(mk_paragraph(txt.strip()))
        return blocks
    return blocks


def get_themes_list(content_plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    # primary expected location
    ti = content_plan.get("thematic_insights")
    if isinstance(ti, dict):
        themes = ti.get("themes")
        if isinstance(themes, list):
            return [t for t in themes if isinstance(t, dict)]

    # fallback 1: sometimes themes at top level
    themes2 = content_plan.get("themes")
    if isinstance(themes2, list):
        return [t for t in themes2 if isinstance(t, dict)]

    # fallback 2: sometimes thematic_insights itself is a list
    if isinstance(ti, list):
        return [t for t in ti if isinstance(t, dict)]

    return []


# =========================
# Key Evidence parsing (NEW, optional)
# =========================
def extract_list_any(d: Dict[str, Any], keys: List[str]) -> List[Dict[str, Any]]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def summarize_key_numbers(key_ev: Dict[str, Any]) -> List[List[Any]]:
    nums = extract_list_any(key_ev, ["key_numbers", "numbers", "metrics", "facts"])
    rows: List[List[Any]] = []
    for it in nums[:KEY_NUMBERS_ROWS]:
        label = it.get("label") or it.get("metric") or it.get("name") or it.get("title") or ""
        value = it.get("value") or it.get("number") or it.get("amount") or ""
        unit = it.get("unit") or ""
        context = it.get("context") or it.get("sentence") or it.get("snippet") or ""
        src = it.get("title") or it.get("speech_title") or ""
        date = str(it.get("published_date") or it.get("date") or "")[:10]
        rows.append([date, src, label, f"{value} {unit}".strip(), context])
    return rows


def summarize_key_statements(key_ev: Dict[str, Any]) -> List[str]:
    st = extract_list_any(key_ev, ["key_statements", "statements", "quotes", "highlights"])
    bullets: List[str] = []
    for it in st[:KEY_STATEMENTS_ROWS]:
        text = it.get("quote") or it.get("text") or it.get("statement") or it.get("line") or ""
        if not text:
            continue
        src = it.get("title") or it.get("speech_title") or ""
        date = str(it.get("published_date") or it.get("date") or "")[:10]
        if src or date:
            bullets.append(f'“{text}” — {src} ({date})'.strip())
        else:
            bullets.append(f'“{text}”')
    return bullets


# =========================
# Main
# =========================
def main() -> None:
    theme = load_json(Path(STEP5_THEME_JSON))
    data_analysis = load_json(Path(STEP5_DATA_ANALYSIS_JSON))
    occasion = load_json(Path(STEP5_OCCASION_JSON))
    coocc = load_json(Path(STEP5_COOCC_JSON))
    ym = load_json(Path(STEP5_YEAR_MONTH_JSON))
    visual_plan = load_json(Path(STEP5_VISUAL_PLAN_JSON))

    # optional key evidence
    key_ev: Optional[Dict[str, Any]] = None
    if STEP5_KEY_EVIDENCE_JSON:
        p = Path(STEP5_KEY_EVIDENCE_JSON)
        if p.exists():
            key_ev = load_json(p)

    manifest_path = Path(STEP5_VISUAL_MANIFEST_JSON) if STEP5_VISUAL_MANIFEST_JSON.strip() else None
    if manifest_path is None or not manifest_path.exists():
        guessed = find_latest_manifest()
        if guessed is None:
            raise FileNotFoundError("Could not find step4_visual_manifest.json. Set STEP5_VISUAL_MANIFEST_JSON.")
        manifest_path = guessed
    manifest = load_json(manifest_path)

    keyword = safe_get(data_analysis, ["meta", "keyword"], "") or safe_get(coocc, ["meta", "keyword"], "")
    search_window = safe_get(data_analysis, ["meta", "search_window"], {}) or safe_get(coocc, ["meta", "search_window"], {})

    vis_lookup = build_visual_lookup(visual_plan, manifest)

    speeches_count = safe_get(data_analysis, ["numbers", "speeches_count"], 0)
    total_mentions = safe_get(data_analysis, ["numbers", "total_mentions"], 0)

    # Robust: theme file may be wrapped or may itself be the plan
    content_plan = theme.get("content_plan")
    if not isinstance(content_plan, dict):
    # fallback: sometimes stored under "content"
        content_plan = theme.get("content") if isinstance(theme.get("content"), dict) else None
    if not isinstance(content_plan, dict):
    # last resort: treat whole file as plan
        content_plan = theme if isinstance(theme, dict) else {}
    report_title = content_plan.get("report_title") or f"Keyword Analysis Report: {keyword}"
    subtitle = f"Data-Driven Analysis of {speeches_count} Speech(es) • Keyword: “{keyword}”"
    report_date = now_date_str()

    exec_line = safe_get(data_analysis, ["text", "executive_summary_line"], "")
    totals_para = safe_get(data_analysis, ["text", "totals_paragraph"], "")
    trend_para = safe_get(data_analysis, ["text", "monthly_trend_paragraph"], "")

    # Tables for display
    top_speeches = safe_get(data_analysis, ["tables", "top_speeches_by_mentions"], []) or []
    top_speeches_rows = [
        [r.get("rank",""), str(r.get("published_date",""))[:10], r.get("mentions",""), r.get("title_short", r.get("title",""))]
        for r in top_speeches[:TOP_SPEECHES_ROWS]
    ]

    occ_dist = occasion.get("distribution", []) or []
    occ_rows = [[r.get("occasion_label",""), r.get("speech_count",""), r.get("mentions_sum","")] for r in occ_dist[:OCCASION_ROWS]]

    # keep TF-IDF table as you said (ok)
    co_terms = safe_get(coocc, ["global", "unigrams", "top_by_tfidf"], []) or []
    co_rows = [[r.get("term",""), r.get("tfidf_sum",""), r.get("speech_df","")] for r in co_terms[:COOCC_ROWS]]

    years = ym.get("years", []) or []
    months = ym.get("months", []) or []
    overall = ym.get("overall", {}) or {}

    sections: List[Dict[str, Any]] = []
    sec_num = 1

    # I Executive Summary
    sec_I_blocks: List[Dict[str, Any]] = []
    sec_I_blocks.append(mk_metrics_strip([("Speeches", speeches_count), ("Mentions", total_mentions)]))
    sec_I_blocks.extend(extract_content_as_blocks(content_plan.get("executive_summary")))
    if exec_line: sec_I_blocks.append(mk_paragraph(exec_line))
    if totals_para: sec_I_blocks.append(mk_paragraph(totals_para))
    if trend_para: sec_I_blocks.append(mk_paragraph(trend_para))
    sections.append({"id": roman(sec_num), "title": "Executive Summary", "level": 1, "blocks": sec_I_blocks})
    sec_num += 1

    # NEW: Key Numbers Mentioned (only if data exists)
    if key_ev is not None:
        num_rows = summarize_key_numbers(key_ev)
        if num_rows:
            blocks = [
                mk_paragraph("Key numeric claims mentioned in the speeches (extracted automatically)."),
                mk_table(["Date", "Speech", "Metric", "Value", "Context"], num_rows),
            ]
            sections.append({"id": roman(sec_num), "title": "Key Numbers Mentioned", "level": 1, "blocks": blocks})
            sec_num += 1

        st_bullets = summarize_key_statements(key_ev)
        if st_bullets:
            blocks = [
                mk_paragraph("Key statements / quote-worthy lines identified from the speeches (extracted automatically)."),
                mk_bullets(st_bullets),
            ]
            sections.append({"id": roman(sec_num), "title": "Key Statements and Quotes", "level": 1, "blocks": blocks})
            sec_num += 1

    # Data Landscape
    sec_II_blocks: List[Dict[str, Any]] = []
    sec_II_blocks.append(mk_heading("A. Monthly frequency", 2))
    sec_II_blocks.append(mk_paragraph("Monthly trend of keyword mentions."))
    sec_II_blocks.extend(images_for_section(vis_lookup, "II.A"))

    sec_II_blocks.append(mk_heading("B. Speech occasions", 2))
    sec_II_blocks.append(mk_paragraph("Distribution of speeches and mentions by occasion."))
    if occ_rows:
        sec_II_blocks.append(mk_table(["Occasion", "Speeches", "Mentions"], occ_rows))
    sec_II_blocks.extend(images_for_section(vis_lookup, "II.B"))

    sec_II_blocks.append(mk_heading("C. Top speeches", 2))
    if top_speeches_rows:
        sec_II_blocks.append(mk_table(["Rank", "Date", "Mentions", "Speech (short)"], top_speeches_rows))
    sec_II_blocks.extend(images_for_section(vis_lookup, "II.C"))

    sections.append({"id": roman(sec_num), "title": "Data Landscape", "level": 1, "blocks": sec_II_blocks})
    sec_num += 1

    # Co-occurrence
    sec_III_blocks: List[Dict[str, Any]] = []
    sec_III_blocks.append(mk_paragraph("Most salient terms and phrases appearing alongside the keyword (TF-IDF weighted)."))
    if co_rows:
        sec_III_blocks.append(mk_table(["Term", "TF-IDF (sum)", "Speech coverage"], co_rows))
    sec_III_blocks.extend(images_for_section(vis_lookup, "III"))
    sections.append({"id": roman(sec_num), "title": "Co-occurrence Analysis", "level": 1, "blocks": sec_III_blocks})
    sec_num += 1

    # Year-wise Analysis
    sec_Y_blocks: List[Dict[str, Any]] = []
    sec_Y_blocks.append(mk_paragraph("Year-wise: what the keyword is most associated with (categories, beneficiaries, salient phrases)."))
    for y in years:
        year = str(y.get("year", "unknown"))
        sec_Y_blocks.append(mk_heading(year, 2))
        sec_Y_blocks.append(mk_metrics_strip([("Mentions", y.get("mentions", 0)), ("Speeches", y.get("speeches_count", 0)), ("Paragraphs", y.get("paragraph_docs", 0))]))
        hl = y.get("highlights", []) or []
        if hl: sec_Y_blocks.append(mk_bullets([str(x) for x in hl]))
        cats = y.get("top_primary_categories", []) or []
        if cats:
            rows = [[c.get("label",""), c.get("count","")] for c in cats[:6]]
            sec_Y_blocks.append(mk_table(["Top categories", "Count"], rows))
    sections.append({"id": roman(sec_num), "title": "Year-wise Analysis", "level": 1, "blocks": sec_Y_blocks})
    sec_num += 1

    # Month Analysis
    sec_M_blocks: List[Dict[str, Any]] = []
    sec_M_blocks.append(mk_paragraph("Month-wise: what the keyword is most associated with (categories, beneficiaries, salient phrases)."))
    for m in months:
        month = str(m.get("month", "unknown"))
        sec_M_blocks.append(mk_heading(month, 2))
        sec_M_blocks.append(mk_metrics_strip([("Mentions", m.get("mentions", 0)), ("Speeches", m.get("speeches_count", 0)), ("Paragraphs", m.get("paragraph_docs", 0))]))
        hl = m.get("highlights", []) or []
        if hl: sec_M_blocks.append(mk_bullets([str(x) for x in hl]))
        cats = m.get("top_primary_categories", []) or []
        if cats:
            rows = [[c.get("label",""), c.get("count","")] for c in cats[:6]]
            sec_M_blocks.append(mk_table(["Top categories", "Count"], rows))
    sections.append({"id": roman(sec_num), "title": "Month Analysis", "level": 1, "blocks": sec_M_blocks})
    sec_num += 1

    # Theme sections (more themes will now appear automatically)
    themes_list = get_themes_list(content_plan)
    print(f"[INFO] Themes found for report: {len(themes_list)}")
    for t in themes_list:
        theme_title = str(t.get("theme_title") or "").strip() or "(Untitled theme)"
        blocks: List[Dict[str, Any]] = []
        blocks.extend(extract_content_as_blocks(t.get("content")))

        subs = t.get("subthemes")
        if isinstance(subs, list):
            for st in subs:
                if not isinstance(st, dict):
                    continue
                st_title = str(st.get("title") or "").strip() or "(Untitled subtheme)"
                blocks.append(mk_heading(st_title, 2))
                blocks.extend(extract_content_as_blocks(st.get("content")))
        sections.append({"id": roman(sec_num), "title": theme_title, "level": 1, "blocks": blocks})
        sec_num += 1

    # Discussion
    disc_blocks = extract_content_as_blocks(content_plan.get("discussion"))
    if not disc_blocks:
        bullets = overall.get("highlights", []) or []
        disc_blocks = [mk_paragraph("Discussion (data-led):"), mk_bullets([str(x) for x in bullets]) if bullets else mk_paragraph("")]
    sections.append({"id": roman(sec_num), "title": "Discussion", "level": 1, "blocks": disc_blocks})
    sec_num += 1

    # Conclusion
    conc_blocks = extract_content_as_blocks(content_plan.get("conclusion"))
    if not conc_blocks:
        conc = overall.get("highlights", []) or []
        conc_blocks = [mk_bullets([str(x) for x in conc[:4]])] if conc else [mk_paragraph("Conclusion.")]
    sections.append({"id": roman(sec_num), "title": "Conclusion", "level": 1, "blocks": conc_blocks})
    sec_num += 1

    blueprint = {
        "meta": {
            "generated_at": now_date_str(),
            "keyword": keyword,
            "search_window": search_window,
            "inputs": {
                "theme": normalize_path(STEP5_THEME_JSON),
                "data_analysis": normalize_path(STEP5_DATA_ANALYSIS_JSON),
                "occasion": normalize_path(STEP5_OCCASION_JSON),
                "cooccurrence": normalize_path(STEP5_COOCC_JSON),
                "year_month": normalize_path(STEP5_YEAR_MONTH_JSON),
                "key_evidence": normalize_path(STEP5_KEY_EVIDENCE_JSON) if STEP5_KEY_EVIDENCE_JSON else "",
                "visual_plan": normalize_path(STEP5_VISUAL_PLAN_JSON),
                "visual_manifest": normalize_path(str(manifest_path)),
            },
            "version": "step5_blueprint_v5_key_evidence_optional",
        },
        "cover": {
            "title": report_title,
            "subtitle": subtitle,
            "date": report_date,
            "keyword": keyword,
            "window": search_window,
            "totals": {"speeches": speeches_count, "mentions": total_mentions},
        },
        "sections": sections,
    }

    out_path = Path(STEP5_OUTPUT_JSON)
    atomic_write_json(out_path, blueprint)
    print("[DONE] Step 5 blueprint written:", out_path.resolve())


if __name__ == "__main__":
    main()
