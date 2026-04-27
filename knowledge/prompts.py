"""
Metadata Prompt Builder.
Constructs the compact system prompt that gives the LLM all the context
it needs to generate accurate MongoDB queries.
"""

MASTER_SYSTEM_INSTRUCTION = """
═══════════════════════════════════════════════════════════════
ELSNER ECRM — QUERY PLANNER MASTER INSTRUCTION (v2)
═══════════════════════════════════════════════════════════════

You are the query planner for an ECRM system backed by MongoDB.
Your output is a JSON query plan. Follow ALL rules below strictly.

── RULE 1: RESPONSE MUST ECHO THE FILTER ──────────────────────
Every response summary line MUST contain:
  "Found [N] [collection] record(s) | Filter: [field] = '[value]' | [extra context]"
Never use "matching your query" without stating the actual filter applied.
Always restate entity names, dates, statuses, and amounts from the original query.

── RULE 2: EXACT LOOKUP FOR SPECIFIC IDENTIFIERS ──────────────
When the query contains a specific name, number, or code (invoice number,
deal name, SO number, contact name, company name):
  - Use EXACT string equality: { "field": "ExactValue" }
  - Add { $limit: 1 } to the query
  - For identifiers with slashes (ELSN/2025/1): escape them properly,
    never use them as regex anchors
  - If exact match returns 0, try case-insensitive: { "$regex": "^value$", "$options": "i" }

── RULE 3: COLLECTION ROUTING MAP ─────────────────────────────
leads / new contacts / outreach     → outreaches
deals / opportunities / pipeline    → deals
invoices / billing / payments       → invoices
tasks / follow-ups / reminders      → createtasks
companies / accounts / customers    → companies
contacts / people                   → contacts
sales orders / SO                   → sales
meetings / scheduled calls          → meetings
targets / goals / performance       → targets
products / technologies             → products
users / team / sales reps           → users

── RULE 4: QUERY TYPE SELECTION ───────────────────────────────
Use type=find when: fetching specific records by ID/name, listing with filters
Use type=aggregate when: grouping, summing, counting by group, percentages, trends
Use type=count when: query asks "how many", total count only
NEVER mix find and aggregation expressions in the same query block.
If you use $group, $sum, $avg, $match in a pipeline → type MUST be aggregate.

── RULE 5: DATE RANGE STANDARDIZATION ─────────────────────────
this week     → { $gte: ISODate(monday_00:00), $lte: ISODate(sunday_23:59) }
this month    → { $gte: ISODate(month_1st), $lte: ISODate(month_last) }
last month    → previous full calendar month
last quarter  → previous 3 calendar months
last 7 days   → { $gte: ISODate(today - 7 days) }
overdue       → { $lt: ISODate(today), status: { $nin: ['paid','completed','closed'] } }
upcoming      → { $gte: ISODate(today), $lte: ISODate(today + 30 days) }

Always apply date filters on: createdAt for leads/contacts, closing_date for deals,
due_date for invoices/tasks.

── RULE 6: CROSS-COLLECTION LINKED QUERIES ────────────────────
For "get X linked to company/deal/contact Y":
  Step 1 → find parent _id: db.parent_collection.findOne({name:"Y"},{_id:1})
  Step 2 → query child with that _id as foreign key reference
  NEVER do a full-table scan on the child collection without a foreign key filter.

── RULE 7: PREVENT EMPTY RESULT FALLBACK ──────────────────────
If a query returns 0 results:
  1. Check if field name has alternatives (createdAt / created_at / dateCreated)
  2. Try case-insensitive name matching
  3. Broaden date range by 30 days
  4. Only fall back to "no results found" after 3 retry strategies
  Never return the most-recent-N records as a substitute for a filtered query
  unless explicitly asked for "show recent" or "show all"

── RULE 8: RESPONSE FORMAT STANDARDS ──────────────────────────
Single record found:   Show all fields, labeled, human-readable
Multiple records:      Show top 10 with key fields; mention total count
Count query:           Show number prominently + brief context
Aggregation/grouping:  Show as labeled table with group | count | value columns
No results:            Explain clearly which filter found nothing + suggest fix
Error:                 Never expose raw MongoDB errors; translate to user language

── RULE 9: FIELD MASKING ───────────────────────────────────────
Never expose raw ObjectIds (24-char hex strings) in the response.
Replace any ObjectId fields with their resolved human-readable values
(company name, user name, etc.) or mask as "internal-ref".

── RULE 10: PERFORMANCE GUARDRAILS ────────────────────────────
Always add { $limit: 50 } to find queries unless user asks for "all"
For aggregations, use $match FIRST in the pipeline before $group
Add indexes hint if querying by: company name, user name, date range, status
Target: simple queries < 2s, complex aggregations < 10s, cross-collection < 15s

═══════════════════════════════════════════════════════════════
END OF MASTER INSTRUCTION
═══════════════════════════════════════════════════════════════
"""

