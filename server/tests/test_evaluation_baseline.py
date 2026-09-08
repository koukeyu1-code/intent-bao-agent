from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import models as m
from app.database import Database
from app.evaluation import run_evaluation
from app.services import DomainError, ensure_ai_snapshot


@pytest.fixture
def sample(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'eval.db').as_posix()}")
    database.create_all()
    with database.session() as session:
        session.add_all([
            m.Project(id="p", name="Evaluation"),
            m.Customer(id="c", code="c", name="C", display_name="C"),
            m.Technology(id="t", code="t", name="T"),
            m.Technology(id="t2", code="t2", name="T2"),
            m.CustomerFunction(id="f", code="f", name="F"),
            m.CustomerFunction(id="f2", code="f2", name="F2"),
        ])
        session.flush()
        session.add(m.MeetingPackage(
            id="package", project_id="p", customer_id="c",
            title="Evaluation", meeting_date=date(2026, 9, 9),
        ))
        session.flush()
        session.add(m.AnalysisRun(
            id="run", package_id="package", adapter="ollama", model_name="test",
            status="已完成", finished_at=datetime.now(timezone.utc),
        ))
        session.flush()
        observation = m.RawIntentObservation(
            id="obs", package_id="package", analysis_run_id="run",
            customer_id="c", technology_id="t", function_id="f", raw_quote="original quote",
            suggested_attitude=2, suggested_adoption_stage="内部评估", confidence=0.2,
        )
        session.add(observation)
        session.flush()
        ensure_ai_snapshot(observation)
        session.commit()
        yield session, observation
    database.engine.dispose()


def golden(**overrides):
    return {"quote": "original quote", "technology_id": "t", "function_id": "f",
            "attitude_level": 2, "adoption_stage": "内部评估", **overrides}


def evaluate(session, records=None, **kwargs):
    return run_evaluation(session, package_id="package", golden=records or [golden()], **kwargs)


def test_human_edits_cannot_improve_or_change_original_model_scores(sample):
    session, observation = sample
    expected = [golden(technology_id="t2", function_id="f2", attitude_level=5,
                       adoption_stage="确认导入")]
    before = evaluate(session, expected)
    observation.raw_quote = "rewritten by reviewer"
    observation.technology_id = "t2"
    observation.function_id = "f2"
    observation.suggested_attitude = 5
    observation.suggested_adoption_stage = "确认导入"
    observation.confidence = 1.0
    observation.version += 1
    ensure_ai_snapshot(observation)
    session.commit()
    after = evaluate(session, expected)
    for key in ("correct", "structured_accuracy", "traceable", "field_accuracy", "errors"):
        assert after[key] == before[key]
    assert after["structured_accuracy"] == 0.0
    assert evaluate(session)["structured_accuracy"] == 1.0


def test_candidate_selection_uses_original_confidence(sample):
    session, observation = sample
    other = m.RawIntentObservation(
        id="other", package_id="package", analysis_run_id="run", customer_id="c",
        technology_id="t", function_id="f", raw_quote="original quote",
        suggested_attitude=5, suggested_adoption_stage="内部评估", confidence=0.9,
    )
    session.add(other)
    session.flush()
    ensure_ai_snapshot(other)
    observation.confidence = 1.0
    observation.version += 1
    session.commit()
    report = evaluate(session)
    assert report["correct"] == 0
    assert report["errors"][0]["model"]["attitude_level"] == 5


def test_unknown_rate_uses_unknown_population_and_counts_missing(sample):
    session, _ = sample
    report = evaluate(session, [golden(attitude_level=None)] + [golden()] * 9)
    assert report["unknown_total"] == 1
    assert report["unknown_misjudged"] == 1
    assert report["unknown_misjudge_rate"] == 1.0
    report = evaluate(session, [golden(attitude_level=None), golden(quote="missing", attitude_level=None)])
    assert report["unknown_total"] == 2
    assert report["unknown_misjudged"] == 1
    assert report["unknown_missed"] == report["missed"] == 1
    assert report["unknown_misjudge_rate"] == 0.5
    assert report["correct"] == 0


def test_default_uses_latest_batch_and_explicit_run_is_reproducible(sample):
    session, _ = sample
    session.add(m.AnalysisRun(
        id="latest", package_id="package", adapter="ollama", model_name="new-model",
        status="已完成", finished_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    ))
    session.flush()
    latest = m.RawIntentObservation(
        package_id="package", analysis_run_id="latest", customer_id="c", technology_id="t",
        function_id="f", raw_quote="original quote", suggested_attitude=5,
        suggested_adoption_stage="内部评估", confidence=0.1,
    )
    session.add(latest)
    session.flush()
    ensure_ai_snapshot(latest)
    session.commit()
    report = evaluate(session)
    assert report["analysis_run_id"] == "latest"
    assert report["correct"] == 0
    assert evaluate(session, analysis_run_id="run")["correct"] == 1


@pytest.mark.parametrize("state", ["处理失败", "运行中", "cancelled", "other-package", "missing"])
def test_invalid_source_batch_is_rejected(sample, state):
    session, _ = sample
    run = session.get(m.AnalysisRun, "run")
    if state == "cancelled":
        run.cancel_requested_at = datetime.now(timezone.utc)
    elif state == "other-package":
        session.add(m.MeetingPackage(
            id="other-package", project_id="p", customer_id="c", title="Other",
            meeting_date=date(2026, 9, 9),
        ))
        session.flush()
        run.package_id = "other-package"
    elif state != "missing":
        run.status = state
    session.commit()
    with pytest.raises(DomainError, match="成功完成"):
        evaluate(session, analysis_run_id="missing" if state == "missing" else "run")


@pytest.mark.parametrize("snapshot", [None, {"attitude_level": 2, "adoption_stage": "内部评估"}])
def test_reviewed_legacy_row_without_complete_baseline_does_not_fake_a_score(sample, snapshot):
    session, observation = sample
    observation.ai_snapshot = snapshot
    observation.version = 2
    session.commit()
    with pytest.raises(DomainError, match="缺少完整AI原始快照"):
        evaluate(session)
    # Re-reviewing or confirming this legacy row cannot reconstruct a lost
    # original from the current human-edited fields.
    ensure_ai_snapshot(observation)
    observation.version += 1
    session.commit()
    with pytest.raises(DomainError, match="缺少完整AI原始快照"):
        evaluate(session)


def test_first_review_captures_missing_fields_from_untouched_legacy_snapshot(sample):
    session, observation = sample
    observation.ai_snapshot = {"attitude_level": 2, "adoption_stage": "内部评估"}
    ensure_ai_snapshot(observation)
    observation.function_id = "f2"
    observation.version += 1
    session.commit()
    assert evaluate(session)["correct"] == 1
