from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from .domain import ATTITUDE_LABELS, adoption_status_key, formal_status_key
from .taxonomy_service import intent_path


def iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def entity_ref(item: Any | None) -> dict[str, Any] | None:
    if item is None:
        return None
    result = {"id": item.id, "name": item.name}
    if hasattr(item, "code"):
        result["code"] = item.code
    if hasattr(item, "display_name"):
        result["display_name"] = item.display_name
    return result


def public_platform_draft(
    session: Session, observation_id: str
) -> dict[str, Any] | None:
    """Expose a pending reviewed path without publishing it as live taxonomy."""

    draft = session.scalar(
        select(m.ObservationPlatformDraft).where(
            m.ObservationPlatformDraft.observation_id == observation_id,
            m.ObservationPlatformDraft.status == "pending",
        )
    )
    if draft is None:
        return None
    technology = session.get(m.Technology, draft.technology_id)
    level2 = session.get(m.PlatformMenuValue, draft.level2_value_id)
    level3 = session.get(m.PlatformMenuValue, draft.level3_value_id)
    if level2 is None or level3 is None:
        return None
    return {
        "id": draft.id,
        "status": draft.status,
        "technologyId": draft.technology_id,
        "technologyName": technology.name if technology else None,
        "level2ValueId": level2.id,
        "level2Value": level2.value,
        "level2IsNew": level2.status == "pending",
        "level3ValueId": level3.id,
        "level3Value": level3.value,
        "level3IsNew": level3.status == "pending",
        "displayName": f"{level2.value} {level3.value}",
    }


def _ai_original(observation: m.RawIntentObservation) -> dict[str, Any]:
    if observation.ai_snapshot:
        return dict(observation.ai_snapshot)
    return {
        "attitude_level": observation.suggested_attitude,
        "adoption_stage": observation.suggested_adoption_stage,
        "support_reason": observation.support_reason,
        "resistance_reason": observation.resistance_reason,
        "bottlenecks": list(observation.bottlenecks or []),
        "decision_influence": observation.decision_influence,
        "confidence": observation.confidence,
        "analysis_run_id": observation.analysis_run_id,
        "model_name": observation.model_name,
        "prompt_version": observation.prompt_version,
        "rule_version": observation.rule_version,
    }


def observation_conflicts(
    session: Session, observation: m.RawIntentObservation
) -> list[str]:
    """Return explainable blockers used by the review presenter.

    A change from a historical formal state is intentionally surfaced as a
    conflict candidate rather than silently treating ``conflict`` as false.
    The product-planning reviewer can still confirm it as a new version.
    """

    reasons: list[str] = []
    if observation.function_id:
        siblings = session.scalars(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.id != observation.id,
                m.RawIntentObservation.package_id == observation.package_id,
                m.RawIntentObservation.customer_id == observation.customer_id,
                m.RawIntentObservation.technology_id
                == observation.technology_id,
                m.RawIntentObservation.generic_platform_id
                == observation.generic_platform_id,
                m.RawIntentObservation.customer_project_id
                == observation.customer_project_id,
                m.RawIntentObservation.function_id == observation.function_id,
            )
        ).all()
        if observation.suggested_attitude is not None:
            opposite = [
                item
                for item in siblings
                if item.suggested_attitude is not None
                and (
                    (
                        observation.suggested_attitude >= 4
                        and item.suggested_attitude <= 2
                    )
                    or (
                        observation.suggested_attitude <= 2
                        and item.suggested_attitude >= 4
                    )
                )
            ]
            if opposite:
                reasons.append(
                    f"同场同职能存在{len(opposite)}条相反态度证据"
                )

        status_key = formal_status_key(
            observation.customer_id,
            observation.technology_id,
            observation.generic_platform_id,
            observation.customer_project_id,
            observation.function_id,
        )
        current_formal = session.scalar(
            select(m.FormalIntentStatus)
            .where(
                m.FormalIntentStatus.status_key == status_key,
                m.FormalIntentStatus.is_current.is_(True),
            )
            .order_by(
                m.FormalIntentStatus.effective_at.desc(),
                m.FormalIntentStatus.version_no.desc(),
            )
        )
        if (
            current_formal
            and current_formal.attitude_level
            != observation.suggested_attitude
        ):
            reasons.append(
                "AI／人工整理态度与当前正式状态不同："
                f"{ATTITUDE_LABELS[current_formal.attitude_level]} → "
                f"{ATTITUDE_LABELS[observation.suggested_attitude]}"
            )

    if observation.suggested_adoption_stage:
        adoption_key = adoption_status_key(
            observation.customer_id,
            observation.technology_id,
            observation.generic_platform_id,
            observation.customer_project_id,
        )
        current_adoption = session.scalar(
            select(m.AdoptionStageState)
            .where(
                m.AdoptionStageState.status_key == adoption_key,
                m.AdoptionStageState.is_current.is_(True),
            )
            .order_by(
                m.AdoptionStageState.effective_at.desc(),
                m.AdoptionStageState.version_no.desc(),
            )
        )
        if (
            current_adoption
            and current_adoption.stage
            != observation.suggested_adoption_stage
        ):
            reasons.append(
                "建议采用阶段与当前正式阶段不同："
                f"{current_adoption.stage} → "
                f"{observation.suggested_adoption_stage}"
            )
    return reasons


