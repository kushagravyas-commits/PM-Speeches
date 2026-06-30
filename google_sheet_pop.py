import json
from datetime import datetime
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials


# ----------------------------
# CONFIG
# ----------------------------
SERVICE_ACCOUNT_JSON = "lucky-airship-483712-b9-fb342f316f3d.json"
SPREADSHEET_ID = "1dQw9ZwnZmlUe_v1BE_1iHnNTTp0OBcbHmChJJePpUwY"
INPUT_JSON_PATH = "word_search_results_middle class2022-06-01_2024-06-01.json"

WORKSHEET_NAME = "Speeches"


# ----------------------------
# HELPERS
# ----------------------------
def parse_date_safe(dt_str: str):
    if not dt_str:
        return None
    s = str(dt_str).strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass

    # last resort: try first 10 chars if looks like YYYY-MM-DD...
    if len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return None


def month_label(d: datetime) -> str:
    return d.strftime("%B %Y")


def ensure_worksheet(sh, title: str, rows=4000, cols=20):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))


def a1(col: int, row: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def write_range(ws, start_row: int, start_col: int, values):
    end_row = start_row + len(values) - 1
    end_col = start_col + len(values[0]) - 1
    ws.update(
        f"{a1(start_col, start_row)}:{a1(end_col, end_row)}",
        values,
        value_input_option="USER_ENTERED",
    )


def merge_row(ws, row: int, start_col: int, end_col: int):
    ws.merge_cells(
        a1(start_col, row) + ":" + a1(end_col, row),
        merge_type="MERGE_ALL",
    )


def is_yyyy_mm_dd(x: str) -> bool:
    if not isinstance(x, str):
        return False
    x = x.strip()
    return (
        len(x) == 10
        and x[4] == "-"
        and x[7] == "-"
        and x[:4].isdigit()
        and x[5:7].isdigit()
        and x[8:10].isdigit()
    )


# ----------------------------
# MAIN
# ----------------------------
def main():
    with open(INPUT_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    speeches = data.get("top_speeches", [])
    if not speeches:
        raise SystemExit("No top_speeches found in JSON.")

    # --- Build speech-wise rows ---
    speech_rows = []
    total_mentions = 0

    for s in speeches:
        d = parse_date_safe(s.get("published_date"))
        if not d:
            continue

        date_str = d.strftime("%Y-%m-%d")
        mlabel = month_label(d)

        title = s.get("title", "") or ""
        mentions = int(s.get("total_count", 0) or 0)
        url = s.get("url", "") or ""

        total_mentions += mentions
        url_formula = f'=HYPERLINK("{url}", "{url}")' if url else ""

        # Date | Month | Speech Title | Middle Class Mentions | URL
        speech_rows.append([date_str, mlabel, title, mentions, url_formula])

    # ✅ FIX: enforce 5 columns + drop malformed rows that cause shifting
    cleaned = []
    for r in speech_rows:
        r = (r + [""] * 5)[:5]  # force exactly 5 columns
        if not is_yyyy_mm_dd(r[0]):  # Date must be valid
            continue
        cleaned.append(r)
    speech_rows = cleaned

    # sort old -> new
    speech_rows.sort(key=lambda r: r[0])

    total_speeches = len(speech_rows)
    speech_total_row = ["", "", "TOTAL", total_mentions, ""]

    # --- Build month-wise aggregation ---
    agg = defaultdict(lambda: {"mentions": 0, "speeches": 0})
    for r in speech_rows:
        mlabel = r[1]
        mentions = int(r[3])
        agg[mlabel]["mentions"] += mentions
        agg[mlabel]["speeches"] += 1

    def sort_key(mlabel):
        return datetime.strptime(mlabel, "%B %Y")

    month_rows = []
    for m in sorted(agg.keys(), key=sort_key):
        month_rows.append([m, agg[m]["mentions"], agg[m]["speeches"]])

    month_total_row = ["TOTAL", total_mentions, total_speeches]

    # --- Auth + open sheet ---
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    ws = ensure_worksheet(sh, WORKSHEET_NAME, rows=4000, cols=20)

    # ✅ FIX: hard clear a wide area so no leftover shifted data remains
    ws.batch_clear(["A:Z"])

    # --- Section 1: Speech-wise ---
    title1 = f"SPEECH-WISE MENTIONS — {total_mentions} mentions across {total_speeches} speeches"
    write_range(ws, 1, 1, [[title1, "", "", "", ""]])
    merge_row(ws, 1, 1, 5)

    header1 = [["Date", "Month", "Speech Title", "Middle Class Mentions", "URL"]]
    write_range(ws, 2, 1, header1)

    write_row_start = 3
    if speech_rows:
        write_range(ws, write_row_start, 1, speech_rows)
        write_row_start += len(speech_rows)

    # TOTAL row directly after speech rows
    write_range(ws, write_row_start, 1, [speech_total_row])

    # Leave 2 blank rows after speech section (including total)
    next_row = write_row_start + 1 + 2

    # --- Section 2: Month-wise ---
    write_range(ws, next_row, 1, [["MONTH-WISE AGGREGATION", "", ""]])
    merge_row(ws, next_row, 1, 3)

    header2 = [["Month", "Total Mentions", "Number of Speeches"]]
    write_range(ws, next_row + 1, 1, header2)

    month_write_row = next_row + 2
    if month_rows:
        write_range(ws, month_write_row, 1, month_rows)
        month_write_row += len(month_rows)

    # TOTAL row for month-wise table
    write_range(ws, month_write_row, 1, [month_total_row])

    print("Done:", WORKSHEET_NAME)


if __name__ == "__main__":
    main()
