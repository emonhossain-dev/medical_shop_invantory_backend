"""SMTP email sending helper. Uses the standard library smtplib (no extra dependency),
run in a thread so it doesn't block the async event loop."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import EMAIL_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USER


def _send_email_sync(to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = EMAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


async def send_email(to: str, subject: str, body: str) -> None:
    """Sends an email without blocking the event loop (smtplib itself is synchronous)."""
    await asyncio.to_thread(_send_email_sync, to, subject, body)