def master_maps(session: Session) -> dict[str, dict[str, Any]]:
    return {
        "customers": {
            item.id: item for item in session.scalars(select(m.Customer)).all()
        },
        "technologies": {
            item.id: item for item in session.scalars(select(m.Technology)).all()
        },
        "platforms": {
            item.id: item
            for item in session.scalars(select(m.GenericPlatform)).all()
        },
        "projects": {
            item.id: item
            for item in session.scalars(select(m.CustomerProject)).all()
        },
        "functions": {
            item.id: item
            for item in session.scalars(select(m.CustomerFunction)).all()
        },
    }


def public_master_data(session: Session) -> dict[str, list[dict[str, Any]]]:
    customers = session.scalars(
        select(m.Customer).order_by(m.Customer.code)
    ).all()
    technologies = session.scalars(
        select(m.Technology).order_by(m.Technology.name)
    ).all()
    platforms = session.scalars(
        select(m.GenericPlatform).order_by(m.GenericPlatform.size_inch)
    ).all()
    customer_projects = session.scalars(
        select(m.CustomerProject).order_by(m.CustomerProject.name)
    ).all()
    functions = session.scalars(
        select(m.CustomerFunction).order_by(m.CustomerFunction.sort_order)
    ).all()
    aliases = session.scalars(select(m.Alias).order_by(m.Alias.alias)).all()
    return {
        "customers": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "display_name": item.display_name,
                "active": item.active,
                "version": item.version,
            }
            for item in customers
        ],
        "technologies": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "category": item.category,
                "description": item.description,
                "active": item.active,
                "version": item.version,
            }
            for item in technologies
        ],
        "generic_platforms": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "size_inch": item.size_inch,
                "resolution": item.resolution,
                "refresh_rate": item.refresh_rate,
                "product_series": item.product_series,
                "active": item.active,
                "version": item.version,
            }
            for item in platforms
        ],
        "customer_projects": [
            {
                "id": item.id,
                "customer_id": item.customer_id,
                "generic_platform_id": item.generic_platform_id,
                "code": item.code,
                "name": item.name,
                "active": item.active,
                "version": item.version,
            }
            for item in customer_projects
        ],
        "customer_functions": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "sort_order": item.sort_order,
                "active": item.active,
                "version": item.version,
            }
            for item in functions
        ],
        "aliases": [
            {
                "id": item.id,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
                "alias": item.alias,
                "active": item.active,
                "version": item.version,
            }
            for item in aliases
        ],
    }


def public_material(item: m.MaterialFile) -> dict[str, Any]:
    return {
        "id": item.id,
        "package_id": item.package_id,
        "original_name": item.original_name,
        "safe_name": item.safe_name,
        "media_type": item.media_type,
        "material_type": item.material_type,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "parse_status": item.parse_status,
        "is_deleted": item.is_deleted,
        "created_at": iso(item.created_at),
        "version": item.version,
    }


def public_task(item: m.ProcessingTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "package_id": item.package_id,
        "analysis_run_id": item.analysis_run_id,
        "stage": item.stage,
        "stage_order": item.stage_order,
        "status": item.status,
        "progress": item.progress,
        "attempt_count": item.attempt_count,
        "started_at": iso(item.started_at),
        "finished_at": iso(item.finished_at),
        "error_message": item.error_message,
        "updated_at": iso(item.updated_at),
        "version": item.version,
    }


