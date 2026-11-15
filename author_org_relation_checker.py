#!/usr/bin/env python3
# -*-  -*-
"""
author_org_relation_checker.py

Automated online check of relationship between an author (person) and an organization (affiliation/employer)
using **legitimate public APIs** (PubMed E-Utilities & Crossref), with optional general web search (Bing or SerpAPI)
to collect evidence links. Results are saved to an Excel file with evidence and verdict per row.

USAGE:
  python author_org_relation_checker.py \
      --input "COI_author_org_relationships_typed.xlsx" \
      --output "COI_author_org_relationships_output.xlsx" \
      --email "your_email@example.com" \
      --ncbi-api-key "YOUR_NCBI_API_KEY" \
      --author-col "author" \
      --org-col "organization" \
      --bing-key "YOUR_BING_KEY" \
      --serpapi-key "YOUR_SERPAPI_KEY"

Only --input and --output are required. If --author-col/--org-col
are omitted, the script will try to detect them heuristically.

LIMITATIONS & ETHICS:
- We do NOT scrape login-protected sites or violate ToS.
- Evidence relies on public metadata (PubMed, Crossref) and public web search.
- Name disambiguation is imperfect; we use simple heuristics + string matching.
"""

import re
import time
import json
import math
import argparse
import logging
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import requests
from urllib.parse import quote_plus

PUBMED_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF_API = "https://api.crossref.org/works"

# --- Helpers -----------------------------------------------------------------

def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def split_name(author_name: str) -> Tuple[str, str]:
    """
    Very simple split: last token is last name; the rest are first/middle.
    Returns (first_part, last_name)
    """
    name = normalize_ws(author_name)
    parts = name.split(" ")
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]

def build_pubmed_query(author: str, org: str) -> str:
    """
    Build a conservative PubMed ESearch term combining author and affiliation.
    We try several patterns joined by OR to increase recall.
    """
    author = normalize_ws(author)
    org = normalize_ws(org)
    first, last = split_name(author)

    terms = []
    # Author field with last name (strongest)
    if last:
        terms.append(f'({quote_plus(last)}[Author])')
    # Full name in all fields (to catch cases with initials)
    terms.append(f'("{quote_plus(author)}")')
    # Organization in affiliation
    if org:
        terms.append(f'("{quote_plus(org)}"[Affiliation])')

    # Combine conservatively
    # Require at least author+org co-occurrence in the record
    # Strategy: (author tokens) AND (org in affiliation)
    author_token = f'("{quote_plus(author)}"[Author] OR {quote_plus(last)}[Author])' if last else f'("{quote_plus(author)}"[Author])'
    affil_token = f'("{quote_plus(org)}"[Affiliation])' if org else ""
    if affil_token:
        final = f'({author_token}) AND {affil_token}'
    else:
        final = author_token
    return final

