"""
Relationship Enhancement Layer — Enhancement 5.

Reads the existing relationship_graph singleton (never rewrites it).
If needs_relationship_join=True in enriched_intent:
  - Detects which collections are involved from the plan + intent
  - Looks up join paths via existing graph.detect_required_joins() / graph.lookup_spec()
  - Adds join hints as "join_hints" key in the wrapped_plan (NEVER touches original_plan)

If needs_relationship_join=False: returns wrapped_plan completely unchanged — zero overhead.

Thread-safe: stateless, no shared mutable state per call.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set

from knowledge.relationship_graph import get_graph
from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("relationship_enhancer")


class RelationshipEnhancer:
    """
    Adds $lookup join hints to the wrapped plan when a relationship join
    is required. Never modifies original_plan under any circumstance.
    """

    def enhance(
        self,
        wrapped_plan: Dict[str, Any],
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Entry point.
        Returns wrapped_plan (possibly with join_hints added).
        On failure or no join needed: returns wrapped_plan unchanged.
        """
        if not isinstance(wrapped_plan, dict):
            return wrapped_plan

        if not isinstance(enriched_intent, dict):
            return wrapped_plan

        if not enriched_intent.get("needs_relationship_join", False):
            return wrapped_plan

        try:
            return self._add_join_hints(wrapped_plan, enriched_intent)
        except Exception as e:
            logger.warning(f"[RelEnhancer] enhance failed: {e} — returning plan unchanged")
            record_event("rel_enhancer_error", {"error": str(e)[:120]})
            return wrapped_plan

    @classmethod
    def safe_default(cls, wrapped_plan: Dict = None) -> Dict[str, Any]:
        """Return the wrapped_plan unchanged — safe no-op default."""
        return wrapped_plan or {}

    # ── Internal ──────────────────────────────────────────────────────────────

    def _add_join_hints(
        self,
        wrapped_plan: Dict[str, Any],
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        graph = get_graph()

        # Determine primary collection from original_plan
        original_plan = wrapped_plan.get("original_plan", {})
        primary_collection = ""
        if isinstance(original_plan, dict):
            primary_collection = original_plan.get("collection", "")

        if not primary_collection:
            # Try collection_hints from enriched_intent
            hints = enriched_intent.get("collection_hints", [])
            if hints:
                primary_collection = hints[0]

        if not primary_collection:
            logger.debug("[RelEnhancer] No primary collection found — skipping join enhancement")
            return wrapped_plan

        # Use existing graph.detect_required_joins() — reads query text via entities
        query_text = " ".join(enriched_intent.get("entity_names", []))
        query_text += " " + " ".join(enriched_intent.get("collection_hints", []))

        lookup_specs = graph.detect_required_joins(query_text, primary_collection)

        # Also check if any entity_names suggest a cross-collection lookup
        additional_lookups = self._infer_entity_lookups(
            enriched_intent, primary_collection, graph
        )
        # Deduplicate by "from" collection
        existing_froms: Set[str] = {spec.get("from", "") for spec in lookup_specs}
        for spec in additional_lookups:
            if spec.get("from") not in existing_froms:
                lookup_specs.append(spec)
                existing_froms.add(spec.get("from", ""))

        result = dict(wrapped_plan)   # shallow copy of wrapper (original_plan untouched)
        result["join_hints"] = lookup_specs

        # Add join steps to structured_steps (informational only — not executed directly)
        existing_steps = list(result.get("structured_steps", []))
        step_offset = len(existing_steps)
        for i, spec in enumerate(lookup_specs, 1):
            existing_steps.append({
                "step": step_offset + i,
                "type": "lookup",
                "description": f"$lookup join: {primary_collection} → {spec.get('from', '')} "
                               f"via {spec.get('localField', '')}",
                "depends_on": step_offset,
            })
        result["structured_steps"] = existing_steps

        record_event("rel_enhancer_ok", {
            "primary_collection": primary_collection,
            "join_count": len(lookup_specs),
        })
        logger.debug(
            f"[RelEnhancer] primary={primary_collection} "
            f"joins_added={len(lookup_specs)}: "
            f"{[s.get('from') for s in lookup_specs]}"
        )
        return result

    def _infer_entity_lookups(
        self,
        enriched_intent: Dict[str, Any],
        primary_collection: str,
        graph: Any,
    ) -> List[Dict]:
        """
        Infer additional lookups from entity_names and collection_hints
        that might not be caught by query-text keyword matching.
        """
        specs: List[Dict] = []
        collection_hints = enriched_intent.get("collection_hints", [])
        for target_coll in collection_hints:
            if target_coll == primary_collection:
                continue
            spec = graph.lookup_spec(primary_collection, target_coll)
            if spec:
                specs.append(spec)
                break  # one hint at a time to avoid over-joining
        return specs


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys, json
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    enhancer = RelationshipEnhancer()
    all_passed = True

    print("=" * 60)
    print("RelationshipEnhancer — Standalone Tests")
    print("=" * 60)

    # Test 1: needs_relationship_join=False → plan unchanged
    print("\nTest 1: needs_relationship_join=False → plan returned unchanged")
    wrapped = {"original_plan": {"type": "find", "collection": "deals"}, "structured_steps": []}
    intent = {"needs_relationship_join": False, "entity_names": [], "collection_hints": []}
    result = enhancer.enhance(wrapped, intent)
    if result is wrapped:
        print("  PASS — same object returned (zero processing)")
    else:
        print(f"  FAIL — object was not returned unchanged")
        all_passed = False

    # Test 2: needs_relationship_join=True, contacts → companies
    print("\nTest 2: contacts plan needing company join")
    wrapped2 = {
        "original_plan": {"type": "find", "collection": "contacts", "filter": {}},
        "structured_steps": [{"step": 1, "type": "filter", "description": "Filter contacts"}],
    }
    intent2 = {
        "needs_relationship_join": True,
        "entity_names": ["company name"],
        "collection_hints": ["contacts", "companies"],
    }
    result2 = enhancer.enhance(wrapped2, intent2)
    if result2.get("original_plan") is wrapped2["original_plan"]:
        print("  PASS — original_plan untouched")
    else:
        print("  FAIL — original_plan was modified")
        all_passed = False
    print(f"  join_hints: {result2.get('join_hints', [])}")

    # Test 3: None wrapped_plan → returns None unchanged
    print("\nTest 3: None wrapped_plan → safe passthrough")
    result3 = enhancer.enhance(None, {"needs_relationship_join": True})
    if result3 is None:
        print("  PASS — None returned safely")
    else:
        print(f"  FAIL — {result3}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
