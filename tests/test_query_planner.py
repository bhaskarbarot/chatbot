"""
Unit tests for agents/query_planner.py

Tests focus on:
  - JSON extraction from messy LLM output
  - Plan schema validation
  - Post-processing correctness
  - Blocked operator detection
  - Extended JSON normalization
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.query_planner import (
    _extract_json,
    _validate_plan_schema,
    _postprocess_plan,
    _build_repair_prompt,
    _build_filter_from_process,
    _normalize_extended_json,
    _contains_blocked_operator,
    _fix_dates,
)
from knowledge.vector_store import evaluate_match_confidence


# ── _extract_json ─────────────────────────────────────

class TestExtractJson:
    def test_bare_json(self):
        raw = '{"type": "find", "collection": "deals", "filter": {}}'
        result = _extract_json(raw)
        assert result == {"type": "find", "collection": "deals", "filter": {}}

    def test_json_in_markdown_block(self):
        raw = 'Here is the plan:\n```json\n{"type": "count", "collection": "invoices", "filter": {}}\n```'
        result = _extract_json(raw)
        assert result is not None
        assert result["type"] == "count"

    def test_json_wrapped_in_text(self):
        raw = 'Response: {"type": "find", "collection": "contacts", "filter": {}} END'
        result = _extract_json(raw)
        assert result is not None
        assert result["collection"] == "contacts"

    def test_returns_none_for_non_json(self):
        result = _extract_json("I cannot help with that query.")
        assert result is None

    def test_returns_none_for_empty_string(self):
        assert _extract_json("") is None

    def test_nested_json(self):
        raw = '{"type": "aggregate", "collection": "deals", "pipeline": [{"$match": {"deleted": false}}]}'
        result = _extract_json(raw)
        assert result is not None
        assert result["type"] == "aggregate"
        assert isinstance(result["pipeline"], list)

    def test_json_with_trailing_garbage(self):
        raw = '{"type": "count", "collection": "deals", "filter": {}} \n\n Some extra text'
        result = _extract_json(raw)
        assert result is not None
        assert result["type"] == "count"


# ── _validate_plan_schema ─────────────────────────────

class TestValidatePlanSchema:
    def _make_find(self, **kwargs):
        base = {"type": "find", "collection": "deals", "filter": {}}
        base.update(kwargs)
        return base

    def test_valid_find_plan(self):
        plan = {"type": "find", "collection": "deals", "filter": {}}
        assert _validate_plan_schema(plan) is None

    def test_valid_aggregate_plan(self):
        plan = {
            "type": "aggregate",
            "collection": "invoices",
            "pipeline": [{"$match": {"deleted": False}}],
        }
        assert _validate_plan_schema(plan) is None

    def test_valid_count_plan(self):
        plan = {"type": "count", "collection": "createtasks", "filter": {}}
        assert _validate_plan_schema(plan) is None

    def test_valid_distinct_plan(self):
        plan = {"type": "distinct", "collection": "deals", "field": "stage", "filter": {}}
        assert _validate_plan_schema(plan) is None

    def test_missing_type(self):
        plan = {"collection": "deals", "filter": {}}
        err = _validate_plan_schema(plan)
        assert err is not None
        assert "type" in err.lower() or "invalid" in err.lower()

    def test_invalid_collection(self):
        plan = {"type": "find", "collection": "nonexistent_collection_xyz", "filter": {}}
        err = _validate_plan_schema(plan)
        assert err is not None
        assert "collection" in err.lower() or "unknown" in err.lower()

    def test_find_with_pipeline_rejected(self):
        plan = {"type": "find", "collection": "deals", "filter": {}, "pipeline": []}
        err = _validate_plan_schema(plan)
        assert err is not None

    def test_aggregate_empty_pipeline_rejected(self):
        plan = {"type": "aggregate", "collection": "deals", "pipeline": []}
        err = _validate_plan_schema(plan)
        assert err is not None

    def test_invalid_aggregate_stage(self):
        plan = {
            "type": "aggregate",
            "collection": "deals",
            "pipeline": [{"notanoperator": {}}],
        }
        err = _validate_plan_schema(plan)
        assert err is not None

    def test_unknown_top_level_key_rejected(self):
        plan = {"type": "find", "collection": "deals", "filter": {}, "malicious_key": "val"}
        err = _validate_plan_schema(plan)
        assert err is not None

    def test_blocked_operator_rejected(self):
        plan = {
            "type": "find",
            "collection": "deals",
            "filter": {"$where": "function() { return true; }"},
        }
        err = _validate_plan_schema(plan)
        assert err is not None

    def test_valid_multi_step(self):
        plan = {
            "type": "multi_step",
            "steps": [
                {"name": "s1", "type": "find", "collection": "companies", "filter": {}},
                {"name": "s2", "type": "find", "collection": "deals", "filter": {}},
            ],
        }
        assert _validate_plan_schema(plan) is None


# ── _contains_blocked_operator ────────────────────────

class TestBlockedOperators:
    def test_where_blocked(self):
        assert _contains_blocked_operator({"$where": "1==1"}) is True

    def test_function_blocked(self):
        assert _contains_blocked_operator({"$function": {}}) is True

    def test_nested_blocked(self):
        obj = {"filter": {"$match": {"field": {"$where": "1==1"}}}}
        assert _contains_blocked_operator(obj) is True

    def test_safe_operators_allowed(self):
        obj = {"$match": {"$and": [{"$gt": 1}]}}
        assert _contains_blocked_operator(obj) is False


# ── _normalize_extended_json ──────────────────────────

class TestNormalizeExtendedJson:
    def test_date_wrapper_converted(self):
        from datetime import datetime
        obj = {"$date": "2025-01-15T00:00:00"}
        result = _normalize_extended_json(obj)
        assert isinstance(result, datetime)

    def test_nested_date_wrapper(self):
        from datetime import datetime
        obj = {"filter": {"createdAt": {"$gte": {"$date": "2025-01-01T00:00:00"}}}}
        result = _normalize_extended_json(obj)
        assert isinstance(result["filter"]["createdAt"]["$gte"], datetime)

    def test_non_date_dict_unchanged(self):
        obj = {"type": "find", "collection": "deals"}
        result = _normalize_extended_json(obj)
        assert result == obj

    def test_list_recurse(self):
        from datetime import datetime
        obj = [{"$date": "2025-06-01T00:00:00"}]
        result = _normalize_extended_json(obj)
        assert isinstance(result[0], datetime)


# ── _postprocess_plan ─────────────────────────────────

class TestPostprocessPlan:
    def test_top_n_adds_limit_to_find(self):
        plan = {"type": "find", "collection": "deals", "filter": {}}
        result = _postprocess_plan(plan, "show top 5 deals")
        assert result["limit"] == 5

    def test_top_n_adds_limit_to_pipeline(self):
        plan = {
            "type": "aggregate",
            "collection": "deals",
            "pipeline": [{"$match": {}}],
        }
        result = _postprocess_plan(plan, "top 3 customers")
        stages = result["pipeline"]
        limit_stages = [s for s in stages if "$limit" in s]
        assert len(limit_stages) == 1
        assert limit_stages[0]["$limit"] == 3

    def test_response_hint_injected_when_missing(self):
        plan = {"type": "find", "collection": "deals", "filter": {}}
        result = _postprocess_plan(plan, "show me deals in a table")
        assert "response_hint" in result

    def test_first_invoice_sets_limit_1(self):
        plan = {"type": "find", "collection": "invoices", "filter": {}}
        result = _postprocess_plan(plan, "get first invoice details")
        assert result.get("limit") == 1
        assert result.get("response_hint") == "detail"

    def test_invoice_identifier_uses_exact_match_and_limit_1(self):
        plan = {"type": "find", "collection": "invoices", "filter": {"invoice_number": {"$regex": "ELSN"}}}
        result = _postprocess_plan(plan, "get invoice ELSN/2025/1 details")
        assert result.get("limit") == 1
        assert result["filter"]["invoice_number"] == "ELSN/2025/1"
        assert not isinstance(result["filter"]["invoice_number"], dict)

    def test_so_number_uses_exact_match_and_limit_1(self):
        plan = {"type": "find", "collection": "sales", "filter": {}}
        result = _postprocess_plan(plan, "show sales order number SO/2026/44")
        assert result.get("limit") == 1
        assert result["filter"]["sales_number"] == "SO/2026/44"

    def test_contact_name_uses_exact_first_last_and_limit_1(self):
        plan = {"type": "find", "collection": "contacts", "filter": {}}
        result = _postprocess_plan(plan, "show contact John Doe details")
        assert result.get("limit") == 1
        assert result["filter"]["firstName"] == "John"
        assert result["filter"]["lastName"] == "Doe"


class TestRetrievalConfidence:
    def test_high_score_gate_accepts_even_with_small_margin(self):
        conf = evaluate_match_confidence([
            {"final_score": 0.76},
            {"final_score": 0.75},
        ])
        assert conf["is_confident"] is True
        assert conf["gate"] == "high_score"

    def test_margin_gate_accepts_moderate_score_with_clear_gap(self):
        conf = evaluate_match_confidence([
            {"final_score": 0.62},
            {"final_score": 0.53},
        ])
        assert conf["is_confident"] is True
        assert conf["gate"] == "margin_gate"

    def test_reject_when_score_below_min(self):
        conf = evaluate_match_confidence([
            {"final_score": 0.54},
            {"final_score": 0.44},
        ])
        assert conf["is_confident"] is False
        assert conf["gate"] == "rejected"

    def test_reject_when_margin_too_small_and_not_high_score(self):
        conf = evaluate_match_confidence([
            {"final_score": 0.60},
            {"final_score": 0.57},
        ])
        assert conf["is_confident"] is False
        assert conf["gate"] == "rejected"


class TestRepairPrompt:
    def test_projection_expression_error_forces_aggregate(self):
        prompt = _build_repair_prompt(
            "BASE",
            "Find projection cannot contain aggregation expressions."
        )
        assert "switch to type='aggregate'" in prompt
        assert "Do NOT return type='find'" in prompt


class TestDateAndOverdueRules:
    def test_fix_dates_supports_week_tokens(self):
        result = _fix_dates({"createdAt": {"$gte": "startOfWeek", "$lte": "endOfWeek"}})
        assert isinstance(result["createdAt"]["$gte"], str)
        assert isinstance(result["createdAt"]["$lte"], str)
        assert result["createdAt"]["$gte"].startswith("20")
        assert result["createdAt"]["$lte"].startswith("20")

    def test_overdue_invoice_uses_not_paid(self):
        filt = _build_filter_from_process("overdue invoices", {}, "invoices", "overdue invoices")
        assert filt["due_date"]["$lt"] == "today"
        assert filt["payment_status"]["$ne"] == "paid"

    def test_overdue_tasks_excludes_completed(self):
        filt = _build_filter_from_process("overdue tasks", {}, "createtasks", "overdue tasks")
        assert filt["due_date"]["$lt"] == "today"
        assert "Completed" in filt["status"]["$nin"]
