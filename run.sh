#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
INDEX_DIR="${PROJECT_DIR}/knowledge/faiss_index"
PARSED_RULES_FILE="${PROJECT_DIR}/knowledge/parsed_rules.json"

cd "${PROJECT_DIR}"

echo "============================================"
echo " Elsner ECRM Chatbot - Run Script"
echo "============================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is not installed or not in PATH."
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "Error: ollama is not installed or not in PATH."
  echo "Install it first: https://ollama.ai"
  exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[1/4] Creating virtual environment..."
  python3 -m venv "${VENV_DIR}"
else
  echo "[1/4] Virtual environment already exists."
fi

echo "[2/4] Activating virtual environment..."
source "${VENV_DIR}/bin/activate"

echo "[3/4] Installing/updating dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

if [[ ! -d "${INDEX_DIR}" || ! -f "${PARSED_RULES_FILE}" ]]; then
  echo "[4/4] Building knowledge base (first-time setup)..."
  python setup.py
else
  echo "[4/4] Knowledge base already exists. Skipping setup.py."
fi

echo
echo "Starting Elsner ECRM Chatbot (React UI + Flask backend)"
echo "  Chat UI  →  http://localhost:8000"
echo "  API base →  http://localhost:8000"
echo "Press Ctrl+C to stop."
echo

# ── Clear port 8000 if anything is already running on it ──
if command -v fuser >/dev/null 2>&1; then
  fuser -k 8000/tcp 2>/dev/null && echo "[run.sh] Cleared existing process on port 8000." || true
  sleep 0.5
fi

# Reduce noisy transformer lazy-import warnings.
export TRANSFORMERS_VERBOSITY=error
export TRANSFORMERS_NO_ADVISORY_WARNINGS=1

# Force Python to flush stdout immediately (so logs appear in real-time)
export PYTHONUNBUFFERED=1

# ── Flask server serves React UI + all API endpoints ──
# Streamlit (app.py) is kept but disabled — to re-enable:
#   exec streamlit run app.py --server.headless true --server.port 8501
exec "${VENV_DIR}/bin/python" -u server.py
