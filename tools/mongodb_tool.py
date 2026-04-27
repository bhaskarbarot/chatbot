"""
MongoDB Tool — connects to ECRM database and executes queries.
Handles connection pooling, query execution, and result limiting.
"""
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from config import MONGODB_URI, MONGODB_DB, MAX_MONGO_RESULTS, SOFT_DELETE_MAP, COLLECTION_NAMES
from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("mongodb_tool")
MAX_MONGO_TIME_MS = 15000

# ── Singleton connection ─────────────────────────────
_client: Optional[MongoClient] = None
_db = None
_db_lock = threading.Lock()


def get_db():
    """Get MongoDB database connection (thread-safe singleton)."""
    global _client, _db
    if _db is not None:
        return _db
    with _db_lock:
        if _db is None:
            try:
                _client = MongoClient(
                    MONGODB_URI,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=10000,
                    maxPoolSize=10
                )
                _client.admin.command("ping")
                _db = _client[MONGODB_DB]
                logger.info(f"Connected to MongoDB: {MONGODB_DB}")
            except PyMongoError as e:
                logger.error(f"MongoDB connection failed: {e}")
                raise
    return _db


def close_db():
    """Close MongoDB connection."""
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None


def execute_find(collection: str, filter_dict: dict,
                 projection: dict = None, sort: list = None,
                 limit: int = None, skip: int = 0) -> List[Dict]:
    """Execute a find query on a collection."""
    if not _is_valid_collection(collection):
        logger.error(f"Invalid collection in find: {collection}")
        record_event("mongo_invalid_collection", {"operation": "find", "collection": collection})
        return []
    db = get_db()
    try:
        filter_dict = _normalize_query_literals(filter_dict)
        # Auto-add soft delete filter
        filter_dict = _add_soft_delete(collection, filter_dict)

        cursor = db[collection].find(filter_dict, projection, max_time_ms=MAX_MONGO_TIME_MS)
        if sort:
            cursor = cursor.sort(sort)
        if skip:
            cursor = cursor.skip(skip)
        cursor = cursor.limit(limit or MAX_MONGO_RESULTS)

        results = []
        for doc in cursor:
            results.append(_serialize_doc(doc))

        logger.info(f"find({collection}) -> {len(results)} docs")
        record_event("mongo_query", {"operation": "find", "collection": collection, "rows": len(results)})
        return results
    except PyMongoError as e:
        logger.error(f"find error on {collection}: {e}")
        record_event("mongo_error", {"operation": "find", "collection": collection, "error": str(e)})
        return []


def execute_aggregate(collection: str, pipeline: list) -> List[Dict]:
    """Execute an aggregation pipeline."""
    if not _is_valid_collection(collection):
        logger.error(f"Invalid collection in aggregate: {collection}")
        record_event("mongo_invalid_collection", {"operation": "aggregate", "collection": collection})
        return []
    db = get_db()
    try:
        pipeline = _normalize_query_literals(pipeline)
        pipeline = _sanitize_pipeline(pipeline)
        # Ensure soft-delete filters are always applied when configured
        pipeline = _inject_soft_delete_match(collection, pipeline)

        # Add result limit at the end if not already present
        has_limit = any("$limit" in stage for stage in pipeline)
        if not has_limit:
            pipeline.append({"$limit": MAX_MONGO_RESULTS})

        cursor = db[collection].aggregate(
            pipeline, allowDiskUse=True, maxTimeMS=MAX_MONGO_TIME_MS
        )
        results = [_serialize_doc(doc) for doc in cursor]

        logger.info(f"aggregate({collection}) -> {len(results)} docs, pipeline stages: {len(pipeline)}")
        record_event("mongo_query", {
            "operation": "aggregate",
            "collection": collection,
            "rows": len(results),
            "stages": len(pipeline),
        })
        return results
    except PyMongoError as e:
        logger.error(f"aggregate error on {collection}: {e}")
        record_event("mongo_error", {"operation": "aggregate", "collection": collection, "error": str(e)})
        return []


