"""Unit tests for the MCP server tools."""

from __future__ import annotations

import base64
import json
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from mcp_lexoffice.server import (
    DEFAULT_TAX_RATE,
    _build_address,
    _build_line_items,
    _contact_link,
    _deep_link,
    _fmt,
    _get_payment_conditions,
    _get_tax_config,
    mcp,
)


# ── Helper: build a fake Context ─────────────────────────────────────


class FakeContext:
    """Minimal stand-in for fastmcp Context with lifespan_context."""

    def __init__(self, lexoffice_client):
        self.lifespan_context = {"lexoffice": lexoffice_client}


_SENTINEL = object()


def make_ctx(
    method_responses: dict[str, object],
    *,
    tax_type: str = "vatfree",
    payment_conditions: object = _SENTINEL,
) -> FakeContext:
    """Create a FakeContext with a mock LexofficeClient.

    By default the payment_conditions cache is pre-filled with an empty list so
    _resolve_payment_condition skips embedding — this keeps invoice/quotation
    tests that don't care about payment conditions independent of that feature.
    Pass payment_conditions=None to leave the cache unset (forcing a real
    list_payment_conditions call), or pass an explicit list to seed it.
    """
    client = AsyncMock()
    for method, response in method_responses.items():
        getattr(client, method).return_value = response
    ctx = FakeContext(client)
    ctx.lifespan_context["tax_config"] = {
        "tax_type": tax_type,
        "default_rate": DEFAULT_TAX_RATE.get(tax_type, 0),
    }
    if payment_conditions is _SENTINEL:
        ctx.lifespan_context["payment_conditions"] = []
    elif payment_conditions is not None:
        ctx.lifespan_context["payment_conditions"] = payment_conditions
    return ctx


# ── _fmt ─────────────────────────────────────────────────────────────


def test_fmt_pretty_json():
    result = _fmt({"key": "wert", "nested": [1, 2]})
    parsed = json.loads(result)
    assert parsed["key"] == "wert"
    assert "\n" in result


def test_fmt_unicode():
    result = _fmt({"name": "Müller & Söhne"})
    assert "Müller" in result


def test_fmt_with_date():
    """_fmt should handle date objects via default=str."""
    result = _fmt({"date": date(2026, 1, 15)})
    parsed = json.loads(result)
    assert parsed["date"] == "2026-01-15"


def test_fmt_empty():
    result = _fmt({})
    assert json.loads(result) == {}


def test_fmt_nested():
    result = _fmt({"a": {"b": {"c": 1}}})
    parsed = json.loads(result)
    assert parsed["a"]["b"]["c"] == 1


# ── _deep_link helper ────────────────────────────────────────────────


class TestDeepLink:
    def test_view_mode(self):
        link = _deep_link("abc-123")
        assert link == "https://app.lexoffice.de/#/voucher/view/abc-123"

    def test_edit_mode(self):
        link = _deep_link("abc-123", edit=True)
        assert link == "https://app.lexoffice.de/#/voucher/edit/abc-123"

    def test_default_is_view(self):
        link = _deep_link("xyz")
        assert "/view/" in link
        assert "/edit/" not in link


# ── _contact_link helper ─────────────────────────────────────────────


class TestContactLink:
    def test_basic(self):
        link = _contact_link("c-001")
        assert link == "https://app.lexoffice.de/#/contacts/c-001"


# ── _build_line_items helper ─────────────────────────────────────────


class TestBuildLineItems:
    def test_minimal_item(self):
        items = _build_line_items([{"name": "Service", "unit_price": 100}])
        assert len(items) == 1
        li = items[0]
        assert li["type"] == "custom"
        assert li["name"] == "Service"
        assert li["quantity"] == 1
        assert li["unitName"] == "Stück"
        assert li["unitPrice"]["currency"] == "EUR"
        assert li["unitPrice"]["netAmount"] == 100
        assert li["unitPrice"]["taxRatePercentage"] == 0
        assert "description" not in li

    def test_full_item(self):
        items = _build_line_items([{
            "name": "Consulting",
            "unit_price": 150,
            "quantity": 8,
            "unit_name": "Stunde",
            "currency": "USD",
            "description": "8h consulting work",
        }])
        li = items[0]
        assert li["quantity"] == 8
        assert li["unitName"] == "Stunde"
        assert li["unitPrice"]["currency"] == "USD"
        assert li["unitPrice"]["netAmount"] == 150
        assert li["description"] == "8h consulting work"

    def test_multiple_items(self):
        items = _build_line_items([
            {"name": "A", "unit_price": 10},
            {"name": "B", "unit_price": 20},
            {"name": "C", "unit_price": 30},
        ])
        assert len(items) == 3
        assert items[0]["name"] == "A"
        assert items[2]["unitPrice"]["netAmount"] == 30

    def test_empty_list(self):
        items = _build_line_items([])
        assert items == []

    def test_tax_rate_always_zero(self):
        """Kleinunternehmer: tax rate should always be 0."""
        items = _build_line_items([{"name": "X", "unit_price": 99}])
        assert items[0]["unitPrice"]["taxRatePercentage"] == 0


# ── _build_address helper ────────────────────────────────────────────


class TestBuildAddress:
    def test_minimal(self):
        addr = _build_address("Acme GmbH")
        assert addr == {"name": "Acme GmbH", "countryCode": "DE"}

    def test_full(self):
        addr = _build_address(
            "Test Corp", street="Hauptstr. 1", zip_code="10115", city="Berlin", country_code="AT"
        )
        assert addr["name"] == "Test Corp"
        assert addr["street"] == "Hauptstr. 1"
        assert addr["zip"] == "10115"
        assert addr["city"] == "Berlin"
        assert addr["countryCode"] == "AT"

    def test_partial_address(self):
        addr = _build_address("X", city="Hamburg")
        assert addr == {"name": "X", "countryCode": "DE", "city": "Hamburg"}
        assert "street" not in addr
        assert "zip" not in addr

    def test_default_country_is_de(self):
        addr = _build_address("Test")
        assert addr["countryCode"] == "DE"


# ── Tool registration ────────────────────────────────────────────────


async def test_all_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "get_profile",
        "search_contacts", "get_contact", "create_contact", "update_contact",
        "create_draft_invoice", "finalize_invoice", "delete_draft_invoice", "send_invoice",
        "get_invoice", "get_invoice_pdf", "list_invoices",
        "upload_voucher",
        "list_expenses", "get_financial_overview", "get_payment_status",
        "create_draft_quotation", "finalize_quotation", "pursue_quotation_to_invoice",
        "create_dunning", "render_dunning_pdf",
        "list_articles", "create_article", "get_article", "update_article",
        "list_vouchers", "list_payment_conditions", "list_countries",
    }
    assert expected == names


async def test_tool_count_is_27():
    tools = await mcp.list_tools()
    assert len(tools) == 28


# ── Profile tool ─────────────────────────────────────────────────────


async def test_get_profile_tool():
    from mcp_lexoffice.server import get_profile

    ctx = make_ctx({"get_profile": {"companyName": "CDIT", "taxType": "vatfree"}})
    result = await get_profile(ctx)
    parsed = json.loads(result)
    assert parsed["companyName"] == "CDIT"


async def test_get_profile_returns_valid_json():
    from mcp_lexoffice.server import get_profile

    ctx = make_ctx({"get_profile": {"a": 1}})
    result = await get_profile(ctx)
    json.loads(result)  # should not raise


# ── Invoice tools ────────────────────────────────────────────────────


