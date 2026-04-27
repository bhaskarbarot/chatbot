"""
Fast regex-based intent pre-classifier.

Runs BEFORE vector search and LLM planning.  For high-confidence, well-defined
intents it generates a query plan directly, skipping the LLM entirely.

Performance impact:
  - Simple count/list queries: saves ~3–8 s of LLM planning
  - Detection is < 1 ms (pure regex, no model)
  - Only activates for confidence >= CONFIDENCE_THRESHOLD (avoids false positives)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional, Tuple

CONFIDENCE_THRESHOLD = 0.75


class Intent(str, Enum):
    COUNT = "count"
    LIST_RECENT = "list_recent"
    REVENUE_TOTAL = "revenue_total"
    OVERDUE_INVOICES = "overdue_invoices"
    OVERDUE_TASKS = "overdue_tasks"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"
    PIPELINE_BY_STAGE = "pipeline_by_stage"
    PENDING_TASKS = "pending_tasks"
    PERSON_LOOKUP = "person_lookup"
    UNKNOWN = "unknown"


# Each pattern set maps to an (Intent, weight) tuple.
# Higher weight = stronger signal.
_PATTERNS: List[Tuple[re.Pattern, Intent, float]] = [
    (re.compile(r"\bhow\s+many\b|\bnumber\s+of\b|\btotal\s+number\b", re.I), Intent.COUNT, 1.0),
    (re.compile(r"\brevenue\b.*\b(total|sum|this\s+year|financial|fy)\b"
                r"|\b(total|sum)\b.*\brevenue\b", re.I), Intent.REVENUE_TOTAL, 1.0),
    (re.compile(r"\boverdue\b.*\b(invoices?|billing|payment)\b"
                r"|\b(invoices?|billing|payment)\b.*\boverdue\b"
                r"|\bpast\s+due\b", re.I), Intent.OVERDUE_INVOICES, 1.0),
    (re.compile(r"\boverdue\b.*\btasks?\b|\btasks?\b.*\boverdue\b"
                r"|\bfollow.?up.*overdue\b", re.I), Intent.OVERDUE_TASKS, 1.0),
    (re.compile(r"\bclosed\s+won\b|\bwon\s+deal\b|\bdeal.*\bwon\b", re.I), Intent.CLOSED_WON, 1.0),
    (re.compile(r"\bclosed\s+lost\b|\blost\s+deal\b|\bdeal.*\blost\b", re.I), Intent.CLOSED_LOST, 1.0),
    (re.compile(r"\bpipeline\b.*\bstage\b|\bby\s+stage\b|\bstage.?wise\b"
                r"|\bsales\s+pipeline\b", re.I), Intent.PIPELINE_BY_STAGE, 1.0),
    (re.compile(r"\bpending\s+tasks?\b|\btasks?.*\bpending\b|\bopen\s+tasks?\b", re.I), Intent.PENDING_TASKS, 1.0),
    (re.compile(r"^\s*who\s+is\s+[a-z]", re.I), Intent.PERSON_LOOKUP, 1.0),
    (re.compile(r"\b(show|list|give|get)\b.*\b(last|recent|latest)\s+\d+\b"
                r"|\blast\s+\d+\b.*\b(deals?|invoices?|contacts?|compan(?:y|ies)|tasks?|leads?)\b",
                re.I), Intent.LIST_RECENT, 0.8),
]

# Secondary signals that boost confidence when present alongside a primary match
_ENTITY_BOOST: Dict[Intent, re.Pattern] = {
    Intent.COUNT: re.compile(r"\b(deal|invoice|company|contact|task|lead|outreach)\b", re.I),
    Intent.REVENUE_TOTAL: re.compile(r"\b(invoice|paid|collection)\b", re.I),
    Intent.OVERDUE_INVOICES: re.compile(r"\b(invoice|payment|billing)\b", re.I),
    Intent.OVERDUE_TASKS: re.compile(r"\b(task|follow.?up|reminder)\b", re.I),
    Intent.CLOSED_WON: re.compile(r"\b(deal|opportunity|pipeline)\b", re.I),
    Intent.CLOSED_LOST: re.compile(r"\b(deal|opportunity|lost)\b", re.I),
    Intent.PIPELINE_BY_STAGE: re.compile(r"\b(deal|opportunity|stage)\b", re.I),
    Intent.PENDING_TASKS: re.compile(r"\b(task|to.?do|follow.?up)\b", re.I),
}

# Intent → likely primary collection
_COLLECTION_MAP: Dict[Intent, str] = {
    Intent.COUNT: "",           # resolved dynamically
    Intent.LIST_RECENT: "",     # resolved dynamically
    Intent.REVENUE_TOTAL: "invoices",
    Intent.OVERDUE_INVOICES: "invoices",
    Intent.OVERDUE_TASKS: "createtasks",
    Intent.CLOSED_WON: "deals",
    Intent.CLOSED_LOST: "deals",
    Intent.PIPELINE_BY_STAGE: "deals",
    Intent.PENDING_TASKS: "createtasks",
    Intent.PERSON_LOOKUP: "users",
}

# Intent → entity keyword for COUNT/LIST disambiguation
_ENTITY_TO_COLLECTION: Dict[str, str] = {
    "deal": "deals", "deals": "deals",
    "invoice": "invoices", "invoices": "invoices",
    "company": "companies", "companies": "companies",
    "contact": "contacts", "contacts": "contacts",
    "task": "createtasks", "tasks": "createtasks",
    "lead": "outreaches", "leads": "outreaches",
    "outreach": "outreaches", "outreaches": "outreaches",
    "user": "users", "users": "users",
    "sales": "sales",
    "meeting": "meetings", "meetings": "meetings",
    "product": "products", "products": "products",
    "bill": "bills", "bills": "bills",
    "vendor": "vendors", "vendors": "vendors",
    "target": "targets", "targets": "targets",
    # Lookup / reference collections
    "department": "departments", "departments": "departments",
    "technology": "technologies", "technologies": "technologies",
    "source": "sources", "sources": "sources",
    "region": "regions", "regions": "regions",
    "campaign": "campaigns", "campaigns": "campaigns",
    "tax": "taxes", "taxes": "taxes",
    "payment": "payments", "payments": "payments",
}


def classify(query: str) -> Tuple[Intent, float]:
    """
    Returns (intent, confidence ∈ [0, 1]).
    Only high-confidence results should be used for LLM bypass.
    """
    q = query.strip()
    best_intent = Intent.UNKNOWN
    best_conf = 0.0

    for pattern, intent, base_weight in _PATTERNS:
        if pattern.search(q):
            confidence = base_weight
            # Secondary entity boost
            boost_pat = _ENTITY_BOOST.get(intent)
            if boost_pat and boost_pat.search(q):
                confidence = min(confidence + 0.15, 1.0)
            if confidence > best_conf:
                best_conf = confidence
                best_intent = intent

    return best_intent, best_conf


def get_collection(intent: Intent, query: str) -> Optional[str]:
    """
    Return the primary MongoDB collection for a classified intent.
    For generic COUNT/LIST intents, extracts entity from the query text.
    """
    collection = _COLLECTION_MAP.get(intent)
    if collection:
        return collection

    # For COUNT and LIST_RECENT, determine collection from entity keyword
    q_lower = query.lower()
    for keyword, coll in _ENTITY_TO_COLLECTION.items():
        if re.search(rf"\b{re.escape(keyword)}\b", q_lower):
            return coll

    return None


def build_fast_plan(intent: Intent, collection: str, query: str) -> Optional[Dict]:
    """
    Build a query plan for well-known intents WITHOUT calling the LLM.
    Returns None if the intent cannot be handled without LLM context.
    """
    q = query.lower()

    if intent == Intent.COUNT:
        return {
            "type": "count",
            "collection": collection,
            "filter": {},
            "response_hint": "count",
        }

    if intent == Intent.LIST_RECENT:
        m = re.search(r"last\s+(\d+)|recent\s+(\d+)|(\d+)\s+recent", q)
        limit = int(m.group(1) or m.group(2) or m.group(3)) if m else 10
        return {
            "type": "find",
            "collection": collection,
            "filter": {},
            "sort": [["createdAt", -1]],
            "limit": min(limit, 50),
            "response_hint": "list",
        }

    if intent == Intent.REVENUE_TOTAL:
        from datetime import datetime
        filter_doc: Dict = {"payment_status": "paid", "deleted": False}
        _apply_temporal_filter(filter_doc, "invoice_date", q)
        return {
            "type": "aggregate",
            "collection": "invoices",
            "pipeline": [
                {"$match": filter_doc},
                {"$group": {
                    "_id": None,
                    "total_revenue_usd": {"$sum": "$grandtotal_in_usd"},
                    "invoice_count": {"$sum": 1},
                }},
            ],
            "response_hint": "summary",
        }

    if intent == Intent.OVERDUE_INVOICES:
        from datetime import datetime
        now_iso = datetime.now().isoformat()
        return {
            "type": "find",
            "collection": "invoices",
            "filter": {
                "payment_status": {"$in": ["draft", "confirmed", "partial_payment"]},
                "due_date": {"$lt": now_iso},
                "deleted": False,
            },
            "sort": [["due_date", 1]],
            "limit": 50,
            "response_hint": "list",
        }

    if intent == Intent.OVERDUE_TASKS:
        from datetime import datetime
        now_iso = datetime.now().isoformat()
        return {
            "type": "find",
            "collection": "createtasks",
            "filter": {
                "status": {"$in": ["Pending", "pending", "Open", "open"]},
                "due_date": {"$lt": now_iso},
                "deleted": False,
            },
            "sort": [["due_date", 1]],
            "limit": 50,
            "response_hint": "list",
        }

    if intent == Intent.CLOSED_WON:
        filter_doc = {"dealWonAt": {"$ne": None}, "dealLostAt": None, "deleted": False}
        _apply_temporal_filter(filter_doc, "dealWonAt", q)
        return {
            "type": "find",
            "collection": "deals",
            "filter": filter_doc,
            "sort": [["dealWonAt", -1]],
            "limit": 50,
            "response_hint": "list",
        }

    if intent == Intent.CLOSED_LOST:
        filter_doc = {"dealLostAt": {"$ne": None}, "deleted": False}
        _apply_temporal_filter(filter_doc, "dealLostAt", q)
        return {
            "type": "find",
            "collection": "deals",
            "filter": filter_doc,
            "sort": [["dealLostAt", -1]],
            "limit": 50,
            "response_hint": "list",
        }

    if intent == Intent.PIPELINE_BY_STAGE:
        return {
            "type": "aggregate",
            "collection": "deals",
            "pipeline": [
                {"$match": {"deleted": False, "dealWonAt": None, "dealLostAt": None}},
                {"$group": {
                    "_id": "$stage",
                    "count": {"$sum": 1},
                    "total_value": {"$sum": "$grand_total_in_usd"},
                }},
                {"$sort": {"count": -1}},
            ],
            "response_hint": "table",
        }

    if intent == Intent.PENDING_TASKS:
        return {
            "type": "find",
            "collection": "createtasks",
            "filter": {
                "status": {"$in": ["Pending", "pending", "Open", "open"]},
                "deleted": False,
            },
            "sort": [["due_date", 1]],
            "limit": 50,
            "response_hint": "list",
        }

    return None


# ── Internal helpers ──────────────────────────────────

def _apply_temporal_filter(filter_doc: Dict, field: str, query_lower: str) -> None:
    """Add date range filter in-place based on temporal keywords."""
    from datetime import datetime, timedelta
    now = datetime.now()

    if "this month" in query_lower:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        filter_doc[field] = {"$gte": start.isoformat(), "$lte": now.isoformat()}
    elif "last month" in query_lower:
        start_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_of_last = start_of_this - timedelta(microseconds=1)
        start_of_last = end_of_last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        filter_doc[field] = {"$gte": start_of_last.isoformat(), "$lte": end_of_last.isoformat()}
    elif "this year" in query_lower or "financial year" in query_lower:
        # Indian FY: Apr 1
        year = now.year if now.month >= 4 else now.year - 1
        start = datetime(year, 4, 1)
        filter_doc[field] = {"$gte": start.isoformat(), "$lte": now.isoformat()}
    else:
        m = re.search(r"last\s+(\d+)\s+(day|week|month|year)s?", query_lower)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {"day": timedelta(days=n), "week": timedelta(weeks=n),
                     "month": timedelta(days=30 * n), "year": timedelta(days=365 * n)}
            start = now - delta.get(unit, timedelta(days=30))
            filter_doc[field] = {"$gte": start.isoformat(), "$lte": now.isoformat()}
