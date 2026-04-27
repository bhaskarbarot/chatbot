"""
Rule Parser — Parses business rules files into structured entries.
This is the KEY component that replaces naive RAG chunking.
Instead of embedding random text chunks, we parse each business rule into:
  - intent: what the user is asking
  - collection: which MongoDB collection(s) to query
  - process: exactly how to build the query
  - keywords: key terms for fast matching
"""
import json
import os
import re
from typing import Dict, List, Tuple

from utils.logger import get_logger

logger = get_logger("rule_parser")


def parse_rules_file(filepath: str) -> List[Dict]:
    """
    Parse a rules.txt file into structured rule entries.
    Format:
      business: <intent description>
      process: <query instructions>
    """
    rules = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by "business:" entries
    blocks = re.split(r'\n(?=business:)', content)

    for block in blocks:
        block = block.strip()
        if not block or block.startswith("#"):
            continue

        # Extract business and process
        biz_match = re.search(r'business:\s*(.+?)(?=\nprocess:)', block, re.DOTALL)
        proc_match = re.search(r'process:\s*(.+?)$', block, re.DOTALL)

        if biz_match and proc_match:
            business = biz_match.group(1).strip()
            process = proc_match.group(1).strip()
            # Remove comment lines from process
            process = "\n".join(
                line for line in process.split("\n")
                if not line.strip().startswith("#")
            ).strip()

            rule = _build_rule_entry(business, process)
            rules.append(rule)

    logger.info(f"Parsed {len(rules)} rules from {filepath}")
    return rules


def parse_chatbot_rules_md(filepath: str) -> List[Dict]:
    """
    Parse CHATBOT_QUERY_RULES.md into structured rule entries.
    Format:
      **business:** <intent>
      **process:** <query instructions>
    """
    rules = []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by business entries
    blocks = re.split(r'\n(?=\*\*business:\*\*)', content)

    for block in blocks:
        block = block.strip()
        if not block.startswith("**business:**"):
            continue

        biz_match = re.search(
            r'\*\*business:\*\*\s*(.+?)(?=\n\*\*process:\*\*)', block, re.DOTALL
        )
        proc_match = re.search(
            r'\*\*process:\*\*\s*(.+?)(?=\n---|\Z)', block, re.DOTALL
        )

        if biz_match and proc_match:
            business = biz_match.group(1).strip()
            process = proc_match.group(1).strip()

            rule = _build_rule_entry(business, process)
            rules.append(rule)

    logger.info(f"Parsed {len(rules)} rules from {filepath}")
    return rules


def parse_database_metadata(filepath: str) -> Dict:
    """
    Extract collection metadata from DATABASE_METADATA.md.
    Returns a compact reference dict for prompt injection.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    metadata = {
        "collections": {},
        "critical_rules": [],
        "decision_tree": "",
        "soft_delete_map": {},
        "enum_values": {},
        "date_fields": {},
        "common_mistakes": [],
    }

    # Extract critical rules
    rules_section = re.search(
        r'## CRITICAL RULES.*?\n---', content, re.DOTALL
    )
    if rules_section:
        rules_text = rules_section.group(0)
        for line in rules_text.split("\n"):
            line = line.strip()
            if line and line[0].isdigit() and "." in line[:3]:
                metadata["critical_rules"].append(
                    line.split(".", 1)[1].strip().strip("*").strip()
                )

    # Extract collection names and key fields
    coll_pattern = re.compile(
        r'### \d+\.\s+`(\w+)`\s+.*?\n\n\*\*Purpose:\*\*\s*(.+?)(?=\n\n)',
        re.DOTALL
    )
    for match in coll_pattern.finditer(content):
        coll_name = match.group(1)
        purpose = match.group(2).strip()
        metadata["collections"][coll_name] = {"purpose": purpose}

    # Extract common mistakes
    mistakes_section = re.search(
        r'## COMMON MISTAKES.*?\n---', content, re.DOTALL
    )
    if mistakes_section:
        for line in mistakes_section.group(0).split("\n"):
            line = line.strip()
            if line and line[0].isdigit():
                metadata["common_mistakes"].append(
                    line.split(".", 1)[1].strip().strip("*").strip()
                )

    logger.info(f"Extracted metadata for {len(metadata['collections'])} collections")
    return metadata


def build_all_rules(rules_path: str, chatbot_rules_path: str) -> List[Dict]:
    """
    Parse all rule files and merge into a single deduplicated list.
    """
    rules1 = parse_rules_file(rules_path)
    rules2 = parse_chatbot_rules_md(chatbot_rules_path)

    # Merge and deduplicate by intent similarity
    seen_intents = set()
    merged = []

    for rule in rules1 + rules2:
        key = rule["intent"].lower().strip()
        if key not in seen_intents:
            seen_intents.add(key)
            merged.append(rule)

    logger.info(f"Total unique rules after merge: {len(merged)}")
    return merged


def save_rules(rules: List[Dict], output_path: str):
    """Save parsed rules to JSON."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(rules)} rules to {output_path}")


