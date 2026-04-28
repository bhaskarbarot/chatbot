# Project Workflow (Current System)

This file describes the **current production workflow** of the Elsner ECRM chatbot, including all enhancement layers added in the latest audit cycle.

---

## 1) System goal

The chatbot converts natural-language CRM questions into safe MongoDB operations and returns contextual, human-readable responses.

Core behavior:
- Understand user intent and context.
- Build or select an execution plan.
- Execute safely on MongoDB.
- Validate + shape results.
- Format response with context and masking.
- Update memory/cache/metrics asynchronously.

---

## 2) Current architecture map

### UI and entrypoint
- `app.py`
  - Streamlit chat UI.
  - Creates `ChatOrchestrator`.
  - Displays answers, logs, and health indicators.

### Central orchestrator
- `agents/orchestrator.py`
  - Main runtime coordinator (`process_query`).
  - Handles routing across fast paths, rule paths, LLM planning, fallback, and recovery.

### Understanding layers
- `knowledge/query_understander.py`
  - Regex/entity/temporal extraction.
- `knowledge/deep_query_understander.py`
  - LLM-backed deeper semantic understanding with rule-based fallback.
- `knowledge/intent_classifier.py`
  - Fast high-confidence intent routing.
- `knowledge/intent_enricher.py`
  - Merges classifier + understanders into one enriched intent contract.

### Planning and execution layers
- `agents/query_planner.py`
  - LLM query plan generation + schema/operator validation.
- `agents/structured_plan_wrapper.py`
  - Adds shape contract, enforced limits, and step metadata.
- `knowledge/relationship_enhancer.py`
  - Injects join hints using relationship graph when needed.
- `tools/mongodb_tool.py`
  - Sanitized Mongo execution engine (`find`, `aggregate`, `count`, `distinct`, `multi_step`).
  - Wrapped with Mongo circuit breaker.

### Post-execution quality layers
- `agents/response_validator.py`
  - Enforces result shape, count/list/single expectations, field filtering, aggregate extraction.
- `agents/self_healing_agent.py`
  - Recovery when results are empty.
  - Uses bounded attempts and strict timeout guards.

### Output control layers
- `utils/response_controller.py`
  - Shape instruction generation + post-format correction.
- `utils/formatter.py`
  - Humanized contextual formatting (never bare number responses).
- `utils/masking.py`
  - Sensitive-value masking.

### Knowledge and retrieval
- `knowledge/rule_parser.py`
  - Parses rule sources to structured rules.
- `knowledge/vector_store.py`
  - FAISS + embeddings retrieval.
  - Thread-safe shared singleton embedding model.
  - Auto-migrates legacy `rules.pkl` -> `rules.json`.
- `knowledge/relationship_graph.py`
  - Collection relationship graph from `connection.txt`.

### Memory, cache, reliability, metrics
- `memory/chat_memory.py` + `memory/enhanced_memory_resolver.py`
  - Follow-up resolution and async context storage.
- `utils/cache.py`
  - Redis semantic response cache.
  - Reuses vector-store embedding singleton.
  - Schema-versioned cache keys to avoid stale-format responses.
- `utils/circuit_breaker.py`
  - LLM + Mongo circuit breakers.
- `utils/metrics.py`
  - Non-blocking metrics writer.

---

## 3) End-to-end runtime flow (current)

1. **Receive query**
   - `app.py` sends query to `ChatOrchestrator.process_query()`.

2. **Guard + preprocessing**
   - Greeting/out-of-scope checks.
   - Follow-up resolution via enhanced memory resolver.
   - Query understanding via fast + deep layers.

3. **Intent enrichment**
   - Build unified `enriched_intent` with:
     - primary intent
     - response shape
     - limit/sort/temporal hints
     - collection hints
     - multi-intent flag

4. **Fast-path routing**
   - Attempt deterministic/specialized pipelines first (existence, person/company linked queries, etc.).

5. **Cache lookup**
   - Exact/semantic Redis cache lookup.
   - If valid cache hit -> return quickly.

