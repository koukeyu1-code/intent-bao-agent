from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditColumns:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, default=1)


class Project(Base, AuditColumns):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    data_scope: Mapped[str] = mapped_column(String(32), default="模拟数据")
    retention_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(Base, AuditColumns):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Technology(Base, AuditColumns):
    __tablename__ = "technologies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    category: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class GenericPlatform(Base, AuditColumns):
    __tablename__ = "generic_platforms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    size_inch: Mapped[float | None] = mapped_column(Float)
    resolution: Mapped[str | None] = mapped_column(String(64))
    refresh_rate: Mapped[str | None] = mapped_column(String(64))
    product_series: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PlatformMenuDefinition(Base, AuditColumns):
    """Administrator-managed names for the dynamic platform dimensions.

    The first release supports exactly two dimensions beneath a technology,
    but stores their labels as data (for example ``尺寸`` and ``分辨率``)
    instead of hard-coding them into the UI.
    """

    __tablename__ = "platform_menu_definitions"
    __table_args__ = (
        UniqueConstraint(
            "technology_id", "level", name="uq_platform_menu_definition"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    taxonomy_revision: Mapped[int] = mapped_column(Integer, default=1)


class PlatformMenuValue(Base, AuditColumns):
    """A value under an existing administrator-defined platform dimension."""

    __tablename__ = "platform_menu_values"
    __table_args__ = (
        UniqueConstraint(
            "definition_id", "normalized_value", name="uq_platform_menu_value"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    definition_id: Mapped[str] = mapped_column(
        ForeignKey("platform_menu_definitions.id", ondelete="CASCADE"),
        index=True,
    )
    value: Mapped[str] = mapped_column(String(200))
    normalized_value: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source: Mapped[str] = mapped_column(String(32), default="excel")
    created_by: Mapped[str] = mapped_column(String(100))
    activated_by: Mapped[str | None] = mapped_column(String(100))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    taxonomy_revision: Mapped[int] = mapped_column(Integer, default=1)


class PlatformVariant(Base, AuditColumns):
    """A materialized concrete leaf produced by level-2 x level-3 values.

    ``GenericPlatform`` remains the stable foreign-key target used by the
    existing intent tables.  This table adds the strict technology ownership
    and menu path without rewriting historical foreign keys.
    """

    __tablename__ = "platform_variants"
    __table_args__ = (
        UniqueConstraint(
            "technology_id",
            "level2_value_id",
            "level3_value_id",
            name="uq_platform_variant_path",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id", ondelete="CASCADE"), index=True
    )
    level2_value_id: Mapped[str] = mapped_column(
        ForeignKey("platform_menu_values.id"), index=True
    )
    level3_value_id: Mapped[str] = mapped_column(
        ForeignKey("platform_menu_values.id"), index=True
    )
    generic_platform_id: Mapped[str] = mapped_column(
        ForeignKey("generic_platforms.id"), unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(300))
    path_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    taxonomy_revision: Mapped[int] = mapped_column(Integer, default=1)
    mapping_status: Mapped[str] = mapped_column(String(32), default="mapped")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(100))


class TaxonomyImportBatch(Base):
    """Audit record written only when an administrator publishes a preview."""

    __tablename__ = "taxonomy_import_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    file_name: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="committed")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    committed_by: Mapped[str] = mapped_column(String(100))
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ObservationPlatformDraft(Base, AuditColumns):
    """A reviewed platform path waiting for the intent's formal confirmation."""

    __tablename__ = "observation_platform_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("raw_intent_observations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id"), index=True
    )
    level2_value_id: Mapped[str | None] = mapped_column(
        ForeignKey("platform_menu_values.id")
    )
    level3_value_id: Mapped[str | None] = mapped_column(
        ForeignKey("platform_menu_values.id")
    )
    status: Mapped[str] = mapped_column(String(32), default="pending")
    proposed_by: Mapped[str] = mapped_column(String(100))


class IntentPlatformPathSnapshot(Base):
    """Immutable display path retained even if the live taxonomy is renamed."""

    __tablename__ = "intent_platform_path_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "version_no",
            name="uq_intent_platform_path_snapshot",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    technology_id: Mapped[str | None] = mapped_column(String(64))
    generic_platform_id: Mapped[str | None] = mapped_column(String(64))
    platform_variant_id: Mapped[str | None] = mapped_column(String(64))
    taxonomy_revision: Mapped[int | None] = mapped_column(Integer)
    mapping_status: Mapped[str] = mapped_column(String(32), default="legacy_pending")
    path: Mapped[dict[str, Any]] = mapped_column(JSON)
    captured_by: Mapped[str] = mapped_column(String(100))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CustomerProject(Base, AuditColumns):
    __tablename__ = "customer_projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    generic_platform_id: Mapped[str | None] = mapped_column(
        ForeignKey("generic_platforms.id")
    )
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CustomerFunction(Base, AuditColumns):
    __tablename__ = "customer_functions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Alias(Base, AuditColumns):
    __tablename__ = "aliases"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "alias", name="uq_alias"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    alias: Mapped[str] = mapped_column(String(200), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class MeetingPackage(Base, AuditColumns):
    __tablename__ = "meeting_packages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    meeting_date: Mapped[date] = mapped_column(Date, index=True)
    data_scope: Mapped[str] = mapped_column(String(32), default="模拟数据")
    status: Mapped[str] = mapped_column(String(32), default="草稿", index=True)
    material_completeness: Mapped[str] = mapped_column(
        String(32), default="待检查"
    )
    pdf_exception_reason: Mapped[str | None] = mapped_column(Text)
    retention_days: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MeetingAttendee(Base, AuditColumns):
    __tablename__ = "meeting_attendees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    internal_person_id: Mapped[str | None] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str | None] = mapped_column(String(100))
    organization: Mapped[str | None] = mapped_column(String(200))
    function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id")
    )
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class IdentityVault(Base, AuditColumns):
    """Separated identity store; normal dashboard queries never access this table."""

    __tablename__ = "identity_vault"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    internal_person_id: Mapped[str] = mapped_column(String(64), unique=True)
    real_name: Mapped[str | None] = mapped_column(String(200))
    masked_name: Mapped[str] = mapped_column(String(100))
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    organization: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(100))
    function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id")
    )


