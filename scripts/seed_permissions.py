"""
Permission catalog seed করার script — idempotent, বারবার চালালেও সমস্যা নেই
(app/core/permissions.py এর seed_permissions() ফাংশনে already existing code
skip করার লজিক আছে)।

চালানোর নিয়ম (project root থেকে):
    python -m scripts.seed_permissions
"""

import asyncio
from app.core.database import AsyncSessionLocal
from app.core.permissions import seed_permissions, iter_permission_codes


async def main():
    async with AsyncSessionLocal() as db:
        await seed_permissions(db)
        await db.commit()

    total = len(list(iter_permission_codes()))
    print(f"✅ Permission catalog seed সম্পন্ন হয়েছে (মোট {total} টা permission code)।")


if __name__ == "__main__":
    asyncio.run(main())