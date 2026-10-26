"""Async HTTP client for the Lexware Office (Lexoffice) REST API."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp.exceptions import ToolError

from .config import get_settings

load_dotenv()

BASE_URL = "https://api.lexoffice.io/v1"

# Lexoffice allows 2 requests/second per token (token bucket) and answers 429 without
# performing the call. Space request starts a little wider than 0.5 s: the docs warn that
# pacing at exactly the limit still gets rate limited because of network jitter.
_MIN_INTERVAL = 0.6
# 429 retries per request, and the wait used when there is no numeric Retry-After
# (doubles per retry). Each wait is capped so a tool call can't hang on one request.
_MAX_429_RETRIES = 3
_BACKOFF_BASE = 1.0
_MAX_RETRY_WAIT = 10.0


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    try:
        wait = float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        wait = _BACKOFF_BASE * 2**attempt
    return min(max(wait, 0.0), _MAX_RETRY_WAIT)


class LexofficeClient:
    def __init__(self) -> None:
        api_key = get_settings().lexoffice_api_key.get_secret_value()
        if not api_key:
            raise RuntimeError(
                "LEXOFFICE_API_KEY must be set (raw token injected via env or .env)"
            )
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                # No default Content-Type: httpx sets application/json for json= calls and
                # multipart/form-data (with boundary) for files= uploads. A hardcoded default
                # here shadows the multipart boundary and breaks /files uploads (Lexoffice 500).
            },
            timeout=30.0,
        )
        # One client per server process (lifespan), so this paces every tool call together.
        self._throttle_lock = asyncio.Lock()
        self._next_slot = 0.0

    async def _throttle(self) -> None:
        """Wait until this request may start: starts are at least _MIN_INTERVAL apart."""
        async with self._throttle_lock:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = time.monotonic() + _MIN_INTERVAL

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        accept: str | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        # Don't set Content-Type when uploading files: httpx must set
        # multipart/form-data with its own boundary.
        if content_type and files is None:
            headers["Content-Type"] = content_type
        # Retrying is safe for writes too: Lexoffice performs no part of a 429'd call.
        for attempt in range(_MAX_429_RETRIES + 1):
            await self._throttle()
            resp = await self._client.request(
                method, path, params=params, json=json, content=content, files=files, headers=headers
            )
            if resp.status_code != 429 or attempt == _MAX_429_RETRIES:
                break
            # The bucket is per token, so hold back every caller, not just this retry.
            self._next_slot = max(self._next_slot, time.monotonic() + _retry_wait(resp, attempt))
        if resp.status_code >= 400:
            # The raw body. A json.dumps pretty-print used to sit here, but the `json`
            # parameter shadows the module, so it always raised and fell back to this.
            error_body = resp.text
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise httpx.HTTPStatusError(
                    f"{e}\nLexoffice response: {error_body}",
                    request=e.request,
                    response=e.response,
                ) from e
        return resp

    # ── Profile ──────────────────────────────────────────────────────

    async def get_profile(self) -> dict:
        resp = await self._request("GET", "/profile")
        return resp.json()

    # ── Contacts ─────────────────────────────────────────────────────

    async def create_contact(self, data: dict) -> dict:
        resp = await self._request("POST", "/contacts", json=data)
        return resp.json()

    async def get_contact(self, contact_id: str) -> dict:
        resp = await self._request("GET", f"/contacts/{contact_id}")
        return resp.json()

    async def update_contact(self, contact_id: str, data: dict) -> dict:
        resp = await self._request("PUT", f"/contacts/{contact_id}", json=data)
        return resp.json()

    async def filter_contacts(
        self,
        *,
        email: str | None = None,
        name: str | None = None,
        number: int | None = None,
        customer: bool | None = None,
        vendor: bool | None = None,
        page: int = 0,
        size: int = 25,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "size": size}
        if email:
            params["email"] = email
        if name:
            params["name"] = name
        if number is not None:
            params["number"] = number
        if customer is not None:
            params["customer"] = customer
        if vendor is not None:
            params["vendor"] = vendor
        resp = await self._request("GET", "/contacts", params=params)
        return resp.json()

    # ── Invoices ─────────────────────────────────────────────────────

    async def create_invoice(
        self, data: dict, *, finalize: bool = False
    ) -> dict:
        params = {}
        if finalize:
            params["finalize"] = "true"
        resp = await self._request(
            "POST", "/invoices", json=data, params=params or None
        )
        return resp.json()

    async def delete_invoice(self, invoice_id: str) -> None:
        await self._request("DELETE", f"/invoices/{invoice_id}")

    async def get_invoice(self, invoice_id: str) -> dict:
        resp = await self._request("GET", f"/invoices/{invoice_id}")
        return resp.json()

    async def get_invoice_or_voucher(self, resource_id: str) -> dict:
        """Try /invoices/{id} first; on 404, fall back to /vouchers/{id}.

        The voucherlist endpoint can return IDs for both Invoice API objects
        and bookkeeping vouchers (Belege). This method resolves either."""
        try:
            resp = await self._request("GET", f"/invoices/{resource_id}")
            result = resp.json()
            result["_resolvedVia"] = "invoices"
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
        resp = await self._request("GET", f"/vouchers/{resource_id}")
        result = resp.json()
        result["_resolvedVia"] = "vouchers"
        return result

    async def render_invoice_document(self, invoice_id: str) -> dict:
        resp = await self._request("GET", f"/invoices/{invoice_id}/document")
        return resp.json()

    async def download_invoice_pdf(self, invoice_id: str) -> bytes:
        resp = await self._request(
            "GET",
            f"/invoices/{invoice_id}/file",
            accept="application/pdf",
        )
        return resp.content

    # ── Quotations ───────────────────────────────────────────────────

    async def create_quotation(
        self, data: dict, *, finalize: bool = False
    ) -> dict:
        params = {}
        if finalize:
            params["finalize"] = "true"
        resp = await self._request(
            "POST", "/quotations", json=data, params=params or None
        )
        return resp.json()

    async def get_quotation(self, quotation_id: str) -> dict:
        resp = await self._request("GET", f"/quotations/{quotation_id}")
        return resp.json()

    async def pursue_quotation(self, quotation_id: str) -> dict:
        """Convert a finalized quotation into a draft invoice."""
        resp = await self._request(
            "POST", f"/quotations/{quotation_id}/pursue"
        )
        return resp.json()

    # ── Credit Notes ─────────────────────────────────────────────────

    async def create_credit_note(
        self, data: dict, *, finalize: bool = False, preceding_id: str | None = None
    ) -> dict:
        params: dict[str, str] = {}
        if finalize:
            params["finalize"] = "true"
        if preceding_id:
            params["precedingSalesVoucherId"] = preceding_id
        resp = await self._request(
            "POST", "/credit-notes", json=data, params=params or None
        )
        return resp.json()

    async def get_credit_note(self, credit_note_id: str) -> dict:
        resp = await self._request("GET", f"/credit-notes/{credit_note_id}")
        return resp.json()

    # ── Dunnings ─────────────────────────────────────────────────────

    async def create_dunning(self, data: dict) -> dict:
        resp = await self._request("POST", "/dunnings", json=data)
        return resp.json()

    async def render_dunning_document(self, dunning_id: str) -> dict:
        resp = await self._request("POST", f"/dunnings/{dunning_id}/document")
        return resp.json()

    # ── Articles ─────────────────────────────────────────────────────

    async def create_article(self, data: dict) -> dict:
        resp = await self._request("POST", "/articles", json=data)
        return resp.json()

    async def get_article(self, article_id: str) -> dict:
        resp = await self._request("GET", f"/articles/{article_id}")
        return resp.json()

    async def update_article(self, article_id: str, data: dict) -> dict:
        resp = await self._request("PUT", f"/articles/{article_id}", json=data)
        return resp.json()

    async def list_articles(self, *, page: int = 0, size: int = 25) -> dict:
        resp = await self._request(
            "GET", "/articles", params={"page": page, "size": size}
        )
        return resp.json()

    # ── Vouchers / Bookkeeping ───────────────────────────────────────

    async def filter_vouchers(
        self,
        voucher_type: str,
        *,
        voucher_status: str | None = None,
        contact_id: str | None = None,
        page: int = 0,
        size: int = 100,
    ) -> dict:
        statuses = [s.strip() for s in (voucher_status or "").split(",") if s.strip()]
        if "overdue" in statuses and len(statuses) > 1:
            # Lexoffice answers 400 "voucherStatus filter 'overdue' cannot be used in
            # combination with other states".
            raise ToolError(
                "Lexoffice rejects status 'overdue' combined with other statuses. "
                "Use status='overdue' on its own, or status='open' (overdue vouchers are "
                "open vouchers past their due date, so they are included)."
            )
        params: dict[str, Any] = {
            "page": page,
            "size": size,
            "voucherType": voucher_type,
        }
        params["voucherStatus"] = voucher_status or "any"
        if contact_id:
            params["contactId"] = contact_id
        resp = await self._request("GET", "/voucherlist", params=params)
        data = resp.json()
        # CDI-2081: voucherlist rows carry their id as `id` (no `voucherId` exists in the
        # API), but every reader (deep links, get_payment_status) and the existing
        # VoucherListEntry.voucherId output field expect `voucherId`. Fill it here, once.
        for row in data.get("content") or []:
            if row.get("id"):
                row["voucherId"] = row["id"]
        return data

    async def get_voucher(self, voucher_id: str) -> dict:
        resp = await self._request("GET", f"/vouchers/{voucher_id}")
        return resp.json()

    async def update_voucher(self, voucher_id: str, data: dict) -> dict:
        resp = await self._request("PUT", f"/vouchers/{voucher_id}", json=data)
        return resp.json()

    async def create_voucher(self, data: dict) -> dict:
        """Create a structured bookkeeping voucher (Beleg) via POST /v1/vouchers.

        Unlike upload_file (which lands a raw, un-OCR'd file in Beleg-Eingang), this
        creates a voucher with persisted amount, tax, and contact — the structured
        object Lexoffice can use for bank-matching."""
        resp = await self._request("POST", "/vouchers", json=data)
        return resp.json()

    async def attach_voucher_file(
        self, voucher_id: str, file_bytes: bytes, file_name: str
    ) -> dict:
        """Attach a file (the original Beleg) to an existing voucher.

        POST /v1/vouchers/{id}/files expects multipart/form-data with a `file` part."""
        import mimetypes

        mime_type = mimetypes.guess_type(file_name)[0] or "application/pdf"
        resp = await self._request(
            "POST",
            f"/vouchers/{voucher_id}/files",
            files={"file": (file_name, file_bytes, mime_type)},
            accept="application/json",
        )
        return resp.json()

    async def list_posting_categories(self) -> list[dict]:
        """List posting categories (Buchungskonten) with their UUIDs, type (income/outgo),
        and contactRequired/splitAllowed flags."""
        resp = await self._request("GET", "/posting-categories")
        return resp.json()

    # ── Recurring Templates ────────────────────────────────────────

    async def list_recurring_templates(self) -> list[dict]:
        resp = await self._request("GET", "/recurring-templates")
        return resp.json()

    async def get_recurring_template(self, template_id: str) -> dict:
        resp = await self._request("GET", f"/recurring-templates/{template_id}")
        return resp.json()

    # ── Payments ─────────────────────────────────────────────────────

    async def get_payments(self, invoice_id: str) -> dict:
        resp = await self._request("GET", f"/payments/{invoice_id}")
        return resp.json()

    # ── Files ────────────────────────────────────────────────────────

    async def upload_file(self, file_bytes: bytes, file_name: str, file_type: str = "voucher") -> dict:
        # Lexoffice /v1/files requires multipart/form-data with a `file` part carrying the
        # filename and a part-level Content-Type. httpx builds this from the files= tuple.
        import mimetypes

        mime_type = mimetypes.guess_type(file_name)[0] or "application/pdf"
        resp = await self._request(
            "POST",
            "/files",
            params={"type": file_type},
            files={"file": (file_name, file_bytes, mime_type)},
            accept="application/json",
        )
        return resp.json()

    async def download_file(self, file_id: str) -> bytes:
        resp = await self._request(
            "GET", f"/files/{file_id}", accept="application/octet-stream"
        )
        return resp.content

    # ── Payment Conditions ───────────────────────────────────────────

    async def list_payment_conditions(self) -> list[dict]:
        resp = await self._request("GET", "/payment-conditions")
        return resp.json()

    # ── Countries ────────────────────────────────────────────────────

    async def list_countries(self) -> list[dict]:
        resp = await self._request("GET", "/countries")
        return resp.json()

    # ── Event Subscriptions (Webhooks) ───────────────────────────────

    async def create_event_subscription(self, data: dict) -> dict:
        resp = await self._request("POST", "/event-subscriptions", json=data)
        return resp.json()

    async def list_event_subscriptions(self) -> list[dict]:
        resp = await self._request("GET", "/event-subscriptions")
        return resp.json()

    async def delete_event_subscription(self, subscription_id: str) -> None:
        await self._request("DELETE", f"/event-subscriptions/{subscription_id}")
