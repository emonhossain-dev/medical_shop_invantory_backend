from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Dict, Any
from app.models.all_models import AuditLog
import json

class AuditService:
    """Service for audit logging."""
    
    @staticmethod
    async def log_action(
        store_id: int,
        user_id: int,
        action: str,
        entity_type: str,
        entity_id: int,
        old_values: Optional[Dict[str, Any]] = None,
        new_values: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        db: AsyncSession = None
    ) -> AuditLog:
        """Log an action to audit trail."""
        audit_log = AuditLog(
            store_id=store_id,
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            old_values=json.dumps(old_values) if old_values else None,
            new_values=json.dumps(new_values) if new_values else None,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        if db:
            db.add(audit_log)
            await db.commit()
            await db.refresh(audit_log)
        
        return audit_log
