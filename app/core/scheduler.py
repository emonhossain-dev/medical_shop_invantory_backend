# app/core/scheduler.py
"""
APScheduler wiring — main.py এর lifespan এ start/shutdown হয়।
Single process এর মধ্যে চলে, তাই gunicorn/uvicorn multi-worker দিয়ে
deploy করলে একই job একাধিকবার রান হতে পারে — সেক্ষেত্রে Celery Beat এ
migrate করার কথা ভাবতে হবে। Single-worker deployment এ এটা যথেষ্ট।
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import LIFECYCLE_CHECK_HOUR
from app.core.database import AsyncSessionLocal  # তোমার session factory — নাম মিলিয়ে নিও
from app.services.subscription_lifecycle import run_lifecycle_check

logger = logging.getLogger("scheduler")
scheduler = AsyncIOScheduler()


async def _lifecycle_job():
    async with AsyncSessionLocal() as db:
        try:
            await run_lifecycle_check(db)
        except Exception:
            logger.exception("Lifecycle check job failed")


def start_scheduler():
    scheduler.add_job(
        _lifecycle_job,
        trigger=CronTrigger(hour=LIFECYCLE_CHECK_HOUR, minute=0),
        id="subscription_lifecycle_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — daily lifecycle check at {LIFECYCLE_CHECK_HOUR}:00 UTC")


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()