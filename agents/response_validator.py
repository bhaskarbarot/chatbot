"""
Post-Execution Validation Layer — Enhancement 6.

Validates raw MongoDB results against the response_shape_contract from
the enriched intent. Runs BEFORE response formatting.

Four checks (all pure Python — zero LLM calls):
  1. Limit enforcement: trim to N if result_limit set
  2. Shape enforcement: convert to correct type (yes/no, count, single, etc.)
  3. Field filtering: remove clearly irrelevant fields (never drop records)
  4. Empty result handling: flag needs_self_healing for non-existence queries

Always returns (validated_results, validation_meta).
On any internal failure: returns (raw_results, safe_default_meta).
Thread-safe: stateless.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from config import MAX_MONGO_RESULTS
from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("response_validator")

# Internal Mongo / system fields never shown to the user
_INTERNAL_FIELDS = {
    "__v", "deletedAt", "deletedBy", "isDeleted", "deleted",
    "password", "token", "refreshToken", "accessToken",
    "salt", "hash", "__t",
}
NUMERIC_FIELD_PRIORITY = [
    "total", "sum", "amount", "revenue", "value", "grand_total",
    "rate", "percentage", "count", "avg", "average", "balance",
    "outstanding", "collected", "paid", "totalAmount", "total_amount",
    "grand_total_in_usd", "invoiced_amount", "conversion_rate",
    "total_sales", "saleAmount", "sale_amount",
    "netAmount", "net_amount", "grossAmount", "gross_amount",
    "orderTotal", "order_total", "salesTotal", "sales_total",
    "subtotal", "sub_total", "lineTotal", "line_total",
    "totalPrice", "total_price", "finalAmount", "final_amount"
]


class ResponseValidator:
    """
    Validates raw Mongo results against enriched_intent contracts.
    Returns (validated_results, validation_meta) — never raises.
    """

    def validate(
        self,
        raw_results: Any,
        enriched_intent: Dict[str, Any],
    ) -> Tuple[Any, Dict[str, Any]]:
        """
        Entry point. Returns (validated_results, validation_meta).
        On any failure: returns (raw_results, safe_default_meta()).
        """
        if not isinstance(enriched_intent, dict):
            return raw_results, self.safe_default_meta()

        try:
            return self._run_checks(raw_results, enriched_intent)
        except Exception as e:
            logger.warning(f"[ResponseValidator] validate failed: {e} — returning raw results")
            record_event("response_validator_error", {"error": str(e)[:120]})
            return raw_results, self.safe_default_meta()

    @classmethod
    def safe_default_meta(cls) -> Dict[str, Any]:
        """Return neutral meta — no healing needed, no changes made."""
        return {
            "needs_self_healing": False,
            "original_count": 0,
            "validated_count": 0,
            "shape_applied": "passthrough",
            "fields_filtered": False,
            "empty_result": False,
        }

    # ── Core checks ───────────────────────────────────────────────────────────

    def _run_checks(
        self,
        raw_results: Any,
        enriched_intent: Dict[str, Any],
    ) -> Tuple[Any, Dict[str, Any]]:
        result_limit  = enriched_intent.get("result_limit")
        response_shape = enriched_intent.get("response_shape", "list") or "list"
        primary_intent = enriched_intent.get("primary_intent", "data_fetch")

        original_count  = _count_results(raw_results)
        is_empty        = _is_empty(raw_results)
        needs_healing   = False
        fields_filtered = False
        shape_applied   = "passthrough"

        data = raw_results

        # ── Check 1: Limit enforcement ────────────────────────────────────────
        if result_limit is not None and isinstance(data, list) and len(data) > result_limit:
            data = data[:result_limit]
            logger.debug(f"[Validator] trimmed {original_count} → {result_limit} (enforced_limit)")

        # ── Check 2: Shape enforcement ────────────────────────────────────────
        if response_shape == "yes_no":
            if isinstance(data, list):
                # yes_no + has results → True; empty → False
                shape_applied = "yes_no"
                # Don't convert to bool yet — leave as original data for formatter
                # The response_controller will enforce yes/no in the final response.
                # If empty → no self-healing needed (the answer IS "No")
                needs_healing = False
            elif isinstance(data, (int, float)):
                shape_applied = "yes_no_count"

        elif response_shape == "number":
            if isinstance(data, list):
                aggregate_value = self._extract_aggregate_value(data)
                if aggregate_value.get("extracted"):
                    shape_applied = "aggregate_number"
                else:
                    # Count query returned a list → convert to count
                    data = len(data)
                    shape_applied = "number_from_list"
            elif isinstance(data, dict) and any(
                k in data for k in ("count", "total", "n", "total_count")
            ):
                shape_applied = "number_from_dict"
            else:
                shape_applied = "number"

        elif response_shape == "single_value":
            if isinstance(data, list) and len(data) > 1:
                data = data[:1]
                shape_applied = "single_trimmed"
            else:
                shape_applied = "single"

        elif response_shape == "list":
            # Cap at MAX_MONGO_RESULTS from config
            if isinstance(data, list) and len(data) > MAX_MONGO_RESULTS:
                data = data[:MAX_MONGO_RESULTS]
                logger.debug(f"[Validator] capped list at MAX_MONGO_RESULTS={MAX_MONGO_RESULTS}")
            shape_applied = "list"

        elif response_shape == "table":
            shape_applied = "table"

        elif response_shape == "analysis_text":
            shape_applied = "analysis_text"

        # ── Check 3: Field filtering ──────────────────────────────────────────
        if isinstance(data, list):
            cleaned = self._filter_fields(data, enriched_intent)
            fields_filtered = cleaned != data
            data = cleaned
            logger.debug("[Validator] field filtering: safe-only mode")

        # ── Check 4: Empty result handling ────────────────────────────────────
        validated_is_empty = _is_empty(data)

        if validated_is_empty:
            if response_shape == "yes_no" or primary_intent == "existence_check":
                # Empty + yes_no → answer is simply "No" — no healing needed
                needs_healing = False
            else:
                # Empty + non-existence query → flag for self-healing
                needs_healing = True

        validated_count = _count_results(data)

        meta: Dict[str, Any] = {
            "needs_self_healing": needs_healing,
            "original_count": original_count,
            "validated_count": validated_count,
            "shape_applied": shape_applied,
            "fields_filtered": fields_filtered,
            "empty_result": validated_is_empty,
        }
        # After shape enforcement, attempt aggregate extraction
        if enriched_intent.get("response_shape") in ("number", "count") \
           or meta.get("shape_applied") in (
               "number", "number_from_list", "count"):

            extracted = self._extract_aggregate_value(
                data if isinstance(data, list) else [data]
            )
            if extracted.get("extracted"):
                meta["aggregate_value"] = extracted
                meta["shape_applied"] = "aggregate_number"
                logger.debug(
                    f"[Validator] aggregate extraction: "
                    f"strategy={extracted.get('strategy')} "
                    f"value={extracted.get('value')} "
                    f"label={extracted.get('label')}"
                )

        if isinstance(data, int):
            meta["count_value"] = data
            meta["shape_applied"] = "direct_count"

        record_event("response_validator_ok", {
            "shape": response_shape,
            "shape_applied": shape_applied,
            "original_count": original_count,
            "validated_count": validated_count,
            "needs_self_healing": needs_healing,
        })
        logger.debug(
            f"[Validator] shape={response_shape} "
            f"original={original_count} validated={validated_count} "
            f"healing={needs_healing} shape_applied={shape_applied}"
        )
        return data, meta

    def _extract_aggregate_value(self, results: list) -> dict:
        """
        Extract numeric value from aggregate results.
        Strategy 1: Single summary doc (1 doc with numeric fields, _id=null)
        Strategy 2: Multi-doc aggregate — sum the primary numeric field
        """
        logger.debug(
            f"[_extract_aggregate_value] called with "
            f"len={len(results) if isinstance(results, list) else 'N/A'} "
            f"first_keys={list(results[0].keys()) if results and isinstance(results[0], dict) else 'N/A'} "
            f"first_id={results[0].get('_id', 'MISSING') if results and isinstance(results[0], dict) else 'N/A'}"
        )
        if not isinstance(results, list) or len(results) == 0:
            return {"extracted": False}

        first = results[0]
        if not isinstance(first, dict):
            return {"extracted": False}

        # Strategy 1: Summary aggregate doc
        # Conditions: 1 doc AND (_id is None OR _id is 0 OR _id not present)
        _id_val = first.get("_id", "MISSING")
        _is_summary_doc = (
            len(results) == 1 and
            (_id_val is None or _id_val == 0 or _id_val == "MISSING" or
             _id_val == "" or _id_val is False)
        )

        if _is_summary_doc:
            for field in NUMERIC_FIELD_PRIORITY:
                if field in first and isinstance(first[field], (int, float)):
                    all_numeric = {
                        k: v for k, v in first.items()
                        if isinstance(v, (int, float)) and k != "_id"
                    }
                    return {
                        "extracted": True,
                        "strategy": "summary_doc",
                        "value": first[field],
                        "label": field,
                        "all_fields": all_numeric
                    }
            # Try any numeric field in summary doc
            for k, v in first.items():
                if k not in ("_id",) and isinstance(v, (int, float)):
                    all_numeric = {
                        kk: vv for kk, vv in first.items()
                        if isinstance(vv, (int, float)) and kk != "_id"
                    }
                    return {
                        "extracted": True,
                        "strategy": "summary_doc",
                        "value": v,
                        "label": k,
                        "all_fields": all_numeric
                    }

        # Strategy 1B: Single doc with non-null _id but has priority numeric fields
        # Example: grouped by date, single result period
        if len(results) == 1:
            for field in NUMERIC_FIELD_PRIORITY:
                if field in first and isinstance(first[field], (int, float)):
                    all_numeric = {
                        k: v for k, v in first.items()
                        if isinstance(v, (int, float)) and k != "_id"
                    }
                    return {
                        "extracted": True,
                        "strategy": "single_group_doc",
                        "value": first[field],
                        "label": field,
                        "all_fields": all_numeric
                    }

        # Strategy 2: Multi-doc result — sum primary numeric field across docs
        # Only apply when shape is explicitly "number" (user asked for total/sum)
        # Do NOT apply for list/table shapes
        if len(results) > 1:
            for field in NUMERIC_FIELD_PRIORITY:
                if field in first and isinstance(first[field], (int, float)):
                    total = sum(
                        r.get(field, 0) for r in results
                        if isinstance(r, dict)
                        and isinstance(r.get(field), (int, float))
                    )
                    if total > 0:
                        return {
                            "extracted": True,
                            "strategy": "summed_multi_doc",
                            "value": total,
                            "label": field,
                            "doc_count": len(results),
                            "all_fields": {field: total}
                        }

        logger.debug("[_extract_aggregate_value] No extraction strategy matched")
        return {"extracted": False}

    # ── Field filtering ───────────────────────────────────────────────────────

    def _filter_fields(self, results: list, enriched_intent: dict) -> list:
        """
        SAFE field filtering only.
        Remove known internal Mongo fields that are never useful to display.
        Do NOT attempt semantic filtering — too risky without schema awareness.
        """
        SAFE_REMOVE_FIELDS = {"__v", "_class", "password", "token",
                              "refresh_token", "salt", "hash"}

        if not isinstance(results, list):
            return results

        cleaned = []
        for record in results:
            if not isinstance(record, dict):
                cleaned.append(record)
                continue
            cleaned_record = {
                k: v for k, v in record.items()
                if k not in SAFE_REMOVE_FIELDS
            }
            cleaned.append(cleaned_record)

        return cleaned


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_results(data: Any) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, (int, float)):
        return int(data)
    if isinstance(data, dict):
        return 1
    return 0


def _is_empty(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, list):
        return len(data) == 0
    if isinstance(data, (int, float)):
        return False   # a count of 0 is a valid result, not empty
    if isinstance(data, dict):
        return len(data) == 0
    return False


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    validator = ResponseValidator()
    all_passed = True

    print("=" * 60)
    print("ResponseValidator — Standalone Tests")
    print("=" * 60)

    # Test 1: list trimmed to result_limit
    print("\nTest 1: 10 results, result_limit=3 → trimmed to 3")
    raw = [{"_id": str(i), "name": f"Deal {i}"} for i in range(10)]
    intent = {"result_limit": 3, "response_shape": "list", "primary_intent": "data_fetch", "entities_mentioned": []}
    result, meta = validator.validate(raw, intent)
    if len(result) == 3 and meta["validated_count"] == 3:
        print("  PASS — trimmed to 3")
    else:
        print(f"  FAIL — len={len(result)} meta={meta}")
        all_passed = False

    # Test 2: yes_no + empty results → no self-healing needed
    print("\nTest 2: yes_no + empty → needs_self_healing=False")
    intent2 = {"result_limit": None, "response_shape": "yes_no", "primary_intent": "existence_check", "entities_mentioned": []}
    result2, meta2 = validator.validate([], intent2)
    if meta2["needs_self_healing"] is False and meta2["empty_result"] is True:
        print("  PASS — no healing needed for yes_no empty result")
    else:
        print(f"  FAIL — meta={meta2}")
        all_passed = False

    # Test 3: list + empty → needs_self_healing=True
    print("\nTest 3: list + empty → needs_self_healing=True")
    intent3 = {"result_limit": None, "response_shape": "list", "primary_intent": "data_fetch", "entities_mentioned": []}
    result3, meta3 = validator.validate([], intent3)
    if meta3["needs_self_healing"] is True:
        print("  PASS — healing flagged for empty list result")
    else:
        print(f"  FAIL — meta={meta3}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
