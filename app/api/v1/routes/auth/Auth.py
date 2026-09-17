import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional

import jwt
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import ACCESS_TOKEN_EXPIRE_MINUTES, PASSWORD_RESET_URL, REFRESH_TOKEN_EXPIRE_DAYS
from app.core.database import get_db
from app.core.security import (
    decode_refresh_token,
    decode_reset_token,
    encode_access,
    encode_refresh_token,
    encode_reset_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.depends.deps import get_current_user
from app.models.all_models import RefreshToken, StoreMember, User
from app.schemas.user import (
    UserLoginRequest,
    UserLoginResponse,
    UserResponse,
    UserTokenResponse,
    UserUpdateRequest,
)
from app.utils.email import send_email

router = APIRouter()

# ============================================================
# FORGOT / RESET PASSWORD SCHEMAS
# ============================================================


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class RefreshTokenRequest(BaseModel):
    refresh_token: str


# ============================================================
# PROFILE IMAGE UPLOAD HELPERS
# ============================================================

UPLOAD_DIR = Path("static/profile_images")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_FILE_SIZE_MB = 5


async def save_profile_image(file: UploadFile) -> str:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    contents = await file.read()
    size_mb = len(contents) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(status_code=400, detail=f"File too large. Max size is {MAX_FILE_SIZE_MB}MB")

    filename = f"{uuid.uuid4().hex}{extension}"
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as f:
        f.write(contents)

    return f"/static/profile_images/{filename}"


def delete_profile_image(image_path: Optional[str]) -> None:
    if not image_path:
        return
    filename = Path(image_path).name
    file_path = UPLOAD_DIR / filename
    if file_path.exists():
        try:
            file_path.unlink()
        except OSError:
            pass


# ============================================================
# REFRESH TOKEN PERSISTENCE HELPERS
# ============================================================


async def _issue_tokens(db: AsyncSession, user: User, request: Request) -> UserTokenResponse:
    """Issues an access + refresh token pair and persists a hashed row for
    the refresh token so it can be looked up / revoked server-side later."""
    access_token = encode_access(user_id=user.id, email=user.email)
    refresh_token_str = encode_refresh_token(user_id=user.id)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token_str),
            device_info=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    await db.commit()

    return UserTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token_str,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ============================================================
# ROUTES
# ============================================================


@router.post("/register", response_model=UserResponse)
async def register(
    full_name: str = Form(..., min_length=1, max_length=150),
    email: EmailStr = Form(...),
    phone: Optional[str] = Form(None, max_length=20),
    password: str = Form(..., min_length=8),
    profile_image: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
):
    email = str(email).lower().strip()
    phone = phone.strip() if phone else None

    email_result = await db.execute(select(User).where(User.email == email))
    if email_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="This email is already registered")

    if phone:
        phone_result = await db.execute(select(User).where(User.phone == phone))
        if phone_result.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="This phone number is already registered")

    image_url = None
    if profile_image is not None:
        image_url = await save_profile_image(profile_image)

    new_user = User(
        full_name=full_name.strip(),
        email=email,
        phone=phone,
        password_hash=hash_password(password.strip()),  # never store plain text passwords
        profile_image=image_url,
    )
    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email or phone number is already registered")
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Internal Server Error")

    # refresh() relationship eager-load করে না, তাই selectinload দিয়ে আবার fetch করুন
    result = await db.execute(
        select(User)
        .options(selectinload(User.store_memberships).selectinload(StoreMember.branch))
        .where(User.id == new_user.id)
    )
    new_user = result.scalar_one()

    return UserResponse.model_validate(new_user)


