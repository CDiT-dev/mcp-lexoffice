"""MCP server for Lexware Office (formerly Lexoffice)."""

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastmcp import FastMCP, Context
from mcp.types import Icon

from .client import LexofficeClient

logger = logging.getLogger(__name__)

LEXOFFICE_UI = "https://app.lexoffice.de"


def _deep_link(resource_id: str, *, edit: bool = False) -> str:
    mode = "edit" if edit else "view"
    return f"{LEXOFFICE_UI}/#/voucher/{mode}/{resource_id}"


def _contact_link(contact_id: str) -> str:
    return f"{LEXOFFICE_UI}/#/contacts/{contact_id}"


@asynccontextmanager
async def lifespan(mcp: FastMCP):
    """Initialize the Lexoffice API client for the server's lifetime."""
    client = LexofficeClient()
    yield {"lexoffice": client}


from .auth import BearerTokenVerifier
from .config import get_settings

_settings = get_settings()
_api_key = _settings.mcp_api_key.get_secret_value()
if _settings.mcp_transport in ("http", "streamable-http") and not _api_key:
    raise SystemExit(
        "MCP_API_KEY is required in HTTP mode. Refusing to start "
        "an unauthenticated server."
    )
_auth = BearerTokenVerifier(api_key=_api_key) if _api_key else None

mcp = FastMCP(
    "Lexware Office",
    instructions=(
        "MCP server for Lexware Office (Lexoffice) — invoices, contacts, quotations, "
        "and accounting. Tax regime is auto-detected from the Lexoffice profile "
        "(vatfree, net, or gross). Default payment terms: Zahlbar sofort, rein netto. "
        "Service catalog: Digitale Sprechstunde (EUR 995 Pauschal), Consulting "
        "(EUR 150/Stunde), Platform Development (EUR 1200/Tag)."
    ),
    icons=[
        Icon(
            src="https://www.lexware.de/favicon.ico",
            mimeType="image/x-icon",
        ),
    ],
    lifespan=lifespan,
    auth=_auth,
)


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def _client(ctx: Context) -> LexofficeClient:
    return ctx.lifespan_context["lexoffice"]


DEFAULT_TAX_RATE = {"vatfree": 0, "net": 19, "gross": 19}


async def _get_tax_config(ctx: Context) -> dict:
    """Auto-detect tax regime from profile API, with env var override and lazy caching."""
    lc = ctx.lifespan_context
    if "tax_config" in lc:
        return lc["tax_config"]
    env_type = os.environ.get("LEXOFFICE_TAX_TYPE", "")
    if env_type in DEFAULT_TAX_RATE:
        tax_type = env_type
    else:
        profile = await _client(ctx).get_profile()
        tax_type = profile.get("taxType", "vatfree")
    config = {"tax_type": tax_type, "default_rate": DEFAULT_TAX_RATE.get(tax_type, 0)}
    lc["tax_config"] = config
    return config


def _build_line_items(items: list[dict], *, default_tax_rate: int = 0) -> list[dict]:
    """Convert simplified line items to Lexoffice format."""
    result = []
    for item in items:
        li: dict[str, Any] = {
            "type": "custom",
            "name": item["name"],
            "quantity": item.get("quantity", 1),
            "unitName": item.get("unit_name", "Stück"),
            "unitPrice": {
                "currency": item.get("currency", "EUR"),
                "netAmount": item["unit_price"],
                "taxRatePercentage": item.get("tax_rate", default_tax_rate),
            },
        }
        if "description" in item:
            li["description"] = item["description"]
        result.append(li)
    return result


def _build_address(
    recipient_name: str,
    street: str | None = None,
    zip_code: str | None = None,
    city: str | None = None,
    country_code: str = "DE",
) -> dict:
    addr: dict[str, str] = {"name": recipient_name, "countryCode": country_code}
    if street:
        addr["street"] = street
    if zip_code:
        addr["zip"] = zip_code
    if city:
        addr["city"] = city
    return addr


# ── Profile ──────────────────────────────────────────────────────────