def pubmed_esearch(query: str, email: Optional[str], api_key: Optional[str], retmax: int = 10) -> List[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(retmax)
    }
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key

    r = requests.get(f"{PUBMED_EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("esearchresult", {}).get("idlist", [])

def pubmed_efetch(pmids: List[str], email: Optional[str], api_key: Optional[str]) -> str:
    if not pmids:
        return ""
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml"
    }
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    r = requests.get(f"{PUBMED_EUTILS_BASE}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
    return r.text

def parse_pubmed_affiliations(xml_text: str) -> List[Dict[str, Any]]:
    """
    Very lightweight parsing: extract <ArticleTitle>, <Author>, <Affiliation> strings.
    We avoid heavy XML libs to keep dependencies minimal.
    """
    # Split on PubmedArticle
    articles = re.split(r"<PubmedArticle(?:\s[^>]*)?>", xml_text)[1:]  # crude split
    results = []
    for art in articles:
        # Title
        title_match = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", art, flags=re.S)
        title = re.sub(r"<.*?>", "", title_match.group(1)).strip() if title_match else ""
        # Authors block
        author_blocks = re.findall(r"<Author(?:\s[^>]*)?>(.*?)</Author>", art, flags=re.S)
        authors = []
        for ab in author_blocks:
            ln = re.search(r"<LastName>(.*?)</LastName>", ab)
            fn = re.search(r"<ForeName>(.*?)</ForeName>", ab)
            inits = re.search(r"<Initials>(.*?)</Initials>", ab)
            affs = re.findall(r"<Affiliation>(.*?)</Affiliation>", ab, flags=re.S)
            affs = [re.sub(r"<.*?>", "", a).strip() for a in affs]
            authors.append({
                "last": ln.group(1).strip() if ln else "",
                "fore": fn.group(1).strip() if fn else "",
                "initials": inits.group(1).strip() if inits else "",
                "affiliations": affs
            })
        results.append({"title": title, "authors": authors})
    return results

def author_matches(rec_author: Dict[str, Any], target_author: str) -> bool:
    ta = normalize_ws(target_author).lower()
    # Build simple author strings to compare
    cand = normalize_ws(f"{rec_author.get('fore','')} {rec_author.get('last','')}").lower()
    cand2 = normalize_ws(f"{rec_author.get('initials','')}{rec_author.get('last','')}").lower()
    # Loose matching to handle initials
    return (ta == cand) or (rec_author.get("last","").lower() in ta and rec_author.get("fore","").lower()[:1] in ta)

def affil_mentions_org(affil: str, org: str) -> bool:
    a = normalize_ws(affil).lower()
    o = normalize_ws(org).lower()
    if not a or not o:
        return False
    # Token-based containment with leniency (strip punctuation, common abbreviations)
    a_clean = re.sub(r"[^a-z0-9\s&\-\.]", " ", a)
    o_clean = re.sub(r"[^a-z0-9\s&\-\.]", " ", o)
    # handle common forms like "Eli Lilly", "Eli Lilly and Company", "Lilly Research Laboratories"
    tokens = set(o_clean.split())
    return all(tok in a_clean for tok in tokens if len(tok) > 2) or (o_clean in a_clean)

def check_pubmed(author: str, org: str, email: Optional[str], api_key: Optional[str]) -> Dict[str, Any]:
    query = build_pubmed_query(author, org)
    pmids = pubmed_esearch(query, email, api_key, retmax=10)
    if not pmids:
        return {"source": "pubmed", "matched": False, "evidence": [], "notes": "No PubMed hits"}

    xml = pubmed_efetch(pmids, email, api_key)
    parsed = parse_pubmed_affiliations(xml)
    evidence = []
    matched_any = False
    for idx, art in enumerate(parsed):
        for a in art["authors"]:
            if author_matches(a, author):
                # check any affiliation mentions org
                if any(affil_mentions_org(af, org) for af in a.get("affiliations", [])):
                    matched_any = True
                    evidence.append({
                        "title": art["title"],
                        "pmid": pmids[idx] if idx < len(pmids) else "",
                        "affiliations": a.get("affiliations", []),
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmids[idx]}/" if idx < len(pmids) else ""
                    })
    return {"source": "pubmed", "matched": matched_any, "evidence": evidence, "notes": "" if matched_any else "No author+org co-mention in affiliations"}

def check_crossref(author: str, org: str, rows: int = 5) -> Dict[str, Any]:
    # Simple Crossref query: author name + org words
    q = f"{author} {org}"
    params = {"query": q, "rows": str(rows)}
    try:
        r = requests.get(CROSSREF_API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"source": "crossref", "matched": False, "evidence": [], "notes": f"Crossref error: {e}"}

    evidence = []
    matched = False
    for item in data.get("message", {}).get("items", []):
        title = " ".join(item.get("title", [])).strip()
        url = ""
        if "URL" in item:
            url = item["URL"]
        # Authors may include affiliation objects
        for auth in item.get("author", []) or []:
            name = f"{auth.get('given','')} {auth.get('family','')}".strip()
            affs = [a.get("name","") for a in (auth.get("affiliation", []) or [])]
            if name and normalize_ws(name).lower() in normalize_ws(author).lower():
                if any(affil_mentions_org(af, org) for af in affs):
                    matched = True
                    evidence.append({
                        "title": title,
                        "url": url,
                        "author": name,
                        "affiliations": affs
                    })
    return {"source": "crossref", "matched": matched, "evidence": evidence, "notes": "" if matched else "No author+org co-mention in affiliations"}

