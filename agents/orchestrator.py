"""
Pipeline Orchestrator.
Main brain of the chatbot. Handles the full query pipeline:
1. Greeting / out-of-scope detection
2. Follow-up resolution using chat memory
3. Context matching (new query related to last conversation?)
4. Person lookup (fast path for "who is <name>")
5. Parallel intent pipeline (analysis/category queries)
6. Dynamic worker pipeline (multi-intent complex queries)
7. Standard single-query pipeline
8. Response formatting with executive summary
9. Memory update
"""
import json
import re
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from config import (
    GREETINGS, MAX_RESPONSE_TIME, TOP_K_RULES, FAISS_INDEX_PATH,
    RETRIEVAL_MIN_SCORE, RETRIEVAL_MIN_MARGIN, COLLECTION_NAMES,
)
from utils.logger import get_logger, PipelineTimer
from utils.formatter import (
    auto_format, detect_format_intent, format_aggregation,
    _executive_summary_lines,
)
from utils.masking import mask_response
from utils.metrics import record_event
from utils.cache import ResponseCache
from utils.circuit_breaker import get_llm_breaker
from knowledge.intent_classifier import (
    classify as classify_intent,
    get_collection as get_intent_collection,
    build_fast_plan,
    CONFIDENCE_THRESHOLD,
    Intent,
)
from memory.chat_memory import ChatMemory
from knowledge.vector_store import (
    keyword_boost_search, is_index_built, evaluate_match_confidence, get_embedding_model
)
from knowledge.prompts import (build_greeting_response, build_not_found_response,
                                build_out_of_scope_response)
from agents.query_planner import plan_query, plan_query_from_rule, get_llm
from tools.mongodb_tool import (execute_query_plan, search_by_keyword,
                                 resolve_name_to_id, get_collection_sample)

logger = get_logger("orchestrator")