def execute_count(collection: str, filter_dict: dict) -> int:
    """Count documents matching a filter."""
    if not _is_valid_collection(collection):
        logger.error(f"Invalid collection in count: {collection}")
        record_event("mongo_invalid_collection", {"operation": "count", "collection": collection})
        return 0
    db = get_db()
    try:
        filter_dict = _normalize_query_literals(filter_dict)
        filter_dict = _add_soft_delete(collection, filter_dict)
        count = db[collection].count_documents(filter_dict, maxTimeMS=MAX_MONGO_TIME_MS)
        logger.info(f"count({collection}) -> {count}")
        record_event("mongo_query", {"operation": "count", "collection": collection, "rows": count})
        return count
    except PyMongoError as e:
        logger.error(f"count error on {collection}: {e}")
        record_event("mongo_error", {"operation": "count", "collection": collection, "error": str(e)})
        return 0


def execute_distinct(collection: str, field: str,
                     filter_dict: dict = None) -> List:
    """Get distinct values of a field."""
    if not _is_valid_collection(collection):
        logger.error(f"Invalid collection in distinct: {collection}")
        record_event("mongo_invalid_collection", {"operation": "distinct", "collection": collection})
        return []
    db = get_db()
    try:
        filter_dict = _normalize_query_literals(filter_dict or {})
        filter_dict = _add_soft_delete(collection, filter_dict or {})
        results = db[collection].distinct(field, filter_dict, maxTimeMS=MAX_MONGO_TIME_MS)
        logger.info(f"distinct({collection}.{field}) -> {len(results)} values")
        record_event("mongo_query", {"operation": "distinct", "collection": collection, "rows": len(results)})
        return results
    except PyMongoError as e:
        logger.error(f"distinct error: {e}")
        record_event("mongo_error", {"operation": "distinct", "collection": collection, "error": str(e)})
        return []


def execute_query_plan(plan: Dict) -> Any:
    """
    Execute a structured query plan.
    Plan format:
    {
        "type": "find" | "aggregate" | "count" | "distinct" | "multi_step",
        "collection": "deals",
        "filter": {...},
        "projection": {...},
        "sort": [["field", 1]],
        "limit": 10,
        "pipeline": [...],       # for aggregate
        "field": "stage",        # for distinct
        "steps": [...]           # for multi_step
    }
    """
    plan = _sanitize_plan(plan or {})
    qtype = plan.get("type", "find")
    collection = plan.get("collection", "")

    if not collection and qtype != "multi_step":
        logger.error("No collection specified in query plan")
        return []

    if qtype == "find":
        return execute_find(
            collection,
            plan.get("filter", {}),
            plan.get("projection"),
            plan.get("sort"),
            plan.get("limit")
        )
    elif qtype == "aggregate":
        return execute_aggregate(collection, plan.get("pipeline", []))
    elif qtype == "count":
        return execute_count(collection, plan.get("filter", {}))
    elif qtype == "distinct":
        return execute_distinct(
            collection, plan.get("field", "_id"), plan.get("filter", {})
        )
    elif qtype == "multi_step":
        return _execute_multi_step(plan.get("steps", []))
    else:
        logger.error(f"Unknown query type: {qtype}")
        return []


