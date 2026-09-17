# app/services/subscription_lifecycle.py
"""
Subscription lifecycle automation — Phase 6

কী করে:
1. trialing subscription এর current_period_end পার হয়ে গেলে → past_due
   (grace_period_end সেট করে দেয়, store.status = grace)
2. active subscription এর current_period_end পার হয়ে গেলে → past_due
   (একই grace flow)
3. past_due subscription এর grace_period_end ও পার হয়ে গেলে → expired
   (store.status = suspended)

canceled subscription কে touch করা হয় না।

এই function টা scheduler আর manual admin trigger — দুই জায়গা থেকেই
কল হয়, তাই db session বাইরে থেকে inject করা হয় (নিজে session বানায় না)।
"""

import logging
from datetime import date, timedelta, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import GRACE_PERIOD_DAYS
from app.models.all_models import (
    Subscription, Store, SubscriptionStatus, StoreStatus,
)

from dateutil.relativedelta import relativedelta
from app.models.all_models import BillingCycle  # top এর import গুলোর সাথে যোগ করো

logger = logging.getLogger("subscription_lifecycle")

# Subscription.status → Store.status sync mapping
# (subscriptions.py router এ আগে যেটা ছিল, এখন এখানে single source of truth)
STATUS_SYNC_MAP = {
    SubscriptionStatus.trialing: StoreStatus.trial,
    SubscriptionStatus.active: StoreStatus.active,
    SubscriptionStatus.past_due: StoreStatus.grace,
    SubscriptionStatus.canceled: StoreStatus.archived,
    SubscriptionStatus.expired: StoreStatus.suspended,
}


async def run_lifecycle_check(db: AsyncSession) -> dict:
    """একবার রান করলে যা যা subscription touch হলো তার summary রিটার্ন করে।"""
    today = date.today()
    result = {"trial_to_grace": 0, "active_to_grace": 0, "grace_to_expired": 0}

    # ---- 1 & 2: trialing/active যাদের period শেষ, তাদের past_due এ পাঠাও ----
    query = select(Subscription).where(
        Subscription.status.in_([SubscriptionStatus.trialing, SubscriptionStatus.active]),
        Subscription.current_period_end < today,
    )
    rows = (await db.execute(query)).scalars().all()

    for sub in rows:
        was_trialing = sub.status == SubscriptionStatus.trialing

        sub.status = SubscriptionStatus.past_due
        sub.grace_period_end = today + timedelta(days=GRACE_PERIOD_DAYS)

        store = await db.get(Store, sub.store_id)
        if store:
            store.status = STATUS_SYNC_MAP[sub.status]

        if was_trialing:
            result["trial_to_grace"] += 1
        else:
            result["active_to_grace"] += 1

        logger.info(f"Subscription {sub.id} (store {sub.store_id}) → past_due, grace till {sub.grace_period_end}")

    # ---- 3: past_due যাদের grace_period_end ও পার হয়ে গেছে, তাদের expired করো ----
    query = select(Subscription).where(
        Subscription.status == SubscriptionStatus.past_due,
        Subscription.grace_period_end.is_not(None),
        Subscription.grace_period_end < today,
    )
    rows = (await db.execute(query)).scalars().all()

    for sub in rows:
        sub.status = SubscriptionStatus.expired
        sub.auto_renew = False

        store = await db.get(Store, sub.store_id)
        if store:
            store.status = STATUS_SYNC_MAP[sub.status]

        result["grace_to_expired"] += 1
        logger.info(f"Subscription {sub.id} (store {sub.store_id}) → expired, store suspended")

    await db.commit()

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    logger.info(f"Lifecycle check done: {result}")
    return result




def extend_subscription_period(subscription: Subscription, billing_cycle: BillingCycle) -> None:
    """
    Payment success হলে current_period_end কে monthly/yearly অনুযায়ী extend করে।
    যদি period ইতিমধ্যে expire করে থাকে (past due), আজকের তারিখ থেকে গোনা শুরু হয়;
    না হলে existing period_end থেকে যোগ হয় (early renewal হলে সময় নষ্ট হয় না)।
    """
    base = subscription.current_period_end
    if base is None or base < date.today():
        base = date.today()

    if billing_cycle == BillingCycle.yearly:
        subscription.current_period_end = base + relativedelta(years=1)
    else:
        subscription.current_period_end = base + relativedelta(months=1)

    subscription.grace_period_end = None