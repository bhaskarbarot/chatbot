"""
Elsner ECRM Chatbot — Streamlit UI
Professional chat interface with live pipeline logs.
"""
import html
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st
from config import OLLAMA_MODEL
from agents.orchestrator import ChatOrchestrator
from knowledge.vector_store import is_index_built
from utils.auth import require_auth, logout, current_user

# ── Page Config ──────────────────────────────────────
st.set_page_config(
    page_title="Elsner ECRM Chatbot",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main .block-container { padding-top: 1rem; max-width: 100%; }
    .log-entry { font-family: monospace; font-size: 12px; padding: 2px 8px; margin: 1px 0;
                 background: #1a1a2e; color: #d4d4d4; border-radius: 4px; }
    .log-container { background: #1a1a2e; border-radius: 8px; padding: 12px;
                     max-height: 600px; overflow-y: auto; }
    .timing-ok { display: inline-block; background: #e8f5e9; color: #2e7d32;
                 padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
    .timing-slow { display: inline-block; background: #fbe9e7; color: #c62828;
                   padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ── Auth Gate ────────────────────────────────────────
require_auth()

# ── Session Init ─────────────────────────────────────
if "orc" not in st.session_state:
    st.session_state.orc = ChatOrchestrator()
if "msgs" not in st.session_state:
    st.session_state.msgs = []
if "logs" not in st.session_state:
    st.session_state.logs = []
if "qcount" not in st.session_state:
    st.session_state.qcount = 0
if "show_logs" not in st.session_state:
    st.session_state.show_logs = True


# ── Sidebar ──────────────────────────────────────────
with st.sidebar:
    st.markdown("### Elsner ECRM Chatbot")
    user = current_user()
    if user:
        st.caption(f"Logged in as: **{user}**")
        if st.button("Logout", use_container_width=True):
            logout()
    st.markdown("---")
    st.markdown("**System Status**")

    idx_ok = is_index_built()
    st.markdown(f"{'OK' if idx_ok else 'MISSING'} | Knowledge Base")
    if not idx_ok:
        st.warning("Run `python setup.py` first!")

    try:
        from tools.mongodb_tool import get_db
        get_db().list_collection_names()
        st.markdown("OK | MongoDB Connected")
    except Exception as e:
        st.markdown("FAIL | MongoDB")
        st.error(str(e)[:80])

    st.markdown(f"OK | Model: {OLLAMA_MODEL}")
    try:
        from utils.circuit_breaker import get_llm_breaker
        cb = get_llm_breaker()
        stats = cb.get_stats()
        cb_state = stats["state"]
        cb_icon = "OK" if cb_state == "closed" else ("WARN" if cb_state == "half_open" else "FAIL")
        st.markdown(f"{cb_icon} | LLM Circuit: {cb_state.upper()}")
    except Exception:
        pass
    st.markdown("---")
    st.metric("Queries", st.session_state.qcount)
    st.session_state.show_logs = st.toggle("Show Pipeline Logs", value=st.session_state.show_logs)

    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        if st.button("Clear Chat", use_container_width=True):
            st.session_state.msgs = []
            st.session_state.logs = []
            st.session_state.qcount = 0
            st.session_state.orc.clear_memory()
            st.rerun()

    with btn_col2:
        if st.button("Clear Cache", use_container_width=True):
            try:
                cache = st.session_state.orc.cache
                cleared = cache.clear_all() if hasattr(cache, "clear_all") else False
                if cleared:
                    st.toast("Cache cleared.", icon="🗑️")
                else:
                    st.toast("Cache cleared.", icon="🗑️")
            except Exception as _e:
                st.toast(f"Cache clear failed: {_e}", icon="⚠️")
            st.rerun()

    st.markdown("---")
    st.markdown("**Quick Queries**")
    for q in [
        "Show all open deals",
        "Total revenue this year",
        "Overdue invoices",
        "Pipeline by stage",
        "Pending tasks",
        "Top 10 customers by revenue",
        "Deals closed this month",
        "Target vs achieved",
    ]:
        if st.button(q, key=f"q_{hash(q)}", use_container_width=True):
            st.session_state.msgs.append({"role": "user", "content": q})
            with st.spinner("Processing..."):
                res = st.session_state.orc.process_query(q)
            st.session_state.msgs.append({
                "role": "assistant", "content": res["response"],
                "timing": res.get("timing", 0)
            })
            st.session_state.qcount += 1
            st.session_state.logs = res.get("logs", [])
            st.rerun()


# ── Main ─────────────────────────────────────────────
if st.session_state.show_logs:
    chat_col, log_col = st.columns([3, 1])
else:
    chat_col = st.container()
    log_col = None

with chat_col:
    st.markdown("## Elsner ECRM Assistant")
    st.caption("Ask anything about deals, invoices, companies, contacts, tasks, targets, outreach, and more.")

    # Chat history
    for m in st.session_state.msgs:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m["role"] == "assistant" and m.get("timing"):
                t = m["timing"]
                cls = "timing-ok" if t <= 10 else "timing-slow"
                st.markdown(f'<span class="{cls}">{t:.1f}s</span>', unsafe_allow_html=True)

    # Chat input
    user_input = st.chat_input("Ask about your CRM data...")
    if user_input:
        st.session_state.msgs.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        with st.chat_message("assistant"):
            with st.spinner("Processing..."):
                res = st.session_state.orc.process_query(user_input)
            st.markdown(res["response"])
            t = res.get("timing", 0)
            cls = "timing-ok" if t <= 10 else "timing-slow"
            st.markdown(f'<span class="{cls}">{t:.1f}s</span>', unsafe_allow_html=True)
        st.session_state.msgs.append({
            "role": "assistant", "content": res["response"],
            "timing": res.get("timing", 0)
        })
        st.session_state.qcount += 1
        st.session_state.logs = res.get("logs", [])
        st.rerun()

# Log panel
if log_col is not None and st.session_state.show_logs:
    with log_col:
        st.markdown("### Pipeline Logs")
        if not st.session_state.logs:
            st.markdown("*Ask a question to see logs*")
        else:
            st.markdown('<div class="log-container">', unsafe_allow_html=True)
            for entry in st.session_state.logs:
                c = "#d4d4d4"
                if "ERROR" in entry: c = "#f44336"
                elif "found" in entry.lower() or "result" in entry.lower(): c = "#4caf50"
                elif "Search" in entry or "Planning" in entry: c = "#64b5f6"
                elif "Total time" in entry: c = "#ffd54f"
                safe_entry = html.escape(entry)
                st.markdown(f'<div class="log-entry" style="color:{c}">{safe_entry}</div>',
                            unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
