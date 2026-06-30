#!/usr/bin/env python
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
TAB = "Speeches"
creds = Credentials.from_authorized_user_file("token.json", SCOPES)
svc = build("sheets", "v4", credentials=creds)

vals = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:E4").execute().get("values", [])
print("HEADER+TOP:")
for r in vals: print("  ", r)

colA = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:A").execute().get("values", [])
print("rows with data in col A:", len(colA))
print("first serial:", colA[1] if len(colA)>1 else None, " last serial:", colA[-1] if colA else None)

# Check the in-scope block: rows where serial in 3341..3426 — read C and E to see current emptiness
rng = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A3342:E3344").execute().get("values", [])
print("sample at sheet rows 3342-3344 (expect serials 3341-3343):")
for r in rng: print("  ", r)