def _sanitize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize malformed planner outputs into executable shape."""
    out = dict(plan)
    qtype = out.get("type")
    if qtype == "find":
        filt = out.get("filter")
        if not isinstance(filt, dict):
            filt = {}
        # Move accidental sort/limit/projection keys out of filter object.
        for bad_key in ("$sort", "$limit", "$project"):
            if bad_key in filt:
                if bad_key == "$sort" and "sort" not in out:
                    out["sort"] = _normalize_sort_spec(filt.pop("$sort"))
                elif bad_key == "$limit" and "limit" not in out:
                    try:
                        out["limit"] = int(filt.pop("$limit"))
                    except Exception:
                        filt.pop("$limit", None)
                else:
                    filt.pop(bad_key, None)
        out["filter"] = filt
        if "sort" in out:
            out["sort"] = _normalize_sort_spec(out.get("sort"))
    elif qtype == "aggregate" and isinstance(out.get("pipeline"), list):
        fixed_pipeline = []
        for stage in out["pipeline"]:
            if not isinstance(stage, dict) or len(stage) != 1:
                continue
            op, payload = next(iter(stage.items()))
            if op == "$sort":
                fixed_pipeline.append({"$sort": _normalize_sort_object(payload)})
            else:
                fixed_pipeline.append({op: payload})
        out["pipeline"] = fixed_pipeline
    return out


def _normalize_sort_spec(sort_spec: Any) -> Any:
    """Normalize find-sort spec into pymongo accepted list of tuples."""
    if isinstance(sort_spec, list):
        if sort_spec and all(isinstance(x, (list, tuple)) and len(x) == 2 for x in sort_spec):
            return [(str(f), int(d)) for f, d in sort_spec]
    if isinstance(sort_spec, dict):
        return [(str(k), int(v)) for k, v in sort_spec.items()]
    return [("createdAt", -1)]


def _normalize_sort_object(sort_payload: Any) -> Dict[str, int]:
    """Normalize aggregate $sort stage payload to object."""
    if isinstance(sort_payload, dict):
        return {str(k): int(v) for k, v in sort_payload.items()}
    if isinstance(sort_payload, list):
        normalized = {}
        for item in sort_payload:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                normalized[str(item[0])] = int(item[1])
        if normalized:
            return normalized
    return {"createdAt": -1}


def find_entity_id(collection: str, search_field: str,
                   search_value: str) -> Optional[str]:
    """
    Find an entity's _id by searching a field with regex match.
    Used to resolve names to ObjectIds for cross-collection queries.
    """
    db = get_db()
    try:
        filter_dict = _add_soft_delete(collection, {
            search_field: {"$regex": search_value, "$options": "i"}
        })
        doc = db[collection].find_one(filter_dict, {"_id": 1})
        if doc:
            return str(doc["_id"])
        return None
    except PyMongoError as e:
        logger.error(f"find_entity_id error: {e}")
        return None


def resolve_name_to_id(name: str, entity_type: str) -> Optional[str]:
    """
    Resolve a name string to its MongoDB ObjectId.
    entity_type: 'user', 'company', 'contact', 'deal', 'region',
                 'source', 'technology', 'campaign', 'product'
    """
    mapping = {
        "user": ("users", "name"),
        "company": ("companies", "companyName"),
        "contact": ("contacts", "firstName"),  # will also check lastName
        "deal": ("deals", "name"),
        "region": ("regions", "regionName"),
        "source": ("sources", "sourceName"),
        "technology": ("technologies", "name"),
        "campaign": ("campaigns", "campaignName"),
        "product": ("products", "name"),
        "vendor": ("vendors", "companyName"),
        "department": ("departments", "name"),
        "projecttype": ("projecttypes", "name"),
    }

    if entity_type not in mapping:
        return None

    coll, field = mapping[entity_type]
    result = find_entity_id(coll, field, name)

    # For contacts, also try lastName or full name
    if not result and entity_type == "contact":
        result = find_entity_id("contacts", "lastName", name)
        if not result:
            # Try matching full name via aggregation
            db = get_db()
            pipeline = [
                {"$addFields": {
                    "fullName": {"$concat": ["$firstName", " ", "$lastName"]}
                }},
                {"$match": {
                    "fullName": {"$regex": name, "$options": "i"},
                    "deleted": False
                }},
                {"$limit": 1},
                {"$project": {"_id": 1}}
            ]
            results = list(db["contacts"].aggregate(pipeline))
            if results:
                result = str(results[0]["_id"])

    return result


def search_by_keyword(collection: str, keyword: str,
                      search_fields: List[str] = None,
                      limit: int = 10) -> List[Dict]:
    """
    Generic keyword search across multiple fields in a collection.
    """
    db = get_db()
    if search_fields is None:
        # Default search fields per collection
        defaults = {
            "companies": ["companyName", "email", "country", "industry"],
            "contacts": ["firstName", "lastName", "email", "jobTitle"],
            "deals": ["name", "stage"],
            "invoices": ["invoice_number", "companyName"],
            "sales": ["sales_number"],
            "users": ["name", "email"],
            "products": ["name", "description_short"],
            "outreaches": ["name", "email", "designation", "country"],
            "vendors": ["companyName", "email"],
            "createtasks": ["Task"],
        }
        search_fields = defaults.get(collection, ["name"])

    or_conditions = [
        {field: {"$regex": keyword, "$options": "i"}}
        for field in search_fields
    ]

    filter_dict = _add_soft_delete(collection, {"$or": or_conditions})

    try:
        cursor = db[collection].find(filter_dict).limit(limit)
        return [_serialize_doc(doc) for doc in cursor]
    except PyMongoError as e:
        logger.error(f"search error: {e}")
        return []


def get_collection_sample(collection: str, limit: int = 3) -> List[Dict]:
    """Get a sample of documents from a collection (for suggestions)."""
    db = get_db()
    try:
        filter_dict = _add_soft_delete(collection, {})
        cursor = db[collection].find(filter_dict).limit(limit)
        return [_serialize_doc(doc) for doc in cursor]
    except PyMongoError as e:
        logger.error(f"sample error: {e}")
        return []


def get_collection_stats(collection: str) -> Dict:
    """Get basic stats about a collection."""
    db = get_db()
    try:
        filter_dict = _add_soft_delete(collection, {})
        count = db[collection].count_documents(filter_dict)
        return {"collection": collection, "count": count}
    except Exception as e:
        logger.error(f"get_collection_stats error on {collection}: {e}")
        return {"collection": collection, "count": 0}


# ── Internal helpers ─────────────────────────────────

def _add_soft_delete(collection: str, filter_dict: dict) -> dict:
    """Auto-add soft delete filter for collections that need it."""
    if collection in SOFT_DELETE_MAP:
        sd = SOFT_DELETE_MAP[collection]
        field = sd["field"]
        value = sd["value"]
        if field not in filter_dict:
            filter_dict[field] = value
    return filter_dict


def _inject_soft_delete_match(collection: str, pipeline: list) -> list:
    """Prepend/merge soft-delete match in aggregation pipelines."""
    if collection not in SOFT_DELETE_MAP:
        return pipeline

    sd = SOFT_DELETE_MAP[collection]
    field = sd["field"]
    value = sd["value"]
    if pipeline and isinstance(pipeline[0], dict) and "$match" in pipeline[0]:
        match_stage = pipeline[0]["$match"]
        if field not in match_stage:
            match_stage[field] = value
        return pipeline

    return [{"$match": {field: value}}] + pipeline


def _is_valid_collection(collection: str) -> bool:
    """Validate collection name against configured and discovered collections."""
    if not isinstance(collection, str) or not collection:
        return False
    if collection in COLLECTION_NAMES:
        return True
    try:
        db = get_db()
        return collection in db.list_collection_names()
    except Exception:
        return False


def _serialize_doc(doc: dict) -> dict:
    """Convert a MongoDB document to JSON-serializable dict."""
    result = {}
    for key, val in doc.items():
        if isinstance(val, ObjectId):
            result[key] = str(val)
        elif isinstance(val, datetime):
            result[key] = val.isoformat()
        elif isinstance(val, bytes):
            result[key] = val.decode("utf-8", errors="replace")
        elif isinstance(val, dict):
            result[key] = _serialize_doc(val)
        elif isinstance(val, list):
            result[key] = [
                _serialize_doc(item) if isinstance(item, dict)
                else str(item) if isinstance(item, ObjectId)
                else item.isoformat() if isinstance(item, datetime)
                else item
                for item in val
            ]
        else:
            result[key] = val
    return result


def _normalize_query_literals(obj: Any) -> Any:
    """Convert extended JSON wrappers from LLM plans into Python literals."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$date"} and isinstance(obj["$date"], str):
            raw = obj["$date"].strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return obj["$date"]
        if set(obj.keys()) == {"$oid"} and isinstance(obj["$oid"], str):
            oid = obj["$oid"].strip()
            if re.match(r"^[0-9a-fA-F]{24}$", oid):
                return ObjectId(oid)
            return oid
        return {k: _normalize_query_literals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_query_literals(item) for item in obj]
    if isinstance(obj, str):
        parsed = _parse_iso_datetime(obj)
        return parsed if parsed is not None else obj
    return obj


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    """Parse ISO datetime strings safely."""
    candidate = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", candidate):
        return None
    candidate = candidate.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _execute_multi_step(steps: list) -> Any:
    """Execute multi-step queries where later steps depend on earlier ones."""
    context = {}
    final_result = []

    for step in steps:
        step_name = step.get("name", "step")
        step_type = step.get("type", "find")

        # Resolve any {ref} placeholders in filters
        if "filter" in step:
            step["filter"] = _resolve_refs(step["filter"], context)
        if "pipeline" in step:
            step["pipeline"] = _resolve_refs(step["pipeline"], context)

        result = execute_query_plan(step)

        # Store result in context for later steps
        if result:
            if isinstance(result, list) and len(result) > 0:
                context[step_name] = result
                # Also store first result's _id for convenience
                if isinstance(result[0], dict) and "_id" in result[0]:
                    context[f"{step_name}_id"] = result[0]["_id"]
            else:
                context[step_name] = result

        # The last step's result is the final result
        final_result = result

    return final_result


