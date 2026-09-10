"""Frontend progress must describe the current run and retain terminal details."""

import json
from datetime import date, datetime, timezone

import pytest

from app import models as m
from app.database import Database
from app.presenters import frontend_compat_dataset, public_task


@pytest.fixture
def progress_database(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'progress.db').as_posix()}")
    database.create_all()
    with database.session() as session:
        session.add_all([
            m.Project(id="project", name="Progress regression"),
            m.Customer(id="customer", code="progress", name="Progress", display_name="Progress"),
        ])
        session.flush()
        session.add(m.MeetingPackage(
            id="package", project_id="project", customer_id="customer",
            title="Progress regression", meeting_date=date(2026, 9, 10),
            status="待身份校验",
        ))
        session.flush()
        session.add_all([
            m.AnalysisRun(id="old", package_id="package", status="处理失败", created_at=datetime(2026, 9, 9, tzinfo=timezone.utc)),
            m.AnalysisRun(id="new", package_id="package", status="已完成", created_at=datetime(2026, 9, 10, tzinfo=timezone.utc)),
        ])
        session.flush()
        session.add_all([
            m.ProcessingTask(id="old-task", package_id="package", analysis_run_id="old", stage="旧失败阶段", stage_order=1, status="处理失败", progress=0),
            m.ProcessingTask(id="new-skip", package_id="package", analysis_run_id="new", stage="音轨与转写", stage_order=1, status="不适用", progress=0),
            m.ProcessingTask(id="new-task", package_id="package", analysis_run_id="new", stage="意向抽取", stage_order=2, status="已完成", progress=100),
        ])
        session.commit()
    yield database
    database.engine.dispose()


def test_bootstrap_uses_latest_run_and_counts_not_applicable_as_complete(progress_database):
    with progress_database.session() as session:
        meeting = frontend_compat_dataset(session)["meetings"][0]
        assert meeting["progress"] == 100
        assert meeting["currentStep"] == "待身份校验"
        assert meeting["status"] == "待身份校验"


def test_new_queued_run_is_selected_instead_of_old_failed_stage(progress_database):
    with progress_database.session() as session:
        session.get(m.MeetingPackage, "package").status = "分析中"
        session.get(m.AnalysisRun, "new").status = "排队中"
        task = session.get(m.ProcessingTask, "new-task")
        task.status = "待处理"
        task.progress = 0
        session.commit()
        meeting = frontend_compat_dataset(session)["meetings"][0]
        assert meeting["progress"] == 50
        assert meeting["currentStep"] == "意向抽取"
        assert meeting["status"] == "分析中"


def test_cancellation_and_failed_stage_metadata_remain_distinct(progress_database):
    with progress_database.session() as session:
        package = session.get(m.MeetingPackage, "package")
        run = session.get(m.AnalysisRun, "new")
        task = session.get(m.ProcessingTask, "new-task")
        package.status = run.status = task.status = "已取消"
        task.error_message = "由用户取消"
        task.finished_at = datetime(2026, 9, 10, 1, 2, 3, tzinfo=timezone.utc)
        task.result_json = json.dumps({"batch": 2, "batches": 5, "completed_batches": 1})
        session.commit()
        meeting = frontend_compat_dataset(session)["meetings"][0]
        assert meeting["status"] == meeting["currentStep"] == "已取消"
        public = public_task(task)
        assert public["analysis_run_id"] == "new"
        assert public["analysis_run_created_at"].startswith("2026-09-10")
        assert public["analysis_run_status"] == "已取消"
        assert public["error_message"] == "由用户取消"
        assert public["finished_at"].startswith("2026-09-10T01:02:03")
        assert public["result"]["batch"] == 2
        package.status = run.status = task.status = "处理失败"
        task.error_message = "模型请求超时"
        session.commit()
        assert frontend_compat_dataset(session)["meetings"][0]["status"] == "处理失败"
        assert public_task(task)["error_message"] == "模型请求超时"
