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
from fastmcp.exceptions import ToolError
from mcp.types import Icon, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_serializer

from mcp_lexoffice.client import LexofficeClient

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


from mcp_lexoffice.auth import BearerTokenVerifier
from mcp_lexoffice.config import get_settings

_settings = get_settings()
_api_key = _settings.mcp_api_key.get_secret_value()
if _settings.mcp_transport in ("http", "streamable-http") and not _api_key:
    raise SystemExit(
        "MCP_API_KEY is required in HTTP mode. Refusing to start "
        "an unauthenticated server."
    )
_auth = BearerTokenVerifier(api_key=_api_key) if _api_key else None

SERVER_INSTRUCTIONS = (
    "MCP server for Lexware Office (Lexoffice) — German invoicing, contacts, quotations, "
    "vouchers (Belege), and accounting via the official REST API.\n\n"
    "Tax regime is auto-detected from the Lexoffice profile (vatfree = Kleinunternehmer 0%, "
    "net = 19%, gross = 19%). Default payment terms: Zahlbar sofort, rein netto.\n\n"
    "Choosing a tool:\n"
    "- One-shot billing ('send an invoice to X for Y'): create_and_send_invoice. For drafts "
    "that need review first: create_draft_invoice, then finalize_invoice + send_invoice.\n"
    "- Quotations (Angebote): create_draft_quotation -> finalize_quotation -> "
    "pursue_quotation_to_invoice, or convert_quotation_and_send in one step.\n"
    "- Listing: list_invoices (sales), list_expenses (purchases), list_quotations; "
    "list_vouchers only for other types (creditnote, orderconfirmation, ...).\n"
    "- Receipt capture (Belegfänger): find_or_create_contact -> create_voucher (structured, "
    "amount + vendor + optional PDF attach). Use upload_voucher only to drop a raw file in "
    "Beleg-Eingang without structuring it.\n"
    "- Contacts: find_or_create_contact for idempotent resolve-or-create; search_contacts to "
    "browse; create_contact/update_contact for explicit CRUD. For accounting/invoice contacts "
    "use this server; for CRM/chat contacts use watermelon.\n"
    "- Money owed: get_payment_status, get_contact_invoices, create_dunning (Mahnung) for "
    "overdue invoices.\n\n"
    "Irreversible actions (finalize_invoice, finalize_quotation, send_invoice, "
    "create_and_send_invoice, convert_quotation_and_send) assign numbers / email customers and "
    "cannot be undone — confirm intent before calling. Reference data is also exposed as "
    "resources under the lexoffice:// scheme (countries, posting-categories, payment-conditions, "
    "service-catalog, status).\n\n"
    "Service catalog: Digitale Sprechstunde (EUR 995 Pauschal), Consulting (EUR 150/Stunde), "
    "Platform Development (EUR 1200/Tag)."
)