async def test_create_draft_invoice_tool():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-1", "version": 0}})
    result = await create_draft_invoice(
        ctx,
        recipient_name="Acme GmbH",
        line_items='[{"name": "Consulting", "unit_price": 3000, "quantity": 1}]',
    )
    parsed = json.loads(result)
    assert parsed["id"] == "inv-1"
    assert "deepLink" in parsed
    call_args = ctx.lifespan_context["lexoffice"].create_invoice.call_args
    invoice_data = call_args[0][0]
    assert invoice_data["taxConditions"]["taxType"] == "vatfree"
    assert invoice_data["address"]["name"] == "Acme GmbH"
    assert invoice_data["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 0


async def test_create_draft_invoice_deep_link_is_edit():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-1"}})
    result = await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    parsed = json.loads(result)
    assert "/edit/" in parsed["deepLink"]


# ── Payment conditions: create_draft_invoice ────────────────────────


async def test_create_draft_invoice_with_explicit_condition_id():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-1"}},
        payment_conditions=[
            {
                "id": "pc-1",
                "organizationId": "org-1",
                "paymentTermLabel": "Netto 14",
                "paymentTermDuration": 14,
                "organizationDefault": False,
            }
        ],
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-1",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    pc = call_data["paymentConditions"]
    assert pc["paymentTermLabel"] == "Netto 14"
    assert pc["paymentTermDuration"] == 14
    # Whitelist: metadata must not be embedded
    assert "id" not in pc
    assert "organizationId" not in pc
    assert "organizationDefault" not in pc


async def test_create_draft_invoice_omits_payment_conditions_by_default():
    """Without an explicit payment_condition_id we omit the field so Lexware
    applies the contact-specific or organization default itself. This also
    avoids overriding contact-level defaults with the org default."""
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-2"}},
        payment_conditions=None,  # cache unset — must not be fetched
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert "paymentConditions" not in call_data
    ctx.lifespan_context["lexoffice"].list_payment_conditions.assert_not_called()


async def test_create_draft_invoice_explicit_id_overrides_default():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-3"}},
        payment_conditions=[
            {
                "id": "pc-default",
                "paymentTermLabel": "Default Label",
                "paymentTermDuration": 0,
                "organizationDefault": True,
            },
            {
                "id": "pc-other",
                "paymentTermLabel": "Netto 30",
                "paymentTermDuration": 30,
                "organizationDefault": False,
            },
        ],
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-other",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["paymentConditions"]["paymentTermLabel"] == "Netto 30"
    assert call_data["paymentConditions"]["paymentTermDuration"] == 30


async def test_create_draft_invoice_invalid_id_refreshes_and_succeeds():
    from mcp_lexoffice.server import create_draft_invoice

    # Cache starts empty; on miss, _get_payment_conditions(refresh=True) is called
    # and returns a list that contains the requested ID.
    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-4"}},
        payment_conditions=[],  # empty cache, will trigger refresh
    )
    # Configure list_payment_conditions mock for the refresh call
    ctx.lifespan_context["lexoffice"].list_payment_conditions.return_value = [
        {
            "id": "pc-new",
            "paymentTermLabel": "Refreshed Label",
            "paymentTermDuration": 21,
            "organizationDefault": False,
        }
    ]
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-new",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["paymentConditions"]["paymentTermLabel"] == "Refreshed Label"
    assert ctx.lifespan_context["lexoffice"].list_payment_conditions.call_count == 1


async def test_create_draft_invoice_invalid_id_returns_error_after_refresh():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-5"}},
        payment_conditions=[],
    )
    # Refresh also returns empty → still no match → structured error
    ctx.lifespan_context["lexoffice"].list_payment_conditions.return_value = []
    result = await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-missing",
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "pc-missing" in parsed["error"]
    # create_invoice must not have been called
    ctx.lifespan_context["lexoffice"].create_invoice.assert_not_called()


async def test_create_draft_invoice_preserves_discount_conditions():
    from mcp_lexoffice.server import create_draft_invoice

    discount = {
        "discountPercentage": 2,
        "discountRange": 10,
    }
    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-8"}},
        payment_conditions=[
            {
                "id": "pc-discount",
                "paymentTermLabel": "2% Skonto 10 Tage, sonst 30",
                "paymentTermDuration": 30,
                "paymentDiscountConditions": discount,
                "organizationDefault": True,
            }
        ],
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-discount",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["paymentConditions"]["paymentDiscountConditions"] == discount


async def test_create_draft_invoice_payment_term_duration_zero():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-pc-9"}},
        payment_conditions=[
            {
                "id": "pc-sofort",
                "paymentTermLabel": "Zahlbar sofort, rein netto",
                "paymentTermDuration": 0,
                "organizationDefault": True,
            }
        ],
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-sofort",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["paymentConditions"]["paymentTermDuration"] == 0
    assert "Zahlbar sofort" in call_data["paymentConditions"]["paymentTermLabel"]


async def test_create_draft_quotation_omits_payment_conditions_by_default():
    """Mirror of invoice: omit paymentConditions so Lexware applies its own default."""
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx(
        {"create_quotation": {"id": "q-pc-1"}},
        payment_conditions=None,
    )
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert "paymentConditions" not in call_data
    ctx.lifespan_context["lexoffice"].list_payment_conditions.assert_not_called()


# ── Payment term label rendering (real API schema with templates) ────


def test_render_payment_term_label_substitutes_payment_range():
    from mcp_lexoffice.server import _render_payment_term_label

    label = _render_payment_term_label(
        {
            "paymentTermLabelTemplate": "Zahlbar in {paymentRange} Tagen, rein netto ohne Abzug",
            "paymentTermDuration": 14,
        }
    )
    assert label == "Zahlbar in 14 Tagen, rein netto ohne Abzug"


def test_render_payment_term_label_substitutes_discount_placeholders():
    from mcp_lexoffice.server import _render_payment_term_label

    label = _render_payment_term_label(
        {
            "paymentTermLabelTemplate": "{discountRange} Tage -{discount}, {paymentRange} Tage netto",
            "paymentTermDuration": 30,
            "paymentDiscountConditions": {
                "discountRange": 10,
                "discountPercentage": 3,
            },
        }
    )
    assert label == "10 Tage -3 %, 30 Tage netto"


def test_render_payment_term_label_passes_static_template_through():
    from mcp_lexoffice.server import _render_payment_term_label

    label = _render_payment_term_label(
        {
            "paymentTermLabelTemplate": "Zahlbar sofort, rein netto",
            "paymentTermDuration": 0,
        }
    )
    assert label == "Zahlbar sofort, rein netto"


def test_render_payment_term_label_uses_direct_label_when_present():
    from mcp_lexoffice.server import _render_payment_term_label

    # Defensive backward-compat path: if the API ever returns a pre-rendered
    # label, prefer it over the template.
    label = _render_payment_term_label(
        {
            "paymentTermLabel": "Pre-rendered Label",
            "paymentTermLabelTemplate": "ignored {paymentRange}",
            "paymentTermDuration": 7,
        }
    )
    assert label == "Pre-rendered Label"


def test_render_payment_term_label_falls_back_when_template_missing():
    from mcp_lexoffice.server import _render_payment_term_label

    assert (
        _render_payment_term_label({"paymentTermDuration": 14})
        == "Zahlbar in 14 Tagen"
    )
    assert _render_payment_term_label({"paymentTermDuration": 0}) == "Zahlbar sofort"
    assert _render_payment_term_label({}) == "Zahlbar sofort"


