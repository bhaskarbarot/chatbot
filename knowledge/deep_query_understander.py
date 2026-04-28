"""
Deep Query Understanding Layer — Enhancement 1.

Sends the resolved user query to the LLM with a focused prompt to extract
structured semantic parameters: intent, response shape, result limit, temporal
filter, relationship join requirements, and follow-up references.

LLM VARIABILITY PROTECTION:
- Every LLM call has a 3-second timeout (fail fast).
- JSON schema validated and missing keys filled with safe defaults.
- On timeout / non-JSON / any exception → pure Python rule-based fallback runs.
- Fallback covers every Section 6 temporal pattern and Section 7 limit pattern.
- Never raises, never crashes — always returns a valid schema-conforming dict.

Thread safety: no shared mutable state per query — each call is independent.
"""
from __future__ import annotations

import calendar
import copy
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import MAX_MONGO_RESULTS, OLLAMA_BASE_URL, OLLAMA_MODEL
from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("deep_query_understander")

# ── Layer-specific LLM settings (fail fast, not the planner's 20s) ──────────
_LLM_TIMEOUT_S = 3
_LLM_MAX_TOKENS = 350
_LLM_TEMPERATURE = 0

# ── Schema defaults — returned on any failure path ───────────────────────────
SCHEMA_DEFAULTS: Dict[str, Any] = {
    "primary_intent": "data_fetch",
    "response_shape": "list",
    "result_limit": None,
    "sort_preference": {"field": None, "direction": "null"},
    "temporal_filter": {
        "type": "none",
        "value": None,
        "year": None,
        "month": None,
        "start_date": None,
        "end_date": None,
    },
    "entities_mentioned": [],
    "is_followup": False,
    "followup_reference": None,
    "is_multi_intent": False,
    "sub_intents": [],
    "comparison_targets": [],
    "needs_relationship_join": False,
    "output_format_hint": None,
}

VALID_SHAPES = {
    "single_value", "yes_no", "list", "table",
    "number", "analysis_text", "greeting"
}
VALID_INTENTS = {
    "data_fetch", "analysis", "count", "existence_check",
    "comparison", "greeting", "out_of_scope",
    "specific_detail", "relationship_lookup"
}
VALID_DIRECTIONS = {"asc", "desc", "null", None}
VALID_TEMPORAL_TYPES = {
    "none", "last_N_days", "last_N_months", "this_month",
    "last_month", "this_year", "last_year", "specific_year",
    "specific_month", "date_range", "this_quarter",
    "last_quarter", "this_week", "yesterday", "today"
}
CRM_KEYWORDS = {
    "lead", "deal", "contact", "company", "meeting",
    "invoice", "account", "opportunity", "task", "pipeline",
    "revenue", "amount", "owner", "status", "stage", "close"
}

# ── LLM prompt (double-braces escape literal braces in .format()) ───────────
_UNDERSTAND_PROMPT = """\
You are a CRM query analyzer. Analyze the user query and return ONLY a single \
JSON object with no explanation, no markdown, no extra text.

User Query: {query}

Return this exact JSON structure (choose values from the listed options):
{{
  "primary_intent": "<data_fetch|analysis|count|existence_check|comparison|greeting|out_of_scope|specific_detail|relationship_lookup>",
  "response_shape": "<single_value|yes_no|list|table|number|analysis_text|greeting>",
  "result_limit": <null or integer>,
  "sort_preference": {{"field": <null or "field_name_hint">, "direction": "<asc|desc|null>"}},
  "temporal_filter": {{
    "type": "<none|last_N_days|last_N_months|this_month|last_month|this_year|last_year|specific_year|specific_month|date_range|this_quarter|last_quarter|this_week|yesterday|today>",
    "value": <null or number>,
    "year": <null or integer>,
    "month": <null or integer>
  }},
  "entities_mentioned": [],
  "is_followup": <true|false>,
  "followup_reference": <null or "pronoun/phrase">,
  "is_multi_intent": <true|false>,
  "sub_intents": [],
  "comparison_targets": [],
  "needs_relationship_join": <true|false>,
  "output_format_hint": <null or "string">
}}
"""

