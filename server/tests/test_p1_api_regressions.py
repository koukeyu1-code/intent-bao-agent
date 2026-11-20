"""第一优先级修复的 API 级回归（CODE_REVIEW_20261120 P1-7 / P1-8）。

- P1-7：建包幂等键——同一 client_request_id 重发返回同一资料包。
- P1-8：强制重跑（force）在资料校验失败时不得取消正在运行的任务。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import models as m



@pytest.fixture()
def isolated(tmp_path: Path):
    data_dir = tmp_path / "data"
    db_path = tmp_path / "intent-bao-p1.db"
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


def test_create_package_is_idempotent_per_client_request_id(isolated) -> None:
    """P1-7：超时重试复用同一幂等键，服务端不得重复建包。"""
    client, *_rest = isolated
    payload = {
        "customer_id": "customer-a",
        "title": "幂等回归会议",
        "meeting_date": "2026-11-20",
        "data_scope": "模拟数据",
        "client_request_id": "req-idem-0001",
    }
    first = client.post("/api/meeting-packages", json=payload)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    # 同键重发（模拟超时后的用户重试）：返回同一个包
    second = client.post("/api/meeting-packages", json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first_id

    # 换一个键：正常新建
    payload["client_request_id"] = "req-idem-0002"
    third = client.post("/api/meeting-packages", json=payload)
    assert third.status_code == 201, third.text
    assert third.json()["id"] != first_id


def test_force_rerun_validation_failure_keeps_active_run(isolated) -> None:
    """P1-8：force 重跑的资料校验失败（422）时，运行中的任务不得被取消。"""
    client, app, _data_dir, _db = isolated
    created = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": "强制重跑校验顺序",
            "meeting_date": "2026-11-20",
            "data_scope": "模拟数据",
        },
    )
    assert created.status_code == 201, created.text
    package_id = created.json()["id"]

    # 直接在库里放一个“运行中”的任务，模拟正在跑的分析
    session_factory = app.state.database.session_factory
    with session_factory() as session:
        running = m.AnalysisRun(
            package_id=package_id, adapter="mock", model_name="mock-intent-v1",
            status="运行中",
        )
        session.add(running)
        session.flush()
        session.add_all([
            m.ProcessingTask(package_id=package_id, analysis_run_id=running.id,
                             stage=stage, stage_order=index)
            for index, stage in enumerate(
                ["文件预检", "文档解析", "音轨与转写", "说话人分离", "录屏解析",
                 "资料对齐", "身份辅助匹配", "意向抽取"], 1)
        ])
        session.commit()
        running_id = running.id

    # 资料包没有任何材料 → force 分析必须 422
    response = client.post(f"/api/meeting-packages/{package_id}/analyze",
                           json={"force": True})
    assert response.status_code == 422, response.text

    with session_factory() as session:
        survivor = session.get(m.AnalysisRun, running_id)
        assert survivor is not None
        assert survivor.status == "运行中", (
            "校验失败的 force 重跑不得取消正在运行的任务"
        )
