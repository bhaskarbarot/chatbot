# ECRM MongoDB Database Metadata
## Complete LLM Query Guide — All 46 Collections

> **Purpose of this file:** Enable an LLM to correctly map any natural-language business question to the right MongoDB collection(s), field names, filter conditions, aggregation logic, and joins (lookups). Read the QUERY PATTERNS and BUSINESS LOGIC sections before generating any MongoDB query.

---

## CRITICAL RULES BEFORE ANY QUERY

1. **Soft Delete Filter:** Collections with `deleted` or `isDeleted` field MUST always include `{ deleted: false }` or `{ isDeleted: false }` in every query — records are never physically deleted.
2. **USD vs Original Currency:** Financial totals exist in two forms — `grand_total` (original currency) and `grand_total_in_usd` (USD). For cross-company comparison always use `_in_usd` fields.
3. **Deal Stage is a String, not a status enum** — deal pipeline is tracked in `stage` field (free text). "Closed Won" = `dealWonAt` is set OR `stage` includes "Closed Won". "Closed Lost" = `dealLostAt` is set OR `stage` includes "Closed Lost".
4. **Invoice payment_status values:** `'draft'`, `'paid'`, `'confirmed'`, `'cancelled'`, `'partial_payment'` — for "paid invoices" use `payment_status: 'paid'`.
5. **Sales ≠ Invoice:** Sales (`Sales` collection) are sales orders. Revenue/money received is tracked in the `Invoice` collection via `payment_status`. Do NOT use Sales for revenue queries — use Invoice.
6. **Date field names differ per collection** — see each collection's date field reference below.
7. **Sequence number auto-format:** Deal = `ELS001`, Sales Order = `SO00001`, Invoice = `ELSN0001`.
8. **All ObjectId references** must use `new mongoose.Types.ObjectId(id)` in queries.

---

## COLLECTION CATALOG

---

### 1. `deals` — Deal / Opportunity Management

**Purpose:** Core sales pipeline. Each record = one sales opportunity being tracked through stages.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `name` | String | Deal title/name |
| `sequence_number` | Number | Auto-increment integer |
| `deal_number` | Virtual String | Formatted as `'ELS' + padded(sequence_number)` e.g. ELS001 |
| `stage` | String | Current pipeline stage (free text, e.g. `'Analysis - To be Quoted'`, `'Closed Won'`, `'Closed Lost'`) |
| `owner` | ObjectId → `users` | Sales rep who owns the deal |
| `createdByOwner` | ObjectId → `users` | Who originally created it |
| `contact` | ObjectId → `contacts` | Primary contact person |
| `company` | ObjectId → `companies` | Client company |
| `business_analyst` | ObjectId → `users` | BA assigned |
| `project_type` | ObjectId → `ProjectType` | Project category |
| `grand_total` | Number | Total deal value (original currency) |
| `grand_total_in_usd` | Number | Total deal value converted to USD |
| `subtotal` | Number | Before tax/discount |
| `subtotal_in_usd` | Number | Subtotal in USD |
| `currency` | String | Currency code (inferred from company) |
| `lineItems` | Array | Products in deal — see lineItem schema |
| `dealWonAt` | Date | Set when deal is won — NULL means not won |
| `dealLostAt` | Date | Set when deal is lost — NULL means not lost |
| `closeDate` | Date | Expected close date |
| `deleted` | Boolean | Soft delete flag — ALWAYS filter `{ deleted: false }` |
| `deletedAt` | Date | When soft deleted |
| `Pre_stage` | String | Previous stage (before last update) |
| `lastActivity` | Object | `{ type, createdAt, id }` last activity info |
| `createdAt` | Date | Record creation date |
| `updatedAt` | Date | Last update date |

**lineItem Sub-fields:**
| Field | Notes |
|---|---|
| `product` | ObjectId → `products` |
| `product_name` | String (cached name) |
| `quantity` | Number |
| `unit_price` | Number |
| `total_price` | Number (original currency) |
| `total_price_in_usd` | Number (USD converted) |
| `project_type` | ObjectId → `ProjectType` |
| `won` | Boolean — true if this line item was won |
| `lost` | Boolean — true if this line item was lost |
| `reason` | String — loss reason for this item |

**QUERY PATTERNS:**
```javascript
// Closed Won Deals
{ deleted: false, dealWonAt: { $ne: null } }
// OR by stage name:
{ deleted: false, stage: { $regex: /closed won/i } }

// Closed Lost Deals
{ deleted: false, dealLostAt: { $ne: null } }

// Active / Open Deals (not won, not lost)
{ deleted: false, dealWonAt: null, dealLostAt: null }

// Deals by specific owner
{ deleted: false, owner: ObjectId("userId") }

// Deals closed this month (won)
{ deleted: false, dealWonAt: { $gte: startOfMonth, $lte: endOfMonth } }

// Deals in a specific stage
{ deleted: false, stage: "Analysis - To be Quoted" }

// Top deals by value
db.deals.find({ deleted: false }).sort({ grand_total_in_usd: -1 }).limit(10)

// Total pipeline value
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: null, dealLostAt: null } },
  { $group: { _id: null, total: { $sum: "$grand_total_in_usd" } } }
])

// Deals won this month with revenue
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: { $gte: startOfMonth, $lte: endOfMonth } } },
  { $group: { _id: "$owner", revenue: { $sum: "$grand_total_in_usd" }, count: { $sum: 1 } } }
])

// Deal count by stage
db.deals.aggregate([
  { $match: { deleted: false } },
  { $group: { _id: "$stage", count: { $sum: 1 }, total: { $sum: "$grand_total_in_usd" } } }
])
```

---

### 2. `invoices` — Customer Invoices & Revenue

**Purpose:** Financial records of money billed and received. Use for revenue, payment, and billing queries. This is the SOURCE OF TRUTH for actual revenue — not Sales orders.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `invoice_number` | String | Auto-generated (ELSN0001 format) |
| `ecomva_invoice_number` | String | Alternate invoice number (sparse) |
| `indian_invoice_number` | String | Indian format number (sparse) |
| `company` | ObjectId → `companies` | Which company was invoiced |
| `so_number` | ObjectId → `sales` | Linked Sales Order |
| `invoiceOwner` | ObjectId → `users` | Invoice owner |
| `sales_person` | ObjectId → `users` | Sales person |
| `createdBy` | ObjectId → `users` | Who created invoice |
| `createdByCompanyOwner` | ObjectId → `users` | Company owner at creation time |
| `invoice_date` | Date | Invoice issue date (DEFAULT: now) |
| `due_date` | Date | Payment due date |
| `payment_date` | Date | When payment was received (null if unpaid) |
| `payment_status` | String Enum | `'draft'` `'paid'` `'confirmed'` `'cancelled'` `'partial_payment'` |
| `approval_status` | String Enum | `'approved'` `'pending'` `'rejected'` |
| `grand_total` | Number | Total amount (original currency) |
| `grandtotal_in_usd` | Number | Total in USD |
| `subtotal` | Number | Before tax/discount |
| `subtotal_in_usd` | Number | Subtotal in USD |
| `currency` | String | Currency code (default: `'USD'`) |
| `tax_amount` | JSON | Tax breakdown |
| `discount_amount` | JSON | Discount breakdown |
| `items` | Array | Line items — see below |
| `payment_history` | Array | Partial payment records |
| `companyName` | String | Cached company name |
| `companyGST` | String | GST number |
| `BillingAddress` | JSON | Billing address |
| `notes` | String | Invoice notes |
| `terms_conditions` | String | Terms text |
| `sgst_amount` | Number | Indian GST component |
| `cgst_amount` | Number | Indian GST component |
| `deleted` | Boolean | Soft delete — ALWAYS filter `{ deleted: false }` |
| `createdAt` | Date | Record creation |
| `updatedAt` | Date | Last update |

**items Sub-fields:**
| Field | Notes |
|---|---|
| `product` | ObjectId → `products` |
| `product_name` | String |
| `project_type` | ObjectId → `ProjectType` |
| `quantity` | Number |
| `unit_price` | Number |
| `total_price` | Number (original currency) |
| `total_price_in_usd` | Number (USD) |
| `tax` | Number (%) |
| `discount` | JSON |
| `is_cross_sell` | Boolean |

**QUERY PATTERNS:**
```javascript
// All paid invoices
{ deleted: false, payment_status: 'paid' }

// Revenue last month (paid invoices)
{
  deleted: false,
  payment_status: 'paid',
  invoice_date: { $gte: ISODate("2024-03-01"), $lte: ISODate("2024-03-31") }
}

// Revenue last month using payment_date (when money actually received)
{
  deleted: false,
  payment_status: 'paid',
  payment_date: { $gte: startOfLastMonth, $lte: endOfLastMonth }
}

// Overdue invoices (past due date and not paid)
{
  deleted: false,
  due_date: { $lt: new Date() },
  payment_status: { $nin: ['paid', 'cancelled'] }
}

// Pending invoices (not yet paid)
{ deleted: false, payment_status: { $in: ['draft', 'confirmed', 'partial_payment'] } }

// Total revenue this year
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid', invoice_date: { $gte: startOfYear } } },
  { $group: { _id: null, totalRevenue: { $sum: "$grandtotal_in_usd" } } }
])

// Monthly revenue trend
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $group: {
    _id: { year: { $year: "$invoice_date" }, month: { $month: "$invoice_date" } },
    revenue: { $sum: "$grandtotal_in_usd" },
    count: { $sum: 1 }
  }},
  { $sort: { "_id.year": 1, "_id.month": 1 } }
])

// Revenue by company
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $group: { _id: "$company", total: { $sum: "$grandtotal_in_usd" }, count: { $sum: 1 } } },
  { $lookup: { from: 'companies', localField: '_id', foreignField: '_id', as: 'companyInfo' } },
  { $sort: { total: -1 } }
])

// Revenue by sales person
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $group: { _id: "$sales_person", total: { $sum: "$grandtotal_in_usd" } } },
  { $lookup: { from: 'users', localField: '_id', foreignField: '_id', as: 'user' } }
])
```

