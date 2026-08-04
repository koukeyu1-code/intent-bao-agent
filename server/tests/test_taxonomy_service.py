from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.taxonomy_service as taxonomy_service
from app import models as m
from app.database import Database
from app.taxonomy_excel import parse_taxonomy_xlsx
from app.taxonomy_service import (
    TaxonomyServiceError,
    catalogue,
    commit_import,
    confirm_observation_platform,
    import_diff,
    intent_path,
    propose_observation_platform,
)


BASE_PAYLOAD = {
    "technologies": [
        {
            "name": "三模",
            "level2": {"name": "尺寸", "values": ["27", "31.5"]},
            "level3": {"name": "分辨率", "values": ["QHD", "UHD"]},
        }
    ]
}
SAMPLE_WORKBOOK = (
    Path(__file__).resolve().parents[3]
    / "参考资料"
    / "意向宝展示看板表单设计_V1.0.xlsx"
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    result = Database(f"sqlite:///{(tmp_path / 'taxonomy.db').as_posix()}")
    result.create_all()
    return result


def _import_base(database: Database) -> str:
    with database.session() as session:
        result = commit_import(
            session,
            BASE_PAYLOAD,
            file_name="three-mode.xlsx",
            file_sha256=_sha("base"),
            actor="系统管理员（admin）",
        )
        assert result["idempotent"] is False
        technology = session.scalar(
            select(m.Technology).where(m.Technology.name == "三模")
        )
        assert technology is not None
        return technology.id


def _seed_observation(
    database: Database,
    technology_id: str,
    *,
    observation_id: str = "obs-taxonomy",
    generic_platform_id: str | None = None,
) -> None:
    with database.session() as session:
        if session.get(m.Project, "project-taxonomy") is None:
            session.add(
                m.Project(
                    id="project-taxonomy",
                    name="意向宝测试",
                    description="平台层级服务测试",
                )
            )
        if session.get(m.Customer, "customer-taxonomy") is None:
            session.add(
                m.Customer(
                    id="customer-taxonomy",
                    code="CUSTOMER-TAXONOMY",
                    name="客户A",
                    display_name="客户A",
                )
            )
        # These models intentionally do not declare ORM relationships, so make
        # the foreign-key insertion order explicit for SQLite.
        session.flush()
        if session.get(m.MeetingPackage, "package-taxonomy") is None:
            session.add(
                m.MeetingPackage(
                    id="package-taxonomy",
                    project_id="project-taxonomy",
                    customer_id="customer-taxonomy",
                    title="平台层级测试会议",
                    meeting_date=date(2026, 7, 31),
                )
            )
        session.flush()
        session.add(
            m.RawIntentObservation(
                id=observation_id,
                package_id="package-taxonomy",
                customer_id="customer-taxonomy",
                technology_id=technology_id,
                generic_platform_id=generic_platform_id,
                raw_quote="客户建议验证该平台。",
                status="待确认",
            )
        )
        session.commit()


def test_import_diff_is_zero_write_and_commit_materializes_four_leaves(
    database: Database,
) -> None:
    with database.session() as session:
        preview = import_diff(session, BASE_PAYLOAD)
        assert preview["mode"] == "safe_merge"
        assert preview["counts"] == {
            "technologiesToCreate": 1,
            "definitionsToCreate": 2,
            "definitionsToRename": 0,
            "valuesToCreate": 4,
            "valuesToActivate": 0,
            "platformsToCreate": 4,
        }
        assert session.scalar(select(func.count()).select_from(m.Technology)) == 0

        result = commit_import(
            session,
            BASE_PAYLOAD,
            file_name="three-mode.xlsx",
            file_sha256=_sha("same-file"),
            actor="系统管理员（admin）",
        )
        assert result["counts"]["platformsToCreate"] == 4

    with database.session() as session:
        data = catalogue(session)
        three_mode = data["technologies"][0]
        assert three_mode["level2"]["name"] == "尺寸"
        assert [item["value"] for item in three_mode["level2"]["values"]] == [
            "27",
            "31.5",
        ]
        assert three_mode["level3"]["name"] == "分辨率"
        assert [item["name"] for item in three_mode["platforms"]] == [
            "27 QHD",
            "27 UHD",
            "31.5 QHD",
            "31.5 UHD",
        ]
        assert session.scalar(select(func.count()).select_from(m.PlatformVariant)) == 4
        assert session.scalar(select(func.count()).select_from(m.GenericPlatform)) == 4
        assert all(
            item["path"]["technology"]["name"] == "三模"
            for item in three_mode["platforms"]
        )


@pytest.mark.skipif(
    not SAMPLE_WORKBOOK.exists(),
    reason="项目外部参考工作簿未随测试环境提供",
)
def test_supplied_excel_preview_commits_to_exact_three_mode_catalogue(
    database: Database,
) -> None:
    payload = parse_taxonomy_xlsx(SAMPLE_WORKBOOK)
    with database.session() as session:
        commit_import(
            session,
            payload,
            file_name=SAMPLE_WORKBOOK.name,
            file_sha256=hashlib.sha256(SAMPLE_WORKBOOK.read_bytes()).hexdigest(),
            actor="系统管理员（admin）",
        )

    with database.session() as session:
        data = catalogue(session)
        assert len(data["technologies"]) == 6
        three_mode = next(
            item for item in data["technologies"] if item["name"] == "三模"
        )
        assert [item["name"] for item in three_mode["platforms"]] == [
            "27 QHD",
            "27 UHD",
            "31.5 QHD",
            "31.5 UHD",
        ]


def test_repeated_sha_is_idempotent_and_omitted_values_are_preserved(
    database: Database,
) -> None:
    _import_base(database)
    with database.session() as session:
        repeated = commit_import(
            session,
            BASE_PAYLOAD,
            file_name="renamed-copy.xlsx",
            file_sha256=_sha("base"),
            actor="系统管理员（admin）",
        )
        assert repeated["idempotent"] is True
        assert session.scalar(select(func.count()).select_from(m.TaxonomyImportBatch)) == 1

        smaller_payload = {
            "technologies": [
                {
                    "name": "三模",
                    "level2": {"name": "尺寸", "values": ["27"]},
                    "level3": {"name": "分辨率", "values": ["QHD"]},
                }
            ]
        }
        preview = import_diff(session, smaller_payload)
        assert preview["technologies"][0]["level2"]["preservedOmittedValues"] == [
            "31.5"
        ]
        assert preview["technologies"][0]["level3"]["preservedOmittedValues"] == [
            "UHD"
        ]
        commit_import(
            session,
            smaller_payload,
            file_name="smaller.xlsx",
            file_sha256=_sha("smaller"),
            actor="系统管理员（admin）",
        )

    with database.session() as session:
        names = [
            item["name"]
            for item in catalogue(session)["technologies"][0]["platforms"]
        ]
        assert names == ["27 QHD", "27 UHD", "31.5 QHD", "31.5 UHD"]


def test_catalogue_survives_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persistent-taxonomy.db"
    first = Database(f"sqlite:///{path.as_posix()}")
    first.create_all()
    _import_base(first)
    first.engine.dispose()

    reopened = Database(f"sqlite:///{path.as_posix()}")
    reopened.create_all()
    with reopened.session() as session:
        platforms = catalogue(session)["technologies"][0]["platforms"]
        assert [item["name"] for item in platforms] == [
            "27 QHD",
            "27 UHD",
            "31.5 QHD",
            "31.5 UHD",
        ]


def test_normal_user_adds_pending_content_then_confirmation_activates_cartesian_leaves(
    database: Database,
) -> None:
    technology_id = _import_base(database)
    _seed_observation(database, technology_id)
    with database.session() as session:
        draft = propose_observation_platform(
            session,
            observation_id="obs-taxonomy",
            technology_id=technology_id,
            level2_value="34",
            level3_value="QHD",
            actor="产品规划负责人（planner）",
        )
        assert draft["level2"]["status"] == "pending"
        assert draft["level2"]["source"] == "user"
        assert draft["level3"]["status"] == "active"
        session.commit()

    with database.session() as session:
        # Pending content does not leak into the formal filters.
        before_names = [
            item["name"]
            for item in catalogue(session, technology_id)["technologies"][0][
                "platforms"
            ]
        ]
        assert "34 QHD" not in before_names
        confirmed = confirm_observation_platform(
            session,
            observation_id="obs-taxonomy",
            actor="产品规划负责人（planner）",
        )
        session.commit()
        assert confirmed["path"]["displayName"] == "34 QHD"

    with database.session() as session:
        after_names = [
            item["name"]
            for item in catalogue(session, technology_id)["technologies"][0][
                "platforms"
            ]
        ]
        # Adding one level-2 content combines it with every active level-3 value.
        assert after_names == [
            "27 QHD",
            "27 UHD",
            "31.5 QHD",
            "31.5 UHD",
            "34 QHD",
            "34 UHD",
        ]
        observation = session.get(m.RawIntentObservation, "obs-taxonomy")
        assert observation is not None
        assert observation.generic_platform_id == confirmed["genericPlatformId"]
        assert session.scalar(
            select(func.count()).select_from(m.IntentPlatformPathSnapshot)
        ) == 1


def test_user_cannot_create_menu_definition(database: Database) -> None:
    with database.session() as session:
        technology = m.Technology(code="tech-no-menu", name="无菜单技术")
        session.add(technology)
        session.commit()
        technology_id = technology.id
    _seed_observation(database, technology_id, observation_id="obs-no-menu")

    with database.session() as session:
        with pytest.raises(TaxonomyServiceError) as exc_info:
            propose_observation_platform(
                session,
                observation_id="obs-no-menu",
                technology_id=technology_id,
                level2_value="27",
                level3_value="QHD",
                actor="普通用户",
            )
        assert exc_info.value.code == "MENU_DEFINITION_NOT_FOUND"
        assert session.scalar(
            select(func.count()).select_from(m.PlatformMenuDefinition)
        ) == 0


def test_confirmed_snapshot_remains_immutable_after_live_labels_change(
    database: Database,
) -> None:
    technology_id = _import_base(database)
    _seed_observation(database, technology_id)
    with database.session() as session:
        propose_observation_platform(
            session,
            observation_id="obs-taxonomy",
            technology_id=technology_id,
            level2_value="27",
            level3_value="QHD",
            actor="planner",
        )
        confirm_observation_platform(
            session, observation_id="obs-taxonomy", actor="planner"
        )
        session.commit()

    with database.session() as session:
        definition = session.scalar(
            select(m.PlatformMenuDefinition).where(
                m.PlatformMenuDefinition.technology_id == technology_id,
                m.PlatformMenuDefinition.level == 2,
            )
        )
        value = session.scalar(
            select(m.PlatformMenuValue).where(
                m.PlatformMenuValue.definition_id == definition.id,
                m.PlatformMenuValue.normalized_value == "27",
            )
        )
        definition.name = "新尺寸标签"
        value.value = "27英寸"
        session.commit()

    with database.session() as session:
        historical = intent_path(
            session,
            target_type="raw_intent_observation",
            target_id="obs-taxonomy",
        )
        assert historical["mappingStatus"] == "mapped"
        assert historical["path"]["level2"]["name"] == "尺寸"
        assert historical["path"]["level2"]["value"] == "27"


def test_legacy_platform_is_reported_pending_without_guessed_mapping(
    database: Database,
) -> None:
    with database.session() as session:
        technology = m.Technology(code="tech-legacy", name="历史技术")
        platform = m.GenericPlatform(code="platform-legacy", name="历史 27 5K")
        session.add_all([technology, platform])
        session.commit()
        technology_id = technology.id
        platform_id = platform.id
    _seed_observation(
        database,
        technology_id,
        observation_id="obs-legacy",
        generic_platform_id=platform_id,
    )

    with database.session() as session:
        resolved = intent_path(
            session,
            target_type="raw_intent_observation",
            target_id="obs-legacy",
        )
        assert resolved["mappingStatus"] == "legacy_pending"
        assert resolved["path"]["level2"] is None
        assert resolved["path"]["level3"] is None
        assert resolved["path"]["legacyPlatformId"] == platform_id
        legacy = catalogue(session)["legacyPlatforms"]
        assert legacy == [
            {
                "genericPlatformId": platform_id,
                "name": "历史 27 5K",
                "mappingStatus": "legacy_pending",
            }
        ]


def test_invalid_payload_is_atomic_and_writes_nothing(database: Database) -> None:
    invalid = {
        "technologies": [
            {
                "name": "三模",
                "level2": {"name": "尺寸", "values": ["27"]},
                "level3": None,
            }
        ]
    }
    with database.session() as session:
        with pytest.raises(TaxonomyServiceError) as exc_info:
            commit_import(
                session,
                invalid,
                file_name="invalid.xlsx",
                file_sha256=_sha("invalid"),
                actor="admin",
            )
        assert exc_info.value.code == "INCOMPLETE_MENU_LEVELS"
        assert session.scalar(select(func.count()).select_from(m.Technology)) == 0
        assert session.scalar(
            select(func.count()).select_from(m.TaxonomyImportBatch)
        ) == 0


def test_publish_rolls_back_all_rows_if_materialization_fails(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_materialization(*_args, **_kwargs):
        raise RuntimeError("simulated materialization failure")

    monkeypatch.setattr(
        taxonomy_service, "materialize_active_variants", fail_materialization
    )
    with database.session() as session:
        with pytest.raises(RuntimeError, match="simulated materialization failure"):
            commit_import(
                session,
                BASE_PAYLOAD,
                file_name="atomic.xlsx",
                file_sha256=_sha("atomic"),
                actor="admin",
            )

    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(m.Technology)) == 0
        assert session.scalar(
            select(func.count()).select_from(m.PlatformMenuDefinition)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(m.PlatformMenuValue)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(m.TaxonomyImportBatch)
        ) == 0
