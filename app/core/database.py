from sqlalchemy.orm import declarative_base
from app.core.config import DATABASE_URL
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)

Base = declarative_base()
# pool_pre_ping: idle থাকার পর DB/NAT connection কেটে দিলে প্রথম request "connection is closed" এ ফেলত না
# pool_recycle: লম্বা-জীবী connection ৩০ মিনিট পর নতুন করে
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise