"""
Response Control Layer — Enhancement 8.

Final gate before and after response formatting. Two responsibilities:

1. get_instructions(enriched_intent) → shape_instructions dict
   Provides shape contract + system prefix injected into formatter prompt.

2. correct_shape(response_str, enriched_intent) → corrected_str
   Post-formatter correction (pure Python, no LLM):
   - yes_no response that is paragraphs long → extract first sentence
   - count response with no number → regex-extract first number found
   - list response that is prose → convert to bullet list
   - single response → truncate to first record description

Thread-safe: stateless — no shared mutable state.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("response_controller")

# ── Shape system prefix template ─────────────────────────────────────────────
_SHAPE_PREFIX_TEMPLATE = (
    "STRICT INSTRUCTION: Your response MUST be shaped as: {shape}. "
    "{limit_instruction}"
    "Do not add extra records, unrelated sections, or unsolicited commentary. "
    "Be concise and direct."
)


class ResponseController:
    """
    Controls the shape of the final response — both before and after
    the formatter runs. Zero LLM calls in this layer.
    """

    def get_instructions(
        self,
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Returns shape_instructions dict to pass into auto_format().
        On any failure: returns safe_default() with empty instructions.
        """
        if not isinstance(enriched_intent, dict):
            return self.safe_default()

        try:
            return self._build_instructions(enriched_intent)
        except Exception as e:
            logger.warning(f"[ResponseController] get_instructions failed: {e}")
            record_event("response_controller_error", {"error": str(e)[:120]})
            return self.safe_default()

    def correct_shape(
        self,
        response: str,
        enriched_intent: Dict[str, Any],
    ) -> str:
        """
        Post-formatter shape correction (pure Python, no LLM).
        On any failure: returns response unchanged.
        """
        if not isinstance(response, str) or not response.strip():
            return response

        if not isinstance(enriched_intent, dict):
            return response

        try:
            return self._apply_correction(response, enriched_intent)
        except Exception as e:
            logger.warning(f"[ResponseController] correct_shape failed: {e}")
            return response

    @classmethod
    def safe_default(cls) -> Dict[str, Any]:
        """Return neutral shape instructions — no enforcement."""
        return {"shape": "auto", "max_items": None, "system_prefix": ""}

    # ── Internal: build instructions ─────────────────────────────────────────

    def _build_instructions(
        self,
        enriched_intent: Dict[str, Any],
    ) -> Dict[str, Any]:
        response_shape = enriched_intent.get("response_shape", "list") or "list"
        result_limit   = enriched_intent.get("result_limit")

        # Map response_shape → output shape label for prompt
        shape_label = _SHAPE_TO_LABEL.get(response_shape, response_shape)

        limit_instruction = ""
        if result_limit is not None:
            limit_instruction = (
                f"Show EXACTLY {result_limit} item(s) — no more, no less. "
            )

        system_prefix = _SHAPE_PREFIX_TEMPLATE.format(
            shape=shape_label,
            limit_instruction=limit_instruction,
        )

        record_event("response_controller_instructions", {
            "shape": response_shape,
            "max_items": result_limit,
        })
        logger.debug(
            f"[ResponseController] shape={response_shape} limit={result_limit}"
        )

        return {
            "shape": response_shape,
            "max_items": result_limit,
            "system_prefix": system_prefix,
        }

    # ── Internal: shape correction ────────────────────────────────────────────

    def _apply_correction(
        self,
        response: str,
        enriched_intent: Dict[str, Any],
    ) -> str:
        response_shape = enriched_intent.get("response_shape", "list") or "list"
        result_limit   = enriched_intent.get("result_limit")

        corrected = response

        if response_shape == "yes_no":
            corrected = self._enforce_yes_no(response)

        elif response_shape in ("number", "count"):
            # FIX 2/5: Only correct if response is truly just a bare number with no label.
            # If the formatter already produced a labeled contextual response
            # (e.g. "**Total:** 80,816.79\nCollection: invoices\nQuery: ..."),
            # leave it unchanged — stripping it to the bare number is wrong.
            stripped_r = response.strip()
            _already_labeled = (
                "**" in stripped_r
                or "Collection:" in stripped_r
                or "Query:" in stripped_r
                or "Total count:" in stripped_r
                or "Total:" in stripped_r
                or "Conversion Rate:" in stripped_r
                or re.match(r'^\d[\d,\.]+\s*$', stripped_r) is None
            )
            if not _already_labeled:
                corrected = self._extract_number_from_response(response)
            # else: keep response unchanged — it already has context

        elif response_shape == "single_value":
            corrected = self._enforce_single(response)

        elif response_shape == "list" and result_limit is not None:
            corrected = self._enforce_list_limit(response, result_limit)

        # For analysis_text / table / greeting: pass through unchanged

        if corrected != response:
            logger.debug(
                f"[ResponseController] shape correction applied: "
                f"shape={response_shape} "
                f"original_len={len(response)} corrected_len={len(corrected)}"
            )
            record_event("response_controller_corrected", {"shape": response_shape})

        return corrected

    # ── Shape-specific enforcers (pure Python) ────────────────────────────────

    def _enforce_yes_no(self, response: str) -> str:
        """
        yes_no response: if more than 2 sentences → keep only first sentence.
        Ensure response starts with Yes/No.
        """
        stripped = response.strip()

        # Already starts with Yes/No → take first 2 sentences max
        if re.match(r'^(yes|no)\b', stripped, re.I):
            sentences = re.split(r'(?<=[.!?])\s+', stripped)
            return " ".join(sentences[:2]).strip()

        # Doesn't start with Yes/No → prepend based on content
        lower = stripped.lower()
        has_yes_signal = any(
            w in lower for w in ("found", "exists", "available", "yes", "there is", "there are")
        )
        has_no_signal = any(
            w in lower for w in ("not found", "no records", "no results",
                                  "couldn't find", "could not find", "no ", "none")
        )

        # Get first sentence for the explanation
        first_sentence = re.split(r'(?<=[.!?])\s+', stripped)[0].strip()

        if has_yes_signal and not has_no_signal:
            return f"Yes. {first_sentence}"
        elif has_no_signal:
            return f"No. {first_sentence}"
        else:
            # Can't determine — return first sentence only
            return first_sentence

    def _enforce_number(self, response: str) -> str:
        """
        count/number response: if response is paragraphs → extract first number.
        """
        stripped = response.strip()

        # Already looks like a count response (short, starts with number)
        if re.match(r'^\d', stripped) or len(stripped.split()) <= 15:
            return stripped

        # Extract first number from the response
        m = re.search(r'\b(\d[\d,]*)\b', stripped)
        if m:
            number_str = m.group(1)
            # Try to find context sentence containing this number
            for sentence in re.split(r'(?<=[.!?])\s+', stripped):
                if number_str in sentence:
                    return sentence.strip()
            return number_str

        return stripped

    def _extract_number_from_response(self, response: str) -> str:
        """Extract number from response text for count/number shapes."""
        text = response.strip()

        # Explicit zero/none patterns
        no_patterns = [
            r'^\s*no\b',
            r'^\s*none\b',
            r'^\s*zero\b',
            r'\bnot found\b',
            r'\bno records\b',
            r'\bno results\b',
            r'\bcould not find\b',
            r"\bcouldn't find\b",
        ]
        for pattern in no_patterns:
            if re.search(pattern, text.lower()):
                return "Total: 0"

        # Extract first number (integer or float)
        match = re.search(r'\b(\d+(?:,\d{3})*(?:\.\d+)?)\b', text)
        if match:
            return f"Total: {match.group(1)}"

        # Written numbers
        written = {
            "one": "1", "two": "2", "three": "3", "four": "4",
            "five": "5", "six": "6", "seven": "7", "eight": "8",
            "nine": "9", "ten": "10"
        }
        for word, num in written.items():
            if re.search(rf'\b{word}\b', text.lower()):
                return f"Total: {num}"

        # Cannot extract — return original response unchanged
        logger.warning(
            f"[ResponseController] cannot extract number from: {text[:80]}"
        )
        return response

    def _enforce_single(self, response: str) -> str:
        """
        single_value: if response has multiple sections → take first section only.
        """
        stripped = response.strip()

        # Split on double newline (section separator)
        sections = re.split(r'\n\n+', stripped)
        if len(sections) > 1:
            return sections[0].strip()

        # Split on numbered list markers
        items = re.split(r'\n\d+[.)]\s+', stripped)
        if len(items) > 1:
            return (items[0] + "\n1. " + items[1]).strip()

        return stripped

    def _enforce_list_limit(self, response: str, limit: int) -> str:
        """
        list + result_limit: if response contains more than N items → trim.
        Detects numbered lists and bullet lists.
        """
        stripped = response.strip()

        # Detect numbered list items: "1. ...", "2. ...", etc.
        numbered = re.split(r'\n(?=\d+[.)]\s)', stripped)
        if len(numbered) > limit + 1:   # +1 for possible header line
            # Keep header (first element if it doesn't start with a number)
            header_parts = []
            item_parts = []
            for part in numbered:
                if re.match(r'^\d+[.)]\s', part.strip()):
                    item_parts.append(part)
                else:
                    header_parts.append(part)
            trimmed_items = item_parts[:limit]
            return "\n".join(header_parts + trimmed_items).strip()

        # Detect bullet list items: "- ...", "• ...", "* ..."
        bullet = re.split(r'\n(?=[-•*]\s)', stripped)
        if len(bullet) > limit + 1:
            header_parts = []
            item_parts = []
            for part in bullet:
                if re.match(r'^[-•*]\s', part.strip()):
                    item_parts.append(part)
                else:
                    header_parts.append(part)
            trimmed_items = item_parts[:limit]
            return "\n".join(header_parts + trimmed_items).strip()

        return stripped


