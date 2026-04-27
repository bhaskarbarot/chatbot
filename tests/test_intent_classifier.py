"""
Unit tests for knowledge/intent_classifier.py

Tests: classification accuracy, collection mapping, fast plan generation.
"""
import sys
import os
import pytest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from knowledge.intent_classifier import (
    classify,
    get_collection,
    build_fast_plan,
    CONFIDENCE_THRESHOLD,
    Intent,
)


# ── classify ──────────────────────────────────────────

class TestClassify:
    @pytest.mark.parametrize("query, expected_intent", [
        ("how many deals do we have", Intent.COUNT),
        ("how many contacts are in the system", Intent.COUNT),
        ("total number of companies", Intent.COUNT),
        ("total revenue this financial year", Intent.REVENUE_TOTAL),
        ("total revenue paid invoices", Intent.REVENUE_TOTAL),
        ("overdue invoices", Intent.OVERDUE_INVOICES),
        ("past due payments", Intent.OVERDUE_INVOICES),
        ("overdue tasks this week", Intent.OVERDUE_TASKS),
        ("closed won deals this month", Intent.CLOSED_WON),
        ("deals won last quarter", Intent.CLOSED_WON),
        ("closed lost deals", Intent.CLOSED_LOST),
        ("sales pipeline by stage", Intent.PIPELINE_BY_STAGE),
        ("show deals by stage", Intent.PIPELINE_BY_STAGE),
        ("pending tasks for the team", Intent.PENDING_TASKS),
        ("who is Rahul Sharma", Intent.PERSON_LOOKUP),
        ("show last 5 deals", Intent.LIST_RECENT),
    ])
    def test_correct_intent(self, query, expected_intent):
        intent, conf = classify(query)
        assert intent == expected_intent, f"Query '{query}': expected {expected_intent}, got {intent}"

    def test_high_confidence_for_clear_queries(self):
        _, conf = classify("how many deals are there")
        assert conf >= CONFIDENCE_THRESHOLD

    def test_unknown_for_unrelated_query(self):
        intent, conf = classify("what is the weather today")
        assert intent == Intent.UNKNOWN or conf < CONFIDENCE_THRESHOLD

    def test_person_lookup_anchored_to_start(self):
        intent, conf = classify("who is John Smith")
        assert intent == Intent.PERSON_LOOKUP
        assert conf >= CONFIDENCE_THRESHOLD

    def test_count_intent_confidence_boosted_by_entity(self):
        _, conf_with_entity = classify("how many deals are open")
        _, conf_without_entity = classify("how many are there")
        assert conf_with_entity >= conf_without_entity


# ── get_collection ────────────────────────────────────

class TestGetCollection:
    @pytest.mark.parametrize("intent, query, expected_coll", [
        (Intent.REVENUE_TOTAL, "total revenue", "invoices"),
        (Intent.OVERDUE_INVOICES, "overdue invoices", "invoices"),
        (Intent.OVERDUE_TASKS, "overdue tasks", "createtasks"),
        (Intent.CLOSED_WON, "closed won deals", "deals"),
        (Intent.CLOSED_LOST, "closed lost deals", "deals"),
        (Intent.PIPELINE_BY_STAGE, "pipeline by stage", "deals"),
        (Intent.PENDING_TASKS, "pending tasks", "createtasks"),
        (Intent.PERSON_LOOKUP, "who is X", "users"),
        (Intent.COUNT, "how many deals", "deals"),
        (Intent.COUNT, "how many invoices", "invoices"),
        (Intent.COUNT, "how many contacts", "contacts"),
        (Intent.LIST_RECENT, "last 5 deals", "deals"),
    ])
    def test_collection_mapping(self, intent, query, expected_coll):
        result = get_collection(intent, query)
        assert result == expected_coll, f"intent={intent} query='{query}': expected {expected_coll}, got {result}"

    def test_unknown_intent_returns_none(self):
        result = get_collection(Intent.UNKNOWN, "random query")
        assert result is None