# ── Month name → number lookup ───────────────────────────────────────────────
_MONTH_MAP: Dict[str, int] = {
    "january": 1,  "february": 2,  "march": 3,    "april": 4,
    "may": 5,      "june": 6,      "july": 7,      "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ── Time units that disqualify a number from being a result limit ────────────
_TIME_UNIT_PAT = r"(?:day|week|month|year|hour|minute)s?"


class DeepQueryUnderstander:
    """
    LLM-based deep semantic understanding of user queries.

    Public API:
      understand(query: str) -> Dict[str, Any]   — always returns valid schema
      safe_default()          -> Dict[str, Any]   — neutral safe output

    Thread safety: The LLM handle is lazily created once (double-checked lock).
    All per-call state is local — no instance-level mutable state per request.
    """

    def __init__(self) -> None:
        self._llm = None
        self._llm_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def understand(self, query: str) -> Dict[str, Any]:
        """
        Entry point. Returns enriched understanding dict — never raises.
        Tries LLM first (3s budget). Falls back to rule-based on any failure.
        Resolves temporal date ranges before returning.
        """
        if not query or not isinstance(query, str):
            return self.safe_default()

        record_event("deep_understander_called", {"query": query[:80]})

        # Attempt LLM path
        result = self._safe_llm_call(query)
        if result is None:
            # Full rule-based fallback
            result = _rule_based_understand(query)
            record_event("deep_understander_fallback_used", {})

        # Resolve temporal date ranges from type+value+year+month
        tf = result.get("temporal_filter", {})
        if isinstance(tf, dict) and tf.get("type") not in (None, "none", "date_range"):
            resolved = _resolve_temporal_dates(
                tf.get("type", "none"),
                tf.get("value"),
                tf.get("year"),
                tf.get("month"),
            )
            result["temporal_filter"]["start_date"] = resolved["start_date"]
            result["temporal_filter"]["end_date"] = resolved["end_date"]

        logger.debug(
            f"[DeepUnderstander] shape={result.get('response_shape')} "
            f"limit={result.get('result_limit')} "
            f"temporal={tf.get('type')} "
            f"multi_intent={result.get('is_multi_intent')} "
            f"join={result.get('needs_relationship_join')}"
        )
        record_event("deep_understander_result", {
            "shape": result.get("response_shape"),
            "limit": result.get("result_limit"),
            "temporal_type": tf.get("type"),
            "is_multi_intent": result.get("is_multi_intent"),
        })
        return result

    @classmethod
    def safe_default(cls) -> Dict[str, Any]:
        """Return neutral safe output used when the layer fails entirely."""
        return copy.deepcopy(SCHEMA_DEFAULTS)

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _get_llm(self):
        """Lazy-init a short-timeout ChatOllama instance (thread-safe)."""
        if self._llm is not None:
            return self._llm
        with self._llm_lock:
            if self._llm is None:
                try:
                    from langchain_ollama import ChatOllama
                    self._llm = ChatOllama(
                        model=OLLAMA_MODEL,
                        base_url=OLLAMA_BASE_URL,
                        temperature=_LLM_TEMPERATURE,
                        num_predict=_LLM_MAX_TOKENS,
                        timeout=_LLM_TIMEOUT_S,
                    )
                    logger.info("[DeepUnderstander] LLM handle created")
                except Exception as e:
                    logger.warning(f"[DeepUnderstander] LLM init failed: {e}")
                    self._llm = None
        return self._llm

    def _safe_llm_call(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Call LLM with strict 3s timeout.
        Validates and fills schema defaults on success.
        Returns None on any failure so caller can use rule-based fallback.
        """
        llm = self._get_llm()
        if llm is None:
            return None

        try:
            prompt = _UNDERSTAND_PROMPT.format(query=query)
            response = llm.invoke(prompt)
            raw = (
                response.content.strip()
                if hasattr(response, "content")
                else str(response).strip()
            )
            logger.debug(f"[DeepUnderstander] LLM raw: {raw[:300]}")

            parsed = _extract_json(raw)
            if parsed is None:
                logger.warning("[DeepUnderstander] LLM returned non-JSON → rule-based fallback")
                record_event("deep_understander_non_json", {"query": query[:80]})
                return None

            validated = _fill_defaults(parsed, SCHEMA_DEFAULTS)
            validated = self._validate_enums(
                validated,
                fallback_fn=lambda: _rule_based_understand(query),
            )
            validated = self._apply_query_sanity(validated, query)
            record_event("deep_understander_llm_ok", {})
            return validated

        except Exception as e:
            logger.warning(f"[DeepUnderstander] LLM call failed: {e} → rule-based fallback")
            record_event("deep_understander_llm_error", {"error": str(e)[:120]})
            return None

    def _validate_enums(self, parsed: dict, fallback_fn) -> dict:
        if parsed.get("response_shape") not in VALID_SHAPES:
            logger.warning(
                f"[DeepUnderstander] invalid response_shape="
                f"{parsed.get('response_shape')} → fallback"
            )
            return fallback_fn()
        if parsed.get("primary_intent") not in VALID_INTENTS:
            logger.warning(
                f"[DeepUnderstander] invalid primary_intent="
                f"{parsed.get('primary_intent')} → fallback"
            )
            return fallback_fn()
        if parsed.get("sort_preference", {}).get("direction") not in VALID_DIRECTIONS:
            parsed["sort_preference"]["direction"] = "null"
        if parsed.get("temporal_filter", {}).get("type") not in VALID_TEMPORAL_TYPES:
            parsed["temporal_filter"]["type"] = "none"
        return parsed

    def _is_true_multi_intent(self, query: str, parts: list) -> bool:
        """
        Returns False if the split parts share the same entity type
        and same operation — meaning it's one query, not two.
        Examples that must return False:
          "top 5 deals and sort by amount"  → same entity, same op
          "leads created and updated today" → same entity
        Examples that must return True:
          "how many leads and how many deals" → different entities
          "compare won vs lost deals"        → comparison operation
          "leads count and meetings count"   → different entity types
        """
        if len(parts) < 2:
            return False

        collection_keywords = [
            "lead", "deal", "contact", "company", "meeting",
            "account", "opportunity", "task", "note"
        ]
        part0_entities = [w for w in collection_keywords if w in parts[0].lower()]
        part1_entities = [w for w in collection_keywords if w in parts[1].lower()]

        if part0_entities and part1_entities:
            if set(part0_entities) == set(part1_entities):
                return False

        sort_words = {"sort", "order", "arrange", "ranked", "sorted"}
        if any(w in parts[1].lower() for w in sort_words):
            return False

        return True

    def _apply_query_sanity(self, parsed: dict, query: str) -> dict:
        query_lower = query.lower()
        if parsed.get("primary_intent") == "out_of_scope":
            if any(kw in query_lower for kw in CRM_KEYWORDS):
                parsed["primary_intent"] = "data_fetch"
                parsed["response_shape"] = "list"
                logger.debug(
                    "[DeepUnderstander] out_of_scope overridden to "
                    "data_fetch — CRM keywords detected"
                )

        count_words = (
            "how many", "count", "number of", "total", "sum", "rate", "ratio", "percentage"
        )
        has_count_word = any(w in query_lower for w in count_words)
        has_entity_word = any(w in query_lower for w in (
            "lead", "deal", "contact", "company", "meeting",
            "invoice", "account", "opportunity", "task"
        ))
        if parsed.get("response_shape") == "number" and has_entity_word and not has_count_word:
            parsed["response_shape"] = "list"
            if parsed.get("primary_intent") == "count":
                parsed["primary_intent"] = "data_fetch"
        return parsed


# ── JSON extraction (reuses pattern from query_planner) ──────────────────────

def _extract_json(text: str) -> Optional[Dict]:
    """Extract a JSON object from raw LLM response text."""
    import ast

    def _try_parse(candidate: str) -> Optional[Dict]:
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict):
                return json.loads(json.dumps(parsed, default=str))
        except Exception:
            pass
        return None

    direct = _try_parse(text)
    if direct is not None:
        return direct

    # JSON wrapped in markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        parsed = _try_parse(m.group(1))
        if parsed is not None:
            return parsed

    # Brace-balance extraction (finds outermost { … })
    start = text.find("{")
    if start >= 0:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    parsed = _try_parse(text[start : idx + 1])
                    if parsed is not None:
                        return parsed
                    break
    return None


# ── Schema default filling ────────────────────────────────────────────────────

def _fill_defaults(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill missing or wrong-type fields from defaults.
    Never raises — always returns a schema-conforming dict.
    """
    result = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return result

    # String fields
    for f in ("primary_intent", "response_shape", "followup_reference", "output_format_hint"):
        v = data.get(f)
        if v is not None and isinstance(v, str):
            result[f] = v

    # Boolean fields
    for f in ("is_followup", "is_multi_intent", "needs_relationship_join"):
        v = data.get(f)
        if isinstance(v, bool):
            result[f] = v
        elif isinstance(v, str):
            result[f] = v.lower() in ("true", "1", "yes")

    # List fields
    for f in ("entities_mentioned", "sub_intents", "comparison_targets"):
        v = data.get(f)
        if isinstance(v, list):
            result[f] = v

    # result_limit — must be int in [1, 10000] or None
    rl = data.get("result_limit")
    if rl is None:
        result["result_limit"] = None
    else:
        try:
            n = int(rl)
            result["result_limit"] = n if 1 <= n <= 10000 else None
        except (TypeError, ValueError):
            result["result_limit"] = None

    # sort_preference
    sp = data.get("sort_preference")
    if isinstance(sp, dict):
        result["sort_preference"] = {
            "field": sp.get("field") if isinstance(sp.get("field"), str) else None,
            "direction": (
                sp.get("direction", "null")
                if isinstance(sp.get("direction"), str)
                else "null"
            ),
        }

    # temporal_filter
    tf = data.get("temporal_filter")
    if isinstance(tf, dict):
        result["temporal_filter"] = {
            "type": tf.get("type", "none") if isinstance(tf.get("type"), str) else "none",
            "value": tf.get("value"),
            "year": _safe_int(tf.get("year")),
            "month": _safe_int(tf.get("month")),
            "start_date": (
                tf.get("start_date") if isinstance(tf.get("start_date"), str) else None
            ),
            "end_date": (
                tf.get("end_date") if isinstance(tf.get("end_date"), str) else None
            ),
        }

    return result


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── Temporal date resolver ────────────────────────────────────────────────────

def _resolve_temporal_dates(
    type_: str,
    value,
    year,
    month,
) -> Dict[str, Optional[str]]:
    """
    Convert temporal type + metadata → ISO 8601 date range (start, end).
    Pure Python datetime + calendar only. Zero hardcoding.
    Covers all patterns documented in Section 6.
    """
    now = datetime.now()
    empty: Dict[str, Optional[str]] = {"start_date": None, "end_date": None}

    def iso(dt: datetime) -> str:
        return dt.isoformat()

    def day_start(dt: datetime) -> datetime:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)

    def day_end(dt: datetime) -> datetime:
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    if not type_ or type_ == "none":
        return empty

    if type_ == "today":
        return {"start_date": iso(day_start(now)), "end_date": iso(now)}

    if type_ == "yesterday":
        yd = now - timedelta(days=1)
        return {"start_date": iso(day_start(yd)), "end_date": iso(day_end(yd))}

    if type_ == "this_week":
        # Monday of current week
        monday = day_start(now - timedelta(days=now.weekday()))
        return {"start_date": iso(monday), "end_date": iso(now)}

    if type_ == "last_week":
        this_monday = day_start(now - timedelta(days=now.weekday()))
        last_monday = this_monday - timedelta(weeks=1)
        last_sunday = this_monday - timedelta(microseconds=1)
        return {"start_date": iso(last_monday), "end_date": iso(last_sunday)}

    if type_ == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"start_date": iso(start), "end_date": iso(now)}

    if type_ == "last_month":
        start_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_last = start_this - timedelta(microseconds=1)
        start_last = end_last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"start_date": iso(start_last), "end_date": iso(end_last)}

    if type_ == "this_year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"start_date": iso(start), "end_date": iso(now)}

    if type_ == "last_year":
        y = now.year - 1
        return {
            "start_date": iso(datetime(y, 1, 1)),
            "end_date": iso(datetime(y, 12, 31, 23, 59, 59)),
        }

    if type_ == "last_N_days":
        n = int(value) if value is not None else 30
        return {"start_date": iso(now - timedelta(days=n)), "end_date": iso(now)}

    if type_ == "last_N_months":
        n = int(value) if value is not None else 3
        return {"start_date": iso(now - timedelta(days=30 * n)), "end_date": iso(now)}

    if type_ == "specific_year":
        y = int(year) if year is not None else now.year
        return {
            "start_date": iso(datetime(y, 1, 1)),
            "end_date": iso(datetime(y, 12, 31, 23, 59, 59)),
        }

    if type_ == "specific_month":
        y = int(year) if year is not None else now.year
        m = int(month) if month is not None else now.month
        last_day = calendar.monthrange(y, m)[1]
        return {
            "start_date": iso(datetime(y, m, 1)),
            "end_date": iso(datetime(y, m, last_day, 23, 59, 59)),
        }

    if type_ == "this_quarter":
        # Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec (0-indexed: 0,1,2,3)
        current_q = (now.month - 1) // 3
        q_start_month = current_q * 3 + 1
        start = datetime(now.year, q_start_month, 1)
        return {"start_date": iso(start), "end_date": iso(now)}

    if type_ == "last_quarter":
        current_q = (now.month - 1) // 3
        if current_q == 0:
            lq_year = now.year - 1
            lq_start_month = 10   # Q4: Oct-Dec
            lq_end_month = 12
        else:
            lq_year = now.year
            lq_start_month = (current_q - 1) * 3 + 1
            lq_end_month = current_q * 3
        last_day = calendar.monthrange(lq_year, lq_end_month)[1]
        return {
            "start_date": iso(datetime(lq_year, lq_start_month, 1)),
            "end_date": iso(datetime(lq_year, lq_end_month, last_day, 23, 59, 59)),
        }

    if type_ == "date_range":
        # start_date / end_date already filled by the caller (quarter parsing)
        return empty

    return empty


