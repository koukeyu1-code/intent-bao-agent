from __future__ import annotations

import hashlib
import io
import os
import zipfile
from html import escape
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import models as m
from app.main import create_app


ADMIN_PASSWORD = os.environ["INTENT_BAO_ADMIN_PASSWORD"]
USER_PASSWORD = os.environ["INTENT_BAO_USER_PASSWORD"]
HEADERS = (
    "一级菜单_名称",
    "二级菜单_名称",
    "二级菜单_内容",
    "三级菜单_名称",
    "三级菜单_内容",
)


def _cell(reference: str, value: object) -> str:
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}'
        "</t></is></c>"
    )


def _taxonomy_xlsx(
    *, level2_values: list[object], level3_values: list[object]
) -> bytes:
    """Build the exact five-column workbook contract without test dependencies."""

    rows: dict[int, list[object | None]] = {2: list(HEADERS)}
    row_count = max(len(level2_values), len(level3_values), 1)
    for index in range(row_count):
        rows[index + 3] = [
            "双模与三模" if index == 0 else None,
            "尺寸" if index == 0 else None,
            level2_values[index] if index < len(level2_values) else None,
            "分辨率" if index == 0 else None,
            level3_values[index] if index < len(level3_values) else None,
        ]

    columns = ("B", "C", "D", "E", "F")
    sheet_rows: list[str] = []
    for row_number, values in sorted(rows.items()):
        cells = "".join(
            _cell(f"{column}{row_number}", value)
            for column, value in zip(columns, values, strict=True)
            if value is not None
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" '
            'r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def _login(client: TestClient, username: str, password: str) -> None:
    client.headers.pop("Authorization", None)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Authorization": f"Bearer {response.json()['access_token']}"}
    )


