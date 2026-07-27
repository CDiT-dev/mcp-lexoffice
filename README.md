# mcp-lexoffice

MCP server for **Lexware Office** (formerly Lexoffice) — a Python-based accounting integration that exposes 41 tools for invoices, contacts, quotations, dunnings, articles, recurring templates, credit notes, financial queries, and voucher management.

Built with [FastMCP 3](https://github.com/jlowin/fastmcp) and designed to work as a **Claude.ai custom connector**.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/CDiT-dev/mcp-lexoffice.git
cd mcp-lexoffice
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env
# Edit .env and add your Lexoffice API key

# Run
python -m mcp_lexoffice.server
```

The server starts on `http://0.0.0.0:8000` with streamable-http transport.

## Authentication

The server uses a **Bearer token** from the Lexware Office API. Three ways to provide it:

### 1. Environment variable (recommended for production)
```bash
export LEXOFFICE_API_KEY=your-api-key-here
python -m mcp_lexoffice.server
```

### 2. `.env` file (recommended for local development)
```bash
# .env
LEXOFFICE_API_KEY=your-api-key-here
```
The server loads `.env` automatically via `python-dotenv`.

### 3. 1Password CLI reference (for secure local development)
```bash
# .env or environment
LEXOFFICE_API_KEY=op://Vault/lexoffice/API key
```
If the value starts with `op://`, the server resolves it via `op read` at startup. Requires the [1Password CLI](https://developer.1password.com/docs/cli/) to be installed and authenticated.

### Getting an API key

1. Log in to [app.lexoffice.de](https://app.lexoffice.de)
2. Go to **Settings** (Einstellungen) > **API**
3. Generate a new API key
4. The key has full access to your Lexoffice account — treat it as a secret

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LEXOFFICE_API_KEY` | *(required)* | Lexoffice API Bearer token (or `op://` reference) |
| `LEXOFFICE_TAX_TYPE` | *(auto-detect)* | Override tax regime: `vatfree`, `net`, or `gross` |
| `MCP_TRANSPORT` | `streamable-http` | Transport: `streamable-http`, `sse`, or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Port number |

## Running

### Local development (stdio for Claude Code)
```bash
source .venv/bin/activate
MCP_TRANSPORT=stdio python -m mcp_lexoffice.server
```

### HTTP server (for Claude.ai connector)
```bash
source .venv/bin/activate
python -m mcp_lexoffice.server
# Listens on http://0.0.0.0:8000/mcp
```

### Docker
```bash
docker compose up --build
```

The `docker-compose.yml` supports a `HOST_PORT` variable to remap the host port:
```bash
HOST_PORT=8001 docker compose up --build
```

### Docker with Komodo (git deploy)

The server is designed for deployment via [Komodo](https://komo.do) with git-based stacks:

1. Create a Komodo Repo pointing to the GitHub repository
2. Create a Komodo Stack on your target server with `run_build=true`
3. Set `LEXOFFICE_API_KEY` in the stack's environment
4. Deploy — Komodo clones, builds the Docker image, and runs it

## Claude.ai Connector Setup

1. Deploy the server behind HTTPS (e.g., via Caddy reverse proxy)
2. Open [claude.ai](https://claude.ai) > Settings > Connectors
3. Click **Add custom connector**
4. Name: `Lexoffice`
5. URL: `https://your-domain.example.com/mcp`
6. No OAuth needed (authless connector)
7. Verify: open a new chat, click "+" > Connectors > enable Lexoffice

### Caddy configuration example

```
mcp-lexoffice.example.com {
    reverse_proxy your-server-ip:8000 {
        flush_interval -1
    }
}
```

## Tools Reference

### Invoices (Invoice Lifecycle)

| Tool | Description |
|------|-------------|
| `create_draft_invoice` | Create a draft invoice with named parameters (recipient, line items, payment terms). Returns ID + Lexoffice deep link. |
| `create_and_send_invoice` | One-shot flow: create draft → finalize → send by email. **Irreversible.** |
| `finalize_invoice` | Finalize a draft — assigns invoice number, makes non-editable. **Cannot be undone.** |
| `send_invoice` | Send a finalized invoice by email. Validates status before sending. |
| `delete_draft_invoice` | Delete a draft invoice (drafts only; finalized invoices cannot be deleted). |
| `get_invoice` | Get full invoice details with deep link (edit link for drafts, view link for finalized). |
| `get_invoice_pdf` | Render and get the document file ID for a finalized invoice PDF. |
| `list_invoices` | List sales invoices with status filter. Computes `daysOverdue` for overdue items. |

**Invoice flow**: `create_draft_invoice` → review in Lexoffice UI → `finalize_invoice` → `send_invoice` (or `create_and_send_invoice` for the one-shot path)

### Financial Queries

| Tool | Description |
|------|-------------|
| `list_expenses` | List purchase invoices/expenses with status filter. |
| `get_financial_overview` | Monthly revenue/expense/net breakdown. Includes open and overdue invoice counts. |
| `get_payment_status` | Check payment status by invoice ID or contact name. |

### Contacts

| Tool | Description |
|------|-------------|
| `search_contacts` | Search by name, email, or role (customer/vendor). |
| `get_contact` | Get full contact details with deep link. |
| `create_contact` | Create a company or person contact with named parameters. |
| `update_contact` | Update contact fields with optimistic locking (version). |
| `find_or_create_contact` | Idempotent lookup-or-create by name/email — returns existing match or creates a new contact. |
| `get_contact_invoices` | List the invoices belonging to a given contact. |

### Quotations (Angebote)

| Tool | Description |
|------|-------------|
| `create_draft_quotation` | Create a draft quotation with the same interface as invoices. |
| `finalize_quotation` | Finalize a quotation — assigns Angebotsnummer. |
| `pursue_quotation_to_invoice` | Convert a finalized quotation to a draft invoice (Angebot → Rechnung). |
| `convert_quotation_and_send` | One-shot: convert a quotation to an invoice, finalize, and send. **Irreversible.** |
| `list_quotations` | List quotations with status filter. |

### Recurring Templates (Wiederkehrende Rechnungen)

| Tool | Description |
|------|-------------|
| `list_recurring_templates` | List configured recurring invoice templates. |
| `get_recurring_template` | Get a recurring template's details. |

### Credit Notes (Gutschriften)

| Tool | Description |
|------|-------------|
| `create_credit_note` | Create a credit note (Gutschrift), optionally linked to a contact. |

### Dunnings (Mahnungen)

| Tool | Description |
|------|-------------|
| `create_dunning` | Create a payment reminder for an overdue invoice. Recipient, positions and tax conditions are copied from the invoice, which is linked via `precedingSalesVoucherId`. Optional `note` / `title` / `remark`. |
| `render_dunning_pdf` | Render a dunning PDF and get the document file ID. |

The invoice must be finalized — dunning a draft is refused. A dunning for an invoice that is
already paid/voided or not yet due is still created, with a `warning` attached.

### Articles (Service Catalog)

| Tool | Description |
|------|-------------|
| `list_articles` | List all configured service articles. |
| `create_article` | Create a reusable service item (e.g., "Consulting", "Sprechstunde"). |
| `get_article` | Get article details. |
| `update_article` | Update article with optimistic locking. |

### Vouchers (Belege)

| Tool | Description |
|------|-------------|
| `upload_voucher` | Upload a raw bill/receipt file (PDF, PNG, JPG) to Lexoffice Beleg-Eingang ("Zu prüfen"). Max 5MB. |
| `create_voucher` | Create a structured purchase invoice (amount + vendor + category), optional PDF attach and read-back. |
| `attach_voucher_file` | Attach a file to an existing voucher (multipart upload). |
| `get_voucher` | Get a voucher's details. |
| `update_voucher` | Update a voucher with optimistic locking. |
| `list_vouchers` | Generic voucher list query by type and status. |
| `list_posting_categories` | List Buchungskonten (posting categories) for voucher categorization. |

### Utilities

| Tool | Description |
|------|-------------|
| `get_profile` | Get organization profile (company name, tax settings). |
| `list_payment_conditions` | List configured payment terms. |
| `list_countries` | List countries with tax classification. |

## Resources & Prompts

In addition to tools, the server exposes MCP **resources** (reference/context data the model can
pull without a tool call) under the `lexoffice://` scheme:

- `lexoffice://service-catalog` — standard offerings + pricing (static)
- `lexoffice://countries`, `lexoffice://posting-categories`, `lexoffice://payment-conditions` — live API reference data
- `lexoffice://tax-config` — auto-detected tax regime + default VAT rate
- `lexoffice://status` — service name, version, uptime (mirrors `/health`)
- `lexoffice://contacts/{contact_id}/invoices` — per-contact invoice list (resource template)

And guided-workflow **prompts**:

- `monthly_close` — overview → overdue invoices → suggested dunnings
- `dunning_run` — find overdue open invoices and walk creating Mahnungen
- `capture_receipt` — Belegfänger flow: `find_or_create_contact` → `create_voucher` (+ optional PDF)

## Tax Configuration

The tax regime is **auto-detected** from the Lexware Office profile API (`GET /v1/profile` → `taxType`):

| Regime | `taxType` | Default Rate |
|--------|-----------|-------------|
| Kleinunternehmerregelung | `vatfree` | 0% |
| Netto (regular VAT) | `net` | 19% |
| Brutto (gross VAT) | `gross` | 19% |

- The result is lazy-cached for the server's lifetime (restart to refresh)
- Override with `LEXOFFICE_TAX_TYPE` env var for testing: `LEXOFFICE_TAX_TYPE=net python -m mcp_lexoffice.server`
- Per-item `tax_rate` override is available on `create_draft_invoice`, `create_draft_quotation`, and `create_article`

## Rate Limiting

The Lexoffice API enforces a **2 requests/second** rate limit. The client handles this with:
- An asyncio semaphore limiting concurrent requests to 2
- Automatic retry on HTTP 429 responses, respecting the `Retry-After` header

## Testing

```bash
source .venv/bin/activate
pip install -e ".[test]"
python -m pytest tests/ -v
```

The test suite includes 297 tests covering:
- All 41 MCP tools with parameter variations
- Client HTTP methods with respx mocks
- 429 retry logic and rate limiting
- Error propagation (400, 401, 403, 404, 409, 422, 500)
- File upload validation (type, size)
- Helper functions (_build_line_items, _build_address, _deep_link)
- Multi-tax-regime detection, caching, env override, and per-item rates
- Overdue calculation edge cases

## Project Structure

```
mcp-lexoffice/
  mcp_lexoffice/
    __init__.py
    client.py          # Async HTTP client (httpx, rate limiting, 429 retry)
    server.py           # FastMCP 3 server with 41 tools
  tests/
    conftest.py         # Shared fixtures (respx mock, test client)
    test_client.py      # Client unit tests (80 tests)
    test_server.py      # Server/tool unit tests (217 tests)
  .env.example          # Environment variable template
  docker-compose.yml    # Docker Compose for deployment
  Dockerfile            # Python 3.13-slim with health check
  pyproject.toml        # Dependencies and project metadata
  CLAUDE.md             # Claude Code project instructions
```

## Dependencies

- **[FastMCP](https://github.com/jlowin/fastmcp)** >= 3.4.2, < 4.0.0 — MCP server framework
- **[httpx](https://www.python-httpx.org/)** >= 0.28.0 — Async HTTP client
- **[pydantic](https://docs.pydantic.dev/)** >= 2.10 / **pydantic-settings** >= 2.12 — typed models & settings
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** >= 1.0.0 — `.env` file loading

## License

AGPL-3.0 — see [LICENSE](LICENSE).

Commercial licensing available for enterprise and partner integrations — contact casey@caseydoes.it.
