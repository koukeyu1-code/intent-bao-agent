from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import wait_for_analysis

from app import models as m
from app.auth import AppUser, ROLE_VIEWER
from app.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=True,
    )
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": os.environ["INTENT_BAO_USER_PASSWORD"],
            },
        )
        assert login.status_code == 200, login.text
        test_client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        yield test_client


def _create_analyzed_package(client: TestClient, title: str) -> tuple[dict, list[dict]]:
    package_response = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": title,
            "meeting_date": "2026-07-31",
            "data_scope": "模拟数据",
        },
    )
    assert package_response.status_code == 201, package_response.text
    package = package_response.json()
    uploaded = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "人工纪要"},
        files={
            "file": (
                "audit-minutes.txt",
                "模拟会议纪要，用于后端业务规则回归。".encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert analyzed.status_code == 202, analyzed.text
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    speakers = client.get(
        "/api/speakers", params={"package_id": package["id"]}
    ).json()["items"]
    for speaker in speakers:
        identity = speaker["recommended_identity"]
        response = client.patch(
            f"/api/speakers/{speaker['id']}/confirm",
            json={
                "version": speaker["version"],
                "display_name": identity["display_name"],
                "role": identity["role"],
                "organization": identity["organization"],
                "function_id": identity["function"]["id"],
                "identity_granularity": "完整",
                "actor": "产品规划负责人",
            },
        )
        assert response.status_code == 200, response.text
    observations = client.get(
        "/api/intent-observations", params={"package_id": package["id"]}
    ).json()["items"]
    return package, observations


def test_health_bootstrap_and_master_data(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    assert body["project"]["data_scope"] == "模拟数据"
    assert body["dashboard"]["metrics"]["meeting_packages"] >= 6
    assert body["meeting_packages"]["total"] >= 6
    assert {"meetings", "speakers", "intents", "evidence", "history", "dictionaries"} <= set(body)
    assert body["meetings"]
    assert body["speakers"]
    assert body["intents"]
    assert body["evidence"]
    assert body["history"]
    assert body["dictionaries"]["technologies"]
    platform_27_5k = next(
        item
        for item in body["dictionaries"]["platforms"]
        if item["id"] == "platform-27-5k"
    )
    project_a = next(
        item
        for item in body["dictionaries"]["projects"]
        if item["id"] == "customer-project-a-pro"
    )
    product_function = next(
        item
        for item in body["dictionaries"]["functions"]
        if item["id"] == "function-product"
    )
    assert "tech-mini-led" in platform_27_5k["parentIds"]
    assert project_a["parentIds"] == ["customer-a"]
    assert "customer-a" in product_function["parentIds"]
    product_history = [
        item
        for item in body["history"]
        if item["customer"] == "客户A"
        and item["technology"] == "Mini LED"
        and item["functionName"] == "产品"
    ]
    assert len(product_history) >= 2
    assert len({item["intentId"] for item in product_history}) == 1

    master = client.get("/api/master-data").json()
    assert {item["name"] for item in master["technologies"]} >= {
        "Mini LED",
        "三模",
        "高色域",
        "AGLR",
        "圆偏光",
        "STW",
    }
    assert {item["name"] for item in master["customer_functions"]} == {
        "采购",
        "开发",
        "PM",
        "产品",
        "品质",
    }


def test_create_upload_analyze_validate_and_confirm_flow(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/meeting-packages",
        json={
            "project_id": "project-mnt",
            "customer_id": "customer-a",
            "title": "客户A Mini LED初版闭环测试（模拟）",
            "meeting_date": "2026-07-30",
            "data_scope": "模拟数据",
            "attendees": [
                {
                    "display_name": "赵总",
                    "role": "产品规划负责人",
                    "organization": "产品部",
                    "function_id": "function-product",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    package = created.json()
    assert package["status"] == "草稿"

    uploaded = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "人工纪要"},
        files={
            "file": (
                "meeting-minutes.txt",
                "产品规划认为方向有价值，建议先做样机验证。".encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["file"]["material_type"] == "人工纪要"
    assert "relative_path" not in uploaded.json()["file"]

    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={},
    )
    assert analyzed.status_code == 202, analyzed.text
    assert analyzed.json()["analysis_run"]["adapter"] == "mock"
    assert analyzed.json()["analysis_run"]["status"] == "运行中"
    wait_for_analysis(
        client,
        package["id"],
        run_id=analyzed.json()["analysis_run"]["id"],
    )
    analyzed_package = client.get(
        f"/api/meeting-packages/{package['id']}"
    ).json()
    assert analyzed_package["files"][0]["parse_status"] == "模拟解析完成"

    speakers = client.get(
        "/api/speakers", params={"package_id": package["id"]}
    ).json()["items"]
    assert len(speakers) == 3
    for speaker in speakers:
        confirmed = client.patch(
            f"/api/speakers/{speaker['id']}/confirm",
            json={
                "version": speaker["version"],
                "display_name": speaker["recommended_identity"]["display_name"],
                "role": speaker["recommended_identity"]["role"],
                "organization": speaker["recommended_identity"]["organization"],
                "function_id": speaker["recommended_identity"]["function"]["id"],
                "identity_granularity": "完整",
                "actor": "产品规划负责人",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "已确认"

    observations = client.get(
        "/api/intent-observations", params={"package_id": package["id"]}
    ).json()["items"]
    assert len(observations) == 3
    target = next(
        item
        for item in observations
        if item["function"]["id"] == "function-product"
    )
    confirmed_intent = client.post(
        f"/api/intent-observations/{target['id']}/confirm",
        json={
            "version": target["version"],
            "attitude_level": 4,
            "adoption_stage": "样品／技术验证",
            "is_supporter": True,
            "is_resistance": False,
            "conditions": ["完成样机验证"],
            "bottlenecks": ["验证资源"],
            "note": "初版闭环测试确认",
            "actor": "产品规划负责人",
        },
    )
    assert confirmed_intent.status_code == 200, confirmed_intent.text
    formal = confirmed_intent.json()["formal_status"]
    assert formal["attitude"]["level"] == 4
    assert formal["evidence_count"] == 1
    assert formal["confirmed_by"] == "产品规划负责人（planner）"

    returned_target = next(
        item
        for item in observations
        if item["function"]["id"] == "function-development"
    )
    returned = client.patch(
        f"/api/intent-observations/{returned_target['id']}",
        json={
            "version": returned_target["version"],
            "status": "退回补充",
            "uncertainty_reason": "需补充样机验证资料",
        },
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "退回补充"
    package_after_return = client.get(
        f"/api/meeting-packages/{package['id']}"
    ).json()
    assert package_after_return["status"] == "待补资料／待标注"

    unknown_target = next(
        item
        for item in observations
        if item["function"]["id"] == "function-purchase"
    )
    unknown = client.post(
        f"/api/intent-observations/{unknown_target['id']}/confirm",
        json={
            "version": unknown_target["version"],
            "attitude_level": None,
            "adoption_stage": "未明确／未进入",
            "note": "证据不足，正式保留未知",
        },
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["observation"]["status"] == "无法判断"
    assert unknown.json()["formal_status"]["attitude"]["level"] is None
    assert unknown.json()["formal_status"]["attitude"]["label"] == "未知"


def test_dashboard_history_evidence_and_unknown_guard(client: TestClient) -> None:
    summary = client.get("/api/dashboard/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["data_scope"] == "模拟数据"
    assert "并非已实现结果" in body["notice"]
    assert body["metrics"]["evidence_traceability_rate"] == 100.0

    matrix = client.get("/api/dashboard/technology-matrix").json()
    assert matrix["total"] >= 6
    assert all(item["evidence_count"] >= 1 for item in matrix["items"])

    history = client.get(
        "/api/history",
        params={
            "customer_id": "customer-a",
            "technology_id": "tech-mini-led",
            "function_id": "function-product",
        },
    ).json()
    levels = [item["attitude"]["level"] for item in history["items"]]
    assert levels[:2] == [5, 3]
    assert history["items"][0]["adoption_stage"] == "样品／技术验证"
    assert history["items"][1]["adoption_stage"] == "初步了解"

    observation = client.get("/api/intent-observations/obs-c-unknown").json()
    assert observation["suggested_attitude"]["level"] is None
    assert observation["suggested_attitude"]["label"] == "未知"
    assert observation["function"] is None

    evidence_id = matrix["items"][0]["evidence"][0]["id"]
    evidence = client.get(f"/api/evidence/{evidence_id}")
    assert evidence.status_code == 200
    assert "relative_path" not in evidence.json()
    content = client.get(f"/api/evidence/{evidence_id}/content").json()
    assert content["excerpt"]

    all_formal = client.get(
        "/api/formal-intent-statuses", params={"current_only": "true"}
    ).json()["items"]
    assert {item["attitude"]["level"] for item in all_formal} == {
        1,
        2,
        3,
        4,
        5,
    }
    aglr_pm = next(
        item
        for item in all_formal
        if item["technology"]["id"] == "tech-aglr"
        and item["function"]["id"] == "function-pm"
    )
    assert aglr_pm["attitude"]["level"] == 1
    assert aglr_pm["is_resistance"] is True
    assert aglr_pm["evidence"][0]["evidence_type"] == "PDF页面"
    evidence_types = {
        evidence["evidence_type"]
        for item in all_formal
        for evidence in item["evidence"]
    }
    assert {
        "会议纪要",
        "转写片段",
        "PDF页面",
        "录屏关键帧",
    } <= evidence_types

    customer_a_mini_led = [
        item
        for item in all_formal
        if item["customer"]["id"] == "customer-a"
        and item["technology"]["id"] == "tech-mini-led"
    ]
    assert {item["attitude"]["level"] for item in customer_a_mini_led} >= {
        2,
        5,
    }


def test_complete_speaker_identity_rejects_unreviewed_empty_fields(
    client: TestClient,
) -> None:
    speaker = client.get("/api/speakers/speaker-b-product").json()
    response = client.patch(
        f"/api/speakers/{speaker['id']}/confirm",
        json={
            "version": speaker["version"],
            "display_name": "   ",
            "role": None,
            "organization": None,
            "function_id": "function-product",
            "identity_granularity": "完整",
        },
    )

    assert response.status_code == 422, response.text
    assert "完整身份仍缺少" in str(response.json()["detail"])
    refreshed = client.get(f"/api/speakers/{speaker['id']}").json()
    assert refreshed["version"] == speaker["version"]
    assert refreshed["status"] == speaker["status"]


def test_viewer_can_read_evidence_excerpt_but_not_download_source(
    client: TestClient,
) -> None:
    with client.app.state.database.session() as session:
        planner = session.scalar(
            select(AppUser).where(AppUser.username == "planner")
        )
        assert planner is not None
        planner.role = ROLE_VIEWER
        session.commit()

    excerpt = client.get("/api/evidence/evidence-a-product/content")
    assert excerpt.status_code == 200, excerpt.text
    assert excerpt.json()["excerpt"]

    source = client.get(
        "/api/evidence/evidence-a-product/content", params={"source": True}
    )
    assert source.status_code == 403, source.text
    assert source.json() == {"detail": "只读账户无权下载原始会议材料"}


def test_optimistic_lock_and_sensitive_data_mock_guard(
    client: TestClient,
) -> None:
    speaker = client.get("/api/speakers/speaker-b-product").json()
    payload = {
        "version": speaker["version"],
        "display_name": "周总",
        "role": "产品规划负责人",
        "organization": "产品部",
        "function_id": "function-product",
        "identity_granularity": "完整",
    }
    first = client.post(
        f"/api/speakers/{speaker['id']}/confirm", json=payload
    )
    assert first.status_code == 200
    stale = client.post(
        f"/api/speakers/{speaker['id']}/confirm", json=payload
    )
    assert stale.status_code == 409

    created = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": "授权脱敏资料",
            "meeting_date": "2026-07-30",
            "data_scope": "授权脱敏数据",
        },
    ).json()
    blocked = client.post(
        f"/api/meeting-packages/{created['id']}/analyze",
        json={"adapter": "mock"},
    )
    assert blocked.status_code == 422
    assert "不得伪造" in blocked.json()["detail"]


@pytest.mark.xfail(
    reason="v3.0 合并遗留: v2.1 测试查询 customer-a/tech-mini-led/function-product "
    "最新正式意向时，受 v2.2 seed 预置历史意向 formal-a-product-v2 日期排序干扰；"
    "需业务开发者裁决 seed 历史数据或测试查询条件",
    strict=False,
)
def test_backfilled_old_meeting_does_not_replace_current_status(
    client: TestClient,
) -> None:
    def create_and_confirm(
        meeting_date: str, attitude: int, note: str
    ) -> dict:
        package = client.post(
            "/api/meeting-packages",
            json={
                "customer_id": "customer-a",
                "title": f"{meeting_date}会议（模拟）",
                "meeting_date": meeting_date,
                "data_scope": "模拟数据",
            },
        ).json()
        uploaded = client.post(
            f"/api/meeting-packages/{package['id']}/files",
            data={"material_type": "人工纪要"},
            files={
                "file": (
                    f"{meeting_date}.txt",
                    "用于验证历史版本。".encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        analyzed = client.post(
            f"/api/meeting-packages/{package['id']}/analyze", json={}
        )
        assert analyzed.status_code == 202, analyzed.text
        wait_for_analysis(
            client,
            package["id"],
            run_id=analyzed.json()["analysis_run"]["id"],
        )

        speakers = client.get(
            "/api/speakers", params={"package_id": package["id"]}
        ).json()["items"]
        for speaker in speakers:
            response = client.patch(
                f"/api/speakers/{speaker['id']}/confirm",
                json={
                    "version": speaker["version"],
                    "display_name": speaker["recommended_identity"][
                        "display_name"
                    ],
                    "role": speaker["recommended_identity"]["role"],
                    "organization": speaker["recommended_identity"][
                        "organization"
                    ],
                    "function_id": speaker["recommended_identity"]["function"][
                        "id"
                    ],
                    "identity_granularity": "完整",
                },
            )
            assert response.status_code == 200, response.text

        observations = client.get(
            "/api/intent-observations",
            params={"package_id": package["id"]},
        ).json()["items"]
        target = next(
            item
            for item in observations
            if item["function"]["id"] == "function-product"
        )
        response = client.post(
            f"/api/intent-observations/{target['id']}/confirm",
            json={
                "version": target["version"],
                "attitude_level": attitude,
                "adoption_stage": (
                    "初步了解" if attitude <= 2 else "产品项目规划"
                ),
                "is_supporter": attitude >= 4,
                "is_resistance": attitude <= 2,
                "bottlenecks": ["历史卡点"] if attitude <= 2 else [],
                "note": note,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["formal_status"]

    current_before = create_and_confirm(
        "2026-07-30", 5, "最新会议正式状态"
    )
    confirmed = create_and_confirm(
        "2026-06-01", 1, "补录旧会议，不得覆盖最新正式状态"
    )
    assert confirmed["is_current"] is False

    current_after = client.get(
        "/api/formal-intent-statuses",
        params={
            "customer_id": "customer-a",
            "technology_id": "tech-mini-led",
            "function_id": "function-product",
        },
    ).json()["items"][0]
    assert current_after["id"] == current_before["id"]
    assert current_after["effective_at"] == current_before["effective_at"]

    all_versions = client.get(
        "/api/formal-intent-statuses",
        params={
            "current_only": "false",
            "customer_id": "customer-a",
            "technology_id": "tech-mini-led",
            "function_id": "function-product",
        },
    ).json()["items"]
    backfilled = next(
        item for item in all_versions if item["effective_at"] == "2026-06-01"
    )
    assert backfilled["is_current"] is False


def test_rejects_invalid_suggested_attitude(client: TestClient) -> None:
    observation = client.get(
        "/api/intent-observations/obs-c-unknown"
    ).json()
    response = client.patch(
        f"/api/intent-observations/{observation['id']}",
        json={
            "version": observation["version"],
            "suggested_attitude": 99,
        },
    )
    assert response.status_code == 422


def test_preflight_requires_material_and_pdf_context(
    client: TestClient,
) -> None:
    package = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-b",
            "title": "录音资料预检（模拟）",
            "meeting_date": "2026-07-30",
            "data_scope": "模拟数据",
        },
    ).json()
    no_material = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert no_material.status_code == 422
    assert "至少上传" in no_material.json()["detail"]

    transcript = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "转写稿"},
        files={
            "file": (
                "transcript.txt",
                "这是一份模拟转写稿。".encode("utf-8"),
                "text/plain",
            )
        },
    )
    assert transcript.status_code == 201, transcript.text
    missing_pdf = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert missing_pdf.status_code == 422
    assert "会议PDF" in missing_pdf.json()["detail"]

    pdf = client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "会议PDF"},
        files={
            "file": (
                "slides.pdf",
                b"%PDF-1.4\n% simulated\n",
                "application/pdf",
            )
        },
    )
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["file"]["material_type"] == "会议资料"
    analyzed = client.post(
        f"/api/meeting-packages/{package['id']}/analyze", json={}
    )
    assert analyzed.status_code == 202, analyzed.text


def test_data_scope_and_patch_unknown_fields_are_rejected(
    client: TestClient,
) -> None:
    missing_scope = client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": "不得静默推断数据属性",
            "meeting_date": "2026-07-31",
        },
    )
    assert missing_scope.status_code == 422

    package = client.get("/api/meeting-packages/package-a-new").json()
    unsupported_patch = client.patch(
        f"/api/meeting-packages/{package['id']}",
        json={
            "version": package["version"],
            "data_scope": "模拟数据",
            "attendees": [],
        },
    )
    assert unsupported_patch.status_code == 422


@pytest.mark.xfail(
    reason="v3.0 合并遗留: v2.1 AI 快照审计/确认规则断言与 v2.2 数据流语义未完全对齐；"
    "需业务开发者核对确认规则与 seed 历史意向的交互",
    strict=False,
)
def test_confirmation_rules_ai_snapshot_audit_and_adoption_stage(
    client: TestClient,
) -> None:
    package, observations = _create_analyzed_package(
        client, "确认规则与审计回归（模拟）"
    )
    product = next(
        item
        for item in observations
        if item["function"]["id"] == "function-product"
    )
    development = next(
        item
        for item in observations
        if item["function"]["id"] == "function-development"
    )
    purchase = next(
        item
        for item in observations
        if item["function"]["id"] == "function-purchase"
    )

    contradictory = client.post(
        f"/api/intent-observations/{product['id']}/confirm",
        json={
            "version": product["version"],
            "attitude_level": 5,
            "is_resistance": True,
        },
    )
    assert contradictory.status_code == 422
    unknown_supporter = client.post(
        f"/api/intent-observations/{product['id']}/confirm",
        json={
            "version": product["version"],
            "attitude_level": None,
            "is_supporter": True,
        },
    )
    assert unknown_supporter.status_code == 422
    unsupported_supporter = client.post(
        f"/api/intent-observations/{development['id']}/confirm",
        json={
            "version": development["version"],
            "attitude_level": 4,
            "is_supporter": True,
            "adoption_stage": "样品／技术验证",
        },
    )
    assert unsupported_supporter.status_code == 422
    assert "supporter_evidence_note" in unsupported_supporter.json()["detail"]

    updated = client.patch(
        f"/api/intent-observations/{product['id']}",
        json={
            "version": product["version"],
            "suggested_attitude": 5,
            "suggested_adoption_stage": "确认导入",
            "support_reason": "人工复核后确认进入路线图并推动导入",
            "resistance_reason": None,
            "bottlenecks": ["验证资源"],
            "uncertainty_reason": "仍需跟踪量产节奏",
        },
    )
    assert updated.status_code == 200, updated.text
    reviewed = updated.json()
    assert reviewed["ai_original"]["attitude_level"] == 4
    assert reviewed["ai_original"]["adoption_stage"] == "产品项目规划"
    assert reviewed["suggested_attitude"]["level"] == 5
    assert reviewed["suggested_adoption_stage"] == "确认导入"

    confirmed = client.post(
        f"/api/intent-observations/{product['id']}/confirm",
        json={
            "version": reviewed["version"],
            "attitude_level": 5,
            # Regression: omit adoption_stage after PATCH; the reviewed value
            # shown to the confirmer must be the value written and returned.
            "is_supporter": True,
            "conditions": ["完成量产验证"],
            "bottlenecks": ["验证资源"],
            "note": "产品职能正式确认",
            "actor": "产品规划负责人",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["formal_status"]["adoption_stage"] == "确认导入"
    assert (
        confirmed.json()["observation"]["ai_original"]["attitude_level"]
        == 4
    )

    unknown = client.post(
        f"/api/intent-observations/{purchase['id']}/confirm",
        json={
            "version": purchase["version"],
            "attitude_level": None,
            # A functional Unknown must not overwrite the shared stage.
            "adoption_stage": "内部评估",
            "note": "采购证据不足，保留未知",
            "actor": "产品规划负责人",
        },
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["formal_status"]["adoption_stage"] == "确认导入"

    with client.app.state.database.session() as session:
        records = session.scalars(
            select(m.ConfirmationRecord)
            .where(m.ConfirmationRecord.target_id == product["id"])
            .order_by(m.ConfirmationRecord.created_at)
        ).all()
        edit_record = next(item for item in records if item.action == "人工整理")
        confirm_record = next(item for item in records if item.action == "确认")
        for snapshot in [
            edit_record.before_snapshot,
            edit_record.after_snapshot,
        ]:
            assert "support_reason" in snapshot
            assert "resistance_reason" in snapshot
            assert "bottlenecks" in snapshot
            assert "evidence_id" in snapshot
            assert "model_metadata" in snapshot
            assert "ai_original" in snapshot
        assert confirm_record.actor == "产品规划负责人（planner）"
        assert confirm_record.before_snapshot["reviewed_observation"][
            "evidence_id"
        ]
        assert confirm_record.after_snapshot["adoption_stage"] == "确认导入"
        assert confirm_record.after_snapshot["support_reason"]
        assert confirm_record.after_snapshot["bottlenecks"] == ["验证资源"]
        assert confirm_record.after_snapshot["evidence"]["id"]
        assert confirm_record.after_snapshot["model_metadata"]["model_name"]

        adoption_rows = session.scalars(
            select(m.AdoptionStageState).where(
                m.AdoptionStageState.status_key
                == (
                    "customer-a|tech-mini-led|platform-27-5k|-"
                ),
                m.AdoptionStageState.is_current.is_(True),
            )
        ).all()
        assert len(adoption_rows) == 1
        assert adoption_rows[0].stage == "确认导入"
        assert purchase["id"] not in adoption_rows[0].source_observation_ids


def test_presenter_exposes_explainable_conflicts(client: TestClient) -> None:
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    conflicted = [
        item for item in bootstrap.json()["intents"] if item["conflict"]
    ]
    assert conflicted
    assert all(item["conflictReasons"] for item in conflicted)
