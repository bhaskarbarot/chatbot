"""
Response Formatter.
Detects user intent (list, table, count, summary, detail) and formats accordingly.
Every response starts with a 3-line executive summary.
Aggregation results are rendered as markdown tables.
"""
import re
import json
from typing import Any, Dict, List, Optional
from datetime import datetime
from utils.masking import mask_dict, mask_response

# ── Intent detection keywords ────────────────────────
LIST_KEYWORDS = ["list", "which", "are", "all", "show me all", "find all",
                 "get all", "give me all", "show all"]
TABLE_KEYWORDS = ["table", "tabular", "tab format", "spreadsheet",
                  "in table", "as table", "table format"]
COUNT_KEYWORDS = ["how many", "count", "total number", "number of",
                  "count of", "how much"]
SUMMARY_KEYWORDS = ["summary", "summarize", "overview", "brief", "short"]
DETAIL_KEYWORDS = ["details", "detail", "everything", "complete", "full",
                   "all details", "in detail", "elaborate"]


def detect_format_intent(query: str) -> str:
    """Detect what format the user wants from their query text."""
    q = query.lower().strip()
    if any(kw in q for kw in TABLE_KEYWORDS):
        return "table"
    if any(kw in q for kw in COUNT_KEYWORDS):
        return "count"
    if any(kw in q for kw in DETAIL_KEYWORDS):
        return "detail"
    if any(kw in q for kw in SUMMARY_KEYWORDS):
        return "summary"
    if any(kw in q for kw in LIST_KEYWORDS):
        return "list"
    return "auto"


def format_date(val: Any) -> str:
    """Format a date value to readable string."""
    if val is None:
        return "N/A"
    if isinstance(val, datetime):
        return val.strftime("%d %b %Y")
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.strftime("%d %b %Y")
        except Exception:
            return val
    return str(val)


def format_currency(val: Any) -> str:
    """Format number as currency."""
    if val is None:
        return "N/A"
    try:
        num = float(val)
        if num >= 1_000_000:
            return f"${num:,.0f}"
        elif num >= 1_000:
            return f"${num:,.2f}"
        else:
            return f"${num:.2f}"
    except (ValueError, TypeError):
        return str(val)


def format_value(key: str, val: Any) -> str:
    """Smart format a value based on its key name."""
    if val is None or val == "" or val == []:
        return "N/A"

    date_keys = {"createdAt", "updatedAt", "dealWonAt", "dealLostAt",
                 "closeDate", "invoice_date", "due_date", "payment_date",
                 "sales_date", "leadWonAt", "leadLostAt", "lastBusinessDate",
                 "inActiveSince", "birthday", "start", "end", "billDate",
                 "dueDate", "interestedDate", "convertedDate", "ReminderDate",
                 "closedDate", "lastLogin"}

    currency_keys = {"grand_total", "grand_total_in_usd", "grandtotal_in_usd",
                     "subtotal", "subtotal_in_usd", "total_price",
                     "total_price_in_usd", "unit_price", "unit_cost",
                     "netPayableAmount", "targetInUSD", "totalRevenue",
                     "revenue", "totalPayable", "totalRevenueUSD",
                     "paidSoFar", "remainingAmount", "totalBilledUSD",
                     "totalPaidUSD", "totalOutstanding", "achieved",
                     "totalValue", "totalValueUSD", "avgDealSize",
                     "totalDealValue", "totalPipelineValue",
                     "total_revenue_usd", "total_value"}

    if key in date_keys:
        return format_date(val)
    if key in currency_keys:
        return format_currency(val)
    if isinstance(val, bool):
        return "Yes" if val else "No"
    if isinstance(val, list):
        if len(val) == 0:
            return "N/A"
        if all(isinstance(x, str) for x in val):
            return ", ".join(val)
        if all(isinstance(x, dict) for x in val):
            return f"{len(val)} item(s)"
        return str(val[:3]) + (" ..." if len(val) > 3 else "")
    if isinstance(val, dict):
        compact = {k: v for k, v in list(val.items())[:5] if v is not None}
        return json.dumps(compact, default=str)
    return str(val)


