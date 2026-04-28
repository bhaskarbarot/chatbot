"""
Structured Plan Wrapper — Enhancement 4.

Wraps an existing query plan with structured metadata:
- structured_steps: human-readable breakdown of plan operations
- enforced_limit / enforced_sort / enforced_temporal: from enriched_intent
- response_shape_contract: what the final response MUST look like

CRITICAL RULES:
- original_plan is NEVER modified — always stored as a reference.
- On any failure: return {"original_plan": plan} and continue.
- Thread-safe: stateless — no shared mutable state.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("structured_plan_wrapper")


class StructuredPlanWrapper:
    """
    Adds execution metadata around an existing MongoDB query plan.
    The original plan is never touched.
    """

    def wrap(
        self,
        plan: Dict[str, Any],
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Entry point. Wraps plan with metadata from enriched_intent.
        Returns enriched wrapper dict. Never raises — returns minimal
        safe wrapper on any failure.
        """
        # Input validation
        if not isinstance(plan, dict):
            return {"original_plan": plan}

        if not isinstance(enriched_intent, dict):
            enriched_intent = {}

        try:
            return self._build_wrapper(plan, enriched_intent)
        except Exception as e:
            logger.warning(f"[PlanWrapper] wrap failed: {e} — returning bare plan")
            record_event("plan_wrapper_error", {"error": str(e)[:120]})
            return self.safe_default(plan)

    @classmethod
    def safe_default(cls, plan: Dict = None) -> Dict[str, Any]:
        """Return minimal safe wrapper."""
        return {
            "original_plan": plan or {},
            "structured_steps": [],
            "enforced_limit": None,
            "enforced_sort": None,
            "enforced_temporal": None,
            "response_shape_contract": "list",
        }

    # ── Internal builder ──────────────────────────────────────────────────────

    def _build_wrapper(
        self,
        plan: Dict[str, Any],
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        structured_steps = self._extract_steps(plan)

        # enforced_limit: from enriched_intent.result_limit if set
        result_limit = enriched_intent.get("result_limit")
        enforced_limit = int(result_limit) if result_limit is not None else None

        # enforced_sort: from enriched_intent.sort_preference if field is set
        sort_pref = enriched_intent.get("sort_preference") or {}
        enforced_sort = None
        if isinstance(sort_pref, dict) and sort_pref.get("field"):
            enforced_sort = {
                "field": sort_pref["field"],
                "direction": sort_pref.get("direction", "desc"),
            }

        # enforced_temporal: from enriched_intent.temporal_filter if type != "none"
        temporal_filter = enriched_intent.get("temporal_filter") or {}
        enforced_temporal = None
        if isinstance(temporal_filter, dict) and temporal_filter.get("type") not in (None, "none"):
            enforced_temporal = copy.deepcopy(temporal_filter)

        # response_shape_contract
        response_shape = enriched_intent.get("response_shape", "list") or "list"
        result_limit_str = f":{enforced_limit}" if enforced_limit else ""
        response_shape_contract = f"{response_shape}{result_limit_str}"

        wrapper = {
            "original_plan": plan,              # NEVER MODIFIED
            "structured_steps": structured_steps,
            "enforced_limit": enforced_limit,
            "enforced_sort": enforced_sort,
            "enforced_temporal": enforced_temporal,
            "response_shape_contract": response_shape_contract,
        }

        record_event("plan_wrapper_ok", {
            "plan_type": plan.get("type"),
            "enforced_limit": enforced_limit,
            "shape_contract": response_shape_contract,
        })
        logger.debug(
            f"[PlanWrapper] type={plan.get('type')} "
            f"steps={len(structured_steps)} "
            f"limit={enforced_limit} "
            f"shape_contract={response_shape_contract}"
        )
        return wrapper

    def _extract_steps(self, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract human-readable steps from any plan type."""
        plan_type = plan.get("type", "")
        steps: List[Dict[str, Any]] = []

        if plan_type == "find":
            steps.append({
                "step": 1,
                "type": "filter",
                "description": f"Filter {plan.get('collection', '')} collection",
                "depends_on": None,
            })
            if plan.get("sort"):
                steps.append({
                    "step": 2,
                    "type": "sort",
                    "description": f"Sort results by {plan.get('sort')}",
                    "depends_on": 1,
                })
            if plan.get("limit"):
                steps.append({
                    "step": len(steps) + 1,
                    "type": "limit",
                    "description": f"Limit to {plan.get('limit')} records",
                    "depends_on": len(steps),
                })

        elif plan_type == "count":
            steps.append({
                "step": 1,
                "type": "count",
                "description": f"Count records in {plan.get('collection', '')}",
                "depends_on": None,
            })

        elif plan_type == "aggregate":
            pipeline = plan.get("pipeline", [])
            for i, stage in enumerate(pipeline, 1):
                if isinstance(stage, dict) and stage:
                    op = next(iter(stage.keys()))
                    step_type = _AGGS_STAGE_TO_TYPE.get(op, "aggregate")
                    steps.append({
                        "step": i,
                        "type": step_type,
                        "description": f"Pipeline stage: {op}",
                        "depends_on": i - 1 if i > 1 else None,
                    })

        elif plan_type == "distinct":
            steps.append({
                "step": 1,
                "type": "lookup",
                "description": f"Distinct values of '{plan.get('field','')}' "
                               f"in {plan.get('collection', '')}",
                "depends_on": None,
            })

        elif plan_type == "multi_step":
            sub_steps = plan.get("steps", [])
            for i, sub in enumerate(sub_steps, 1):
                steps.append({
                    "step": i,
                    "type": sub.get("type", "filter"),
                    "description": f"Step {i}: {sub.get('type', '')} on {sub.get('collection', '')}",
                    "depends_on": i - 1 if i > 1 else None,
                })

        return steps


# ── Stage type mapping ────────────────────────────────────────────────────────
_AGGS_STAGE_TO_TYPE: Dict[str, str] = {
    "$match": "filter",
    "$group": "aggregate",
    "$sort": "sort",
    "$limit": "limit",
    "$skip": "limit",
    "$lookup": "lookup",
    "$unwind": "lookup",
    "$project": "filter",
    "$count": "count",
    "$addFields": "filter",
}


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys, json
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    wrapper = StructuredPlanWrapper()
    all_passed = True

    print("=" * 60)
    print("StructuredPlanWrapper — Standalone Tests")
    print("=" * 60)

    # Test 1: find plan with limit from enriched_intent
    print("\nTest 1: find plan + result_limit=5 from enriched_intent")
    plan1 = {"type": "find", "collection": "deals", "filter": {}, "sort": [["createdAt", -1]], "limit": 50}
    intent1 = {"result_limit": 5, "response_shape": "list", "sort_preference": {"field": None, "direction": "null"}, "temporal_filter": {"type": "none"}}
    result1 = wrapper.wrap(plan1, intent1)
    if result1["enforced_limit"] == 5 and result1["original_plan"] is plan1:
        print("  PASS — enforced_limit=5, original_plan untouched")
    else:
        print(f"  FAIL — enforced_limit={result1['enforced_limit']}, original_plan is plan={result1['original_plan'] is plan1}")
        all_passed = False
    print(f"  steps={result1['structured_steps']}")

    # Test 2: count plan
    print("\nTest 2: count plan → shape_contract=number")
    plan2 = {"type": "count", "collection": "contacts", "filter": {}}
    intent2 = {"result_limit": None, "response_shape": "number", "sort_preference": {}, "temporal_filter": {"type": "none"}}
    result2 = wrapper.wrap(plan2, intent2)
    if result2["response_shape_contract"] == "number":
        print("  PASS — shape_contract=number")
    else:
        print(f"  FAIL — shape_contract={result2['response_shape_contract']!r}")
        all_passed = False

    # Test 3: failure path — bad plan returns safe default
    print("\nTest 3: None plan → safe default returned")
    result3 = wrapper.wrap(None, {})
    if "original_plan" in result3:
        print("  PASS — safe default returned with original_plan key")
    else:
        print(f"  FAIL — {result3}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
