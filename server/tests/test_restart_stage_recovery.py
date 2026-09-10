"""Restart recovery must expose one consistent terminal pipeline state."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event, select

from app import models as m
from app.database import Database
from app.stage_orchestrator import mark_stale_analysis_runs_failed


@pytest.fixture
def recovery_database(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    database.create_all()
    with database.session() as session:
        project = m.Project(id="project", name="Recovery regression")
        customer = m.Customer(id="customer", code="recovery", name="Recovery", display_name="Recovery")
        session.add_all([project, customer])
        session.flush()
        session.add(m.MeetingPackage(
            id="package", project_id=project.id, customer_id=customer.id,
            title="Restart regression", meeting_date=date(2026, 9, 8),
            status="分析中", version=3,
        ))
        session.flush()
        session.add(m.AnalysisRun(id="run", package_id="package", status="运行中"))
        session.flush()
        for order, status in enumerate(("已完成", "不适用", "处理中", "待处理", "处理失败"), 1):
            session.add(m.ProcessingTask(
                id=f"task-{order}", package_id="package", analysis_run_id="run",
                stage=f"stage-{order}", stage_order=order, status=status,
                progress=100 if order < 3 else 37 if order == 3 else 0,
                result_json='{"kept": true}',
                error_message="existing failure" if order == 5 else None,
                finished_at=datetime(2026, 9, 7, tzinfo=timezone.utc) if order in (1, 2, 5) else None,
            ))
        session.commit()
    yield database
    database.engine.dispose()


def test_restart_fails_only_open_stages_and_preserves_diagnostics(recovery_database):
    database = recovery_database
    commits = []

    def record_commit(_connection):
        commits.append(True)

    event.listen(database.engine, "commit", record_commit)
    try:
        assert mark_stale_analysis_runs_failed(database) == 1
    finally:
        event.remove(database.engine, "commit", record_commit)
    assert len(commits) == 1
    with database.session() as session:
        run = session.get(m.AnalysisRun, "run")
        package = session.get(m.MeetingPackage, "package")
        tasks = session.scalars(select(m.ProcessingTask).order_by(m.ProcessingTask.stage_order)).all()
        assert run.status == package.status == "处理失败"
        assert package.version == 4
        assert package.last_error == run.error_message
        assert "服务重启" in run.error_message
        assert [task.status for task in tasks] == ["已完成", "不适用", "处理失败", "处理失败", "处理失败"]
        for task in tasks[2:4]:
            assert task.error_message == run.error_message
            assert task.finished_at == run.finished_at
        assert [task.progress for task in tasks] == [100, 100, 37, 0, 0]
        assert all(task.result_json == '{"kept": true}' for task in tasks)
        assert tasks[0].error_message is None
        assert tasks[1].error_message is None
        assert tasks[4].error_message == "existing failure"
        assert tasks[0].finished_at == tasks[1].finished_at == tasks[4].finished_at
        finished_at = run.finished_at
    assert mark_stale_analysis_runs_failed(database) == 0
    with database.session() as session:
        assert session.get(m.AnalysisRun, "run").finished_at == finished_at
        assert session.get(m.MeetingPackage, "package").version == 4


def test_restart_preserves_requested_cancellation(recovery_database):
    database = recovery_database
    with database.session() as session:
        session.get(m.AnalysisRun, "run").cancel_requested_at = m.utcnow()
        session.commit()
    assert mark_stale_analysis_runs_failed(database) == 0
    with database.session() as session:
        run = session.get(m.AnalysisRun, "run")
        tasks = session.scalars(select(m.ProcessingTask).order_by(m.ProcessingTask.stage_order)).all()
        assert run.status == "已取消"
        assert run.error_message == "由用户取消"
        assert [task.status for task in tasks] == ["已完成", "不适用", "已取消", "已取消", "已取消"]
        assert all(task.error_message == "由用户取消" for task in tasks[2:])


def test_restart_commit_failure_rolls_back_run_package_and_stages(recovery_database, monkeypatch):
    database = recovery_database
    session_factory = database.session

    def failing_session():
        session = session_factory()

        def refuse_commit(_session):
            raise RuntimeError("simulated commit failure")

        event.listen(session, "before_commit", refuse_commit)
        return session

    with monkeypatch.context() as patch:
        patch.setattr(database, "session", failing_session)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            mark_stale_analysis_runs_failed(database)
    with database.session() as session:
        assert session.get(m.AnalysisRun, "run").status == "运行中"
        assert session.get(m.MeetingPackage, "package").status == "分析中"
        assert session.get(m.MeetingPackage, "package").version == 3
        assert session.get(m.ProcessingTask, "task-3").status == "处理中"
        assert session.get(m.ProcessingTask, "task-4").status == "待处理"
