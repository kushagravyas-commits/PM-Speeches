import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape

def load_visuals_from_meta(meta: dict) -> dict:
    """
    Expects meta.inputs.visual_manifest to point to a JSON file.
    That JSON should map visual_id -> { kind: "vegaLite"|"plotly", spec: {...}, title?: "..."}
    """
    inputs = (meta or {}).get("inputs") or {}
    vm_path = inputs.get("visual_manifest")
    if not vm_path:
        return {}

    p = Path(vm_path)
    if not p.exists():
        return {}

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def safe_slug(s: str) -> str:
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in [" ", "-", "_"]:
            out.append("-")
    slug = "".join(out)
    slug = "-".join([p for p in slug.split("-") if p])
    return slug[:80] or "report"


def copy_images_and_rewrite(report: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    report = json.loads(json.dumps(report))  # deep copy via json
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    def handle_blocks(blocks):
        for b in blocks:
            if b.get("type") == "image" and b.get("path"):
                src = Path(b["path"])
                # keep filename, fallback to visual_id
                name = src.name if src.name else (b.get("visual_id", "figure") + ".png")
                dst = assets / name
                if src.exists():
                    shutil.copyfile(src, dst)
                    b["path"] = f"assets/{name}"
                else:
                    # if file missing, keep path but renderer will show broken image
                    pass

    for s in report.get("sections", []):
        if "blocks" in s:
            handle_blocks(s["blocks"])
        if "themes" in s:
            for t in s["themes"]:
                handle_blocks(t.get("blocks", []))

    handle_blocks(report.get("appendix", {}).get("blocks", []))
    return report


def main(inp_json: str, template_dir: str = "templates", template_name: str = "research_brief_v1.html.j2"):
    with open(inp_json, "r", encoding="utf-8") as f:
        report = json.load(f)
    visuals = load_visuals_from_meta(report.get("meta", {}))
    visuals_json = json.dumps(visuals, ensure_ascii=False)
    keyword = report.get("cover", {}).get("keyword", "report")
    start = report.get("cover", {}).get("window", {}).get("start_date", "")
    end = report.get("cover", {}).get("window", {}).get("end_date", "")
    report_id = safe_slug(f"{keyword}-{start}-{end}")

    out_dir = Path("dist") / "reports" / report_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = copy_images_and_rewrite(report, out_dir)

    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tmpl = env.get_template(template_name)

    html = tmpl.render(
        meta=report.get("meta", {}),
        cover=report.get("cover", {}),
        sections=report.get("sections", []),
        appendix=report.get("appendix", {"blocks": []}),
        visuals_json=visuals_json,
    )

    (out_dir / "index.html").write_text(html, encoding="utf-8")
    print("Wrote:", out_dir / "index.html")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="normalized report json")
    args = ap.parse_args()
    main(args.inp)