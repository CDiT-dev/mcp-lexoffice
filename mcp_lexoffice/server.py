"""MCP server for Lexware Office (formerly Lexoffice)."""

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP, Context
from mcp.types import Icon

from .client import LexofficeClient

logger = logging.getLogger(__name__)

LEXOFFICE_UI = "https://app.lexoffice.de"


def _is_taxrate_rejection(exc: httpx.HTTPStatusError) -> bool:
    """True when Lexoffice rejected a voucher because the posting category does not
    accept the supplied ``taxRatePercent`` (HTTP 406, IssueList source ``taxRatePercent``,
    e.g. ``invalid_taxrate_19``).

    Some Buchungskonten are tax-restricted (reverse-charge / intra-community / 0%-only),
    so a generic default category may reject standard VAT rates. Detecting this lets
    create_voucher fall back to a universally-accepted 0% booking instead of failing the
    whole (often headless) run — the gross amount + vendor still persist for bank-matching,
    and the user re-books the VAT in 'Zu prüfen'."""
    resp = exc.response
    if resp is None or resp.status_code != 406:
        return False
    try:
        issues = resp.json().get("IssueList") or []
    except Exception:
        return False
    return any(isinstance(i, dict) and i.get("source") == "taxRatePercent" for i in issues)


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


async def _default_purchase_category_id(ctx: Context) -> str:
    """Resolve a sane default expense posting category (Buchungskonto) for purchase
    vouchers, lazily cached on the lifespan context.

    The Lexoffice docs only list travel categories for purchaseinvoice and recommend the
    posting-categories endpoint for the full set. We pick a contact-optional 'outgo'
    category, preferring a generic catch-all expense account; the user can re-book it in
    'Zu prüfen'. Callers may always override with an explicit category_id."""
    lc = ctx.lifespan_context
    if "default_purchase_category_id" in lc:
        return lc["default_purchase_category_id"]
    categories = await _client(ctx).list_posting_categories()
    outgo = [c for c in categories if c.get("type") == "outgo"]
    preferred = ("sonstige", "betriebsbedarf", "bürobedarf", "buerobedarf", "wareneingang", "wareneinkauf")
    chosen = None
    for keyword in preferred:
        for c in outgo:
            blob = f"{c.get('name', '')} {c.get('groupName', '')}".lower()
            if keyword in blob and not c.get("contactRequired", False):
                chosen = c
                break
        if chosen:
            break
    if chosen is None:
        chosen = next((c for c in outgo if not c.get("contactRequired", False)), None)
    if chosen is None:
        chosen = outgo[0] if outgo else None
    if chosen is None:
        raise RuntimeError("No expense (outgo) posting categories available on this account")
    category_id = chosen["id"]
    lc["default_purchase_category_id"] = category_id
    return category_id


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
        list[dict[str, Any]],
        "Line items. Each: {name, unit_price, quantity?, unit_name?, description?, tax_rate?}",
    ],
    contact_id: Annotated[str | None, "UUID of an existing Lexoffice contact (links invoice to contact record)"] = None,
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

    Use create_and_send_invoice instead if you want to create, finalize, and send in one step.
    Line items example: [{"name": "IT Consulting", "unit_price": 3000, "quantity": 1}]
    Tax regime is auto-detected from the Lexoffice profile. Override per-item via tax_rate field.
    """
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    address = {"contactId": contact_id} if contact_id else _build_address(recipient_name, street, zip_code, city, country_code)
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": address,
        "lineItems": _build_line_items(line_items, default_tax_rate=effective_rate),
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
    await _client(ctx).finalize_invoice(invoice_id)
    result = await _client(ctx).get_invoice(invoice_id)
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
    invoice_id: Annotated[str, "UUID of the invoice or voucher (IDs from list_invoices work here)"],
) -> str:
    """[finance] Get full details for a specific invoice, including a deep link to Lexoffice UI.

    Handles both Invoice API objects and bookkeeping vouchers (Belege) —
    tries /invoices/{id} first, falls back to /vouchers/{id} on 404."""
    result = await _client(ctx).get_invoice_or_voucher(invoice_id)
    resolved_via = result.pop("_resolvedVia", "invoices")
    is_draft = result.get("voucherStatus") == "draft"
    result["deepLink"] = _deep_link(invoice_id, edit=is_draft)
    if resolved_via == "vouchers":
        result["_note"] = (
            "This is a bookkeeping voucher (Beleg), not an Invoice API object. "
            "It was likely uploaded or imported, not created via the invoicing UI/API."
        )
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
        "Filter: draft, open, paid, paidoff, voided, overdue, unchecked "
        "(comma-separated for multiple). 'unchecked' = uploaded Belege awaiting review.",
    ] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List sales invoices, optionally filtered by status. Returns voucher number, contact, amount, and deep links.

    Note: This queries the voucherlist API which returns both Invoice API objects
    (created via UI/API) and bookkeeping vouchers (Belege). Items with status
    'unchecked' are uploaded documents, not proper invoices."""
    result = await _client(ctx).filter_vouchers(
        "salesinvoice,invoice", voucher_status=status, page=page
    )
    today = date.today()
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
        if item.get("voucherStatus") == "unchecked":
            item["_note"] = "Bookkeeping voucher (Beleg), not an Invoice API object"
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
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue, unchecked (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List purchase invoices and expenses."""
    result = await _client(ctx).filter_vouchers(
        "purchaseinvoice", voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return _fmt(result)


@mcp.tool
async def get_financial_overview(
    ctx: Context,
    months: Annotated[int, "Number of months to include (1-12, default 6)"] = 6,
) -> str:
    """[finance] Get a monthly revenue/expense/net overview. Queries sales and purchase invoices.

    months defaults to 6, maximum 12. Large ranges may be slow as it queries all settled invoices."""
    months = max(1, min(12, months))
    sales = await _client(ctx).filter_vouchers("salesinvoice,invoice", voucher_status="paidoff", size=250)
    purchases = await _client(ctx).filter_vouchers("purchaseinvoice", voucher_status="paidoff", size=250)
    open_invoices = await _client(ctx).filter_vouchers("salesinvoice,invoice", voucher_status="open", size=250)

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
        vouchers = await _client(ctx).filter_vouchers("salesinvoice,invoice", voucher_status="open", size=250)
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
    role: Annotated[Literal["customer", "vendor"], "Contact role"] = "customer",
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
    company_name: Annotated[str | None, "Updated company name"] = None,
    first_name: Annotated[str | None, "Updated person first name"] = None,
    last_name: Annotated[str | None, "Updated person last name"] = None,
    email: Annotated[str | None, "Updated email address"] = None,
) -> str:
    """[finance] Update an existing contact. Fetches current state and applies changes atomically."""
    existing = await _client(ctx).get_contact(contact_id)

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
    line_items: Annotated[
        list[dict[str, Any]],
        "Line items. Each: {name, unit_price, quantity?, unit_name?, description?, tax_rate?}",
    ],
    contact_id: Annotated[str | None, "UUID of an existing Lexoffice contact (links quotation to contact record)"] = None,
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
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    address = {"contactId": contact_id} if contact_id else _build_address(recipient_name, street, zip_code, city, country_code)
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": address,
        "lineItems": _build_line_items(line_items, default_tax_rate=effective_rate),
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
    await _client(ctx).finalize_quotation(quotation_id)
    result = await _client(ctx).get_quotation(quotation_id)
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
async def list_articles(
    ctx: Context,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
    size: Annotated[int, "Results per page (max 250)"] = 25,
) -> str:
    """[finance] List all configured service articles (reusable line items)."""
    result = await _client(ctx).list_articles(page=page, size=size)
    return _fmt(result)


@mcp.tool
async def create_article(
    ctx: Context,
    name: Annotated[str, "Article name (e.g. 'Digitale Sprechstunde')"],
    net_price: Annotated[float, "Net price in EUR"],
    unit_name: Annotated[str, "Unit: Stunde, Tag, Pauschal, Stück"] = "Stück",
    article_type: Annotated[Literal["SERVICE", "PRODUCT"], "Article type"] = "SERVICE",
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
    name: Annotated[str | None, "Updated article name"] = None,
    net_price: Annotated[float | None, "Updated net price"] = None,
    unit_name: Annotated[str | None, "Updated unit name"] = None,
    description: Annotated[str | None, "Updated description"] = None,
) -> str:
    """[finance] Update an existing article. Fetches current state and applies changes atomically."""
    existing = await _client(ctx).get_article(article_id)
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
        Literal[
            "invoice", "salesinvoice", "creditnote", "orderconfirmation",
            "quotation", "deliverynote", "downpaymentinvoice",
            "purchaseinvoice", "purchasecreditnote",
        ],
        "Voucher type to list",
    ],
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List vouchers of a given type with optional status filter.

    Prefer dedicated tools: list_invoices for salesinvoice, list_expenses for purchaseinvoice,
    list_quotations for quotation. Use this for other types (creditnote, orderconfirmation, etc.)."""
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


# ── Structured Voucher Creation (Belegfänger enrichment) ─────────────


@mcp.tool
async def list_posting_categories(
    ctx: Context,
    category_type: Annotated[
        Literal["income", "outgo", "all"],
        "Filter: income (revenue), outgo (expense), or all",
    ] = "all",
) -> str:
    """[finance] List posting categories (Buchungskonten) with their UUIDs.

    Use an 'outgo' (expense) category id as the category_id when creating purchase
    vouchers with create_voucher. Each entry includes contactRequired (whether a contact
    must be attached) and splitAllowed (whether mixed tax rates are permitted)."""
    categories = await _client(ctx).list_posting_categories()
    if category_type != "all":
        categories = [c for c in categories if c.get("type") == category_type]
    return _fmt(categories)


@mcp.tool
async def create_voucher(
    ctx: Context,
    total_amount: Annotated[float, "Total invoice amount. Interpreted per tax_type: a GROSS total (tax included) when tax_type='gross' (default), a NET total when tax_type='net'."],
    voucher_date: Annotated[str, "Issue date in yyyy-MM-dd format (e.g. 2026-05-29)"],
    contact_id: Annotated[str | None, "UUID of the vendor contact (resolve via find_or_create_contact). If omitted, the collective vendor contact is used."] = None,
    contact_name: Annotated[str | None, "Free-text vendor name shown on the voucher (useful with the collective contact when no contact_id is available)."] = None,
    tax_rate: Annotated[int, "VAT rate percent for the line (e.g. 0, 7, 19)"] = 19,
    tax_amount: Annotated[float | None, "Override the tax amount. If omitted, it is derived from total_amount and tax_rate."] = None,
    category_id: Annotated[str | None, "Posting category (Buchungskonto) UUID. If omitted, a default expense category is resolved automatically. Discover ids via list_posting_categories."] = None,
    tax_type: Annotated[Literal["gross", "net"], "Whether total_amount is gross (tax included) or net. Default gross. NOTE: Lexoffice rejects net vouchers with status 'unchecked' — use gross to land a voucher in 'Zu prüfen'."] = "gross",
    voucher_status: Annotated[Literal["unchecked", "open"], "unchecked → lands in 'Zu prüfen' (default). open → recorded as an open payable."] = "unchecked",
    voucher_number: Annotated[str | None, "Vendor's invoice/reference number"] = None,
    due_date: Annotated[str | None, "Payment due date in yyyy-MM-dd format"] = None,
    remark: Annotated[str | None, "Free-text remark (full-text searchable in Lexoffice)"] = None,
    file_content: Annotated[str | None, "Optional base64-encoded receipt file (PDF/PNG/JPG) to attach to the voucher in the same call."] = None,
    file_name: Annotated[str | None, "File name for file_content (e.g. 'invoice.pdf'). Required if file_content is given."] = None,
) -> str:
    """[finance] Create a structured purchase voucher (Beleg) with the amount, tax, and vendor pre-filled.

    Unlike upload_voucher (which lands a raw, un-OCR'd file in Beleg-Eingang), this creates a
    structured purchaseinvoice voucher so the booking amount + vendor persist — enabling
    Lexoffice's bank-matching to surface a payment-clearing suggestion. Optionally attaches the
    original receipt file and reads the voucher back to confirm enrichment stuck.

    Belegfänger flow: resolve the vendor (find_or_create_contact), then call this with the EUR
    amount, voucher_date, contact_id, and the rendered PDF as file_content.

    If the resolved posting category rejects the given tax_rate (tax-restricted accounts —
    e.g. reverse-charge foreign SaaS — return HTTP 406 invalid_taxrate), the voucher is
    re-booked at 0% VAT so it still lands with the amount + vendor; the response sets
    _enrichment.tax_rate_adjusted and a tax_note. Re-book the VAT during review."""
    if tax_type == "net" and voucher_status == "unchecked":
        return _fmt({
            "error": (
                "Lexoffice rejects net vouchers with status 'unchecked'. "
                "Use tax_type='gross' (recommended for receipts — the amount is already gross) "
                "or set voucher_status='open'."
            ),
        })
    if file_content and not file_name:
        return _fmt({"error": "file_name is required when file_content is provided"})

    rate = tax_rate
    if tax_type == "gross":
        gross = round(total_amount, 2)
        if tax_amount is not None:
            tax = round(tax_amount, 2)
        elif rate:
            net = round(gross / (1 + rate / 100), 2)
            tax = round(gross - net, 2)
        else:
            tax = 0.0
        item_amount = gross
        total_gross = gross
    else:  # net
        net = round(total_amount, 2)
        tax = round(tax_amount if tax_amount is not None else net * rate / 100, 2)
        item_amount = net
        total_gross = round(net + tax, 2)
    total_tax = tax

    data: dict[str, Any] = {
        "type": "purchaseinvoice",
        "voucherStatus": voucher_status,
        "voucherDate": voucher_date,
        "totalGrossAmount": total_gross,
        "totalTaxAmount": total_tax,
        "taxType": tax_type,
        "voucherItems": [
            {
                "amount": item_amount,
                "taxAmount": tax,
                "taxRatePercent": rate,
                "categoryId": category_id or await _default_purchase_category_id(ctx),
            }
        ],
    }
    if voucher_number:
        data["voucherNumber"] = voucher_number
    if due_date:
        data["dueDate"] = due_date
    if remark:
        data["remark"] = remark
    if contact_id:
        data["useCollectiveContact"] = False
        data["contactId"] = contact_id
    else:
        data["useCollectiveContact"] = True
    if contact_name:
        data["contactName"] = contact_name

    tax_rate_adjusted = False
    try:
        created = await _client(ctx).create_voucher(data)
    except httpx.HTTPStatusError as exc:
        # The resolved posting category rejected this VAT rate (tax-restricted account,
        # e.g. reverse-charge foreign SaaS). Re-book at 0% — universally accepted — so the
        # voucher still lands with the gross amount + vendor for bank-matching. The user
        # corrects the VAT in 'Zu prüfen'.
        if rate and _is_taxrate_rejection(exc):
            rate = 0
            item = data["voucherItems"][0]
            item["taxAmount"] = 0.0
            item["taxRatePercent"] = 0
            data["totalTaxAmount"] = 0.0
            # With no tax, gross == the booked line amount (matches Lexoffice's own
            # 0%-reverse-charge vouchers, where totalGrossAmount == amount).
            data["totalGrossAmount"] = item["amount"]
            created = await _client(ctx).create_voucher(data)
            tax_rate_adjusted = True
        else:
            raise
    voucher_id = created.get("id", "")

    attachment_error = None
    if file_content and voucher_id:
        allowed_ext = (".pdf", ".png", ".jpg", ".jpeg")
        if not any(file_name.lower().endswith(ext) for ext in allowed_ext):
            attachment_error = f"Unsupported file type. Allowed: {', '.join(allowed_ext)}"
        else:
            file_bytes = base64.b64decode(file_content)
            if len(file_bytes) > 5 * 1024 * 1024:
                attachment_error = "File exceeds 5MB Lexoffice upload limit"
            else:
                await _client(ctx).attach_voucher_file(voucher_id, file_bytes, file_name)

    # Read back to confirm enrichment persisted (the failure mode CDI-1164 was opened for).
    persisted = await _client(ctx).get_voucher(voucher_id) if voucher_id else created
    result: dict[str, Any] = {
        "id": voucher_id,
        "voucherStatus": persisted.get("voucherStatus"),
        "voucherNumber": persisted.get("voucherNumber"),
        "totalGrossAmount": persisted.get("totalGrossAmount"),
        "totalTaxAmount": persisted.get("totalTaxAmount"),
        "taxType": persisted.get("taxType"),
        "contactId": persisted.get("contactId"),
        "contactName": persisted.get("contactName"),
        "useCollectiveContact": persisted.get("useCollectiveContact"),
        "files": persisted.get("files", []),
        "version": persisted.get("version"),
        "deepLink": _deep_link(voucher_id),
        "_enrichment": {
            "amount_persisted": bool(persisted.get("totalGrossAmount")),
            "contact_attached": bool(persisted.get("contactId")) or bool(persisted.get("useCollectiveContact")),
            "file_attached": bool(persisted.get("files")),
        },
    }
    if tax_rate_adjusted:
        result["_enrichment"]["tax_rate_adjusted"] = True
        result["tax_note"] = (
            f"Posting category rejected {tax_rate}% VAT — booked at 0% so the voucher "
            "could be created. Re-book the VAT in 'Zu prüfen' if input tax is reclaimable."
        )
    if attachment_error:
        result["_enrichment"]["file_attached"] = False
        result["attachment_error"] = attachment_error
    return _fmt(result)


@mcp.tool
async def attach_voucher_file(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher to attach the file to"],
    file_content: Annotated[str, "Base64-encoded file content (PDF, PNG, or JPG, max 5MB)"],
    file_name: Annotated[str, "Original file name (e.g. 'invoice.pdf')"],
) -> str:
    """[finance] Attach a receipt/invoice file (the original Beleg) to an existing voucher.

    Use after create_voucher when you need to attach the file separately. POST to
    /v1/vouchers/{id}/files. Returns the file id and voucher id."""
    allowed_ext = (".pdf", ".png", ".jpg", ".jpeg")
    if not any(file_name.lower().endswith(ext) for ext in allowed_ext):
        return _fmt({"error": f"Unsupported file type. Allowed: {', '.join(allowed_ext)}"})

    file_bytes = base64.b64decode(file_content)
    if len(file_bytes) > 5 * 1024 * 1024:
        return _fmt({"error": "File exceeds 5MB Lexoffice upload limit"})

    result = await _client(ctx).attach_voucher_file(voucher_id, file_bytes, file_name)
    result["deepLink"] = _deep_link(voucher_id)
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


# ── Recurring Templates ────────────────────────────────────────────


@mcp.tool
async def list_recurring_templates(ctx: Context) -> str:
    """[finance] List all recurring invoice templates (Abo-Rechnungen).

    Shows scheduled invoices that are automatically created on a recurring basis.
    Each template contains the line items, recipient, schedule, and next execution date."""
    result = await _client(ctx).list_recurring_templates()
    if isinstance(result, list):
        for item in result:
            tid = item.get("id", "")
            item["deepLink"] = f"{LEXOFFICE_UI}/#/permalink/recurring-templates/view/{tid}"
    return _fmt(result)


@mcp.tool
async def get_recurring_template(
    ctx: Context,
    template_id: Annotated[str, "UUID of the recurring template"],
) -> str:
    """[finance] Get full details for a recurring invoice template, including schedule, line items, and next execution date."""
    result = await _client(ctx).get_recurring_template(template_id)
    result["deepLink"] = f"{LEXOFFICE_UI}/#/permalink/recurring-templates/view/{template_id}"
    return _fmt(result)


# ── Capability Tools (composite workflows) ─────────────────────────


@mcp.tool
async def create_and_send_invoice(
    ctx: Context,
    recipient_name: Annotated[str, "Company or person name for the invoice recipient"],
    recipient_email: Annotated[str, "Email address to send the invoice to"],
    line_items: Annotated[
        list[dict[str, Any]],
        "Line items. Each: {name, unit_price, quantity?, unit_name?, description?, tax_rate?}",
    ],
    contact_id: Annotated[str | None, "UUID of an existing Lexoffice contact (links invoice to contact record)"] = None,
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
    """[finance] Create, finalize, and send an invoice in one step.

    Use when the user says 'send an invoice to X for Y'. Creates a draft, finalizes it
    (assigns invoice number), and emails it to the recipient. For drafts that need
    review first, use create_draft_invoice instead."""
    tax_config = await _get_tax_config(ctx)
    effective_rate = tax_rate if tax_rate is not None else tax_config["default_rate"]
    address = {"contactId": contact_id} if contact_id else _build_address(recipient_name, street, zip_code, city, country_code)
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": address,
        "lineItems": _build_line_items(line_items, default_tax_rate=effective_rate),
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

    created = await _client(ctx).create_invoice(data)
    invoice_id = created.get("id", "")
    await _client(ctx).finalize_invoice(invoice_id)
    await _client(ctx).send_invoice(invoice_id, recipient_email)
    invoice = await _client(ctx).get_invoice(invoice_id)
    invoice["deepLink"] = _deep_link(invoice_id)
    return _fmt({
        "status": "sent",
        "invoice_id": invoice_id,
        "voucherNumber": invoice.get("voucherNumber"),
        "recipient": recipient_email,
        "totalAmount": invoice.get("totalPrice", {}).get("totalNetAmount"),
        "deepLink": invoice["deepLink"],
    })


@mcp.tool
async def find_or_create_contact(
    ctx: Context,
    name: Annotated[str, "Company or person name to search for"],
    role: Annotated[Literal["customer", "vendor"], "Contact role"] = "customer",
    email: Annotated[str | None, "Email address (used for creation if contact not found)"] = None,
    first_name: Annotated[str | None, "Person first name (if not a company)"] = None,
    last_name: Annotated[str | None, "Person last name (if not a company)"] = None,
) -> str:
    """[finance] Find a contact by name or create one if it doesn't exist. Returns the contact ID.

    Searches by name first. If exactly one match is found, returns it.
    If no match, creates a new contact. If multiple matches, returns them all for disambiguation."""
    results = await _client(ctx).filter_contacts(name=name)
    matches = results.get("content", [])

    if len(matches) == 1:
        contact = matches[0]
        contact["deepLink"] = _contact_link(contact.get("id", ""))
        contact["_action"] = "found_existing"
        return _fmt(contact)

    if len(matches) > 1:
        for c in matches:
            c["deepLink"] = _contact_link(c.get("id", ""))
        return _fmt({"_action": "multiple_matches", "message": f"Found {len(matches)} contacts matching '{name}'. Pick one or refine the search.", "contacts": matches})

    data: dict[str, Any] = {"version": 0, "roles": {role: {}}}
    if first_name or last_name:
        person: dict[str, str] = {}
        if first_name:
            person["firstName"] = first_name
        if last_name:
            person["lastName"] = last_name
        data["person"] = person
    else:
        data["company"] = {"name": name}
    if email:
        data["emailAddresses"] = {"business": [email]}

    result = await _client(ctx).create_contact(data)
    contact_id = result.get("id", "")
    result["deepLink"] = _contact_link(contact_id)
    result["_action"] = "created_new"
    return _fmt(result)


@mcp.tool
async def convert_quotation_and_send(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the finalized quotation"],
    recipient_email: Annotated[str, "Email address to send the invoice to"],
) -> str:
    """[finance] Convert an accepted quotation into an invoice and send it in one step.

    The quotation must be finalized first. Creates a draft invoice from the quotation,
    finalizes it, and sends it by email."""
    quotation = await _client(ctx).get_quotation(quotation_id)
    status = quotation.get("voucherStatus", "")
    if status == "draft":
        return _fmt({"error": "Quotation is still a draft. Finalize it first.", "deepLink": _deep_link(quotation_id, edit=True)})

    pursued = await _client(ctx).pursue_quotation(quotation_id)
    invoice_id = pursued.get("id", "")
    await _client(ctx).finalize_invoice(invoice_id)
    await _client(ctx).send_invoice(invoice_id, recipient_email)
    invoice = await _client(ctx).get_invoice(invoice_id)
    invoice["deepLink"] = _deep_link(invoice_id)
    return _fmt({
        "status": "sent",
        "invoice_id": invoice_id,
        "voucherNumber": invoice.get("voucherNumber"),
        "recipient": recipient_email,
        "quotation_id": quotation_id,
        "deepLink": invoice["deepLink"],
    })


@mcp.tool
async def list_quotations(
    ctx: Context,
    status: Annotated[str | None, "Filter: draft, open, accepted, rejected, voided (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> str:
    """[finance] List quotations (Angebote), optionally filtered by status."""
    result = await _client(ctx).filter_vouchers(
        "quotation", voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return _fmt(result)


@mcp.tool
async def get_contact_invoices(
    ctx: Context,
    contact_name: Annotated[str | None, "Contact name to search for"] = None,
    contact_id: Annotated[str | None, "UUID of the contact (preferred over name)"] = None,
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue (comma-separated)"] = None,
) -> str:
    """[finance] List all invoices for a specific contact. Search by name or provide a contact ID directly.

    Use when the user asks 'show all invoices for Acme GmbH' or 'what did we bill StoryKeep?'."""
    if not contact_id and not contact_name:
        return _fmt({"error": "Provide either contact_id or contact_name"})

    if not contact_id:
        contacts = await _client(ctx).filter_contacts(name=contact_name)
        matches = contacts.get("content", [])
        if not matches:
            return _fmt({"error": f"No contact found matching '{contact_name}'"})
        if len(matches) > 1:
            return _fmt({
                "error": f"Multiple contacts match '{contact_name}'. Provide a contact_id.",
                "contacts": [{"id": c.get("id"), "name": c.get("company", {}).get("name") or f"{c.get('person', {}).get('firstName', '')} {c.get('person', {}).get('lastName', '')}".strip()} for c in matches],
            })
        contact_id = matches[0].get("id", "")

    result = await _client(ctx).filter_vouchers(
        "salesinvoice,invoice", voucher_status=status, contact_id=contact_id
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


@mcp.tool
async def create_credit_note(
    ctx: Context,
    recipient_name: Annotated[str, "Company or person name"],
    line_items: Annotated[
        list[dict[str, Any]],
        "Line items. Each: {name, unit_price, quantity?, unit_name?, description?, tax_rate?}",
    ],
    preceding_invoice_id: Annotated[str | None, "UUID of the original invoice this corrects"] = None,
    contact_id: Annotated[str | None, "UUID of an existing Lexoffice contact"] = None,
    street: Annotated[str | None, "Recipient street address"] = None,
    zip_code: Annotated[str | None, "Recipient postal code"] = None,
    city: Annotated[str | None, "Recipient city"] = None,
    country_code: Annotated[str, "ISO country code"] = "DE",
    title: Annotated[str, "Credit note title"] = "Gutschrift",
    introduction: Annotated[str | None, "Introduction text"] = None,
    remark: Annotated[str | None, "Closing remark"] = None,
    finalize: Annotated[bool, "Finalize immediately (assigns number, cannot be undone)"] = False,
) -> str:
    """[finance] Create a credit note (Gutschrift) for a correction or refund.

    Optionally link to the original invoice via preceding_invoice_id."""
    tax_config = await _get_tax_config(ctx)
    address = {"contactId": contact_id} if contact_id else _build_address(recipient_name, street, zip_code, city, country_code)
    data: dict[str, Any] = {
        "voucherDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "address": address,
        "lineItems": _build_line_items(line_items, default_tax_rate=tax_config["default_rate"]),
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": tax_config["tax_type"]},
        "title": title,
    }
    if introduction:
        data["introduction"] = introduction
    if remark:
        data["remark"] = remark

    result = await _client(ctx).create_credit_note(
        data, finalize=finalize, preceding_id=preceding_invoice_id
    )
    cn_id = result.get("id", "")
    result["deepLink"] = _deep_link(cn_id, edit=not finalize)
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