# ── Rule-based understand (pure Python, no LLM) ──────────────────────────────

def _rule_based_understand(query: str) -> Dict[str, Any]:
    """
    Pure Python fallback for when LLM is unavailable or returns bad output.
    Covers all Section 6 temporal patterns and Section 7 limit patterns.
    """
    result = copy.deepcopy(SCHEMA_DEFAULTS)
    ql = query.lower().strip()

    result["primary_intent"] = _detect_primary_intent(ql)
    result["response_shape"] = _detect_response_shape(ql)
    result["result_limit"] = _extract_limit(ql)
    result["sort_preference"] = _extract_sort_preference(ql)
    result["temporal_filter"] = _extract_temporal_filter(ql)
    result["is_multi_intent"] = _detect_multi_intent(ql)
    result["needs_relationship_join"] = _detect_relationship_join(ql)
    result["is_followup"] = _detect_followup(ql)
    result["followup_reference"] = _extract_followup_reference(ql)
    result["entities_mentioned"] = _extract_entity_mentions(query)

    if result["is_multi_intent"]:
        result["sub_intents"] = _extract_sub_intents(ql)

    query_lower = query.lower()
    if result.get("primary_intent") == "out_of_scope":
        if any(kw in query_lower for kw in CRM_KEYWORDS):
            result["primary_intent"] = "data_fetch"
            result["response_shape"] = "list"
            logger.debug(
                "[DeepUnderstander] out_of_scope overridden to "
                "data_fetch — CRM keywords detected"
            )

    return result


