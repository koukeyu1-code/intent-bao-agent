from __future__ import annotations

import csv
import io
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .domain import ATTITUDE_LABELS
from .query_visibility import visible_observation_clause


def _spreadsheet_text(value: str) -> str:
    # Keep readable tabs/newlines, but render other control characters
    # explicitly so evidence cannot contain invisible XML-invalid bytes.
    return "".join(
        f"\\x{ord(char):02x}"
        if unicodedata.category(char) == "Cc" and char not in "\t\r\n"
        else char
        for char in value
    )


def _csv_safe_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = _spreadsheet_text(value)
    prefix_length = 0
    for char in text:
        if not (char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}):
            break
        prefix_length += 1
    candidate = text[prefix_length:]
    # CSV quoting escapes delimiters only; spreadsheet readers can still
    # execute quoted formulas, including those hidden by leading whitespace.
    if text.startswith(("\t", "\r", "\n")) or candidate.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _package_rows(
    session: Session, package_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observations = session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.package_id == package_id,
            visible_observation_clause(),
        )
    ).all()
    observation_rows: list[dict[str, Any]] = []
    for item in observations:
        speaker = (
            session.get(m.SpeakerCluster, item.speaker_id)
            if item.speaker_id
            else None
        )
        technology = session.get(m.Technology, item.technology_id)
        function = (
            session.get(m.CustomerFunction, item.function_id)
            if item.function_id
            else None
        )
        evidence = (
            session.get(m.EvidenceFragment, item.evidence_id)
            if item.evidence_id
            else None
        )
        observation_rows.append(
            {
                "类型": "原始意向观察",
                "状态": item.status,
                "技术": technology.name if technology else item.technology_id,
                "职能": function.name if function else "",
                "说话人": (
                    speaker.confirmed_name
                    or speaker.recommended_name
                    if speaker
                    else ""
                ),
                "五档态度": (
                    f"{item.suggested_attitude} "
                    f"{ATTITUDE_LABELS.get(item.suggested_attitude, '')}"
                    if item.suggested_attitude is not None
                    else "未知"
                ),
                "采用阶段": item.suggested_adoption_stage or "",
                "支持理由": item.support_reason or "",
                "阻力理由": item.resistance_reason or "",
                "卡点": "、".join(item.bottlenecks or []),
                "证据定位": evidence.locator if evidence else "",
                "原文": item.raw_quote,
                "置信度": item.confidence,
                "模型": item.model_name,
                "更新时间": (
                    item.updated_at.isoformat() if item.updated_at else ""
                ),
            }
        )

    formal_rows: list[dict[str, Any]] = []
    # A package export is intentionally package-scoped.  Formal status rows
    # are append-only customer history, so filtering by customer alone would
    # disclose conclusions sourced from other meetings.  Only include status
    # versions whose source observation belongs to this package.
    package_observation_ids = {item.id for item in observations}
    formal_items = [
        item
        for item in session.scalars(select(m.FormalIntentStatus)).all()
        if package_observation_ids.intersection(item.source_observation_ids or [])
    ]
    formal_by_key: dict[str, m.FormalIntentStatus] = {}
    for item in formal_items:
        existing = formal_by_key.get(item.status_key)
        if existing is None or item.version_no > existing.version_no:
            formal_by_key[item.status_key] = item
    for item in formal_by_key.values():
        technology = session.get(m.Technology, item.technology_id)
        function = (
            session.get(m.CustomerFunction, item.function_id)
            if item.function_id
            else None
        )
        formal_rows.append(
            {
                "类型": "正式职能状态",
                "状态": item.is_current and "当前有效" or "历史版本",
                "技术": technology.name if technology else item.technology_id,
                "职能": function.name if function else item.function_id,
                "说话人": "",
                "五档态度": (
                    f"{item.attitude_level} "
                    f"{ATTITUDE_LABELS.get(item.attitude_level, '')}"
                    if item.attitude_level is not None
                    else "未知"
                ),
                "采用阶段": "",
                "支持理由": "是" if item.is_supporter else "",
                "阻力理由": "是" if item.is_resistance else "",
                "卡点": "、".join(item.bottlenecks or []),
                "证据定位": "",
                "原文": "",
                "置信度": "",
                "模型": "",
                "更新时间": (
                    item.confirmed_at.isoformat() if item.confirmed_at else ""
                ),
            }
        )
    return observation_rows, formal_rows


def export_package_csv(
    session: Session, package_id: str
) -> tuple[str, str]:
    observation_rows, formal_rows = _package_rows(session, package_id)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=list(observation_rows[0].keys())
        if observation_rows
        else list(formal_rows[0].keys()) if formal_rows else ["类型"],
    )
    writer.writeheader()
    for row in observation_rows + formal_rows:
        writer.writerow({key: _csv_safe_value(value) for key, value in row.items()})
    return f"intent-bao-{package_id}.csv", output.getvalue()


def export_package_markdown(
    session: Session, package_id: str
) -> tuple[str, str]:
    observation_rows, formal_rows = _package_rows(session, package_id)
    lines = [
        f"# 意向宝资料包导出：{package_id}",
        "",
        "> 模拟数据或脱敏数据演示；正式结论均经过产品规划负责人确认。",
        "",
    ]

    def render_table(rows: list[dict[str, Any]]) -> list[str]:
        if not rows:
            return []
        headers = list(rows[0].keys())
        table = ["| " + " | ".join(headers) + " |"]
        table.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            table.append(
                "| "
                + " | ".join(str(row.get(key, "")).replace("|", "\\|") for key in headers)
                + " |"
            )
        return table

    lines.append("## 原始意向观察（AI草稿）")
    lines.extend(render_table(observation_rows))
    lines.append("")
    lines.append("## 正式职能状态")
    lines.extend(render_table(formal_rows))
    lines.append("")
    return f"intent-bao-{package_id}.md", "\n".join(lines)


def export_package_xlsx(
    session: Session, package_id: str
) -> tuple[str, bytes]:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    observation_rows, formal_rows = _package_rows(session, package_id)
    workbook = Workbook()
    headers = list(observation_rows[0].keys()) if observation_rows else (
        list(formal_rows[0].keys()) if formal_rows else []
    )

    def write_values(sheet, row_index: int, values: list[Any]) -> None:
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row_index, column)
            if isinstance(value, str):
                cell.value = _spreadsheet_text(value)
                # openpyxl otherwise infers a formula from a leading '='.
                cell.data_type = "s"
            else:
                cell.value = value

    def fill(sheet, rows: list[dict[str, Any]]) -> None:
        if headers:
            write_values(sheet, 1, headers)
            for cell in sheet[1]:
                cell.font = Font(bold=True)
        for row_index, row in enumerate(rows, 2 if headers else 1):
            write_values(sheet, row_index, [row.get(key, "") for key in headers])

    sheet = workbook.active
    sheet.title = "意向观察"
    fill(sheet, observation_rows)
    if formal_rows:
        sheet2 = workbook.create_sheet("正式状态")
        fill(sheet2, formal_rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return f"intent-bao-{package_id}.xlsx", buffer.getvalue()


def export_package(
    session: Session, package_id: str, fmt: str
) -> tuple[str, bytes]:
    normalized = (fmt or "csv").lower()
    if normalized == "csv":
        filename, text = export_package_csv(session, package_id)
        return filename, text.encode("utf-8-sig")
    if normalized == "md":
        filename, text = export_package_markdown(session, package_id)
        return filename, text.encode("utf-8")
    if normalized == "xlsx":
        return export_package_xlsx(session, package_id)
    raise ValueError("导出格式仅支持csv、xlsx或md")
