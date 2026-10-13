from datetime import datetime, timedelta, timezone
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.all_models import (
    Store,
    StoreMember,
    Branch,
    StoreStatus,
    User,
    Subscription,
    Role,
    RolePermission,
    Permission,
)
from app.depends.deps import get_current_user
from app.schemas.store import (
    StoreCreateRequest,
    StoreResponse,
    StoreListItem,
    StoreUpdateRequest,
    StoreSettingsResponse,
    StoreSettingsUpdateRequest,
)


router = APIRouter(prefix="/store", tags=["Store"])


# -------------------- Logo upload config --------------------
# Local disk path where uploaded logo files are stored. In main.py this
# directory must be mounted as static files, e.g.:
#   app.mount("/static", StaticFiles(directory="static"), name="static")
# Change UPLOAD_DIR / PUBLIC_BASE_URL to match your actual static mount
# and/or CDN/object-storage setup if you're not just serving from disk.
UPLOAD_DIR = Path("static/uploads/store_logos")
PUBLIC_BASE_URL = "/static/uploads/store_logos"  # served path prefix, not a filesystem path

ALLOWED_LOGO_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
MAX_LOGO_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


def _delete_logo_file(logo_url: str | None) -> None:
    """Best-effort removal of the previous logo file from disk when it's
    being replaced or cleared. Never raises -- a missing/foreign file
    (e.g. an old externally-hosted URL from before this change) is fine."""
    if not logo_url or not logo_url.startswith(PUBLIC_BASE_URL):
        return
    filename = logo_url.rsplit("/", 1)[-1]
    old_path = UPLOAD_DIR / filename
    try:
        if old_path.is_file():
            old_path.unlink()
    except OSError:
        pass


async def save_store_logo(store_id: int, file: UploadFile) -> str:
    """Validates and saves an uploaded logo image to disk, returning the
    public URL path to store on Store.logo_url. Raises HTTPException on
    invalid type/size."""
    if file.content_type not in ALLOWED_LOGO_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PNG, JPEG, or WEBP images are allowed for the store logo",
        )

    contents = await file.read()
    if len(contents) > MAX_LOGO_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Logo image must be 2MB or smaller",
        )
    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    ext = ALLOWED_LOGO_CONTENT_TYPES[file.content_type]
    filename = f"store-{store_id}-{uuid.uuid4().hex}.{ext}"
    file_path = UPLOAD_DIR / filename

    with open(file_path, "wb") as f:
        f.write(contents)

    return f"{PUBLIC_BASE_URL}/{filename}"


def make_slug(text: str) -> str:
    """Pure Python slug generator"""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = re.sub(r'^-+|-+$', '', text)
    return text[:200] or "store"


async def generate_unique_slug(name: str, db: AsyncSession) -> str:
    base = make_slug(name)
    slug = base
    counter = 1

    while True:
        result = await db.execute(select(Store).where(Store.slug == slug))
        if not result.scalar_one_or_none():
            return slug
        slug = f"{base}-{counter}"
        counter += 1


async def get_store_or_404(store_id: int, db: AsyncSession) -> Store:
    result = await db.execute(select(Store).where(Store.id == store_id))
    store = result.scalar_one_or_none()
    if not store:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


async def get_store_with_relations_or_404(store_id: int, db: AsyncSession) -> Store:
    """Same as get_store_or_404 but eager-loads branches + members so that
    the ORM-level cascade="all, delete-orphan" on Store.branches /
    Store.members actually fires when we db.delete(store). Without loading
    these, SQLAlchemy doesn't know the children exist and won't cascade the
    delete, which would otherwise hit a FK constraint error at the DB."""
    result = await db.execute(
        select(Store)
        .options(
            selectinload(Store.branches),
            selectinload(Store.members),
        )
        .where(Store.id == store_id)
    )
    store = result.scalar_one_or_none()
    if not store:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return store


async def check_store_owner(store: Store, current_user: User) -> None:
    if store.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the store owner can perform this action"
        )