# ── Primary intent detection ──────────────────────────────────────────────────

def _detect_primary_intent(ql: str) -> str:
    if re.search(r'\bhello\b|\bhi\b|\bhey\b|\bgood\s+(?:morning|evening|afternoon|day)\b', ql):
        return "greeting"
    # Count check BEFORE existence_check: "how many X do we have" is a count, not existence
    if re.search(r'\bhow\s+many\b|\bnumber\s+of\b|\btotal\s+number\b', ql):
        return "count"
    if re.search(
        r'\bany\b.{0,30}\bavailable\b|\bis\s+there\b|\bare\s+there\s+any\b'
        r'|\bdo\s+we\s+have\b|\bdoes\s+.{0,20}\bexist\b|\bfound\s+in\b',
        ql
    ):
        return "existence_check"
    if re.search(r'\bcompare\b|\bvs\.?\b|\bversus\b|\bdifference\s+between\b|\bbetter\s+than\b', ql):
        return "comparison"
    if re.search(r'\banalyze\b|\banalysis\b|\binsight\b|\boverview\b|\bperformance\b|\bhow\s+are\s+we\b', ql):
        return "analysis"
    if re.search(
        r'\blinked\s+to\b|\bassociated\s+with\b|\bcontacts\s+of\b|\bdeals?\s+for\b'
        r'|\bowned?\s+by\b|\brelated\s+to\b|\bfor\s+company\b|\bfor\s+owner\b',
        ql
    ):
        return "relationship_lookup"
    if re.search(r'\bdetails?\b|\beverything\s+about\b|\bfull\s+info\b|\bcomplete\s+info\b', ql):
        return "specific_detail"
    return "data_fetch"


