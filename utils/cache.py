"""
Semantic response cache with Redis backend.

Two-tier lookup:
  1. Exact match  — O(1), zero compute cost
  2. Semantic match — cosine similarity over stored query embeddings,
     only triggered when no exact match exists

The query index is capped at MAX_INDEX_SIZE entries (FIFO eviction) to keep
similarity scan time bounded.  At 500 entries with 384-dim float32 vectors the
scan takes < 1 ms on CPU.
"""
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from config import REDIS_URL, CACHE_TTL_SECONDS, CACHE_SIMILARITY_THRESHOLD
from utils.logger import get_logger

logger = get_logger("cache")

_RESP_PREFIX = "chat:resp:"
_EMBD_PREFIX = "chat:embd:"
_INDEX_KEY = "chat:qidx"
MAX_INDEX_SIZE = 500


def _normalize(query: str) -> str:
    q = (query or "").lower().strip()
    return re.sub(r"\s+", " ", q)


@dataclass
class CacheHit:
    key: str
    score: float
    payload: Dict[str, Any]


class ResponseCache:
    """
    Semantic response cache backed by Redis.

    Falls back to disabled/no-op if Redis is unavailable so the main pipeline
    is never blocked by cache infrastructure.
    """

    def __init__(self):
        self._redis = None
        self._enabled = False
        self._model = None
        self._model_lock = threading.Lock()
        self._init_client()

    # ── Setup ─────────────────────────────────────────

    def _init_client(self):
        try:
            import redis
            client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
            client.ping()
            self._redis = client
            self._enabled = True
            logger.info("Redis semantic cache connected")
        except Exception as exc:
            self._enabled = False
            logger.warning(f"Redis cache disabled: {exc}")

    def _get_model(self):
        """Lazy-load embedding model (same one used by FAISS) — thread-safe."""
        with self._model_lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    from config import EMBEDDING_MODEL
                    self._model = SentenceTransformer(EMBEDDING_MODEL)
                    logger.info("Cache embedding model loaded")
                except Exception as exc:
                    logger.warning(f"Cache embedding model unavailable: {exc}")
        return self._model

    # ── Public API ────────────────────────────────────

    def get_best(self, query: str) -> Optional[CacheHit]:
        """
        Return best cached response for query, or None.

        Priority: exact match > semantic similarity >= CACHE_SIMILARITY_THRESHOLD
        """
        if not self._enabled or not query:
            return None

        q = _normalize(query)

        # ── Tier 1: exact match ───────────────────────
        hit = self._exact_get(q)
        if hit:
            logger.debug(f"Cache exact hit for '{q[:60]}'")
            return hit

        # ── Tier 2: semantic similarity ───────────────
        return self._semantic_get(q)

    def set(self, query: str, value: Dict[str, Any]):
        """Store query → response mapping with embedding for future similarity lookups."""
        if not self._enabled or not query:
            return

        q = _normalize(query)
        resp_key = f"{_RESP_PREFIX}{q}"

        try:
            serialized = json.dumps(value, default=str).encode("utf-8")
            self._redis.setex(resp_key, CACHE_TTL_SECONDS, serialized)

            # Store embedding for semantic lookups
            vec = self._embed(q)
            if vec is not None:
                emb_key = f"{_EMBD_PREFIX}{q}"
                self._redis.setex(emb_key, CACHE_TTL_SECONDS, vec.tobytes())
                self._update_index(q)

        except Exception as exc:
            logger.warning(f"Cache set failed: {exc}")

    def delete(self, query: str):
        if not self._enabled or not query:
            return
        q = _normalize(query)
        try:
            self._redis.delete(f"{_RESP_PREFIX}{q}", f"{_EMBD_PREFIX}{q}")
        except Exception as exc:
            logger.warning(f"Cache delete failed: {exc}")

    def stats(self) -> Dict[str, Any]:
        if not self._enabled:
            return {"enabled": False}
        try:
            index = self._load_index()
            return {"enabled": True, "cached_queries": len(index), "threshold": CACHE_SIMILARITY_THRESHOLD}
        except Exception:
            return {"enabled": True, "cached_queries": "unknown"}

    # ── Internal helpers ──────────────────────────────

    def _embed(self, text: str) -> Optional[np.ndarray]:
        model = self._get_model()
        if model is None:
            return None
        try:
            vec = model.encode([text], normalize_embeddings=True)
            return vec[0].astype(np.float32)
        except Exception as exc:
            logger.warning(f"Cache embed error: {exc}")
            return None

    def _exact_get(self, q_norm: str) -> Optional[CacheHit]:
        try:
            raw = self._redis.get(f"{_RESP_PREFIX}{q_norm}")
            if raw:
                payload = json.loads(raw.decode("utf-8"))
                return CacheHit(key=q_norm, score=1.0, payload=payload)
        except Exception as exc:
            logger.warning(f"Cache exact get failed: {exc}")
        return None

    def _semantic_get(self, q_norm: str) -> Optional[CacheHit]:
        """Find best matching cached query via cosine similarity."""
        query_vec = self._embed(q_norm)
        if query_vec is None:
            return None

        index = self._load_index()
        if not index:
            return None

        # Batch-load all embeddings and compute similarity
        best_score = 0.0
        best_query = None

        # Build batch for vectorized numpy multiply (fast)
        cached_queries: List[str] = []
        cached_vecs: List[np.ndarray] = []

        try:
            pipe = self._redis.pipeline(transaction=False)
            for cq in index:
                pipe.get(f"{_EMBD_PREFIX}{cq}")
            raw_vecs = pipe.execute()

            for cq, raw in zip(index, raw_vecs):
                if raw is None:
                    continue
                vec = np.frombuffer(raw, dtype=np.float32)
                if vec.shape == query_vec.shape:
                    cached_queries.append(cq)
                    cached_vecs.append(vec)

            if cached_vecs:
                matrix = np.stack(cached_vecs)          # (N, D)
                scores = matrix @ query_vec              # cosine sim (already normalized)
                best_idx = int(np.argmax(scores))
                best_score = float(scores[best_idx])
                best_query = cached_queries[best_idx]

        except Exception as exc:
            logger.warning(f"Semantic cache scan failed: {exc}")
            return None

        if best_score < CACHE_SIMILARITY_THRESHOLD or best_query is None:
            return None

        hit = self._exact_get(best_query)
        if hit:
            hit.score = best_score
            logger.info(f"Semantic cache hit: score={best_score:.3f} for '{q_norm[:60]}'")
        return hit

    def _load_index(self) -> List[str]:
        try:
            raw = self._redis.get(_INDEX_KEY)
            if raw:
                return json.loads(raw.decode("utf-8"))
        except Exception:
            pass
        return []

    def _update_index(self, q_norm: str):
        try:
            index = self._load_index()
            if q_norm in index:
                return
            index.append(q_norm)
            if len(index) > MAX_INDEX_SIZE:
                index = index[-MAX_INDEX_SIZE:]  # FIFO eviction
            self._redis.setex(
                _INDEX_KEY,
                CACHE_TTL_SECONDS,
                json.dumps(index).encode("utf-8"),
            )
        except Exception as exc:
            logger.warning(f"Cache index update failed: {exc}")
