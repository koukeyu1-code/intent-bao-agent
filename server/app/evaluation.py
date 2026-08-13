from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .query_visibility import visible_observation_clause


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").strip()


def run_evaluation(
    session: Session,
    *,
    package_id: str,
    golden: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare golden intent records against AI draft observations.

    A golden record matches an observation when its quote is present
    verbatim (whitespace-insensitive).  A record counts as correct only when
    technology, function, attitude and adoption stage all match.
    """

    observations = session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.package_id == package_id,
            visible_observation_clause(),
        )
    ).all()
    by_quote: dict[str, list[m.RawIntentObservation]] = {}
    for observation in observations:
        by_quote.setdefault(
            _normalize(observation.raw_quote), []
        ).append(observation)

    total = len(golden)
    correct = 0
    traceable = 0
    unknown_total = 0
    unknown_misjudged = 0
    field_hits = {"technology": 0, "function": 0, "attitude": 0, "stage": 0}
    errors: list[dict[str, Any]] = []

    for record in golden:
        quote = _normalize(record.get("quote"))
        candidates = by_quote.get(quote, [])
        if not candidates:
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
        candidate = max(candidates, key=lambda item: item.confidence or 0)
        traceable += 1

        technology_ok = (
            candidate.technology_id == record.get("technology_id")
        )
        function_ok = (
            (candidate.function_id or None)
            == (record.get("function_id") or None)
        )
        attitude_ok = (
            candidate.suggested_attitude == record.get("attitude_level")
        )
        stage_ok = (
            (candidate.suggested_adoption_stage or None)
            == (record.get("adoption_stage") or None)
        )
        field_hits["technology"] += int(technology_ok)
        field_hits["function"] += int(function_ok)
        field_hits["attitude"] += int(attitude_ok)
        field_hits["stage"] += int(stage_ok)

        golden_attitude = record.get("attitude_level")
        if golden_attitude is None:
            unknown_total += 1
            if candidate.suggested_attitude is not None:
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
                        "technology_id": candidate.technology_id,
                        "function_id": candidate.function_id,
                        "attitude_level": candidate.suggested_attitude,
                        "adoption_stage": (
                            candidate.suggested_adoption_stage
                        ),
                    },
                }
            )

    def ratio(hits: int) -> float | None:
        return round(hits / total, 4) if total else None

    return {
        "package_id": package_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "correct": correct,
        "structured_accuracy": ratio(correct),
        "traceable": traceable,
        "traceable_rate": ratio(traceable),
        "unknown_total": unknown_total,
        "unknown_misjudged": unknown_misjudged,
        "unknown_misjudge_rate": (
            ratio(unknown_misjudged) if unknown_total else None
        ),
        "field_accuracy": {
            key: ratio(value) for key, value in field_hits.items()
        },
        "errors": errors[:100],
        "error_count": len(errors),
        "notice": "指标为AI原始输出对比结果，人工修正不计入模型准确率；85%是目标不是当前成绩。",
    }
