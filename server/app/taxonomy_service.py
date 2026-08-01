from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models as m
from .taxonomy_excel import (
    normalize_menu_value,
    normalize_taxonomy_key,
    normalize_taxonomy_text,
)


class TaxonomyServiceError(ValueError):
    """Structured domain error suitable for translation to an HTTP response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(value: Any, *, code: str, label: str) -> str:
    text = normalize_taxonomy_text(value)
    if not text:
        raise TaxonomyServiceError(code, f"{label}不能为空")
    return text


def _value_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name", value.get("value"))
    return normalize_menu_value(value)


def _validated_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("technologies"), list
    ):
        raise TaxonomyServiceError(
            "INVALID_PAYLOAD", "导入内容缺少 technologies 数组"
        )

    result: list[dict[str, Any]] = []
    seen_technologies: set[str] = set()
    for index, raw_technology in enumerate(payload["technologies"], 1):
        if not isinstance(raw_technology, dict):
            raise TaxonomyServiceError(
                "INVALID_TECHNOLOGY", f"第 {index} 个技术必须是对象"
            )
        name = _require_text(
            raw_technology.get("name"),
            code="EMPTY_TECHNOLOGY_NAME",
            label=f"第 {index} 个技术名称",
        )
        technology_key = normalize_taxonomy_key(name)
        if technology_key in seen_technologies:
            raise TaxonomyServiceError(
                "DUPLICATE_TECHNOLOGY", f"导入内容中技术“{name}”重复"
            )
        seen_technologies.add(technology_key)

        normalized: dict[str, Any] = {
            "name": name,
            "key": technology_key,
            "code": normalize_taxonomy_text(raw_technology.get("code")) or None,
            "level2": None,
            "level3": None,
        }
        for level in (2, 3):
            raw_definition = raw_technology.get(f"level{level}")
            if raw_definition is None:
                continue
            if not isinstance(raw_definition, dict):
                raise TaxonomyServiceError(
                    "INVALID_MENU_DEFINITION",
                    f"技术“{name}”的{level}级菜单必须是对象",
                )
            definition_name = _require_text(
                raw_definition.get("name"),
                code="EMPTY_MENU_NAME",
                label=f"技术“{name}”的{level}级菜单名称",
            )
            raw_values = raw_definition.get("values", [])
            if not isinstance(raw_values, list):
                raise TaxonomyServiceError(
                    "INVALID_MENU_VALUES",
                    f"技术“{name}”的{level}级菜单内容必须是数组",
                )
            values: list[str] = []
            seen_values: set[str] = set()
            for raw_value in raw_values:
                value = _value_text(raw_value)
                if not value:
                    raise TaxonomyServiceError(
                        "EMPTY_MENU_VALUE",
                        f"技术“{name}”的{level}级菜单包含空内容",
                    )
                key = normalize_taxonomy_key(value)
                if key in seen_values:
                    raise TaxonomyServiceError(
                        "DUPLICATE_MENU_VALUE",
                        f"技术“{name}”的{level}级菜单内容“{value}”重复",
                    )
                seen_values.add(key)
                values.append(value)
            normalized[f"level{level}"] = {
                "name": definition_name,
                "values": values,
            }

        if bool(normalized["level2"]) != bool(normalized["level3"]):
            raise TaxonomyServiceError(
                "INCOMPLETE_MENU_LEVELS",
                f"技术“{name}”必须同时配置二级和三级菜单，或都不配置",
            )
        result.append(normalized)
    return result


def _technology_code(session: Session, name: str, requested: str | None) -> str:
    base = requested or f"tech-{hashlib.sha1(normalize_taxonomy_key(name).encode()).hexdigest()[:12]}"
    candidate = base
    suffix = 2
    while session.scalar(select(m.Technology.id).where(m.Technology.code == candidate)):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _platform_code(
    technology_id: str, level2_value_id: str, level3_value_id: str
) -> str:
    digest = hashlib.sha1(
        f"{technology_id}|{level2_value_id}|{level3_value_id}".encode()
    ).hexdigest()[:20]
    return f"platform-{digest}"


def _definitions_for_technology(
    session: Session, technology_id: str, *, active_only: bool = True
) -> dict[int, m.PlatformMenuDefinition]:
    query = select(m.PlatformMenuDefinition).where(
        m.PlatformMenuDefinition.technology_id == technology_id
    )
    if active_only:
        query = query.where(m.PlatformMenuDefinition.active.is_(True))
    return {item.level: item for item in session.scalars(query).all()}


def _active_values(
    session: Session, definition_id: str
) -> list[m.PlatformMenuValue]:
    return list(
        session.scalars(
            select(m.PlatformMenuValue)
            .where(
                m.PlatformMenuValue.definition_id == definition_id,
                m.PlatformMenuValue.status == "active",
            )
            .order_by(m.PlatformMenuValue.sort_order, m.PlatformMenuValue.value)
        ).all()
    )


def _variant_path(
    technology: m.Technology,
    definitions: dict[int, m.PlatformMenuDefinition],
    level2: m.PlatformMenuValue,
    level3: m.PlatformMenuValue,
) -> dict[str, Any]:
    return {
        "technology": {"id": technology.id, "name": technology.name},
        "level2": {
            "definitionId": definitions[2].id,
            "name": definitions[2].name,
            "valueId": level2.id,
            "value": level2.value,
        },
        "level3": {
            "definitionId": definitions[3].id,
            "name": definitions[3].name,
            "valueId": level3.id,
            "value": level3.value,
        },
        "displayName": f"{level2.value} {level3.value}",
    }


def materialize_active_variants(
    session: Session, technology_id: str, *, actor: str
) -> list[m.PlatformVariant]:
    """Create missing leaves for the active level-2 x level-3 Cartesian product.

    Existing variants and omitted values are never deleted.  The function only
    flushes; its caller owns the surrounding transaction.
    """

    technology = session.get(m.Technology, technology_id)
    if technology is None:
        raise TaxonomyServiceError("TECHNOLOGY_NOT_FOUND", "技术不存在")
    definitions = _definitions_for_technology(session, technology_id)
    if 2 not in definitions or 3 not in definitions:
        return []
    level2_values = _active_values(session, definitions[2].id)
    level3_values = _active_values(session, definitions[3].id)
    if not level2_values or not level3_values:
        return []

    existing = {
        (item.level2_value_id, item.level3_value_id): item
        for item in session.scalars(
            select(m.PlatformVariant).where(
                m.PlatformVariant.technology_id == technology_id
            )
        ).all()
    }
    result: list[m.PlatformVariant] = []
    revision = max(
        definitions[2].taxonomy_revision,
        definitions[3].taxonomy_revision,
    )
    for level2 in level2_values:
        for level3 in level3_values:
            key = (level2.id, level3.id)
            variant = existing.get(key)
            if variant is None:
                display_name = f"{level2.value} {level3.value}"
                platform = m.GenericPlatform(
                    code=_platform_code(technology_id, level2.id, level3.id),
                    name=display_name,
                    size_inch=(
                        float(level2.value)
                        if definitions[2].name in {"尺寸", "屏幕尺寸"}
                        and _is_number(level2.value)
                        else None
                    ),
                    resolution=(
                        level3.value
                        if definitions[3].name in {"分辨率", "清晰度"}
                        else None
                    ),
                    product_series=technology.name,
                    active=True,
                )
                session.add(platform)
                session.flush()
                path = _variant_path(
                    technology, definitions, level2, level3
                )
                variant = m.PlatformVariant(
                    technology_id=technology_id,
                    level2_value_id=level2.id,
                    level3_value_id=level3.id,
                    generic_platform_id=platform.id,
                    display_name=display_name,
                    path_snapshot=path,
                    taxonomy_revision=revision,
                    mapping_status="mapped",
                    active=True,
                    created_by=actor,
                )
                session.add(variant)
                session.flush()
                existing[key] = variant
            result.append(variant)
    return result


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def import_diff(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a zero-write safe-merge preview for an already parsed workbook."""

    technologies = _validated_payload(payload)
    existing_technologies = {
        normalize_taxonomy_key(item.name): item
        for item in session.scalars(select(m.Technology)).all()
    }
    counts = {
        "technologiesToCreate": 0,
        "definitionsToCreate": 0,
        "definitionsToRename": 0,
        "valuesToCreate": 0,
        "valuesToActivate": 0,
        "platformsToCreate": 0,
    }
    items: list[dict[str, Any]] = []
    for technology_spec in technologies:
        technology = existing_technologies.get(technology_spec["key"])
        technology_item: dict[str, Any] = {
            "name": technology_spec["name"],
            "action": "unchanged" if technology else "create",
            "level2": None,
            "level3": None,
            "platformsToCreate": [],
        }
        if technology is None:
            counts["technologiesToCreate"] += 1
            existing_paths: set[tuple[str, str]] = set()
            definitions: dict[int, m.PlatformMenuDefinition] = {}
        else:
            definitions = _definitions_for_technology(
                session, technology.id, active_only=False
            )
            existing_paths = {
                (
                    session.get(m.PlatformMenuValue, item.level2_value_id).normalized_value,
                    session.get(m.PlatformMenuValue, item.level3_value_id).normalized_value,
                )
                for item in session.scalars(
                    select(m.PlatformVariant).where(
                        m.PlatformVariant.technology_id == technology.id
                    )
                ).all()
            }

        prospective_values: dict[int, dict[str, str]] = {2: {}, 3: {}}
        for level in (2, 3):
            definition_spec = technology_spec[f"level{level}"]
            if definition_spec is None:
                continue
            definition = definitions.get(level)
            action = "unchanged"
            if definition is None:
                action = "create"
                counts["definitionsToCreate"] += 1
                existing_values: dict[str, m.PlatformMenuValue] = {}
            else:
                if normalize_taxonomy_key(definition.name) != normalize_taxonomy_key(
                    definition_spec["name"]
                ):
                    action = "rename"
                    counts["definitionsToRename"] += 1
                existing_values = {
                    item.normalized_value: item
                    for item in session.scalars(
                        select(m.PlatformMenuValue).where(
                            m.PlatformMenuValue.definition_id == definition.id
                        )
                    ).all()
                }
            prospective_values[level].update(
                {
                    key: item.value
                    for key, item in existing_values.items()
                    if item.status == "active"
                }
            )
            add_values: list[str] = []
            activate_values: list[str] = []
            for value in definition_spec["values"]:
                key = normalize_taxonomy_key(value)
                prospective_values[level][key] = value
                existing_value = existing_values.get(key)
                if existing_value is None:
                    add_values.append(value)
                    counts["valuesToCreate"] += 1
                elif existing_value.status != "active":
                    activate_values.append(value)
                    counts["valuesToActivate"] += 1
            technology_item[f"level{level}"] = {
                "name": definition_spec["name"],
                "action": action,
                "addValues": add_values,
                "activateValues": activate_values,
                "preservedOmittedValues": [
                    item.value
                    for key, item in existing_values.items()
                    if key
                    not in {
                        normalize_taxonomy_key(value)
                        for value in definition_spec["values"]
                    }
                ],
            }
        if technology_spec["level2"] and technology_spec["level3"]:
            for level2_key, level2 in prospective_values[2].items():
                for level3_key, level3 in prospective_values[3].items():
                    path_key = (level2_key, level3_key)
                    if path_key not in existing_paths:
                        technology_item["platformsToCreate"].append(
                            f"{level2} {level3}"
                        )
                        counts["platformsToCreate"] += 1
        items.append(technology_item)
    return {"mode": "safe_merge", "counts": counts, "technologies": items}