class MaterialFile(Base, AuditColumns):
    __tablename__ = "material_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(String(500))
    safe_name: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str | None] = mapped_column(String(200))
    material_type: Mapped[str] = mapped_column(String(32))
    relative_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(32), default="已上传")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class AnalysisRun(Base, AuditColumns):
    __tablename__ = "analysis_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    adapter: Mapped[str] = mapped_column(String(64), default="mock")
    model_name: Mapped[str] = mapped_column(String(128), default="mock-intent-v1")
    prompt_version: Mapped[str] = mapped_column(String(64), default="intent-prompt-v1")
    rule_version: Mapped[str] = mapped_column(String(64), default="business-rule-v1")
    status: Mapped[str] = mapped_column(String(32), default="排队中")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)


class ProcessingTask(Base, AuditColumns):
    __tablename__ = "processing_tasks"
    __table_args__ = (
        UniqueConstraint("analysis_run_id", "stage", name="uq_run_stage"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64))
    stage_order: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="待处理")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)


class SpeakerCluster(Base, AuditColumns):
    __tablename__ = "speaker_clusters"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(32))
    recommended_name: Mapped[str | None] = mapped_column(String(100))
    recommended_role: Mapped[str | None] = mapped_column(String(100))
    recommended_organization: Mapped[str | None] = mapped_column(String(200))
    recommended_function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id")
    )
    confirmed_name: Mapped[str | None] = mapped_column(String(100))
    confirmed_role: Mapped[str | None] = mapped_column(String(100))
    confirmed_organization: Mapped[str | None] = mapped_column(String(200))
    confirmed_function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id")
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="待确认")


class SpeakerSegment(Base, AuditColumns):
    __tablename__ = "speaker_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    speaker_id: Mapped[str] = mapped_column(
        ForeignKey("speaker_clusters.id", ondelete="CASCADE"), index=True
    )
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    transcript: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    overlapping: Mapped[bool] = mapped_column(Boolean, default=False)


class SpeakerIdentityMapping(Base, AuditColumns):
    __tablename__ = "speaker_identity_mappings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    speaker_id: Mapped[str] = mapped_column(
        ForeignKey("speaker_clusters.id", ondelete="CASCADE"), index=True
    )
    internal_person_id: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str | None] = mapped_column(String(100))
    organization: Mapped[str | None] = mapped_column(String(200))
    function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id")
    )
    identity_granularity: Mapped[str] = mapped_column(String(32), default="职能")
    confirmed_by: Mapped[str] = mapped_column(
        String(100), default="本地产品规划负责人"
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class EvidenceFragment(Base, AuditColumns):
    __tablename__ = "evidence_fragments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    material_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("material_files.id")
    )
    evidence_type: Mapped[str] = mapped_column(String(64))
    locator: Mapped[str] = mapped_column(String(500))
    excerpt: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int | None] = mapped_column(Integer)
    end_ms: Mapped[int | None] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer)
    frame_no: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    playback_available: Mapped[bool] = mapped_column(Boolean, default=False)