def test_render_payment_term_label_falls_back_on_unresolved_placeholder():
    from mcp_lexoffice.server import _render_payment_term_label

    # Discount data missing but template references {discount}/{discountRange}
    # → don't ship a label with literal "{...}" in it; use the deterministic
    # fallback instead.
    label = _render_payment_term_label(
        {
            "paymentTermLabelTemplate": "{discountRange} Tage -{discount}, {paymentRange} Tage netto",
            "paymentTermDuration": 30,
        }
    )
    assert label == "Zahlbar in 30 Tagen"


async def test_create_draft_invoice_renders_template_when_id_is_explicit():
    """Regression test for the 406 bug: when the user passes an explicit
    payment_condition_id, GET /payment-conditions returns paymentTermLabelTemplate
    (not paymentTermLabel), and we must render it before POSTing to /invoices."""
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx(
        {"create_invoice": {"id": "inv-tpl-1"}},
        payment_conditions=[
            {
                "id": "pc-tpl",
                "paymentTermLabelTemplate": "Zahlbar in {paymentRange} Tagen, rein netto ohne Abzug",
                "paymentTermDuration": 7,
                "organizationDefault": True,
            }
        ],
    )
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-tpl",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    pc = call_data["paymentConditions"]
    assert pc["paymentTermLabel"] == "Zahlbar in 7 Tagen, rein netto ohne Abzug"
    assert pc["paymentTermDuration"] == 7
    # Regression: never ship an empty label (API rejects with 406).
    assert pc["paymentTermLabel"] != ""


async def test_create_draft_quotation_renders_template_when_id_is_explicit():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx(
        {"create_quotation": {"id": "q-tpl-1"}},
        payment_conditions=[
            {
                "id": "pc-tpl",
                "paymentTermLabelTemplate": "Zahlbar in {paymentRange} Tagen, rein netto ohne Abzug",
                "paymentTermDuration": 14,
                "organizationDefault": True,
            }
        ],
    )
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        payment_condition_id="pc-tpl",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert (
        call_data["paymentConditions"]["paymentTermLabel"]
        == "Zahlbar in 14 Tagen, rein netto ohne Abzug"
    )


async def test_create_draft_invoice_with_introduction_and_remark():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-4"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        introduction="Sehr geehrte Damen und Herren",
        remark="Vielen Dank",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["introduction"] == "Sehr geehrte Damen und Herren"
    assert call_data["remark"] == "Vielen Dank"


async def test_create_draft_invoice_without_introduction_and_remark():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-5"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert "introduction" not in call_data
    assert "remark" not in call_data


async def test_create_draft_invoice_full_address():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-6"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Firma XY",
        line_items='[{"name": "A", "unit_price": 1}]',
        street="Musterstr. 1",
        zip_code="12345",
        city="Berlin",
        country_code="AT",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    addr = call_data["address"]
    assert addr["name"] == "Firma XY"
    assert addr["street"] == "Musterstr. 1"
    assert addr["zip"] == "12345"
    assert addr["city"] == "Berlin"
    assert addr["countryCode"] == "AT"


async def test_create_draft_invoice_custom_title():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-7"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        title="Custom Invoice Title",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["title"] == "Custom Invoice Title"


async def test_create_draft_invoice_default_title():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-8"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["title"] == "Rechnung"


async def test_create_draft_invoice_currency():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-9"}})
    await create_draft_invoice(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        currency="USD",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["totalPrice"]["currency"] == "USD"


async def test_create_draft_invoice_with_contact_id_discards_address_fields():
    """contact_id sets address.contactId + linkedContactId; explicit street/zip/city are discarded."""
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-contact-1"}})
    result = await create_draft_invoice(
        ctx,
        recipient_name="Acme GmbH",
        line_items='[{"name": "Consulting", "unit_price": 3000}]',
        contact_id="c-acme",
        street="Ignored Str. 1",
        zip_code="99999",
        city="Nowhere",
        country_code="AT",
    )
    parsed = json.loads(result)
    assert parsed["linkedContactId"] == "c-acme"

    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    addr = call_data["address"]
    assert addr["contactId"] == "c-acme"
    assert addr["name"] == "Acme GmbH"
    assert addr["countryCode"] == "AT"
    assert "street" not in addr
    assert "zip" not in addr
    assert "city" not in addr


async def test_create_draft_invoice_without_contact_id_omits_linked_contact_id():
    """Regression: without contact_id, payload stays free-form and linkedContactId is absent."""
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-contact-3"}})
    result = await create_draft_invoice(
        ctx,
        recipient_name="Freeform Kunde",
        line_items='[{"name": "A", "unit_price": 1}]',
        street="Musterstr. 1",
        zip_code="12345",
        city="Berlin",
    )
    parsed = json.loads(result)
    assert "linkedContactId" not in parsed

    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    addr = call_data["address"]
    assert "contactId" not in addr
    assert addr["name"] == "Freeform Kunde"
    assert addr["street"] == "Musterstr. 1"
    assert addr["zip"] == "12345"
    assert addr["city"] == "Berlin"


async def test_finalize_invoice_tool():
    from mcp_lexoffice.server import finalize_invoice

    ctx = make_ctx({"finalize_invoice": {"id": "inv-1", "voucherNumber": "RE-2026-001"}})
    result = await finalize_invoice(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert parsed["voucherNumber"] == "RE-2026-001"
    assert "deepLink" in parsed


async def test_finalize_invoice_deep_link_is_view():
    """Finalized invoice deep link should use 'view', not 'edit'."""
    from mcp_lexoffice.server import finalize_invoice

    ctx = make_ctx({"finalize_invoice": {"id": "inv-1"}})
    result = await finalize_invoice(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert "/view/" in parsed["deepLink"]
    assert "/edit/" not in parsed["deepLink"]


async def test_send_invoice_blocks_draft():
    from mcp_lexoffice.server import send_invoice

    ctx = make_ctx({"get_invoice": {"id": "inv-1", "voucherStatus": "draft"}})
    result = await send_invoice(ctx, invoice_id="inv-1", recipient_email="test@test.de")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "draft" in parsed["error"].lower()
    assert "/edit/" in parsed["deepLink"]


async def test_send_invoice_finalized():
    from mcp_lexoffice.server import send_invoice

    ctx = make_ctx({
        "get_invoice": {"id": "inv-1", "voucherStatus": "open"},
        "send_invoice": None,
    })
    result = await send_invoice(ctx, invoice_id="inv-1", recipient_email="test@test.de")
    parsed = json.loads(result)
    assert parsed["status"] == "sent"
    assert parsed["recipient"] == "test@test.de"
    assert parsed["invoice_id"] == "inv-1"


async def test_send_invoice_paidoff_status_allowed():
    """Sending should work for any non-draft status (e.g. paidoff)."""
    from mcp_lexoffice.server import send_invoice

    ctx = make_ctx({
        "get_invoice": {"id": "inv-1", "voucherStatus": "paidoff"},
        "send_invoice": None,
    })
    result = await send_invoice(ctx, invoice_id="inv-1", recipient_email="x@y.de")
    parsed = json.loads(result)
    assert parsed["status"] == "sent"


async def test_get_invoice_tool_deep_link():
    from mcp_lexoffice.server import get_invoice

    ctx = make_ctx({"get_invoice": {"id": "inv-1", "voucherStatus": "draft"}})
    result = await get_invoice(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert "edit" in parsed["deepLink"]

    ctx2 = make_ctx({"get_invoice": {"id": "inv-2", "voucherStatus": "open"}})
    result2 = await get_invoice(ctx2, invoice_id="inv-2")
    parsed2 = json.loads(result2)
    assert "view" in parsed2["deepLink"]


async def test_get_invoice_tool_returns_valid_json():
    from mcp_lexoffice.server import get_invoice

    ctx = make_ctx({"get_invoice": {"id": "inv-1", "voucherStatus": "open", "totalAmount": 1500}})
    result = await get_invoice(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert parsed["totalAmount"] == 1500


async def test_get_invoice_pdf_tool():
    from mcp_lexoffice.server import get_invoice_pdf

    ctx = make_ctx({"render_invoice_document": {"documentFileId": "file-abc"}})
    result = await get_invoice_pdf(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert parsed["documentFileId"] == "file-abc"


async def test_list_invoices_with_overdue():
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v1", "voucherStatus": "open", "dueDate": "2020-01-01"},
            ]
        }
    })
    result = await list_invoices(ctx, status="open")
    parsed = json.loads(result)
    assert parsed["content"][0]["daysOverdue"] > 0
    assert "deepLink" in parsed["content"][0]


async def test_list_invoices_not_overdue():
    """An invoice with future due date should not have daysOverdue."""
    from mcp_lexoffice.server import list_invoices

    future = (date.today() + timedelta(days=30)).isoformat()
    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v2", "voucherStatus": "open", "dueDate": future},
            ]
        }
    })
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    assert "daysOverdue" not in parsed["content"][0]


