"""
Query Planner Agent.
Takes a user query + matched rules + chat context and generates
a structured MongoDB query plan using the local Ollama model.
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import (
    OLLAMA_BASE_URL, OLLAMA_MODEL, COLLECTION_NAMES,
    OLLAMA_NUM_PREDICT, OLLAMA_TIMEOUT_S, LLM_PLAN_RETRIES,
)
from utils.logger import get_logger, PipelineTimer
from utils.metrics import record_event
from knowledge.prompts import build_query_prompt

logger = get_logger("query_planner")
VALID_PLAN_TYPES = {"find", "aggregate", "count", "distinct", "multi_step"}
ALLOWED_TOP_KEYS = {
    "type", "collection", "filter", "projection", "sort", "limit",
    "pipeline", "steps", "lookups", "response_hint", "entity", "field",
}
MONGO_OPERATOR_WHITELIST = {
    "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$and", "$or", "$not",
    "$exists", "$regex", "$options", "$elemMatch", "$size", "$type", "$all",
    "$expr", "$sum", "$avg", "$min", "$max", "$first", "$last", "$push", "$addToSet",
    "$group", "$project", "$match", "$sort", "$limit", "$skip", "$lookup", "$unwind",
    "$addFields", "$set", "$unset", "$dateToString", "$cond", "$ifNull", "$multiply",
    "$divide", "$round", "$month", "$year", "$toString", "$count",
    "$dateSubtract", "$dateAdd", "$dateFromParts", "$dateFromString", "$toDate",
    "$switch", "$concat", "$substr", "$substrBytes", "$trim", "$replaceAll",
    "$arrayElemAt", "$filter", "$map", "$reduce", "$let",
}
MONGO_OPERATOR_BLOCKLIST = {"$where", "$function", "$accumulator"}
_llm = None
_llm_lock = __import__("threading").Lock()


def get_llm():
    """Get Ollama LLM instance (thread-safe singleton)."""
    global _llm
    if _llm is not None:
        return _llm
    with _llm_lock:
        if _llm is None:
            from langchain_ollama import ChatOllama
            _llm = ChatOllama(
                model=OLLAMA_MODEL,
                base_url=OLLAMA_BASE_URL,
                temperature=0,
                num_predict=OLLAMA_NUM_PREDICT,
                top_p=0.9,
                timeout=OLLAMA_TIMEOUT_S,
            )
    return _llm


def plan_query(user_query: str, matched_rules: List[Dict],
               chat_context: str = "",
               resolved_query: str = None,
               format_hint: str = "auto",
               relationship_context: str = "",
               sort_hint: str = "",
               limit_override: int = None) -> Optional[Dict]:
    """
    Generate a MongoDB query plan from a user query.
    Returns a structured query plan dict or None on failure.
    Wraps all LLM calls with circuit breaker — returns None if breaker is OPEN.
    """
    from utils.circuit_breaker import get_llm_breaker
    breaker = get_llm_breaker()

    if not breaker.is_available():
        logger.warning("LLM circuit breaker is OPEN — skipping LLM planning")
        record_event("planner_circuit_open", {"query": user_query[:80]})
        return None

    with PipelineTimer("LLM query planning", logger):
        llm = get_llm()

        try:
            prompt = build_query_prompt(
                user_query, matched_rules, chat_context, resolved_query, format_hint,
                relationship_context=relationship_context,
                sort_hint=sort_hint,
                limit_override=limit_override,
            )
            plan, error = _generate_and_validate_plan_safe(llm, prompt, user_query, breaker)
            if plan:
                record_event("planner_plan_valid", {
                    "plan_type": plan.get("type"),
                    "collection": plan.get("collection"),
                    "retry_count": 0,
                })
                logger.info(
                    f"Query plan: type={plan.get('type')}, "
                    f"collection={plan.get('collection', 'multi')}"
                )
                return plan

            repaired_error = error
            for attempt in range(max(0, LLM_PLAN_RETRIES)):
                if not breaker.is_available():
                    logger.warning("Circuit breaker opened during retry — aborting")
                    break
                record_event("planner_plan_invalid", {
                    "attempt": attempt + 1,
                    "error": repaired_error,
                })
                logger.warning(
                    f"Planner validation failed, retry {attempt + 1}/{LLM_PLAN_RETRIES}: {repaired_error}"
                )
                repair_prompt = _build_repair_prompt(
                    prompt, repaired_error or "Invalid or missing JSON query plan."
                )
                repaired_plan, repaired_error = _generate_and_validate_plan_safe(
                    llm, repair_prompt, user_query, breaker
                )
                if repaired_plan:
                    record_event("planner_plan_valid", {
                        "plan_type": repaired_plan.get("type"),
                        "collection": repaired_plan.get("collection"),
                        "retry_count": attempt + 1,
                    })
                    logger.info(
                        f"Query plan (repaired): type={repaired_plan.get('type')}, "
                        f"collection={repaired_plan.get('collection', 'multi')}"
                    )
                    return repaired_plan

            record_event("planner_plan_failed", {"error": repaired_error})
            logger.error(f"Failed to build valid plan after retries: {repaired_error}")
            return None

        except Exception as e:
            record_event("planner_exception", {"error": str(e)})
            logger.error(f"LLM planning error: {e}")
            return None


def _generate_and_validate_plan_safe(
    llm: Any, prompt: str, user_query: str, breaker: Any
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Wrapper that routes ONLY the network call (llm.invoke) through the circuit
    breaker.  Content/validation failures (bad JSON, schema errors) are soft
    errors — they do NOT trip the breaker because they indicate an LLM output
    quality issue, not a connectivity failure.
    """
    result_holder: list = [None]
    error_holder: list = ["No result"]

    def _invoke():
        # Only this line can raise a connectivity exception that trips the breaker.
        response = llm.invoke(prompt)
        raw_text = response.content.strip()
        logger.debug(f"LLM raw output: {raw_text[:500]}")

        # Content validation — failures here silently return None (no exception).
        plan = _extract_json_robust(raw_text)
        if not isinstance(plan, dict):
            error_holder[0] = "Could not extract a JSON object from model output."
            return
        plan = _fix_regex_operators(plan)
        plan = _postprocess_plan(plan, user_query)
        err = _validate_plan_schema(plan)
        if err:
            error_holder[0] = err
            return
        result_holder[0] = plan
        error_holder[0] = None

    breaker.call(_invoke, fallback=None)

    if result_holder[0] is not None:
        return result_holder[0], None
    return None, error_holder[0]