@mcp.tool
async def get_profile(ctx: Context) -> str:
    """[finance] Get the Lexware Office organization profile — company name, tax settings, currency."""
    result = await _client(ctx).get_profile()
    return _fmt(result)


# ── Invoices ─────────────────────────────────────────────────────────


@mcp.tool
async def create_draft_invoice(
    ctx: Context,
    recipient_name: Annotated[str, "Company or person name for the invoice recipient"],
    line_items: Annotated[
        str,
        "JSON array of line items. Each: {name, unit_price, quantity?, unit_name?, description?, tax_rate?}",
    ],
    street: Annotated[str | None, "Recipient street address"] = None,
    zip_code: Annotated[str | None, "Recipient postal code"] = None,
    city: Annotated[str | None, "Recipient city"] = None,
    country_code: Annotated[str, "ISO country code"] = "DE",
    currency: Annotated[str, "Currency code"] = "EUR",
    payment_term_duration: Annotated[int | None, "Payment term in days (e.g. 14)"] = None,
    title: Annotated[str, "Invoice title"] = "Rechnung",
    introduction: Annotated[str | None, "Introduction text above line items"] = None,
    remark: Annotated[str | None, "Closing remark below line items"] = None,
    tax_rate: Annotated[int | None, "Override tax rate percentage for all line items"] = None,
) -> str:
    """[finance] Create a draft invoice in Lexware Office. Returns the invoice ID and a deep link to review it.

    Line items example: [{"name": "IT Consulting", "unit_price": 3000, "quantity": 1}]
    Tax regime is auto-detected from the Lexoffice profile. Override per-item via tax_rate field.
    """
    items = json.loads(line_items)
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": _build_address(recipient_name, street, zip_code, city, country_code),
        "lineItems": _build_line_items(items, default_tax_rate=effective_rate),
        "totalPrice": {"currency": currency},
        "taxConditions": {"taxType": tax_config["tax_type"]},
        "shippingConditions": {"shippingType": "none"},
        "title": title,
    }
    if introduction:
        data["introduction"] = introduction
    if remark:
        data["remark"] = remark
    if payment_term_duration:
        data["paymentConditions"] = {
            "paymentTermLabel": f"{payment_term_duration} Tage",
            "paymentTermDuration": payment_term_duration,
        }

    result = await _client(ctx).create_invoice(data)
    invoice_id = result.get("id", "")
    result["deepLink"] = _deep_link(invoice_id, edit=True)
    return _fmt(result)


@mcp.tool
async def finalize_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the draft invoice to finalize"],
) -> str:
    """[finance] Finalize a draft invoice — assigns an invoice number and makes it non-editable.
    Review the draft in Lexoffice UI before calling this. Cannot be undone."""
    result = await _client(ctx).finalize_invoice(invoice_id)
    result["deepLink"] = _deep_link(invoice_id)
    return _fmt(result)


@mcp.tool
async def delete_draft_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the draft invoice to delete"],
) -> str:
    """[finance] Delete a draft invoice. Only works on drafts — finalized invoices cannot be deleted."""
    invoice = await _client(ctx).get_invoice(invoice_id)
    status = invoice.get("voucherStatus", "")
    if status != "draft":
        return _fmt({"error": f"Cannot delete invoice with status '{status}'. Only drafts can be deleted.", "deepLink": _deep_link(invoice_id)})

    await _client(ctx).delete_invoice(invoice_id)
    return _fmt({"status": "deleted", "invoice_id": invoice_id})


@mcp.tool
async def send_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the finalized invoice"],
    recipient_email: Annotated[str, "Email address to send the invoice to"],
) -> str:
    """[finance] Send a finalized invoice by email. The invoice must be finalized first."""
    invoice = await _client(ctx).get_invoice(invoice_id)
    status = invoice.get("voucherStatus", "")
    if status == "draft":
        return _fmt({"error": "Invoice is still a draft. Finalize it first.", "deepLink": _deep_link(invoice_id, edit=True)})

    await _client(ctx).send_invoice(invoice_id, recipient_email)
    return _fmt({"status": "sent", "invoice_id": invoice_id, "recipient": recipient_email})


