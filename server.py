"""
Elsner ECRM Chatbot — Flask server.
Serves the React chat-ui/dist and connects directly to ChatOrchestrator.
No FastAPI, no separate process — same Python backend, new UI.
"""
import os
import sys
import time
import threading
import logging

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, request, send_from_directory, Response

app = Flask(__name__)

# ── Enable request logs in terminal ───────────────────────────────────────────
import logging as _logging
_logging.basicConfig(
    format="[%(asctime)s] %(levelname)-7s | %(name)-18s | %(message)s",
    datefmt="%H:%M:%S",
    level=_logging.INFO,
    stream=sys.stdout,
)
# Show Flask/Werkzeug access lines (127.0.0.1 - GET /chat 200 -)
_logging.getLogger("werkzeug").setLevel(_logging.INFO)
app.logger.setLevel(_logging.INFO)

# ── Silence noisy third-party libs that flood the terminal ────────────────────
for _noisy in (
    "httpx", "httpcore", "httpcore.connection", "httpcore.http11",
    "huggingface_hub", "huggingface_hub.utils._http",
    "sentence_transformers", "sentence_transformers.base.model",
    "transformers", "urllib3", "filelock",
):
    _logging.getLogger(_noisy).setLevel(_logging.WARNING)

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_DIST = os.path.join(_BASE, "chat-ui", "dist")
_SRC_ASSETS = os.path.join(_BASE, "chat-ui", "src", "assets")

# ── Lazy orchestrator singleton (same pattern as Streamlit session_state) ─────
_orc = None
_orc_lock = threading.Lock()


def _get_orc():
    global _orc
    if _orc is None:
        with _orc_lock:
            if _orc is None:
                from agents.orchestrator import ChatOrchestrator
                _orc = ChatOrchestrator()
    return _orc


# ── CORS (needed when React dev server runs on a different port) ───────────────
@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def _options(path):
    return Response(status=204)


# ══════════════════════════════════════════════════════════════════════════════
#  API endpoints (called by React UI)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    """Backend liveness probe. React polls this every 15s."""
    try:
        from config import OLLAMA_MODEL, MONGODB_DB
        from tools.mongodb_tool import get_db
        db = get_db()
        db.command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False

    try:
        from config import OLLAMA_MODEL, MONGODB_DB
    except Exception:
        OLLAMA_MODEL = "unknown"
        MONGODB_DB = "unknown"

    from knowledge.vector_store import is_index_built
    return jsonify({
        "status": "ok",
        "mongo_ok": mongo_ok,
        "index_built": is_index_built(),
        "model": OLLAMA_MODEL,
        "db": MONGODB_DB,
    })


@app.route("/cache/status")
def cache_status():
    """Redis cache connection status."""
    try:
        from utils.cache import ResponseCache
        cache = ResponseCache()
        connected = cache._redis is not None
    except Exception:
        connected = False
    return jsonify({"redis_connected": connected})


@app.route("/cache/clear", methods=["POST"])
def cache_clear():
    """Clear all cached responses."""
    try:
        from utils.cache import ResponseCache
        cleared = ResponseCache().clear_all()
        return jsonify({"cleared": bool(cleared)})
    except Exception as e:
        return jsonify({"cleared": False, "error": str(e)})


@app.route("/sources")
def sources():
    """Return available data collections for the sidebar."""
    from config import COLLECTION_NAMES
    return jsonify({
        "mongodb": COLLECTION_NAMES[:15],
        "knowledge_base": ["rules.txt", "CHATBOT_QUERY_RULES.md"],
    })


@app.route("/feedback/learnings")
def learnings():
    """Placeholder — feedback learnings (no learnings engine yet)."""
    return jsonify({
        "total_feedback_processed": 0,
        "negative_rules": [],
        "intent_specific_rules": [],
        "format_preference_rules": [],
        "collection_selection_rules": [],
        "response_style_rules": [],
    })


