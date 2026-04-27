# Project Workflow (Complete Guide)

This document explains the full workflow of this project in simple language: what happens from app start to final response, and how rules, embeddings, connections, query building, and chat summary work together.

---

## 1) What this project does

This is an AI-powered CRM chatbot built with Streamlit.

It allows a user to ask business questions in natural language (for example, "show my latest meetings", "how many leads this month", "companies linked to owner X"), and then:

1. Understands the user intent.
2. Converts the question into a safe MongoDB query plan.
3. Executes that query on MongoDB.
4. Formats the results into a human-friendly answer.
5. Stores useful chat context for follow-up questions.

---

## 2) Main components and their jobs

## UI Layer
- `app.py`
  - Streamlit chat interface.
  - Accepts user query.
  - Shows answer and optional pipeline/debug info.

## Core Brain (Orchestration)
- `agents/orchestrator.py`
  - Main controller (`ChatOrchestrator.process_query()`).
  - Decides which path to use (greeting, fast intent, special pipeline, full LLM planning, fallback, etc.).
  - Connects all other modules.

## Query Planner (LLM + validation)
- `agents/query_planner.py`
  - Builds prompts for the LLM.
  - Converts LLM output into JSON query plan.
  - Validates schema and retries if needed.
  - Blocks risky operators and enforces safe query shape.

## Rule Knowledge + Embedding Retrieval
- `knowledge/rule_parser.py`
  - Parses rule files (`rules.txt`, `CHATBOT_QUERY_RULES.md`) into structured rule entries.
- `knowledge/vector_store.py`
  - Creates embeddings of rules.
  - Stores/retrieves them in FAISS.
  - Returns top matching rules for a user query.

## Query Understanding / Intent
- `knowledge/query_understander.py`
  - Extracts entities, limits, sorting hints, temporal hints, and intent clues.
- `knowledge/intent_classifier.py`
  - Fast intent detection path.
  - Can directly build simple plans without expensive full planning.

## Data Layer (MongoDB)
- `tools/mongodb_tool.py`
  - MongoDB connection singleton.
  - Executes `find`, `aggregate`, `count`, `distinct`, and multi-step plans.
  - Applies sanitization and safe defaults.

## Conversation Memory + Summary
- `memory/chat_memory.py`
  - Stores recent chat exchanges.
  - Resolves follow-up questions using previous topic/entity.
  - Maintains summary and contextual relationship checks.

## Output and Guardrails
- `utils/formatter.py` -> response formatting.
- `utils/masking.py` -> hides sensitive information where needed.
- `utils/cache.py` -> semantic cache for repeated/similar queries.
- `utils/circuit_breaker.py` -> protects against repeated failures (LLM/Mongo).
- `utils/metrics.py` -> logs timings/metrics.

## Relationship Metadata
- `knowledge/relationship_graph.py`
  - Reads `connection.txt`.
  - Builds relation graph (collection links / foreign key style connections).
  - Helps linked/multi-collection logic.

---

## 3) Full runtime flow (end-to-end)

When a user asks a question, this is the practical sequence:

1. **Input received**
   - User sends query in Streamlit UI.
   - Query enters `ChatOrchestrator.process_query()`.

2. **Early checks**
   - Greeting/out-of-scope checks run first.
   - If greeting, return friendly message.
   - If out of scope, return safe "not supported" style response.

3. **Use memory context**
   - Chat memory checks if this is a follow-up ("that one", "same owner", "what about this month?").
   - If yes, query may be enriched with last known entity/topic.

4. **Quick understanding**
   - Extract intent clues, entities, dates, limits, sorting preferences.
   - Detect if query matches specialized pipeline (for example meetings/person lookup/comparison/existence/linked queries).

5. **Specialized pipeline attempt**
   - If a specialized handler fits, it may directly build an accurate plan and skip heavy planning.
   - This is faster and often more reliable for known patterns.

6. **Cache check**
   - System checks semantic cache for similar recent queries.
   - If valid cached response exists, it returns quickly.

7. **Rule retrieval (RAG step)**
   - Query embedding is generated.
   - FAISS retrieves most relevant rules.
   - Keyword boosting and confidence gating rank best rules.

8. **Plan building**
   - Two major possibilities:
     - **Deterministic plan** from matched rule / fast intent.
     - **LLM-generated plan** using prompt + matched rules + constraints.
   - Planner validates output JSON format and schema.
   - If invalid, repair/retry logic runs.

9. **Plan post-processing**
   - Business intent overrides.
   - Entity constraints and collection alignment.
   - Temporal sort/limit normalization.
   - Additional sanity checks before execution.

10. **Mongo execution**
   - Plan is sanitized and executed in MongoDB.
   - Supports aggregation, filtering, sorting, counting, distinct, and multi-step operations.

11. **Fallbacks if no/poor results**
   - Exact lookup recovery attempts.
   - Replan attempts with adjusted constraints.
   - Date field alias retries.
   - Schema fallback plan in some cases.

12. **Response generation**
   - Results are auto-formatted or LLM-formatted.
   - Sensitive data masking applied.
   - Final text returned to UI.

13. **Post-response updates**
   - Save query/response in memory.
   - Update summary asynchronously.
   - Store useful response in semantic cache.
   - Record metrics/logs.

---

## 4) How rule understanding works

Rules are the project’s domain knowledge layer.

### Rule sources
- `rules.txt`
- `CHATBOT_QUERY_RULES.md`