def _publish(client: TestClient, content: bytes, name: str) -> dict:
    preflight = client.post(
        "/api/taxonomy/import/preflight",
        files={
            "file": (
                name,
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert preflight.status_code == 200, preflight.text
    preview = preflight.json()
    assert preview["valid"] is True, preview
    assert preview["preview_token"]
    commit = client.post(
        "/api/taxonomy/import/commit",
        json={
            "preview_token": preview["preview_token"],
            "file_sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert commit.status_code == 200, commit.text
    return commit.json()


def _new_app(tmp_path: Path, *, seed: bool):
    return create_app(
        database_url=f"sqlite:///{(tmp_path / 'taxonomy-api.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=seed,
    )


def _three_mode(taxonomy: dict) -> dict:
    return next(
        item for item in taxonomy["technologies"] if item["name"] == "双模与三模"
    )


def _level(technology: dict, number: int) -> dict:
    return next(item for item in technology["levels"] if item["level"] == number)


def test_safe_merge_keeps_values_omitted_by_later_excel(tmp_path: Path) -> None:
    app = _new_app(tmp_path, seed=False)
    with TestClient(app) as client:
        _login(client, "admin", ADMIN_PASSWORD)
        original = _taxonomy_xlsx(
            level2_values=[27, 31.5], level3_values=["QHD", "UHD"]
        )
        first = _publish(client, original, "taxonomy-v1.xlsx")
        assert first["idempotent"] is False

        smaller = _taxonomy_xlsx(level2_values=[27], level3_values=["QHD"])
        preflight = client.post(
            "/api/taxonomy/import/preflight",
            files={"file": ("taxonomy-v2.xlsx", smaller)},
        )
        assert preflight.status_code == 200, preflight.text
        preview = preflight.json()
        technology_diff = preview["diff"]["technologies"][0]
        assert technology_diff["level2"]["preservedOmittedValues"] == ["31.5"]
        assert technology_diff["level3"]["preservedOmittedValues"] == ["UHD"]
        commit = client.post(
            "/api/taxonomy/import/commit",
            json={
                "preview_token": preview["preview_token"],
                "file_sha256": hashlib.sha256(smaller).hexdigest(),
            },
        )
        assert commit.status_code == 200, commit.text
        assert commit.json()["idempotent"] is False

        # Publishing the exact same preflight again is an idempotent no-op.
        repeated = client.post(
            "/api/taxonomy/import/commit",
            json={
                "preview_token": preview["preview_token"],
                "file_sha256": hashlib.sha256(smaller).hexdigest(),
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["idempotent"] is True

        technology = _three_mode(client.get("/api/platform-taxonomy").json())
        assert [item["name"] for item in _level(technology, 2)["values"]] == [
            "27",
            "31.5",
        ]
        assert [item["name"] for item in _level(technology, 3)["values"]] == [
            "QHD",
            "UHD",
        ]
        assert [item["displayName"] for item in technology["leaves"]] == [
            "27 QHD",
            "27 UHD",
            "31.5 QHD",
            "31.5 UHD",
        ]


def test_user_value_activates_only_on_formal_confirmation_and_expands_cartesian_product(
    tmp_path: Path,
) -> None:
    app = _new_app(tmp_path, seed=True)
    with TestClient(app) as client:
        _login(client, "admin", ADMIN_PASSWORD)
        _publish(
            client,
            _taxonomy_xlsx(
                level2_values=[27, 31.5], level3_values=["QHD", "UHD"]
            ),
            "three-mode.xlsx",
        )

        _login(client, "planner", USER_PASSWORD)
        taxonomy = client.get("/api/platform-taxonomy").json()
        technology = _three_mode(taxonomy)
        qhd = next(
            item
            for item in _level(technology, 3)["values"]
            if item["name"] == "QHD"
        )
        observation = client.get(
            "/api/intent-observations/obs-b-product"
        ).json()
        draft_response = client.post(
            "/api/intent-observations/obs-b-product/platform-draft",
            json={
                "version": observation["version"],
                "technology_id": technology["id"],
                "level2_new_value": "34",
                "level3_value_id": qhd["id"],
            },
        )
        assert draft_response.status_code == 200, draft_response.text
        draft_body = draft_response.json()
        assert draft_body["platform_draft"]["level2"]["status"] == "pending"
        assert draft_body["platform_draft"]["level3"]["status"] == "active"
        pending_observation = client.get(
            "/api/intent-observations/obs-b-product"
        ).json()
        assert pending_observation["platform_draft"] == {
            "id": draft_body["platform_draft"]["id"],
            "status": "pending",
            "technologyId": technology["id"],
            "technologyName": "双模与三模",
            "level2ValueId": draft_body["platform_draft"]["level2"]["id"],
            "level2Value": "34",
            "level2IsNew": True,
            "level3ValueId": qhd["id"],
            "level3Value": "QHD",
            "level3IsNew": False,
            "displayName": "34 QHD",
        }
        bootstrap_intent = next(
            item
            for item in client.get("/api/bootstrap").json()["intents"]
            if item["id"] == "obs-b-product"
        )
        assert bootstrap_intent["platformDraft"]["displayName"] == "34 QHD"
        assert bootstrap_intent["platform"] == "34 QHD（待确认）"

        # A pending user value must not enter either selector or formal leaves.
        # The seed pre-populates this technology's menu, so the published
        # 27/31.5 values merge with the seeded values; only the invariant that
        # the pending user value "34" is absent matters here.
        before = _three_mode(client.get("/api/platform-taxonomy").json())
        before_l2_names = [item["name"] for item in _level(before, 2)["values"]]
        assert "34" not in before_l2_names

        confirmed = client.post(
            "/api/intent-observations/obs-b-product/confirm",
            json={
                "version": draft_body["observation"]["version"],
                "attitude_level": 4,
                "adoption_stage": "产品项目规划",
                "is_supporter": False,
                "is_resistance": False,
                "conditions": [],
                "bottlenecks": [],
                "note": "确认34英寸QHD平台路径",
                # The server must ignore this forged audit identity.
                "actor": "伪造管理员",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        formal = confirmed.json()["formal_status"]
        assert formal["platform_path"]["mappingStatus"] == "mapped"
        assert formal["platform_path"]["path"]["displayName"] == "34 QHD"

        after = _three_mode(client.get("/api/platform-taxonomy").json())
        after_l2_names = [item["name"] for item in _level(after, 2)["values"]]
        assert "34" in after_l2_names
        # One new L2 value combines with every active L3 value, not only QHD.
        new_leaf_names = {
            item["displayName"]
            for item in after["leaves"]
            if item["displayName"].startswith("34 ")
        }
        assert {"34 QHD", "34 UHD"} <= new_leaf_names

        with app.state.database.session() as session:
            value_34 = session.scalar(
                select(m.PlatformMenuValue).where(
                    m.PlatformMenuValue.value == "34"
                )
            )
            assert value_34 is not None
            assert value_34.status == "active"
            assert value_34.source == "user"
            assert value_34.activated_by == "产品规划负责人（planner）"
            # The confirmed user value "34" must materialise one variant per
            # active level-3 value (cartesian expansion), regardless of how
            # many level-3 values the seed pre-populated.
            value_34_variant_count = session.scalar(
                select(func.count())
                .select_from(m.PlatformVariant)
                .where(
                    m.PlatformVariant.technology_id == technology["id"],
                    m.PlatformVariant.level2_value_id == value_34.id,
                )
            )
            active_l3_count = session.scalar(
                select(func.count())
                .select_from(m.PlatformMenuValue)
                .join(m.PlatformMenuDefinition)
                .where(
                    m.PlatformMenuDefinition.technology_id
                    == technology["id"],
                    m.PlatformMenuDefinition.level == 3,
                    m.PlatformMenuValue.status == "active",
                )
            )
            assert value_34_variant_count == active_l3_count
            stored_formal = session.get(m.FormalIntentStatus, formal["id"])
            assert stored_formal is not None
            assert stored_formal.confirmed_by == "产品规划负责人（planner）"


def test_taxonomy_routes_enforce_real_server_side_rbac(tmp_path: Path) -> None:
    app = _new_app(tmp_path, seed=False)
    workbook = _taxonomy_xlsx(level2_values=[27], level3_values=["QHD"])
    commit_payload = {
        "preview_token": "x" * 32,
        "file_sha256": "0" * 64,
    }
    with TestClient(app) as client:
        assert client.get("/api/platform-taxonomy").status_code == 401
        assert (
            client.post(
                "/api/taxonomy/import/preflight",
                files={"file": ("taxonomy.xlsx", workbook)},
            ).status_code
            == 401
        )

        _login(client, "planner", USER_PASSWORD)
        assert client.get("/api/platform-taxonomy").status_code == 200
        denied_preflight = client.post(
            "/api/taxonomy/import/preflight",
            files={"file": ("taxonomy.xlsx", workbook)},
        )
        denied_commit = client.post(
            "/api/taxonomy/import/commit", json=commit_payload
        )
        assert denied_preflight.status_code == 403
        assert denied_commit.status_code == 403
        assert denied_preflight.json() == denied_commit.json() == {
            "detail": "当前账户无权执行此操作"
        }

        _login(client, "admin", ADMIN_PASSWORD)
        allowed = client.post(
            "/api/taxonomy/import/preflight",
            files={"file": ("taxonomy.xlsx", workbook)},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["valid"] is True
