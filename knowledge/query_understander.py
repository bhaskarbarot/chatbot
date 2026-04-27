"""
Advanced Query Understanding Layer.

Extracts structured parameters from any natural-language query.
Zero hardcoding: all detection is regex/pattern-based, not entity-specific.

Outputs a QueryParams dataclass used throughout the pipeline to:
  - Route comparison/existence queries to dedicated handlers
  - Override LLM plan limits with user-specified N
  - Apply sort direction from "low/high/latest/oldest" phrasing
  - Detect which collections are likely relevant
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class QueryIntent(str, Enum):
    COUNT       = "count"
    LIST        = "list"
    DETAIL      = "detail"
    COMPARISON  = "comparison"
    PERCENTAGE  = "percentage"
    EXISTENCE   = "existence"
    AGGREGATION = "aggregation"
    SUMMARY     = "summary"
    REVENUE     = "revenue"
    UNKNOWN     = "unknown"


@dataclass
class QueryParams:
    intent: QueryIntent = QueryIntent.UNKNOWN
    # Dynamic limit — None means use system default
    limit: Optional[int] = None
    # Sort hints
    sort_field: Optional[str] = None
    sort_direction: int = -1          # -1=desc, 1=asc
    sort_reason: Optional[str] = None
    # Filters detected in query text
    filter_hints: Dict[str, Any] = field(default_factory=dict)
    # Entity names (full, unsplit)
    entity_names: List[str] = field(default_factory=list)
    # For comparison/percentage: the two groups being compared
    compare_entities: List[str] = field(default_factory=list)
    # For existence: name being checked
    existence_target: Optional[str] = None
    existence_entity_type: Optional[str] = None
    # Temporal context
    temporal_hint: Optional[str] = None
    # Probable primary collection(s)
    collection_hints: List[str] = field(default_factory=list)


def understand(query: str) -> QueryParams:
    """
    Parse a natural-language query into structured QueryParams.
    Call once per request; result drives routing decisions.
    """
    params = QueryParams()
    q = query.strip()
    ql = q.lower()

    params.intent           = _intent(ql)
    params.limit            = _limit(ql)
    params.sort_field, params.sort_direction, params.sort_reason = _sort(ql)
    params.filter_hints     = _filters(ql)
    params.entity_names     = _entity_names(q)
    params.temporal_hint    = _temporal(ql)
    params.collection_hints = _collection_hints(ql)

    if params.intent in (QueryIntent.COMPARISON, QueryIntent.PERCENTAGE):
        params.compare_entities = _compare_entities(ql)

    if params.intent == QueryIntent.EXISTENCE:
        params.existence_target, params.existence_entity_type = _existence_target(q, ql)

    return params


# ── Intent detection ──────────────────────────────────

def _intent(ql: str) -> QueryIntent:
    # Percentage / comparison with numbers
    if re.search(
        r'\bpercentage\b|\bpercent\b|\b%\b'
        r'|\bdifference\b.*\b(?:between|vs|and)\b'
        r'|\bhow\s+much\s+(?:percent|%|difference)\b',
        ql
    ):
        return QueryIntent.PERCENTAGE

    # Comparison (no percentage required)
    if re.search(
        r'\bcompare\b|\bvs\.?\b|\bversus\b'
        r'|\bdifference\s+between\b'
        r'|\bbetter\s+than\b|\bworse\s+than\b',
        ql
    ):
        return QueryIntent.COMPARISON

    # Existence check — "any X available?", "is there a X?", "do we have X?"
    if re.search(
        r'\bany\b.{0,30}\bavailable\b'
        r'|\bis\s+there\s+(?:a|an)?\b'
        r'|\bare\s+there\s+any\b'
        r'|\bdo\s+we\s+have\b'
        r'|\bdoes\s+.{0,20}\bexist\b'
        r'|\bfound\s+in\s+(?:the\s+)?crm\b',
        ql
    ):
        return QueryIntent.EXISTENCE

    # Count
    if re.search(r'\bhow\s+many\b|\bcount\b|\btotal\s+number\b|\bnumber\s+of\b', ql):
        return QueryIntent.COUNT

    # Revenue / financial aggregation
    if re.search(r'\brevenue\b|\btotal\s+(?:paid|collection)\b|\bincome\b', ql):
        return QueryIntent.REVENUE

    # Aggregation / breakdown
    if re.search(
        r'\bby\s+(?:stage|status|owner|region|month|year|source|priority)\b'
        r'|\bbreakdown\b|\banalysis\b|\bpipeline\b'
        r'|\btop\s+\d+\b.*\bby\b|\bgrouped?\b',
        ql
    ):
        return QueryIntent.AGGREGATION

    # Summary
    if re.search(r'\boverview\b|\bbrief\b|\bsummariz\b|\bsummary\b', ql):
        return QueryIntent.SUMMARY

    # Detail
    if re.search(
        r'\bdetails?\b|\beverything\b|\bcomplete\b|\bfull\s+(?:info|details?)\b',
        ql
    ):
        return QueryIntent.DETAIL

    return QueryIntent.LIST


# ── Dynamic limit extraction ──────────────────────────

def _limit(ql: str) -> Optional[int]:
    """
    Extract any explicit number from "first 5", "top 10", "last 3",
    "show 20", "give me 7", "latest 4", "only 2 records", etc.

    Excludes numbers followed by time units (e.g. "last 3 months" is temporal, not a limit).
    """
    # Time units that disqualify a number from being a limit
    _TIME_UNITS = r"(?:day|week|month|year|hour|minute)s?"

    patterns = [
        # "last N" only if NOT followed by a time unit
        rf'\b(?:first|top|show\s+me|give\s+me|last|latest|recent|bottom|only)\s+(\d+)(?!\s+{_TIME_UNITS})\b',
        r'\blimit\s+(?:to\s+)?(\d+)\b',
        r'\b(\d+)\s+(?:records?|results?|rows?|items?|entries?|contacts?|deals?|invoices?)\b',
        r'\b(\d+)(?:st|nd|rd|th)\b',
    ]
    for pat in patterns:
        m = re.search(pat, ql)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 500:
                return n
    return None


# ── Sort direction ────────────────────────────────────

def _sort(ql: str) -> Tuple[Optional[str], int, Optional[str]]:
    """
    Returns (field_hint, direction, reason) where direction 1=asc, -1=desc.
    field_hint is a generic token; the LLM maps it to the real DB field.
    """
    # Low / small / cheap / minimum → ascending amount
    if re.search(
        r'\blow(?:est)?\s*(?:amount|value|price|cost|total)?\b'
        r'|\bsmall(?:est)?\s*(?:amount|value|price|cost|total)?\b'
        r'|\b(?:amount|value|price|cost|total)\s+(?:is\s+)?(?:low|small|min)\b'
        r'|\bcheap(?:est)?\b|\bminimum\b|\bmin\b|\bsmallest\b|\btiny\b'
        r'|\bascending\b|\basc\b|\bleast\b',
        ql
    ):
        return "amount", 1, "low_value"

    # High / big / large / maximum → descending amount
    if re.search(
        r'\bhigh(?:est)?\s*(?:amount|value|price|cost|total)?\b'
        r'|\bbig(?:gest)?\s*(?:amount|value|price|cost|total)?\b'
        r'|\blarge(?:st)?\s*(?:amount|value|price|cost|total)?\b'
        r'|\b(?:amount|value|price|cost|total)\s+(?:is\s+)?(?:high|big|large|max)\b'
        r'|\bmost\s+(?:expensive|valuable|revenue)\b|\bmaximum\b|\bmax\b'
        r'|\blargest\b|\bbiggest\b|\bhuge\b|\bdescending\b|\bdesc\b',
        ql
    ):
        return "amount", -1, "high_value"

    # Latest / newest / most recent → date descending
    if re.search(r'\blatest\b|\bnewest\b|\bmost\s+recent\b', ql):
        return "createdAt", -1, "latest"

    # Oldest / first created → date ascending
    if re.search(r'\boldest\b|\bfirst\s+(?:created|added|entered|logged)\b', ql):
        return "createdAt", 1, "oldest"

    return None, -1, None


# ── Filter hints ──────────────────────────────────────

def _filters(ql: str) -> Dict[str, Any]:
    """Extract explicit field=value hints from query text."""
    hints: Dict[str, Any] = {}

    # Status
    m = re.search(r'\bstatus\s+(?:is\s+|=\s*)?["\']?([\w_]+)["\']?', ql)
    if m:
        hints["status"] = m.group(1)

    # Priority
    m = re.search(r'\bpriority\s+(?:is\s+|=\s*)?["\']?(high|medium|low)["\']?', ql, re.I)
    if m:
        hints["priority"] = m.group(1).capitalize()

    # Amount ranges
    m = re.search(r'\babove\s+(?:amount\s+)?(\d[\d,]*)', ql)
    if m:
        hints["amount_gt"] = int(m.group(1).replace(",", ""))
    m = re.search(r'\bbelow\s+(?:amount\s+)?(\d[\d,]*)', ql)
    if m:
        hints["amount_lt"] = int(m.group(1).replace(",", ""))
    m = re.search(r'\bbetween\s+(\d[\d,]*)\s+(?:and|to|-)\s+(\d[\d,]*)', ql)
    if m:
        hints["amount_gte"] = int(m.group(1).replace(",", ""))
        hints["amount_lte"] = int(m.group(2).replace(",", ""))

    return hints


# ── Entity name extraction ────────────────────────────

def _entity_names(query: str) -> List[str]:
    """
    Extract proper-noun entity names — full names including hyphens/commas.
    Also detects company names in "Company X this company..." patterns.
    """
    names: List[str] = []
    q = query.strip()
    ql = q.lower()

    # Patterns for "this company" queries
    _lead_stop = {"give", "show", "get", "list", "find", "what", "who", "how",
                  "tell", "me", "all", "the", "a", "an", "give me", "show me"}
    _coll_words = {"invoice", "invoices", "deal", "deals", "contact", "contacts",
                   "task", "tasks", "sales", "payment", "order", "note", "meeting"}

    # Pattern 1: "[CompanyName] this company [collection]..."
    # e.g. "Transportation Services this company invoice..."
    m = re.match(
        r'^([\w][\w\s\-&.,]{2,100}?)\s+this\s+company\b',
        q, re.IGNORECASE
    )
    if m:
        candidate = m.group(1).strip()
        first_word = candidate.lower().split()[0] if candidate else ""
        if first_word not in _lead_stop and len(candidate) > 2:
            names.append(candidate)
            return names

    # Pattern 2: "... this company [collection] [CompanyName] ..."
    # e.g. "give me this company invoice Transportation Services ..."
    m = re.search(
        r'\bthis\s+company\s+(?:' + '|'.join(_coll_words) + r')\s+([\w][\w\s\-&.,]{2,100}?)(?:\s+(?:payment|currency|owner|created|who|give|show|get|name|status)\b|$)',
        q, re.IGNORECASE
    )
    if m:
        candidate = m.group(1).strip().rstrip(".,;")
        if len(candidate) > 2 and candidate.lower() not in _lead_stop:
            names.append(candidate)
            return names

    # Triggers: "for X", "of X", "about X", "named X", "called X", "linked to X"
    trigger_patterns = [
        r'\blinked\s+to\s+([\w][\w\s\-&.,]{2,100})',
        r'\bassociated\s+with\s+([\w][\w\s\-&.,]{2,100})',
        r'\bhistory\s+for\s+([\w][\w\s\-&.,]{2,100})',
        r'\bdetails?\s+for\s+([\w][\w\s\-&.,]{2,100})',
        r'\bdetails?\s+of\s+([\w][\w\s\-&.,]{2,100})',
        r'\bfor\s+([\w][\w\s\-&.,]{2,100})',
        r'\bof\s+([\w][\w\s\-&.,]{2,100})',
    ]

    stop_words = {
        "deal", "deals", "invoice", "invoices", "company", "companies",
        "contact", "contacts", "task", "tasks", "sales", "users", "user",
        "me", "all", "the", "this", "that", "a", "an", "my", "our",
        "today", "week", "month", "year", "now", "last", "first",
        "high", "low", "pending", "open", "closed", "active",
    }
    # Skip temporal phrases — "last 3 months" is not an entity name
    _temporal_pat = re.compile(r'\b(?:last|next|past)\s+\d+\s+(?:day|week|month|year)s?\b', re.I)

    for pat in trigger_patterns:
        m = re.search(pat, q, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            # Clean trailing stop-phrases
            name = re.sub(
                r'\s+\b(give|show|get|and|with|from|in|or|please|available|crm|using|including)\b.*$',
                '', name, flags=re.IGNORECASE
            ).strip().rstrip(".,;?")
            # Skip if it's just a stop word, too short, or a temporal phrase
            if (len(name) > 2
                    and name.lower() not in stop_words
                    and not _temporal_pat.search(name)):
                if name not in names:
                    names.append(name)
            break   # first match is most specific

    return names


# ── Comparison entities ───────────────────────────────

def _compare_entities(ql: str) -> List[str]:
    """
    Extract the two groups being compared.
    E.g. "Closed Won & Closed Lost" → ["closed won", "closed lost"]
    E.g. "difference between paid and unpaid" → ["paid", "unpaid"]
    """
    # Lead/tail noise words removed from entity strings
    # Note: keep entity words like "all", "deals" — only strip meta/connector words
    noise = {"the", "a", "an", "is", "are", "in", "of", "how", "many",
             "percentage", "percent", "difference", "between", "what", "much",
             "compare", "comparison", "show", "get", "give", "me"}

    def _clean(s: str) -> str:
        # Strip noise from both ends (not middle — "closed won" must stay whole)
        words = s.strip().strip("'\"").split()
        while words and words[0] in noise:
            words.pop(0)
        while words and words[-1] in noise:
            words.pop()
        return " ".join(words).strip()

    # Priority 1: explicit "between X and Y" (bounded — max 4 words per side)
    m = re.search(
        r'\bbetween\s+([\w]+(?:\s+[\w]+){0,3}?)\s+(?:&|and)\s+([\w]+(?:\s+[\w]+){0,3})',
        ql
    )
    if m:
        e1, e2 = _clean(m.group(1)), _clean(m.group(2))
        if e1 and e2:
            return [e1, e2]

    # Priority 2: "percentage/percent of X and Y"
    m = re.search(
        r'\b(?:percentage|percent|%)\s+of\s+([\w]+(?:\s+[\w]+){0,3}?)\s+and\s+([\w]+(?:\s+[\w]+){0,3})',
        ql
    )
    if m:
        e1, e2 = _clean(m.group(1)), _clean(m.group(2))
        if e1 and e2:
            return [e1, e2]

    # Priority 3: "X vs Y" or "X & Y" (bounded)
    m = re.search(
        r'\b([\w]+(?:\s+[\w]+){0,3})\s+(?:&|vs\.?|versus)\s+([\w]+(?:\s+[\w]+){0,3})',
        ql
    )
    if m:
        e1, e2 = _clean(m.group(1)), _clean(m.group(2))
        if e1 and e2:
            return [e1, e2]

    # Priority 4: quoted entities "X" and "Y"
    quoted = re.findall(r'["\']([^"\']+)["\']', ql)
    if len(quoted) >= 2:
        return [quoted[0].strip(), quoted[1].strip()]

    return []


# ── Existence intent ──────────────────────────────────

def _existence_target(query: str, ql: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (target_name, entity_type) for existence queries.
    E.g. "any Pankaj contact available" → ("Pankaj", "contact")
    """
    entity_types = [
        "contact", "contacts", "company", "companies", "deal", "deals",
        "invoice", "invoices", "user", "users", "lead", "leads",
        "task", "tasks", "outreach", "record", "records",
    ]

    # "any <name> <entity_type> available"
    for etype in entity_types:
        m = re.search(
            rf'\bany\s+([\w][\w\s\-&.]*?)\s+{re.escape(etype)}\b',
            query, re.IGNORECASE
        )
        if m:
            name = m.group(1).strip().rstrip(".,;")
            if len(name) > 1:
                return name, etype.rstrip("s")

    # "is there a <name> in <entity_type>"
    m = re.search(
        r'\bis\s+there\s+(?:a\s+|an\s+)?([\w][\w\s\-&.]{1,60}?)(?:\s+in\s+(?:the\s+)?(?:crm|system|database))?\s*\??$',
        query, re.IGNORECASE
    )
    if m:
        name = m.group(1).strip().rstrip(".,;?")
        if len(name) > 1:
            return name, None

    # "do we have a <name>"
    m = re.search(
        r'\bdo\s+we\s+have\s+(?:a\s+|an\s+)?([\w][\w\s\-&.]{1,60})',
        query, re.IGNORECASE
    )
    if m:
        name = m.group(1).strip().rstrip(".,;?")
        if len(name) > 1:
            return name, None

    return None, None


