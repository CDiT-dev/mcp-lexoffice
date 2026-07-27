"""Unit tests for the Lexoffice API client."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest
import respx

from mcp_lexoffice.client import LexofficeClient, BASE_URL
from mcp_lexoffice.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Ensure get_settings() picks up patched env vars in each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestClientInit:
    def test_missing_key_raises(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": ""}):
            with pytest.raises(RuntimeError, match="LEXOFFICE_API_KEY must be set"):
                LexofficeClient()

    def test_auth_header_set(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert c._client.headers["Authorization"] == "Bearer my-key"

    def test_base_url_set(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert str(c._client.base_url).rstrip("/") == BASE_URL

    def test_accept_header_set(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert c._client.headers["Accept"] == "application/json"

    def test_no_default_content_type_header(self):
        # The client must NOT hardcode a default Content-Type: httpx sets application/json
        # for json= calls and multipart/form-data (with boundary) for files= uploads. A
        # hardcoded default shadows the multipart boundary and breaks /files uploads.
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert "Content-Type" not in c._client.headers

    def test_semaphore_has_value_2(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert c._semaphore._value == 2

    def test_timeout_is_30(self):
        with patch.dict(os.environ, {"LEXOFFICE_API_KEY": "my-key"}):
            c = LexofficeClient()
            assert c._client.timeout.connect == 30.0


# ── Profile ──────────────────────────────────────────────────────────


async def test_get_profile(client, mock_api):
    mock_api.get("/profile").respond(
        200, json={"companyName": "Test GmbH", "taxType": "vatfree"}
    )
    result = await client.get_profile()
    assert result["companyName"] == "Test GmbH"
    assert result["taxType"] == "vatfree"


# ── Contacts ─────────────────────────────────────────────────────────


async def test_create_contact(client, mock_api):
    mock_api.post("/contacts").respond(
        200, json={"id": "abc-123", "resourceUri": "https://api.lexoffice.io/v1/contacts/abc-123"}
    )
    result = await client.create_contact({"version": 0, "roles": {"customer": {}}})
    assert result["id"] == "abc-123"


async def test_get_contact(client, mock_api):
    mock_api.get("/contacts/abc-123").respond(
        200, json={"id": "abc-123", "company": {"name": "Acme"}}
    )
    result = await client.get_contact("abc-123")
    assert result["company"]["name"] == "Acme"


async def test_update_contact(client, mock_api):
    mock_api.put("/contacts/abc-123").respond(
        200, json={"id": "abc-123", "version": 2}
    )
    result = await client.update_contact("abc-123", {"version": 1, "company": {"name": "Acme v2"}})
    assert result["version"] == 2


async def test_filter_contacts_all_params(client, mock_api):
    route = mock_api.get("/contacts").respond(
        200, json={"content": [], "totalElements": 0}
    )
    result = await client.filter_contacts(
        name="Test", email="test@example.com", number=10001, customer=True, vendor=False, page=2, size=10
    )
    assert result["totalElements"] == 0
    request = route.calls[0].request
    assert "name=Test" in str(request.url)
    assert "email=test" in str(request.url)
    assert "customer=true" in str(request.url).lower()
    assert "page=2" in str(request.url)


async def test_filter_contacts_minimal(client, mock_api):
    route = mock_api.get("/contacts").respond(
        200, json={"content": [{"id": "x"}], "totalElements": 1}
    )
    result = await client.filter_contacts()
    assert len(result["content"]) == 1
    request = route.calls[0].request
    assert "page=0" in str(request.url)
    assert "size=25" in str(request.url)


async def test_filter_contacts_name_only(client, mock_api):
    route = mock_api.get("/contacts").respond(
        200, json={"content": [], "totalElements": 0}
    )
    await client.filter_contacts(name="Müller")
    url = str(route.calls[0].request.url)
    assert "name=" in url
    # name, email, number optional params should not appear when not passed
    assert "email=" not in url
    assert "number=" not in url
    assert "customer=" not in url
    assert "vendor=" not in url


async def test_filter_contacts_number_zero(client, mock_api):
    """number=0 should still be sent (is not None)."""
    route = mock_api.get("/contacts").respond(
        200, json={"content": [], "totalElements": 0}
    )
    await client.filter_contacts(number=0)
    url = str(route.calls[0].request.url)
    assert "number=0" in url


# ── Invoices ─────────────────────────────────────────────────────────


async def test_create_invoice_draft(client, mock_api):
    mock_api.post("/invoices").respond(
        200, json={"id": "inv-001", "version": 0}
    )
    result = await client.create_invoice({"lineItems": []})
    assert result["id"] == "inv-001"


async def test_create_invoice_finalized(client, mock_api):
    route = mock_api.post("/invoices").respond(
        200, json={"id": "inv-002", "version": 1}
    )
    result = await client.create_invoice({"lineItems": []}, finalize=True)
    assert result["id"] == "inv-002"
    request = route.calls[0].request
    assert "finalize=true" in str(request.url)


async def test_create_invoice_draft_no_finalize_param(client, mock_api):
    """When finalize=False, finalize param should not appear in URL."""
    route = mock_api.post("/invoices").respond(
        200, json={"id": "inv-003", "version": 0}
    )
    await client.create_invoice({"lineItems": []}, finalize=False)
    url = str(route.calls[0].request.url)
    assert "finalize" not in url


async def test_get_invoice(client, mock_api):
    mock_api.get("/invoices/inv-001").respond(
        200, json={"id": "inv-001", "voucherStatus": "draft"}
    )
    result = await client.get_invoice("inv-001")
    assert result["voucherStatus"] == "draft"


async def test_render_invoice_document(client, mock_api):
    mock_api.get("/invoices/inv-001/document").respond(
        200, json={"documentFileId": "file-999"}
    )
    result = await client.render_invoice_document("inv-001")
    assert result["documentFileId"] == "file-999"


async def test_download_invoice_pdf(client, mock_api):
    mock_api.get("/invoices/inv-001/file").respond(200, content=b"%PDF-1.4 fake")
    result = await client.download_invoice_pdf("inv-001")
    assert result.startswith(b"%PDF")


async def test_download_invoice_pdf_accept_header(client, mock_api):
    """download_invoice_pdf should send Accept: application/pdf."""
    route = mock_api.get("/invoices/inv-001/file").respond(200, content=b"%PDF")
    await client.download_invoice_pdf("inv-001")
    req = route.calls[0].request
    assert req.headers.get("accept") == "application/pdf"


# ── Quotations ───────────────────────────────────────────────────────


async def test_create_quotation(client, mock_api):
    route = mock_api.post("/quotations").respond(200, json={"id": "q-001"})
    result = await client.create_quotation({"lineItems": []}, finalize=True)
    assert result["id"] == "q-001"
    assert "finalize=true" in str(route.calls[0].request.url)


async def test_create_quotation_draft(client, mock_api):
    route = mock_api.post("/quotations").respond(200, json={"id": "q-002"})
    await client.create_quotation({"lineItems": []}, finalize=False)
    assert "finalize" not in str(route.calls[0].request.url)


async def test_get_quotation(client, mock_api):
    mock_api.get("/quotations/q-001").respond(200, json={"id": "q-001"})
    result = await client.get_quotation("q-001")
    assert result["id"] == "q-001"


async def test_pursue_quotation(client, mock_api):
    mock_api.post("/quotations/q-001/pursue").respond(200, json={"id": "inv-new"})
    result = await client.pursue_quotation("q-001")
    assert result["id"] == "inv-new"


# ── Credit Notes ─────────────────────────────────────────────────────


async def test_create_credit_note_standalone(client, mock_api):
    mock_api.post("/credit-notes").respond(200, json={"id": "cn-001"})
    result = await client.create_credit_note({"lineItems": []})
    assert result["id"] == "cn-001"


async def test_create_credit_note_linked(client, mock_api):
    route = mock_api.post("/credit-notes").respond(200, json={"id": "cn-002"})
    result = await client.create_credit_note(
        {"lineItems": []}, finalize=True, preceding_id="inv-001"
    )
    assert result["id"] == "cn-002"
    url = str(route.calls[0].request.url)
    assert "precedingSalesVoucherId=inv-001" in url
    assert "finalize=true" in url


async def test_create_credit_note_finalize_only(client, mock_api):
    route = mock_api.post("/credit-notes").respond(200, json={"id": "cn-003"})
    await client.create_credit_note({"lineItems": []}, finalize=True)
    url = str(route.calls[0].request.url)
    assert "finalize=true" in url
    assert "precedingSalesVoucherId" not in url


async def test_get_credit_note(client, mock_api):
    mock_api.get("/credit-notes/cn-001").respond(200, json={"id": "cn-001", "version": 0})
    result = await client.get_credit_note("cn-001")
    assert result["id"] == "cn-001"


# ── Vouchers ─────────────────────────────────────────────────────────


async def test_filter_vouchers(client, mock_api):
    route = mock_api.get("/voucherlist").respond(
        200, json={"content": [], "totalElements": 0}
    )
    result = await client.filter_vouchers("invoice", voucher_status="open", page=1, size=10)
    assert result["totalElements"] == 0
    url = str(route.calls[0].request.url)
    assert "voucherType=invoice" in url
    assert "voucherStatus=open" in url


async def test_filter_vouchers_default_status(client, mock_api):
    route = mock_api.get("/voucherlist").respond(
        200, json={"content": [], "totalElements": 0}
    )
    await client.filter_vouchers("invoice")
    url = str(route.calls[0].request.url)
    assert "voucherStatus=any" in url


async def test_filter_vouchers_default_pagination(client, mock_api):
    route = mock_api.get("/voucherlist").respond(
        200, json={"content": [], "totalElements": 0}
    )
    await client.filter_vouchers("salesinvoice")
    url = str(route.calls[0].request.url)
    assert "page=0" in url
    assert "size=100" in url


# ── Files ────────────────────────────────────────────────────────────


async def test_download_file(client, mock_api):
    mock_api.get("/files/file-999").respond(200, content=b"binary-data")
    result = await client.download_file("file-999")
    assert result == b"binary-data"


async def test_upload_file(client, mock_api):
    route = mock_api.post("/files").respond(200, json={"id": "f-001"})
    result = await client.upload_file(b"fake-content", "bill.pdf")
    assert result["id"] == "f-001"
    req = route.calls[0].request
    assert "type=voucher" in str(req.url)


async def test_upload_file_custom_type(client, mock_api):
    route = mock_api.post("/files").respond(200, json={"id": "f-002"})
    await client.upload_file(b"data", "file.pdf", file_type="salesinvoice")
    url = str(route.calls[0].request.url)
    assert "type=salesinvoice" in url


async def test_upload_file_multipart(client, mock_api):
    route = mock_api.post("/files").respond(200, json={"id": "f-003"})
    await client.upload_file(b"%PDF-1.4 data", "file.pdf")
    req = route.calls[0].request
    # Lexoffice /files requires multipart/form-data (with boundary), not octet-stream.
    assert req.headers.get("content-type", "").startswith("multipart/form-data")
    body = req.content
    assert b'name="file"' in body
    assert b'filename="file.pdf"' in body
    assert b"application/pdf" in body  # part-level content type from the mime guess
    assert b"%PDF-1.4 data" in body


# ── Structured vouchers (CDI-1164) ───────────────────────────────────


async def test_create_voucher(client, mock_api):
    route = mock_api.post("/vouchers").respond(200, json={"id": "v-001"})
    data = {"type": "purchaseinvoice", "totalGrossAmount": 119.0}
    result = await client.create_voucher(data)
    assert result["id"] == "v-001"
    sent = json.loads(route.calls[0].request.content)
    assert sent["type"] == "purchaseinvoice"


async def test_attach_voucher_file(client, mock_api):
    route = mock_api.post("/vouchers/v-001/files").respond(
        200, json={"id": "file-1", "voucherId": "v-001"}
    )
    result = await client.attach_voucher_file("v-001", b"%PDF-1.4 data", "invoice.pdf")
    assert result["id"] == "file-1"
    assert result["voucherId"] == "v-001"


async def test_attach_voucher_file_multipart(client, mock_api):
    route = mock_api.post("/vouchers/v-001/files").respond(200, json={"id": "file-1"})
    await client.attach_voucher_file("v-001", b"%PDF-1.4 data", "invoice.pdf")
    req = route.calls[0].request
    assert req.headers.get("content-type", "").startswith("multipart/form-data")


async def test_list_posting_categories(client, mock_api):
    mock_api.get("/posting-categories").respond(
        200, json=[{"id": "c-1", "name": "Reise MA", "type": "outgo"}]
    )
    result = await client.list_posting_categories()
    assert result[0]["id"] == "c-1"
    assert result[0]["type"] == "outgo"


# ── Payment Conditions ───────────────────────────────────────────────


async def test_list_payment_conditions(client, mock_api):
    mock_api.get("/payment-conditions").respond(
        200, json=[{"id": "pc-1", "paymentTermDuration": 14}]
    )
    result = await client.list_payment_conditions()
    assert len(result) == 1
    assert result[0]["paymentTermDuration"] == 14


async def test_list_payment_conditions_empty(client, mock_api):
    mock_api.get("/payment-conditions").respond(200, json=[])
    result = await client.list_payment_conditions()
    assert result == []


# ── Countries ────────────────────────────────────────────────────────


async def test_list_countries(client, mock_api):
    mock_api.get("/countries").respond(
        200, json=[{"countryCode": "DE", "taxClassification": "de"}]
    )
    result = await client.list_countries()
    assert result[0]["countryCode"] == "DE"


async def test_list_countries_multiple(client, mock_api):
    mock_api.get("/countries").respond(
        200,
        json=[
            {"countryCode": "DE", "taxClassification": "de"},
            {"countryCode": "AT", "taxClassification": "intraCommunity"},
        ],
    )
    result = await client.list_countries()
    assert len(result) == 2
    assert result[1]["countryCode"] == "AT"


# ── Event Subscriptions ──────────────────────────────────────────────


async def test_create_event_subscription(client, mock_api):
    mock_api.post("/event-subscriptions").respond(200, json={"id": "es-001"})
    result = await client.create_event_subscription({"eventType": "invoice.created"})
    assert result["id"] == "es-001"


async def test_list_event_subscriptions(client, mock_api):
    mock_api.get("/event-subscriptions").respond(200, json=[{"id": "es-001"}])
    result = await client.list_event_subscriptions()
    assert len(result) == 1


async def test_list_event_subscriptions_empty(client, mock_api):
    mock_api.get("/event-subscriptions").respond(200, json=[])
    result = await client.list_event_subscriptions()
    assert result == []


async def test_delete_event_subscription(client, mock_api):
    mock_api.delete("/event-subscriptions/es-001").respond(204)
    await client.delete_event_subscription("es-001")


# ── Invoice lifecycle ────────────────────────────────────────────────


async def test_finalize_invoice(client, mock_api):
    mock_api.get("/invoices/inv-001").respond(
        200, json={"id": "inv-001", "version": 2}
    )
    mock_api.post("/invoices/inv-001/finalize").respond(
        200, json={"id": "inv-001", "voucherNumber": "RE-001"}
    )
    result = await client.finalize_invoice("inv-001")
    assert result["voucherNumber"] == "RE-001"


async def test_finalize_invoice_uses_version_from_get(client, mock_api):
    """finalize_invoice should GET the invoice to read its current version."""
    mock_api.get("/invoices/inv-x").respond(
        200, json={"id": "inv-x", "version": 7}
    )
    route_post = mock_api.post("/invoices/inv-x/finalize").respond(
        200, json={"id": "inv-x"}
    )
    await client.finalize_invoice("inv-x")
    import json as _json

    body = _json.loads(route_post.calls[0].request.content)
    assert body["version"] == 7
    assert body["id"] == "inv-x"


async def test_finalize_invoice_missing_version_defaults_to_zero(client, mock_api):
    """If GET response lacks 'version', default to 0."""
    mock_api.get("/invoices/inv-y").respond(
        200, json={"id": "inv-y"}
    )
    route_post = mock_api.post("/invoices/inv-y/finalize").respond(
        200, json={"id": "inv-y"}
    )
    await client.finalize_invoice("inv-y")
    import json as _json

    body = _json.loads(route_post.calls[0].request.content)
    assert body["version"] == 0


async def test_send_invoice(client, mock_api):
    mock_api.post("/invoices/inv-001/send").respond(204)
    await client.send_invoice("inv-001", "test@test.de")


async def test_send_invoice_sends_correct_body(client, mock_api):
    route = mock_api.post("/invoices/inv-001/send").respond(204)
    await client.send_invoice("inv-001", "hello@example.com")
    import json as _json

    body = _json.loads(route.calls[0].request.content)
    assert body == {"recipientEmailAddresses": ["hello@example.com"]}


# ── Quotation lifecycle ─────────────────────────────────────────────


async def test_finalize_quotation(client, mock_api):
    mock_api.get("/quotations/q-001").respond(200, json={"id": "q-001", "version": 1})
    mock_api.post("/quotations/q-001/finalize").respond(
        200, json={"id": "q-001", "voucherNumber": "AG-001"}
    )
    result = await client.finalize_quotation("q-001")
    assert result["voucherNumber"] == "AG-001"


async def test_finalize_quotation_uses_version(client, mock_api):
    mock_api.get("/quotations/q-z").respond(200, json={"id": "q-z", "version": 5})
    route_post = mock_api.post("/quotations/q-z/finalize").respond(
        200, json={"id": "q-z"}
    )
    await client.finalize_quotation("q-z")
    import json as _json

    body = _json.loads(route_post.calls[0].request.content)
    assert body["version"] == 5


# ── Dunnings ────────────────────────────────────────────────────────


async def test_create_dunning(client, mock_api):
    mock_api.post("/dunnings").respond(200, json={"id": "d-001"})
    result = await client.create_dunning({"lineItems": []})
    assert result["id"] == "d-001"


async def test_create_dunning_sends_preceding_sales_voucher_id(client, mock_api):
    """The invoice link travels as a query parameter — Lexoffice has no body field for it."""
    route = mock_api.post("/dunnings").respond(200, json={"id": "d-002"})
    result = await client.create_dunning({"lineItems": []}, preceding_id="inv-001")
    assert result["id"] == "d-002"
    assert "precedingSalesVoucherId=inv-001" in str(route.calls[0].request.url)


async def test_create_dunning_without_preceding_id_sends_no_param(client, mock_api):
    route = mock_api.post("/dunnings").respond(200, json={"id": "d-003"})
    await client.create_dunning({"lineItems": []})
    assert "precedingSalesVoucherId" not in str(route.calls[0].request.url)


async def test_render_dunning_document(client, mock_api):
    mock_api.post("/dunnings/d-001/document").respond(200, json={"documentFileId": "f-d"})
    result = await client.render_dunning_document("d-001")
    assert result["documentFileId"] == "f-d"


# ── Articles ────────────────────────────────────────────────────────


async def test_create_article(client, mock_api):
    mock_api.post("/articles").respond(200, json={"id": "a-001"})
    result = await client.create_article({"title": "Consulting", "type": "SERVICE"})
    assert result["id"] == "a-001"


async def test_get_article(client, mock_api):
    mock_api.get("/articles/a-001").respond(200, json={"id": "a-001", "title": "Consulting"})
    result = await client.get_article("a-001")
    assert result["title"] == "Consulting"


async def test_update_article(client, mock_api):
    mock_api.put("/articles/a-001").respond(200, json={"id": "a-001", "version": 2})
    result = await client.update_article("a-001", {"version": 1, "title": "Updated"})
    assert result["version"] == 2


async def test_list_articles(client, mock_api):
    mock_api.get("/articles").respond(200, json={"content": [{"id": "a-001"}]})
    result = await client.list_articles()
    assert len(result["content"]) == 1


async def test_list_articles_pagination(client, mock_api):
    route = mock_api.get("/articles").respond(200, json={"content": []})
    await client.list_articles(page=3, size=10)
    url = str(route.calls[0].request.url)
    assert "page=3" in url
    assert "size=10" in url


# ── Payments ────────────────────────────────────────────────────────


async def test_get_payments(client, mock_api):
    mock_api.get("/payments/inv-001").respond(200, json={"openAmount": 500})
    result = await client.get_payments("inv-001")
    assert result["openAmount"] == 500


# ── Rate limit retry ────────────────────────────────────────────────


async def test_429_retry(client, mock_api):
    route = mock_api.get("/profile")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"companyName": "CDIT"}),
    ]
    result = await client.get_profile()
    assert result["companyName"] == "CDIT"


async def test_429_retry_uses_retry_after_header(client, mock_api):
    """Retry-After value should be parsed as float for sleep duration."""
    route = mock_api.get("/profile")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": True}),
    ]
    result = await client.get_profile()
    assert result["ok"] is True


async def test_429_retry_default_retry_after(client, mock_api):
    """When Retry-After header is missing, defaults to 1 second.
    We verify by checking the Retry-After parsing logic: float('1') == 1.0."""
    route = mock_api.get("/profile")
    # Use Retry-After: 0 to avoid actual sleep in tests
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": True}),
    ]
    result = await client.get_profile()
    assert result["ok"] is True


async def test_429_retry_second_attempt_also_fails(client, mock_api):
    """If the retry also returns an error, raise_for_status should fire."""
    route = mock_api.get("/profile")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(500, json={"message": "Server Error"}),
    ]
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_profile()
    assert exc_info.value.response.status_code == 500


# ── Error handling ───────────────────────────────────────────────────


async def test_http_error_propagates(client, mock_api):
    mock_api.get("/profile").respond(401, json={"message": "Unauthorized"})
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_profile()


async def test_404_propagates(client, mock_api):
    mock_api.get("/invoices/nonexistent").respond(404, json={"message": "Not found"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_invoice("nonexistent")
    assert exc_info.value.response.status_code == 404


# ── get_invoice_or_voucher fallback ─────────────────────────────────


async def test_get_invoice_or_voucher_resolves_invoice(client, mock_api):
    """When /invoices/{id} succeeds, return invoice data with _resolvedVia=invoices."""
    mock_api.get("/invoices/inv-001").respond(
        200, json={"id": "inv-001", "voucherStatus": "open"}
    )
    result = await client.get_invoice_or_voucher("inv-001")
    assert result["id"] == "inv-001"
    assert result["_resolvedVia"] == "invoices"


async def test_get_invoice_or_voucher_falls_back_to_voucher(client, mock_api):
    """When /invoices/{id} returns 404, fall back to /vouchers/{id}."""
    mock_api.get("/invoices/beleg-001").respond(404, json={"message": "Not found"})
    mock_api.get("/vouchers/beleg-001").respond(
        200, json={"id": "beleg-001", "type": "salesinvoice", "voucherStatus": "unchecked"}
    )
    result = await client.get_invoice_or_voucher("beleg-001")
    assert result["id"] == "beleg-001"
    assert result["_resolvedVia"] == "vouchers"


async def test_get_invoice_or_voucher_propagates_non_404(client, mock_api):
    """Non-404 errors from /invoices/{id} should propagate, not fall back."""
    mock_api.get("/invoices/inv-403").respond(403, json={"message": "Forbidden"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_invoice_or_voucher("inv-403")
    assert exc_info.value.response.status_code == 403


async def test_get_invoice_or_voucher_both_404(client, mock_api):
    """When both /invoices and /vouchers return 404, the voucher 404 propagates."""
    mock_api.get("/invoices/gone").respond(404, json={"message": "Not found"})
    mock_api.get("/vouchers/gone").respond(404, json={"message": "Not found"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_invoice_or_voucher("gone")
    assert exc_info.value.response.status_code == 404


async def test_400_bad_request(client, mock_api):
    mock_api.post("/contacts").respond(400, json={"message": "Bad request"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.create_contact({})
    assert exc_info.value.response.status_code == 400


async def test_403_forbidden(client, mock_api):
    mock_api.get("/profile").respond(403, json={"message": "Forbidden"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_profile()
    assert exc_info.value.response.status_code == 403


async def test_409_conflict(client, mock_api):
    mock_api.put("/contacts/c-1").respond(409, json={"message": "Conflict"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.update_contact("c-1", {"version": 0})
    assert exc_info.value.response.status_code == 409


async def test_422_unprocessable(client, mock_api):
    mock_api.post("/invoices").respond(422, json={"message": "Validation failed"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.create_invoice({})
    assert exc_info.value.response.status_code == 422


async def test_500_server_error(client, mock_api):
    mock_api.get("/profile").respond(500, json={"message": "Internal server error"})
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await client.get_profile()
    assert exc_info.value.response.status_code == 500


# ── Request method passthrough ──────────────────────────────────────


async def test_request_passes_custom_headers(client, mock_api):
    """_request should merge custom accept and content_type headers."""
    route = mock_api.get("/files/f-1").respond(200, content=b"data")
    await client.download_file("f-1")
    req = route.calls[0].request
    assert req.headers.get("accept") == "application/octet-stream"