def public_package(
    session: Session, item: m.MeetingPackage, *, include_detail: bool = False
) -> dict[str, Any]:
    customer = session.get(m.Customer, item.customer_id)
    material_count = session.scalar(
        select(func.count())
        .select_from(m.MaterialFile)
        .where(
            m.MaterialFile.package_id == item.id,
            m.MaterialFile.is_deleted.is_(False),
        )
    )
    pending_observations = session.scalar(
        select(func.count())
        .select_from(m.RawIntentObservation)
        .where(
            m.RawIntentObservation.package_id == item.id,
            m.RawIntentObservation.status.in_(["AI草稿", "待确认", "退回补充"]),
        )
    )
    latest_run = session.scalar(
        select(m.AnalysisRun)
        .where(m.AnalysisRun.package_id == item.id)
        .order_by(m.AnalysisRun.created_at.desc())
    )
    result: dict[str, Any] = {
        "id": item.id,
        "project_id": item.project_id,
        "customer": entity_ref(customer),
        "title": item.title,
        "meeting_date": iso(item.meeting_date),
        "data_scope": item.data_scope,
        "status": item.status,
        "material_completeness": item.material_completeness,
        "pdf_exception_reason": item.pdf_exception_reason,
        "retention_days": item.retention_days,
        "notes": item.notes,
        "material_count": material_count or 0,
        "pending_observation_count": pending_observations or 0,
        "last_analysis_run": (
            {
                "id": latest_run.id,
                "status": latest_run.status,
                "adapter": latest_run.adapter,
                "model_name": latest_run.model_name,
                "started_at": iso(latest_run.started_at),
                "finished_at": iso(latest_run.finished_at),
            }
            if latest_run
            else None
        ),
        "confirmed_by": item.confirmed_by,
        "confirmed_at": iso(item.confirmed_at),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
        "version": item.version,
    }
    if include_detail:
        attendees = session.scalars(
            select(m.MeetingAttendee)
            .where(m.MeetingAttendee.package_id == item.id)
            .order_by(m.MeetingAttendee.created_at)
        ).all()
        materials = session.scalars(
            select(m.MaterialFile)
            .where(
                m.MaterialFile.package_id == item.id,
                m.MaterialFile.is_deleted.is_(False),
            )
            .order_by(m.MaterialFile.created_at)
        ).all()
        tasks = session.scalars(
            select(m.ProcessingTask)
            .where(m.ProcessingTask.package_id == item.id)
            .order_by(
                m.ProcessingTask.created_at.desc(), m.ProcessingTask.stage_order
            )
        ).all()
        result["attendees"] = [
            {
                "id": attendee.id,
                "display_name": attendee.display_name,
                "role": attendee.role,
                "organization": attendee.organization,
                "function": entity_ref(
                    session.get(m.CustomerFunction, attendee.function_id)
                    if attendee.function_id
                    else None
                ),
                "is_confirmed": attendee.is_confirmed,
                "version": attendee.version,
            }
            for attendee in attendees
        ]
        result["files"] = [public_material(material) for material in materials]
        result["tasks"] = [public_task(task) for task in tasks]
    return result


def public_speaker(
    session: Session, speaker: m.SpeakerCluster, *, include_segments: bool = True
) -> dict[str, Any]:
    recommended_function = (
        session.get(m.CustomerFunction, speaker.recommended_function_id)
        if speaker.recommended_function_id
        else None
    )
    confirmed_function = (
        session.get(m.CustomerFunction, speaker.confirmed_function_id)
        if speaker.confirmed_function_id
        else None
    )
    result: dict[str, Any] = {
        "id": speaker.id,
        "package_id": speaker.package_id,
        "label": speaker.label,
        "recommended_identity": {
            "display_name": speaker.recommended_name,
            "role": speaker.recommended_role,
            "organization": speaker.recommended_organization,
            "function": entity_ref(recommended_function),
        },
        "confirmed_identity": {
            "display_name": speaker.confirmed_name,
            "role": speaker.confirmed_role,
            "organization": speaker.confirmed_organization,
            "function": entity_ref(confirmed_function),
        },
        "confidence": speaker.confidence,
        "status": speaker.status,
        "version": speaker.version,
        "updated_at": iso(speaker.updated_at),
    }
    if include_segments:
        segments = session.scalars(
            select(m.SpeakerSegment)
            .where(m.SpeakerSegment.speaker_id == speaker.id)
            .order_by(m.SpeakerSegment.start_ms)
        ).all()
        result["segments"] = [
            {
                "id": segment.id,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "transcript": segment.transcript,
                "confidence": segment.confidence,
                "overlapping": segment.overlapping,
            }
            for segment in segments
        ]
    return result


def public_evidence(
    session: Session, evidence: m.EvidenceFragment
) -> dict[str, Any]:
    package = session.get(m.MeetingPackage, evidence.package_id)
    material = (
        session.get(m.MaterialFile, evidence.material_file_id)
        if evidence.material_file_id
        else None
    )
    return {
        "id": evidence.id,
        "package_id": evidence.package_id,
        "meeting_title": package.title if package else None,
        "meeting_date": iso(package.meeting_date) if package else None,
        "data_scope": package.data_scope if package else None,
        "material": (
            {
                "id": material.id,
                "name": material.safe_name,
                "type": material.material_type,
            }
            if material
            else None
        ),
        "evidence_type": evidence.evidence_type,
        "locator": evidence.locator,
        "excerpt": evidence.excerpt,
        "start_ms": evidence.start_ms,
        "end_ms": evidence.end_ms,
        "page_no": evidence.page_no,
        "frame_no": evidence.frame_no,
        "confidence": evidence.confidence,
        "playback_available": evidence.playback_available,
        "content_url": f"/api/evidence/{evidence.id}/content",
        "created_at": iso(evidence.created_at),
    }


