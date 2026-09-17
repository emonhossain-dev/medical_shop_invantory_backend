import httpx
import string
from decimal import Decimal, InvalidOperation
from sqlalchemy import create_engine, Column, Integer, String, Numeric, Boolean, DateTime, func
from sqlalchemy.orm import sessionmaker, declarative_base

# ---------------------------------------------------------------------------
# Standalone sync DB connection (শুধু এই script-এর জন্য, app-er async engine er sathe mixed na)
# connection string apnar .env / app/core/config theke niye, শুধু asyncpg বাদ দিয়ে psycopg2 বসিয়ে দিন
# ---------------------------------------------------------------------------
DATABASE_URL = "postgresql+psycopg2://postgres:123456@localhost:5432/medicine_store"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class MasterMedicine(Base):
    __tablename__ = "master_medicines"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, unique=True, nullable=False)
    sku = Column(String(100))
    generic_name = Column(String(200), nullable=False)
    brand_name = Column(String(200), nullable=False)
    strength = Column(String(100))
    dosage_form = Column(String(100))
    manufacturer = Column(String(200))
    reference_sale_price = Column(Numeric(10, 2))
    reference_mrp = Column(Numeric(10, 2))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


BASE_URL = "https://hospital.manazr.com/api/medicinelist"
RESULT_CAP = 20
MAX_TERM_LENGTH = 3
CHARSET = string.ascii_lowercase + string.digits

all_records = {}


def to_decimal(value):
    try:
        return Decimal(value) if value not in (None, "N/A") else None
    except InvalidOperation:
        return None


def search(client: httpx.Client, term: str):
    response = client.get(BASE_URL, params={"search": term})
    response.raise_for_status()
    return response.json()


def collect(client: httpx.Client, term: str):
    records = search(client, term)

    if not isinstance(records, list):
        return

    for item in records:
        all_records[item["id"]] = item

    print(f"search='{term}' -> {len(records)} item")

    if len(records) >= RESULT_CAP and len(term) < MAX_TERM_LENGTH:
        for ch in CHARSET:
            collect(client, term + ch)


def crawl_all():
    with httpx.Client(timeout=30.0) as client:
        collect(client, "")
        for ch in CHARSET:
            collect(client, ch)


def upsert_to_db():
    db = SessionLocal()
    total = 0

    for item in all_records.values():
        existing = db.query(MasterMedicine).filter(
            MasterMedicine.source_id == item["id"]
        ).first()

        if existing:
            existing.sku = item.get("sku")
            existing.generic_name = item.get("generic_name")
            existing.brand_name = item.get("brand_name")
            existing.strength = item.get("strength")
            existing.dosage_form = item.get("dosage_form")
            existing.manufacturer = item.get("manufacturer")
            existing.reference_sale_price = to_decimal(item.get("sale_price"))
            existing.reference_mrp = to_decimal(item.get("mrp"))
        else:
            medicine = MasterMedicine(
                source_id=item["id"],
                sku=item.get("sku"),
                generic_name=item.get("generic_name"),
                brand_name=item.get("brand_name"),
                strength=item.get("strength"),
                dosage_form=item.get("dosage_form"),
                manufacturer=item.get("manufacturer"),
                reference_sale_price=to_decimal(item.get("sale_price")),
                reference_mrp=to_decimal(item.get("mrp")),
            )
            db.add(medicine)

        total += 1

    db.commit()
    db.close()
    print(f"Total unique medicines synced: {total}")


def sync_medicines():
    crawl_all()
    print(f"\nTotal unique medicines found: {len(all_records)}")
    upsert_to_db()


if __name__ == "__main__":
    sync_medicines()
