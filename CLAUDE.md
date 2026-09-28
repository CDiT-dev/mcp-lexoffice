# mcp-lexoffice

MCP server for **Lexware Office** (formerly Lexoffice) — Python + FastMCP 4.

## Stack
- Python 3.11+, FastMCP `>=4.0.10,<5.0.0`, httpx, pydantic-settings (`config.py`)
- Lexware Office REST API (`https://api.lexoffice.io/v1`), token `LEXOFFICE_API_KEY`
- Transport: streamable-http, `json_response=True`, `stateless_http=True` (port 8000)
- Auth: static bearer `MCP_API_KEY` (`auth.py::BearerTokenVerifier`), the only auth path. No Keycloak/OAuth, no `op://` resolution; stale references to either are wrong.

## Running
```bash
source .venv/bin/activate
python -m mcp_lexoffice.server
# or with 1Password:
LEXOFFICE_API_KEY='op://Vault/item-id/API key' python -m mcp_lexoffice.server
```

## Deployment
- Komodo stack `git-mcp-lexoffice` builds from source (`docker-compose.yml`: `build: .`, `network_mode: host`, host port `HOST_PORT`, default 8001). A merge to `main` is the deploy.
- Release is tag-only: `release.yml` tests, runs pip-audit, tags the next patch and pushes a GHCR image built with `APP_VERSION`. The stack does not use that image and passes no `APP_VERSION`, so production `/health` reports the static `pyproject.toml` version, which lags the tags.
- Clients reach it through the Cloudflare MCP portal, which holds `MCP_API_KEY`.

## fastmcp 4 idioms
- Fleet conventions (tag-only releases, bearer `MCP_API_KEY`, usage telemetry, long-job pattern): `CDiT-infrastructure/docs/wiki/topics/mcp-fleet.md`.
- Tests: the `mcp-testing` skill (in-memory `Client(mcp)`, protocol surface in `tests/test_mcp_protocol.py`).
- Release/deploy workflow changes: the `cdit-release-pipeline` skill.
- `main` is protected: branch, PR, the `test` check must pass.
- The portal forwards no elicitation or other server-to-client requests; never block a tool on one. New tools/params need a portal catalog refresh.
- `usage.py` is vendored unchanged into every fleet server; do not fork it here.

## Tax Configuration
- Auto-detected from the Lexoffice profile API (`GET /v1/profile` → `taxType`)
- Supported regimes: `vatfree` (Kleinunternehmerregelung, 0%), `net` (19%), `gross` (19%)
- Override with `LEXOFFICE_TAX_TYPE` env var for testing (skips API call)
- Lazy-cached in `lifespan_context` — server restart clears cache
- Per-item `tax_rate` override available on invoices, quotations, and articles
- Default payment terms: "Zahlbar sofort, rein netto"

## Tools (36 total)
- **Invoices**: create_draft_invoice (finalize=true to finalize on create), get_invoice, get_invoice_pdf, list_invoices, delete_draft_invoice
- **Financial**: list_expenses, get_financial_overview, get_payment_status
- **Contacts**: search_contacts, get_contact, create_contact, update_contact, find_or_create_contact, get_contact_invoices
- **Quotations**: create_draft_quotation (finalize=true), pursue_quotation_to_invoice, list_quotations
- **Recurring**: list_recurring_templates, get_recurring_template
- **Credit Notes**: create_credit_note
- **Dunnings**: create_dunning, render_dunning_pdf
- **Articles**: list_articles, create_article, get_article, update_article
- **Vouchers**: upload_voucher (raw file → Beleg-Eingang), create_voucher (structured purchaseinvoice w/ amount+vendor, optional PDF attach + read-back), attach_voucher_file, get_voucher, update_voucher, list_vouchers, list_posting_categories
- **Other**: get_profile, list_payment_conditions, list_countries

All 36 tools carry MCP annotations (`readOnlyHint` on reads, `destructiveHint` on
finalize-capable creates/delete, `idempotentHint`, `openWorldHint`, human `title`) and first-class
`tags` (e.g. `finance`, `invoice`, `irreversible`, `belegfaenger`).

### Typed output schemas
31 of the 36 tools return typed Pydantic models, so fastmcp advertises a per-tool
`output_schema` and emits machine-validated structured content alongside the human-readable
JSON. Reusable domain models live in `server.py`: `Profile`, `Invoice`, `VoucherList`
(+`VoucherListEntry`), `Contact`/`ContactList`, `Quotation`, `Article`/`ArticleList`,
`Voucher`, `CreateVoucherResult`, `CreditNote`, `Dunning`, `RecurringTemplate`, `DocumentRef`,
`FileRef`, `SendResult`, `DeleteResult`, `SentInvoiceResult`, plus the pass-1
`FinancialOverview`. Each is reused across sibling get/list/create/finalize tools.

**Backward-compat contract** (a manually-synced Cloudflare portal + live clients depend on it):
every model subclasses `LexofficeBase` with `ConfigDict(populate_by_name=True, extra="allow")`
and an optional `error: str | None`. Declared fields use the EXACT current camelCase wire keys
(via field name or `serialization_alias`), `extra="allow"` carries the rest of the Lexoffice
object graph (and conditionally-injected keys like `daysOverdue` / `_note` / `_enrichment` /
`_action`) verbatim, and every model validates BOTH the success and the `{"error": ...}`
short-circuit payload — so the structured schema never explodes on the error path.

The 5 tools still returning a raw JSON string do so deliberately: `list_countries`,
`list_posting_categories`, `list_payment_conditions`, `list_recurring_templates` return bare
JSON arrays (a typed `list[Model]` return makes fastmcp wrap them under a `result` key, which
would change the wire shape), and `get_payment_status` is polymorphic (dict | list | error).

`get_financial_overview` additionally sets a `truncated` flag when an underlying voucher page
hits the 250-row cap.

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
uv sync
uv run pytest tests/ -v
uv run ruff check .
```