def public_observation(
    session: Session, observation: m.RawIntentObservation
) -> dict[str, Any]:
    maps = master_maps(session)
    evidence = (
        session.get(m.EvidenceFragment, observation.evidence_id)
        if observation.evidence_id
        else None
    )
    speaker = (
        session.get(m.SpeakerCluster, observation.speaker_id)
        if observation.speaker_id
        else None
    )
    conflict_reasons = observation_conflicts(session, observation)
    platform_path = intent_path(
        session,
        target_type="raw_intent_observation",
        target_id=observation.id,
    )
    platform_draft = public_platform_draft(session, observation.id)
    return {
        "id": observation.id,
        "package_id": observation.package_id,
        "analysis_run_id": observation.analysis_run_id,
        "customer": entity_ref(maps["customers"].get(observation.customer_id)),
        "technology": entity_ref(
            maps["technologies"].get(observation.technology_id)
        ),
        "generic_platform": entity_ref(
            maps["platforms"].get(observation.generic_platform_id)
        ),
        "platform_path": platform_path,
        "platform_draft": platform_draft,
        "customer_project": entity_ref(
            maps["projects"].get(observation.customer_project_id)
        ),
        "function": entity_ref(maps["functions"].get(observation.function_id)),
        "speaker": (
            {
                "id": speaker.id,
                "label": speaker.label,
                "display_name": speaker.confirmed_name or speaker.recommended_name,
                "status": speaker.status,
            }
            if speaker
            else None
        ),
        "evidence": public_evidence(session, evidence) if evidence else None,
        "raw_quote": observation.raw_quote,
        "suggested_attitude": {
            "level": observation.suggested_attitude,
            "label": ATTITUDE_LABELS[observation.suggested_attitude],
        },
        "suggested_adoption_stage": observation.suggested_adoption_stage,
        "support_reason": observation.support_reason,
        "resistance_reason": observation.resistance_reason,
        "bottlenecks": observation.bottlenecks or [],
        "decision_influence": observation.decision_influence,
        "confidence": observation.confidence,
        "status": observation.status,
        "uncertainty_reason": observation.uncertainty_reason,
        "model_name": observation.model_name,
        "prompt_version": observation.prompt_version,
        "rule_version": observation.rule_version,
        "ai_original": _ai_original(observation),
        "conflict": bool(conflict_reasons),
        "conflict_reasons": conflict_reasons,
        "created_at": iso(observation.created_at),
        "updated_at": iso(observation.updated_at),
        "version": observation.version,
    }


def public_formal_status(
    session: Session, item: m.FormalIntentStatus
) -> dict[str, Any]:
    maps = master_maps(session)
    adoption_key = "|".join(
        [
            item.customer_id,
            item.technology_id,
            item.generic_platform_id or "-",
            item.customer_project_id or "-",
        ]
    )
    adoption = session.scalar(
        select(m.AdoptionStageState)
        .where(
            m.AdoptionStageState.status_key == adoption_key,
            m.AdoptionStageState.effective_at <= item.effective_at,
        )
        .order_by(
            m.AdoptionStageState.effective_at.desc(),
            m.AdoptionStageState.version_no.desc(),
        )
    )
    evidence_items: list[dict[str, Any]] = []
    source_observations: list[m.RawIntentObservation] = []
    for observation_id in item.source_observation_ids or []:
        observation = session.get(m.RawIntentObservation, observation_id)
        if observation:
            source_observations.append(observation)
        if observation and observation.evidence_id:
            evidence = session.get(m.EvidenceFragment, observation.evidence_id)
            if evidence:
                evidence_items.append(public_evidence(session, evidence))
    platform_path = intent_path(
        session,
        target_type="formal_intent_status",
        target_id=item.id,
    )
    return {
        "id": item.id,
        "status_key": item.status_key,
        "customer": entity_ref(maps["customers"].get(item.customer_id)),
        "technology": entity_ref(
            maps["technologies"].get(item.technology_id)
        ),
        "generic_platform": entity_ref(
            maps["platforms"].get(item.generic_platform_id)
        ),
        "platform_path": platform_path,
        "customer_project": entity_ref(
            maps["projects"].get(item.customer_project_id)
        ),
        "function": entity_ref(maps["functions"].get(item.function_id)),
        "attitude": {
            "level": item.attitude_level,
            "label": ATTITUDE_LABELS[item.attitude_level],
        },
        "adoption_stage": adoption.stage if adoption else None,
        "is_supporter": item.is_supporter,
        "is_resistance": item.is_resistance,
        "conditions": item.conditions or [],
        "bottlenecks": item.bottlenecks or [],
        "support_reasons": [
            observation.support_reason
            for observation in source_observations
            if observation.support_reason
        ],
        "resistance_reasons": [
            observation.resistance_reason
            for observation in source_observations
            if observation.resistance_reason
        ],
        "source_observation_ids": item.source_observation_ids or [],
        "evidence": evidence_items,
        "evidence_count": len(evidence_items),
        "effective_at": iso(item.effective_at),
        "confirmed_by": item.confirmed_by,
        "confirmed_at": iso(item.confirmed_at),
        "change_note": item.change_note,
        "version_no": item.version_no,
        "previous_version_id": item.previous_version_id,
        "is_current": item.is_current,
        "created_at": iso(item.created_at),
    }


