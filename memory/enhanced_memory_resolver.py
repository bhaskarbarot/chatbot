"""
Enhanced Memory Resolver — Enhancement 9.

Wraps the existing ChatMemory WITHOUT modifying it.

Responsibilities:
1. Resolve pronoun/reference follow-ups: "those", "them", "same", "it",
   "their", "again", "those 5", "the ones you showed" → inject last context.
2. Store full structured context (enriched_intent + result IDs) after
   each response — async/non-blocking so the main pipeline is never delayed.

Integration contract:
  resolve(query, chat_memory) → (resolved_query_str, is_followup_bool, merged_ctx)
  store_context_async(enriched_intent, raw_results) → None (fire-and-forget)

Thread safety:
  - _state is protected by a threading.Lock.
  - store_context_async runs in a daemon thread; never blocks the caller.
  - resolve() reads _state under lock; is thread-safe.
"""
from __future__ import annotations

import copy
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("enhanced_memory_resolver")

# ── Reference patterns → resolution strategy ─────────────────────────────────
_REFERENCE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\b(those|them|they)\b', re.I),        "last_result_set"),
    (re.compile(r'\bthe\s+ones\s+you\s+showed\b', re.I),"last_result_set"),
    (re.compile(r'\bthose\s+\d+\b', re.I),              "last_result_set_slice"),
    (re.compile(r'\bsame\s+(filter|query|search)\b', re.I), "same_filter"),
    (re.compile(r'\bagain\b', re.I),                     "repeat_last"),
    (re.compile(r'\b(it|that)\b', re.I),                 "last_single_entity"),
    (re.compile(r'\btheir\b|\bits\b', re.I),             "last_entity_possession"),
    (re.compile(r'\bthat\s+(company|lead|deal|contact|owner|user)\b', re.I),
                                                          "last_entity_by_type"),
]


