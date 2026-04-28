"""
Intent Enrichment Layer — Enhancement 2.

Merges the output of the existing intent_classifier and query_understander
with the new deep_query_understander into a single enriched intent object.

Rules (no exceptions, no crashes):
- Never replaces existing intent classifier fields — only adds on top.
- For response_shape / result_limit: deep_understander wins.
- For collection_hints / entity: existing qparams wins (it has rule knowledge).
- For temporal_filter: deep_understander wins if type != "none", else carry
  existing temporal_hint from qparams.
- If confidence_combined < 0.4: return qparams wrapped with safe defaults.
- If deep_understanding is empty or None: return qparams wrapped with safe defaults.
- Always returns the same schema shape — never raises.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("intent_enricher")

# ── Output-type map (response_shape → output_type) ───────────────────────────
_SHAPE_TO_OUTPUT_TYPE: Dict[str, str] = {
    "yes_no":       "yes_no",
    "number":       "count",
    "single_value": "single",
    "list":         "list",
    "table":        "table",
    "analysis_text":"analysis",
    "greeting":     "list",
}

# ── Operation-type map (primary_intent → operation_type) ─────────────────────
_INTENT_TO_OPERATION: Dict[str, str] = {
    "count":            "filter",
    "existence_check":  "existence",
    "comparison":       "compare",
    "analysis":         "aggregate",
    "relationship_lookup": "lookup",
    "data_fetch":       "filter",
    "specific_detail":  "lookup",
    "greeting":         "filter",
    "out_of_scope":     "filter",
}

# ── Safe default schema ───────────────────────────────────────────────────────
_SAFE_DEFAULTS: Dict[str, Any] = {
    # --- qparams mirror fields (filled at enrich time from qparams) ---
    "intent":                   "unknown",
    "limit":                    None,
    "sort_field":               None,
    "sort_direction":           -1,
    "sort_reason":              None,
    "filter_hints":             {},
    "entity_names":             [],
    "compare_entities":         [],
    "existence_target":         None,
    "existence_entity_type":    None,
    "temporal_hint":            None,
    "collection_hints":         [],
    # --- classifier fields ---
    "classifier_intent":        "unknown",
    "classifier_confidence":    0.0,
    # --- enriched fields ---
    "output_type":              "list",
    "operation_type":           "filter",
    "reasoning_type":           "simple",
    "temporal_filter":          {"type": "none", "value": None, "year": None,
                                 "month": None, "start_date": None, "end_date": None},
    "result_limit":             None,
    "response_shape":           "list",
    "is_multi_intent":          False,
    "sub_intents":              [],
    "needs_relationship_join":  False,
    "entities_mentioned":       [],
    "confidence_combined":      0.5,
    "primary_intent":           "data_fetch",
    "comparison_targets":       [],
}


class IntentEnricher:
    """
    Merges intent_classifier + query_understander + deep_query_understander
    outputs into a single enriched intent dict.

    Thread-safe: stateless — no instance-level mutable state per call.
    """

    def enrich(
        self,
        classifier_result: Tuple,   # (Intent, float) from classify_intent()
        qparams: Any,               # QueryParams dataclass from understand_query()
        deep_understanding: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Entry point. Returns enriched intent dict — never raises.

        Priority rules:
        - response_shape / result_limit: deep_understanding wins
        - collection_hints / entity fields: qparams wins
        - temporal_filter: deep_understanding if type != "none", else qparams.temporal_hint
        - confidence_combined < 0.4 → return safe default wrapper
        """
        # Input validation
        if qparams is None:
            return self.safe_default()

        if not isinstance(deep_understanding, dict) or not deep_understanding:
            return self._wrap_qparams_only(classifier_result, qparams)

        try:
            return self._build_enriched(classifier_result, qparams, deep_understanding)
        except Exception as e:
            logger.warning(f"[IntentEnricher] enrich failed: {e} — using qparams wrapper")
            record_event("intent_enricher_error", {"error": str(e)[:120]})
            return self._wrap_qparams_only(classifier_result, qparams)

    @classmethod
    def safe_default(cls) -> Dict[str, Any]:
        """Return neutral safe output — used when the layer fails entirely."""
        return copy.deepcopy(_SAFE_DEFAULTS)

    # ── Internal builders ─────────────────────────────────────────────────────

    def _build_enriched(
        self,
        classifier_result: Tuple,
        qparams: Any,
        deep_understanding: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = copy.deepcopy(_SAFE_DEFAULTS)

        # ── 1. Mirror all qparams fields (existing system — unchanged) ────────
        result["intent"]                = getattr(qparams, "intent", None)
        result["limit"]                 = getattr(qparams, "limit", None)
        result["sort_field"]            = getattr(qparams, "sort_field", None)
        result["sort_direction"]        = getattr(qparams, "sort_direction", -1)
        result["sort_reason"]           = getattr(qparams, "sort_reason", None)
        result["filter_hints"]          = copy.deepcopy(getattr(qparams, "filter_hints", {}))
        result["entity_names"]          = list(getattr(qparams, "entity_names", []))
        result["compare_entities"]      = list(getattr(qparams, "compare_entities", []))
        result["existence_target"]      = getattr(qparams, "existence_target", None)
        result["existence_entity_type"] = getattr(qparams, "existence_entity_type", None)
        result["temporal_hint"]         = getattr(qparams, "temporal_hint", None)
        result["collection_hints"]      = list(getattr(qparams, "collection_hints", []))

        # ── 2. Classifier result ──────────────────────────────────────────────
        if classifier_result and len(classifier_result) == 2:
            intent_enum, conf = classifier_result
            result["classifier_intent"]    = _get_intent_value(intent_enum)
            result["classifier_confidence"]= float(conf) if conf is not None else 0.0
        else:
            result["classifier_intent"]    = "unknown"
            result["classifier_confidence"]= 0.0

        # ── 3. Conflict resolution: deep_understanding wins for these ─────────

        # response_shape — prefer deep_understander
        deep_shape = deep_understanding.get("response_shape", "list")
        result["response_shape"] = deep_shape if deep_shape else "list"

        # output_type derived from response_shape
        result["output_type"] = _SHAPE_TO_OUTPUT_TYPE.get(result["response_shape"], "list")

        # result_limit — deep_understander wins; fallback to qparams.limit
        deep_limit = deep_understanding.get("result_limit")
        result["result_limit"] = (
            deep_limit if deep_limit is not None
            else getattr(qparams, "limit", None)
        )

        # primary_intent from deep understander
        result["primary_intent"] = deep_understanding.get("primary_intent", "data_fetch")

        # operation_type derived from primary_intent
        result["operation_type"] = _INTENT_TO_OPERATION.get(
            result["primary_intent"], "filter"
        )

        # Temporal normalization — single source of truth
        deep_temporal = deep_understanding.get("temporal_filter", {})
        existing_temporal = getattr(qparams, "temporal_hint", None)

        if isinstance(deep_temporal, dict) and deep_temporal.get("type", "none") != "none":
            # Deep understander resolved a specific temporal — use it
            final_temporal = copy.deepcopy(deep_temporal)
            temporal_source = "deep_understander"
        elif existing_temporal:
            # Fall back to existing qparams temporal hint
            final_temporal = {
                "type": "legacy",
                "raw": str(existing_temporal),
                "start_date": None,
                "end_date": None
            }
            temporal_source = "qparams"
        else:
            final_temporal = {"type": "none"}
            temporal_source = "none"

        # Store as single field — ONLY this is used downstream
        result["final_temporal"] = final_temporal
        result["temporal_source"] = temporal_source
        # Keep temporal_filter for backward compat but point to same object
        result["temporal_filter"] = final_temporal
        logger.debug(
            f"[IntentEnricher] temporal source={temporal_source} "
            f"type={final_temporal.get('type')}"
        )

        # Multi-intent / sub-intents from deep understander
        result["is_multi_intent"]       = bool(deep_understanding.get("is_multi_intent", False))
        result["sub_intents"]           = list(deep_understanding.get("sub_intents", []))
        result["needs_relationship_join"]= bool(deep_understanding.get("needs_relationship_join", False))
        result["entities_mentioned"]    = list(deep_understanding.get("entities_mentioned", []))
        result["comparison_targets"]    = list(deep_understanding.get("comparison_targets", []))

        # ── 4. reasoning_type ────────────────────────────────────────────────
        if result["needs_relationship_join"]:
            result["reasoning_type"] = "relational"
        elif result["is_multi_intent"] or len(result["sub_intents"]) >= 2:
            result["reasoning_type"] = "multi_step"
        else:
            result["reasoning_type"] = "simple"

        # ── 5. confidence_combined ────────────────────────────────────────────
        base_conf = result["classifier_confidence"]
        # Boost if deep understander found non-default useful signals
        boost = 0.0
        if result["response_shape"] != "list":
            boost += 0.1
        if result["result_limit"] is not None:
            boost += 0.05
        if result["temporal_filter"].get("type") not in (None, "none"):
            boost += 0.05
        result["confidence_combined"] = min(base_conf + boost, 1.0)

        # ── 6. Low-confidence guard: fall back to qparams-only wrapper ────────
        # Still preserve deep_understanding's response_shape even on low confidence —
        # shape detection is semantic and doesn't depend on classifier confidence.
        if result["confidence_combined"] < 0.4 and base_conf < 0.4:
            logger.debug(
                f"[IntentEnricher] confidence_combined={result['confidence_combined']:.2f} "
                f"< 0.4 — using qparams-only wrapper (preserving response_shape)"
            )
            fallback = self._wrap_qparams_only(classifier_result, qparams)
            # Preserve the semantic shape signal from deep_understanding
            deep_shape = deep_understanding.get("response_shape")
            if deep_shape and deep_shape != "list":
                fallback["response_shape"] = deep_shape
                fallback["output_type"] = _SHAPE_TO_OUTPUT_TYPE.get(deep_shape, "list")
            return fallback

        record_event("intent_enricher_ok", {
            "output_type": result["output_type"],
            "reasoning_type": result["reasoning_type"],
            "result_limit": result["result_limit"],
        })
        logger.debug(
            f"[IntentEnricher] output_type={result['output_type']} "
            f"reasoning={result['reasoning_type']} "
            f"limit={result['result_limit']} "
            f"conf={result['confidence_combined']:.2f}"
        )
        return result

    def _wrap_qparams_only(
        self,
        classifier_result: Tuple,
        qparams: Any,
    ) -> Dict[str, Any]:
        """Minimal wrapping when deep understanding is unavailable."""
        result = copy.deepcopy(_SAFE_DEFAULTS)
        if qparams is not None:
            result["intent"]      = getattr(qparams, "intent", None)
            result["limit"]       = getattr(qparams, "limit", None)
            result["sort_field"]  = getattr(qparams, "sort_field", None)
            result["sort_direction"] = getattr(qparams, "sort_direction", -1)
            result["collection_hints"] = list(getattr(qparams, "collection_hints", []))
            result["entity_names"]     = list(getattr(qparams, "entity_names", []))
            result["result_limit"]     = getattr(qparams, "limit", None)
        if classifier_result and len(classifier_result) == 2:
            intent_enum, conf = classifier_result
            result["classifier_intent"]    = _get_intent_value(intent_enum)
            result["classifier_confidence"]= float(conf) if conf is not None else 0.0
        return result


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_intent_value(intent_enum) -> str:
    """Safely extract string value from Intent enum or plain string."""
    if intent_enum is None:
        return "unknown"
    if hasattr(intent_enum, "value"):
        return str(intent_enum.value)
    return str(intent_enum)


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys, json
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    from knowledge.deep_query_understander import DeepQueryUnderstander
    from knowledge.query_understander import understand as understand_query
    from knowledge.intent_classifier import classify as classify_intent, Intent

    enricher = IntentEnricher()
    understander = DeepQueryUnderstander()

    print("=" * 60)
    print("IntentEnricher — Standalone Tests")
    print("=" * 60)

    queries = [
        ("how many leads do we have?",  "count",   "number"),
        ("show me top 3 deals",          "list",    "list"),
        ("do we have any open deals?",   "yes_no",  "yes_no"),
    ]

    all_passed = True
    for i, (query, expected_output_type, expected_shape) in enumerate(queries, 1):
        print(f"\nTest {i}: {query!r}")
        qp = understand_query(query)
        cr = classify_intent(query)
        from knowledge.deep_query_understander import _rule_based_understand
        du = _rule_based_understand(query)
        result = enricher.enrich(cr, qp, du)

        errors = []
        if result.get("response_shape") != expected_shape:
            errors.append(
                f"  response_shape={result.get('response_shape')!r} (expected {expected_shape!r})"
            )
        # output_type check
        from knowledge.intent_enricher import _SHAPE_TO_OUTPUT_TYPE
        expected_ot = _SHAPE_TO_OUTPUT_TYPE.get(expected_shape, expected_output_type)
        if result.get("output_type") != expected_ot:
            errors.append(
                f"  output_type={result.get('output_type')!r} (expected {expected_ot!r})"
            )

        if errors:
            print("  FAIL:")
            for e in errors:
                print(e)
            all_passed = False
        else:
            print("  PASS")

        snap = {k: result[k] for k in
                ("output_type", "response_shape", "result_limit",
                 "reasoning_type", "is_multi_intent", "confidence_combined")}
        print(f"  {json.dumps(snap, default=str)}")

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
