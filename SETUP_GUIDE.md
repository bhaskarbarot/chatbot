# Elsner ECRM Chatbot — Complete Setup and Implementation Guide

---

## Project Overview

An agentic chatbot that answers business queries from the Elsner ECRM MongoDB database. It uses local Ollama models, FAISS vector search over parsed business rules, and a structured pipeline to deliver accurate, fast, zero-hallucination responses.

**Key Architecture:**
```
User Query
    |
    v
[Greeting/Scope Check] --> Greeting response OR "out of scope" reply
    |
    v
[Chat Memory] --> Resolve follow-ups ("give me details" -> "details of Ketul")
    |
    v
[FAISS Vector Search] --> Find top matching business rules
    |
    v
[LLM Query Planner] --> Generate MongoDB query plan (JSON)
    |
    v
[MongoDB Executor] --> Run query, get raw data
    |
    v
[Response Formatter] --> Format as list/table/summary + mask sensitive IDs
    |
    v
[Memory Update] --> Save entity context + keywords for follow-ups
    |
    v
Final Response (max 10 seconds)
```

---

## Prerequisites

Before starting, make sure you have:

1. **Python 3.10+** installed
2. **Ollama** installed and running locally (https://ollama.ai)
3. **An Ollama model with tool-calling support** pulled (see Step 2)
4. **Network access** to MongoDB at `180.211.96.19:2008`

---

## Step-by-Step Setup

### Step 1: Create Project Directory

```bash
mkdir elsner_chatbot
cd elsner_chatbot
```

Copy ALL the project files into this directory maintaining this exact structure:

```
elsner_chatbot/
├── .env                          # Environment configuration
├── config.py                     # Central settings
├── setup.py                      # One-time index builder
├── app.py                        # Streamlit UI
├── requirements.txt              # Python dependencies
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py           # Main pipeline brain
│   └── query_planner.py          # LLM-based query plan generator
├── knowledge/
│   ├── __init__.py
│   ├── prompts.py                # System prompts for LLM
│   ├── rule_parser.py            # Parses business rule files
│   └── vector_store.py           # FAISS index for rule matching
├── memory/
│   ├── __init__.py
│   └── chat_memory.py            # Chat history + follow-up resolver
├── tools/
│   ├── __init__.py
│   └── mongodb_tool.py           # MongoDB connection + query executor
├── utils/
│   ├── __init__.py
│   ├── formatter.py              # Response formatting (list/table/count)
│   ├── logger.py                 # Logging with pipeline timing
│   └── masking.py                # ID/PAN/GST masking
├── logs/                         # Auto-created log files
└── knowledge/
    └── faiss_index/              # Auto-created by setup.py
```

### Step 2: Install Ollama and Pull Model

```bash
# Install Ollama (if not already installed)
curl -fsSL https://ollama.ai/install.sh | sh

# Pull a model with tool-calling support (choose ONE):
ollama pull qwen2.5:7b          # RECOMMENDED: Fast, good tool support
# OR
ollama pull llama3.1:8b          # Alternative: Good general performance
# OR
ollama pull mistral:7b           # Alternative: Fast inference

# Verify Ollama is running
ollama list
```

**Important:** Update `.env` file with your chosen model name:
```
OLLAMA_MODEL=qwen2.5:7b
```

### Step 3: Install Python Dependencies

```bash
# Create virtual environment (recommended)
python -m venv venv
source venv/bin/activate          # Linux/Mac
# OR
venv\Scripts\activate             # Windows

# Install all dependencies
pip install -r requirements.txt
```

**Note:** The first run of `setup.py` will download the `all-MiniLM-L6-v2` embedding model (~80MB). This only happens once.

### Step 4: Place Your Rule Files

Copy your 3 rule files into the project root OR the `knowledge/` folder:

```bash
# Copy files to project root
cp /path/to/rules.txt ./
cp /path/to/CHATBOT_QUERY_RULES.md ./
cp /path/to/DATABASE_METADATA.md ./
```

The setup script will find them automatically.

### Step 5: Run Setup (One-Time)

```bash
python setup.py
```

This will:
1. Parse all business rules from your 3 files
2. Extract ~200+ unique query patterns
3. Create embeddings for each pattern
4. Build a FAISS vector index for fast similarity search
5. Test the MongoDB connection
6. Run a quick sanity check

**Expected output:**
```
============================================================
  ELSNER ECRM CHATBOT — Setup
============================================================

[1/4] Locating rule files...
  rules.txt: /path/to/rules.txt
  CHATBOT_QUERY_RULES.md: /path/to/CHATBOT_QUERY_RULES.md
  DATABASE_METADATA.md: /path/to/DATABASE_METADATA.md

[2/4] Parsing business rules...
  Total unique rules: 200+

[3/4] Parsing database metadata...
  Collections documented: 46
  Critical rules: 8

[4/4] Building FAISS vector index...
  Index built: 200+ vectors

  Setup Complete!
  Run the chatbot with: streamlit run app.py
```

### Step 6: Launch the Chatbot

```bash
streamlit run app.py
```

Open your browser to `http://localhost:8501`

---

## How It Works (Architecture Deep Dive)

### 1. Rule Parsing (NOT naive chunking)

The key difference from your previous attempts: instead of blindly embedding chunks of the 3 files, we PARSE each file into structured rules:

```python
{
    "intent": "Get details for this company: [name]",
    "process": "Query companies where deleted=false and companyName matches...",
    "collections": ["companies", "users", "regions"],
    "fields": ["companyName", "companyOwner", "region"],
    "keywords": ["company", "details", "account"],
    "search_text": "Get details company companies users regions companyName..."
}
```

Each rule becomes a searchable unit. The `search_text` field is what gets embedded -- it combines the intent, collections, and fields for maximum semantic matching.

### 2. FAISS Vector Search (not full RAG)

When a user asks "show me Acme Corp details", the system:
1. Embeds the query using `all-MiniLM-L6-v2`
2. Searches FAISS for the top 5 matching rules
3. Applies keyword boosting (exact term matches get a score bump)
4. Returns the best matching rule WITH its process instructions

This is faster and more accurate than full RAG because we are searching over STRUCTURED rules, not raw text.

### 3. LLM Query Planning

The matched rules are injected into the LLM prompt as templates. The LLM generates a JSON query plan:

```json
{
    "type": "aggregate",
    "collection": "companies",
    "pipeline": [
        {"$match": {"companyName": {"$regex": "Acme", "$options": "i"}, "deleted": false}},
        {"$lookup": {"from": "users", "localField": "companyOwner", "foreignField": "_id", "as": "ownerInfo"}},
        {"$unwind": {"path": "$ownerInfo", "preserveNullAndEmptyArrays": true}}
    ]
}
```

The system prompt includes ALL critical rules (soft delete, field names, collection mapping, USD fields, etc.) so the LLM never generates wrong queries.

### 4. Chat Memory (Smart Follow-ups)

The memory system tracks:
- **Short-term:** Last 10 exchanges with keywords
- **Entity context:** The last discussed entity (person, company, deal)
- **Summary:** Compressed older conversation

Follow-up resolution:
```
"who is Ketul" -> entity: {type: "user", name: "Ketul"}
"give me details" -> detects no entity, checks memory -> "give me details of Ketul"
"show me deals" -> detects own entity ("deals"), NOT a follow-up -> standalone query
```

### 5. ID Masking

All ObjectIds, PAN numbers, and GST numbers are automatically masked:
```
69a57d60bcba5c1fad335849 -> 69a57d60bcba5c1fadxxxxx
AAACU0564G -> AAACUxxxxx
22AAAAA0000A1Z5 -> 22AAAAA0000xxxxx
```

### 6. Response Formatting

The formatter detects intent from query words:
- "how many" / "count" -> count format
- "list" / "which" / "all" -> numbered list
- "table" -> markdown table
- "details" -> full detail view
- Default: auto (single=detail, few=summary, many=list)

Every response starts with a 3-line summary.

---

## Configuration Options

Edit `.env` to customize:

```env
# Change Ollama model
OLLAMA_MODEL=qwen2.5:7b

# Change MongoDB connection
MONGODB_URI=mongodb://root:RPbMknbcvuqzYVeuTxtd@180.211.96.19:2008/ecrm?authSource=admin
MONGODB_DB=ecrm

# Performance tuning
MAX_RESPONSE_TIME=10    # Target max seconds per response
MAX_MONGO_RESULTS=50    # Max documents returned per query
TOP_K_RULES=5           # Number of rules to match per query

# Embedding model (change for different accuracy/speed trade-off)
EMBEDDING_MODEL=all-MiniLM-L6-v2
```

---

## Troubleshooting

### "Knowledge Base: Not Built"
Run `python setup.py` before starting the app.

### "MongoDB: Disconnected"
- Check if `180.211.96.19:2008` is reachable from your machine
- Verify the username/password in `.env`
- Check firewall rules

### Slow responses (>10s)
- Use a smaller model: `qwen2.5:7b` instead of `14b`
- Ensure Ollama has GPU access: `ollama ps` should show GPU
- Reduce `TOP_K_RULES` to 3 in `.env`

### Wrong query results
- Check `logs/chatbot_YYYYMMDD.log` for the generated query plan
- The pipeline logs in the UI show each step
- If a specific rule is missing, add it to `rules.txt` and re-run `setup.py`

### "No results found" for valid data
- Verify the collection name in the log
- Check if soft-delete filter is being applied correctly
- Try the query directly in MongoDB compass

---

## Feature Checklist

| # | Feature | Status |
|---|---------|--------|
| 1 | 3-line summary in all responses | Implemented in formatter.py |
| 2 | Understands list vs count vs detail | detect_format_intent() in formatter.py |
| 3 | "top 10" returns exactly 10 | Limit extraction in query_planner.py |
| 4 | Complex multi-part queries | multi_step in mongodb_tool.py |
| 5 | Direct search (invoice number, company name) | search_by_keyword() fallback |
| 6 | Chat history follow-up understanding | ChatMemory.resolve_followup() |
| 7 | Proper collection selection by intent | FAISS rule matching + LLM decision tree |
| 8 | Live pipeline logs | Streamlit log panel |
| 9 | Business rules behavior | Parsed rules injected into LLM prompt |
| 10 | Non-relevant query handling | _is_out_of_scope() + 3 suggestions |
| 11 | Greeting handling | _is_greeting() with friendly response |
| 12 | ID masking (ObjectId, PAN, GST) | masking.py |
| 13 | User format preference (list/table) | detect_format_intent() |
| 14 | Zero hardcoding | All dynamic via rules + LLM |
| 15 | No emojis | Enforced in prompts |

---

## Adding New Rules

To add new business query patterns:

1. Open `rules.txt`
2. Add a new entry:
```
business: Your new query pattern here
process: How to build the MongoDB query for this
```
3. Re-run `python setup.py` to rebuild the index
4. Restart the app

No code changes needed. The system picks up new rules automatically.

---

## File Summary

| File | Purpose | Lines |
|------|---------|-------|
| `config.py` | All settings, collection maps, keywords | ~90 |
| `setup.py` | One-time index builder | ~110 |
| `app.py` | Streamlit chat UI | ~160 |
| `agents/orchestrator.py` | Main pipeline brain | ~280 |
| `agents/query_planner.py` | LLM query plan generation | ~280 |
| `knowledge/prompts.py` | System prompts for LLM | ~200 |
| `knowledge/rule_parser.py` | Parses business rule files | ~200 |
| `knowledge/vector_store.py` | FAISS index operations | ~170 |
| `memory/chat_memory.py` | Chat history + follow-up resolver | ~250 |
| `tools/mongodb_tool.py` | MongoDB connection + queries | ~330 |
| `utils/formatter.py` | Response formatting | ~320 |
| `utils/logger.py` | Logging + timing | ~55 |
| `utils/masking.py` | Sensitive data masking | ~120 |

**Total:** ~2,550 lines of Python across 13 files.

---

## Quick Start Commands Summary

```bash
# 1. Setup environment
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Pull Ollama model
ollama pull qwen2.5:7b

# 3. Place rule files in project root
# (rules.txt, CHATBOT_QUERY_RULES.md, DATABASE_METADATA.md)

# 4. Build knowledge base (one-time)
python setup.py

# 5. Launch chatbot
streamlit run app.py
```

That's it. The chatbot is ready to use.
