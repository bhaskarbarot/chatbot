"""
Central configuration for Elsner ECRM Chatbot.
Loads from .env and provides defaults.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── MongoDB ──────────────────────────────────────────
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise EnvironmentError(
        "MONGODB_URI is not set. Create a .env file with MONGODB_URI=mongodb://..."
    )
MONGODB_DB = os.getenv("MONGODB_DB", "ecrm")

# ── Ollama ───────────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1024"))
OLLAMA_TIMEOUT_S = int(os.getenv("OLLAMA_TIMEOUT_S", "20"))
LLM_PLAN_RETRIES = int(os.getenv("LLM_PLAN_RETRIES", "1"))
OLLAMA_SUMMARY_MODEL = os.getenv("OLLAMA_SUMMARY_MODEL", os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b"))

# ── Embeddings ───────────────────────────────────────
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
FAISS_INDEX_PATH = os.path.join(os.path.dirname(__file__), "knowledge", "faiss_index")

# ── Performance ──────────────────────────────────────
MAX_RESPONSE_TIME = int(os.getenv("MAX_RESPONSE_TIME", "30"))
MAX_MONGO_RESULTS = int(os.getenv("MAX_MONGO_RESULTS", "50"))
TOP_K_RULES = int(os.getenv("TOP_K_RULES", "5"))
RETRIEVAL_MIN_SCORE = float(os.getenv("RETRIEVAL_MIN_SCORE", "0.55"))
RETRIEVAL_MIN_MARGIN = float(os.getenv("RETRIEVAL_MIN_MARGIN", "0.06"))

# ── Cache (Redis) ────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
CACHE_SIMILARITY_THRESHOLD = float(os.getenv("CACHE_SIMILARITY_THRESHOLD", "0.85"))

# ── Logging ──────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# ── Paths ────────────────────────────────────────────
KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
RULES_JSON_PATH = os.path.join(KNOWLEDGE_DIR, "parsed_rules.json")
METADATA_PROMPT_PATH = os.path.join(KNOWLEDGE_DIR, "metadata_prompt.txt")

# ── Collection Map (exact MongoDB names) ─────────────
COLLECTION_NAMES = [
    "deals", "invoices", "sales", "companies", "contacts", "users",
    "createtasks", "outreaches", "bills", "vendors", "products",
    "notifications", "emails", "mails", "targets", "commonnotes",
    "notes", "dealsnotes", "contactsnotes", "companynotes", "salesnotes",
    "remotejobNotes", "regions", "campaigns", "departments", "projecttypes",
    "technologies", "technologycategories", "sources", "taxes",
    "lead_status", "lifecycle_stage", "dealstagesettings", "countryregions",
    "meetings", "remotejobs", "payments", "publicleads", "deletedcompanies",
    "bademails", "activities", "activityevents", "outreachactivities",
    "statuses", "categorys"
]

# ── Soft Delete Map ──────────────────────────────────
SOFT_DELETE_MAP = {
    "deals": {"field": "deleted", "value": False},
    "invoices": {"field": "deleted", "value": False},
    "sales": {"field": "deleted", "value": False},
    "companies": {"field": "deleted", "value": False},
    "contacts": {"field": "deleted", "value": False},
    "createtasks": {"field": "deleted", "value": False},
    "dealstagesettings": {"field": "deleted", "value": False},
    "outreaches": {"field": "isDeleted", "value": False},
    "remotejobs": {"field": "isDeleted", "value": False},
    "notifications": {"field": "isDeleted", "value": False},
}

# ── Sensitive Fields for Masking ─────────────────────
MASK_PATTERNS = {
    "objectid": {"pattern": r"[0-9a-fA-F]{24}", "mask_last": 5},
    "pan": {"pattern": r"[A-Z]{5}[0-9]{4}[A-Z]", "mask_last": 5},
    "gst": {"pattern": r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}", "mask_last": 5},
}

# ── Greeting Patterns ────────────────────────────────
GREETINGS = ["hi", "hello", "hey", "good morning", "good afternoon",
             "good evening", "howdy", "greetings", "namaste", "hola"]
