#!/usr/bin/env python
"""One-time OAuth + inspect the target Google Sheet structure."""
import os, json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
TOKEN = "token.json"
CREDS = "credentials.json"

def get_creds():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES)
            print(">>> A browser window will open. Sign in with the Google account that can EDIT the sheet, and approve access.", flush=True)
            creds = flow.run_local_server(port=0, open_browser=True,
                authorization_prompt_message=">>> If no browser opens, visit this URL:\n{url}",
                success_message="Authorization complete. You can close this tab and return to Claude.")
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return creds

creds = get_creds()
svc = build("sheets", "v4", credentials=creds)
meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
print("TITLE:", meta.get("properties", {}).get("title"))
print("TABS:")
for s in meta["sheets"]:
    p = s["properties"]
    gp = p.get("gridProperties", {})
    print(f"  - name={p['title']!r} sheetId={p['sheetId']} rows={gp.get('rowCount')} cols={gp.get('columnCount')}")
first_tab = meta["sheets"][0]["properties"]["title"]
# Read header + first/last few rows of first tab
rng = f"'{first_tab}'!A1:E5"
vals = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute().get("values", [])
print("HEADER+TOP (A1:E5):")
for row in vals:
    print("  ", row)
# total values in col A
colA = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{first_tab}'!A1:A").execute().get("values", [])
print("COL_A_LEN:", len(colA), " last_serial:", colA[-1] if colA else None)