6. **Rule retrieval (RAG)**
   - FAISS search + confidence gating.
   - High confidence may trigger rule-compiled plan.

7. **Plan generation**
   - Either:
     - deterministic plan from rule/intent, or
     - LLM plan (`query_planner`) with strict schema/operator safety.

8. **Plan wrapping + enhancement**
   - `structured_plan_wrapper`: adds shape contracts/limits.
   - `relationship_enhancer`: adds join hints where needed.

9. **Mongo execution**
   - Execute via sanitized `mongodb_tool`.
   - Circuit breaker protects connection/command failures.

10. **Validation layer**
   - `response_validator` enforces shape and extracts aggregate numeric values.
   - Flags empty result behavior.

11. **Self-healing (bounded recovery)**
   - Triggered only when required and within time budget.
   - Attempt order (bounded):
     1. Batched date alias `$or` retry (single call strategy).
     2. Constraint relaxation.
     3. Replan with hard timeout (5s max).
   - Stops with graceful message if all fail.

12. **Response shaping and formatting**
   - `response_controller` + `formatter` produce final text.
   - Numeric answers always include label + collection + query context.
   - Humanized collection names used for user-facing language.

13. **Mask + return**
   - Sensitive values masked.
   - Final response returned to UI.

14. **Async post-response updates**
   - Memory store/update.
   - Cache set.
   - Metrics logging.

---

## 4) Rule and embedding workflow

### Sources
- `rules.txt`
- `CHATBOT_QUERY_RULES.md`

### Build/setup
1. Parse rules to structured records.
2. Create embeddings.
3. Build FAISS index (`rules.index`) + metadata (`rules.json`).

### Runtime retrieval
1. Embed user query.
2. Search FAISS top-k.
3. Keyword-boost rerank.
4. Confidence gate to decide:
   - trust rule-compiled plan, or
   - fall back to LLM planning with retrieved context.

### Legacy migration behavior
- If only `rules.pkl` exists, system loads it and writes `rules.json` immediately (one-time auto-migration).

---

## 5) Query safety model

Safety controls currently active:
- Planner blocks dangerous operators (e.g. `$where`, `$function`).
- Mongo tool sanitizes filter/pipeline content.
- Collection validation against allowed set.
- Soft-delete conditions injected where configured.
- Mongo circuit breaker wraps DB calls.
- LLM circuit breaker wraps model calls.
- All failure paths return graceful, non-crashing output.

---

## 6) Response quality model (current)

Current quality guarantees:
- No bare numeric responses (label + context required).
- Human-readable intros for lists.
- Dynamic collection humanization (e.g. `createtasks` -> `task`).
- Aggregate extraction handles `_id = 0` summary docs.
- Conversion/rate-like numeric responses formatted as percentages.
- Query echo included for relevance transparency.

---

## 7) Memory and follow-up behavior

- Short-term conversational context maintained.
- Follow-up strategies include:
  - last result set references ("those", "these")
  - same-filter continuation
  - entity carry-forward (guarded by type relevance)
- Async context storage uses daemon threads and does not block response path.

---

## 8) Performance and resiliency controls

- Shared thread-safe embedding singleton (no duplicate concurrent model loads).
- Semantic cache for repeated/similar queries.
- Strict time budgeting in orchestrator.
- Self-healing replan hard timeout (5s).
- Non-blocking memory/cache/metrics updates where applicable.

---

## 9) Operational files and purpose

- `config.py` -> centralized runtime configuration.
- `connection.txt` -> relationship metadata source.
- `requirements.txt` -> dependencies.
- `setup.py` -> builds rule artifacts and index.
- `scripts/run_qa_tests.py` -> 60-query QA.
- `scripts/run_full_qa_tests.py` -> 170-query QA.

---

## 10) Current mental model

**User query -> understand + enrich intent -> retrieve rules -> build/validate plan -> execute safely -> validate/heal -> shape + format + mask -> async memory/cache/metrics update -> return response.**

