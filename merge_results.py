#!/usr/bin/env python
# Merge scraped results.tsv into speech_sheet_rows.csv (fill Speech Description + YouTube Link by Serial Number).
import csv

CSV = "speech_sheet_rows.csv"
RESULTS = "results.tsv"

# Load scraped results keyed by serial number
res = {}
with open(RESULTS, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        serial = parts[0]
        status = parts[1] if len(parts) > 1 else ""
        title  = parts[2] if len(parts) > 2 else ""
        yt     = parts[3] if len(parts) > 3 else ""
        res[serial] = {"status": status, "title": title, "yt": yt}

rows = []
with open(CSV, encoding="utf-8-sig", newline="") as f:
    reader = csv.reader(f)
    header = next(reader)
    rows.append(header)
    filled_desc = filled_yt = touched = skipped_fail = 0
    for row in reader:
        if not row:
            continue
        serial = row[0]
        r = res.get(serial)
        if r and r["status"] == "OK":
            touched += 1
            # Only fill if currently empty (don't overwrite existing data)
            if len(row) > 2 and row[2].strip() == "" and r["title"]:
                row[2] = r["title"]; filled_desc += 1
            if len(row) > 4 and row[4].strip() == "" and r["yt"]:
                row[4] = r["yt"]; filled_yt += 1
        elif r and r["status"] == "FAIL":
            skipped_fail += 1
        rows.append(row)

with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerows(rows)

print(f"rows matched to scraped serials: {touched}")
print(f"descriptions filled: {filled_desc}")
print(f"youtube links filled: {filled_yt}")
print(f"scrape failures skipped: {skipped_fail}")
