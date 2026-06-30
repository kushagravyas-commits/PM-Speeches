#!/usr/bin/env python
"""Push Speech Description (C) + YouTube Link (E) from the local CSV to the
'Speeches' tab for a given serial range. Verifies transcript-URL alignment per
row before writing. Default = dry run; pass --apply to write.
Usage: python fill_sheet_from_csv.py LOW HIGH [--apply]   (inclusive serials)"""
import sys, csv, re
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
TAB = "Speeches"
CSV = "speech_sheet_rows.csv"

args = [a for a in sys.argv[1:] if not a.startswith("--")]
LOW, HIGH = int(args[0]), int(args[1])
APPLY = "--apply" in sys.argv

def clean_yt(u):
    u = u.strip()
    # strip stray query junk after an embed id, e.g. .../embed/ID&t  -> .../embed/ID
    m = re.match(r"(https?://www\.youtube\.com/embed/[A-Za-z0-9_-]+)", u)
    return m.group(1) if m else u

def slug(url):
    return url.rstrip("/").split("/")[-1][:25] if url else ""

# Load CSV rows for the range
csv_rows = {}
with open(CSV, encoding="utf-8-sig", newline="") as f:
    for r in csv.reader(f):
        if r and r[0].isdigit() and LOW <= int(r[0]) <= HIGH:
            csv_rows[r[0]] = {"date": r[1], "desc": r[2], "transcript": r[3], "yt": clean_yt(r[4])}

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
svc = build("sheets", "v4", credentials=creds)
grid = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:E").execute().get("values", [])

sheet = {}
for i, row in enumerate(grid):
    if row and (row[0] or "").strip().isdigit():
        sheet[row[0].strip()] = {
            "row": i + 1,
            "desc": row[2] if len(row) > 2 else "",
            "transcript": row[3] if len(row) > 3 else "",
            "yt": row[4] if len(row) > 4 else "",
        }

data, mism, miss, already = [], [], [], 0
for serial, c in sorted(csv_rows.items(), key=lambda kv: int(kv[0])):
    s = sheet.get(serial)
    if not s:
        miss.append(serial); continue
    if c["transcript"] and s["transcript"] and slug(c["transcript"]) != slug(s["transcript"]):
        mism.append((serial, s["row"], s["transcript"], c["transcript"])); continue
    if s["desc"].strip() and s["yt"].strip():
        already += 1
    data.append({"range": f"'{TAB}'!C{s['row']}", "values": [[c["desc"]]]})
    data.append({"range": f"'{TAB}'!E{s['row']}", "values": [[c["yt"]]]})

print(f"serials in range {LOW}-{HIGH} from CSV: {len(csv_rows)}")
print(f"planned writes: {len(data)} cells ({len(data)//2} rows)")
print(f"already-populated in sheet (will be overwritten with same/clean data): {already}")
print(f"missing in sheet: {miss}   mismatches: {len(mism)}")
for m in mism[:5]: print("  MISMATCH", m)
print("--- samples ---")
for serial, c in sorted(csv_rows.items(), key=lambda kv: int(kv[0]))[:3]:
    print(f"  {serial} -> row {sheet[serial]['row']}: C='{c['desc'][:50]}' E='{c['yt']}'")

if not APPLY:
    print("\nDRY RUN — re-run with --apply to write."); sys.exit(0)
if mism or miss:
    print("\nABORT: alignment issues."); sys.exit(1)
resp = svc.spreadsheets().values().batchUpdate(
    spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": data}).execute()
print("\nWROTE:", resp.get("totalUpdatedCells"), "cells")
