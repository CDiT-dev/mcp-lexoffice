"""The MCP surface over the wire, via an in-memory ``fastmcp.Client``.

test_server.py mostly calls tool functions directly with a FakeContext and reads
the manifest server-side (``mcp.list_tools()``). These go through the protocol:
the server stands up (lifespan included), its tools reach the client, the hints
survive serialisation, and a read-only call dispatches down to the real
LexofficeClient — with HTTP intercepted by respx, so nothing reaches Lexoffice.
test_server.py's ``test_array_tool_text_content_backward_compatible`` is the
other in-memory round trip (list tools, client object swapped out).
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import patch

import httpx
import pytest
from fastmcp import Client

from mcp_lexoffice.config import get_settings
from mcp_lexoffice.server import MAX_REQUEST_BODY_BYTES, mcp

EXPECTED_TOOLS = {
    "get_profile", "get_invoice", "list_invoices", "create_draft_invoice",
    "search_contacts", "create_voucher",
    "get_financial_overview", "list_countries",
}


@pytest.fixture()
def wire_env():
    # The lifespan builds a real LexofficeClient, which needs an API key; Settings is cached.
    get_settings.cache_clear()
    with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "test-key-wire"}):
        yield
    get_settings.cache_clear()


async def test_server_registers_its_tools(wire_env):
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


async def test_annotations_survive_the_wire(wire_env):
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["get_profile"].annotations.read_only_hint is True
    assert tools["create_draft_invoice"].annotations.destructive_hint is True


async def test_removed_dead_routes_stay_removed(wire_env):
    """CDI-1905: Lexoffice has no /finalize or /send routes; those tools must not return."""
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert not names & {"finalize_invoice", "finalize_quotation", "send_invoice",
                        "create_and_send_invoice", "convert_quotation_and_send"}


async def test_create_draft_invoice_finalize_hits_create_with_query(wire_env, mock_api):
    """CDI-1905: finalizing is only POST /invoices?finalize=true at create time."""
    mock_api.get("/profile").respond(200, json={"taxType": "net"})
    create = mock_api.post("/invoices", params={"finalize": "true"}).respond(
        200, json={"id": "inv-9", "version": 1}
    )
    mock_api.post("/invoices").respond(500)  # any other POST shape is a bug
    async with Client(mcp) as client:
        await client.call_tool("create_draft_invoice", {
            "recipient_name": "Acme", "finalize": True,
            "line_items": [{"name": "Consulting", "unit_price": 150}],
        })
    assert create.call_count == 1
    assert create.calls.last.request.url.params["finalize"] == "true"
    assert not any("/finalize" in str(c.request.url.path) for c in mock_api.calls)


async def test_read_only_call_round_trips(wire_env, mock_api):
    route = mock_api.get("/profile").mock(
        return_value=httpx.Response(
            200,
            json={"organizationId": "org-1", "companyName": "Test GmbH",
                  "taxType": "vatfree", "smallBusiness": True},
        )
    )
    async with Client(mcp) as client:
        result = await client.call_tool("get_profile", {})
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-key-wire"
    assert result.structured_content["companyName"] == "Test GmbH"
    assert "Test GmbH" in result.content[0].text


async def test_a_tool_call_emits_one_usage_line(wire_env, mock_api, capfd):
    mock_api.get("/profile").mock(return_value=httpx.Response(200, json={"companyName": "Test GmbH"}))
    capfd.readouterr()
    async with Client(mcp) as client:
        await client.call_tool("get_profile", {})
    lines = [ln for ln in capfd.readouterr().err.splitlines() if '"mcp_usage"' in ln]
    assert len(lines) == 1
    assert '"server": "lexoffice"' in lines[0]
    assert '"tool": "get_profile"' in lines[0]
    assert '"outcome": "ok"' in lines[0]


async def _post_mcp(body: bytes) -> httpx.Response:
    """POST a raw JSON-RPC body to the real streamable-HTTP app (lifespan included)."""
    app = mcp.http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            return await http.post("/mcp", content=body, headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            })


async def test_upload_bigger_than_sdk_default_body_cap_reaches_the_tool(wire_env, mock_api):
    """CDI-2080: a 4.5 MB receipt is ~6 MB of base64, over the SDK's 4 MiB default. It used
    to get a bare 413 before the tool ran; now it reaches Lexoffice like any other upload."""
    files = mock_api.post("/files").respond(200, json={"id": "file-1"})
    pdf = b"%PDF" + b"x" * 4_500_000
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "upload_voucher", "arguments": {
            "file_content": base64.b64encode(pdf).decode(), "file_name": "receipt.pdf",
        }},
    }).encode()
    assert len(body) > 4 * 1024 * 1024
    resp = await _post_mcp(body)
    assert resp.status_code == 200
    assert resp.json()["result"]["isError"] is False
    assert files.call_count == 1


async def test_body_over_the_raised_cap_is_still_refused(wire_env):
    resp = await _post_mcp(b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert resp.status_code == 413
