from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.models import User, UserSession, UserTenant, Tenant
from app.db.session import get_db
from app.schemas import LoginRequest, LoginResponse, TenantBrief, ChangePasswordRequest
from app.utils.security import hash_password, new_session_token, session_token_hash, utcnow, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return authorization.split(" ", 1)[1].strip()



def current_session(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> tuple[User, UserSession]:
    token = _get_bearer_token(authorization)
    token_hash = session_token_hash(token)
    stmt = (
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash)
        .where(UserSession.revoked_at.is_(None))
    )
    try:
        result = db.execute(stmt).first()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database connection temporarily unavailable")
    if not result:
        raise HTTPException(status_code=401, detail="Invalid session")
    session_row, user = result
    if session_row.expires_at < utcnow():
        raise HTTPException(status_code=401, detail="Session expired")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User inactive")
    return user, session_row



def current_user(payload=Depends(current_session)) -> User:
    return payload[0]



def current_tenant_id(
    user: User = Depends(current_user),
    x_tenant_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if user.role == "admin":
        if not x_tenant_id:
            raise HTTPException(status_code=400, detail="Missing X-Tenant-Id header")
        tenant = db.get(Tenant, x_tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return tenant.id

    if not x_tenant_id:
        link = (
            db.query(UserTenant)
            .filter(UserTenant.user_id == user.id)
            .order_by(UserTenant.is_default.desc(), UserTenant.id.asc())
            .first()
        )
        if not link:
            raise HTTPException(status_code=403, detail="No tenant access assigned")
        return link.tenant_id

    link = (
        db.query(UserTenant)
        .filter(UserTenant.user_id == user.id, UserTenant.tenant_id == x_tenant_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=403, detail="Forbidden for selected tenant")

    tenant = db.get(Tenant, x_tenant_id)
    if not tenant or not tenant.is_active or tenant.status != "active":
        raise HTTPException(status_code=403, detail="Selected tenant is inactive")
    return tenant.id


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database connection temporarily unavailable")
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User inactive")

    token, token_hash, expires_at = new_session_token()
    session_row = UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    db.add(session_row)
    try:
        db.commit()
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database connection temporarily unavailable")

    try:
        tenant_rows = (
        db.query(UserTenant, Tenant)
        .join(Tenant, Tenant.id == UserTenant.tenant_id)
        .filter(UserTenant.user_id == user.id)
        .order_by(UserTenant.is_default.desc(), Tenant.tenant_name.asc())
        .all()
        )
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database connection temporarily unavailable")
    tenants = [
        TenantBrief(
            tenant_id=tenant.id,
            tenant_name=tenant.tenant_name,
            tenant_code=tenant.tenant_code,
            tenant_role=link.tenant_role,
            is_default=link.is_default,
        )
        for link, tenant in tenant_rows
        if tenant.is_active
    ]

    landing_page = "/static/admin.html" if user.role == "admin" else "/static/tenant.html"
    return LoginResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        tenants=tenants,
        landing_page=landing_page,
    )


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    tenant_rows = (
        db.query(UserTenant, Tenant)
        .join(Tenant, Tenant.id == UserTenant.tenant_id)
        .filter(UserTenant.user_id == user.id)
        .order_by(UserTenant.is_default.desc(), Tenant.tenant_name.asc())
        .all()
        )
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Database connection temporarily unavailable")
    return {
        "user_id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "tenants": [
            {
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.tenant_name,
                "tenant_code": tenant.tenant_code,
                "tenant_role": link.tenant_role,
                "is_default": link.is_default,
            }
            for link, tenant in tenant_rows
        ],
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    auth=Depends(current_session),
    db: Session = Depends(get_db),
):
    user, _session = auth
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@router.post("/logout")
def logout(auth=Depends(current_session), db: Session = Depends(get_db)):
    _user, session_row = auth
    session_row.revoked_at = utcnow()
    db.commit()
    return {"ok": True}
