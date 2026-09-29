## ADDED Requirements

### Requirement: List invoices with filters
The server SHALL provide a `list_invoices` tool that queries sales invoices with status, date, and contact filters.

#### Scenario: List all open invoices
- **WHEN** `list_invoices` is called with `status="open"`
- **THEN** the tool returns all open sales invoices with voucher number, contact name, total amount, due date, and deep link

#### Scenario: List invoices by date range
- **WHEN** `list_invoices` is called with `date_from` and `date_to` (ISO dates)
- **THEN** the tool returns invoices within that date range

#### Scenario: List overdue invoices
- **WHEN** `list_invoices` is called with `status="overdue"`
- **THEN** the tool returns invoices past their due date with days overdue calculated

### Requirement: List expenses
The server SHALL provide a `list_expenses` tool that queries purchase invoices and receipts.

#### Scenario: List all expenses
- **WHEN** `list_expenses` is called with optional `status` filter
- **THEN** the tool returns purchase invoices/vouchers with vendor name, amount, date, and status

### Requirement: Get financial overview
The server SHALL provide a `get_financial_overview` tool that computes revenue, expenses, and net by month.

#### Scenario: Monthly financial overview
- **WHEN** `get_financial_overview` is called with `months` (default 6)
- **THEN** the tool queries voucherlist for salesinvoices and purchaseinvoices, groups by month, and returns revenue, expenses, and net per month

#### Scenario: Current month summary
- **WHEN** `get_financial_overview` is called with `months=1`
- **THEN** the tool returns the current month's revenue, expenses, net, and count of open/overdue invoices

### Requirement: Get payment status
The server SHALL provide a `get_payment_status` tool that checks payment status for a specific invoice.

#### Scenario: Check payment by invoice ID
- **WHEN** `get_payment_status` is called with `invoice_id`
- **THEN** the tool returns payment status (open, partial, paid, overdue), paid amount, open amount, payment date, and due date

#### Scenario: Check payment by contact name
- **WHEN** `get_payment_status` is called with `contact_name` instead of `invoice_id`
- **THEN** the tool searches the voucherlist for open invoices matching that contact and returns payment status for each
