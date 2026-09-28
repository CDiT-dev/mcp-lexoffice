## ADDED Requirements

### Requirement: Create draft quotation
The server SHALL provide a `create_draft_quotation` tool with the same named-parameter pattern as `create_draft_invoice`.

#### Scenario: Create quotation with named params
- **WHEN** `create_draft_quotation` is called with `recipient_name`, `line_items`, and optional `expiration_date`, `title`, `introduction`, `remark`
- **THEN** the tool creates a draft quotation in Lexoffice with `taxConditions.taxType: "vatfree"` and returns the quotation ID and deep link

### Requirement: Finalize quotation
The server SHALL provide a `finalize_quotation` tool that finalizes a draft quotation.

#### Scenario: Finalize quotation
- **WHEN** `finalize_quotation` is called with `quotation_id`
- **THEN** the tool finalizes the quotation (assigns Angebotsnummer) and returns the number and deep link

### Requirement: Pursue quotation to invoice
The server SHALL provide a `pursue_quotation_to_invoice` tool that converts an accepted quotation into a draft invoice.

#### Scenario: Convert quotation to invoice
- **WHEN** `pursue_quotation_to_invoice` is called with `quotation_id`
- **THEN** the tool creates a new draft invoice with the quotation's line items, recipient, and terms pre-filled, and returns the new invoice ID and deep link

#### Scenario: Pursue non-finalized quotation
- **WHEN** `pursue_quotation_to_invoice` is called with a quotation that is still in draft
- **THEN** the tool returns an error indicating the quotation must be finalized first
