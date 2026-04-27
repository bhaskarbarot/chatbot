"""
Setup Script — Run ONCE to build the FAISS vector index from rule files.
This parses all business rules, creates embeddings, and saves the index.

Usage:
    python setup.py
"""
import os
import sys
import shutil

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from config import FAISS_INDEX_PATH, KNOWLEDGE_DIR
from knowledge.rule_parser import (parse_rules_file, parse_chatbot_rules_md,
                                    parse_database_metadata, build_all_rules,
                                    save_rules)
from knowledge.vector_store import build_index, is_index_built
from utils.logger import get_logger

logger = get_logger("setup")


def main():
    print("=" * 60)
    print("  ELSNER ECRM CHATBOT — Setup")
    print("=" * 60)

    # Step 1: Locate rule files
    print("\n[1/4] Locating rule files...")

    # Check for files in knowledge dir or common locations
    rules_path = _find_file("rules.txt")
    chatbot_rules_path = _find_file("CHATBOT_QUERY_RULES.md")
    metadata_path = _find_file("DATABASE_METADATA.md")

    if not rules_path:
        print("ERROR: rules.txt not found!")
        print("Please place rules.txt in the project root or knowledge/ directory")
        sys.exit(1)
    if not chatbot_rules_path:
        print("ERROR: CHATBOT_QUERY_RULES.md not found!")
        sys.exit(1)
    if not metadata_path:
        print("ERROR: DATABASE_METADATA.md not found!")
        sys.exit(1)

    print(f"  rules.txt: {rules_path}")
    print(f"  CHATBOT_QUERY_RULES.md: {chatbot_rules_path}")
    print(f"  DATABASE_METADATA.md: {metadata_path}")

    # Step 2: Parse rules
    print("\n[2/4] Parsing business rules...")
    rules = build_all_rules(rules_path, chatbot_rules_path)
    print(f"  Total unique rules: {len(rules)}")

    # Save parsed rules
    rules_json_path = os.path.join(KNOWLEDGE_DIR, "parsed_rules.json")
    save_rules(rules, rules_json_path)
    print(f"  Saved to: {rules_json_path}")

    # Step 3: Parse database metadata
    print("\n[3/4] Parsing database metadata...")
    metadata = parse_database_metadata(metadata_path)
    print(f"  Collections documented: {len(metadata['collections'])}")
    print(f"  Critical rules: {len(metadata['critical_rules'])}")
    print(f"  Common mistakes: {len(metadata['common_mistakes'])}")

    # Copy source files to knowledge dir for reference
    for src_path in [rules_path, chatbot_rules_path, metadata_path]:
        dest = os.path.join(KNOWLEDGE_DIR, os.path.basename(src_path))
        if os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.copy2(src_path, dest)

    # Step 4: Build FAISS index
    print("\n[4/4] Building FAISS vector index...")
    print("  Loading embedding model (first time may download ~80MB)...")

    if os.path.exists(FAISS_INDEX_PATH):
        shutil.rmtree(FAISS_INDEX_PATH)

    index, indexed_rules = build_index(rules, FAISS_INDEX_PATH)
    print(f"  Index built: {index.ntotal} vectors")
    print(f"  Saved to: {FAISS_INDEX_PATH}")

    # Verify
    print("\n" + "=" * 60)
    print("  Setup Complete!")
    print("=" * 60)
    print(f"\n  Rules indexed: {len(rules)}")
    print(f"  Vector index: {FAISS_INDEX_PATH}")
    print(f"\n  Run the chatbot with: streamlit run app.py")

    # Quick test
    print("\n  Quick test - searching for 'total revenue'...")
    from knowledge.vector_store import search_rules
    results = search_rules("total revenue this year", top_k=3)
    for i, r in enumerate(results, 1):
        print(f"    {i}. [{r['similarity_score']:.3f}] {r['intent'][:70]}")

    # Test MongoDB connection
    print("\n  Testing MongoDB connection...")
    try:
        from tools.mongodb_tool import get_db
        db = get_db()
        colls = db.list_collection_names()
        print(f"  Connected! Found {len(colls)} collections")
    except Exception as e:
        print(f"  WARNING: MongoDB connection failed: {e}")
        print("  The chatbot needs MongoDB to be accessible.")


def _find_file(filename: str) -> str:
    """Search for a file in common locations."""
    search_paths = [
        os.path.join(os.path.dirname(__file__), filename),
        os.path.join(os.path.dirname(__file__), "knowledge", filename),
        os.path.join(os.path.dirname(__file__), "..", filename),
        filename,
    ]

    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)
    return None


if __name__ == "__main__":
    main()
