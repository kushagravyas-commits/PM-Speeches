import csv
from pathlib import Path

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


SPREADSHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
WORKSHEET_NAME = "Speeches"
CSV_PATH = Path("speech_sheet_rows.csv")
CREDS_PATH = Path("credentials.json")
TOKEN_PATH = Path("token_sheets.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def authorize():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return gspread.authorize(creds)


def ensure_worksheet(sh, title, rows, cols):
    try:
        ws = sh.worksheet(title)
        ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
        return ws
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def main():
    if not CSV_PATH.exists():
        raise SystemExit(f"Missing {CSV_PATH}")

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        values = list(csv.reader(f))

    gc = authorize()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = ensure_worksheet(sh, WORKSHEET_NAME, rows=len(values) + 10, cols=5)

    ws.batch_clear(["A:E"])
    ws.update(f"A1:E{len(values)}", values, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format(
        "A1:E1",
        {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        },
    )
    ws.columns_auto_resize(0, 5)
    print(f"Wrote {len(values) - 1} rows to {sh.url} / {WORKSHEET_NAME}")


if __name__ == "__main__":
    main()