def _resolve_refs(obj: Any, context: dict) -> Any:
    """Resolve {ref.field} placeholders in query objects."""
    if isinstance(obj, str):
        match = re.match(r'\{(\w+)\.(\w+)\}', obj)
        if match:
            ref_name, ref_field = match.groups()
            ref_data = context.get(ref_name)
            if ref_data and isinstance(ref_data, list) and len(ref_data) > 0:
                val = ref_data[0].get(ref_field)
                if val and re.match(r'^[0-9a-fA-F]{24}$', str(val)):
                    return ObjectId(val)
                return val
            return context.get(f"{ref_name}_{ref_field}", obj)
        # Check for ObjectId pattern
        if re.match(r'^[0-9a-fA-F]{24}$', obj):
            return ObjectId(obj)
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_refs(v, context) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_refs(item, context) for item in obj]
    return obj


def _sanitize_pipeline(pipeline: list) -> list:
    """Best-effort sanitizer for near-valid LLM aggregation pipelines."""
    sanitized = []
    for stage in pipeline:
        if not isinstance(stage, dict) or len(stage) != 1:
            continue
        op, payload = next(iter(stage.items()))
        if op == "$project":
            sanitized.append({op: _sanitize_expression_tree(payload)})
        elif op == "$group":
            sanitized.append({op: _sanitize_group_stage(payload)})
        else:
            sanitized.append({op: _sanitize_expression_tree(payload)})
    return sanitized


