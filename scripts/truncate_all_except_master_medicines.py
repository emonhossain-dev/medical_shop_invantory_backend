"""
সব টেবিলের ডেটা মুছে fresh করার script — master_medicines এবং alembic_version বাদে।

চালানোর আগে অবশ্যই ডাটাবেস backup নিয়ে নিন:
    pg_dump -Fc your_db_name > backup_before_truncate.dump

চালানোর নিয়ম (project root থেকে):
    python -m scripts.truncate_all_except_master_medicines
    (বা যেখানে বসাবেন সেখান থেকে সরাসরি: python truncate_all_except_master_medicines.py)
"""

import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal

# এই টেবিলগুলো কখনো truncate হবে না
EXCLUDE_TABLES = {"master_medicines", "alembic_version"}


async def truncate_all():
    async with AsyncSessionLocal() as db:
        # public schema এর সব টেবিলের নাম বের করি
        result = await db.execute(
            text(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
        )
        all_tables = {row[0] for row in result.fetchall()}

        tables_to_truncate = sorted(all_tables - EXCLUDE_TABLES)

        if not tables_to_truncate:
            print("Truncate করার মতো কোনো টেবিল পাওয়া যায়নি।")
            return

        print("এই টেবিলগুলো truncate করা হবে:")
        for t in tables_to_truncate:
            print(f"  - {t}")

        confirm = input("\nনিশ্চিত? এই ডেটা আর ফেরত পাওয়া যাবে না। (yes লিখুন কনফার্ম করতে): ")
        if confirm.strip().lower() != "yes":
            print("বাতিল করা হলো।")
            return

        # সব টেবিল একসাথে TRUNCATE ... RESTART IDENTITY CASCADE
        # CASCADE FK dependency নিজে থেকেই সামলাবে, তাই order নিয়ে চিন্তা করতে হবে না
        table_list = ", ".join(f'"{t}"' for t in tables_to_truncate)
        await db.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))
        await db.commit()

        print(f"\n✅ {len(tables_to_truncate)} টা টেবিল truncate হয়ে গেছে (id sequence ও reset হয়েছে)।")


if __name__ == "__main__":
    asyncio.run(truncate_all())
