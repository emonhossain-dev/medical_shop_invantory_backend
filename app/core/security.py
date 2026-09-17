import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from app.core.config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    REFRESH_TOKEN_EXPIRE_DAYS,
    SECRET_KEY,
)

PASSWORD_RESET_EXPIRE_MINUTES = 15

# ============================================================
# PASSWORD HASHING
# ============================================================


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")  # store as str, not bytes


def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


# ============================================================
# ACCESS TOKEN
#
# IMPORTANT: every token type below uses "sub" (JWT's standard
# "subject" claim) to carry the user id, and a "purpose" claim to
# say what the token is for. deps.py, and the refresh/reset flows,
# all read "sub" -- keep this consistent everywhere a token is
# issued or read, or auth will silently break.
# ============================================================


def encode_access(user_id: int, email: str) -> str:
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "email": email,
        "purpose": "access",
        "exp": expire_at,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access(token: str) -> dict:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("purpose") != "access":
        raise jwt.InvalidTokenError("This token cannot be used as an access token")
    return payload


def verify_token(token: str) -> Optional[dict]:
    """Best-effort decode for use in dependencies: returns the payload,
    or None if the token is missing/expired/invalid, instead of raising.
    Callers that need a specific error message (login flows, refresh,
    reset) should call decode_access/decode_refresh_token/decode_reset_token
    directly and handle jwt.ExpiredSignatureError / jwt.InvalidTokenError."""
    try:
        return decode_access(token)
    except jwt.PyJWTError:
        return None


# ============================================================
# REFRESH TOKEN
# ============================================================


def encode_refresh_token(user_id: int) -> str:
    """Long-lived token used ONLY to obtain a new access token -- separate
    purpose claim so a leaked access token can never be replayed as a
    refresh token."""
    expire_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "purpose": "refresh",
        "exp": expire_at,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_refresh_token(token: str) -> dict:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("purpose") != "refresh":
        raise jwt.InvalidTokenError("This token cannot be used as a refresh token")
    return payload


def hash_token(token: str) -> str:
    """SHA-256 hash of a refresh token, for storage in RefreshToken.token_hash.
    We never store the raw refresh token, so a DB leak alone can't be
    replayed -- the attacker would still need the original JWT."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ============================================================
# PASSWORD RESET TOKEN
# ============================================================


def encode_reset_token(user_id: int) -> str:
    """Short-lived token used ONLY for password reset -- separate from the
    login access token so a normal login session can never be used to
    reset a password."""
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "purpose": "password_reset",
        "exp": expire_at,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_reset_token(token: str) -> dict:
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    if payload.get("purpose") != "password_reset":
        raise jwt.InvalidTokenError("This token cannot be used for password reset")
    return payload