---

### 3. `companies` — Company / Account Management

**Purpose:** Master record for each client organization. Hub that deals, contacts, invoices, and sales all link to.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `companyName` | String | Company name |
| `companyOwner` | ObjectId → `users` | Assigned sales rep |
| `createdBy` | ObjectId → `users` | Who created the record |
| `industry` | String | Industry vertical |
| `type` | String Enum | `'Prospect'` `'Partner'` `'Reseller'` `'Vendor'` `'Other'` |
| `region` | ObjectId → `regions` | Which sales region |
| `country` | String | Country name |
| `city` | String | City |
| `stateRegion` | String | State/province |
| `lifecycleStage` | String | CRM lifecycle stage name |
| `leadStatus` | String | Current lead status name |
| `currency` | String | Preferred currency |
| `email` | String | Primary email |
| `phoneNumber` | String | Phone |
| `websiteUrl` | String | Website |
| `webTechnologies` | Array of ObjectId → `technologies` | Tech stack |
| `numberOfEmployees` | Number | Company size |
| `annualRevenue` | String | Revenue band |
| `source` | ObjectId → `sources` | How the lead came in |
| `tags` | Array of Strings | Custom tags |
| `leadWonAt` | Date | When became a customer (won) |
| `leadLostAt` | Date | When marked as lost |
| `wonByCompanyOwner` | ObjectId → `users` | Who closed the deal |
| `inActiveSince` | Date | When marked inactive |
| `inActiveBy` | ObjectId → `users` | Who marked inactive |
| `inactiveReason` | String | Reason for inactivity |
| `lastBusinessDate` | Date | Last transaction/activity |
| `BillingAddress` | JSON String | Billing address object |
| `meta_data` | Object | Flexible extra data |
| `deleted` | Boolean | Soft delete — ALWAYS filter `{ deleted: false }` |
| `createdAt` | Date | When record was created |
| `updatedAt` | Date | Last update |
| `clientHealth` | String | Client health status |
| `gst` | String | GST number |
| `userType` | String Enum | `'end user'` `'IT reseller'` |
| `odoo_company_id` | String | External system ID |

**QUERY PATTERNS:**
```javascript
// All active companies
{ deleted: false }

// Companies by region
{ deleted: false, region: ObjectId("regionId") }

// Won customers (leadWonAt set)
{ deleted: false, leadWonAt: { $ne: null } }

// Lost customers
{ deleted: false, leadLostAt: { $ne: null } }

// Inactive companies
{ deleted: false, inActiveSince: { $ne: null } }

// Prospects (not won, not lost, not inactive)
{ deleted: false, leadWonAt: null, leadLostAt: null, inActiveSince: null }

// Companies by country
{ deleted: false, country: "India" }

// Companies by owner
{ deleted: false, companyOwner: ObjectId("userId") }

// Companies with specific technology
{ deleted: false, webTechnologies: ObjectId("techId") }

// Companies created this month
{ deleted: false, createdAt: { $gte: startOfMonth, $lte: endOfMonth } }

// Companies count by lifecycleStage
db.companies.aggregate([
  { $match: { deleted: false } },
  { $group: { _id: "$lifecycleStage", count: { $sum: 1 } } }
])
```

---

### 4. `contacts` — Individual Contact Persons

**Purpose:** Individual people linked to companies. Used in deals, tasks, notes.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `firstName` | String | First name |
| `lastName` | String | Last name |
| `email` | String | Required, unique per contact |
| `phoneNumber` | String | Phone |
| `jobTitle` | String | Role/position |
| `company` | ObjectId → `companies` | Which company they work at |
| `contactOwner` | ObjectId → `users` | Assigned sales rep |
| `createdBy` | ObjectId → `users` | Who created |
| `lifecycleStage` | String | CRM stage |
| `leadStatus` | String | Lead status name |
| `source` | ObjectId → `sources` | Lead source |
| `isPrimary` | Boolean | Is primary contact for company |
| `birthday` | Date | Date of birth |
| `contactLinkedIn` | String | LinkedIn URL |
| `deleted` | Boolean | Soft delete — ALWAYS filter `{ deleted: false }` |
| `createdAt` | Date | Creation date |
| `updatedAt` | Date | Last update |
| `odoo_contact_id` | String | External system ID |

**QUERY PATTERNS:**
```javascript
// All active contacts
{ deleted: false }

// Contacts for a company
{ deleted: false, company: ObjectId("companyId") }

// Primary contacts only
{ deleted: false, isPrimary: true }

// Contacts by owner
{ deleted: false, contactOwner: ObjectId("userId") }

// Contacts without a company (orphan contacts)
{ deleted: false, company: null }
```

---

### 5. `sales` — Sales Orders

**Purpose:** Sales orders created for clients. Link between a company and future invoice. NOT the revenue record — invoices are. Sales orders track what was agreed to be sold.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `sales_number` | String | Auto-generated format: `SO00001` |
| `salesOwner` | ObjectId → `users` | Sales rep who owns this order |
| `company` | ObjectId → `companies` | Customer company |
| `createdByCompanyOwner` | ObjectId → `users` | Company owner at time of creation |
| `items` | Array | Line items — product, qty, price |
| `subtotal` | Number | Sub-total |
| `tax` | Number | Tax % |
| `tax_name` | String | Tax label |
| `tax_amount` | Number | Calculated tax amount |
| `discount_amount` | Number | Discount applied |
| `grand_total` | Number | Total amount |
| `status` | String | Order status (free text) |
| `activities` | String | Activity notes |
| `isRecurring` | Boolean | Is this a recurring order |
| `recurringDay` | String | Recurrence day/pattern |
| `sales_date` | Date | Order date (DEFAULT: now) |
| `sales_updated_date` | Date | Last update date |
| `contract` | Object | Uploaded contract file |
| `paymentReceipt` | Object | Uploaded payment receipt |
| `lineupEmailComments` | String | Lineup email notes |
| `isLineupMailSended` | Boolean | Was lineup email sent |
| `deleted` | Boolean | Soft delete — ALWAYS filter `{ deleted: false }` |
| `createdAt` | Date | Creation date |
| `updatedAt` | Date | Last update |
| `odoo_sales_id` | String | External ID |

**items Sub-fields:**
| Field | Notes |
|---|---|
| `product` | ObjectId → `products` |
| `product_name` | String |
| `quantity` | Number |
| `unit_price` | Number |
| `discount` | Number |
| `discount_type` | String |
| `project_type` | ObjectId → `ProjectType` |
| `total_price` | Number |

**QUERY PATTERNS:**
```javascript
// All active sales orders
{ deleted: false }

// Sales by company
{ deleted: false, company: ObjectId("companyId") }

// Recurring sales orders
{ deleted: false, isRecurring: true }

// Sales by date range
{ deleted: false, sales_date: { $gte: startDate, $lte: endDate } }

// Find invoice for a sales order
db.invoices.find({ deleted: false, so_number: ObjectId("salesId") })
```

> **IMPORTANT:** Sales orders don't have payment_status. To know if a sales order is paid, look it up in the `invoices` collection via `so_number` field and check `payment_status`.

---

### 6. `outreaches` — Outreach Prospects

**Purpose:** Cold outreach prospects being tracked before they become CRM contacts/companies. Separate from main CRM pipeline — used by BDR/outreach teams.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `name` | String | Prospect name |
| `email` | String | Email (lowercase) |
| `phone` | String | Phone |
| `website` | String | Website |
| `linkedin` | String | LinkedIn profile |
| `designation` | String | Job title |
| `city` | String | City |
| `country` | String | Country |
| `status` | String Enum | `'Unassigned'` `'Not Contacted'` `'Contacted'` `'Followup'` `'Converted to Deal'` |
| `leadStatus` | String Enum | `'-'` `'Nurturing'` `'Lost'` `'Intrested'` `'Mislabeled'` `'Converted to Deal'` `'To Be Verified'` `'Verified'` |
| `priority` | String Enum | `'-'` `'Low'` `'Medium'` `'High'` |
| `campaign` | ObjectId → `campaigns` | Which campaign this belongs to |
| `region` | ObjectId → `regions` | Sales region |
| `assignedTo` | ObjectId → `users` | Assigned BDR/rep |
| `createdBy` | ObjectId → `users` | Who added this prospect |
| `interestedDate` | Date | When prospect showed interest |
| `convertedDate` | Date | When converted to deal |
| `conversionComments` | String | Notes on conversion |
| `verificationComments` | String | Verification notes |
| `ReminderDate` | Date | Next follow-up reminder |
| `lastAddedNote` | Date | When last note was added |
| `sourceFile` | String | Which import file it came from |
| `isDeleted` | Boolean | Soft delete — ALWAYS filter `{ isDeleted: false }` |
| `createdAt` | Date | When added |
| `updatedAt` | Date | Last update |

