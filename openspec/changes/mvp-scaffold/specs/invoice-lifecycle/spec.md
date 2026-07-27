## ADDED Requirements

### Requirement: Create draft invoice with named parameters
The server SHALL provide a `create_draft_invoice` tool that accepts named parameters and creates a draft invoice in Lexoffice.

#### Scenario: Create draft invoice with minimal params
- **WHEN** `create_draft_invoice` is called with `recipient_name`, and `line_items` (list of name + quantity + unit_price)
- **THEN** the tool creates a draft invoice in Lexoffice with `taxConditions.taxType: "vatfree"`, returns the invoice ID, invoice number (if assigned), and a deep link to `https://app.lexoffice.de/#/voucher/edit/{id}`

#### Scenario: Create draft invoice with full params
- **WHEN** `create_draft_invoice` is called with `recipient_name`, `recipient_address` (street, zip, city, country_code), `recipient_email`, `line_items`, `currency`, `payment_condition_id`, `title`, `introduction`, `remark`
- **THEN** the tool embeds the payment condition matching `payment_condition_id` from `/v1/payment-conditions` (fields: `paymentTermLabel`, `paymentTermDuration`, optional `paymentDiscountConditions`) and creates a draft invoice with all provided fields populated

#### Scenario: Default payment condition resolution
- **WHEN** `create_draft_invoice` is called without `payment_condition_id`
- **THEN** the tool uses the entry marked `organizationDefault` from `/v1/payment-conditions`
- **AND IF** conditions exist but none is marked `organizationDefault`, the tool returns an error listing available IDs
- **AND IF** no conditions are configured, the `paymentConditions` field is omitted from the request
- **AND IF** `payment_condition_id` is provided but not found, the tool refreshes the cache once; if still missing, it returns an error listing available IDs

#### Scenario: Line item structure
- **WHEN** a line item is provided with `name`, `quantity`, `unit_price`, and optionally `description`, `unit_name`
- **THEN** the tool maps it to Lexoffice format with `type: "custom"`, `unitPrice.currency: "EUR"`, `unitPrice.netAmount: unit_price`, `taxRatePercentage: 0`

### Requirement: Finalize invoice
The server SHALL provide a `finalize_invoice` tool that finalizes a draft invoice (assigns invoice number, makes it non-editable).

#### Scenario: Finalize a draft invoice
- **WHEN** `finalize_invoice` is called with an `invoice_id`
- **THEN** the tool calls the Lexoffice finalize endpoint, and returns the assigned invoice number and a deep link to `https://app.lexoffice.de/#/voucher/view/{id}`

#### Scenario: Finalize an already-finalized invoice
- **WHEN** `finalize_invoice` is called with an invoice that is already finalized
- **THEN** the tool returns an error message indicating the invoice is already finalized

### Requirement: Send invoice by email
The server SHALL provide a `send_invoice` tool that sends a finalized invoice to a recipient email address.

#### Scenario: Send finalized invoice
- **WHEN** `send_invoice` is called with `invoice_id` and `recipient_email`
- **THEN** the tool sends the invoice PDF to the recipient via Lexoffice email delivery

#### Scenario: Send draft invoice
- **WHEN** `send_invoice` is called with an invoice that is still in draft status
- **THEN** the tool returns an error indicating the invoice must be finalized first

### Requirement: Get invoice with deep link
The server SHALL provide a `get_invoice` tool that returns full invoice details including a deep link.

#### Scenario: Get existing invoice
- **WHEN** `get_invoice` is called with an `invoice_id`
- **THEN** the tool returns the full invoice JSON with an added `deepLink` field pointing to the Lexoffice UI

### Requirement: Get invoice PDF
The server SHALL provide a `get_invoice_pdf` tool that renders and returns the document file ID for a finalized invoice.

#### Scenario: Get PDF for finalized invoice
- **WHEN** `get_invoice_pdf` is called with an `invoice_id` for a finalized invoice
- **THEN** the tool renders the document and returns the `documentFileId`