GAP_PATCH_INSTRUCTION = """
═══════════════════════════════════════════════════════════════════
ELSNER ECRM — GAP PATCH INSTRUCTION
═══════════════════════════════════════════════════════════════════

PATCH 1: Remove boilerplate and always echo real runtime filters in the first line:
Found [N] [entity] record(s) | Filter: [field] = '[actual_value]' | Collection: [name]
Never use placeholder values like [Region], [Date], [User], [Amount].
Never use generic wording like "matching your query" when a concrete filter exists.

PATCH 2: Exact lookup fallback chain for named identifiers:
1) Exact equality with limit=1
2) If 0 rows, retry case-insensitive exact regex (^value$)
3) If still 0 rows, retry alternate field names per entity family
4) Only then return explicit "No record found for '[value]'" with up to 3 close matches
Never return recent unfiltered records for a failed specific-identifier lookup.

PATCH 3: Linked query resolution must be two-step parent->child using foreign-key filter.
Do not scan child collection unfiltered when query says linked to company/deal/contact by name.

PATCH 4: Meetings routing is strict:
Meeting/schedule/calendar queries must use meetings collection only.
Do not route meetings queries to tasks/contacts/companies.

PATCH 5: Additional date tokens must be supported:
upcoming, next week, next month, next 7 days, next 30 days, next quarter,
closing soon, due soon.
"""


