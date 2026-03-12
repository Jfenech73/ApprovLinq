import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

def safe_table_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if not cleaned:
        raise ValueError("Invalid table name")
    return f"invoice_run_{cleaned}"

@router.post("/create-custom-table/{table_name}")
def create_custom_table(table_name: str, db: Session = Depends(get_db)):
    try:
        t = safe_table_name(table_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sql = f'''
    create table if not exists {t} (
        id bigserial primary key,
        page_no int not null,
        supplier_name text,
        invoice_number text,
        invoice_date date,
        description text,
        net_amount numeric(14,2),
        vat_amount numeric(14,2),
        total_amount numeric(14,2),
        currency text,
        tax_code text,
        method_used text,
        confidence_score numeric(5,2),
        validation_status text,
        review_required boolean not null default false,
        raw_payload jsonb,
        created_at timestamptz not null default now()
    );
    '''
    db.execute(text(sql))
    db.commit()
    return {"created_table": t}