def commit_import(
    session: Session,
    payload: dict[str, Any],
    *,
    file_name: str,
    file_sha256: str,
    actor: str,
) -> dict[str, Any]:
    """Atomically publish a parsed workbook using additive safe-merge semantics.

    The SHA-256 key makes repeated publication idempotent.  This function owns
    the transaction and commits on success; all mutations are rolled back on
    any error.
    """

    actor = _require_text(actor, code="EMPTY_ACTOR", label="管理员")
    file_name = _require_text(
        file_name, code="EMPTY_FILE_NAME", label="导入文件名"
    )
    file_sha256 = normalize_taxonomy_text(file_sha256).lower()
    if len(file_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in file_sha256
    ):
        raise TaxonomyServiceError("INVALID_FILE_SHA256", "文件 SHA-256 无效")

    technologies = _validated_payload(payload)
    try:
        previous = session.scalar(
            select(m.TaxonomyImportBatch).where(
                m.TaxonomyImportBatch.file_sha256 == file_sha256
            )
        )
        if previous is not None:
            return {
                **dict(previous.result or {}),
                "batchId": previous.id,
                "idempotent": True,
            }

        diff = import_diff(session, payload)
        changed = any(diff["counts"].values())
        imported_technology_ids: list[str] = []
        for technology_spec in technologies:
            technology = session.scalar(
                select(m.Technology).where(
                    func.lower(m.Technology.name)
                    == technology_spec["name"].lower()
                )
            )
            if technology is None:
                technology = next(
                    (
                        item
                        for item in session.scalars(select(m.Technology)).all()
                        if normalize_taxonomy_key(item.name)
                        == technology_spec["key"]
                    ),
                    None,
                )
            if technology is None:
                technology = m.Technology(
                    code=_technology_code(
                        session,
                        technology_spec["name"],
                        technology_spec["code"],
                    ),
                    name=technology_spec["name"],
                    category="产品技术",
                    active=True,
                )
                session.add(technology)
                session.flush()
            else:
                technology.active = True
            imported_technology_ids.append(technology.id)

            existing_definitions = _definitions_for_technology(
                session, technology.id, active_only=False
            )
            revision = max(
                (item.taxonomy_revision for item in existing_definitions.values()),
                default=0,
            ) + (1 if changed else 0)
            revision = max(revision, 1)
            for level in (2, 3):
                definition_spec = technology_spec[f"level{level}"]
                if definition_spec is None:
                    continue
                definition = existing_definitions.get(level)
                if definition is None:
                    definition = m.PlatformMenuDefinition(
                        technology_id=technology.id,
                        level=level,
                        name=definition_spec["name"],
                        sort_order=level,
                        active=True,
                        taxonomy_revision=revision,
                    )
                    session.add(definition)
                    session.flush()
                    existing_definitions[level] = definition
                else:
                    definition.name = definition_spec["name"]
                    definition.active = True
                    definition.sort_order = level
                    definition.taxonomy_revision = revision

                existing_values = {
                    item.normalized_value: item
                    for item in session.scalars(
                        select(m.PlatformMenuValue).where(
                            m.PlatformMenuValue.definition_id == definition.id
                        )
                    ).all()
                }
                for sort_order, value in enumerate(
                    definition_spec["values"], 1
                ):
                    key = normalize_taxonomy_key(value)
                    item = existing_values.get(key)
                    if item is None:
                        item = m.PlatformMenuValue(
                            definition_id=definition.id,
                            value=value,
                            normalized_value=key,
                            sort_order=sort_order,
                            status="active",
                            source="excel",
                            created_by=actor,
                            activated_by=actor,
                            activated_at=_now(),
                            taxonomy_revision=revision,
                        )
                        session.add(item)
                        existing_values[key] = item
                    else:
                        item.value = value
                        item.sort_order = sort_order
                        item.status = "active"
                        item.activated_by = item.activated_by or actor
                        item.activated_at = item.activated_at or _now()
                        item.taxonomy_revision = revision
                session.flush()

            materialize_active_variants(session, technology.id, actor=actor)

        result = {
            "mode": "safe_merge",
            "idempotent": False,
            "technologyIds": imported_technology_ids,
            "counts": diff["counts"],
        }
        batch = m.TaxonomyImportBatch(
            file_name=file_name,
            file_sha256=file_sha256,
            status="committed",
            payload=payload,
            diff=diff,
            result=result,
            committed_by=actor,
        )
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return {**result, "batchId": batch.id}
    except IntegrityError as exc:
        session.rollback()
        # A concurrent request may have won the unique SHA race.
        previous = session.scalar(
            select(m.TaxonomyImportBatch).where(
                m.TaxonomyImportBatch.file_sha256 == file_sha256
            )
        )
        if previous is not None:
            return {
                **dict(previous.result or {}),
                "batchId": previous.id,
                "idempotent": True,
            }
        raise TaxonomyServiceError(
            "IMPORT_CONFLICT", "导入发布发生唯一性冲突"
        ) from exc
    except Exception:
        session.rollback()
        raise


