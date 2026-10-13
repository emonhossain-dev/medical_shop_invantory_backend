
CRUD = ["create", "read", "update", "delete"]

PERMISSION_CATALOG: dict[str, list[str]] = {
    "medicines":            CRUD,
    "suppliers":             CRUD,
    "purchases":              CRUD + ["approve"],          # requisition -> purchase approve flow-এর সাথেও শেয়ার হয়
    "purchase_requisitions":  CRUD + ["approve", "reject", "order", "complete", "cancel"],
    "stock":                  CRUD + ["adjust", "release"],  # stock_batches / stock_adjustments / stock_receipts / stock_releases
    "stock_transfers":        CRUD + ["receive", "cancel"],
    "sales":                   CRUD + ["void"],
    "customers":               CRUD,
    "due_payments":            CRUD + ["collect"],
    "branches":                 CRUD,
    "staff":                    CRUD,                        # store_members + roles ম্যানেজমেন্ট
    "reports":                   ["read", "export"],
}

# human-readable label (UI dropdown এ দেখানোর জন্য) — চাইলে Bengali দিন
MODULE_LABELS = {
    "medicines": "ঔষধ",
    "suppliers": "সাপ্লায়ার",
    "purchases": "ক্রয়",
    "purchase_requisitions": "রিকুইজিশন",
    "stock": "স্টক",
    "stock_transfers": "স্টক ট্রান্সফার",
    "sales": "বিক্রয়",
    "customers": "কাস্টমার",
    "due_payments": "বাকি পেমেন্ট",
    "branches": "শাখা",
    "staff": "স্টাফ / রোল",
    "reports": "রিপোর্ট",
}


def iter_permission_codes():
    """yields (module, action, code) for every catalog entry"""
    for module, actions in PERMISSION_CATALOG.items():
        for action in actions:
            yield module, action, f"{module}:{action}"


# ---------------------------------------------------------------------------
# Seed script — একবার migration এর পরে চালান (idempotent, re-run করলে safe)
# ---------------------------------------------------------------------------
# app/scripts/seed_permissions.py হিসেবে রাখতে পারেন, অথবা alembic
# data-migration এর ভেতরেও call করতে পারেন।

async def seed_permissions(db) -> None:
    from sqlalchemy import select
    from app.models.all_models import Permission

    existing = {p.code for p in (await db.execute(select(Permission))).scalars().all()}

    to_add = []
    for module, action, code in iter_permission_codes():
        if code in existing:
            continue
        to_add.append(
            Permission(
                module=module,
                action=action,
                code=code,
                label=f"{MODULE_LABELS.get(module, module)} — {action}",
            )
        )
    if to_add:
        db.add_all(to_add)
        await db.flush()


# ---------------------------------------------------------------------------
# নতুন store তৈরি হওয়ার সময় call করুন — system "Owner" role বানিয়ে দেয়
# (সব permission সহ, rename/delete করা যাবে না) এবং চাইলে কিছু starter
# preset role (Manager / Staff) ও বানিয়ে দিতে পারেন — এগুলো normal role,
# পরে owner চাইলে edit/delete করতে পারবে।
# ---------------------------------------------------------------------------

async def bootstrap_store_roles(db, store_id: int) -> "Role":
    from sqlalchemy import select
    from app.models.all_models import Permission, Role, RolePermission

    all_perms = (await db.execute(select(Permission))).scalars().all()

    owner_role = Role(store_id=store_id, name="Owner", slug="owner", is_system=True,
                       description="সব permission — delete/rename করা যাবে না")
    db.add(owner_role)
    await db.flush()
    db.add_all([RolePermission(role_id=owner_role.id, permission_id=p.id) for p in all_perms])

    # সুবিধাজনক starter presets (edit/delete যোগ্য, is_system=False)
    manager_codes = {
        c for m, a, c in iter_permission_codes()
        if m != "staff" or a in ("read",)  # ম্যানেজার সাধারণত staff/role এডিট করতে পারবে না, শুধু দেখবে
    }
    manager_role = Role(store_id=store_id, name="Manager", slug="manager",
                         description="প্রায় সব module — staff/role এডিট বাদে")
    db.add(manager_role)
    await db.flush()
    manager_perms = [p for p in all_perms if p.code in manager_codes]
    db.add_all([RolePermission(role_id=manager_role.id, permission_id=p.id) for p in manager_perms])

    await db.flush()
    return owner_role