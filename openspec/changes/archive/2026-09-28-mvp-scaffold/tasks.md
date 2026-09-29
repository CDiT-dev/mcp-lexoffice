## 1. Infrastructure & Transport (server-transport)

- [x] 1.1 Upgrade `pyproject.toml` to `fastmcp>=3.0.0`, add `python-dotenv`
- [x] 1.2 Add `.env` file with `LEXOFFICE_API_KEY` placeholder and `.env.example`
- [x] 1.3 Update `client.py` — add dotenv loading, keep `op://` fallback, add 429 retry-after handling
- [x] 1.4 Update `server.py` — FastMCP 3 constructor with `json_response=True`, streamable-http transport on port 8000
- [x] 1.5 Verify server starts and responds to HTTP health check

## 2. Invoice Lifecycle (MVP — CDI-672)

- [x] 2.1 Add `create_draft_invoice` tool — named params (recipient_name, line_items, currency, payment_term_duration, title, introduction, remark), vatfree defaults, returns ID + deep link
- [x] 2.2 Add `finalize_invoice` tool — takes invoice_id, returns invoice number + deep link
- [x] 2.3 Add `send_invoice` tool — takes invoice_id + recipient_email, validates invoice is finalized first
- [x] 2.4 Reshape existing `get_invoice` to include deep link in response
- [x] 2.5 Reshape existing `get_invoice_pdf_link` to `get_invoice_pdf`
- [x] 2.6 Add client methods: `finalize_invoice()`, `send_invoice()`

## 3. Voucher Ingestion (MVP — CDI-673)

- [x] 3.1 Add `upload_voucher` client method — `POST /v1/files` with `type=voucher`, accepts bytes
- [x] 3.2 Add `upload_voucher` tool — accepts base64 file_content, file_name, voucher_type, validates size (<5MB) and type (PDF/PNG/JPG)

## 4. Financial Queries (Phase 2 — CDI-678, CDI-679)

- [x] 4.1 Reshape existing `list_invoices` tool — add date_from/date_to params, compute days_overdue for overdue items, include deep links
- [x] 4.2 Add `list_expenses` tool — query purchaseinvoice voucherlist with status filter
- [x] 4.3 Add `get_financial_overview` tool — query sales + purchase vouchers, group by month, compute revenue/expenses/net
- [x] 4.4 Add `get_payment_status` tool — GET /v1/payments/{id}, return status/amounts/dates
- [x] 4.5 Add contact-name-based payment lookup — search voucherlist by contact, then check payment for each match

## 5. Quotation Lifecycle (Phase 2 — CDI-681)

- [x] 5.1 Add `create_draft_quotation` tool — same named-param pattern as invoice, with expiration_date
- [x] 5.2 Add `finalize_quotation` tool — assigns Angebotsnummer, returns number + deep link
- [x] 5.3 Add `pursue_quotation_to_invoice` tool — converts finalized quotation to draft invoice, returns new invoice ID + deep link
- [x] 5.4 Add client methods: `finalize_quotation()`, `pursue_quotation()`

## 6. Dunning (Phase 2 — CDI-682)

- [x] 6.1 Add `create_dunning` client method — `POST /v1/dunnings`
- [x] 6.2 Add `render_dunning_pdf` client method — `POST /v1/dunnings/{id}/document`
- [x] 6.3 Add `create_dunning` tool — takes invoice_id + optional note
- [x] 6.4 Add `render_dunning_pdf` tool — takes dunning_id, returns document file ID

## 7. Article Catalog (Phase 2 — CDI-683)

- [x] 7.1 Add article CRUD client methods — `POST/GET/PUT /v1/articles`
- [x] 7.2 Add `list_articles` tool
- [x] 7.3 Add `create_article` tool — name, type, net_price, unit_name, description; vatfree default
- [x] 7.4 Add `get_article` tool
- [x] 7.5 Add `update_article` tool — with version param for optimistic locking
- [x] 7.6 Document CDiT service catalog in server instructions

## 8. Contacts Reshape (Phase 2 — CDI-680)

- [x] 8.1 Reshape existing `search_contacts` — add role filter (customer/vendor/both)
- [x] 8.2 Reshape existing `create_contact` — named params instead of raw JSON (company_name or person first/last, role, address, email)
- [x] 8.3 Reshape existing `update_contact` — named params with version for optimistic locking
- [x] 8.4 Add deep links to contact tool responses

## 9. Testing & Validation

- [x] 9.1 Unit tests for new client methods (respx mocks for each endpoint)
- [x] 9.2 Unit tests for tool parameter validation (file size, type, status checks)
- [ ] 9.3 E2E test script: create draft → finalize → verify via get_invoice
- [ ] 9.4 Register as Claude.ai custom connector (CDI-674)
- [ ] 9.5 E2E test via Claude.ai: invoice lifecycle (CDI-675)
- [ ] 9.6 E2E test via Claude.ai: Gmail bill → upload_voucher (CDI-675)
