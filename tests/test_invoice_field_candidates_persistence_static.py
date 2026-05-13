from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "app" / "services" / "invoice_arbitration.py"


def _src() -> str:
    return SRC.read_text()


def test_arbitration_imports_candidate_model():
    src = _src()
    assert "InvoiceFieldCandidate" in src
    assert "from app.db.review_models import" in src


def test_persist_field_candidates_helper_exists_and_is_write_only():
    src = _src()
    assert "def persist_field_candidates" in src
    assert "This function is deliberately write-only evidence capture" in src
    assert "must not mutate" in src


def test_candidate_persistence_populates_required_fields():
    src = _src()
    required = [
        "tenant_id=tenant_id",
        "company_id=getattr(row, \"company_id\", None)",
        "batch_id=batch_id",
        "row_id=row.id",
        "source_file_id=getattr(row, \"source_file_id\", None)",
        "field_name=candidate.field_name",
        "candidate_value=value_text",
        "normalised_value=_normalised_value_for_storage",
        "source_type=(candidate.source_type",
        "source_id=source_id",
        "confidence=candidate.confidence",
        "evidence=(candidate.evidence",
        "reason=(candidate.reason",
        "selected=selected",
        "applied=bool(selected and decision.applied)",
        "rejected_reason=_candidate_rejected_reason",
        "conflict=bool(decision.conflict)",
    ]
    for token in required:
        assert token in src


def test_selected_applied_conflict_rejected_reason_logic_exists():
    src = _src()
    assert "selected = candidate is winner" in src
    assert "decision.applied" in src
    assert "decision.conflict" in src
    assert "def _candidate_rejected_reason" in src
    assert "Rejected because selected candidate had stronger source precedence" in src
    assert "Rejected because selected candidate had higher confidence" in src


def test_persistence_deduplicates_within_one_run():
    src = _src()
    assert "seen: set[tuple[Any, ...]]" in src
    assert "if key in seen" in src
    assert "seen.add(key)" in src


def test_arbitration_calls_persistence_after_decisions():
    src = _src()
    assert "persist_field_candidates(db, batch, row, result)" in src
    assert src.index("persist_field_candidates(db, batch, row, result)") > src.index("if result.review_required")