def dashboard_matrix(
    session: Session,
    *,
    technology_id: str | None = None,
    customer_id: str | None = None,
) -> dict[str, Any]:
    query = select(m.FormalIntentStatus).where(
        m.FormalIntentStatus.is_current.is_(True)
    )
    if technology_id:
        query = query.where(m.FormalIntentStatus.technology_id == technology_id)
    if customer_id:
        query = query.where(m.FormalIntentStatus.customer_id == customer_id)
    formal_items = session.scalars(
        query.order_by(
            m.FormalIntentStatus.technology_id,
            m.FormalIntentStatus.customer_id,
            m.FormalIntentStatus.function_id,
        )
    ).all()
    items = [public_formal_status(session, item) for item in formal_items]
    master = public_master_data(session)
    return {
        "items": items,
        "total": len(items),
        "filters": {
            "customers": master["customers"],
            "technologies": master["technologies"],
            "generic_platforms": master["generic_platforms"],
            "customer_functions": master["customer_functions"],
        },
        "data_scope": "模拟数据",
        "updated_at": iso(
            max((item.updated_at for item in formal_items), default=datetime.now())
        ),
    }


def dashboard_summary(session: Session) -> dict[str, Any]:
    package_total = session.scalar(
        select(func.count()).select_from(m.MeetingPackage)
    ) or 0
    pending_total = session.scalar(
        select(func.count())
        .select_from(m.MeetingPackage)
        .where(
            m.MeetingPackage.status.in_(
                ["待身份校验", "待意向确认", "待补资料／待标注", "处理失败"]
            )
        )
    ) or 0
    formal_total = session.scalar(
        select(func.count())
        .select_from(m.FormalIntentStatus)
        .where(m.FormalIntentStatus.is_current.is_(True))
    ) or 0
    evidence_backed = session.scalar(
        select(func.count())
        .select_from(m.FormalIntentStatus)
        .where(
            m.FormalIntentStatus.is_current.is_(True),
            func.json_array_length(m.FormalIntentStatus.source_observation_ids) > 0,
        )
    ) or 0
    statuses = session.execute(
        select(m.MeetingPackage.status, func.count())
        .group_by(m.MeetingPackage.status)
        .order_by(m.MeetingPackage.status)
    ).all()
    attitudes = session.execute(
        select(m.FormalIntentStatus.attitude_level, func.count())
        .where(m.FormalIntentStatus.is_current.is_(True))
        .group_by(m.FormalIntentStatus.attitude_level)
        .order_by(m.FormalIntentStatus.attitude_level.desc())
    ).all()
    adoptions = session.execute(
        select(m.AdoptionStageState.stage, func.count())
        .where(m.AdoptionStageState.is_current.is_(True))
        .group_by(m.AdoptionStageState.stage)
    ).all()
    latest_packages = session.scalars(
        select(m.MeetingPackage)
        .order_by(m.MeetingPackage.meeting_date.desc())
        .limit(5)
    ).all()
    latest_formal_updated_at = session.scalar(
        select(func.max(m.FormalIntentStatus.updated_at)).where(
            m.FormalIntentStatus.is_current.is_(True)
        )
    )
    return {
        "data_scope": "模拟数据",
        "notice": "当前为模拟数据流程演示，85%是后续真实样本验证目标，并非已实现结果。",
        "updated_at": iso(latest_formal_updated_at or datetime.now()),
        "metrics": {
            "meeting_packages": package_total,
            "pending_confirmation": pending_total,
            "confirmed_intents": formal_total,
            "customers_covered": session.scalar(
                select(func.count(func.distinct(m.FormalIntentStatus.customer_id))).where(
                    m.FormalIntentStatus.is_current.is_(True)
                )
            )
            or 0,
            "technologies_covered": session.scalar(
                select(
                    func.count(func.distinct(m.FormalIntentStatus.technology_id))
                ).where(m.FormalIntentStatus.is_current.is_(True))
            )
            or 0,
            "evidence_traceability_rate": (
                round(evidence_backed / formal_total * 100, 1)
                if formal_total
                else 0.0
            ),
        },
        "package_status_counts": [
            {"status": status, "count": count} for status, count in statuses
        ],
        "attitude_counts": [
            {
                "level": level,
                "label": ATTITUDE_LABELS[level],
                "count": count,
            }
            for level, count in attitudes
        ],
        "adoption_stage_counts": [
            {"stage": stage, "count": count} for stage, count in adoptions
        ],
        "recent_packages": [
            public_package(session, item) for item in latest_packages
        ],
    }