class RawIntentObservation(Base, AuditColumns):
    __tablename__ = "raw_intent_observations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    analysis_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_runs.id")
    )
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id"), index=True
    )
    generic_platform_id: Mapped[str | None] = mapped_column(
        ForeignKey("generic_platforms.id")
    )
    customer_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_projects.id")
    )
    function_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_functions.id"), index=True
    )
    speaker_id: Mapped[str | None] = mapped_column(ForeignKey("speaker_clusters.id"))
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_fragments.id")
    )
    raw_quote: Mapped[str] = mapped_column(Text)
    suggested_attitude: Mapped[int | None] = mapped_column(Integer)
    suggested_adoption_stage: Mapped[str | None] = mapped_column(String(64))
    support_reason: Mapped[str | None] = mapped_column(Text)
    resistance_reason: Mapped[str | None] = mapped_column(Text)
    bottlenecks: Mapped[list[str] | None] = mapped_column(JSON)
    decision_influence: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="AI草稿", index=True)
    uncertainty_reason: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(128), default="mock-intent-v1")
    prompt_version: Mapped[str] = mapped_column(String(64), default="intent-prompt-v1")
    rule_version: Mapped[str] = mapped_column(String(64), default="business-rule-v1")
    # Immutable baseline captured when the model output is first reviewed.  The
    # mutable ``suggested_*`` fields above are the human-reviewed draft used by
    # the current MVP UI; this JSON keeps the original AI output available for
    # audit and later accuracy evaluation.
    ai_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class FormalIntentStatus(Base, AuditColumns):
    __tablename__ = "formal_intent_statuses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    status_key: Mapped[str] = mapped_column(String(500), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id"), index=True
    )
    generic_platform_id: Mapped[str | None] = mapped_column(
        ForeignKey("generic_platforms.id")
    )
    customer_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_projects.id")
    )
    function_id: Mapped[str] = mapped_column(
        ForeignKey("customer_functions.id"), index=True
    )
    attitude_level: Mapped[int | None] = mapped_column(Integer)
    is_supporter: Mapped[bool] = mapped_column(Boolean, default=False)
    is_resistance: Mapped[bool] = mapped_column(Boolean, default=False)
    conditions: Mapped[list[str] | None] = mapped_column(JSON)
    bottlenecks: Mapped[list[str] | None] = mapped_column(JSON)
    source_observation_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    effective_at: Mapped[date] = mapped_column(Date)
    confirmed_by: Mapped[str] = mapped_column(String(100))
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    change_note: Mapped[str | None] = mapped_column(Text)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("formal_intent_statuses.id")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class AdoptionStageState(Base, AuditColumns):
    __tablename__ = "adoption_stage_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    status_key: Mapped[str] = mapped_column(String(500), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    technology_id: Mapped[str] = mapped_column(
        ForeignKey("technologies.id"), index=True
    )
    generic_platform_id: Mapped[str | None] = mapped_column(
        ForeignKey("generic_platforms.id")
    )
    customer_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("customer_projects.id")
    )
    stage: Mapped[str] = mapped_column(String(64))
    source_observation_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    effective_at: Mapped[date] = mapped_column(Date)
    confirmed_by: Mapped[str] = mapped_column(String(100))
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    change_note: Mapped[str | None] = mapped_column(Text)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("adoption_stage_states.id")
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ConfirmationRecord(Base):
    __tablename__ = "confirmation_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    target_type: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(32))
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(100))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class StatusVersion(Base):
    __tablename__ = "status_versions"
    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "version_no", name="uq_status_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    changed_by: Mapped[str] = mapped_column(String(100))
    change_reason: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class TranscriptSegment(Base, AuditColumns):
    """Timed transcript segments produced by ASR or parsed from subtitles."""

    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    material_file_id: Mapped[str | None] = mapped_column(
        ForeignKey("material_files.id", ondelete="SET NULL")
    )
    order_index: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    speaker_label: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    output_hash: Mapped[str | None] = mapped_column(String(64))


class PdfPageText(Base, AuditColumns):
    """Per-page extracted text for text-based PDF materials."""

    __tablename__ = "pdf_page_texts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    material_file_id: Mapped[str] = mapped_column(
        ForeignKey("material_files.id", ondelete="CASCADE"), index=True
    )
    page_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    extracted_by: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))


class KeyFrame(Base, AuditColumns):
    """Key frames extracted from screen recordings."""

    __tablename__ = "key_frames"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    material_file_id: Mapped[str] = mapped_column(
        ForeignKey("material_files.id", ondelete="CASCADE"), index=True
    )
    time_ms: Mapped[int | None] = mapped_column(Integer)
    image_relative_path: Mapped[str] = mapped_column(String(500))
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    ocr_text: Mapped[str | None] = mapped_column(Text)
    page_candidates_json: Mapped[str | None] = mapped_column(Text)


class AlignmentLink(Base, AuditColumns):
    """Semantic alignment between a transcript segment and a page/key frame."""

    __tablename__ = "alignment_links"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.id", ondelete="CASCADE"), index=True
    )
    package_id: Mapped[str] = mapped_column(
        ForeignKey("meeting_packages.id", ondelete="CASCADE"), index=True
    )
    segment_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_segments.id", ondelete="CASCADE")
    )
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="候选")
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
