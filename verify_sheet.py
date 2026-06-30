#!/usr/bin/env python
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
TAB = "Speeches"
creds = Credentials.from_authorized_user_file("token.json", SCOPES)
svc = build("sheets", "v4", credentials=creds)
# in-scope serials 3341..3426 => sheet rows 3342..3427
grid = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A3342:E3427").execute().get("values", [])
empty_c = empty_e = 0
for r in grid:
    c = r[2] if len(r) > 2 else ""
    e = r[4] if len(r) > 4 else ""
    if not c.strip(): empty_c += 1
    if not e.strip(): empty_e += 1
print(f"rows read in-scope: {len(grid)}")
print(f"still-empty Description: {empty_c}")
print(f"still-empty YouTube: {empty_e}")
print("first:", grid[0][0], grid[0][2][:50], "|", grid[0][4])
print("last :", grid[-1][0], grid[-1][2][:50], "|", grid[-1][4])
