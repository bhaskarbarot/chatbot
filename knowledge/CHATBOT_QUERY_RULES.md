# CHATBOT QUERY RULES — Complete MongoDB Translation Guide
## All Unique Business Queries with Process Descriptions

---

## SCHEMA QUICK REFERENCE (Read Before Writing Any Query)

| Collection | Key Fields | Soft Delete | Amount Fields |
|---|---|---|---|
| `invoices` | invoice_number, payment_status, invoice_date, due_date, payment_date, company→Company, so_number→Sales, invoiceOwner→User | `deleted:false` | grand_total, grandtotal_in_usd, subtotal, subtotal_in_usd |
| `deals` | name, stage (String), dealWonAt, dealLostAt, owner→User, company→Company, contact→Contact, closeDate, sequence_number | `deleted:false` | grand_total, grand_total_in_usd, subtotal |
| `companies` | companyName, companyOwner→User, type, region→Region, lifecycleStage, leadStatus, leadWonAt, leadLostAt, lastBusinessDate, source→Source | `deleted:false` | annualRevenue (String) |
| `contacts` | firstName, lastName, email, phoneNumber, jobTitle, contactOwner→User, company→Company, source→Source | `deleted:false` | — |
| `sales` | sales_number (SO00001 format), salesOwner→User, company→Company, status (String), sales_date, sales_updated_date | `deleted:false` | grand_total, subtotal, tax_amount, discount_amount |
| `createtasks` | Task (title), status (Pending/Completed), priority (Low/Medium/High), category (Open/Close), createdBy→User, due_date, closedDate, contectId→Contact, companyId/company→Company, dealsId→Deal, invoiceId→Invoice, salesId→Sales | `deleted:false` | — |
| `targets` | userId→User, month (0-indexed: Jan=0, Dec=11), year, targetInUSD, teamName | none | targetInUSD |
| `outreaches` | name, email, designation, city, country, status (Unassigned/Not Contacted/Contacted/Followup/Converted to Deal), leadStatus (-/Nurturing/Lost/Intrested/Mislabeled/Converted to Deal/To Be Verified/Verified), priority (-/Low/Medium/High), campaign→Campaign, region→Region, assignedTo→User, sourceFile (=CSV/dataset name), ReminderDate, interestedDate | `isDeleted:false` | — |
| `meetings` | title, start, end, attendees[] (email strings), createdBy→User | none | — |
| `products` | name, product_type→ProjectType, technology[]→Technology, unit_cost, currency, isActive | none | unit_cost |

### CRITICAL RULES
- **Revenue queries → ALWAYS use `invoices` collection, never `sales`**
- **Paid revenue → filter `payment_status='paid'`, amount field = `grandtotal_in_usd`**
- **Open deals → `dealWonAt=null AND dealLostAt=null`**
- **Won deals → `dealWonAt` is NOT null**
- **Lost deals → `dealLostAt` is NOT null**
- **"Intrested" is stored as that spelling** — not "Interested"
- **targets.month is 0-indexed** — January=0, February=1 … December=11
- **Deal number format** = `ELS` + sequence_number padded to 3 digits (e.g., ELS001)
- **Achieved for targets** = sum of `grandtotal_in_usd` from `invoices` where `sales_person=userId` AND `payment_status='paid'` for that month/year
- **Remaining invoice amount** = `grandtotal_in_usd` minus sum of `payment_history[].payment_amount_in_usd`
- **"Customer since" date** = `companies.leadWonAt`
- **payment_status enum** = "draft" | "paid" | "confirmed" | "cancelled" | "partial_payment"

---

## SECTION 1 — CRM (General)

---

**business:** Get details for this company: [name]
**process:** Query `companies` where `deleted=false` and `companyName` matches [name] case-insensitively (regex). Lookup `users` on `companyOwner` for ownerName. Lookup `regions` on `region` for regionName. Lookup `sources` on `source` for sourceName. Lookup `technologies` on `webTechnologies` array for tech names. Return: companyName, email, phoneNumber, type, industry, country, city, stateRegion, postalCode, lifecycleStage, leadStatus, leadWonAt, leadLostAt, lastBusinessDate, annualRevenue, Currency, BillingAddress, websiteUrl, ownerName, regionName, sourceName, webTechnologies, createdAt.

---

**business:** Get details for this contact: [name]
**process:** Query `contacts` where `deleted=false` and (`firstName` OR `lastName` OR concatenated `firstName lastName`) matches [name] case-insensitively. Lookup `companies` on `company` for companyName. Lookup `users` on `contactOwner` for ownerName. Lookup `sources` on `source` for sourceName. Return: firstName, lastName, email, phoneNumber, jobTitle, lifecycleStage, leadStatus, companyName, ownerName, sourceName, birthday, lastActivity, createdAt.

---

**business:** Get details for this deal: [name or deal number]
**process:** Query `deals` where `deleted=false` and either `name` matches [name] (regex) OR `sequence_number` equals the numeric part extracted from [deal number] (e.g., ELS007 → 7). Lookup `companies` on `company` for companyName. Lookup `users` on `owner` for ownerName. Lookup `contacts` on `contact` for contact name and email. Return: name, stage, grand_total, grand_total_in_usd, closeDate, dealWonAt, dealLostAt, type, lineItems, ownerName, companyName, createdAt. Show deal number as `ELS` + sequence_number padded to 3 digits.

---

**business:** Get details for this invoice: [invoice number]
**process:** Query `invoices` where `deleted=false` and `invoice_number` = [number]. Lookup `companies` on `company` for companyName. Lookup `users` on `invoiceOwner` for ownerName. Lookup `sales` on `so_number` for sales_number. Return all fields: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, subtotal, tax_amount, discount_amount, invoice_date, due_date, payment_date, items array, payment_history, companyName, ownerName, linked sales_number.

---

**business:** Get details for this sales order: [SO number]
**process:** Query `sales` where `deleted=false` and `sales_number` = [number] (exact match, e.g., SO00001). Lookup `companies` on `company` for companyName. Lookup `users` on `salesOwner` for ownerName. Lookup `invoices` where `so_number=sales._id` for linked invoice details. Return: sales_number, status, grand_total, subtotal, tax, tax_name, tax_amount, discount_amount, items[], sales_date, createdAt, companyName, ownerName, linked invoice.

---

**business:** Get tasks linked to this company: [company]
**process:** Find company `_id` by matching `companyName` in `companies`. Query `createtasks` where `deleted=false` and (`company`=companyId OR `companyId`=companyId). Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, closedDate, category, associated_module, note, ownerName. Sort by due_date ascending.

---

**business:** Get tasks linked to this deal: [deal name or number]
**process:** Find deal `_id` from `deals` by name or sequence_number. Query `createtasks` where `deleted=false` and `dealsId`=dealId. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, category, note, ownerName. Sort by due_date ascending.

---

**business:** Get contacts linked to this company: [company]
**process:** Find company `_id` from `companies` by name. Query `contacts` where `deleted=false` and `company`=companyId. Lookup `users` on `contactOwner` for ownerName. Return: firstName, lastName, email, phoneNumber, jobTitle, leadStatus, lifecycleStage, ownerName, createdAt. Sort by createdAt desc.

---

**business:** Get deals linked to this company: [company]
**process:** Find company `_id` from `companies`. Query `deals` where `deleted=false` and `company`=companyId. Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, dealWonAt, dealLostAt, ownerName, createdAt. Sort by createdAt desc.

---

**business:** Get invoices linked to this company: [company]
**process:** Find company `_id` from `companies`. Query `invoices` where `deleted=false` and `company`=companyId. Lookup `users` on `invoiceOwner` for ownerName. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, invoice_date, due_date, payment_date, ownerName. Sort by invoice_date desc.

---

**business:** Get sales orders linked to this company: [company]
**process:** Find company `_id` from `companies`. Query `sales` where `deleted=false` and `company`=companyId. Lookup `users` on `salesOwner` for ownerName. Return: sales_number, status, grand_total, sales_date, ownerName. Sort by sales_date desc.

---