def bing_search(query: str, key: str, count: int = 5) -> List[Dict[str, str]]:
    endpoint = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": key}
    params = {"q": query, "count": count, "responseFilter": "Webpages"}
    r = requests.get(endpoint, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    items = []
    for w in r.json().get("webPages", {}).get("value", []):
        items.append({"name": w.get("name",""), "url": w.get("url","")})
    return items

def serpapi_search(query: str, key: str, num: int = 5) -> List[Dict[str, str]]:
    endpoint = "https://serpapi.com/search.json"
    params = {"q": query, "engine": "google", "api_key": key, "num": num}
    r = requests.get(endpoint, params=params, timeout=30)
    r.raise_for_status()
    items = []
    for w in r.json().get("organic_results", []):
        items.append({"name": w.get("title",""), "url": w.get("link","")})
    return items

def generic_web_evidence(author: str, org: str, bing_key: Optional[str], serp_key: Optional[str]) -> List[str]:
    """Collect a few public evidence links from the web (press releases, staff pages, profiles)."""
    queries = [
        f'"{author}" "{org}"',
        f'"{author}" site:{org.replace(" ", "")}.com',
        f'"{author}" {org} affiliation',
        f'"{author}" {org} profile'
    ]
    urls = []
    for q in queries:
        try:
            if bing_key:
                hits = bing_search(q, bing_key, count=5)
                urls.extend([h["url"] for h in hits])
            elif serp_key:
                hits = serpapi_search(q, serp_key, num=5)
                urls.extend([h["url"] for h in hits])
        except Exception:
            continue
    # de-dup
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq[:8]

# --- Main Runner -------------------------------------------------------------

def detect_columns(df: pd.DataFrame, author_col: Optional[str], org_col: Optional[str]) -> Tuple[str, str]:
    if author_col and author_col in df.columns:
        ac = author_col
    else:
        ac = None
        for c in df.columns:
            lc = str(c).strip().lower()
            if ("author" in lc) or ("name" in lc):
                ac = c
                break
        if ac is None:
            ac = df.columns[0]

    if org_col and org_col in df.columns:
        oc = org_col
    else:
        oc = None
        for c in df.columns:
            lc = str(c).strip().lower()
            if ("org" in lc) or ("organization" in lc) or ("organisation" in lc) or ("affiliation" in lc):
                oc = c
                break
        if oc is None and len(df.columns) > 1:
            oc = df.columns[1]
        elif oc is None:
            raise ValueError("Could not detect organization column")

    return ac, oc

def decide_verdict(pubmed_res, crossref_res, web_links) -> Tuple[str, str]:
    """
    Returns (verdict, rationale)
    verdict: 'Found', 'Likely', 'Inconclusive', 'Not Found'
    """
    if pubmed_res.get("matched") and pubmed_res.get("evidence"):
        return "Found", "PubMed author affiliation includes the organization."
    if crossref_res.get("matched") and crossref_res.get("evidence"):
        return "Found", "Crossref author affiliation includes the organization."
    if web_links:
        return "Likely", "No structured metadata match, but public web pages mention both names."
    return "Inconclusive", "No strong evidence found via PubMed/Crossref/web search."

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Input Excel path")
    ap.add_argument("--output", required=True, help="Output Excel path")
    ap.add_argument("--email", default=None, help="Your email (NCBI polite usage)")
    ap.add_argument("--ncbi-api-key", default=None, help="NCBI API key (optional, improves rate limits)")
    ap.add_argument("--author-col", default=None, help="Author column name")
    ap.add_argument("--org-col", default=None, help="Organization column name")
    ap.add_argument("--bing-key", default=None, help="Bing Web Search API key (optional)")
    ap.add_argument("--serpapi-key", default=None, help="SerpAPI key (optional)")
    ap.add_argument("--sleep", type=float, default=0.35, help="Seconds to sleep between API calls (be polite)")
    args = ap.parse_args()

    df = pd.read_excel(args.input)
    author_col, org_col = detect_columns(df, args.author_col, args.org_col)

    rows = []
    for idx, row in df.iterrows():
        author = normalize_ws(str(row.get(author_col, "")))
        org = normalize_ws(str(row.get(org_col, "")))
        if not author or not org:
            rows.append({
                "author": author,
                "organization": org,
                "verdict": "Inconclusive",
                "rationale": "Missing author or org",
                "pubmed_evidence": "",
                "crossref_evidence": "",
                "web_evidence": ""
            })
            continue

        # PubMed
        try:
            pub_res = check_pubmed(author, org, args.email, args.ncbi_api_key)
        except Exception as e:
            pub_res = {"source": "pubmed", "matched": False, "evidence": [], "notes": f"Error: {e}"}

        time.sleep(args.sleep)

        # Crossref
        try:
            cr_res = check_crossref(author, org, rows=5)
        except Exception as e:
            cr_res = {"source": "crossref", "matched": False, "evidence": [], "notes": f"Error: {e}"}

        time.sleep(args.sleep)

        # Web links (optional)
        web_links = []
        try:
            if args.bing_key or args.serpapi_key:
                web_links = generic_web_evidence(author, org, args.bing_key, args.serpapi_key)
        except Exception:
            pass

        verdict, rationale = decide_verdict(pub_res, cr_res, web_links)

        pub_ev_links = [e.get("url","") for e in pub_res.get("evidence", []) if e.get("url")]
        cr_ev_links = [e.get("url","") for e in cr_res.get("evidence", []) if e.get("url")]

        rows.append({
            "author": author,
            "organization": org,
            "verdict": verdict,
            "rationale": rationale,
            "pubmed_notes": pub_res.get("notes",""),
            "crossref_notes": cr_res.get("notes",""),
            "pubmed_pmids": "; ".join([str(e.get("pmid","")) for e in pub_res.get("evidence", []) if e.get("pmid")]),
            "pubmed_titles": "; ".join([e.get("title","") for e in pub_res.get("evidence", [])]),
            "pubmed_evidence_urls": "; ".join(pub_ev_links),
            "crossref_evidence_urls": "; ".join(cr_ev_links),
            "web_evidence_urls": "; ".join(web_links[:8])
        })

    out_df = pd.DataFrame(rows)
    out_df.to_excel(args.output, index=False)
    print(f"Saved: {args.output}")
    print(f"Detected columns: author='{author_col}', organization='{org_col}'")

if __name__ == "__main__":
    main()