# ── build_fast_plan ───────────────────────────────────

class TestBuildFastPlan:
    def test_count_plan_structure(self):
        plan = build_fast_plan(Intent.COUNT, "deals", "how many deals")
        assert plan is not None
        assert plan["type"] == "count"
        assert plan["collection"] == "deals"
        assert plan["response_hint"] == "count"

    def test_revenue_plan_structure(self):
        plan = build_fast_plan(Intent.REVENUE_TOTAL, "invoices", "total revenue this year")
        assert plan is not None
        assert plan["type"] == "aggregate"
        assert plan["collection"] == "invoices"
        pipeline = plan["pipeline"]
        match_stage = pipeline[0].get("$match", {})
        assert match_stage.get("payment_status") == "paid"
        group_stage = pipeline[1].get("$group", {})
        assert "total_revenue_usd" in group_stage

    def test_overdue_invoices_plan(self):
        plan = build_fast_plan(Intent.OVERDUE_INVOICES, "invoices", "overdue invoices")
        assert plan is not None
        assert plan["collection"] == "invoices"
        filt = plan["filter"]
        assert "$in" in filt.get("payment_status", {})
        assert "$lt" in filt.get("due_date", {})

    def test_overdue_tasks_plan(self):
        plan = build_fast_plan(Intent.OVERDUE_TASKS, "createtasks", "overdue tasks")
        assert plan is not None
        assert plan["collection"] == "createtasks"

    def test_pipeline_by_stage_is_aggregate(self):
        plan = build_fast_plan(Intent.PIPELINE_BY_STAGE, "deals", "sales pipeline by stage")
        assert plan is not None
        assert plan["type"] == "aggregate"
        assert plan["response_hint"] == "table"

    def test_closed_won_plan(self):
        plan = build_fast_plan(Intent.CLOSED_WON, "deals", "closed won deals")
        assert plan is not None
        filt = plan["filter"]
        assert filt.get("dealWonAt") == {"$ne": None}

    def test_closed_lost_plan(self):
        plan = build_fast_plan(Intent.CLOSED_LOST, "deals", "closed lost deals")
        assert plan is not None
        filt = plan["filter"]
        assert filt.get("dealLostAt") == {"$ne": None}

    def test_pending_tasks_plan(self):
        plan = build_fast_plan(Intent.PENDING_TASKS, "createtasks", "pending tasks")
        assert plan is not None
        assert "$in" in plan["filter"].get("status", {})

    def test_list_recent_extracts_limit(self):
        plan = build_fast_plan(Intent.LIST_RECENT, "deals", "show last 7 deals")
        assert plan is not None
        assert plan["limit"] == 7

    def test_list_recent_defaults_to_10(self):
        plan = build_fast_plan(Intent.LIST_RECENT, "contacts", "show recent contacts")
        assert plan is not None
        assert plan["limit"] == 10

    def test_temporal_filter_applied_for_this_month(self):
        plan = build_fast_plan(Intent.CLOSED_WON, "deals", "closed won deals this month")
        assert plan is not None
        filt = plan["filter"]
        won_filter = filt.get("dealWonAt", {})
        assert "$gte" in won_filter or "$lte" in won_filter


# ── Edge cases ────────────────────────────────────────

class TestEdgeCases:
    def test_empty_query(self):
        intent, conf = classify("")
        assert intent == Intent.UNKNOWN

    def test_very_long_query(self):
        long_q = "how many " + "deal " * 100
        intent, conf = classify(long_q)
        assert intent == Intent.COUNT

    def test_mixed_signals_highest_wins(self):
        # "closed won deals" should win over "how many" if both present
        # Both are strong signals; test that we get a result without error
        intent, conf = classify("how many closed won deals are there")
        assert intent in (Intent.COUNT, Intent.CLOSED_WON)
        assert conf > 0

    def test_plan_for_unknown_intent_returns_none(self):
        plan = build_fast_plan(Intent.UNKNOWN, "deals", "random")
        assert plan is None