# ── Response shape detection ──────────────────────────────────────────────────

def _detect_response_shape(ql: str) -> str:
    # COUNT / NUMBER — check FIRST (higher priority: "how many ... do we have" is a count)
    if re.search(r'\bhow\s+many\b|\bnumber\s+of\b|\bcount\b|\btotal\s+number\b', ql):
        return "number"

    # YES / NO — only when NOT asking for a count
    if re.search(
        r'\bany\b.{0,30}\bavailable\b|\bis\s+there\b|\bare\s+there\s+any\b'
        r'|\bdo\s+we\s+have\b|\bdoes\s+.{0,20}\bexist\b',
        ql
    ):
        return "yes_no"

    # TABLE
    if re.search(r'\btable\b|\btabular\b|\bin\s+table\b|\bspreadsheet\b|\btab\s+format\b', ql):
        return "table"

    # ANALYSIS TEXT
    if re.search(
        r'\banalyze\b|\banalysis\b|\binsight\b|\boverview\b|\bperformance\b'
        r'|\bhow\s+are\s+we\b|\bsummarize\b|\bsummary\b',
        ql
    ):
        return "analysis_text"

    # SINGLE VALUE — indefinite article + singular CRM entity noun
    if re.search(
        r'\b(?:give\s+me\s+|show\s+me\s+|get\s+me\s+|find\s+me\s+)?'
        r'(?:a|an)\s+'
        r'(?:lead|deal|contact|company|invoice|task|record|user|vendor|meeting|opportunity)\b',
        ql
    ):
        return "single_value"

    # Ordinal reference
    if re.search(
        r'\b(?:first|1st|second|2nd|third|3rd)\s+'
        r'(?:lead|deal|contact|company|invoice|task|record|opportunity)\b',
        ql
    ):
        return "single_value"

    return "list"


# ── Limit extraction (Section 7 patterns) ────────────────────────────────────

def _extract_limit(ql: str) -> Optional[int]:
    """
    Dynamically extract result limit.
    Section 7: top N, last N, first N, show N, N records, highest N, lowest N,
    latest N, oldest N, singular article → 1, plural no number → None.
    """
    # "top N", "first N", "last N" (not followed by time unit), "latest N", etc.
    m = re.search(
        rf'\b(?:top|first|latest|oldest|last|recent|bottom|highest|lowest)\s+'
        rf'(\d+)(?!\s+{_TIME_UNIT_PAT})\b',
        ql
    )
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10000:
            return n

    # "show me N", "give me N", "get me N", "only N"
    m = re.search(r'\b(?:show\s+me|give\s+me|get\s+me|only)\s+(\d+)\b', ql)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10000:
            return n

    # "N records/results/leads/deals/..."
    m = re.search(
        r'\b(\d+)\s+'
        r'(?:records?|results?|rows?|items?|entries?|contacts?|deals?|invoices?|leads?|'
        r'companies|tasks?|users?|opportunities|outreaches?)\b',
        ql
    )
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10000:
            return n

    # "limit to N" / "limit N"
    m = re.search(r'\blimit\s+(?:to\s+)?(\d+)\b', ql)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10000:
            return n

    # Ordinal: "1st", "2nd", "3rd", "4th" → single item
    if re.search(r'\b\d+(?:st|nd|rd|th)\b', ql):
        m = re.search(r'\b(\d+)(?:st|nd|rd|th)\b', ql)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 10000:
                return n

    # Indefinite article + singular CRM noun → exactly 1
    if re.search(
        r'\b(?:a|an)\s+'
        r'(?:lead|deal|contact|company|invoice|task|record|user|vendor|meeting|opportunity)\b',
        ql
    ):
        return 1

    # "all" keyword → None (MAX_MONGO_RESULTS applied at execution time from config)
    return None


