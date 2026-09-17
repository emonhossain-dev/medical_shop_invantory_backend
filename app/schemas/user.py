from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from app.models.all_models import StaffRole


class BranchResponse(BaseModel):
    id: int
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    is_main: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class StoreMembershipResponse(BaseModel):
    store_id: int
    role: StaffRole
    branch: Optional[BranchResponse] = None

    model_config = ConfigDict(from_attributes=True)


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