"""
Query Decomposition Layer — Enhancement 3.

Activated ONLY when enriched_intent.is_multi_intent=True.
Rule-based splitting (NO LLM call for decomposition — uses "and/also/vs" connectors).

Per mandatory adjustment 1:
  - Plan BUILDING is sequential (Ollama is single-threaded).
  - Only MongoDB EXECUTION calls are parallelized via ThreadPoolExecutor.

Per mandatory adjustment 4:
  - When this layer returns a valid response, sets _decomposition_handled=True
    on the enriched_intent dict so the existing _dynamic_worker_pipeline
    call in the orchestrator is skipped — preventing double decomposition.

Safety:
  - Max 3 sub-queries.
  - 20-second total timeout for all parallel executions.
  - If any sub-query fails: include successful ones, note failed part.
  - If decomposer itself fails: raise so orchestrator catches it and falls
    through to single-query path.

Thread-safe: no shared state between calls.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("query_decomposer")

_MAX_SUB_QUERIES = 3
_PARALLEL_EXEC_TIMEOUT_S = 20.0


class QueryDecomposer:
    """
    Splits multi-intent queries into sub-queries, plans each sequentially,
    then executes all sub-query MongoDB calls in parallel.
    """

    def decompose_and_execute(
        self,
        resolved_query: str,
        enriched_intent: Dict[str, Any],
        plan_builder_fn: Callable,       # (query, matched_rules, context) → plan_dict|None
        mongo_execute_fn: Callable,      # (plan_dict) → results
        formatter_fn: Callable,          # (data, query) → str
    ) -> Optional[str]:
        """
        Entry point.
        Returns merged response string on success.
        Returns None if only 1 sub-query was found (caller continues normally).
        Raises on critical failure so orchestrator catches it and falls through.
        """
        sub_queries = self._split_query(resolved_query, enriched_intent)

        if len(sub_queries) <= 1:
            return None   # not actually multi-intent — let main pipeline handle it

        record_event("query_decomposer_triggered", {
            "sub_query_count": len(sub_queries),
            "query": resolved_query[:80],
        })
        logger.info(
            f"[QueryDecomposer] {len(sub_queries)} sub-queries from: "
            f"'{resolved_query[:60]}'"
        )

        # ── Phase 1: Build plans SEQUENTIALLY (Ollama is single-threaded) ────
        plans: List[Tuple[Dict, str]] = []
        for sq in sub_queries:
            plan = self._build_plan_safe(sq, plan_builder_fn)
            if plan:
                plans.append((plan, sq["sub_query_text"]))
                logger.debug(
                    f"[QueryDecomposer] plan built for '{sq['sub_query_text'][:50]}': "
                    f"type={plan.get('type')}"
                )
            else:
                logger.warning(
                    f"[QueryDecomposer] plan failed for '{sq['sub_query_text'][:50]}' — skipping"
                )

        if not plans:
            logger.warning("[QueryDecomposer] No plans built — falling through")
            return None

        # ── Phase 2: Execute in PARALLEL (MongoDB I/O bound) ─────────────────
        results = self._execute_parallel(plans, mongo_execute_fn)

        if not results:
            return None   # all executions failed — fall through to single path

        # ── Phase 3: Merge results ────────────────────────────────────────────
        response = self._merge_results(
            resolved_query, sub_queries, results, formatter_fn
        )

        record_event("query_decomposer_ok", {
            "plans_built": len(plans),
            "results_returned": len(results),
        })
        return response

    # ── Query splitting ───────────────────────────────────────────────────────

    def _split_query(
        self,
        query: str,
        enriched_intent: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Rule-based split on connectors. Returns list of sub-query objects.
        Max 3 sub-queries.
        """
        # Strategy 1: "X and give/show/get me Y" explicit splits
        parts = re.split(
            r'\s+and\s+(?:give\s+me|show\s+me|get\s+me|also\s+show|also\s+give)\s+',
            query,
            flags=re.IGNORECASE,
        )
        if len(parts) >= 2:
            return self._build_sub_query_objects(parts[:_MAX_SUB_QUERIES], query)

        # Strategy 2: "X versus/vs Y" or "X compared to Y"
        vs_parts = re.split(
            r'\s+(?:vs\.?|versus|compared\s+to)\s+',
            query,
            flags=re.IGNORECASE,
        )
        if len(vs_parts) >= 2:
            return self._build_sub_query_objects(vs_parts[:_MAX_SUB_QUERIES], query)

        # Strategy 3: "X and also Y"
        also_parts = re.split(r'\s+and\s+also\s+', query, flags=re.IGNORECASE)
        if len(also_parts) >= 2:
            return self._build_sub_query_objects(also_parts[:_MAX_SUB_QUERIES], query)

        # Strategy 4: "as well as"
        aswell_parts = re.split(r'\s+as\s+well\s+as\s+', query, flags=re.IGNORECASE)
        if len(aswell_parts) >= 2:
            return self._build_sub_query_objects(aswell_parts[:_MAX_SUB_QUERIES], query)

        # Strategy 5: Use sub_intents from enriched_intent to synthesise queries
        sub_intents = enriched_intent.get("sub_intents", [])
        entity = _infer_entity(query)
        if len(sub_intents) >= 2 and entity:
            synthetic = self._synthesise_from_sub_intents(
                query, entity, sub_intents[:_MAX_SUB_QUERIES]
            )
            if len(synthetic) >= 2:
                return self._build_sub_query_objects(synthetic, query)

        return [self._single_sub_query(query)]

    def _build_sub_query_objects(
        self,
        parts: List[str],
        original_query: str,
    ) -> List[Dict[str, Any]]:
        """Convert raw split parts into sub-query dicts."""
        result = []
        for i, part in enumerate(parts):
            text = part.strip()
            if not text:
                continue
            result.append({
                "sub_query_text": text,
                "sub_intent": _infer_sub_intent(text),
                "response_shape": _infer_shape(text),
                "merge_label": _infer_label(text, i + 1),
            })
        return result

    def _single_sub_query(self, query: str) -> Dict[str, Any]:
        return {
            "sub_query_text": query,
            "sub_intent": "data_fetch",
            "response_shape": "list",
            "merge_label": "Results",
        }

    def _synthesise_from_sub_intents(
        self,
        query: str,
        entity: str,
        sub_intents: List[str],
    ) -> List[str]:
        """Build synthetic sub-queries from detected sub_intents."""
        parts = []
        for si in sub_intents:
            if si == "count":
                parts.append(f"how many {entity} are there in total")
            elif si == "analysis":
                parts.append(f"analysis and insights for {entity}")
            elif si == "top_n":
                m = re.search(r'top\s+(\d+)', query.lower())
                n = m.group(1) if m else "10"
                parts.append(f"top {n} {entity} by value")
            elif si == "breakdown":
                if "status" in query.lower():
                    parts.append(f"breakdown of {entity} by status with counts")
                elif "stage" in query.lower():
                    parts.append(f"breakdown of {entity} by stage with counts")
                else:
                    parts.append(f"breakdown of {entity} by category")
            elif si == "comparison":
                parts.append(query)   # keep original for comparison
        return parts

    # ── Plan building ─────────────────────────────────────────────────────────

    def _build_plan_safe(
        self,
        sub_query: Dict[str, Any],
        plan_builder_fn: Callable,
    ) -> Optional[Dict]:
        """Build a plan for a single sub-query. Returns None on failure."""
        try:
            plan = plan_builder_fn(sub_query["sub_query_text"], [], "")
            return plan if isinstance(plan, dict) else None
        except Exception as e:
            logger.warning(
                f"[QueryDecomposer] plan_builder_fn failed for "
                f"'{sub_query['sub_query_text'][:50]}': {e}"
            )
            return None

    # ── Parallel execution ────────────────────────────────────────────────────

    def _execute_parallel(
        self,
        plans: List[Tuple[Dict, str]],
        mongo_execute_fn: Callable,
    ) -> List[Dict[str, Any]]:
        """
        Execute all plans in parallel. Returns list of result dicts.
        Total timeout: _PARALLEL_EXEC_TIMEOUT_S seconds.
        """
        results = []

        def _run(plan_sq: Tuple[Dict, str]) -> Optional[Dict]:
            plan, sq_text = plan_sq
            try:
                data = mongo_execute_fn(plan)
                return {
                    "sub_query": sq_text,
                    "data": data,
                    "plan": plan,
                }
            except Exception as e:
                logger.warning(
                    f"[QueryDecomposer] execution failed for '{sq_text[:50]}': {e}"
                )
                return None

        workers = min(len(plans), _MAX_SUB_QUERIES)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_run, p): p[1] for p in plans}
            try:
                for future in as_completed(
                    futures, timeout=_PARALLEL_EXEC_TIMEOUT_S
                ):
                    sq_text = futures[future]
                    try:
                        res = future.result()
                        if res is not None:
                            results.append(res)
                    except Exception as e:
                        logger.warning(
                            f"[QueryDecomposer] future result error "
                            f"for '{sq_text[:50]}': {e}"
                        )
            except TimeoutError:
                logger.warning(
                    f"[QueryDecomposer] parallel execution timed out after "
                    f"{_PARALLEL_EXEC_TIMEOUT_S}s"
                )

        return results

    # ── Result merging ────────────────────────────────────────────────────────

    def _merge_results(
        self,
        original_query: str,
        sub_queries: List[Dict],
        results: List[Dict[str, Any]],
        formatter_fn: Callable,
    ) -> str:
        """Merge all sub-query results into a single labelled response."""
        sections = [
            f"**Multi-part query results for:** {original_query[:100]}\n",
        ]

        # Build label map from sub_queries
        label_map: Dict[str, str] = {}
        for sq in sub_queries:
            label_map[sq["sub_query_text"]] = sq.get("merge_label", "Results")

        successful = 0
        for res in results:
            sq_text = res.get("sub_query", "")
            data    = res.get("data")
            label   = label_map.get(sq_text, sq_text[:50])

            sections.append(f"\n### {label}")

            if data is None or (isinstance(data, list) and len(data) == 0):
                sections.append("No data found for this part of the query.")
                continue

            try:
                section_response = formatter_fn(data, sq_text)
                sections.append(section_response)
                successful += 1
            except Exception as e:
                logger.warning(f"[QueryDecomposer] formatter failed for '{sq_text[:50]}': {e}")
                if isinstance(data, (int, float)):
                    sections.append(f"Count: **{int(data)}**")
                elif isinstance(data, list):
                    sections.append(f"Found {len(data)} records.")
                else:
                    sections.append(str(data)[:200])
                successful += 1

            sections.append("")

        if successful == 0:
            return ""

        # Note any failed parts
        total_attempted = len(sub_queries)
        if successful < total_attempted:
            failed_count = total_attempted - successful
            sections.append(
                f"\n*Note: {failed_count} part(s) of the query could not be processed.*"
            )

        return "\n".join(sections)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_entity(query: str) -> str:
    m = re.search(
        r'\b(deals?|invoices?|contacts?|compan(?:y|ies)|tasks?|leads?|'
        r'users?|outreaches?|sales|meetings?)\b',
        query, re.IGNORECASE
    )
    return m.group(1) if m else ""