async def test_list_invoices_draft_no_overdue():
    """Draft invoices should not get daysOverdue even with past due date."""
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v3", "voucherStatus": "draft", "dueDate": "2020-01-01"},
            ]
        }
    })
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    assert "daysOverdue" not in parsed["content"][0]


async def test_list_invoices_no_due_date():
    """Invoice without dueDate should not get daysOverdue."""
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v4", "voucherStatus": "open"},
            ]
        }
    })
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    assert "daysOverdue" not in parsed["content"][0]


async def test_list_invoices_invalid_due_date():
    """Invalid dueDate should not crash, just skip overdue calculation."""
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v5", "voucherStatus": "open", "dueDate": "not-a-date"},
            ]
        }
    })
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    assert "daysOverdue" not in parsed["content"][0]


async def test_list_invoices_deep_links_on_all():
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v1"},
                {"voucherId": "v2"},
            ]
        }
    })
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    for item in parsed["content"]:
        assert "deepLink" in item


async def test_list_invoices_empty():
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    result = await list_invoices(ctx)
    parsed = json.loads(result)
    assert parsed["content"] == []


async def test_list_invoices_passes_status_and_page():
    from mcp_lexoffice.server import list_invoices

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    await list_invoices(ctx, status="draft", page=3)
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_vouchers.call_args
    assert call_kwargs[0][0] == "salesinvoice"
    assert call_kwargs[1]["voucher_status"] == "draft"
    assert call_kwargs[1]["page"] == 3


# ── Voucher upload ───────────────────────────────────────────────────


async def test_upload_voucher_valid_pdf():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-1"}})
    content = base64.b64encode(b"fake-pdf-content").decode()
    result = await upload_voucher(ctx, file_content=content, file_name="bill.pdf")
    parsed = json.loads(result)
    assert parsed["id"] == "file-1"


async def test_upload_voucher_valid_png():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-png"}})
    content = base64.b64encode(b"png-data").decode()
    result = await upload_voucher(ctx, file_content=content, file_name="receipt.png")
    parsed = json.loads(result)
    assert parsed["id"] == "file-png"


async def test_upload_voucher_valid_jpg():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-jpg"}})
    content = base64.b64encode(b"jpg-data").decode()
    result = await upload_voucher(ctx, file_content=content, file_name="photo.jpg")
    parsed = json.loads(result)
    assert parsed["id"] == "file-jpg"


async def test_upload_voucher_valid_jpeg():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-jpeg"}})
    content = base64.b64encode(b"jpeg-data").decode()
    result = await upload_voucher(ctx, file_content=content, file_name="scan.jpeg")
    parsed = json.loads(result)
    assert parsed["id"] == "file-jpeg"


async def test_upload_voucher_bad_type():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({})
    result = await upload_voucher(ctx, file_content="abc", file_name="file.docx")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "Unsupported" in parsed["error"]


async def test_upload_voucher_bad_type_txt():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({})
    result = await upload_voucher(ctx, file_content="abc", file_name="notes.txt")
    parsed = json.loads(result)
    assert "error" in parsed


async def test_upload_voucher_bad_type_xlsx():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({})
    result = await upload_voucher(ctx, file_content="abc", file_name="data.xlsx")
    parsed = json.loads(result)
    assert "error" in parsed


async def test_upload_voucher_too_large():
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({})
    big = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
    result = await upload_voucher(ctx, file_content=big, file_name="huge.pdf")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "5MB" in parsed["error"]


async def test_upload_voucher_exactly_5mb():
    """File exactly at 5MB limit should be accepted."""
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-5mb"}})
    data = b"x" * (5 * 1024 * 1024)
    content = base64.b64encode(data).decode()
    result = await upload_voucher(ctx, file_content=content, file_name="exact.pdf")
    parsed = json.loads(result)
    assert parsed["id"] == "file-5mb"


async def test_upload_voucher_case_insensitive_extension():
    """Extension check should be case-insensitive (.PDF should work)."""
    from mcp_lexoffice.server import upload_voucher

    ctx = make_ctx({"upload_file": {"id": "file-upper"}})
    content = base64.b64encode(b"data").decode()
    result = await upload_voucher(ctx, file_content=content, file_name="BILL.PDF")
    parsed = json.loads(result)
    assert parsed["id"] == "file-upper"


# ── Financial overview ──────────────────────────────────────────────


async def test_get_financial_overview():
    from mcp_lexoffice.server import get_financial_overview

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherDate": "2026-01-15", "totalAmount": 3000},
                {"voucherDate": "2026-02-10", "totalAmount": 1500},
            ]
        }
    })
    result = await get_financial_overview(ctx, months=6)
    parsed = json.loads(result)
    assert "monthly" in parsed
    assert "open_invoices" in parsed
    assert "overdue_invoices" in parsed


async def test_get_financial_overview_empty():
    from mcp_lexoffice.server import get_financial_overview

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    result = await get_financial_overview(ctx, months=3)
    parsed = json.loads(result)
    assert parsed["monthly"] == []
    assert parsed["open_invoices"] == 0
    assert parsed["overdue_invoices"] == 0


async def test_get_financial_overview_monthly_grouping():
    """Sales and purchases in the same month should be grouped."""
    from mcp_lexoffice.server import get_financial_overview

    sales_data = {"content": [
        {"voucherDate": "2026-01-10", "totalAmount": 1000},
        {"voucherDate": "2026-01-20", "totalAmount": 2000},
    ]}
    purchases_data = {"content": [
        {"voucherDate": "2026-01-15", "totalAmount": 500},
    ]}
    open_data = {"content": []}

    mock_client = AsyncMock()
    # Three calls to filter_vouchers: sales (paidoff), purchases (paidoff), open
    mock_client.filter_vouchers.side_effect = [sales_data, purchases_data, open_data]
    ctx = FakeContext(mock_client)

    result = await get_financial_overview(ctx, months=6)
    parsed = json.loads(result)
    assert len(parsed["monthly"]) == 1
    month = parsed["monthly"][0]
    assert month["month"] == "2026-01"
    assert month["revenue"] == 3000.0
    assert month["expenses"] == 500.0
    assert month["net"] == 2500.0


