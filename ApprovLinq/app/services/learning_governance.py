from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as M


class LearningGovernanceError(PermissionError):
    """Raised when learning governance blocks an action."""


LEARNING_ACTION_ROLES = {
    "view": frozenset({"tenant_admin", "learning_admin", "learning_approver", "learning_operator", "learning_viewer"}),
    "run": frozenset({"tenant_admin", "learning_admin"}),
    "approve": frozenset({"tenant_admin", "learning_approver"}),
    "promote": frozenset({"tenant_admin", "learning_operator"}),
    "rollback": frozenset({"tenant_admin", "learning_operator"}),
}


def _tenant_role(db: Session, *, user: Any, tenant_id: Any) -> str | None:
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    membership = db.execute(
        select(M.UserTenant).where(
            M.UserTenant.user_id == user_id,
            M.UserTenant.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    return getattr(membership, "tenant_role", None) if membership else None


def require_learning_permission(db: Session, *, user: Any, tenant_id: Any, action: str) -> None:
    """Enforce tenant-scoped RBAC for controlled learning actions."""

    if getattr(user, "role", None) == "admin":
        return
    allowed = LEARNING_ACTION_ROLES.get(action)
    if allowed is None:
        raise LearningGovernanceError(f"Unknown learning governance action: {action}")
    role = _tenant_role(db, user=user, tenant_id=tenant_id)
    if role not in allowed:
        raise LearningGovernanceError(f"User is not permitted to {action} learning recommendations")


def assert_different_user(first_user_id: Any, second_user_id: Any, *, message: str) -> None:
    if first_user_id is not None and second_user_id is not None and first_user_id == second_user_id:
        raise LearningGovernanceError(message)