class ChatOrchestrator:
    """
    Main chatbot orchestrator. Manages the full query pipeline.
    """

    def __init__(self):
        self.memory = ChatMemory()
        self.cache = ResponseCache()
        self.logs: List[str] = []
        self._llm = None
        self._collection_token_cache: Dict[str, set] = {}
        self._warmup_started = False
        self._current_query: str = ""
        self._start_background_warmup()

    def _start_background_warmup(self):
        """Warm embedding/index resources in background for faster first query."""
        if self._warmup_started:
            return
        self._warmup_started = True

        def _warm():
            try:
                if is_index_built():
                    get_embedding_model()
            except Exception:
                pass

        threading.Thread(target=_warm, daemon=True).start()

    def process_query(self, user_query: str) -> Dict:
        """
        Process a user query through the full pipeline.
        Returns: {response, logs, entity, format, timing, error, collection}
        """
        start_time = time.time()
        self.logs = []
        self._current_query = user_query
        self._log("Received query", user_query)

        try:
            # Step 0: Greetings
            if self._is_greeting(user_query):
                response = build_greeting_response(user_query)
                return self._build_result(response, start_time, format_type="greeting")

            # Step 1: Out-of-scope
            if self._is_out_of_scope(user_query):
                response = build_out_of_scope_response(user_query)
                return self._build_result(response, start_time, format_type="out_of_scope")

            # Step 2: Follow-up resolution using memory
            resolved_query, is_followup = self.memory.resolve_followup(user_query)
            if is_followup:
                self._log("Follow-up resolved", f"'{user_query}' -> '{resolved_query}'")

            # Step 2.1: Context matching — check if new query relates to last conversation
            if not is_followup and self.memory.short_term:
                is_related, ctx_hint = self.memory.is_contextually_related(resolved_query)
                if is_related:
                    self._log("Context match", "Query is related to previous conversation context")
                else:
                    self._log("Context match", "New topic — using fresh context")

            # Step 2.25: Fast person lookup for "who is <name>" queries
            person_result = self._person_lookup_pipeline(user_query, resolved_query, start_time)
            if person_result is not None:
                return person_result

            # Step 3: Format hint detection (done early so routing pipelines can use it)
            format_hint = detect_format_intent(user_query)
            self._log("Format intent", format_hint)

            # Step 3.1: Redis semantic cache lookup
            cache_hit = self.cache.get_best(resolved_query)
            if cache_hit and isinstance(cache_hit.payload, dict):
                self._log("Cache hit", f"score={cache_hit.score:.2f}")
                cached_response = cache_hit.payload.get("response")
                if cached_response:
                    entity = cache_hit.payload.get("entity")
                    collection = cache_hit.payload.get("collection")
                    fmt = cache_hit.payload.get("format", format_hint)
                    self.memory.add_exchange(
                        user_query, cached_response, entity=entity, collection=collection
                    )
                    return self._build_result(
                        cached_response,
                        start_time,
                        format_type=fmt,
                        entity=entity,
                        collection=collection,
                    )

            # Step 2.5: Parallel intent pipeline for analysis/category/breakdown queries
            parallel_result = self._parallel_intent_pipeline(
                user_query, resolved_query, is_followup, start_time, format_hint
            )
            if parallel_result is not None:
                return parallel_result

            # Step 2.75: Dynamic worker pipeline for multi-intent complex queries
            dynamic_result = self._dynamic_worker_pipeline(
                user_query, resolved_query, is_followup, start_time
            )
            if dynamic_result is not None:
                return dynamic_result

            # Step 3.5: Intent pre-classifier fast-path (LLM bypass for clear intents)
            intent, conf = classify_intent(resolved_query)
            if intent != Intent.UNKNOWN and conf >= CONFIDENCE_THRESHOLD:
                collection_hint = get_intent_collection(intent, resolved_query)
                if collection_hint:
                    fast_plan = build_fast_plan(intent, collection_hint, resolved_query)
                    if fast_plan:
                        self._log(
                            "Intent fast-path",
                            f"intent={intent.value} conf={conf:.2f} collection={collection_hint}",
                        )
                        record_event("intent_fast_path", {
                            "intent": intent.value,
                            "confidence": round(conf, 3),
                            "collection": collection_hint,
                        })
                        fast_plan = self._apply_business_intent_overrides(resolved_query, fast_plan)
                        fast_plan = self._apply_entity_constraints(resolved_query, fast_plan)
                        data = execute_query_plan(fast_plan)
                        if not self._is_empty_result(data):
                            result_count = self._estimate_result_count(data)
                            self._log("Fast-path result", f"{result_count} records")
                            response = auto_format(data, user_query,
                                                   fast_plan.get("response_hint", format_hint))
                            response = mask_response(response)
                            entity = fast_plan.get("entity")
                            collection = fast_plan.get("collection", "")
                            self.memory.add_exchange(
                                user_query, response, entity=entity, collection=collection
                            )
                            return self._build_result(
                                response, start_time,
                                format_type=fast_plan.get("response_hint", format_hint),
                                entity=entity, collection=collection,
                            )
                        self._log("Fast-path empty", "Falling through to full pipeline")

            # Step 4: Vector rule search
            self._log("Searching rules", "...")
            matched_rules = []
            retrieval_is_confident = False
            if is_index_built():
                matched_rules = keyword_boost_search(
                    resolved_query, top_k=TOP_K_RULES
                )
                if matched_rules:
                    top_score = matched_rules[0].get("final_score", 0)
                    self._log("Top rule match",
                              f"score={top_score:.3f}: {matched_rules[0]['intent'][:80]}")
                    confidence = evaluate_match_confidence(
                        matched_rules,
                        min_score=RETRIEVAL_MIN_SCORE,
                        min_margin=RETRIEVAL_MIN_MARGIN,
                    )
                    retrieval_is_confident = confidence["is_confident"]
                    self._log(
                        "Retrieval confidence",
                        f"confident={confidence['is_confident']} "
                        f"top={confidence['top_score']:.3f} "
                        f"margin={confidence['margin']:.3f}"
                    )
                    record_event("retrieval_confidence", {
                        "query": resolved_query[:120],
                        "is_confident": confidence["is_confident"],
                        "top_score": confidence["top_score"],
                        "margin": confidence["margin"],
                    })
                    if not confidence["is_confident"]:
                        self._log("Low-confidence retrieval", "Using only top rule template")
                        matched_rules = matched_rules[:1]

            chat_context = self.memory.get_context_string()

            # Step 5: Rule-compiled fast plan (no LLM)
            query_plan = None
            if matched_rules and retrieval_is_confident:
                candidate_plan = plan_query_from_rule(
                    user_query, matched_rules[0], chat_context,
                    resolved_query if is_followup else None
                )
                if candidate_plan and self._is_rule_plan_compatible(resolved_query, candidate_plan):
                    # Skip rule-compiled COUNT plan when user explicitly wants a table —
                    # COUNT cannot produce a table; we need an aggregate GROUP BY from the LLM.
                    if (format_hint == "table"
                            and candidate_plan.get("type") == "count"):
                        self._log("Rule-compiled plan skipped",
                                  "Table format incompatible with COUNT plan — using LLM")
                    else:
                        query_plan = candidate_plan
                        self._log("Rule-compiled plan",
                                  f"type={query_plan.get('type')}, collection={query_plan.get('collection', 'multi')}")
                elif candidate_plan:
                    self._log("Rule-compiled plan skipped", "Collection mismatch with query intent")
            elif matched_rules:
                self._log("Rule-compiled plan skipped", "Low retrieval confidence; using LLM planning")

            # Step 5b: LLM plan generation
            if not query_plan:
                self._log("Planning query", "Sending to LLM...")
                query_plan = plan_query(
                    user_query, matched_rules, chat_context,
                    resolved_query if is_followup else None,
                    format_hint=format_hint,
                )

            if not query_plan:
                self._log("LLM plan failed", "Trying keyword search fallback")
                return self._fallback_search(
                    user_query, resolved_query, format_hint, start_time, matched_rules
                )

            self._log("Query plan",
                      f"type={query_plan.get('type')}, "
                      f"collection={query_plan.get('collection', 'multi')}")

            query_plan = self._apply_business_intent_overrides(resolved_query, query_plan)
            query_plan = self._apply_entity_constraints(resolved_query, query_plan)
            query_plan = self._align_plan_with_collection_intent(resolved_query, query_plan)

            # Step 6: Execute query
            self._log("Executing query", "...")
            data = execute_query_plan(query_plan)
            result_count = self._estimate_result_count(data)
            self._log("Query result", f"{result_count} records")

            # Step 6.5: Semantic verifier (for narrow entity searches with weak results)
            if (not self._is_empty_result(data)
                    and self._should_run_relevance_verifier(resolved_query, data)
                    and matched_rules
                    and (time.time() - start_time) < max(8, MAX_RESPONSE_TIME * 0.4)):
                self._log("Verifier", "Low relevance detected, attempting corrective replan")
                verify_query = (
                    f"{resolved_query}\n\n"
                    "Previous result appears semantically weak. "
                    "Return a corrected MongoDB plan aligned to intent and matched business rules."
                )
                verified_plan = plan_query(
                    verify_query, matched_rules, chat_context,
                    resolved_query if is_followup else None,
                    format_hint=format_hint,
                )
                if verified_plan:
                    verified_data = execute_query_plan(verified_plan)
                    if not self._is_empty_result(verified_data):
                        data = verified_data
                        query_plan = verified_plan
                        self._log("Verifier", "Corrective replan produced better result")

            # Step 7: Empty result handling with replan
            if self._is_empty_result(data):
                if matched_rules and (time.time() - start_time) < max(15, MAX_RESPONSE_TIME * 0.65):
                    self._log("Replan", "No results — retrying with semantic hint")
                    replan_query = (
                        f"{resolved_query}\n\n"
                        "Previous plan returned zero rows. Try an alternative valid query plan "
                        "using the matched business rules and the closest relevant collection."
                    )
                    retry_plan = plan_query(
                        replan_query, matched_rules, chat_context,
                        resolved_query if is_followup else None,
                        format_hint=format_hint,
                    )
                    if retry_plan:
                        self._log("Replan query plan",
                                  f"type={retry_plan.get('type')}, collection={retry_plan.get('collection', 'multi')}")
                        retry_data = execute_query_plan(retry_plan)
                        retry_count = self._estimate_result_count(retry_data)
                        self._log("Replan result", f"{retry_count} records")
                        if not self._is_empty_result(retry_data):
                            data = retry_data
                            query_plan = retry_plan
                elif matched_rules:
                    self._log("Replan skipped", "Time budget exceeded; using fallback strategy")

            if self._is_empty_result(data):
                # Generic schema-aware fallback for empty aggregate/count outcomes.
                fallback_plan = self._build_schema_fallback_plan(
                    resolved_query, query_plan, format_hint
                )
                if fallback_plan:
                    self._log(
                        "Fallback replan",
                        f"type={fallback_plan.get('type')}, collection={fallback_plan.get('collection', 'multi')}",
                    )
                    fallback_data = execute_query_plan(fallback_plan)
                    fallback_count = self._estimate_result_count(fallback_data)
                    self._log("Fallback result", f"{fallback_count} records")
                    if not self._is_empty_result(fallback_data):
                        data = fallback_data
                        query_plan = fallback_plan

            if self._is_empty_result(data):
                collection = query_plan.get("collection", "")
                response = build_not_found_response(resolved_query, collection)
                return self._build_result(response, start_time,
                                          format_type="not_found",
                                          collection=collection)

            # Step 8: Format response
            self._log("Formatting response", format_hint)
            response_hint = query_plan.get("response_hint", format_hint)

            if self._needs_llm_formatting(data, user_query):
                response = self._llm_format(user_query, data, response_hint)
                response = self._ensure_executive_summary(response, data, user_query)
            else:
                response = auto_format(data, user_query, response_hint)

            # Step 9: Masking
            response = mask_response(response)

            # Step 10: Update memory
            entity = query_plan.get("entity")
            collection = query_plan.get("collection", "")
            if isinstance(query_plan.get("steps"), list) and query_plan["steps"]:
                collection = query_plan["steps"][-1].get("collection", collection)
            self.memory.add_exchange(
                user_query, response, entity=entity, collection=collection
            )

            return self._build_result(response, start_time,
                                      format_type=response_hint,
                                      entity=entity, collection=collection)

        except Exception as e:
            logger.error(f"Pipeline error: {e}\n{traceback.format_exc()}")
            self._log("ERROR", str(e))
            return self._build_result(
                f"I encountered an error processing your query. "
                f"Please try rephrasing your question.\n\nError: {str(e)}",
                start_time, error=str(e)
            )

    # ── Dynamic Worker Pipeline ───────────────────────

    def _dynamic_worker_pipeline(self, user_query: str, resolved_query: str,
                                  is_followup: bool, start_time: float) -> Optional[Dict]:
        """
        Dynamic parallel worker pipeline for multi-intent complex queries.

        Flow:
        1. Analyze query complexity (1-5)
        2. Decompose query into sub-tasks if complexity >= 3
        3. Plan each sub-task (sequential LLM calls; fast path used first)
        4. Execute all sub-task plans in parallel (MongoDB I/O bound)
        5. Merge results into a single structured response
        """
        complexity = self._analyze_query_complexity(resolved_query)
        if complexity < 3:
            return None

        sub_queries = self._decompose_query(resolved_query)
        if len(sub_queries) <= 1:
            return None

        worker_count = min(len(sub_queries), 5)
        self._log("Dynamic workers",
                  f"{worker_count} worker(s) | complexity={complexity} | query='{resolved_query[:60]}'")

        chat_context = self.memory.get_context_string()

        # Step 1: Generate plans sequentially (Ollama is single-threaded)
        plans: List[Tuple[str, Dict]] = []
        for sq in sub_queries:
            try:
                matched_rules = keyword_boost_search(sq, top_k=3) if is_index_built() else []
                query_plan = None

                if matched_rules:
                    candidate = plan_query_from_rule(sq, matched_rules[0], chat_context)
                    if candidate and self._is_rule_plan_compatible(sq, candidate):
                        query_plan = candidate

                if not query_plan:
                    query_plan = plan_query(
                        sq, matched_rules, chat_context,
                        format_hint=detect_format_intent(sq),
                    )

                if query_plan:
                    query_plan = self._apply_business_intent_overrides(sq, query_plan)
                    query_plan = self._apply_entity_constraints(sq, query_plan)
                    plans.append((sq, query_plan))
                    self._log("Worker plan ready", f"'{sq[:50]}' -> {query_plan.get('type')}/{query_plan.get('collection', '')}")
                else:
                    self._log("Worker plan failed", f"'{sq[:50]}' — skipping")
            except Exception as e:
                self._log("Worker plan error", f"'{sq[:50]}': {e}")

        if not plans:
            return None

        # Step 2: Execute all plans in parallel (I/O bound — big speedup)
        def _execute_worker(sq_plan: Tuple[str, Dict]) -> Optional[Dict]:
            sq, plan = sq_plan
            try:
                data = execute_query_plan(plan)
                return {
                    "sub_query": sq,
                    "plan": plan,
                    "data": data,
                    "type": plan.get("type"),
                    "collection": plan.get("collection", ""),
                }
            except Exception as exc:
                logger.warning(f"Worker execution error for '{sq}': {exc}")
                return None

        results: List[Dict] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(_execute_worker, p): p[0] for p in plans}
            for future in as_completed(futures):
                sq = futures[future]
                try:
                    result = future.result()
                    if result and not self._is_empty_result(result.get("data")):
                        results.append(result)
                except Exception as exc:
                    self._log("Worker exec failed", f"'{sq[:50]}': {exc}")

        if not results:
            return None

        self._log("Workers completed", f"{len(results)}/{len(plans)} returned data")

        # Step 3: Merge and format
        response = self._merge_worker_results(user_query, results)
        response = mask_response(response)

        primary_collection = results[0]["plan"].get("collection", "") if results else ""
        self.memory.add_exchange(user_query, response, collection=primary_collection)

        return self._build_result(
            response, start_time, format_type="summary", collection=primary_collection
        )

    def _analyze_query_complexity(self, query: str) -> int:
        """
        Rate query complexity 1-5.
        1-2: Simple single-intent (handled by standard pipeline)
        3+: Multi-intent or multi-part (handled by dynamic workers)
        """
        q = query.lower()

        # Explicit multi-part: "X and give me Y and give me Z"
        if re.search(r'\band\s+(?:give\s+me|show\s+me|get\s+me)\b', q):
            return 4

        # Multi-intent single query: count + breakdown + analysis
        intent_count = 0
        if re.search(r'\bhow\s+many\b|\bcount\b|\btotal\s+number\b', q):
            intent_count += 1
        if re.search(
            r'\bstatus\s+(?:wise|vise|count|and\s+count)\b|'
            r'\bby\s+status\b|\bstage\s+(?:wise|vise|count)\b|\bby\s+stage\b|'
            r'\bcount\s+(?:with|also|and)\s+(?:status|stage)\b|'
            r'\b(?:status|stage)\s+and\s+count\b', q
        ):
            intent_count += 1
        if re.search(r'\banalysis\b|\banalyze\b|\binsight\b', q):
            intent_count += 1
        if re.search(r'\btop\s+\d+\b', q) and intent_count > 0:
            intent_count += 1

        if intent_count >= 2:
            return 3 + (intent_count - 2)

        return 1

    def _decompose_query(self, query: str) -> List[str]:
        """
        Break a complex query into parallel sub-tasks.
        Returns list of sub-query strings (2-5 items for complex queries, 1 for simple).
        """
        q_lower = query.lower()

        # Pattern 1: "X and give me Y and give me Z"
        splits = re.split(
            r'\s+and\s+(?:give\s+me|show\s+me|get\s+me)\s+',
            query, flags=re.IGNORECASE
        )
        if len(splits) >= 2:
            entity = self._infer_primary_entity(splits[0])
            result = []
            for part in splits:
                part = part.strip()
                if not part:
                    continue
                has_entity = bool(re.search(
                    r'\b(deal|invoice|company|contact|task|sales|lead|outreach)\b',
                    part, re.IGNORECASE
                ))
                if entity and not has_entity:
                    result.append(f"{entity} - {part}")
                else:
                    result.append(part)
            if len(result) >= 2:
                return result[:5]

        # Pattern 2: Multi-intent single query (count + breakdown + analysis + top-N)
        has_count = bool(re.search(r'\bhow\s+many\b|\bcount\b|\btotal\s+number\b', q_lower))
        has_breakdown = bool(re.search(
            r'\bstatus\s+(?:wise|vise)\b|\bby\s+status\b|\bstage\s+wise\b|\bby\s+stage\b', q_lower
        ))
        has_analysis = bool(re.search(r'\banalysis\b|\banalyze\b|\binsight\b', q_lower))
        has_top = bool(re.search(r'\btop\s+\d+\b', q_lower))

        detected = sum([has_count, has_breakdown, has_analysis, has_top])
        if detected >= 2:
            entity = self._infer_primary_entity(query) or "records"
            parts = []
            if has_count:
                parts.append(f"how many {entity} are there in total")
            if has_breakdown:
                if "status" in q_lower:
                    parts.append(f"give me {entity} breakdown by status with counts")
                elif "stage" in q_lower:
                    parts.append(f"give me {entity} breakdown by stage with counts")
                else:
                    parts.append(f"give me {entity} breakdown by category with counts")
            if has_analysis:
                parts.append(f"analysis and insights for {entity} dataset")
            if has_top:
                m = re.search(r'top\s+(\d+)', q_lower)
                n = m.group(1) if m else "10"
                parts.append(f"top {n} {entity} by value or count")
            if len(parts) >= 2:
                return parts[:5]

        return [query]

    def _infer_primary_entity(self, text: str) -> str:
        """Infer the primary CRM entity from query text."""
        m = re.search(
            r'\b(deals?|invoices?|companies|contacts?|tasks?|sales\s+orders?|leads?|outreaches?|customers?)\b',
            text, re.IGNORECASE
        )
        return m.group(1) if m else ""

    def _merge_worker_results(self, original_query: str,
                               worker_results: List[Dict]) -> str:
        """
        Merge results from multiple parallel workers into a single structured response.
        Always starts with executive summary, then each worker's section.
        """
        if not worker_results:
            return build_not_found_response(original_query, "")

        entity = self._infer_primary_entity(original_query) or "CRM"

        # Find the largest count across workers
        total_scope = 0
        for wr in worker_results:
            d = wr.get("data")
            if isinstance(d, (int, float)):
                total_scope = max(total_scope, int(d))
            elif isinstance(d, list):
                total_scope = max(total_scope, len(d))

        # Executive summary header (3 lines)
        sections = [
            f"Summary: Processed {len(worker_results)} sub-task(s) for your multi-part query about {entity}.",
            f"Scope: {entity} module | Records in largest sub-query: {total_scope}",
            "Complete breakdown of all sub-query results is shown below.\n",
        ]

        # Each worker result as a named section
        for wr in worker_results:
            sub_q = wr.get("sub_query", "").strip()
            data = wr.get("data")

            sections.append(f"### {sub_q.capitalize()}")

            if isinstance(data, (int, float)):
                sections.append(f"Count: **{int(data)}**")
            elif isinstance(data, list) and data:
                if isinstance(data[0], dict) and "_id" in data[0] and len(data[0]) <= 6:
                    # Aggregation breakdown → table
                    sections.append(format_aggregation(data, sub_q))
                else:
                    # Regular list → summary format
                    sections.append(auto_format(data, sub_q, "list"))
            elif isinstance(data, dict):
                sections.append(auto_format(data, sub_q, "summary"))
            else:
                sections.append("No data found for this sub-query.")

            sections.append("")

        return "\n".join(sections)

    # ── Existing Pipeline Methods (preserved) ─────────

    def _fallback_search(self, user_query: str, resolved_query: str,
                         format_hint: str, start_time: float,
                         matched_rules: List[Dict] = None) -> Dict:
        """Fallback when LLM planning fails — keyword search across likely collections."""
        self._log("Fallback", "Trying keyword search...")

        collections = self._collections_from_rules(matched_rules or [])
        if not collections:
            collections = self._guess_collections(resolved_query)
        keyword = self._extract_search_keyword(resolved_query)

        if not keyword and collections:
            safe_coll = collections[0]
            safe_plan = {
                "type": "find",
                "collection": safe_coll,
                "filter": {},
                "sort": [["createdAt", -1]],
                "limit": 10,
            }
            safe_results = execute_query_plan(safe_plan)
            if isinstance(safe_results, list) and safe_results:
                response = auto_format(safe_results, user_query, format_hint)
                response = mask_response(response)
                return self._build_result(
                    response, start_time, format_type=format_hint, collection=safe_coll
                )
            return self._build_result(
                build_not_found_response(user_query, safe_coll),
                start_time, format_type="not_found", collection=safe_coll
            )
        if not keyword:
            return self._build_result(
                build_not_found_response(user_query),
                start_time, format_type="not_found"
            )

        all_results = []
        for coll in collections[:3]:
            results = search_by_keyword(coll, keyword, limit=10)
            if results:
                all_results.extend(results)
                self._log(f"Fallback found", f"{len(results)} in {coll}")
                break

        if not all_results and collections:
            safe_coll = collections[0]
            safe_plan = {
                "type": "find",
                "collection": safe_coll,
                "filter": {},
                "sort": [["createdAt", -1]],
                "limit": 10,
            }
            safe_results = execute_query_plan(safe_plan)
            if isinstance(safe_results, list) and safe_results:
                all_results.extend(safe_results)
                self._log("Fallback default", f"{len(safe_results)} recent rows from {safe_coll}")

        if all_results:
            response = auto_format(all_results, user_query, format_hint)
            response = mask_response(response)
            return self._build_result(response, start_time,
                                      format_type=format_hint,
                                      collection=collections[0] if collections else None)

        return self._build_result(
            build_not_found_response(user_query),
            start_time, format_type="not_found"
        )

    def _collections_from_rules(self, matched_rules: List[Dict]) -> List[str]:
        if not matched_rules:
            return []
        ranked = []
        for rule in matched_rules[:5]:
            score = rule.get("final_score", rule.get("similarity_score", 0))
            for coll in rule.get("collections", []):
                ranked.append((coll, score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        unique = []
        for coll, _ in ranked:
            if coll not in unique:
                unique.append(coll)
        return unique

    def _needs_llm_formatting(self, data: Any, query: str) -> bool:
        """Decide if LLM formatting is needed vs direct formatter."""
        if isinstance(data, (int, float)):
            return False
        if isinstance(data, list):
            if len(data) <= 3 and all(isinstance(d, dict) for d in data):
                return False
            if len(data) > 20:
                return False
        q_lower = query.lower()
        if any(w in q_lower for w in ["compare", "analysis", "trend",
                                       "performance", "vs", "versus"]):
            return True
        return False

    def _llm_format(self, query: str, data: Any, format_hint: str) -> str:
        """Use LLM to format the response for complex analytical queries."""
        from knowledge.prompts import build_response_prompt

        data_str = json.dumps(data, indent=2, default=str)
        if len(data_str) > 4000:
            data_str = data_str[:4000] + "\n... (truncated)"

        prompt = build_response_prompt(query, data_str, format_hint)

        try:
            llm = get_llm()
            response = llm.invoke(prompt)
            return response.content.strip()
        except Exception as e:
            logger.error(f"LLM formatting error: {e}")
            return auto_format(data, query, format_hint)

    def _ensure_executive_summary(self, response: str, data: Any, query: str) -> str:
        """
        Ensure every LLM-formatted response starts with an executive summary.
        Adds a 3-line summary header if not already present.
        """
        summary_starters = (
            "summary:", "answer:", "found ", "total ", "the count",
            "no ", "results:", "based on the data", "here are",
        )
        if any(response.lower().startswith(s) for s in summary_starters):
            return response

        summary_lines = _executive_summary_lines(data, query)
        summary_text = "\n".join(summary_lines)
        return f"{summary_text}\n{response}"

    def _is_greeting(self, query: str) -> bool:
        q = query.lower().strip().rstrip("!?.")
        return q in GREETINGS or q in [
            "hey there", "hi there", "hello there",
            "what's up", "whats up", "sup",
            "good day", "yo"
        ]

    def _is_out_of_scope(self, query: str) -> bool:
        q = query.lower()
        out_of_scope_patterns = [
            r"what is (?:machine learning|ai|python|java|react)",
            r"(?:tell|teach) me about (?:programming|coding|science|history)",
            r"(?:write|create) (?:a|an) (?:essay|poem|story|code|program)",
            r"(?:translate|convert) .+ to .+",
            r"(?:weather|news|stock|crypto|bitcoin)",
            r"(?:recipe|cook|food|restaurant)",
            r"(?:movie|music|song|book|game)",
            r"(?:joke|riddle|puzzle|quiz)",
            r"who (?:is|was) (?:the president|elon musk|bill gates|narendra modi)",
            r"(?:calculate|solve|compute) (?:math|equation|integral|derivative)",
        ]
        if any(re.search(p, q) for p in out_of_scope_patterns):
            return True

        crm_keywords = {
            "deal", "deals", "invoice", "invoices", "company", "companies",
            "contact", "contacts", "task", "tasks", "sales", "order",
            "revenue", "target", "pipeline", "outreach", "prospect",
            "meeting", "vendor", "bill", "product", "lead", "leads",
            "customer", "customers", "client", "clients", "business", "businesses",
            "rep", "owner", "stage", "status",
            "paid", "unpaid", "overdue", "pending", "closed", "won",
            "lost", "open", "active", "inactive", "region", "department",
            "campaign", "technology", "note", "notes", "email",
            "elsner", "ecrm", "crm", "account", "accounts",
        }
        words = set(re.findall(r'\w+', q))
        if len(words) > 4 and not words & crm_keywords:
            data_intent_terms = {
                "show", "list", "give", "find", "get", "count", "total",
                "top", "last", "month", "year", "week", "customer", "customers",
                "owner", "owners", "rep", "reps", "conversion", "performance",
                "business", "businesses",
            }
            if words & data_intent_terms:
                return False
            return True

        return False

    def _guess_collections(self, query: str) -> List[str]:
        # Generic collection ranking using query-token overlap against collection
        # names and sampled schema fields.
        ranked = self._rank_collections_by_query(query)
        return ranked[:5] if ranked else ["companies", "deals", "contacts"]

    def _extract_search_keyword(self, query: str) -> Optional[str]:
        quoted = re.search(r'["\'](.+?)["\']', query)
        if quoted:
            return quoted.group(1).strip()

        entity_match = re.search(
            r'([A-Za-z][\w&.\-\s]{2,60}?)\s+(company|customer|client|invoice|deal|user|contact)s?\b',
            query, re.IGNORECASE
        )
        if entity_match:
            return entity_match.group(1).strip()

        q = re.sub(
            r'^(show|get|find|list|give|tell|what|who|how|which|where)\s+(me\s+)?(all\s+)?(the\s+)?',
            '', query, flags=re.IGNORECASE
        ).strip()

        words = q.split()
        if words:
            return " ".join(words[:4])
        return None

    def _is_low_relevance_result(self, query: str, data: Any) -> bool:
        try:
            q_terms = {
                t for t in re.findall(r"[a-z0-9_]+", query.lower())
                if len(t) > 2 and t not in {
                    "show", "give", "list", "find", "last", "this", "that", "with",
                    "from", "into", "over", "under", "month", "year", "week", "data"
                }
            }
            if not q_terms:
                return False
            sample = data[:3] if isinstance(data, list) else [data]
            hay = json.dumps(sample, default=str).lower()
            matched = sum(1 for t in q_terms if t in hay)
            return (matched / max(1, len(q_terms))) < 0.12
        except Exception:
            return False

    def _should_run_relevance_verifier(self, query: str, data: Any) -> bool:
        if isinstance(data, (int, float)):
            return False
        q = query.lower()
        if any(t in q for t in ["trend", "performance", "conversion", "funnel", "top performing"]):
            return False
        if isinstance(data, list) and len(data) > 5:
            return False
        return self._is_low_relevance_result(query, data)

    def _apply_entity_constraints(self, query: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        try:
            out = dict(plan or {})
            coll = out.get("collection")
            q = query or ""
            q_lower = q.lower()
            date_filter = self._infer_date_filter(q_lower, coll)

            inv_match = re.search(r"\b([A-Za-z]{2,}[A-Za-z0-9/_-]*\d+[A-Za-z0-9/_-]*)\b", q)
            if coll == "invoices" and inv_match and "invoice" in q_lower:
                token = inv_match.group(1)
                filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
                filt.setdefault("invoice_number", {"$regex": token, "$options": "i"})
                out["filter"] = filt
                if re.search(r"\b(first|1st)\b", q_lower) and out.get("type") == "find":
                    out["limit"] = 1
                if date_filter:
                    out = self._merge_date_filter(out, date_filter)
                return out

            if coll == "users":
                user_name = self._extract_person_name(q)
                if user_name and out.get("type") == "find":
                    filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
                    filt.setdefault("name", {"$regex": user_name, "$options": "i"})
                    out["filter"] = filt
                    if any(t in q_lower for t in ["who is", "details", "summary"]):
                        out["limit"] = 1
                    if date_filter:
                        out = self._merge_date_filter(out, date_filter)

            company_phrase = None
            m = re.search(r"([A-Za-z][\w&.\-\s]{2,80}?)\s+company\b", q, re.IGNORECASE)
            if m:
                company_phrase = m.group(1).strip()
            elif " of " in q_lower and any(t in q_lower for t in ["invoices", "deals", "contacts", "sales"]):
                rhs = q.split(" of ", 1)[1].strip()
                company_phrase = rhs[:80]

            if company_phrase and coll in {"invoices", "deals", "sales", "contacts"}:
                company_id = resolve_name_to_id(company_phrase, "company")
                if company_id:
                    key_map = {
                        "invoices": "company",
                        "deals": "company",
                        "sales": "company",
                        "contacts": "company",
                    }
                    field = key_map.get(coll)
                    if out.get("type") == "find":
                        filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
                        filt.setdefault(field, company_id)
                        out["filter"] = filt
                    elif out.get("type") == "aggregate":
                        pipeline = out.get("pipeline") if isinstance(out.get("pipeline"), list) else []
                        if pipeline and isinstance(pipeline[0], dict) and "$match" in pipeline[0]:
                            if isinstance(pipeline[0]["$match"], dict):
                                pipeline[0]["$match"].setdefault(field, company_id)
                        else:
                            pipeline = [{"$match": {field: company_id}}] + pipeline
                        out["pipeline"] = pipeline

            if date_filter:
                out = self._merge_date_filter(out, date_filter)
            return out
        except Exception:
            return plan

    def _apply_business_intent_overrides(self, query: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        q = query.lower()
        out = dict(plan or {})
        date_filter = self._infer_date_filter(q, "invoices")

        if "revenue" in q and ("total" in q or "how much" in q or "summary" in q):
            match_stage = {"payment_status": "paid"}
            if date_filter:
                match_stage.update(date_filter)
            return {
                "type": "aggregate",
                "collection": "invoices",
                "pipeline": [
                    {"$match": match_stage},
                    {"$group": {
                        "_id": None,
                        "total_revenue_usd": {"$sum": "$grandtotal_in_usd"},
                        "invoice_count": {"$sum": 1},
                    }},
                ],
                "response_hint": "summary",
            }

        if "invoice" in q and any(k in q for k in ["list", "invoices list", "show invoices"]):
            if out.get("type") != "find" or out.get("collection") != "invoices":
                out = {
                    "type": "find",
                    "collection": "invoices",
                    "filter": {},
                    "sort": [["invoice_date", -1]],
                    "limit": 10,
                }
            out["response_hint"] = "list"
            if date_filter:
                out = self._merge_date_filter(out, date_filter)
            return out

        return out

    def _infer_date_filter(self, query_lower: str, collection: Optional[str]) -> Optional[Dict[str, Any]]:
        field_map = {
            "invoices": "invoice_date",
            "sales": "sales_date",
            "deals": "createdAt",
            "createtasks": "due_date",
            "contacts": "createdAt",
            "companies": "createdAt",
        }
        field = field_map.get(collection or "", "createdAt")
        now = datetime.now()

        if "last month" in query_lower:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_month_end = month_start - timedelta(microseconds=1)
            last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return {field: {"$gte": last_month_start.isoformat(), "$lte": last_month_end.isoformat()}}

        if "this month" in query_lower:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return {field: {"$gte": start.isoformat(), "$lte": now.isoformat()}}

        rel = re.search(r"\blast\s+(\d+)\s+(day|week|month|year)s?\b", query_lower)
        if rel:
            n = int(rel.group(1))
            unit = rel.group(2)
            delta_map = {
                "day": timedelta(days=n),
                "week": timedelta(weeks=n),
                "month": timedelta(days=30 * n),
                "year": timedelta(days=365 * n),
            }
            start = now - delta_map.get(unit, timedelta(days=30))
            return {field: {"$gte": start.isoformat(), "$lte": now.isoformat()}}

        m = re.search(
            r'(?:from|is|date is|date)\s+([a-z]{3,9})\s+(\d{4})\s+(?:to|till|until)\s+now',
            query_lower
        )
        if m:
            month_name, year = m.group(1), int(m.group(2))
            month_map = {
                "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
                "nov": 11, "november": 11, "dec": 12, "december": 12,
            }
            month = month_map.get(month_name, 1)
            start = datetime(year, month, 1)
            return {field: {"$gte": start.isoformat(), "$lte": now.isoformat()}}

        return None

    def _merge_date_filter(self, plan: Dict[str, Any], date_filter: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(plan)
        if out.get("type") == "find":
            filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
            for k, v in date_filter.items():
                if k not in filt:
                    filt[k] = v
            out["filter"] = filt
        elif out.get("type") == "aggregate":
            pipeline = out.get("pipeline") if isinstance(out.get("pipeline"), list) else []
            if (pipeline and isinstance(pipeline[0], dict)
                    and "$match" in pipeline[0]
                    and isinstance(pipeline[0]["$match"], dict)):
                for k, v in date_filter.items():
                    pipeline[0]["$match"].setdefault(k, v)
            else:
                pipeline = [{"$match": dict(date_filter)}] + pipeline
            out["pipeline"] = pipeline
        elif out.get("type") == "count":
            filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
            for k, v in date_filter.items():
                filt.setdefault(k, v)
            out["filter"] = filt
        return out

    def _pick_numeric_metric_field(self, collection: str) -> Optional[str]:
        """Pick a likely numeric metric field from collection schema sample."""
        samples = get_collection_sample(collection, limit=3)
        fields = set()
        for row in samples:
            if isinstance(row, dict):
                fields.update(row.keys())
        if not fields:
            return None
        priorities = [
            "grand_total_in_usd", "grandtotal_in_usd", "grand_total", "netPayableAmount",
            "subtotal_in_usd", "subtotal", "amount", "value", "targetInUSD",
        ]
        for p in priorities:
            if p in fields:
                return p
        for f in fields:
            lf = f.lower()
            if any(k in lf for k in ["amount", "value", "total", "usd", "revenue"]):
                return f
        return None

    def _build_schema_fallback_plan(self, query: str, prior_plan: Dict[str, Any], format_hint: str) -> Optional[Dict[str, Any]]:
        """
        Build a safe fallback plan from schema + intent when primary plans return empty.
        This is generic and does not depend on specific query templates.
        """
        if not isinstance(prior_plan, dict):
            return None
        collection = prior_plan.get("collection")
        if not collection:
            return None

        category_field = self._infer_category_field(collection, query)
        base_filter = self._infer_base_filter(collection, query.lower())
        date_filter = self._infer_date_filter(query.lower(), collection)
        if date_filter:
            base_filter.update(date_filter)

        response_hint = prior_plan.get("response_hint", format_hint or "auto")
        is_tabular = response_hint == "table" or format_hint == "table" or prior_plan.get("type") == "aggregate"

        if is_tabular:
            metric = self._pick_numeric_metric_field(collection)
            group = {"_id": f"${category_field}", "count": {"$sum": 1}}
            project = {"_id": 1, "count": 1}
            sort_field = "count"
            if metric:
                group["total_value"] = {"$sum": f"${metric}"}
                group["avg_value"] = {"$avg": f"${metric}"}
                project["total_value"] = 1
                project["avg_value"] = 1
                sort_field = "total_value"
            return {
                "type": "aggregate",
                "collection": collection,
                "pipeline": [
                    {"$match": base_filter},
                    {"$group": group},
                    {"$project": project},
                    {"$sort": {sort_field: -1}},
                    {"$limit": 20},
                ],
                "response_hint": "table" if format_hint == "table" else "summary",
            }

        return {
            "type": "find",
            "collection": collection,
            "filter": base_filter,
            "sort": [["createdAt", -1]],
            "limit": 20,
            "response_hint": "list",
        }

    def _extract_person_name(self, query: str) -> Optional[str]:
        patterns = [
            r'^\s*who\s+is\s+([a-z][a-z\s]{1,40})',
            r'\bof\s+([a-z][a-z\s]{1,40})\b',
            r'\bfor\s+([a-z][a-z\s]{1,40})\b',
        ]
        for p in patterns:
            m = re.search(p, query, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                name = re.sub(
                    r'\b(give|show|get|details|summary|his|her|in|with|and)\b.*$',
                    '', name, flags=re.IGNORECASE,
                ).strip()
                if name:
                    return name
        return None

    def _is_rule_plan_compatible(self, query: str, plan: Dict[str, Any]) -> bool:
        coll = str(plan.get("collection", "")).lower()
        ranked = self._rank_collections_by_query(query)
        if not ranked:
            return True
        return coll in ranked[:2]

    def _get_collection_tokens(self, collection: str) -> set:
        """Token set from collection name + sampled schema keys."""
        if collection in self._collection_token_cache:
            return self._collection_token_cache[collection]
        tokens = set(re.findall(r"[a-z0-9_]+", collection.lower()))
        try:
            samples = get_collection_sample(collection, limit=3)
            for row in samples:
                if not isinstance(row, dict):
                    continue
                for key in row.keys():
                    tokens.update(re.findall(r"[a-z0-9_]+", str(key).lower()))
        except Exception:
            pass
        self._collection_token_cache[collection] = tokens
        return tokens

    def _rank_collections_by_query(self, query: str) -> List[str]:
        """Rank collections by semantic token overlap with query text."""
        q_tokens = set(re.findall(r"[a-z0-9_]+", (query or "").lower()))
        if not q_tokens:
            return []
        q_stems = {t[:-1] if t.endswith("s") else t for t in q_tokens}
        candidates = [
            c for c in COLLECTION_NAMES
            if c in {"deals", "invoices", "companies", "contacts", "createtasks",
                     "users", "sales", "targets", "outreaches", "bills",
                     "meetings", "products", "departments", "regions", "sources", "technologies"}
        ]
        scored = []
        for coll in candidates:
            c_tokens = self._get_collection_tokens(coll)
            if not c_tokens:
                continue
            overlap = len(q_tokens & c_tokens)
            coll_stem = coll[:-1] if coll.endswith("s") else coll
            name_match_boost = 0
            if coll in q_tokens:
                name_match_boost += 4
            if coll_stem in q_stems:
                name_match_boost += 3
            score = overlap + name_match_boost
            if score > 0:
                scored.append((coll, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in scored]

    def _align_plan_with_collection_intent(self, query: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generic alignment step: if plan collection is clearly off-intent,
        switch to top-ranked collection for safe/simple plan types.
        """
        try:
            out = dict(plan or {})
            p_type = out.get("type")
            coll = out.get("collection")
            if p_type not in {"find", "count"} or not coll:
                return out
            ranked = self._rank_collections_by_query(query)
            if not ranked:
                return out
            if coll not in ranked[:2]:
                aligned = ranked[0]
                self._log("Plan collection aligned", f"{coll} -> {aligned}")
                out["collection"] = aligned
                # Reset potentially incompatible shape constraints from old collection.
                if out.get("type") == "find":
                    out.pop("projection", None)
                    out["sort"] = [["createdAt", -1]]
                    out["limit"] = min(int(out.get("limit", 20) or 20), 20)
            return out
        except Exception:
            return plan

    def _build_result(self, response: str, start_time: float,
                      format_type: str = "auto", entity: dict = None,
                      collection: str = None, error: str = None) -> Dict:
        elapsed = time.time() - start_time
        self._log("Total time", f"{elapsed:.2f}s")
        if elapsed > MAX_RESPONSE_TIME:
            logger.warning(f"Response time exceeded limit: {elapsed:.2f}s > {MAX_RESPONSE_TIME}s")
        record_event("chat_request", {
            "duration_s": round(elapsed, 3),
            "format": format_type,
            "collection": collection,
            "has_error": bool(error),
            "is_slow": bool(elapsed > MAX_RESPONSE_TIME),
        })
        result = {
            "response": response,
            "logs": self.logs.copy(),
            "entity": entity,
            "format": format_type,
            "timing": round(elapsed, 2),
            "error": error,
            "collection": collection,
        }
        # Cache only successful, non-empty responses.
        if (
            not error
            and response
            and format_type not in {"greeting", "out_of_scope"}
            and self._is_cacheable_response(response)
            and self._current_query
        ):
            self.cache.set(self._current_query, result)
        return result

    def _is_cacheable_response(self, response: str) -> bool:
        """Avoid caching low-signal placeholder responses."""
        txt = (response or "").strip()
        if not txt:
            return False
        if "No results found" in txt or "I could not find any results" in txt:
            return False
        # Placeholder-heavy lists are usually weak semantic matches.
        if len(re.findall(r"^\d+\.\s+Record\b", txt, flags=re.MULTILINE)) >= 3:
            return False
        return True

    # ── Parallel Intent Pipeline (preserved) ──────────

    def _parallel_intent_pipeline(self, user_query: str, resolved_query: str,
                                   is_followup: bool, start_time: float,
                                   format_hint: str = "auto") -> Optional[Dict]:
        """
        Parallel worker path for composite analytical intents.
        Triggers on: analysis, category, breakdown, status-count, or table+count queries.
        """
        q = resolved_query.lower()

        is_analysis_ask = bool(re.search(r"\b(analysis|analyze|insight|summary|breakdown|distribution)\b", q))
        is_count_ask = bool(re.search(r"\b(count|counts|how many|total number)\b", q))
        is_table_count_request = format_hint == "table" and is_count_ask

        is_followup_summary = is_followup and any(
            k in q for k in [
                "summary of this", "summary of that", "with categories",
                "categories of it", "categories of this",
                "breakdown of it", "breakdown of this",
            ]
        )

        domain_hit = any(k in q for k in [
            "deal", "deals", "invoice", "invoices", "task", "tasks",
            "company", "companies", "contact", "contacts",
        ])

        triggers = is_analysis_ask or is_table_count_request
        if not ((domain_hit and triggers) or is_followup_summary):
            return None

        collection = self.memory.get_last_collection() if is_followup_summary else None
        if not collection:
            guessed = self._guess_collections(resolved_query)
            collection = guessed[0] if guessed else "deals"

        category_field = self._infer_category_field(collection, q)
        base_filter = self._infer_base_filter(collection, q)
        self._log("Parallel pipeline",
                  f"collection={collection}, category_field={category_field}")

        def worker_total_count() -> Tuple[str, Any]:
            plan = {"type": "count", "collection": collection, "filter": dict(base_filter)}
            return "count", execute_query_plan(plan)

        def worker_category_breakdown() -> Tuple[str, Any]:
            pipeline = [
                {"$match": dict(base_filter)},
                {"$group": {"_id": f"${category_field}", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 10},
            ]
            plan = {"type": "aggregate", "collection": collection, "pipeline": pipeline}
            return "categories", execute_query_plan(plan)

        def worker_sample_rows() -> Tuple[str, Any]:
            plan = {
                "type": "find",
                "collection": collection,
                "filter": dict(base_filter),
                "sort": [["createdAt", -1]],
                "limit": 20,
            }
            return "sample", execute_query_plan(plan)

        outputs: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_map = {
                executor.submit(worker_total_count): "count",
                executor.submit(worker_category_breakdown): "categories",
                executor.submit(worker_sample_rows): "sample",
            }
            for fut in as_completed(future_map):
                key = future_map[fut]
                try:
                    result_key, value = fut.result()
                    outputs[result_key] = value
                except Exception as exc:
                    self._log("Parallel worker error", f"{key}: {exc}")
                    outputs[key] = None

        count_val = outputs.get("count", 0)
        categories = outputs.get("categories") or []
        sample = outputs.get("sample") or []
        if count_val in (None, "") and not categories and not sample:
            return None

        response = self._build_parallel_summary_response(
            user_query=user_query,
            collection=collection,
            total_count=int(count_val or 0),
            categories=categories if isinstance(categories, list) else [],
            sample=sample if isinstance(sample, list) else [],
            format_hint=format_hint,
        )

        entity = {"type": collection.rstrip("s"), "name": collection}
        self.memory.add_exchange(user_query, response, entity=entity, collection=collection)
        return self._build_result(
            response,
            start_time,
            format_type=("table" if format_hint == "table" else "summary"),
            entity=entity,
            collection=collection,
        )

    def _person_lookup_pipeline(self, user_query: str, resolved_query: str,
                                 start_time: float) -> Optional[Dict]:
        q = resolved_query.lower()
        if not re.search(r'^\s*who\s+is\s+[a-z]', q):
            return None
        name = self._extract_person_name(resolved_query)
        if not name:
            return None

        self._log("Person lookup", f"name={name}")
        users = execute_query_plan({
            "type": "find",
            "collection": "users",
            "filter": {"name": {"$regex": name, "$options": "i"}},
            "sort": [["name", 1]],
            "limit": 1,
        })
        if isinstance(users, list) and users:
            u = users[0]
            response = self._format_person_summary(u)
            entity = {"type": "user", "name": u.get("name", name)}
            self.memory.add_exchange(user_query, response, entity=entity, collection="users")
            return self._build_result(response, start_time, format_type="summary",
                                      entity=entity, collection="users")

        contacts = execute_query_plan({
            "type": "find",
            "collection": "contacts",
            "filter": {
                "$or": [
                    {"firstName": {"$regex": name, "$options": "i"}},
                    {"lastName": {"$regex": name, "$options": "i"}},
                ]
            },
            "sort": [["createdAt", -1]],
            "limit": 1,
        })
        if isinstance(contacts, list) and contacts:
            c = contacts[0]
            response = (
                f"Found contact: {c.get('firstName', '')} {c.get('lastName', '')}".strip() + "\n"
                f"Email: {c.get('email', 'N/A')}\n"
                f"Phone: {c.get('phoneNumber', 'N/A')}"
            )
            entity = {
                "type": "contact",
                "name": (c.get("firstName", "") + " " + c.get("lastName", "")).strip(),
            }
            self.memory.add_exchange(user_query, response, entity=entity, collection="contacts")
            return self._build_result(response, start_time, format_type="summary",
                                      entity=entity, collection="contacts")

        response = f"I could not find a user or contact named '{name}'."
        return self._build_result(response, start_time, format_type="not_found",
                                  collection="users")

    def _format_person_summary(self, user_doc: Dict[str, Any]) -> str:
        name = user_doc.get("name", "Unknown")
        email = user_doc.get("email", "N/A")
        role_parts = []
        if user_doc.get("isSuperAdmin"):
            role_parts.append("Super Admin")
        if user_doc.get("isAdmin"):
            role_parts.append("Admin")
        if user_doc.get("isRegionHead"):
            role_parts.append("Region Head")
        role = ", ".join(role_parts) if role_parts else "Team Member"
        active = "Active" if user_doc.get("isActive") else "Inactive"
        last_login = user_doc.get("lastLogin", "N/A")
        return (
            f"{name} is a {role} in the CRM team.\n"
            f"Status: {active} | Email: {email}\n"
            f"Last login: {last_login}\n"
            "Ask 'show full details' if you want the complete profile."
        )

    def _infer_category_field(self, collection: str, query: str) -> str:
        """
        Infer grouping field dynamically from query wording + collection sample schema.
        Avoid brittle collection-specific hardcoding so new query styles still work.
        """
        query_tokens = set(re.findall(r"[a-zA-Z_]+", query.lower()))
        samples = get_collection_sample(collection, limit=3)
        field_names = set()
        for row in samples:
            if isinstance(row, dict):
                field_names.update(row.keys())

        # Drop obviously unsuitable fields for category grouping.
        ignored = {
            "_id", "__v", "createdAt", "updatedAt", "deletedAt",
            "deleted", "isDeleted", "lineItems", "items", "notes",
        }
        candidates = [f for f in field_names if f not in ignored and not f.endswith("Id")]

        # Respect explicit grouping words in the question.
        if "status" in query_tokens:
            for f in candidates:
                if "status" in f.lower():
                    return f
            if "stage" in field_names:
                return "stage"
        if "stage" in query_tokens:
            for f in candidates:
                if "stage" in f.lower():
                    return f

        # Prefer fields directly referenced by query tokens, scored for categorical suitability.
        def _field_score(field: str) -> int:
            f_lower = field.lower()
            parts = set(re.findall(r"[a-z]+", f_lower))
            overlap = len(query_tokens.intersection(parts | {f_lower}))
            score = overlap
            semantic_keys = ["status", "stage", "type", "owner", "priority", "region", "source"]
            if any((k in f_lower) and (k in query_tokens) for k in semantic_keys):
                score += 2
            if any(k in f_lower for k in ["date", "time", "total", "amount", "usd", "number", "subtotal"]):
                score -= 2
            return score

        scored = sorted(((f, _field_score(f)) for f in candidates), key=lambda x: x[1], reverse=True)
        if scored and scored[0][1] > 0:
            return scored[0][0]

        # Fall back to common categorical fields only if present in collection schema.
        priority = [
            "status", "stage", "leadStatus", "lifecycleStage", "payment_status",
            "priority", "type", "owner", "companyOwner", "contactOwner",
            "source", "region", "department",
        ]
        for p in priority:
            if p in field_names:
                return p

        # Final fallback for unknown collections.
        return "status"

    def _infer_base_filter(self, collection: str, query: str) -> Dict[str, Any]:
        filt: Dict[str, Any] = {}
        now_iso = datetime.now().isoformat()
        if collection == "deals":
            if "closed won" in query:
                filt["dealWonAt"] = {"$ne": None}
                filt["dealLostAt"] = None
            elif "closed lost" in query or "lost deals" in query:
                filt["dealLostAt"] = {"$ne": None}
            elif "open deals" in query:
                filt["dealWonAt"] = None
                filt["dealLostAt"] = None
        elif collection == "invoices":
            if "pending" in query:
                filt["payment_status"] = {"$in": ["draft", "confirmed", "partial_payment"]}
            elif "paid" in query:
                filt["payment_status"] = "paid"
            if "overdue" in query:
                filt["due_date"] = {"$lt": now_iso}
        elif collection == "createtasks":
            if "pending" in query:
                filt["status"] = {"$in": ["Pending", "pending", "Open", "open"]}
            if "overdue" in query:
                filt["due_date"] = {"$lt": now_iso}
        return filt

    def _build_parallel_summary_response(self, user_query: str, collection: str,
                                          total_count: int, categories: List[Dict],
                                          sample: List[Dict],
                                          format_hint: str = "summary") -> str:
        q = user_query.lower()
        category_field = _humanize_field(self._infer_category_field(collection, q))

        if format_hint == "table":
            lines = [
                f"Total {collection}: {total_count}",
                "",
                f"| {category_field} | Count |",
                "|---|---:|",
            ]
            if categories:
                for row in categories[:20]:
                    bucket = row.get("_id")
                    label = "Unknown" if bucket in (None, "", []) else str(bucket)
                    lines.append(f"| {label} | {int(row.get('count', 0))} |")
            else:
                lines.append("| No data | 0 |")
            return "\n".join(lines)

        if ("categories" in q or "breakdown" in q) and "analysis" not in q and "summary" not in q:
            lines = [f"Category breakdown by {category_field}:"]
            if categories:
                for row in categories[:10]:
                    lines.append(f"- {row.get('_id', 'Unknown')}: {row.get('count', 0)}")
            else:
                lines.append("- No category breakdown available.")
            return "\n".join(lines)

        lines = [
            f"Summary: Found {total_count} {collection} record(s) matching your query.",
            f"Filtered from the ECRM {collection} module per your request.",
            f"Category breakdown and analysis shown below.\n",
            f"Total {collection}: {total_count}",
            f"Category breakdown by {category_field}:",
        ]
        if categories:
            for row in categories[:6]:
                lines.append(f"- {row.get('_id', 'Unknown')}: {row.get('count', 0)}")
        else:
            lines.append("- No category breakdown available.")
        lines.append("Quick analysis:")
        if categories:
            top = categories[0]
            lines.append(
                f"- Highest volume bucket is '{top.get('_id', 'Unknown')}' "
                f"with {top.get('count', 0)} records."
            )
        if total_count == 0:
            lines.append("- No matching records for the current filter.")
        else:
            lines.append(f"- Based on your intent with {total_count} matching records.")
        if sample:
            lines.append(f"- Recent sample size checked: {len(sample[:5])}")
        return "\n".join(lines)

    def _is_empty_result(self, data: Any) -> bool:
        if data is None:
            return True
        if isinstance(data, list):
            return len(data) == 0
        if isinstance(data, dict):
            return len(data) == 0
        if isinstance(data, (int, float)):
            return False
        return False

    def _estimate_result_count(self, data: Any) -> int:
        if data is None:
            return 0
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return 1 if data else 0
        if isinstance(data, (int, float)):
            return int(data)
        return 1

    def _log(self, step: str, detail: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {step}: {detail}"
        self.logs.append(entry)
        logger.info(f"{step}: {detail}")

    def get_memory_state(self) -> Dict:
        return self.memory.to_dict()

    def restore_memory(self, state: Dict):
        self.memory.from_dict(state)

    def clear_memory(self):
        self.memory.clear()


def _humanize_field(field: str) -> str:
    return field.replace("_", " ").title()
