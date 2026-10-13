from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict
# NOTE: StaffRole enum আর ব্যবহার হচ্ছে না — StoreMember.role এখন Role object
# (relationship, role_id FK থেকে), fixed enum না। তাই import সরিয়ে দিলাম।


class BranchResponse(BaseModel):
    id: int
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class RoleResponse(BaseModel):
    id: int
    name: str
    slug: str
    is_system: bool = False

    model_config = ConfigDict(from_attributes=True)


class StoreMembershipResponse(BaseModel):
    store_id: int
    role: RoleResponse
    branch: Optional[BranchResponse] = None
    # StoreMember.permission_codes (property) থেকে আসে -- role এর সব permission
    # + per-member "extra" - per-member "revoke" (StoreMember.permissions JSONB)।
    # validation_alias দিয়ে অ্যাট্রিবিউট নাম ম্যাপ করা হয়েছে যাতে StoreMember এর
    # আসল "permissions" কলাম (JSONB override dict) এর সাথে নাম-সংঘর্ষ না হয়।
    permissions: list[str] = Field(default_factory=list, validation_alias="permission_codes")

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class UserResponse(BaseModel):
    id: int
    email: str
    full_name: str
    phone: Optional[str] = None
    profile_image: Optional[str] = None
    is_super_admin: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime
    store_memberships: list[StoreMembershipResponse] = []

    model_config = ConfigDict(from_attributes=True)


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=150)
    phone: Optional[str] = Field(None, max_length=20)


class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserLoginResponse(BaseModel):
    user: UserResponse
    token: UserTokenResponse