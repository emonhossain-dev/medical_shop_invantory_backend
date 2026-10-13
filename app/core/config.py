import os
from dotenv import load_dotenv

load_dotenv()

# --- Database ---
_raw_db_url = os.getenv("DATABASE_URL")

# Render/Heroku পুরনো "postgres://" scheme দেয়।
# SQLAlchemy async engine শুধু "postgresql+asyncpg://" চিনে।
def _to_async_url(url: str | None) -> str | None:
    if not url:
        return url
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # already postgresql+asyncpg:// বা অন্য কিছু হলে অপরিবর্তিত
    return url

DATABASE_URL = _to_async_url(_raw_db_url)

SECRET_KEY = os.getenv("SECRET_KEY", "SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = 30

# --- SMTP / email settings ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)

# --- Used to build the reset link sent in the email ---
PASSWORD_RESET_URL = os.getenv("PASSWORD_RESET_URL", "http://127.0.0.1:8000/reset-password")

GRACE_PERIOD_DAYS: int = 7
LIFECYCLE_CHECK_HOUR: int = 1

# --- bKash Tokenized Checkout ---
BKASH_BASE_URL = os.getenv("BKASH_BASE_URL", "https://tokenized.sandbox.bka.sh/v1.2.0-beta")
BKASH_APP_KEY = os.getenv("BKASH_APP_KEY", "")
BKASH_APP_SECRET = os.getenv("BKASH_APP_SECRET", "")
BKASH_USERNAME = os.getenv("BKASH_USERNAME", "")
BKASH_PASSWORD = os.getenv("BKASH_PASSWORD", "")
BKASH_CALLBACK_URL = os.getenv("BKASH_CALLBACK_URL", "http://127.0.0.1:8000/store/payments/bkash/callback")

# --- Nagad Checkout ---
NAGAD_BASE_URL = os.getenv("NAGAD_BASE_URL", "https://sandbox.mynagad.com:10443/remote-payment-gateway-1.0")
NAGAD_MERCHANT_ID = os.getenv("NAGAD_MERCHANT_ID", "")
NAGAD_MERCHANT_PRIVATE_KEY = os.getenv("NAGAD_MERCHANT_PRIVATE_KEY", "")
NAGAD_PG_PUBLIC_KEY = os.getenv("NAGAD_PG_PUBLIC_KEY", "")
NAGAD_CALLBACK_URL = os.getenv("NAGAD_CALLBACK_URL", "http://127.0.0.1:8000/store/payments/nagad/callback")

# --- Manual payment receive numbers ---
MANUAL_BKASH_NUMBER = os.getenv("MANUAL_BKASH_NUMBER", "01XXXXXXXXX")
MANUAL_NAGAD_NUMBER = os.getenv("MANUAL_NAGAD_NUMBER", "01XXXXXXXXX")