@app.route("/feedback", methods=["POST"])
def feedback():
    """Store user feedback — saved to MongoDB feedback collection."""
    data = request.get_json(silent=True) or {}
    rating  = data.get("rating", 0)
    query   = str(data.get("query", ""))[:120]
    comment = str(data.get("comment", ""))[:300]
    app.logger.warning(f"[Feedback] rating={rating} query={query!r} comment={comment!r}")
    try:
        from tools.mongodb_tool import get_db
        db = get_db()
        db["chatbot_feedback"].insert_one({
            "query": query, "rating": rating, "comment": comment,
            "response": str(data.get("response", ""))[:500],
            "ts": __import__("datetime").datetime.utcnow().isoformat(),
        })
    except Exception:
        pass  # non-blocking — feedback save failure never errors the UI
    return jsonify({"saved": True})


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """
    Voice transcription endpoint (Firefox/Safari MediaRecorder fallback).
    Uses faster-whisper if available; otherwise returns a friendly error.
    Chrome/Edge use Web Speech API natively — they never hit this endpoint.
    """
    try:
        import whisper as _whisper  # openai-whisper
        import tempfile, os as _os
        audio = request.files.get("audio")
        if not audio:
            return jsonify({"text": ""}), 400
        suffix = ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            audio.save(tmp.name)
            tmp_path = tmp.name
        try:
            model = _whisper.load_model("base")
            result = model.transcribe(tmp_path)
            return jsonify({"text": result.get("text", "").strip()})
        finally:
            _os.unlink(tmp_path)
    except ImportError:
        return jsonify({
            "text": "",
            "error": "Voice transcription requires openai-whisper. "
                     "Please use Chrome or Edge for voice input (built-in speech recognition)."
        }), 501
    except Exception as exc:
        return jsonify({"text": "", "error": str(exc)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    """
    Main chat endpoint.
    Body: { "query": str, "history": [...] }
    Returns: { "answer": str, "data": any, "sources_used": [...],
               "confidence": float, "processing_time_ms": int,
               "query_plan": {...}, "query_used": any }
    """
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"answer": "Please enter a query.", "data": None}), 400

    t0 = time.time()
    try:
        orc = _get_orc()
        result = orc.process_query(query)
        elapsed_ms = int((time.time() - t0) * 1000)

        response_text = result.get("response", "") if isinstance(result, dict) else str(result)
        collection    = result.get("collection", "") if isinstance(result, dict) else ""
        fmt           = result.get("format", "auto") if isinstance(result, dict) else "auto"
        logs          = result.get("logs", [])        if isinstance(result, dict) else []

        return jsonify({
            "answer":             response_text,
            "data":               None,
            "sources_used":       [collection] if collection else [],
            "confidence":         1.0,
            "processing_time_ms": elapsed_ms,
            "agent_time_ms":      None,
            "query_plan": {
                "intent":     fmt,
                "collection": collection,
                "filters":    {},
                "output_format": fmt,
            },
            "query_used":  None,
            "logs":        logs,
        })
    except Exception as exc:
        elapsed_ms = int((time.time() - t0) * 1000)
        app.logger.error(f"[Chat] error: {exc}")
        return jsonify({
            "answer": f"An error occurred: {exc}",
            "data": None,
            "sources_used": [],
            "confidence": 0.0,
            "processing_time_ms": elapsed_ms,
        }), 500


# ══════════════════════════════════════════════════════════════════════════════
#  Static file serving — React dist + src/assets fallback
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/src/assets/<path:filename>")
def src_assets(filename):
    """Serve React source assets (logos, images referenced as URL strings)."""
    return send_from_directory(_SRC_ASSETS, filename)


@app.route("/assets/<path:filename>")
def dist_assets(filename):
    """Serve Vite-bundled assets (JS/CSS with hash names)."""
    return send_from_directory(os.path.join(_DIST, "assets"), filename)


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(_DIST, "favicon.svg")


@app.route("/icons.svg")
def icons():
    return send_from_directory(_DIST, "icons.svg")


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path):
    """SPA catch-all — serve index.html for any non-API route."""
    # Try to serve an exact static file first
    if path:
        candidate = os.path.join(_DIST, path)
        if os.path.isfile(candidate):
            return send_from_directory(_DIST, path)
    return send_from_directory(_DIST, "index.html")


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  Elsner ECRM Chatbot")
    print(f"  Chat UI  →  http://localhost:{port}")
    print(f"  API base →  http://localhost:{port}\n")
    # threaded=True → multiple browser tabs work without blocking
    # use_reloader=False → prevents Werkzeug from starting a silent child process
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