# ── Sort preference extraction ────────────────────────────────────────────────

def _extract_sort_preference(ql: str) -> Dict[str, Optional[str]]:
    # Descending by amount
    if re.search(
        r'\bhighest?\b|\bbiggest?\b|\blargest?\b|\bmaximum\b|\bmax\b'
        r'|\bmost\s+(?:expensive|valuable|revenue)\b|\bdesc(?:ending)?\b',
        ql
    ):
        return {"field": "amount", "direction": "desc"}

    # Ascending by amount
    if re.search(
        r'\blowest?\b|\bsmallest?\b|\bminimum\b|\bmin\b'
        r'|\bcheapest?\b|\bleast\b|\basc(?:ending)?\b',
        ql
    ):
        return {"field": "amount", "direction": "asc"}

    # Latest / newest / most recent → date descending
    if re.search(r'\blatest\b|\bnewest\b|\bmost\s+recent\b', ql):
        return {"field": "createdAt", "direction": "desc"}

    # Oldest → date ascending
    if re.search(r'\boldest\b|\bfirst\s+created\b|\bfirst\s+added\b|\bfirst\s+entered\b', ql):
        return {"field": "createdAt", "direction": "asc"}

    # "last N" (without time unit) = most recently created
    if re.search(rf'\blast\s+\d+(?!\s+{_TIME_UNIT_PAT})\b', ql):
        return {"field": "createdAt", "direction": "desc"}

    return {"field": None, "direction": "null"}


# ── Temporal filter extraction (Section 6 patterns) ──────────────────────────

def _extract_temporal_filter(ql: str) -> Dict[str, Any]:
    """
    Rule-based temporal extraction — covers ALL Section 6 patterns.
    Returns dict with type, value, year, month (dates resolved separately).
    """
    now = datetime.now()
    base: Dict[str, Any] = {
        "type": "none", "value": None,
        "year": None, "month": None,
        "start_date": None, "end_date": None,
    }

    # ── Single-keyword patterns (ordered most-specific first) ─────────────────
    if re.search(r'\btoday\b', ql):
        return {**base, "type": "today"}

    if re.search(r'\byesterday\b', ql):
        return {**base, "type": "yesterday"}

    if re.search(r'\bthis\s+week\b', ql):
        return {**base, "type": "this_week"}

    if re.search(r'\blast\s+week\b', ql):
        return {**base, "type": "last_week"}

    if re.search(r'\bthis\s+month\b', ql):
        return {**base, "type": "this_month"}

    if re.search(r'\blast\s+month\b', ql):
        return {**base, "type": "last_month"}

    if re.search(r'\bthis\s+(?:financial\s+)?year\b|\bfy\b|\bcurrent\s+year\b', ql):
        return {**base, "type": "this_year"}

    if re.search(r'\blast\s+year\b', ql):
        return {**base, "type": "last_year"}

    if re.search(r'\bthis\s+quarter\b|\bcurrent\s+quarter\b', ql):
        return {**base, "type": "this_quarter"}

    if re.search(r'\blast\s+quarter\b', ql):
        return {**base, "type": "last_quarter"}

    # ── Q1/Q2/Q3/Q4 with optional year ───────────────────────────────────────
    # Matches: "Q3 2023", "q1", "first quarter 2022", "second quarter", etc.
    _quarter_word_map = {
        "first": 1, "second": 2, "third": 3, "fourth": 4,
    }
    q_match = re.search(
        r'\b(?:[qQ]([1-4])|([fF]irst|[sS]econd|[tT]hird|[fF]ourth)\s+quarter)\b'
        r'(?:\s+(?:of\s+)?(\d{4}))?',
        ql
    )
    if q_match:
        if q_match.group(1):
            q_num = int(q_match.group(1))
        else:
            q_num = _quarter_word_map.get(q_match.group(2).lower(), 1)

        year_str = q_match.group(3)
        y = int(year_str) if year_str else now.year

        # Quarter → month range
        q_start_month = (q_num - 1) * 3 + 1   # Q1→1, Q2→4, Q3→7, Q4→10
        q_end_month = q_start_month + 2
        last_day = calendar.monthrange(y, q_end_month)[1]

        return {
            **base,
            "type": "date_range",
            "year": y,
            "start_date": datetime(y, q_start_month, 1).isoformat(),
            "end_date": datetime(y, q_end_month, last_day, 23, 59, 59).isoformat(),
        }

    # ── last N days / weeks / months / years ─────────────────────────────────
    n_unit = re.search(r'\blast\s+(\d+)\s+(day|week|month|year)s?\b', ql)
    if n_unit:
        n = int(n_unit.group(1))
        unit = n_unit.group(2)
        if unit == "week":
            return {**base, "type": "last_N_days", "value": n * 7}
        if unit == "year":
            return {**base, "type": "last_N_days", "value": n * 365}
        if unit == "month":
            return {**base, "type": "last_N_months", "value": n}
        # day
        return {**base, "type": "last_N_days", "value": n}

    # ── "between DATE and DATE" ───────────────────────────────────────────────
    between_m = re.search(
        r'\bbetween\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})'
        r'\s+and\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b',
        ql
    )
    if between_m:
        return {
            **base,
            "type": "date_range",
            "start_date": between_m.group(1),
            "end_date": between_m.group(2),
        }

    # ── Specific year: "year 2022", "in 2022", "of 2022", bare "2022" ────────
    year_m = re.search(r'\b(?:year\s+|in\s+|of\s+)?(\d{4})\b', ql)
    if year_m:
        y = int(year_m.group(1))
        if 2000 <= y <= now.year + 1:
            return {**base, "type": "specific_year", "year": y}

    # ── Specific month name: "January", "in February 2023", etc. ─────────────
    for mname, mnum in _MONTH_MAP.items():
        if re.search(rf'\b{re.escape(mname)}\b', ql):
            # Try to find adjacent year
            year_nearby = re.search(
                rf'\b{re.escape(mname)}\s+(\d{{4}})\b|\b(\d{{4}})\s+{re.escape(mname)}\b',
                ql
            )
            y = now.year
            if year_nearby:
                y = int(year_nearby.group(1) or year_nearby.group(2))
            return {**base, "type": "specific_month", "year": y, "month": mnum}

    return base


