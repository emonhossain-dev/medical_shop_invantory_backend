from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class PermissionOut(BaseModel):
    code: str
    module: str
    action: str
    label: Optional[str] = None


class RoleIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)  # " Cashier" আর "Cashier" আলাদা role হতো

    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    permission_codes: list[str] = Field(default_factory=list, max_length=200)


class RoleUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    permission_codes: Optional[list[str]] = Field(default=None, max_length=200)  # দিলে পুরনো সব বাদ দিয়ে নতুন সেট বসবে


class RoleOut(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    is_system: bool
    permission_codes: list[str]

    model_config = ConfigDict(from_attributes=True)


class StaffAssignIn(BaseModel):
    role_id: int
    branch_id: Optional[int] = None  # None = store-wide access


class StaffOut(BaseModel):
    id: int
    user_id: int
    role_id: int
    role_name: str
    branch_id: Optional[int]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)