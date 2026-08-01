from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.main import create_app
from app.taxonomy_service import commit_import


USER_PASSWORD = os.environ["INTENT_BAO_USER_PASSWORD"]


def _new_app(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'atomic-review.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=True,
    )
    with app.state.database.session() as session:
        commit_import(
            session,
            {
                "technologies": [
                    {
                        "name": "三模",
                        "level2": {"name": "尺寸", "values": ["27", "31.5"]},
                        "level3": {
                            "name": "分辨率",
                            "values": ["QHD", "UHD"],
                        },
                    }
                ]
            },
            file_name="atomic-review.xlsx",
            file_sha256="a" * 64,
            actor="测试管理员",
        )
    return app


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Authorization": f"Bearer {response.json()['access_token']}"}
    )


def _three_mode(client: TestClient) -> dict:
    taxonomy = client.get("/api/platform-taxonomy").json()
    return next(
        item for item in taxonomy["technologies"] if item["name"] == "三模"
    )


def _value_id(technology: dict, level: int, name: str) -> str:
    definition = next(
        item for item in technology["levels"] if item["level"] == level
    )
    return next(
        item["id"] for item in definition["values"] if item["name"] == name
    )


def test_review_fields_and_platform_path_commit_in_one_version(tmp_path: Path) -> None:
    app = _new_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        technology = _three_mode(client)
        before = client.get("/api/intent-observations/obs-b-product").json()

        response = client.put(
            "/api/intent-observations/obs-b-product/review-draft",
            json={
                "version": before["version"],
                "suggested_attitude": 4,
                "suggested_adoption_stage": "产品项目规划",
                "support_reason": "客户产品团队支持进入规划",
                "bottlenecks": ["采购仍需确认预算"],
                "platform": {
                    "technology_id": technology["id"],
                    "level2_value_id": _value_id(technology, 2, "27"),
                    "level3_value_id": _value_id(technology, 3, "QHD"),
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["observation"]["version"] == before["version"] + 1
        assert body["observation"]["suggested_attitude"]["level"] == 4
        assert body["platform_draft"]["status"] == "pending"
        assert body["observation"]["platform_draft"]["displayName"] == "27 QHD"


def test_invalid_platform_id_rolls_back_all_review_field_changes(
    tmp_path: Path,
) -> None:
    app = _new_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        technology = _three_mode(client)
        before = client.get("/api/intent-observations/obs-b-product").json()

        response = client.put(
            "/api/intent-observations/obs-b-product/review-draft",
            json={
                "version": before["version"],
                "suggested_attitude": 5,
                "suggested_adoption_stage": "确认导入",
                "support_reason": "这段修改必须随错误平台一起回滚",
                "bottlenecks": [],
                "platform": {
                    "technology_id": technology["id"],
                    "level2_value_id": _value_id(technology, 2, "27"),
                    "level3_value_id": "missing-level-3-id",
                },
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"].startswith("MENU_VALUE_NOT_FOUND")
        after = client.get("/api/intent-observations/obs-b-product").json()
        assert after["version"] == before["version"]
        assert after["suggested_attitude"] == before["suggested_attitude"]
        assert after["support_reason"] == before["support_reason"]
        assert after["platform_draft"] == before["platform_draft"]

        with app.state.database.session() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(m.PlatformMenuValue)
                    .where(m.PlatformMenuValue.value == "missing-level-3-id")
                )
                == 0
            )