# ── Multi-intent detection ────────────────────────────────────────────────────

def _detect_multi_intent(ql: str) -> bool:
    """Detect if query contains multiple independent intents."""
    # Explicit "and give/show/get me" connector
    if re.search(
        r'\band\s+(?:give\s+me|show\s+me|get\s+me|also\s+show|also\s+give)\b', ql
    ):
        detected_parts = [p.strip() for p in re.split(
            r'\b(?:and|also|as\s+well\s+as)\b',
            ql,
            maxsplit=1,
        ) if p.strip()]
        is_multi_intent = DeepQueryUnderstander()._is_true_multi_intent(ql, detected_parts)
        return is_multi_intent

    # Two different CRM entity types in one query
    _entities_pat = (
        r'(?:deals?|invoices?|contacts?|compan(?:y|ies)|tasks?|leads?|'
        r'users?|sales|outreaches?|meetings?)'
    )
    entity_matches = re.findall(_entities_pat, ql)
    unique_entities = set(entity_matches)
    if len(unique_entities) >= 2:
        # Only multi-intent if they appear in separate clauses (connector present)
        if re.search(r'\band\b|\balso\b|\bas\s+well\s+as\b', ql):
            return True

    # Explicit comparison / parallel connectors
    if re.search(r'\bvs\.?\b|\bversus\b|\bcompared\s+to\b|\bas\s+well\s+as\b', ql):
        return True

    # "also" mid-query (at least 20 chars before it)
    if re.search(r'.{20,}\balso\b.{10,}', ql):
        return True

    return False


# ── Relationship join detection ───────────────────────────────────────────────

def _detect_relationship_join(ql: str) -> bool:
    return bool(re.search(
        r'\blinked\s+to\b|\bassociated\s+with\b|\bcontacts\s+of\b|\bdeals?\s+for\b'
        r'|\bowned?\s+by\b|\bbelongs?\s+to\b|\brelated\s+to\b'
        r'|\bfor\s+(?:company|owner|contact)\b|\bby\s+owner\b'
        r'|\bunder\s+(?:company|account)\b',
        ql
    ))


# ── Follow-up detection ───────────────────────────────────────────────────────

def _detect_followup(ql: str) -> bool:
    return bool(re.search(
        r'^\s*(?:those|them|they|same|it|that|this|again)\b'
        r'|\b(?:those|them|their|its|same\s+filter|same\s+query|the\s+ones\s+you\s+showed)\b',
        ql
    ))


def _extract_followup_reference(ql: str) -> Optional[str]:
    for pattern, ref in [
        (r'\b(those|them|they)\b', "last_result_set"),
        (r'\bsame\b', "same_filter"),
        (r'\b(it|that)\b', "last_single_entity"),
        (r'\btheir\b|\bits\b', "last_entity_possession"),
        (r'\bagain\b|\bsame\s+query\b', "repeat_last"),
        (r'\bthe\s+ones\s+you\s+showed\b|\bthose\s+\d+\b', "last_result_set_slice"),
    ]:
        if re.search(pattern, ql):
            return ref
    return None