async def test_get_financial_overview_months_limit():
    """Should only return the requested number of months."""
    from mcp_lexoffice.server import get_financial_overview

    sales_data = {"content": [
        {"voucherDate": "2026-01-10", "totalAmount": 100},
        {"voucherDate": "2026-02-10", "totalAmount": 200},
        {"voucherDate": "2026-03-10", "totalAmount": 300},
    ]}
    mock_client = AsyncMock()
    mock_client.filter_vouchers.side_effect = [
        sales_data,
        {"content": []},
        {"content": []},
    ]
    ctx = FakeContext(mock_client)

    result = await get_financial_overview(ctx, months=2)
    parsed = json.loads(result)
    assert len(parsed["monthly"]) == 2
    # Should be most recent first
    assert parsed["monthly"][0]["month"] == "2026-03"
    assert parsed["monthly"][1]["month"] == "2026-02"


async def test_get_financial_overview_overdue_count():
    from mcp_lexoffice.server import get_financial_overview

    past_date = (date.today() - timedelta(days=10)).isoformat()
    future_date = (date.today() + timedelta(days=10)).isoformat()
    open_data = {"content": [
        {"dueDate": past_date},
        {"dueDate": future_date},
        {"dueDate": past_date},
    ]}

    mock_client = AsyncMock()
    mock_client.filter_vouchers.side_effect = [
        {"content": []},
        {"content": []},
        open_data,
    ]
    ctx = FakeContext(mock_client)

    result = await get_financial_overview(ctx, months=1)
    parsed = json.loads(result)
    assert parsed["open_invoices"] == 3
    assert parsed["overdue_invoices"] == 2


# ── Payment status ──────────────────────────────────────────────────


async def test_get_payment_status_by_id():
    from mcp_lexoffice.server import get_payment_status

    ctx = make_ctx({"get_payments": {"openAmount": 500}})
    result = await get_payment_status(ctx, invoice_id="inv-1")
    parsed = json.loads(result)
    assert parsed["openAmount"] == 500


async def test_get_payment_status_no_params():
    from mcp_lexoffice.server import get_payment_status

    ctx = make_ctx({})
    result = await get_payment_status(ctx)
    parsed = json.loads(result)
    assert "error" in parsed


async def test_get_payment_status_by_contact_name():
    from mcp_lexoffice.server import get_payment_status

    mock_client = AsyncMock()
    mock_client.filter_vouchers.return_value = {
        "content": [
            {"voucherId": "v1", "voucherNumber": "RE-001", "contactName": "Acme GmbH", "totalAmount": 1000, "dueDate": "2026-01-01"},
            {"voucherId": "v2", "voucherNumber": "RE-002", "contactName": "Other Corp", "totalAmount": 500, "dueDate": "2026-02-01"},
        ]
    }
    mock_client.get_payments.return_value = {"openAmount": 1000}
    ctx = FakeContext(mock_client)

    result = await get_payment_status(ctx, contact_name="Acme")
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["contactName"] == "Acme GmbH"
    assert parsed[0]["payment"]["openAmount"] == 1000
    assert "deepLink" in parsed[0]


async def test_get_payment_status_by_contact_case_insensitive():
    from mcp_lexoffice.server import get_payment_status

    mock_client = AsyncMock()
    mock_client.filter_vouchers.return_value = {
        "content": [
            {"voucherId": "v1", "contactName": "Acme GmbH", "totalAmount": 100},
        ]
    }
    mock_client.get_payments.return_value = {"openAmount": 100}
    ctx = FakeContext(mock_client)

    result = await get_payment_status(ctx, contact_name="acme")
    parsed = json.loads(result)
    assert len(parsed) == 1


async def test_get_payment_status_by_contact_no_matches():
    from mcp_lexoffice.server import get_payment_status

    mock_client = AsyncMock()
    mock_client.filter_vouchers.return_value = {
        "content": [
            {"voucherId": "v1", "contactName": "Acme GmbH"},
        ]
    }
    ctx = FakeContext(mock_client)

    result = await get_payment_status(ctx, contact_name="Nonexistent")
    parsed = json.loads(result)
    assert parsed == []


async def test_get_payment_status_by_contact_payment_error():
    """If get_payments raises, should fall back to status: unknown."""
    from mcp_lexoffice.server import get_payment_status

    mock_client = AsyncMock()
    mock_client.filter_vouchers.return_value = {
        "content": [
            {"voucherId": "v1", "contactName": "Acme GmbH", "totalAmount": 100},
        ]
    }
    mock_client.get_payments.side_effect = Exception("API error")
    ctx = FakeContext(mock_client)

    result = await get_payment_status(ctx, contact_name="Acme")
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["payment"]["status"] == "unknown"


# ── Contact tools ────────────────────────────────────────────────────


async def test_create_contact_company():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-1"}})
    result = await create_contact(ctx, company_name="Acme GmbH", role="customer")
    parsed = json.loads(result)
    assert parsed["id"] == "c-1"
    assert "deepLink" in parsed
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert call_data["company"]["name"] == "Acme GmbH"
    assert "customer" in call_data["roles"]


async def test_create_contact_person():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-2"}})
    result = await create_contact(ctx, first_name="Max", last_name="Müller")
    parsed = json.loads(result)
    assert parsed["id"] == "c-2"
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert call_data["person"]["firstName"] == "Max"
    assert call_data["person"]["lastName"] == "Müller"


async def test_create_contact_person_first_name_only():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-3"}})
    result = await create_contact(ctx, first_name="Max")
    parsed = json.loads(result)
    assert parsed["id"] == "c-3"
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert call_data["person"]["firstName"] == "Max"
    assert "lastName" not in call_data["person"]


async def test_create_contact_no_name():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({})
    result = await create_contact(ctx)
    parsed = json.loads(result)
    assert "error" in parsed


async def test_create_contact_with_email():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-4"}})
    await create_contact(ctx, company_name="Test", email="info@test.de")
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert call_data["emailAddresses"]["business"] == ["info@test.de"]


async def test_create_contact_without_email():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-5"}})
    await create_contact(ctx, company_name="Test")
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert "emailAddresses" not in call_data


async def test_create_contact_with_address():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-6"}})
    await create_contact(
        ctx, company_name="Test", street="Hauptstr. 1", zip_code="10115", city="Berlin"
    )
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    billing = call_data["addresses"]["billing"][0]
    assert billing["street"] == "Hauptstr. 1"
    assert billing["zip"] == "10115"
    assert billing["city"] == "Berlin"
    assert billing["countryCode"] == "DE"


async def test_create_contact_vendor_role():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-7"}})
    await create_contact(ctx, company_name="Supplier", role="vendor")
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert "vendor" in call_data["roles"]


async def test_create_contact_version_zero():
    from mcp_lexoffice.server import create_contact

    ctx = make_ctx({"create_contact": {"id": "c-8"}})
    await create_contact(ctx, company_name="Test")
    call_data = ctx.lifespan_context["lexoffice"].create_contact.call_args[0][0]
    assert call_data["version"] == 0


async def test_search_contacts_role_filter_customer():
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({"filter_contacts": {"content": [], "totalElements": 0}})
    await search_contacts(ctx, role="customer")
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_contacts.call_args[1]
    assert call_kwargs["customer"] is True
    assert call_kwargs["vendor"] is None


async def test_search_contacts_role_filter_vendor():
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({"filter_contacts": {"content": [], "totalElements": 0}})
    await search_contacts(ctx, role="vendor")
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_contacts.call_args[1]
    assert call_kwargs["customer"] is None
    assert call_kwargs["vendor"] is True