def _build_repair_prompt(base_prompt: str, validation_error: str) -> str:
    """Prompt used when first model output is invalid."""
    error_text = (validation_error or "").strip()
    force_aggregate = "Find projection cannot contain aggregation expressions" in error_text
    extra = ""
    if force_aggregate:
        extra = (
            "\nMANDATORY FIX:\n"
            "- The previous plan incorrectly used type='find' with aggregation expressions in projection.\n"
            "- IMMEDIATELY switch to type='aggregate' and move expression logic into a valid pipeline.\n"
            "- Do NOT return type='find' in this retry.\n"
        )
    return (
        f"{base_prompt}\n\n"
        "Your previous response was invalid.\n"
        f"Validation error: {error_text}\n"
        f"{extra}\n"
        "Return ONLY a corrected JSON object with no extra text."
    )


def plan_query_from_rule(user_query: str, rule: Dict,
                         chat_context: str = "",
                         resolved_query: str = None) -> Optional[Dict]:
    """
    Try to build a query plan directly from a matched rule without LLM.
    This is the fast path for high-confidence rule matches.
    Only works for simple, well-defined rules.
    """
    process = rule.get("process", "")
    collections = rule.get("collections", [])
    params = rule.get("params", [])

    if not collections:
        return None

    # Extract parameter values from the user query
    param_values = _extract_params(user_query, params, resolved_query)

    query_text = (resolved_query or user_query).lower()
    # Try to build a simple rule-derived query
    primary_collection = collections[0]
    filter_dict = _build_filter_from_process(
        process, param_values, primary_collection, query_text=query_text
    )
    if filter_dict is None:
        filter_dict = {}

    limit = _extract_limit(user_query) or _extract_limit(process) or 10
    if re.search(r"\blast\s+\d+\b", query_text):
        limit = _extract_limit(query_text) or limit

    use_count = any(w in query_text for w in ["count", "how many", "number of"])
    if use_count:
        return {
            "type": "count",
            "collection": primary_collection,
            "filter": filter_dict,
            "response_hint": "count",
        }

    # Generic "by owner" fallback when user did not provide an owner value:
    # return grouped ownership summary instead of empty entity lookup.
    if "by owner" in query_text and not param_values and primary_collection in {"contacts", "createtasks", "deals"}:
        owner_field = {
            "contacts": "contactOwner",
            "createtasks": "createdBy",
            "deals": "owner",
        }[primary_collection]
        return {
            "type": "aggregate",
            "collection": primary_collection,
            "pipeline": [
                {"$match": filter_dict},
                {"$group": {"_id": f"${owner_field}", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": limit},
            ],
            "response_hint": "table",
        }

    use_aggregate = _process_suggests_aggregate(process, query_text, collections)
    if not use_aggregate:
        plan = {
            "type": "find",
            "collection": primary_collection,
            "filter": filter_dict,
            "sort": _extract_sort(f"{process} {query_text}") or [("createdAt", -1)],
            "limit": limit,
        }
    else:
        pipeline = [{"$match": filter_dict}]
        for lu in _extract_lookups(process):
            pipeline.append({"$lookup": lu})
            pipeline.append({"$unwind": {
                "path": f"${lu['as']}",
                "preserveNullAndEmptyArrays": True
            }})
        sort = _extract_sort(f"{process} {query_text}")
        if sort:
            pipeline.append({"$sort": dict(sort)})
        pipeline.append({"$limit": limit})
        plan = {
            "type": "aggregate",
            "collection": primary_collection,
            "pipeline": pipeline,
        }

    entity = _extract_entity(user_query, rule, param_values)
    if entity:
        plan["entity"] = entity
    return plan


def _extract_json_robust(text: str) -> Dict:
    """
    Extract JSON from LLM output regardless of wrapping.
    Handles: raw JSON, markdown code blocks, explanation+JSON,
    JSON with trailing text, partial JSON repair.
    """
    if not text or not text.strip():
        return {}

    # Strategy 1: Try direct parse first
    try:
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    # Strategy 2: Extract from markdown code block
    for pattern in [
        r"```json\s*([\s\S]*?)\s*```",
        r"```\s*([\s\S]*?)\s*```",
        r"`({[\s\S]*?})`",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1).strip())
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass

    # Strategy 3: Find first balanced { ... } block
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, c in enumerate(text[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        parsed = json.loads(chunk)
                        return parsed if isinstance(parsed, dict) else {}
                    except Exception:
                        # Try simple repair: remove trailing commas
                        chunk = re.sub(r",\s*}", "}", chunk)
                        chunk = re.sub(r",\s*]", "]", chunk)
                        try:
                            parsed = json.loads(chunk)
                            return parsed if isinstance(parsed, dict) else {}
                        except Exception:
                            break

    return {}


def _extract_json(text: str) -> Optional[Dict]:
    """Backward-compatible alias."""
    parsed = _extract_json_robust(text)
    return parsed if isinstance(parsed, dict) and parsed else None


def _fix_regex_operators(plan: dict) -> dict:
    """
    Fix LLM-generated regex patterns before plan execution.
    Converts invalid nested regex to proper MongoDB regex syntax.
    """
    plan_str = json.dumps(plan)
    plan_str = re.sub(
        r'"\$in"\s*:\s*(\{[^}]*"\$regex"[^}]*\})',
        lambda m: m.group(1),
        plan_str,
    )
    try:
        loaded = json.loads(plan_str)
        return loaded if isinstance(loaded, dict) else plan
    except Exception:
        return plan


def _postprocess_plan(plan: Dict, user_query: str) -> Dict:
    """Validate and fix common issues in generated query plans."""
    plan = _normalize_extended_json(plan)
    plan = _ensure_plan_type(plan)

    # Ensure dates are proper
    plan = _fix_dates(plan)

    # Add limit if "top N" is mentioned
    top_match = re.search(r'top\s+(\d+)', user_query.lower())
    if top_match:
        limit = int(top_match.group(1))
        if plan.get("type") == "aggregate" and plan.get("pipeline"):
            # Add or update $limit in pipeline
            has_limit = any("$limit" in stage for stage in plan["pipeline"])
            if not has_limit:
                plan["pipeline"].append({"$limit": limit})
        elif plan.get("type") == "find":
            plan["limit"] = limit

    q_lower = user_query.lower()
    # "1st/first invoice details" should return a single detailed record.
    if (re.search(r"\b(first|1st)\b", q_lower)
            and "invoice" in q_lower
            and plan.get("type") == "find"):
        plan["limit"] = 1
        plan["response_hint"] = "detail"

    # Detect response format hint
    if "response_hint" not in plan:
        from utils.formatter import detect_format_intent
        plan["response_hint"] = detect_format_intent(user_query)

    plan = _enforce_exact_entity_lookup(plan, user_query)

    return plan


def _ensure_plan_type(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Infer a valid plan type when model output omits/invalidates it."""
    out = dict(plan or {})
    plan_type = out.get("type")
    if plan_type in VALID_PLAN_TYPES:
        return out

    if isinstance(out.get("steps"), list) and out.get("steps"):
        out["type"] = "multi_step"
        return out
    if isinstance(out.get("pipeline"), list) and out.get("pipeline"):
        out["type"] = "aggregate"
        return out
    if isinstance(out.get("field"), str) and out.get("field").strip():
        out["type"] = "distinct"
        return out
    hint = str(out.get("response_hint", "")).lower()
    if hint == "count":
        out["type"] = "count"
        return out

    # Safe default for plain collection/filter plans.
    out["type"] = "find"
    return out


def _enforce_exact_entity_lookup(plan: Dict[str, Any], user_query: str) -> Dict[str, Any]:
    """
    Enforce exact-equality lookup for specific entity identifiers.
    Applies to find/count plans with concrete invoice/deal/SO/contact/user identifiers.
    """
    out = dict(plan or {})
    if out.get("type") not in {"find", "count"}:
        return out

    collection = str(out.get("collection", "")).strip()
    if not collection:
        return out

    filter_dict = out.get("filter") if isinstance(out.get("filter"), dict) else {}
    q = user_query or ""
    q_lower = q.lower()

    invoice_no = _extract_invoice_number(q)
    so_no = _extract_sales_order_number(q)
    deal_name = _extract_entity_name_from_query(q, {"deal"})
    contact_name = _extract_entity_name_from_query(q, {"contact", "person"})
    user_name = _extract_entity_name_from_query(q, {"user", "owner", "rep"})

    if collection == "invoices" and invoice_no:
        filter_dict.pop("invoice_number", None)
        filter_dict["invoice_number"] = invoice_no
        out["limit"] = 1
        return _strip_regex_for_exact_fields(out, {"invoice_number"})

    if collection == "sales" and so_no:
        filter_dict.pop("sales_number", None)
        filter_dict["sales_number"] = so_no
        out["limit"] = 1
        return _strip_regex_for_exact_fields(out, {"sales_number"})

    if collection == "deals" and deal_name:
        filter_dict.pop("name", None)
        filter_dict["name"] = deal_name
        out["limit"] = 1
        return _strip_regex_for_exact_fields(out, {"name"})

    if collection == "users" and user_name:
        filter_dict.pop("name", None)
        filter_dict["name"] = user_name
        out["limit"] = 1
        return _strip_regex_for_exact_fields(out, {"name"})

    if collection == "contacts" and contact_name:
        parts = [p for p in contact_name.split() if p]
        if len(parts) >= 2:
            filter_dict.pop("firstName", None)
            filter_dict.pop("lastName", None)
            filter_dict["firstName"] = parts[0]
            filter_dict["lastName"] = " ".join(parts[1:])
            out["limit"] = 1
            out["filter"] = filter_dict
            return _strip_regex_for_exact_fields(out, {"firstName", "lastName"})
        # Single-token contact lookup still exact, no regex.
        filter_dict["$or"] = [{"firstName": contact_name}, {"lastName": contact_name}]
        out["limit"] = 1
        out["filter"] = filter_dict
        return _strip_regex_for_exact_fields(out, {"firstName", "lastName"})

    out["filter"] = filter_dict
    return out


def _extract_invoice_number(query: str) -> Optional[str]:
    # Matches invoice-like IDs including slashes/hyphens, e.g. ELSN/2025/1
    m = re.search(r"\b([A-Z]{2,}[A-Z0-9]*[/-]\d{2,4}[/-]\d+)\b", query, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_sales_order_number(query: str) -> Optional[str]:
    m = re.search(
        r"\b(?:so|sales\s*order)\s*(?:number|no|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b",
        query,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _extract_entity_name_from_query(query: str, entity_words: set[str]) -> Optional[str]:
    qw = "|".join(re.escape(w) for w in sorted(entity_words, key=len, reverse=True))
    patterns = [
        rf"\b(?:for|of|named|name is)\s+([A-Za-z][A-Za-z0-9 .&'_-]{{1,80}}?)\s+(?:{qw})\b",
        rf"\b(?:{qw})\s+(?:named|name is)\s+([A-Za-z][A-Za-z0-9 .&'_-]{{1,80}})\b",
        rf"\b(?:{qw})\s+([A-Za-z][A-Za-z0-9 .&'_-]{{1,80}})\b",
    ]
    for p in patterns:
        m = re.search(p, query, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip().strip(".,")
            candidate = re.sub(r"\b(details?|summary|record|records)\b.*$", "", candidate, flags=re.IGNORECASE).strip()
            if candidate:
                return candidate
    return None


def _strip_regex_for_exact_fields(plan: Dict[str, Any], fields: set[str]) -> Dict[str, Any]:
    out = dict(plan)
    filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
    for field in fields:
        val = filt.get(field)
        if isinstance(val, dict) and "$regex" in val:
            filt[field] = val.get("$regex")
    # Clean regex from simple OR conditions for these fields.
    if isinstance(filt.get("$or"), list):
        new_or = []
        for cond in filt["$or"]:
            if isinstance(cond, dict) and len(cond) == 1:
                key = next(iter(cond.keys()))
                if key in fields and isinstance(cond[key], dict) and "$regex" in cond[key]:
                    new_or.append({key: cond[key]["$regex"]})
                    continue
            new_or.append(cond)
        filt["$or"] = new_or
    out["filter"] = filt
    return out


def _validate_plan_schema(plan: Dict) -> Optional[str]:
    """Return validation error message, or None if valid."""
    if not isinstance(plan, dict):
        return "Plan must be a JSON object."

    for key in plan:
        if key not in ALLOWED_TOP_KEYS:
            return f"Unsupported top-level key: {key}"

    plan_type = plan.get("type")
    if plan_type not in VALID_PLAN_TYPES:
        return f"Invalid or missing plan type: {plan_type}"

    if plan_type != "multi_step":
        collection = plan.get("collection")
        if not isinstance(collection, str) or not collection.strip():
            return "Non-multi_step plans require a non-empty collection."
        if collection not in COLLECTION_NAMES:
            return f"Unknown collection in plan: {collection}"

    if plan_type == "find":
        if "pipeline" in plan:
            return "Find plans cannot include pipeline."
        if plan.get("filter") is not None and not isinstance(plan.get("filter"), dict):
            return "Find filter must be an object."
        if plan.get("projection") is not None and not isinstance(plan.get("projection"), dict):
            return "Find projection must be an object."
        projection = plan.get("projection") or {}
        if _contains_expression_projection(projection):
            return "Find projection cannot contain aggregation expressions."

    if plan_type == "aggregate":
        pipeline = plan.get("pipeline")
        if not isinstance(pipeline, list) or not pipeline:
            return "Aggregate plans require a non-empty pipeline list."
        for stage in pipeline:
            if not isinstance(stage, dict) or len(stage) != 1:
                return "Each aggregate stage must be a single-key object."
            stage_key = next(iter(stage.keys()))
            if not isinstance(stage_key, str) or not stage_key.startswith("$"):
                return f"Invalid aggregate stage operator: {stage_key}"

    if plan_type == "count":
        if "pipeline" in plan:
            return "Count plans cannot include pipeline."
        if plan.get("filter") is not None and not isinstance(plan.get("filter"), dict):
            return "Count filter must be an object."

    if plan_type == "distinct":
        if not isinstance(plan.get("field"), str) or not plan.get("field"):
            return "Distinct plans require a non-empty field string."
        if plan.get("filter") is not None and not isinstance(plan.get("filter"), dict):
            return "Distinct filter must be an object."

    if plan_type == "multi_step":
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            return "Multi-step plans require a non-empty steps list."
        for step in steps:
            if not isinstance(step, dict):
                return "Each multi-step step must be an object."
            step_type = step.get("type")
            if step_type not in {"find", "aggregate", "count", "distinct"}:
                return f"Invalid step type in multi_step: {step_type}"
            step_collection = step.get("collection")
            if not isinstance(step_collection, str) or step_collection not in COLLECTION_NAMES:
                return f"Invalid or unknown collection in step: {step_collection}"

    if _contains_blocked_operator(plan):
        return "Plan contains blocked Mongo operators."

    return None


def _contains_expression_projection(projection: Dict[str, Any]) -> bool:
    """Reject projection expressions for find plans."""
    for value in projection.values():
        if isinstance(value, dict):
            for key in value.keys():
                if key.startswith("$"):
                    return True
    return False


def _contains_blocked_operator(obj: Any) -> bool:
    """Block only dangerous operators while allowing evolving safe operators."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in MONGO_OPERATOR_BLOCKLIST:
                return True
            if key.startswith("$") and key not in MONGO_OPERATOR_WHITELIST:
                # Unknown operators are tolerated to avoid brittle failures.
                # MongoDB will reject invalid ones at execution, and retry/fallback handles it.
                logger.debug(f"Unknown operator observed in plan: {key}")
            if _contains_blocked_operator(value):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _contains_blocked_operator(item):
                return True
    return False


def _normalize_extended_json(obj: Any) -> Any:
    """Convert Mongo shell extended JSON wrappers into python literals."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$date"} and isinstance(obj["$date"], str):
            raw = obj["$date"].strip()
            raw = raw.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return obj["$date"]
        return {k: _normalize_extended_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_extended_json(v) for v in obj]
    return obj


def _fix_dates(obj: Any) -> Any:
    """Convert date strings to proper format in query objects."""
    if isinstance(obj, str):
        # Check for date-like patterns
        date_patterns = [
            (r'startOfWeek', lambda: (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                                      - timedelta(days=datetime.now().weekday())).isoformat()),
            (r'endOfWeek', lambda: ((datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                                     - timedelta(days=datetime.now().weekday()))
                                    + timedelta(days=6, hours=23, minutes=59, seconds=59)).isoformat()),
            (r'startOfMonth', lambda: datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()),
            (r'endOfMonth', lambda: ((datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(microseconds=1)).isoformat()),
            (r'startOfYear', lambda: datetime.now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()),
            (r'endOfYear', lambda: datetime.now().replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=0).isoformat()),
            (r'today', lambda: datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()),
            (r'now', lambda: datetime.now().isoformat()),
        ]
        for pattern, replacement in date_patterns:
            if re.fullmatch(pattern, obj.strip(), re.IGNORECASE):
                return replacement()
        if re.match(r'\d{4}-\d{2}-\d{2}', obj):
            return obj  # Already ISO format
        return obj
    elif isinstance(obj, dict):
        return {k: _fix_dates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_fix_dates(item) for item in obj]
    return obj


def _extract_params(query: str, param_names: list,
                    resolved_query: str = None) -> Dict:
    """Extract parameter values from the user query."""
    params = {}
    q = resolved_query or query

    for param in param_names:
        p_lower = param.lower()
        if p_lower in ("name", "company", "account name"):
            # Capture FULL name including hyphens, commas, ampersands — never truncate
            name_match = re.search(
                r'(?:for|of|about|named?|company|account)\s+["\']?([\w][\w\s\-&.,]{1,120}?)["\']?'
                r'(?:\s*$|\s+\b(?:and|or|with|from|in|give|show|get|please)\b)',
                q, re.IGNORECASE
            )
            if name_match:
                params[param] = name_match.group(1).strip().rstrip(".,;")
        elif p_lower in ("date", "start date", "end date"):
            date_match = re.search(r'(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})', q)
            if date_match:
                params[param] = date_match.group(1)
        elif p_lower in ("user", "owner", "rep"):
            # Capture full user name with hyphens/spaces
            name_match = re.search(
                r'(?:user|owner|rep|for|by)\s+["\']?([\w][\w\s\-&.,]{1,80}?)["\']?(?:\s*$|\s+\b)',
                q, re.IGNORECASE
            )
            if name_match:
                params[param] = name_match.group(1).strip().rstrip(".,;")

    return params


def _build_filter_from_process(process: str, params: Dict,
                                collection: str, query_text: str = "") -> Optional[Dict]:
    """Try to build a MongoDB filter dict from process text."""
    filter_dict: Dict[str, Any] = {}
    text = process or ""
    lower = text.lower()

    # Parse direct equality hints like `field`='value'
    for field, value in re.findall(r"`([\w.]+)`\s*=\s*'([^']+)'", text):
        filter_dict[field] = value

    # Parse null checks
    for field in re.findall(r"`([\w.]+)`\s*=\s*null", lower):
        filter_dict[field] = None

    # Parse IN list conditions
    for field, values in re.findall(r"`([\w.]+)`\s+in\s+\[([^\]]+)\]", lower):
        parsed = [v.strip(" '\"") for v in values.split(",") if v.strip()]
        if parsed:
            filter_dict[field] = {"$in": parsed}

    # Common temporal/process hints from business rules
    if "past due" in lower or "overdue" in lower:
        filter_dict.setdefault("due_date", {"$lt": "today"})
        if collection == "invoices":
            filter_dict.setdefault("payment_status", {"$ne": "paid"})
        if collection == "createtasks":
            filter_dict.setdefault("status", {"$nin": ["Completed", "completed"]})
    if "this week" in lower:
        filter_dict.setdefault("createdAt", {"$gte": "startOfWeek"})
        filter_dict["createdAt"]["$lte"] = "endOfWeek"
    if "this month" in lower:
        filter_dict.setdefault("createdAt", {"$gte": "startOfMonth"})
    if "this year" in lower or "financial year" in lower:
        filter_dict.setdefault("createdAt", {"$gte": "startOfYear"})

    # Use extracted named parameter
    if params:
        first_param = next(iter(params.values()))
        # Bind to a likely textual field in a generic way
        likely_fields = ["companyName", "name", "invoice_number", "sales_number", "email"]
        chosen_field = next((f for f in likely_fields if f in text), likely_fields[0])
        filter_dict.setdefault(chosen_field, {"$regex": str(first_param), "$options": "i"})

    # Apply direct user-intent cues from query text.
    # IMPORTANT: relative temporal windows must be computed from "now" at runtime
    # so they never become stale from cached/rule-template periods.
    q = (query_text or "").lower()
    m_last_days = re.search(r"\blast\s+(\d+)\s+days?\b", q)
    m_last_weeks = re.search(r"\blast\s+(\d+)\s+weeks?\b", q)
    m_last_months = re.search(r"\blast\s+(\d+)\s+months?\b", q)
    if m_last_days or m_last_weeks or m_last_months:
        now_dt = datetime.now()
        if m_last_days:
            n_days = max(1, int(m_last_days.group(1)))
            start_dt = now_dt - timedelta(days=n_days)
        elif m_last_weeks:
            n_weeks = max(1, int(m_last_weeks.group(1)))
            start_dt = now_dt - timedelta(days=n_weeks * 7)
        else:
            n_months = max(1, int(m_last_months.group(1)))
            start_dt = now_dt - timedelta(days=n_months * 30)
        filter_dict["createdAt"] = {
            "$gte": start_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "$lte": now_dt.isoformat(),
        }

    if collection == "deals":
        if "closed won" in q or "won deals" in q:
            filter_dict.setdefault("dealWonAt", {"$ne": None})
            filter_dict.setdefault("dealLostAt", None)
        if "closed lost" in q or "lost deals" in q:
            filter_dict.setdefault("dealLostAt", {"$ne": None})
        if "open deals" in q:
            filter_dict.setdefault("dealWonAt", None)
            filter_dict.setdefault("dealLostAt", None)
        if "proposal stage" in q:
            filter_dict.setdefault("stage", {"$regex": "proposal", "$options": "i"})

    if collection == "invoices":
        if "paid invoices" in q or ("paid" in q and "invoice" in q):
            filter_dict.setdefault("payment_status", "paid")
        if "pending invoices" in q or ("pending" in q and "invoice" in q):
            filter_dict.setdefault("payment_status", {"$in": ["draft", "confirmed", "partial_payment"]})
        if "overdue" in q:
            filter_dict.setdefault("due_date", {"$lt": "today"})
            filter_dict.setdefault("payment_status", {"$in": ["draft", "confirmed", "partial_payment"]})

    if collection == "createtasks":
        if "pending" in q:
            filter_dict.setdefault("status", {"$in": ["Pending", "pending", "Open", "open"]})
        if "overdue" in q:
            filter_dict.setdefault("due_date", {"$lt": "today"})

    return filter_dict


def _process_suggests_aggregate(process: str, query_text: str, collections: List[str]) -> bool:
    merged = f"{process} {query_text}".lower()
    aggregate_terms = [
        "group", "sum", "avg", "average", "trend", "rate", "conversion",
        "breakdown", "by owner", "by stage", "monthly", "yearly", "top performing",
        "total outstanding", "pipeline", "lookup"
    ]
    if any(t in merged for t in aggregate_terms):
        return True
    return len(collections) > 1


def _extract_sort(process: str) -> Optional[List]:
    """Extract sort specification from process text."""
    sort_match = re.search(r'sort\s+by\s+`?(\w+)`?\s*(desc|asc)?', process, re.IGNORECASE)
    if sort_match:
        field = sort_match.group(1)
        direction = -1 if sort_match.group(2) and sort_match.group(2).lower() == "desc" else 1
        return [(field, direction)]
    if "desc" in process.lower():
        if "createdAt" in process:
            return [("createdAt", -1)]
        if "grand_total" in process:
            return [("grand_total_in_usd", -1)]
    return None


def _extract_limit(query: str) -> Optional[int]:
    """Extract limit from user query."""
    match = re.search(r'(?:top|first|limit|latest|recent|last)\s+(\d+)', query.lower())
    if match:
        return int(match.group(1))
    ordinal_match = re.search(r'\b(\d+)(?:st|nd|rd|th)\b', query.lower())
    if ordinal_match:
        return int(ordinal_match.group(1))
    return None


def _extract_lookups(process: str) -> List[Dict]:
    """Extract $lookup specifications from process text."""
    lookups = []
    # Pattern: "Lookup `collection` on `field`"
    for match in re.finditer(
        r'[Ll]ookup\s+`(\w+)`\s+on\s+`(\w+)`\s+for\s+(\w+)',
        process
    ):
        from_coll = match.group(1)
        local_field = match.group(2)
        alias = match.group(3)
        lookups.append({
            "from": from_coll,
            "localField": local_field,
            "foreignField": "_id",
            "as": alias + "Info"
        })
    return lookups


def _extract_entity(query: str, rule: Dict,
                    params: Dict) -> Optional[Dict]:
    """Extract entity information for memory tracking."""
    if params:
        first_param = list(params.values())[0]
        # Determine type from collection
        colls = rule.get("collections", [])
        type_map = {
            "companies": "company", "contacts": "contact",
            "deals": "deal", "invoices": "invoice",
            "users": "user", "sales": "sales_order",
            "outreaches": "outreach", "vendors": "vendor",
        }
        entity_type = "unknown"
        for c in colls:
            if c in type_map:
                entity_type = type_map[c]
                break
        return {"type": entity_type, "name": first_param}
    return None


class QueryPlanner:
    """
    Backward-compatible wrapper for planner module functions.
    """

    def plan(
        self,
        user_query: str,
        matched_rules: List[Dict],
        chat_context: str = "",
        resolved_query: str = None,
        format_hint: str = "auto",
        relationship_context: str = "",
        sort_hint: str = "",
        limit_override: int = None,
    ) -> Optional[Dict]:
        return plan_query(
            user_query=user_query,
            matched_rules=matched_rules,
            chat_context=chat_context,
            resolved_query=resolved_query,
            format_hint=format_hint,
            relationship_context=relationship_context,
            sort_hint=sort_hint,
            limit_override=limit_override,
        )

    def _extract_json_robust(self, text: str) -> dict:
        return _extract_json_robust(text)
