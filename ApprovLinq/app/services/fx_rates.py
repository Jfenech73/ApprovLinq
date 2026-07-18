from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.insight_models import FxRate


DEFAULT_REPORTING_CURRENCY = "EUR"


@dataclass(frozen=True)
class FxRateSnapshot:
    rate: Decimal
    source: str
    rate_date: date | None
    provenance: dict[str, Any]


def normalise_currency(value: Any, fallback: str = DEFAULT_REPORTING_CURRENCY) -> str:
    text = str(value or fallback).strip().upper()
    return text[:20] if text else fallback


def resolve_fx_rate_snapshot(
    db: Session,
    *,
    tenant_id: Any,
    currency: Any,
    invoice_date: date | None,
    reporting_currency: str = DEFAULT_REPORTING_CURRENCY,
) -> FxRateSnapshot:
    """Resolve a reporting FX rate without mutating facts or fetching live rates."""

    src = normalise_currency(currency)
    reporting = normalise_currency(reporting_currency)
    if src == reporting:
        return FxRateSnapshot(
            rate=Decimal("1.00000000"),
            source="identity",
            rate_date=invoice_date,
            provenance={
                "model": "identity",
                "currency": src,
                "reporting_currency": reporting,
                "reason": "source currency already equals reporting currency",
            },
        )

    if invoice_date is None:
        return FxRateSnapshot(
            rate=Decimal("1.00000000"),
            source="missing_invoice_date_not_applied",
            rate_date=None,
            provenance={
                "model": "configured_fx_rate",
                "currency": src,
                "reporting_currency": reporting,
                "reason": "invoice date is unavailable; reporting amounts preserve approved source values",
            },
        )

    rate = db.execute(
        select(FxRate)
        .where(
            FxRate.tenant_id == tenant_id,
            FxRate.currency == src,
            FxRate.reporting_currency == reporting,
            FxRate.rate_date <= invoice_date,
        )
        .order_by(FxRate.rate_date.desc(), FxRate.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if rate is None:
        return FxRateSnapshot(
            rate=Decimal("1.00000000"),
            source="missing_rate_not_applied",
            rate_date=invoice_date,
            provenance={
                "model": "configured_fx_rate",
                "currency": src,
                "reporting_currency": reporting,
                "reason": "no configured FX rate found; reporting amounts preserve approved source values",
            },
        )
    return FxRateSnapshot(
        rate=Decimal(str(rate.rate)).quantize(Decimal("0.00000001")),
        source=rate.source,
        rate_date=rate.rate_date,
        provenance={
            "model": "configured_fx_rate",
            "currency": src,
            "reporting_currency": reporting,
            "fx_rate_id": rate.id,
            "source_reference": rate.source_reference,
            "source_provenance": rate.provenance_json,
        },
    )
