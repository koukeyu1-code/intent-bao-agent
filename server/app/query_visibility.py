from __future__ import annotations

from sqlalchemy import exists, or_, select
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


def visible_speaker_clause():
    """Only expose speakers referenced by at least one visible observation."""

    return exists(
        select(m.RawIntentObservation.id).where(
            m.RawIntentObservation.speaker_id == m.SpeakerCluster.id,
            visible_observation_clause(),
        )
    ).correlate(m.SpeakerCluster)


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
