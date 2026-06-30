from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import importlib.util


def now_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def sanitize_slug(s: str, max_len: int = 60) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return (s or "keyword")[:max_len]


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def infer_keyword_and_window(word_search_json: Dict[str, Any]) -> Tuple[str, str, str]:
    keyword = ""
    start = ""
    end = ""

    top = word_search_json.get("top_speeches") or []
    if isinstance(top, list) and top and isinstance(top[0], dict):
        keyword = (top[0].get("searched_word") or "").strip()
        start = (top[0].get("start_date") or "").strip()
        end = (top[0].get("end_date") or top[0].get("end_date_inclusive") or "").strip()

    q = word_search_json.get("query") or {}
    if isinstance(q, dict):
        start = start or (q.get("start_date") or "").strip()
        end = end or (q.get("end_date_inclusive") or q.get("end_date") or "").strip()
        if not keyword:
            terms = q.get("english_terms") or []
            if isinstance(terms, list) and terms:
                keyword = str(terms[0]).strip()

    return keyword or "keyword", start or "unknown_start", end or "unknown_end"


def project_root_from_this_file() -> Path:
    return Path(__file__).resolve().parents[1]


def run_subprocess(script_path: Path, env_overrides: Dict[str, str], cwd: Path) -> None:
    env = os.environ.copy()
    env.update(env_overrides)
    cmd = [sys.executable, str(script_path)]
    print(f"\n[PIPELINE] Running: {script_path.name}")
    r = subprocess.run(cmd, cwd=str(cwd), env=env)
    if r.returncode != 0:
        raise RuntimeError(f"Step failed: {script_path.name} (exit code {r.returncode})")


