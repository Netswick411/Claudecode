#!/usr/bin/env python3
"""
merge_results.py

Join the per-company event research back onto the FULL lead list, preserving
every original column and the original row order, and appending the new
event columns. This is the deterministic final step of the event-finder skill.

The results CSV must contain at least:
    company_key, Event Name, Event Date, Event Role, Event Source

(company_key values must match those produced by dedupe_companies.py)

Any lead whose company has no result row is filled with "No".

Usage:
    python merge_results.py ORIGINAL.csv RESULTS.csv OUTPUT.csv
"""
import csv
import sys
import re

NEW_COLS = ["Event Name", "Event Date", "Event Role", "Event Source", "Event Proof"]


def norm_domain(url: str) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].split("?")[0].strip()


def norm_name(name: str) -> str:
    if not name:
        return ""
    n = re.sub(r"[,\.]", "", name.strip().lower())
    n = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|plc|the)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def company_key(row: dict) -> str:
    d = norm_domain(row.get("Company Website", ""))
    if d:
        return d
    return norm_name(row.get("Company Name", "")) or norm_name(row.get("Org", ""))


def main(original: str, results: str, output: str) -> None:
    # Build lookup: company_key -> {new col: value}
    lookup = {}
    with open(results, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (r.get("company_key") or "").strip().lower()
            if not key:
                continue
            lookup[key] = {c: (r.get(c) or "").strip() for c in NEW_COLS}

    with open(original, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        in_fields = reader.fieldnames or []
        out_fields = in_fields + [c for c in NEW_COLS if c not in in_fields]
        rows = list(reader)

    matched = 0
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            res = lookup.get(company_key(row))
            if res and res.get("Event Name") and res["Event Name"].lower() != "no":
                matched += 1
                for c in NEW_COLS:
                    row[c] = res.get(c, "")
            else:
                row["Event Name"] = "No"
                row["Event Date"] = ""
                row["Event Role"] = ""
                row["Event Source"] = ""
            writer.writerow(row)

    print(f"Rows written:        {len(rows)}")
    print(f"Leads with an event: {matched}")
    print(f"Output written:      {output}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python merge_results.py ORIGINAL.csv RESULTS.csv OUTPUT.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