def _serialize_menu_value(item: m.PlatformMenuValue) -> dict[str, Any]:
    return {
        "id": item.id,
        "value": item.value,
        "status": item.status,
        "source": item.source,
        "sortOrder": item.sort_order,
    }


def catalogue(
    session: Session, technology_id: str | None = None
) -> dict[str, Any]:
    """Return the live dynamic menu catalogue and materialized leaves."""

    query = select(m.Technology).where(m.Technology.active.is_(True))
    if technology_id:
        query = query.where(m.Technology.id == technology_id)
    technologies = list(session.scalars(query.order_by(m.Technology.name)).all())
    items: list[dict[str, Any]] = []
    mapped_platform_ids: set[str] = set()
    for technology in technologies:
        definitions = _definitions_for_technology(session, technology.id)
        values_by_id: dict[str, m.PlatformMenuValue] = {}
        serialized_definitions: dict[int, dict[str, Any] | None] = {
            2: None,
            3: None,
        }
        for level in (2, 3):
            definition = definitions.get(level)
            if definition is None:
                continue
            values = _active_values(session, definition.id)
            values_by_id.update({value.id: value for value in values})
            serialized_definitions[level] = {
                "id": definition.id,
                "name": definition.name,
                "level": level,
                "values": [_serialize_menu_value(value) for value in values],
            }

        leaves: list[dict[str, Any]] = []
        for variant in session.scalars(
            select(m.PlatformVariant)
            .where(
                m.PlatformVariant.technology_id == technology.id,
                m.PlatformVariant.active.is_(True),
                m.PlatformVariant.mapping_status == "mapped",
            )
            .order_by(m.PlatformVariant.display_name)
        ).all():
            level2 = values_by_id.get(variant.level2_value_id)
            level3 = values_by_id.get(variant.level3_value_id)
            if level2 is None or level3 is None or 2 not in definitions or 3 not in definitions:
                continue
            mapped_platform_ids.add(variant.generic_platform_id)
            live_path = _variant_path(
                technology, definitions, level2, level3
            )
            leaves.append(
                {
                    "id": variant.id,
                    "genericPlatformId": variant.generic_platform_id,
                    "name": live_path["displayName"],
                    "level2ValueId": level2.id,
                    "level3ValueId": level3.id,
                    "mappingStatus": "mapped",
                    "path": live_path,
                }
            )
        items.append(
            {
                "id": technology.id,
                "code": technology.code,
                "name": technology.name,
                "level2": serialized_definitions[2],
                "level3": serialized_definitions[3],
                "platforms": leaves,
            }
        )

    all_mapped_ids = set(
        session.scalars(select(m.PlatformVariant.generic_platform_id)).all()
    )
    legacy = [
        {
            "genericPlatformId": item.id,
            "name": item.name,
            "mappingStatus": "legacy_pending",
        }
        for item in session.scalars(
            select(m.GenericPlatform)
            .where(
                m.GenericPlatform.active.is_(True),
                ~m.GenericPlatform.id.in_(all_mapped_ids or {""}),
            )
            .order_by(m.GenericPlatform.name)
        ).all()
    ]
    return {"technologies": items, "legacyPlatforms": legacy}


