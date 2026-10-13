import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.permissions import MODULE_LABELS, iter_permission_codes
from app.depends.deps import get_current_store_member, get_current_user, get_effective_permissions, require_permission
from app.models.all_models import Branch, Permission, Role, RolePermission, StoreMember, User
from app.schemas.rbac import (
    PermissionOut, RoleIn, RoleUpdateIn, RoleOut, StaffAssignIn, StaffOut,
)

router = APIRouter(prefix="", tags=["roles"])

# deps.require_store_admin এই slug দুটো দেখে admin ধরে -> কোনো custom role এগুলো পাবে না
RESERVED_SLUGS = {"owner", "manager"}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


async def _unique_slug(db: AsyncSession, store_id: int, name: str) -> str:
    """Bengali নাম হলে slugify() খালি আসে -> 'role' fallback; store-এ duplicate ও reserved হলে -2, -3 ..."""
    base = slugify(name) or "role"
    slug, n = base, 1
    while True:
        taken = await db.scalar(select(Role.id).where(Role.store_id == store_id, Role.slug == slug))
        if taken is None and slug not in RESERVED_SLUGS:
            return slug
        n += 1
        slug = f"{base}-{n}"


def _is_unrestricted(member: StoreMember, user: User) -> bool:
    return user.is_super_admin or "*" in get_effective_permissions(member)


def _assert_can_grant(member: StoreMember, user: User, codes: set[str], what: str) -> None:
    """নিজের যে permission নেই সেটা অন্য কাউকে দেওয়া/তার role বদলানো যাবে না (privilege escalation বন্ধ)।"""
    if _is_unrestricted(member, user):
        return
    beyond = codes - get_effective_permissions(member)
    if beyond:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{what}: আপনার নিজের নেই এমন permission আছে: {sorted(beyond)}",
        )


async def _codes_to_permissions(db: AsyncSession, codes: set[str]) -> list[Permission]:
    valid_codes = {code for _, _, code in iter_permission_codes()}
    bad = codes - valid_codes
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid permission code(s): {sorted(bad)}")
    if not codes:
        return []
    perms = (await db.execute(select(Permission).where(Permission.code.in_(codes)))).scalars().all()
    missing = codes - {p.code for p in perms}
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Permission(s) not seeded in DB yet: {sorted(missing)} (seed_permissions চালান)",
        )
    return list(perms)


# ---------------------------------------------------------------------------
# Permission catalog (read-only reference)
# ---------------------------------------------------------------------------

@router.get("/roles/permissions", response_model=dict[str, list[PermissionOut]])
async def list_permission_catalog(
    member: StoreMember = Depends(get_current_store_member),
):
    """Module wise grouped catalog — role তৈরির form এ checkbox list বানাতে দরকার হবে।"""
    grouped: dict[str, list[PermissionOut]] = {}
    for module, action, code in iter_permission_codes():
        grouped.setdefault(module, []).append(
            PermissionOut(code=code, module=module, action=action,
                          label=f"{MODULE_LABELS.get(module, module)} — {action}")
        )
    return grouped


# ---------------------------------------------------------------------------
# Roles CRUD — permission "staff:*" (role slug-এর উপর নির্ভর করে না)
# ---------------------------------------------------------------------------

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    member: StoreMember = Depends(require_permission("staff", "read")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Role)
        .options(selectinload(Role.permission_links).selectinload(RolePermission.permission))
        .where(Role.store_id == member.store_id)
        .order_by(Role.id)
    )
    roles = result.scalars().all()
    return [
        RoleOut(
            id=r.id, name=r.name, slug=r.slug, description=r.description, is_system=r.is_system,
            permission_codes=sorted(link.permission.code for link in r.permission_links),
        )
        for r in roles
    ]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleIn,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("staff", "create")),
    db: AsyncSession = Depends(get_db),
):
    if slugify(payload.name) == "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="এই নামে role বানানো যাবে না")

    exists = await db.execute(
        select(Role).where(Role.store_id == member.store_id, Role.name == payload.name)
    )
    if exists.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="এই নামে role আগে থেকেই আছে")

    codes = set(payload.permission_codes)
    perms = await _codes_to_permissions(db, codes)
    _assert_can_grant(member, current_user, codes, "Role তৈরি")

    role = Role(store_id=member.store_id, name=payload.name,
                slug=await _unique_slug(db, member.store_id, payload.name),
                description=payload.description, is_system=False)
    db.add(role)
    await db.flush()
    db.add_all([RolePermission(role_id=role.id, permission_id=p.id) for p in perms])
    await db.flush()

    out = RoleOut(
        id=role.id, name=role.name, slug=role.slug, description=role.description, is_system=role.is_system,
        permission_codes=sorted(codes),
    )
    await db.commit()  # commit এর পরে ORM object ধরা হয় না -> expire সমস্যা নেই
    return out


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: int,
    payload: RoleUpdateIn,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("staff", "update")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Role)
        .options(selectinload(Role.permission_links).selectinload(RolePermission.permission))
        .where(Role.id == role_id, Role.store_id == member.store_id)
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_system:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="System role (Owner) edit করা যাবে না")

    current_codes = {link.permission.code for link in role.permission_links}
    # নিজের চেয়ে বেশি ক্ষমতার role (যেমন নিজের উপরের কেউ) এডিট করা যাবে না
    _assert_can_grant(member, current_user, current_codes, "এই role এডিট")

    if payload.name is not None and payload.name != role.name:
        if slugify(payload.name) == "owner":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="এই নামে role বানানো যাবে না")
        dup = await db.scalar(
            select(Role.id).where(Role.store_id == member.store_id, Role.name == payload.name, Role.id != role.id)
        )
        if dup is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="এই নামে role আগে থেকেই আছে")
        # slug আর বদলায় না (stable identity) — rename করে "manager" slug দখল/হারানো যাবে না
        role.name = payload.name
    if payload.description is not None:
        role.description = payload.description

    new_codes = current_codes
    if payload.permission_codes is not None:
        new_codes = set(payload.permission_codes)
        perms = await _codes_to_permissions(db, new_codes)
        _assert_can_grant(member, current_user, new_codes, "Role আপডেট")

        for link in list(role.permission_links):
            await db.delete(link)
        await db.flush()
        db.add_all([RolePermission(role_id=role.id, permission_id=p.id) for p in perms])

    await db.flush()
    out = RoleOut(
        id=role.id, name=role.name, slug=role.slug, description=role.description, is_system=role.is_system,
        permission_codes=sorted(new_codes),
    )
    await db.commit()
    return out


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    current_user: User = Depends(get_current_user),
    member: StoreMember = Depends(require_permission("staff", "delete")),
    db: AsyncSession = Depends(get_db),
):
    role = (await db.execute(
        select(Role)
        .options(selectinload(Role.permission_links).selectinload(RolePermission.permission))
        .where(Role.id == role_id, Role.store_id == member.store_id)
    )).scalar_one_or_none()
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.is_system:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="System role (Owner) ডিলিট করা যাবে না")
    _assert_can_grant(member, current_user, {l.permission.code for l in role.permission_links}, "এই role ডিলিট")

    in_use = (await db.execute(select(StoreMember.id).where(StoreMember.role_id == role_id).limit(1))).first()
    if in_use:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="এই role এ staff assigned আছে, আগে তাদের অন্য role এ সরান")

    await db.delete(role)
    await db.commit()


