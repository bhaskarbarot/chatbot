"""
Chat Memory System.
Maintains conversation history with:
  1. Short-term: Last 3 exchanges for immediate follow-up resolution
  2. LLM Summary: Compact 4-line AI-generated summary (runs in background thread)
  3. Context matching: Determines if new query relates to last conversation

LLM summarization runs asynchronously after each exchange — it does not block
the current response. The summary is ready for the NEXT query.
"""
import re
import threading
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from utils.logger import get_logger

logger = get_logger("chat_memory")


class ConversationSummarizer:
    """
    Uses a small Ollama LLM to summarize the last 3 chat exchanges into a
    compact 4-line context block with topics, counts, and KPIs.
    Falls back to keyword extraction if the LLM is unavailable.
    """

    def __init__(self):
        self._llm = None
        self._lock = threading.Lock()

    def _get_llm(self):
        """Lazy LLM initialization (thread-safe)."""
        with self._lock:
            if self._llm is None:
                try:
                    from config import OLLAMA_BASE_URL, OLLAMA_SUMMARY_MODEL
                    from langchain_ollama import ChatOllama
                    self._llm = ChatOllama(
                        model=OLLAMA_SUMMARY_MODEL,
                        base_url=OLLAMA_BASE_URL,
                        temperature=0,
                        num_predict=200,
                        timeout=15,
                    )
                except Exception as e:
                    logger.warning(f"ConversationSummarizer LLM init failed: {e}")
                    self._llm = None
        return self._llm

    def summarize(self, exchanges: List[Dict]) -> str:
        """
        Summarize last exchanges into max 4 lines with keywords, counts, and KPIs.
        Format: Topic | Count/Value | Status
        """
        if not exchanges:
            return ""

        conv_parts = []
        for ex in exchanges[-1:]:
            user = ex.get("user", "")[:200]
            bot = ex.get("bot_summary", "")[:300]
            kws = ", ".join(ex.get("keywords", [])[:6])
            coll = ex.get("collection") or ""
            conv_parts.append(f"User: {user}\nResponse: {bot}\nCollection: {coll} | Keywords: {kws}")

        conv_text = "\n\n".join(conv_parts)
        prompt = (
            "Summarize this CRM conversation in AT MOST 4 lines.\n"
            "Include: main topics, important counts or numbers found, key KPIs and entities.\n"
            "Format each line as: Topic | Count/Value | Status/Detail\n\n"
            f"Conversation:\n{conv_text}\n\n"
            "Summary (4 lines max, include specific numbers and keywords):"
        )

        try:
            llm = self._get_llm()
            if llm is None:
                raise RuntimeError("LLM not available")
            result = llm.invoke(prompt)
            summary = result.content.strip()
            lines = [ln.strip() for ln in summary.splitlines() if ln.strip()][:4]
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Summarizer LLM failed, using keyword fallback: {e}")
            return self._keyword_summary(exchanges)

    def _keyword_summary(self, exchanges: List[Dict]) -> str:
        """Keyword-based fallback summary when LLM is unavailable."""
        lines = []
        for ex in exchanges[-1:]:
            kws = ", ".join(ex.get("keywords", [])[:5])
            entity = ex.get("entity") or {}
            entity_str = f" ({entity.get('name', '')})" if entity.get("name") else ""
            coll = ex.get("collection") or "CRM"
            bot = ex.get("bot_summary", "")[:80]
            lines.append(f"Topic: {coll}{entity_str} | Keywords: {kws} | Response: {bot}")
        return "\n".join(lines[:4])

    def is_related(self, new_query: str, last_summary: str) -> bool:
        """
        Use LLM to decide if new_query is a follow-up or continuation of last_summary.
        Returns True if related, False if a completely different topic.
        """
        if not last_summary:
            return False

        prompt = (
            "You are a CRM assistant. Decide if the new query is a FOLLOW-UP or CONTINUATION "
            "of the previous conversation.\n\n"
            f"Previous conversation summary:\n{last_summary}\n\n"
            f"New query: {new_query}\n\n"
            "Answer ONLY 'YES' if this is a follow-up/related query, or 'NO' if it is a completely new topic."
        )

        try:
            llm = self._get_llm()
            if llm is None:
                raise RuntimeError("LLM not available")
            result = llm.invoke(prompt)
            answer = result.content.strip().upper()
            return answer.startswith("YES")
        except Exception as e:
            logger.debug(f"Context match LLM failed, using keyword fallback: {e}")
            return self._keyword_is_related(new_query, last_summary)

    def _keyword_is_related(self, new_query: str, summary: str) -> bool:
        """Keyword overlap fallback for context matching."""
        _stop = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                 "have", "has", "had", "do", "does", "did", "will", "would",
                 "can", "could", "to", "of", "in", "for", "on", "with",
                 "give", "show", "get", "me", "all", "and", "or", "from",
                 "that", "this", "those", "these", "it", "its"}
        summary_words = {w for w in re.findall(r'\w+', summary.lower()) if w not in _stop and len(w) > 2}
        query_words = {w for w in re.findall(r'\w+', new_query.lower()) if w not in _stop and len(w) > 2}
        common = summary_words & query_words
        return len(common) >= 2


