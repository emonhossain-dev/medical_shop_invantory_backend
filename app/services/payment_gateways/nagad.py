# app/services/payment_gateways/nagad.py
"""
Nagad Payment Gateway client — checkout flow.

ব্যবহারের আগে অবশ্যই এই দুইটা env var বসাতে হবে (Nagad merchant onboarding
থেকে পাওয়া যায়):
  - NAGAD_MERCHANT_PRIVATE_KEY  (তোমার merchant private key, PEM ফরম্যাটে)
  - NAGAD_PG_PUBLIC_KEY         (Nagad এর দেওয়া public key, PEM ফরম্যাটে)
এই দুইটা বসালেই এই client টা কাজ শুরু করবে, কোড বদলাতে হবে না।
"""

import base64
import json
import time
import uuid

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core import config


class NagadClient:
    def __init__(self):
        self.base_url = config.NAGAD_BASE_URL
        self.merchant_id = config.NAGAD_MERCHANT_ID

    def _private_key(self):
        return serialization.load_pem_private_key(
            config.NAGAD_MERCHANT_PRIVATE_KEY.encode(), password=None
        )

    def _pg_public_key(self):
        return serialization.load_pem_public_key(config.NAGAD_PG_PUBLIC_KEY.encode())

    def _sign(self, data: str) -> str:
        signature = self._private_key().sign(data.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def _encrypt(self, data: str) -> str:
        encrypted = self._pg_public_key().encrypt(data.encode(), padding.PKCS1v15())
        return base64.b64encode(encrypted).decode()

    async def initialize_payment(self, order_id: str, client_ip: str) -> dict:
        timestamp = time.strftime("%Y%m%d%H%M%S")
        sensitive = json.dumps({
            "merchantId": self.merchant_id,
            "datetime": timestamp,
            "orderId": order_id,
            "challenge": uuid.uuid4().hex,
        })
        payload = {
            "accountNumber": self.merchant_id,
            "dateTime": timestamp,
            "sensitiveData": self._encrypt(sensitive),
            "signature": self._sign(sensitive),
        }
        headers = {
            "X-KM-IP-V4": client_ip,
            "X-KM-Client-Type": "PC_WEB",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/api/dfs/check-out/initialize/{self.merchant_id}/{order_id}",
                headers=headers, json=payload,
            )
            resp.raise_for_status()
            return resp.json()  # { paymentReferenceId, challenge }

    async def complete_payment(self, payment_ref_id: str, amount: str, order_id: str, client_ip: str) -> dict:
        sensitive = json.dumps({
            "merchantId": self.merchant_id,
            "orderId": order_id,
            "amount": amount,
            "currencyCode": "050",
            "challenge": uuid.uuid4().hex,
        })
        payload = {
            "sensitiveData": self._encrypt(sensitive),
            "signature": self._sign(sensitive),
            "merchantCallbackURL": config.NAGAD_CALLBACK_URL,
        }
        headers = {
            "X-KM-IP-V4": client_ip,
            "X-KM-Client-Type": "PC_WEB",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/api/dfs/check-out/complete/{payment_ref_id}",
                headers=headers, json=payload,
            )
            resp.raise_for_status()
            return resp.json()  # { callBackUrl }  ← ইউজারকে এখানে রিডাইরেক্ট করাও

    async def verify_payment(self, payment_ref_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/api/dfs/verify/payment/{payment_ref_id}")
            resp.raise_for_status()
            return resp.json()  # { status: "Success"/"Failed", issuerPaymentRefNo, ... }


nagad_client = NagadClient()