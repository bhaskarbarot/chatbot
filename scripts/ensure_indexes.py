#!/usr/bin/env python3
"""
MongoDB Index Maintenance Script.

Creates all indexes needed for the ECRM chatbot query patterns.
Safe to run repeatedly — uses ensure_index which is idempotent.

Usage:
    python scripts/ensure_indexes.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pymongo import ASCENDING as ASC, DESCENDING as DESC, TEXT
from pymongo.errors import PyMongoError

from tools.mongodb_tool import get_db
from utils.logger import get_logger

logger = get_logger("ensure_indexes")

# ── Index definitions ────────────────────────────────
# Each entry: (collection, [(field, direction), ...], options_dict)
# Background=True avoids locking the collection during creation.

INDEXES: List[Tuple[str, list, dict]] = [
    # ── deals ────────────────────────────────────────
    ("deals", [("deleted", ASC), ("dealWonAt", ASC), ("dealLostAt", ASC)], {}),
    ("deals", [("deleted", ASC), ("stage", ASC), ("createdAt", DESC)], {}),
    ("deals", [("deleted", ASC), ("owner", ASC), ("createdAt", DESC)], {}),
    ("deals", [("deleted", ASC), ("company", ASC)], {}),
    ("deals", [("deleted", ASC), ("closeDate", DESC)], {}),
    ("deals", [("deleted", ASC), ("createdAt", DESC)], {}),

    # ── invoices ─────────────────────────────────────
    ("invoices", [("deleted", ASC), ("payment_status", ASC), ("invoice_date", DESC)], {}),
    ("invoices", [("deleted", ASC), ("payment_status", ASC), ("due_date", ASC)], {}),
    ("invoices", [("deleted", ASC), ("company", ASC), ("invoice_date", DESC)], {}),
    ("invoices", [("deleted", ASC), ("sales_person", ASC), ("invoice_date", DESC)], {}),
    ("invoices", [("deleted", ASC), ("invoiceOwner", ASC)], {}),

    # ── companies ────────────────────────────────────
    ("companies", [("deleted", ASC), ("lifecycleStage", ASC)], {}),
    ("companies", [("deleted", ASC), ("companyOwner", ASC)], {}),
    ("companies", [("deleted", ASC), ("region", ASC)], {}),
    ("companies", [("companyName", TEXT)], {"default_language": "english"}),

    # ── contacts ─────────────────────────────────────
    ("contacts", [("deleted", ASC), ("company", ASC)], {}),
    ("contacts", [("deleted", ASC), ("contactOwner", ASC)], {}),
    ("contacts", [("firstName", ASC), ("lastName", ASC)], {}),

    # ── createtasks ──────────────────────────────────
    ("createtasks", [("deleted", ASC), ("status", ASC), ("due_date", ASC)], {}),
    ("createtasks", [("deleted", ASC), ("createdBy", ASC), ("due_date", ASC)], {}),
    ("createtasks", [("deleted", ASC), ("dealsId", ASC)], {}),

    # ── outreaches ───────────────────────────────────
    ("outreaches", [("isDeleted", ASC), ("leadStatus", ASC)], {}),
    ("outreaches", [("isDeleted", ASC), ("assignedTo", ASC)], {}),
    ("outreaches", [("isDeleted", ASC), ("campaign", ASC)], {}),

    # ── targets ──────────────────────────────────────
    ("targets", [("userId", ASC), ("year", ASC), ("month", ASC)], {"unique": True}),

    # ── sales ────────────────────────────────────────
    ("sales", [("deleted", ASC), ("salesOwner", ASC), ("sales_date", DESC)], {}),
    ("sales", [("deleted", ASC), ("company", ASC)], {}),

    # ── bills ────────────────────────────────────────
    ("bills", [("vendor", ASC), ("status", ASC)], {}),
    ("bills", [("dueDate", ASC), ("status", ASC)], {}),

    # ── users ────────────────────────────────────────
    ("users", [("isActive", ASC), ("name", ASC)], {}),
    ("users", [("email", ASC)], {"unique": True, "sparse": True}),

    # ── meetings ─────────────────────────────────────
    ("meetings", [("createdBy", ASC), ("start", DESC)], {}),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print index plans without creating them")
    args = parser.parse_args()

    db = get_db()
    created = 0
    skipped = 0
    failed = 0

    for collection, key_spec, options in INDEXES:
        idx_name = "_".join(f"{f}_{d}" for f, d in key_spec)
        options["background"] = True
        options["name"] = idx_name[:64]  # MongoDB index name limit

        if args.dry_run:
            print(f"[DRY-RUN] {collection}: {key_spec}  opts={options}")
            continue

        try:
            existing = {idx["name"] for idx in db[collection].list_indexes()}
            if options["name"] in existing:
                print(f"  EXISTS  {collection}.{options['name']}")
                skipped += 1
                continue

            db[collection].create_index(key_spec, **options)
            print(f"  CREATED {collection}.{options['name']}")
            created += 1
        except PyMongoError as exc:
            print(f"  FAILED  {collection}.{options['name']}: {exc}")
            failed += 1

    if not args.dry_run:
        print(f"\nDone — created: {created}, skipped: {skipped}, failed: {failed}")


if __name__ == "__main__":
    main()
