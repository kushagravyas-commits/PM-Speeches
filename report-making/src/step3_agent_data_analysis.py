from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================
# CONFIG
# =========================
STEP3_INPUT_JSON_PATH = os.getenv(
    "STEP3_INPUT_JSON_PATH",
    "outputs/middle_class_2025-02-06_2025-02-08_contexts_step2_v2_ai.json",
)
STEP3_OUTPUT_JSON_PATH = os.getenv(
    "STEP3_OUTPUT_JSON_PATH",
    "outputs/step3_data_analysis.json",
)
TOP_SPEECHES_N = int(os.getenv("STEP3_TOP_SPEECHES_N", "10"))


# =========================
# Helpers
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


def short_title(title: str, max_len: int = 90) -> str:
    title = (title or "").strip()
    if len(title) <= max_len:
        return title
    return title[: max_len - 1] + "…"


@dataclass
class SpeechStat:
    speech_id: str
    url: str
    title: str
    published_date: str
    year: str
    month: str
    mentions: int
    paragraphs_with_keyword: int


def iter_speech_stats(step2: Dict[str, Any]) -> List[SpeechStat]:
    speeches = step2.get("speeches", [])
    if not isinstance(speeches, list):
        return []

    out: List[SpeechStat] = []
    for sp in speeches:
        if not isinstance(sp, dict):
            continue

        speech_id = str(sp.get("speech_id", "") or "")
        url = str(sp.get("url", "") or "")
        title = str(sp.get("title", "") or "")
        published_date = str(sp.get("published_date", "") or "")

        dt = parse_date_best_effort(published_date)
        y = year_key(dt)
        m = month_key(dt)

        paragraphs = sp.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            paragraphs = []
        paragraphs_with_keyword = len(paragraphs)

        mentions = 0
        for p in paragraphs:
            occs = p.get("occurrences", [])
            if isinstance(occs, list):
                mentions += len(occs)

        out.append(
            SpeechStat(
                speech_id=speech_id,
                url=url,
                title=title,
                published_date=published_date,
                year=y,
                month=m,
                mentions=mentions,
                paragraphs_with_keyword=paragraphs_with_keyword,
            )
        )
    return out


def compute_series(stats: List[SpeechStat], key: str) -> List[Dict[str, Any]]:
    buckets: Dict[str, int] = {}
    for s in stats:
        k = getattr(s, key)
        buckets[k] = buckets.get(k, 0) + s.mentions

    keys = sorted([k for k in buckets.keys() if k != "unknown"])
    if "unknown" in buckets:
        keys.append("unknown")

    return [{key: k, "mentions": buckets[k]} for k in keys]


def compute_ranked_top_speeches(stats: List[SpeechStat], top_n: int) -> List[Dict[str, Any]]:
    def sort_key(s: SpeechStat):
        dt = parse_date_best_effort(s.published_date)
        ts = dt.timestamp() if dt else -1
        return (-s.mentions, -ts, s.title.lower())

    ranked = sorted(stats, key=sort_key)
    top = ranked[:top_n]
    total = sum(s.mentions for s in stats) or 1

    out = []
    for i, s in enumerate(top, start=1):
        out.append(
            {
                "rank": i,
                "speech_id": s.speech_id,
                "title": s.title,
                "title_short": short_title(s.title, 90),
                "url": s.url,
                "published_date": s.published_date,
                "year": s.year,
                "month": s.month,
                "mentions": s.mentions,
                "share_of_total": round((s.mentions / total) * 100, 2),
                "paragraphs_with_keyword": s.paragraphs_with_keyword,
            }
        )
    return out