### Rule parsing process
1. Parse raw rule text into structured entries:
   - intent
   - process instructions
   - target collections
   - relevant fields
   - keywords
   - search text (used for embedding)
2. Merge duplicates by normalized intent.
3. Keep final rule set for retrieval/planning.

### Runtime rule usage
1. User query is compared semantically to rules.
2. Top rules are fetched from FAISS + boosted by keyword overlap.
3. Confidence checks (score + margin) decide trust level.
4. High-confidence matches can create direct deterministic plans.
5. Otherwise, retrieved rules are injected into LLM prompt so planner still follows rule intent.

---

## 5) How embeddings work

Embeddings are used for semantic matching of user questions to rules.

### Embedding model
- Sentence Transformers model from `EMBEDDING_MODEL`.

### Index build (offline/setup)
1. Build embedding vector for each rule’s searchable text.
2. Normalize vectors.
3. Store in FAISS index (`knowledge/faiss_index/rules.index`).
4. Save metadata mapping (rule JSON).

### Query-time retrieval
1. Convert user query into embedding.
2. Search nearest vectors in FAISS (top-k).
3. Apply keyword boost re-ranking.
4. Return best rules with confidence values.

This gives semantic recall even when user wording is different from exact rule text.

---

## 6) How connection graph is used

The file `connection.txt` stores relationship knowledge (how collections are linked).

`knowledge/relationship_graph.py` parses this into a graph-like mapping that helps:
- linked entity lookups,
- multi-step query planning,
- selecting join-style traversal paths,
- suggesting collection path when user asks relation-based questions.

This is especially useful for queries like:
- "show contacts linked to company X"
- "owners for companies with meetings this week"
- "records connected through another entity"

---

## 7) How query plan is built

The project does not run raw user text directly on DB.  
It first builds a safe, structured plan.

### Plan inputs
- user query,
- extracted entities/intents,
- retrieved rules,
- relationship hints,
- business constraints.

### Plan strategies
1. **Fast deterministic path**
   - For known simple intents.
   - Built via `build_fast_plan()`.
2. **Rule-based direct plan**
   - If confident rule match exists.
3. **LLM planning path**
   - For complex/ambiguous requests.
   - Prompt contains strict schema and safety instructions.

### Plan safety and validation
- JSON extraction and schema validation.
- Dangerous operator blocking.
- Plan sanitization before execution.
- Retry/repair when malformed.

This structure reduces hallucination risk and keeps DB execution controlled.

---

## 8) How chat memory and summary work

The chatbot keeps short conversational memory to support natural follow-ups.

### Stored context
- recent user/assistant exchanges,
- last entity mentioned,
- last topic/collection context,
- running summary.

### Follow-up resolution examples
- "what about this month?"
- "and for owner Rahul?"
- "same for last week"

System resolves these by carrying forward relevant context from previous turn.

### Summary behavior
- Summary generation/refresh runs asynchronously.
- Main response path is not blocked by summary updates.
- Relatedness checks decide whether previous context should apply.

---

## 9) Data connections and external dependencies

### MongoDB
- Main source of business records.
- Configured by `MONGODB_URI` and `MONGODB_DB`.

### Ollama / LLM
- Planner and optional formatter/summarizer models.
- Configured by `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_SUMMARY_MODEL`.

### Redis (optional but useful)
- Semantic response caching.
- Controlled by `REDIS_URL`, TTL and similarity thresholds.

### FAISS (local)
- Rule embedding index.

### Sentence-transformers
- Embedding generation model.

---

## 10) Startup and setup workflow

### Typical run
1. Execute `run.sh`.
2. Create/use Python venv.
3. Install dependencies from `requirements.txt`.
4. If index is missing, run `setup.py`.
5. Start Streamlit app (`streamlit run app.py`).

### What `setup.py` prepares
- parses and merges rules,
- builds/saves FAISS index + metadata,
- parses connections metadata,
- basic retrieval sanity checks,
- Mongo connectivity check.

### Optional support scripts
- `scripts/ensure_indexes.py` -> create Mongo indexes.
- `scripts/run_qa_tests.py` / `scripts/run_full_qa_tests.py` -> QA/regression checks.
- `scripts/benchmark_all_rules.py` -> performance/coverage benchmarking.

---

## 11) Key configuration checklist

Minimum to run:
- `MONGODB_URI`

Common required for full quality:
- `MONGODB_DB`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `EMBEDDING_MODEL`
- `TOP_K_RULES`
- `MAX_MONGO_RESULTS`
- `RETRIEVAL_MIN_SCORE`
- `RETRIEVAL_MIN_MARGIN`
- `LOG_LEVEL`

Optional but recommended:
- `REDIS_URL`
- `CACHE_TTL_SECONDS`
- `CACHE_SIMILARITY_THRESHOLD`
- circuit breaker envs for reliability
- auth envs if login protection is needed

---

## 12) Reliability and safety features

- Query plan schema enforcement.
- Dangerous Mongo operators blocked.
- Sanitization before execution.
- Circuit breakers for repeated dependency failure.
- Timeout limits and response-time controls.
- Sensitive field masking in final response.
- Cache short-circuit for repeated questions.
- Multiple fallback chains when result is empty.

---

## 13) One-line mental model

**User question -> understand intent + context -> retrieve relevant rules (embedding search) -> build safe query plan -> execute on MongoDB -> format/mask response -> store memory/summary/cache -> return answer.**

