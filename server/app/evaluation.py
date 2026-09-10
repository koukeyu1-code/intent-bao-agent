from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .query_visibility import visible_observation_clause
from .services import DomainError, ai_original_snapshot


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").strip()


def run_evaluation(
    session: Session,
    *,
    package_id: str,
    golden: list[dict[str, Any]],
    analysis_run_id: str | None = None,
) -> dict[str, Any]:
    """Compare golden intent records against AI draft observations.

    A golden record matches an observation when its quote is present
    verbatim (whitespace-insensitive).  A record counts as correct only when
    technology, function, attitude and adoption stage all match.
    """

    successful_runs = select(m.AnalysisRun).where(
        m.AnalysisRun.package_id == package_id,
        m.AnalysisRun.status == "已完成",
        m.AnalysisRun.cancel_requested_at.is_(None),
    )
    if analysis_run_id is not None:
        run = session.scalar(successful_runs.where(m.AnalysisRun.id == analysis_run_id))
        if run is None:
            raise DomainError(422, "请选择本资料包中成功完成且未取消的分析批次")
    else:
        run = session.scalar(successful_runs.order_by(
            m.AnalysisRun.finished_at.desc(), m.AnalysisRun.started_at.desc(),
            m.AnalysisRun.id.desc(),
        ).limit(1))
    selected_run_id = run.id if run else None
    observations = session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.package_id == package_id,
            m.RawIntentObservation.analysis_run_id == selected_run_id,
            visible_observation_clause(),
        )
    ).all()
    by_quote: dict[str, list[dict[str, Any]]] = {}
    required = {"quote", "technology_id", "function_id", "attitude_level", "adoption_stage"}
    for observation in observations:
        snapshot = ai_original_snapshot(observation)
        if (not observation.ai_snapshot and observation.version != 1) or not required.issubset(snapshot):
            raise DomainError(422, "该分析批次缺少完整AI原始快照，无法可靠评测；请重新分析后再评测")
        by_quote.setdefault(_normalize(snapshot["quote"]), []).append(snapshot)

    total = len(golden)
    correct = 0
    traceable = 0
    unknown_total = sum(record.get("attitude_level") is None for record in golden)
    unknown_misjudged = 0
    unknown_missed = 0
    missed = 0
    field_hits = {"technology": 0, "function": 0, "attitude": 0, "stage": 0}
    errors: list[dict[str, Any]] = []

    for record in golden:
        quote = _normalize(record.get("quote"))
        candidates = by_quote.get(quote, [])
        if not candidates:
            missed += 1
            unknown_missed += int(record.get("attitude_level") is None)
            errors.append(
                {
                    "quote": record.get("quote"),
                    "technology_id": record.get("technology_id"),
                    "function_id": record.get("function_id"),
                    "attitude_level": record.get("attitude_level"),
                    "adoption_stage": record.get("adoption_stage"),
                    "error": "未找到匹配的AI观察",
                }
            )
            continue
        candidate = max(candidates, key=lambda item: item.get("confidence") or 0)
        traceable += 1

        technology_ok = (
            candidate["technology_id"] == record.get("technology_id")
        )
        function_ok = (
            (candidate["function_id"] or None)
            == (record.get("function_id") or None)
        )
        attitude_ok = (
            candidate["attitude_level"] == record.get("attitude_level")
        )
        stage_ok = (
            (candidate["adoption_stage"] or None)
            == (record.get("adoption_stage") or None)
        )
        field_hits["technology"] += int(technology_ok)
        field_hits["function"] += int(function_ok)
        field_hits["attitude"] += int(attitude_ok)
        field_hits["stage"] += int(stage_ok)

        golden_attitude = record.get("attitude_level")
        if golden_attitude is None:
            if candidate["attitude_level"] is not None:
                unknown_misjudged += 1

        if technology_ok and function_ok and attitude_ok and stage_ok:
            correct += 1
        else:
            errors.append(
                {
                    "quote": record.get("quote"),
                    "technology_id": record.get("technology_id"),
                    "function_id": record.get("function_id"),
                    "attitude_level": record.get("attitude_level"),
                    "adoption_stage": record.get("adoption_stage"),
                    "error": "字段不一致",
                    "model": {
                        "technology_id": candidate["technology_id"],
                        "function_id": candidate["function_id"],
                        "attitude_level": candidate["attitude_level"],
                        "adoption_stage": candidate["adoption_stage"],
                    },
                }
            )

    def ratio(hits: int) -> float | None:
        return round(hits / total, 4) if total else None

    return {
        "package_id": package_id,
        "analysis_run_id": selected_run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "correct": correct,
        "structured_accuracy": ratio(correct),
        "traceable": traceable,
        "traceable_rate": ratio(traceable),
        "missed": missed,
        "unknown_total": unknown_total,
        "unknown_misjudged": unknown_misjudged,
        "unknown_missed": unknown_missed,
        "unknown_misjudge_rate": (
            round(unknown_misjudged / unknown_total, 4) if unknown_total else None
        ),
        "field_accuracy": {
            key: ratio(value) for key, value in field_hits.items()
        },
        "errors": errors[:100],
        "error_count": len(errors),
        "notice": "只评测所选批次的AI原始快照，默认最近成功批次；无批次时仅评测未修改的历史记录。未知误判率分母为全部未知金标准样本，漏检单独计数且不算正确；85%是目标不是当前成绩。",
    }