async def test_search_contacts_role_filter_none():
    """No role filter should pass None for both customer and vendor."""
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({"filter_contacts": {"content": [], "totalElements": 0}})
    await search_contacts(ctx)
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_contacts.call_args[1]
    assert call_kwargs["customer"] is None
    assert call_kwargs["vendor"] is None


async def test_search_contacts_role_filter_both():
    """role='both' should pass None for both (no filter = both)."""
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({"filter_contacts": {"content": [], "totalElements": 0}})
    await search_contacts(ctx, role="both")
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_contacts.call_args[1]
    assert call_kwargs["customer"] is None
    assert call_kwargs["vendor"] is None


async def test_search_contacts_deep_links():
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({
        "filter_contacts": {
            "content": [{"id": "c-1"}, {"id": "c-2"}],
            "totalElements": 2,
        }
    })
    result = await search_contacts(ctx)
    parsed = json.loads(result)
    for item in parsed["content"]:
        assert "deepLink" in item
        assert "/contacts/" in item["deepLink"]


async def test_search_contacts_passes_name_and_email():
    from mcp_lexoffice.server import search_contacts

    ctx = make_ctx({"filter_contacts": {"content": [], "totalElements": 0}})
    await search_contacts(ctx, name="Müller", email="m@test.de")
    call_kwargs = ctx.lifespan_context["lexoffice"].filter_contacts.call_args[1]
    assert call_kwargs["name"] == "Müller"
    assert call_kwargs["email"] == "m@test.de"


async def test_get_contact_tool():
    from mcp_lexoffice.server import get_contact

    ctx = make_ctx({"get_contact": {"id": "c-1", "company": {"name": "Acme"}}})
    result = await get_contact(ctx, contact_id="c-1")
    parsed = json.loads(result)
    assert parsed["company"]["name"] == "Acme"
    assert "deepLink" in parsed
    assert "/contacts/c-1" in parsed["deepLink"]


async def test_update_contact_tool():
    from mcp_lexoffice.server import update_contact

    mock_client = AsyncMock()
    mock_client.get_contact.return_value = {
        "id": "c-1",
        "version": 1,
        "company": {"name": "Old Name"},
    }
    mock_client.update_contact.return_value = {"id": "c-1", "version": 2}
    ctx = FakeContext(mock_client)

    result = await update_contact(ctx, contact_id="c-1", version=2, company_name="New Name")
    parsed = json.loads(result)
    assert parsed["version"] == 2
    assert "deepLink" in parsed

    # Check that existing data was merged
    update_data = mock_client.update_contact.call_args[0][1]
    assert update_data["company"]["name"] == "New Name"
    assert update_data["version"] == 2


async def test_update_contact_person_fields():
    from mcp_lexoffice.server import update_contact

    mock_client = AsyncMock()
    mock_client.get_contact.return_value = {
        "id": "c-2",
        "version": 0,
        "person": {"firstName": "Max", "lastName": "Alt"},
    }
    mock_client.update_contact.return_value = {"id": "c-2", "version": 1}
    ctx = FakeContext(mock_client)

    await update_contact(ctx, contact_id="c-2", version=1, first_name="Moritz", last_name="Neu")
    update_data = mock_client.update_contact.call_args[0][1]
    assert update_data["person"]["firstName"] == "Moritz"
    assert update_data["person"]["lastName"] == "Neu"


async def test_update_contact_email():
    from mcp_lexoffice.server import update_contact

    mock_client = AsyncMock()
    mock_client.get_contact.return_value = {"id": "c-3", "version": 0}
    mock_client.update_contact.return_value = {"id": "c-3", "version": 1}
    ctx = FakeContext(mock_client)

    await update_contact(ctx, contact_id="c-3", version=1, email="new@test.de")
    update_data = mock_client.update_contact.call_args[0][1]
    assert update_data["emailAddresses"]["business"] == ["new@test.de"]


async def test_update_contact_no_changes():
    """Calling update without any optional params should still work."""
    from mcp_lexoffice.server import update_contact

    mock_client = AsyncMock()
    mock_client.get_contact.return_value = {"id": "c-4", "version": 0}
    mock_client.update_contact.return_value = {"id": "c-4", "version": 1}
    ctx = FakeContext(mock_client)

    result = await update_contact(ctx, contact_id="c-4", version=1)
    parsed = json.loads(result)
    assert parsed["version"] == 1


# ── Quotation tools ──────────────────────────────────────────────────


async def test_create_draft_quotation_tool():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-1"}})
    result = await create_draft_quotation(
        ctx,
        recipient_name="Test Client",
        line_items='[{"name": "Service", "unit_price": 500}]',
    )
    parsed = json.loads(result)
    assert parsed["id"] == "q-1"
    assert "deepLink" in parsed
    assert "/edit/" in parsed["deepLink"]


async def test_create_draft_quotation_with_expiration():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-2"}})
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        expiration_date="2026-04-01",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert call_data["expirationDate"] == "2026-04-01"


async def test_create_draft_quotation_without_expiration():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-3"}})
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert "expirationDate" not in call_data


async def test_create_draft_quotation_with_introduction_remark():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-4"}})
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
        introduction="Hello",
        remark="Thanks",
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert call_data["introduction"] == "Hello"
    assert call_data["remark"] == "Thanks"


async def test_create_draft_quotation_default_title():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-5"}})
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert call_data["title"] == "Angebot"


async def test_create_draft_quotation_vatfree():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-6"}})
    await create_draft_quotation(
        ctx,
        recipient_name="Test",
        line_items='[{"name": "A", "unit_price": 1}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert call_data["taxConditions"]["taxType"] == "vatfree"


async def test_finalize_quotation_tool():
    from mcp_lexoffice.server import finalize_quotation

    ctx = make_ctx({"finalize_quotation": {"id": "q-1", "voucherNumber": "AG-001"}})
    result = await finalize_quotation(ctx, quotation_id="q-1")
    parsed = json.loads(result)
    assert parsed["voucherNumber"] == "AG-001"
    assert "/view/" in parsed["deepLink"]


async def test_pursue_quotation_blocks_draft():
    from mcp_lexoffice.server import pursue_quotation_to_invoice

    ctx = make_ctx({"get_quotation": {"id": "q-1", "voucherStatus": "draft"}})
    result = await pursue_quotation_to_invoice(ctx, quotation_id="q-1")
    parsed = json.loads(result)
    assert "error" in parsed
    assert "draft" in parsed["error"].lower()
    assert "/edit/" in parsed["deepLink"]


async def test_pursue_quotation_finalized():
    from mcp_lexoffice.server import pursue_quotation_to_invoice

    mock_client = AsyncMock()
    mock_client.get_quotation.return_value = {"id": "q-1", "voucherStatus": "open"}
    mock_client.pursue_quotation.return_value = {"id": "inv-new"}
    ctx = FakeContext(mock_client)

    result = await pursue_quotation_to_invoice(ctx, quotation_id="q-1")
    parsed = json.loads(result)
    assert parsed["id"] == "inv-new"
    assert "/edit/" in parsed["deepLink"]


# ── Dunning tools ────────────────────────────────────────────────────


async def test_create_dunning_tool_with_note():
    from mcp_lexoffice.server import create_dunning

    ctx = make_ctx({"create_dunning": {"id": "d-1"}})
    result = await create_dunning(ctx, invoice_id="inv-1", note="Bitte zahlen")
    parsed = json.loads(result)
    assert parsed["id"] == "d-1"
    assert "deepLink" in parsed
    call_data = ctx.lifespan_context["lexoffice"].create_dunning.call_args[0][0]
    assert call_data["text"] == "Bitte zahlen"
    assert call_data["invoiceId"] == "inv-1"


async def test_create_dunning_tool_without_note():
    from mcp_lexoffice.server import create_dunning

    ctx = make_ctx({"create_dunning": {"id": "d-2"}})
    result = await create_dunning(ctx, invoice_id="inv-2")
    parsed = json.loads(result)
    assert parsed["id"] == "d-2"
    call_data = ctx.lifespan_context["lexoffice"].create_dunning.call_args[0][0]
    assert "text" not in call_data


async def test_render_dunning_pdf_tool():
    from mcp_lexoffice.server import render_dunning_pdf

    ctx = make_ctx({"render_dunning_document": {"documentFileId": "f-d"}})
    result = await render_dunning_pdf(ctx, dunning_id="d-1")
    parsed = json.loads(result)
    assert parsed["documentFileId"] == "f-d"


# ── Article tools ────────────────────────────────────────────────────


async def test_list_articles_tool():
    from mcp_lexoffice.server import list_articles

    ctx = make_ctx({"list_articles": {"content": [{"id": "a-1"}, {"id": "a-2"}]}})
    result = await list_articles(ctx)
    parsed = json.loads(result)
    assert len(parsed["content"]) == 2


async def test_create_article_tool():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "a-1"}})
    result = await create_article(ctx, name="Sprechstunde", net_price=995.0, unit_name="Pauschal")
    parsed = json.loads(result)
    assert parsed["id"] == "a-1"
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["price"]["taxRatePercentage"] == 0
    assert call_data["price"]["netPrice"] == 995.0
    assert call_data["price"]["currency"] == "EUR"
    assert call_data["unitName"] == "Pauschal"
    assert call_data["type"] == "SERVICE"