def group_summary(stats: List[SpeechStat], group_key: str) -> List[Dict[str, Any]]:
    """
    For each year/month:
      - mentions total
      - speeches count
      - top speech in that group
    """
    groups: Dict[str, List[SpeechStat]] = {}
    for s in stats:
        k = getattr(s, group_key)
        groups.setdefault(k, []).append(s)

    keys = sorted([k for k in groups.keys() if k != "unknown"])
    if "unknown" in groups:
        keys.append("unknown")

    out: List[Dict[str, Any]] = []
    for k in keys:
        items = groups[k]
        mentions_total = sum(x.mentions for x in items)
        speeches_count = len(items)
        top = sorted(items, key=lambda x: (-x.mentions, x.title.lower()))[0] if items else None

        out.append(
            {
                group_key: k,
                "mentions": mentions_total,
                "speeches_count": speeches_count,
                "top_speech_title_short": short_title(top.title, 90) if top else "",
                "top_speech_mentions": top.mentions if top else 0,
                "top_speech_url": top.url if top else "",
            }
        )
    return out


def build_narrative(keyword: str, total_mentions: int, speeches_count: int, paragraphs_count: int, monthly_series: List[Dict[str, Any]]) -> Dict[str, str]:
    peak_month = None
    peak_val = None
    for row in monthly_series:
        m = row.get("month")
        if m == "unknown":
            continue
        v = row.get("mentions", 0)
        if peak_val is None or v > peak_val:
            peak_val = v
            peak_month = m

    bullets = [
        f"Total mentions: {total_mentions}",
        f"Speeches with mentions: {speeches_count}",
        f"Paragraphs containing keyword: {paragraphs_count}",
    ]
    if peak_month is not None:
        bullets.append(f"Peak month: {peak_month} ({peak_val})")

    exec_summary_line = " | ".join(bullets)
    totals_para = f"The keyword “{keyword}” appears {total_mentions} times across {speeches_count} speech(es), within {paragraphs_count} keyword-containing paragraph(s)."
    trend_para = f"Monthly frequency peaks in {peak_month} with {peak_val} mention(s)." if peak_month else "Monthly frequency could not be determined (missing/unknown dates)."

    return {
        "executive_summary_line": exec_summary_line,
        "totals_paragraph": totals_para,
        "monthly_trend_paragraph": trend_para,
    }


def main() -> None:
    in_path = Path(STEP3_INPUT_JSON_PATH)
    if not in_path.exists():
        raise FileNotFoundError(f"Step3 input JSON not found: {in_path.resolve()}")

    step2 = load_json(in_path)
    keyword = (step2.get("meta", {}) or {}).get("keyword") or "keyword"
    search_window = (step2.get("meta", {}) or {}).get("search_window", {}) or {}

    stats = iter_speech_stats(step2)
    speeches_count = len(stats)
    total_mentions = sum(s.mentions for s in stats)
    paragraphs_count = sum(s.paragraphs_with_keyword for s in stats)

    monthly_mentions = compute_series(stats, "month")  # [{"month":..., "mentions":...}]
    yearly_mentions = compute_series(stats, "year")    # [{"year":..., "mentions":...}]

    top_speeches = compute_ranked_top_speeches(stats, TOP_SPEECHES_N)

    yearly_summary = group_summary(stats, "year")
    monthly_summary = group_summary(stats, "month")

    narrative = build_narrative(keyword, total_mentions, speeches_count, paragraphs_count, monthly_mentions)

    out = {
        "meta": {
            "agent": "step3_data_analysis_agent",
            "generated_at": now_iso(),
            "input_file": str(in_path).replace("\\", "/"),
            "keyword": keyword,
            "search_window": search_window,
            "version": "step3_agent1_v2_year_month",
        },
        "numbers": {
            "total_mentions": total_mentions,
            "speeches_count": speeches_count,
            "paragraphs_with_keyword_count": paragraphs_count,
        },
        "tables": {
            "monthly_mentions": monthly_mentions,
            "yearly_mentions": yearly_mentions,
            "top_speeches_by_mentions": top_speeches,
            "yearly_summary": yearly_summary,
            "monthly_summary": monthly_summary,
        },
        "text": narrative,
    }

    out_path = Path(STEP3_OUTPUT_JSON_PATH)
    atomic_write_json(out_path, out)
    print("[DONE] Step3 Data Analysis Agent updated output:", out_path.resolve())


if __name__ == "__main__":
    main()