> **NOTE:** Uses `isDeleted` (not `deleted`) for soft delete.

**QUERY PATTERNS:**
```javascript
// Active prospects
{ isDeleted: false }

// Uncontacted prospects
{ isDeleted: false, status: 'Not Contacted' }

// High priority prospects needing follow-up
{ isDeleted: false, status: 'Followup', priority: 'High' }

// Prospects converted to deals
{ isDeleted: false, status: 'Converted to Deal' }

// Verified leads
{ isDeleted: false, leadStatus: 'Verified' }

// Prospects by region
{ isDeleted: false, region: ObjectId("regionId") }

// Prospects by campaign
{ isDeleted: false, campaign: ObjectId("campaignId") }

// Prospects assigned to a rep
{ isDeleted: false, assignedTo: ObjectId("userId") }

// Prospects needing reminder today
{ isDeleted: false, ReminderDate: { $lte: new Date() } }

// Conversion rate calculation
db.outreaches.aggregate([
  { $match: { isDeleted: false } },
  { $group: {
    _id: null,
    total: { $sum: 1 },
    converted: { $sum: { $cond: [{ $eq: ["$status", "Converted to Deal"] }, 1, 0] } }
  }}
])
```

---

### 7. `users` — System Users

**Purpose:** CRM users (sales reps, admins, managers). All ownership and assignment fields reference this collection.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `name` | String | Full name |
| `email` | String | Login email (unique) |
| `password` | String | MD5 hashed password |
| `department` | ObjectId → `departments` | Their department |
| `regionId` | ObjectId → `regions` | Primary region |
| `regions` | Array of ObjectId → `regions` | All regions they manage |
| `reporting_manager` | ObjectId → `users` | Their manager |
| `isAdmin` | Boolean | Admin flag |
| `isSuperAdmin` | Boolean | Super admin flag |
| `isRegionHead` | Boolean | Region head flag |
| `isActive` | Boolean | Account active (default: true) |
| `checkLimitedAccess` | Boolean | Restricted access flag |
| `googleAccessToken` | String | Google OAuth access token |
| `googleAccessEmail` | String | Connected Google email |
| `lastEmailSync` | Date | Last Gmail sync time |
| `lastLogin` | Date | Last login timestamp |
| `img` | String | Profile image URL |
| `tokens` | Object | OAuth tokens object |
| `odoo_user_id` | Number | External Odoo user ID |
| `createdAt` | Date | Account creation |
| `updatedAt` | Date | Last update |

**QUERY PATTERNS:**
```javascript
// All active users
{ isActive: true }

// All admins
{ isActive: true, isAdmin: true }

// Region heads
{ isActive: true, isRegionHead: true }

// Users in a region
{ isActive: true, regions: ObjectId("regionId") }

// Reporting chain (subordinates of a manager)
{ isActive: true, reporting_manager: ObjectId("managerId") }

// Users with Google connected
{ isActive: true, googleAccessToken: { $ne: null } }
```

---

### 8. `createtasks` — Tasks & Reminders

**Purpose:** Tasks assigned to deals, companies, contacts, or invoices. Tracks to-dos with priority, reminders, and recurrence.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `Task` | String | Task name/description |
| `status` | String Enum | `'Pending'` `'Completed'` |
| `category` | String Enum | `'Open'` `'Close'` |
| `priority` | String Enum | `'Low'` `'Medium'` `'High'` (required) |
| `createdBy` | ObjectId → `users` | Who created the task |
| `dealsId` | ObjectId → `deals` | Linked deal (if any) |
| `companyId` | ObjectId → `companies` | Linked company (if any) |
| `company` | ObjectId → `companies` | Also company ref (duplicate field) |
| `contectId` | ObjectId → `contacts` | Linked contact (if any) |
| `invoiceId` | ObjectId → `invoices` | Linked invoice (if any) |
| `salesId` | ObjectId → `sales` | Linked sales order (if any) |
| `associated_module` | String | Module name: `'Deal'` `'Company'` `'Contact'` `'Invoice'` |
| `associated_item` | String | ID of associated item (as string) |
| `due_date` | Date | Task due date |
| `time` | Date | Task due time |
| `closedDate` | Date | When completed |
| `setToRepeat` | String | Recurrence type (`'None'`, `'Daily'`, etc.) |
| `repeatDateTime` | String | Recurrence details |
| `reminder` | String Enum | `'At task due date'` `'10 min before'` `'30 min before'` `'1 hour before'` `'1 day before'` `'2 day before'` `'3 day before'` |
| `lastReminderSent` | Date | Last time reminder was sent |
| `note` | String | Extra notes |
| `deleted` | Boolean | Soft delete — ALWAYS filter `{ deleted: false }` |
| `createdAt` | Date | Created |
| `updatedAt` | Date | Updated |
| `odoo_task_id` | String | External ID |

> **NOTE:** Uses `deleted` (not `isDeleted`) for soft delete.

**QUERY PATTERNS:**
```javascript
// All pending tasks
{ deleted: false, status: 'Pending' }

// Overdue tasks
{ deleted: false, status: 'Pending', due_date: { $lt: new Date() } }

// High priority pending tasks
{ deleted: false, status: 'Pending', priority: 'High' }

// Tasks for a specific deal
{ deleted: false, dealsId: ObjectId("dealId") }

// Tasks for a specific company
{ deleted: false, companyId: ObjectId("companyId") }

// Tasks due today
{
  deleted: false,
  status: 'Pending',
  due_date: { $gte: startOfToday, $lte: endOfToday }
}

// Tasks by user (created by them)
{ deleted: false, createdBy: ObjectId("userId") }

// Tasks needing reminder (lastReminderSent is null or past)
{ deleted: false, status: 'Pending', lastReminderSent: null }
```

---

### 9. `bills` — Vendor Bills / Payables

**Purpose:** Accounts payable — bills received from vendors. Opposite of invoices (which are outgoing).

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `vendor` | ObjectId → `vendors` | The vendor who sent this bill |
| `vendorInvoiceNo` | String | Vendor's own invoice number |
| `systemBillNo` | String | Our internal bill number |
| `projectSalesOrder` | String | Related sales order ref |
| `billDate` | Date | Bill date |
| `dueDate` | Date | Payment due date |
| `billType` | String Enum | `'Service'` `'Product'` `'Subscription'` `'AMC'` `'Cloud'` `'Freelancer'` |
| `lineItems` | Array | Description, quantity, unitPrice, amount |
| `subtotal` | Number | Sub-total |
| `discount` | Number | Discount amount |
| `taxableAmount` | Number | Taxable portion |
| `gstPercent` | Number | GST % |
| `tdsPercent` | Number | TDS % |
| `tdsAmount` | Number | TDS deducted |
| `netPayableAmount` | Number | Final amount to pay |
| `status` | String Enum | `'Submitted'` `'Approved'` `'Payment Scheduled'` `'Paid'` `'Rejected'` |
| `createdBy` | ObjectId → `users` | Who submitted |
| `attachInvoice` | Object | Uploaded invoice file |
| `createdAt` | Date | Created |
| `updatedAt` | Date | Updated |

**QUERY PATTERNS:**
```javascript
// Pending approval bills
{ status: 'Submitted' }

// Approved but not paid
{ status: { $in: ['Approved', 'Payment Scheduled'] } }

// Paid bills
{ status: 'Paid' }

// Overdue bills (due date passed, not paid)
{ dueDate: { $lt: new Date() }, status: { $nin: ['Paid', 'Rejected'] } }

// Bills by vendor
{ vendor: ObjectId("vendorId") }

// Total payable amount
db.bills.aggregate([
  { $match: { status: { $nin: ['Paid', 'Rejected'] } } },
  { $group: { _id: null, total: { $sum: "$netPayableAmount" } } }
])
```

---

### 10. `vendors` — Vendor Master Data

**Purpose:** Supplier/vendor records with contact persons and bank details.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `companyName` | String | Vendor company name (required) |
| `email` | String | Primary email |
| `phone` | String | Phone |
| `technology` | Array of ObjectId → `technologies` | Tech expertise |
| `GSTIN` | String | GST identification |
| `PAN` | String | PAN number |
| `website` | String | Website |
| `address` | String | Address |
| `city` | String | City |
| `state` | String | State |
| `country` | String | Country |
| `currency` | String | Billing currency |
| `stage` | String Enum | `'Pending Approval'` `'Active'` `'Inactive'` `'Blacklisted'` |
| `contactPersons` | Array | `{ name, email, mobile, isPrimary }` |
| `bankDetails` | Object | `{ bankName, accountNumber, accountHolderName, ifscOrSwift, branch }` |
| `createdBy` | ObjectId → `users` | Who added |
| `createdAt` | Date | Created |

---

### 11. `products` — Product Catalog

