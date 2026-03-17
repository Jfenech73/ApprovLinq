from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User, UserSession, UserTenant, Tenant
from app.db.session import get_db
from app.schemas import LoginRequest, LoginResponse, TenantBrief, ChangePasswordRequest
from app.utils.security import hash_password, new_session_token, session_token_hash, utcnow, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _get_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return authorization.split(" ", 1)[1].strip()


def _db_unavailable(db: Session, message: str = "Database connection temporarily unavailable. Please try again.") -> HTTPException:
    try:
        db.rollback()
    except Exception:
        pass
    return HTTPException(status_code=503, detail=message)


def _load_user_tenants(db: Session, user_id):
    return (
        db.query(UserTenant, Tenant)
        .join(Tenant, Tenant.id == UserTenant.tenant_id)
        .filter(UserTenant.user_id == user_id)
        .order_by(UserTenant.is_default.desc(), Tenant.tenant_name.asc())
        .all()
    )


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
    except SQLAlchemyError:
        raise _db_unavailable(db)
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
    try:
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
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise _db_unavailable(db)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    try:
        email = payload.email.lower().strip()
        user = db.query(User).filter(User.email == email).first()
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="User inactive")

        tenant_rows = _load_user_tenants(db, user.id)
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

        token, token_hash, expires_at = new_session_token()
        db.add(UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires_at))
        db.commit()

        landing_page = "/static/admin.html" if user.role == "admin" else "/static/tenant.html"
        return {
            "access_token": token,
            "user_id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "tenants": [
                {
                    "tenant_id": str(t.tenant_id),
                    "tenant_name": t.tenant_name,
                    "tenant_code": t.tenant_code,
                    "tenant_role": t.tenant_role,
                    "is_default": bool(t.is_default),
                }
                for t in tenants
            ],
            "landing_page": landing_page,
        }
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise _db_unavailable(db)
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong on the server. Please try again.")


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        tenant_rows = _load_user_tenants(db, user.id)
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
    except SQLAlchemyError:
        raise _db_unavailable(db)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    auth=Depends(current_session),
    db: Session = Depends(get_db),
):
    user, _session = auth
    try:
        if not verify_password(payload.current_password, user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        user.password_hash = hash_password(payload.new_password)
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise _db_unavailable(db)


@router.post("/logout")
def logout(auth=Depends(current_session), db: Session = Depends(get_db)):
    _user, session_row = auth
    try:
        session_row.revoked_at = utcnow()
        db.commit()
        return {"ok": True}
    except SQLAlchemyError:
        raise _db_unavailable(db)