def _definition_value(
    session: Session,
    definition: m.PlatformMenuDefinition,
    raw_value: str,
    *,
    actor: str,
    value_is_id: bool = False,
) -> m.PlatformMenuValue:
    value_or_id = _require_text(
        raw_value, code="EMPTY_MENU_VALUE", label=f"{definition.level}级菜单内容"
    )
    by_id = session.get(m.PlatformMenuValue, value_or_id)
    if by_id is not None:
        if by_id.definition_id != definition.id:
            raise TaxonomyServiceError(
                "VALUE_DEFINITION_MISMATCH", "菜单内容与当前菜单名称不匹配"
            )
        return by_id
    if value_is_id:
        raise TaxonomyServiceError(
            "MENU_VALUE_NOT_FOUND",
            "所选菜单内容已不存在，请刷新数据字典后重新选择",
        )
    display_value = normalize_menu_value(value_or_id)
    key = normalize_taxonomy_key(display_value)
    existing = session.scalar(
        select(m.PlatformMenuValue).where(
            m.PlatformMenuValue.definition_id == definition.id,
            m.PlatformMenuValue.normalized_value == key,
        )
    )
    if existing is not None:
        return existing
    max_order = session.scalar(
        select(func.max(m.PlatformMenuValue.sort_order)).where(
            m.PlatformMenuValue.definition_id == definition.id
        )
    ) or 0
    item = m.PlatformMenuValue(
        definition_id=definition.id,
        value=display_value,
        normalized_value=key,
        sort_order=max_order + 1,
        status="pending",
        source="user",
        created_by=actor,
        taxonomy_revision=definition.taxonomy_revision,
    )
    session.add(item)
    session.flush()
    return item


