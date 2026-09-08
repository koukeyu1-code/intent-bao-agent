from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.main import create_app


@pytest.fixture
def split_case(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'split.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=True,
    )
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": os.environ["INTENT_BAO_ADMIN_PASSWORD"]},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    created = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": "说话人拆分独立回归",
            "meeting_date": "2026-09-01",
            "data_scope": "模拟数据",
        },
    )
    assert created.status_code == 201, created.text
    package_id = created.json()["id"]
    with app.state.database.session() as session:
        run = m.AnalysisRun(
            package_id=package_id,
            status="已完成",
            finished_at=m.utcnow(),
        )
        speaker = m.SpeakerCluster(
            package_id=package_id,
            label="Speaker original",
            recommended_name="原说话人",
            recommended_function_id="function-product",
            confirmed_function_id="function-product",
            status="已确认",
        )
        foreign = m.SpeakerCluster(package_id=package_id, label="Speaker other")
        session.add_all([run, speaker, foreign])
        session.flush()
        repeated_quote = "优先验证拖影与响应时间"
        segment_data = {
            "selected": (1000, 2500, f"客户明确表示需要{repeated_quote}。"),
            "kept": (3000, 4500, "采购侧要求成本保持不变。"),
            "repeated": (5000, 6500, f"另一位发言人也提出{repeated_quote}。"),
            "orphan": (7000, 8000, "茶歇十分钟，随后继续。"),
        }
        segments = {
            name: m.SpeakerSegment(
                speaker_id=speaker.id,
                start_ms=start,
                end_ms=end,
                transcript=text,
            )
            for name, (start, end, text) in segment_data.items()
        }
        foreign_segment = m.SpeakerSegment(
            speaker_id=foreign.id, start_ms=9000, end_ms=10000, transcript="其他人发言。"
        )
        evidence = m.EvidenceFragment(
            package_id=package_id,
            evidence_type="录音转写",
            locator="00:01.200–00:02.000",
            excerpt=repeated_quote,
            start_ms=1200,
            end_ms=2000,
        )
        session.add_all([*segments.values(), foreign_segment, evidence])
        session.flush()
        observations = {}
        for name, quote, evidence_id in (
            ("selected", repeated_quote, evidence.id),
            ("ambiguous", repeated_quote, None),
            ("kept", "采购侧要求成本保持不变", None),
        ):
            observation = m.RawIntentObservation(
                package_id=package_id,
                analysis_run_id=run.id,
                customer_id="customer-a",
                technology_id="tech-pro-gaming",
                function_id="function-product",
                speaker_id=speaker.id,
                evidence_id=evidence_id,
                raw_quote=quote,
                suggested_attitude=4,
                suggested_adoption_stage="内部评估",
                status="待确认",
                confidence=0.9,
            )
            session.add(observation)
            observations[name] = observation
        session.commit()
        case = {
            "client": client,
            "database": app.state.database,
            "package_id": package_id,
            "speaker_id": speaker.id,
            "run_id": run.id,
            "segments": {name: item.id for name, item in segments.items()},
            "foreign_segment_id": foreign_segment.id,
            "observations": {name: item.id for name, item in observations.items()},
        }
    yield case
    client.close()
    app.state.database.engine.dispose()