def _infer_sub_intent(text: str) -> str:
    ql = text.lower()
    if re.search(r'\bhow\s+many\b|\bcount\b|\bnumber\s+of\b', ql):
        return "count"
    if re.search(r'\banalyze\b|\banalysis\b|\binsight\b', ql):
        return "analysis"
    if re.search(r'\btop\s+\d+\b', ql):
        return "top_n"
    if re.search(r'\bbreakdown\b|\bby\s+(?:stage|status|owner)\b', ql):
        return "breakdown"
    if re.search(r'\bcompare\b|\bvs\.?\b|\bversus\b', ql):
        return "comparison"
    return "data_fetch"


def _infer_shape(text: str) -> str:
    ql = text.lower()
    if re.search(r'\bhow\s+many\b|\bcount\b|\bnumber\s+of\b', ql):
        return "number"
    if re.search(r'\bany\b.{0,20}\bavailable\b|\bdo\s+we\s+have\b', ql):
        return "yes_no"
    if re.search(r'\btable\b|\btabular\b', ql):
        return "table"
    return "list"


def _infer_label(text: str, index: int) -> str:
    """Create a section label for a sub-query result."""
    # Capitalise first significant word
    text = text.strip()
    if not text:
        return f"Part {index}"
    # Shorten to first 60 chars
    label = text[:60].strip()
    if len(text) > 60:
        label += "..."
    return label.capitalize()


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    decomposer = QueryDecomposer()
    all_passed = True

    print("=" * 60)
    print("QueryDecomposer — Standalone Tests")
    print("=" * 60)

    # Test 1: Split "how many leads and how many deals" → 2 sub-queries
    print("\nTest 1: 'and give me' split")
    query1 = "how many leads do we have and give me how many deals are open"
    sqs1 = decomposer._split_query(query1, {"sub_intents": ["count", "count"]})
    if len(sqs1) >= 2:
        print(f"  PASS — {len(sqs1)} sub-queries: {[s['sub_query_text'][:40] for s in sqs1]}")
    else:
        print(f"  FAIL — only {len(sqs1)} sub-query found: {sqs1}")
        all_passed = False

    # Test 2: "vs" split
    print("\nTest 2: 'vs' split → 2 sub-queries")
    query2 = "closed won deals vs closed lost deals this year"
    sqs2 = decomposer._split_query(query2, {"sub_intents": ["comparison"]})
    if len(sqs2) >= 2:
        print(f"  PASS — {len(sqs2)} sub-queries: {[s['sub_query_text'][:40] for s in sqs2]}")
    else:
        print(f"  FAIL — only {len(sqs2)} sub-query: {sqs2}")
        all_passed = False

    # Test 3: No connector → single sub-query (decomposer returns 1)
    print("\nTest 3: No connector → returns 1 sub-query (decompose_and_execute returns None)")
    query3 = "show me top 5 deals"
    sqs3 = decomposer._split_query(query3, {"sub_intents": ["top_n"]})
    if len(sqs3) == 1:
        print("  PASS — single sub-query, decomposer will return None")
    else:
        print(f"  FAIL — {len(sqs3)} sub-queries: {sqs3}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