# ── Entity mention extraction ─────────────────────────────────────────────────

def _extract_entity_mentions(query: str) -> List[str]:
    """
    Extract likely proper-noun entity mentions (company names, person names)
    for downstream field-relevance filtering in response_validator.
    """
    _noise = {
        "deal", "deals", "invoice", "invoices", "company", "companies",
        "contact", "contacts", "task", "tasks", "lead", "leads",
        "show", "give", "get", "me", "all", "the", "a", "an",
        "my", "our", "how", "many", "what", "which", "where",
        "when", "who", "is", "are", "do", "last", "this", "that",
        "any", "available", "please", "from", "with", "for",
        "about", "in", "to", "of", "and", "or",
    }
    # Sequences of capitalised words
    candidates = re.findall(r'\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})*)\b', query)
    return [n for n in candidates if n.lower() not in _noise and len(n) > 1][:5]


# ── Sub-intent labels for multi-intent queries ────────────────────────────────

def _extract_sub_intents(ql: str) -> List[str]:
    sub = []
    if re.search(r'\bhow\s+many\b|\bcount\b|\bnumber\s+of\b', ql):
        sub.append("count")
    if re.search(r'\banalyze\b|\banalysis\b|\binsight\b', ql):
        sub.append("analysis")
    if re.search(r'\btop\s+\d+\b', ql):
        sub.append("top_n")
    if re.search(r'\bby\s+(?:stage|status|owner|region|month|year)\b|\bbreakdown\b', ql):
        sub.append("breakdown")
    if re.search(r'\bcompare\b|\bvs\.?\b|\bversus\b', ql):
        sub.append("comparison")
    return sub


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    print("=" * 65)
    print("DeepQueryUnderstander — Standalone Tests (rule-based path)")
    print("=" * 65)

    u = DeepQueryUnderstander()
    all_passed = True

    tests = [
        (
            "show me top 5 deals sorted by highest amount",
            {
                "response_shape": "list",
                "result_limit": 5,
                "sort_direction": "desc",
                "primary_intent": "data_fetch",
            },
        ),
        (
            "do we have any open deals?",
            {
                "response_shape": "yes_no",
                "primary_intent": "existence_check",
            },
        ),
        (
            "show me leads from Q3 2023",
            {
                "temporal_type": "date_range",
                "temporal_start_prefix": "2023-07",
                "temporal_end_prefix": "2023-09",
            },
        ),
    ]

    for i, (query, expected) in enumerate(tests, 1):
        print(f"\nTest {i}: {query!r}")
        result = _rule_based_understand(query)

        # Resolve temporal dates
        tf = result.get("temporal_filter", {})
        if tf.get("type") and tf["type"] not in ("none", "date_range"):
            resolved = _resolve_temporal_dates(
                tf["type"], tf.get("value"), tf.get("year"), tf.get("month")
            )
            result["temporal_filter"]["start_date"] = resolved["start_date"]
            result["temporal_filter"]["end_date"] = resolved["end_date"]

        errors = []
        for key, val in expected.items():
            actual = None
            if key == "response_shape":
                actual = result.get("response_shape")
            elif key == "result_limit":
                actual = result.get("result_limit")
            elif key == "primary_intent":
                actual = result.get("primary_intent")
            elif key == "sort_direction":
                actual = result.get("sort_preference", {}).get("direction")
            elif key == "temporal_type":
                actual = result.get("temporal_filter", {}).get("type")
            elif key == "temporal_start_prefix":
                sd = result.get("temporal_filter", {}).get("start_date") or ""
                if not sd.startswith(val):
                    errors.append(f"  start_date={sd!r} (expected prefix {val!r})")
                continue
            elif key == "temporal_end_prefix":
                ed = result.get("temporal_filter", {}).get("end_date") or ""
                if not ed.startswith(val):
                    errors.append(f"  end_date={ed!r} (expected prefix {val!r})")
                continue

            if actual != val:
                errors.append(f"  {key}: got {actual!r}, expected {val!r}")

        if errors:
            print("  FAIL:")
            for e in errors:
                print(e)
            all_passed = False
        else:
            print("  PASS")

        print(f"  Result snapshot:")
        snap = {
            "primary_intent": result.get("primary_intent"),
            "response_shape": result.get("response_shape"),
            "result_limit": result.get("result_limit"),
            "sort_preference": result.get("sort_preference"),
            "temporal_filter": result.get("temporal_filter"),
            "is_multi_intent": result.get("is_multi_intent"),
            "needs_relationship_join": result.get("needs_relationship_join"),
        }
        print(f"  {json.dumps(snap, indent=4, default=str)}")

    print("\n" + "=" * 65)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 65)