@router.post("/login", response_model=UserLoginResponse)
async def login(request: UserLoginRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    email = request.email.lower().strip()
    password = request.password.strip()

    result = await db.execute(select(User).options(selectinload(User.store_memberships).selectinload(StoreMember.branch)).where(User.email == email))
    user = result.scalar_one_or_none()

    if user and user.is_active and verify_password(password, user.password_hash):
        token = await _issue_tokens(db, user, http_request)
        return UserLoginResponse(user=UserResponse.model_validate(user), token=token)

    raise HTTPException(status_code=401, detail="Incorrect email or password")


@router.post("/refresh-token", response_model=UserTokenResponse)
async def refresh_token(request: RefreshTokenRequest, http_request: Request, db: AsyncSession = Depends(get_db)):
    """Exchanges a valid, non-revoked refresh token for a new access +
    refresh token pair (rotation). The old refresh token's DB row is
    revoked as part of the rotation, so a stolen-and-reused old token
    fails even though the JWT itself hasn't expired yet."""
    try:
        payload = decode_refresh_token(request.refresh_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired, please login again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    token_hash = hash_token(request.refresh_token)

    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    stored_token = result.scalar_one_or_none()
    if stored_token is None or stored_token.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Refresh token has been revoked, please login again")

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    stored_token.revoked_at = datetime.now(timezone.utc)  # rotation: this token can't be reused
    await db.commit()

    return await _issue_tokens(db, user, http_request)


@router.post("/logout")
async def logout(
    request: RefreshTokenRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Revokes the given refresh token's DB row. The client must also
    discard the access token locally -- it stays valid (stateless JWT)
    until its own short expiry passes."""
    token_hash = hash_token(request.refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.user_id == current_user.id,
        )
    )
    stored_token = result.scalar_one_or_none()
    if stored_token is not None and stored_token.revoked_at is None:
        stored_token.revoked_at = datetime.now(timezone.utc)
        await db.commit()

    return {"detail": "Logged out successfully"}


@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    email = str(request.email).lower().strip()

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is not None:
        reset_token = encode_reset_token(user.id)
        reset_link = f"{PASSWORD_RESET_URL}?token={reset_token}"
        try:
            await send_email(
                to=user.email,
                subject="Reset your password",
                body=(
                    f"Hi {user.full_name},\n\n"
                    f"Click the link below to reset your password. This link expires in 15 minutes.\n\n"
                    f"{reset_link}\n\n"
                    f"If you didn't request this, you can safely ignore this email."
                ),
            )
        except Exception as e:
            # Don't leak SMTP failures to the client -- log server-side and
            # still return the generic message below so this endpoint can't
            # be used to probe which emails are registered.
            print(f"FAILED TO SEND RESET EMAIL to {user.email}: {e}")

    return {"detail": "If this email is registered, a password reset link has been sent"}


@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    try:
        payload = decode_reset_token(request.token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="Reset link has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user.password_hash = hash_password(request.new_password.strip())

    # A password reset is a strong signal of a compromised or forgotten
    # credential -- revoke every existing session so old refresh tokens
    # stop working immediately.
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
    )
    for token_row in result.scalars():
        token_row.revoked_at = datetime.now(timezone.utc)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Internal Server Error")

    return {"detail": "Password has been reset successfully"}


@router.get("/profile", response_model=UserResponse)
async def profile(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    # get_current_user শুধু User row লোড করে -- store_memberships এবং তার
    # nested branch relationship eager-load করা থাকে না, তাই এখানে আলাদা
    # করে selectinload সহ re-fetch করা হচ্ছে। নাহলে response serialize করার
    # সময় lazy-load ট্রিগার হয়ে MissingGreenlet error দেয়।
    result = await db.execute(
        select(User)
        .options(selectinload(User.store_memberships).selectinload(StoreMember.branch))
        .where(User.id == current_user.id)
    )
    user = result.scalar_one()
    return UserResponse.model_validate(user)


@router.put("/profile-update", response_model=UserResponse)
async def update_profile(
    request: UserUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.store_memberships).selectinload(StoreMember.branch))  # আপনার relationship নাম অনুযায়ী
        .where(User.id == current_user.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if request.full_name is not None:
        user.full_name = request.full_name.strip()
    if request.phone is not None:
        phone = request.phone.strip()
        if phone != user.phone:
            phone_result = await db.execute(select(User).where(User.phone == phone))
            if phone_result.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail="This phone number is already registered")
        user.phone = phone

    try:
        await db.commit()
        await db.refresh(user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="This phone number is already registered")
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Internal Server Error")

    return UserResponse.model_validate(user)


@router.patch("/profile/image", response_model=UserResponse)
async def update_profile_image(
    current_user: Annotated[User, Depends(get_current_user)],
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.store_memberships).selectinload(StoreMember.branch))
        .where(User.id == current_user.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    old_image = user.profile_image
    new_image_url = await save_profile_image(image)
    user.profile_image = new_image_url

    try:
        await db.commit()
        await db.refresh(user)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Internal Server Error")

    delete_profile_image(old_image)  # clean up the old file only after a successful commit

    return UserResponse.model_validate(user)