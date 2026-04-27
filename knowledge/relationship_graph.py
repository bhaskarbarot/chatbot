"""
Collection Relationship Graph — parsed from connection.txt.

Builds a directed graph of MongoDB foreign-key relationships so the LLM
can generate correct $lookup pipelines and so the orchestrator can detect
which collections need to be joined for a given query.

No hardcoding: every edge is read dynamically from connection.txt at startup.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional, Set, Tuple

from utils.logger import get_logger

logger = get_logger("relationship_graph")

_graph: Optional["RelationshipGraph"] = None
_graph_lock = threading.Lock()


class RelationshipGraph:
    """
    Directed graph of collection relationships.

    Edge:  source_collection.local_field  →  target_collection._id
    """

    def __init__(self):
        # forward: source_coll → [{local_field, foreign_collection, foreign_field}]
        self.forward: Dict[str, List[Dict]] = {}
        # reverse: target_coll → [{source_collection, local_field}]
        self.reverse: Dict[str, List[Dict]] = {}
        # all collections mentioned in the graph
        self.all_collections: Set[str] = set()

    # ── Loading ───────────────────────────────────────

    def load(self, filepath: str) -> "RelationshipGraph":
        """Parse connection.txt and populate the graph."""
        if not os.path.exists(filepath):
            logger.warning(f"connection.txt not found at {filepath}")
            return self

        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()

        # Pattern: source_collection.field[...] -> target_collection._id (ModelName)
        pattern = re.compile(
            r"^([\w]+)\.([\w\[\].]+)\s*->\s*([\w]+)\._id",
            re.MULTILINE,
        )
        edge_count = 0
        for m in pattern.finditer(content):
            src_coll = m.group(1).strip()
            src_field = m.group(2).strip().replace("[]", "").rstrip(".")
            tgt_coll = m.group(3).strip()

            self.all_collections.update([src_coll, tgt_coll])

            # Forward edge
            self.forward.setdefault(src_coll, [])
            if not any(
                e["local_field"] == src_field and e["foreign_collection"] == tgt_coll
                for e in self.forward[src_coll]
            ):
                self.forward[src_coll].append({
                    "local_field": src_field,
                    "foreign_collection": tgt_coll,
                    "foreign_field": "_id",
                })
                edge_count += 1

            # Reverse edge (for "which collections reference X?")
            self.reverse.setdefault(tgt_coll, [])
            self.reverse[tgt_coll].append({
                "source_collection": src_coll,
                "local_field": src_field,
            })

        logger.info(
            f"Relationship graph loaded: {len(self.all_collections)} collections, "
            f"{edge_count} edges from {filepath}"
        )
        return self

    # ── Query helpers ─────────────────────────────────

    def joins_from(self, collection: str) -> List[Dict]:
        """Collections this collection references (outbound FK)."""
        return self.forward.get(collection, [])

    def refs_to(self, collection: str) -> List[Dict]:
        """Collections that reference this collection (inbound FK)."""
        return self.reverse.get(collection, [])

    def find_path(
        self, from_coll: str, to_coll: str, max_depth: int = 4
    ) -> Optional[List[str]]:
        """BFS shortest path between two collections (undirected)."""
        if from_coll == to_coll:
            return [from_coll]

        visited: Set[str] = {from_coll}
        queue: List[Tuple[str, List[str]]] = [(from_coll, [from_coll])]

        while queue:
            current, path = queue.pop(0)
            if len(path) >= max_depth:
                continue

            neighbors: List[str] = []
            for e in self.forward.get(current, []):
                neighbors.append(e["foreign_collection"])
            for e in self.reverse.get(current, []):
                neighbors.append(e["source_collection"])

            for nxt in neighbors:
                if nxt in visited:
                    continue
                new_path = path + [nxt]
                if nxt == to_coll:
                    return new_path
                visited.add(nxt)
                queue.append((nxt, new_path))

        return None  # no path found

    def lookup_spec(self, from_coll: str, to_coll: str) -> Optional[Dict]:
        """
        Return a MongoDB $lookup stage spec to join from_coll → to_coll.
        Returns None if no direct relationship exists.
        """
        # Direct FK: from_coll.field → to_coll._id
        for e in self.forward.get(from_coll, []):
            if e["foreign_collection"] == to_coll:
                return {
                    "from": to_coll,
                    "localField": e["local_field"],
                    "foreignField": "_id",
                    "as": f"{to_coll}_data",
                }
        # Reverse FK: to_coll.field → from_coll._id
        for e in self.forward.get(to_coll, []):
            if e["foreign_collection"] == from_coll:
                return {
                    "from": to_coll,
                    "localField": "_id",
                    "foreignField": e["local_field"],
                    "as": f"{to_coll}_data",
                }
        return None

    def related_collections(self, collection: str, depth: int = 1) -> Set[str]:
        """All collections reachable within `depth` hops."""
        reachable: Set[str] = set()
        frontier = {collection}
        for _ in range(depth):
            next_frontier: Set[str] = set()
            for coll in frontier:
                for e in self.forward.get(coll, []):
                    c = e["foreign_collection"]
                    if c not in reachable:
                        reachable.add(c)
                        next_frontier.add(c)
                for e in self.reverse.get(coll, []):
                    c = e["source_collection"]
                    if c not in reachable:
                        reachable.add(c)
                        next_frontier.add(c)
            frontier = next_frontier
        return reachable

    # ── Prompt context generation ─────────────────────

    def prompt_context(self, collections: List[str], max_edges: int = 12) -> str:
        """
        Build a compact relationship reference string for LLM prompt injection.
        Only includes edges relevant to the given collections.
        """
        if not collections:
            return ""

        coll_set = set(collections)
        lines: List[str] = []
        seen: Set[str] = set()

        for coll in collections:
            for e in self.forward.get(coll, []):
                key = f"{coll}.{e['local_field']}->{e['foreign_collection']}"
                if key not in seen and len(lines) < max_edges:
                    seen.add(key)
                    lines.append(
                        f"  {coll}.{e['local_field']} → {e['foreign_collection']}._id"
                    )
            for e in self.reverse.get(coll, []):
                key = f"{e['source_collection']}.{e['local_field']}->{coll}"
                if key not in seen and len(lines) < max_edges:
                    seen.add(key)
                    lines.append(
                        f"  {e['source_collection']}.{e['local_field']} → {coll}._id"
                    )

        if not lines:
            return ""
        return "COLLECTION JOINS (use $lookup when needed):\n" + "\n".join(lines)

    def detect_required_joins(self, query: str, primary_collection: str) -> List[Dict]:
        """
        Detect which extra collections are likely needed to answer a query
        based on keyword signals (e.g. 'owner name' → need users join).
        Returns list of $lookup specs.
        """
        ql = query.lower()
        lookups: List[Dict] = []
        added: Set[str] = set()

        # Keywords → likely target collections
        signals = [
            (["owner", "owner name", "assigned to", "created by", "sales rep"], "users"),
            (["company name", "account name", "company"], "companies"),
            (["contact name", "contact"], "contacts"),
            (["region", "territory"], "regions"),
            (["technology", "tech stack"], "technologies"),
            (["source", "lead source"], "sources"),
            (["department"], "departments"),
            (["product"], "products"),
            (["campaign"], "campaigns"),
        ]

        for keywords, target in signals:
            if any(kw in ql for kw in keywords) and target != primary_collection:
                spec = self.lookup_spec(primary_collection, target)
                if spec and target not in added:
                    lookups.append(spec)
                    added.add(target)

        return lookups


# ── Singleton accessor ────────────────────────────────

def get_graph() -> RelationshipGraph:
    """Return the singleton RelationshipGraph, loading connection.txt once."""
    global _graph
    if _graph is not None:
        return _graph
    with _graph_lock:
        if _graph is None:
            conn_file = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "connection.txt"
            )
            _graph = RelationshipGraph().load(conn_file)
    return _graph