def run_script_import_mode(script_path: Path, overrides: Dict[str, Any], cwd: Path) -> None:
    """
    Import a script as a module, override globals, call main().
    This makes IO fully dynamic and avoids reliance on env parsing.
    """
    print(f"\n[PIPELINE] Running: {script_path.name} (import-mode for dynamic IO)")

    spec = importlib.util.spec_from_file_location(script_path.stem, str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module: {script_path}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore

    for k, v in overrides.items():
        setattr(mod, k, v)

    os.chdir(str(cwd))
    if not hasattr(mod, "main"):
        raise RuntimeError(f"{script_path.name} has no main()")
    mod.main()  # type: ignore


def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end keyword report pipeline (Step1→Step6)")
    ap.add_argument("--input", required=True, help="Path to the word-search result JSON (only required input).")
    ap.add_argument("--outputs-root", default="outputs", help="Root outputs directory (default: outputs).")
    args = ap.parse_args()

    proj_root = project_root_from_this_file()
    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input JSON not found: {input_path}")

    ws = load_json(input_path)
    keyword, start_date, end_date = infer_keyword_and_window(ws)

    keyword_slug = sanitize_slug(keyword)
    window_slug = sanitize_slug(f"{start_date}_{end_date}", max_len=80)

    ts = now_stamp()
    run_dir = (proj_root / args.outputs_root / keyword_slug / window_slug / f"run_{ts}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "inputs").mkdir(exist_ok=True)
    (run_dir / "artifacts").mkdir(exist_ok=True)
    (run_dir / "visuals").mkdir(exist_ok=True)

    copied_input = run_dir / "inputs" / "word_search_result.json"
    shutil.copy2(input_path, copied_input)

    step1_out = run_dir / "artifacts" / f"step1_contexts_{keyword_slug}_{start_date}_{end_date}.json"
    step2_out = run_dir / "artifacts" / f"step2_enriched_{keyword_slug}_{start_date}_{end_date}.json"

    step3_data_out = run_dir / "artifacts" / "step3_data_analysis.json"
    step3_occ_out = run_dir / "artifacts" / "step3_occasion_classification.json"
    step3_coocc_out = run_dir / "artifacts" / "step3_cooccurrence.json"
    step3_ym_out = run_dir / "artifacts" / "step3_year_month_insights.json"
    step3_keyev_out = run_dir / "artifacts" / "step3_key_evidence.json"         
    step3_theme_out = run_dir / "artifacts" / "step3_theme_synthesis.json"
    step3_visplan_out = run_dir / "artifacts" / "step3_visual_plan.json"

    step4_manifest = run_dir / "visuals" / "step4_visual_manifest.json"
    step5_blueprint = run_dir / "artifacts" / "step5_report_blueprint.json"
    final_pdf = run_dir / f"final_report_{keyword_slug}_{start_date}_{end_date}.pdf"

    run_meta = {
        "run_id": f"run_{ts}",
        "created_at_utc": ts,
        "keyword": keyword,
        "start_date": start_date,
        "end_date": end_date,
        "paths": {
            "run_dir": str(run_dir).replace("\\", "/"),
            "input_original": str(input_path).replace("\\", "/"),
            "input_copied": str(copied_input).replace("\\", "/"),
            "step1_out": str(step1_out).replace("\\", "/"),
            "step2_out": str(step2_out).replace("\\", "/"),
            "step3_data_out": str(step3_data_out).replace("\\", "/"),
            "step3_occ_out": str(step3_occ_out).replace("\\", "/"),
            "step3_coocc_out": str(step3_coocc_out).replace("\\", "/"),
            "step3_ym_out": str(step3_ym_out).replace("\\", "/"),
            "step3_keyev_out": str(step3_keyev_out).replace("\\", "/"),   # ✅ NEW
            "step3_theme_out": str(step3_theme_out).replace("\\", "/"),
            "step3_visplan_out": str(step3_visplan_out).replace("\\", "/"),
            "step4_manifest": str(step4_manifest).replace("\\", "/"),
            "step5_blueprint": str(step5_blueprint).replace("\\", "/"),
            "final_pdf": str(final_pdf).replace("\\", "/"),
        },
    }
    (run_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")

    src_dir = proj_root / "src"
    step1_script = src_dir / "step1_extract_contexts.py"
    step2_script = src_dir / "step2_ai_enrich_step1v2.py"
    step3_data_script = src_dir / "step3_agent_data_analysis.py"
    step3_occ_script = src_dir / "step3_agent_occasion_classifier.py"
    step3_coocc_script = src_dir / "step3_agent_cooccurrence.py"
    step3_ym_script = src_dir / "step3_agent_year_month_insights.py"
    step3_keyev_script = src_dir / "step3_agent_key_evidence_extractor.py"  
    step3_theme_script = src_dir / "step3_agent_theme_synthesis.py"
    step3_visplan_script = src_dir / "step3_agent_visual_planner.py"
    step4_script = src_dir / "step4_visual_generator.py"
    step5_script = src_dir / "step5_build_report_blueprint.py"
    step6_script = src_dir / "step6_generate_pdf_report.py"

    for p in [
        step1_script, step2_script,
        step3_data_script, step3_occ_script, step3_coocc_script, step3_ym_script, step3_keyev_script,
        step3_theme_script, step3_visplan_script,
        step4_script, step5_script, step6_script
    ]:
        if not p.exists():
            raise FileNotFoundError(f"Missing required script: {p}")

    # STEP 1 (import-mode)
    run_script_import_mode(
        step1_script,
        {"INPUT_JSON_PATH": str(copied_input), "OUTPUT_JSON_PATH": str(step1_out)},
        cwd=proj_root
    )

    # STEP 2 (import-mode)
    run_script_import_mode(
        step2_script,
        {"STEP2_INPUT_JSON_PATH": str(step1_out), "STEP2_OUTPUT_JSON_PATH": str(step2_out)},
        cwd=proj_root
    )

    # STEP 3 agents
    run_subprocess(step3_data_script, {"STEP3_INPUT_JSON_PATH": str(step2_out), "STEP3_OUTPUT_JSON_PATH": str(step3_data_out)}, cwd=proj_root)
    run_subprocess(step3_occ_script, {"STEP3_OCC_INPUT_JSON_PATH": str(step2_out), "STEP3_OCC_OUTPUT_JSON_PATH": str(step3_occ_out)}, cwd=proj_root)
    run_subprocess(step3_coocc_script, {"STEP3_COOCC_INPUT_JSON_PATH": str(step2_out), "STEP3_COOCC_OUTPUT_JSON_PATH": str(step3_coocc_out)}, cwd=proj_root)

    run_subprocess(step3_ym_script, {
        "STEP3_YM_INPUT_JSON_PATH": str(step2_out),
        "STEP3_YM_OUTPUT_JSON_PATH": str(step3_ym_out),
    }, cwd=proj_root)

    run_subprocess(step3_keyev_script, {
        "STEP3_KEY_EVIDENCE_INPUT_JSON_PATH": str(step2_out),
        "STEP3_KEY_EVIDENCE_OUTPUT_JSON_PATH": str(step3_keyev_out),
    }, cwd=proj_root)

    run_subprocess(step3_theme_script, {
        "STEP3_THEME_STEP2_JSON": str(step2_out),
        "STEP3_THEME_DATA_ANALYSIS_JSON": str(step3_data_out),
        "STEP3_THEME_OCCASION_JSON": str(step3_occ_out),
        "STEP3_THEME_COOCC_JSON": str(step3_coocc_out),
        "STEP3_THEME_OUTPUT_JSON": str(step3_theme_out),
    }, cwd=proj_root)

    run_subprocess(step3_visplan_script, {
        "STEP3_VIS_DATA_ANALYSIS_JSON": str(step3_data_out),
        "STEP3_VIS_OCCASION_JSON": str(step3_occ_out),
        "STEP3_VIS_COOCC_JSON": str(step3_coocc_out),
        "STEP3_VIS_THEME_JSON": str(step3_theme_out),
        "STEP3_VIS_OUTPUT_JSON": str(step3_visplan_out),
    }, cwd=proj_root)

    # STEP 4 visuals → flat into run_dir/visuals
    run_subprocess(step4_script, {
        "STEP4_PLAN_PATH": str(step3_visplan_out),
        "STEP4_OUTPUT_ROOT": str(run_dir / "visuals"),
        "STEP4_FLAT_OUTPUT": "true"
    }, cwd=proj_root)

    if not step4_manifest.exists():
        raise FileNotFoundError(f"Step4 manifest not found: {step4_manifest}")

    # STEP 5 blueprint
    run_subprocess(step5_script, {
        "STEP5_THEME_JSON": str(step3_theme_out),
        "STEP5_DATA_ANALYSIS_JSON": str(step3_data_out),
        "STEP5_OCCASION_JSON": str(step3_occ_out),
        "STEP5_YEAR_MONTH_JSON": str(step3_ym_out),
        "STEP5_COOCC_JSON": str(step3_coocc_out),
        "STEP5_KEY_EVIDENCE_JSON": str(step3_keyev_out),            
        "STEP5_VISUAL_PLAN_JSON": str(step3_visplan_out),
        "STEP5_VISUAL_MANIFEST_JSON": str(step4_manifest),
        "STEP5_OUTPUT_JSON": str(step5_blueprint),
    }, cwd=proj_root)

    # STEP 6 PDF
    run_subprocess(step6_script, {
        "STEP6_BLUEPRINT_JSON": str(step5_blueprint),
        "STEP6_OUTPUT_PDF": str(final_pdf)
    }, cwd=proj_root)

    print("\n[PIPELINE] ✅ COMPLETE")
    print("Run folder:", run_dir)
    print("Final PDF :", final_pdf)


if __name__ == "__main__":
    main()