**Purpose:** Products/services that can be added to deals, sales orders, and invoices.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `name` | String | Product name (required) |
| `description_short` | String | Short description |
| `description_long` | String | Long description |
| `product_type` | ObjectId → `ProjectType` | Product category/type |
| `sku` | String | Stock keeping unit (unique, sparse) |
| `billing_frequency` | String | Monthly, Annual, etc. |
| `term` | String | Contract term |
| `url` | String | Product URL |
| `unit_cost` | Number | Default price (required, default: 0) |
| `currency` | String | Price currency (default: `'USD'`) |
| `technology` | Array of ObjectId → `technologies` | Associated technologies |
| `tax_rate` | Number | Default tax % |
| `isActive` | Boolean | Is available (default: true) |
| `isNoNBillable` | Boolean | Non-billable flag |
| `createdAt` | Date | Created |

---

### 12. `notifications` — System Notifications

**Purpose:** In-app notifications for users about tasks, outreach reminders, and emails.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `receivers` | Array of ObjectId → `users` | Who receives this notification |
| `type` | String Enum | `'Followup Reminder'` `'Verification Request'` `'Verified'` `'Task Reminder'` `'Task Assigned'` `'New Email Received'` |
| `outreachId` | ObjectId → `outreaches` | Related outreach prospect |
| `taskId` | ObjectId → `createtasks` | Related task |
| `contactId` | ObjectId → `contacts` | Related contact |
| `readBy` | Array of ObjectId → `users` | Who has read it |
| `isDeleted` | Boolean | Soft delete flag |
| `createdAt` | Date | Created |

**QUERY PATTERNS:**
```javascript
// Unread notifications for a user
{ isDeleted: false, receivers: ObjectId("userId"), readBy: { $ne: ObjectId("userId") } }

// All unread task reminders
{ isDeleted: false, type: 'Task Reminder', readBy: { $size: 0 } }
```

---

### 13. `emails` — Gmail Synced Emails

**Purpose:** Stores Gmail emails synced from users' connected Google accounts.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `user` | ObjectId → `users` | Which user's inbox |
| `messageId` | String | Gmail message ID (unique) |
| `threadId` | String | Gmail thread ID |
| `from` | String | Sender email |
| `to` | Array of Strings | Recipients |
| `cc` | Array of Strings | CC recipients |
| `bcc` | Array of Strings | BCC |
| `subject` | String | Email subject |
| `date` | Date | Email date |
| `snippet` | String | Short preview text |
| `body` | String | Full email body (base64) |
| `createdAt` | Date | When synced |

---

### 14. `targets` — Sales Targets

**Purpose:** Monthly revenue targets assigned to each user.

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `userId` | ObjectId → `users` | The user this target is for |
| `month` | Number | Month index (0=January, 11=December) |
| `year` | Number | 4-digit year |
| `targetInUSD` | Number | Target amount in USD |
| `teamName` | String | Team name |
| `createdBy` | ObjectId → `users` | Who set the target |
| `updatedBy` | ObjectId → `users` | Who last updated |

> **IMPORTANT:** `month` is 0-indexed (0 = January, 11 = December).

**QUERY PATTERNS:**
```javascript
// Target for a user for current month (March = month 2)
{ userId: ObjectId("userId"), month: 2, year: 2024 }

// All targets for a year
{ year: 2024 }

// Achieve vs Target (join with invoices)
db.targets.aggregate([
  { $match: { year: 2024, month: 2 } },
  { $lookup: {
    from: 'invoices',
    let: { userId: '$userId' },
    pipeline: [
      { $match: { $expr: {
        $and: [
          { $eq: ['$sales_person', '$$userId'] },
          { $eq: [{ $month: '$invoice_date' }, 3] }, // month 3 = March
          { $eq: [{ $year: '$invoice_date' }, 2024] },
          { $eq: ['$payment_status', 'paid'] },
          { $eq: ['$deleted', false] }
        ]
      }}}
    ],
    as: 'invoices'
  }},
  { $addFields: { achieved: { $sum: '$invoices.grandtotal_in_usd' } } }
])
```

---

### 15. `notes` (CommonNote) — Notes Across All Modules

**Purpose:** General notes attached to any CRM record (Company, Deal, Contact, Sales, Invoice, Task).

**Key Fields:**
| Field | Type | Notes |
|---|---|---|
| `_id` | ObjectId | Primary key |
| `note` | String | Note content (required) |
| `type` | String Enum | `'Company'` `'Deal'` `'Task'` `'Contact'` `'Sales'` `'Invoice'` |
| `createdBy` | ObjectId → `users` | Author |
| `companyId` | ObjectId → `companies` | Company context (required) |
| `dealId` | ObjectId → `deals` | Deal context |
| `taskId` | ObjectId → `createtasks` | Task context |
| `contactId` | ObjectId → `contacts` | Contact context |
| `salesId` | ObjectId → `sales` | Sales context |
| `invoiceId` | ObjectId → `invoices` | Invoice context |
| `isPinned` | Boolean | Pinned to top |
| `isLog` | Boolean | Is an activity log entry |
| `isFromTaskhub` | Boolean | From Taskhub integration |
| `attachment` | Object | File attachment |
| `mentionedUsers` | Array | `{ user: ObjectId, label: String }` |
| `createdAt` | Date | Created |

---

### 16. `regions` — Sales Regions

**Purpose:** Geographic/organizational regions for segmenting teams, outreach, and reports.

**Key Fields:**
| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `regionName` | String (unique) — e.g. `'APAC'`, `'EMEA'`, `'USA'`, `'UAE'` |
| `createdAt` | Date |

---

### 17. `campaigns` — Outreach Campaigns

**Purpose:** Groups outreach prospects into campaign buckets.

**Key Fields:**
| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `campaignName` | String (unique, required) |
| `categoryId` | ObjectId → `categories` |
| `createdBy` | ObjectId → `users` |

---

### 18. `departments` — Department Master

**Purpose:** Department definitions for user org structure.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String |

---

### 19. `projecttypes` — Project Type Master

**Purpose:** Classification of project types used in deals, sales, invoices.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String (unique, required) |

---

### 20. `technologies` — Technology Master

**Purpose:** Web technology tags used on companies and products.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String |
| `category` | ObjectId → `technologycategories` |

---

### 21. `technologycategories` — Technology Category Master

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `categoryName` | String (unique) |

---

### 22. `sources` — Lead Source Master

**Purpose:** How a lead was acquired (e.g. Website, Referral, Cold Call).

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `sourceName` | String (unique, capitalized) |

---

### 23. `taxes` — Tax Configuration

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String |
| `amount` | String (tax rate value) |
| `createdBy` | ObjectId → `users` |

---

### 24. `lead_status` — Lead Status Master

**Purpose:** Valid lead status values used on companies and contacts.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String |

---

### 25. `lifecycle_stage` — Lifecycle Stage Master

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `name` | String |

---

### 26. `dealstagesettings` — Custom Deal Stages

**Purpose:** Custom deal pipeline stage configuration.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `dealStageName` | String |
| `isZeroAcceptable` | Boolean — whether $0 deals are valid at this stage |
| `deleted` | Boolean — ALWAYS filter `{ deleted: false }` |
| `lastUpdatedBy` | ObjectId → `users` |
| `createdBy` | ObjectId → `users` |

---

### 27. `countryregions` — Country to Region Mapping

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `country` | String |
| `region` | String |

---

### 28. `meetings` — Meeting Records

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `title` | String (required) |
| `description` | String |
| `start` | Date (required) |
| `end` | Date (required) |
| `location` | String |
| `attendees` | Array of Strings |
| `recurrence` | String |
| `eventId` | String — Google Calendar event ID |
| `createdBy` | ObjectId → `users` |
| `createdAt` | Date |

---

### 29. `remotejobs` — Remote Job Board

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `companyName` | String |
| `position` | String |
| `description` | String |
| `city` | String |
| `country` | String |
| `region` | ObjectId → `regions` |
| `budget` | String |
| `postedDate` | Date |
| `assignedTo` | ObjectId → `users` |
| `isActioned` | Boolean |
| `isExpiringSoon` | Boolean |
| `isDeleted` | Boolean — filter `{ isDeleted: false }` |
| `redirection_url` | String |

---

### 30. `payments` — Payment Method Master

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `payment_name` | String |
| `description` | String |
| `payment_fee` | Number |
| `payment_link` | String |

---

### 31. `publicleads` — Public Web Form Leads

**Purpose:** Leads submitted via external web forms/webhooks. Flexible schema.

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `posted_data` | Mixed — any form data |
| `createdAt` | Date |

---

### 32. `deletedcompanies` — Deleted Company Audit Trail

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `companyName` | String |
| `deletedBy` | String (user name) |
| `deletionTime` | Date |
| `companyDetails` | Mixed — full company snapshot |

---

### 33. `bademails` — Invalid Email Tracking

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `email` | String |
| `quality` | String — quality score/reason |
| `sourceFile` | String |
| `originalOutReachId` | ObjectId → `outreaches` |
| `processedAt` | Date |
| `notes` | String |

---

### 34. `mails` (UserEmail) — IMAP Synced Emails

**Purpose:** Emails synced via IMAP protocol (separate from Gmail API emails).

| Field | Notes |
|---|---|
| `_id` | ObjectId |
| `userEmail` | String |
| `mailId` | String |
| `mailbox` | String |
| `from` | String |
| `to` | String |
| `subject` | String |
| `body` | String |
| `date` | String |
| `parsedDate` | Date |
| `attachments` | Array of `{ filename, url, size, contentType }` |

---

### 35–46. Activity & Note Sub-Collections

