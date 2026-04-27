#!/usr/bin/env python3
"""
Random topic/module query tester for queries.txt.

- Parses section headers from queries.txt
- Samples 2-3 random queries per section
- Executes each query via ChatOrchestrator
- Writes CSV with:
  query, response_full, processing_time_s, collection_used, confidence, accuracy
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.orchestrator import ChatOrchestrator


SECTION_SEPARATOR = "=" * 10


@dataclass
class Row:
    topic: str
    query: str
    response_full: str
    processing_time_s: float
    collection_used: str
    confidence: float
    accuracy: str
    error: str


def _is_header_line(line: str) -> bool:
    if not line:
        return False
    if line.startswith(SECTION_SEPARATOR):
        return False
    if "[" in line and "]" in line:
        return False
    if line.endswith(":"):
        return True
    letters = re.sub(r"[^A-Za-z ]", "", line)
    if not letters.strip():
        return False
    # "LEADS & CUSTOMERS", "INVOICE MODULE", etc.
    return line.upper() == line and len(line.split()) <= 6


def parse_topics(path: Path) -> Dict[str, List[str]]:
    topics: Dict[str, List[str]] = {}
    current = "GENERAL"
    topics[current] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(SECTION_SEPARATOR):
            continue
        if _is_header_line(line):
            current = line.rstrip(":").strip()
            topics.setdefault(current, [])
            continue
        topics.setdefault(current, []).append(line)

    # Remove empty buckets
    return {k: v for k, v in topics.items() if v}


def extract_confidence(logs: List[str]) -> float:
    for entry in logs:
        if "Retrieval confidence" in entry:
            m = re.search(r"top=([0-9]*\.?[0-9]+)", entry)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    return 0.0
    return 0.0


def score_accuracy(query: str, response: str, collection: str, err: str, t: float) -> str:
    if err:
        return "fail"
    r = (response or "").lower()
    q = query.lower()
    if "could not find any results" in r and any(
        token in q for token in ["show", "list", "give", "total", "pending", "overdue", "closed", "deals", "invoices"]
    ):
        return "low"
    if collection in {"", "N/A"}:
        return "low"
    if t > 20:
        return "medium"
    return "high"


def sample_queries(topics: Dict[str, List[str]], per_topic: int, seed: int) -> List[Tuple[str, str]]:
    rng = random.Random(seed)
    selected: List[Tuple[str, str]] = []
    for topic, queries in topics.items():
        k = min(per_topic, len(queries))
        for q in rng.sample(queries, k):
            selected.append((topic, q))
    return selected


def run_once(orchestrator: ChatOrchestrator, topic: str, query: str) -> Row:
    result = orchestrator.process_query(query)
    response = (result.get("response") or "").strip()
    timing = float(result.get("timing") or 0.0)
    collection = result.get("collection") or "N/A"
    err = result.get("error") or ""
    confidence = extract_confidence(result.get("logs") or [])
    accuracy = score_accuracy(query, response, collection, err, timing)
    return Row(
        topic=topic,
        query=query,
        response_full=response,
        processing_time_s=round(timing, 3),
        collection_used=collection,
        confidence=round(confidence, 3),
        accuracy=accuracy,
        error=err,
    )


def write_csv(rows: List[Row], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "topic",
                "query",
                "response_full",
                "processing_time_s",
                "collection_used",
                "confidence",
                "accuracy",
                "error",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Random topic query tester from queries.txt")
    parser.add_argument("--queries-file", default="../queries.txt", help="Path to queries.txt")
    parser.add_argument("--per-topic", type=int, default=2, choices=[2, 3], help="Random samples per topic")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--retry-failed", action="store_true", help="Retry only failed/low rows once")
    args = parser.parse_args()

    queries_path = Path(args.queries_file).resolve()
    topics = parse_topics(queries_path)
    selected = sample_queries(topics, args.per_topic, args.seed)

    orchestrator = ChatOrchestrator()
    rows = [run_once(orchestrator, topic, query) for topic, query in selected]

    if args.retry_failed:
        retry_rows: List[Row] = []
        for row in rows:
            if row.accuracy in {"fail", "low"}:
                retry_rows.append(run_once(orchestrator, row.topic, row.query))
            else:
                retry_rows.append(row)
        rows = retry_rows

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("logs")
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"topic_random_test_{stamp}.csv"
    write_csv(rows, csv_path)

    total = len(rows)
    high = len([r for r in rows if r.accuracy == "high"])
    medium = len([r for r in rows if r.accuracy == "medium"])
    low = len([r for r in rows if r.accuracy == "low"])
    fail = len([r for r in rows if r.accuracy == "fail"])
    avg_time = round(sum(r.processing_time_s for r in rows) / total, 3) if total else 0.0
    accuracy_pct = round(((high + medium) / total) * 100.0, 2) if total else 0.0

    print(f"Topics: {len(topics)} | Queries tested: {total}")
    print(f"Quality -> high={high}, medium={medium}, low={low}, fail={fail}")
    print(f"Overall accuracy: {accuracy_pct}%")
    print(f"Avg time: {avg_time}s")
    print(f"Saved CSV: {csv_path}")


if __name__ == "__main__":
    main()