class ChatMemory:
    """
    Manages conversation history with LLM-based summary and smart context matching.

    Key behaviours:
    - Keeps ONLY the last 3 exchanges in short-term memory.
    - After each exchange, triggers background LLM summarization of all 3 exchanges.
    - New queries check keyword overlap with recent history first (fast).
    - Uses LLM context matching only for ambiguous cases.
    - Supports "from that / from this" as follow-up patterns.
    """

    def __init__(self, max_short_term: int = 3, max_context_tokens: int = 2000):
        self.max_short_term = max_short_term
        self.max_context_tokens = max_context_tokens

        # Short-term: last 3 exchanges
        self.short_term: List[Dict] = []

        # LLM-generated 4-line summary (updated in background)
        self.lm_summary: str = ""

        # Legacy summary field (kept for backwards compat / fallback)
        self.summary: str = ""

        # Last entity context: tracks the most recent entity discussed
        self.last_entity: Optional[Dict] = None

        # Topic tracker
        self.current_topic: Optional[str] = None
        self.topic_entity: Optional[str] = None

        # Background summarizer
        self._summarizer = ConversationSummarizer()
        self._summary_thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────

    def add_exchange(self, user_msg: str, bot_response: str,
                     entity: Dict = None, collection: str = None):
        """
        Add a user-bot exchange to memory.
        Triggers background LLM summary refresh when ≥2 exchanges exist.
        """
        keywords = self._extract_keywords(user_msg)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user": user_msg,
            "bot_summary": self._summarize_response(bot_response),
            "keywords": keywords,
            "entity": entity,
            "collection": collection,
        }

        self.short_term.append(entry)

        if entity:
            self.last_entity = entity
            self.topic_entity = entity.get("name")
            self.current_topic = entity.get("type")

        # Keep only last max_short_term exchanges
        if len(self.short_term) > self.max_short_term:
            self.short_term = self.short_term[-self.max_short_term:]

        # Kick off background LLM summary when we have enough exchanges
        if len(self.short_term) >= 1:
            self._start_background_summary()

    def resolve_followup(self, query: str) -> Tuple[str, bool]:
        """
        Determine if a query is a follow-up to the previous topic.
        Returns: (resolved_query, is_followup)

        Examples:
          Previous: "give me all closed won deals"
          Current: "from that which company has most?" -> follow-up = True
          Current: "show me all pending invoices" -> follow-up = False
        """
        if not self.short_term:
            return query, False

        q_lower = query.lower().strip()

        has_own_entity = self._has_entity_reference(query)
        if has_own_entity:
            return query, False

        followup_patterns = [
            r'^(give|show|get|tell)\s+(me\s+)?(his|her|their|its|the)\s+',
            r'^(give|show|get|tell)\s+(me\s+)?(details|info|information|more)',
            r'^(what|how)\s+(about|is|are)\s+(his|her|their|its|the)\s+',
            r'^(and|also|additionally)\s+',
            r'^(more|details|elaborate|expand)',
            r'^(same|this|that)\s+',
            r'^what\s+about\s+',
            r'^from\s+(that|this|above|those|these)\b',  # "from that give me..."
            r'.*\b(summary|analysis|breakdown|categories)\b.*\b(this|that|above|previous)\b.*',
        ]

        is_followup = any(re.match(p, q_lower) for p in followup_patterns)
        if not is_followup:
            is_followup = any(re.search(p, q_lower) for p in followup_patterns[-2:])

        if not is_followup and not has_own_entity:
            generic_starts = ["give me", "show me", "get me", "tell me",
                              "what is", "what are", "details", "more info",
                              "list", "how many"]
            if any(q_lower.startswith(g) for g in generic_starts):
                if self.short_term:
                    last_keywords = set(self.short_term[-1].get("keywords", []))
                    current_keywords = set(self._extract_keywords(query))
                    if len(current_keywords - {"give", "show", "get", "me",
                                                "details", "list", "all"}) < 2:
                        is_followup = True

        if is_followup and self.last_entity:
            entity_name = self.last_entity.get("name", "")
            entity_type = self.last_entity.get("type", "")
            if entity_name and entity_name.lower() not in q_lower:
                resolved = f"{query} of {entity_name}"
                logger.info(f"Follow-up resolved: '{query}' -> '{resolved}' "
                            f"(entity: {entity_name}, type: {entity_type})")
                return resolved, True

        if is_followup and self.get_last_collection():
            # Ensure unresolved follow-ups carry immediate prior module context.
            collection = self.get_last_collection()
            q_words = set(re.findall(r"\w+", q_lower))
            needs_context = not bool(q_words & {
                "deal", "deals", "invoice", "invoices", "company", "companies",
                "contact", "contacts", "task", "tasks", "user", "users",
                "department", "departments", "outreach", "lead", "leads",
                "sales", "meeting", "meetings", "product", "products",
            })
            if needs_context and collection:
                resolved = f"{query} from {collection}"
                return resolved, True
            return query, True

        return query, False

    def is_contextually_related(self, new_query: str) -> Tuple[bool, str]:
        """
        Check if a new query is contextually related to the last conversation.
        Uses fast keyword overlap first; falls back to LLM for ambiguous cases.
        Returns: (is_related, context_hint)
        """
        if not self.short_term:
            return False, ""

        _noise = {"give", "show", "me", "all", "list", "get", "find",
                  "how", "many", "the", "a", "an", "of", "in", "for"}

        q_keywords = set(self._extract_keywords(new_query)) - _noise
        last_keywords: set = set()
        for ex in self.short_term:
            last_keywords.update(ex.get("keywords", []))
        last_keywords -= _noise

        overlap = len(q_keywords & last_keywords)
        total_q_kw = len(q_keywords)

        if total_q_kw > 0 and (overlap / max(1, total_q_kw)) >= 0.4:
            ctx = self.lm_summary or self._build_simple_context()
            return True, ctx

        if overlap == 0 and total_q_kw > 2:
            return False, ""

        # Ambiguous: try LLM context match
        if self.lm_summary:
            is_rel = self._summarizer.is_related(new_query, self.lm_summary)
            if is_rel:
                return True, self.lm_summary
        return False, ""

    def get_context_string(self) -> str:
        """
        Build a context string for prompt injection.
        Uses LLM summary when available, falls back to keyword summary.
        """
        parts = []

        if self.lm_summary:
            parts.append(f"Conversation Context Summary:\n{self.lm_summary}")
        elif self.summary:
            parts.append(f"Conversation Summary: {self.summary}")
        elif self.short_term:
            # Build a quick keyword-based summary
            parts.append("Recent conversation topics:")
            for ex in self.short_term[-1:]:
                user = ex.get("user", "")[:100]
                bot = ex.get("bot_summary", "")[:150]
                parts.append(f"  User: {user}")
                parts.append(f"  Response: {bot}")
                if ex.get("entity"):
                    e = ex["entity"]
                    parts.append(f"  Entity: {e.get('name')} ({e.get('type')})")

        if self.last_entity:
            parts.append(
                f"Current entity in focus: {self.last_entity.get('name')} "
                f"({self.last_entity.get('type')})"
            )

        return "\n".join(parts)

    def get_last_entity(self) -> Optional[Dict]:
        return self.last_entity

    def get_last_collection(self) -> Optional[str]:
        if self.short_term:
            return self.short_term[-1].get("collection")
        return None

    def get_lm_summary(self) -> str:
        return self.lm_summary

    def clear(self):
        """Clear all memory."""
        self.short_term = []
        self.lm_summary = ""
        self.summary = ""
        self.last_entity = None
        self.current_topic = None
        self.topic_entity = None

    def to_dict(self) -> Dict:
        """Serialize memory to dict (for session persistence)."""
        return {
            "short_term": self.short_term,
            "lm_summary": self.lm_summary,
            "summary": self.summary,
            "last_entity": self.last_entity,
            "current_topic": self.current_topic,
            "topic_entity": self.topic_entity,
        }

    def from_dict(self, data: Dict):
        """Restore memory from dict."""
        self.short_term = data.get("short_term", [])
        self.lm_summary = data.get("lm_summary", "")
        self.summary = data.get("summary", "")
        self.last_entity = data.get("last_entity")
        self.current_topic = data.get("current_topic")
        self.topic_entity = data.get("topic_entity")

    # ── Background LLM summary ────────────────────────

    def _start_background_summary(self):
        """Launch background thread to refresh LLM summary (non-blocking)."""
        if self._summary_thread and self._summary_thread.is_alive():
            return  # already running
        self._summary_thread = threading.Thread(
            target=self._refresh_lm_summary_safe,
            daemon=True,
        )
        self._summary_thread.start()

    def _refresh_lm_summary_safe(self):
        """Thread-safe LLM summary refresh."""
        try:
            new_summary = self._summarizer.summarize(self.short_term)
            if new_summary:
                self.lm_summary = new_summary
                logger.debug(f"LM summary refreshed: {new_summary[:80]}")
        except Exception as e:
            logger.debug(f"Background LM summary failed: {e}")

    def _build_simple_context(self) -> str:
        """Build a simple context string from short_term when LM summary is absent."""
        if not self.short_term:
            return ""
        last = self.short_term[-1]
        entity = last.get("entity") or {}
        coll = last.get("collection") or ""
        kws = ", ".join(last.get("keywords", [])[:5])
        name = entity.get("name", "")
        return f"Last query about: {coll}{f' ({name})' if name else ''} | Keywords: {kws}"

    # ── Internal helpers ──────────────────────────────

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract meaningful keywords from text."""
        stop = {"i", "me", "my", "the", "a", "an", "is", "are", "was",
                "were", "be", "been", "being", "have", "has", "had",
                "do", "does", "did", "will", "would", "shall", "should",
                "can", "could", "may", "might", "must", "need", "dare",
                "to", "of", "in", "for", "on", "with", "at", "by",
                "from", "as", "into", "through", "during", "before",
                "after", "above", "below", "between", "and", "but",
                "or", "not", "no", "nor", "so", "yet", "both", "either",
                "this", "that", "these", "those", "it", "its", "what",
                "which", "who", "whom", "how", "when", "where", "why",
                "all", "each", "every", "any", "few", "more", "most",
                "give", "show", "get", "tell", "find", "please"}
        words = re.findall(r'\w+', text.lower())
        return [w for w in words if w not in stop and len(w) > 1]

    def _has_entity_reference(self, query: str) -> bool:
        """Check if query contains its own entity reference (not a pronoun/follow-up)."""
        q_lower = query.lower()
        if re.search(r"\b(this|that|those|these|above|previous)\b", q_lower):
            return False
        if re.search(r'["\'][\w\s]+["\']', query):
            return True

        entity_patterns = [
            r'for\s+(\w+\s+\w+)',
            r'of\s+(\w+\s+\w+)',
            r'company\s*:\s*(\w+)',
            r'deal\s+(ELS\d+)',
            r'invoice\s+(ELSN?\d+)',
            r'SO\d+',
            r'named?\s+(\w+)',
        ]

        if q_lower in ["give me details", "show details", "more details",
                       "tell me more", "what else", "expand", "elaborate"]:
            return False

        words = query.split()
        for i, word in enumerate(words):
            if i > 0 and word[0].isupper() and word.lower() not in {
                "get", "show", "give", "tell", "find", "list", "all",
                "the", "for", "and", "deal", "deals", "invoice", "invoices",
                "company", "contact", "task", "tasks", "sales", "order",
                "user", "meeting", "meetings", "note", "notes", "product",
                "target", "bill", "vendor", "outreach", "region",
            }:
                return True

        return any(re.search(p, query, re.IGNORECASE) for p in entity_patterns)

    def _summarize_response(self, response: str) -> str:
        """Create a short summary of a bot response for memory storage."""
        if not response:
            return ""
        sentences = response.split(".")
        if sentences:
            summary = sentences[0].strip()
            if len(summary) > 150:
                summary = summary[:147] + "..."
            return summary
        return response[:100] + "..." if len(response) > 100 else response

