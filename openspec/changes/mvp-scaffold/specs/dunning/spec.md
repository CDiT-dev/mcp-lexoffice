## ADDED Requirements

### Requirement: Create dunning for overdue invoice
The server SHALL provide a `create_dunning` tool that creates a payment reminder (Mahnung) for an overdue invoice.

#### Scenario: Create dunning
- **WHEN** `create_dunning` is called with `invoice_id` and optional `note` (custom dunning text), `title`, `remark`
- **THEN** the tool creates a dunning via `POST /v1/dunnings?precedingSalesVoucherId={invoice_id}` — the link to the chased invoice is a query parameter, NOT a body field — and returns the dunning ID, the `precedingSalesVoucherId` and a deep link

#### Scenario: Dunning body derived from the invoice
- **WHEN** `create_dunning` builds the request body
- **THEN** it reads the invoice via `GET /v1/invoices/{id}` and sends a complete sales voucher: `voucherDate`, `address` (only `contactId` for a contact-linked invoice), the invoice `lineItems` minus read-only fields, `totalPrice.currency`, the invoice `taxConditions` (falling back to the detected tax regime), `shippingConditions` when present, `title`, `introduction` (the `note`, else a default German dunning text) and `remark` when given

#### Scenario: Create dunning for draft invoice
- **WHEN** `create_dunning` is called for an invoice that is still a draft
- **THEN** the tool raises an error telling the caller to finalize the invoice first, and creates nothing

#### Scenario: Create dunning for non-overdue invoice
- **WHEN** `create_dunning` is called for an invoice that is not overdue, or is already paid off / voided
- **THEN** the tool returns a warning but still creates the dunning (Lexoffice allows this)

### Requirement: Render dunning PDF
The server SHALL provide a `render_dunning_pdf` tool.

#### Scenario: Render dunning document
- **WHEN** `render_dunning_pdf` is called with `dunning_id`
- **THEN** the tool renders the dunning PDF via `POST /v1/dunnings/{id}/document` and returns the document file ID
