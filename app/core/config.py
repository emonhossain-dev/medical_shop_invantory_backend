import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:123456@localhost:5432/medicine_store",
    #"postgresql+asyncpg://medicine_inventory_3bk2_user:QcO9ijDrptI134qo0J6t7QPK4mvkg03o@dpg-da4lobrl550s738a7ro0-a.oregon-postgres.render.com/medicine_inventory_3bk2",
)
SECRET_KEY = os.getenv("SECRET_KEY", "SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = 30

# --- SMTP / email settings ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")          # e.g. your Gmail address, or provider username
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")  # for Gmail: an "App Password", not your normal password
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)

# --- Used to build the reset link sent in the email ---
PASSWORD_RESET_URL = os.getenv("PASSWORD_RESET_URL", "http://127.0.0.1:8000/reset-password")

GRACE_PERIOD_DAYS: int = 7          # active → past_due হওয়ার পর কতদিন গ্রেস
LIFECYCLE_CHECK_HOUR: int = 1       # দিনে কখন চলবে (UTC hour), e.g. রাত ১টায়


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
NAGAD_MERCHANT_PRIVATE_KEY = os.getenv("NAGAD_MERCHANT_PRIVATE_KEY", "")  # PEM string, .env এ পুরো key
NAGAD_PG_PUBLIC_KEY = os.getenv("NAGAD_PG_PUBLIC_KEY", "")                # Nagad এর দেওয়া public key
NAGAD_CALLBACK_URL = os.getenv("NAGAD_CALLBACK_URL", "http://127.0.0.1:8000/store/payments/nagad/callback")

# --- Manual payment receive numbers (store owner কে দেখানো হবে) ---
MANUAL_BKASH_NUMBER = os.getenv("MANUAL_BKASH_NUMBER", "01XXXXXXXXX")
MANUAL_NAGAD_NUMBER = os.getenv("MANUAL_NAGAD_NUMBER", "01XXXXXXXXX")