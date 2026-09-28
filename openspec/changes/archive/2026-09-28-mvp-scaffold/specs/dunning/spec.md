## ADDED Requirements

### Requirement: Create dunning for overdue invoice
The server SHALL provide a `create_dunning` tool that creates a payment reminder (Mahnung) for an overdue invoice.

#### Scenario: Create dunning
- **WHEN** `create_dunning` is called with `invoice_id` and optional `note` (custom dunning text)
- **THEN** the tool creates a dunning attached to the invoice via `POST /v1/dunnings` and returns the dunning ID and deep link

#### Scenario: Create dunning for non-overdue invoice
- **WHEN** `create_dunning` is called for an invoice that is not overdue
- **THEN** the tool returns a warning but still creates the dunning (Lexoffice allows this)

### Requirement: Render dunning PDF
The server SHALL provide a `render_dunning_pdf` tool.

#### Scenario: Render dunning document
- **WHEN** `render_dunning_pdf` is called with `dunning_id`
- **THEN** the tool renders the dunning PDF via `POST /v1/dunnings/{id}/document` and returns the document file ID