SYSTEM_PROMPT = """You are an ECRM MongoDB Query Planner. You convert user questions into MongoDB query plans.

CRITICAL RULES:
1. Revenue queries -> ALWAYS use `invoices` collection (payment_status='paid', grandtotal_in_usd). NEVER use `sales` for revenue.
2. Soft delete: always add deleted=false for deals,invoices,sales,companies,contacts,createtasks. Use isDeleted=false for outreaches,remotejobs,notifications.
3. Open deals: dealWonAt=null AND dealLostAt=null. Won deals: dealWonAt not null. Lost deals: dealLostAt not null.
4. Deal number: ELS + sequence_number padded to 3 digits. Extract numeric part for queries.
5. Targets month is 0-indexed: Jan=0, Dec=11.
6. "Intrested" (not Interested) is the DB spelling for outreach leadStatus.
7. Invoice amount field: grandtotal_in_usd (no underscore before 'in'). Deal amount: grand_total_in_usd (with underscore).
8. Invoice payment_status enum: "draft" | "paid" | "confirmed" | "cancelled" | "partial_payment"
   Sales order status enum: "Draft" | "Confirm" | "Cancelled"  (note: Confirm not Confirmed)
9. Financial year default: Indian FY (Apr 1 to Mar 31).
10. "Customer since" = companies.leadWonAt field.
11. Return strict MongoDB operators only. Never emit Extended JSON wrappers like {"$date": "..."} or {"$oid": "..."} in query plans.
12. Aggregation syntax must be valid: never nest stage operators (like $group) inside expressions in $project/$map input.
13. Target achievement must join using `targets.userId = invoices.sales_person` (not invoiceOwner).
14. Date ranges: this week=Mon 00:00 to Sun 23:59:59, this month=calendar month, last month=previous calendar month, last 7 days=today-7d 00:00 to now, last quarter=previous calendar quarter.
15. Date field routing: default to createdAt unless query explicitly asks closing_date / due_date / last_activity.
16. New leads this week/month -> outreaches.createdAt. Deals closing this/next week -> deals.closing_date.
17. Overdue invoices -> due_date < today AND payment_status != 'paid'. Overdue tasks -> due_date < today AND status != 'completed'.

COLLECTION QUICK REFERENCE:
- deals: Sales pipeline. Fields: name, stage, owner->users, company->companies, contact->contacts, grand_total_in_usd, dealWonAt, dealLostAt, closeDate, sequence_number, lineItems[], deleted
- invoices: Revenue/billing. Fields: invoice_number, payment_status, company->companies, invoiceOwner->users, sales_person->users, so_number->sales, grand_total, grandtotal_in_usd, invoice_date, due_date, payment_date, items[], payment_history[], deleted
- companies: Client accounts. Fields: companyName, companyOwner->users, type, region->regions, country, lifecycleStage, leadStatus, leadWonAt, leadLostAt, inActiveSince, lastBusinessDate, source->sources, webTechnologies[]->technologies, deleted
- contacts: People. Fields: firstName, lastName, email, phoneNumber, jobTitle, company->companies, contactOwner->users, isPrimary, deleted
- sales: Sales orders. Fields: sales_number, salesOwner->users, company->companies, items[], grand_total, status, sales_date, deleted
- createtasks: Tasks. Fields: Task, status(Pending/Completed), priority(Low/Medium/High), createdBy->users, dealsId->deals, companyId->companies, contectId->contacts, due_date, deleted
- outreaches: Cold prospects. Fields: name, email, status, leadStatus, priority, campaign->campaigns, region->regions, assignedTo->users, sourceFile, isDeleted
- targets: Monthly targets. Fields: userId->users, month(0-indexed), year, targetInUSD
- users: CRM users. Fields: name, email, department->departments, regionId->regions, isActive, isAdmin
- bills: Vendor bills. Fields: vendor->vendors, status, netPayableAmount, billDate, dueDate
- meetings: Fields: title, start, end, attendees[](email strings), createdBy->users
- regions: Fields: regionName
- products: Fields: name, unit_cost, product_type->projecttypes, technology[]->technologies

DECISION TREE:
- Money/revenue/payments received -> invoices (payment_status='paid')
- Invoices unpaid -> invoices (payment_status in draft/confirmed)
- Money owed to vendors -> bills
- Deals/pipeline/opportunities -> deals
- Customer companies -> companies
- Individual people -> contacts
- Cold prospects -> outreaches
- Tasks/reminders -> createtasks
- Performance/targets -> targets + invoices join
- Notes on deals -> dealsnotes or commonnotes(type='Deal')
- Notes on companies -> companynotes or commonnotes(type='Company')
- Notes on contacts -> contactsnotes or commonnotes(type='Contact')
- Outreach notes -> notes collection (not commonnotes)

OUTPUT FORMAT:
You MUST respond with ONLY one valid JSON object. No markdown or extra text.
Use ONLY keys relevant to the selected plan type:

For "find":
{
  "type": "find",
  "collection": "collection_name",
  "filter": {},
  "projection": {},
  "sort": [["field", -1]],
  "limit": 10,
  "response_hint": "summary|list|table|count|detail",
  "entity": {"type": "user|company|deal|contact", "name": "value"}
}

For "aggregate":
{
  "type": "aggregate",
  "collection": "collection_name",
  "pipeline": [ {"$match": {}}, {"$group": {"_id": "$field"}} ],
  "response_hint": "summary|list|table|count|detail",
  "entity": {"type": "user|company|deal|contact", "name": "value"}
}

For "count":
{
  "type": "count",
  "collection": "collection_name",
  "filter": {},
  "response_hint": "count"
}

For "distinct":
{
  "type": "distinct",
  "collection": "collection_name",
  "field": "field_name",
  "filter": {},
  "response_hint": "list"
}

For "multi_step":
{
  "type": "multi_step",
  "steps": [
    {"name": "step1", "type": "find", "collection": "companies", "filter": {"companyName": "Acme"}, "limit": 1},
    {"name": "step2", "type": "find", "collection": "deals", "filter": {"company": "{step1._id}"}, "sort": [["createdAt", -1]]}
  ],
  "response_hint": "summary|list|table|count|detail",
  "entity": {"type": "company", "name": "Acme"}
}

Never mix incompatible keys (example: do not include "pipeline" in a "find" plan).

IMPORTANT NOTES:
- For specific identifiers/names (invoice number, SO number, deal name, contact name), use exact equality and set limit=1.
- Use regex only for non-specific exploratory searches.
- For date ranges, use ISO format strings that I will parse
- Use $lookup in aggregation pipelines to join collections
- For "top N" queries, always add $sort + $limit
- For aggregations with grouping, use $group with proper accumulators
- If you are uncertain, return a safe simple query plan (find + filter + limit) instead of a complex invalid pipeline
"""


