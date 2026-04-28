"""
Vector Store — FAISS-based semantic search over parsed business rules.
Uses sentence-transformers for local embeddings (no API calls).
This is the core of the RAG system - matching user queries to the right
business rule + query template.
"""
import os
import re
import json
import threading
import numpy as np
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger("vector_store")

_model = None
_index = None
_rules = None
_index_path = None
_model_lock = threading.Lock()


def get_embedding_model():
    """Load sentence-transformers model (cached)."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            from config import EMBEDDING_MODEL
            logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
            _model = SentenceTransformer(EMBEDDING_MODEL)
            logger.info("Embedding model loaded")
    return _model


def build_index(rules: List[Dict], index_dir: str):
    """
    Build FAISS index from parsed rules.
    Each rule's search_text is embedded and indexed.
    """
    import faiss
    import json as _json

    model = get_embedding_model()
    os.makedirs(index_dir, exist_ok=True)

    texts = [rule["search_text"] for rule in rules]
    logger.info(f"Embedding {len(texts)} rules...")
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = np.array(embeddings, dtype="float32")

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    faiss.write_index(index, os.path.join(index_dir, "rules.index"))
    with open(os.path.join(index_dir, "rules.json"), "w", encoding="utf-8") as f:
        _json.dump(rules, f, ensure_ascii=False)

    logger.info(f"FAISS index built: {index.ntotal} vectors, dim={dim}")
    return index, rules


def load_index(index_dir: str) -> Tuple[any, List[Dict]]:
    """Load FAISS index and rules from disk."""
    global _index, _rules, _index_path
    import faiss
    import json as _json

    if _index is not None and _index_path == index_dir:
        return _index, _rules

    index_file = os.path.join(index_dir, "rules.index")
    rules_file = os.path.join(index_dir, "rules.json")
    # Backward compat: fall back to legacy pickle if JSON not yet generated
    rules_pkl = os.path.join(index_dir, "rules.pkl")

    if not os.path.exists(index_file):
        return None, None

    if os.path.exists(rules_file):
        with open(rules_file, "r", encoding="utf-8") as f:
            loaded_rules = _json.load(f)
    elif os.path.exists(rules_pkl):
        import pickle
        with open(rules_pkl, "rb") as f:
            loaded_rules = pickle.load(f)
        # BUG 6 / C5: auto-migrate one-time from legacy pickle to JSON.
        try:
            with open(rules_file, "w", encoding="utf-8") as jf:
                json.dump(loaded_rules, jf, ensure_ascii=False)
            logger.info("Migrated legacy rules.pkl to rules.json")
        except Exception as migration_error:
            logger.warning(f"Legacy rules migration failed: {migration_error}")
    else:
        return None, None

    _index = faiss.read_index(index_file)
    _rules = loaded_rules
    _index_path = index_dir

    logger.info(f"Loaded FAISS index: {_index.ntotal} vectors")
    return _index, _rules


def search_rules(query: str, top_k: int = 5,
                 index_dir: str = None) -> List[Dict]:
    """
    Search for the most relevant business rules for a user query.
    Returns top_k matches with similarity scores.
    """
    from config import FAISS_INDEX_PATH
    if index_dir is None:
        index_dir = FAISS_INDEX_PATH

    index, rules = load_index(index_dir)
    if index is None or rules is None:
        logger.warning("FAISS index not found. Run setup first.")
        return []

    model = get_embedding_model()

    # Embed the query
    query_vec = model.encode([query], normalize_embeddings=True)
    query_vec = np.array(query_vec, dtype="float32")

    # Search
    scores, indices = index.search(query_vec, min(top_k, index.ntotal))

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(rules):
            continue
        rule = rules[idx].copy()
        rule["similarity_score"] = float(score)
        results.append(rule)

    logger.info(f"Rule search for '{query[:50]}...' -> {len(results)} matches, "
                f"top score: {results[0]['similarity_score']:.3f}" if results else "no matches")
    return results


def keyword_boost_search(query: str, top_k: int = 5,
                         index_dir: str = None) -> List[Dict]:
    """
    Enhanced search: semantic similarity + keyword overlap boost.
    This gives better results for exact-match business terms.
    """
    semantic_results = search_rules(query, top_k=top_k * 2, index_dir=index_dir)

    if not semantic_results:
        return []

    query_words = _tokenize_text(query)

    # Boost score based on keyword overlap
    for rule in semantic_results:
        rule_keywords = _tokenize_text(" ".join(rule.get("keywords", [])))
        rule_text_words = _tokenize_text(rule.get("search_text", ""))
        overlap = len(query_words & (rule_keywords | rule_text_words))
        # Boost: up to 0.2 bonus for keyword overlap
        boost = min(overlap * 0.05, 0.2)
        rule["final_score"] = rule["similarity_score"] + boost

    # Re-sort by boosted score
    semantic_results.sort(key=lambda x: x["final_score"], reverse=True)

    return semantic_results[:top_k]


def evaluate_match_confidence(results: List[Dict],
                              min_score: float = 0.55,
                              min_margin: float = 0.06) -> Dict[str, float]:
    """
    Evaluate confidence of retrieved rules.
    Returns metadata used to decide whether to trust retrieval context.
    """
    if not results:
        return {"is_confident": False, "top_score": 0.0, "second_score": 0.0, "margin": 0.0}
    top = float(results[0].get("final_score", results[0].get("similarity_score", 0.0)))
    second = float(results[1].get("final_score", results[1].get("similarity_score", 0.0))) if len(results) > 1 else 0.0
    margin = top - second
    high_score_gate = top >= 0.75
    margin_gate = top >= float(min_score) and margin >= float(min_margin)
    is_confident = bool(high_score_gate or margin_gate)
    gate = "high_score" if high_score_gate else ("margin_gate" if margin_gate else "rejected")
    return {
        "is_confident": is_confident,
        "top_score": top,
        "second_score": second,
        "margin": margin,
        "gate": gate,
    }


def _tokenize_text(text: str) -> set:
    """Normalize text into stable lowercase tokens for overlap scoring."""
    if not text:
        return set()
    stop_words = {
        "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "by",
        "with", "is", "are", "this", "that", "show", "get", "list",
    }
    tokens = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    return {t for t in tokens if t not in stop_words}


def get_best_rule(query: str, threshold: float = 0.3,
                  index_dir: str = None) -> Optional[Dict]:
    """
    Get the single best matching rule for a query.
    Returns None if similarity is below threshold.
    """
    results = keyword_boost_search(query, top_k=1, index_dir=index_dir)
    if results and results[0].get("final_score", 0) >= threshold:
        return results[0]
    return None


def is_index_built(index_dir: str = None) -> bool:
    """Check if FAISS index exists on disk."""
    from config import FAISS_INDEX_PATH
    if index_dir is None:
        index_dir = FAISS_INDEX_PATH
    index_exists = os.path.exists(os.path.join(index_dir, "rules.index"))
    rules_exists = (
        os.path.exists(os.path.join(index_dir, "rules.json")) or
        os.path.exists(os.path.join(index_dir, "rules.pkl"))
    )
    return index_exists and rules_exists