| Collection | Model Name | Purpose |
|---|---|---|
| `activities` | `Activity` | Activity type definitions (names) |
| `activityevents` | `ActivityEvent` | Log of activities with creator |
| `outreachactivities` | `OutreachActivity` | Region-level outreach activity count |
| `dealsnotes` | `DealsNote` | Notes on deals |
| `contactsnotes` | `ContactsNote` | Notes on contacts |
| `companynotes` | `CompanyNote` | Notes on companies |
| `salesnotes` | `SalesNote` | Notes on sales orders |
| `notes` | `Note` | Notes on outreach prospects |
| `statuses` | `Status` | Status name definitions |
| `categorys` | `Category` | Category master |
| `remotejobNotes` | `RemoteJobNote` | Notes on remote job listings |

---

## CROSS-COLLECTION RELATIONSHIPS MAP

```
User ←────────────────────── owns/creates almost everything
  │
  ├── companies (companyOwner, createdBy)
  │     ├── contacts (company)
  │     ├── deals (company)
  │     │     └── lineItems → products → projecttypes
  │     ├── invoices (company) ←── so_number ──── sales (company)
  │     └── tasks (companyId)
  │
  ├── outreaches (assignedTo, createdBy)
  │     ├── campaigns (campaign) → categories
  │     └── regions (region)
  │
  ├── targets (userId)
  │
  └── emails / mails (user)

regions ──── companies, outreaches, users, targets, remotejobs
technologies ── companies (webTechnologies), products, vendors
```

---

## BUSINESS QUERY PATTERN DICTIONARY

This section maps natural language questions to the correct MongoDB query strategy.

---

### DEALS & PIPELINE

| Question | Collection | Key Filters |
|---|---|---|
| "Closed won deals" | `deals` | `{ deleted: false, dealWonAt: { $ne: null } }` |
| "Closed lost deals" | `deals` | `{ deleted: false, dealLostAt: { $ne: null } }` |
| "Open/active deals" | `deals` | `{ deleted: false, dealWonAt: null, dealLostAt: null }` |
| "Deals by stage" | `deals` | Group by `stage` field |
| "Pipeline value" | `deals` | Sum `grand_total_in_usd` where open |
| "Deals won this month" | `deals` | `dealWonAt` range + `$ne: null` |
| "Deals by sales rep" | `deals` | Filter by `owner` (ObjectId) |
| "Deal win rate" | `deals` | Count won / count (won+lost) |
| "Average deal size" | `deals` | Avg of `grand_total_in_usd` where won |
| "Deals closing soon" | `deals` | `closeDate` range, open deals |
| "Deals by company" | `deals` | Filter by `company` field |

---

### REVENUE & INVOICES

| Question | Collection | Key Filters |
|---|---|---|
| "Revenue last month" | `invoices` | `payment_status: 'paid'`, `invoice_date` last month range |
| "Paid invoices" | `invoices` | `{ deleted: false, payment_status: 'paid' }` |
| "Unpaid / outstanding invoices" | `invoices` | `payment_status: { $in: ['draft', 'confirmed'] }` |
| "Overdue invoices" | `invoices` | `due_date: { $lt: now }`, status not paid/cancelled |
| "Partial payments" | `invoices` | `payment_status: 'partial_payment'` |
| "Revenue by rep" | `invoices` | Group by `sales_person` |
| "Revenue by company" | `invoices` | Group by `company` |
| "Monthly revenue trend" | `invoices` | Group by year+month of `invoice_date` |
| "Total receivables" | `invoices` | Sum `grandtotal_in_usd` where not paid |
| "Revenue this year" | `invoices` | `invoice_date` in year range, `payment_status: 'paid'` |

---

### COMPANIES & ACCOUNTS

| Question | Collection | Key Filters |
|---|---|---|
| "Active companies" | `companies` | `{ deleted: false }` |
| "Won customers" | `companies` | `leadWonAt: { $ne: null }` |
| "Lost prospects" | `companies` | `leadLostAt: { $ne: null }` |
| "Inactive accounts" | `companies` | `inActiveSince: { $ne: null }` |
| "Companies by region" | `companies` | Filter by `region` ObjectId |
| "New companies this month" | `companies` | `createdAt` in month range |
| "Companies by owner" | `companies` | Filter by `companyOwner` |

---

### OUTREACH & PROSPECTING

| Question | Collection | Key Filters |
|---|---|---|
| "Prospects to contact" | `outreaches` | `status: 'Not Contacted', isDeleted: false` |
| "Prospects due for follow-up" | `outreaches` | `status: 'Followup', isDeleted: false` |
| "Converted prospects" | `outreaches` | `status: 'Converted to Deal'` |
| "Prospects by campaign" | `outreaches` | Filter by `campaign` |
| "Prospects by region" | `outreaches` | Filter by `region` |
| "Verified leads" | `outreaches` | `leadStatus: 'Verified'` |
| "High priority prospects" | `outreaches` | `priority: 'High'` |

---

### TASKS

| Question | Collection | Key Filters |
|---|---|---|
| "Pending tasks" | `createtasks` | `{ deleted: false, status: 'Pending' }` |
| "Overdue tasks" | `createtasks` | `status: 'Pending', due_date: { $lt: now }` |
| "Tasks due today" | `createtasks` | `due_date` = today range |
| "High priority tasks" | `createtasks` | `priority: 'High', status: 'Pending'` |
| "Tasks for a deal" | `createtasks` | `{ deleted: false, dealsId: ObjectId }` |
| "Completed tasks" | `createtasks` | `status: 'Completed'` |

---

### TARGETS & PERFORMANCE

| Question | Collection | Key Filters / Strategy |
|---|---|---|
| "Sales target this month" | `targets` | `{ userId, month: currentMonth (0-indexed), year }` |
| "Target achievement" | `targets` + `invoices` | Join via `userId` = `sales_person`, filter same month/year in invoices |
| "Who hit target?" | `targets` + `invoices` | Compare `targetInUSD` vs sum of paid invoices `grandtotal_in_usd` |

---

## IMPORTANT ENUM VALUES REFERENCE

### Deal Stages (Common Values — free text field, not enum)
- `'Analysis - To be Quoted'`
- `'Negotiation'`
- `'Closed Won'`
- `'Closed Lost'`
- `'On Hold'`

### Invoice Payment Status
- `'draft'` — Not yet finalized
- `'confirmed'` — Finalized, awaiting payment
- `'paid'` — Payment received
- `'partial_payment'` — Partially paid
- `'cancelled'` — Cancelled invoice

### Invoice Approval Status
- `'pending'`
- `'approved'`
- `'rejected'`

### OutReach Status
- `'Unassigned'`
- `'Not Contacted'`
- `'Contacted'`
- `'Followup'`
- `'Converted to Deal'`

### OutReach Lead Status
- `'-'` (default/none)
- `'Nurturing'`
- `'Lost'`
- `'Intrested'` ← note: typo in DB (not "Interested")
- `'Mislabeled'`
- `'Converted to Deal'`
- `'To Be Verified'`
- `'Verified'`

### Task Status
- `'Pending'`
- `'Completed'`

### Task Priority
- `'Low'`
- `'Medium'`
- `'High'`

### Task Reminder
- `'At task due date'`
- `'10 min before'`
- `'30 min before'`
- `'1 hour before'`
- `'1 day before'`
- `'2 day before'`
- `'3 day before'`

### Company Type
- `'Prospect'`
- `'Partner'`
- `'Reseller'`
- `'Vendor'`
- `'Other'`

### Bill Status
- `'Submitted'`
- `'Approved'`
- `'Payment Scheduled'`
- `'Paid'`
- `'Rejected'`

### Vendor Stage
- `'Pending Approval'`
- `'Active'`
- `'Inactive'`
- `'Blacklisted'`

### Bill Type
- `'Service'`
- `'Product'`
- `'Subscription'`
- `'AMC'`
- `'Cloud'`
- `'Freelancer'`

### Notification Types
- `'Followup Reminder'`
- `'Verification Request'`
- `'Verified'`
- `'Task Reminder'`
- `'Task Assigned'`
- `'New Email Received'`

---

## DATE FIELD REFERENCE BY COLLECTION

| Collection | Date Created | Date Updated | Business Date |
|---|---|---|---|
| `deals` | `createdAt` | `updatedAt` | `dealWonAt`, `dealLostAt`, `closeDate` |
| `invoices` | `createdAt` | `updatedAt` | `invoice_date`, `due_date`, `payment_date` |
| `sales` | `createdAt` | `updatedAt` | `sales_date`, `sales_updated_date` |
| `companies` | `createdAt` | `updatedAt` | `leadWonAt`, `leadLostAt`, `inActiveSince`, `lastBusinessDate` |
| `contacts` | `createdAt` | `updatedAt` | `birthday` |
| `outreaches` | `createdAt` | `updatedAt` | `interestedDate`, `convertedDate`, `ReminderDate` |
| `createtasks` | `createdAt` | `updatedAt` | `due_date`, `closedDate` |
| `targets` | `createdAt` | `updatedAt` | `month` + `year` fields (0-indexed month) |
| `bills` | `createdAt` | `updatedAt` | `billDate`, `dueDate` |
| `meetings` | `createdAt` | — | `start`, `end` |

---

## SOFT DELETE REFERENCE

