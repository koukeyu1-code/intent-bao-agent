"""Isolated regression probes for the non-core P0/P1 scope.

These tests intentionally document the currently exposed API behaviour.  The
corresponding Markdown reports state where that behaviour still falls short of
the confirmed development plan.  Every test receives a private SQLite database
and private data directory, and never touches ``server/data`` or the demo DB.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from conftest import wait_for_analysis

from app import models as m
from app.main import create_app
from app.model_adapters import IntentModelAdapter


@pytest.fixture()
def isolated(tmp_path: Path):
    data_dir = tmp_path / "data"
    db_path = tmp_path / "intent-bao-test.db"
    app = create_app(
        database_url=f"sqlite:///{db_path.as_posix()}",
        data_dir=data_dir,
        seed=True,
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": os.environ["INTENT_BAO_USER_PASSWORD"],
            },
        )
        assert login.status_code == 200, login.text
        client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        yield client, app, data_dir, db_path


def create_package(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "project_id": "project-mnt",
        "customer_id": "customer-a",
        "title": "后续范围隔离测试资料包",
        "meeting_date": "2026-07-31",
        "data_scope": "模拟数据",
        "attendees": [],
    }
    payload.update(overrides)
    response = client.post("/api/meeting-packages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def upload_text(client: TestClient, package_id: str, name="minutes.txt", content=b"test"):
    return client.post(
        f"/api/meeting-packages/{package_id}/files",
        data={"material_type": "人工纪要"},
        files={"file": (name, content, "text/plain")},
    )


def test_empty_filters_pagination_and_unknown_resource_errors(isolated) -> None:
    client, _app, _data_dir, _db_path = isolated
    empty_formal = client.get(
        "/api/formal-intent-statuses",
        params={"customer_id": "does-not-exist"},
    )
    assert empty_formal.status_code == 200
    assert empty_formal.json()["items"] == []
    assert empty_formal.json()["total"] == 0

    page1 = client.get("/api/meeting-packages", params={"offset": 0, "limit": 2})
    page2 = client.get("/api/meeting-packages", params={"offset": 2, "limit": 2})
    assert page1.status_code == page2.status_code == 200
    assert page1.json()["total"] >= 4
    assert len(page1.json()["items"]) == len(page2.json()["items"]) == 2
    assert {item["id"] for item in page1.json()["items"]}.isdisjoint(
        {item["id"] for item in page2.json()["items"]}
    )
    assert client.get("/api/meeting-packages/missing").status_code == 404
    assert client.get("/api/meeting-packages", params={"offset": -1}).status_code == 422
    assert client.get("/api/meeting-packages", params={"limit": 201}).status_code == 422


def test_package_edit_and_required_data_scope_contract(isolated) -> None:
    client, _app, _data_dir, _db_path = isolated
    assert client.post(
        "/api/meeting-packages",
        json={
            "project_id": "project-mnt",
            "customer_id": "customer-a",
            "title": "未标数据属性",
            "meeting_date": "2026-07-31",
        },
    ).status_code == 422
    package = create_package(client, notes="原始备注")
    updated = client.patch(
        f"/api/meeting-packages/{package['id']}",
        json={"version": package["version"], "title": "可编辑的标题", "notes": "更新备注"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "可编辑的标题"
    stale = client.patch(
        f"/api/meeting-packages/{package['id']}",
        json={"version": package["version"], "notes": "旧页覆盖"},
    )
    assert stale.status_code == 409
    # The API purposefully has no attendee/data-scope mutation field.  Reject
    # unsupported mutations explicitly instead of silently ignoring them.
    rejected = client.patch(
        f"/api/meeting-packages/{package['id']}",
        json={
            "version": updated.json()["version"],
            "attendees": [{"display_name": "不应写入"}],
            "data_scope": "企业真实敏感数据",
        },
    )
    assert rejected.status_code == 422


def test_upload_boundaries_filename_safety_and_duplicate_semantics(isolated) -> None:
    client, _app, data_dir, _db_path = isolated
    package = create_package(client)
    empty = upload_text(client, package["id"], "empty.txt", b"")
    assert empty.status_code == 422, empty.text
    fake_pdf = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "会议资料"},
        files={"file": ("not-a-pdf.pdf", b"this is not PDF", "application/pdf")},
    )
    assert fake_pdf.status_code == 422, fake_pdf.text
    unsafe = upload_text(client, package["id"], "../../outside.txt", b"safe")
    assert unsafe.status_code == 201, unsafe.text
    assert ".." not in unsafe.json()["file"]["safe_name"]
    assert not list(data_dir.parent.glob("outside.txt"))
    mismatch = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "会议资料"},
        files={"file": ("not-pdf.txt", b"x", "text/plain")},
    )
    assert mismatch.status_code == 415
    duplicate = upload_text(client, package["id"], "same-content.txt", b"safe")
    assert duplicate.status_code == 201
    assert duplicate.json()["duplicate_in_package"] is True


def test_pdf_preflight_rejects_fake_and_valid_signature_can_analyze(isolated) -> None:
    client, _app, _data_dir, _db_path = isolated
    package = create_package(client)
    upload_text(client, package["id"], "transcript.txt", b"text only")
    rejected = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "会议资料"},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert rejected.status_code == 422
    accepted = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "会议资料"},
        files={
            "file": (
                "slides.pdf",
                b"%PDF-1.4\n% isolated preflight fixture\n",
                "application/pdf",
            )
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["file"]["parse_status"] == "预检通过"
    analyzed = client.post(f"/api/meeting-packages/{package['id']}/analyze", json={})
    assert analyzed.status_code == 202
    assert analyzed.json()["analysis_run"]["status"] == "运行中"
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )


def test_task_idempotency_force_runs_and_missing_retry_cancel_endpoints(isolated) -> None:
    client, _app, _data_dir, _db_path = isolated
    package = create_package(client)
    assert upload_text(client, package["id"]).status_code == 201
    first = client.post(f"/api/meeting-packages/{package['id']}/analyze", json={})
    second = client.post(f"/api/meeting-packages/{package['id']}/analyze", json={})
    forced = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={"force": True}
    )
    assert first.status_code == second.status_code == forced.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert forced.json()["task_id"] != first.json()["task_id"]
    tasks = client.get(f"/api/meeting-packages/{package['id']}/tasks").json()
    assert tasks["total"] == 16  # two completed mock AnalysisRun records
    assert client.post(f"/api/meeting-packages/{package['id']}/retry").status_code == 404
    assert client.post(f"/api/meeting-packages/{package['id']}/cancel").status_code == 404


class BrokenAdapter(IntentModelAdapter):
    name = "broken-for-test"
    model_name = "broken-for-test"

    def analyze(self, _context):
        raise RuntimeError("intentional isolated analysis failure")


class SlowAdapter(IntentModelAdapter):
    name = "mock"
    model_name = "slow-cancel-test"

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def analyze(self, _context):
        self.started.set()
        if not self.release.wait(timeout=10.0):
            raise RuntimeError("slow adapter was not released by the test")
        return {"mode": "mock", "model": self.model_name}


def test_cancel_request_survives_slow_adapter_completion(
    isolated, monkeypatch
) -> None:
    client, app, _data_dir, _db_path = isolated
    import app.stage_orchestrator as orchestrator

    slow_adapter = SlowAdapter()
    monkeypatch.setattr(
        orchestrator,
        "get_model_adapter",
        lambda *_args, **_kwargs: slow_adapter,
    )
    package = create_package(client)
    assert upload_text(client, package["id"]).status_code == 201
    started = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["task_id"]
    assert slow_adapter.started.wait(timeout=5.0)

    try:
        cancelled = client.post(f"/api/analysis-runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["analysis_run"]["status"] == "已取消"
        assert cancelled.json()["analysis_run"]["cancel_requested_at"]
    finally:
        slow_adapter.release.set()

    deadline = time.monotonic() + 10.0
    while app.state.analysis_queue.contains(run_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not app.state.analysis_queue.contains(run_id)

    with app.state.database.session() as session:
        run = session.get(m.AnalysisRun, run_id)
        assert run is not None
        assert run.cancel_requested_at is not None
        assert run.status == "已取消"
        assert run.result_summary is None
        refreshed_package = session.get(m.MeetingPackage, package["id"])
        assert refreshed_package is not None
        assert refreshed_package.status == "已取消"
        assert refreshed_package.last_error == "由用户取消"
        tasks = session.query(m.ProcessingTask).filter_by(
            analysis_run_id=run_id
        ).all()
        assert len(tasks) == 8
        assert {task.status for task in tasks} == {"已取消"}
        assert (
            session.query(m.RawIntentObservation)
            .filter_by(analysis_run_id=run_id)
            .count()
            == 0
        )


def test_cancel_cleanup_removes_committed_run_drafts_but_keeps_upload(
    isolated,
) -> None:
    """A safe-boundary cancel must clean rows committed by earlier stages."""

    client, app, data_dir, _db_path = isolated
    from app.services import (
        finalize_analysis_run_cancellation,
        request_analysis_run_cancellation,
    )
    from app.stage_orchestrator import mark_stale_analysis_runs_failed

    package = create_package(client, title="分阶段提交后取消")
    uploaded = upload_text(
        client,
        package["id"],
        name="committed-before-cancel.txt",
        content=b"Mini LED committed draft evidence",
    )
    assert uploaded.status_code == 201, uploaded.text
    material_id = uploaded.json()["file"]["id"]

    with app.state.database.session() as session:
        package_row = session.get(m.MeetingPackage, package["id"])
        material = session.get(m.MaterialFile, material_id)
        assert package_row is not None and material is not None
        package_row.status = "分析中"
        run = m.AnalysisRun(
            package_id=package_row.id,
            adapter="ollama",
            model_name="cancel-after-commit-test",
            status="运行中",
            started_at=m.utcnow(),
        )
        speaker = m.SpeakerCluster(
            package_id=package_row.id,
            label="SPEAKER_CANCELLED",
            status="待确认",
        )
        evidence = m.EvidenceFragment(
            package_id=package_row.id,
            material_file_id=material.id,
            evidence_type="人工纪要",
            locator="committed-before-cancel.txt 第1行",
            excerpt="Mini LED committed draft evidence",
        )
        session.add_all([run, speaker, evidence])
        session.flush()
        transcript = m.TranscriptSegment(
            analysis_run_id=run.id,
            package_id=package_row.id,
            material_file_id=material.id,
            order_index=1,
            start_ms=0,
            end_ms=1000,
            text="Mini LED committed draft evidence",
            speaker_label=speaker.label,
        )
        observation = m.RawIntentObservation(
            package_id=package_row.id,
            analysis_run_id=run.id,
            customer_id=package_row.customer_id,
            technology_id="tech-mini-led-backlight",
            function_id="function-product",
            speaker_id=speaker.id,
            evidence_id=evidence.id,
            raw_quote=evidence.excerpt,
            suggested_attitude=4,
            status="待确认",
            model_name=run.model_name,
        )
        session.add_all([transcript, observation])
        session.flush()
        alignment = m.AlignmentLink(
            analysis_run_id=run.id,
            package_id=package_row.id,
            segment_id=transcript.id,
            target_type="PDF页面",
            target_id="cancelled-target",
            confidence=0.8,
        )
        session.add(alignment)
        session.commit()
        run_id = run.id
        observation_id = observation.id
        evidence_id = evidence.id
        transcript_id = transcript.id
        alignment_id = alignment.id
        original_path = (data_dir / material.relative_path).resolve()
        original_hash = material.sha256

        assert request_analysis_run_cancellation(session, run) is True
        # Simulate the process stopping after the API commits the marker but
        # before the worker reaches its next safe boundary.

    assert mark_stale_analysis_runs_failed(app.state.database) == 0
    with app.state.database.session() as session:
        cancelled = session.get(m.AnalysisRun, run_id)
        assert cancelled is not None and cancelled.status == "已取消"

        assert session.get(m.RawIntentObservation, observation_id) is None
        assert session.get(m.EvidenceFragment, evidence_id) is None
        assert session.get(m.TranscriptSegment, transcript_id) is None
        assert session.get(m.AlignmentLink, alignment_id) is None
        kept_material = session.get(m.MaterialFile, material_id)
        assert kept_material is not None
        assert kept_material.sha256 == original_hash
        assert original_path.is_file()
        assert session.get(m.AnalysisRun, run_id) is not None

        # Cleanup remains idempotent after the restart sweep.
        again = finalize_analysis_run_cancellation(session, cancelled)
        assert again is not None and again.status == "已取消"
        assert session.get(m.MaterialFile, material_id) is not None


def test_task_failure_is_persisted_but_no_api_recovery_action(isolated, monkeypatch) -> None:
    client, app, _data_dir, _db_path = isolated
    import app.stage_orchestrator as orchestrator

    monkeypatch.setattr(
        orchestrator,
        "get_model_adapter",
        lambda *_args, **_kwargs: BrokenAdapter(),
    )
    package = create_package(client)
    assert upload_text(client, package["id"]).status_code == 201
    started = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert started.status_code == 202
    deadline = time.monotonic() + 15.0
    detail = {}
    while time.monotonic() < deadline:
        detail = client.get(f"/api/meeting-packages/{package['id']}").json()
        if detail["status"] == "处理失败":
            break
        time.sleep(0.1)
    assert detail["status"] == "处理失败"
    with app.state.database.session() as session:
        run = session.query(m.AnalysisRun).filter_by(package_id=package["id"]).one()
        assert run.status == "处理失败"
        assert "intentional isolated" in (run.error_message or "")


def test_dictionary_excel_and_crud_api_are_not_exposed(isolated) -> None:
    client, _app, _data_dir, _db_path = isolated
    before = client.get("/api/master-data").json()
    assert client.post("/api/master-data", json={}).status_code == 405
    excel = client.post(
        "/api/master-data/import",
        files={"file": ("fake.xlsx", b"not really xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert excel.status_code == 404
    after = client.get("/api/master-data").json()
    assert after["technologies"] == before["technologies"]


def test_cors_non_mock_scope_and_path_traversal_protection(isolated) -> None:
    client, app, _data_dir, _db_path = isolated
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    blocked = client.get("/api/health", headers={"Origin": "https://evil.example"})
    unlisted_local_port = client.get(
        "/api/health", headers={"Origin": "http://localhost:4173"}
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in blocked.headers
    assert "access-control-allow-origin" not in unlisted_local_port.headers

    protected = create_package(client, data_scope="授权脱敏数据")
    assert upload_text(client, protected["id"]).status_code == 201
    rejected = client.post(f"/api/meeting-packages/{protected['id']}/analyze", json={})
    assert rejected.status_code == 422
    assert "mock" in rejected.json()["detail"]
    reserved = client.post(
        f"/api/meeting-packages/{protected['id']}/analyze",
        json={"adapter": "openai-compatible"},
    )
    assert reserved.status_code == 422

    # Directly inject an invalid stored relative path into the isolated DB;
    # the evidence endpoint must still refuse to serve a file outside data_dir.
    package = create_package(client)
    assert upload_text(client, package["id"]).status_code == 201
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert analyzed.status_code == 202
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    observation = client.get("/api/intent-observations", params={"package_id": package["id"]}).json()["items"][0]
    evidence_id = observation["evidence"]["id"]
    with app.state.database.session() as session:
        evidence = session.get(m.EvidenceFragment, evidence_id)
        material = session.get(m.MaterialFile, evidence.material_file_id)
        material.relative_path = "../../outside-secret.txt"
        session.commit()
    assert client.get(f"/api/evidence/{evidence_id}/content", params={"source": "true"}).status_code == 404


def test_sqlite_copy_restore_and_small_query_latency(isolated, tmp_path: Path) -> None:
    client, _app, data_dir, db_path = isolated
    # Seeded, local-scale requests should be fast.  This is a smoke threshold,
    # not the 60-90 minute media performance acceptance test.
    import time

    started = time.perf_counter()
    for _ in range(20):
        assert client.get("/api/dashboard/summary").status_code == 200
        assert client.get("/api/meeting-packages", params={"limit": 50}).status_code == 200
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0

    backup_dir = tmp_path / "manual-backup"
    backup_dir.mkdir()
    backup_db = backup_dir / "intent-bao.db"
    shutil.copy2(db_path, backup_db)
    restored = create_app(
        database_url=f"sqlite:///{backup_db.as_posix()}",
        data_dir=tmp_path / "restored-data",
        seed=False,
    )
    with TestClient(restored) as restored_client:
        login = restored_client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": os.environ["INTENT_BAO_USER_PASSWORD"],
            },
        )
        assert login.status_code == 200, login.text
        restored_client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        assert restored_client.get("/api/bootstrap").status_code == 200
        assert restored_client.get("/api/meeting-packages").json()["total"] >= 4
    assert data_dir.exists()
