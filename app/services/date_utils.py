# app/services/date_utils.py
"""Asia/Dhaka ভিত্তিক দিন-সীমা। DB-তে created_at UTC (timezone-aware) ধরা হয়েছে।
date_from / date_to দুটোই INCLUSIVE (ক্যালেন্ডার দিন)।"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

DHAKA = ZoneInfo("Asia/Dhaka")


def dhaka_now() -> datetime:
    return datetime.now(DHAKA)


def dhaka_today() -> date:
    return dhaka_now().date()


def to_dhaka_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DHAKA).date()


def day_start_utc(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=DHAKA).astimezone(timezone.utc)


def apply_date_range(query, column, date_from: Optional[date], date_to: Optional[date]):
    if date_from is not None:
        query = query.where(column >= day_start_utc(date_from))
    if date_to is not None:
        query = query.where(column < day_start_utc(date_to + timedelta(days=1)))
    return query