async def check_store_access(store: Store, current_user: User, db: AsyncSession) -> None:
    """Owner OR any active StoreMember (any role) can read the store /
    its settings. Reused by GET /{store_id} and GET /{store_id}/settings
    so both endpoints apply the same rule."""
    if store.owner_id == current_user.id:
        return
    result = await db.execute(
        select(StoreMember).where(
            StoreMember.store_id == store.id,
            StoreMember.user_id == current_user.id,
            StoreMember.is_active == True,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this store")


# -------------------- LIST (my stores) --------------------
@router.get("/my/list", response_model=list[StoreListItem])
async def list_my_stores(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns every store the current user has active access to, either as
    owner or as staff, along with their role in that store. The owner is
    also inserted as a StoreMember (role=owner) at creation time, so a
    single query against store_members covers both cases."""
    # StoreMember.role এখন Role model (custom per-store role), তাই Role এর
    # column গুলো সরাসরি select করছি -- এতে Role অবজেক্ট বা তার
    # permission_links lazy-load হয় না।
    result = await db.execute(
        select(Store, Role.id, Role.name, Role.slug)
        .select_from(StoreMember)
        .join(Store, StoreMember.store_id == Store.id)
        .join(Role, StoreMember.role_id == Role.id)
        .where(
            StoreMember.user_id == current_user.id,
            StoreMember.is_active == True,
        )
        .order_by(Store.created_at)
    )
    rows = result.all()

    return [
        StoreListItem(
            id=store.id,
            name=store.name,
            slug=store.slug,
            logo_url=store.logo_url,
            status=store.status,
            role=role_slug,
            role_id=role_id,
            role_name=role_name,
            is_owner=(store.owner_id == current_user.id),
        )
        for store, role_id, role_name, role_slug in rows
    ]


# -------------------- CREATE --------------------
@router.post("", response_model=StoreResponse, status_code=status.HTTP_201_CREATED)
async def create_store(
    data: StoreCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Slug handle
    if data.slug:
        existing = await db.execute(select(Store).where(Store.slug == data.slug))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This slug is already taken")
        slug = data.slug
    else:
        slug = await generate_unique_slug(data.name, db)

    # Store create
    store = Store(
        owner_id=current_user.id,
        name=data.name,
        slug=slug,
        address=data.address,
        phone=data.phone,
        status=StoreStatus.trial,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
    )
    db.add(store)
    await db.flush()

    # Main Branch
    main_branch = Branch(
        store_id=store.id,
        name="Main Branch",
        address=data.address,
        phone=data.phone,
        is_main=True,
        is_active=True,
    )
    db.add(main_branch)
    await db.flush()

    # System "Owner" role (সব permission সহ) -- প্রতিটা store এ একটা
    owner_role = Role(
        store_id=store.id,
        name="Owner",
        slug="owner",
        description="Store owner with full access",
        is_system=True,
    )
    db.add(owner_role)
    await db.flush()

    perm_ids = (await db.execute(select(Permission.id))).scalars().all()
    db.add_all([
        RolePermission(role_id=owner_role.id, permission_id=pid)
        for pid in perm_ids
    ])

    # Owner as StoreMember — branch_id ইচ্ছাকৃতভাবে None রাখা হচ্ছে, যাতে
    # owner কোনো একটা branch-এ bound না হয়ে store-এর সব branch access পায়
    # (stock/staff endpoint গুলোতে branch_id=None মানেই "all branches").
    membership = StoreMember(
        store_id=store.id,
        user_id=current_user.id,
        branch_id=None,
        role_id=owner_role.id,
        is_active=True,
    )
    db.add(membership)

    await db.commit()
    await db.refresh(store)

    return StoreResponse.model_validate(store)


# -------------------- GET --------------------
@router.get("/{store_id}", response_model=StoreResponse)
async def get_store(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_or_404(store_id, db)
    await check_store_access(store, current_user, db)
    return StoreResponse.model_validate(store)


# -------------------- UPDATE --------------------
@router.put("/{store_id}", response_model=StoreResponse)
async def update_store(
    store_id: int,
    data: StoreUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_or_404(store_id, db)
    await check_store_owner(store, current_user)

    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    for field, value in update_data.items():
        setattr(store, field, value)

    store.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(store)

    return StoreResponse.model_validate(store)


# -------------------- SETTINGS (logo / invoice template / currency / timezone) --------------------
@router.get("/{store_id}/settings", response_model=StoreSettingsResponse)
async def get_store_settings(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Any active member (or the owner) can view settings -- read-only for
    staff, so cashiers/pharmacists can e.g. render invoices with the
    correct currency/timezone without being able to change them."""
    store = await get_store_or_404(store_id, db)
    await check_store_access(store, current_user, db)
    return StoreSettingsResponse.model_validate(store)


@router.put("/{store_id}/settings", response_model=StoreSettingsResponse)
async def update_store_settings(
    store_id: int,
    data: StoreSettingsUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only, kept separate from PUT /{store_id} so that logo/invoice/
    currency/timezone changes go through their own endpoint (and can later
    get their own permission if staff should be allowed to manage them)."""
    store = await get_store_or_404(store_id, db)
    await check_store_owner(store, current_user)

    update_data = data.model_dump(exclude_unset=True)

    # timezone is intentionally not user-editable -- always stays at the
    # DB default ("Asia/Dhaka"). Drop it silently even if the client sends
    # it, instead of erroring, so existing clients don't break.
    update_data.pop("timezone", None)

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    if "currency" in update_data:
        update_data["currency"] = update_data["currency"].upper()

    for field, value in update_data.items():
        setattr(store, field, value)

    store.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(store)

    return StoreSettingsResponse.model_validate(store)


# -------------------- LOGO UPLOAD --------------------
@router.post("/{store_id}/logo", response_model=StoreSettingsResponse)
async def upload_store_logo(
    store_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only. Accepts a multipart image file directly (instead of a
    logo_url string), saves it to disk, deletes the previous logo file if
    any, and updates Store.logo_url to the new public path."""
    store = await get_store_or_404(store_id, db)
    await check_store_owner(store, current_user)

    old_logo_url = store.logo_url
    new_logo_url = await save_store_logo(store_id, file)

    store.logo_url = new_logo_url
    store.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(store)

    _delete_logo_file(old_logo_url)

    return StoreSettingsResponse.model_validate(store)


@router.delete("/{store_id}/logo", response_model=StoreSettingsResponse)
async def delete_store_logo(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owner-only. Removes the store's logo file from disk and clears
    Store.logo_url."""
    store = await get_store_or_404(store_id, db)
    await check_store_owner(store, current_user)

    old_logo_url = store.logo_url
    store.logo_url = None
    store.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(store)

    _delete_logo_file(old_logo_url)

    return StoreSettingsResponse.model_validate(store)


@router.delete("/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    store = await get_store_with_relations_or_404(store_id, db)
    await check_store_owner(store, current_user)

    # Subscription has no cascade defined on the Store side, so deleting a
    # store with a live (trialing/active) subscription would either orphan
    # billing records or blow up on the FK -- block it explicitly and make
    # the owner cancel/downgrade first. Adjust the status list if your
    # SubscriptionStatus enum uses different values.
    result = await db.execute(
        select(Subscription).where(
            Subscription.store_id == store_id,
            Subscription.status.in_(["trialing", "active"]),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a store with an active or trialing subscription. Cancel it first.",
        )

    # branches + members are already eager-loaded, so the ORM-level
    # cascade="all, delete-orphan" on Store.branches / Store.members will
    # correctly emit DELETE statements for them before/along with the store.
    old_logo_url = store.logo_url

    await db.delete(store)
    await db.commit()

    _delete_logo_file(old_logo_url)
    return None