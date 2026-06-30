from __future__ import annotations

import csv
import json
import os
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import matplotlib.pyplot as plt

STEP4_PLAN_PATH = os.getenv("STEP4_PLAN_PATH", "outputs/step3_visual_plan.json")
STEP4_OUTPUT_ROOT = os.getenv("STEP4_OUTPUT_ROOT", "outputs/visuals")
STEP4_FLAT_OUTPUT = os.getenv("STEP4_FLAT_OUTPUT", "false").lower() in {"1", "true", "yes"}

DEFAULT_WRAP_WIDTH = int(os.getenv("STEP4_WRAP_WIDTH", "40"))
MIN_BAR_ROW_PX = int(os.getenv("STEP4_MIN_BAR_ROW_PX", "38"))
BASE_BAR_PX = int(os.getenv("STEP4_BASE_BAR_PX", "220"))

# JSONPath: $.a.b.c and $.a.b[0].c
_PART_RE = re.compile(r"([^\[\]]+)(\[[0-9]+\])*")

NUMERIC_CANDIDATES = ["tfidf_sum", "score", "count", "mentions", "speech_count", "mentions_sum", "value"]
LABEL_CANDIDATES = ["term", "ngram", "title_short", "title", "occasion_label", "month", "label"]


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def jsonpath_get(obj: Any, path: str) -> Any:
    path = (path or "").strip()
    if path == "$":
        return obj
    if not path.startswith("$."):
        raise ValueError(f"Unsupported json_path: {path}")

    cur: Any = obj
    parts = path[2:].split(".")
    for part in parts:
        if not part:
            continue
        m = _PART_RE.fullmatch(part)
        if not m:
            raise ValueError(f"Invalid json_path part: {part}")
        key = m.group(1)

        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(f"Key '{key}' not found while resolving {path}")
        cur = cur[key]

        idx_parts = re.findall(r"\[([0-9]+)\]", part)
        for idx_s in idx_parts:
            idx = int(idx_s)
            if not isinstance(cur, list):
                raise TypeError("Indexing non-list in jsonpath")
            cur = cur[idx]
    return cur


def load_cached_json(cache: Dict[str, Any], fp: str) -> Any:
    fp2 = str(Path(fp))
    if fp2 not in cache:
        cache[fp2] = load_json(Path(fp2))
    return cache[fp2]


def fig_size_from_px(w: int, h: int, dpi: int) -> Tuple[float, float]:
    return (max(2.0, w / dpi), max(2.0, h / dpi))


def apply_global_style(gs: Dict[str, Any]) -> int:
    dpi = int(gs.get("figure_dpi", 200) or 200)
    font = gs.get("font_family")
    if font:
        plt.rcParams["font.family"] = font
    base = gs.get("base_font_size")
    if base:
        try:
            plt.rcParams["font.size"] = float(base)
        except Exception:
            pass
    return dpi


def auto_height(n_rows: int, w: int, h: int) -> Tuple[int, int]:
    min_h = BASE_BAR_PX + n_rows * MIN_BAR_ROW_PX
    return w, max(h, min_h)


def wrap_label_limited(text: str, width: int = 28, max_lines: int = 2, max_chars: int = 85) -> str:
    s = (text or "").strip()
    if not s:
        return s
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    lines = textwrap.wrap(s, width=width)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    kept[-1] = kept[-1].rstrip(" .") + "…"
    return "\n".join(kept)


def field_present(data: List[Dict[str, Any]], f: Optional[str]) -> bool:
    if not f:
        return False
    return any(isinstance(r, dict) and f in r for r in data)


def pick_first_present(data: List[Dict[str, Any]], cands: List[str]) -> Optional[str]:
    for c in cands:
        if field_present(data, c):
            return c
    return None


def infer_label_value(data: List[Dict[str, Any]], label_field: Optional[str], value_field: Optional[str]) -> Tuple[str, str]:
    if not field_present(data, label_field):
        label_field = pick_first_present(data, LABEL_CANDIDATES)
    if not field_present(data, value_field):
        value_field = pick_first_present(data, NUMERIC_CANDIDATES)
    if not label_field or not value_field:
        raise ValueError("Could not infer label/value fields")
    return label_field, value_field