| Collection | Field Name | Filter Value |
|---|---|---|
| `deals` | `deleted` | `false` |
| `invoices` | `deleted` | `false` |
| `sales` | `deleted` | `false` |
| `companies` | `deleted` | `false` |
| `contacts` | `deleted` | `false` |
| `createtasks` | `deleted` | `false` |
| `dealstagesettings` | `deleted` | `false` |
| `outreaches` | `isDeleted` | `false` |
| `remotejobs` | `isDeleted` | `false` |
| `notifications` | `isDeleted` | `false` |

> Collections NOT in this list do NOT have soft delete — all records are active.

---

## MULTI-COLLECTION QUERY TEMPLATES

### 1. Full Deal Details (with company, contact, owner)
```javascript
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: { $ne: null } } },
  { $lookup: { from: 'companies', localField: 'company', foreignField: '_id', as: 'company' } },
  { $lookup: { from: 'contacts', localField: 'contact', foreignField: '_id', as: 'contact' } },
  { $lookup: { from: 'users', localField: 'owner', foreignField: '_id', as: 'owner' } },
  { $unwind: { path: '$company', preserveNullAndEmptyArrays: true } },
  { $unwind: { path: '$contact', preserveNullAndEmptyArrays: true } },
  { $unwind: { path: '$owner', preserveNullAndEmptyArrays: true } }
])
```

### 2. Invoice with Company and Sales Rep
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $lookup: { from: 'companies', localField: 'company', foreignField: '_id', as: 'company' } },
  { $lookup: { from: 'users', localField: 'sales_person', foreignField: '_id', as: 'salesPerson' } },
  { $unwind: { path: '$company', preserveNullAndEmptyArrays: true } },
  { $unwind: { path: '$salesPerson', preserveNullAndEmptyArrays: true } }
])
```

### 3. Revenue vs Target by User This Month
```javascript
// Step 1: Get targets for month
db.targets.aggregate([
  { $match: { month: <0-indexed-month>, year: <year> } },
  { $lookup: {
    from: 'invoices',
    let: { uid: '$userId' },
    pipeline: [
      { $match: { $expr: {
        $and: [
          { $eq: ['$sales_person', '$$uid'] },
          { $eq: ['$payment_status', 'paid'] },
          { $eq: ['$deleted', false] },
          { $gte: ['$invoice_date', <startOfMonth>] },
          { $lte: ['$invoice_date', <endOfMonth>] }
        ]
      }}}
    ],
    as: 'paidInvoices'
  }},
  { $addFields: { achieved: { $sum: '$paidInvoices.grandtotal_in_usd' } } },
  { $lookup: { from: 'users', localField: 'userId', foreignField: '_id', as: 'user' } }
])
```

### 4. All Tasks for a Deal with Notes
```javascript
// Tasks
db.createtasks.find({ deleted: false, dealsId: ObjectId("dealId") })
// Notes
db.notes.find({ dealId: ObjectId("dealId"), type: 'Deal' })
// Deal-specific notes
db.dealsnotes.find({ deals: ObjectId("dealId") })
```

### 5. Company with All Deals, Invoices, Contacts
```javascript
db.companies.aggregate([
  { $match: { _id: ObjectId("companyId"), deleted: false } },
  { $lookup: { from: 'deals', localField: '_id', foreignField: 'company', as: 'deals',
    pipeline: [{ $match: { deleted: false } }] } },
  { $lookup: { from: 'invoices', localField: '_id', foreignField: 'company', as: 'invoices',
    pipeline: [{ $match: { deleted: false } }] } },
  { $lookup: { from: 'contacts', localField: '_id', foreignField: 'company', as: 'contacts',
    pipeline: [{ $match: { deleted: false } }] } }
])
```

### 6. Outreach Funnel by Region
```javascript
db.outreaches.aggregate([
  { $match: { isDeleted: false } },
  { $lookup: { from: 'regions', localField: 'region', foreignField: '_id', as: 'regionInfo' } },
  { $group: {
    _id: { region: '$region', status: '$status' },
    count: { $sum: 1 }
  }},
  { $lookup: { from: 'regions', localField: '_id.region', foreignField: '_id', as: 'region' } },
  { $sort: { '_id.region': 1 } }
])
```

### 7. Top Performing Sales Reps (Closed Won Deals + Revenue)
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid',
    invoice_date: { $gte: startOfYear, $lte: endOfYear } } },
  { $group: { _id: '$sales_person', revenue: { $sum: '$grandtotal_in_usd' }, invoiceCount: { $sum: 1 } } },
  { $lookup: { from: 'users', localField: '_id', foreignField: '_id', as: 'user' } },
  { $unwind: '$user' },
  { $project: { name: '$user.name', email: '$user.email', revenue: 1, invoiceCount: 1 } },
  { $sort: { revenue: -1 } }
])
```

---

## COMMON MISTAKES TO AVOID

1. **Using `Sales` for revenue** — Sales orders do NOT have payment status. Always use `invoices` for revenue.
2. **Forgetting soft delete** — Always add `{ deleted: false }` or `{ isDeleted: false }`.
3. **Wrong date field** — "Last month revenue" = filter `invoice_date` on `invoices`, NOT `sales_date` on `sales`.
4. **USD vs original currency** — For multi-currency comparisons, use `grand_total_in_usd` / `grandtotal_in_usd` (note: deals use `grand_total_in_usd`, invoices use `grandtotal_in_usd` without underscore before `in`).
5. **Target month is 0-indexed** — January = 0, December = 11.
6. **OutReach uses `isDeleted`** — NOT `deleted` like other collections.
7. **Deal stage is free text** — There is no fixed enum. Query with `$regex` for fuzzy matching or exact string for known stages.
8. **Notes are in multiple collections** — `notes` (CommonNote) is the general one. But `dealsnotes`, `contactsnotes`, `companynotes`, `salesnotes` are separate specialized collections. `Note` (singular, no 's') is specifically for outreach prospects.
9. **Invoice number fields** — There are 3: `invoice_number` (default), `indian_invoice_number`, `ecomva_invoice_number`. Use `invoice_number` unless specifically asked about Indian or Ecomva invoices.
10. **`deals.grand_total_in_usd`** vs **`invoices.grandtotal_in_usd`** — Different field names! Deals use underscore before `in`, invoices do not.

---

## ADVANCED AGGREGATION QUERY LIBRARY

### DASHBOARD QUERIES

#### Total Pipeline Summary (Open Deals)
```javascript
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: null, dealLostAt: null } },
  { $group: {
    _id: null,
    totalDeals: { $sum: 1 },
    totalPipelineValue: { $sum: "$grand_total_in_usd" },
    avgDealSize: { $avg: "$grand_total_in_usd" }
  }}
])
```

#### Win/Loss Ratio for a Period
```javascript
db.deals.aggregate([
  { $match: {
    deleted: false,
    $or: [
      { dealWonAt: { $gte: startDate, $lte: endDate } },
      { dealLostAt: { $gte: startDate, $lte: endDate } }
    ]
  }},
  { $group: {
    _id: null,
    won: { $sum: { $cond: [{ $ne: ["$dealWonAt", null] }, 1, 0] } },
    lost: { $sum: { $cond: [{ $ne: ["$dealLostAt", null] }, 1, 0] } },
    wonValue: { $sum: { $cond: [{ $ne: ["$dealWonAt", null] }, "$grand_total_in_usd", 0] } }
  }}
])
```

#### Monthly Revenue + Invoice Count (Last 12 Months)
```javascript
db.invoices.aggregate([
  { $match: {
    deleted: false,
    payment_status: 'paid',
    invoice_date: { $gte: new Date(new Date().setFullYear(new Date().getFullYear() - 1)) }
  }},
  { $group: {
    _id: { year: { $year: "$invoice_date" }, month: { $month: "$invoice_date" } },
    revenue: { $sum: "$grandtotal_in_usd" },
    count: { $sum: 1 }
  }},
  { $sort: { "_id.year": 1, "_id.month": 1 } }
])
```