def format_as_summary(data: List[Dict], query: str, total_count: int = None,
                      response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format data as executive summary + key details."""
    if not data:
        lines = _executive_summary_lines([], query, response_meta=response_meta)
        lines.append("No matching records found for your query.")
        return "\n".join(lines)

    count = total_count or len(data)
    lines = _executive_summary_lines(data, query, response_meta=response_meta)

    if count == 1:
        lines.append(_format_single_record(mask_dict(data[0])))
    else:
        for i, record in enumerate(data[:10], 1):
            lines.append(_format_list_item(mask_dict(record) if isinstance(record, dict) else record, i))
        if count > 10:
            lines.append(f"\n... and {count - 10} more records.")

    return "\n".join(lines)


def format_as_list(data: List[Dict], query: str,
                   response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format data as a numbered list with executive summary."""
    if not data:
        lines = _executive_summary_lines([], query, response_meta=response_meta)
        lines.append("No results found for your query.")
        return "\n".join(lines)

    lines = _executive_summary_lines(data, query, response_meta=response_meta)
    lines.append(f"Found {len(data)} result(s):\n")
    for i, record in enumerate(data, 1):
        lines.append(_format_list_item(mask_dict(record) if isinstance(record, dict) else record, i))

    return "\n".join(lines)


def format_as_table(data: List[Dict], query: str,
                    response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format data as a markdown table with executive summary."""
    if not data:
        lines = _executive_summary_lines([], query, response_meta=response_meta)
        lines.append("No results found for your query.")
        return "\n".join(lines)

    summary = "\n".join(_executive_summary_lines(data, query, response_meta=response_meta))

    priority_keys = ["name", "companyName", "firstName", "lastName",
                     "invoice_number", "sales_number", "deal_number",
                     "stage", "status", "payment_status", "grand_total_in_usd",
                     "grandtotal_in_usd", "email", "country", "ownerName"]

    all_keys = []
    for record in data:
        for k in record:
            if k not in all_keys and k != "_id":
                all_keys.append(k)

    ordered_keys = [k for k in priority_keys if k in all_keys]
    ordered_keys += [k for k in all_keys if k not in ordered_keys]
    ordered_keys = ordered_keys[:8]

    headers = " | ".join(_humanize_key(k) for k in ordered_keys)
    sep = " | ".join("---" for _ in ordered_keys)
    table_lines = [f"| {headers} |", f"| {sep} |"]

    for record in data[:30]:
        masked = mask_dict(record) if isinstance(record, dict) else record
        row = " | ".join(format_value(k, masked.get(k, "")) for k in ordered_keys)
        table_lines.append(f"| {row} |")

    result = summary + "\n\n" + "\n".join(table_lines)
    if len(data) > 30:
        result += f"\n\n... showing 30 of {len(data)} records."
    return result


def format_as_count(data: Any, query: str,
                    response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format as a count response with executive summary."""
    if isinstance(data, (int, float)):
        count = int(data)
    elif isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        if "count" in data:
            count = data["count"]
        elif "total" in data:
            count = data["total"]
        else:
            count = len(data)
    else:
        count = data

    lines = _executive_summary_lines(count, query, response_meta=response_meta)
    lines.append(f"Total Count: **{count}**")
    return "\n".join(lines)


def format_as_detail(data: List[Dict], query: str,
                     response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format full details for a single or few records."""
    if not data:
        lines = _executive_summary_lines([], query, response_meta=response_meta)
        lines.append("No results found for your query.")
        return "\n".join(lines)

    lines = _executive_summary_lines(data, query, response_meta=response_meta)
    for i, record in enumerate(data[:5]):
        masked = mask_dict(record) if isinstance(record, dict) else record
        if i > 0:
            lines.append("\n" + "-" * 50 + "\n")
        lines.append(_format_single_record(masked))

    if len(data) > 5:
        lines.append(f"\n... and {len(data) - 5} more records. Ask me to narrow down.")

    return "\n".join(lines)


def format_aggregation(data: List[Dict], query: str,
                       response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Format aggregation results as a markdown table with executive summary."""
    if not data:
        return "No aggregation results found."

    lines = _executive_summary_lines(data, query, response_meta=response_meta)

    if data and isinstance(data[0], dict):
        keys = list(data[0].keys())
        headers = " | ".join(_humanize_key(k) for k in keys)
        sep = " | ".join("---" for _ in keys)
        lines += [f"| {headers} |", f"| {sep} |"]
        for row in data[:30]:
            cells = " | ".join(format_value(k, row.get(k, "")) for k in keys)
            lines.append(f"| {cells} |")
        if len(data) > 30:
            lines.append(f"\n... and {len(data) - 30} more rows.")

    return "\n".join(lines)


def auto_format(data: Any, query: str, format_hint: str = "auto",
                response_meta: Optional[Dict[str, Any]] = None) -> str:
    """Automatically choose and apply the best format."""
    if format_hint == "auto":
        format_hint = detect_format_intent(query)

    if isinstance(data, (int, float)):
        return format_as_count(data, query, response_meta=response_meta)

    if isinstance(data, dict):
        if any(k in data for k in ["count", "total", "total_revenue_usd",
                                    "totalRevenue", "invoice_count"]):
            return format_as_count(data, query, response_meta=response_meta)
        data = [data]

    if not isinstance(data, list):
        return str(data)

    if len(data) == 0:
        lines = _executive_summary_lines([], query, response_meta=response_meta)
        lines.append("No results found for your query.")
        return "\n".join(lines)

    # Detect aggregation results (dicts with _id field throughout)
    is_aggregation = (
        len(data) > 0
        and all(isinstance(d, dict) for d in data[:3])
        and "_id" in data[0]
        and any(k in data[0] for k in {"count", "total", "sum", "avg", "total_value", "total_revenue_usd"})
        and not any(k in data[0] for k in {"createdAt", "updatedAt", "email", "phoneNumber"})
    )
    if is_aggregation:
        return format_aggregation(data, query, response_meta=response_meta)

    masked_data = [mask_dict(r) if isinstance(r, dict) else r for r in data]

    if format_hint == "count":
        return format_as_count(len(masked_data), query, response_meta=response_meta)
    elif format_hint == "table":
        return format_as_table(masked_data, query, response_meta=response_meta)
    elif format_hint == "list":
        return format_as_list(masked_data, query, response_meta=response_meta)
    elif format_hint == "detail":
        return format_as_detail(masked_data, query, response_meta=response_meta)
    elif format_hint == "summary":
        return format_as_summary(masked_data, query, response_meta=response_meta)
    else:
        if len(masked_data) <= 5:
            return format_as_summary(masked_data, query, response_meta=response_meta)
        else:
            return format_as_list(masked_data, query, response_meta=response_meta)


# ── Executive Summary ────────────────────────────────

def _executive_summary_lines(data: Any, query: str,
                             response_meta: Optional[Dict[str, Any]] = None) -> List[str]:
    """
    Generate 3-line executive summary at top of every response.
    Line 1: Direct answer with count/value
    Line 2: Context about what was found or what the database provides
    Line 3: Action hint for the user
    """
    q = query.lower()
    entity = _detect_entity_type_from_query(q)
    meta = response_meta or {}
    explicit_entity = str(meta.get("entity") or "").strip()
    explicit_collection = str(meta.get("collection") or "").strip()
    if explicit_entity:
        entity = explicit_entity

    returned_count = _resolve_returned_count(data)
    echo_filter = _render_filter_for_echo(
        meta.get("filter"),
        query=query,
    )
    echo_collection = explicit_collection or _infer_collection_from_entity(entity)
    echo_line_1 = (
        f"Found {returned_count} {entity} record(s) | "
        f"Filter: {echo_filter} | "
        f"Collection: {echo_collection}"
    )
    echo_line_2 = (
        f"Query echo -> Entity: {entity} | "
        f"Filter: {echo_filter} | "
        f"Returned: {returned_count} records"
    )

    # Handle numeric count
    if isinstance(data, (int, float)):
        count = int(data)
        if count == 0:
            line1 = f"Answer: No {entity} records found for the applied filters."
            line2 = _get_no_data_explanation(q)
            line3 = "Try adjusting filters, date range, or search terms."
        else:
            line1 = f"Answer: Found {count} {entity} record(s) for the applied filters."
            line2 = f"This is the total count from the ECRM {entity} module."
            line3 = "Ask for a breakdown by status, stage, or owner for more detailed insights."
        return [echo_line_1, echo_line_2, line1, line2, line3, ""]

    count = len(data) if isinstance(data, list) else 1

    if count == 0:
        line1 = f"No {entity} records found for the applied filters."
        line2 = _get_no_data_explanation(q)
        line3 = "Try broadening your search, adjusting date range, or checking spelling."
        return [echo_line_1, echo_line_2, line1, line2, line3, ""]

    total_val = _extract_total_value(data) if isinstance(data, list) else None
    key_stat = _extract_key_stat(data, q) if isinstance(data, list) and data else ""

    # Line 1: Direct answer based on query intent
    if "how many" in q:
        line1 = f"Answer: {count} {entity}(s) found matching your criteria."
    elif "revenue" in q or "total" in q:
        if total_val:
            line1 = f"Summary: Total value is {total_val} across {count} record(s)."
        else:
            line1 = f"Summary: {count} revenue/value record(s) found in the ECRM system."
    elif "closed won" in q:
        line1 = f"Summary: {count} Closed Won deal(s) found in the ECRM pipeline."
    elif "closed lost" in q or "lost deal" in q:
        line1 = f"Summary: {count} Closed Lost deal(s) found in the ECRM system."
    elif "overdue" in q:
        line1 = f"Summary: {count} overdue record(s) require immediate attention."
    elif "pending" in q:
        line1 = f"Summary: {count} pending {entity} record(s) are awaiting action."
    elif re.search(r'top\s+\d+', q):
        m = re.search(r'top\s+(\d+)', q)
        top_n = m.group(1) if m else str(count)
        line1 = f"Summary: Top {top_n} {entity} record(s) retrieved based on your criteria."
    elif "this month" in q:
        line1 = f"Summary: {count} {entity} record(s) found for this month."
    elif "last month" in q:
        line1 = f"Summary: {count} {entity} record(s) found for last month."
    elif "this year" in q or "financial year" in q:
        line1 = f"Summary: {count} {entity} record(s) found for this financial year."
    else:
        line1 = f"Summary: Found {count} {entity} record(s) for the applied filters."

    # Line 2: Context or key insight
    if key_stat:
        line2 = key_stat
    elif "stage" in q or "status" in q:
        line2 = f"Stage/status constraints were applied on the ECRM {entity} module."
    elif "owner" in q or "rep" in q or "user" in q:
        line2 = f"User/owner constraints were applied from the ECRM system."
    elif "this month" in q or "last month" in q or "this year" in q:
        line2 = f"Date-window constraints were applied from the ECRM {entity} module."
    else:
        line2 = f"Records are returned from the ECRM {entity} module with applied constraints."

    # Line 3: Action hint
    if count > 20:
        line3 = f"Showing top records from {count} total. Use specific filters to narrow down results."
    elif count > 0:
        line3 = "Ask for table view, full details, or filter by owner/date/status for more insights."
    else:
        line3 = "Try adjusting your search criteria or date range for more results."

    return [echo_line_1, echo_line_2, line1, line2, line3, ""]


def _resolve_returned_count(data: Any) -> int:
    if isinstance(data, (int, float)):
        return int(data)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        if "count" in data and isinstance(data["count"], (int, float)):
            return int(data["count"])
        if "total" in data and isinstance(data["total"], (int, float)):
            return int(data["total"])
        return 1 if data else 0
    return 0


_SOFT_DELETE_FIELDS = {"deleted", "isDeleted", "__v", "deletedAt"}


def _render_filter_for_echo(filter_obj: Any, query: str = "") -> str:
    """
    Build a human-readable filter string for the echo line.
    Strips soft-delete noise; falls back to query-text extraction when
    the plan filter has no meaningful conditions.
    """
    clean: Dict = {}
    if isinstance(filter_obj, dict):
        for k, v in filter_obj.items():
            if k not in _SOFT_DELETE_FIELDS:
                clean[k] = v

    if clean:
        fragments = _flatten_filter_fragments(clean)
        if fragments:
            return " AND ".join(fragments[:6])
        try:
            return json.dumps(clean, default=str)[:280]
        except Exception:
            return str(clean)[:280]

    # No meaningful plan filter — extract from query text
    return _extract_filter_hint_from_query(query)


def _extract_filter_hint_from_query(query: str) -> str:
    """
    When the executed plan had no filter beyond soft-delete, build a
    filter description from the query text so the echo line is never blank.
    If the query genuinely has no filter (e.g. 'Get all deals'), return 'none'.
    """
    q = query.strip()
    ql = q.lower()

    # Entity name after trigger phrases — capture FULL name including hyphens/commas
    for trigger in ["linked to", "associated with", "history for", "details for",
                    "details of", " for ", " of "]:
        idx = ql.find(trigger)
        if idx != -1:
            name = q[idx + len(trigger):].strip()
            # Remove trailing common suffixes
            name = re.sub(
                r'\s+\b(give|show|and|with|in|from|using|please|including)\b.*$',
                '', name, flags=re.IGNORECASE
            ).strip().rstrip(".,;")
            if 2 < len(name) <= 120:
                return f"name contains '{name}'"

    # Explicit field=value patterns
    checks = [
        (r'\bpriority\s+([A-Za-z]+)', "priority"),
        (r'\bwith\s+priority\s+([A-Za-z]+)', "priority"),
        (r'\bstatus\s+([A-Za-z_]+)', "status"),
        (r'\bwith\s+status\s+([A-Za-z_]+)', "status"),
        (r'\bby\s+region\s+([A-Za-z]+)', "region"),
        (r'\bregion\s+([A-Za-z]+)', "region"),
        (r'\bby\s+source\s+([\w/]+)', "source"),
        (r'\bsource\s+([\w/]+)', "source"),
        (r'\btechnology\s+([\w]+)', "technology"),
        (r'\bjob\s+title\s+([\w\s]+)', "jobTitle"),
        (r'\bby\s+country\s+([A-Za-z]+)', "country"),
    ]
    for pat, field in checks:
        m = re.search(pat, ql)
        if m:
            return f"{field} = '{m.group(1)}'"

    # Date / temporal
    if "today" in ql:
        return "date = today"
    if "this week" in ql:
        return "date within this week"
    if "this month" in ql:
        return "date within this month"
    if "last month" in ql:
        return "date within last month"
    if "this year" in ql or "financial year" in ql or "fy" in ql:
        return "date within this financial year"
    m_rel = re.search(r"last\s+(\d+)\s+(day|week|month|year)s?", ql)
    if m_rel:
        return f"date within last {m_rel.group(1)} {m_rel.group(2)}(s)"
    m_date = re.search(r'(?:after|before|on|due)\s+(\d{4}-\d{2}-\d{2})', ql)
    if m_date:
        direction = "after" if "after" in ql else "before" if "before" in ql else "on"
        return f"date {direction} {m_date.group(1)}"
    m_range = re.search(r'(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})', ql)
    if m_range:
        return f"date between {m_range.group(1)} and {m_range.group(2)}"
    m_amt = re.search(r'amount\s+(?:range\s+)?(\d+)\s+to\s+(\d+)', ql)
    if m_amt:
        return f"amount between {m_amt.group(1)} and {m_amt.group(2)}"

    # Overdue / pending / status keywords
    if "overdue" in ql:
        return "due_date < today AND status != paid"
    if "pending" in ql:
        return "status = 'pending/open'"
    if "closed won" in ql:
        return "dealWonAt != null AND dealLostAt = null"
    if "closed lost" in ql:
        return "dealLostAt != null"
    if "paid" in ql:
        return "payment_status = 'paid'"
    if "draft" in ql:
        return "status = 'Draft'"
    if "cancelled" in ql:
        return "status = 'Cancelled'"
    if "confirmed" in ql or "confirm" in ql:
        return "status = 'Confirm'"

    return "none"


def _flatten_filter_fragments(filter_obj: Any, prefix: str = "") -> List[str]:
    fragments: List[str] = []
    if isinstance(filter_obj, dict):
        for key, value in filter_obj.items():
            if key in {"$and", "$or"} and isinstance(value, list):
                nested: List[str] = []
                for part in value:
                    nested.extend(_flatten_filter_fragments(part))
                if nested:
                    joiner = f" {key[1:].upper()} "
                    fragments.append(f"({joiner.join(nested[:4])})")
                continue
            if isinstance(value, dict):
                ops: List[str] = []
                for op, op_val in value.items():
                    if op == "$regex":
                        ops.append(f"{key} contains '{op_val}'")
                    elif op == "$in" and isinstance(op_val, list):
                        vals = ", ".join(f"'{x}'" for x in op_val[:5])
                        ops.append(f"{key} in [{vals}]")
                    elif op in {"$gte", "$gt", "$lte", "$lt", "$ne", "$eq"}:
                        sym = {
                            "$gte": ">=",
                            "$gt": ">",
                            "$lte": "<=",
                            "$lt": "<",
                            "$ne": "!=",
                            "$eq": "=",
                        }[op]
                        ops.append(f"{key} {sym} '{op_val}'")
                    elif op == "$options":
                        continue
                    else:
                        ops.append(f"{key}.{op}='{op_val}'")
                if ops:
                    fragments.append(" and ".join(ops))
            else:
                fragments.append(f"{key} = '{value}'")
    return fragments


def _infer_collection_from_entity(entity: str) -> str:
    ent = (entity or "").strip().lower()
    mapping = {
        "deal": "deals",
        "invoice": "invoices",
        "company": "companies",
        "contact": "contacts",
        "task": "createtasks",
        "user": "users",
        "lead": "outreaches",
        "target": "targets",
        "sales": "sales",
        "bill": "bills",
        "product": "products",
    }
    return mapping.get(ent, ent + "s" if ent else "unknown")


# ── Internal helpers ─────────────────────────────────

def _detect_entity_type_from_query(q: str) -> str:
    """Detect the primary entity type from query text."""
    if "deal" in q:
        return "deal"
    if "invoice" in q:
        return "invoice"
    if "profit" in q:
        return "invoice"
    if "company" in q or "companies" in q or "account" in q:
        return "company"
    if "contact" in q:
        return "contact"
    if "task" in q:
        return "task"
    if "user" in q or "rep" in q or "owner" in q:
        return "user"
    if "outreach" in q or "prospect" in q or "lead" in q:
        return "lead"
    if "revenue" in q or "payment" in q:
        return "invoice"
    if "target" in q:
        return "target"
    if "sales" in q:
        return "sales"
    if "bill" in q or "vendor" in q:
        return "bill"
    if "product" in q:
        return "product"
    return "CRM"


def _extract_total_value(data: Any) -> Optional[str]:
    """Extract a total/revenue value from data if present."""
    if not isinstance(data, list):
        return None
    for item in data[:5]:
        if isinstance(item, dict):
            for k in ["total_revenue_usd", "grand_total_in_usd", "grandtotal_in_usd",
                      "totalRevenue", "total", "sum", "total_value", "totalValue"]:
                if k in item and item[k] is not None:
                    try:
                        return f"${float(item[k]):,.2f}"
                    except Exception:
                        pass
    return None


def _extract_key_stat(data: List[Dict], query: str) -> str:
    """Extract a key statistic from the data for the summary line 2."""
    if not data or not isinstance(data[0], dict):
        return ""

    # For aggregation/group results, show the top group
    if "_id" in data[0]:
        top = data[0]
        group_val = top.get("_id") or "Unknown"
        count_val = top.get("count", top.get("total", ""))
        if count_val:
            return f"Top group: '{group_val}' with {count_val} records."

    # For revenue-type data
    for item in data[:3]:
        if isinstance(item, dict):
            for k in ["total_revenue_usd", "totalRevenue", "grandtotal_in_usd"]:
                if k in item:
                    try:
                        return f"Key value: {format_currency(item[k])}"
                    except Exception:
                        pass

    # For deals: show stages present
    stages = []
    for item in data[:5]:
        if isinstance(item, dict) and "stage" in item and item["stage"]:
            if item["stage"] not in stages:
                stages.append(item["stage"])
    if stages:
        return f"Stages present: {', '.join(stages[:4])}."

    # For invoices: show payment statuses
    statuses = []
    for item in data[:5]:
        if isinstance(item, dict) and "payment_status" in item and item["payment_status"]:
            if item["payment_status"] not in statuses:
                statuses.append(item["payment_status"])
    if statuses:
        return f"Payment statuses: {', '.join(statuses[:4])}."

    return ""


def _get_no_data_explanation(query: str) -> str:
    """Explain what the database provides when no records are found."""
    q = query.lower()
    if "deal" in q:
        return "The ECRM deals module tracks sales pipeline with stages, owners, and values."
    if "invoice" in q:
        return "The ECRM invoices module stores paid, draft, confirmed, and overdue billing records."
    if "company" in q or "companies" in q:
        return "The ECRM companies module contains client accounts, lifecycle stages, and ownership info."
    if "contact" in q:
        return "The ECRM contacts module holds individual person records linked to companies."
    if "task" in q:
        return "The ECRM tasks module holds pending, completed, and overdue task records."
    if "outreach" in q or "lead" in q:
        return "The ECRM outreach module tracks cold prospects and lead engagement data."
    if "target" in q:
        return "The ECRM targets module stores monthly sales targets per user."
    return "The ECRM database contains deals, invoices, companies, contacts, tasks, targets, and more."


def _humanize_key(key: str) -> str:
    """Convert a field name to human-readable label."""
    mapping = {
        "companyName": "Company", "firstName": "First Name",
        "lastName": "Last Name", "grand_total_in_usd": "Total (USD)",
        "grandtotal_in_usd": "Total (USD)", "payment_status": "Payment Status",
        "invoice_number": "Invoice No.", "sales_number": "SO Number",
        "deal_number": "Deal No.", "ownerName": "Owner",
        "createdAt": "Created", "updatedAt": "Updated",
        "dealWonAt": "Won Date", "dealLostAt": "Lost Date",
        "closeDate": "Close Date", "invoice_date": "Invoice Date",
        "due_date": "Due Date", "payment_date": "Payment Date",
        "lifecycleStage": "Lifecycle Stage", "leadStatus": "Lead Status",
        "phoneNumber": "Phone", "jobTitle": "Job Title",
        "stateRegion": "State/Region", "websiteUrl": "Website",
        "targetInUSD": "Target (USD)", "sequence_number": "Seq No.",
        "_id": "Group", "count": "Count", "total": "Total",
        "total_revenue_usd": "Total Revenue (USD)",
    }
    if key in mapping:
        return mapping[key]
    result = re.sub(r'([a-z])([A-Z])', r'\1 \2', key)
    result = result.replace("_", " ").title()
    return result


def _get_name_field(record: dict) -> Optional[str]:
    """Extract the most likely name/identifier from a record."""
    for key in ["name", "companyName", "invoice_number", "sales_number",
                "deal_number", "Task", "title"]:
        if key in record and record[key]:
            return str(record[key])
    if "firstName" in record:
        return f"{record.get('firstName', '')} {record.get('lastName', '')}".strip()
    return None


def _get_record_type(record: dict) -> Optional[str]:
    """Infer record type from its fields."""
    if "dealWonAt" in record or "stage" in record:
        return "Deal"
    if "invoice_number" in record or "payment_status" in record:
        return "Invoice"
    if "sales_number" in record:
        return "Sales Order"
    if "companyName" in record and "lifecycleStage" in record:
        return "Company"
    if "firstName" in record and "lastName" in record:
        return "Contact"
    if "Task" in record:
        return "Task"
    return None


def _format_single_record(record: dict) -> str:
    """Format a single record with all fields."""
    lines = []
    skip = {"__v", "password", "tokens", "googleAccessToken",
            "items", "lineItems", "payment_history", "activities"}
    for key, val in record.items():
        if key in skip or key.startswith("__"):
            continue
        human_key = _humanize_key(key)
        formatted_val = format_value(key, val)
        if formatted_val != "N/A":
            lines.append(f"  {human_key}: {formatted_val}")
    return "\n".join(lines)


def _format_list_item(record: Any, index: int) -> str:
    """Format a record as a single-line list item."""
    if not isinstance(record, dict):
        return f"{index}. {record}"
    name = _get_name_field(record)
    parts = [f"{index}. {name or 'Record'}"]

    detail_keys = ["stage", "status", "payment_status", "grand_total_in_usd",
                   "grandtotal_in_usd", "email", "country", "priority",
                   "due_date", "invoice_date", "ownerName"]
    added = 0
    for key in detail_keys:
        if key in record and record[key] and added < 3:
            parts.append(f"{_humanize_key(key)}: {format_value(key, record[key])}")
            added += 1

    return " | ".join(parts)
