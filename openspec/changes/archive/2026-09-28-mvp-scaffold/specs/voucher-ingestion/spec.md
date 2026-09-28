## ADDED Requirements

### Requirement: Upload voucher file
The server SHALL provide an `upload_voucher` tool that uploads a bill/receipt file to Lexoffice as a voucher for review.

#### Scenario: Upload PDF bill
- **WHEN** `upload_voucher` is called with `file_content` (base64-encoded), `file_name`, and `voucher_type` (default "purchaseinvoice")
- **THEN** the tool uploads the file via `POST /v1/files` with `type=voucher` and returns the file ID and confirmation

#### Scenario: Upload with metadata
- **WHEN** `upload_voucher` is called with optional `contact_name` and `note`
- **THEN** the tool includes the metadata so the voucher appears in Lexoffice "Zu prüfen" with context

#### Scenario: File too large
- **WHEN** `upload_voucher` is called with a file larger than 5MB
- **THEN** the tool returns an error indicating the file exceeds the Lexoffice upload limit

#### Scenario: Unsupported file type
- **WHEN** `upload_voucher` is called with a file that is not PDF, PNG, or JPG
- **THEN** the tool returns an error listing the supported file types