#### Revenue by Region (via Company → Region)
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $lookup: {
    from: 'companies',
    localField: 'company',
    foreignField: '_id',
    as: 'companyData'
  }},
  { $unwind: { path: '$companyData', preserveNullAndEmptyArrays: true } },
  { $lookup: {
    from: 'regions',
    localField: 'companyData.region',
    foreignField: '_id',
    as: 'regionData'
  }},
  { $unwind: { path: '$regionData', preserveNullAndEmptyArrays: true } },
  { $group: {
    _id: '$regionData.regionName',
    revenue: { $sum: '$grandtotal_in_usd' },
    invoiceCount: { $sum: 1 }
  }},
  { $sort: { revenue: -1 } }
])
```

#### Deals Won by Rep This Month (with names)
```javascript
db.deals.aggregate([
  { $match: {
    deleted: false,
    dealWonAt: { $gte: startOfMonth, $lte: endOfMonth }
  }},
  { $group: {
    _id: '$owner',
    dealsWon: { $sum: 1 },
    totalValue: { $sum: '$grand_total_in_usd' }
  }},
  { $lookup: { from: 'users', localField: '_id', foreignField: '_id', as: 'user' } },
  { $unwind: '$user' },
  { $project: { name: '$user.name', email: '$user.email', dealsWon: 1, totalValue: 1 } },
  { $sort: { totalValue: -1 } }
])
```

---

### REPORTING QUERIES

#### Weekly Outreach Report by Region
```javascript
db.outreaches.aggregate([
  { $match: {
    isDeleted: false,
    createdAt: { $gte: startOfWeek, $lte: endOfWeek }
  }},
  { $lookup: { from: 'regions', localField: 'region', foreignField: '_id', as: 'region' } },
  { $unwind: { path: '$region', preserveNullAndEmptyArrays: true } },
  { $group: {
    _id: { region: '$region.regionName', status: '$status' },
    count: { $sum: 1 }
  }},
  { $group: {
    _id: '$_id.region',
    statuses: { $push: { status: '$_id.status', count: '$count' } },
    total: { $sum: '$count' }
  }},
  { $sort: { _id: 1 } }
])
```

#### User Monthly Summary (Deals + Revenue)
```javascript
// For a specific user in a month
db.deals.aggregate([
  { $match: {
    deleted: false,
    owner: ObjectId("userId"),
    dealWonAt: { $gte: startOfMonth, $lte: endOfMonth }
  }},
  { $group: {
    _id: null,
    dealsWon: { $sum: 1 },
    totalDealValue: { $sum: '$grand_total_in_usd' }
  }}
])
// Combine with invoices for same user/month:
db.invoices.aggregate([
  { $match: {
    deleted: false,
    sales_person: ObjectId("userId"),
    payment_status: 'paid',
    invoice_date: { $gte: startOfMonth, $lte: endOfMonth }
  }},
  { $group: { _id: null, invoicesRaised: { $sum: 1 }, revenueCollected: { $sum: '$grandtotal_in_usd' } } }
])
```

#### Overdue Invoices with Company and Owner Details
```javascript
db.invoices.aggregate([
  { $match: {
    deleted: false,
    due_date: { $lt: new Date() },
    payment_status: { $nin: ['paid', 'cancelled'] }
  }},
  { $lookup: { from: 'companies', localField: 'company', foreignField: '_id', as: 'company' } },
  { $lookup: { from: 'users', localField: 'invoiceOwner', foreignField: '_id', as: 'owner' } },
  { $unwind: { path: '$company', preserveNullAndEmptyArrays: true } },
  { $unwind: { path: '$owner', preserveNullAndEmptyArrays: true } },
  { $addFields: {
    daysOverdue: {
      $dateDiff: { startDate: '$due_date', endDate: new Date(), unit: 'day' }
    }
  }},
  { $project: {
    invoice_number: 1,
    grandtotal_in_usd: 1,
    due_date: 1,
    daysOverdue: 1,
    payment_status: 1,
    'company.companyName': 1,
    'owner.name': 1,
    'owner.email': 1
  }},
  { $sort: { daysOverdue: -1 } }
])
```

#### Inactive Companies Report (No Invoice in Last 90 Days)
```javascript
const ninetyDaysAgo = new Date(Date.now() - 90 * 24 * 60 * 60 * 1000);
db.companies.aggregate([
  { $match: { deleted: false, leadWonAt: { $ne: null }, inActiveSince: null } },
  { $lookup: {
    from: 'invoices',
    let: { companyId: '$_id' },
    pipeline: [
      { $match: { $expr: {
        $and: [
          { $eq: ['$company', '$$companyId'] },
          { $eq: ['$deleted', false] },
          { $gte: ['$invoice_date', ninetyDaysAgo] }
        ]
      }}}
    ],
    as: 'recentInvoices'
  }},
  { $match: { recentInvoices: { $size: 0 } } },
  { $lookup: { from: 'users', localField: 'companyOwner', foreignField: '_id', as: 'owner' } },
  { $unwind: { path: '$owner', preserveNullAndEmptyArrays: true } },
  { $project: { companyName: 1, country: 1, lastBusinessDate: 1, 'owner.name': 1 } }
])
```

#### Sales Pipeline by Stage with Value
```javascript
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: null, dealLostAt: null } },
  { $group: {
    _id: '$stage',
    count: { $sum: 1 },
    totalValue: { $sum: '$grand_total_in_usd' },
    avgValue: { $avg: '$grand_total_in_usd' }
  }},
  { $sort: { totalValue: -1 } }
])
```

#### Top 10 Clients by Revenue (All Time)
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid' } },
  { $group: { _id: '$company', totalRevenue: { $sum: '$grandtotal_in_usd' }, invoiceCount: { $sum: 1 } } },
  { $sort: { totalRevenue: -1 } },
  { $limit: 10 },
  { $lookup: { from: 'companies', localField: '_id', foreignField: '_id', as: 'company' } },
  { $unwind: '$company' },
  { $project: {
    companyName: '$company.companyName',
    country: '$company.country',
    totalRevenue: 1,
    invoiceCount: 1
  }}
])
```

#### Task Completion Rate by User
```javascript
db.createtasks.aggregate([
  { $match: { deleted: false } },
  { $group: {
    _id: '$createdBy',
    total: { $sum: 1 },
    completed: { $sum: { $cond: [{ $eq: ['$status', 'Completed'] }, 1, 0] } },
    pending: { $sum: { $cond: [{ $eq: ['$status', 'Pending'] }, 1, 0] } }
  }},
  { $addFields: {
    completionRate: { $multiply: [{ $divide: ['$completed', '$total'] }, 100] }
  }},
  { $lookup: { from: 'users', localField: '_id', foreignField: '_id', as: 'user' } },
  { $unwind: { path: '$user', preserveNullAndEmptyArrays: true } },
  { $sort: { completionRate: -1 } }
])
```

#### Outreach Conversion Funnel
```javascript
db.outreaches.aggregate([
  { $match: { isDeleted: false } },
  { $group: {
    _id: '$status',
    count: { $sum: 1 }
  }},
  { $addFields: {
    sortOrder: {
      $switch: {
        branches: [
          { case: { $eq: ['$_id', 'Unassigned'] }, then: 1 },
          { case: { $eq: ['$_id', 'Not Contacted'] }, then: 2 },
          { case: { $eq: ['$_id', 'Contacted'] }, then: 3 },
          { case: { $eq: ['$_id', 'Followup'] }, then: 4 },
          { case: { $eq: ['$_id', 'Converted to Deal'] }, then: 5 }
        ],
        default: 6
      }
    }
  }},
  { $sort: { sortOrder: 1 } }
])
```

---

### VENDOR & PAYABLES QUERIES

#### Total Payables by Vendor
```javascript
db.bills.aggregate([
  { $match: { status: { $nin: ['Paid', 'Rejected'] } } },
  { $group: {
    _id: '$vendor',
    totalPayable: { $sum: '$netPayableAmount' },
    billCount: { $sum: 1 }
  }},
  { $lookup: { from: 'vendors', localField: '_id', foreignField: '_id', as: 'vendor' } },
  { $unwind: '$vendor' },
  { $project: { vendorName: '$vendor.companyName', totalPayable: 1, billCount: 1 } },
  { $sort: { totalPayable: -1 } }
])
```

#### Bills Pending Approval
```javascript
db.bills.aggregate([
  { $match: { status: 'Submitted' } },
  { $lookup: { from: 'vendors', localField: 'vendor', foreignField: '_id', as: 'vendor' } },
  { $lookup: { from: 'users', localField: 'createdBy', foreignField: '_id', as: 'submittedBy' } },
  { $unwind: { path: '$vendor', preserveNullAndEmptyArrays: true } },
  { $unwind: { path: '$submittedBy', preserveNullAndEmptyArrays: true } },
  { $project: {
    systemBillNo: 1,
    billDate: 1,
    dueDate: 1,
    netPayableAmount: 1,
    billType: 1,
    'vendor.companyName': 1,
    'submittedBy.name': 1
  }},
  { $sort: { dueDate: 1 } }
])
```

---

## CHATBOT NATURAL LANGUAGE → QUERY TRANSLATION EXAMPLES

These are example pairs showing how to translate user questions into correct MongoDB queries. Use these as reasoning templates.

---

**Q: "Show me all deals that were closed won last month"**
```
Collection: deals
Filter: deleted=false, dealWonAt is in last month range (not null)
Key logic: "closed won" = dealWonAt field is set (not null), date range on dealWonAt
```
```javascript
const start = new Date('2024-03-01'); // first day of last month
const end = new Date('2024-03-31T23:59:59'); // last day of last month
db.deals.find({ deleted: false, dealWonAt: { $gte: start, $lte: end } })
```

---

**Q: "What is our total revenue this year?"**
```
Collection: invoices (NOT sales)
Filter: deleted=false, payment_status='paid', invoice_date in current year
Aggregate: sum of grandtotal_in_usd
Key logic: revenue = paid invoices only
```
```javascript
db.invoices.aggregate([
  { $match: {
    deleted: false,
    payment_status: 'paid',
    invoice_date: { $gte: new Date('2024-01-01'), $lte: new Date('2024-12-31') }
  }},
  { $group: { _id: null, totalRevenue: { $sum: '$grandtotal_in_usd' } } }
])
```

---

