# app/services/branch_scope.py
"""Branch-bound staff (member.branch_id != None) যেন শুধু নিজের branch ছুঁতে পারে।
Owner/manager (member.branch_id is None) সব branch দেখতে পারে।"""
from typing import Optional

from fastapi import HTTPException, status


def assert_branch_access(member, branch_id: Optional[int]) -> None:
    if member.branch_id is not None and branch_id != member.branch_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You don't have access to this branch's data"
        )


def effective_branch_filter(member, requested: Optional[int]) -> Optional[int]:
    """List/report endpoint-এর branch filter: branch-bound staff সবসময় নিজের branch।"""
    if member.branch_id is not None:
        if requested is not None and requested != member.branch_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "You can only access your own branch"
            )
        return member.branch_id
    return requested