def normalize_sort_spec(sort_spec: Any) -> Optional[Dict[str, Any]]:
    if isinstance(sort_spec, dict):
        return sort_spec
    if isinstance(sort_spec, str):
        s = sort_spec.strip()
        m = re.match(r"^(.*)_(asc|desc)$", s)
        if m:
            return {"field": m.group(1), "order": m.group(2)}
    return None


def sort_dataset(data: List[Dict[str, Any]], sort_spec: Any) -> List[Dict[str, Any]]:
    spec = normalize_sort_spec(sort_spec)
    if not spec:
        return data
    field = spec.get("field")
    order = (spec.get("order") or "asc").lower()
    if not field:
        return data

    def key_fn(r: Dict[str, Any]):
        v = r.get(field)
        try:
            return float(v)
        except Exception:
            return str(v or "")

    return sorted(data, key=key_fn, reverse=(order == "desc"))


def top_n_dataset(data: List[Dict[str, Any]], n: Any) -> List[Dict[str, Any]]:
    if n is None:
        return data
    try:
        n = int(n)
    except Exception:
        return data
    return data[: max(0, n)]


def make_figure(title: str, subtitle: str, w: int, h: int, dpi: int):
    fig, ax = plt.subplots(figsize=fig_size_from_px(w, h, dpi), dpi=dpi)

    # Main title
    if title:
        fig.suptitle(title, fontsize=16, y=0.99)

    # Subtitle as figure text (NOT ax title) => no overlap
    if subtitle:
        fig.text(0.5, 0.945, subtitle, ha="center", va="top", fontsize=12)

    return fig, ax



# =========================
# Renderers
# =========================

def per_bar_colors(n: int) -> List[Any]:
    """
    Returns a list of n colors using matplotlib's default color cycle.
    Each bar will have a different color.
    """
    cycle = plt.rcParams["axes.prop_cycle"].by_key().get("color", [])
    if not cycle:
        cycle = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
    return [cycle[i % len(cycle)] for i in range(n)]