def _sanitize_group_stage(group_spec: Any) -> Dict[str, Any]:
    """Ensure $group fields use valid accumulator expressions."""
    if not isinstance(group_spec, dict):
        return {"_id": None}
    result: Dict[str, Any] = {}
    accumulators = {
        "$sum", "$avg", "$min", "$max", "$first", "$last",
        "$push", "$addToSet", "$count"
    }
    for key, value in group_spec.items():
        value = _sanitize_expression_tree(value)
        if key == "_id":
            result[key] = value
            continue
        if isinstance(value, dict) and len(value) == 1:
            op = next(iter(value.keys()))
            if op in accumulators:
                result[key] = value
                continue
        # Convert invalid non-accumulator field into deterministic accumulator.
        result[key] = {"$first": value}
    if "_id" not in result:
        result["_id"] = None
    return result


def _sanitize_expression_tree(node: Any) -> Any:
    """Fix common malformed expression trees emitted by LLM plans."""
    if isinstance(node, list):
        return [_sanitize_expression_tree(item) for item in node]
    if isinstance(node, dict):
        cleaned = {k: _sanitize_expression_tree(v) for k, v in node.items()}
        # Fix malformed $dateDiff style:
        # {"$dateDiff": {"startDate": ...}, "unit": "day"} -> {"$dateDiff": {"startDate": ..., "unit": "day"}}
        if "$dateDiff" in cleaned and "unit" in cleaned:
            date_diff = cleaned.get("$dateDiff")
            if isinstance(date_diff, dict):
                merged = dict(date_diff)
                merged["unit"] = cleaned["unit"]
                return {"$dateDiff": merged}
        # Fix malformed $dateFromString wrapper if extra keys leak in.
        if "$dateFromString" in cleaned and len(cleaned) > 1:
            return {"$dateFromString": cleaned.get("$dateFromString")}
        # Fix malformed $subtract payloads.
        if "$subtract" in cleaned:
            sub_expr = cleaned.get("$subtract")
            if not isinstance(sub_expr, list):
                sub_expr = [sub_expr, 0]
            elif len(sub_expr) == 1:
                sub_expr = [sub_expr[0], 0]
            elif len(sub_expr) > 2:
                sub_expr = sub_expr[:2]
            return {"$subtract": sub_expr}
        # Guard divide-by-zero for literal denominator.
        if "$divide" in cleaned:
            div_expr = cleaned.get("$divide")
            if isinstance(div_expr, list) and len(div_expr) >= 2:
                numerator = div_expr[0]
                denominator = div_expr[1]
                if denominator == 0:
                    denominator = 1
                return {"$divide": [numerator, denominator]}
        # Keep single-operator expression clean when malformed extras are present.
        op_keys = [k for k in cleaned if isinstance(k, str) and k.startswith("$")]
        if len(op_keys) == 1 and len(cleaned) > 1:
            return {op_keys[0]: cleaned[op_keys[0]]}
        return cleaned
    return node
