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
from knowledge.query_understander import understand as understand_query, QueryIntent
from knowledge.relationship_graph import get_graph
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

            # Step 2.1: Deep query understanding — extract intent, limit, sort, entities
            qparams = understand_query(resolved_query)
            self._log(
                "Query understood",
                f"intent={qparams.intent.value} limit={qparams.limit} "
                f"sort={qparams.sort_reason} entities={qparams.entity_names}",
            )
            record_event("query_understood", {
                "intent": qparams.intent.value,
                "limit": qparams.limit,
                "sort_reason": qparams.sort_reason,
                "collection_hints": qparams.collection_hints[:3],
            })

            # Step 2.1b: Context matching
            if not is_followup and self.memory.short_term:
                is_related, ctx_hint = self.memory.is_contextually_related(resolved_query)
                self._log(
                    "Context match",
                    "Query is related to previous conversation context"
                    if is_related else "New topic — using fresh context",
                )

            # Step 2.25: Fast person lookup for "who is <name>" queries
            person_result = self._person_lookup_pipeline(user_query, resolved_query, start_time)
            if person_result is not None:
                return person_result

            # Step 2.28: Owner-names lookup ("give me owner names of X")
            owner_result = self._owner_names_pipeline(user_query, resolved_query, start_time)
            if owner_result is not None:
                return owner_result

            # Step 2.29: Company-linked data pipeline ("Company X [collection] [fields]")
            company_linked_result = self._company_linked_query_pipeline(
                user_query, resolved_query, qparams, start_time
            )
            if company_linked_result is not None:
                return company_linked_result

            # Step 2.26: Existence query pipeline ("any Pankaj contact available?")
            if qparams.intent == QueryIntent.EXISTENCE and qparams.existence_target:
                exist_result = self._existence_pipeline(
                    user_query, resolved_query, qparams, start_time
                )
                if exist_result is not None:
                    return exist_result

            # Step 2.27: Comparison / percentage pipeline
            if qparams.intent in (QueryIntent.PERCENTAGE, QueryIntent.COMPARISON):
                cmp_result = self._comparison_pipeline(
                    user_query, resolved_query, qparams, start_time
                )
                if cmp_result is not None:
                    return cmp_result

            # Step 2.3: Hard meetings routing pipeline
            meetings_result = self._meetings_query_pipeline(user_query, resolved_query, start_time)
            if meetings_result is not None:
                return meetings_result

            # Step 2.4: Hard linked-query pipeline (parent -> child ID filtering)
            linked_result = self._linked_query_pipeline(user_query, resolved_query, start_time)
            if linked_result is not None:
                return linked_result

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
                        # Apply sort/limit from query understander on fast-path too
                        if qparams.limit:
                            fast_plan["limit"] = qparams.limit
                        if qparams.sort_field and qparams.sort_reason in ("low_value", "high_value"):
                            fast_plan = self._apply_understander_sort(
                                fast_plan, qparams.sort_field, qparams.sort_direction, qparams.sort_reason
                            )
                        data = execute_query_plan(fast_plan)
                        if not self._is_empty_result(data):
                            result_count = self._estimate_result_count(data)
                            self._log("Fast-path result", f"{result_count} records")
                            response = auto_format(
                                data,
                                user_query,
                                fast_plan.get("response_hint", format_hint),
                                response_meta=self._build_response_meta(fast_plan, user_query, data),
                            )
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
                        f"margin={confidence['margin']:.3f} "
                        f"gate={confidence.get('gate', 'unknown')}"
                    )
                    if confidence.get("gate") == "high_score":
                        self._log("Retrieval gate", "Top score >= 0.75 — rule-compiled planning preferred")
                    elif confidence.get("gate") == "margin_gate":
                        self._log("Retrieval gate", "Moderate confidence — rule accepted via margin gate")
                    else:
                        self._log("Retrieval gate", "Below confidence gate — LLM planning allowed")
                    record_event("retrieval_confidence", {
                        "query": resolved_query[:120],
                        "is_confident": confidence["is_confident"],
                        "top_score": confidence["top_score"],
                        "margin": confidence["margin"],
                        "gate": confidence.get("gate"),
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

            # Step 5b: LLM plan generation (with relationship + sort/limit context)
            if not query_plan:
                self._log("Planning query", "Sending to LLM...")
                # Build relationship context for LLM
                rel_ctx = get_graph().prompt_context(
                    qparams.collection_hints or self._guess_collections(resolved_query)
                )
                # Build sort hint string for prompt
                sort_hint_str = ""
                if qparams.sort_field and qparams.sort_reason:
                    dir_word = "ascending (smallest first)" if qparams.sort_direction == 1 else "descending (largest first)"
                    sort_hint_str = (
                        f"Sort by '{qparams.sort_field}' field {dir_word} "
                        f"(reason: user asked for '{qparams.sort_reason}' values)."
                    )
                query_plan = plan_query(
                    user_query, matched_rules, chat_context,
                    resolved_query if is_followup else None,
                    format_hint=format_hint,
                    relationship_context=rel_ctx,
                    sort_hint=sort_hint_str,
                    limit_override=qparams.limit,
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
            query_plan = self._apply_temporal_sort_policy(resolved_query, query_plan)
            # Apply dynamic limit from query understander (overrides LLM default)
            if qparams.limit:
                if query_plan.get("type") == "find":
                    query_plan["limit"] = qparams.limit
                elif query_plan.get("type") == "aggregate":
                    pipeline = query_plan.get("pipeline", [])
                    pipeline = [s for s in pipeline if "$limit" not in s]
                    pipeline.append({"$limit": qparams.limit})
                    query_plan["pipeline"] = pipeline
                self._log("Dynamic limit", f"Applied user-specified limit={qparams.limit}")

            # Apply sort direction from query understander (overrides LLM sort for low/high queries)
            if qparams.sort_field and qparams.sort_reason in ("low_value", "high_value"):
                query_plan = self._apply_understander_sort(
                    query_plan, qparams.sort_field, qparams.sort_direction,
                    qparams.sort_reason
                )

            # Step 6: Execute query
            self._log("Executing query", "...")
            data = execute_query_plan(query_plan)
            result_count = self._estimate_result_count(data)
            self._log("Query result", f"{result_count} records")

            # Specific entity lookup hard fallback chain (Patch 2 hard)
            if self._is_empty_result(data):
                exact_result = self._exact_lookup_zero_result_chain(resolved_query, query_plan, start_time)
                if exact_result is not None:
                    return exact_result

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

            # Step 7: Empty result handling — max 1 LLM replan to prevent infinite loops
            _replan_attempts = 0
            if self._is_empty_result(data):
                time_ok = (time.time() - start_time) < max(15, MAX_RESPONSE_TIME * 0.65)
                if matched_rules and time_ok and _replan_attempts < 1:
                    _replan_attempts += 1
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
                    self._log("Replan skipped", "Time budget or retry limit reached")

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
                date_retry_data, date_retry_plan = self._retry_with_date_field_aliases(
                    resolved_query, query_plan
                )
                if not self._is_empty_result(date_retry_data):
                    self._log("Date-field retry", "Recovered results using alternate date field")
                    data = date_retry_data
                    query_plan = date_retry_plan

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
                response = self._ensure_executive_summary(
                    response,
                    data,
                    user_query,
                    response_meta=self._build_response_meta(query_plan, user_query, data),
                )
            else:
                response = auto_format(
                    data,
                    user_query,
                    response_hint,
                    response_meta=self._build_response_meta(query_plan, user_query, data),
                )

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

    # ── Existence Pipeline ────────────────────────────

    def _existence_pipeline(
        self, user_query: str, resolved_query: str, qparams: Any, start_time: float
    ) -> Optional[Dict]:
        """
        Handle YES/NO existence queries: "any Pankaj contact available?"
        Searches only for exactly what was asked — returns minimal relevant result.
        """
        target = qparams.existence_target
        etype = qparams.existence_entity_type or ""
        self._log("Existence query", f"target='{target}' type='{etype}'")

        # Determine collection from entity type hint
        etype_map = {
            "contact": "contacts", "contacts": "contacts",
            "company": "companies", "companies": "companies",
            "deal": "deals", "deals": "deals",
            "invoice": "invoices", "invoices": "invoices",
            "user": "users", "users": "users",
            "lead": "outreaches", "outreach": "outreaches",
            "task": "createtasks", "tasks": "createtasks",
        }
        collection = etype_map.get(etype.lower(), "")

        # If no entity type, guess from collection hints
        if not collection and qparams.collection_hints:
            collection = qparams.collection_hints[0]
        if not collection:
            guessed = self._guess_collections(resolved_query)
            collection = guessed[0] if guessed else "contacts"

        # Build search filter — regex on likely name fields
        name_fields_map = {
            "contacts": [
                {"$or": [
                    {"firstName": {"$regex": target, "$options": "i"}},
                    {"lastName": {"$regex": target, "$options": "i"}},
                ]},
            ],
            "companies": [{"companyName": {"$regex": target, "$options": "i"}}],
            "deals": [{"name": {"$regex": target, "$options": "i"}}],
            "users": [{"name": {"$regex": target, "$options": "i"}}],
            "invoices": [{"invoice_number": {"$regex": target, "$options": "i"}}],
            "createtasks": [{"Task": {"$regex": target, "$options": "i"}}],
        }
        filters = name_fields_map.get(collection, [{"name": {"$regex": target, "$options": "i"}}])

        for filt in filters:
            results = execute_query_plan({
                "type": "find",
                "collection": collection,
                "filter": filt,
                "limit": 3,
                "sort": [["createdAt", -1]],
            })
            if isinstance(results, list) and results:
                count = len(results)
                from utils.formatter import auto_format
                data_response = auto_format(
                    results, user_query, "detail",
                    response_meta=self._build_response_meta(
                        {"type": "find", "collection": collection, "filter": filt},
                        user_query, results,
                    ),
                )
                response = (
                    f"Yes, found {count} record(s) matching '{target}' in {collection}.\n"
                    f"{data_response}"
                )
                response = mask_response(response)
                self.memory.add_exchange(user_query, response, collection=collection)
                return self._build_result(response, start_time, format_type="detail",
                                          collection=collection)

        # Not found
        response = (
            f"No, '{target}' was not found in {collection}.\n"
            f"The CRM has no {etype or 'record'} matching this name.\n"
            f"Try checking spelling or searching in a different module."
        )
        return self._build_result(response, start_time, format_type="not_found",
                                  collection=collection)

    # ── Comparison / Percentage Pipeline ─────────────

    def _comparison_pipeline(
        self, user_query: str, resolved_query: str, qparams: Any, start_time: float
    ) -> Optional[Dict]:
        """
        Handle percentage / comparison queries:
        "how many % difference between Closed Won & Closed Lost?"

        Strategy:
        1. Detect the dimension field (stage, status, payment_status, priority…)
        2. Run a single $group aggregate to get counts for ALL categories
        3. Filter results to the compared entities
        4. Compute and render percentages
        """
        entities = qparams.compare_entities
        if not entities or len(entities) < 2:
            return None

        # Detect "all X" as a total-denominator signal (not a group to match)
        # e.g. "all deals" means total count; "closed lost" is the specific group
        _ALL_TOKENS = {"all", "total", "every", "entire", "complete"}
        total_override_entity = None
        filtered_entities = []
        for ent in entities:
            first_word = ent.strip().split()[0].lower() if ent.strip() else ""
            if first_word in _ALL_TOKENS:
                total_override_entity = ent  # this one IS the total
            else:
                filtered_entities.append(ent)

        # Determine collection
        collection = (qparams.collection_hints[0]
                      if qparams.collection_hints else
                      self._guess_collections(resolved_query)[0]
                      if self._guess_collections(resolved_query) else "deals")

        self._log("Comparison pipeline", f"entities={entities} collection={collection}")

        # Get total count for denominator
        total_all_count = execute_query_plan({
            "type": "count", "collection": collection, "filter": {}
        })
        total_all = int(total_all_count) if isinstance(total_all_count, (int, float)) else 0

        # Infer grouping field from the entities or collection
        work_entities = filtered_entities if total_override_entity else entities
        group_field = self._infer_comparison_field(work_entities, collection)
        self._log("Comparison field", group_field)

        # Aggregate: count all groups
        agg_result = execute_query_plan({
            "type": "aggregate",
            "collection": collection,
            "pipeline": [
                {"$group": {"_id": f"${group_field}", "count": {"$sum": 1}}},
            ],
        })

        if not isinstance(agg_result, list) or not agg_result:
            return None

        # Build lookup dict: {group_label_lower → count}
        group_counts: Dict[str, int] = {}
        agg_total = 0
        for row in agg_result:
            label = str(row.get("_id") or "Unknown").lower()
            cnt = int(row.get("count", 0))
            group_counts[label] = cnt
            agg_total += cnt

        if total_all == 0:
            total_all = agg_total
        if total_all == 0:
            return None

        # If user asked "all X vs Y" — include total as first entry
        if total_override_entity:
            matched: Dict[str, int] = {total_override_entity: total_all}
            for ent in filtered_entities:
                ent_lower = ent.lower().strip()
                if ent_lower in group_counts:
                    matched[ent] = group_counts[ent_lower]
                else:
                    for label, cnt in group_counts.items():
                        if ent_lower in label or label in ent_lower:
                            matched[ent] = matched.get(ent, 0) + cnt
        else:
            matched = {}
            for ent in entities:
                ent_lower = ent.lower().strip()
                if ent_lower in group_counts:
                    matched[ent] = group_counts[ent_lower]
                    continue
                for label, cnt in group_counts.items():
                    if ent_lower in label or label in ent_lower:
                        matched[ent] = matched.get(ent, 0) + cnt

        if len(matched) < 2:
            # If only 1 matched (specific group requested vs total), still show
            if total_override_entity and len(matched) >= 1:
                pass  # show what we have
            else:
                return None

        # Build response
        lines = [
            f"Found {len(matched)} groups to compare | "
            f"Field: {group_field} | Collection: {collection}",
            f"Query echo -> Entities: {' vs '.join(entities)} | Total records: {total_all}",
            f"Comparison Results:",
            "",
        ]
        pcts: List[float] = []
        for ent, cnt in matched.items():
            pct = (cnt / total_all) * 100
            pcts.append(pct)
            lines.append(f"  {ent}: {cnt} record(s) ({pct:.1f}%)")

        if len(pcts) == 2:
            diff = abs(pcts[0] - pcts[1])
            lines.append(f"\nDifference: {diff:.1f}%")

        lines.append(f"\nTotal {collection}: {total_all}")
        lines.append("Ask for a detailed breakdown to see all categories.")

        response = "\n".join(lines)
        response = mask_response(response)
        self.memory.add_exchange(user_query, response, collection=collection)
        return self._build_result(response, start_time, format_type="summary",
                                  collection=collection)

    def _infer_comparison_field(self, entities: List[str], collection: str) -> str:
        """
        Infer the MongoDB field to group by for a comparison query.
        Driven entirely by the entity names and collection — no hardcoding.
        """
        entities_lower = " ".join(e.lower() for e in entities)

        # Keyword signals → field name
        signals = [
            (["won", "lost", "closed", "open", "negotiation", "proposal", "stage"], "stage"),
            (["paid", "draft", "confirmed", "cancelled", "partial", "payment"], "payment_status"),
            (["pending", "completed", "open", "in progress", "status"], "status"),
            (["high", "medium", "low", "priority"], "priority"),
            (["active", "inactive", "lifecycle", "lead status"], "leadStatus"),
            (["region", "territory", "usa", "apac", "emea"], "region"),
            (["source", "organic", "email", "campaign"], "source"),
        ]
        for keywords, field in signals:
            if any(kw in entities_lower for kw in keywords):
                return field

        # Fall back to schema sample
        from tools.mongodb_tool import get_collection_sample
        samples = get_collection_sample(collection, limit=3)
        for row in samples:
            if isinstance(row, dict):
                for candidate in ["stage", "status", "payment_status", "priority",
                                   "leadStatus", "lifecycleStage"]:
                    if candidate in row:
                        return candidate
        return "status"

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
                response = auto_format(
                    safe_results,
                    user_query,
                    format_hint,
                    response_meta=self._build_response_meta(safe_plan, user_query, safe_results),
                )
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
            response = auto_format(
                all_results,
                user_query,
                format_hint,
                response_meta=self._build_response_meta(
                    {"collection": collections[0] if collections else ""},
                    user_query,
                    all_results,
                ),
            )
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

    def _ensure_executive_summary(self, response: str, data: Any, query: str,
                                  response_meta: Optional[Dict[str, Any]] = None) -> str:
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

        summary_lines = _executive_summary_lines(data, query, response_meta=response_meta)
        summary_text = "\n".join(summary_lines)
        return f"{summary_text}\n{response}"

    def _build_response_meta(self, plan: Optional[Dict[str, Any]], query: str,
                             data: Any) -> Dict[str, Any]:
        """Build formatter metadata for mandatory query/filter/count echo."""
        plan = plan or {}
        collection = plan.get("collection", "")
        entity = plan.get("entity")
        if isinstance(entity, dict):
            entity = entity.get("type") or entity.get("name")
        if not entity and collection:
            entity = collection[:-1] if collection.endswith("s") else collection

        filter_obj: Any = plan.get("filter")
        if (filter_obj is None or filter_obj == {}) and plan.get("type") == "aggregate":
            pipeline = plan.get("pipeline") if isinstance(plan.get("pipeline"), list) else []
            for stage in pipeline:
                if isinstance(stage, dict) and "$match" in stage and isinstance(stage["$match"], dict):
                    filter_obj = stage["$match"]
                    break

        return {
            "entity": entity,
            "collection": collection,
            "filter": filter_obj or {},
        }

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
                filt["invoice_number"] = token
                out["filter"] = filt
                if out.get("type") == "find":
                    out["limit"] = 1
                if date_filter:
                    out = self._merge_date_filter(out, date_filter)
                return out

            if coll == "users":
                user_name = self._extract_person_name(q)
                if user_name and out.get("type") == "find":
                    filt = out.get("filter") if isinstance(out.get("filter"), dict) else {}
                    filt["name"] = user_name
                    out["filter"] = filt
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
        field = self._infer_date_field(query_lower, collection)
        now = datetime.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

        if "last month" in query_lower:
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_month_end = month_start - timedelta(microseconds=1)
            last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return {field: {"$gte": last_month_start.isoformat(), "$lte": last_month_end.isoformat()}}

        if "this month" in query_lower:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return {field: {"$gte": start.isoformat(), "$lte": today_end.isoformat()}}

        if "this week" in query_lower:
            week_start = day_start - timedelta(days=day_start.weekday())
            week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
            return {field: {"$gte": week_start.isoformat(), "$lte": week_end.isoformat()}}

        if "next week" in query_lower:
            this_week_start = day_start - timedelta(days=day_start.weekday())
            next_week_start = this_week_start + timedelta(days=7)
            next_week_end = next_week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
            return {field: {"$gte": next_week_start.isoformat(), "$lte": next_week_end.isoformat()}}

        if "next month" in query_lower:
            first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month_start = (first_this_month + timedelta(days=32)).replace(day=1)
            next_month_end = ((next_month_start + timedelta(days=32)).replace(day=1)) - timedelta(seconds=1)
            return {field: {"$gte": next_month_start.isoformat(), "$lte": next_month_end.isoformat()}}

        if "next 7 days" in query_lower:
            end = today_end + timedelta(days=7)
            return {field: {"$gte": day_start.isoformat(), "$lte": end.isoformat()}}

        if "next 30 days" in query_lower or "upcoming" in query_lower:
            end = today_end + timedelta(days=30)
            return {field: {"$gte": day_start.isoformat(), "$lte": end.isoformat()}}

        if re.search(r"\blast\s+7\s+days\b", query_lower):
            start = (day_start - timedelta(days=7))
            return {field: {"$gte": start.isoformat(), "$lte": now.isoformat()}}

        if "last quarter" in query_lower:
            quarter = ((now.month - 1) // 3) + 1
            prev_quarter = 4 if quarter == 1 else quarter - 1
            prev_year = now.year - 1 if quarter == 1 else now.year
            start_month = ((prev_quarter - 1) * 3) + 1
            quarter_start = datetime(prev_year, start_month, 1, 0, 0, 0)
            if start_month == 10:
                quarter_end = datetime(prev_year + 1, 1, 1, 0, 0, 0) - timedelta(seconds=1)
            else:
                quarter_end = datetime(prev_year, start_month + 3, 1, 0, 0, 0) - timedelta(seconds=1)
            return {field: {"$gte": quarter_start.isoformat(), "$lte": quarter_end.isoformat()}}

        if "next quarter" in query_lower:
            quarter = ((now.month - 1) // 3) + 1
            next_quarter = 1 if quarter == 4 else quarter + 1
            next_year = now.year + 1 if quarter == 4 else now.year
            start_month = ((next_quarter - 1) * 3) + 1
            quarter_start = datetime(next_year, start_month, 1, 0, 0, 0)
            if start_month == 10:
                quarter_end = datetime(next_year + 1, 1, 1, 0, 0, 0) - timedelta(seconds=1)
            else:
                quarter_end = datetime(next_year, start_month + 3, 1, 0, 0, 0) - timedelta(seconds=1)
            return {field: {"$gte": quarter_start.isoformat(), "$lte": quarter_end.isoformat()}}

        if "closing soon" in query_lower:
            end = today_end + timedelta(days=14)
            close_field = "closing_date" if (collection or "").lower() == "deals" else field
            return {close_field: {"$gte": day_start.isoformat(), "$lte": end.isoformat()}}

        if "due soon" in query_lower:
            end = today_end + timedelta(days=7)
            due_field = "due_date" if (collection or "").lower() in {"invoices", "createtasks"} else field
            return {due_field: {"$gte": day_start.isoformat(), "$lte": end.isoformat()}}

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

    def _infer_date_field(self, query_lower: str, collection: Optional[str]) -> str:
        """Pick date field for range filter based on query intent and collection."""
        explicit = re.search(r"\b(closing_date|closeDate|due_date|last_activity|createdAt)\b", query_lower)
        if explicit:
            token = explicit.group(1)
            if token == "closeDate":
                return "closing_date"
            return token

        coll = (collection or "").lower()
        if coll == "deals" and any(t in query_lower for t in ["closing", "close this week", "close next week"]):
            return "closing_date"
        if coll == "deals" and any(t in query_lower for t in ["upcoming closing", "closing soon"]):
            return "closing_date"
        if coll == "outreaches" and any(t in query_lower for t in ["new lead", "new leads"]):
            return "createdAt"
        if coll == "invoices" and "overdue" in query_lower:
            return "due_date"
        if coll == "createtasks" and "overdue" in query_lower:
            return "due_date"
        if coll == "meetings":
            return "start"

        return "createdAt"

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

    def _retry_with_date_field_aliases(self, query: str,
                                       prior_plan: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
        """
        If a date-range query returns empty, retry with common date field aliases.
        """
        if not isinstance(prior_plan, dict):
            return [], prior_plan
        q_lower = (query or "").lower()
        if not self._is_date_range_query(q_lower):
            return [], prior_plan

        aliases = ["created_at", "date_created", "createdDate"]
        plan_type = prior_plan.get("type")
        if plan_type not in {"find", "count", "aggregate"}:
            return [], prior_plan

        def _swap_key(obj: Dict[str, Any], from_key: str, to_key: str) -> Dict[str, Any]:
            cloned = dict(obj)
            if from_key in cloned:
                cloned[to_key] = cloned.pop(from_key)
            return cloned

        # Find existing date field in filter/$match
        current_field = None
        if plan_type in {"find", "count"}:
            filt = prior_plan.get("filter") if isinstance(prior_plan.get("filter"), dict) else {}
            for k, v in filt.items():
                if isinstance(v, dict) and any(op in v for op in ["$gte", "$lte", "$gt", "$lt"]):
                    current_field = k
                    break
            if not current_field:
                return [], prior_plan
            for alias in aliases:
                retry_plan = dict(prior_plan)
                retry_filter = _swap_key(filt, current_field, alias)
                retry_plan["filter"] = retry_filter
                retry_data = execute_query_plan(retry_plan)
                if not self._is_empty_result(retry_data):
                    return retry_data, retry_plan
            return [], prior_plan

        pipeline = prior_plan.get("pipeline") if isinstance(prior_plan.get("pipeline"), list) else []
        if not pipeline:
            return [], prior_plan
        first_match_idx = -1
        for i, st in enumerate(pipeline):
            if isinstance(st, dict) and "$match" in st and isinstance(st["$match"], dict):
                first_match_idx = i
                break
        if first_match_idx < 0:
            return [], prior_plan
        match_stage = dict(pipeline[first_match_idx]["$match"])
        for k, v in match_stage.items():
            if isinstance(v, dict) and any(op in v for op in ["$gte", "$lte", "$gt", "$lt"]):
                current_field = k
                break
        if not current_field:
            return [], prior_plan

        for alias in aliases:
            retry_plan = dict(prior_plan)
            retry_pipeline = list(pipeline)
            retry_match = _swap_key(match_stage, current_field, alias)
            retry_pipeline[first_match_idx] = {"$match": retry_match}
            retry_plan["pipeline"] = retry_pipeline
            retry_data = execute_query_plan(retry_plan)
            if not self._is_empty_result(retry_data):
                return retry_data, retry_plan
        return [], prior_plan

    def _is_meeting_query(self, query: str) -> bool:
        q = (query or "").lower()
        return any(t in q for t in [
            "meeting", "meetings", "scheduled call", "calendar", "schedule for",
            "meeting history", "upcoming call", "booked session",
        ])

    def _meetings_query_pipeline(self, user_query: str, resolved_query: str,
                                 start_time: float) -> Optional[Dict]:
        if not self._is_meeting_query(resolved_query):
            return None
        q = resolved_query.lower()
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        upcoming_end = today_end + timedelta(days=30)

        filt: Dict[str, Any] = {}
        date_label = "upcoming 30 days"

        if "meeting history" in q:
            filt["start"] = {"$lt": now.isoformat()}
            date_label = "past meetings"
            entity = self._extract_named_object(q, {"company", "contact"})
            if entity:
                filt["$or"] = [{"title": {"$regex": entity, "$options": "i"}}]
            sort = [["start", -1]]
        elif "today" in q:
            filt["start"] = {"$gte": today_start.isoformat(), "$lte": today_end.isoformat()}
            date_label = "today"
            sort = [["start", 1]]
        elif "this week" in q:
            filt["start"] = {"$gte": week_start.isoformat(), "$lte": week_end.isoformat()}
            date_label = "this week"
            sort = [["start", 1]]
        elif "upcoming" in q or "next 30 days" in q or "get meetings" in q:
            filt["start"] = {"$gte": today_start.isoformat(), "$lte": upcoming_end.isoformat()}
            date_label = "today to next 30 days"
            sort = [["start", 1]]
        else:
            filt["start"] = {"$gte": today_start.isoformat(), "$lte": upcoming_end.isoformat()}
            sort = [["start", 1]]

        user_name = self._extract_person_name(resolved_query)
        if user_name and any(t in q for t in ["for ", "schedule for", "meetings for"]):
            filt["assigned_to"] = user_name

        plan = {"type": "find", "collection": "meetings", "filter": filt, "sort": sort, "limit": 20}
        data = execute_query_plan(plan)

        if self._is_empty_result(data):
            response = (
                f"No meetings scheduled for {date_label}.\n"
                "Try: 'Get upcoming meetings for [user]' to see future meetings."
            )
            return self._build_result(response, start_time, format_type="not_found", collection="meetings")

        prefix = (
            f"Found {len(data)} meeting record(s) | Filter: date = '{date_label}' | Collection: meetings"
        )
        body = auto_format(data, user_query, "list", response_meta=self._build_response_meta(plan, user_query, data))
        response = f"{prefix}\n{body}"
        self.memory.add_exchange(user_query, response, collection="meetings")
        return self._build_result(response, start_time, format_type="list", collection="meetings")

    def _linked_query_pipeline(self, user_query: str, resolved_query: str,
                               start_time: float) -> Optional[Dict]:
        q = (resolved_query or "").lower()
        relation_hit = any(t in q for t in [
            "linked to", "associated with", "for this company", "for this deal",
            "belonging to", "under ", "attached to",
        ])
        if not relation_hit:
            return None

        parent_type = None
        if "company" in q:
            parent_type = "company"
        elif "deal" in q:
            parent_type = "deal"
        elif "contact" in q:
            parent_type = "contact"
        if not parent_type:
            return None

        parent_name = self._extract_named_object(resolved_query, {parent_type})
        if not parent_name:
            return None

        child_map = [
            ("task", "createtasks", "company_id", "deal_id"),
            ("contact", "contacts", "company_id", "deal_id"),
            ("deal", "deals", "company_id", "deal_id"),
            ("invoice", "invoices", "company_id", "deal_id"),
            ("sales", "sales", "company_id", "deal_id"),
            ("order", "sales", "company_id", "deal_id"),
        ]
        child_type, child_collection, company_fk, deal_fk = "record", "", "", ""
        for token, coll, c_fk, d_fk in child_map:
            if token in q:
                child_type, child_collection, company_fk, deal_fk = token, coll, c_fk, d_fk
                break
        if not child_collection:
            return None

        parent_id = resolve_name_to_id(parent_name, parent_type)
        if not parent_id:
            response = f"{parent_type.title()} '{parent_name}' was not found. Cannot retrieve linked {child_type}s."
            return self._build_result(response, start_time, format_type="not_found", collection=child_collection)

        fk_field = company_fk if parent_type == "company" else deal_fk
        plan = {
            "type": "find",
            "collection": child_collection,
            "filter": {fk_field: parent_id},
            "sort": [["createdAt", -1]],
            "limit": 50,
        }
        data = execute_query_plan(plan)
        if self._is_empty_result(data):
            response = f"No {child_type} records are linked to '{parent_name}'."
            return self._build_result(response, start_time, format_type="not_found", collection=child_collection)

        prefix = (
            f"Found {len(data)} {child_type} record(s) | "
            f"Linked to: {parent_type} = '{parent_name}' | Collection: {child_collection}"
        )
        body = auto_format(data, user_query, "list", response_meta=self._build_response_meta(plan, user_query, data))
        response = f"{prefix}\n{body}"
        self.memory.add_exchange(user_query, response, collection=child_collection)
        return self._build_result(response, start_time, format_type="list", collection=child_collection)

    def _extract_named_object(self, query: str, anchors: set) -> Optional[str]:
        q = query or ""
        anchor_rx = "|".join(re.escape(a) for a in anchors)
        patterns = [
            rf"(?:linked to|associated with|for|under|attached to)\s+(?:the\s+)?(?:{anchor_rx})\s+([A-Za-z0-9&.\-_/ ]{{2,120}})",
            rf"(?:{anchor_rx})\s+(?:named|name is)\s+([A-Za-z0-9&.\-_/ ]{{2,120}})",
        ]
        for p in patterns:
            m = re.search(p, q, re.IGNORECASE)
            if m:
                value = m.group(1).strip().strip(" .")
                value = re.sub(r"\b(details?|records?|list|show|get)\b.*$", "", value, flags=re.IGNORECASE).strip()
                if value:
                    return value
        return None

    def _extract_specific_lookup_target(self, query: str, collection: str) -> Tuple[Optional[str], List[str]]:
        q = query or ""
        coll = (collection or "").lower()
        invoice = re.search(r"\b([A-Z]{2,}[A-Z0-9]*[/-]\d{2,4}[/-]\d+)\b", q, re.IGNORECASE)
        so = re.search(r"\b(?:so|sales\s*order)\s*(?:number|no|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b", q, re.IGNORECASE)
        name = self._extract_named_object(q, {"invoice", "deal", "contact", "company", "sales order", "so"})

        if coll == "invoices" and invoice:
            return invoice.group(1).strip(), ["invoice_number", "name", "invoice_no", "ref"]
        if coll == "sales" and so:
            return so.group(1).strip(), ["so_number", "name", "order_number", "ref"]
        if coll == "deals" and name:
            return name, ["deal_name", "name", "title"]
        if coll == "contacts" and name:
            return name, ["full_name", "name", "display_name"]
        if coll == "companies" and name:
            return name, ["company_name", "name", "account_name"]
        return None, []

    def _exact_lookup_zero_result_chain(self, query: str, plan: Dict[str, Any],
                                        start_time: float) -> Optional[Dict]:
        if not isinstance(plan, dict) or plan.get("type") != "find":
            return None
        collection = plan.get("collection", "")
        value, fields = self._extract_specific_lookup_target(query, collection)
        if not value or not fields:
            return None

        sort = plan.get("sort", [["createdAt", -1]])
        for field in fields:
            # Attempt 1: exact equality
            p1 = {"type": "find", "collection": collection, "filter": {field: value}, "sort": sort, "limit": 1}
            d1 = execute_query_plan(p1)
            if isinstance(d1, list) and len(d1) == 1:
                response = auto_format(d1, query, "detail", response_meta=self._build_response_meta(p1, query, d1))
                return self._build_result(response, start_time, format_type="detail", collection=collection)

            # Attempt 2: case-insensitive exact regex
            p2 = {
                "type": "find",
                "collection": collection,
                "filter": {field: {"$regex": f"^{re.escape(value)}$", "$options": "i"}},
                "sort": sort,
                "limit": 1,
            }
            d2 = execute_query_plan(p2)
            if isinstance(d2, list) and len(d2) == 1:
                response = auto_format(d2, query, "detail", response_meta=self._build_response_meta(p2, query, d2))
                return self._build_result(response, start_time, format_type="detail", collection=collection)

        similar = search_by_keyword(collection, value, limit=3) or []
        names = []
        for row in similar[:3]:
            if isinstance(row, dict):
                for key in ["name", "invoice_number", "so_number", "companyName", "full_name", "title"]:
                    if row.get(key):
                        names.append(str(row.get(key)))
                        break
        close = ", ".join(names[:3]) if names else "none"
        response = (
            f"No record found for '{value}'. Please verify the name or number.\n"
            f"Similar records available: {close}."
        )
        return self._build_result(response, start_time, format_type="not_found", collection=collection)

    def _apply_temporal_sort_policy(self, query: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(plan or {})
        q = (query or "").lower()
        if out.get("type") != "find":
            return out
        field = self._infer_date_field(q, out.get("collection"))
        if any(t in q for t in ["upcoming", "next week", "next month", "next quarter", "next 7 days", "next 30 days", "closing soon", "due soon"]):
            out["sort"] = [[field, 1]]
        elif "overdue" in q:
            out["sort"] = [[field, -1]]
        return out

    def _is_date_range_query(self, q_lower: str) -> bool:
        return bool(
            ("week" in q_lower)
            or ("month" in q_lower)
            or ("quarter" in q_lower)
            or ("upcoming" in q_lower)
            or ("next" in q_lower)
            or ("soon" in q_lower)
            or re.search(r"\blast\s+\d+\s+days\b", q_lower)
            or ("today" in q_lower)
            or ("date" in q_lower)
        )

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
        # Capture FULL names including hyphens, commas, ampersands — never truncate
        patterns = [
            r'^\s*who\s+is\s+([\w][\w\s\-&.,]{1,100})',
            r'\bof\s+([\w][\w\s\-&.,]{1,100})',
            r'\bfor\s+([\w][\w\s\-&.,]{1,100})',
        ]
        for p in patterns:
            m = re.search(p, query, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                name = re.sub(
                    r'\s+\b(give|show|get|details|summary|his|her|in|with|and|please|including)\b.*$',
                    '', name, flags=re.IGNORECASE,
                ).strip().rstrip(".,;")
                if len(name) > 1:
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

        # Always use case-insensitive regex — never exact match
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

        # Build contact filter: handle single name OR "First Last" format
        name_parts = name.strip().split()
        if len(name_parts) >= 2:
            first, last = name_parts[0], " ".join(name_parts[1:])
            contact_filter = {
                "$and": [
                    {"firstName": {"$regex": first, "$options": "i"}},
                    {"lastName": {"$regex": last, "$options": "i"}},
                ]
            }
        else:
            contact_filter = {
                "$or": [
                    {"firstName": {"$regex": name, "$options": "i"}},
                    {"lastName": {"$regex": name, "$options": "i"}},
                ]
            }

        contacts = execute_query_plan({
            "type": "find",
            "collection": "contacts",
            "filter": contact_filter,
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

    def _company_linked_query_pipeline(
        self, user_query: str, resolved_query: str, qparams: Any, start_time: float
    ) -> Optional[Dict]:
        """
        Handle queries that ask for data from a collection linked to a specific company.

        Patterns:
          "Transportation Services this company invoice payment status and currency"
          "Transportation Services this company invoice who created give me name"
          "Transportation Services this company invoice owner name"

        Flow:
          1. Detect "this company" + company name + target collection
          2. Resolve company name → _id via companies collection
          3. Query target collection filtered by company
          4. If field resolution needed (owner/created by → user name),
             use relationship graph to find FK field and join with users
        """
        q = resolved_query.lower()

        # Must contain "this company" to trigger
        if "this company" not in q:
            return None

        # Extract company name from qparams (already extracted by query understander)
        company_name = (qparams.entity_names[0]
                        if qparams.entity_names else None)
        if not company_name:
            return None

        # Detect target collection from keywords
        coll_keywords = [
            (["invoice", "invoices", "billing", "bill"], "invoices"),
            (["deal", "deals", "opportunity"], "deals"),
            (["contact", "contacts"], "contacts"),
            (["task", "tasks"], "createtasks"),
            (["sales order", "sales"], "sales"),
            (["note", "notes"], "commonnotes"),
            (["meeting", "meetings"], "meetings"),
        ]
        target_coll = None
        for keywords, coll in coll_keywords:
            if any(kw in q for kw in keywords):
                target_coll = coll
                break

        if not target_coll:
            return None

        self._log(
            "Company-linked pipeline",
            f"company='{company_name}' collection={target_coll}"
        )

        # Step 1: Resolve company name → ObjectId
        company_id = resolve_name_to_id(company_name, "company")
        if not company_id:
            response = (
                f"No company named '{company_name}' was found in the CRM.\n"
                f"Check the spelling or try a partial name."
            )
            return self._build_result(response, start_time, format_type="not_found",
                                      collection="companies")

        # Step 2: Determine company FK field in target collection
        # Use relationship graph dynamically — no hardcoding
        graph = get_graph()
        company_fk = "company"  # default
        for edge in graph.joins_from(target_coll):
            if edge["foreign_collection"] == "companies":
                company_fk = edge["local_field"]
                break

        # Step 3: Convert company_id string → ObjectId for proper MongoDB match
        from bson import ObjectId as _ObjId
        try:
            cid_obj = _ObjId(str(company_id)) if re.match(r"^[0-9a-fA-F]{24}$", str(company_id)) else company_id
        except Exception:
            cid_obj = company_id

        # Step 4: Query the linked collection
        records = execute_query_plan({
            "type": "find",
            "collection": target_coll,
            "filter": {company_fk: cid_obj},
            "sort": [["createdAt", -1]],
            "limit": 20,
        })

        if self._is_empty_result(records):
            response = (
                f"No {target_coll} found for company '{company_name}'.\n"
                f"The company exists but has no linked {target_coll} records."
            )
            return self._build_result(response, start_time, format_type="not_found",
                                      collection=target_coll)

        # Step 4: Detect if user wants a specific FK field resolved to a name
        # Use relationship graph to find FK fields pointing to users
        graph = get_graph()
        user_fk_fields = [
            e["local_field"] for e in graph.joins_from(target_coll)
            if e["foreign_collection"] == "users"
        ]

        # Match query keywords to FK field names (dynamic, no hardcoding)
        requested_fk = self._match_fk_field_from_query(q, user_fk_fields)

        if requested_fk:
            # Resolve the FK → user names and return a focused response
            return self._resolve_fk_names_response(
                user_query, records, target_coll, company_name,
                requested_fk, start_time
            )

        # Step 5: Standard format — show records with relevant fields
        plan_meta = {
            "type": "find",
            "collection": target_coll,
            "filter": {company_fk: company_id},
        }
        response = auto_format(
            records, user_query, "list",
            response_meta=self._build_response_meta(plan_meta, user_query, records),
        )
        response = mask_response(response)
        self.memory.add_exchange(user_query, response, collection=target_coll)
        return self._build_result(response, start_time, format_type="list",
                                  collection=target_coll)

    def _match_fk_field_from_query(self, ql: str, fk_fields: List[str]) -> Optional[str]:
        """
        Dynamically match a query's intent to a FK field name using keyword overlap.
        No hardcoding — driven entirely by field names from the relationship graph.
        """
        best_field = None
        best_score = 0

        for field in fk_fields:
            # Split camelCase into component words
            words = re.findall(r"[a-z]+", re.sub(r"([A-Z])", r" \1", field).lower())
            score = sum(1 for w in words if w in ql and len(w) > 2)
            if score > best_score:
                best_score = score
                best_field = field

        return best_field if best_score > 0 else None

    def _resolve_fk_names_response(
        self, user_query: str, records: List[Dict],
        collection: str, company_name: str,
        fk_field: str, start_time: float
    ) -> Dict:
        """
        Resolve a FK ObjectId field to user names and return a formatted response.
        Used for "who created", "owner name" type queries.
        """
        from bson import ObjectId as _ObjId
        from tools.mongodb_tool import get_db as _get_db

        # Collect all unique FK ObjectIds
        oid_to_name: Dict[str, str] = {}
        oid_list = []
        for rec in records:
            raw = str(rec.get(fk_field) or "")
            if re.match(r"^[0-9a-fA-F]{24}$", raw):
                try:
                    oid_list.append(_ObjId(raw))
                except Exception:
                    pass

        if oid_list:
            try:
                db = _get_db()
                for u in db["users"].find(
                    {"_id": {"$in": oid_list}}, {"name": 1, "email": 1}
                ):
                    oid_to_name[str(u["_id"])] = u.get("name") or u.get("email") or str(u["_id"])
            except Exception as exc:
                logger.warning(f"FK name resolution failed: {exc}")

        # Build clean response
        field_label = re.sub(r"([A-Z])", r" \1", fk_field).strip().title()
        lines = [
            f"Found {len(records)} {collection} record(s) | Filter: company = '{company_name}' | Collection: {collection}",
            f"Query echo -> Entity: {collection} | Field resolved: {fk_field} → users | Returned: {len(records)} records",
            f"{field_label} for {company_name}'s {collection}:",
            "",
        ]

        seen_names: List[str] = []
        for rec in records:
            raw = str(rec.get(fk_field) or "")
            name = oid_to_name.get(raw, raw[:19] + "xxxxx" if len(raw) >= 24 else raw)
            inv_no = rec.get("invoice_number") or rec.get("name") or str(rec.get("_id", ""))[:10]
            if name not in seen_names:
                seen_names.append(name)
            lines.append(f"  {inv_no}: {field_label} = {name}")

        if seen_names:
            unique_names = list(dict.fromkeys(seen_names))
            lines.append(f"\nUnique {field_label}(s): {', '.join(unique_names)}")

        response = "\n".join(lines)
        response = mask_response(response)
        self.memory.add_exchange(user_query, response, collection=collection)
        return self._build_result(response, start_time, format_type="list",
                                  collection=collection)

    def _apply_understander_sort(
        self, plan: Dict[str, Any], sort_field: str,
        direction: int, reason: str
    ) -> Dict[str, Any]:
        """
        Override plan sort when user explicitly asks for low/high/latest/oldest.
        Maps generic sort_field token ('amount', 'createdAt') to actual DB field
        by sampling the collection schema.
        """
        out = dict(plan)
        coll = out.get("collection", "")

        # Map generic token to likely DB field for this collection
        amount_fields = {
            "invoices": "grandtotal_in_usd",
            "deals": "grand_total_in_usd",
            "sales": "grand_total_in_usd",
            "bills": "netPayableAmount",
        }
        date_fields = {
            "invoices": "invoice_date",
            "deals": "createdAt",
            "createtasks": "due_date",
            "contacts": "createdAt",
            "companies": "createdAt",
        }

        if sort_field == "amount":
            real_field = amount_fields.get(coll, "grand_total_in_usd")
        else:
            real_field = date_fields.get(coll, "createdAt")

        self._log("Sort override", f"field={real_field} dir={direction} reason={reason}")

        if out.get("type") == "find":
            out["sort"] = [[real_field, direction]]
            # Remove any extra date/status filters added by LLM for low/high sort queries
            # (user only asked for sort, not a filtered subset)
            if reason in ("low_value", "high_value") and isinstance(out.get("filter"), dict):
                filt = dict(out["filter"])
                # Remove ALL LLM-added filters — user only asked for sort, not a subset
                noisy_keys = [
                    "payment_status", "invoice_date", "sales_date", "createdAt",
                    "grandtotal_in_usd", "grand_total_in_usd", "grand_total",
                    "subtotal_in_usd", "subtotal", "amount", "netPayableAmount",
                ]
                for noisy_key in noisy_keys:
                    filt.pop(noisy_key, None)
                # Also remove any existence-only conditions ($exists) on amount fields
                for k in list(filt.keys()):
                    if isinstance(filt[k], dict) and list(filt[k].keys()) == ["$exists"]:
                        filt.pop(k, None)
                out["filter"] = filt
        elif out.get("type") == "aggregate":
            pipeline = out.get("pipeline", [])
            # Remove existing $sort stages, add new one
            pipeline = [s for s in pipeline if "$sort" not in s]
            pipeline.append({"$sort": {real_field: direction}})
            out["pipeline"] = pipeline

        return out

    def _owner_names_pipeline(
        self, user_query: str, resolved_query: str, start_time: float
    ) -> Optional[Dict]:
        """
        Handle "give me owner names of [X]" queries.
        Fetches the primary collection, resolves owner ObjectIds via users join,
        and returns a clean list of owner names.
        """
        q = resolved_query.lower()
        # Only trigger for AGGREGATE owner-listing queries — NOT specific field lookups
        # Must have owner + names + of a collection (not "created by" or single-entity lookups)
        if not re.search(r'\bowner\s+names?\s+of\b|\bowners?\s+of\b|\ball\s+owners?\b|\blist\s+owners?\b', q):
            return None
        # Do NOT trigger if query is about a specific company's data (handled by company pipeline)
        if re.search(r'\bthis\s+company\b', q):
            return None

        # Map common noun → collection
        coll_signals = [
            (["account", "accounts", "company", "companies", "client"], "companies", "companyOwner"),
            (["contact", "contacts"], "contacts", "contactOwner"),
            (["deal", "deals", "opportunity"], "deals", "owner"),
            (["task", "tasks"], "createtasks", "createdBy"),
            (["lead", "leads", "outreach"], "outreaches", "assignedTo"),
            (["invoice", "invoices"], "invoices", "invoiceOwner"),
            (["sales", "sales order"], "sales", "salesOwner"),
        ]

        collection, owner_field = "companies", "companyOwner"
        for keywords, coll, field in coll_signals:
            if any(kw in q for kw in keywords):
                collection, owner_field = coll, field
                break

        self._log("Owner-names pipeline", f"collection={collection} field={owner_field}")

        # Aggregate: group by owner field → count
        agg_data = execute_query_plan({
            "type": "aggregate",
            "collection": collection,
            "pipeline": [
                {"$group": {"_id": f"${owner_field}", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 50},
            ],
        })

        if not isinstance(agg_data, list) or not agg_data:
            return None

        # Resolve owner ObjectIds → user names
        # Must use ObjectId objects for _id lookup — string IDs won't match in MongoDB
        from bson import ObjectId as _ObjId
        from tools.mongodb_tool import get_db as _get_db

        id_to_name: Dict[str, str] = {}
        oid_list = []
        for row in agg_data:
            raw = str(row.get("_id") or "")
            if re.match(r"^[0-9a-fA-F]{24}$", raw):
                try:
                    oid_list.append(_ObjId(raw))
                except Exception:
                    pass

        if oid_list:
            try:
                db = _get_db()
                for u in db["users"].find({"_id": {"$in": oid_list}}, {"name": 1, "email": 1}):
                    id_to_name[str(u["_id"])] = u.get("name") or u.get("email") or str(u["_id"])
            except Exception as exc:
                logger.warning(f"Owner name lookup failed: {exc}")

        # Build response
        lines = [
            f"Found {len(agg_data)} owner(s) | Field: {owner_field} | Collection: {collection}",
            f"Query echo -> Entity: {collection} owner | Returned: {len(agg_data)} unique owners",
            f"Owner names for {collection}:",
            "",
        ]
        for row in agg_data:
            raw_id = str(row.get("_id", ""))
            owner_name = id_to_name.get(raw_id) or id_to_name.get(raw_id[:24]) or raw_id[:19] + "xxxxx" if len(raw_id) >= 24 else raw_id
            count = row.get("count", 0)
            lines.append(f"  {owner_name}: {count} {collection}")

        response = "\n".join(lines)
        response = mask_response(response)
        self.memory.add_exchange(user_query, response, collection=collection)
        return self._build_result(response, start_time, format_type="list",
                                  collection=collection)

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
                filt["payment_status"] = {"$ne": "paid"}
        elif collection == "createtasks":
            if "pending" in query:
                filt["status"] = {"$in": ["Pending", "pending", "Open", "open"]}
            if "overdue" in query:
                filt["due_date"] = {"$lt": now_iso}
                filt["status"] = {"$nin": ["Completed", "completed"]}
        return filt

    def _build_parallel_summary_response(self, user_query: str, collection: str,
                                          total_count: int, categories: List[Dict],
                                          sample: List[Dict],
                                          format_hint: str = "summary") -> str:
        q = user_query.lower()
        category_field = _humanize_field(self._infer_category_field(collection, q))
        echo_line = (
            f"Found {total_count} {collection.rstrip('s')} record(s) | "
            f"Filter applied: {self._infer_base_filter(collection, q) or 'none'} | "
            f"Collection: {collection}"
        )

        if format_hint == "table":
            lines = [
                echo_line,
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
            lines = [
                echo_line,
                f"Category breakdown by {category_field}:",
            ]
            if categories:
                for row in categories[:10]:
                    lines.append(f"- {row.get('_id', 'Unknown')}: {row.get('count', 0)}")
            else:
                lines.append("- No category breakdown available.")
            return "\n".join(lines)

        lines = [
            echo_line,
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
