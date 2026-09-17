# app/services/payment_gateways/bkash.py
"""bKash Tokenized Checkout (v1.2.0-beta) client"""

import httpx
from app.core import config


class BkashClient:
    def __init__(self):
        self.base_url = config.BKASH_BASE_URL

    async def _grant_token(self) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/tokenized/checkout/token/grant",
                headers={
                    "Content-Type": "application/json",
                    "username": config.BKASH_USERNAME,
                    "password": config.BKASH_PASSWORD,
                },
                json={"app_key": config.BKASH_APP_KEY, "app_secret": config.BKASH_APP_SECRET},
            )
            resp.raise_for_status()
            return resp.json()["id_token"]

    def _headers(self, token: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": token,
            "X-App-Key": config.BKASH_APP_KEY,
        }

    async def create_payment(self, amount: str, invoice_no: str) -> dict:
        token = await self._grant_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/tokenized/checkout/create",
                headers=self._headers(token),
                json={
                    "mode": "0011",
                    "payerReference": invoice_no,
                    "callbackURL": config.BKASH_CALLBACK_URL,
                    "amount": amount,
                    "currency": "BDT",
                    "intent": "sale",
                    "merchantInvoiceNumber": invoice_no,
                },
            )
            resp.raise_for_status()
            return resp.json()  # { paymentID, bkashURL, ... }

    async def execute_payment(self, payment_id: str) -> dict:
        token = await self._grant_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/tokenized/checkout/execute",
                headers=self._headers(token),
                json={"paymentID": payment_id},
            )
            resp.raise_for_status()
            return resp.json()  # { trxID, transactionStatus, ... }


bkash_client = BkashClient()