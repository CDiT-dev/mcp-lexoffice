# mcp-lexoffice

An MCP server for **Lexware Office**, the German accounting service formerly called **Lexoffice**. It gives an MCP client (Claude, n8n, or any other MCP host) 36 tools for invoices, quotations, contacts, credit notes, dunnings, articles, recurring templates, vouchers (Belege) and financial queries, all through the official Lexware Office REST API. It is meant for freelancers and small businesses who already keep their books in Lexware Office and want an assistant to draft invoices, capture receipts and check who still owes them money.

Built with [FastMCP](https://github.com/PrefectHQ/fastmcp) 4 (`fastmcp>=4.0.10,<5.0.0`).

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A Lexware Office account with a public API key
- Docker with Compose, if you want to run it as a container

## Install and run

### Local

```bash
git clone https://github.com/CDiT-dev/mcp-lexoffice.git
cd mcp-lexoffice
uv sync
cp .env.example .env   # then fill in LEXOFFICE_API_KEY and MCP_API_KEY
```

stdio, for a local MCP client such as Claude Code or Claude Desktop:

```bash
MCP_TRANSPORT=stdio uv run mcp-lexoffice
```

Streamable HTTP, for a remote client:

```bash
MCP_TRANSPORT=streamable-http MCP_API_KEY=change-me uv run mcp-lexoffice
# listens on http://0.0.0.0:8000/mcp
```

`MCP_TRANSPORT` defaults to `stdio` when unset. The `.env.example` sets it to `streamable-http`, so with a copied `.env` the server starts in HTTP mode and needs `MCP_API_KEY`.

A client config for stdio:

```json
{
  "mcpServers": {
    "lexoffice": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-lexoffice", "run", "mcp-lexoffice"],
      "env": { "MCP_TRANSPORT": "stdio", "LEXOFFICE_API_KEY": "your-api-key" }
    }
  }
}
```

### Docker

```bash
LEXOFFICE_API_KEY=... MCP_API_KEY=... docker compose up --build
```

The Compose file builds the image from source, runs streamable HTTP, and uses host networking. The listening port comes from `HOST_PORT` (default `8001`), so the endpoint is `http://localhost:8001/mcp`. `GET /health` (and `/healthz`) returns status, version and uptime; the container health check uses it.

## Configuration

All configuration comes from environment variables or a `.env` file in the working directory.

| Variable | Default | Description |
|----------|---------|-------------|
| `LEXOFFICE_API_KEY` | *(required)* | Lexware Office API token. Startup fails without it. |
| `MCP_API_KEY` | *(empty)* | Bearer token that MCP clients must send. Required for HTTP transport. |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, or `http` (an alias for `streamable-http`). The Docker image sets `streamable-http`. |
| `MCP_HOST` | `0.0.0.0` | Bind address for HTTP transport. |
| `MCP_PORT` | `8000` | Port for HTTP transport. |
| `LEXOFFICE_TAX_TYPE` | *(auto-detect)* | Force the tax regime: `vatfree`, `net`, or `gross`. |
| `APP_VERSION` | *(package version)* | Version reported by `/health` and `lexoffice://status`. Set by the release build. |
| `HOST_PORT` | `8001` | Compose only: the port the container listens on. |

## Authentication

There are two separate credentials.

- **Upstream (Lexware Office).** `LEXOFFICE_API_KEY` is sent as a bearer token to the Lexware Office API. Create one in the Lexware Office web app ([app.lexoffice.de](https://app.lexoffice.de)) under Settings (Einstellungen), Public API. The key has full access to the account, so treat it as a secret.
- **Clients (this server).** In HTTP mode every request must carry `Authorization: Bearer <MCP_API_KEY>`. The token is compared in constant time. If `MCP_API_KEY` is empty in HTTP mode, the server exits at startup instead of running unauthenticated. There is no OAuth. In stdio mode the client launches the server as a local process and no bearer token is used.

For a public deployment, put the server behind a reverse proxy or MCP gateway that terminates TLS and holds the bearer token.

## Tools

Tool failures (bad input, unknown contact, unsupported file type) raise an MCP `ToolError`, so the client gets a clear error message. Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) and a human-readable title.

### Invoices

| Tool | Description |
|------|-------------|
| `create_draft_invoice` | Create an invoice from named parameters (recipient, line items, payment terms). Draft by default; `finalize=true` finalizes on create, which assigns the invoice number and cannot be undone. |
| `delete_draft_invoice` | Delete a draft invoice. Finalized invoices cannot be deleted. |
| `get_invoice` | Full invoice details with a deep link into Lexware Office. |
| `get_invoice_pdf` | Render a finalized invoice and return the document file ID. |
| `list_invoices` | List sales invoices by status; computes `daysOverdue` for overdue items. |

The Lexware Office API only allows finalizing at create time (`POST /invoices?finalize=true`). It has no endpoint to finalize an existing draft or to email an invoice, so this server has no finalize or send tool. Send invoices from the Lexware Office web app.

### Quotations (Angebote)

| Tool | Description |
|------|-------------|
| `create_draft_quotation` | Create a quotation with the same interface as invoices; `finalize=true` finalizes on create. |
| `pursue_quotation_to_invoice` | Turn a finalized quotation into a new draft invoice. |
| `list_quotations` | List quotations by status. |

### Contacts

| Tool | Description |
|------|-------------|
| `search_contacts` | Search by name, email, or role (customer or vendor). |
| `get_contact` | Full contact details with a deep link. |
| `create_contact` | Create a company or person contact. |
| `update_contact` | Update a contact (optimistic locking via `version`). |
| `find_or_create_contact` | Return a matching contact by name or email, or create one. |
| `get_contact_invoices` | List the invoices for one contact. |

### Financial queries

| Tool | Description |
|------|-------------|
| `list_expenses` | List purchase invoices and expenses by status. |
| `get_financial_overview` | Monthly revenue, expenses and net, plus open and overdue invoice counts. |
| `get_payment_status` | Payment status by invoice ID or contact name. |

### Recurring templates, credit notes, dunnings

| Tool | Description |
|------|-------------|
| `list_recurring_templates` | List recurring invoice templates. |
| `get_recurring_template` | Details of one recurring template. |
| `create_credit_note` | Create a credit note (Gutschrift). |
| `create_dunning` | Create a payment reminder (Mahnung) for an overdue invoice. |
| `render_dunning_pdf` | Render a dunning and return the document file ID. |

### Articles

| Tool | Description |
|------|-------------|
| `list_articles` | List service articles. |
| `create_article` | Create a reusable article, for example an hourly consulting rate. |
| `get_article` | Article details. |
| `update_article` | Update an article (optimistic locking). |

### Vouchers (Belege)

| Tool | Description |
|------|-------------|
| `upload_voucher` | Upload a raw receipt file (PDF, PNG, JPG, max 5 MB) to the voucher inbox for review. |
| `create_voucher` | Create a structured purchase voucher (amount, vendor, posting category), with optional file attach and read-back. |
| `attach_voucher_file` | Attach a file to an existing voucher. |
| `get_voucher` | Voucher details. |
| `update_voucher` | Update a voucher (optimistic locking). |
| `list_vouchers` | Generic voucher list by type and status. |
| `list_posting_categories` | List posting categories (Buchungskonten) for voucher categorization. |

### Utilities

| Tool | Description |
|------|-------------|
| `get_profile` | Organization profile (company name, tax settings). |
| `list_payment_conditions` | Configured payment terms. |
| `list_countries` | Countries with tax classification. |

## Resources and prompts

Resources under the `lexoffice://` scheme give the model reference data without a tool call:

- `lexoffice://service-catalog`: standard offerings and prices (static)
- `lexoffice://countries`, `lexoffice://posting-categories`, `lexoffice://payment-conditions`: live reference data from the API
- `lexoffice://tax-config`: detected tax regime and default VAT rate
- `lexoffice://status`: service name, version and uptime
- `lexoffice://contact/{contact_id}/invoices`: invoices for one contact (resource template)

Prompts for guided workflows:

- `monthly_close`: overview, then overdue invoices, then suggested dunnings
- `dunning_run`: find overdue open invoices and create reminders
- `capture_receipt`: `find_or_create_contact`, then `create_voucher` with an optional PDF

## Tax regime

The tax regime is read from the Lexware Office profile (`GET /v1/profile`, field `taxType`) on first use and cached until restart.

| Regime | `taxType` | Default rate |
|--------|-----------|-------------|
| Kleinunternehmerregelung | `vatfree` | 0% |
| Net prices | `net` | 19% |
| Gross prices | `gross` | 19% |

`LEXOFFICE_TAX_TYPE` overrides detection. `create_draft_invoice`, `create_draft_quotation` and `create_article` also accept a per-item `tax_rate`.

## Rate limiting

The Lexware Office API allows 2 requests per second. The client limits concurrency to 2 and retries HTTP 429 responses, honoring `Retry-After`.

## Usage telemetry

A small middleware (`mcp_lexoffice/usage.py`) writes one JSON line per tool call to stderr with the server name, tool name, duration, outcome and negotiated MCP protocol version. It never logs arguments or results, and it sends nothing anywhere; the lines stay in your process logs.

## Development

```bash
uv sync
uv run pytest
```

Tests mock the Lexware Office API with `respx` and use FastMCP's in-memory client for the protocol surface, so they need no API key or network access.

CI (`.github/workflows/ci.yml`) runs the test suite in a job named `test`. `main` is protected: changes go through a pull request, and the `test` check must pass before merge.

## Releases

Releases are tag-only; no commit bumps a version number. When a change lands on `main`, `.github/workflows/release.yml` runs the tests and `pip-audit`, pushes the next patch tag (`vX.Y.Z`) and builds a container image with that version. The version in `pyproject.toml` only seeds the first tag. The Compose file does not use the published image; it builds from source.

## Support

If this server saves you time, you can [buy me a coffee](https://buymeacoffee.com/caseyberlin).

## License

AGPL-3.0. See [LICENSE](LICENSE).

Commercial licensing is available for enterprise and partner integrations. Contact casey@caseydoes.it.
