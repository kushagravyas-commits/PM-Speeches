#!/usr/bin/env python
"""Fill Speech Description (col C) + YouTube Link (col E) in the 'Speeches' tab
from results.tsv, matching by Serial Number. Robust: builds serial->row map from
a full read and verifies the transcript URL before writing."""
import sys, csv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_ID = "1l3kI62JuYqJBR15wd_AD-xv6Z0dDFkON2TbxR-njItU"
TAB = "Speeches"
DRY = "--apply" not in sys.argv

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
svc = build("sheets", "v4", credentials=creds)

# Full read of A:E
grid = svc.spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:E").execute().get("values", [])
# Map serial -> (rownum_1based, transcript_url, cur_desc, cur_yt)
serial_row = {}
for i, row in enumerate(grid):
    rownum = i + 1
    if not row: continue
    serial = (row[0] or "").strip()
    if serial == "Serial Number" or not serial.isdigit(): continue
    d = row[3] if len(row) > 3 else ""
    c = row[2] if len(row) > 2 else ""
    e = row[4] if len(row) > 4 else ""
    serial_row[serial] = (rownum, d, c, e)

# Load scraped results
results = {}
with open("results.tsv", encoding="utf-8") as f:
    for line in f:
        p = line.rstrip("\n").split("\t")
        if len(p) >= 4 and p[1] == "OK":
            results[p[0]] = (p[2], p[3])  # title, yt

print(f"sheet data rows: {len(serial_row)}   scraped results: {len(results)}")

# Slug fragment from transcript URL for cross-check
def slug_frag(url): return url.rstrip("/").split("/")[-1][:25]

# Load our transcript URLs (from worklist) to verify alignment per serial
work = {}
with open("worklist.tsv", encoding="utf-8") as f:
    for line in f:
        ln, serial, url = line.rstrip("\n").split("\t")
        work[serial] = url

data, mismatches, missing = [], [], []
for serial, (title, yt) in results.items():
    if serial not in serial_row:
        missing.append(serial); continue
    rownum, d_sheet, cur_c, cur_e = serial_row[serial]
    our_url = work.get(serial, "")
    # verify the sheet's transcript URL matches ours (alignment check)
    if our_url and d_sheet and slug_frag(our_url) != slug_frag(d_sheet):
        mismatches.append((serial, rownum, d_sheet, our_url)); continue
    data.append({"range": f"'{TAB}'!C{rownum}", "values": [[title]]})
    data.append({"range": f"'{TAB}'!E{rownum}", "values": [[yt]]})

print(f"planned cell-writes: {len(data)} ({len(data)//2} rows)")
print(f"missing serials in sheet: {missing}")
print(f"alignment mismatches: {len(mismatches)}")
for m in mismatches[:5]: print("   MISMATCH", m)

# show 3 samples
print("--- samples ---")
shown = 0
for serial, (title, yt) in results.items():
    if serial in serial_row and serial not in missing:
        rn = serial_row[serial][0]
        print(f"  serial {serial} -> row {rn}: C='{title[:55]}'  E='{yt}'")
        shown += 1
        if shown == 3: break

if DRY:
    print("\nDRY RUN — no changes written. Re-run with --apply to write.")
else:
    if mismatches or missing:
        print("\nABORT: alignment issues present; not writing.")
        sys.exit(1)
    resp = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": data}).execute()
    print("\nWROTE:", resp.get("totalUpdatedCells"), "cells across",
          resp.get("totalUpdatedRanges"), "ranges")