@mcp.tool
async def get_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the invoice"],
) -> str:
    """[finance] Get full details for a specific invoice, including a deep link to Lexoffice UI."""
    result = await _client(ctx).get_invoice(invoice_id)
    is_draft = result.get("voucherStatus") == "draft"
    result["deepLink"] = _deep_link(invoice_id, edit=is_draft)
    return _fmt(result)


@mcp.tool
async def get_invoice_pdf(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the finalized invoice"],
) -> str:
    """[finance] Render and get the document file ID for an invoice PDF. Invoice must be finalized."""
    result = await _client(ctx).render_invoice_document(invoice_id)
    return _fmt(result)


@mcp.tool
async def list_invoices(
    ctx: Context,
    status: Annotated[
        str | None,
        "Filter: draft, open, paid, paidoff, voided, overdue (comma-separated for multiple)",
    ] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List sales invoices, optionally filtered by status. Returns voucher number, contact, amount, and deep links."""
    result = await _client(ctx).filter_vouchers(
        "salesinvoice", voucher_status=status, page=page
    )
    today = date.today()
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
        due = item.get("dueDate")
        if due and item.get("voucherStatus") == "open":
            try:
                due_date = date.fromisoformat(due[:10])
                if due_date < today:
                    item["daysOverdue"] = (today - due_date).days
            except (ValueError, TypeError):
                pass
    return _fmt(result)


# ── Voucher Upload ───────────────────────────────────────────────────


@mcp.tool
async def upload_voucher(
    ctx: Context,
    file_content: Annotated[str, "Base64-encoded file content"],
    file_name: Annotated[str, "Original file name (e.g. 'invoice.pdf')"],
    voucher_type: Annotated[str, "Voucher type: purchaseinvoice, receipt, etc."] = "purchaseinvoice",
) -> str:
    """[finance] Upload a bill/receipt file to Lexoffice as a voucher for review.
    Accepts PDF, PNG, or JPG files up to 5MB. The file appears in Lexoffice 'Zu prüfen'."""
    allowed_ext = (".pdf", ".png", ".jpg", ".jpeg")
    if not any(file_name.lower().endswith(ext) for ext in allowed_ext):
        return _fmt({"error": f"Unsupported file type. Allowed: {', '.join(allowed_ext)}"})

    file_bytes = base64.b64decode(file_content)
    if len(file_bytes) > 5 * 1024 * 1024:
        return _fmt({"error": "File exceeds 5MB Lexoffice upload limit"})

    result = await _client(ctx).upload_file(file_bytes, file_name)
    return _fmt(result)


# ── Financial Queries ────────────────────────────────────────────────


@mcp.tool
async def list_expenses(
    ctx: Context,
    status: Annotated[str | None, "Filter: open, paid, paidoff, voided, overdue (comma-separated). Default: open,paid,paidoff,overdue"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List purchase invoices and expenses.

    Returns purchase invoices and expenses. For filtering by voucher type, use list_vouchers instead."""
    effective_status = status if status is not None else "open,paid,paidoff,overdue"
    result = await _client(ctx).filter_vouchers(
        "purchaseinvoice", voucher_status=effective_status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return _fmt(result)


@mcp.tool
async def get_financial_overview(
    ctx: Context,
    months: Annotated[int, "Number of months to include (default 6)"] = 6,
) -> str:
    """[finance] Get a monthly revenue/expense/net overview. Queries sales and purchase invoices.

    months defaults to 6, maximum 12. Large ranges may be slow as it queries all settled invoices."""
    sales = await _client(ctx).filter_vouchers("salesinvoice", voucher_status="paidoff", size=250)
    purchases = await _client(ctx).filter_vouchers("purchaseinvoice", voucher_status="paidoff", size=250)
    open_invoices = await _client(ctx).filter_vouchers("salesinvoice", voucher_status="open", size=250)

    today = date.today()
    monthly: dict[str, dict[str, float]] = {}

    for item in sales.get("content", []):
        voucher_date = item.get("voucherDate", "")[:7]
        if voucher_date:
            monthly.setdefault(voucher_date, {"revenue": 0, "expenses": 0})
            monthly[voucher_date]["revenue"] += item.get("totalAmount", 0)

    for item in purchases.get("content", []):
        voucher_date = item.get("voucherDate", "")[:7]
        if voucher_date:
            monthly.setdefault(voucher_date, {"revenue": 0, "expenses": 0})
            monthly[voucher_date]["expenses"] += item.get("totalAmount", 0)

    overview = []
    for month_key in sorted(monthly.keys(), reverse=True)[:months]:
        data = monthly[month_key]
        overview.append({
            "month": month_key,
            "revenue": round(data["revenue"], 2),
            "expenses": round(data["expenses"], 2),
            "net": round(data["revenue"] - data["expenses"], 2),
        })

    open_count = len(open_invoices.get("content", []))
    overdue_count = sum(
        1 for inv in open_invoices.get("content", [])
        if inv.get("dueDate") and date.fromisoformat(inv["dueDate"][:10]) < today
    )

    return _fmt({
        "monthly": overview,
        "open_invoices": open_count,
        "overdue_invoices": overdue_count,
    })


@mcp.tool
async def get_payment_status(
    ctx: Context,
    invoice_id: Annotated[str | None, "UUID of a specific invoice"] = None,
    contact_name: Annotated[str | None, "Search open invoices by contact name"] = None,
) -> str:
    """[finance] Check payment status for an invoice (by ID) or all open invoices for a contact."""
    if invoice_id:
        result = await _client(ctx).get_payments(invoice_id)
        return _fmt(result)

    if contact_name:
        vouchers = await _client(ctx).filter_vouchers("salesinvoice", voucher_status="open", size=250)
        matches = [
            v for v in vouchers.get("content", [])
            if contact_name.lower() in v.get("contactName", "").lower()
        ]
        results = []
        for v in matches:
            vid = v.get("voucherId", "")
            try:
                payment = await _client(ctx).get_payments(vid)
            except Exception:
                payment = {"status": "unknown"}
            results.append({
                "voucherId": vid,
                "voucherNumber": v.get("voucherNumber"),
                "contactName": v.get("contactName"),
                "totalAmount": v.get("totalAmount"),
                "dueDate": v.get("dueDate"),
                "payment": payment,
                "deepLink": _deep_link(vid),
            })
        return _fmt(results)

    return _fmt({"error": "Provide either invoice_id or contact_name"})


# ── Contacts ─────────────────────────────────────────────────────────


@mcp.tool
async def search_contacts(
    ctx: Context,
    name: Annotated[str | None, "Filter by name (min 3 chars)"] = None,
    email: Annotated[str | None, "Filter by email (min 3 chars)"] = None,
    role: Annotated[str | None, "Filter: customer, vendor, or both"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] Search and filter contacts in Lexware Office.

    Disambiguation: For accounting/invoice contacts → lexoffice. For CRM/chat contacts → watermelon."""
    customer = None
    vendor = None
    if role == "customer":
        customer = True
    elif role == "vendor":
        vendor = True
    result = await _client(ctx).filter_contacts(
        name=name, email=email, customer=customer, vendor=vendor, page=page
    )
    for item in result.get("content", []):
        cid = item.get("id", "")
        item["deepLink"] = _contact_link(cid)
    return _fmt(result)


@mcp.tool
async def get_contact(
    ctx: Context,
    contact_id: Annotated[str, "UUID of the contact"],
) -> str:
    """[finance] Get full details for a specific contact, including deep link."""
    result = await _client(ctx).get_contact(contact_id)
    result["deepLink"] = _contact_link(contact_id)
    return _fmt(result)


@mcp.tool
async def create_contact(
    ctx: Context,
    company_name: Annotated[str | None, "Company name (use this OR person fields)"] = None,
    first_name: Annotated[str | None, "Person first name"] = None,
    last_name: Annotated[str | None, "Person last name"] = None,
    role: Annotated[str, "Contact role: customer or vendor"] = "customer",
    email: Annotated[str | None, "Email address"] = None,
    street: Annotated[str | None, "Street address"] = None,
    zip_code: Annotated[str | None, "Postal code"] = None,
    city: Annotated[str | None, "City"] = None,
    country_code: Annotated[str, "ISO country code"] = "DE",
) -> str:
    """[finance] Create a new contact (company or person) in Lexware Office.

    Disambiguation: For accounting/invoice contacts → lexoffice. For CRM/chat contacts → watermelon."""
    data: dict[str, Any] = {"version": 0, "roles": {role: {}}}

    if company_name:
        data["company"] = {"name": company_name}
    elif first_name or last_name:
        data["person"] = {}
        if first_name:
            data["person"]["firstName"] = first_name
        if last_name:
            data["person"]["lastName"] = last_name
    else:
        return _fmt({"error": "Provide either company_name or first_name/last_name"})

    if email:
        data["emailAddresses"] = {"business": [email]}

    if street or zip_code or city:
        addr: dict[str, str] = {"countryCode": country_code}
        if street:
            addr["street"] = street
        if zip_code:
            addr["zip"] = zip_code
        if city:
            addr["city"] = city
        data["addresses"] = {"billing": [addr]}

    result = await _client(ctx).create_contact(data)
    contact_id = result.get("id", "")
    result["deepLink"] = _contact_link(contact_id)
    return _fmt(result)


@mcp.tool
async def update_contact(
    ctx: Context,
    contact_id: Annotated[str, "UUID of the contact"],
    version: Annotated[int, "Current version number (for optimistic locking)"],
    company_name: Annotated[str | None, "Updated company name"] = None,
    first_name: Annotated[str | None, "Updated person first name"] = None,
    last_name: Annotated[str | None, "Updated person last name"] = None,
    email: Annotated[str | None, "Updated email address"] = None,
) -> str:
    """[finance] Update an existing contact. Get the current version from get_contact first.

    Note: The Lexoffice API uses optimistic locking. Call get_contact first to obtain the current `version` field, then pass it here."""
    existing = await _client(ctx).get_contact(contact_id)
    existing["version"] = version

    if company_name and "company" in existing:
        existing["company"]["name"] = company_name
    if first_name and "person" in existing:
        existing["person"]["firstName"] = first_name
    if last_name and "person" in existing:
        existing["person"]["lastName"] = last_name
    if email:
        existing["emailAddresses"] = {"business": [email]}

    result = await _client(ctx).update_contact(contact_id, existing)
    result["deepLink"] = _contact_link(contact_id)
    return _fmt(result)


# ── Quotations ───────────────────────────────────────────────────────


@mcp.tool
async def create_draft_quotation(
    ctx: Context,
    recipient_name: Annotated[str, "Company or person name"],
    line_items: Annotated[str, "JSON array of line items: [{name, unit_price, quantity?, unit_name?, description?, tax_rate?}]"],
    street: Annotated[str | None, "Recipient street address"] = None,
    zip_code: Annotated[str | None, "Recipient postal code"] = None,
    city: Annotated[str | None, "Recipient city"] = None,
    country_code: Annotated[str, "ISO country code"] = "DE",
    currency: Annotated[str, "Currency code"] = "EUR",
    expiration_date: Annotated[str | None, "Quotation expiry date (ISO format)"] = None,
    title: Annotated[str, "Quotation title"] = "Angebot",
    introduction: Annotated[str | None, "Introduction text"] = None,
    remark: Annotated[str | None, "Closing remark"] = None,
    tax_rate: Annotated[int | None, "Override tax rate percentage for all line items"] = None,
) -> str:
    """[finance] Create a draft quotation (Angebot) in Lexware Office. Returns ID and deep link."""
    items = json.loads(line_items)
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": _build_address(recipient_name, street, zip_code, city, country_code),
        "lineItems": _build_line_items(items, default_tax_rate=effective_rate),
        "totalPrice": {"currency": currency},
        "taxConditions": {"taxType": tax_config["tax_type"]},
        "shippingConditions": {"shippingType": "none"},
        "title": title,
    }
    if introduction:
        data["introduction"] = introduction
    if remark:
        data["remark"] = remark
    if expiration_date:
        data["expirationDate"] = expiration_date

    result = await _client(ctx).create_quotation(data)
    qid = result.get("id", "")
    result["deepLink"] = _deep_link(qid, edit=True)
    return _fmt(result)


@mcp.tool
async def finalize_quotation(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the draft quotation"],
) -> str:
    """[finance] Finalize a quotation — assigns Angebotsnummer, makes it sendable."""
    result = await _client(ctx).finalize_quotation(quotation_id)
    result["deepLink"] = _deep_link(quotation_id)
    return _fmt(result)


@mcp.tool
async def pursue_quotation_to_invoice(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the finalized quotation"],
) -> str:
    """[finance] Convert a finalized quotation into a draft invoice (Angebot to Rechnung).
    The quotation must be finalized first."""
    quotation = await _client(ctx).get_quotation(quotation_id)
    status = quotation.get("voucherStatus", "")
    if status == "draft":
        return _fmt({"error": "Quotation is still a draft. Finalize it first.", "deepLink": _deep_link(quotation_id, edit=True)})

    result = await _client(ctx).pursue_quotation(quotation_id)
    invoice_id = result.get("id", "")
    result["deepLink"] = _deep_link(invoice_id, edit=True)
    return _fmt(result)


# ── Dunnings ─────────────────────────────────────────────────────────


@mcp.tool
async def create_dunning(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the overdue invoice"],
    note: Annotated[str | None, "Custom dunning text"] = None,
) -> str:
    """[finance] Create a payment reminder (Mahnung) for an overdue invoice."""
    data: dict[str, Any] = {"invoiceId": invoice_id}
    if note:
        data["text"] = note
    result = await _client(ctx).create_dunning(data)
    dunning_id = result.get("id", "")
    result["deepLink"] = _deep_link(dunning_id)
    return _fmt(result)


@mcp.tool
async def render_dunning_pdf(
    ctx: Context,
    dunning_id: Annotated[str, "UUID of the dunning"],
) -> str:
    """[finance] Render a dunning PDF and get the document file ID."""
    result = await _client(ctx).render_dunning_document(dunning_id)
    return _fmt(result)


# ── Articles ─────────────────────────────────────────────────────────


@mcp.tool
async def list_articles(ctx: Context) -> str:
    """[finance] List all configured service articles (reusable line items)."""
    result = await _client(ctx).list_articles()
    return _fmt(result)


@mcp.tool
async def create_article(
    ctx: Context,
    name: Annotated[str, "Article name (e.g. 'Digitale Sprechstunde')"],
    net_price: Annotated[float, "Net price in EUR"],
    unit_name: Annotated[str, "Unit: Stunde, Tag, Pauschal, Stück"] = "Stück",
    article_type: Annotated[str, "Type: SERVICE or PRODUCT"] = "SERVICE",
    description: Annotated[str | None, "Article description"] = None,
    tax_rate: Annotated[int | None, "Override tax rate percentage"] = None,
) -> str:
    """[finance] Create a reusable service article in Lexware Office. Tax rate is auto-detected from profile."""
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    data: dict[str, Any] = {
        "title": name,
        "type": article_type,
        "unitName": unit_name,
        "price": {
            "netPrice": net_price,
            "currency": "EUR",
            "taxRatePercentage": effective_rate,
        },
    }
    if description:
        data["description"] = description
    result = await _client(ctx).create_article(data)
    return _fmt(result)


@mcp.tool
async def get_article(
    ctx: Context,
    article_id: Annotated[str, "UUID of the article"],
) -> str:
    """[finance] Get full details for a specific article."""
    result = await _client(ctx).get_article(article_id)
    return _fmt(result)


@mcp.tool
async def update_article(
    ctx: Context,
    article_id: Annotated[str, "UUID of the article"],
    version: Annotated[int, "Current version number (for optimistic locking)"],
    name: Annotated[str | None, "Updated article name"] = None,
    net_price: Annotated[float | None, "Updated net price"] = None,
    unit_name: Annotated[str | None, "Updated unit name"] = None,
    description: Annotated[str | None, "Updated description"] = None,
) -> str:
    """[finance] Update an existing article. Get the current version from get_article first.

    Call get_article first to obtain the current `version` field."""
    existing = await _client(ctx).get_article(article_id)
    existing["version"] = version
    if name:
        existing["title"] = name
    if net_price is not None:
        existing.setdefault("price", {})["netPrice"] = net_price
    if unit_name:
        existing["unitName"] = unit_name
    if description is not None:
        existing["description"] = description
    result = await _client(ctx).update_article(article_id, existing)
    return _fmt(result)


# ── Voucher List (generic) ──────────────────────────────────────────


@mcp.tool
async def list_vouchers(
    ctx: Context,
    voucher_type: Annotated[
        str,
        "Type: salesinvoice, creditnote, orderconfirmation, quotation, deliverynote, "
        "downpaymentinvoice, purchaseinvoice, purchasecreditnote",
    ],
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List vouchers of a given type with optional status filter.

    For a simpler view of purchase invoices without type filtering, use list_expenses."""
    result = await _client(ctx).filter_vouchers(
        voucher_type, voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return _fmt(result)


@mcp.tool
async def get_voucher(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher"],
) -> str:
    """[finance] Get full details for a voucher (purchase invoice, receipt, etc.) including line items with tax amounts.

    Returns voucherItems with amount, taxAmount, and taxRatePercent per line item.
    Use this to inspect VAT (Vorsteuer) on purchase invoices."""
    result = await _client(ctx).get_voucher(voucher_id)
    result["deepLink"] = _deep_link(voucher_id)
    return _fmt(result)


@mcp.tool
async def update_voucher(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher"],
    version: Annotated[int, "Current version number (for optimistic locking — get from get_voucher first)"],
    voucher_items: Annotated[str, "JSON array of updated voucher items: [{amount, taxAmount, taxRatePercent, categoryId}]"],
) -> str:
    """[finance] Update a voucher's line items (e.g., to fix tax rates on purchase invoices).

    Get the current version and items from get_voucher first. Requires optimistic locking via version field.
    Common use: fix vatfree purchases to correct 19% MwSt rate."""
    import json as _json
    items = _json.loads(voucher_items)
    data = {"version": version, "voucherItems": items}
    result = await _client(ctx).update_voucher(voucher_id, data)
    return _fmt(result)


# ── Payment Conditions ───────────────────────────────────────────────


@mcp.tool
async def list_payment_conditions(ctx: Context) -> str:
    """[finance] List all configured payment conditions (Zahlungsbedingungen)."""
    result = await _client(ctx).list_payment_conditions()
    return _fmt(result)


# ── Utilities ────────────────────────────────────────────────────────


@mcp.tool
async def list_countries(ctx: Context) -> str:
    """[finance] List all countries with their tax classification (DE, intraCommunity, thirdPartyCountry)."""
    result = await _client(ctx).list_countries()
    return _fmt(result)


from datetime import datetime, timezone as _tz  # noqa: E402
from starlette.requests import Request as _SReq  # noqa: E402
from starlette.responses import JSONResponse as _SResp  # noqa: E402

_start_time = datetime.now(_tz.utc)
try:
    from mcp_lexoffice import __version__ as _version
except ImportError:
    _version = "0.1.0"


@mcp.custom_route("/health", methods=["GET"])
async def _health(request: _SReq) -> _SResp:
    return _SResp({
        "status": "healthy",
        "service": "mcp-lexoffice",
        "version": _version,
        "upstream_reachable": True,
        "uptime_seconds": int((datetime.now(_tz.utc) - _start_time).total_seconds()),
    })


@mcp.custom_route("/healthz", methods=["GET"])
async def _healthz(request: _SReq) -> _SResp:
    return await _health(request)


def main():
    import os

    transport = os.environ.get("MCP_TRANSPORT", "streamable-http")
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    # Normalize legacy "http" to "streamable-http"
    if transport == "http":
        transport = "streamable-http"

    if transport == "streamable-http":
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
        )
    else:
        mcp.run(transport=transport, host=host, port=port, json_response=True)


if __name__ == "__main__":
    main()