# ---------------------------------------------------------------------------
# Staff: role + branch assignment
# ---------------------------------------------------------------------------

@router.get("/staff", response_model=list[StaffOut])
async def list_staff(
    member: StoreMember = Depends(require_permission("staff", "read")),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(StoreMember)
        .options(selectinload(StoreMember.role))
        .where(StoreMember.store_id == member.store_id)
        .order_by(StoreMember.id)
    )
    if member.branch_id is not None:  # branch-bound admin শুধু নিজের branch-এর staff দেখবে
        stmt = stmt.where(StoreMember.branch_id == member.branch_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        StaffOut(id=m.id, user_id=m.user_id, role_id=m.role_id, role_name=m.role.name,
                 branch_id=m.branch_id, is_active=m.is_active)
        for m in rows
    ]


@router.patch("/staff/{member_id}", response_model=StaffOut)
async def assign_staff_role_and_branch(
    member_id: int,
    payload: StaffAssignIn,
    current_user: User = Depends(get_current_user),
    admin: StoreMember = Depends(require_permission("staff", "update")),
    db: AsyncSession = Depends(get_db),
):
    """
    - role_id: এই staff কোন permission set পাবে (শুধু নিজের permission-এর ভেতরের role দেওয়া যাবে)
    - branch_id: কোন branch-এ bound (None = store-wide; branch-bound admin এটা দিতে পারবে না)
    """
    target = (await db.execute(
        select(StoreMember).options(selectinload(StoreMember.role))
        .where(StoreMember.id == member_id, StoreMember.store_id == admin.store_id)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Staff member not found")

    if target.role.is_system and target.role.slug == "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Owner এর role/branch এভাবে বদলানো যাবে না")

    admin_branch: Optional[int] = admin.branch_id
    unrestricted = _is_unrestricted(admin, current_user)

    if admin_branch is not None and not current_user.is_super_admin:
        # branch-bound admin নিজের branch-এর বাইরে/store-wide করতে পারবে না (নিজেকেও না)
        if target.branch_id != admin_branch:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="অন্য branch এর staff বদলানো যাবে না")
        if payload.branch_id != admin_branch:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="আপনি শুধু নিজের branch এ staff রাখতে পারবেন")

    # যার ক্ষমতা আমার চেয়ে বেশি তাকে বদলানো যাবে না
    if not unrestricted:
        _assert_can_grant(admin, current_user, target.role.permission_codes, "এই staff এর বর্তমান role")

    new_role = (await db.execute(
        select(Role).where(Role.id == payload.role_id, Role.store_id == admin.store_id)
    )).scalar_one_or_none()
    if new_role is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid role_id")
    if new_role.is_system and new_role.slug == "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="আরেকজনকে Owner role assign করা যাবে না")
    _assert_can_grant(admin, current_user, new_role.permission_codes, "নতুন role")

    if payload.branch_id is not None:
        branch = (await db.execute(
            select(Branch).where(
                Branch.id == payload.branch_id,
                Branch.store_id == admin.store_id,
                Branch.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if branch is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid branch_id")

    target.role_id = new_role.id
    target.branch_id = payload.branch_id
    await db.flush()
    out = StaffOut(id=target.id, user_id=target.user_id, role_id=new_role.id, role_name=new_role.name,
                   branch_id=payload.branch_id, is_active=target.is_active)
    await db.commit()
    return out
