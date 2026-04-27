"""
Metadata Prompt Builder.
Constructs the compact system prompt that gives the LLM all the context
it needs to generate accurate MongoDB queries.
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
9. Financial year default: Indian FY (Apr 1 to Mar 31).
10. "Customer since" = companies.leadWonAt field.
11. Return strict MongoDB operators only. Never emit Extended JSON wrappers like {"$date": "..."} or {"$oid": "..."} in query plans.
12. Aggregation syntax must be valid: never nest stage operators (like $group) inside expressions in $project/$map input.
13. Target achievement must join using `targets.userId = invoices.sales_person` (not invoiceOwner).

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
    {"name": "step1", "type": "find", "collection": "companies", "filter": {"companyName": {"$regex": "Acme", "$options": "i"}}, "limit": 1},
    {"name": "step2", "type": "find", "collection": "deals", "filter": {"company": "{step1._id}"}, "sort": [["createdAt", -1]]}
  ],
  "response_hint": "summary|list|table|count|detail",
  "entity": {"type": "company", "name": "Acme"}
}

Never mix incompatible keys (example: do not include "pipeline" in a "find" plan).

IMPORTANT NOTES:
- For name-based searches, ALWAYS use case-insensitive regex: {"$regex": "name", "$options": "i"}
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
    parts = [SYSTEM_PROMPT]

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


def build_not_found_response(query: str, collection: str = None) -> str:
    """Generate a response when no data is found."""
    base = f"I could not find any results matching your query."

    suggestions = _get_suggestions(collection)
    if suggestions:
        base += "\n\nHere are some queries you can try:\n"
        for i, s in enumerate(suggestions, 1):
            base += f"{i}. {s}\n"

    return base


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