def render_line(data, title, subtitle, x_field, y_field, options, out_path, dpi, w, h, grid_on):
    xs = [str(r.get(x_field, "")) for r in data]
    ys = []
    for r in data:
        try:
            ys.append(float(r.get(y_field, 0) or 0))
        except Exception:
            ys.append(0.0)

    w = max(w, 1600)
    h = max(h, 900)

    fig, ax = make_figure(title, subtitle, w, h, dpi)

    x_idx = list(range(len(xs)))
    ax.plot(x_idx, ys, marker="o", linewidth=2)

    if options.get("grid", grid_on):
        ax.grid(True, alpha=0.25)

    # Tick thinning
    n = len(xs)
    step = 1
    if n > 24:
        step = 3
    elif n > 12:
        step = 2
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels([xs[i] for i in ticks], rotation=45, ha="right")

    ax.set_xlabel(options.get("x_label") or "Month")
    ax.set_ylabel(options.get("y_label") or "Mentions")

    # clean layout (prevents “squashed axis”)
    fig.tight_layout(rect=[0.04, 0.18, 0.98, 0.86])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def render_horizontal_bar(data, title, subtitle, label_field, value_field, options, out_path, dpi, w, h, grid_on):
    is_title_like = label_field in {"title", "title_short"}
    wrap_w = int(options.get("wrap_width", 40) or 40)

    labels = []
    for r in data:
        s = str(r.get(label_field, ""))
        if is_title_like:
            labels.append(wrap_label_limited(s, width=80, max_lines=1, max_chars=70))
        else:
            labels.append(wrap_label_limited(s, width=wrap_w, max_lines=2, max_chars=80))

    values = []
    for r in data:
        try:
            values.append(float(r.get(value_field, 0) or 0))
        except Exception:
            values.append(0.0)

    w = max(w, 1700)
    w, h = auto_height(len(labels), w, h)

    fig, ax = make_figure(title, subtitle, w, h, dpi)
    ax.barh(labels, values, color=per_bar_colors(len(values)))


    mx = max(values) if values else 0.0
    if mx > 0:
        ax.set_xlim(0, mx * 1.10)

    if options.get("grid", grid_on):
        ax.grid(True, axis="x", alpha=0.25)

    ax.set_xlabel(options.get("x_label") or value_field)
    ax.set_ylabel(options.get("y_label") or label_field)
    ax.invert_yaxis()

    # give extra left space for long labels
    fig.subplots_adjust(left=0.55, right=0.98, top=0.86, bottom=0.08)

    if options.get("bar_label", False):
        for i, val in enumerate(values):
            txt = f"{int(val)}" if float(val).is_integer() else f"{val:.2f}"
            ax.text(val, i, f" {txt}", va="center")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def render_pie(data, title, subtitle, label_field, value_field, options, out_path, dpi, w, h):
    labels = [str(r.get(label_field, "")) for r in data]
    values = []
    for r in data:
        try:
            values.append(float(r.get(value_field, 0) or 0))
        except Exception:
            values.append(0.0)

    w = max(w, 1400)
    h = max(h, 900)

    fig, ax = make_figure(title, subtitle, w, h, dpi)

    total = sum(values)
    if total <= 0:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center", fontsize=14)
    else:
        ax.pie(
            values,
            labels=None,
            autopct=options.get("autopct", "%1.1f%%"),
            startangle=90,
            radius=1.25,
            pctdistance=0.75,
        )
        ax.axis("equal")
        ax.legend(labels, loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=10)

    fig.tight_layout(rect=[0.04, 0.10, 0.98, 0.92])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def export_csv(data: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        out_path.write_text("", encoding="utf-8")
        return
    keys = []
    seen = set()
    for row in data:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in data:
            w.writerow(row)


# =========================
# Main
# =========================
def main() -> None:
    plan_path = Path(STEP4_PLAN_PATH)
    if not plan_path.exists():
        raise FileNotFoundError(f"Visual plan not found: {plan_path.resolve()}")

    plan = load_json(plan_path)
    dpi = apply_global_style(plan.get("global_style", {}) or {})
    grid_on = bool((plan.get("global_style", {}) or {}).get("grid", True))

    rid = run_id()
    out_root = Path(STEP4_OUTPUT_ROOT)
    run_dir = out_root if STEP4_FLAT_OUTPUT else out_root / f"run_{rid}"
    ensure_dir(run_dir)

    cache: Dict[str, Any] = {}
    manifest = {
        "meta": {
            "agent": "step4_visual_generator_agent",
            "generated_at": now_iso(),
            "plan_file": str(plan_path).replace("\\", "/"),
            "output_dir": str(run_dir).replace("\\", "/"),
            "version": "step4_v7_safe_fields",
        },
        "results": [],
    }

    visuals = plan.get("visuals", []) or []
    if not isinstance(visuals, list):
        raise ValueError("Invalid plan: visuals must be a list")

    for v in visuals:
        vid = v.get("visual_id", "")
        chart = v.get("chart", {}) or {}
        chart_type = str(chart.get("type", "") or "").strip().lower()
        # --- chart type aliases (fix: bar_horizontal coming from plan) ---
        if chart_type in {"bar_horizontal", "barh", "horizontal"}:
            chart_type = "horizontal_bar"
        elif chart_type in {"bar_vertical", "barv", "vertical"}:
            chart_type = "bar"
        # ---------------------------------------------------------------

        title = v.get("title", "") or ""
        subtitle = v.get("subtitle", "") or ""
        out = v.get("output", {}) or {}
        filename = out.get("filename", f"{vid}.png") or f"{vid}.png"

        result = {"visual_id": vid, "chart_type": chart_type, "status": "unknown", "output": None, "error": None}

        try:
            if chart_type in {"none", ""}:
                result["status"] = "skipped"
                manifest["results"].append(result)
                continue

            # ✅ define x_field/y_field safely at the top (fixes your error)
            x_field: Optional[str] = None
            y_field: Optional[str] = None
            x_obj = chart.get("x")
            y_obj = chart.get("y")
            if isinstance(x_obj, dict):
                x_field = x_obj.get("field")
            if isinstance(y_obj, dict):
                y_field = y_obj.get("field")

            data_ref = v.get("data_ref", {}) or {}
            ref_file = data_ref.get("file")
            ref_path = data_ref.get("json_path")
            if not ref_file or not ref_path:
                raise ValueError("Missing data_ref.file or data_ref.json_path")

            src = load_cached_json(cache, ref_file)
            dataset = jsonpath_get(src, ref_path)
            if not isinstance(dataset, list):
                raise TypeError(f"Dataset is not a list for {ref_path}")

            data_list = [row if isinstance(row, dict) else {"value": row} for row in dataset]

            options = chart.get("options", {}) or {}
            data_list = sort_dataset(data_list, options.get("sort_y_by") or options.get("sort_by"))
            data_list = top_n_dataset(data_list, options.get("top_n"))

            w = int(out.get("width_px", 1400) or 1400)
            h = int(out.get("height_px", 700) or 700)
            out_path = run_dir / filename

            if chart_type == "line":
                if not field_present(data_list, x_field):
                    x_field = pick_first_present(data_list, ["month"]) or x_field
                if not field_present(data_list, y_field):
                    y_field = pick_first_present(data_list, ["mentions", "count", "value"]) or y_field
                if not x_field or not y_field:
                    raise ValueError("Line: could not infer x/y fields")

                # pass nice labels if available
                x_label = x_obj.get("label") if isinstance(x_obj, dict) else None
                y_label = y_obj.get("label") if isinstance(y_obj, dict) else None
                options = {**options, "x_label": x_label or "Month", "y_label": y_label or "Mentions"}

                render_line(data_list, title, subtitle, x_field, y_field, options, out_path, dpi, w, h, grid_on)

            elif chart_type in {"horizontal_bar", "bar"}:
                # mapping: y = label, x = value
                label_field, value_field = infer_label_value(data_list, y_field, x_field)
                x_label = x_obj.get("label") if isinstance(x_obj, dict) else None
                y_label = y_obj.get("label") if isinstance(y_obj, dict) else None
                options = {**options, "x_label": x_label or value_field, "y_label": y_label or label_field}

                render_horizontal_bar(data_list, title, subtitle, label_field, value_field, options, out_path, dpi, w, h, grid_on)

            elif chart_type == "pie":
                label_field, value_field = infer_label_value(data_list, x_field, y_field)
                render_pie(data_list, title, subtitle, label_field, value_field, options, out_path, dpi, w, h)

            elif chart_type in {"table", "table_or_bar"}:
                csv_path = run_dir / Path(filename).with_suffix(".csv").name
                export_csv(data_list, csv_path)
                result["status"] = "ok"
                result["output"] = str(csv_path).replace("\\", "/")
                manifest["results"].append(result)
                continue

            else:
                raise ValueError(f"Unsupported chart type: {chart_type}")

            result["status"] = "ok"
            result["output"] = str(out_path).replace("\\", "/")

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        manifest["results"].append(result)

    manifest_path = run_dir / "step4_visual_manifest.json"
    atomic_write_json(manifest_path, manifest)

    ok = sum(1 for r in manifest["results"] if r["status"] == "ok")
    err = sum(1 for r in manifest["results"] if r["status"] == "error")
    skip = sum(1 for r in manifest["results"] if r["status"] == "skipped")

    print("[DONE] Step 4 Visual Generator finished.")
    print("Outputs directory:", run_dir.resolve())
    print("Manifest:", manifest_path.resolve())
    print(f"Summary: ok={ok}, skipped={skip}, error={err}")


if __name__ == "__main__":
    main()