# ── Shape label map ───────────────────────────────────────────────────────────
_SHAPE_TO_LABEL: Dict[str, str] = {
    "yes_no":       "YES or NO answer with one sentence explanation",
    "number":       "a single number with brief context",
    "single_value": "a single record description only",
    "list":         "a clean numbered or bullet list",
    "table":        "a formatted markdown table",
    "analysis_text":"analytical insight paragraphs",
    "greeting":     "a friendly greeting",
    "auto":         "the most appropriate format",
}


# ── Standalone test block ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    controller = ResponseController()
    all_passed = True

    print("=" * 60)
    print("ResponseController — Standalone Tests")
    print("=" * 60)

    # Test 1: yes_no enforcement — multi-paragraph → first sentence
    print("\nTest 1: yes_no correction — multi-paragraph → first sentence")
    long_response = (
        "Yes, we found several open deals in the pipeline. "
        "There are currently 15 open deals across various stages. "
        "The pipeline looks healthy with a good conversion rate. "
        "Here is a breakdown by stage..."
    )
    intent1 = {"response_shape": "yes_no", "result_limit": None}
    corrected1 = controller.correct_shape(long_response, intent1)
    if len(corrected1.split(". ")) <= 2 and corrected1.lower().startswith("yes"):
        print(f"  PASS — corrected: {corrected1!r}")
    else:
        print(f"  FAIL — corrected={corrected1!r}")
        all_passed = False

    # Test 2: number enforcement — verbose → extract number
    print("\nTest 2: number correction — verbose text → number extracted")
    verbose = "Based on the analysis, there are a total of 247 contacts in the system as of today."
    intent2 = {"response_shape": "number", "result_limit": None}
    corrected2 = controller.correct_shape(verbose, intent2)
    if "247" in corrected2:
        print(f"  PASS — number extracted: {corrected2!r}")
    else:
        print(f"  FAIL — corrected={corrected2!r}")
        all_passed = False

    # Test 3: get_instructions — result_limit set
    print("\nTest 3: get_instructions with result_limit=5")
    intent3 = {"response_shape": "list", "result_limit": 5}
    instructions = controller.get_instructions(intent3)
    if instructions["max_items"] == 5 and "5" in instructions["system_prefix"]:
        print(f"  PASS — max_items=5, prefix contains '5'")
    else:
        print(f"  FAIL — {instructions}")
        all_passed = False

    print("\n" + "=" * 60)
    print(f"Result: {'ALL TESTS PASSED ✓' if all_passed else 'SOME TESTS FAILED ✗'}")
    print("=" * 60)
