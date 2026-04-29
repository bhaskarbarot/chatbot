"""
Minimal session-based authentication for the Streamlit UI.

Configuration (add to .env):
  AUTH_ENABLED=true
  AUTH_USERNAME=admin
  AUTH_PASSWORD_HASH=<sha256_hex_of_password>
  SESSION_TIMEOUT_S=28800

To generate a password hash:
  python3 -c "import hashlib,sys; sys.stdout.write(hashlib.sha256(b'yourpassword').hexdigest())"

Usage in app.py:
  from utils.auth import require_auth
  require_auth()   # must be the first call in app body
"""
import hashlib
import os
import time
from typing import Optional

import streamlit as st

AUTH_ENABLED: bool = os.getenv("AUTH_ENABLED", "false").lower() == "true"
AUTH_USERNAME: str = os.getenv("AUTH_USERNAME", "admin")
AUTH_PASSWORD_HASH: str = os.getenv("AUTH_PASSWORD_HASH", "")
SESSION_TIMEOUT_S: int = int(os.getenv("SESSION_TIMEOUT_S", "28800"))  # 8 h


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _session_valid() -> bool:
    if "auth_user" not in st.session_state:
        return False
    elapsed = time.time() - float(st.session_state.get("auth_time", 0))
    if elapsed >= SESSION_TIMEOUT_S:
        _clear_session()
        return False
    return True


def _clear_session():
    st.session_state.pop("auth_user", None)
    st.session_state.pop("auth_time", None)


def _show_login() -> bool:
    """Render login form. Returns True on successful login."""
    st.markdown(
        "<h2 style='text-align:center'>Elsner ECRM Chatbot</h2>"
        "<p style='text-align:center;color:#888'>Authorized access only</p>",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form", clear_on_submit=True):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)

        if submitted:
            if not AUTH_PASSWORD_HASH:
                st.error(
                    "AUTH_PASSWORD_HASH is not set in .env. "
                    "Run: python3 -c \"import hashlib,sys; sys.stdout.write(hashlib.sha256(b'yourpw').hexdigest())\""
                )
                return False
            if username == AUTH_USERNAME and _hash(password) == AUTH_PASSWORD_HASH:
                st.session_state["auth_user"] = username
                st.session_state["auth_time"] = time.time()
                return True
            else:
                st.error("Invalid username or password.")
                return False

    return False


def require_auth():
    """
    Call once at the top of app.py (before any other st.* calls).
    If AUTH_ENABLED is false (default), this is a no-op.
    If authentication fails, the app is halted with st.stop().
    """
    if not AUTH_ENABLED:
        return

    if not _session_valid():
        logged_in = _show_login()
        if not logged_in:
            st.stop()


def logout():
    """Programmatic logout — call from a sidebar button."""
    _clear_session()
    st.rerun()


def current_user() -> Optional[str]:
    """Return the authenticated username, or None if not authenticated."""
    return st.session_state.get("auth_user")