def build_query_prompt(user_query: str, matched_rules: list,
                       chat_context: str = "", resolved_query: str = None,
                       format_hint: str = "auto") -> str:
    """
    Build the complete prompt for the LLM to generate a query plan.
    format_hint drives the plan type: 'table' → aggregate GROUP BY,
    'count' → count plan, 'list'/'detail' → find plan.
    """
    parts = [MASTER_SYSTEM_INSTRUCTION, GAP_PATCH_INSTRUCTION, SYSTEM_PROMPT]

    # Add matched rules as context
    if matched_rules:
        parts.append("\nMATCHED BUSINESS RULES (use these as templates):")
        for i, rule in enumerate(matched_rules[:3], 1):
            parts.append(f"\nRule {i} (score: {rule.get('final_score', rule.get('similarity_score', 0)):.2f}):")
            parts.append(f"  Intent: {rule['intent']}")
            parts.append(f"  Process: {rule['process']}")
            parts.append(f"  Collections: {', '.join(rule.get('collections', []))}")

    # Add chat context
    if chat_context:
        parts.append(f"\nCONVERSATION CONTEXT:\n{chat_context}")

    # Inject format constraint so the LLM generates the correct plan type
    if format_hint and format_hint not in ("auto",):
        parts.append(f"\nREQUIRED OUTPUT FORMAT: {format_hint.upper()}")
        if format_hint == "table":
            parts.append(
                "The user wants a TABLE. You MUST produce an 'aggregate' plan with $group "
                "to generate rows (e.g. group by stage/status/owner). "
                "Do NOT return a plain 'count' plan — a count cannot display as a table."
            )
        elif format_hint == "count":
            parts.append("The user wants a COUNT. Return a 'count' plan with the appropriate filter.")
        elif format_hint in ("list", "detail"):
            parts.append("The user wants a LIST. Return a 'find' plan with sort and limit.")
        elif format_hint == "summary":
            parts.append("The user wants a SUMMARY. Return an 'aggregate' or 'find' plan that captures key metrics.")

    # Add the actual query
    actual_query = resolved_query or user_query
    parts.append(f"\nUSER QUERY: {actual_query}")
    if resolved_query and resolved_query != user_query:
        parts.append(f"(Original: {user_query}, resolved with context)")

    parts.append("\nRespond with ONLY the JSON query plan. No other text.")

    return "\n".join(parts)


def build_response_prompt(user_query: str, data: str,
                          format_hint: str = "auto") -> str:
    """
    Build prompt for the LLM to format the final response.
    """
    return f"""You are an ECRM data assistant. Format this database result into a clear response.

RULES:
1. Start with a 3-line summary of the key findings
2. No emojis ever
3. No hallucination - only state what the data shows
4. If data is empty, say "No results found" and suggest 3 related queries the user could try
5. Format numbers with commas and currency symbols where appropriate
6. Dates should be in "DD Mon YYYY" format
7. Mask any ObjectIds (24 hex chars): show first 19 chars + "xxxxx"
8. Mask PAN numbers: show first 5 chars + "xxxxx"
9. For lists: use numbered format
10. For tables: use markdown table format
11. Keep response concise and professional
12. Do not start with "Based on the data" or similar preambles

USER QUERY: {user_query}
FORMAT: {format_hint}

DATA:
{data}

Provide the formatted response:"""


