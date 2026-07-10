from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_candidate_model_has_outcome_label_fields():
    src = read("app/db/review_models.py")
    for token in [
        "user_accepted",
        "user_corrected",
        "final_value",
        "finalised_at",
        "finalised_by",
        "outcome_source",
    ]:
        assert token in src


def test_alembic_schema_adds_candidate_outcome_columns():
    src = read("alembic/versions/2026_05_13_0002_candidate_outcome_labels.py")
    for token in [
        "user_accepted",
        "user_corrected",
        "final_value",
        "finalised_at",
        "finalised_by",
        "outcome_source",
    ]:
        assert token in src


def test_outcome_service_labels_only_after_acceptance_events():
    service = read("app/services/candidate_outcomes.py")
    assert "def label_row_candidates" in service
    assert "def label_batch_candidates" in service
    assert "cand.selected" in service
    assert "cand.user_accepted" in service
    assert "cand.user_corrected" in service
    assert "final_value" in service

    review = read("app/routers/review.py")
    exporter = read("app/services/corrected_exporter.py")
    assert "outcome_source=\"manual_review\"" in review
    assert "outcome_source=\"mark_reviewed\"" in review
    assert "outcome_source=\"export\"" in exporter


def test_migration_adds_candidate_outcome_columns():
    src = read("alembic/versions/2026_05_13_0002_candidate_outcome_labels.py")
    assert 'revision = "20260513_0002"' in src
    assert 'down_revision = "20260513_0001"' in src
    for token in ["user_accepted", "user_corrected", "final_value", "finalised_at", "finalised_by", "outcome_source"]:
        assert token in src