async def test_create_article_vatfree_default():
    """Tax rate should always be 0% (Kleinunternehmer)."""
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "a-2"}})
    await create_article(ctx, name="Test", net_price=100.0)
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["price"]["taxRatePercentage"] == 0


async def test_create_article_product_type():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "a-3"}})
    await create_article(ctx, name="Widget", net_price=50.0, article_type="PRODUCT")
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["type"] == "PRODUCT"


async def test_create_article_with_description():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "a-4"}})
    await create_article(ctx, name="Test", net_price=100.0, description="Detailed desc")
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["description"] == "Detailed desc"


async def test_create_article_without_description():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "a-5"}})
    await create_article(ctx, name="Test", net_price=100.0)
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert "description" not in call_data


async def test_get_article_tool():
    from mcp_lexoffice.server import get_article

    ctx = make_ctx({"get_article": {"id": "a-1", "title": "Consulting", "version": 3}})
    result = await get_article(ctx, article_id="a-1")
    parsed = json.loads(result)
    assert parsed["title"] == "Consulting"
    assert parsed["version"] == 3


async def test_update_article_tool():
    from mcp_lexoffice.server import update_article

    mock_client = AsyncMock()
    mock_client.get_article.return_value = {
        "id": "a-1",
        "version": 2,
        "title": "Old Name",
        "unitName": "Stück",
        "price": {"netPrice": 100, "currency": "EUR"},
    }
    mock_client.update_article.return_value = {"id": "a-1", "version": 3}
    ctx = FakeContext(mock_client)

    result = await update_article(ctx, article_id="a-1", version=3, name="New Name", net_price=200.0)
    parsed = json.loads(result)
    assert parsed["version"] == 3

    update_data = mock_client.update_article.call_args[0][1]
    assert update_data["title"] == "New Name"
    assert update_data["price"]["netPrice"] == 200.0
    assert update_data["version"] == 3


async def test_update_article_version_handling():
    """update_article should override version with the provided one."""
    from mcp_lexoffice.server import update_article

    mock_client = AsyncMock()
    mock_client.get_article.return_value = {"id": "a-1", "version": 5, "title": "X"}
    mock_client.update_article.return_value = {"id": "a-1", "version": 6}
    ctx = FakeContext(mock_client)

    await update_article(ctx, article_id="a-1", version=6)
    update_data = mock_client.update_article.call_args[0][1]
    assert update_data["version"] == 6


async def test_update_article_unit_name():
    from mcp_lexoffice.server import update_article

    mock_client = AsyncMock()
    mock_client.get_article.return_value = {"id": "a-1", "version": 0, "unitName": "Stück"}
    mock_client.update_article.return_value = {"id": "a-1", "version": 1}
    ctx = FakeContext(mock_client)

    await update_article(ctx, article_id="a-1", version=1, unit_name="Stunde")
    update_data = mock_client.update_article.call_args[0][1]
    assert update_data["unitName"] == "Stunde"


async def test_update_article_description():
    from mcp_lexoffice.server import update_article

    mock_client = AsyncMock()
    mock_client.get_article.return_value = {"id": "a-1", "version": 0}
    mock_client.update_article.return_value = {"id": "a-1", "version": 1}
    ctx = FakeContext(mock_client)

    await update_article(ctx, article_id="a-1", version=1, description="New desc")
    update_data = mock_client.update_article.call_args[0][1]
    assert update_data["description"] == "New desc"


# ── Voucher list ─────────────────────────────────────────────────────


async def test_list_vouchers_tool():
    from mcp_lexoffice.server import list_vouchers

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "v1", "voucherNumber": "RE-001"},
                {"voucherId": "v2", "voucherNumber": "RE-002"},
            ]
        }
    })
    result = await list_vouchers(ctx, voucher_type="salesinvoice")
    parsed = json.loads(result)
    assert len(parsed["content"]) == 2
    for item in parsed["content"]:
        assert "deepLink" in item


async def test_list_vouchers_passes_params():
    from mcp_lexoffice.server import list_vouchers

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    await list_vouchers(ctx, voucher_type="creditnote", status="open", page=2)
    call_args = ctx.lifespan_context["lexoffice"].filter_vouchers.call_args
    assert call_args[0][0] == "creditnote"
    assert call_args[1]["voucher_status"] == "open"
    assert call_args[1]["page"] == 2


async def test_list_vouchers_empty():
    from mcp_lexoffice.server import list_vouchers

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    result = await list_vouchers(ctx, voucher_type="quotation")
    parsed = json.loads(result)
    assert parsed["content"] == []


# ── Expenses ─────────────────────────────────────────────────────────


async def test_list_expenses_tool():
    from mcp_lexoffice.server import list_expenses

    ctx = make_ctx({
        "filter_vouchers": {
            "content": [
                {"voucherId": "e1", "totalAmount": 100},
            ]
        }
    })
    result = await list_expenses(ctx)
    parsed = json.loads(result)
    assert len(parsed["content"]) == 1
    assert "deepLink" in parsed["content"][0]


async def test_list_expenses_passes_purchaseinvoice():
    from mcp_lexoffice.server import list_expenses

    ctx = make_ctx({"filter_vouchers": {"content": []}})
    await list_expenses(ctx, status="open", page=1)
    call_args = ctx.lifespan_context["lexoffice"].filter_vouchers.call_args
    assert call_args[0][0] == "purchaseinvoice"
    assert call_args[1]["voucher_status"] == "open"
    assert call_args[1]["page"] == 1


# ── Payment conditions ──────────────────────────────────────────────


async def test_list_payment_conditions_tool():
    from mcp_lexoffice.server import list_payment_conditions

    ctx = make_ctx({"list_payment_conditions": [{"id": "pc-1"}]})
    result = await list_payment_conditions(ctx)
    parsed = json.loads(result)
    assert len(parsed) == 1


# ── Countries ────────────────────────────────────────────────────────