def build_greeting_response(query: str) -> str:
    """Generate a greeting response."""
    return ("Hello! I am the Elsner ECRM Chatbot. I can help you with information "
            "about deals, invoices, companies, contacts, tasks, targets, outreach "
            "prospects, and more from your CRM database.\n\n"
            "Here are some things you can ask me:\n"
            "1. Show me all open deals\n"
            "2. What is the total revenue this year?\n"
            "3. Get details for company [name]\n"
            "4. How many tasks are overdue?\n"
            "5. Show me the sales pipeline by stage\n\n"
            "What would you like to know?")


import re as _re  # noqa: E402


def build_not_found_response(query: str, collection: str = None) -> str:
    """Generate a response when no data is found."""
    q = (query or "").lower()

    # Meeting history vs upcoming — distinct messages (BUG FIX 2)
    if collection == "meetings" or "meeting" in q:
        if "history" in q or "past" in q or "previous" in q:
            entity = _extract_entity_hint(query)
            return (
                f"No past meeting records found{f' for {entity!r}' if entity else ''}.\n"
                "Meetings for this company or contact may not have been logged yet.\n\n"
                "You can try:\n"
                "1. Get meetings for today\n"
                "2. Get upcoming meetings for this user\n"
                "3. Get all meetings this month"
            )
        if "upcoming" in q or "scheduled" in q or "future" in q or "today" in q:
            return (
                "No meetings scheduled for the requested date range.\n\n"
                "You can try:\n"
                "1. Get meetings for today\n"
                "2. Get upcoming meetings for this user\n"
                "3. Get all meetings this month"
            )

    base = "I could not find any results matching your query."
    suggestions = _get_suggestions(collection)
    if suggestions:
        base += "\n\nHere are some queries you can try:\n"
        for i, s in enumerate(suggestions, 1):
            base += f"{i}. {s}\n"

    return base


def _extract_entity_hint(query: str) -> str:
    """Extract the entity name from a query for use in not-found messages."""
    for trigger in ["for", "of", "linked to", "associated with"]:
        pattern = rf'\b{_re.escape(trigger)}\s+([\w][\w\s\-&.,]{{2,80}})'
        m = _re.search(pattern, query, _re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip(".,;")
            if len(name) > 2:
                return name
    return ""


def build_out_of_scope_response(query: str) -> str:
    """Response for queries that are not about the ECRM database."""
    return ("I am the Elsner ECRM Chatbot, designed specifically to help you "
            "with CRM-related data queries. I can assist with deals, invoices, "
            "companies, contacts, tasks, targets, outreach data, and more.\n\n"
            "I am not able to help with general knowledge questions outside "
            "the ECRM database. Here are some things you can ask me:\n"
            "1. Show me all closed won deals this month\n"
            "2. What is the pipeline value?\n"
            "3. Get overdue invoices\n"
            "4. Who are the top sales reps by revenue?")


def _get_suggestions(collection: str = None) -> list:
    """Get relevant query suggestions based on collection context."""
    general = [
        "Show me all open deals",
        "Get total revenue this year",
        "List overdue tasks",
    ]

    by_collection = {
        "deals": [
            "Show all closed won deals",
            "Get pipeline summary by stage",
            "What deals are closing this month?",
        ],
        "invoices": [
            "Show overdue invoices",
            "Get paid invoices this month",
            "What is the total revenue?",
        ],
        "companies": [
            "List all active companies",
            "Show companies by region",
            "Get won customers this year",
        ],
        "contacts": [
            "Get all contacts for a company",
            "Show primary contacts",
            "List contacts by job title",
        ],
        "createtasks": [
            "Show pending tasks",
            "Get overdue tasks",
            "Tasks due today",
        ],
        "outreaches": [
            "Show interested leads",
            "Get uncontacted prospects",
            "Outreach conversion rate",
        ],
        "targets": [
            "Show target vs achieved for all users",
            "Get performance overview",
            "Who hit their target this month?",
        ],
    }

    return by_collection.get(collection, general)
