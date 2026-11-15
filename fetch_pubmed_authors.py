#!/usr/bin/env python3
"""
Fetch PubMed authors (plus journal, title, year) for a list of PubMed IDs.

Usage (terminal):
    python fetch_pubmed_authors.py --input COIPonD_COI_Declared.xlsx --email you@example.com --api-key YOUR_NCBI_API_KEY

Notes:
- You MUST provide a valid email (NCBI requirement). API key is optional but recommended.
- Input file must contain a column named "pmid".
- Outputs:
    - COIPonD_COI_Declared_with_Authors.xlsx
    - COIPonD_COI_Declared_with_Authors.csv
"""

import argparse
import json
import math
import os
import re
import sys
import time
from typing import List, Dict

import pandas as pd
from Bio import Entrez

def parse_args():
    p = argparse.ArgumentParser(description="Fetch PubMed authors for a list of PMIDs")
    p.add_argument("--input", type=str, default="COIPonD_COI_Declared.xlsx", help="Path to Excel/CSV with a 'pmid' column")
    p.add_argument("--email", type=str, required=True, help="Your email (required by NCBI)")
    p.add_argument("--api-key", type=str, default=None, help="NCBI API key (optional, increases rate limits)")
    p.add_argument("--batch-size", type=int, default=200, help="PMIDs per ESummary request (<= 200 recommended)")
    p.add_argument("--sleep", type=float, default=0.34, help="Sleep seconds between requests (0.34 ≈ 3 req/sec)")
    p.add_argument("--output-xlsx", type=str, default="COIPonD_COI_Declared_with_Authors.xlsx", help="Output Excel path")
    p.add_argument("--output-csv", type=str, default="COIPonD_COI_Declared_with_Authors.csv", help="Output CSV path")
    return p.parse_args()

def extract_year(pubdate: str) -> str:
    if not isinstance(pubdate, str):
        return ""
    m = re.search(r"(19|20)\d{2}", pubdate)
    return m.group(0) if m else ""

def fetch_summary_batch(pmids: List[str], email: str, api_key: str = None) -> Dict[str, dict]:
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key
    # Use ESummary (JSON) to grab authors, title, journal, pubdate in bulk
    handle = Entrez.esummary(db="pubmed", id=",".join(pmids), retmode="json")
    data = json.loads(handle.read())
    handle.close()
    # The result set is in data["result"], with a "uids" list
    result = data.get("result", {})
    # Remove "uids" from the dict if present
    result.pop("uids", None)
    return result

def summarize_record(rec: dict) -> dict:
    # Fields vary; handle safely
    title = rec.get("title") or ""
    journal = rec.get("fulljournalname") or rec.get("source") or ""
    pubdate = rec.get("pubdate") or ""
    year = extract_year(pubdate)

    # Authors list can be 'authors' with 'name' OR 'authors' with lastname/foremane
    authors = rec.get("authors") or []
    author_names = []
    for a in authors:
        # Prefer 'name' if present, else combine last/fore
        name = a.get("name")
        if name:
            author_names.append(name)
        else:
            last = a.get("lastname") or ""
            fore = a.get("forename") or a.get("initials") or ""
            nm = (fore + " " + last).strip()
            if nm:
                author_names.append(nm)

    return {
        "title": title,
        "journal": journal,
        "pubdate": pubdate,
        "year": year,
        "authors": "; ".join(author_names) if author_names else ""
    }

def main():
    args = parse_args()

    # Load IDs
    if args.input.lower().endswith(".xlsx") or args.input.lower().endswith(".xls"):
        df = pd.read_excel(args.input)
    else:
        df = pd.read_csv(args.input)

    if "pmid" not in df.columns:
        print("ERROR: Input file must contain a 'pmid' column.", file=sys.stderr)
        sys.exit(1)

    # Prepare PMID list (unique, non-null)
    pmids = df["pmid"].dropna().astype(str).unique().tolist()

    out_rows = []
    total = len(pmids)
    for start in range(0, total, args.batch_size):
        batch = pmids[start : start + args.batch_size]
        tries = 0
        while True:
            try:
                result = fetch_summary_batch(batch, email=args.email, api_key=args.api_key)
                break
            except Exception as e:
                tries += 1
                if tries > 5:
                    print(f"ERROR: failed batch starting at {start}: {e}", file=sys.stderr)
                    result = {}
                    break
                sleep_time = min(60, 2**tries)
                print(f"Warning: error '{e}'. Retrying in {sleep_time}s...", file=sys.stderr)
                time.sleep(sleep_time)

        # Append results row-wise, mapping back to PMID
        for pmid in batch:
            rec = result.get(pmid, {})
            if rec:
                summary = summarize_record(rec)
            else:
                summary = {"title": "", "journal": "", "pubdate": "", "year": "", "authors": ""}
            summary["pmid"] = pmid
            out_rows.append(summary)

        # Be polite to NCBI servers
        time.sleep(args.sleep)

        # Optional progress
        done = min(start + args.batch_size, total)
        print(f"Fetched {done}/{total} ({(done/total)*100:.1f}%)")

    meta_df = pd.DataFrame(out_rows)

    # Merge back on pmid to keep original columns (e.g., coi, coi_category)
    df["pmid"] = df["pmid"].astype(str)
    meta_df["pmid"] = meta_df["pmid"].astype(str)
    merged = df.merge(meta_df, on="pmid", how="left")

    # Save outputs
    merged.to_excel(args.output_xlsx, index=False)
    merged.to_csv(args.output_csv, index=False, encoding="utf-8")

    print(f"✅ Done. Saved:\n - {args.output_xlsx}\n - {args.output_csv}")

if __name__ == "__main__":
    main()
