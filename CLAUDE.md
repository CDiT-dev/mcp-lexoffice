# mcp-lexoffice

MCP server for **Lexware Office** (formerly Lexoffice) — Python + FastMCP 3.

## Stack
- Python 3.11+, FastMCP `>=3.4.2,<4.0.0`, httpx, python-dotenv
- Lexware Office REST API (`https://api.lexoffice.io/v1`)
- Transport: streamable-http with `json_response=True` (port 8000)
- Auth: Bearer token from env/.env (with `op://` 1Password fallback)

## Running
```bash
source .venv/bin/activate
python -m mcp_lexoffice.server
# or with 1Password:
LEXOFFICE_API_KEY='op://Vault/item-id/API key' python -m mcp_lexoffice.server
```

## Deployment
- Docker container (see `docker-compose.yml`)
- Reverse proxy (Caddy/nginx) recommended for HTTPS
- Configurable host port via `HOST_PORT` env var

## Tax Configuration
- Auto-detected from the Lexoffice profile API (`GET /v1/profile` → `taxType`)
- Supported regimes: `vatfree` (Kleinunternehmerregelung, 0%), `net` (19%), `gross` (19%)
- Override with `LEXOFFICE_TAX_TYPE` env var for testing (skips API call)
- Lazy-cached in `lifespan_context` — server restart clears cache
- Per-item `tax_rate` override available on invoices, quotations, and articles
- Default payment terms: "Zahlbar sofort, rein netto"

## Tools (41 total)
- **Invoices**: create_draft_invoice, create_and_send_invoice, finalize_invoice, send_invoice, get_invoice, get_invoice_pdf, list_invoices, delete_draft_invoice
- **Financial**: list_expenses, get_financial_overview, get_payment_status
- **Contacts**: search_contacts, get_contact, create_contact, update_contact, find_or_create_contact, get_contact_invoices
- **Quotations**: create_draft_quotation, finalize_quotation, pursue_quotation_to_invoice, convert_quotation_and_send, list_quotations
- **Recurring**: list_recurring_templates, get_recurring_template
- **Credit Notes**: create_credit_note
- **Dunnings**: create_dunning, render_dunning_pdf
- **Articles**: list_articles, create_article, get_article, update_article
- **Vouchers**: upload_voucher (raw file → Beleg-Eingang), create_voucher (structured purchaseinvoice w/ amount+vendor, optional PDF attach + read-back), attach_voucher_file, get_voucher, update_voucher, list_vouchers, list_posting_categories
- **Other**: get_profile, list_payment_conditions, list_countries

All 41 tools carry MCP annotations (`readOnlyHint` on reads, `destructiveHint` on
finalize/send/delete, `idempotentHint`, `openWorldHint`, human `title`) and first-class
`tags` (e.g. `finance`, `invoice`, `irreversible`, `belegfaenger`). `get_financial_overview`
returns a typed Pydantic model (`FinancialOverview`) so fastmcp advertises an output schema;
it also sets a `truncated` flag when an underlying voucher page hits the 250-row cap.

## Resources (`lexoffice://`)
Reference/context data exposed as resources so the model can pull it without a tool call:
- `lexoffice://service-catalog` — standard offerings + pricing (static)
- `lexoffice://countries`, `lexoffice://posting-categories`, `lexoffice://payment-conditions` — live API reference data
- `lexoffice://tax-config` — auto-detected tax regime + default VAT rate
- `lexoffice://status` — service name, version, uptime (mirrors `/health`)

## Prompts (guided workflows)
- `monthly_close` — overview → overdue invoices → suggested dunnings
- `dunning_run` — find overdue open invoices and walk creating Mahnungen
- `capture_receipt` — Belegfänger flow: find_or_create_contact → create_voucher (+ optional PDF)

## API Notes
- Rate limit: 2 requests/second (HTTP 429 on exceed, auto-retry with Retry-After)
- All mutations use optimistic locking via `version` field
- Invoice statuses: draft → open (finalized) → paidoff / voided
- All tools return deep links to Lexoffice UI
- Base URL migrating from lexoffice.io to lexware.io (both work currently)
- **Structured vouchers** (`POST /v1/vouchers`): each voucherItem needs a `categoryId` (Buchungskonto — discover via `list_posting_categories`; `create_voucher` auto-resolves a default `outgo` category if none given). Lexoffice **rejects `taxType=net` + `voucherStatus=unchecked`** — use `gross` to land a voucher in "Zu prüfen", or `open` for net. Only `open` and `unchecked` statuses are writeable. File attach is `POST /v1/vouchers/{id}/files` (multipart field `file`).

## Testing
```bash
source .venv/bin/activate
pip install -e ".[test]"
python -m pytest tests/ -v  # 248 tests
```
