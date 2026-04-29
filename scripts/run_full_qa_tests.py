#!/usr/bin/env python3
"""
FULL QA Test Runner — ALL queries from queries.txt with real DB values.
Covers every section: Main Business, CRM Commands, Account, Deals, Contacts,
Tasks, Invoice modules — all placeholders replaced with real database values.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── Real DB values (fetched live above) ───────────────────────────────────
COMPANY     = "Watsica - Hudson Solutions"
COMPANY2    = "Startup Accelerator"
CONTACT     = "Jane Medhurst"
CONTACT2    = "Shanon Kovacek"
DEAL        = "IT Consulting Package - Hansen, Kutch and Swift Solutions"
DEAL2       = "Inventory Management System - Pagac LLC Inc"
SO_NUM      = "SO00004"          # Confirm status
SO_CANCEL   = "SO00001"          # Cancelled
INV_PAID    = "ELSN/2025/1"
INV_DRAFT   = "ELSN/2025/2"
INV_CONF    = "ELSN/2025/3"
USER        = "Ketul Trivedi"
USER2       = "Rohan Pandey"
REGION      = "USA"
SOURCE      = "Organic/SEO"
TECH        = "Shopify"
PRODUCT     = "ROR Support Package"
TAX         = "Tax 15%"
STAGE       = "Negotiation"
PRIORITY    = "High"
TASK_STATUS = "Pending"
INDUSTRY    = "Consulting"
DATASET     = "Jewels.csv"
DATE_PAST   = "2025-08-04"
DATE_INV    = "2025-08-23"
DATE_NEW    = "2026-04-03"
DATE_RANGE  = "2025-01-01 to 2025-12-31"
AMT_RANGE   = "1000 to 50000"

# ── FULL Query Suite ───────────────────────────────────────────────────────
QUERIES: List[Tuple[str, str, str]] = [
    # (qid, category, query)

    # ── SECTION 1: MAIN BUSINESS QUERIES ──────────────────────────────────
    # LEADS & CUSTOMERS
    ("Q001", "LEADS",    "Which leads have not been contacted in 7 days"),
    ("Q002", "LEADS",    "List new leads from this week"),
    ("Q003", "LEADS",    "List new leads this month"),
    ("Q004", "LEADS",    "New leads this week"),
    ("Q005", "LEADS",    "New leads this month"),
    ("Q006", "LEADS",    "What is the conversion rate from lead to customer"),
    ("Q007", "LEADS",    "Conversion rate from lead to customer"),
    ("Q008", "LEADS",    "Which customers have not given business in 3 months"),
    ("Q009", "LEADS",    f"Who is {USER}"),

    # REVENUE & SALES
    ("Q010", "REVENUE",  "Total revenue generated in FY2025"),
    ("Q011", "REVENUE",  "Total revenue this financial year"),
    ("Q012", "REVENUE",  "Total revenue of last month"),
    ("Q013", "REVENUE",  "Show me total sales for last month"),
    ("Q014", "REVENUE",  "Total sales for last month"),
    ("Q015", "REVENUE",  "Total collection"),

    # INVOICES & PAYMENTS
    ("Q016", "INVOICES", "Pending invoices"),
    ("Q017", "INVOICES", "Overdue payments"),
    ("Q018", "INVOICES", "Overdue invoice aging and outstanding amount by company"),

    # DEALS & PIPELINE
    ("Q019", "DEALS",    "How many deals are there"),
    ("Q020", "DEALS",    "Give me all deals"),
    ("Q021", "DEALS",    "Give me closed won deals"),
    ("Q022", "DEALS",    "Closed won deals"),
    ("Q023", "DEALS",    "Which deals are stuck in the proposal stage"),
    ("Q024", "DEALS",    "Opportunities lost in July"),
    ("Q025", "DEALS",    "Lost deals last quarter"),
    ("Q026", "DEALS",    "Sales pipeline by stage"),
    ("Q027", "DEALS",    "Pipeline distribution by stage"),

    # PERFORMANCE & ANALYTICS
    ("Q028", "ANALYTICS","Top 10 customers by revenue"),
    ("Q029", "ANALYTICS","Show me the top 10 customers by revenue"),
    ("Q030", "ANALYTICS","Which sales rep closed the most deals this quarter"),
    ("Q031", "ANALYTICS","Top performing sales owners in last 6 months with monthly trend"),
    ("Q032", "ANALYTICS","Funnel performance and conversion to closed won by owner"),
    ("Q033", "ANALYTICS","Best product and project type combinations by value avg deal size and win rate"),
    ("Q034", "ANALYTICS","High activity companies with weak payment conversion"),

    # TASKS & ACTIVITIES
    ("Q035", "TASKS",    "What tasks are pending"),
    ("Q036", "TASKS",    "Pending tasks"),
    ("Q037", "TASKS",    "What tasks are pending for the sales team"),
    ("Q038", "TASKS",    "What tasks are pending for the users"),
    ("Q039", "TASKS",    "Any follow-ups overdue this week"),

    # CONTACTS
    ("Q040", "CONTACTS", "Last 5 contacts details"),

    # ORGANIZATION
    ("Q041", "ORG",      "How many departments"),

    # ── SECTION 2: CRM COMMANDS — GENERAL ─────────────────────────────────
    ("Q042", "GENERAL",  f"Get details for this company {COMPANY}"),
    ("Q043", "GENERAL",  f"Get details for this contact {CONTACT}"),
    ("Q044", "GENERAL",  f"Get details for this deal {DEAL}"),
    ("Q045", "GENERAL",  f"Get details for this invoice {INV_PAID}"),
    ("Q046", "GENERAL",  f"Get details for this sales order {SO_NUM}"),

    # ── LINKED DATA ────────────────────────────────────────────────────────
    ("Q047", "LINKED",   f"Get tasks linked to this company {COMPANY}"),
    ("Q048", "LINKED",   f"Get tasks linked to this deal {DEAL}"),
    ("Q049", "LINKED",   f"Get contacts linked to this company {COMPANY}"),
    ("Q050", "LINKED",   f"Get deals linked to this company {COMPANY}"),
    ("Q051", "LINKED",   f"Get invoices linked to this company {COMPANY}"),
    ("Q052", "LINKED",   f"Get sales orders linked to this company {COMPANY}"),

    # ── MEETINGS & TASKS ───────────────────────────────────────────────────
    ("Q053", "MEETINGS", "Get meetings for today"),
    ("Q054", "MEETINGS", f"Get upcoming meetings for this user {USER}"),
    ("Q055", "MEETINGS", f"Get all open tasks for this user {USER}"),
    ("Q056", "MEETINGS", f"Get tasks due on {DATE_PAST}"),

    # ── DEALS ──────────────────────────────────────────────────────────────
    ("Q057", "DEALS",    f"Get deals in {STAGE} stage"),
    ("Q058", "DEALS",    f"Get deals closing on {DATE_PAST}"),
    ("Q059", "DEALS",    f"Get deals owned by {USER}"),

    # ── INVOICES ───────────────────────────────────────────────────────────
    ("Q060", "INVOICES", f"Get invoices due on {DATE_INV}"),
    ("Q061", "INVOICES", "Get invoices with status paid"),
    ("Q062", "INVOICES", "Get invoices with status draft"),

    # ── SALES ORDERS ───────────────────────────────────────────────────────
    ("Q063", "SALES_ORD","Get sales orders with status Confirm"),
    ("Q064", "SALES_ORD","Get confirmed sales orders"),
    ("Q065", "SALES_ORD","Get cancelled sales orders"),
    ("Q066", "SALES_ORD","Get draft sales orders"),
    ("Q067", "SALES_ORD",f"Get sales order details {SO_NUM}"),
    ("Q068", "SALES_ORD",f"Get sales orders owned by {USER}"),
    ("Q069", "SALES_ORD",f"Get sales orders created by {USER}"),
    ("Q070", "SALES_ORD",f"Get sales orders between date range {DATE_RANGE}"),
    ("Q071", "SALES_ORD",f"Get sales orders between amount range {AMT_RANGE}"),

    # ── COMPANIES & CONTACT FILTERS ────────────────────────────────────────
    ("Q072", "FILTERS",  f"Get companies by region {REGION}"),
    ("Q073", "FILTERS",  f"Get companies by source {SOURCE}"),
    ("Q074", "FILTERS",  "Get contacts by job title Manager"),
    ("Q075", "FILTERS",  f"Get contacts created on {DATE_NEW}"),

    # ── REPORTS & ANALYTICS ────────────────────────────────────────────────
    ("Q076", "REPORTS",  "Get pipeline summary for this month"),
    ("Q077", "REPORTS",  "Get leads by source for this period"),
    ("Q078", "REPORTS",  "Get deals by stage for this period"),
    ("Q079", "REPORTS",  "Get revenue summary for this month"),
    ("Q080", "REPORTS",  "Get regional performance for this month"),
    ("Q081", "REPORTS",  "Get top products for this period"),
    ("Q082", "REPORTS",  "Get lost leads and reasons for this month"),

    # ── TARGETS ────────────────────────────────────────────────────────────
    ("Q083", "TARGETS",  f"Get target details for {USER}"),
    ("Q084", "TARGETS",  "Get target vs achieved for all users"),
    ("Q085", "TARGETS",  "Get performance by user"),
    ("Q086", "TARGETS",  "Get performance monthly"),

    # ── OUTREACH ───────────────────────────────────────────────────────────
    ("Q087", "OUTREACH", f"Get outreach data for dataset {DATASET}"),
    ("Q088", "OUTREACH", "Get interested leads for last 7 days"),
    ("Q089", "OUTREACH", "Get unique touches and total touches"),
    ("Q090", "OUTREACH", f"Get outreach contacts by region {REGION}"),
    ("Q091", "OUTREACH", f"Get outreach history for {CONTACT}"),

    # ── PRODUCTS & SYSTEM ──────────────────────────────────────────────────
    ("Q092", "PRODUCTS", f"Get product details for {PRODUCT}"),
    ("Q093", "PRODUCTS", f"Get products under technology {TECH}"),
    ("Q094", "PRODUCTS", f"Get tax details for {TAX}"),
    ("Q095", "PRODUCTS", "Get all payment methods"),
    ("Q096", "PRODUCTS", "Get all industries"),
    ("Q097", "PRODUCTS", "Get all technologies"),
    ("Q098", "PRODUCTS", "Get all sources"),

    # ── ACCOUNT MODULE ─────────────────────────────────────────────────────
    ("Q099", "ACCOUNT",  f"Get account details for {COMPANY}"),
    ("Q100", "ACCOUNT",  f"Get account owner for {COMPANY}"),
    ("Q101", "ACCOUNT",  f"Get phone number for {COMPANY}"),
    ("Q102", "ACCOUNT",  f"Get email for {COMPANY}"),
    ("Q103", "ACCOUNT",  f"Get account type for {COMPANY}"),
    ("Q104", "ACCOUNT",  f"Get country for {COMPANY}"),
    ("Q105", "ACCOUNT",  f"Get source for {COMPANY}"),
    ("Q106", "ACCOUNT",  f"Get creation date for {COMPANY}"),
    ("Q107", "ACCOUNT",  f"Get customer since date for {COMPANY}"),
    ("Q108", "ACCOUNT",  f"Get last activity for {COMPANY}"),
    ("Q109", "ACCOUNT",  f"Get last business date for {COMPANY}"),
    ("Q110", "ACCOUNT",  f"Get address details for {COMPANY}"),
    ("Q111", "ACCOUNT",  f"Get industry for {COMPANY}"),
    ("Q112", "ACCOUNT",  f"Get annual revenue for {COMPANY}"),
    ("Q113", "ACCOUNT",  f"Get website for {COMPANY}"),
    ("Q114", "ACCOUNT",  f"Get total deals value for {COMPANY}"),
    ("Q115", "ACCOUNT",  f"Get total invoices amount for {COMPANY}"),
    ("Q116", "ACCOUNT",  f"Get tasks for {COMPANY}"),
    ("Q117", "ACCOUNT",  f"Get meeting history for {COMPANY}"),
    ("Q118", "ACCOUNT",  f"Get sales status for {COMPANY}"),

    # ── DEALS MODULE ───────────────────────────────────────────────────────
    ("Q119", "DEALS_MOD",f"Get all deals for {COMPANY}"),
    ("Q120", "DEALS_MOD",f"Get open deals for {COMPANY}"),
    ("Q121", "DEALS_MOD",f"Get deal stages for {COMPANY}"),
    ("Q122", "DEALS_MOD",f"Get deal owners for {COMPANY}"),
    ("Q123", "DEALS_MOD",f"Get total deal amount for {COMPANY}"),
    ("Q124", "DEALS_MOD",f"Get deal value in USD for {COMPANY}"),
    ("Q125", "DEALS_MOD",f"Get amount for {DEAL}"),
    ("Q126", "DEALS_MOD",f"Get stage details for {DEAL}"),
    ("Q127", "DEALS_MOD","Get deals in Closed Won"),
    ("Q128", "DEALS_MOD","Get deals in Closed Lost"),
    ("Q129", "DEALS_MOD","Get highest value deal"),
    ("Q130", "DEALS_MOD","Get lowest value deal"),
    ("Q131", "DEALS_MOD","Get overdue deals"),
    ("Q132", "DEALS_MOD","Get upcoming closing deals"),

    # ── CONTACTS MODULE ────────────────────────────────────────────────────
    ("Q133", "CONTACT_MOD",f"Get contact details for {CONTACT}"),
    ("Q134", "CONTACT_MOD",f"Get contact email for {CONTACT}"),
    ("Q135", "CONTACT_MOD",f"Get contact phone for {CONTACT}"),
    ("Q136", "CONTACT_MOD",f"Get contact owner for {CONTACT}"),
    ("Q137", "CONTACT_MOD",f"Get contact account for {CONTACT}"),
    ("Q138", "CONTACT_MOD","Get contact creation date"),
    ("Q139", "CONTACT_MOD",f"Get last activity for {CONTACT}"),
    ("Q140", "CONTACT_MOD","Get contacts by owner"),
    ("Q141", "CONTACT_MOD","Get contacts by source"),
    ("Q142", "CONTACT_MOD",f"Get contacts by region {REGION}"),
    ("Q143", "CONTACT_MOD","Get contacts by country India"),
    ("Q144", "CONTACT_MOD","Get contacts by lead status"),
    ("Q145", "CONTACT_MOD","Get contacts created after 2026-01-01"),

    # ── TASKS MODULE ───────────────────────────────────────────────────────
    ("Q146", "TASKS_MOD",f"Get tasks owned by {USER}"),
    ("Q147", "TASKS_MOD",f"Get tasks with priority {PRIORITY}"),
    ("Q148", "TASKS_MOD",f"Get tasks with status {TASK_STATUS}"),
    ("Q149", "TASKS_MOD",f"Get tasks due before 2025-12-31"),
    ("Q150", "TASKS_MOD",f"Get tasks due after 2025-01-01"),
    ("Q151", "TASKS_MOD","Get tasks created before 2026-01-01"),
    ("Q152", "TASKS_MOD","Get tasks created after 2025-01-01"),
    ("Q153", "TASKS_MOD",f"Get tasks associated with {COMPANY}"),
    ("Q154", "TASKS_MOD",f"Get tasks associated with {CONTACT}"),
    ("Q155", "TASKS_MOD","Get overdue tasks"),
    ("Q156", "TASKS_MOD","Get count of tasks by status"),

    # ── INVOICE MODULE ─────────────────────────────────────────────────────
    ("Q157", "INV_MOD",  f"Get invoice details {INV_PAID}"),
    ("Q158", "INV_MOD",  f"Get invoice status for {INV_PAID}"),
    ("Q159", "INV_MOD",  f"Get invoice date for {INV_PAID}"),
    ("Q160", "INV_MOD",  f"Get invoice due date for {INV_PAID}"),
    ("Q161", "INV_MOD",  f"Get invoice amount for {INV_PAID}"),
    ("Q162", "INV_MOD",  f"Get remaining amount for {INV_DRAFT}"),
    ("Q163", "INV_MOD",  f"Get payment history for {INV_PAID}"),
    ("Q164", "INV_MOD",  "Get invoices by status paid"),
    ("Q165", "INV_MOD",  "Get overdue invoices"),
    ("Q166", "INV_MOD",  "Get invoices due this week"),
    ("Q167", "INV_MOD",  "Get invoices pending payment"),
    ("Q168", "INV_MOD",  "Get invoices above amount 5000"),
    ("Q169", "INV_MOD",  "Get total invoiced amount"),
    ("Q170", "INV_MOD",  "Get total remaining amount"),
]

WAIT_BETWEEN_S = 10


# ── Validation checks (same 16 as before) ─────────────────────────────────

def check_01_summary(response):
    lines = [l.strip() for l in response.split("\n") if l.strip()]
    return (True, f"{len(lines)} lines") if len(lines) >= 3 else (False, f"Only {len(lines)} line(s)")

def check_02_ui_format(response):
    if response.strip().startswith("{") or response.strip().startswith("["):
        try:
            json.loads(response.strip()); return (False, "Raw JSON")
        except json.JSONDecodeError:
            pass
    if re.search(r"ObjectId\(|ISODate\(", response): return (False, "Raw BSON")
    if len(response.strip()) < 10: return (False, "Too short")
    return (True, "Clean text")

def check_03_performance(timing):
    return (True, f"{timing:.1f}s") if timing <= 30 else (False, f"{timing:.1f}s > 30s")

def check_04_intent(response, query, collection):
    if any(k in query.lower() for k in ["deal","invoice","company","contact","task","revenue",
            "sales","lead","target","overdue","pipeline","stage","user","product","outreach"]):
        if "not able to help with general knowledge" in response.lower():
            return (False, "Out-of-scope misclassification")
    return (True, f"→ {collection or 'N/A'}")

def check_05_chat_history(response): return (True, "N/A")

def check_06_semantic_relevance(response, query):
    stop = {"the","a","an","of","for","to","in","on","and","or","by","get","give",
            "show","list","what","which","me","all","is","are","this","details"}
    q_tok = {t for t in re.findall(r"[a-z0-9]{3,}", query.lower()) if t not in stop}
    r_tok = {t for t in re.findall(r"[a-z0-9]{3,}", response.lower()) if t not in stop}
    if not q_tok: return (True, "N/A")
    overlap = len(q_tok & r_tok) / max(1, len(q_tok))
    if overlap >= 0.25 or "could not find" in response.lower() or "no results" in response.lower():
        return (True, f"{overlap*100:.0f}% overlap")
    return (False, f"Low overlap {overlap*100:.0f}%")

def check_07_rag(response):
    return (False, "Pipeline error") if "i encountered an error" in response.lower() else (True, "OK")

def check_08_efficiency(timing, qid):
    simple = {"Q019","Q035","Q036","Q041","Q127","Q128","Q155","Q156","Q165","Q167"}
    if qid in simple:
        return (True, f"{timing:.1f}s fast") if timing <= 15 else (False, f"{timing:.1f}s > 15s")
    return (True, f"{timing:.1f}s")

def check_09_tool_usage(collection, query):
    return (True, f"{collection or 'N/A'}")

def check_10_search(response, query):
    r = response.lower()
    has = any(c.isdigit() for c in response) or any(
        w in r for w in ["found","total","count","result","record","deal","invoice",
                         "company","contact","task","lead","n/a","no "])
    return (True, "Data present") if has else (False, "No data/not-found")

def check_11_complexity(response, query):
    ok = ("|" in response or
          any(response.lstrip().startswith(p) for p in ["1.","2.","-","*","#"]) or
          re.search(r"\b\d+\b", response) or
          any(w in response for w in ["Summary","Total","Found","Count","N/A"]))
    return (True, "Structured") if ok else (False, "Unstructured")

def check_12_limits(response, query):
    q = query.lower()
    _TEMPORAL = re.compile(r"\s*(?:month|week|day|year|hour|minute)s?\b")
    for pat in [r"top\s+(\d+)", r"last\s+(\d+)", r"(\d+)\s+contacts?"]:
        m = re.search(pat, q)
        if m:
            # Skip if the number is immediately followed by a temporal unit
            # e.g. "last 6 months" is a date range, not a row-count limit
            if _TEMPORAL.match(q, m.end(1)):
                continue
            n = int(m.group(1))
            rows = re.findall(r"^\d+\.", response, re.MULTILINE)
            if rows and len(rows) > n:
                return (False, f"Got {len(rows)}, expected ≤{n}")
            return (True, f"≤{n} respected")
    return (True, "N/A")

def check_13_collection(collection, query):
    q = query.lower()
    domain = {"deal","invoice","company","contact","task","revenue","sales","lead",
              "target","overdue","pipeline","stage","product","outreach","user","order"}
    if not collection or collection in ("N/A","","None"):
        if any(w in q for w in domain):
            return (False, "Domain query, no collection")
    return (True, f"{collection or 'N/A'}")

def check_14_business_rules(response, query):
    if "revenue" in query.lower() and "error" in response.lower():
        return (False, "Revenue query errored")
    return (True, "Rules OK")

def check_15_cache(cache_hit):
    return (True, "HIT" if cache_hit else "MISS")

def check_16_masking(response):
    raw = re.findall(r'\b[0-9a-fA-F]{24}\b', response)
    return (False, f"Unmasked OID: {raw[:1]}") if raw else (True, "Masked OK")

def compute_score(checks, timing, response):
    base = sum(5 for p, _ in checks.values() if p)
    bonus = 0
    lines = [l for l in response.split("\n") if l.strip()]
    if len(lines) >= 5: bonus += 5
    if "|" in response: bonus += 5
    if timing <= 5: bonus += 5
    elif timing <= 15: bonus += 3
    if re.search(r"\$[\d,]+", response): bonus += 3
    return min(100, base + bonus)


# ── Runner ─────────────────────────────────────────────────────────────────

def run():
    print("=" * 70)
    print("  ELSNER ECRM — FULL QA TEST SUITE (ALL QUERIES)")
    print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Queries : {len(QUERIES)}")
    print("=" * 70)

    from agents.orchestrator import ChatOrchestrator
    from utils.circuit_breaker import get_llm_breaker
    get_llm_breaker().reset()

    orc = ChatOrchestrator()
    rows = []
    cat_scores: Dict[str, List[int]] = {}

    for idx, (qid, cat, query) in enumerate(QUERIES, 1):
        print(f"\n[{idx:03d}/{len(QUERIES)}] {qid} [{cat}]: {query}")
        print("-" * 65)

        t0 = time.time()
        try:
            res   = orc.process_query(query)
            resp  = (res.get("response") or "").strip()
            coll  = res.get("collection") or ""
            timing = float(res.get("timing") or 0)
            error = res.get("error") or ""
            cache_hit = any("Cache hit" in lg or "cache hit" in lg.lower()
                            for lg in res.get("logs", []))
        except Exception as exc:
            resp, coll, timing, error, cache_hit = f"EXCEPTION: {exc}", "", time.time()-t0, str(exc), False

        print(f"  Collection : {coll or '—'} | Time: {timing:.2f}s | Cache: {cache_hit}")
        print(f"  Response   : {resp[:100].replace(chr(10),' ')}...")

        checks = {
            "F01": check_01_summary(resp),
            "F02": check_02_ui_format(resp),
            "F03": check_03_performance(timing),
            "F04": check_04_intent(resp, query, coll),
            "F05": check_05_chat_history(resp),
            "F06": check_06_semantic_relevance(resp, query),
            "F07": check_07_rag(resp),
            "F08": check_08_efficiency(timing, qid),
            "F09": check_09_tool_usage(coll, query),
            "F10": check_10_search(resp, query),
            "F11": check_11_complexity(resp, query),
            "F12": check_12_limits(resp, query),
            "F13": check_13_collection(coll, query),
            "F14": check_14_business_rules(resp, query),
            "F15": check_15_cache(cache_hit),
            "F16": check_16_masking(resp),
        }

        passed_all = all(p for p, _ in checks.values())
        score = compute_score(checks, timing, resp)
        cat_scores.setdefault(cat, []).append(score)

        failed = ", ".join(f"{k}({n})" for k,(p,n) in checks.items() if not p) or "none"
        chk_str = " ".join(f"{'✓' if p else '✗'}{k[1:]}" for k,(p,_) in checks.items())
        print(f"  Checks : {chk_str}")
        print(f"  Score  : {score}/100  |  Pass: {passed_all}")

        rows.append({
            "qid": qid, "category": cat, "query": query,
            "full_response": resp.replace("\n", " | ")[:500],
            "collection_used": coll or "N/A",
            "accuracy_score": score,
            "response_time_seconds": round(timing, 2),
            "cache_hit": cache_hit,
            "passed_all_checks": passed_all,
            "failed_checks": failed,
        })

        if idx < len(QUERIES):
            print(f"  Waiting {WAIT_BETWEEN_S}s...")
            time.sleep(WAIT_BETWEEN_S)

    # ── Save CSV ───────────────────────────────────────────────────────────
    out = Path("logs"); out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"full_qa_{stamp}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "qid","category","query","full_response","collection_used",
            "accuracy_score","response_time_seconds","cache_hit",
            "passed_all_checks","failed_checks"])
        w.writeheader(); w.writerows(rows)

    # ── Final Report ───────────────────────────────────────────────────────
    total   = len(rows)
    scores  = [r["accuracy_score"] for r in rows]
    times   = sorted(r["response_time_seconds"] for r in rows)
    hits    = sum(1 for r in rows if r["cache_hit"])
    passed  = sum(1 for r in rows if r["passed_all_checks"])
    over30  = sum(1 for t in times if t > 30)

    print(f"\n\nCSV saved → {csv_path}")
    print("\n" + "=" * 70)
    print("  FINAL METRICS")
    print("=" * 70)
    print(f"  Total queries       : {total}")
    print(f"  Avg accuracy score  : {sum(scores)/total:.1f} / 100")
    print(f"  Min / Max score     : {min(scores)} / {max(scores)}")
    print(f"  Scores = 100        : {sum(1 for s in scores if s==100)}")
    print(f"  Scores >= 90        : {sum(1 for s in scores if s>=90)}")
    print(f"  Scores >= 80        : {sum(1 for s in scores if s>=80)}")
    print(f"  All 16 checks pass  : {passed}/{total}")
    print(f"  Avg response time   : {sum(times)/total:.2f}s")
    print(f"  P50 / P95 time      : {times[total//2]:.2f}s / {times[int(0.95*total)]:.2f}s")
    print(f"  Max response time   : {max(times):.2f}s")
    print(f"  Queries > 30s       : {over30}")
    print(f"  Cache hits          : {hits}/{total} ({hits/total*100:.0f}%)")

    print("\n  SCORE BY CATEGORY:")
    for cat, sc in sorted(cat_scores.items()):
        print(f"    {cat:<14} avg={sum(sc)/len(sc):.1f}  queries={len(sc)}")

    avg = sum(scores)/total
    green = avg >= 80 and over30 == 0 and (total - passed) / total <= 0.25
    print("\n" + "=" * 70)
    print("  🟢 GREEN FLAG: PRODUCTION READY" if green else "  🔴 RED FLAG: NEEDS FIXES")
    print("=" * 70)


if __name__ == "__main__":
    run()
