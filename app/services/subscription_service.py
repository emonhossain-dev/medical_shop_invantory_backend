from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.all_models import Subscription, SubscriptionPlan

class SubscriptionService:
    """Service for subscription management and limits checking."""
    
    @staticmethod
    async def check_user_limit(
        store_id: int,
        db: AsyncSession,
        current_count: int
    ) -> bool:
        """Check if store can add more users based on subscription plan."""
        result = await db.execute(
            select(Subscription).where(Subscription.store_id == store_id)
        )
        subscription = result.scalar_one_or_none()
        
        if not subscription or not subscription.is_active:
            return False
        
        return current_count < subscription.max_users
    
    @staticmethod
    async def check_medicine_limit(
        store_id: int,
        db: AsyncSession,
        current_count: int
    ) -> bool:
        """Check if store can add more medicines based on subscription plan."""
        result = await db.execute(
            select(Subscription).where(Subscription.store_id == store_id)
        )
        subscription = result.scalar_one_or_none()
        
        if not subscription or not subscription.is_active:
            return False
        
        return current_count < subscription.max_medicines
    
    @staticmethod
    async def get_subscription(
        store_id: int,
        db: AsyncSession
    ) -> Subscription:
        """Get subscription details for a store."""
        result = await db.execute(
            select(Subscription).where(Subscription.store_id == store_id)
        )
        return result.scalar_one_or_none()