**Q: "Which clients have unpaid invoices?"**
```
Collection: invoices
Filter: deleted=false, payment_status in ['draft','confirmed','partial_payment']
Join: companies for client names
Key logic: unpaid = not 'paid' and not 'cancelled'
```
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: { $in: ['draft', 'confirmed', 'partial_payment'] } } },
  { $lookup: { from: 'companies', localField: 'company', foreignField: '_id', as: 'client' } },
  { $unwind: '$client' },
  { $project: { invoice_number: 1, grandtotal_in_usd: 1, due_date: 1, payment_status: 1, 'client.companyName': 1 } }
])
```

---

**Q: "How many prospects did we contact this week?"**
```
Collection: outreaches (NOT contacts, NOT companies)
Filter: isDeleted=false, status in ['Contacted','Followup','Converted to Deal'], updatedAt in this week
Key logic: outreach = BDR cold prospects, not CRM contacts
```
```javascript
db.outreaches.find({
  isDeleted: false,
  status: { $in: ['Contacted', 'Followup', 'Converted to Deal'] },
  updatedAt: { $gte: startOfWeek, $lte: endOfWeek }
}).count()
```

---

**Q: "Who is the top sales person this quarter?"**
```
Collection: invoices
Filter: deleted=false, payment_status='paid', invoice_date in current quarter
Group by: sales_person
Sort by: sum of grandtotal_in_usd descending
Join: users for names
```
```javascript
db.invoices.aggregate([
  { $match: { deleted: false, payment_status: 'paid', invoice_date: { $gte: startOfQ, $lte: endOfQ } } },
  { $group: { _id: '$sales_person', revenue: { $sum: '$grandtotal_in_usd' } } },
  { $sort: { revenue: -1 } },
  { $limit: 1 },
  { $lookup: { from: 'users', localField: '_id', foreignField: '_id', as: 'user' } },
  { $unwind: '$user' }
])
```

---

**Q: "Show all pending tasks for deal ELS042"**
```
Collection 1: deals — find deal with deal_number ELS042 (sequence_number = 42)
Collection 2: createtasks — filter by dealsId = that deal's _id
Filter: deleted=false, status='Pending'
Key logic: deal_number is a virtual — query by sequence_number (strip 'ELS' prefix → parseInt → 42)
```
```javascript
// Step 1: Find deal
const deal = db.deals.findOne({ deleted: false, sequence_number: 42 });
// Step 2: Find tasks
db.createtasks.find({ deleted: false, dealsId: deal._id, status: 'Pending' })
```

---

**Q: "Which companies have been inactive for more than 60 days?"**
```
Collection: companies
Filter: deleted=false, inActiveSince is set and > 60 days ago
Key logic: inactive = inActiveSince field is not null
```
```javascript
const sixtyDaysAgo = new Date(Date.now() - 60 * 24 * 60 * 60 * 1000);
db.companies.find({
  deleted: false,
  inActiveSince: { $ne: null, $lte: sixtyDaysAgo }
})
```

---

**Q: "How many deals are in each stage right now?"**
```
Collection: deals
Filter: deleted=false, open deals (dealWonAt=null, dealLostAt=null)
Group by: stage field
Key logic: "right now" = currently open pipeline, not historical
```
```javascript
db.deals.aggregate([
  { $match: { deleted: false, dealWonAt: null, dealLostAt: null } },
  { $group: { _id: '$stage', count: { $sum: 1 }, value: { $sum: '$grand_total_in_usd' } } },
  { $sort: { count: -1 } }
])
```

---

**Q: "Show me all bills that are overdue and not paid"**
```
Collection: bills
Filter: dueDate < today, status NOT IN ['Paid','Rejected']
Key logic: bills have their own status lifecycle separate from invoices
```
```javascript
db.bills.find({
  dueDate: { $lt: new Date() },
  status: { $nin: ['Paid', 'Rejected'] }
}).sort({ dueDate: 1 })
```

---

**Q: "What is the outreach to deal conversion rate for APAC region?"**
```
Collection: outreaches
Filter: isDeleted=false, region = APAC region ObjectId
Logic: converted = status 'Converted to Deal', rate = converted/total * 100
Step 1: Lookup APAC region _id from regions collection
Step 2: Aggregate outreaches filtered by that region
```
```javascript
// Step 1
const apac = db.regions.findOne({ regionName: 'APAC' });
// Step 2
db.outreaches.aggregate([
  { $match: { isDeleted: false, region: apac._id } },
  { $group: {
    _id: null,
    total: { $sum: 1 },
    converted: { $sum: { $cond: [{ $eq: ['$status', 'Converted to Deal'] }, 1, 0] } }
  }},
  { $addFields: { conversionRate: { $multiply: [{ $divide: ['$converted', '$total'] }, 100] } } }
])
```

---

**Q: "Has [sales rep name] hit their target this month?"**
```
Collections: users (find rep) + targets (get target) + invoices (sum paid revenue)
Month: 0-indexed! March = 2, April = 3
Join logic: targets.userId = invoices.sales_person
```
```javascript
// Step 1: find user
const user = db.users.findOne({ name: /John Smith/i });
// Step 2: get target (April = month 3)
const target = db.targets.findOne({ userId: user._id, month: 3, year: 2024 });
// Step 3: sum revenue
const result = db.invoices.aggregate([
  { $match: {
    deleted: false,
    sales_person: user._id,
    payment_status: 'paid',
    invoice_date: { $gte: new Date('2024-04-01'), $lte: new Date('2024-04-30') }
  }},
  { $group: { _id: null, achieved: { $sum: '$grandtotal_in_usd' } } }
])
// Compare result[0].achieved vs target.targetInUSD
```

---

## COLLECTION NAME QUICK REFERENCE

> Use these exact names in MongoDB queries (`db.<name>.find(...)`)

| Model File | MongoDB Collection Name |
|---|---|
| Deal.js | `deals` |
| Invoice.js | `invoices` |
| Sales.js | `sales` |
| Company.js | `companies` |
| Contact.js | `contacts` |
| User.js | `users` |
| CreateTasks.js | `createtasks` |
| OutReach.js | `outreaches` |
| Bill.js | `bills` |
| Vendor.js | `vendors` |
| Product.js | `products` |
| Notifications.js | `notifications` |
| Email.js | `emails` |
| UserEmail.js | `mails` |
| Target.js | `targets` |
| Notes.js | `commonnotes` |
| Note.js | `notes` |
| DealsNote.js | `dealsnotes` |
| ContactNote.js | `contactsnotes` |
| CompanyNote.js | `companynotes` |
| SalesNote.js | `salesnotes` |
| RemoteJobNotes.js | `remotejobNotes` |
| Region.js | `regions` |
| Campaign.js | `campaigns` |
| Department.js | `departments` |
| ProjectType.js | `projecttypes` |
| Technology.js | `technologies` |
| TechnologyCategory.js | `technologycategories` |
| Source.js | `sources` |
| Tax.js | `taxes` |
| LeadStatus.js | `lead_status` |
| LifeCycleStage.js | `lifecycle_stage` |
| DealStageSetting.js | `dealstagesettings` |
| CountryRegion.js | `countryregions` |
| Meeting.js | `meetings` |
| RemoteJob.js | `remotejobs` |
| Payments.js | `payments` |
| PublicLead.js | `publicleads` |
| DeletedCompany.js | `deletedcompanies` |
| BadEmails.js | `bademails` |
| Activities.js | `activities` |
| ActivityEvent.js | `activityevents` |
| OutreachActivity.js | `outreachactivities` |
| Status.js | `statuses` |
| Category.js | `categorys` |

---

## LLM DECISION TREE — WHICH COLLECTION TO USE?

```
User asks about...
│
├── MONEY / REVENUE / PAYMENTS?
│     ├── Money RECEIVED from clients → invoices (payment_status='paid')
│     ├── Invoices raised but unpaid → invoices (payment_status in draft/confirmed)
│     ├── Money OWED to vendors → bills
│     └── Payment methods/gateways → payments
│
├── DEALS / PIPELINE / OPPORTUNITIES?
│     ├── Won deals → deals (dealWonAt not null)
│     ├── Lost deals → deals (dealLostAt not null)
│     ├── Open pipeline → deals (both null)
│     └── Deal stages/count → deals (group by stage)
│
├── CUSTOMERS / ACCOUNTS?
│     ├── Client companies → companies
│     ├── Individual people → contacts
│     └── Vendors/suppliers → vendors
│
├── OUTREACH / PROSPECTING?
│     └── Cold prospects (not yet in CRM) → outreaches
│
├── TASKS / REMINDERS?
│     └── createtasks (main task collection)
│
├── TARGETS / PERFORMANCE?
│     ├── Set targets → targets
│     └── Achieved revenue → invoices (join with targets)
│
├── EMAILS?
│     ├── Gmail synced → emails
│     └── IMAP synced → mails
│
├── NOTES?
│     ├── On deals → dealsnotes OR commonnotes (type='Deal')
│     ├── On companies → companynotes OR commonnotes (type='Company')
│     ├── On contacts → contactsnotes OR commonnotes (type='Contact')
│     ├── On sales orders → salesnotes OR commonnotes (type='Sales')
│     ├── On invoices → commonnotes (type='Invoice')
│     └── On outreach prospects → notes (singular)
│
├── MASTER / CONFIG DATA?
│     ├── Regions → regions
│     ├── Deal stages config → dealstagesettings
│     ├── Products catalog → products
│     ├── Technologies → technologies
│     ├── Lead statuses → lead_status
│     ├── Lifecycle stages → lifecycle_stage
│     └── Tax rates → taxes
│
└── ACTIVITY / AUDIT?
      ├── System activities → activities / activityevents
      ├── Deleted company trail → deletedcompanies
      └── Bad/invalid emails → bademails
```
