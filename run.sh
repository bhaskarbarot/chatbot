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
echo "Starting Streamlit app on http://localhost:8501"
echo "Press Ctrl+C to stop."
echo

# Reduce noisy transformer lazy-import warnings triggered by Streamlit file watching.
export TRANSFORMERS_VERBOSITY=error
export TRANSFORMERS_NO_ADVISORY_WARNINGS=1

exec streamlit run app.py \
  --server.headless true \
  --server.port 8501 \
  --server.fileWatcherType none
