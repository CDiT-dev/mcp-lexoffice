"""Async HTTP client for the Lexware Office (Lexoffice) REST API."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.lexoffice.io/v1"


def _resolve_api_key() -> str:
    key = os.environ.get("LEXOFFICE_API_KEY", "")
    if key.startswith("op://"):
        result = subprocess.run(
            ["op", "read", key],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"1Password CLI failed: {result.stderr.strip()}")
        return result.stdout.strip()
    return key


class LexofficeClient:
    def __init__(self) -> None:
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "LEXOFFICE_API_KEY must be set (raw key or op:// reference)"
            )
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self._semaphore = asyncio.Semaphore(2)  # respect 2 req/s rate limit

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        accept: str | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        async with self._semaphore:
            headers: dict[str, str] = {}
            if accept:
                headers["Accept"] = accept
            if content_type:
                headers["Content-Type"] = content_type
            resp = await self._client.request(
                method, path, params=params, json=json, content=content, headers=headers
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                await asyncio.sleep(retry_after)
                resp = await self._client.request(
                    method, path, params=params, json=json, content=content, headers=headers
                )
            if resp.status_code >= 400:
                try:
                    error_body = json.dumps(resp.json(), indent=2, ensure_ascii=False, default=str)
                except Exception:
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

    async def finalize_invoice(self, invoice_id: str) -> dict:
        resp = await self._request("GET", f"/invoices/{invoice_id}")
        data = resp.json()
        version = data.get("version", 0)
        resp = await self._request(
            "POST",
            f"/invoices/{invoice_id}/finalize",
            json={"id": invoice_id, "version": version},
        )
        return resp.json()

    async def send_invoice(self, invoice_id: str, recipient_email: str) -> None:
        await self._request(
            "POST",
            f"/invoices/{invoice_id}/send",
            json={"recipientEmailAddresses": [recipient_email]},
        )

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

    async def finalize_quotation(self, quotation_id: str) -> dict:
        resp = await self._request("GET", f"/quotations/{quotation_id}")
        data = resp.json()
        version = data.get("version", 0)
        resp = await self._request(
            "POST",
            f"/quotations/{quotation_id}/finalize",
            json={"id": quotation_id, "version": version},
        )
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
        page: int = 0,
        size: int = 100,
    ) -> dict:
        params: dict[str, Any] = {
            "page": page,
            "size": size,
            "voucherType": voucher_type,
        }
        params["voucherStatus"] = voucher_status or "any"
        resp = await self._request("GET", "/voucherlist", params=params)
        return resp.json()

    async def get_voucher(self, voucher_id: str) -> dict:
        resp = await self._request("GET", f"/vouchers/{voucher_id}")
        return resp.json()

    # ── Payments ─────────────────────────────────────────────────────

    async def get_payments(self, invoice_id: str) -> dict:
        resp = await self._request("GET", f"/payments/{invoice_id}")
        return resp.json()

    # ── Files ────────────────────────────────────────────────────────

    async def upload_file(self, file_bytes: bytes, file_name: str, file_type: str = "voucher") -> dict:
        resp = await self._request(
            "POST",
            "/files",
            params={"type": file_type},
            content=file_bytes,
            content_type="application/octet-stream",
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
