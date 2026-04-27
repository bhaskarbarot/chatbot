#!/usr/bin/env python3
"""
Benchmark all parsed rule intents through orchestrator.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.orchestrator import ChatOrchestrator


def load_rule_intents(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as fh:
        rules = json.load(fh)
    intents = []
    for item in rules:
        intent = (item.get("intent") or "").strip()
        if intent:
            intents.append(intent)
    return intents


def score(row: dict) -> str:
    if row["error"]:
        return "fail"
    if row["collection"] in {"", "N/A", None}:
        return "low"
    text = (row["response_preview"] or "").lower()
    if "could not find any results" in text:
        return "low"
    if row["time_s"] > 20:
        return "medium"
    return "high"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=0, help="Optional cap on number of intents")
    args = parser.parse_args()

    intents = load_rule_intents(Path("knowledge/parsed_rules.json"))
    if args.max > 0:
        intents = intents[:args.max]

    orchestrator = ChatOrchestrator()
    rows = []
    for idx, query in enumerate(intents, start=1):
        result = orchestrator.process_query(query)
        row = {
            "idx": idx,
            "query": query,
            "collection": result.get("collection") or "N/A",
            "time_s": float(result.get("timing") or 0.0),
            "error": result.get("error") or "",
            "response_preview": (result.get("response") or "").replace("\n", " | ")[:240],
        }
        row["quality"] = score(row)
        rows.append(row)

    out_dir = Path("logs")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"benchmark_all_rules_{stamp}.csv"

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["idx", "query", "collection", "time_s", "quality", "error", "response_preview"],
        )
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    high = len([r for r in rows if r["quality"] == "high"])
    medium = len([r for r in rows if r["quality"] == "medium"])
    low = len([r for r in rows if r["quality"] == "low"])
    fail = len([r for r in rows if r["quality"] == "fail"])
    avg_time = round(sum(r["time_s"] for r in rows) / total, 3) if total else 0.0
    print(f"Evaluated {total} intents -> high={high}, medium={medium}, low={low}, fail={fail}, avg={avg_time}s")
    print(f"Saved: {csv_path}")


if __name__ == "__main__":
    main()