class EnhancedMemoryResolver:
    """
    Stateful wrapper over ChatMemory.
    Tracks last structured context across turns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-session state (reset on first use, updated after each turn)
        self._last_result_ids: List[str] = []
        self._last_collection: str = ""
        self._last_temporal_filter: Dict[str, Any] = {}
        self._last_structured_context: Dict[str, Any] = {}
        self._last_entity_by_type: Dict[str, str] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(
        self,
        user_query: str,
        chat_memory: Any,
    ) -> Tuple[str, bool, Dict[str, Any]]:
        """
        Resolve references in user_query using last structured context.

        Returns: (resolved_query_str, is_followup_bool, merged_context_dict)
        On any failure: returns (user_query, False, {}) — never blocks.
        """
        if not user_query or not isinstance(user_query, str):
            return user_query, False, {}

        try:
            # Always use existing chat_memory.resolve_followup() as the base
            resolved_query, is_followup = chat_memory.resolve_followup(user_query)

            # Detect reference patterns in the ORIGINAL query (not the already-resolved one)
            strategy = self._detect_reference_strategy(user_query)

            if not strategy:
                return resolved_query, is_followup, {}

            with self._lock:
                last_ctx = copy.deepcopy(self._last_structured_context)
                last_ids = list(self._last_result_ids)
                last_coll = self._last_collection
                last_tf = copy.deepcopy(self._last_temporal_filter)
                last_ent = copy.deepcopy(self._last_entity_by_type)

            if not last_ctx and not last_ids:
                # No stored context yet — can't resolve references
                return resolved_query, is_followup, {}

            merged_context = self._build_merged_context(
                user_query, resolved_query, strategy,
                last_ctx, last_ids, last_coll, last_tf, last_ent
            )

            record_event("enhanced_memory_resolved", {
                "strategy": strategy,
                "has_last_ids": bool(last_ids),
                "collection": last_coll,
            })
            logger.debug(
                f"[EnhancedMemory] strategy={strategy} "
                f"collection={last_coll} ids={len(last_ids)}"
            )

            return resolved_query, True, merged_context

        except Exception as e:
            logger.warning(f"[EnhancedMemory] resolve failed: {e} — using original query")
            record_event("enhanced_memory_resolve_error", {"error": str(e)[:120]})
            return user_query, False, {}

    def store_context_async(
        self,
        enriched_intent: Dict[str, Any],
        raw_results: Any,
    ) -> None:
        """
        Store structured context after each response.
        Runs in a daemon thread — never blocks the main pipeline.
        """
        def _store():
            try:
                self._store_context(enriched_intent, raw_results)
            except Exception as e:
                logger.warning(f"[EnhancedMemory] store_context failed: {e}")

        t = threading.Thread(target=_store, daemon=True)
        t.start()

    @classmethod
    def safe_default(cls) -> Tuple[str, bool, Dict[str, Any]]:
        """Return neutral safe output when layer is unavailable."""
        return "", False, {}

    # ── Internal: reference strategy detection ────────────────────────────────

    def _detect_reference_strategy(self, query: str) -> Optional[str]:
        """Identify the first matching reference strategy in query."""
        ql = query.lower()
        for pattern, strategy in _REFERENCE_PATTERNS:
            if pattern.search(ql):
                return strategy
        return None

    # ── Internal: context building ────────────────────────────────────────────

    def _build_merged_context(
        self,
        original_query: str,
        resolved_query: str,
        strategy: str,
        last_ctx: Dict,
        last_ids: List[str],
        last_coll: str,
        last_tf: Dict,
        last_ent: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Build a merged context dict that the orchestrator can use to
        inject last state into the current turn.
        """
        ctx: Dict[str, Any] = {}

        if strategy == "last_result_set":
            ctx["inject_ids"] = last_ids
            ctx["inject_collection"] = last_coll
            ctx["inject_filter_on_ids"] = True

        elif strategy == "last_result_set_slice":
            # "those 5" → get the N from the original query
            m = re.search(r'those\s+(\d+)', original_query, re.I)
            n = int(m.group(1)) if m else len(last_ids)
            ctx["inject_ids"] = last_ids[:n]
            ctx["inject_collection"] = last_coll
            ctx["inject_filter_on_ids"] = True

        elif strategy == "same_filter":
            ctx["inject_temporal_filter"] = last_tf
            ctx["inject_collection"] = last_coll
            ctx["carry_forward_filters"] = True

        elif strategy == "repeat_last":
            ctx["repeat_last_query"] = True
            ctx["inject_collection"] = last_coll
            ctx["inject_temporal_filter"] = last_tf

        elif strategy == "last_single_entity":
            # "it" / "that" → last discussed single entity
            entity = last_ctx.get("entity_names", [])
            if entity:
                ctx["inject_entity"] = entity[0]
            ctx["inject_collection"] = last_coll

        elif strategy == "last_entity_possession":
            # "their" / "its" → last discussed entity's related data
            ctx["inject_entity"] = last_ent
            ctx["inject_collection"] = last_coll
            ctx["possession_lookup"] = True

        elif strategy == "last_entity_by_type":
            # "that company/lead/deal" → last entity of that specific type
            m = re.search(
                r'\bthat\s+(company|lead|deal|contact|owner|user)\b',
                original_query, re.I
            )
            if m:
                etype = m.group(1).lower()
                entity_name = last_ent.get(etype)
                if entity_name:
                    ctx["inject_entity"] = entity_name
                    ctx["inject_entity_type"] = etype
            ctx["inject_collection"] = last_coll

        # Always carry forward temporal context if current query doesn't have one
        # and strategy didn't already set it
        if "inject_temporal_filter" not in ctx and last_tf.get("type") not in (None, "none"):
            # Only auto-carry temporal for "same" / follow-up patterns
            if strategy in ("same_filter", "repeat_last"):
                ctx["inject_temporal_filter"] = last_tf

        ctx["_strategy"] = strategy
        return ctx

    # ── Internal: context storage ─────────────────────────────────────────────

    def _store_context(
        self,
        enriched_intent: Dict[str, Any],
        raw_results: Any,
    ) -> None:
        """Extract and store structured state from latest turn."""
        if not isinstance(enriched_intent, dict):
            return

        # Extract _id values from raw_results list
        result_ids: List[str] = []
        if isinstance(raw_results, list):
            for item in raw_results:
                if isinstance(item, dict):
                    _id = item.get("_id")
                    if _id is not None:
                        result_ids.append(str(_id))

        # Extract entity-by-type mapping
        entity_by_type: Dict[str, str] = {}
        for name in enriched_intent.get("entity_names", []):
            if name:
                # Store under generic "entity" key; specific type detection
                # requires DB schema knowledge not available here
                entity_by_type["entity"] = str(name)
        # Preserve any existing typed entities already in store
        with self._lock:
            merged_ent = copy.deepcopy(self._last_entity_by_type)
        merged_ent.update(entity_by_type)

        collection = ""
        coll_hints = enriched_intent.get("collection_hints", [])
        if coll_hints:
            collection = coll_hints[0]

        temporal_filter = enriched_intent.get("temporal_filter", {})
        if not isinstance(temporal_filter, dict):
            temporal_filter = {}

        with self._lock:
            self._last_result_ids = result_ids[:200]   # cap stored IDs
            self._last_collection = collection
            self._last_temporal_filter = copy.deepcopy(temporal_filter)
            self._last_structured_context = {
                k: copy.deepcopy(v) for k, v in enriched_intent.items()
            }
            self._last_entity_by_type = merged_ent

        logger.debug(
            f"[EnhancedMemory] stored: ids={len(result_ids)} "
            f"collection={collection} "
            f"temporal_type={temporal_filter.get('type')}"
        )


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    from memory.chat_memory import ChatMemory

    print("=" * 60)
    print("EnhancedMemoryResolver — Standalone Tests")
    print("=" * 60)

    resolver = EnhancedMemoryResolver()
    memory = ChatMemory()
    all_passed = True

    # Test 1: No stored context — "those" reference returns original query
    print("\nTest 1: 'show me those contacts' with no stored context")
    rq, is_fu, ctx = resolver.resolve("show me those contacts", memory)
    if ctx.get("inject_filter_on_ids") is not True and not ctx:
        print("  PASS — no context stored yet, no injection attempted")
    else:
        # ctx will be empty since last_ids is empty
        if not ctx.get("inject_ids"):
            print("  PASS — empty inject_ids as expected")
        else:
            print(f"  FAIL — unexpected ctx: {ctx}")
            all_passed = False

    # Test 2: Store context then resolve "those"
    print("\nTest 2: Store leads context, then resolve 'show me those records'")
    import time
    enriched = {
        "entity_names": ["Acme Corp"],
        "collection_hints": ["contacts"],
        "temporal_filter": {"type": "this_month"},
    }
    fake_results = [{"_id": "abc123"}, {"_id": "def456"}, {"_id": "ghi789"}]
    resolver.store_context_async(enriched, fake_results)
    time.sleep(0.1)   # give the daemon thread a moment

    rq2, is_fu2, ctx2 = resolver.resolve("show me those records", memory)
    if ctx2.get("inject_ids") == ["abc123", "def456", "ghi789"]:
        print("  PASS — inject_ids resolved correctly")
    else:
        print(f"  FAIL — inject_ids={ctx2.get('inject_ids')!r}")
        all_passed = False

    # Test 3: Detect "same filter" strategy
    print("\nTest 3: 'same filter for this month' → same_filter strategy")
    rq3, is_fu3, ctx3 = resolver.resolve("same filter for leads", memory)
    if ctx3.get("_strategy") == "same_filter":
        print("  PASS — strategy=same_filter detected")
    else:
        print(f"  FAIL — strategy={ctx3.get('_strategy')!r}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