def propose_observation_platform(
    session: Session,
    *,
    observation_id: str,
    technology_id: str,
    level2_value: str,
    level3_value: str,
    level2_value_is_id: bool = False,
    level3_value_is_id: bool = False,
    actor: str,
) -> dict[str, Any]:
    """Let a normal user select or propose content under existing menu names.

    This operation deliberately has no menu-name parameters, so it cannot add
    or restructure a definition.  New contents remain ``pending`` until the
    observation is formally confirmed.  The caller owns the transaction.
    """

    actor = _require_text(actor, code="EMPTY_ACTOR", label="操作人")
    observation = session.get(m.RawIntentObservation, observation_id)
    if observation is None:
        raise TaxonomyServiceError("OBSERVATION_NOT_FOUND", "意向观察不存在")
    definitions = _definitions_for_technology(session, technology_id)
    if 2 not in definitions or 3 not in definitions:
        raise TaxonomyServiceError(
            "MENU_DEFINITION_NOT_FOUND",
            "当前技术尚未由管理员配置完整的二、三级菜单",
        )
    level2 = _definition_value(
        session,
        definitions[2],
        level2_value,
        actor=actor,
        value_is_id=level2_value_is_id,
    )
    level3 = _definition_value(
        session,
        definitions[3],
        level3_value,
        actor=actor,
        value_is_id=level3_value_is_id,
    )
    draft = session.scalar(
        select(m.ObservationPlatformDraft).where(
            m.ObservationPlatformDraft.observation_id == observation_id
        )
    )
    if draft is None:
        draft = m.ObservationPlatformDraft(
            observation_id=observation_id,
            technology_id=technology_id,
            level2_value_id=level2.id,
            level3_value_id=level3.id,
            status="pending",
            proposed_by=actor,
        )
        session.add(draft)
    else:
        draft.technology_id = technology_id
        draft.level2_value_id = level2.id
        draft.level3_value_id = level3.id
        draft.status = "pending"
        draft.proposed_by = actor
    session.flush()
    return {
        "id": draft.id,
        "observationId": observation_id,
        "technologyId": technology_id,
        "status": draft.status,
        "level2": _serialize_menu_value(level2),
        "level3": _serialize_menu_value(level3),
    }


