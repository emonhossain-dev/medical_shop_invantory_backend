from sqlalchemy.orm import declarative_base
from app.core.config import DATABASE_URL
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
)

Base = declarative_base()

# Render Postgres (external URL) সাধারণত SSL require করে।
# Internal URL হলে SSL এর দরকার নেই, তখন connect_args বাদ দিলেও চলে।
_connect_args = {}
if DATABASE_URL and "asyncpg" in DATABASE_URL:
    # local development এ SSL না দিলেও asyncpg কাজ করে,
    # কিন্তু Render external URL এ SSL require হয়।
    # .env এ DB_SSL=true দিলে force করা যাবে।
    import os
    if os.getenv("DB_SSL", "false").lower() in ("1", "true", "yes"):
        _connect_args["ssl"] = True

engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,      # idle-এ connection মরে গেলে প্রথম request fail করবে না
    pool_recycle=1800,       # ৩০ মিনিট পর connection নতুন করে
    connect_args=_connect_args,
)

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