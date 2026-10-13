import asyncio
from app.core.database import AsyncSessionLocal
from app.models.all_models import Unit

DEFAULT_GLOBAL_UNITS = [
    ("Pcs", "pcs"), ("Strip", "str"), ("Box", "box"), ("Bottle", "btl"),
    ("Vial", "vial"), ("Tube", "tube"), ("Sachet", "sch"), ("Ampoule", "amp"),
    ("Carton", "ctn"), ("Kg", "kg"), ("Litre", "L"),
]

async def seed():
    async with AsyncSessionLocal() as db:
        for name, code in DEFAULT_GLOBAL_UNITS:
            db.add(Unit(store_id=None, name=name, short_code=code, is_active=True))
        await db.commit()
        print(f"Seeded {len(DEFAULT_GLOBAL_UNITS)} global units")

if __name__ == "__main__":
    asyncio.run(seed())