def _get(client: TestClient, url: str, **kwargs) -> dict:
    response = client.get(url, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def _split(case: dict, *segment_names: str) -> dict:
    response = case["client"].post(
        f"/api/speakers/{case['speaker_id']}/split",
        json={
            "package_id": case["package_id"],
            "segment_ids": [case["segments"][name] for name in segment_names],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["speaker"]


def _speaker_ids(case: dict) -> set[str]:
    return {
        item["id"]
        for item in _get(
            case["client"], "/api/speakers", params={"package_id": case["package_id"]}
        )["items"]
    }


def _bootstrap_speaker_ids(case: dict) -> set[str]:
    return {
        item["id"]
        for item in _get(case["client"], "/api/bootstrap")["speakers"]
        if item["meetingId"] == case["package_id"]
    }


def _confirm_payload(version: int) -> dict:
    return {
        "version": version,
        "display_name": "拆分后开发代表",
        "function_id": "function-development",
        "identity_granularity": "职能",
    }


def test_split_is_visible_and_confirmable_with_evidence_matched_observation(split_case):
    case = split_case
    client = case["client"]
    original = _get(client, f"/api/speakers/{case['speaker_id']}")
    before = {
        name: _get(client, f"/api/intent-observations/{observation_id}")
        for name, observation_id in case["observations"].items()
    }
    target = _split(case, "selected")
    target_id = target["id"]

    assert target_id != case["speaker_id"]
    assert target["status"] == "待确认"
    assert target_id in _speaker_ids(case)
    assert target_id in _bootstrap_speaker_ids(case)
    detail = _get(client, f"/api/speakers/{target_id}")
    assert {item["id"] for item in detail["segments"]} == {case["segments"]["selected"]}
    remaining = _get(client, f"/api/speakers/{case['speaker_id']}")
    assert {item["id"] for item in remaining["segments"]} == {
        case["segments"][name] for name in ("kept", "repeated", "orphan")
    }
    assert remaining["version"] == original["version"] + 1

    migrated = _get(client, f"/api/intent-observations/{case['observations']['selected']}")
    assert migrated["speaker"]["id"] == target_id
    assert migrated["function"] is None
    assert migrated["version"] == before["selected"]["version"] + 1
    assert migrated["evidence"] == before["selected"]["evidence"]
    # Both segments contain the same quote. Only the observation with an
    # unambiguous source time may move; the untimed duplicate must stay put.
    for name in ("ambiguous", "kept"):
        unchanged = _get(client, f"/api/intent-observations/{case['observations'][name]}")
        assert unchanged["speaker"]["id"] == case["speaker_id"]
        assert unchanged["function"]["id"] == "function-product"
        assert unchanged["version"] == before[name]["version"]

    confirmed = client.post(
        f"/api/speakers/{target_id}/confirm", json=_confirm_payload(target["version"])
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "已确认"
    assert confirmed.json()["version"] == target["version"] + 1
    migrated = _get(client, f"/api/intent-observations/{case['observations']['selected']}")
    assert migrated["function"]["id"] == "function-development"
    assert migrated["version"] == before["selected"]["version"] + 2
    assert migrated["ai_original"]["function_id"] == "function-product"
    stale = client.post(
        f"/api/speakers/{target_id}/confirm", json=_confirm_payload(target["version"])
    )
    assert stale.status_code == 409


def test_split_without_an_intent_remains_visible_and_can_be_confirmed(split_case):
    case = split_case
    target = _split(case, "orphan")
    assert target["id"] in _speaker_ids(case)
    assert target["id"] in _bootstrap_speaker_ids(case)
    detail = _get(case["client"], f"/api/speakers/{target['id']}")
    assert detail["segments"][0]["transcript"] == "茶歇十分钟，随后继续。"
    observations = _get(
        case["client"], "/api/intent-observations", params={"package_id": case["package_id"]}
    )["items"]
    assert all(item["speaker"]["id"] != target["id"] for item in observations)

    confirmed = case["client"].post(
        f"/api/speakers/{target['id']}/confirm", json=_confirm_payload(target["version"])
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "已确认"


@pytest.mark.parametrize("status", ["处理失败", "已取消", "运行中"])
def test_unsuccessful_analysis_cannot_expose_or_split_its_speakers(split_case, status):
    case = split_case
    with case["database"].session() as session:
        run = session.get(m.AnalysisRun, case["run_id"])
        run.status = status
        if status == "已取消":
            run.cancel_requested_at = m.utcnow()
        session.commit()

    assert case["speaker_id"] not in _speaker_ids(case)
    assert case["speaker_id"] not in _bootstrap_speaker_ids(case)
    assert case["client"].get(f"/api/speakers/{case['speaker_id']}").status_code == 404
    rejected = case["client"].post(
        f"/api/speakers/{case['speaker_id']}/split",
        json={"package_id": case["package_id"], "segment_ids": [case["segments"]["orphan"]]},
    )
    assert rejected.status_code == 404


def test_cancelled_source_hides_manual_split_and_blocks_further_identity_writes(split_case):
    case = split_case
    target = _split(case, "orphan")
    assert target["id"] in _speaker_ids(case)
    with case["database"].session() as session:
        run = session.get(m.AnalysisRun, case["run_id"])
        # A late cancellation flag must hide the run even if its final status
        # still reads "已完成"; a manual split must not bypass that boundary.
        run.cancel_requested_at = m.utcnow()
        session.commit()

    assert _speaker_ids(case) == set()
    assert _bootstrap_speaker_ids(case) == set()
    for speaker_id in (case["speaker_id"], target["id"]):
        assert case["client"].get(f"/api/speakers/{speaker_id}").status_code == 404
        assert case["client"].post(
            f"/api/speakers/{speaker_id}/confirm", json=_confirm_payload(1)
        ).status_code == 404
    assert case["client"].post(
        f"/api/speakers/{target['id']}/split",
        json={"package_id": case["package_id"], "segment_ids": [case["segments"]["orphan"]]},
    ).status_code == 404


@pytest.mark.parametrize("invalid_selection", ["empty", "missing", "foreign"])
def test_invalid_segment_selection_is_rejected_atomically(split_case, invalid_selection):
    case = split_case
    client = case["client"]
    original = _get(client, f"/api/speakers/{case['speaker_id']}")
    package = _get(client, f"/api/meeting-packages/{case['package_id']}")
    observations = _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    )
    with case["database"].session() as session:
        speaker_count = session.scalar(select(func.count()).select_from(m.SpeakerCluster))
        audit_count = session.scalar(select(func.count()).select_from(m.ConfirmationRecord))
    segment_ids = [] if invalid_selection == "empty" else [
        case["segments"]["selected"],
        "missing-segment" if invalid_selection == "missing" else case["foreign_segment_id"],
    ]
    response = client.post(
        f"/api/speakers/{case['speaker_id']}/split",
        json={"package_id": case["package_id"], "segment_ids": segment_ids},
    )
    assert response.status_code == 422, response.text
    assert _get(client, f"/api/speakers/{case['speaker_id']}") == original
    assert _get(client, f"/api/meeting-packages/{case['package_id']}") == package
    assert _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    ) == observations
    with case["database"].session() as session:
        assert session.scalar(select(func.count()).select_from(m.SpeakerCluster)) == speaker_count
        assert session.scalar(select(func.count()).select_from(m.ConfirmationRecord)) == audit_count


def _share_speaker_across_two_completed_runs(case: dict) -> str:
    """Two known utterances have distinct evidence and distinct source runs."""
    with case["database"].session() as session:
        second_run = m.AnalysisRun(
            package_id=case["package_id"], status="已完成", finished_at=m.utcnow()
        )
        second_evidence = m.EvidenceFragment(
            package_id=case["package_id"],
            evidence_type="录音转写",
            locator="00:03.200–00:04.000",
            excerpt="采购侧要求成本保持不变",
            start_ms=3200,
            end_ms=4000,
        )
        session.add_all([second_run, second_evidence])
        session.flush()
        second_observation = session.get(m.RawIntentObservation, case["observations"]["kept"])
        second_observation.analysis_run_id = second_run.id
        second_observation.evidence_id = second_evidence.id
        session.delete(session.get(m.RawIntentObservation, case["observations"]["ambiguous"]))
        session.delete(session.get(m.SpeakerSegment, case["segments"]["repeated"]))
        session.commit()
        return second_run.id


def test_split_child_inherits_only_its_own_run_when_source_speaker_spans_batches(split_case):
    case = split_case
    client = case["client"]
    _share_speaker_across_two_completed_runs(case)
    target = _split(case, "selected")
    assert target["id"] in _speaker_ids(case)
    assert _get(client, f"/api/intent-observations/{case['observations']['selected']}")[
        "speaker"
    ]["id"] == target["id"]
    assert _get(client, f"/api/intent-observations/{case['observations']['kept']}")[
        "speaker"
    ]["id"] == case["speaker_id"]

    with case["database"].session() as session:
        first_run = session.get(m.AnalysisRun, case["run_id"])
        first_run.cancel_requested_at = m.utcnow()
        session.commit()

    # The other successful batch must not lend visibility to a child whose
    # selected speech and observation came exclusively from the cancelled one.
    assert client.get(f"/api/speakers/{target['id']}").status_code == 404
    assert client.get(
        f"/api/intent-observations/{case['observations']['selected']}"
    ).status_code == 404
    assert client.post(
        f"/api/speakers/{target['id']}/confirm", json=_confirm_payload(target["version"])
    ).status_code == 404
    assert _speaker_ids(case) == {case["speaker_id"]}
    assert _bootstrap_speaker_ids(case) == {case["speaker_id"]}
    remaining = _get(client, f"/api/speakers/{case['speaker_id']}")
    assert {item["id"] for item in remaining["segments"]} == {
        case["segments"]["kept"], case["segments"]["orphan"]
    }
    assert _get(client, f"/api/intent-observations/{case['observations']['kept']}")[
        "speaker"
    ]["id"] == case["speaker_id"]


def test_split_unattributed_speech_from_mixed_batches_is_rejected_atomically(split_case):
    case = split_case
    client = case["client"]
    _share_speaker_across_two_completed_runs(case)
    source = _get(client, f"/api/speakers/{case['speaker_id']}")
    package = _get(client, f"/api/meeting-packages/{case['package_id']}")
    observations = _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    )
    with case["database"].session() as session:
        speaker_count = session.scalar(select(func.count()).select_from(m.SpeakerCluster))
        audit_count = session.scalar(select(func.count()).select_from(m.ConfirmationRecord))

    response = client.post(
        f"/api/speakers/{case['speaker_id']}/split",
        json={"package_id": case["package_id"], "segment_ids": [case["segments"]["orphan"]]},
    )
    assert response.status_code == 422, response.text
    assert _get(client, f"/api/speakers/{case['speaker_id']}") == source
    assert _get(client, f"/api/meeting-packages/{case['package_id']}") == package
    assert _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    ) == observations
    with case["database"].session() as session:
        assert session.scalar(select(func.count()).select_from(m.SpeakerCluster)) == speaker_count
        assert session.scalar(select(func.count()).select_from(m.ConfirmationRecord)) == audit_count


@pytest.mark.parametrize(
    ("other_run_timing", "expected_status"),
    [("later_failed", 422), ("unfinished", 422), ("earlier_failed", 200)],
)
def test_legacy_speaker_split_rejects_only_runs_that_could_have_added_uncertain_speech(
    split_case, other_run_timing, expected_status,
):
    case = split_case
    client = case["client"]
    source_created = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    finished_at = {
        "later_failed": source_created + timedelta(hours=1),
        "earlier_failed": source_created - timedelta(hours=1),
        "unfinished": None,
    }[other_run_timing]
    with case["database"].session() as session:
        source = session.get(m.SpeakerCluster, case["speaker_id"])
        source.created_at = source_created
        session.add(m.AnalysisRun(
            package_id=case["package_id"],
            status="运行中" if other_run_timing == "unfinished" else "处理失败",
            created_at=source_created - timedelta(hours=2),
            finished_at=finished_at,
        ))
        session.commit()
        speaker_count = session.scalar(select(func.count()).select_from(m.SpeakerCluster))
        audit_count = session.scalar(select(func.count()).select_from(m.ConfirmationRecord))
    original = _get(client, f"/api/speakers/{case['speaker_id']}")
    package = _get(client, f"/api/meeting-packages/{case['package_id']}")
    observations = _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    )

    response = client.post(
        f"/api/speakers/{case['speaker_id']}/split",
        json={"package_id": case["package_id"], "segment_ids": [case["segments"]["orphan"]]},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        target = response.json()["speaker"]
        assert target["id"] in _speaker_ids(case)
        assert _get(client, f"/api/speakers/{target['id']}")["segments"][0]["id"] == (
            case["segments"]["orphan"]
        )
        return

    assert _get(client, f"/api/speakers/{case['speaker_id']}") == original
    assert _get(client, f"/api/meeting-packages/{case['package_id']}") == package
    assert _get(
        client, "/api/intent-observations", params={"package_id": case["package_id"]}
    ) == observations
    with case["database"].session() as session:
        assert session.scalar(select(func.count()).select_from(m.SpeakerCluster)) == speaker_count
        assert session.scalar(select(func.count()).select_from(m.ConfirmationRecord)) == audit_count
