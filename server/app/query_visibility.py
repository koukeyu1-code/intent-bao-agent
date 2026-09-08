from __future__ import annotations

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from . import models as m


def visible_observation_clause():
    """Return the business-query gate for model-produced observations.

    Rows created before analysis-run provenance was introduced remain visible.
    Provenanced rows are visible only after their run completed successfully and
    while no cancellation request is attached to that run.
    """

    successful_run = exists(
        select(m.AnalysisRun.id).where(
            m.AnalysisRun.id == m.RawIntentObservation.analysis_run_id,
            m.AnalysisRun.status == "已完成",
            m.AnalysisRun.cancel_requested_at.is_(None),
        )
    ).correlate(m.RawIntentObservation)
    return or_(
        m.RawIntentObservation.analysis_run_id.is_(None),
        successful_run,
    )


def visible_split_provenance_clause():
    """Manual splits inherit the visibility of their recorded source run."""
    run_id = m.ConfirmationRecord.after_snapshot["analysis_run_id"].as_string()
    successful_run = exists(select(m.AnalysisRun.id).where(
        m.AnalysisRun.id == run_id,
        m.AnalysisRun.status == "已完成",
        m.AnalysisRun.cancel_requested_at.is_(None),
    )).correlate(m.ConfirmationRecord)
    return and_(
        m.ConfirmationRecord.target_type == "speaker",
        m.ConfirmationRecord.action == "拆分来源",
        or_(run_id.is_(None), successful_run),
    )


def visible_speaker_clause():
    """Expose visible observations' speakers and attributable manual splits."""

    referenced = exists(
        select(m.RawIntentObservation.id).where(
            m.RawIntentObservation.speaker_id == m.SpeakerCluster.id,
            visible_observation_clause(),
        )
    ).correlate(m.SpeakerCluster)
    split_source = exists(select(m.ConfirmationRecord.id).where(
        m.ConfirmationRecord.target_id == m.SpeakerCluster.id,
        visible_split_provenance_clause(),
    )).correlate(m.SpeakerCluster)
    invalid_split_source = exists(select(m.ConfirmationRecord.id).where(
        m.ConfirmationRecord.target_id == m.SpeakerCluster.id,
        m.ConfirmationRecord.target_type == "speaker",
        m.ConfirmationRecord.action == "拆分来源",
        ~visible_split_provenance_clause(),
    )).correlate(m.SpeakerCluster)
    return and_(or_(referenced, split_source), ~invalid_split_source)


def get_visible_observation(
    session: Session, observation_id: str
) -> m.RawIntentObservation | None:
    return session.scalar(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.id == observation_id,
            visible_observation_clause(),
        )
    )


def get_visible_speaker(
    session: Session, speaker_id: str
) -> m.SpeakerCluster | None:
    return session.scalar(
        select(m.SpeakerCluster).where(
            m.SpeakerCluster.id == speaker_id,
            visible_speaker_clause(),
        )
    )
