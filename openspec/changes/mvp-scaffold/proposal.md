## Why

mcp-lexoffice exists as a skeleton (FastMCP 2, 1Password key resolution, 15 raw tools) but doesn't work as a Claude.ai connector yet and can't handle the two things Casey needs _today_: **sending invoices** and **ingesting bills from Gmail**. CDI-670 is In Progress with a due date of 2026-03-16. The server needs to upgrade to FastMCP 3 with streamable-http + `json_response=True` (required for Claude.ai connector init), switch to `.env`-based bearer token auth for production deployment behind Caddy, and reshape the tools from raw API wrappers into Claude-friendly flows (draft → review → finalize → send).

Beyond the immediate MVP, CDI-677 (Phase 2) scopes a full accounting assistant — voucherlist queries, payment status, quotations, dunnings, articles, and contacts. This proposal covers both phases in a single scaffold so implementation can be prioritized without re-architecting. Webhook/n8n integration is out of scope — a BFF endpoint will be offered later.

## What Changes

### Infrastructure & transport
- **BREAKING**: Upgrade from FastMCP 2 to FastMCP 3.x (`fastmcp>=3.0.0`)
- Switch transport to `streamable-http` with `json_response=True` for Claude.ai connector compatibility
- Replace 1Password CLI key resolution with `.env` file bearer token (loaded at server start)
- Add `dotenv` support for local development

### Phase 1 — MVP tools (CDI-670, due 2026-03-16)
- `create_draft_invoice` — simplified params (recipient_name, line_items, etc.), returns voucher ID + Lexoffice deep link (CDI-672)
- `finalize_invoice` — assigns invoice number, generates PDF
- `send_invoice` — sends finalized invoice to recipient email
- `upload_voucher` — accepts bill data/file from Gmail for Lexoffice Belegempfang (CDI-673)
- Reshape existing `list_invoices` for Claude-friendly output
- Register as Claude.ai custom connector (CDI-674)

### Phase 2 — accounting assistant tools (CDI-677)
- **Voucherlist queries** (CDI-678): `list_invoices` with status/date/contact filters, `list_expenses`, `get_financial_overview` (revenue/burn/net by month)
- **Payment status** (CDI-679): `get_payment_status` with contact name lookup
- **Contacts** (CDI-680): `search_contacts`, `get_contact`, `create_contact`, `update_contact` — already exist but need Claude-friendly reshaping
- **Quotations** (CDI-681): `create_draft_quotation`, `finalize_quotation`, `pursue_quotation_to_invoice` (Angebot → Rechnung pipeline)
- **Dunnings** (CDI-682): `create_dunning` for overdue invoice reminders
- **Articles** (CDI-683): CRUD for reusable service catalog (Sprechstunde €995, Consulting €150/h, Platform Dev €1200/d)
- ~~Event subscriptions~~ — deferred, BFF endpoint later

### Testing & validation
- E2E test: invoice create → finalize → send via Claude.ai (CDI-675)
- E2E test: Gmail bill → Lexoffice voucher upload via Claude.ai (CDI-675)
- Unit tests for client methods (respx mocks)

## Capabilities

### New Capabilities
- `server-transport`: FastMCP 3 streamable-http setup, json_response config, .env auth loading
- `invoice-lifecycle`: Draft → finalize → send → PDF flow with Claude-friendly tool interfaces
- `voucher-ingestion`: Bill/receipt upload to Lexoffice from external sources (Gmail)
- `financial-queries`: Voucherlist queries, payment status, revenue/burn overview
- `quotation-lifecycle`: Angebot create → finalize → pursue to Rechnung pipeline
- `dunning`: Payment reminder creation for overdue invoices
- `article-catalog`: Reusable service item CRUD (CDiT's standard offerings)
<!-- webhook-subscriptions: deferred — BFF endpoint later -->

### Modified Capabilities
<!-- No existing specs to modify — this is a greenfield openspec setup -->

## Impact

- **pyproject.toml**: `fastmcp>=3.0.0`, add `python-dotenv`
- **mcp_lexoffice/client.py**: Remove 1Password CLI resolution, load from env/dotenv. Add payment, dunning, article, file upload endpoints.
- **mcp_lexoffice/server.py**: Rewrite all tool decorators for FastMCP 3 API. Reshape tool signatures from raw JSON to named parameters. Add `json_response=True` to server config.
- **New**: `.env` file with `LEXOFFICE_API_KEY=...`
- **Deployment**: Docker container behind Caddy (CDI-525, already done)
- **Downstream consumers**: Besserwisser dashboard (CDI-544) will consume the same Lexoffice API — shared cache layer possible. MegaFön (CDI-354) will connect via a future BFF endpoint.
- **Linear issues**: CDI-670 (MVP), CDI-672, CDI-673, CDI-674, CDI-675, CDI-677, CDI-678–684, CDI-94, CDI-544, CDI-347
