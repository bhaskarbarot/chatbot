#!/usr/bin/env python3
"""
Benchmark harness for ECRM chatbot.
Runs a query suite and records latency, routing, and response quality signals.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.orchestrator import ChatOrchestrator


QUERY_SUITE: List[Tuple[str, str]] = [
    ("Q04", "Show me the top 10 customers by revenue."),
    ("Q05", "Give me closed won deals"),
    ("Q06", "List new leads from this week."),
    ("Q07", "Show me the total sales for last month."),
    ("C01", "top performing sales owners in last 6 months with monthly trend"),
    ("C02", "funnel performance and conversion to closed won by owner"),
    ("C03", "overdue invoice aging and outstanding amount by company"),
    ("C04", "high activity companies with weak payment conversion"),
    ("C05", "best product and project type combinations by value avg deal size and win rate"),
    ("R02", "total revenue this financial year"),
    ("R05", "show me the total sales for last month"),
    ("R06", "total sales for last month"),
    ("R07", "pending invoices"),
    ("R08", "overdue payments"),
    ("R09", "total collection"),
    ("R10", "last 5 contacts details?"),
    ("R11", "new leads this week"),
    ("R12", "new leads this month"),
    ("R13", "which leads have not been contacted in 7 days"),
    ("R14", "conversion rate from lead to customer"),
    ("R15", "which customers have not given business in 3 months"),
    ("R16", "closed won deals"),
    ("R17", "give me all deals"),
    ("R18", "sales pipeline by stage"),
    ("R19", "which deals are stuck in the proposal stage"),
    ("R20", "opportunities lost in July"),
    ("R21", "lost deals last quarter"),
    ("R22", "which sales rep closed the most deals this quarter"),
    ("R23", "what tasks are pending for the sales team"),
    ("R24", "pending tasks"),
    ("R25", "any follow-ups overdue this week"),
]


def _score_response(query: str, response: str, collection: str, timing: float, error: str | None) -> str:
    """Heuristic quality rubric for regression tracking."""
    if error:
        return "fail"
    response_lower = (response or "").lower()
    if "could not find any results" in response_lower and any(
        keyword in query.lower() for keyword in ["show", "list", "total", "closed", "pending", "sales", "revenue"]
    ):
        return "low"
    if "error" in response_lower:
        return "fail"
    if collection in {"N/A", "", None}:
        return "low"
    if timing > 20:
        return "medium"
    if re.search(r"\b(total|count|rate|score|table|stage|pending|closed)\b", response_lower):
        return "high"
    return "medium"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run chatbot benchmark query suite.")
    parser.add_argument("--sample-size", type=int, default=10, help="Number of random queries to run.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument("--full-suite", action="store_true", help="Run full query suite.")
    args = parser.parse_args()

    if args.full_suite:
        selected = QUERY_SUITE
    else:
        selected = random.Random(args.seed).sample(QUERY_SUITE, min(args.sample_size, len(QUERY_SUITE)))

    orchestrator = ChatOrchestrator()
    rows: List[Dict] = []
    for qid, query in selected:
        result = orchestrator.process_query(query)
        response = (result.get("response") or "").strip()
        timing = float(result.get("timing") or 0.0)
        collection = result.get("collection") or "N/A"
        error = result.get("error")
        quality = _score_response(query, response, collection, timing, error)
        rows.append({
            "id": qid,
            "query": query,
            "collection": collection,
            "processing_time_s": round(timing, 3),
            "quality": quality,
            "error": error or "",
            "response_preview": response.replace("\n", " | ")[:260],
        })

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("logs")
    out_dir.mkdir(exist_ok=True)
    json_path = out_dir / f"benchmark_{stamp}.json"
    csv_path = out_dir / f"benchmark_{stamp}.csv"

    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "id", "query", "collection", "processing_time_s",
                "quality", "error", "response_preview",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    high = len([r for r in rows if r["quality"] == "high"])
    medium = len([r for r in rows if r["quality"] == "medium"])
    low = len([r for r in rows if r["quality"] == "low"])
    fail = len([r for r in rows if r["quality"] == "fail"])
    avg_time = round(sum(r["processing_time_s"] for r in rows) / total, 3) if total else 0.0
    p95 = sorted(r["processing_time_s"] for r in rows)[max(0, int(0.95 * total) - 1)] if total else 0.0

    print(f"Benchmark complete: {total} queries")
    print(f"Quality counts -> high: {high}, medium: {medium}, low: {low}, fail: {fail}")
    print(f"Latency -> avg: {avg_time}s, p95: {p95}s")
    print(f"Saved: {json_path}")
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