# ── Temporal ──────────────────────────────────────────

def _temporal(ql: str) -> Optional[str]:
    if re.search(r'\bthis\s+week\b', ql):         return "this_week"
    if re.search(r'\bthis\s+month\b', ql):        return "this_month"
    if re.search(r'\blast\s+month\b', ql):        return "last_month"
    if re.search(r'\bthis\s+(?:financial\s+)?year\b|\bfy\b', ql): return "this_year"
    if re.search(r'\blast\s+year\b', ql):         return "last_year"
    if re.search(r'\btoday\b', ql):               return "today"
    if re.search(r'\byesterday\b', ql):           return "yesterday"
    m = re.search(r'\blast\s+(\d+)\s+(day|week|month|year)s?\b', ql)
    if m:                                          return f"last_{m.group(1)}_{m.group(2)}"
    return None


# ── Collection hints ──────────────────────────────────

def _collection_hints(ql: str) -> List[str]:
    """
    Lightweight collection signal detection for relationship graph context.
    Only adds collections that have explicit keyword signals.
    """
    signals = [
        (["deal", "deals", "pipeline", "opportunity", "stage", "closed won", "closed lost"], "deals"),
        (["invoice", "invoices", "billing", "payment", "overdue payment", "revenue"], "invoices"),
        (["company", "companies", "account", "accounts", "client"], "companies"),
        (["contact", "contacts", "person"], "contacts"),
        (["task", "tasks", "to-do", "follow-up", "reminder"], "createtasks"),
        (["user", "users", "owner", "rep", "sales rep", "assigned"], "users"),
        (["lead", "leads", "outreach", "prospect"], "outreaches"),
        (["target", "targets", "quota", "achievement"], "targets"),
        (["sales order", "sales orders", "so number"], "sales"),
        (["product", "products"], "products"),
        (["meeting", "meetings"], "meetings"),
        (["region", "regions", "territory"], "regions"),
        (["source", "sources", "lead source"], "sources"),
        (["technology", "technologies", "tech"], "technologies"),
        (["department", "departments"], "departments"),
        (["bill", "bills", "vendor bill"], "bills"),
    ]
    found: List[str] = []
    for keywords, coll in signals:
        if any(kw in ql for kw in keywords) and coll not in found:
            found.append(coll)
    return found