**business:** Get meetings for today
**process:** Query `meetings` where `start` >= today 00:00:00 AND `start` <= today 23:59:59 (use today's date in UTC). Lookup `users` on `createdBy` for creatorName. Return: title, description, start, end, location, attendees[], creatorName. Sort by start ascending.

---

**business:** Get upcoming meetings for this user: [user]
**process:** Find user from `users` by name. Query `meetings` where `start` > now AND (`attendees` array contains user's email OR `createdBy`=userId). Return: title, start, end, location, attendees[], description. Sort by start ascending.

---

**business:** Get all open tasks for this user: [user]
**process:** Find user `_id` from `users` by name. Query `createtasks` where `deleted=false`, `status='Pending'`, and `createdBy`=userId. Add computed field `isOverdue = (due_date < today)`. Return: Task, priority, due_date, category, associated_module, note, isOverdue. Sort by due_date ascending. Flag overdue tasks clearly.

---

**business:** Get tasks due on this date: [date]
**process:** Query `createtasks` where `deleted=false` and `due_date` >= [date] 00:00:00 AND `due_date` <= [date] 23:59:59. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName, associated_module. Sort by priority (High first).

---

**business:** Get deals in this stage: [stage name]
**process:** Query `deals` where `deleted=false`, `dealWonAt`=null, `dealLostAt`=null, and `stage` matches [stage] case-insensitively (regex). Lookup `companies` on `company` for companyName. Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, companyName, ownerName. Sort by closeDate ascending.

---

**business:** Get deals closing on this date: [date]
**process:** Query `deals` where `deleted=false` and `closeDate` >= [date] 00:00:00 AND `closeDate` <= [date] 23:59:59. Lookup `companies` for companyName. Lookup `users` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, dealWonAt, dealLostAt, companyName, ownerName.

---

**business:** Get invoices due on this date: [date]
**process:** Query `invoices` where `deleted=false` and `due_date` >= [date] 00:00:00 AND `due_date` <= [date] 23:59:59. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, due_date, companyName. Sort by payment_status.

---

**business:** Get invoices with status: [status]
**process:** Query `invoices` where `deleted=false` and `payment_status`=[status]. Valid values exactly: "draft", "paid", "confirmed", "cancelled", "partial_payment". Lookup `companies` on `company` for companyName. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, invoice_date, due_date, companyName. Sort by invoice_date desc.

---

**business:** Get sales orders with status: [status]
**process:** Query `sales` where `deleted=false` and `status` matches [status] (case-insensitive, status is a free-text String field). Lookup `companies` for companyName. Lookup `users` on `salesOwner` for ownerName. Return: sales_number, status, grand_total, sales_date, companyName, ownerName. Sort by sales_date desc.

---

**business:** Get companies by region: [region name]
**process:** Find region `_id` from `regions` where `regionName` matches [region] case-insensitively. Query `companies` where `deleted=false` and `region`=regionId. Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, phoneNumber, country, lifecycleStage, leadStatus, ownerName, createdAt. Sort by companyName.

---

**business:** Get companies by source: [source name]
**process:** Find source `_id` from `sources` where `sourceName` matches [source]. Query `companies` where `deleted=false` and `source`=sourceId. Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, country, lifecycleStage, leadStatus, ownerName, createdAt.

---

**business:** Get contacts by job title: [title]
**process:** Query `contacts` where `deleted=false` and `jobTitle` matches [title] case-insensitively (regex). Lookup `companies` on `company` for companyName. Lookup `users` on `contactOwner` for ownerName. Return: firstName, lastName, email, phoneNumber, jobTitle, companyName, ownerName.

---

**business:** Get contacts created on this date: [date]
**process:** Query `contacts` where `deleted=false` and `createdAt` >= [date] 00:00:00 AND `createdAt` <= [date] 23:59:59. Lookup `companies` for companyName. Return: firstName, lastName, email, jobTitle, companyName, createdAt.

---

**business:** Get deals owned by this user: [user]
**process:** Find user `_id` from `users` by name. Query `deals` where `deleted=false` and `owner`=userId. Lookup `companies` for companyName. Return: name, stage, grand_total_in_usd, closeDate, dealWonAt, dealLostAt, companyName. Sort by createdAt desc.

---

**business:** Get companies owned by this user: [user]
**process:** Find user `_id` from `users` by name. Query `companies` where `deleted=false` and `companyOwner`=userId. Return: companyName, email, country, lifecycleStage, leadStatus, lastBusinessDate, createdAt. Sort by createdAt desc.

---

**business:** Get pipeline summary for this period: [date range]
**process:** Query `deals` where `deleted=false`, open (dealWonAt=null AND dealLostAt=null), and `closeDate` >= [start] AND `closeDate` <= [end]. Group by `stage`: count deals, sum `grand_total_in_usd`. Also group by `owner` for rep breakdown. Lookup `users` for rep names. Return: stage-wise table (stage, dealCount, totalValueUSD, avgDealSize) + rep-wise table + total pipeline value.

---

**business:** Get leads by source for this period
**process:** Query `companies` where `deleted=false` and `createdAt` falls within the given period. Group by `source`: count per source. Lookup `sources` for sourceName. Also query `outreaches` where `isDeleted=false` and `createdAt` in period, group by `campaign` for outreach source breakdown. Return: sourceName, leadCount per source, sorted by count desc.

---

**business:** Get deals by stage for this period
**process:** Query `deals` where `deleted=false` and `createdAt` falls within the given period. Group by `stage`: count deals, sum `grand_total_in_usd`, avg deal size. Sort by totalValue desc. Return: stage, dealCount, totalValueUSD, avgDealSize.

---

**business:** Get revenue summary for this period
**process:** Query `invoices` where `deleted=false`, `payment_status='paid'`, `invoice_date` >= periodStart AND `invoice_date` <= periodEnd. Use $facet: (1) summary → $group by null: totalRevenue=$sum grandtotal_in_usd, invoiceCount=$sum 1, avgInvoice=$avg grandtotal_in_usd; (2) byMonth → group by year+month of invoice_date; (3) topCompanies → group by company (top 5 by revenue). Lookup companies for names. Return: totalRevenue, invoiceCount, avgInvoice, monthly breakdown, top companies.

---

**business:** Get regional performance for this period
**process:** Start from `companies` where `deleted=false`. Lookup `regions` for regionName. Join to `invoices` with sub-pipeline: company=_id, payment_status='paid', invoice_date in period. Group by region: sum grandtotal_in_usd as revenue, count companies. Return: regionName, companyCount, totalRevenueUSD. Sort by totalRevenueUSD desc.

---

**business:** Get top products for this period
**process:** Query `invoices` where `deleted=false`, `payment_status='paid'`, `invoice_date` in period. Unwind `items` array. Group by `items.product_name`: totalRevenue=$sum items.total_price_in_usd, totalQty=$sum items.quantity. Sort by totalRevenue desc, limit 10. Return: product_name, totalRevenueUSD, totalQuantity.

---

**business:** Get lost leads and reasons for this period
**process:** Query `companies` where `deleted=false` and `leadLostAt` >= periodStart AND `leadLostAt` <= periodEnd. Group by `leadLostReason`: count per reason (for summary). Also return individual records: companyName, leadLostAt, leadLostReason, leadLostReasonInDetail. Lookup `users` on `companyOwner` for ownerName. Sort by leadLostAt desc.

---

**business:** Get target details for this user: [user]
**process:** Find user `_id` from `users` by name. Query `targets` where `userId`=userId. Sort by year desc, month desc. For each target record, calculate achieved = sum `grandtotal_in_usd` from `invoices` where `sales_person`=userId AND `payment_status='paid'` AND the invoice_date year and month match the target's year and 0-indexed month. Convert month 0-index to name (0=January … 11=December). Return: monthName, year, targetInUSD, achievedUSD, score=(achieved/target)*100, gap=target-achieved.

---

**business:** Get target vs achieved for all users
**process:** Query all `targets`. Lookup `users` on `userId` for userName. For each target, calculate achieved from `invoices` where `sales_person`=userId, `payment_status='paid'`, month and year match. Compute: score=(achieved/target)*100, gap=target-achieved. Return table: userName, monthName (convert from 0-index), year, targetInUSD, achievedUSD, score, gap. Sort by year desc, month desc.

---

**business:** Get outreach data for this dataset: [dataset name]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[dataset name] (exact or regex). Group by `status` for summary counts. Lookup `users` on `assignedTo` for assigneeName. Lookup `regions` on `region` for regionName. Return: summary (total, by-status counts) + individual list: name, email, designation, country, status, leadStatus, priority, assigneeName, regionName.

---

**business:** Get interested leads for the last 7 days
**process:** Query `outreaches` where `isDeleted=false`, `leadStatus='Intrested'` (exact spelling), and `interestedDate` >= (today minus 7 days). Lookup `users` on `assignedTo` for assigneeName. Lookup `regions` on `region` for regionName. Return: name, email, designation, country, priority, interestedDate, assigneeName, regionName. Sort by interestedDate desc.

---

**business:** Get unique touches and total touches for this dataset
**process:** Query `outreaches` where `sourceFile`=[dataset]. Get distinct `region` _ids from those records. Query `outreachactivities` where `regionId` in those region _ids. Total touches = $sum of `count` across all matching records. Unique touches = count of distinct `ActivityId` values (unique activity types touched). Return: datasetName, totalTouches, uniqueTouches.

---

**business:** Get outreach contacts by region: [region]
**process:** Find region `_id` from `regions` by regionName. Query `outreaches` where `isDeleted=false` and `region`=regionId. Group by `status` for summary. Return individual contacts: name, email, designation, country, status, leadStatus, priority. Sort by status.

---

**business:** Get outreach history for this contact
**process:** Query `outreaches` where `isDeleted=false` and `name` matches [contact name] (regex). Return all fields: name, status, leadStatus, ReminderDate, lastAddedNote, convertedDate, conversionComments, interestedDate, priority, sourceFile. Also query `notes` where `outreachId` matches the found _id for note history. Sort by updatedAt desc.

---

**business:** Get product details for this product: [name]
**process:** Query `products` where `name` matches [name] case-insensitively (regex). Lookup `projecttypes` on `product_type` for projectTypeName. Lookup `technologies` on `technology` array for tech names. Return: name, description_short, description_long, projectTypeName, sku, billing_frequency, term, url, unit_cost, currency, technology names, tax_rate, isActive.

---

**business:** Get products under this technology: [technology]
**process:** Find technology `_id` from `technologies` where `name` matches [technology]. Query `products` where `technology` array contains that techId. Lookup `projecttypes` on `product_type` for typeName. Return: name, description_short, unit_cost, currency, billing_frequency, projectTypeName, isActive.

---

**business:** Get tax details for this tax name: [name]
**process:** Query `taxes` where `name` matches [name] case-insensitively (regex). Return: name, amount (tax percentage).

---

**business:** Get all payment methods
**process:** Query `payments` collection, find all documents. Return: payment_name, description, payment_fee, payment_link.

---

**business:** Get all industries
**process:** Query `companies` where `deleted=false`. Use distinct on `industry` field to get all unique industry values. Filter out null/empty strings. Return sorted alphabetical list.

---

**business:** Get all technologies
**process:** Query `technologies` collection, find all. Lookup `technologycategories` on `category` for categoryName. Return: name, categoryName. Sort by categoryName, then name.

---

**business:** Get all sources
**process:** Query `sources` collection, find all. Return: sourceName, _id. Sort alphabetically by sourceName.

---

## SECTION 2 — SALES ORDERS

---

**business:** Get sales order list created on this date: [date]
**process:** Query `sales` where `deleted=false` and `createdAt` >= [date] 00:00:00 AND `createdAt` <= [date] 23:59:59. Lookup `companies` for companyName. Lookup `users` on `salesOwner` for ownerName. Return: sales_number, status, grand_total, companyName, ownerName, createdAt.

---

**business:** Get sales orders updated on this date: [date]
**process:** Query `sales` where `deleted=false` and `sales_updated_date` >= [date] 00:00:00 AND `sales_updated_date` <= [date] 23:59:59. Lookup `companies` for companyName. Return: sales_number, status, grand_total, companyName, sales_updated_date.

---

**business:** Get sales orders owned by this user: [user]
**process:** Find user `_id` from `users` by name. Query `sales` where `deleted=false` and `salesOwner`=userId. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by sales_date desc.

---

**business:** Get sales orders created by this user: [user]
**process:** Find user `_id` from `users` by name. Query `sales` where `deleted=false` and `createdByCompanyOwner`=userId. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by sales_date desc.

---

**business:** Get sales orders with grand total above: [amount]
**process:** Query `sales` where `deleted=false` and `grand_total` > [amount]. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by grand_total desc.

---

**business:** Get sales orders with grand total below: [amount]
**process:** Query `sales` where `deleted=false` and `grand_total` < [amount]. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by grand_total asc.

---

**business:** Get sales orders between this amount range: [min - max]
**process:** Query `sales` where `deleted=false` and `grand_total` >= [min] AND `grand_total` <= [max]. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by grand_total desc.

---

**business:** Get sales orders between this date range: [start - end]
**process:** Query `sales` where `deleted=false` and `sales_date` >= [start] 00:00:00 AND `sales_date` <= [end] 23:59:59. Lookup `companies` for companyName. Return: sales_number, status, grand_total, sales_date, companyName. Sort by sales_date desc.

---

**business:** Get creation and update history for this sales order: [SO number]
**process:** Query `sales` where `deleted=false` and `sales_number`=[number]. Lookup `users` on `salesOwner` for ownerName. Lookup `users` on `createdByCompanyOwner` for creatorName. Return: sales_number, status, createdAt, sales_updated_date, ownerName, creatorName.

---

**business:** Get all line items for this sales order: [SO number]
**process:** Query `sales` where `deleted=false` and `sales_number`=[number]. Return `items` array with full details. Lookup `products` on `items.product` for product details. Lookup `projecttypes` on `items.project_type` for typeName. Return each item: product_name, quantity, unit_price, discount, discount_type, project_type name, total_price.

---

**business:** Get total quantity for this sales order: [SO number]
**process:** Query `sales` where `sales_number`=[number]. Unwind `items`. Group by null: totalQty=$sum items.quantity, itemCount=$sum 1. Return: sales_number, totalQuantity, itemCount.

---

**business:** Get subtotal, tax, and grand total for this sales order: [SO number]
**process:** Query `sales` where `deleted=false` and `sales_number`=[number]. Return: sales_number, subtotal, tax, tax_name, tax_amount, discount_amount, grand_total.

---

**business:** Get discount details for this sales order: [SO number]
**process:** Query `sales` where `deleted=false` and `sales_number`=[number]. Return: sales_number, discount_amount (overall discount), and from `items` array: each item's product_name, discount value, discount_type.

---

**business:** Get project type breakdown for this sales order: [SO number]
**process:** Query `sales` where `sales_number`=[number]. Unwind `items`. Lookup `projecttypes` on `items.project_type` for projectTypeName. Group by project_type: sum total_price as subtotalPerType, sum quantity. Return: projectTypeName, subtotal, totalQuantity per project type.

---

**business:** Get pricing details for a specific product under this order: [Product Name] in [SO Number]
**process:** Query `sales` where `sales_number`=[SO number]. Unwind `items`. Filter where `items.product_name` matches [Product Name] case-insensitively. Return: product_name, quantity, unit_price, discount, discount_type, total_price.

---

**business:** Get invoice linked to this sales order: [SO number]
**process:** Find sales `_id` from `sales` where `sales_number`=[number]. Query `invoices` where `deleted=false` and `so_number`=salesId. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, invoice_date, due_date, payment_date, payment_mode.

---

**business:** Get invoice payment mode for this order: [SO number]
**process:** Find sales `_id`. Query `invoices` where `so_number`=salesId and `deleted=false`. Return: invoice_number, payment_mode, payment_status.

---

**business:** Get partial vs full payment details for this invoice: [Invoice Number]
**process:** Query `invoices` where `deleted=false` and `invoice_number`=[number]. Compute: paidSoFar=$sum of `payment_history[].payment_amount_in_usd`. remainingAmount=grandtotal_in_usd minus paidSoFar. Return: invoice_number, grand_total, grandtotal_in_usd, currency, payment_status, paidSoFar, remainingAmount, and full `payment_history` array (each entry: payment_date, payment_amount, payment_amount_in_usd).

---

**business:** Get account contact details for this order: [SO number]
**process:** Find sales record → get `company` _id. Query `contacts` where `deleted=false` and `company`=companyId. Return: firstName, lastName, email, phoneNumber, jobTitle, isPrimary. Flag the primary contact (isPrimary=true) first.

---

**business:** Get deals connected to this sales order: [SO number]
**process:** Find sales record → get `company` _id. Query `deals` where `deleted=false` and `company`=companyId. Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, dealWonAt, dealLostAt, closeDate, ownerName. (Note: deals and sales link via shared company, no direct foreign key.)

---

**business:** Get latest order for this account: [Account Name]
**process:** Find company `_id` from `companies`. Query `sales` where `deleted=false` and `company`=companyId. Sort by `sales_date` desc, limit 1. Return: sales_number, status, grand_total, sales_date, items count.

---

**business:** Get order summary for this account: [Account Name]
**process:** Find company `_id`. Query `sales` where `company`=companyId. Aggregate: totalOrders=$count, totalValue=$sum grand_total, avgOrderValue=$avg grand_total, firstOrderDate=$min sales_date, lastOrderDate=$max sales_date. Return summary.

---

**business:** Get outstanding amount for this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status` NOT IN ['paid','cancelled']. For each invoice compute: paidSoFar=$sum payment_history[].payment_amount_in_usd. amountRemaining=grandtotal_in_usd minus paidSoFar. Sum all amountRemaining for totalOutstanding. Return: totalOutstanding, and per-invoice: invoice_number, payment_status, grandtotal_in_usd, paidSoFar, amountRemaining.

---

**business:** Get total billed vs paid for this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false` and `company`=companyId. Aggregate: totalBilledUSD=$sum grandtotal_in_usd (excluding cancelled), totalPaidUSD=$sum grandtotal_in_usd where payment_status='paid', totalPartialPaidUSD=$sum grandtotal_in_usd where payment_status='partial_payment'. outstanding=totalBilledUSD minus totalPaidUSD. Return: totalBilledUSD, totalPaidUSD, totalPartialPaidUSD, outstanding.

---

**business:** Get pending invoices for this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status` IN ['draft','confirmed','partial_payment']. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, due_date, invoice_date. Sort by due_date ascending.

---

**business:** Get purchased products for this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='paid'`. Unwind `items`. Group by `items.product_name`: totalRevenueUSD=$sum items.total_price_in_usd, totalQty=$sum items.quantity. Return: product_name, totalRevenueUSD, totalQuantity. Sort by totalRevenueUSD desc.

---

**business:** Get service/project list for this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='paid'`. Unwind `items`. Lookup `projecttypes` on `items.project_type`. Return: product_name, projectTypeName, quantity, total_price per line item.

---

**business:** Get total quantity purchased by this account: [Account Name]
**process:** Find company `_id`. Query `invoices` where `company`=companyId and `payment_status='paid'`. Unwind `items`. Group by null: totalQuantity=$sum items.quantity. Return: companyName, totalQuantity (all time).

---

**business:** Get active stage deals for this account: [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false`, `company`=companyId, `dealWonAt`=null, `dealLostAt`=null (open deals only). Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, ownerName. Sort by closeDate ascending.

---

**business:** Get deal value summary for this account: [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Aggregate: totalDeals=$count, totalValueUSD=$sum grand_total_in_usd, wonValueUSD=$sum grand_total_in_usd where dealWonAt!=null, lostValueUSD=$sum grand_total_in_usd where dealLostAt!=null, activeValueUSD=$sum grand_total_in_usd where both null. Return full summary.

---

**business:** Get notes linked to this account: [Account Name]
**process:** Find company `_id`. Query `companynotes` where `company`=companyId. Lookup `users` on `createdBy` for authorName. Return: title, notes, isPinned, authorName, createdAt. Sort by isPinned desc, createdAt desc.

---

**business:** Get interaction history for this account: [Account Name]
**process:** Find company `_id`. Query `commonnotes` where `companyId`=companyId. Lookup `users` on `createdBy` for authorName. Return: note, type, isPinned, isLog, attachment, createdAt, authorName. Sort by createdAt desc.

---

**business:** Get owner logged actions for this account: [Account Name]
**process:** Find company `_id`. Query `commonnotes` where `companyId`=companyId AND `isLog`=true. Lookup `users` on `createdBy`. Return: note, type, authorName, createdAt — these represent system-logged owner actions.

---

**business:** Get all sales orders linked with invoice: [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Get `so_number` (ObjectId ref to Sales). Query `sales` where `_id`=so_number. Return: sales_number, status, grand_total, sales_date. (Each invoice links to exactly one SO via the so_number field.)

---

## SECTION 3 — INVOICES

---

**business:** Get remaining amount for [Invoice Number]
**process:** Query `invoices` where `deleted=false` and `invoice_number`=[number]. Compute: paidSoFar=$sum of all `payment_history[].payment_amount_in_usd`. remainingInUSD=grandtotal_in_usd minus paidSoFar. Return: invoice_number, grand_total, grandtotal_in_usd, currency, payment_status, paidSoFar, remainingInUSD.

---

**business:** Get payment received date for [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Return: invoice_number, payment_date (date full payment was received), payment_history array (each with payment_date for partial payments), payment_status.

---

**business:** Get payment history for [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Project `payment_history` array. Lookup `users` on `payment_history[].payment_by` for payerName. Return each payment entry: payment_date, payment_amount, payment_amount_in_usd, payerName, createdAt.

---

**business:** Get invoices owned by [Account Owner]
**process:** Find user `_id` from `users` by name. Query `invoices` where `deleted=false` and `sales_person`=userId. Lookup `companies` on `company` for companyName. Return: invoice_number, payment_status, grand_total, grandtotal_in_usd, invoice_date, companyName. Sort by invoice_date desc.

---

**business:** Get all invoices with country [Country Name]
**process:** Find company `_id`s from `companies` where `deleted=false` and `country`=[country] (case-insensitive). Query `invoices` where `deleted=false` and `company` IN those _ids. Lookup `companies` for companyName and country. Return: invoice_number, payment_status, grand_total, invoice_date, companyName, country.

---

**business:** Get all invoices with technology [Technology]
**process:** Find technology `_id` from `technologies`. Find company `_id`s from `companies` where `webTechnologies` contains that techId. Query `invoices` where `company` IN those _ids and `deleted=false`. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, invoice_date, companyName.

---

**business:** Get all invoices with project type [Project Type Name]
**process:** Find projecttype `_id` from `projecttypes` where `name`=[type]. Query `invoices` where `deleted=false` and `items.project_type`=projectTypeId (match inside items array using $elemMatch). Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, invoice_date, companyName.

---

**business:** Get list of paid invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='paid'`. Return: invoice_number, grand_total, grandtotal_in_usd, currency, invoice_date, payment_date. Sort by invoice_date desc.

---

**business:** Get list of partially paid invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='partial_payment'`. Compute for each: paidSoFar=$sum payment_history[].payment_amount_in_usd, remainingAmount=grandtotal_in_usd minus paidSoFar. Return: invoice_number, grandtotal_in_usd, paidSoFar, remainingAmount, due_date. Sort by due_date asc.

---

**business:** Get list of draft invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='draft'`. Return: invoice_number, grand_total, invoice_date, due_date. Sort by invoice_date desc.

---

**business:** Get list of confirmed invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='confirmed'`. Return: invoice_number, grand_total, invoice_date, due_date. Sort by due_date asc.

---

**business:** Get list of cancelled invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status='cancelled'`. Return: invoice_number, grand_total, invoice_date, cancellation_reason.

---

**business:** Get overdue invoices for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status` IN ['draft','confirmed','partial_payment'], and `due_date` < today. Add computed `daysOverdue = today minus due_date (in days)`. Return: invoice_number, payment_status, grand_total, due_date, daysOverdue. Sort by daysOverdue desc.

---

**business:** Get invoices created on [Invoice Date]
**process:** Query `invoices` where `deleted=false` and `invoice_date` >= [date] 00:00:00 AND `invoice_date` <= [date] 23:59:59. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, companyName, invoice_date.

---

**business:** Get invoices where payment received on [Payment Received Date]
**process:** Query `invoices` where `deleted=false` and `payment_date` >= [date] 00:00:00 AND `payment_date` <= [date] 23:59:59. Lookup `companies` for companyName. Return: invoice_number, grand_total, grandtotal_in_usd, payment_date, companyName.

---

**business:** Get invoices due date before [Date]
**process:** Query `invoices` where `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], and `due_date` < [date]. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, due_date, companyName. Sort by due_date asc.

---

**business:** Get invoices due date after [Date]
**process:** Query `invoices` where `deleted=false` and `due_date` > [date]. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, due_date, companyName. Sort by due_date asc.

---

**business:** Get invoices date before [Date]
**process:** Query `invoices` where `deleted=false` and `invoice_date` < [date]. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, invoice_date, companyName. Sort by invoice_date desc.

---

**business:** Get invoices date after [Date]
**process:** Query `invoices` where `deleted=false` and `invoice_date` > [date]. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, invoice_date, companyName. Sort by invoice_date desc.

---

**business:** Get invoices with payment received date before [Date]
**process:** Query `invoices` where `deleted=false`, `payment_status='paid'`, and `payment_date` < [date]. Lookup `companies` for companyName. Return: invoice_number, grand_total, payment_date, companyName. Sort by payment_date desc.

---

**business:** Get upcoming invoice due dates for [Account Name]
**process:** Find company `_id`. Query `invoices` where `company`=companyId, `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], `due_date` >= today. Return: invoice_number, payment_status, grand_total, due_date. Sort by due_date asc.

---

**business:** Get invoices due this week
**process:** Calculate this week's Monday 00:00:00 to Sunday 23:59:59. Query `invoices` where `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], `due_date` >= startOfWeek AND `due_date` <= endOfWeek. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, due_date, companyName. Sort by due_date asc.

---

**business:** Get invoices due next month
**process:** Calculate next month's first day 00:00:00 to last day 23:59:59. Query `invoices` where `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], `due_date` >= startOfNextMonth AND `due_date` <= endOfNextMonth. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, due_date, companyName. Sort by due_date asc.

---

**business:** Get invoices pending payment this week
**process:** Query `invoices` where `deleted=false`, `payment_status` IN ['draft','confirmed','partial_payment'], `due_date` <= endOfThisWeek. Add `isOverdue = (due_date < today)`. Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, due_date, isOverdue, companyName. Sort by due_date asc.

---

**business:** Get invoices with remaining amount above [Amount]
**process:** Query `invoices` where `deleted=false` and `payment_status` NOT IN ['paid','cancelled']. For each, compute: paidSoFar=$sum payment_history[].payment_amount_in_usd, remainingInUSD=grandtotal_in_usd minus paidSoFar. Match where remainingInUSD > [amount]. Lookup `companies` for companyName. Return: invoice_number, grandtotal_in_usd, paidSoFar, remainingInUSD, companyName. Sort by remainingInUSD desc.

---

**business:** Get invoices with remaining amount below [Amount]
**process:** Same as above but match where remainingInUSD < [amount] AND remainingInUSD > 0 (exclude fully paid). Sort by remainingInUSD asc.

---

**business:** Get invoices with subtotal above [Amount]
**process:** Query `invoices` where `deleted=false` and `subtotal_in_usd` > [amount]. Lookup `companies` for companyName. Return: invoice_number, subtotal, subtotal_in_usd, currency, invoice_date, companyName. Sort by subtotal_in_usd desc.

---

**business:** Get invoices with subtotal below [Amount]
**process:** Query `invoices` where `deleted=false` and `subtotal_in_usd` < [amount]. Lookup `companies` for companyName. Return: invoice_number, subtotal, subtotal_in_usd, currency, invoice_date, companyName. Sort by subtotal_in_usd asc.

---

**business:** Get total invoiced amount for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId. Aggregate: totalInvoicedUSD=$sum grandtotal_in_usd (all non-cancelled), totalPaidUSD=$sum grandtotal_in_usd where payment_status='paid', invoiceCount=$sum 1. outstanding=totalInvoicedUSD minus totalPaidUSD. Return: totalInvoicedUSD, totalPaidUSD, outstanding, invoiceCount.

---

**business:** Get total remaining amount for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId, `payment_status` NOT IN ['paid','cancelled']. For each, compute remaining. Sum all remainingInUSD. Return: totalRemainingUSD, invoiceCount pending.

---

**business:** Get all product names linked with invoice number [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Project `items` array. Return list of distinct `items[].product_name` values from the items array.

---

**business:** Get grand total of line items linked with invoice number [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Unwind `items`. Group by null: totalLineItems=$sum items.total_price, totalLineItemsUSD=$sum items.total_price_in_usd. Return: invoice grand_total (invoice level), totalLineItems sum, totalLineItemsUSD sum.

---

**business:** Get unit discount of line items linked with invoice number [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Return `items` array projected to: product_name, discount (unit discount per item). Return list.

---

**business:** Get list of unit prices of line items linked with [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Return `items` array projected to: product_name, unit_price per item.

---

**business:** Get list of total prices of line items linked with [Invoice Number]
**process:** Query `invoices` where `invoice_number`=[number]. Return `items` array projected to: product_name, quantity, total_price, total_price_in_usd per item.

---

**business:** Get all invoices with line item containing [Product Name]
**process:** Query `invoices` where `deleted=false` and `items.product_name` matches [Product Name] case-insensitively (use $elemMatch with $regex on items array). Lookup `companies` for companyName. Return: invoice_number, payment_status, grand_total, invoice_date, companyName.

---

## SECTION 4 — ACCOUNTS (COMPANIES)

---

**business:** Get payment summary for [Account Name]
**process:** Find company `_id`. Query `invoices` where `deleted=false`, `company`=companyId. Aggregate: totalBilledUSD=$sum grandtotal_in_usd (excluding cancelled), totalPaidUSD (payment_status='paid'), totalPartialUSD (partial_payment), invoiceCount=$count. outstanding=totalBilledUSD minus totalPaidUSD. Return: totalBilledUSD, totalPaidUSD, totalPartialUSD, outstanding, invoiceCount.

---

**business:** Get customer since date for [Account Name]
**process:** Query `companies` where `deleted=false` and `companyName` matches [name]. Return: companyName, `leadWonAt` — this is the date the lead was won (= customer since date).

---

**business:** Get last activity for [Account Name]
**process:** Query `companies` where `companyName` matches [name]. Return: companyName, `lastActivity.type`, `lastActivity.createdAt`.

---

**business:** Get last business date for [Account Name]
**process:** Query `companies` where `companyName` matches [name]. Return: companyName, `lastBusinessDate`.

---

**business:** Get address details for [Account Name]
**process:** Query `companies` where `companyName` matches [name]. Return: companyName, `BillingAddress` (JSON — parse it), city, stateRegion, country, postalCode.

---

**business:** Get meeting history for [Account Name]
**process:** Find contacts for this company from `contacts` where `company`=companyId. Get their emails. Query `meetings` where `attendees` array contains any of those emails. Also include meetings where `createdBy` is the company owner. Return: title, start, end, location, attendees. Sort by start desc.

---

**business:** Get technologies used by [Account Name]
**process:** Find company record from `companies`. Get `webTechnologies` array (ObjectId refs). Lookup `technologies` on each _id. Return: companyName, list of technology names.

---

**business:** Get all details of accounts created after/before [Date]
**process:** Query `companies` where `deleted=false` and `createdAt` > [date] (for "after") or `createdAt` < [date] (for "before"). Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, country, lifecycleStage, leadStatus, ownerName, createdAt. Sort by createdAt desc.

---

**business:** Get all details of accounts with a lead won date after/before [Date]
**process:** Query `companies` where `deleted=false` and `leadWonAt` > [date] (or < for "before"). Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, country, leadWonAt, ownerName. Sort by leadWonAt desc.

---

**business:** Get all details of accounts with a lead lost date after/before [Date]
**process:** Query `companies` where `deleted=false` and `leadLostAt` > [date] (or < for "before"). Return: companyName, email, country, leadLostAt, leadLostReason, leadLostReasonInDetail. Sort by leadLostAt desc.

---

**business:** Get all details of accounts with last activity after [Date]
**process:** Query `companies` where `deleted=false` and `lastActivity.createdAt` > [date]. Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, country, lastActivity, ownerName.

---

**business:** Get all details of accounts with customer since date before/after [Date]
**process:** "Customer since" = `leadWonAt` field. Query `companies` where `deleted=false` and `leadWonAt` < [date] (for "before") or > [date] (for "after"). Return: companyName, email, country, leadWonAt. Sort by leadWonAt.

---

**business:** Get all details of accounts with country [Country Name]
**process:** Query `companies` where `deleted=false` and `country` matches [country] case-insensitively. Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, phoneNumber, city, lifecycleStage, leadStatus, ownerName, createdAt.

---

**business:** Get all details of accounts with annual revenue greater than [Amount]
**process:** Query `companies` where `deleted=false`. Note: `annualRevenue` is stored as String — use $toDouble conversion or filter as numeric. Match where converted annualRevenue > [amount]. Return: companyName, annualRevenue, country, industry. Sort by annualRevenue desc. Note for LLM: annualRevenue may need type coercion since it is stored as String in the schema.

---

**business:** Get all details of accounts with email address [Email]
**process:** Query `companies` where `deleted=false` and `email`=[email] (case-insensitive match). Return: companyName, email, phoneNumber, country, lifecycleStage, leadStatus, companyOwner (lookup users for name).

---

**business:** Get accounts with lead status [status]
**process:** Query `companies` where `deleted=false` and `leadStatus` matches [status] case-insensitively. Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, country, leadStatus, lifecycleStage, ownerName. Sort by createdAt desc.

---

**business:** Get all details of account with phone number [Phone Number]
**process:** Query `companies` where `deleted=false` and `phoneNumber` matches [phone] (regex or exact). Lookup `users` on `companyOwner` for ownerName. Return: companyName, email, phoneNumber, country, lifecycleStage, ownerName.

---

**business:** Get all details of accounts with account type [Account Type]
**process:** Query `companies` where `deleted=false` and `type`=[type]. Valid enum: 'Prospect', 'Partner', 'Reseller', 'Vendor', 'Other'. Return: companyName, email, country, type, lifecycleStage, companyOwner (lookup users).

---

## SECTION 5 — DEALS

---

**business:** Get open deals for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false`, `company`=companyId, `dealWonAt`=null, `dealLostAt`=null. Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, ownerName. Sort by closeDate asc.

---

**business:** Get deal stages for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Group by `stage`: count deals per stage, sum grand_total_in_usd per stage. Return: stage, dealCount, totalValueUSD per stage.

---

**business:** Get deal owners for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Group by `owner`: count deals, sum grand_total_in_usd. Lookup `users` on `owner` for ownerName. Return: ownerName, dealCount, totalValueUSD.

---

**business:** Get total deal amount for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Aggregate: $group by null → totalAmount=$sum grand_total, totalAmountUSD=$sum grand_total_in_usd, dealCount=$sum 1. Return total summary.

---

**business:** Get deal created dates for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Return: name, stage, createdAt. Sort by createdAt desc.

---

**business:** Get deal closing dates for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Return: name, stage, closeDate, dealWonAt, dealLostAt. Sort by closeDate asc.

---

**business:** Get deal number for [Deal Name]
**process:** Query `deals` where `deleted=false` and `name` matches [Deal Name] (regex). Return: name, sequence_number displayed as deal_number = `ELS` + sequence_number padded to 3 digits with leading zeros.

---

**business:** Get highest value deal for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Sort by `grand_total_in_usd` desc, limit 1. Return: name, stage, grand_total_in_usd, closeDate, dealWonAt, dealLostAt.

---

**business:** Get lowest value deal for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false` and `company`=companyId. Sort by `grand_total_in_usd` asc, limit 1. Return: name, stage, grand_total_in_usd, closeDate.

---

**business:** Get overdue deals for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false`, `company`=companyId, open (dealWonAt=null AND dealLostAt=null), `closeDate` < today. Add computed `daysOverdue = today minus closeDate (in days)`. Return: name, stage, grand_total_in_usd, closeDate, daysOverdue. Sort by daysOverdue desc.

---

**business:** Get upcoming closing deals for [Account Name]
**process:** Find company `_id`. Query `deals` where `deleted=false`, `company`=companyId, open, `closeDate` >= today AND `closeDate` <= today plus 30 days. Return: name, stage, grand_total_in_usd, closeDate. Sort by closeDate asc.

---

**business:** Get total deals amount in USD
**process:** Query `deals` where `deleted=false`. Aggregate: $group by null → totalAmountUSD=$sum grand_total_in_usd, dealCount=$sum 1, wonAmountUSD=$sum grand_total_in_usd where dealWonAt!=null, lostAmountUSD where dealLostAt!=null, activeAmountUSD where both null. Return full summary.

---

**business:** Get account owner for deal number [Deal Number]
**process:** Extract numeric part from [Deal Number] (e.g., ELS007 → 7). Query `deals` where `sequence_number`=7. Lookup `users` on `owner` for dealOwnerName. Lookup `companies` on `company` → then lookup `users` on `companyOwner` for accountOwnerName. Return: dealName, dealOwnerName, accountOwnerName.

---

**business:** Get contact person for [Deal Name]
**process:** Query `deals` where `deleted=false` and `name` matches [Deal Name] (regex). Lookup `contacts` on `contact`. Return: dealName, contact firstName, lastName, email, phoneNumber, jobTitle.

---

**business:** Get notes for [Deal Name]
**process:** Find deal `_id`. Query `dealsnotes` where `deals`=dealId. Lookup `users` on `createdBy` for authorName. Return: title, notes, isPinned, authorName, createdAt. Sort by isPinned desc, createdAt desc.

---

**business:** Get meetings for [Deal Name]
**process:** Find deal's contact from `deals` → `contacts`. Get contact email. Query `meetings` where `attendees` contains that email. Return: title, start, end, location, attendees. Sort by start desc.

---

**business:** Get line items for [Deal Name]
**process:** Query `deals` where `deleted=false` and `name` matches [Deal Name]. Lookup `products` on `lineItems[].product`. Return `lineItems` array: product_name, quantity, unit_price, project_type (lookup projecttypes for name), total_price, total_price_in_usd, won, lost, reason (lost reason if applicable).

---

**business:** Get grand total for [Deal Name]
**process:** Query `deals` where `name` matches [Deal Name]. Return: name, subtotal, grand_total, grand_total_in_usd.

---

**business:** Get US deals grouped by business analyst
**process:** Find region `_id`s from `regions` where regionName contains "US" or "United States". Find company `_id`s where `region` in those regionIds. Query `deals` where `deleted=false` and `company` in those companyIds. Group by `business_analyst`: count deals, sum grand_total_in_usd. Lookup `users` on `business_analyst` for analystName. Return: analystName, dealCount, totalValueUSD. Sort by dealCount desc.

---

**business:** Get US deals grouped by project type
**process:** Find US-region company `_id`s (same as above). Query `deals` where `deleted=false` and `company` in US companyIds. Group by `project_type`: count deals, sum grand_total_in_usd. Lookup `projecttypes` on `project_type` for typeName. Return: projectTypeName, dealCount, totalValueUSD.

---

**business:** Get US deals grouped by technology
**process:** Find US-region company `_id`s. Query `deals` where `company` in US companyIds. Unwind `lineItems`. Lookup `products` on `lineItems.product`. Unwind `products.technology`. Lookup `technologies` for techName. Group by technology: count, sum total_price_in_usd. Return: techName, dealCount, totalValueUSD.

---

## SECTION 6 — CONTACTS

---

**business:** Get contacts by owner [Owner Name]
**process:** Find user `_id` from `users` by name. Query `contacts` where `deleted=false` and `contactOwner`=userId. Lookup `companies` on `company` for companyName. Return: firstName, lastName, email, phoneNumber, jobTitle, companyName, createdAt. Sort by createdAt desc.

---

**business:** Get contact type for [Contact Name]
**process:** Query `contacts` where `deleted=false` and name matches. Return: `lifecycleStage` and `leadStatus` — these define the contact's classification.

---

**business:** Get deals linked to [Contact Name]
**process:** Find contact `_id` from `contacts`. Query `deals` where `deleted=false` and `contact`=contactId. Lookup `users` on `owner` for ownerName. Return: name, stage, grand_total_in_usd, closeDate, dealWonAt, dealLostAt, ownerName.

---

**business:** Get recent activities for [Contact Name]
**process:** Find contact `_id`. Return `contacts.lastActivity` (type + createdAt). Also query `commonnotes` where `contactId`=contactId, sorted by createdAt desc. Return: lastActivity summary + recent notes list.

---

**business:** Get email communications for [Contact Name]
**process:** Find contact's email from `contacts`. Query `mails` collection (UserEmail) where `from` contains that email OR `to` contains that email. Return: subject, from, to, date, snippet. Sort by date desc.

---

**business:** Get meetings with [Contact Name]
**process:** Find contact email from `contacts`. Query `meetings` where `attendees` array contains that email. Return: title, start, end, location, attendees. Sort by start desc.

---

**business:** Get notes for [Contact Name]
**process:** Find contact `_id`. Query `contactsnotes` where `contact_id`=contactId. Lookup `users` on `createdBy` for authorName. Return: title, notes, isPinned, authorName, createdAt. Sort by isPinned desc, createdAt desc.

---

**business:** Get tasks for [Contact Name]
**process:** Find contact `_id`. Query `createtasks` where `deleted=false` and `contectId`=contactId. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName, note.

---

**business:** Get overdue tasks for [Contact Name]
**process:** Find contact `_id`. Query `createtasks` where `deleted=false`, `contectId`=contactId, `status='Pending'`, `due_date` < today. Add daysOverdue = today minus due_date. Return: Task, priority, due_date, daysOverdue. Sort by daysOverdue desc.

---

**business:** Get contacts with source [Source Name]
**process:** Find source `_id` from `sources`. Query `contacts` where `deleted=false` and `source`=sourceId. Lookup `companies` for companyName. Return: firstName, lastName, email, phoneNumber, jobTitle, companyName.

---

**business:** Get contacts with technology [Technology]
**process:** Find technology `_id`. Find company `_id`s where `webTechnologies` contains techId. Query `contacts` where `deleted=false` and `company` IN those companyIds. Lookup `companies` for companyName. Return: firstName, lastName, email, phoneNumber, companyName.

---

**business:** Get contacts with region [Region]
**process:** Find region `_id`. Find company `_id`s where `region`=regionId. Query `contacts` where `deleted=false` and `company` IN those companyIds. Lookup `companies` for companyName. Return: firstName, lastName, email, phoneNumber, companyName.

---

**business:** Get contacts with account type [Account Type]
**process:** Find company `_id`s from `companies` where `type`=[Account Type]. Query `contacts` where `deleted=false` and `company` IN those companyIds. Return: firstName, lastName, email, phoneNumber, companyName.

---

**business:** Get contacts with lead status [Lead Status]
**process:** Query `contacts` where `deleted=false` and `leadStatus` matches [status] case-insensitively. Lookup `companies` for companyName. Return: firstName, lastName, email, phoneNumber, leadStatus, companyName.

---

**business:** Get contacts with created date after/before [Date]
**process:** Query `contacts` where `deleted=false` and `createdAt` > [date] (or < for "before"). Lookup `companies` for companyName. Return: firstName, lastName, email, createdAt, companyName. Sort by createdAt.

---

**business:** Get contacts with last activity after/before [Date]
**process:** Query `contacts` where `deleted=false` and `lastActivity.createdAt` > [date] (or < for "before"). Lookup `companies` for companyName. Return: firstName, lastName, email, lastActivity.createdAt, companyName.

---

**business:** Get contacts with birth date after/before [Date]
**process:** Query `contacts` where `deleted=false` and `birthday` > [date] (or < for "before"). Lookup `companies` for companyName. Return: firstName, lastName, email, birthday, phoneNumber, companyName.

---

**business:** Get total contacts for [Owner Name]
**process:** Find user `_id`. Query `contacts` where `deleted=false` and `contactOwner`=userId. Use $count. Return: ownerName, totalContacts.

---

**business:** Get all accounts for [Contact Name]
**process:** Query `contacts` where `deleted=false` and name matches. Get `company` _id. Query `companies` for full company details. Return: companyName, type, country, lifecycleStage, leadStatus, companyOwner (lookup users for name).

---

**business:** Get all upcoming meetings for [Contact Name]
**process:** Find contact email. Query `meetings` where `attendees` contains email AND `start` > now. Return: title, start, end, location. Sort by start asc.

---

**business:** Get all open deals for [Contact Name]
**process:** Find contact `_id`. Query `deals` where `deleted=false`, `contact`=contactId, `dealWonAt`=null, `dealLostAt`=null. Return: name, stage, grand_total_in_usd, closeDate. Sort by closeDate asc.

---

**business:** Get contacts with country [Country Name]
**process:** Find company `_id`s from `companies` where `country` matches [country] case-insensitively. Query `contacts` where `deleted=false` and `company` IN those companyIds. Lookup `companies` for companyName. Return: firstName, lastName, email, phoneNumber, companyName, country.

---

## SECTION 7 — TARGETS

---

**business:** Get targets for [User Name]
**process:** Find user `_id` from `users` by name. Query `targets` where `userId`=userId. Sort by year desc, month desc. Convert month 0-index to name (0=January … 11=December). For each, also calculate achieved=sum grandtotal_in_usd from `invoices` where sales_person=userId, payment_status='paid', and invoice_date falls in that month/year. Return: userName, monthName, year, targetInUSD, achievedUSD, score=(achieved/target)*100.

---

**business:** Get all performance by user with targets below [Target Value]
**process:** Query `targets` where `targetInUSD` < [value]. Lookup `users` on `userId` for userName. For each, calculate achieved from `invoices` (same logic: sales_person=userId, paid, matching month/year). Compute score and gap. Return: userName, monthName, year, targetInUSD, achievedUSD, score, gap.

---

**business:** Get all performance by user with targets above [Target Value]
**process:** Same as above but `targetInUSD` > [value].

---

**business:** Get targets with performance above [Percentage]
**process:** Query all `targets`. For each, calculate achieved from `invoices`. Compute score=(achieved/targetInUSD)*100. Filter where score > [percentage]. Lookup `users` on `userId` for userName. Return: userName, monthName, year, targetInUSD, achievedUSD, score.

---

**business:** Get targets with performance below [Percentage]
**process:** Same as above but filter where score < [percentage].

---

**business:** Get targets with value greater than [Amount]
**process:** Query `targets` where `targetInUSD` > [amount]. Lookup `users` on `userId` for userName. Convert month to name. Return: userName, monthName, year, targetInUSD, teamName.

---

**business:** Get list of all performance by user for [Year]
**process:** Query `targets` where `year`=[year]. Lookup `users` on `userId` for userName. For each, calculate achieved from `invoices` for that user+month+year. Return: userName, monthName (converted from 0-index), targetInUSD, achievedUSD, score. Sort by month asc.

---

**business:** Get list of all performance by user for [Month]
**process:** Convert month name to 0-indexed number (e.g., January=0). Query `targets` where `month`=[0-indexed month]. Lookup `users`. Calculate achieved from `invoices` for matching userId+month+year. Return: userName, year, targetInUSD, achievedUSD, score.

---

**business:** Get the score for [User Name]
**process:** Find user `_id`. Get current month target from `targets` where userId=userId, month=current month (0-indexed), year=current year. Calculate achieved=sum grandtotal_in_usd from `invoices` where sales_person=userId, payment_status='paid', invoice_date in this month. Score=(achieved/targetInUSD)*100. Return: userName, currentMonth, year, targetInUSD, achievedUSD, score.

---

**business:** Get the performance overview for [User Name]
**process:** Find user `_id`. Query ALL `targets` for this user. For each, calculate achieved. Return full history: monthName, year, targetInUSD, achievedUSD, score, gap=target-achieved. Also include totals: totalTarget, totalAchieved, overallScore across all periods.

---

**business:** Get the target achieved status for [User Name]
**process:** Find user's current month target. Calculate score. Assign status: score >= 100 = "Achieved", 75-99 = "Near Target", < 75 = "Below Target". Return: userName, monthName, year, targetInUSD, achievedUSD, score, status.

---

**business:** Get the total target data for [Month]
**process:** Convert month name to 0-index. Query `targets` where `month`=[0-index]. Sum targetInUSD across all users. Calculate totalAchieved from `invoices` for all those users in that month (any year — or specify year). Return: monthName, userCount, totalTargetUSD, totalAchievedUSD, overallScore.

---

**business:** Get the Overall Performance yearly
**process:** Query all `targets`. Group by `year`: sum targetInUSD. For each year, sum achieved from `invoices` (payment_status='paid', invoice_date within that year). Compute yearlyScore=(achieved/target)*100. Return: year, totalTargetUSD, totalAchievedUSD, yearlyScore. Sort by year desc.

---

**business:** Get the Overall Performance monthly
**process:** Query all `targets`. Group by year+month: sum targetInUSD. For each group, sum achieved from invoices for that month/year. Return: year, monthName (convert), totalTargetUSD, totalAchievedUSD, score. Sort by year desc, month desc.

---

**business:** Get the Overall Performance quarterly
**process:** Query all `targets`. Assign quarter from month: Q1=months 0-2, Q2=3-5, Q3=6-8, Q4=9-11. Group by year+quarter: sum targetInUSD. Calculate achieved per quarter from invoices. Return: year, quarter (Q1-Q4), totalTargetUSD, totalAchievedUSD, score.

---

**business:** Get the Total Achievement yearly
**process:** Query `invoices` where `deleted=false` and `payment_status='paid'`. Group by year of invoice_date: totalAchievedUSD=$sum grandtotal_in_usd, invoiceCount=$sum 1. Return: year, totalAchievedUSD, invoiceCount. Sort by year desc.

---

**business:** Get the Total Achievement monthly
**process:** Query `invoices` where `deleted=false` and `payment_status='paid'`. Group by year+month of invoice_date: totalAchievedUSD=$sum grandtotal_in_usd. Return: year, monthName, totalAchievedUSD. Sort by year desc, month desc.

---

**business:** Get the Achieved value for [Month]
**process:** Convert month name to number (1-based, since $month operator returns 1-12). Query `invoices` where `deleted=false`, `payment_status='paid'`, and $month of invoice_date = [month number]. Group by null: totalAchievedUSD=$sum grandtotal_in_usd. Return: monthName, totalAchievedUSD.

---

**business:** Get the Achieved value for [Year]
**process:** Query `invoices` where `deleted=false`, `payment_status='paid'`, and $year of invoice_date = [year]. Group by null: totalAchievedUSD=$sum grandtotal_in_usd. Return: year, totalAchievedUSD.

---

**business:** Get the Growth Potential for [User Name]
**process:** Find user's current month target. Calculate achievedUSD. growthPotential=targetInUSD minus achievedUSD. If positive: remaining potential. Return: userName, targetInUSD, achievedUSD, growthPotential, growthPotentialPct=(growthPotential/target)*100.

---

**business:** Get the Quarterly analytics view for [User Name]
**process:** Find user `_id`. Query all `targets` for this user. Group by year+quarter (month 0-2=Q1, 3-5=Q2, 6-8=Q3, 9-11=Q4): sum targetInUSD per quarter. Calculate achievedUSD per quarter from invoices. Return: year, quarter, targetInUSD, achievedUSD, score, growthPotential=target-achieved.

---

**business:** Get the Performance by Month view for [Year]
**process:** Query `targets` where `year`=[year]. For each of 12 months (0-11), aggregate targetInUSD. Calculate achieved from invoices per month. Return 12-row table: monthName, totalTargetUSD, totalAchievedUSD, score. Fill 0 for months with no target data.

---

**business:** Get the Performance by User data
**process:** Query all `targets`. Group by `userId`: sum targetInUSD. Calculate totalAchieved from invoices per user (all time). Lookup `users` for userName. Return: userName, totalTargetUSD, totalAchievedUSD, overallScore=(totalAchieved/totalTarget)*100, growthPotential. Sort by overallScore desc.

---

**business:** Get the Total Achievement for [User Name]
**process:** Find user `_id`. Sum grandtotal_in_usd from `invoices` where `sales_person`=userId AND `payment_status='paid'` (all time). Also sum targetInUSD from `targets` where `userId`=userId (all time). Return: userName, totalTargetUSD, totalAchievedUSD, overallScore.

---

**business:** Get all performance by user with score below [Score Value]
**process:** Query all `targets`. For each, calculate score=(achieved/target)*100. Filter where score < [value]. Lookup `users` for userName. Return: userName, monthName, year, targetInUSD, achievedUSD, score.

---

**business:** Get all performance by user with Growth Potential below [Growth Potential Value]
**process:** Query all `targets`. Calculate growthPotential=targetInUSD minus achievedUSD for each. Filter where growthPotential < [value]. Return: userName (lookup), monthName, year, targetInUSD, achievedUSD, growthPotential.

---

## SECTION 8 — OUTREACH

---

**business:** Get the Total Outreach Data figure
**process:** Query `outreaches` where `isDeleted=false`. Count total. Also group by `status` for breakdown. Valid statuses: 'Unassigned', 'Not Contacted', 'Contacted', 'Followup', 'Converted to Deal'. Return: totalCount, statusBreakdown with count per status.

---

**business:** Get the count of Unassigned CSV
**process:** Query `outreaches` where `isDeleted=false` and `assignedTo`=null. Count records (unassigned contacts). Also get count of distinct `sourceFile` values where contacts are unassigned (unassigned datasets). Return: unassignedContactCount, unassignedDatasetCount.

---

**business:** Get the number of 7 Days Interested contacts
**process:** Query `outreaches` where `isDeleted=false`, `leadStatus='Intrested'` (exact spelling in DB), and `interestedDate` >= (today minus 7 days). Use $count. Return: count.

---

**business:** Get the Total Interested count
**process:** Query `outreaches` where `isDeleted=false` and `leadStatus='Intrested'`. Use $count. Return: totalInterestedCount.

---

**business:** Get the No. of Datasets touches last week
**process:** Calculate last week's Monday 00:00 to Sunday 23:59. Query `outreachactivities` where `createdAt` >= startOfLastWeek AND `createdAt` <= endOfLastWeek. Group by `regionId`: sum `count`. Lookup `regions` on `regionId` for regionName. Return: totalTouchesLastWeek, breakdown by regionName.

---

**business:** Get the Unique Touches (Lifetime) value
**process:** Query `outreachactivities`. Sum total `count` across all records for totalTouches (lifetime). For unique: count distinct `ActivityId` values to get unique activity types touched. Return: totalTouchesLifetime, uniqueActivityTypes.

---

**business:** Get the count of 7 Days Unique Touches
**process:** Query `outreachactivities` where `createdAt` >= (today minus 7 days). Sum `count`. Count distinct `ActivityId` values (unique types). Return: totalTouches7Days, uniqueTouches7Days.

---

**business:** Get the Name of the contact with [Lead Status]
**process:** Query `outreaches` where `isDeleted=false` and `leadStatus`=[status]. Valid leadStatus values: '-', 'Nurturing', 'Lost', 'Intrested', 'Mislabeled', 'Converted to Deal', 'To Be Verified', 'Verified'. Return: name, email, designation, country, priority, assignedTo (lookup users for name).

---

**business:** Get the City for the [contact]
**process:** Query `outreaches` where `isDeleted=false` and `name` matches [contact] (regex). Return: name, city, country.

---

**business:** Get the Country for the contact with [Status]
**process:** Query `outreaches` where `isDeleted=false` and `status`=[status]. Return: name, country, city, status.

---

**business:** Get the contacts with [Priority]
**process:** Query `outreaches` where `isDeleted=false` and `priority`=[priority]. Valid: '-', 'Low', 'Medium', 'High'. Return: name, email, designation, country, status, leadStatus, priority.

---

**business:** Get the contacts with [Status]
**process:** Query `outreaches` where `isDeleted=false` and `status`=[status]. Valid: 'Unassigned', 'Not Contacted', 'Contacted', 'Followup', 'Converted to Deal'. Return: name, email, designation, country, leadStatus, priority, assignedTo (lookup users).

---

**business:** Get the contacts filtered by [City]
**process:** Query `outreaches` where `isDeleted=false` and `city` matches [city] case-insensitively (regex). Return: name, email, designation, city, country, status, leadStatus.

---

**business:** Get the contacts filtered by [Country]
**process:** Query `outreaches` where `isDeleted=false` and `country` matches [country] case-insensitively (regex). Return: name, email, designation, country, status, leadStatus, priority.

---

**business:** Get the contacts filtered by [Dataset]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[dataset name] (exact or regex match). Lookup `users` on `assignedTo` for assigneeName. Lookup `regions` on `region` for regionName. Return: name, email, designation, country, status, leadStatus, priority, assigneeName, regionName.

---

**business:** Get the contacts filtered by [Category]
**process:** Find category `_id` from `categories` where `categoryName` matches [category]. Find campaign `_id`s from `campaigns` where `categoryId`=categoryId. Query `outreaches` where `isDeleted=false` and `campaign` IN those campaign _ids. Return: name, email, designation, country, status, leadStatus.

---

**business:** Get the contacts filtered by [Assigned To]
**process:** Find user `_id` from `users` by name. Query `outreaches` where `isDeleted=false` and `assignedTo`=userId. Return: name, email, designation, country, status, leadStatus, priority, sourceFile.

---

**business:** Get the CSV Name for [Dataset Name]
**process:** Query `outreaches` where `isDeleted=false`. Get distinct `sourceFile` values that match or relate to [Dataset Name]. Return: list of CSV/sourceFile names (in this system, sourceFile IS the CSV/dataset name).

---

**business:** Get the Category for [CSV Name]
**process:** Query `outreaches` where `sourceFile`=[CSV Name]. Get `campaign` _id. Lookup `campaigns` on `campaign`. Lookup `categories` on `campaigns.categoryId`. Return: csvName, campaignName, categoryName.

---

**business:** Get the Region for [Dataset Name]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[Dataset Name]. Get distinct `region` _ids. Lookup `regions` for each. Return: datasetName, list of regionNames.

---

**business:** Get the Total Touches value for [CSV Name]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[CSV Name]. Get distinct `region` _ids from those records. Query `outreachactivities` where `regionId` IN those region _ids. Sum `count`. Return: csvName, totalTouches.

---

**business:** Get the Total Data count for [CSV Name]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[CSV Name]. Use $count. Return: csvName, totalRecords.

---

**business:** Get the CSV file Assigned To [User Name]
**process:** Find user `_id` from `users` by name. Query `outreaches` where `assignedTo`=userId. Get distinct `sourceFile` values. Return: list of CSV/dataset names assigned to this user.

---

**business:** Get the Creation Date for [CSV Name]
**process:** Query `outreaches` where `sourceFile`=[CSV Name]. Sort by `createdAt` ascending, limit 1. Return: csvName, earliest createdAt (= the date this dataset was first imported/created).

---

**business:** Get the Dataset name for [contact]
**process:** Query `outreaches` where `isDeleted=false` and `name` matches [contact] (regex). Return: name, `sourceFile` (this is the dataset/CSV name for this contact).

---

**business:** Get the count of Items displayed for [User Name]
**process:** Find user `_id`. Count `outreaches` where `assignedTo`=userId AND `isDeleted=false`. Return: userName, assignedContactCount.

---

**business:** Get the total Touch count for [User Name]
**process:** Find user `_id`. Query `outreachactivities` where `createdBy`=userId. Sum `count` across all records. Return: userName, totalTouchCount.

---

**business:** Get the report data
**process:** Query `outreachactivities`. Lookup `regions` on `regionId`. Lookup `activityevents` on `ActivityId` for activityName. Group by region: sum count. Return: regionName, activityName (or types), totalTouches, with createdAt range.

---

**business:** Get the report details for [Report Name]
**process:** Report = a sourceFile/dataset. Query `outreaches` where `sourceFile`=[Report Name]. Summarize: count by status, count by leadStatus, count by priority. Get distinct regions. Return: reportName, totalContacts, statusBreakdown, leadStatusBreakdown, priorityBreakdown, regionNames.

---

**business:** Get the report details before [Date]
**process:** Query `outreachactivities` where `createdAt` < [date]. Lookup `regions` on `regionId`. Group by region: sum count. Return: regionName, totalTouches for activity before that date.

---

**business:** Get the report data after [Date]
**process:** Query `outreachactivities` where `createdAt` > [date]. Lookup `regions` for regionName. Group by region: sum count. Return: regionName, totalTouches after that date.

---

**business:** Get the Regions associated with [Report Name]
**process:** Query `outreaches` where `sourceFile`=[Report Name]. Get distinct `region` _ids. Lookup `regions`. Return: list of regionNames for this report/dataset.

---

**business:** Get the Category Name list
**process:** Query `categories` collection, find all documents. Return: list of all categoryName values. Sort alphabetically.

---

**business:** Get the total number of Categories
**process:** Query `categories` collection, use $count. Return: totalCategories.

---

**business:** Get the list of all Datasets
**process:** Query `outreaches` where `isDeleted=false`. Use distinct on `sourceFile` field. Filter out null/empty. Return: sorted list of all dataset/CSV names.

---

**business:** Get the CSV Count for [Dataset Name]
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[Dataset Name]. Use $count. Return: datasetName, csvCount (total contacts in this dataset).

---

## SECTION 9 — TASKS

---

**business:** Get the Associated Record for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title] (regex). Return: Task, associated_module, associated_item, and resolve linked records: if companyId set → lookup companies for companyName; if contectId set → lookup contacts for contact name; if dealsId set → lookup deals for deal name; if invoiceId set → lookup invoices for invoice_number; if salesId set → lookup sales for sales_number.

---

**business:** Get the Due Date for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title] (regex). Return: Task, due_date, status, isOverdue=(due_date < today).

---

**business:** Get the Owner for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Lookup `users` on `createdBy` for ownerName. Return: Task, ownerName.

---

**business:** Get the tasks with a Priority of [Priority Level]
**process:** Query `createtasks` where `deleted=false` and `priority`=[priority]. Valid: "Low", "Medium", "High". Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName, associated_module. Sort by due_date asc.

---

**business:** Get the tasks with a Closed Time of [Date]
**process:** Query `createtasks` where `deleted=false` and `closedDate` >= [date] 00:00:00 AND `closedDate` <= [date] 23:59:59. Lookup `users` on `createdBy` for ownerName. Return: Task, status, closedDate, ownerName.

---

**business:** Get the tasks with a Created date of [Date]
**process:** Query `createtasks` where `deleted=false` and `createdAt` >= [date] 00:00:00 AND `createdAt` <= [date] 23:59:59. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName, createdAt.

---

**business:** Get the Set To Repeat status for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, setToRepeat, repeatDateTime. setToRepeat default is "None"; other values are custom repeat settings.

---

**business:** Get the associated record details for [Associated Record Name]
**process:** Query `createtasks` where `deleted=false` and `associated_item` matches [name] (regex). Return all matching tasks with: Task, associated_module, associated_item, status, priority, due_date. Then resolve the actual record: use `associated_module` to know which collection to look up (Company/Deal/Contact/Invoice/Sales).

---

**business:** Get the count of tasks with [Priority Level]
**process:** Query `createtasks` where `deleted=false` and `priority`=[priority]. Use $count. Return: priority, taskCount.

---

**business:** Get the tasks with Due Date before [Date]
**process:** Query `createtasks` where `deleted=false` and `due_date` < [date]. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName. Sort by due_date asc.

---

**business:** Get the tasks with Created date after [Date]
**process:** Query `createtasks` where `deleted=false` and `createdAt` > [date]. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, createdAt, ownerName. Sort by createdAt desc.

---

**business:** Get the tasks with Created date before [Date]
**process:** Query `createtasks` where `deleted=false` and `createdAt` < [date]. Return: Task, status, priority, createdAt. Sort by createdAt desc.

---

**business:** Get the tasks with Due Date after [Date]
**process:** Query `createtasks` where `deleted=false` and `due_date` > [date]. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName. Sort by due_date asc.

---

**business:** Get the tasks with Closed Time before [Date]
**process:** Query `createtasks` where `deleted=false` and `closedDate` < [date]. Lookup `users` on `createdBy`. Return: Task, status, closedDate, ownerName. Sort by closedDate asc.

---

**business:** Get the tasks with Closed Time after [Date]
**process:** Query `createtasks` where `deleted=false` and `closedDate` > [date]. Return: Task, status, closedDate. Sort by closedDate desc.

---

**business:** Get the tasks that are set to repeat
**process:** Query `createtasks` where `deleted=false` and `setToRepeat` != 'None'. Return: Task, setToRepeat, repeatDateTime, due_date, status, priority.

---

**business:** Get the tasks that are set to do not repeat
**process:** Query `createtasks` where `deleted=false` and `setToRepeat`='None'. Return: Task, setToRepeat, due_date, status.

---

**business:** Get the Status for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, status (enum: "Pending" or "Completed"), category (enum: "Open" or "Close").

---

**business:** Get the Description for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, `note` field (this is the task description/detail field).

---

**business:** Get the Category for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, category. Valid values: "Open" or "Close".

---

**business:** Get the Reminder set for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, reminder (valid values: "At task due date", "10 min before", "30 min before", "1 hour before", "1 day before", "2 day before", "3 day before"), lastReminderSent.

---

**business:** Get Priority level for [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` matches [title]. Return: Task, priority. Valid: "Low", "Medium", "High".

---

**business:** Get tasks with category [Category Type]
**process:** Query `createtasks` where `deleted=false` and `category`=[type]. Valid: "Open" or "Close". Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, category, ownerName.

---

**business:** Get tasks with Reminder [Reminder Type]
**process:** Query `createtasks` where `deleted=false` and `reminder`=[type]. Return: Task, status, priority, due_date, reminder.

---

**business:** Get tasks with status [Status]
**process:** Query `createtasks` where `deleted=false` and `status`=[status]. Valid: "Pending" or "Completed". Lookup `users` on `createdBy` for ownerName. Return: Task, priority, due_date, closedDate, associated_module, ownerName.

---

**business:** Get the Tasks with contact [Contact Name]
**process:** Find contact `_id` from `contacts` by name. Query `createtasks` where `deleted=false` and `contectId`=contactId. Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, ownerName.

---

**business:** Get the task with Description [Description Text]
**process:** Query `createtasks` where `deleted=false` and `note` matches [description text] (case-insensitive regex on the `note` field). Return: Task, note, status, priority, due_date.

---

**business:** Get count of all tasks with [Set To Repeat Status]
**process:** Query `createtasks` where `deleted=false` and `setToRepeat`=[status value]. Use $count. Return: setToRepeat value, count.

---

**business:** Get count of all tasks with owner [User Name]
**process:** Find user `_id`. Query `createtasks` where `deleted=false` and `createdBy`=userId. Use $count. Return: ownerName, totalTasks.

---

**business:** Get count of all tasks with due date before [Date]
**process:** Query `createtasks` where `deleted=false` and `due_date` < [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with due date after [Date]
**process:** Query `createtasks` where `deleted=false` and `due_date` > [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with created date after [Date]
**process:** Query `createtasks` where `deleted=false` and `createdAt` > [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with created date before [Date]
**process:** Query `createtasks` where `deleted=false` and `createdAt` < [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with closed time before [Date]
**process:** Query `createtasks` where `deleted=false` and `closedDate` < [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with closed time after [Date]
**process:** Query `createtasks` where `deleted=false` and `closedDate` > [date]. Use $count. Return: count.

---

**business:** Get count of all tasks with status [Status]
**process:** Query `createtasks` where `deleted=false` and `status`=[status]. Use $count. Return: status, count.

---

**business:** Get count of all tasks with category [Category Type]
**process:** Query `createtasks` where `deleted=false` and `category`=[category]. Use $count. Return: category, count.

---

**business:** Get count of all tasks with reminder set to [Reminder Type]
**process:** Query `createtasks` where `deleted=false` and `reminder`=[reminder]. Use $count. Return: reminder, count.

---

**business:** Get count of all tasks with Associated Record [Associated Record Name]
**process:** Query `createtasks` where `deleted=false` and `associated_item` matches [name] (regex). Use $count. Return: associatedRecord, count.

---

**business:** Get all tasks with Associated Record [Associated Record Name]
**process:** Query `createtasks` where `deleted=false` and `associated_item` matches [name] (regex). Lookup `users` on `createdBy` for ownerName. Return: Task, status, priority, due_date, associated_module, associated_item, ownerName.

---

**business:** Get the tasks that are not associated with any Contact
**process:** Query `createtasks` where `deleted=false` and `contectId`=null AND (`associated_module` != 'Contact' OR `associated_module`=null). Return: Task, status, priority, due_date, associated_module.

---

**business:** Get the list of tasks not owned by [User Name]
**process:** Find user `_id`. Query `createtasks` where `deleted=false` and `createdBy` != userId. Lookup `users` on `createdBy` for actual ownerName. Return: Task, status, priority, due_date, ownerName.

---

**business:** Get the tasks where the Associated Record is of type [Account/Deal/Contact/Invoice/Sales]
**process:** Query `createtasks` where `deleted=false` and `associated_module`=[module type] (e.g., "Company", "Deal", "Contact", "Invoice", "Sales" — match the exact string stored). Return: Task, status, priority, due_date, associated_module, associated_item.

---

**business:** Get all tasks not with Associated Record [Associated Record Name]
**process:** Query `createtasks` where `deleted=false` and `associated_item` NOT matching [name] (use $not with $regex). Return: Task, status, priority, due_date, associated_module.

---

**business:** Get all tasks not with [Task Title]
**process:** Query `createtasks` where `deleted=false` and `Task` NOT matching [title] (use $not with $regex). Return: Task, status, priority, due_date. (Returns all other tasks.)

---

## SECTION 10 — COMMON NATURAL LANGUAGE BUSINESS QUERIES

---

**business:** What tasks are pending for the users by?
**process:** Query `createtasks` where `deleted=false` and `status='Pending'`. Group by `createdBy` (task owner). For each user compute: totalPending=$sum 1, highPriority=$sum where priority='High', mediumPriority=$sum where priority='Medium', lowPriority=$sum where priority='Low', overdueTasks=$sum where due_date < today. Lookup `users` on `createdBy` for userName. Sort by overdueTasks desc, then totalPending desc. Return table: userName, totalPending, highPriority, mediumPriority, lowPriority, overdueTasks. Add a TOTAL row at the bottom.

---

**business:** List new leads from this week
**process:** Calculate start of current week = Monday 00:00:00 of this week. TWO SOURCES — query both: (1) `companies` where `deleted=false` and `createdAt` >= startOfWeek — these are CRM leads. Lookup `users` on `companyOwner` for ownerName. Return: companyName, country, leadStatus, lifecycleStage, ownerName, createdAt. (2) `outreaches` where `isDeleted=false` and `createdAt` >= startOfWeek — these are outreach prospects. Lookup `users` on `assignedTo`. Return: name, email, designation, country, status, createdAt. Show both lists separately with counts. Total new leads = CRM count + Outreach count.

---

**business:** New leads this month
**process:** Calculate start of current month = 1st day 00:00:00, end = last day 23:59:59. TWO SOURCES — (1) `companies` where `deleted=false` and `createdAt` >= startOfMonth AND `createdAt` <= endOfMonth. Lookup `users` on `companyOwner` for ownerName. Return: companyName, country, leadStatus, ownerName, createdAt. (2) `outreaches` where `isDeleted=false` and `createdAt` >= startOfMonth. Lookup `users` on `assignedTo`. Return: name, email, designation, country, status, createdAt. Show both lists with total count from each source.

---

**business:** Total revenue this financial year
**process:** Dynamically calculate current Indian FY: if current month >= 4 (April or later) → FY start = April 1 of current year, FY end = March 31 of next year. If current month < 4 (Jan/Feb/Mar) → FY start = April 1 of previous year, FY end = March 31 of current year. Query `invoices` where `deleted=false`, `payment_status='paid'`, `invoice_date` >= fyStart AND `invoice_date` <= fyEnd. Group: totalRevenueUSD=$sum grandtotal_in_usd, invoiceCount=$sum 1, avgInvoice=$avg grandtotal_in_usd. Monthly breakdown for all months in FY. Return: FY period label (e.g., "FY2026: Apr 2025–Mar 2026"), totalRevenueUSD, invoiceCount, avgInvoice, monthly breakdown.

---

**business:** Pending invoices
**process:** Query `invoices` where `deleted=false` and `payment_status` IN ['draft', 'confirmed', 'partial_payment'] (everything that is NOT fully paid and NOT cancelled). Lookup `companies` on `company` for companyName. Add computed fields: isOverdue=(due_date < today), daysOverdue=today minus due_date (only if overdue). Summary group: pendingCount=$sum 1, totalPendingValueUSD=$sum grandtotal_in_usd, overdueCount=$sum where isOverdue=true. Return summary + individual list: invoice_number, payment_status, grand_total, grandtotal_in_usd, currency, due_date, isOverdue, daysOverdue, companyName. Sort by isOverdue desc, due_date asc (most urgent first).

---

**business:** Overdue payments
**process:** Query `invoices` where `deleted=false`, `payment_status` IN ['draft', 'confirmed', 'partial_payment'], AND `due_date` < today (strictly past due). For each, compute: paidSoFar=$sum payment_history[].payment_amount_in_usd, remainingAmount=grandtotal_in_usd minus paidSoFar, daysOverdue=today minus due_date (in whole days). Lookup `companies` on `company` for companyName. Lookup `users` on `invoiceOwner` for ownerName. Sort by daysOverdue desc (longest overdue first). Return: total outstanding amount (sum of all remainingAmount), overdueCount, and list: invoice_number, payment_status, grandtotal_in_usd, paidSoFar, remainingAmount, due_date, daysOverdue, companyName, ownerName.

---

**business:** Sales pipeline by stage
**process:** Query `deals` where `deleted=false`, `dealWonAt`=null, `dealLostAt`=null (open/active deals ONLY — these are the pipeline). Group by `stage`: dealCount=$sum 1, totalValueUSD=$sum grand_total_in_usd, avgDealSize=$avg grand_total_in_usd. Compute totalPipelineValue=$sum of all groups' totalValueUSD. For each stage add pipelineSharePct=(stage totalValueUSD / totalPipelineValue)*100. Sort by totalValueUSD desc. Return: stage, dealCount, totalValueUSD, avgDealSize, pipelineSharePct. Add TOTAL row: summed dealCount, summed totalValueUSD. Do NOT include won or lost deals.

---

**business:** Show pipeline distribution by stage
**process:** Same as "Sales pipeline by stage" above. Query `deals` where `deleted=false`, open (dealWonAt=null AND dealLostAt=null). Group by `stage`: dealCount, totalValueUSD, avgDealSize. Calculate each stage's percentage share of total pipeline value. Return: stage, dealCount, totalValueUSD, avgDealSize, pipelineSharePct (%). Sort by totalValueUSD desc. Present as a distribution table.

---

**business:** Which deals are stuck in the proposal stage?
**process:** Query `deals` where `deleted=false`, `dealWonAt`=null, `dealLostAt`=null (open only), AND `stage` matches "proposal" case-insensitively (use regex /proposal/i — covers "Proposal", "Proposal Sent", "In Proposal" etc.), AND `updatedAt` < (today minus 7 days) — no update in 7+ days means stuck. Add computed `daysStuck = today minus updatedAt (in days)`. Lookup `companies` on `company` for companyName. Lookup `users` on `owner` for ownerName. Sort by daysStuck desc. Return: name, stage, grand_total_in_usd, closeDate, daysStuck, companyName, ownerName. Flag deals where daysStuck >= 14 as critical.

---

**business:** How many opportunities were lost in July?
**process:** Determine the relevant July year: if today is before July of current year → use previous year's July; otherwise use current year's July. Query `deals` where `deleted=false` and `dealLostAt` >= July 1 00:00:00 AND `dealLostAt` <= July 31 23:59:59 of that year. Count total lost deals and sum grand_total_in_usd for total value lost. Group by `owner` for rep-level breakdown. Lookup `users` on `owner` for repName. Return: totalLostDeals, totalValueLostUSD, breakdown by rep (repName, dealsLost, valueLost). Sort reps by dealsLost desc.

---

**business:** Lost deals last quarter
**process:** Calculate last quarter date range dynamically: determine current quarter (Q1=Jan–Mar, Q2=Apr–Jun, Q3=Jul–Sep, Q4=Oct–Dec), then go back one quarter (handle year rollover: if Q1 is current, last quarter = Q4 of previous year). Query `deals` where `deleted=false` and `dealLostAt` >= lastQuarterStart AND `dealLostAt` <= lastQuarterEnd. Aggregate: totalLost=$count, totalValueLostUSD=$sum grand_total_in_usd. Group by `owner` for rep breakdown. Group by `stage` to see which stages deals were lost from. Lookup `users` on `owner` for repName. Return: quarterLabel (e.g., "Q1 2025 Jan–Mar"), totalLostDeals, totalValueLostUSD, byRep table (repName, count, valueLost), byStage table (stage, count).

---

**business:** Which sales rep closed the most deals this quarter?
**process:** Calculate current quarter: Q1=Jan–Mar (months 1–3), Q2=Apr–Jun (4–6), Q3=Jul–Sep (7–9), Q4=Oct–Dec (10–12). Get quarterStart=first day 00:00:00 and quarterEnd=last day 23:59:59 of current quarter. Query `deals` where `deleted=false` and `dealWonAt` >= quarterStart AND `dealWonAt` <= quarterEnd. Group by `owner`: dealsWon=$sum 1, totalRevenueUSD=$sum grand_total_in_usd. Sort by dealsWon desc. Lookup `users` on `owner` for repName and email. Return: TOP REP (name, dealsWon, totalRevenueUSD) clearly highlighted + full ranked list of all reps. Show quarter label (e.g., "Q2 2026: Apr–Jun 2026").

---

**business:** What tasks are pending for the sales team?
**process:** Find the Sales department _id: query `departments` where `name` matches "Sales" (case-insensitive). Find user `_id`s from `users` where `department`=salesDeptId AND `isActive`=true. Query `createtasks` where `deleted=false`, `status='Pending'`, and `createdBy` IN those sales user _ids. Add: isOverdue=(due_date < today), daysOverdue for overdue ones. Lookup `users` on `createdBy` for ownerName. Sort by isOverdue desc, priority desc (High→Medium→Low), due_date asc. Summary: totalPending=$count, overdueCount, highPriorityCount. Return summary + list: Task, priority, due_date, isOverdue, daysOverdue, ownerName, associated_module, note.

---

**business:** Pending tasks
**process:** Query `createtasks` where `deleted=false` and `status='Pending'`. Add: isOverdue=(due_date < today), daysOverdue=today minus due_date (for overdue ones only). Lookup `users` on `createdBy` for ownerName. Summary: totalPending=$count, overdueCount=$sum where isOverdue=true, highPriorityCount=$sum where priority='High', mediumPriorityCount, lowPriorityCount. Return summary + full list: Task, priority, due_date, isOverdue, daysOverdue, ownerName, associated_module, note. Sort by isOverdue desc, then priority desc (High first), then due_date asc.

---

**business:** Total revenue ?
**process:** Revenue must come from `invoices` (not `sales`). If period is not provided, default to current Indian financial year (Apr 1 to Mar 31). Query `invoices` where `deleted=false`, `payment_status='paid'`, and `invoice_date` within the requested/default period. Aggregate: totalRevenueUSD=$sum grandtotal_in_usd, invoiceCount=$sum 1, avgInvoiceValueUSD=$avg grandtotal_in_usd. Also create month-wise breakdown grouped by year+month of `invoice_date`. Return: periodLabel, totalRevenueUSD, invoiceCount, avgInvoiceValueUSD, monthly breakdown.

---

**business:** Show me the top 10 customers by revenue.
**process:** Use `invoices` as the revenue source. Query `invoices` where `deleted=false` and `payment_status='paid'`. Group by `company`: totalRevenueUSD=$sum grandtotal_in_usd, invoiceCount=$sum 1, avgInvoiceValueUSD=$avg grandtotal_in_usd. Sort by totalRevenueUSD descending and limit 10. Lookup `companies` for companyName/country and lookup `users` on companyOwner for ownerName. Return ranked top-10 table with revenue and invoice counts.

---

**business:** Show me the total sales for last month.
**process:** Compute previous calendar month boundaries (start at 1st 00:00:00, end at last day 23:59:59). Query `sales` where `deleted=false` and `sales_date` is within this range. Aggregate summary: totalSalesOrders=$sum 1, totalSalesValue=$sum grand_total, avgSalesOrderValue=$avg grand_total. Optional owner split: group by `salesOwner` and lookup `users` for rep names.

---

**business:** Which leads haven’t been contacted in 7 days?
**process:** Query both lead sources. (1) `companies` where `deleted=false`, `lifecycleStage` matches /lead/i, and (`lastActivity.createdAt` < now-7days OR `lastActivity.createdAt` is null). Lookup `users` on `companyOwner`. (2) `outreaches` where `isDeleted=false`, `status='Not Contacted'`, and `createdAt` <= now-7days. Lookup `users` on `assignedTo`. Return both result sets with counts and combined total.

---

**business:** What’s the conversion rate from lead to customer?
**process:** Use `companies` for CRM conversion. Choose a period (if not provided, default to current Indian FY). totalLeads = count of `companies` where `deleted=false` and `createdAt` in period. convertedCustomers = count where `deleted=false`, `createdAt` in period, and `leadWonAt` is not null. conversionRatePct=(convertedCustomers/totalLeads)*100 (if totalLeads=0, return 0). Optional owner-level split by `companyOwner` with `users` lookup.

---

**business:** Any follow-ups overdue this week
**process:** Query two follow-up channels. (1) `createtasks` where `deleted=false`, `status='Pending'`, `due_date` <= endOfCurrentWeek, and `due_date` < now. Lookup `users` on `createdBy`. (2) `outreaches` where `isDeleted=false`, `status='Followup'`, `ReminderDate` <= endOfCurrentWeek, and `ReminderDate` < now. Lookup `users` on `assignedTo`. Return overdue task count, overdue outreach count, combined total, and detail rows.

---

**business:** What is the conversion rate from leads to customers
**process:** Same conversion logic as "What’s the conversion rate from lead to customer?" Use `companies` only: totalLeads from `createdAt` in selected period, convertedCustomers where `leadWonAt` is not null, conversionRatePct=(convertedCustomers/totalLeads)*100. Default period = current Indian financial year when user does not specify dates.

---

**business:** Get status for this sales order
**process:** Resolve sales order using `sales_number`. Query `sales` where `deleted=false` and `sales_number`=[SO number]. Return: sales_number, status, sales_date, sales_updated_date.

---

**business:** Get owner for this sales order
**process:** Query `sales` where `deleted=false` and `sales_number`=[SO number]. Lookup `users` on `salesOwner`. Return: sales_number, ownerName, ownerEmail.

---

**business:** Get complete details for this sales order
**process:** Query `sales` where `deleted=false` and `sales_number`=[SO number]. Lookup `companies` on `company` and `users` on `salesOwner`. Return full SO details: sales_number, status, items, subtotal, tax, tax_amount, discount_amount, grand_total, sales_date, contract, paymentReceipt, companyName, ownerName, createdAt, updatedAt.

---

**business:** Get product list for this sales order
**process:** Query `sales` where `deleted=false` and `sales_number`=[SO number]. Unwind `items`. Return: product_name, quantity, unit_price, discount, discount_type, total_price for each line item.

---

**business:** Get price summary for each product under this order
**process:** Query `sales` where `deleted=false` and `sales_number`=[SO number]. Unwind `items`. Group by product (`items.product` or `items.product_name`): totalQty=$sum quantity, avgUnitPrice=$avg unit_price, totalPrice=$sum total_price. Return product-wise summary sorted by totalPrice desc.

---

**business:** Get project type breakdown for this order
**process:** Query `sales` where `deleted=false` and `sales_number`=[SO number]. Unwind `items`. Lookup `projecttypes` on `items.project_type`. Group by project type: lineItemCount=$sum 1, totalQty=$sum quantity, totalValue=$sum total_price. Return: projectTypeName, lineItemCount, totalQty, totalValue.

---

**business:** Get invoice amount linked to this order
**process:** Find sales `_id` from `sales` using `sales_number`. Query `invoices` where `deleted=false` and `so_number`=salesId. Return: invoice_number, grand_total, grandtotal_in_usd, currency, payment_status.

---

**business:** Get invoice issue date for this order
**process:** Find sales `_id` from `sales` using `sales_number`. Query `invoices` where `deleted=false` and `so_number`=salesId. Return: invoice_number, invoice_date, payment_status.

---

**business:** Get invoice due date for this order
**process:** Find sales `_id` from `sales` using `sales_number`. Query `invoices` where `deleted=false` and `so_number`=salesId. Return: invoice_number, due_date, payment_status.

---

**business:** Get invoice status for this order
**process:** Find sales `_id` from `sales` using `sales_number`. Query `invoices` where `deleted=false` and `so_number`=salesId. Return: invoice_number, payment_status, approval_status, invoice_date, due_date.

---

**business:** Get account details for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name] (case-insensitive regex). Lookup `users` on `companyOwner` and `sources` on `source`. Return: companyName, ownerName, email, phoneNumber, industry, type, country, city, sourceName, lifecycleStage, leadStatus, createdAt.

---

**business:** Get account owner for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Lookup `users` on `companyOwner`. Return: companyName, ownerName, ownerEmail.

---

**business:** Get phone number for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, phoneNumber.

---

**business:** Get email for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, email.

---

**business:** Get account type for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, type.

---

**business:** Get country for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, country, city, stateRegion.

---

**business:** Get source for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Lookup `sources` on `source`. Return: companyName, sourceName.

---

**business:** Get creation date for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, createdAt.

---

**business:** Get industry for Account Name
**process:** Query `companies` where `deleted=false` and `companyName` matches [Account Name]. Return: companyName, industry.

---

**business:** Get notes for Account Name
**process:** Find company `_id` from `companies`. Query both `companynotes` and `commonnotes` (`companyId`=companyId, type='Company'). Lookup `users` on `createdBy`. Merge and sort by createdAt desc. Return: note/title, authorName, isPinned, createdAt.

---

**business:** Get deals with stage Stage Name
**process:** Query `deals` where `deleted=false` and `stage` matches [Stage Name] case-insensitively. Lookup `companies` on `company` and `users` on `owner`. Return: deal name, stage, grand_total_in_usd, closeDate, companyName, ownerName.

---

**business:** Get deals with close date before Date
**process:** Query `deals` where `deleted=false` and `closeDate` < [Date]. Lookup `companies` and `users`. Return: name, stage, closeDate, grand_total_in_usd, companyName, ownerName.

---

**business:** Get deals with close date after Date
**process:** Query `deals` where `deleted=false` and `closeDate` > [Date]. Lookup `companies` and `users`. Return: name, stage, closeDate, grand_total_in_usd, companyName, ownerName.

---

**business:** Show invoices overdue
**process:** Query `invoices` where `deleted=false`, `payment_status` IN ['draft','confirmed','partial_payment'], and `due_date` < today. Compute daysOverdue=today minus due_date. Lookup `companies` for companyName. Return: invoice_number, payment_status, due_date, daysOverdue, grandtotal_in_usd, companyName.

---

**business:** Show invoices due this week
**process:** Calculate current week range (Monday 00:00:00 to Sunday 23:59:59). Query `invoices` where `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], and `due_date` between startOfWeek and endOfWeek. Lookup `companies` for companyName. Return invoice list sorted by due_date asc.

---

**business:** Show invoices due next month
**process:** Calculate next month boundaries. Query `invoices` where `deleted=false`, `payment_status` NOT IN ['paid','cancelled'], and `due_date` in next month range. Lookup `companies` for companyName. Return invoice list sorted by due_date asc.

---

**business:** Show invoices pending payment this week
**process:** Query `invoices` where `deleted=false`, `payment_status` IN ['draft','confirmed','partial_payment'], and `due_date` <= endOfCurrentWeek. Add `isOverdue` flag (`due_date` < now). Lookup `companies` for companyName. Return pending invoice list.

---

**business:** Get name of contact for Account Name
**process:** Find company `_id` by account name in `companies`. Query `contacts` where `deleted=false` and `company`=companyId. Return: firstName, lastName, isPrimary. Sort primary first.

---

**business:** Get email of contact for Account Name
**process:** Find company `_id` by account name. Query `contacts` where `deleted=false` and `company`=companyId. Return: firstName, lastName, email, isPrimary.

---

**business:** Get owner of contact for Account Name
**process:** Find company `_id` by account name. Query `contacts` where `deleted=false` and `company`=companyId. Lookup `users` on `contactOwner`. Return: contactName, contactOwnerName, contactOwnerEmail.

---

**business:** Get company of contact for Account Name
**process:** Find company by account name, then query `contacts` where `deleted=false` and `company`=companyId. Return each contact with companyName, email, phoneNumber, isPrimary.

---

**business:** Get unique touches and total touches for this dataset
**process:** Query `outreaches` where `isDeleted=false` and `sourceFile`=[dataset]. Get distinct `region` IDs. Query `outreachactivities` where `regionId` IN regionIds. Compute totalTouches=$sum count and uniqueTouches=count distinct `ActivityId`. Return: dataset, totalTouches, uniqueTouches.

---

**business:** Get confirmed sales orders
**process:** Query `sales` where `deleted=false` and `status` matches /^confirmed$/i. Lookup `companies` and `users` on `salesOwner`. Return: sales_number, status, grand_total, sales_date, companyName, ownerName.

---

**business:** Get cancelled sales orders
**process:** Query `sales` where `deleted=false` and `status` matches /^cancelled$/i. Lookup `companies` and `users` on `salesOwner`. Return: sales_number, status, grand_total, sales_date, companyName, ownerName.

---

**business:** Get draft sales orders
**process:** Query `sales` where `deleted=false` and `status` matches /^draft$/i. Lookup `companies` and `users` on `salesOwner`. Return: sales_number, status, grand_total, sales_date, companyName, ownerName.

---

**business:** Get total deals value for Account Name
**process:** Find company `_id` by account name. Query `deals` where `deleted=false` and `company`=companyId. Aggregate: totalDeals=$sum 1, totalDealValueUSD=$sum grand_total_in_usd, totalDealValueNative=$sum grand_total.

---

**business:** Get total invoices amount for Account Name
**process:** Find company `_id` by account name. Query `invoices` where `deleted=false` and `company`=companyId. Aggregate: invoiceCount=$sum 1, totalInvoiceAmountUSD=$sum grandtotal_in_usd, totalInvoiceAmountNative=$sum grand_total, paidInvoiceAmountUSD=$sum grandtotal_in_usd where payment_status='paid'.

---

**business:** Get sales date for Account Name
**process:** Find company `_id` by account name. Query `sales` where `deleted=false` and `company`=companyId. Return: sales_number, sales_date, status, grand_total. Sort by sales_date desc.

---

**business:** Get sales status for Account Name
**process:** Find company `_id` by account name. Query `sales` where `deleted=false` and `company`=companyId. Group by `status`: count=$sum 1, totalValue=$sum grand_total. Return status-wise summary.

---

## GLOBAL RULES FOR LLM

```
RULE 01 — Revenue: ALWAYS use `invoices` collection. NEVER use `sales` for revenue.
RULE 02 — Paid revenue: filter payment_status='paid', use grandtotal_in_usd for USD amounts.
RULE 03 — Soft delete: deleted=false for (deals, invoices, sales, companies, contacts, createtasks). isDeleted=false for outreaches.
RULE 04 — Open deals: dealWonAt=null AND dealLostAt=null.
RULE 05 — Won deals: dealWonAt is NOT null.
RULE 06 — Lost deals: dealLostAt is NOT null.
RULE 07 — Deal number format: ELS + sequence_number padded to 3 digits (e.g., ELS007).
RULE 08 — Targets month is 0-indexed: Jan=0, Feb=1, Mar=2, ... Dec=11.
RULE 09 — Achieved (targets): sum grandtotal_in_usd from invoices where sales_person=userId AND payment_status='paid' for that month/year.
RULE 10 — Remaining invoice amount: grandtotal_in_usd minus sum of payment_history[].payment_amount_in_usd.
RULE 11 — "Customer since" = companies.leadWonAt field.
RULE 12 — FY2025 (Indian): Apr 1, 2024 to Mar 31, 2025. Default to Indian FY (this is an India-based CRM with GST support).
RULE 13 — "Intrested" is stored with that spelling — NOT "Interested".
RULE 14 — payment_status enum: "draft" | "paid" | "confirmed" | "cancelled" | "partial_payment".
RULE 15 — Outreach status enum: 'Unassigned' | 'Not Contacted' | 'Contacted' | 'Followup' | 'Converted to Deal'.
RULE 16 — Outreach leadStatus enum: '-' | 'Nurturing' | 'Lost' | 'Intrested' | 'Mislabeled' | 'Converted to Deal' | 'To Be Verified' | 'Verified'.
RULE 17 — company.type enum: 'Prospect' | 'Partner' | 'Reseller' | 'Vendor' | 'Other'.
RULE 18 — createtasks.status enum: 'Pending' | 'Completed'. category enum: 'Open' | 'Close'. priority enum: 'Low' | 'Medium' | 'High'.
RULE 19 — "This week" = Monday 00:00:00 to Sunday 23:59:59 of current week.
RULE 20 — "Last month" = invoice_date between 1st and last day of previous calendar month.
RULE 21 — deals and sales are NOT directly linked. They share a company. Join via company field.
RULE 22 — Each invoice links to exactly ONE sales order via the so_number field (ObjectId ref to Sales).
RULE 23 — Task owner/assignee = createtasks.createdBy field (not a separate assignee field).
RULE 24 — "Stuck deal" = open deal where updatedAt < (today minus 7 days) in a named stage.
RULE 25 — Outreach "dataset" = sourceFile field (stores CSV file name).
RULE 26 — annualRevenue in companies is stored as String — may need type conversion for numeric comparisons.
RULE 27 — Meetings attendees[] is an array of email strings, NOT ObjectId references.
RULE 28 — Score = (achievedUSD / targetInUSD) * 100. Growth Potential = targetInUSD minus achievedUSD.
```