mcp = FastMCP(
    "Lexware Office",
    instructions=SERVER_INSTRUCTIONS,
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


# ── Structured output models ─────────────────────────────────────────
# These give fastmcp an output_schema for the highest-value composite/aggregate
# tools so clients receive machine-validated structured content alongside the
# human-readable JSON. Top-level field names mirror the previous dict payloads.


class MonthlyFinancials(BaseModel):
    """Revenue, expenses, and net for a single calendar month."""

    month: str = Field(description="Calendar month, YYYY-MM")
    revenue: float = Field(description="Settled (paidoff) sales total for the month, EUR")
    expenses: float = Field(description="Settled (paidoff) purchase total for the month, EUR")
    net: float = Field(description="revenue - expenses, EUR")


class FinancialOverview(BaseModel):
    """Aggregated monthly revenue/expense/net overview plus open-invoice counts."""

    monthly: list[MonthlyFinancials] = Field(
        description="Per-month figures, newest first, capped to the requested window"
    )
    open_invoices: int = Field(description="Count of open (unpaid, finalized) sales invoices")
    overdue_invoices: int = Field(description="Subset of open_invoices whose dueDate has passed")
    truncated: bool = Field(
        default=False,
        description="True if any underlying voucher page hit the 250-row cap and figures "
        "may exclude older settled invoices",
    )


# ── Reusable domain output models ────────────────────────────────────
# Every model below is *permissive by construction* so a single schema validates
# BOTH the success payload AND the error payload a tool may emit:
#   - extra="allow"            → unknown/passthrough Lexoffice API keys are preserved verbatim
#   - populate_by_name=True    → validation accepts both the snake field name and its camel alias
#   - serialization_alias=...  → fastmcp serializes camelCase wire keys (the format clients/the
#                                Cloudflare portal already depend on); never rename the wire key
#   - error: str | None        → the {"error": "..."} short-circuit payloads validate too
#   - every field Optional w/ default & list fields default_factory=list → nothing is required
# This is the exact shape that avoids the mcp-zernio output-schema bug (a model that only
# validated the happy path and exploded on the error payload).


class LexofficeBase(BaseModel):
    """Permissive base for every typed tool return.

    Declared fields document the salient top-level keys; ``extra="allow"`` carries the rest
    of the (large, type-dependent) Lexoffice object graph unchanged. An optional ``error`` is
    always present so the error payload validates against the same schema as the success one.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    error: str | None = Field(default=None, description="Set only on the error payload; null on success")
    deepLink: str | None = Field(default=None, description="Deep link into the Lexware Office UI")


def _model(cls: type[BaseModel], payload: dict[str, Any]) -> BaseModel:
    """Validate a (camelCase) Lexoffice/handler dict into a typed model, preserving extras."""
    return cls.model_validate(payload)


class LexofficeListItem(BaseModel):
    """Permissive base for the *array-element* models behind the four bare-JSON-array tools
    (list_countries / list_posting_categories / list_payment_conditions /
    list_recurring_templates).

    Same validate-both-paths guarantees as ``LexofficeBase`` (extra="allow",
    populate_by_name, optional ``error``) BUT with one deliberate serialization difference: a
    ``model_serializer`` drops declared fields that are ``None`` so the on-wire JSON array stays
    byte-for-structure equivalent to the previous ``json.dumps(list)`` text — i.e. a row that the
    API returned as ``{"countryCode": "DE"}`` serializes as exactly that, NOT
    ``{"countryCode": "DE", "error": null, ...}``.

    Why this differs from ``LexofficeBase``: the object-returning tools already emit their
    declared ``null`` keys (that text shape was locked in by the pass-1/2 uplift and the portal
    re-synced to it). But these four tools return a *bare array*; rule 2's "same array"
    backward-compat constraint is strongest there, so injecting per-element ``error: null`` /
    ``deepLink: null`` would change the element shape. Dropping ``None`` keeps the unstructured
    text an equivalent JSON array while still advertising a typed, additive ``structured_content``
    (verified end-to-end with an in-memory fastmcp Client). The optional ``error`` keeps rule 3
    satisfied: an ``{"error": "..."}`` payload still validates against the same schema.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    error: str | None = Field(default=None, description="Set only on an error payload; absent otherwise")

    @model_serializer(mode="wrap")
    def _drop_none(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


class Country(LexofficeListItem):
    """One row of GET /countries (list_countries)."""

    countryCode: str | None = None
    taxClassification: str | None = None


class PostingCategory(LexofficeListItem):
    """One Buchungskonto row of GET /posting-categories (list_posting_categories)."""

    id: str | None = None
    name: str | None = None
    type: str | None = None
    contactRequired: bool | None = None
    splitAllowed: bool | None = None
    groupName: str | None = None


class PaymentCondition(LexofficeListItem):
    """One row of GET /payment-conditions (list_payment_conditions)."""

    id: str | None = None
    paymentTermLabel: str | None = None
    paymentTermLabelTemplate: str | None = None
    paymentTermDuration: int | None = None
    organizationDefault: bool | None = None


class RecurringTemplateSummary(LexofficeListItem):
    """One row of GET /recurring-templates (list_recurring_templates), with the server-injected
    ``deepLink``. Mirrors the detail ``RecurringTemplate`` model but stays permissive for the
    list shape."""

    id: str | None = None
    organizationId: str | None = None
    title: str | None = None
    recurringTemplateSettings: dict[str, Any] | None = None
    deepLink: str | None = None


class Profile(LexofficeBase):
    """Organization profile (GET /profile)."""

    organizationId: str | None = None
    companyName: str | None = None
    taxType: str | None = None
    smallBusiness: bool | None = None


class Invoice(LexofficeBase):
    """An invoice / sales-voucher object, or the resolved Beleg fallback (get_invoice)."""

    id: str | None = None
    organizationId: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    dueDate: str | None = None
    version: int | None = None
    title: str | None = None
    totalPrice: dict[str, Any] | None = None
    lineItems: list[dict[str, Any]] = Field(default_factory=list)
    # NB: get_invoice may inject a "_note" key on the Beleg-fallback path; it is carried
    # verbatim by extra="allow" (a leading-underscore name can't be a real Pydantic field).


class VoucherListEntry(LexofficeBase):
    """One row of a voucherlist page (with server-injected deepLink/daysOverdue/_note)."""

    id: str | None = None
    voucherId: str | None = None
    voucherType: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    dueDate: str | None = None
    contactId: str | None = None
    contactName: str | None = None
    totalAmount: float | None = None
    openAmount: float | None = None
    currency: str | None = None
    archived: bool | None = None
    # "daysOverdue" is injected only when an open voucher's dueDate has passed, and "_note"
    # only for Belege; both are carried verbatim by extra="allow" so the key stays ABSENT
    # (not null) on non-overdue rows — preserving the original wire contract.


class VoucherList(LexofficeBase):
    """A paginated voucherlist response (list_invoices / list_expenses / list_vouchers /
    list_quotations / get_contact_invoices)."""

    content: list[VoucherListEntry] = Field(default_factory=list)
    totalPages: int | None = None
    totalElements: int | None = None
    numberOfElements: int | None = None
    size: int | None = None
    number: int | None = None
    first: bool | None = None
    last: bool | None = None


class Contact(LexofficeBase):
    """A contact object (get_contact / create_contact / update_contact /
    find_or_create_contact single-match)."""

    id: str | None = None
    organizationId: str | None = None
    version: int | None = None
    roles: dict[str, Any] | None = None
    company: dict[str, Any] | None = None
    person: dict[str, Any] | None = None
    emailAddresses: dict[str, Any] | None = None
    addresses: dict[str, Any] | None = None
    # find_or_create_contact injects an "_action" key; carried verbatim by extra="allow".


class ContactList(LexofficeBase):
    """A paginated contact search response (search_contacts) or a multiple-matches envelope
    from find_or_create_contact ({_action, message, contacts})."""

    content: list[Contact] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    totalPages: int | None = None
    totalElements: int | None = None
    numberOfElements: int | None = None
    size: int | None = None
    number: int | None = None
    first: bool | None = None
    last: bool | None = None
    message: str | None = None
    # find_or_create_contact sets "_action" on the multiple-matches envelope; carried by extra.


class Quotation(LexofficeBase):
    """A quotation (Angebot) object (create_draft_quotation / finalize_quotation)."""

    id: str | None = None
    organizationId: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    expirationDate: str | None = None
    version: int | None = None
    title: str | None = None
    totalPrice: dict[str, Any] | None = None
    lineItems: list[dict[str, Any]] = Field(default_factory=list)


class Article(LexofficeBase):
    """A service/product article (create_article / get_article / update_article)."""

    id: str | None = None
    organizationId: str | None = None
    version: int | None = None
    title: str | None = None
    type: str | None = None
    unitName: str | None = None
    price: dict[str, Any] | None = None
    description: str | None = None


class ArticleList(LexofficeBase):
    """A paginated article list (list_articles)."""

    content: list[Article] = Field(default_factory=list)
    totalPages: int | None = None
    totalElements: int | None = None
    numberOfElements: int | None = None
    size: int | None = None
    number: int | None = None
    first: bool | None = None
    last: bool | None = None


class Voucher(LexofficeBase):
    """A bookkeeping voucher (Beleg) read back in full (get_voucher / update_voucher)."""

    id: str | None = None
    organizationId: str | None = None
    type: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    dueDate: str | None = None
    version: int | None = None
    totalGrossAmount: float | None = None
    totalTaxAmount: float | None = None
    taxType: str | None = None
    contactId: str | None = None
    contactName: str | None = None
    useCollectiveContact: bool | None = None
    voucherItems: list[dict[str, Any]] = Field(default_factory=list)
    files: list[Any] = Field(default_factory=list)


class CreateVoucherResult(LexofficeBase):
    """The structured, read-back enrichment summary returned by create_voucher."""

    id: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    totalGrossAmount: float | None = None
    totalTaxAmount: float | None = None
    taxType: str | None = None
    contactId: str | None = None
    contactName: str | None = None
    useCollectiveContact: bool | None = None
    files: list[Any] = Field(default_factory=list)
    version: int | None = None
    tax_note: str | None = None
    attachment_error: str | None = None
    # The "_enrichment" wire key (and any other underscore-prefixed keys) are carried verbatim
    # by extra="allow" — declaring them as fields would risk colliding with the captured extra.


class CreditNote(LexofficeBase):
    """A credit note (Gutschrift) object (create_credit_note)."""

    id: str | None = None
    organizationId: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    version: int | None = None
    title: str | None = None
    totalPrice: dict[str, Any] | None = None
    lineItems: list[dict[str, Any]] = Field(default_factory=list)


class Dunning(LexofficeBase):
    """A dunning (Mahnung) object (create_dunning)."""

    id: str | None = None
    organizationId: str | None = None
    voucherStatus: str | None = None
    voucherNumber: str | None = None
    voucherDate: str | None = None
    version: int | None = None
    title: str | None = None
    totalPrice: dict[str, Any] | None = None
    lineItems: list[dict[str, Any]] = Field(default_factory=list)


class RecurringTemplate(LexofficeBase):
    """A recurring-invoice template (get_recurring_template)."""

    id: str | None = None
    organizationId: str | None = None
    version: int | None = None
    title: str | None = None
    recurringTemplateSettings: dict[str, Any] | None = None
    address: dict[str, Any] | None = None
    lineItems: list[dict[str, Any]] = Field(default_factory=list)
    totalPrice: dict[str, Any] | None = None


class DocumentRef(LexofficeBase):
    """A rendered-document reference (get_invoice_pdf / render_dunning_pdf)."""

    documentFileId: str | None = None


class FileRef(LexofficeBase):
    """A file-upload / attach result (upload_voucher / attach_voucher_file)."""

    id: str | None = None
    voucherId: str | None = None


class SendResult(LexofficeBase):
    """Status envelope for send_invoice ({status, invoice_id, recipient} or error)."""

    status: str | None = None
    invoice_id: str | None = None
    recipient: str | None = None


class DeleteResult(LexofficeBase):
    """Status envelope for delete_draft_invoice ({status, invoice_id} or error)."""

    status: str | None = None
    invoice_id: str | None = None


class SentInvoiceResult(LexofficeBase):
    """One-shot send summary (create_and_send_invoice / convert_quotation_and_send)."""

    status: str | None = None
    invoice_id: str | None = None
    voucherNumber: str | None = None
    recipient: str | None = None
    totalAmount: float | None = None
    quotation_id: str | None = None


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


# Software/SaaS subscriptions are the bulk of API-created purchase vouchers (e.g. Belegfänger
# receipts), and book to "Lizenzen und Konzessionen" — an expense account that accepts standard
# VAT (so the rate stays selectable in 'Zu prüfen' and input tax is reclaimable). Prefer it, then
# fall back to other generic expense accounts. Tax-restricted catch-all accounts (which reject
# 19% and grey out the tax dropdown) are deliberately not at the front of this list.
_PURCHASE_CATEGORY_PREFERENCES = (
    "lizenzen und konzessionen",
    "lizenz",
    "konzession",
    "sonstige",
    "betriebsbedarf",
    "bürobedarf",
    "buerobedarf",
    "wareneingang",
    "wareneinkauf",
)


async def _default_purchase_category_id(ctx: Context, *, has_contact: bool = True) -> str:
    """Resolve a sane default expense posting category (Buchungskonto) for purchase vouchers,
    lazily cached on the lifespan context (per has_contact).

    Prefers 'Lizenzen und Konzessionen' (software/license expense) — the correct account for the
    SaaS/subscription receipts that dominate API voucher creation, and one that accepts standard
    VAT so the tax rate stays editable in review and Vorsteuer is reclaimable. When a contact is
    attached, categories flagged contactRequired are eligible too — Lexoffice only needs the
    contact, which we supply, so requiring contact-optional accounts would needlessly skip good
    categories (this is why the old default landed on a tax-restricted catch-all). Callers may
    always override with an explicit category_id."""
    lc = ctx.lifespan_context
    cache_key = f"default_purchase_category_id:{has_contact}"
    if cache_key in lc:
        return lc[cache_key]
    categories = await _client(ctx).list_posting_categories()
    outgo = [c for c in categories if c.get("type") == "outgo"]

    def _eligible(c: dict) -> bool:
        return has_contact or not c.get("contactRequired", False)

    chosen = None
    for keyword in _PURCHASE_CATEGORY_PREFERENCES:
        for c in outgo:
            blob = f"{c.get('name', '')} {c.get('groupName', '')}".lower()
            if keyword in blob and _eligible(c):
                chosen = c
                break
        if chosen:
            break
    if chosen is None:
        chosen = next((c for c in outgo if _eligible(c)), None)
    if chosen is None:
        chosen = outgo[0] if outgo else None
    if chosen is None:
        raise RuntimeError("No expense (outgo) posting categories available on this account")
    category_id = chosen["id"]
    lc[cache_key] = category_id
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


@mcp.tool(
    tags={"finance", "profile", "read"},
    annotations=ToolAnnotations(
        title="Get organization profile",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_profile(ctx: Context) -> Profile:
    """[finance] Get the Lexware Office organization profile — company name, tax settings, currency."""
    result = await _client(ctx).get_profile()
    return Profile.model_validate(result)


# ── Invoices ─────────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "invoice", "write"},
    annotations=ToolAnnotations(
        title="Create draft invoice",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> Invoice:
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
    return Invoice.model_validate(result)


@mcp.tool(
    tags={"finance", "invoice", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Finalize invoice (irreversible)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def finalize_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the draft invoice to finalize"],
) -> Invoice:
    """[finance] Finalize a draft invoice — assigns an invoice number and makes it non-editable.
    Review the draft in Lexoffice UI before calling this. Cannot be undone."""
    await _client(ctx).finalize_invoice(invoice_id)
    result = await _client(ctx).get_invoice(invoice_id)
    result["deepLink"] = _deep_link(invoice_id)
    return Invoice.model_validate(result)


@mcp.tool(
    tags={"finance", "invoice", "delete", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Delete draft invoice",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def delete_draft_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the draft invoice to delete"],
) -> DeleteResult:
    """[finance] Delete a draft invoice. Only works on drafts — finalized invoices cannot be deleted."""
    invoice = await _client(ctx).get_invoice(invoice_id)
    status = invoice.get("voucherStatus", "")
    if status != "draft":
        raise ToolError(
            f"Cannot delete invoice with status '{status}'. Only drafts can be deleted. "
            f"Review it at {_deep_link(invoice_id)}"
        )

    await _client(ctx).delete_invoice(invoice_id)
    return DeleteResult.model_validate({"status": "deleted", "invoice_id": invoice_id})


@mcp.tool(
    tags={"finance", "invoice", "send", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Send invoice by email",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def send_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the finalized invoice"],
    recipient_email: Annotated[str, "Email address to send the invoice to"],
) -> SendResult:
    """[finance] Send a finalized invoice by email. The invoice must be finalized first."""
    invoice = await _client(ctx).get_invoice(invoice_id)
    status = invoice.get("voucherStatus", "")
    if status == "draft":
        raise ToolError(
            "Invoice is still a draft. Finalize it first. "
            f"Edit it at {_deep_link(invoice_id, edit=True)}"
        )

    await _client(ctx).send_invoice(invoice_id, recipient_email)
    return SendResult.model_validate({"status": "sent", "invoice_id": invoice_id, "recipient": recipient_email})


@mcp.tool(
    tags={"finance", "invoice", "read"},
    annotations=ToolAnnotations(
        title="Get invoice",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_invoice(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the invoice or voucher (IDs from list_invoices work here)"],
) -> Invoice:
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
    return Invoice.model_validate(result)


@mcp.tool(
    tags={"finance", "invoice", "read", "document"},
    annotations=ToolAnnotations(
        title="Render invoice PDF",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_invoice_pdf(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the finalized invoice"],
) -> DocumentRef:
    """[finance] Render and get the document file ID for an invoice PDF. Invoice must be finalized."""
    result = await _client(ctx).render_invoice_document(invoice_id)
    return DocumentRef.model_validate(result)


@mcp.tool(
    tags={"finance", "invoice", "list", "read"},
    annotations=ToolAnnotations(
        title="List sales invoices",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_invoices(
    ctx: Context,
    status: Annotated[
        str | None,
        "Filter: draft, open, paid, paidoff, voided, overdue, unchecked "
        "(comma-separated for multiple). 'unchecked' = uploaded Belege awaiting review.",
    ] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> VoucherList:
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
    return VoucherList.model_validate(result)


# ── Voucher Upload ───────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "voucher", "upload", "write"},
    annotations=ToolAnnotations(
        title="Upload raw voucher file",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def upload_voucher(
    ctx: Context,
    file_content: Annotated[str, "Base64-encoded file content"],
    file_name: Annotated[str, "Original file name (e.g. 'invoice.pdf')"],
    voucher_type: Annotated[str, "Voucher type: purchaseinvoice, receipt, etc."] = "purchaseinvoice",
) -> FileRef:
    """[finance] Upload a bill/receipt file to Lexoffice as a voucher for review.
    Accepts PDF, PNG, or JPG files up to 5MB. The file appears in Lexoffice 'Zu prüfen'."""
    allowed_ext = (".pdf", ".png", ".jpg", ".jpeg")
    if not any(file_name.lower().endswith(ext) for ext in allowed_ext):
        raise ToolError(f"Unsupported file type. Allowed: {', '.join(allowed_ext)}")

    file_bytes = base64.b64decode(file_content)
    if len(file_bytes) > 5 * 1024 * 1024:
        raise ToolError("File exceeds 5MB Lexoffice upload limit")

    result = await _client(ctx).upload_file(file_bytes, file_name)
    return FileRef.model_validate(result)


# ── Financial Queries ────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "expense", "list", "read"},
    annotations=ToolAnnotations(
        title="List expenses",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_expenses(
    ctx: Context,
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue, unchecked (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> VoucherList:
    """[finance] List purchase invoices and expenses."""
    result = await _client(ctx).filter_vouchers(
        "purchaseinvoice", voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return VoucherList.model_validate(result)


# Hard ceiling on pages fetched per voucher query, so a pathologically large account
# can never hang the (often headless / portal-budgeted) call. 250 rows/page * 200 pages
# = 50_000 vouchers — well beyond any realistic monthly-overview window.
_FINANCIAL_PAGE_SIZE = 250
_FINANCIAL_MAX_PAGES = 200


async def _fetch_all_vouchers(
    ctx: Context,
    voucher_type: str,
    *,
    voucher_status: str | None,
) -> tuple[list[dict], bool]:
    """Fetch every voucher across all pages for a filter, returning (rows, truncated).

    Replaces the old single-page-then-flag heuristic so the financial overview no longer
    silently drops older settled invoices on busy accounts. Pagination is driven off the
    Lexoffice voucherlist paging envelope (``last`` / ``totalPages``):

    * keep requesting the next page until the API reports the last page, or
    * stop at ``_FINANCIAL_MAX_PAGES`` and report ``truncated=True`` (explicit cap, never
      a silent loss).

    When the response carries no paging metadata at all (``last``/``totalPages`` absent),
    we treat the single page as authoritative and only flag ``truncated`` if it came back
    completely full — i.e. we cannot prove we saw everything."""
    client = _client(ctx)
    rows: list[dict] = []
    page = 0
    truncated = False

    while True:
        resp = await client.filter_vouchers(
            voucher_type,
            voucher_status=voucher_status,
            page=page,
            size=_FINANCIAL_PAGE_SIZE,
        )
        content = resp.get("content", []) or []
        rows.extend(content)

        has_paging = ("last" in resp) or ("totalPages" in resp)
        if not has_paging:
            # No envelope to drive pagination — a brimful page means there may be more
            # we cannot reach safely, so be honest about it.
            if len(content) >= _FINANCIAL_PAGE_SIZE:
                truncated = True
            break

        total_pages = resp.get("totalPages")
        is_last = bool(resp.get("last")) or (
            isinstance(total_pages, int) and page >= total_pages - 1
        )
        if is_last or not content:
            break

        page += 1
        if page >= _FINANCIAL_MAX_PAGES:
            # Genuine more-data-exists situation we deliberately stop at.
            truncated = True
            break

    return rows, truncated


@mcp.tool(
    tags={"finance", "report", "read"},
    annotations=ToolAnnotations(
        title="Financial overview (monthly)",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_financial_overview(
    ctx: Context,
    months: Annotated[int, "Number of months to include (1-12, default 6)"] = 6,
) -> FinancialOverview:
    """[finance] Get a monthly revenue/expense/net overview. Queries sales and purchase invoices.

    months defaults to 6, maximum 12. Large ranges may be slow as it pages through all settled
    invoices. Each query is fully paginated; only an account exceeding the internal page ceiling
    sets the result's `truncated` flag (older settled invoices may then be excluded)."""
    months = max(1, min(12, months))
    await ctx.info(f"Aggregating financial overview for the last {months} month(s)")

    sales, sales_trunc = await _fetch_all_vouchers(
        ctx, "salesinvoice,invoice", voucher_status="paidoff"
    )
    await ctx.report_progress(1, 3)
    purchases, purchases_trunc = await _fetch_all_vouchers(
        ctx, "purchaseinvoice", voucher_status="paidoff"
    )
    await ctx.report_progress(2, 3)
    open_rows, open_trunc = await _fetch_all_vouchers(
        ctx, "salesinvoice,invoice", voucher_status="open"
    )
    await ctx.report_progress(3, 3)

    truncated = sales_trunc or purchases_trunc or open_trunc

    today = date.today()
    monthly: dict[str, dict[str, float]] = {}

    for item in sales:
        voucher_date = item.get("voucherDate", "")[:7]
        if voucher_date:
            monthly.setdefault(voucher_date, {"revenue": 0, "expenses": 0})
            monthly[voucher_date]["revenue"] += item.get("totalAmount", 0)

    for item in purchases:
        voucher_date = item.get("voucherDate", "")[:7]
        if voucher_date:
            monthly.setdefault(voucher_date, {"revenue": 0, "expenses": 0})
            monthly[voucher_date]["expenses"] += item.get("totalAmount", 0)

    overview = []
    for month_key in sorted(monthly.keys(), reverse=True)[:months]:
        data = monthly[month_key]
        overview.append(MonthlyFinancials(
            month=month_key,
            revenue=round(data["revenue"], 2),
            expenses=round(data["expenses"], 2),
            net=round(data["revenue"] - data["expenses"], 2),
        ))

    open_count = len(open_rows)
    overdue_count = sum(
        1 for inv in open_rows
        if inv.get("dueDate") and date.fromisoformat(inv["dueDate"][:10]) < today
    )

    return FinancialOverview(
        monthly=overview,
        open_invoices=open_count,
        overdue_invoices=overdue_count,
        truncated=truncated,
    )


@mcp.tool(
    tags={"finance", "payment", "read"},
    annotations=ToolAnnotations(
        title="Get payment status",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_payment_status(
    ctx: Context,
    invoice_id: Annotated[str | None, "UUID of a specific invoice"] = None,
    contact_name: Annotated[str | None, "Search open invoices by contact name"] = None,
) -> str:
    """[finance] Check payment status for an invoice (by ID) or all open invoices for a contact.

    Deliberately returns a raw JSON *string* (not a typed model). Its payload is genuinely
    polymorphic by branch: a single payments OBJECT when called by invoice_id (the Lexoffice
    GET /payments/{id} shape), versus an ARRAY of per-invoice payment summaries when called by
    contact_name. A typed return can't represent "object XOR array" without changing one
    branch's wire shape (e.g. wrapping the object in a list), so this stays str for
    backward-compat. The missing-arg case now raises ToolError instead of returning an error
    envelope, unifying error signalling with the rest of the server."""
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

    raise ToolError("Provide either invoice_id or contact_name")


# ── Contacts ─────────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "contact", "search", "read"},
    annotations=ToolAnnotations(
        title="Search contacts",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def search_contacts(
    ctx: Context,
    name: Annotated[str | None, "Filter by name (min 3 chars)"] = None,
    email: Annotated[str | None, "Filter by email (min 3 chars)"] = None,
    role: Annotated[str | None, "Filter: customer, vendor, or both"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> ContactList:
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
    return ContactList.model_validate(result)


@mcp.tool(
    tags={"finance", "contact", "read"},
    annotations=ToolAnnotations(
        title="Get contact",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_contact(
    ctx: Context,
    contact_id: Annotated[str, "UUID of the contact"],
) -> Contact:
    """[finance] Get full details for a specific contact, including deep link."""
    result = await _client(ctx).get_contact(contact_id)
    result["deepLink"] = _contact_link(contact_id)
    return Contact.model_validate(result)


@mcp.tool(
    tags={"finance", "contact", "write"},
    annotations=ToolAnnotations(
        title="Create contact",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> Contact:
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
        raise ToolError("Provide either company_name or first_name/last_name")

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
    return Contact.model_validate(result)


@mcp.tool(
    tags={"finance", "contact", "write"},
    annotations=ToolAnnotations(
        title="Update contact",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def update_contact(
    ctx: Context,
    contact_id: Annotated[str, "UUID of the contact"],
    company_name: Annotated[str | None, "Updated company name"] = None,
    first_name: Annotated[str | None, "Updated person first name"] = None,
    last_name: Annotated[str | None, "Updated person last name"] = None,
    email: Annotated[str | None, "Updated email address"] = None,
) -> Contact:
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
    return Contact.model_validate(result)


# ── Quotations ───────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "quotation", "write"},
    annotations=ToolAnnotations(
        title="Create draft quotation",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> Quotation:
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
    return Quotation.model_validate(result)


@mcp.tool(
    tags={"finance", "quotation", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Finalize quotation (irreversible)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def finalize_quotation(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the draft quotation"],
) -> Quotation:
    """[finance] Finalize a quotation — assigns Angebotsnummer, makes it sendable."""
    await _client(ctx).finalize_quotation(quotation_id)
    result = await _client(ctx).get_quotation(quotation_id)
    result["deepLink"] = _deep_link(quotation_id)
    return Quotation.model_validate(result)


@mcp.tool(
    tags={"finance", "quotation", "invoice", "write"},
    annotations=ToolAnnotations(
        title="Pursue quotation to draft invoice",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def pursue_quotation_to_invoice(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the finalized quotation"],
) -> Invoice:
    """[finance] Convert a finalized quotation into a draft invoice (Angebot to Rechnung).
    The quotation must be finalized first."""
    quotation = await _client(ctx).get_quotation(quotation_id)
    status = quotation.get("voucherStatus", "")
    if status == "draft":
        raise ToolError(
            "Quotation is still a draft. Finalize it first. "
            f"Edit it at {_deep_link(quotation_id, edit=True)}"
        )

    result = await _client(ctx).pursue_quotation(quotation_id)
    invoice_id = result.get("id", "")
    result["deepLink"] = _deep_link(invoice_id, edit=True)
    return Invoice.model_validate(result)


# ── Dunnings ─────────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "dunning", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Create dunning (Mahnung)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def create_dunning(
    ctx: Context,
    invoice_id: Annotated[str, "UUID of the overdue invoice"],
    note: Annotated[str | None, "Custom dunning text"] = None,
) -> Dunning:
    """[finance] Create a payment reminder (Mahnung) for an overdue invoice."""
    data: dict[str, Any] = {"invoiceId": invoice_id}
    if note:
        data["text"] = note
    result = await _client(ctx).create_dunning(data)
    dunning_id = result.get("id", "")
    result["deepLink"] = _deep_link(dunning_id)
    return Dunning.model_validate(result)


@mcp.tool(
    tags={"finance", "dunning", "read", "document"},
    annotations=ToolAnnotations(
        title="Render dunning PDF",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def render_dunning_pdf(
    ctx: Context,
    dunning_id: Annotated[str, "UUID of the dunning"],
) -> DocumentRef:
    """[finance] Render a dunning PDF and get the document file ID."""
    result = await _client(ctx).render_dunning_document(dunning_id)
    return DocumentRef.model_validate(result)


# ── Articles ─────────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "article", "list", "read"},
    annotations=ToolAnnotations(
        title="List articles",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_articles(
    ctx: Context,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
    size: Annotated[int, "Results per page (max 250)"] = 25,
) -> ArticleList:
    """[finance] List all configured service articles (reusable line items)."""
    result = await _client(ctx).list_articles(page=page, size=size)
    return ArticleList.model_validate(result)


@mcp.tool(
    tags={"finance", "article", "write"},
    annotations=ToolAnnotations(
        title="Create article",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def create_article(
    ctx: Context,
    name: Annotated[str, "Article name (e.g. 'Digitale Sprechstunde')"],
    net_price: Annotated[float, "Net price in EUR"],
    unit_name: Annotated[str, "Unit: Stunde, Tag, Pauschal, Stück"] = "Stück",
    article_type: Annotated[Literal["SERVICE", "PRODUCT"], "Article type"] = "SERVICE",
    description: Annotated[str | None, "Article description"] = None,
    tax_rate: Annotated[int | None, "Override tax rate percentage"] = None,
) -> Article:
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
    return Article.model_validate(result)


@mcp.tool(
    tags={"finance", "article", "read"},
    annotations=ToolAnnotations(
        title="Get article",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_article(
    ctx: Context,
    article_id: Annotated[str, "UUID of the article"],
) -> Article:
    """[finance] Get full details for a specific article."""
    result = await _client(ctx).get_article(article_id)
    return Article.model_validate(result)


@mcp.tool(
    tags={"finance", "article", "write"},
    annotations=ToolAnnotations(
        title="Update article",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def update_article(
    ctx: Context,
    article_id: Annotated[str, "UUID of the article"],
    name: Annotated[str | None, "Updated article name"] = None,
    net_price: Annotated[float | None, "Updated net price"] = None,
    unit_name: Annotated[str | None, "Updated unit name"] = None,
    description: Annotated[str | None, "Updated description"] = None,
) -> Article:
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
    return Article.model_validate(result)


# ── Voucher List (generic) ──────────────────────────────────────────


@mcp.tool(
    tags={"finance", "voucher", "list", "read"},
    annotations=ToolAnnotations(
        title="List vouchers (generic)",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
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
) -> VoucherList:
    """[finance] List vouchers of a given type with optional status filter.

    Prefer dedicated tools: list_invoices for salesinvoice, list_expenses for purchaseinvoice,
    list_quotations for quotation. Use this for other types (creditnote, orderconfirmation, etc.)."""
    result = await _client(ctx).filter_vouchers(
        voucher_type, voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return VoucherList.model_validate(result)


@mcp.tool(
    tags={"finance", "voucher", "read"},
    annotations=ToolAnnotations(
        title="Get voucher",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_voucher(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher"],
) -> Voucher:
    """[finance] Get full details for a voucher (purchase invoice, receipt, etc.) including line items with tax amounts.

    Returns voucherItems with amount, taxAmount, and taxRatePercent per line item.
    Use this to inspect VAT (Vorsteuer) on purchase invoices."""
    result = await _client(ctx).get_voucher(voucher_id)
    result["deepLink"] = _deep_link(voucher_id)
    return Voucher.model_validate(result)


@mcp.tool(
    tags={"finance", "voucher", "write"},
    annotations=ToolAnnotations(
        title="Update voucher line items",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def update_voucher(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher"],
    version: Annotated[int, "Current version number (for optimistic locking — get from get_voucher first)"],
    voucher_items: Annotated[str, "JSON array of updated voucher items: [{amount, taxAmount, taxRatePercent, categoryId}]"],
) -> Voucher:
    """[finance] Update a voucher's line items (e.g., to fix tax rates on purchase invoices).

    Get the current version and items from get_voucher first. Requires optimistic locking via version field.
    Common use: fix vatfree purchases to correct 19% MwSt rate."""
    import json as _json
    items = _json.loads(voucher_items)
    data = {"version": version, "voucherItems": items}
    result = await _client(ctx).update_voucher(voucher_id, data)
    return Voucher.model_validate(result)


# ── Structured Voucher Creation (Belegfänger enrichment) ─────────────


@mcp.tool(
    tags={"finance", "voucher", "reference", "read"},
    annotations=ToolAnnotations(
        title="List posting categories",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_posting_categories(
    ctx: Context,
    category_type: Annotated[
        Literal["income", "outgo", "all"],
        "Filter: income (revenue), outgo (expense), or all",
    ] = "all",
) -> list[PostingCategory]:
    """[finance] List posting categories (Buchungskonten) with their UUIDs.

    Use an 'outgo' (expense) category id as the category_id when creating purchase
    vouchers with create_voucher. Each entry includes contactRequired (whether a contact
    must be attached) and splitAllowed (whether mixed tax rates are permitted)."""
    categories = await _client(ctx).list_posting_categories()
    if category_type != "all":
        categories = [c for c in categories if c.get("type") == category_type]
    return [PostingCategory.model_validate(c) for c in categories]


@mcp.tool(
    tags={"finance", "voucher", "write", "belegfaenger"},
    annotations=ToolAnnotations(
        title="Create structured purchase voucher",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> CreateVoucherResult:
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
        raise ToolError(
            "Lexoffice rejects net vouchers with status 'unchecked'. "
            "Use tax_type='gross' (recommended for receipts — the amount is already gross) "
            "or set voucher_status='open'."
        )
    if file_content and not file_name:
        raise ToolError("file_name is required when file_content is provided")

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
                "categoryId": category_id or await _default_purchase_category_id(ctx, has_contact=bool(contact_id)),
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
    await ctx.info(f"Creating purchase voucher (gross {total_gross} {tax_type}, {rate}% VAT)")
    try:
        created = await _client(ctx).create_voucher(data)
    except httpx.HTTPStatusError as exc:
        # The resolved posting category rejected this VAT rate (tax-restricted account,
        # e.g. reverse-charge foreign SaaS). Re-book at 0% — universally accepted — so the
        # voucher still lands with the gross amount + vendor for bank-matching. The user
        # corrects the VAT in 'Zu prüfen'.
        if rate and _is_taxrate_rejection(exc):
            await ctx.warning(
                f"Posting category rejected {rate}% VAT — re-booking at 0% so the voucher persists"
            )
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
                await ctx.info(f"Attaching receipt file {file_name} to voucher {voucher_id}")
                await _client(ctx).attach_voucher_file(voucher_id, file_bytes, file_name)

    # Read back to confirm enrichment persisted (the failure mode CDI-1164 was opened for).
    await ctx.info("Reading voucher back to confirm enrichment persisted")
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
    return CreateVoucherResult.model_validate(result)


@mcp.tool(
    tags={"finance", "voucher", "write", "file"},
    annotations=ToolAnnotations(
        title="Attach file to voucher",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def attach_voucher_file(
    ctx: Context,
    voucher_id: Annotated[str, "UUID of the voucher to attach the file to"],
    file_content: Annotated[str, "Base64-encoded file content (PDF, PNG, or JPG, max 5MB)"],
    file_name: Annotated[str, "Original file name (e.g. 'invoice.pdf')"],
) -> FileRef:
    """[finance] Attach a receipt/invoice file (the original Beleg) to an existing voucher.

    Use after create_voucher when you need to attach the file separately. POST to
    /v1/vouchers/{id}/files. Returns the file id and voucher id."""
    allowed_ext = (".pdf", ".png", ".jpg", ".jpeg")
    if not any(file_name.lower().endswith(ext) for ext in allowed_ext):
        raise ToolError(f"Unsupported file type. Allowed: {', '.join(allowed_ext)}")

    file_bytes = base64.b64decode(file_content)
    if len(file_bytes) > 5 * 1024 * 1024:
        raise ToolError("File exceeds 5MB Lexoffice upload limit")

    result = await _client(ctx).attach_voucher_file(voucher_id, file_bytes, file_name)
    result["deepLink"] = _deep_link(voucher_id)
    return FileRef.model_validate(result)


# ── Payment Conditions ───────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "reference", "read"},
    annotations=ToolAnnotations(
        title="List payment conditions",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_payment_conditions(ctx: Context) -> list[PaymentCondition]:
    """[finance] List all configured payment conditions (Zahlungsbedingungen)."""
    result = await _client(ctx).list_payment_conditions()
    return [PaymentCondition.model_validate(c) for c in result]


# ── Utilities ────────────────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "reference", "read"},
    annotations=ToolAnnotations(
        title="List countries",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_countries(ctx: Context) -> list[Country]:
    """[finance] List all countries with their tax classification (DE, intraCommunity, thirdPartyCountry)."""
    result = await _client(ctx).list_countries()
    return [Country.model_validate(c) for c in result]


# ── Recurring Templates ────────────────────────────────────────────


@mcp.tool(
    tags={"finance", "recurring", "list", "read"},
    annotations=ToolAnnotations(
        title="List recurring templates",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_recurring_templates(ctx: Context) -> list[RecurringTemplateSummary]:
    """[finance] List all recurring invoice templates (Abo-Rechnungen).

    Shows scheduled invoices that are automatically created on a recurring basis.
    Each template contains the line items, recipient, schedule, and next execution date."""
    result = await _client(ctx).list_recurring_templates()
    if isinstance(result, list):
        for item in result:
            tid = item.get("id", "")
            item["deepLink"] = f"{LEXOFFICE_UI}/#/permalink/recurring-templates/view/{tid}"
    return [RecurringTemplateSummary.model_validate(item) for item in result]


@mcp.tool(
    tags={"finance", "recurring", "read"},
    annotations=ToolAnnotations(
        title="Get recurring template",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_recurring_template(
    ctx: Context,
    template_id: Annotated[str, "UUID of the recurring template"],
) -> RecurringTemplate:
    """[finance] Get full details for a recurring invoice template, including schedule, line items, and next execution date."""
    result = await _client(ctx).get_recurring_template(template_id)
    result["deepLink"] = f"{LEXOFFICE_UI}/#/permalink/recurring-templates/view/{template_id}"
    return RecurringTemplate.model_validate(result)


# ── Capability Tools (composite workflows) ─────────────────────────


@mcp.tool(
    tags={"finance", "invoice", "composite", "send", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Create, finalize & send invoice (irreversible)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> SentInvoiceResult:
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

    await ctx.info(f"Creating draft invoice for {recipient_name}")
    created = await _client(ctx).create_invoice(data)
    invoice_id = created.get("id", "")
    await ctx.report_progress(1, 3)
    await ctx.info("Finalizing invoice (assigns number)")
    await _client(ctx).finalize_invoice(invoice_id)
    await ctx.report_progress(2, 3)
    await ctx.info(f"Sending invoice to {recipient_email}")
    await _client(ctx).send_invoice(invoice_id, recipient_email)
    await ctx.report_progress(3, 3)
    invoice = await _client(ctx).get_invoice(invoice_id)
    invoice["deepLink"] = _deep_link(invoice_id)
    return SentInvoiceResult.model_validate({
        "status": "sent",
        "invoice_id": invoice_id,
        "voucherNumber": invoice.get("voucherNumber"),
        "recipient": recipient_email,
        "totalAmount": invoice.get("totalPrice", {}).get("totalNetAmount"),
        "deepLink": invoice["deepLink"],
    })


@mcp.tool(
    tags={"finance", "contact", "composite", "write"},
    annotations=ToolAnnotations(
        title="Find or create contact",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def find_or_create_contact(
    ctx: Context,
    name: Annotated[str, "Company or person name to search for"],
    role: Annotated[Literal["customer", "vendor"], "Contact role"] = "customer",
    email: Annotated[str | None, "Email address (used for creation if contact not found)"] = None,
    first_name: Annotated[str | None, "Person first name (if not a company)"] = None,
    last_name: Annotated[str | None, "Person last name (if not a company)"] = None,
) -> Contact:
    """[finance] Find a contact by name or create one if it doesn't exist. Returns the contact ID.

    Searches by name first. If exactly one match is found, returns it.
    If no match, creates a new contact. If multiple matches, returns them all for disambiguation.

    Polymorphic payload: a single matched/created contact object (carrying ``_action`` =
    found_existing / created_new), or a disambiguation envelope ({``_action`` = multiple_matches,
    ``message``, ``contacts``[]}). Both validate against the permissive Contact model."""
    results = await _client(ctx).filter_contacts(name=name)
    matches = results.get("content", [])

    if len(matches) == 1:
        contact = matches[0]
        contact["deepLink"] = _contact_link(contact.get("id", ""))
        contact["_action"] = "found_existing"
        return Contact.model_validate(contact)

    if len(matches) > 1:
        for c in matches:
            c["deepLink"] = _contact_link(c.get("id", ""))
        return Contact.model_validate({"_action": "multiple_matches", "message": f"Found {len(matches)} contacts matching '{name}'. Pick one or refine the search.", "contacts": matches})

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
    return Contact.model_validate(result)


@mcp.tool(
    tags={"finance", "quotation", "invoice", "composite", "send", "write", "irreversible"},
    annotations=ToolAnnotations(
        title="Convert quotation & send invoice (irreversible)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def convert_quotation_and_send(
    ctx: Context,
    quotation_id: Annotated[str, "UUID of the finalized quotation"],
    recipient_email: Annotated[str, "Email address to send the invoice to"],
) -> SentInvoiceResult:
    """[finance] Convert an accepted quotation into an invoice and send it in one step.

    The quotation must be finalized first. Creates a draft invoice from the quotation,
    finalizes it, and sends it by email."""
    quotation = await _client(ctx).get_quotation(quotation_id)
    status = quotation.get("voucherStatus", "")
    if status == "draft":
        raise ToolError(
            "Quotation is still a draft. Finalize it first. "
            f"Edit it at {_deep_link(quotation_id, edit=True)}"
        )

    await ctx.info("Converting quotation to draft invoice")
    pursued = await _client(ctx).pursue_quotation(quotation_id)
    invoice_id = pursued.get("id", "")
    await ctx.report_progress(1, 3)
    await ctx.info("Finalizing invoice (assigns number)")
    await _client(ctx).finalize_invoice(invoice_id)
    await ctx.report_progress(2, 3)
    await ctx.info(f"Sending invoice to {recipient_email}")
    await _client(ctx).send_invoice(invoice_id, recipient_email)
    await ctx.report_progress(3, 3)
    invoice = await _client(ctx).get_invoice(invoice_id)
    invoice["deepLink"] = _deep_link(invoice_id)
    return SentInvoiceResult.model_validate({
        "status": "sent",
        "invoice_id": invoice_id,
        "voucherNumber": invoice.get("voucherNumber"),
        "recipient": recipient_email,
        "quotation_id": quotation_id,
        "deepLink": invoice["deepLink"],
    })


@mcp.tool(
    tags={"finance", "quotation", "list", "read"},
    annotations=ToolAnnotations(
        title="List quotations",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def list_quotations(
    ctx: Context,
    status: Annotated[str | None, "Filter: draft, open, accepted, rejected, voided (comma-separated)"] = None,
    page: Annotated[int, "Page number (0-indexed)"] = 0,
) -> VoucherList:
    """[finance] List quotations (Angebote), optionally filtered by status."""
    result = await _client(ctx).filter_vouchers(
        "quotation", voucher_status=status, page=page
    )
    for item in result.get("content", []):
        vid = item.get("voucherId", "")
        item["deepLink"] = _deep_link(vid)
    return VoucherList.model_validate(result)


@mcp.tool(
    tags={"finance", "invoice", "contact", "composite", "read"},
    annotations=ToolAnnotations(
        title="Get invoices for a contact",
        readOnlyHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def get_contact_invoices(
    ctx: Context,
    contact_name: Annotated[str | None, "Contact name to search for"] = None,
    contact_id: Annotated[str | None, "UUID of the contact (preferred over name)"] = None,
    status: Annotated[str | None, "Filter: draft, open, paid, paidoff, voided, overdue (comma-separated)"] = None,
) -> VoucherList:
    """[finance] List all invoices for a specific contact. Search by name or provide a contact ID directly.

    Use when the user asks 'show all invoices for Acme GmbH' or 'what did we bill StoryKeep?'."""
    if not contact_id and not contact_name:
        raise ToolError("Provide either contact_id or contact_name")

    if not contact_id:
        contacts = await _client(ctx).filter_contacts(name=contact_name)
        matches = contacts.get("content", [])
        if not matches:
            raise ToolError(f"No contact found matching '{contact_name}'")
        if len(matches) > 1:
            def _label(c: dict) -> str:
                company = (c.get("company") or {}).get("name")
                if company:
                    name = company
                else:
                    person = c.get("person") or {}
                    name = f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
                return f"{name} ({c.get('id')})"

            candidates = ", ".join(_label(c) for c in matches)
            raise ToolError(
                f"Multiple contacts match '{contact_name}'. Provide a contact_id. "
                f"Candidates: {candidates}"
            )
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
    return VoucherList.model_validate(result)


@mcp.tool(
    tags={"finance", "creditnote", "write"},
    annotations=ToolAnnotations(
        title="Create credit note",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
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
) -> CreditNote:
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
    return CreditNote.model_validate(result)


from datetime import timezone as _tz  # noqa: E402

_start_time = datetime.now(_tz.utc)
try:
    from mcp_lexoffice import __version__ as _version
except ImportError:
    _version = "0.1.0"


# ── Resources (reference data + server context) ──────────────────────
# Static and cacheable reference data is exposed under the lexoffice:// scheme so
# clients can pull it deterministically (e.g. while drafting an invoice) without
# spending a tool call. The live-API resources mirror their list_* tool counterparts.

# The fixed service catalog used when drafting invoices/quotations. Kept here (and
# summarized in the server instructions) so the model can read pricing as data.
SERVICE_CATALOG = [
    {"name": "Digitale Sprechstunde", "unit": "Pauschal", "net_price": 995, "currency": "EUR"},
    {"name": "Consulting", "unit": "Stunde", "net_price": 150, "currency": "EUR"},
    {"name": "Platform Development", "unit": "Tag", "net_price": 1200, "currency": "EUR"},
]


@mcp.resource(
    "lexoffice://service-catalog",
    name="Service catalog",
    description="Standard service offerings and pricing used when drafting invoices/quotations.",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def service_catalog_resource() -> str:
    """Standard service offerings and their list pricing (net, EUR)."""
    return _fmt(SERVICE_CATALOG)


@mcp.resource(
    "lexoffice://countries",
    name="Countries",
    description="Countries with tax classification (DE, intraCommunity, thirdPartyCountry).",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def countries_resource(ctx: Context) -> str:
    """Live country list with tax classification, from the Lexoffice API."""
    return _fmt(await _client(ctx).list_countries())


@mcp.resource(
    "lexoffice://posting-categories",
    name="Posting categories",
    description="Buchungskonten (UUID, type income/outgo, contactRequired, splitAllowed).",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def posting_categories_resource(ctx: Context) -> str:
    """Live posting categories (Buchungskonten) for booking vouchers, from the API."""
    return _fmt(await _client(ctx).list_posting_categories())


@mcp.resource(
    "lexoffice://payment-conditions",
    name="Payment conditions",
    description="Configured Zahlungsbedingungen (payment terms) for the account.",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def payment_conditions_resource(ctx: Context) -> str:
    """Live payment conditions (Zahlungsbedingungen), from the API."""
    return _fmt(await _client(ctx).list_payment_conditions())


@mcp.resource(
    "lexoffice://tax-config",
    name="Tax configuration",
    description="Auto-detected tax regime (vatfree/net/gross) and default VAT rate for this account.",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def tax_config_resource(ctx: Context) -> str:
    """The account's resolved tax regime and default VAT rate (auto-detected from the profile)."""
    return _fmt(await _get_tax_config(ctx))


@mcp.resource(
    "lexoffice://status",
    name="Server status",
    description="mcp-lexoffice service name, version, and uptime.",
    mime_type="application/json",
    annotations={"readOnlyHint": True},
)
def status_resource() -> str:
    """Lightweight server status (name, version, uptime) — mirrors the /health route."""
    return _fmt({
        "service": "mcp-lexoffice",
        "version": _version,
        "uptime_seconds": int((datetime.now(_tz.utc) - _start_time).total_seconds()),
    })


@mcp.resource(
    "lexoffice://contact/{contact_id}/invoices",
    name="Contact invoices",
    description="All invoices for a contact (by UUID), mirroring the get_contact_invoices tool.",
    mime_type="application/json",
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def contact_invoices_resource(contact_id: str, ctx: Context) -> str:
    """Read-only view of a contact's invoices, reusing the get_contact_invoices tool.

    Error-path-safe: instead of letting a ToolError (e.g. unknown contact) or transport
    failure abort the resource read, any problem is captured and returned as a structured
    ``{"error": ...}`` JSON document — the same permissive shape the typed tool returns."""
    try:
        result = await get_contact_invoices(ctx, contact_id=contact_id)
        return _fmt(result.model_dump(by_alias=True, exclude_none=True))
    except ToolError as exc:
        return _fmt({"error": str(exc), "contactId": contact_id})
    except Exception as exc:  # noqa: BLE001 — resources must degrade, never explode
        return _fmt({"error": f"Failed to load invoices: {exc}", "contactId": contact_id})


# ── Prompts (guided multi-step accounting workflows) ─────────────────


@mcp.prompt(
    name="monthly_close",
    description="Guided monthly accounting close: overview, overdue invoices, dunning suggestions.",
    tags={"finance", "workflow"},
)
def monthly_close(months: str = "1") -> str:
    """Walk the monthly close: pull the financial overview, list overdue invoices, suggest dunnings."""
    return (
        f"Run a monthly accounting close covering the last {months} month(s) using the "
        "Lexware Office tools. Steps:\n"
        f"1. Call get_financial_overview(months={months}) for revenue/expenses/net and the "
        "open/overdue invoice counts. If the result's `truncated` flag is true, note that older "
        "settled invoices were excluded.\n"
        "2. Call list_invoices(status=\"open\") and identify any invoice whose daysOverdue is set.\n"
        "3. For each overdue invoice, propose a dunning (create_dunning) — but do NOT create it "
        "until I confirm. Summarize the amount, contact, and days overdue.\n"
        "4. Finish with a short plain-language summary: net result, cash still owed, and the "
        "recommended dunning actions."
    )


@mcp.prompt(
    name="dunning_run",
    description="Find overdue open invoices and walk creating payment reminders (Mahnungen).",
    tags={"finance", "workflow", "dunning"},
)
def dunning_run() -> str:
    """Guide a dunning run over all overdue open invoices."""
    return (
        "Help me chase overdue invoices in Lexware Office:\n"
        "1. Call list_invoices(status=\"open\") and keep only the items that carry a daysOverdue "
        "value (their dueDate has passed).\n"
        "2. Present them as a table: voucherNumber, contactName, totalAmount, daysOverdue, deepLink. "
        "Sort by daysOverdue descending.\n"
        "3. For each one I confirm, call create_dunning(invoice_id=...) and then render_dunning_pdf "
        "to get the document. create_dunning is a write action — never call it without my explicit "
        "go-ahead for that specific invoice.\n"
        "4. Report which dunnings were created and which I skipped."
    )


@mcp.prompt(
    name="capture_receipt",
    description="Belegfänger flow: resolve the vendor, then create a structured purchase voucher.",
    tags={"finance", "workflow", "voucher", "belegfaenger"},
)
def capture_receipt(
    vendor: str = "",
    amount: str = "",
    voucher_date: str = "",
) -> str:
    """Guide capturing a paper/PDF receipt into a structured, bank-matchable voucher."""
    return (
        "Capture a receipt into Lexware Office as a structured, bank-matchable voucher.\n"
        f"Vendor: {vendor or '(ask me)'} · Amount (gross EUR): {amount or '(ask me)'} · "
        f"Date: {voucher_date or '(ask me)'}\n\n"
        "Steps:\n"
        "1. Resolve the vendor with find_or_create_contact(name=<vendor>, role=\"vendor\"). If it "
        "returns multiple_matches, ask me which one before continuing.\n"
        "2. Call create_voucher with total_amount=<gross EUR>, voucher_date=<yyyy-MM-dd>, "
        "contact_id=<resolved id>, tax_type=\"gross\", and (if I provide one) the receipt PDF as "
        "file_content + file_name. The default posting category is 'Lizenzen und Konzessionen'; "
        "override category_id only if I tell you the expense type.\n"
        "3. Inspect the result's _enrichment block — confirm amount_persisted, contact_attached, "
        "and (if a file was given) file_attached are all true. If tax_rate_adjusted is set, tell me "
        "the VAT was re-booked at 0% and must be corrected in 'Zu prüfen'.\n"
        "4. Return the deepLink so I can review the voucher."
    )


from starlette.requests import Request as _SReq  # noqa: E402
from starlette.responses import JSONResponse as _SResp  # noqa: E402


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
        # fastmcp >=3.4.3 rejects non-localhost Host with 421 unless allowed_hosts set (edge CF-Access/Tailscale gated).
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            allowed_hosts=["*"],
        )
    else:
        mcp.run(transport=transport, host=host, port=port, json_response=True)


if __name__ == "__main__":
    main()
