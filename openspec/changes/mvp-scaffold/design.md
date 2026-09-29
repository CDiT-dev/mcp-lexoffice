## Context

mcp-lexoffice is a Python MCP server exposing Lexware Office REST API tools to Claude. It currently runs on FastMCP 2 with 1Password CLI key resolution, 15 raw API wrapper tools, and stdio transport. It needs to become a production Claude.ai connector behind Caddy, with Claude-friendly tool signatures (named params, not raw JSON blobs).

The server already has a working httpx client with rate limiting (2 req/s semaphore) and covers contacts, invoices, quotations, credit notes, vouchers, payment conditions, and countries. The upgrade reshapes this foundation rather than replacing it.

Lexware Office account: Casey does IT (CDIT), Kleinunternehmerregelung (vatfree), payment terms "Zahlbar sofort, rein netto".

## Goals / Non-Goals

**Goals:**
- Upgrade to FastMCP 3.x with streamable-http transport + `json_response=True`
- Replace 1Password CLI auth with `.env`-based bearer token
- Reshape tools from raw JSON input to named parameters (Claude-friendly)
- Add invoice lifecycle tools: draft → finalize → send
- Add voucher upload for Gmail bill ingestion
- Add Phase 2 tools: financial queries, payment status, quotations, dunnings, articles, contacts
- Deploy as Claude.ai custom connector via Caddy

**Non-Goals:**
- n8n integration / webhook event subscriptions (BFF endpoint later)
- SQLite caching layer (Besserwisser concern, not MCP server concern)
- OAuth / multi-tenant auth (single-operator server)
- Frontend / UI of any kind
- Automated Gmail scanning (Claude's own Gmail connector handles that)

## Decisions

### D1: FastMCP 3 streamable-http with json_response=True

**Choice**: Use `streamable-http` transport with `json_response=True` on port 8000.

**Why**: Claude.ai custom connectors require HTTP transport. The `json_response=True` flag prevents GET request hangs during connector initialization (discovered during CDI-670 work). SSE transport also works but streamable-http is the FastMCP 3 default.

**Alternatives**: SSE (works but older pattern), stdio (doesn't work with Claude.ai connectors).

### D2: .env file auth with dotenv

**Choice**: Load `LEXOFFICE_API_KEY` from `.env` via `python-dotenv` at server startup. Keep the `op://` fallback for local dev convenience.

**Why**: Production deployment uses Docker environment variables injected by the compose file. The `.env` file is for local development only. Keeping the `op://` fallback costs nothing and helps Casey's local workflow.

**Alternatives**: Remove 1Password entirely (loses dev convenience), use Docker secrets (overkill for single-operator).

### D3: Named parameters over raw JSON

**Choice**: Reshape tool signatures from `invoice_json: str` (raw JSON blob) to named parameters (`recipient_name`, `line_items`, `currency`, etc.).

**Why**: Claude performs significantly better with named parameters — it can fill them from conversation context without constructing valid JSON. The raw JSON approach requires Claude to know the exact Lexoffice API schema.

**Alternatives**: Keep raw JSON (poor Claude UX), use Pydantic models as params (FastMCP 3 supports this but adds complexity).

### D4: Two-step invoice flow (draft + finalize)

**Choice**: Separate `create_draft_invoice` and `finalize_invoice` into distinct tools. Add `send_invoice` as a third optional step.

**Why**: Invoices in Germany have legal implications once finalized (Rechnungsnummer assigned, can't be deleted). The two-step flow lets Casey review in Lexoffice UI before committing. Deep link to Lexoffice UI returned with every draft.

**Alternatives**: Single `create_invoice(finalize=True)` (exists already, too risky for conversational use).

### D5: Voucher upload via Lexoffice file API

**Choice**: Use `POST /v1/files` with `type=voucher` to upload bill attachments. Claude extracts the file from Gmail, passes base64 content to the MCP tool.

**Why**: Direct API upload gives immediate confirmation. The alternative (forwarding to Lexoffice XL's Belegempfang email address) works but is async and less reliable for confirmation.

**Alternatives**: Email forwarding to `inbox.lexware.email` (CDI-673 describes this as a fallback — simpler but no confirmation loop).

### D6: Keep existing client.py pattern

**Choice**: Extend the existing `LexofficeClient` class with new methods. Don't restructure into separate modules.

**Why**: The client is ~230 lines with a clean pattern (method per endpoint, shared `_request` with rate limiting). Adding payment, dunning, article, and file upload methods keeps it under 400 lines — still manageable as a single file.

**Alternatives**: Split into `client/invoices.py`, `client/contacts.py`, etc. (premature for 7 capability areas).

### D7: Deep links for all mutations

**Choice**: Every tool that creates or modifies a resource returns a Lexoffice deep link (`https://app.lexoffice.de/...`) alongside the resource ID.

**Why**: Casey reviews in the Lexoffice UI after Claude drafts. The deep link eliminates manual navigation. Pattern: `https://app.lexoffice.de/#/voucher/edit/{id}` for drafts, `https://app.lexoffice.de/#/voucher/view/{id}` for finalized.

## Risks / Trade-offs

**[Rate limiting]** → The 2 req/s Lexoffice API limit means chained tool calls (e.g., search contact → create invoice → finalize) can hit 429s. Mitigation: existing semaphore in client.py handles this. Add retry-after header parsing for 429 responses.

**[FastMCP 3 breaking changes]** → FastMCP 2→3 may change decorator API, lifespan pattern, or context access. Mitigation: check FastMCP 3 docs before implementation. The core pattern (`@mcp.tool`, lifespan context) is likely stable.

**[Base64 file size]** → Gmail bill attachments as base64 through MCP could be large. Mitigation: Lexoffice file upload limit is 5MB — most bills are well under. Add size validation in the tool.

**[Kleinunternehmer tax defaults]** → All invoices/quotations must use `taxType: "vatfree"` and `taxRatePercentage: 0`. If this is wrong, invoices are legally invalid. Mitigation: hardcode as defaults in tool params, allow override for future flexibility.
