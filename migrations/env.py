import asyncio
import os
import sys
from pathlib import Path
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# project root path-এ নিয়ে যায়, যাতে models import করা যায়
sys.path.append(str(Path(__file__).resolve().parents[1]))

from models import Base   # <-- তোমার আসল filename অনুযায়ী বদলাও (যেমন all_models হলে: from all_models import Base)

load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# .env থেকে DATABASE_URL নিয়ে alembic.ini-র url override করে দিচ্ছে
db_url = os.getenv("DATABASE_URL")
if db_url:
    # async driver থাকলে migration-এর জন্য sync driver-এ কনভার্ট করা ভালো,
    # কিন্তু এই env.py async_engine_from_config ব্যবহার করছে বলে asyncpg-ই রাখো
    config.set_main_option("sqlalchemy.url", db_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate-এর জন্য models এর metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()