def frontend_compat_dataset(session: Session) -> dict[str, Any]:
    """Camel-case aggregate kept for the current Next.js MVP.

    Resource endpoints remain snake_case. This adapter deliberately contains no
    physical paths or identity-vault fields.
    """

    maps = master_maps(session)
    packages = session.scalars(
        select(m.MeetingPackage).order_by(m.MeetingPackage.meeting_date.desc())
    ).all()
    meetings: list[dict[str, Any]] = []
    for package in packages:
        files = session.scalars(
            select(m.MaterialFile).where(
                m.MaterialFile.package_id == package.id,
                m.MaterialFile.is_deleted.is_(False),
            )
        ).all()
        observations = session.scalars(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.package_id == package.id
            )
        ).all()
        tasks = session.scalars(
            select(m.ProcessingTask).where(
                m.ProcessingTask.package_id == package.id
            )
        ).all()
        technologies = sorted(
            {
                maps["technologies"][item.technology_id].name
                for item in observations
                if item.technology_id in maps["technologies"]
            }
        )
        platforms = sorted(
            {
                maps["platforms"][item.generic_platform_id].name
                for item in observations
                if item.generic_platform_id in maps["platforms"]
            }
        )
        material_map = {
            "会议资料": "会议PDF",
            "录音": "录音",
            "录屏": "录屏",
            "转写稿": "转写稿",
            "人工纪要": "人工纪要",
        }
        progress = (
            round(sum(task.progress for task in tasks) / len(tasks))
            if tasks
            else (100 if package.status == "已确认" else 0)
        )
        active_task = next(
            (
                task
                for task in sorted(tasks, key=lambda value: value.stage_order)
                if task.status in {"处理中", "待处理", "处理失败"}
            ),
            None,
        )
        status_map = {
            "待补资料／待标注": "待补资料",
            "已取消": "处理失败",
        }
        meetings.append(
            {
                "id": package.id,
                "title": package.title,
                "customer": maps["customers"][package.customer_id].display_name,
                "date": iso(package.meeting_date),
                "owner": "产品企划组",
                "duration": "待补充",
                "materials": [
                    material_map[file.material_type]
                    for file in files
                    if file.material_type in material_map
                ],
                "materialFiles": [
                    {
                        "id": file.id,
                        "name": file.safe_name,
                        "type": material_map.get(
                            file.material_type, file.material_type
                        ),
                        "sizeBytes": file.size_bytes,
                        "parseStatus": file.parse_status,
                    }
                    for file in files
                ],
                "materialCompleteness": package.material_completeness,
                "technologies": technologies,
                "platforms": platforms,
                "dataClass": package.data_scope,
                "status": status_map.get(package.status, package.status),
                "progress": progress,
                "currentStep": (
                    active_task.stage
                    if active_task
                    else (
                        "已形成正式状态版本"
                        if package.status == "已确认"
                        else package.status
                    )
                ),
                "updatedAt": iso(package.updated_at),
                **(
                    {"warning": "资料不完整，平台关联结果需人工确认"}
                    if package.material_completeness == "资料不完整"
                    else {}
                ),
            }
        )

    speaker_items = session.scalars(
        select(m.SpeakerCluster).order_by(
            m.SpeakerCluster.package_id, m.SpeakerCluster.label
        )
    ).all()
    speakers: list[dict[str, Any]] = []
    for speaker in speaker_items:
        segment = session.scalar(
            select(m.SpeakerSegment)
            .where(m.SpeakerSegment.speaker_id == speaker.id)
            .order_by(m.SpeakerSegment.start_ms)
        )
        function_id = (
            speaker.confirmed_function_id or speaker.recommended_function_id
        )
        function = maps["functions"].get(function_id)
        start_ms = segment.start_ms if segment else 0
        end_ms = segment.end_ms if segment else 0
        speakers.append(
            {
                "id": speaker.id,
                "meetingId": speaker.package_id,
                "speakerCode": speaker.label,
                "alias": speaker.confirmed_name
                or speaker.recommended_name
                or "待确认",
                "role": speaker.confirmed_role
                or speaker.recommended_role
                or "待确认",
                "organization": speaker.confirmed_organization
                or speaker.recommended_organization
                or "待确认",
                "functionName": function.name if function else "待确认",
                "confidence": speaker.confidence or 0,
                "confirmed": speaker.status == "已确认",
                "timeRange": f"{_timecode(start_ms)}–{_timecode(end_ms)}",
                "excerpt": segment.transcript if segment else "",
                "recommendationBasis": (
                    "结合本场参会信息、称呼和发言语义推荐；仍以人工校验为准。"
                    if speaker.status != "已确认"
                    else "已由人工结合本场参会信息完成确认。"
                ),
                "version": speaker.version,
            }
        )

    observation_items = session.scalars(
        select(m.RawIntentObservation).order_by(
            m.RawIntentObservation.updated_at.desc()
        )
    ).all()
    evidence_by_id: dict[str, dict[str, Any]] = {}
    intents: list[dict[str, Any]] = []
    for observation in observation_items:
        conflict_reasons = observation_conflicts(session, observation)
        evidence = (
            session.get(m.EvidenceFragment, observation.evidence_id)
            if observation.evidence_id
            else None
        )
        speaker = (
            session.get(m.SpeakerCluster, observation.speaker_id)
            if observation.speaker_id
            else None
        )
        function = maps["functions"].get(observation.function_id)
        platform = maps["platforms"].get(observation.generic_platform_id)
        platform_path = intent_path(
            session,
            target_type="raw_intent_observation",
            target_id=observation.id,
        )
        live_path = (platform_path or {}).get("path") or {}
        level2_path = live_path.get("level2") or {}
        level3_path = live_path.get("level3") or {}
        platform_draft = public_platform_draft(session, observation.id)
        customer_project = maps["projects"].get(observation.customer_project_id)
        intent_status_map = {
            "已被后续版本取代": "已确认",
        }
        intent = {
            "id": observation.id,
            "customer": maps["customers"][observation.customer_id].display_name,
            "technology": maps["technologies"][observation.technology_id].name,
            "technologyId": observation.technology_id,
            "platform": (
                f"{platform_draft['displayName']}（待确认）"
                if platform_draft
                else platform.name if platform else "平台待确认"
            ),
            "platformDraft": platform_draft,
            "platformMappingStatus": (
                platform_path.get("mappingStatus")
                if platform_path
                else "unmapped"
            ),
            **(
                {"platformLeafId": platform_path["platformVariantId"]}
                if platform_path and platform_path.get("platformVariantId")
                else {}
            ),
            **(
                {"level2ValueId": level2_path["valueId"]}
                if level2_path.get("valueId")
                else {}
            ),
            **(
                {"level3ValueId": level3_path["valueId"]}
                if level3_path.get("valueId")
                else {}
            ),
            "customerProject": customer_project.name
            if customer_project
            else "客户项目待确认",
            "functionName": function.name if function else "职能待确认",
            "person": (
                speaker.confirmed_name
                or speaker.recommended_name
                or "未识别"
                if speaker
                else "未识别"
            ),
            "identityConfirmed": bool(
                speaker is not None
                and speaker.status == "已确认"
                and speaker.confirmed_function_id is not None
            ),
            "attitude": observation.suggested_attitude,
            "adoptionStage": observation.suggested_adoption_stage
            or "未明确／未进入",
            "confidence": observation.confidence or 0,
            "decisionImpact": observation.decision_influence or "未知",
            "supportReason": observation.support_reason
            or "尚无明确支持理由",
            "blocker": "、".join(observation.bottlenecks or [])
            or observation.resistance_reason
            or observation.uncertainty_reason
            or "暂无明确卡点",
            "status": intent_status_map.get(
                observation.status, observation.status
            ),
            "evidenceId": observation.evidence_id or "",
            "conflict": bool(conflict_reasons),
            "conflictReasons": conflict_reasons,
            "updatedAt": iso(observation.updated_at),
            **(
                {"confirmedBy": "产品规划负责人"}
                if observation.status in {"已确认", "已被后续版本取代"}
                else {}
            ),
            "version": observation.version,
        }
        intents.append(intent)
        if evidence and evidence.id not in evidence_by_id:
            package = session.get(m.MeetingPackage, evidence.package_id)
            source_type = {
                "转写片段": "原始录音",
                "会议纪要": "人工纪要",
                "模拟会议纪要": "人工纪要",
                "PDF页面": "会议PDF",
                "录屏关键帧": "录屏画面",
            }.get(evidence.evidence_type, "人工纪要")
            evidence_by_id[evidence.id] = {
                "id": evidence.id,
                "meetingId": evidence.package_id,
                "meetingTitle": package.title if package else "",
                "meetingDate": iso(package.meeting_date) if package else "",
                "speaker": (
                    speaker.confirmed_name
                    or speaker.recommended_name
                    or "未识别"
                    if speaker
                    else "未识别"
                ),
                "speakerFunction": function.name if function else "职能待确认",
                "quote": evidence.excerpt,
                "timecode": evidence.locator,
                **({"pdfPage": evidence.page_no} if evidence.page_no else {}),
                **(
                    {"frameLabel": f"录屏关键帧 {evidence.frame_no}"}
                    if evidence.frame_no
                    else {}
                ),
                "sourceType": source_type,
                "aiReason": (
                    f"AI建议态度为“{ATTITUDE_LABELS[observation.suggested_attitude]}”；"
                    "该结果仍需产品规划负责人确认。"
                ),
            }

    formal_items = session.scalars(
        select(m.FormalIntentStatus).order_by(
            m.FormalIntentStatus.effective_at.desc(),
            m.FormalIntentStatus.version_no.desc(),
        )
    ).all()
    history: list[dict[str, Any]] = []
    for formal in formal_items:
        source_observation_id = (
            formal.source_observation_ids[0]
            if formal.source_observation_ids
            else ""
        )
        source_observation = (
            session.get(m.RawIntentObservation, source_observation_id)
            if source_observation_id
            else None
        )
        adoption_key = "|".join(
            [
                formal.customer_id,
                formal.technology_id,
                formal.generic_platform_id or "-",
                formal.customer_project_id or "-",
            ]
        )
        adoption = session.scalar(
            select(m.AdoptionStageState)
            .where(
                m.AdoptionStageState.status_key == adoption_key,
                m.AdoptionStageState.effective_at <= formal.effective_at,
            )
            .order_by(
                m.AdoptionStageState.effective_at.desc(),
                m.AdoptionStageState.version_no.desc(),
            )
        )
        history.append(
            {
                "id": f"history-{formal.id}",
                # Group the complete version chain by the stable formal-status
                # business key. Observation ids change from meeting to meeting
                # and would incorrectly split V1/V2 into separate timelines.
                "intentId": formal.status_key,
                "statusKey": formal.status_key,
                "customer": maps["customers"][formal.customer_id].display_name,
                "technology": maps["technologies"][formal.technology_id].name,
                "platform": (
                    maps["platforms"][formal.generic_platform_id].name
                    if formal.generic_platform_id in maps["platforms"]
                    else "平台待确认"
                ),
                "customerProject": (
                    maps["projects"][formal.customer_project_id].name
                    if formal.customer_project_id in maps["projects"]
                    else "客户项目待确认"
                ),
                "functionName": maps["functions"][formal.function_id].name,
                "date": iso(formal.effective_at),
                "title": f"职能态度确认为{ATTITUDE_LABELS[formal.attitude_level]}",
                "description": formal.change_note
                or "产品规划负责人基于会议证据确认正式状态。",
                "attitude": formal.attitude_level,
                "adoptionStage": adoption.stage
                if adoption
                else "未明确／未进入",
                "actor": formal.confirmed_by,
                "evidenceId": (
                    source_observation.evidence_id
                    if source_observation
                    else ""
                ),
                "version": f"正式版本 V{formal.version_no}",
                "isCurrent": formal.is_current,
            }
        )

    platform_parent_ids: dict[str, set[str]] = {}
    function_parent_ids: dict[str, set[str]] = {}
    relation_items = [
        *session.scalars(select(m.RawIntentObservation)).all(),
        *session.scalars(
            select(m.FormalIntentStatus).where(
                m.FormalIntentStatus.is_current.is_(True)
            )
        ).all(),
    ]
    for relation in relation_items:
        if relation.generic_platform_id and relation.technology_id:
            platform_parent_ids.setdefault(
                relation.generic_platform_id, set()
            ).add(relation.technology_id)
        if relation.function_id and relation.customer_id:
            function_parent_ids.setdefault(relation.function_id, set()).add(
                relation.customer_id
            )
    for variant in session.scalars(
        select(m.PlatformVariant).where(m.PlatformVariant.active.is_(True))
    ).all():
        platform_parent_ids.setdefault(
            variant.generic_platform_id, set()
        ).add(variant.technology_id)

    alias_items = session.scalars(
        select(m.Alias).where(m.Alias.active.is_(True))
    ).all()
    aliases_by_entity: dict[tuple[str, str], list[str]] = {}
    for alias in alias_items:
        aliases_by_entity.setdefault(
            (alias.entity_type, alias.entity_id), []
        ).append(alias.alias)

    def dictionary_item(
        item: Any,
        entity_type: str,
        note: str,
        parent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.display_name
            if hasattr(item, "display_name")
            else item.name,
            "code": item.code,
            "aliases": aliases_by_entity.get((entity_type, item.id), []),
            "parentIds": parent_ids or [],
            "status": "启用" if item.active else "停用",
            "updatedAt": iso(item.updated_at),
            "note": note,
        }

    dictionaries = {
        "technologies": [
            dictionary_item(
                item, "technology", item.description or item.category or "技术字典"
            )
            for item in maps["technologies"].values()
        ],
        "customers": [
            dictionary_item(item, "customer", "比赛演示脱敏客户")
            for item in maps["customers"].values()
        ],
        "platforms": [
            dictionary_item(
                item,
                "platform",
                "产品平台",
                sorted(platform_parent_ids.get(item.id, set())),
            )
            for item in maps["platforms"].values()
        ],
        "projects": [
            dictionary_item(
                item,
                "customer_project",
                "客户内部项目",
                [item.customer_id],
            )
            for item in maps["projects"].values()
        ],
        "functions": [
            dictionary_item(
                item,
                "function",
                "客户职能",
                sorted(function_parent_ids.get(item.id, set())),
            )
            for item in maps["functions"].values()
        ],
    }
    return {
        "meetings": meetings,
        "speakers": speakers,
        "intents": intents,
        "evidence": list(evidence_by_id.values()),
        "history": history,
        "dictionaries": dictionaries,
    }


def _timecode(milliseconds: int) -> str:
    seconds = max(milliseconds, 0) // 1000
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