def capture_path_snapshot(
    session: Session,
    *,
    target_type: str,
    target_id: str,
    technology_id: str,
    generic_platform_id: str,
    actor: str,
    version_no: int | None = None,
) -> m.IntentPlatformPathSnapshot:
    """Append an immutable path snapshot for a confirmed intent target."""

    existing_versions = list(
        session.scalars(
            select(m.IntentPlatformPathSnapshot)
            .where(
                m.IntentPlatformPathSnapshot.target_type == target_type,
                m.IntentPlatformPathSnapshot.target_id == target_id,
            )
            .order_by(m.IntentPlatformPathSnapshot.version_no)
        ).all()
    )
    if version_no is None:
        version_no = max((item.version_no for item in existing_versions), default=0) + 1
    duplicate = next(
        (item for item in existing_versions if item.version_no == version_no), None
    )
    if duplicate is not None:
        return duplicate

    variant = session.scalar(
        select(m.PlatformVariant).where(
            m.PlatformVariant.generic_platform_id == generic_platform_id,
            m.PlatformVariant.technology_id == technology_id,
            m.PlatformVariant.mapping_status == "mapped",
        )
    )
    if variant is None:
        platform = session.get(m.GenericPlatform, generic_platform_id)
        technology = session.get(m.Technology, technology_id)
        path = {
            "technology": (
                {"id": technology.id, "name": technology.name}
                if technology
                else {"id": technology_id, "name": None}
            ),
            "level2": None,
            "level3": None,
            "displayName": platform.name if platform else None,
            "legacyPlatformId": generic_platform_id,
        }
        mapping_status = "legacy_pending"
        taxonomy_revision = None
        variant_id = None
    else:
        # Copy the creation-time path rather than storing a live reference.
        path = dict(variant.path_snapshot)
        mapping_status = "mapped"
        taxonomy_revision = variant.taxonomy_revision
        variant_id = variant.id
    snapshot = m.IntentPlatformPathSnapshot(
        target_type=target_type,
        target_id=target_id,
        version_no=version_no,
        technology_id=technology_id,
        generic_platform_id=generic_platform_id,
        platform_variant_id=variant_id,
        taxonomy_revision=taxonomy_revision,
        mapping_status=mapping_status,
        path=path,
        captured_by=actor,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def confirm_observation_platform(
    session: Session, *, observation_id: str, actor: str
) -> dict[str, Any]:
    """Activate proposed values, materialize leaves, and snapshot the path.

    The caller should invoke this inside the same transaction that creates the
    formal intent, so a failure rolls back both operations.
    """

    draft = session.scalar(
        select(m.ObservationPlatformDraft).where(
            m.ObservationPlatformDraft.observation_id == observation_id
        )
    )
    if draft is None:
        raise TaxonomyServiceError("PLATFORM_DRAFT_NOT_FOUND", "当前意向没有待确认平台路径")
    observation = session.get(m.RawIntentObservation, observation_id)
    if observation is None:
        raise TaxonomyServiceError("OBSERVATION_NOT_FOUND", "意向观察不存在")
    if draft.level2_value_id is None or draft.level3_value_id is None:
        raise TaxonomyServiceError("INCOMPLETE_PLATFORM_PATH", "平台路径必须选择完整的二、三级内容")

    values = [
        session.get(m.PlatformMenuValue, draft.level2_value_id),
        session.get(m.PlatformMenuValue, draft.level3_value_id),
    ]
    if any(item is None for item in values):
        raise TaxonomyServiceError("MENU_VALUE_NOT_FOUND", "平台路径内容已不存在")
    for item in values:
        assert item is not None
        if item.status == "pending":
            item.status = "active"
            item.activated_by = actor
            item.activated_at = _now()

    # Database sessions deliberately use autoflush=False; publish the status
    # transition before querying the active Cartesian product.
    session.flush()
    variants = materialize_active_variants(
        session, draft.technology_id, actor=actor
    )
    variant = next(
        (
            item
            for item in variants
            if item.level2_value_id == draft.level2_value_id
            and item.level3_value_id == draft.level3_value_id
        ),
        None,
    )
    if variant is None:
        raise TaxonomyServiceError("PLATFORM_NOT_MATERIALIZED", "无法生成具体产品平台")

    observation.technology_id = draft.technology_id
    observation.generic_platform_id = variant.generic_platform_id
    draft.status = "confirmed"
    snapshot = capture_path_snapshot(
        session,
        target_type="raw_intent_observation",
        target_id=observation.id,
        technology_id=draft.technology_id,
        generic_platform_id=variant.generic_platform_id,
        actor=actor,
        version_no=1,
    )
    session.flush()
    return {
        "observationId": observation.id,
        "technologyId": draft.technology_id,
        "genericPlatformId": variant.generic_platform_id,
        "platformVariantId": variant.id,
        "mappingStatus": snapshot.mapping_status,
        "path": dict(snapshot.path),
    }


def intent_path(
    session: Session, *, target_type: str, target_id: str
) -> dict[str, Any] | None:
    """Read the newest immutable snapshot or a non-guessing legacy fallback."""

    snapshot = session.scalar(
        select(m.IntentPlatformPathSnapshot)
        .where(
            m.IntentPlatformPathSnapshot.target_type == target_type,
            m.IntentPlatformPathSnapshot.target_id == target_id,
        )
        .order_by(m.IntentPlatformPathSnapshot.version_no.desc())
    )
    if snapshot is not None:
        return {
            "snapshotId": snapshot.id,
            "versionNo": snapshot.version_no,
            "mappingStatus": snapshot.mapping_status,
            "platformVariantId": snapshot.platform_variant_id,
            "genericPlatformId": snapshot.generic_platform_id,
            "taxonomyRevision": snapshot.taxonomy_revision,
            "path": dict(snapshot.path),
        }

    target: m.RawIntentObservation | m.FormalIntentStatus | None
    if target_type == "raw_intent_observation":
        target = session.get(m.RawIntentObservation, target_id)
    elif target_type == "formal_intent_status":
        target = session.get(m.FormalIntentStatus, target_id)
    else:
        raise TaxonomyServiceError("UNSUPPORTED_TARGET_TYPE", "不支持的意向路径目标类型")
    if target is None or target.generic_platform_id is None:
        return None

    variant = session.scalar(
        select(m.PlatformVariant).where(
            m.PlatformVariant.generic_platform_id == target.generic_platform_id,
            m.PlatformVariant.technology_id == target.technology_id,
        )
    )
    if variant is not None:
        return {
            "snapshotId": None,
            "versionNo": None,
            "mappingStatus": "mapped",
            "platformVariantId": variant.id,
            "genericPlatformId": variant.generic_platform_id,
            "taxonomyRevision": variant.taxonomy_revision,
            "path": dict(variant.path_snapshot),
        }
    platform = session.get(m.GenericPlatform, target.generic_platform_id)
    technology = session.get(m.Technology, target.technology_id)
    return {
        "snapshotId": None,
        "versionNo": None,
        "mappingStatus": "legacy_pending",
        "platformVariantId": None,
        "genericPlatformId": target.generic_platform_id,
        "taxonomyRevision": None,
        "path": {
            "technology": (
                {"id": technology.id, "name": technology.name}
                if technology
                else {"id": target.technology_id, "name": None}
            ),
            "level2": None,
            "level3": None,
            "displayName": platform.name if platform else None,
            "legacyPlatformId": target.generic_platform_id,
        },
    }


__all__ = [
    "TaxonomyServiceError",
    "capture_path_snapshot",
    "catalogue",
    "commit_import",
    "confirm_observation_platform",
    "import_diff",
    "intent_path",
    "materialize_active_variants",
    "propose_observation_platform",
]