def load_rules(path: str) -> List[Dict]:
    """Load parsed rules from JSON."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ── Internal helpers ─────────────────────────────────

def _build_rule_entry(business: str, process: str) -> Dict:
    """Build a structured rule entry from business + process text."""
    # Extract collection names from process text
    collections = _extract_collections(process)

    # Extract key fields mentioned
    fields = _extract_fields(process)

    # Extract parameter placeholders [name], [date], etc.
    params = re.findall(r'\[([^\]]+)\]', business)

    # Build search text for embedding
    # Include condensed process hints so retrieval captures constraints
    # like status filters, joins, and date windows.
    process_keywords = " ".join(re.findall(r"[A-Za-z_]{3,}", process))[:1200]
    search_text = (
        f"{business} {' '.join(collections)} {' '.join(fields)} {process_keywords}"
    )

    # Extract keywords for fast matching
    keywords = _extract_keywords(business)

    return {
        "intent": business,
        "process": process,
        "collections": collections,
        "fields": fields,
        "params": params,
        "keywords": keywords,
        "search_text": search_text,
    }


def _extract_collections(text: str) -> List[str]:
    """Extract MongoDB collection names from process text."""
    known = {
        "deals", "invoices", "sales", "companies", "contacts", "users",
        "createtasks", "outreaches", "bills", "vendors", "products",
        "notifications", "emails", "mails", "targets", "commonnotes",
        "notes", "dealsnotes", "contactsnotes", "companynotes", "salesnotes",
        "regions", "campaigns", "departments", "projecttypes",
        "technologies", "technologycategories", "sources", "taxes",
        "meetings", "remotejobs", "payments", "publicleads",
        "activities", "activityevents", "outreachactivities",
        "lead_status", "lifecycle_stage", "dealstagesettings",
        "countryregions", "statuses", "categorys",
    }

    found = []
    text_lower = text.lower()
    # Match backtick-quoted collection names
    backtick = re.findall(r'`(\w+)`', text)
    for name in backtick:
        if name.lower() in known and name not in found:
            found.append(name)

    # Also match plain mentions
    for name in known:
        if name in text_lower and name not in [f.lower() for f in found]:
            found.append(name)

    return found


def _extract_fields(text: str) -> List[str]:
    """Extract field names from process text."""
    # Match backtick-quoted field names and common field patterns
    fields = re.findall(r'`(\w+)`', text)
    # Filter out collection names and operators
    known_colls = {"deals", "invoices", "sales", "companies", "contacts",
                   "users", "createtasks", "outreaches", "bills", "vendors"}
    return [f for f in fields if f not in known_colls and not f.startswith("$")]


def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from business intent text."""
    # Remove parameter placeholders
    clean = re.sub(r'\[.*?\]', '', text)
    # Remove common stop words
    stop = {"get", "for", "this", "the", "a", "an", "of", "in", "to",
            "by", "with", "on", "at", "is", "are", "and", "or", "all",
            "show", "me", "give", "list", "details", "data"}
    words = re.findall(r'\w+', clean.lower())
    return [w for w in words if w not in stop and len(w) > 2]