async def test_list_countries_tool():
    from mcp_lexoffice.server import list_countries

    ctx = make_ctx({"list_countries": [{"countryCode": "DE"}]})
    result = await list_countries(ctx)
    parsed = json.loads(result)
    assert parsed[0]["countryCode"] == "DE"


# ── main() transport ─────────────────────────────────────────────────


def test_main_defaults_to_streamable_http():
    with (
        patch.dict(os.environ, {}, clear=False),
        patch("mcp_lexoffice.server.mcp") as mock_mcp,
    ):
        os.environ.pop("MCP_TRANSPORT", None)
        from mcp_lexoffice.server import main

        main()
        mock_mcp.run.assert_called_once_with(
            transport="streamable-http", host="0.0.0.0", port=8000, json_response=True
        )


def test_main_custom_transport():
    with (
        patch.dict(os.environ, {"MCP_TRANSPORT": "stdio", "MCP_HOST": "127.0.0.1", "MCP_PORT": "9000"}),
        patch("mcp_lexoffice.server.mcp") as mock_mcp,
    ):
        from mcp_lexoffice.server import main

        main()
        mock_mcp.run.assert_called_once_with(
            transport="stdio", host="127.0.0.1", port=9000, json_response=True
        )


# ── All tools return valid JSON ─────────────────────────────────────


@pytest.mark.parametrize("tool_name", [
    "get_profile",
    "list_articles",
    "list_payment_conditions",
    "list_countries",
])
async def test_simple_tools_return_valid_json(tool_name):
    """Tools that just call client and format should return valid JSON."""
    import mcp_lexoffice.server as srv

    tool_fn = getattr(srv, tool_name)
    method_map = {
        "get_profile": "get_profile",
        "list_articles": "list_articles",
        "list_payment_conditions": "list_payment_conditions",
        "list_countries": "list_countries",
    }
    ctx = make_ctx({method_map[tool_name]: {"data": "test"}})
    result = await tool_fn(ctx)
    json.loads(result)  # should not raise


# ── _get_tax_config ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_tax_config_caches():
    """Second call should use cached value, not call get_profile again."""
    client = AsyncMock()
    client.get_profile.return_value = {"taxType": "net"}
    ctx = FakeContext(client)
    config1 = await _get_tax_config(ctx)
    config2 = await _get_tax_config(ctx)
    assert config1 is config2
    client.get_profile.assert_called_once()


@pytest.mark.anyio
async def test_get_payment_conditions_caches():
    """Second call should use cached value, not call list_payment_conditions again."""
    client = AsyncMock()
    client.list_payment_conditions.return_value = [
        {"id": "pc-1", "organizationDefault": True}
    ]
    ctx = FakeContext(client)
    first = await _get_payment_conditions(ctx)
    second = await _get_payment_conditions(ctx)
    assert first is second
    client.list_payment_conditions.assert_called_once()


@pytest.mark.anyio
async def test_get_payment_conditions_refresh_forces_reload():
    """refresh=True should bypass the cache and fetch again."""
    client = AsyncMock()
    client.list_payment_conditions.side_effect = [
        [{"id": "pc-1"}],
        [{"id": "pc-2"}],
    ]
    ctx = FakeContext(client)
    first = await _get_payment_conditions(ctx)
    second = await _get_payment_conditions(ctx, refresh=True)
    assert first == [{"id": "pc-1"}]
    assert second == [{"id": "pc-2"}]
    assert client.list_payment_conditions.call_count == 2
    # Cache must be updated after refresh
    assert ctx.lifespan_context["payment_conditions"] == [{"id": "pc-2"}]


@pytest.mark.anyio
async def test_get_tax_config_env_override():
    """LEXOFFICE_TAX_TYPE env var should skip the API call."""
    client = AsyncMock()
    ctx = FakeContext(client)
    with patch.dict(os.environ, {"LEXOFFICE_TAX_TYPE": "net"}):
        config = await _get_tax_config(ctx)
    assert config["tax_type"] == "net"
    assert config["default_rate"] == 19
    client.get_profile.assert_not_called()


@pytest.mark.anyio
async def test_get_tax_config_auto_detect():
    """Without env var, should read taxType from profile API."""
    client = AsyncMock()
    client.get_profile.return_value = {"taxType": "gross"}
    ctx = FakeContext(client)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("LEXOFFICE_TAX_TYPE", None)
        config = await _get_tax_config(ctx)
    assert config["tax_type"] == "gross"
    assert config["default_rate"] == 19
    client.get_profile.assert_called_once()


# ── _build_line_items with tax rates ────────────────────────────────


def test_build_line_items_default_rate_19():
    items = [{"name": "Consulting", "unit_price": 150}]
    result = _build_line_items(items, default_tax_rate=19)
    assert result[0]["unitPrice"]["taxRatePercentage"] == 19


def test_build_line_items_per_item_override():
    items = [{"name": "Books", "unit_price": 50, "tax_rate": 7}]
    result = _build_line_items(items, default_tax_rate=19)
    assert result[0]["unitPrice"]["taxRatePercentage"] == 7


# ── Multi-tax-regime tool tests ─────────────────────────────────────


@pytest.mark.anyio
async def test_create_draft_invoice_net_tax():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-net"}}, tax_type="net")
    result = await create_draft_invoice(
        ctx,
        recipient_name="Net GmbH",
        line_items='[{"name": "Consulting", "unit_price": 150}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["taxConditions"]["taxType"] == "net"
    assert call_data["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 19


@pytest.mark.anyio
async def test_create_draft_invoice_tax_rate_override():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-override"}}, tax_type="net")
    result = await create_draft_invoice(
        ctx,
        recipient_name="Books GmbH",
        line_items='[{"name": "Books", "unit_price": 50}]',
        tax_rate=7,
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["taxConditions"]["taxType"] == "net"
    assert call_data["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 7


@pytest.mark.anyio
async def test_create_draft_invoice_gross_tax():
    from mcp_lexoffice.server import create_draft_invoice

    ctx = make_ctx({"create_invoice": {"id": "inv-gross"}}, tax_type="gross")
    result = await create_draft_invoice(
        ctx,
        recipient_name="Gross GmbH",
        line_items='[{"name": "Service", "unit_price": 200}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_invoice.call_args[0][0]
    assert call_data["taxConditions"]["taxType"] == "gross"
    assert call_data["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 19


@pytest.mark.anyio
async def test_create_draft_quotation_net_tax():
    from mcp_lexoffice.server import create_draft_quotation

    ctx = make_ctx({"create_quotation": {"id": "q-net"}}, tax_type="net")
    result = await create_draft_quotation(
        ctx,
        recipient_name="Net GmbH",
        line_items='[{"name": "Consulting", "unit_price": 150}]',
    )
    call_data = ctx.lifespan_context["lexoffice"].create_quotation.call_args[0][0]
    assert call_data["taxConditions"]["taxType"] == "net"
    assert call_data["lineItems"][0]["unitPrice"]["taxRatePercentage"] == 19


@pytest.mark.anyio
async def test_create_article_net_tax():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "art-net"}}, tax_type="net")
    result = await create_article(ctx, name="Consulting", net_price=150)
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["price"]["taxRatePercentage"] == 19


@pytest.mark.anyio
async def test_create_article_tax_rate_override():
    from mcp_lexoffice.server import create_article

    ctx = make_ctx({"create_article": {"id": "art-7"}}, tax_type="net")
    result = await create_article(ctx, name="Books", net_price=50, tax_rate=7)
    call_data = ctx.lifespan_context["lexoffice"].create_article.call_args[0][0]
    assert call_data["price"]["taxRatePercentage"] == 7
