#!/usr/bin/env python3
"""
dedupe_companies.py

Collapse a large lead CSV into a list of UNIQUE companies to research.
This is the deterministic first step of the event-finder skill: instead of
researching the same company once per lead (the Claygent failure mode), we
research each company a single time, then join results back later.

Company identity key = normalized website domain (preferred) or, if no
website, normalized company name. This avoids treating "acme.com" and
"www.acme.com/about" as different companies.

Usage:
    python dedupe_companies.py INPUT.csv WORKLIST.csv

Output columns: company_key, Company Name, Company Website, Company LinkedIn,
                Company Industry, Company Country, lead_count
"""
import csv
import sys
import re
from collections import OrderedDict


def norm_domain(url: str) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("/")[0].split("?")[0].strip()
    return u


def norm_name(name: str) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    n = re.sub(r"[,\.]", "", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|plc|the)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def company_key(row: dict) -> str:
    d = norm_domain(row.get("Company Website", ""))
    if d:
        return d
    return norm_name(row.get("Company Name", "")) or norm_name(row.get("Org", ""))


def main(inp: str, out: str) -> None:
    companies = OrderedDict()  # key -> representative row + count
    with open(inp, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = company_key(row)
            if not key:
                continue
            if key not in companies:
                companies[key] = {
                    "company_key": key,
                    "Company Name": row.get("Company Name") or row.get("Org", ""),
                    "Company Website": row.get("Company Website", ""),
                    "Company LinkedIn": row.get("Company LinkedIn", ""),
                    "Company Industry": row.get("Company Industry", ""),
                    "Company Country": row.get("Company Country") or row.get("Country", ""),
                    "lead_count": 0,
                }
            companies[key]["lead_count"] += 1

    fields = ["company_key", "Company Name", "Company Website",
              "Company LinkedIn", "Company Industry", "Company Country", "lead_count"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec in companies.values():
            writer.writerow(rec)

    total_leads = sum(c["lead_count"] for c in companies.values())
    print(f"Leads read:       {total_leads}")
    print(f"Unique companies: {len(companies)}")
    print(f"Worklist written: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python dedupe_companies.py INPUT.csv WORKLIST.csv")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
