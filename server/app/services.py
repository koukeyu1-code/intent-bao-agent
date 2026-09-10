from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from fastapi import UploadFile
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import models as m
from .config import Settings
from .diarization import DiarizationCancelled, DiarizationUnavailable, get_diarizer
from .domain import (
    TERMINAL_OBSERVATION_STATUSES,
    adoption_status_key,
    formal_status_key,
)
from .audio_adapters import AsrAdapter, AsrSegment, get_asr_adapter
from .asr_checkpoints import AsrCheckpoints
from .media_processing import (
    extract_audio_track,
    extract_key_frames,
    extract_pdf_text,
    ocr_image,
    parse_subtitle,
    probe_media_duration,
    slice_audio_segment,
)
from .model_adapters import IntentModelAdapter, ModelContext, get_model_adapter
from .stage_adapters import get_stage_adapter
from .request_budget import estimate_tokens, split_items_for_request
from .schemas import IntentConfirmRequest, SpeakerConfirmRequest
from .query_visibility import (
    get_visible_speaker, visible_observation_clause, visible_split_provenance_clause,
)
from .seed import TASK_STAGES
from .taxonomy_service import (
    capture_path_snapshot,
    confirm_observation_platform,
)

logger = logging.getLogger(__name__)

_ASR_WINDOW_MS = 30_000


def _asr_windows(
    start_ms: int, end_ms: int, window_ms: int = _ASR_WINDOW_MS
) -> list[tuple[int, int]]:
    """Split [start_ms, end_ms) into bounded windows for llama-server."""
    if end_ms <= start_ms:
        return []
    windows: list[tuple[int, int]] = []
    cursor = start_ms
    while cursor < end_ms:
        next_ms = min(end_ms, cursor + window_ms)
        windows.append((cursor, next_ms))
        cursor = next_ms
    return windows


class DomainError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _AnalysisCancellationRequested(Exception):
    """Internal control flow used to stop a run at a safe stage boundary."""


def analysis_run_cancel_requested(
    session: Session, run: m.AnalysisRun
) -> bool:
    """Refresh the cooperative cancellation marker from the database."""

    session.refresh(
        run,
        attribute_names=["status", "cancel_requested_at"],
    )
    return run.status == "已取消" or run.cancel_requested_at is not None


def _raise_if_analysis_cancel_requested(
    session: Session, run: m.AnalysisRun
) -> None:
    if analysis_run_cancel_requested(session, run):
        raise _AnalysisCancellationRequested


def _mark_analysis_run_cancelled(
    session: Session, run: m.AnalysisRun
) -> None:
    now = m.utcnow()
    run.cancel_requested_at = run.cancel_requested_at or now
    run.status = "已取消"
    run.error_message = "由用户取消"
    run.finished_at = run.finished_at or now
    tasks = session.scalars(
        select(m.ProcessingTask).where(
            m.ProcessingTask.analysis_run_id == run.id
        )
    ).all()
    for task in tasks:
        if task.status not in {"已完成", "不适用"}:
            task.status = "已取消"
            task.error_message = "由用户取消"
            task.finished_at = task.finished_at or now
    package = session.get(m.MeetingPackage, run.package_id)
    if package is not None and package.status == "分析中":
        another_active_run = session.scalar(
            select(m.AnalysisRun.id).where(
                m.AnalysisRun.package_id == run.package_id,
                m.AnalysisRun.id != run.id,
                m.AnalysisRun.status.in_(("排队中", "运行中")),
                m.AnalysisRun.cancel_requested_at.is_(None),
            )
        )
        if another_active_run is None:
            package.status = "已取消"
            package.last_error = "由用户取消"
            package.version += 1


def _cleanup_cancelled_analysis_run_artifacts(
    session: Session, run: m.AnalysisRun
) -> None:
    """Remove run-owned drafts without touching uploads or confirmed history.

    Some pipeline stages intentionally commit their output before the next
    expensive model call.  A cancellation that arrives after such a commit
    must therefore clean up or quarantine those rows explicitly.  Tables that
    do not yet carry ``analysis_run_id`` (PDF pages, key frames and speaker
    clusters) are deliberately left alone: guessing their ownership from a
    package or timestamp could delete output from an earlier successful run.
    """

    observations = list(
        session.scalars(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.analysis_run_id == run.id
            )
        ).all()
    )
    observation_ids = {item.id for item in observations}
    protected_ids: set[str] = set()
    if observation_ids:
        protected_ids.update(
            session.scalars(
                select(m.ConfirmationRecord.target_id).where(
                    m.ConfirmationRecord.target_type
                    == "raw_intent_observation",
                    m.ConfirmationRecord.target_id.in_(observation_ids),
                )
            ).all()
        )
        protected_ids.update(
            session.scalars(
                select(m.IntentPlatformPathSnapshot.target_id).where(
                    m.IntentPlatformPathSnapshot.target_type
                    == "raw_intent_observation",
                    m.IntentPlatformPathSnapshot.target_id.in_(observation_ids),
                )
            ).all()
        )
        protected_ids.update(
            session.scalars(
                select(m.ObservationPlatformDraft.observation_id).where(
                    m.ObservationPlatformDraft.observation_id.in_(observation_ids)
                )
            ).all()
        )
        for formal in session.scalars(select(m.FormalIntentStatus)).all():
            protected_ids.update(
                observation_ids.intersection(formal.source_observation_ids or [])
            )
        for adoption in session.scalars(select(m.AdoptionStageState)).all():
            protected_ids.update(
                observation_ids.intersection(adoption.source_observation_ids or [])
            )

    candidate_evidence_ids: set[str] = set()
    for observation in observations:
        if observation.id in protected_ids:
            # Historical/audited observations remain in storage, but normal
            # queries and the confirmation Gate exclude cancelled runs.
            continue
        if observation.evidence_id:
            candidate_evidence_ids.add(observation.evidence_id)
        session.delete(observation)
    session.flush()

    for evidence_id in candidate_evidence_ids:
        still_referenced = session.scalar(
            select(m.RawIntentObservation.id).where(
                m.RawIntentObservation.evidence_id == evidence_id
            )
        )
        if still_referenced is None:
            evidence = session.get(m.EvidenceFragment, evidence_id)
            if evidence is not None:
                session.delete(evidence)

    for alignment in session.scalars(
        select(m.AlignmentLink).where(m.AlignmentLink.analysis_run_id == run.id)
    ).all():
        session.delete(alignment)
    for transcript in session.scalars(
        select(m.TranscriptSegment).where(
            m.TranscriptSegment.analysis_run_id == run.id
        )
    ).all():
        session.delete(transcript)


def request_analysis_run_cancellation(
    session: Session, run: m.AnalysisRun
) -> bool:
    """Persist an idempotent cancellation request from the API thread."""

    now = m.utcnow()
    requested = session.execute(
        update(m.AnalysisRun)
        .where(
            m.AnalysisRun.id == run.id,
            m.AnalysisRun.status.in_(("排队中", "运行中")),
            m.AnalysisRun.cancel_requested_at.is_(None),
        )
        .values(
            cancel_requested_at=now,
            status="已取消",
            error_message="由用户取消",
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if requested.rowcount != 1:
        session.rollback()
        session.refresh(run)
        return False
    session.refresh(run)
    _mark_analysis_run_cancelled(session, run)
    session.commit()
    session.refresh(run)
    return True


def cancel_active_analysis_runs(session: Session, package_id: str) -> int:
    """Request cancellation for every queued/running run of a package.

    Used by manual stop actions and by force re-runs so a new run never
    waits behind a stale one in the serial worker queue.
    """
    runs = session.scalars(
        select(m.AnalysisRun).where(
            m.AnalysisRun.package_id == package_id,
            m.AnalysisRun.status.in_(("排队中", "运行中")),
        )
    ).all()
    cancelled = 0
    for run in runs:
        if request_analysis_run_cancellation(session, run):
            cancelled += 1
    return cancelled


def finalize_analysis_run_cancellation(
    session: Session, run: m.AnalysisRun
) -> m.AnalysisRun | None:
    """Discard current-stage writes and preserve a requested cancellation."""

    run_id = run.id
    session.rollback()
    current = session.get(m.AnalysisRun, run_id)
    if current is None or (
        current.status != "已取消"
        and current.cancel_requested_at is None
    ):
        return None
    _cleanup_cancelled_analysis_run_artifacts(session, current)
    _mark_analysis_run_cancelled(session, current)
    session.commit()
    session.refresh(current)
    return current


def _human_size(size_bytes: int) -> str:
    if size_bytes % (1024 * 1024 * 1024) == 0:
        return f"{size_bytes // (1024 * 1024 * 1024)}GB"
    if size_bytes % (1024 * 1024) == 0:
        return f"{size_bytes // (1024 * 1024)}MB"
    if size_bytes % 1024 == 0:
        return f"{size_bytes // 1024}KB"
    return f"{size_bytes}字节"


_DIRECT_TEXT_EXTENSIONS = {".txt", ".md", ".srt", ".vtt"}


def _llm_num_ctx() -> int:
    try:
        num_ctx = int(os.environ.get("INTENT_BAO_LLM_NUM_CTX") or 32768)
    except (TypeError, ValueError):
        num_ctx = 32768
    return max(8192, min(131_072, num_ctx))


def _estimate_material_tokens(items: list[dict[str, Any]]) -> float:
    """Cheap token estimate: CJK ~1 token/char, ASCII ~0.35, JSON/item overhead."""
    tokens = 0.0
    for item in items:
        content = str(item.get("content") or "")
        ascii_chars = sum(1 for char in content if char.isascii())
        tokens += ascii_chars * 0.35 + (len(content) - ascii_chars) * 1.0
    tokens += len(items) * 45.0
    return tokens


def _split_model_materials_for_analysis(
    model_materials: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Fallback for custom adapters; production adapters budget their exact prompt."""
    token_budget = max(1, int(_llm_num_ctx() * 0.85) - 5_000)
    return split_items_for_request(
        model_materials,
        fits=lambda items: estimate_tokens(json.dumps(items, ensure_ascii=False)) <= token_budget,
    )

_MODEL_TEXT_READ_LIMIT_BYTES = 512 * 1024

# Upload limits reflect the first-stage usage profile: a screen recording may
# be large, while audio, slide decks and text documents should be bounded much
# more tightly.  Package-wide quotas prevent many individually valid uploads
# from exhausting the local workstation.
MATERIAL_FILE_LIMITS_BYTES = {
    "录屏": 2 * 1024 * 1024 * 1024,
    "录音": 1024 * 1024 * 1024,
    "会议资料": 100 * 1024 * 1024,
    "转写稿": 50 * 1024 * 1024,
    "人工纪要": 50 * 1024 * 1024,
}
MAX_FILES_PER_PACKAGE = 40
MAX_BYTES_PER_PACKAGE = 4 * 1024 * 1024 * 1024
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024

# DOCX is a ZIP container.  These limits are checked before any member is
# decompressed, then the members we consume are also read through bounded
# streams so a forged central directory cannot make the parser allocate an
# unbounded amount of memory.
MAX_DOCX_ARCHIVE_MEMBERS = 1_000
MAX_DOCX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_DOCX_DOCUMENT_XML_BYTES = 20 * 1024 * 1024
MAX_DOCX_CONTENT_TYPES_BYTES = 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200
MIN_DOCX_RATIO_CHECK_BYTES = 1024 * 1024
_ADOPTION_STAGES = {
    "未明确／未进入",
    "初步了解",
    "内部评估",
    "样品／技术验证",
    "产品项目规划",
    "确认导入",
    "暂停／放弃",
}


def _decode_customer_text(
    raw: bytes, *, allow_truncated_tail: bool = False
) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            if not allow_truncated_tail:
                return raw.decode(encoding)
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            # final=False deliberately permits an incomplete multibyte code
            # point at the byte-limit boundary to remain buffered and omitted.
            return decoder.decode(raw, final=False)
        except UnicodeDecodeError:
            continue
    raise DomainError(
        422,
        "文本材料编码无法识别；请另存为 UTF-8 编码后重新上传。",
    )


def _read_zip_member_limited(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    if member.file_size > max_bytes:
        raise DomainError(
            422,
            f"DOCX安全检查失败：{label}展开后不得超过"
            f"{_human_size(max_bytes)}",
        )
    with archive.open(member, "r") as source:
        content = source.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise DomainError(
            422,
            f"DOCX安全检查失败：{label}实际展开大小超过"
            f"{_human_size(max_bytes)}",
        )
    return content


def _validate_docx_archive(
    archive: zipfile.ZipFile,
) -> tuple[zipfile.ZipInfo, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_DOCX_ARCHIVE_MEMBERS:
        raise DomainError(
            422,
            "DOCX安全检查失败：压缩包成员数量过多"
            f"（最多{MAX_DOCX_ARCHIVE_MEMBERS}个）",
        )

    total_uncompressed = 0
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        normalized_name = info.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if path.is_absolute() or ".." in path.parts:
            raise DomainError(
                422, "DOCX安全检查失败：压缩包包含非法成员路径"
            )
        if info.flag_bits & 0x1:
            raise DomainError(422, "DOCX安全检查失败：不支持加密成员")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise DomainError(
                422, "DOCX安全检查失败：包含不支持的压缩算法"
            )
        if info.is_dir():
            continue
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DomainError(
                422,
                "DOCX安全检查失败：压缩包总展开大小不得超过"
                f"{_human_size(MAX_DOCX_UNCOMPRESSED_BYTES)}",
            )
        if info.file_size >= MIN_DOCX_RATIO_CHECK_BYTES:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_DOCX_COMPRESSION_RATIO:
                raise DomainError(
                    422,
                    "DOCX安全检查失败：检测到异常压缩比，文件可能是压缩炸弹",
                )
        by_name[normalized_name] = info

    content_types = by_name.get("[Content_Types].xml")
    document_xml = by_name.get("word/document.xml")
    if content_types is None or document_xml is None:
        raise DomainError(
            422,
            "DOCX安全检查失败：缺少[Content_Types].xml或word/document.xml",
        )
    if document_xml.file_size > MAX_DOCX_DOCUMENT_XML_BYTES:
        raise DomainError(
            422,
            "DOCX安全检查失败：word/document.xml展开后不得超过"
            f"{_human_size(MAX_DOCX_DOCUMENT_XML_BYTES)}",
        )
    return content_types, document_xml


def _read_model_text(
    path: Path, *, max_text_bytes: int = _MODEL_TEXT_READ_LIMIT_BYTES
) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _DIRECT_TEXT_EXTENSIONS:
        with path.open("rb") as source:
            raw = source.read(max_text_bytes)
        return _decode_customer_text(raw, allow_truncated_tail=True)
    if suffix != ".docx":
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            _content_types, document_info = _validate_docx_archive(archive)
            document_xml = _read_zip_member_limited(
                archive,
                document_info,
                max_bytes=MAX_DOCX_DOCUMENT_XML_BYTES,
                label="word/document.xml",
            )
        root = ElementTree.fromstring(document_xml)
    except DomainError:
        raise
    except (
        KeyError,
        OSError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ):
        raise DomainError(422, f"无法读取Word正文：{path.name}") from None
    text_nodes = [
        node.text or ""
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "t"
    ]
    return "\n".join(value for value in text_nodes if value.strip())


def _build_model_materials(
    settings: Settings,
    package: m.MeetingPackage,
    files: list[m.MaterialFile],
) -> tuple[list[dict[str, Any]], set[str]]:
    materials: list[dict[str, Any]] = []
    readable_ids: set[str] = set()
    for item in files:
        path = (settings.data_dir / item.relative_path).resolve()
        if settings.data_dir.resolve() not in path.parents or not path.is_file():
            raise DomainError(404, f"材料文件不存在：{item.safe_name}")
        content = _read_model_text(path, max_text_bytes=max(_MODEL_TEXT_READ_LIMIT_BYTES, path.stat().st_size))
        summary: dict[str, Any] = {
            "id": item.id,
            "type": item.material_type,
            "name": item.safe_name,
            "source_material_file_id": item.id,
            "source_material_type": item.material_type,
            "source_material_name": item.safe_name,
        }
        if content is not None and content.strip():
            summary["content"] = content.strip()
            readable_ids.add(item.id)
        materials.append(summary)

    if package.notes and package.notes.strip():
        note = package.notes.strip()
        materials.append(
            {
                "id": None,
                "type": "资料包备注",
                "name": "资料包备注",
                "content": note,
            }
        )

    if not any(item.get("content") for item in materials):
        has_media = any(
            item.material_type in {"录音", "录屏"} for item in files
        )
        if not has_media:
            raise DomainError(
                422,
                "当前Ollama意向抽取仅能直接读取TXT、MD、SRT、VTT、DOCX和资料包备注；"
                "录音、录屏与PDF仍需后续ASR/OCR组件处理。",
            )
    return materials, readable_ids


def _master_data_for_model(session: Session) -> dict[str, list[dict[str, Any]]]:
    aliases = session.scalars(
        select(m.Alias).where(m.Alias.active.is_(True))
    ).all()
    alias_map: dict[tuple[str, str], list[str]] = {}
    for item in aliases:
        alias_map.setdefault((item.entity_type, item.entity_id), []).append(
            item.alias
        )

    def entity_rows(
        entity_type: str, rows: list[Any]
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "name": item.name,
                "aliases": alias_map.get((entity_type, item.id), []),
            }
            for item in rows
        ]

    return {
        "technologies": entity_rows(
            "technology",
            list(
                session.scalars(
                    select(m.Technology)
                    .where(m.Technology.active.is_(True))
                    .order_by(m.Technology.name)
                ).all()
            ),
        ),
        "platforms": entity_rows(
            "platform",
            list(
                session.scalars(
                    select(m.GenericPlatform)
                    .where(m.GenericPlatform.active.is_(True))
                    .order_by(m.GenericPlatform.name)
                ).all()
            ),
        ),
        "functions": entity_rows(
            "function",
            list(
                session.scalars(
                    select(m.CustomerFunction)
                    .where(m.CustomerFunction.active.is_(True))
                    .order_by(m.CustomerFunction.sort_order)
                ).all()
            ),
        ),
    }


def _locate_exact_quote(
    materials: list[dict[str, Any]], quote: str
) -> tuple[dict[str, Any], int] | None:
    candidates: list[tuple[int, dict[str, Any], int]] = []
    for material in materials:
        content = material.get("content")
        if not isinstance(content, str):
            continue
        offset = content.find(quote)
        if offset >= 0:
            # Prefer the most precise derived source over a direct text copy.
            # SRT/VTT content, for example, may occur both in the uploaded file
            # and in a timed TranscriptSegment; the latter can be replayed.
            priority = 0
            if material.get("start_ms") is not None:
                priority = 3
            elif material.get("page_no") is not None:
                priority = 2
            elif material.get("frame_no") is not None:
                priority = 1
            candidates.append(
                (priority, material, content.count("\n", 0, offset) + 1 + int(material.get("_line_offset", 0)))
            )
    if not candidates:
        return None
    _, material, line_no = max(candidates, key=lambda item: item[0])
    return material, line_no


def _optional_non_negative_int(value: Any) -> int | None:
    """Return a usable evidence coordinate without accepting booleans."""

    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _evidence_source(
    material: dict[str, Any],
    *,
    valid_file_ids: set[str],
    line_no: int,
) -> dict[str, Any]:
    """Resolve a derived quote back to its uploaded file and exact locator."""

    source_file_id = material.get("source_material_file_id")
    if source_file_id not in valid_file_ids:
        direct_id = material.get("id")
        source_file_id = direct_id if direct_id in valid_file_ids else None

    source_name = str(
        material.get("source_material_name")
        or material.get("name")
        or "会议材料"
    )
    source_type = str(
        material.get("source_material_type")
        or material.get("type")
        or "会议材料"
    )
    start_ms = _optional_non_negative_int(material.get("start_ms"))
    end_ms = _optional_non_negative_int(material.get("end_ms"))
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        start_ms = None
        end_ms = None
    page_no = _optional_non_negative_int(material.get("page_no"))
    if page_no == 0:
        page_no = None
    frame_no = _optional_non_negative_int(material.get("frame_no"))

    if start_ms is not None and end_ms is not None:
        evidence_type = "转写片段"
        locator = (
            f"{source_name} · {_fmt_ms(start_ms)}–{_fmt_ms(end_ms)}"
        )
    elif page_no is not None:
        evidence_type = "PDF页面"
        locator = f"{source_name} · 第{page_no}页"
    elif frame_no is not None:
        evidence_type = "录屏关键帧"
        locator = f"{source_name} · 关键帧{frame_no}"
    else:
        evidence_type = source_type
        locator = f"{source_name} · 第{max(1, line_no)}行"

    return {
        "material_file_id": source_file_id,
        "evidence_type": evidence_type,
        "locator": locator,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "page_no": page_no,
        "frame_no": frame_no,
        "playback_available": bool(
            start_ms is not None
            and end_ms is not None
            and source_type in {"录音", "录屏"}
        ),
    }


def _finish_analysis_tasks(
    run: m.AnalysisRun,
    tasks: list[m.ProcessingTask],
    *,
    adapter_name: str,
) -> None:
    text_only_skips = {"音轨与转写", "说话人分离", "画面与PDF解析"}
    for task in tasks:
        task.status = (
            "不适用"
            if adapter_name != "mock" and task.stage in text_only_skips
            else "已完成"
        )
        task.progress = 100
        task.attempt_count = max(task.attempt_count, 1)
        task.started_at = task.started_at or m.utcnow()
        task.finished_at = m.utcnow()
        task.output_hash = hashlib.sha256(
            f"{run.id}|{task.stage}|{adapter_name}".encode()
        ).hexdigest()


def _update_task(
    session: Session,
    run: m.AnalysisRun,
    stage: str,
    *,
    status: str,
    progress: int | None = None,
    result_json: dict | None = None,
) -> None:
    task = session.scalar(
        select(m.ProcessingTask).where(
            m.ProcessingTask.analysis_run_id == run.id,
            m.ProcessingTask.stage == stage,
        )
    )
    if task is None:
        return
    task.status = status
    if status in {"已完成", "不适用"}:
        task.progress = 100
    elif progress is not None:
        task.progress = progress
    if result_json is not None:
        task.result_json = json.dumps(result_json, ensure_ascii=False)
    task.attempt_count = max(task.attempt_count, 1)
    task.started_at = task.started_at or m.utcnow()
    if status in {"已完成", "不适用", "处理失败"}:
        task.finished_at = task.finished_at or m.utcnow()
    task.output_hash = hashlib.sha256(
        f"{run.id}|{task.stage}|{status}".encode()
    ).hexdigest()


def _fmt_ms(millis: int) -> str:
    seconds, millis = divmod(millis, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _build_hotwords(session: Session, package: m.MeetingPackage) -> list[str]:
    words: list[str] = []
    for technology in session.scalars(
        select(m.Technology).where(m.Technology.active.is_(True))
    ).all():
        words.append(technology.name)
    for platform in session.scalars(
        select(m.GenericPlatform).where(m.GenericPlatform.active.is_(True))
    ).all():
        words.append(platform.name)
    for attendee in session.scalars(
        select(m.MeetingAttendee).where(
            m.MeetingAttendee.package_id == package.id
        )
    ).all():
        if attendee.display_name:
            words.append(attendee.display_name)
    return words[:50]


def _parse_document_stage(
    session: Session,
    settings: Settings,
    run: m.AnalysisRun,
    package: m.MeetingPackage,
    files: list[m.MaterialFile],
    *,
    parsed_pages: list[m.PdfPageText] | None = None,
) -> tuple[set[str], list[dict], list[m.TranscriptSegment]]:
    """Extract PDF page text and subtitle segments into derived tables."""

    readable: set[str] = set()
    extra_materials: list[dict] = []
    parsed_transcripts: list[m.TranscriptSegment] = []
    for item in files:
        path = (settings.data_dir / item.relative_path).resolve()
        if settings.data_dir.resolve() not in path.parents or not path.is_file():
            raise DomainError(404, f"材料文件不存在：{item.safe_name}")
        suffix = Path(item.safe_name).suffix.lower()
        if suffix == ".pdf":
            try:
                pages = extract_pdf_text(path)
            except (OSError, ValueError, RuntimeError) as exc:
                raise DomainError(
                    422, f"PDF解析失败：{item.safe_name}：{exc}"
                ) from exc
            for page in pages:
                page_row = m.PdfPageText(
                    material_file_id=item.id,
                    page_no=page.page_no,
                    text=page.text,
                    char_count=page.char_count,
                    extracted_by="pymupdf",
                )
                session.add(page_row)
                if parsed_pages is not None:
                    parsed_pages.append(page_row)
                if page.text.strip():
                    readable.add(item.id)
                    extra_materials.append(
                        {
                            "id": None,
                            "type": "PDF页面",
                            "name": f"{item.safe_name} 第{page.page_no}页",
                            "content": page.text,
                            "source_material_file_id": item.id,
                            "source_material_type": item.material_type,
                            "source_material_name": item.safe_name,
                            "page_no": page.page_no,
                        }
                    )
            item.parse_status = "解析完成"
        elif suffix in {".srt", ".vtt"}:
            try:
                segments = parse_subtitle(path)
            except (OSError, ValueError) as exc:
                raise DomainError(
                    422, f"字幕解析失败：{item.safe_name}：{exc}"
                ) from exc
            for index, segment in enumerate(segments, 1):
                transcript = m.TranscriptSegment(
                    analysis_run_id=run.id,
                    package_id=package.id,
                    material_file_id=item.id,
                    order_index=index,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text=segment.text,
                )
                session.add(transcript)
                parsed_transcripts.append(transcript)
                readable.add(item.id)
            item.parse_status = "解析完成"
    session.flush()
    return readable, extra_materials, parsed_transcripts


def _extend_materials_with_derived(
    model_materials: list[dict],
    extra_materials: list[dict],
    transcripts: list[m.TranscriptSegment],
    pages: list[m.PdfPageText],
) -> list[dict]:
    combined = list(model_materials)
    source_by_id = {
        str(item.get("source_material_file_id") or item.get("id")): item
        for item in combined
        if item.get("source_material_file_id") or item.get("id")
    }
    # Keep every derived item so nothing is dropped before the LLM is called.
    # Total size and individual long items are bounded by request splitting.
    parsed_page_keys = {(page.material_file_id, page.page_no) for page in pages}
    for item in extra_materials:
        if item.get("type") == "PDF页面" and (
            item.get("source_material_file_id"), item.get("page_no")
        ) in parsed_page_keys:
            continue
        content = str(item.get("content") or "")
        if not content:
            continue
        clipped = content
        combined.append(
            {
                **item,
                "content": clipped,
                "truncated": len(clipped) < len(content),
            }
        )

    for transcript in transcripts:
        if not transcript.text.strip():
            continue
        content = (
            f"[{_fmt_ms(transcript.start_ms)}-{_fmt_ms(transcript.end_ms)}] "
            f"{transcript.text}"
        )
        combined.append(
            {
                "id": transcript.id,
                "type": "转写片段",
                "name": transcript.speaker_label or "转写片段",
                "content": content,
                "truncated": False,
                "source_material_file_id": transcript.material_file_id,
                "source_material_type": (
                    source_by_id.get(str(transcript.material_file_id), {}).get(
                        "source_material_type"
                    )
                ),
                "source_material_name": (
                    source_by_id.get(str(transcript.material_file_id), {}).get(
                        "source_material_name"
                    )
                ),
                "transcript_segment_id": transcript.id,
                "speaker_label": transcript.speaker_label,
                "start_ms": transcript.start_ms,
                "end_ms": transcript.end_ms,
            }
        )

    for page in pages:
        content = page.text
        if not content:
            continue
        clipped = content
        combined.append(
            {
                "id": page.id,
                "type": "PDF页面",
                "name": (
                    f"{source_by_id.get(str(page.material_file_id), {}).get('source_material_name') or 'PDF'} "
                    f"第{page.page_no}页"
                ),
                "content": clipped,
                "truncated": len(clipped) < len(content),
                "source_material_file_id": page.material_file_id,
                "source_material_type": (
                    source_by_id.get(str(page.material_file_id), {}).get(
                        "source_material_type"
                    )
                ),
                "source_material_name": (
                    source_by_id.get(str(page.material_file_id), {}).get(
                        "source_material_name"
                    )
                ),
                "pdf_page_text_id": page.id,
                "page_no": page.page_no,
            }
        )
    return combined


def _run_alignment_stage(
    session: Session, stage_adapter, run: m.AnalysisRun, package: m.MeetingPackage,
    transcripts: list[m.TranscriptSegment], pages: list[m.PdfPageText],
    frames: list[m.KeyFrame],
) -> None:
    segment_payload = [{"id": row.id, "start_ms": row.start_ms,
                        "end_ms": row.end_ms, "speaker": row.speaker_label,
                        "text": row.text, "material_file_id": row.material_file_id}
                       for row in transcripts if row.text.strip()]
    targets = ([{"id": row.id, "page_no": row.page_no, "text": row.text,
                 "kind": "pages"} for row in pages if row.text.strip()]
               + [{"id": row.id, "time_ms": row.time_ms, "text": row.ocr_text,
                   "kind": "frames"} for row in frames if (row.ocr_text or "").strip()])
    if stage_adapter is None or not segment_payload or not targets:
        _update_task(session, run, "资料对齐", status="不适用",
                     result_json={"alignments": 0, "reason": "无可用非空片段或页面"})
        return

    def payload(segment_batch, target_batch):
        return {"segments": segment_batch,
                "pages": [{k: v for k, v in row.items() if k != "kind"}
                          for row in target_batch if row["kind"] == "pages"],
                "frames": [{k: v for k, v in row.items() if k != "kind"}
                           for row in target_batch if row["kind"] == "frames"]}

    def fits(segment_batch, target_batch):
        request = payload(segment_batch, target_batch)
        if hasattr(stage_adapter, "alignment_request_fits"):
            return stage_adapter.alignment_request_fits(**request)
        return estimate_tokens(json.dumps(request, ensure_ascii=False)) < int(_llm_num_ctx() * 0.75) - 3_000

    persisted = 0
    requests = []
    completed = 0
    try:
        # Keep nearby speech together and reserve at least half the request for
        # candidate pages. Page groups are revisited so long documents are not lost.
        windows: list[list[dict]] = []
        for item in sorted(segment_payload, key=lambda row: (row["start_ms"], row["id"])):
            if not windows or item["start_ms"] - windows[-1][0]["start_ms"] >= 180_000:
                windows.append([])
            windows[-1].append(item)
        for window in windows:
            segment_batches = split_items_for_request(
                window, text_key="text",
                fits=lambda items: fits(items, []) and estimate_tokens(json.dumps(items, ensure_ascii=False)) <= int(_llm_num_ctx() * 0.30),
            )
            for segment_batch in segment_batches:
                target_batches = split_items_for_request(
                    targets, text_key="text", fits=lambda items: fits(segment_batch, items),
                )
                requests.extend(payload(segment_batch, group) for group in target_batches)
        seen = set()
        for index, request in enumerate(requests, 1):
            _raise_if_analysis_cancel_requested(session, run)
            progress_data = {"batch": index, "batches": len(requests),
                             "completed_batches": completed, "alignments": persisted,
                             "started_at": m.utcnow().isoformat(), "last_activity_at": m.utcnow().isoformat()}
            _update_task(session, run, "资料对齐", status="处理中",
                         progress=int(99 * completed / len(requests)), result_json=progress_data)
            session.commit()
            last_write = [0.0]

            def heartbeat(info):
                if time.monotonic() - last_write[0] < 5:
                    return
                last_write[0] = time.monotonic()
                _update_task(session, run, "资料对齐", status="处理中", result_json={
                    **progress_data, **info, "last_activity_at": m.utcnow().isoformat(),
                })
                session.commit()

            if hasattr(stage_adapter, "set_runtime_hooks"):
                stage_adapter.set_runtime_hooks(
                    check_cancelled=lambda: _raise_if_analysis_cancel_requested(session, run),
                    on_progress=heartbeat,
                )
            try:
                alignments = stage_adapter.align_materials(**request)
            finally:
                if hasattr(stage_adapter, "set_runtime_hooks"):
                    stage_adapter.set_runtime_hooks(check_cancelled=None, on_progress=None)
            _raise_if_analysis_cancel_requested(session, run)
            valid_segments = {row["id"] for row in request["segments"]}
            valid_targets = {"pdf_page": {row["id"] for row in request["pages"]},
                             "key_frame": {row["id"] for row in request["frames"]}}
            for alignment in alignments:
                key = (alignment.segment_id, alignment.target_type, alignment.target_id)
                if (key in seen or alignment.segment_id not in valid_segments
                        or alignment.target_id not in valid_targets.get(alignment.target_type, set())):
                    continue
                seen.add(key)
                session.add(m.AlignmentLink(
                    analysis_run_id=run.id, package_id=package.id,
                    segment_id=alignment.segment_id, target_type=alignment.target_type,
                    target_id=alignment.target_id, confidence=alignment.confidence,
                    reason=alignment.reason,
                ))
                persisted += 1
            completed += 1
        _update_task(session, run, "资料对齐", status="已完成",
                     result_json={"alignments": persisted, "batches": len(requests),
                                  "completed_batches": completed})
    except _AnalysisCancellationRequested:
        raise
    except Exception as exc:
        _raise_if_analysis_cancel_requested(session, run)
        reason = f"资料对齐失败（已完成 {completed}/{len(requests)} 批）：{exc}"
        _update_task(session, run, "资料对齐", status="处理失败",
                     result_json={"alignments": persisted, "batches": len(requests),
                                  "completed_batches": completed, "error": reason})
        task = session.scalar(select(m.ProcessingTask).where(
            m.ProcessingTask.analysis_run_id == run.id, m.ProcessingTask.stage == "资料对齐"))
        if task:
            task.error_message = reason
        session.commit()
        raise DomainError(422, reason) from exc


def _run_identity_stage(
    session: Session,
    stage_adapter,
    run: m.AnalysisRun,
    package: m.MeetingPackage,
    master_data: dict,
    speakers: list[m.SpeakerCluster],
) -> None:
    if stage_adapter is None or not speakers:
        _update_task(
            session,
            run,
            "身份辅助匹配",
            status="不适用",
            result_json={"recommendations": 0},
        )
        return
    speaker_ids = [item.id for item in speakers]
    observations = session.scalars(
        select(m.RawIntentObservation).where(
            m.RawIntentObservation.analysis_run_id == run.id,
            m.RawIntentObservation.speaker_id.in_(speaker_ids),
        )
    ).all()
    segments = []
    for speaker in speakers:
        quotes = [
            item.raw_quote for item in observations if item.speaker_id == speaker.id
        ]
        segments.append(
            {
                "id": speaker.id,
                "speaker_label": speaker.label,
                "text": "；".join(quotes[:3]) or speaker.recommended_name or speaker.label,
            }
        )
    attendees = [
        {
            "display_name": item.display_name,
            "role": item.role,
            "organization": item.organization,
            "function_id": item.function_id,
        }
        for item in session.scalars(
            select(m.MeetingAttendee).where(
                m.MeetingAttendee.package_id == package.id
            )
        ).all()
    ]
    try:
        def fits(items):
            if hasattr(stage_adapter, "identity_request_fits"):
                return stage_adapter.identity_request_fits(
                    segments=items, attendees=attendees, master_data=master_data)
            return estimate_tokens(json.dumps({"segments": items, "attendees": attendees,
                                               "master_data": master_data}, ensure_ascii=False)) < int(_llm_num_ctx() * .8) - 3_000

        batches = split_items_for_request(segments, text_key="text", fits=fits)
        recommendations = []
        for index, batch in enumerate(batches, 1):
            _raise_if_analysis_cancel_requested(session, run)
            state = {"batch": index, "batches": len(batches), "completed_batches": index - 1,
                     "started_at": m.utcnow().isoformat()}
            _update_task(session, run, "身份辅助匹配", status="处理中",
                         progress=int(99 * (index - 1) / len(batches)), result_json=state)
            session.commit()
            last_write = [0.0]

            def heartbeat(info):
                if time.monotonic() - last_write[0] < 5:
                    return
                last_write[0] = time.monotonic()
                _update_task(session, run, "身份辅助匹配", status="处理中", result_json={
                    **state, **info, "last_activity_at": m.utcnow().isoformat(),
                })
                session.commit()

            if hasattr(stage_adapter, "set_runtime_hooks"):
                stage_adapter.set_runtime_hooks(
                    check_cancelled=lambda: _raise_if_analysis_cancel_requested(session, run),
                    on_progress=heartbeat)
            try:
                recommendations.extend(stage_adapter.recommend_identities(
                    segments=batch, attendees=attendees, master_data=master_data))
            finally:
                if hasattr(stage_adapter, "set_runtime_hooks"):
                    stage_adapter.set_runtime_hooks(check_cancelled=None, on_progress=None)
    except _AnalysisCancellationRequested:
        raise
    except Exception as exc:
        _raise_if_analysis_cancel_requested(session, run)
        reason = f"身份推荐调用失败：{exc}"
        _update_task(session, run, "身份辅助匹配", status="处理失败",
                     result_json={"recommendations": 0, "error": reason})
        task = session.scalar(select(m.ProcessingTask).where(
            m.ProcessingTask.analysis_run_id == run.id, m.ProcessingTask.stage == "身份辅助匹配"))
        if task:
            task.error_message = reason
        session.commit()
        raise DomainError(422, reason) from exc
    _raise_if_analysis_cancel_requested(session, run)
    applied = 0
    for recommendation in recommendations:
        speaker = next(
            (item for item in speakers if item.label == recommendation.speaker_label),
            None,
        )
        if speaker is None:
            continue
        if recommendation.display_name:
            speaker.recommended_name = recommendation.display_name
        if recommendation.role:
            speaker.recommended_role = recommendation.role
        if recommendation.organization:
            speaker.recommended_organization = recommendation.organization
        if recommendation.function_id and session.get(
            m.CustomerFunction, recommendation.function_id
        ):
            speaker.recommended_function_id = recommendation.function_id
        speaker.confidence = recommendation.confidence
        applied += 1
    _update_task(
        session,
        run,
        "身份辅助匹配",
        status="已完成",
        result_json={"recommendations": applied},
    )


class _AudioTranscriptionFailure(DomainError):
    def __init__(
        self, failures: list[dict[str, Any]], stats: dict[str, int] | None = None,
        skipped_segments: list[dict[str, Any]] | None = None,
    ):
        super().__init__(422, f"{len(failures)} 个语音片段在重试后仍无法完整转写")
        self.failures = failures
        self.stats = stats or {}
        self.skipped_segments = skipped_segments or []


def _audio_result_summary(file_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "files": file_results,
        "skipped_segments": [segment for item in file_results
                             for segment in item.get("skipped_segments", [])],
        "transcription": {
            key: sum(item.get("transcription", {}).get(key, 0) for item in file_results)
            for key in ("jobs", "ok", "failed", "empty", "reused", "skipped")
        },
    }


def _run_audio_stage(
    session: Session,
    settings: Settings,
    run: m.AnalysisRun,
    package: m.MeetingPackage,
    files: list[m.MaterialFile],
    asr_settings,
) -> tuple[bool, list[m.TranscriptSegment]]:
    targets = [
        item for item in files if item.material_type in {"录音", "录屏"}
    ]
    if not targets or asr_settings is None or not asr_settings.enabled:
        _update_task(session, run, "音轨与转写", status="不适用")
        _update_task(session, run, "说话人分离", status="不适用")
        return False, []
    adapter = get_asr_adapter(
        asr_settings.provider,
        base_url=asr_settings.base_url,
        model_name=asr_settings.model_name,
    )
    transcripts: list[m.TranscriptSegment] = []
    file_results: list[dict[str, Any]] = []
    _update_task(session, run, "音轨与转写", status="处理中", progress=0)
    _update_task(session, run, "说话人分离", status="处理中", progress=0)
    session.commit()
    for file_index, target in enumerate(targets):
        _raise_if_analysis_cancel_requested(session, run)
        try:
            file_transcripts, summary = _run_audio_file(
                session, settings, run, package, target, adapter,
                file_index=file_index, file_count=len(targets),
                order_offset=len(transcripts),
                previous_file_results=file_results,
                cache_config={"provider": asr_settings.provider,
                              "base_url": asr_settings.base_url,
                              "model": asr_settings.model_name},
            )
        except _AnalysisCancellationRequested:
            raise
        except Exception as exc:
            _raise_if_analysis_cancel_requested(session, run)
            failures = (
                exc.failures if isinstance(exc, _AudioTranscriptionFailure)
                else [{"material_file_id": target.id, "start_ms": None,
                       "end_ms": None, "error": "音轨提取或转写失败", "attempts": 1}]
            )
            first_failure = failures[0]
            location = (
                f"（{_fmt_ms(first_failure['start_ms'])}–{_fmt_ms(first_failure['end_ms'])}）"
                if first_failure["start_ms"] is not None
                and first_failure["end_ms"] is not None else ""
            )
            reason = (
                f"材料“{target.safe_name}”转写不完整：{len(failures)} 个片段失败"
                f"{location}：{first_failure['error']}，请检查后重试"
            )
            target.parse_status = "解析失败"
            failed_file = {
                "material_file_id": target.id, "material_name": target.safe_name,
                "failed_segments": failures,
                "skipped_segments": getattr(exc, "skipped_segments", []),
                "transcription": getattr(exc, "stats", {}),
            }
            _update_task(
                session, run, "音轨与转写", status="处理失败",
                result_json={**_audio_result_summary(file_results + [failed_file]),
                             "failed_segments": failures, "segments": len(transcripts),
                             "files_done": file_index, "files_total": len(targets)},
            )
            task = session.scalar(select(m.ProcessingTask).where(
                m.ProcessingTask.analysis_run_id == run.id,
                m.ProcessingTask.stage == "音轨与转写",
            ))
            if task is not None:
                task.error_message = reason
            # The outer cancellation check rolls back current-stage changes.
            # Persist the failure coordinates before handing it control.
            session.commit()
            raise DomainError(422, reason) from exc
        transcripts.extend(file_transcripts)
        file_results.append(summary)
        _update_task(
            session, run, "音轨与转写", status="处理中",
            progress=min(99, int(99 * (file_index + 1) / len(targets))),
            result_json={**_audio_result_summary(file_results), "segments": len(transcripts),
                         "files_done": file_index + 1, "files_total": len(targets)},
        )
        session.commit()
    _raise_if_analysis_cancel_requested(session, run)
    _update_task(
        session, run, "音轨与转写", status="已完成", progress=100,
        result_json={**_audio_result_summary(file_results), "segments": len(transcripts),
                     "files_done": len(targets), "files_total": len(targets),
                     "duration_ms": sum(item["duration_ms"] or 0 for item in file_results),
                     "model": adapter.model_name},
    )
    _update_task(
        session, run, "说话人分离", status="已完成", progress=100,
        result_json={"speaker_labels": len({item.speaker_label for item in transcripts
                                             if item.speaker_label})},
    )
    return True, transcripts


def _run_audio_file(
    session: Session,
    settings: Settings,
    run: m.AnalysisRun,
    package: m.MeetingPackage,
    target: m.MaterialFile,
    adapter: AsrAdapter,
    *,
    file_index: int,
    file_count: int,
    order_offset: int,
    cache_config: dict[str, Any] | None = None,
    previous_file_results: list[dict[str, Any]] | None = None,
) -> tuple[list[m.TranscriptSegment], dict[str, Any]]:
    transcripts: list[m.TranscriptSegment] = []
    path = (settings.data_dir / target.relative_path).resolve()
    audio_path = path
    if target.material_type == "录屏":
        wav_path = settings.data_dir / "temp" / f"{run.id}-{target.id}.wav"
        if not extract_audio_track(path, wav_path):
            raise DomainError(
                422, "FFmpeg不可用或音轨提取失败，无法处理录屏"
            )
        audio_path = wav_path
    elif path.suffix.lower() not in {".wav", ".mp3", ".m4a"}:
        wav_path = settings.data_dir / "temp" / f"{run.id}-{target.id}.wav"
        if not extract_audio_track(path, wav_path):
            raise DomainError(
                422, "FFmpeg不可用或音频转换失败，无法处理该录音格式"
            )
        audio_path = wav_path

    duration_ms = probe_media_duration(audio_path)
    if not duration_ms or duration_ms <= 0:
        raise _AudioTranscriptionFailure([{
            "material_file_id": target.id, "start_ms": 0, "end_ms": None,
            "error": "无法读取音频时长，请检查 FFprobe 和媒体文件", "category": "duration",
            "attempts": 1,
        }])
    hotwords = _build_hotwords(session, package)

    diarized_turns: list[Any] = []
    diarization_note: dict[str, Any] = {
        "provider": None,
        "turns": 0,
        "speakers": 0,
    }
    progress_state = {"last": 0}

    def _report_audio_progress(percent: int) -> None:
        percent = min(99, int((file_index * 100 + percent) / file_count))
        # Throttle to ~1% steps; every write commits so polling clients see
        # movement during the multi-minute embedding/transcription loop.
        if percent < progress_state["last"] + 1 and percent < 99:
            return
        progress_state["last"] = percent
        _update_task(
            session, run, "音轨与转写", status="处理中", progress=percent
        )
        session.commit()

    try:
        diarizer = get_diarizer(settings)
        if diarizer is not None:
            diarized_turns = diarizer.diarize(
                audio_path,
                duration_ms=duration_ms,
                should_cancel=lambda: analysis_run_cancel_requested(
                    session, run
                ),
                on_progress=lambda done, total: _report_audio_progress(
                    5 + int(65 * done / total)
                ),
            )
            diarization_note = {
                "provider": "funasr",
                "turns": len(diarized_turns),
                "speakers": len(
                    {turn.speaker_label for turn in diarized_turns}
                ),
            }
    except (DiarizationCancelled, _AnalysisCancellationRequested):
        # Must be caught before the generic handler below, which would
        # silently degrade a cancellation into whole-file transcription.
        raise _AnalysisCancellationRequested
    except Exception as exc:
        logger.warning("说话人分离不可用，退回整段转写：%s", exc)
    _raise_if_analysis_cancel_requested(session, run)

    transcript_model = adapter.model_name
    jobs: list[tuple[int, int, str | None]] = []
    if diarized_turns:
        for turn in diarized_turns:
            jobs.extend((start, end, turn.speaker_label) for start, end in
                        _asr_windows(turn.start_ms, turn.end_ms))
    else:
        jobs = [(start, end, None) for start, end in _asr_windows(0, duration_ms)]
    checkpoint = AsrCheckpoints(
        settings.data_dir / "temp" / "asr-checkpoints" / package.id / target.id,
        path, config={**(cache_config or {}), "hotwords": hotwords, "language": "zh",
                      "window_ms": _ASR_WINDOW_MS, "jobs": jobs}, run_id=run.id,
        check_cancelled=lambda: _raise_if_analysis_cancel_requested(session, run),
    )
    segments: list[AsrSegment] = []
    failed_segments: list[dict[str, Any]] = []
    skipped_segments: list[dict[str, Any]] = []
    stats = {"jobs": len(jobs), "ok": 0, "failed": 0, "empty": 0, "reused": 0, "skipped": 0}
    consecutive_failures = 0
    temp_dir = settings.data_dir / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    mode_label = "说话人分段转写" if diarized_turns else "整段录音自动分窗转写"
    for job_index, job in enumerate(jobs):
        _raise_if_analysis_cancel_requested(session, run)
        start_ms, end_ms, speaker_label = job
        current_file_result = {
            "material_file_id": target.id, "material_name": target.safe_name,
            "transcription": dict(stats), "skipped_segments": list(skipped_segments),
        }
        job_state = {**_audio_result_summary((previous_file_results or []) + [current_file_result]),
                     "mode": mode_label, "transcribed": stats["ok"],
                     "total": len(jobs), "failed": stats["failed"],
                     "empty": stats["empty"], "reused": stats["reused"], "skipped": stats["skipped"],
                     "current_file": target.safe_name, "start_ms": start_ms, "end_ms": end_ms,
                     "files_done": file_index,
                     "files_total": file_count, "current_started_at": m.utcnow().isoformat(),
                     "last_activity_at": m.utcnow().isoformat()}
        _update_task(
            session, run, "音轨与转写", status="处理中",
            progress=min(99, int((file_index * 100 + 70 + 28 * job_index / len(jobs)) / file_count)),
            result_json=job_state,
        )
        session.commit()
        cached = checkpoint.load(job)
        if cached:
            segments.extend(cached)
            stats["ok"] += 1
            stats["reused"] += 1
            consecutive_failures = 0
            continue
        seg_wav = temp_dir / f"{run.id}-{target.id}-asr-{job_index}.wav"
        turn_result = None
        failure_reason, category = "语音服务转写失败", "asr"
        used_context = (start_ms, end_ms)
        attempt = 0
        last_write = [0.0]

        def heartbeat(info):
            if time.monotonic() - last_write[0] < 5:
                return
            last_write[0] = time.monotonic()
            _update_task(session, run, "音轨与转写", status="处理中", result_json={
                **job_state, **info, "attempt": attempt, "last_activity_at": m.utcnow().isoformat(),
            })
            session.commit()

        if hasattr(adapter, "set_runtime_hooks"):
            adapter.set_runtime_hooks(
                check_cancelled=lambda: _raise_if_analysis_cancel_requested(session, run),
                on_progress=heartbeat)
        try:
            for attempt in range(1, 3):
                _raise_if_analysis_cancel_requested(session, run)
                context_start, context_end = start_ms, end_ms
                # Only add quiet context outside this speaker turn. Never include
                # an adjacent speaker's words and attribute them to this turn.
                if attempt == 2 and end_ms - start_ms < 1_500 and category in {"empty_response", "audio_too_short"}:
                    other_turns = [turn for turn in diarized_turns if not (
                        turn.speaker_label == speaker_label
                        and turn.start_ms <= start_ms and turn.end_ms >= end_ms)]
                    left_limit = max((min(start_ms, turn.end_ms) for turn in other_turns
                                      if turn.start_ms < start_ms), default=0)
                    right_limit = min((max(end_ms, turn.start_ms) for turn in other_turns
                                       if turn.end_ms > end_ms), default=duration_ms)
                    context_start = max(left_limit, start_ms - 750)
                    context_end = min(right_limit, end_ms + 750)
                used_context = (context_start, context_end)
                direct_file = not diarized_turns and len(jobs) == 1
                input_path = audio_path if direct_file else seg_wav
                try:
                    if not direct_file and not slice_audio_segment(
                        audio_path, context_start, context_end, seg_wav
                    ):
                        failure_reason, category = "音频切片失败，请检查 FFmpeg 和媒体文件", "slice"
                        continue
                    candidate = adapter.transcribe(
                        input_path, language="zh", hotwords=hotwords,
                        duration_ms=context_end - context_start,
                    )
                    _raise_if_analysis_cancel_requested(session, run)
                    if not candidate.segments or any(not row.text.strip() for row in candidate.segments):
                        failure_reason, category = "语音服务返回空转写", "empty_response"
                        continue
                    turn_result = candidate
                    break
                except _AnalysisCancellationRequested:
                    raise
                except Exception as exc:
                    failure_reason = str(exc) if isinstance(exc, DomainError) or getattr(exc, "category", None) else "音频切片或语音服务转写失败"
                    category = getattr(exc, "category", "asr")
                    if category == "dependency" or (getattr(exc, "retryable", True) is False
                                                   and category not in {"empty_response", "audio_too_short"}):
                        break
            if turn_result is None:
                failure = {"material_file_id": target.id, "material_name": target.safe_name,
                           "start_ms": start_ms,
                           "end_ms": end_ms, "error": failure_reason, "category": category,
                           "attempts": attempt, "context_start_ms": used_context[0],
                           "context_end_ms": used_context[1]}
                if category in {"empty_response", "audio_too_short"}:
                    stats["empty"] += 1
                    stats["skipped"] += 1
                    skipped_segments.append(failure)
                    # Keep the unrecognized interval visible without inventing
                    # speech, identity, or successful reusable ASR evidence.
                    segments.append(AsrSegment(
                        start_ms=start_ms, end_ms=end_ms, text="", speaker_label=None,
                        confidence=0.0, language="zh",
                    ))
                    consecutive_failures = 0
                    continue
                stats["failed"] += 1
                consecutive_failures += 1
                failed_segments.append(failure)
                if category == "dependency" or consecutive_failures >= 3:
                    failure["remaining_jobs"] = len(jobs) - job_index - 1
                    failure["stopped_early"] = True
                    break
                continue
            rows = [AsrSegment(
                start_ms=start_ms, end_ms=end_ms, text=row.text.strip(),
                speaker_label=speaker_label or row.speaker_label,
                confidence=row.confidence, language=row.language,
            ) for row in turn_result.segments]
            checkpoint.save(job, rows)
            segments.extend(rows)
            stats["ok"] += 1
            consecutive_failures = 0
        finally:
            if hasattr(adapter, "set_runtime_hooks"):
                adapter.set_runtime_hooks(check_cancelled=None, on_progress=None)
            try:
                seg_wav.unlink(missing_ok=True)
            except OSError:
                pass
    if failed_segments:
        raise _AudioTranscriptionFailure(failed_segments, stats, skipped_segments)

    existing_clusters = session.scalars(
        select(m.SpeakerCluster).where(
            m.SpeakerCluster.package_id == package.id
        )
    ).all()
    label_to_cluster: dict[str, m.SpeakerCluster] = {
        item.label: item for item in existing_clusters
    }
    existing_segment_keys = {
        (item.speaker_id, item.start_ms, item.end_ms, item.transcript)
        for item in session.scalars(
            select(m.SpeakerSegment).where(
                m.SpeakerSegment.speaker_id.in_(
                    [cluster.id for cluster in existing_clusters]
                )
            )
        ).all()
    } if existing_clusters else set()
    for index, segment in enumerate(segments, 1):
        # Diarizer labels are local to one input file.  Never merge two
        # recordings' SPEAKER_00 identities just because their labels match.
        speaker_label = (
            "Speaker " + hashlib.sha256(
                f"{run.id}|{target.id}|{segment.speaker_label}".encode()
            ).hexdigest()[:20]
            if segment.speaker_label else None
        )
        transcript = m.TranscriptSegment(
            analysis_run_id=run.id,
            package_id=package.id,
            material_file_id=target.id,
            order_index=order_offset + index,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text=segment.text,
            speaker_label=speaker_label,
            language=segment.language,
            confidence=segment.confidence,
        )
        session.add(transcript)
        transcripts.append(transcript)
        if speaker_label and segment.text.strip():
            cluster = label_to_cluster.get(speaker_label)
            if cluster is None:
                cluster = m.SpeakerCluster(
                    package_id=package.id,
                    label=speaker_label,
                    recommended_name=f"待确认人员{len(label_to_cluster) + 1}",
                    confidence=0.5,
                    status="待确认",
                )
                session.add(cluster)
                # Assign the string PK before referencing it from segments.
                session.flush()
                label_to_cluster[speaker_label] = cluster
            segment_key = (
                cluster.id,
                segment.start_ms,
                segment.end_ms,
                segment.text,
            )
            if segment_key not in existing_segment_keys:
                session.add(m.SpeakerSegment(
                    speaker_id=cluster.id,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    transcript=segment.text,
                    confidence=segment.confidence,
                ))
                existing_segment_keys.add(segment_key)
    session.flush()
    target.parse_status = "解析完成"
    return transcripts, {
        "material_file_id": target.id,
        "material_name": target.safe_name,
        "segments": len(transcripts),
        "duration_ms": duration_ms,
        "model": transcript_model,
        "transcription": stats,
        "skipped_segments": skipped_segments,
        **({"diarization": diarization_note} if diarized_turns else {}),
    }


def _run_visual_stage(
    session: Session,
    settings: Settings,
    run: m.AnalysisRun,
    package: m.MeetingPackage,
    files: list[m.MaterialFile],
) -> list[m.KeyFrame]:
    screen = [item for item in files if item.material_type == "录屏"]
    if not screen:
        _update_task(session, run, "画面与PDF解析", status="不适用")
        return []
    frames: list[m.KeyFrame] = []
    _update_task(session, run, "画面与PDF解析", status="处理中", progress=0)
    for file_index, item in enumerate(screen):
        _raise_if_analysis_cancel_requested(session, run)
        path = (settings.data_dir / item.relative_path).resolve()
        target_dir = (
            settings.data_dir
            / "projects"
            / package.project_id
            / package.id
            / "derived"
            / "frames"
            / run.id
            / item.id
        )
        extracted = extract_key_frames(path, target_dir, max_frames=20)
        _raise_if_analysis_cancel_requested(session, run)
        if not extracted:
            item.parse_status = "解析失败"
            reason = f"录屏“{item.safe_name}”未提取到画面，请检查媒体工具或文件后重试"
            _update_task(
                session, run, "画面与PDF解析", status="处理失败",
                result_json={"failed_material_file_id": item.id},
            )
            task = session.scalar(select(m.ProcessingTask).where(
                m.ProcessingTask.analysis_run_id == run.id,
                m.ProcessingTask.stage == "画面与PDF解析",
            ))
            if task is not None:
                task.error_message = reason
            session.commit()
            raise DomainError(422, reason)
        for descriptor in extracted:
            relative = descriptor["path"].relative_to(settings.data_dir).as_posix()
            ocr = ocr_image(descriptor["path"])
            frame = m.KeyFrame(
                material_file_id=item.id,
                time_ms=descriptor.get("time_ms"),
                image_relative_path=relative,
                image_sha256=descriptor.get("sha256"),
                ocr_text=ocr,
            )
            session.add(frame)
            frames.append(frame)
        item.parse_status = "解析完成"
        _update_task(
            session, run, "画面与PDF解析", status="处理中",
            progress=min(99, int(99 * (file_index + 1) / len(screen))),
        )
    session.flush()
    _update_task(
        session,
        run,
        "画面与PDF解析",
        status="已完成",
        progress=100,
        result_json={"key_frames": len(frames), "material_files": len(screen)},
    )
    return frames


def _persist_mock_observations(
    session: Session,
    package: m.MeetingPackage,
    run: m.AnalysisRun,
    files: list[m.MaterialFile],
    adapter: IntentModelAdapter,
) -> tuple[int, int, list[str]]:
    function_specs = [
        (
            "function-product",
            "产品规划负责人",
            4,
            "产品项目规划",
            "认可方向并建议进入路线图候选",
            None,
            [],
        ),
        (
            "function-development",
            "开发负责人",
            3,
            "样品／技术验证",
            None,
            "需要先完成样机验证",
            ["性能", "验证资源"],
        ),
        (
            "function-purchase",
            "采购负责人",
            3,
            "内部评估",
            None,
            "成本条件仍需评估",
            ["成本"],
        ),
    ]
    technology = session.scalar(
        select(m.Technology).where(
            m.Technology.id == "tech-mini-led-backlight"
        )
    ) or session.scalar(select(m.Technology).order_by(m.Technology.name))
    platform = session.scalar(
        select(m.GenericPlatform).where(
            m.GenericPlatform.id
            == "platform-tech-mini-led-backlight-27QHD-240Hz"
        )
    ) or session.scalar(select(m.GenericPlatform))
    if technology is None:
        raise DomainError(500, "基础字典中没有可用技术")

    for index, (
        function_id,
        role,
        attitude,
        stage,
        support,
        resistance,
        bottlenecks,
    ) in enumerate(function_specs, 1):
        speaker = m.SpeakerCluster(
            package_id=package.id,
            label=f"Speaker {chr(64 + index)}",
            recommended_name=f"待确认人员{index}",
            recommended_role=role,
            recommended_organization="客户组织（模拟）",
            recommended_function_id=function_id,
            confidence=round(0.82 - index * 0.04, 2),
            status="待确认",
        )
        session.add(speaker)
        session.flush()
        quote = support or resistance or "该职能尚未形成可确认的明确态度。"
        evidence = m.EvidenceFragment(
            package_id=package.id,
            material_file_id=files[0].id if files else None,
            evidence_type="模拟会议纪要",
            locator=f"模拟片段 {index}",
            excerpt=quote,
            confidence=round(0.90 - index * 0.03, 2),
            playback_available=False,
        )
        session.add(evidence)
        session.flush()
        influence = (
            "推动"
            if function_id == "function-product"
            else "把关" if function_id == "function-purchase" else "评估"
        )
        confidence = round(0.88 - index * 0.03, 2)
        session.add(
            m.RawIntentObservation(
                package_id=package.id,
                analysis_run_id=run.id,
                customer_id=package.customer_id,
                technology_id=technology.id,
                generic_platform_id=platform.id if platform else None,
                function_id=function_id,
                speaker_id=speaker.id,
                evidence_id=evidence.id,
                raw_quote=quote,
                suggested_attitude=attitude,
                suggested_adoption_stage=stage,
                support_reason=support,
                resistance_reason=resistance,
                bottlenecks=bottlenecks,
                decision_influence=influence,
                confidence=confidence,
                status="待确认",
                model_name=adapter.model_name,
                ai_snapshot={
                    "quote": quote,
                    "technology_id": technology.id,
                    "function_id": function_id,
                    "attitude_level": attitude,
                    "adoption_stage": stage,
                    "support_reason": support,
                    "resistance_reason": resistance,
                    "bottlenecks": list(bottlenecks),
                    "decision_influence": influence,
                    "confidence": confidence,
                    "analysis_run_id": run.id,
                    "model_name": adapter.model_name,
                    "prompt_version": "intent-prompt-v1",
                    "rule_version": "business-rule-v1",
                },
            )
        )
        session.add(
            m.SpeakerSegment(
                speaker_id=speaker.id,
                start_ms=index * 120_000,
                end_ms=index * 120_000 + 24_000,
                transcript=quote,
                confidence=round(0.84 - index * 0.03, 2),
            )
        )
    return len(function_specs), len(function_specs), []


def _persist_model_observations(
    session: Session,
    package: m.MeetingPackage,
    run: m.AnalysisRun,
    materials: list[dict[str, Any]],
    adapter_result: dict[str, Any],
    adapter: IntentModelAdapter,
    material_file_ids: set[str] | None = None,
) -> tuple[int, int, list[str]]:
    observations = adapter_result.get("observations")
    if not isinstance(observations, list):
        raise DomainError(502, "模型响应缺少结构化 observations 数组")
    valid_file_ids = material_file_ids or set()

    technology_ids = set(
        session.scalars(
            select(m.Technology.id).where(m.Technology.active.is_(True))
        ).all()
    )
    platform_ids = set(
        session.scalars(
            select(m.GenericPlatform.id).where(
                m.GenericPlatform.active.is_(True)
            )
        ).all()
    )
    function_ids = set(
        session.scalars(
            select(m.CustomerFunction.id).where(
                m.CustomerFunction.active.is_(True)
            )
        ).all()
    )

    existing_speakers = list(
        session.scalars(
            select(m.SpeakerCluster).where(
                m.SpeakerCluster.package_id == package.id
            )
        ).all()
    )
    speakers_by_label = {item.label: item for item in existing_speakers}
    created = 0
    observation_speaker_ids: set[str] = set()
    unresolved: list[str] = []
    for index, raw in enumerate(observations, 1):
        if not isinstance(raw, dict):
            unresolved.append(f"第{index}条输出不是对象")
            continue
        technology_id = raw.get("technology_id")
        if technology_id not in technology_ids:
            unresolved.append(f"第{index}条技术无法映射到基础字典")
            continue
        quote = str(raw.get("quote") or "").strip()
        located = _locate_exact_quote(raw.get("_source_scope", materials), quote) if quote else None
        if located is None:
            unresolved.append(f"第{index}条缺少可在原材料中逐字核对的证据")
            continue

        platform_id = raw.get("generic_platform_id")
        if platform_id not in platform_ids:
            platform_id = None
        function_id = raw.get("function_id")
        if function_id not in function_ids:
            function_id = None
        attitude = raw.get("attitude_level")
        if attitude not in {1, 2, 3, 4, 5, None}:
            unresolved.append(f"第{index}条意向档位越界")
            continue
        adoption_stage = raw.get("adoption_stage")
        if adoption_stage not in _ADOPTION_STAGES:
            adoption_stage = None
        try:
            confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        bottlenecks = [
            str(item).strip()
            for item in (raw.get("bottlenecks") or [])
            if str(item).strip()
        ][:20]
        material, line_no = located
        source = _evidence_source(
            material,
            valid_file_ids=valid_file_ids,
            line_no=line_no,
        )
        # A model may quote package notes, but those notes are not an uploaded
        # source that can be opened later.  Keep the item unresolved instead
        # of manufacturing a formally confirmable evidence chain.
        if source["material_file_id"] is None:
            unresolved.append(f"第{index}条证据无法关联原始材料文件")
            continue

        speaker_label = str(
            material.get("speaker_label") or f"Speaker {index}"
        ).strip()
        speaker = speakers_by_label.get(speaker_label)
        if speaker is None:
            speaker = m.SpeakerCluster(
                package_id=package.id,
                label=speaker_label,
                recommended_name=(
                    str(raw.get("speaker_name")).strip()
                    if raw.get("speaker_name")
                    else f"待确认人员{len(speakers_by_label) + 1}"
                ),
                recommended_role=(
                    str(raw.get("speaker_role")).strip()
                    if raw.get("speaker_role")
                    else None
                ),
                recommended_organization=(
                    str(raw.get("speaker_organization")).strip()
                    if raw.get("speaker_organization")
                    else "客户组织（待确认）"
                ),
                recommended_function_id=function_id,
                confidence=confidence,
                status="待确认",
            )
            session.add(speaker)
            session.flush()
            speakers_by_label[speaker_label] = speaker
        else:
            # ASR creates the stable cluster first.  Intent extraction may add
            # useful identity hints, but must not replace a human-confirmed
            # identity or erase earlier recommendations.
            if speaker.status != "已确认":
                if not speaker.recommended_name and raw.get("speaker_name"):
                    speaker.recommended_name = str(raw["speaker_name"]).strip()
                if not speaker.recommended_role and raw.get("speaker_role"):
                    speaker.recommended_role = str(raw["speaker_role"]).strip()
                if (
                    not speaker.recommended_organization
                    and raw.get("speaker_organization")
                ):
                    speaker.recommended_organization = str(
                        raw["speaker_organization"]
                    ).strip()
                if (
                    speaker.recommended_function_id is None
                    and function_id is not None
                ):
                    speaker.recommended_function_id = function_id
                if speaker.confidence is None:
                    speaker.confidence = confidence
        observation_speaker_ids.add(speaker.id)
        evidence = m.EvidenceFragment(
            package_id=package.id,
            material_file_id=source["material_file_id"],
            evidence_type=source["evidence_type"],
            locator=source["locator"],
            excerpt=quote,
            start_ms=source["start_ms"],
            end_ms=source["end_ms"],
            page_no=source["page_no"],
            frame_no=source["frame_no"],
            confidence=confidence,
            playback_available=source["playback_available"],
        )
        session.add(evidence)
        session.flush()
        snapshot = {
            "quote": quote,
            "technology_id": technology_id,
            "function_id": function_id,
            "attitude_level": attitude,
            "adoption_stage": adoption_stage,
            "support_reason": raw.get("support_reason"),
            "resistance_reason": raw.get("resistance_reason"),
            "bottlenecks": bottlenecks,
            "decision_influence": raw.get("decision_influence"),
            "confidence": confidence,
            "uncertainty_reason": raw.get("uncertainty_reason"),
            "analysis_run_id": run.id,
            "model_name": adapter.model_name,
            "prompt_version": "intent-prompt-v2-ollama",
            "rule_version": "business-rule-v1",
        }
        session.add(
            m.RawIntentObservation(
                package_id=package.id,
                analysis_run_id=run.id,
                customer_id=package.customer_id,
                technology_id=technology_id,
                generic_platform_id=platform_id,
                function_id=function_id,
                speaker_id=speaker.id,
                evidence_id=evidence.id,
                raw_quote=quote,
                suggested_attitude=attitude,
                suggested_adoption_stage=adoption_stage,
                support_reason=raw.get("support_reason"),
                resistance_reason=raw.get("resistance_reason"),
                bottlenecks=bottlenecks,
                decision_influence=raw.get("decision_influence"),
                confidence=confidence,
                status="待确认",
                uncertainty_reason=raw.get("uncertainty_reason"),
                model_name=adapter.model_name,
                prompt_version="intent-prompt-v2-ollama",
                ai_snapshot=snapshot,
            )
        )
        created += 1
    return len(observation_speaker_ids), created, unresolved


def _snapshot(model: Any, fields: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        value = getattr(model, field)
        if isinstance(value, (datetime,)):
            value = value.isoformat()
        result[field] = value
    return result


def ai_original_snapshot(
    observation: m.RawIntentObservation,
) -> dict[str, Any]:
    """Return the immutable model baseline, with a legacy-row fallback."""

    if not observation.ai_snapshot and observation.version != 1:
        return {
            "unavailable": True,
            "reason": "历史记录已修改，未保留AI原始快照",
            "analysis_run_id": observation.analysis_run_id,
        }
    baseline = {
        "quote": observation.raw_quote,
        "technology_id": observation.technology_id,
        "function_id": observation.function_id,
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
    if observation.ai_snapshot:
        # Only untouched legacy rows can safely supply fields absent in older
        # snapshots. Never infer the model's original fields after review.
        if observation.version == 1:
            return {**baseline, **observation.ai_snapshot}
        return dict(observation.ai_snapshot)
    return baseline


def ensure_ai_snapshot(observation: m.RawIntentObservation) -> dict[str, Any]:
    """Capture the AI baseline once, before any human mutation."""

    if not observation.ai_snapshot or observation.version == 1:
        observation.ai_snapshot = ai_original_snapshot(observation)
    return dict(observation.ai_snapshot)


def observation_review_snapshot(
    observation: m.RawIntentObservation,
) -> dict[str, Any]:
    """Complete audit view of the human-reviewable observation."""

    return {
        "function_id": observation.function_id,
        "suggested_attitude": observation.suggested_attitude,
        "suggested_adoption_stage": observation.suggested_adoption_stage,
        "support_reason": observation.support_reason,
        "resistance_reason": observation.resistance_reason,
        "bottlenecks": list(observation.bottlenecks or []),
        "decision_influence": observation.decision_influence,
        "uncertainty_reason": observation.uncertainty_reason,
        "evidence_id": observation.evidence_id,
        "status": observation.status,
        "version": observation.version,
        "model_metadata": {
            "analysis_run_id": observation.analysis_run_id,
            "model_name": observation.model_name,
            "prompt_version": observation.prompt_version,
            "rule_version": observation.rule_version,
        },
        "ai_original": ai_original_snapshot(observation),
    }


_SUPPORTER_PUSH_PATTERN = re.compile(
    r"(主动|推动|协调|牵头|安排|推荐|发起|组织|导入|路线图|"
    r"愿意.{0,8}(测试|验证|评估|配合)|"
    r"建议.{0,12}(推进|测试|验证|导入|路线图|样机))"
)


def _has_supporter_basis(
    observation: m.RawIntentObservation,
    payload: IntentConfirmRequest,
) -> bool:
    if payload.supporter_evidence_note and payload.supporter_evidence_note.strip():
        return True
    evidence_text = " ".join(
        value
        for value in [observation.raw_quote, observation.support_reason]
        if value
    )
    return bool(_SUPPORTER_PUSH_PATTERN.search(evidence_text))


def _safe_filename(filename: str) -> str:
    basename = Path(filename or "upload.bin").name
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", basename).strip(" .")
    return clean[:180] or "upload.bin"


def infer_material_type(filename: str, explicit: str | None) -> str:
    if explicit:
        if explicit == "会议PDF":
            explicit = "会议资料"
        allowed = {"录音", "录屏", "转写稿", "人工纪要", "会议资料"}
        if explicit not in allowed:
            raise DomainError(422, f"不支持的材料类型：{explicit}")
        return explicit
    extension = Path(filename).suffix.lower()
    if extension in {".mp3", ".wav", ".m4a"}:
        return "录音"
    if extension == ".mp4":
        return "录屏"
    if extension in {".srt", ".vtt"}:
        return "转写稿"
    if extension in {".txt", ".md", ".docx"}:
        return "人工纪要"
    if extension == ".pdf":
        return "会议资料"
    raise DomainError(415, f"首期不支持文件格式：{extension or '无扩展名'}")


def _text_is_readable_and_nonblank(path: Path) -> bool:
    """Check common meeting-text encodings without loading the whole file."""

    with path.open("rb") as source:
        prefix = source.read(4)
    encodings = (
        ("utf-16",)
        if prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
        else ("utf-8-sig", "gb18030")
    )
    for encoding in encodings:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        has_nonblank = False
        try:
            with path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    text = decoder.decode(chunk)
                    if "\x00" in text:
                        raise UnicodeDecodeError(
                            encoding, chunk, 0, 1, "包含NUL字节"
                        )
                    has_nonblank = has_nonblank or bool(text.strip())
                tail = decoder.decode(b"", final=True)
                if "\x00" in tail:
                    raise UnicodeDecodeError(
                        encoding, b"\x00", 0, 1, "包含NUL字节"
                    )
                has_nonblank = has_nonblank or bool(tail.strip())
        except (LookupError, UnicodeDecodeError):
            continue
        if has_nonblank:
            return True
    return False


def _preflight_uploaded_file(path: Path, suffix: str, size: int) -> None:
    """Perform a deliberately shallow, content-based first-layer preflight.

    This validates recognizable containers/text only.  It does not claim to
    determine PDF page count or encryption, media duration, codecs, or full
    document parseability.
    """

    if size == 0:
        raise DomainError(422, "文件内容预检失败：文件不能为空")

    if suffix in {".txt", ".md", ".srt", ".vtt"}:
        if not _text_is_readable_and_nonblank(path):
            raise DomainError(
                422, "文件内容预检失败：文本为空、包含二进制内容或编码不可读取"
            )
        return

    if suffix == ".pdf":
        with path.open("rb") as source:
            header = source.read(1024)
        if not header.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
            raise DomainError(422, "文件内容预检失败：未检测到PDF文件签名")
        return

    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                content_info, _document_info = _validate_docx_archive(archive)
                content_types = _read_zip_member_limited(
                    archive,
                    content_info,
                    max_bytes=MAX_DOCX_CONTENT_TYPES_BYTES,
                    label="[Content_Types].xml",
                )
        except DomainError as exc:
            raise DomainError(
                exc.status_code, f"文件内容预检失败：{exc.message}"
            ) from None
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
            raise DomainError(
                422,
                "文件内容预检失败：DOCX不是可读取的ZIP容器",
            ) from None
        if not content_types.strip():
            raise DomainError(
                422, "文件内容预检失败：DOCX的[Content_Types].xml为空"
            )
        return

    if suffix in {".mp4", ".m4a"}:
        with path.open("rb") as source:
            header = source.read(12)
        if len(header) < 12 or header[4:8] != b"ftyp":
            label = "MP4" if suffix == ".mp4" else "M4A"
            raise DomainError(
                422, f"文件内容预检失败：未检测到{label}容器签名"
            )
        return

    if suffix == ".wav":
        with path.open("rb") as source:
            header = source.read(12)
        if (
            len(header) < 12
            or header[:4] != b"RIFF"
            or header[8:12] != b"WAVE"
        ):
            raise DomainError(422, "文件内容预检失败：未检测到WAV容器签名")
        return

    if suffix == ".mp3":
        with path.open("rb") as source:
            header = source.read(3)
        has_id3 = header == b"ID3"
        has_frame_sync = (
            len(header) >= 2
            and header[0] == 0xFF
            and header[1] & 0xE0 == 0xE0
        )
        if not (has_id3 or has_frame_sync):
            raise DomainError(422, "文件内容预检失败：未检测到MP3文件签名")


def _package_upload_usage(session: Session, package_id: str) -> tuple[int, int]:
    count, total_bytes = session.execute(
        select(
            func.count(m.MaterialFile.id),
            func.coalesce(func.sum(m.MaterialFile.size_bytes), 0),
        ).where(
            m.MaterialFile.package_id == package_id,
            m.MaterialFile.is_deleted.is_(False),
        )
    ).one()
    return int(count or 0), int(total_bytes or 0)


def _disk_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _ensure_upload_disk_space(
    path: Path, *, incoming_bytes: int, phase: str
) -> None:
    required = MIN_FREE_DISK_BYTES + max(0, incoming_bytes)
    available = _disk_free_bytes(path)
    if available < required:
        raise DomainError(
            507,
            f"{phase}磁盘可用空间不足：需至少保留"
            f"{_human_size(MIN_FREE_DISK_BYTES)}安全空间；"
            "上传已停止，请清理磁盘后重试。",
        )


def _enforce_upload_size(
    *,
    material_type: str,
    upload_size: int,
    existing_package_bytes: int,
) -> None:
    file_limit = MATERIAL_FILE_LIMITS_BYTES[material_type]
    if upload_size > file_limit:
        raise DomainError(
            413,
            f"{material_type}单文件不得超过{_human_size(file_limit)}",
        )
    if existing_package_bytes + upload_size > MAX_BYTES_PER_PACKAGE:
        raise DomainError(
            413,
            "单个资料包文件累计容量不得超过"
            f"{_human_size(MAX_BYTES_PER_PACKAGE)}",
        )


async def save_uploaded_file(
    session: Session,
    settings: Settings,
    package: m.MeetingPackage,
    upload: UploadFile,
    material_type: str | None,
) -> tuple[m.MaterialFile, bool]:
    safe_name = _safe_filename(upload.filename or "upload.bin")
    resolved_type = infer_material_type(safe_name, material_type)
    suffix = Path(safe_name).suffix.lower()
    allowed_extensions = {
        "录音": {".mp3", ".wav", ".m4a"},
        "录屏": {".mp4"},
        "转写稿": {".txt", ".md", ".docx", ".srt", ".vtt"},
        "人工纪要": {".txt", ".md", ".docx", ".pdf"},
        "会议资料": {".pdf"},
    }
    if suffix not in allowed_extensions[resolved_type]:
        raise DomainError(415, f"{resolved_type}不支持扩展名{suffix}")

    existing_count, existing_bytes = _package_upload_usage(session, package.id)
    if existing_count >= MAX_FILES_PER_PACKAGE:
        raise DomainError(
            413, f"单个资料包最多允许上传{MAX_FILES_PER_PACKAGE}个文件"
        )

    declared_size = getattr(upload, "size", None)
    if isinstance(declared_size, int) and declared_size >= 0:
        _enforce_upload_size(
            material_type=resolved_type,
            upload_size=declared_size,
            existing_package_bytes=existing_bytes,
        )

    package_dir = (
        settings.projects_dir / package.project_id / package.id / "source"
    ).resolve()
    if settings.projects_dir.resolve() not in package_dir.parents:
        raise DomainError(400, "文件保存路径非法")
    package_dir.mkdir(parents=True, exist_ok=True)

    _ensure_upload_disk_space(
        package_dir,
        incoming_bytes=(declared_size if isinstance(declared_size, int) else 0),
        phase="开始上传前",
    )

    file_id = m.new_id()
    target = package_dir / f"{file_id}__{safe_name}"
    hasher = hashlib.sha256()
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                next_size = size + len(chunk)
                _enforce_upload_size(
                    material_type=resolved_type,
                    upload_size=next_size,
                    existing_package_bytes=existing_bytes,
                )
                _ensure_upload_disk_space(
                    package_dir,
                    incoming_bytes=len(chunk),
                    phase="上传过程中",
                )
                hasher.update(chunk)
                output.write(chunk)
                size = next_size
        _preflight_uploaded_file(target, suffix, size)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    digest = hasher.hexdigest()
    duplicate = (
        session.scalar(
            select(m.MaterialFile.id).where(
                m.MaterialFile.package_id == package.id,
                m.MaterialFile.sha256 == digest,
                m.MaterialFile.is_deleted.is_(False),
            )
        )
        is not None
    )
    relative_path = target.relative_to(settings.data_dir).as_posix()
    item = m.MaterialFile(
        id=file_id,
        package_id=package.id,
        original_name=upload.filename or safe_name,
        safe_name=safe_name,
        media_type=upload.content_type,
        material_type=resolved_type,
        relative_path=relative_path,
        sha256=digest,
        size_bytes=size,
        parse_status="预检通过",
    )
    session.add(item)
    package.status = "待预检"
    package.version += 1
    session.commit()
    session.refresh(item)
    return item, duplicate


def create_analysis_run(
    session: Session,
    package: m.MeetingPackage,
    *,
    adapter_name: str,
    force: bool,
    settings: Settings | None = None,
    adapter: IntentModelAdapter | None = None,
    stage_adapter=None,
    asr_settings=None,
) -> m.AnalysisRun:
    """Validate inputs and persist a run plus its eight stage tasks.

    This runs in the request thread so callers receive a run id and can
    return HTTP 202 immediately; stage execution is delegated to the
    background worker (``execute_analysis_run``).
    """
    if adapter is None:
        try:
            adapter = get_model_adapter(adapter_name)
        except ValueError as exc:
            raise DomainError(422, str(exc)) from exc
    if package.data_scope != "模拟数据" and adapter.name == "mock":
        raise DomainError(
            422, "mock适配器仅用于模拟数据；授权脱敏或真实敏感材料不得伪造分析结果。"
        )
    if not force:
        existing = session.scalar(
            select(m.AnalysisRun)
            .where(
                m.AnalysisRun.package_id == package.id,
                m.AnalysisRun.adapter == adapter.name,
                m.AnalysisRun.model_name == adapter.model_name,
                m.AnalysisRun.status.in_(("已完成", "运行中")),
            )
            .order_by(m.AnalysisRun.created_at.desc())
        )
        if existing:
            return existing

    files = session.scalars(
        select(m.MaterialFile).where(
            m.MaterialFile.package_id == package.id,
            m.MaterialFile.is_deleted.is_(False),
        )
    ).all()
    if not files:
        package.material_completeness = "资料不完整"
        package.status = "待补资料／待标注"
        package.version += 1
        session.commit()
        raise DomainError(422, "至少上传一份可分析材料后才能开始分析")

    has_pdf = any(Path(item.safe_name).suffix.lower() == ".pdf" for item in files)
    has_recording = any(
        item.material_type in {"录音", "转写稿"} for item in files
    )
    has_screen_recording = any(item.material_type == "录屏" for item in files)
    requires_pdf = has_recording and not has_screen_recording
    if requires_pdf and not has_pdf and not package.pdf_exception_reason:
        package.material_completeness = "资料不完整"
        package.status = "待补资料／待标注"
        package.version += 1
        session.commit()
        raise DomainError(
            422,
            "录音或转写稿需要同步上传会议PDF；如确实没有PDF，请先填写例外原因并由负责人确认。",
        )
    package.material_completeness = "完整"

    run = m.AnalysisRun(
        package_id=package.id,
        adapter=adapter.name,
        model_name=adapter.model_name,
        status="运行中",
        started_at=m.utcnow(),
    )
    session.add(run)
    session.flush()
    for index, stage in enumerate(TASK_STAGES, 1):
        session.add(
            m.ProcessingTask(
                package_id=package.id,
                analysis_run_id=run.id,
                stage=stage,
                stage_order=index,
                status="处理中" if index == 1 else "待处理",
                progress=5 if index == 1 else 0,
                attempt_count=1 if index == 1 else 0,
                started_at=m.utcnow() if index == 1 else None,
            )
        )
    package.status = "分析中"
    package.version += 1
    session.commit()
    session.refresh(run)
    return run


def execute_analysis_run(
    session: Session,
    run: m.AnalysisRun,
    *,
    settings: Settings | None = None,
    adapter: IntentModelAdapter | None = None,
    stage_adapter=None,
    asr_settings=None,
) -> m.AnalysisRun:
    """Run the stage pipeline for an already-created analysis run.

    Called by the background worker (and by ``run_analysis`` for tests and
    compatibility).  Each stage persists its own result_json so a client can
    poll ``/api/meeting-packages/{id}/tasks`` for live progress.
    """
    if analysis_run_cancel_requested(session, run):
        cancelled_run = finalize_analysis_run_cancellation(session, run)
        if cancelled_run is not None:
            return cancelled_run
    package = session.get(m.MeetingPackage, run.package_id)
    if package is None:
        raise DomainError(404, "会议资料包不存在")
    if adapter is None:
        try:
            adapter = get_model_adapter(run.adapter)
        except ValueError as exc:
            raise DomainError(422, str(exc)) from exc
    files = session.scalars(
        select(m.MaterialFile).where(
            m.MaterialFile.package_id == package.id,
            m.MaterialFile.is_deleted.is_(False),
        )
    ).all()
    model_materials = [
        {
            "id": item.id,
            "type": item.material_type,
            "name": item.safe_name,
        }
        for item in files
    ]
    readable_material_ids: set[str] = set()
    master_data: dict[str, list[dict[str, Any]]] = {}
    if adapter.name != "mock":
        if settings is None:
            raise DomainError(500, "真实模型分析缺少本地数据目录配置")
        model_materials, readable_material_ids = _build_model_materials(
            settings, package, list(files)
        )
        master_data = _master_data_for_model(session)

    try:
        _raise_if_analysis_cancel_requested(session, run)
        if adapter.name == "mock":
            _update_task(session, run, "文件预检", status="已完成", progress=100)
            adapter_result = adapter.analyze(
                ModelContext(
                    package_id=package.id,
                    customer_id=package.customer_id,
                    material_summaries=model_materials,
                    master_data=master_data,
                )
            )
            _raise_if_analysis_cancel_requested(session, run)
            tasks = session.scalars(
                select(m.ProcessingTask)
                .where(m.ProcessingTask.analysis_run_id == run.id)
                .order_by(m.ProcessingTask.stage_order)
            ).all()
            _finish_analysis_tasks(run, list(tasks), adapter_name=adapter.name)
            for material in files:
                material.parse_status = "模拟解析完成"
            speaker_count, observation_count, unresolved = (
                _persist_mock_observations(
                    session, package, run, list(files), adapter
                )
            )
            _raise_if_analysis_cancel_requested(session, run)
        else:
            if settings is None:
                raise DomainError(500, "真实模型分析缺少本地数据目录配置")
            _update_task(session, run, "文件预检", status="已完成", progress=100)

            pages: list[m.PdfPageText] = []
            parsed_readable, extra_materials, parsed_transcripts = (
                _parse_document_stage(
                    session, settings, run, package, list(files), parsed_pages=pages,
                )
            )
            readable_material_ids |= parsed_readable
            _update_task(
                session,
                run,
                "文档解析",
                status="已完成",
                result_json={"derived_materials": len(extra_materials)},
            )
            _raise_if_analysis_cancel_requested(session, run)
            session.commit()

            _raise_if_analysis_cancel_requested(session, run)
            _asr_done, asr_transcripts = _run_audio_stage(
                session,
                settings,
                run,
                package,
                list(files),
                asr_settings,
            )
            _raise_if_analysis_cancel_requested(session, run)
            transcripts = parsed_transcripts + asr_transcripts
            frames = _run_visual_stage(
                session, settings, run, package, list(files)
            )
            _raise_if_analysis_cancel_requested(session, run)
            session.commit()
            model_materials = _extend_materials_with_derived(
                model_materials,
                extra_materials,
                transcripts,
                pages,
            )

            _raise_if_analysis_cancel_requested(session, run)
            _run_alignment_stage(
                session,
                stage_adapter,
                run,
                package,
                transcripts,
                pages,
                frames,
            )
            _raise_if_analysis_cancel_requested(session, run)
            session.commit()

            _raise_if_analysis_cancel_requested(session, run)
            full_context = ModelContext(
                package_id=package.id, customer_id=package.customer_id,
                material_summaries=model_materials, master_data=master_data,
            )
            if not any(str(item.get("content") or "").strip() for item in model_materials):
                # ASR can legitimately finish with only empty placeholders.
                # File names alone are not evidence for intent extraction.
                batches = []
            elif hasattr(adapter, "split_context"):
                contexts = adapter.split_context(full_context)
                batches = [context.material_summaries for context in contexts]
            else:
                batches = _split_model_materials_for_analysis(model_materials)
            merged_observations: list[dict[str, Any]] = []
            merged_usage: dict[str, Any] = {}
            base_result: dict[str, Any] | None = None
            total_batches = max(len(batches), 1)
            _update_task(
                session,
                run,
                "意向抽取",
                status="处理中",
                progress=5,
                result_json={
                    "mode": "分批分析",
                    "batch": 0,
                    "batches": len(batches),
                },
            )
            session.commit()
            for batch_index, batch in enumerate(batches, 1):
                _raise_if_analysis_cancel_requested(session, run)
                _update_task(
                    session,
                    run,
                    "意向抽取",
                    status="处理中",
                    progress=5 + int(90 * (batch_index - 1) / total_batches),
                    result_json={
                        "mode": "分批分析",
                        "batch": batch_index,
                        "batches": len(batches),
                    },
                )
                session.commit()
                logger.info(
                    "意向抽取 批次 %s/%s 材料项=%s 预估token=%.0f",
                    batch_index,
                    total_batches,
                    len(batch),
                    _estimate_material_tokens(batch),
                )
                batch_started_at = m.utcnow().isoformat()
                last_write = [0.0]

                def heartbeat(info):
                    if time.monotonic() - last_write[0] < 5:
                        return
                    last_write[0] = time.monotonic()
                    _update_task(session, run, "意向抽取", status="处理中", result_json={
                        "mode": "分批分析", "batch": batch_index, "batches": total_batches,
                        "completed_batches": batch_index - 1, "started_at": batch_started_at,
                        "last_activity_at": m.utcnow().isoformat(), **info,
                    })
                    session.commit()

                if hasattr(adapter, "set_runtime_hooks"):
                    adapter.set_runtime_hooks(
                        check_cancelled=lambda: _raise_if_analysis_cancel_requested(session, run),
                        on_progress=heartbeat,
                    )
                try:
                    batch_result = adapter.analyze(ModelContext(
                        package_id=package.id, customer_id=package.customer_id,
                        material_summaries=batch, master_data=master_data,
                    ))
                finally:
                    if hasattr(adapter, "set_runtime_hooks"):
                        adapter.set_runtime_hooks(check_cancelled=None, on_progress=None)
                _raise_if_analysis_cancel_requested(session, run)
                _update_task(session, run, "意向抽取", status="处理中",
                             progress=5 + int(90 * batch_index / total_batches),
                             result_json={"mode": "分批分析", "batch": batch_index,
                                          "batches": total_batches, "completed_batches": batch_index,
                                          "last_activity_at": m.utcnow().isoformat()})
                session.commit()
                observations = batch_result.get("observations") or []
                if not isinstance(observations, list):
                    observations = []
                scope = []
                for part in batch:
                    offset = int(part.get("content_offset") or 0)
                    source = next((item for item in model_materials
                                   if all(item.get(key) == part.get(key) for key in
                                          ("id", "source_material_file_id", "type", "name"))), part)
                    scope.append({**part, "_line_offset": str(source.get("content") or "").count("\n", 0, offset)})
                for observation in observations:
                    if not isinstance(observation, dict):
                        continue
                    quote = str(observation.get("quote") or "").strip()
                    located = _locate_exact_quote(scope, quote) if quote else None
                    source_key = None
                    if located:
                        source, line_no = located
                        source_key = (source.get("source_material_file_id"),
                                      source.get("start_ms"), source.get("end_ms"),
                                      source.get("page_no"), source.get("frame_no"),
                                      int(source.get("content_offset") or 0) + str(source.get("content") or "").find(quote))
                    # Source scope comes from the request we actually sent. Never
                    # let a later batch cite the first identical quote in another file.
                    merged_observations.append({**observation, "_source_scope": scope,
                                                "_source_key": source_key})
                usage = batch_result.get("usage")
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        if isinstance(value, (int, float)):
                            merged_usage[key] = (
                                merged_usage.get(key, 0) + value
                            )
                        else:
                            merged_usage.setdefault(key, value)
                base_result = batch_result

            # Collapse accidental duplicates while keeping distinct quotes and
            # interpretations (a repeated sentence can still be two records).
            deduped: list[dict[str, Any]] = []
            seen: set[tuple[Any, ...]] = set()
            for observation in merged_observations:
                key = (
                    " ".join(str(observation.get("quote") or "").split()),
                    str(observation.get("technology_id") or ""),
                    str(observation.get("speaker_name") or ""),
                    observation.get("attitude_level"),
                    observation.get("function_id"),
                    observation.get("_source_key"),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(observation)
            if len(deduped) != len(merged_observations):
                logger.info(
                    "意向抽取合并去重：%s 条 -> %s 条",
                    len(merged_observations),
                    len(deduped),
                )
            adapter_result = dict(base_result or {})
            adapter_result["observations"] = deduped
            if merged_usage:
                adapter_result["usage"] = merged_usage
            adapter_result["batches"] = len(batches)
            _raise_if_analysis_cancel_requested(session, run)
            speaker_count, observation_count, unresolved = (
                _persist_model_observations(
                    session,
                    package,
                    run,
                    model_materials,
                    adapter_result,
                    adapter,
                    material_file_ids={item.id for item in files},
                )
            )
            _update_task(
                session,
                run,
                "意向抽取",
                status="已完成" if batches else "不适用",
                progress=100,
                result_json={
                    "observations": observation_count,
                    "batches": len(batches),
                    "mode": "分批分析",
                },
            )
            _raise_if_analysis_cancel_requested(session, run)
            session.commit()

            _raise_if_analysis_cancel_requested(session, run)
            observation_speaker_ids = session.scalars(
                select(m.RawIntentObservation.speaker_id).where(
                    m.RawIntentObservation.analysis_run_id == run.id,
                    m.RawIntentObservation.speaker_id.is_not(None),
                )
            ).all()
            speakers: list[m.SpeakerCluster] = []
            if observation_speaker_ids:
                speakers = list(
                    session.scalars(
                        select(m.SpeakerCluster).where(
                            m.SpeakerCluster.id.in_(observation_speaker_ids)
                        )
                    ).all()
                )
            _run_identity_stage(
                session,
                stage_adapter,
                run,
                package,
                master_data,
                speakers,
            )
            _raise_if_analysis_cancel_requested(session, run)
            session.commit()

            for material in files:
                if material.id in readable_material_ids:
                    material.parse_status = "模型已读取"
                elif material.parse_status in {None, "待专用解析"}:
                    material.parse_status = "待专用解析"

        _raise_if_analysis_cancel_requested(session, run)
        finished_at = m.utcnow()
        safe_adapter_summary = {
            key: adapter_result[key]
            for key in ("mode", "model", "usage", "notice")
            if key in adapter_result
        }
        result_summary = {
            **safe_adapter_summary,
            "speaker_count": speaker_count,
            "observation_count": observation_count,
            "unresolved_count": len(unresolved),
            "unresolved_reasons": unresolved[:20],
            "data_scope": package.data_scope,
        }
        completion = session.execute(
            update(m.AnalysisRun)
            .where(
                m.AnalysisRun.id == run.id,
                m.AnalysisRun.status == "运行中",
                m.AnalysisRun.cancel_requested_at.is_(None),
            )
            .values(
                status="已完成",
                finished_at=finished_at,
                result_summary=result_summary,
            )
            .execution_options(synchronize_session=False)
        )
        if completion.rowcount != 1:
            raise _AnalysisCancellationRequested
        package.status = (
            "待身份校验" if observation_count > 0 else "待意向确认"
        )
        package.version += 1
        session.commit()
        session.refresh(run)
        return run
    except _AnalysisCancellationRequested:
        cancelled_run = finalize_analysis_run_cancellation(session, run)
        if cancelled_run is not None:
            return cancelled_run
        raise DomainError(409, "分析任务已停止")
    except Exception as exc:
        cancelled_run = finalize_analysis_run_cancellation(session, run)
        if cancelled_run is not None:
            return cancelled_run
        failed_run = session.get(m.AnalysisRun, run.id)
        failed_package = session.get(m.MeetingPackage, package.id)
        if failed_run:
            failed_run.status = "处理失败"
            failed_run.error_message = str(exc)
            failed_run.finished_at = m.utcnow()
        if failed_package:
            failed_package.status = "处理失败"
            failed_package.last_error = str(exc)
            failed_package.version += 1
        open_tasks = session.scalars(
            select(m.ProcessingTask).where(
                m.ProcessingTask.analysis_run_id == run.id,
                m.ProcessingTask.status.in_(("待处理", "处理中")),
            )
        ).all()
        for task in open_tasks:
            task.status = "处理失败"
            task.error_message = str(exc)
            task.finished_at = m.utcnow()
        session.commit()
        if isinstance(exc, DomainError):
            raise
        raise DomainError(500, f"AI分析失败：{exc}") from exc


def run_analysis(
    session: Session,
    package: m.MeetingPackage,
    *,
    adapter_name: str,
    force: bool,
    settings: Settings | None = None,
    adapter: IntentModelAdapter | None = None,
    stage_adapter=None,
    asr_settings=None,
) -> m.AnalysisRun:
    """Synchronous entrypoint retained for tests and compatibility.

    Production API routes use ``create_analysis_run`` + the background worker
    instead; this helper keeps the previous blocking behaviour available to
    callers that intentionally wait for completion in one call.
    """
    run = create_analysis_run(
        session,
        package,
        adapter_name=adapter_name,
        force=force,
        settings=settings,
        adapter=adapter,
        stage_adapter=stage_adapter,
        asr_settings=asr_settings,
    )
    if run.status == "已完成":
        # A completed run was reused because force=False.
        return run
    return execute_analysis_run(
        session,
        run,
        settings=settings,
        adapter=adapter,
        stage_adapter=stage_adapter,
        asr_settings=asr_settings,
    )


def run_mock_analysis(
    session: Session,
    package: m.MeetingPackage,
    *,
    adapter_name: str,
    force: bool,
    settings: Settings | None = None,
    adapter: IntentModelAdapter | None = None,
) -> m.AnalysisRun:
    """Backward-compatible entrypoint retained for existing API/tests."""

    return run_analysis(
        session,
        package,
        adapter_name=adapter_name,
        force=force,
        settings=settings,
        adapter=adapter,
    )


def merge_speakers(
    session: Session,
    *,
    package_id: str,
    keep_speaker_id: str,
    merge_speaker_ids: list[str],
    actor: str,
) -> m.SpeakerCluster:
    keep = session.get(m.SpeakerCluster, keep_speaker_id)
    if keep is None or keep.package_id != package_id:
        raise DomainError(404, "保留说话人不存在或不属于该资料包")
    for speaker_id in merge_speaker_ids:
        target = session.get(m.SpeakerCluster, speaker_id)
        if target is None or target.package_id != package_id:
            raise DomainError(404, f"说话人不存在：{speaker_id}")
        if target.status == "已确认":
            raise DomainError(
                422,
                f"说话人{target.label}已确认身份，请先拆分或取消确认后再合并",
            )
        segments = session.scalars(
            select(m.SpeakerSegment).where(
                m.SpeakerSegment.speaker_id == target.id
            )
        ).all()
        for segment in segments:
            segment.speaker_id = keep.id
        observations = session.scalars(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.speaker_id == target.id
            )
        ).all()
        for observation in observations:
            ensure_ai_snapshot(observation)
            observation.speaker_id = keep.id
            observation.version += 1
        session.add(
            m.ConfirmationRecord(
                target_type="speaker",
                target_id=target.id,
                action="合并",
                before_snapshot={"merged_into": keep.id},
                after_snapshot={"merged_into": keep.id},
                actor=actor,
                note=f"合并到{keep.label}",
            )
        )
        session.delete(target)
    keep.version += 1
    session.commit()
    session.refresh(keep)
    return keep


def _next_speaker_label(session: Session, package_id: str) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(m.SpeakerCluster)
        .where(m.SpeakerCluster.package_id == package_id)
    )
    return int(count or 0) + 1


def split_speaker(
    session: Session,
    *,
    package_id: str,
    speaker_id: str,
    segment_ids: list[str],
    actor: str,
) -> m.SpeakerCluster:
    source = get_visible_speaker(session, speaker_id)
    if source is None or source.package_id != package_id:
        raise DomainError(404, "说话人不存在或不属于该资料包")
    all_segments = session.scalars(
        select(m.SpeakerSegment).where(
            m.SpeakerSegment.speaker_id == source.id,
        )
    ).all()
    selected_ids = set(segment_ids)
    segments = [item for item in all_segments if item.id in selected_ids]
    if not segments or {item.id for item in segments} != selected_ids:
        raise DomainError(422, "所选片段不存在或不属于该说话人")
    observations = session.scalars(select(m.RawIntentObservation).where(
        m.RawIntentObservation.speaker_id == source.id,
        visible_observation_clause(),
    )).all()
    source_runs = {item.analysis_run_id for item in observations}
    for record in session.scalars(select(m.ConfirmationRecord).where(
        m.ConfirmationRecord.target_id == source.id,
        visible_split_provenance_clause(),
    )).all():
        source_runs.add(record.after_snapshot["analysis_run_id"])
    # Older clusters could be reused by later failed analyses and their
    # speech rows carry no run id. Do not give those ambiguous rows a manual
    # visibility exemption. New ASR labels are scoped to the analysis run.
    uncertain_run = session.scalar(select(m.AnalysisRun.id).where(
        m.AnalysisRun.package_id == package_id,
        m.AnalysisRun.id.not_in([item for item in source_runs if item is not None]),
        or_(m.AnalysisRun.status != "已完成", m.AnalysisRun.cancel_requested_at.is_not(None)),
        or_(m.AnalysisRun.finished_at.is_(None), m.AnalysisRun.finished_at >= source.created_at),
    ).limit(1))
    if uncertain_run:
        raise DomainError(422, "说话人可能包含未成功分析的片段，来源无法确定；请完成或重新分析后再拆分")
    label_index = _next_speaker_label(session, package_id)
    target = m.SpeakerCluster(
        package_id=package_id,
        label=f"Speaker {label_index}",
        recommended_name="待确认人员（拆分）",
        confidence=0.5,
        status="待确认",
    )
    session.add(target)
    session.flush()
    moved_ids = [segment.id for segment in segments]
    for segment in segments:
        segment.speaker_id = target.id
    moved_observations = []
    target_runs = set()
    attributed_segments = set()
    for observation in observations:
        quote = re.sub(r"\s+", "", observation.raw_quote)
        evidence = session.get(m.EvidenceFragment, observation.evidence_id) if observation.evidence_id else None
        matches = []
        for segment in all_segments:
            if not quote or quote not in re.sub(r"\s+", "", segment.transcript):
                continue
            if evidence and evidence.start_ms is not None and evidence.end_ms is not None:
                if not (segment.start_ms < evidence.end_ms and evidence.start_ms < segment.end_ms):
                    continue
            matches.append(segment.id)
        # Ambiguous text shared with an unselected segment stays with the source.
        if matches and set(matches).issubset(selected_ids):
            ensure_ai_snapshot(observation)
            observation.speaker_id = target.id
            observation.function_id = None
            observation.status = "待确认"
            observation.version += 1
            moved_observations.append(observation.id)
            target_runs.add(observation.analysis_run_id)
            attributed_segments.update(matches)
    if attributed_segments != selected_ids:
        if len(source_runs) != 1:
            session.rollback()
            raise DomainError(422, "片段跨分析批次且来源不明确，无法安全拆分；请先核对证据来源")
        target_runs = set(source_runs)
    # A child must not inherit unrelated runs as a fallback source. Preserve
    # an otherwise unreferenced parent for its remaining manual-review clips.
    owners = [(target, target_runs)]
    if len(moved_observations) == len(observations):
        owners.append((source, source_runs))
    for owner, run_ids in owners:
        for run_id in run_ids:
            session.add(m.ConfirmationRecord(
                target_type="speaker", target_id=owner.id, action="拆分来源",
                after_snapshot={"analysis_run_id": run_id, "source_speaker_id": source.id},
                actor=actor,
            ))
    session.add(
        m.ConfirmationRecord(
            target_type="speaker",
            target_id=source.id,
            action="拆分",
            before_snapshot={"segments": moved_ids},
            after_snapshot={"created_speaker": target.id, "segments": moved_ids,
                            "observations": moved_observations},
            actor=actor,
        )
    )
    source.version += 1
    package = session.get(m.MeetingPackage, package_id)
    if package:
        package.status = "待身份校验"
        package.version += 1
    session.commit()
    session.refresh(target)
    return target


def confirm_speaker(
    session: Session, speaker: m.SpeakerCluster, payload: SpeakerConfirmRequest
) -> m.SpeakerCluster:
    if speaker.version != payload.version:
        raise DomainError(
            409, f"说话人已被更新，请刷新后重试（当前版本{speaker.version}）"
        )
    if payload.function_id and not session.get(m.CustomerFunction, payload.function_id):
        raise DomainError(422, "客户职能不存在或已被删除")

    before = _snapshot(
        speaker,
        [
            "confirmed_name",
            "confirmed_role",
            "confirmed_organization",
            "confirmed_function_id",
            "status",
            "version",
        ],
    )
    speaker.confirmed_name = payload.display_name or speaker.recommended_name
    speaker.confirmed_role = payload.role or speaker.recommended_role
    speaker.confirmed_organization = (
        payload.organization or speaker.recommended_organization
    )
    speaker.confirmed_function_id = (
        payload.function_id or speaker.recommended_function_id
    )
    is_confirmed = (
        payload.identity_granularity != "待确认"
        and speaker.confirmed_function_id is not None
    )
    speaker.status = "已确认" if is_confirmed else "待确认"
    speaker.version += 1

    mapping = m.SpeakerIdentityMapping(
        speaker_id=speaker.id,
        display_name=speaker.confirmed_name,
        role=speaker.confirmed_role,
        organization=speaker.confirmed_organization,
        function_id=speaker.confirmed_function_id,
        identity_granularity=payload.identity_granularity,
        confirmed_by=payload.actor,
    )
    session.add(mapping)
    if is_confirmed:
        observations = session.scalars(
            select(m.RawIntentObservation).where(
                m.RawIntentObservation.speaker_id == speaker.id
            )
        ).all()
        for observation in observations:
            if observation.function_id is None:
                ensure_ai_snapshot(observation)
                observation.function_id = speaker.confirmed_function_id
                observation.version += 1

    after = _snapshot(
        speaker,
        [
            "confirmed_name",
            "confirmed_role",
            "confirmed_organization",
            "confirmed_function_id",
            "status",
            "version",
        ],
    )
    session.add(
        m.ConfirmationRecord(
            target_type="speaker",
            target_id=speaker.id,
            action="确认" if is_confirmed else "保留待确认",
            before_snapshot=before,
            after_snapshot=after,
            actor=payload.actor,
        )
    )
    session.add(
        m.StatusVersion(
            entity_type="speaker",
            entity_id=speaker.id,
            version_no=speaker.version,
            snapshot=after,
            changed_by=payload.actor,
            change_reason="人工校验说话人身份",
        )
    )

    package = session.get(m.MeetingPackage, speaker.package_id)
    pending_count = session.scalar(
        select(func.count())
        .select_from(m.SpeakerCluster)
        .where(
            m.SpeakerCluster.package_id == speaker.package_id,
            m.SpeakerCluster.status != "已确认",
            m.SpeakerCluster.id != speaker.id,
        )
    )
    if package and is_confirmed and pending_count == 0:
        package.status = "待意向确认"
        package.version += 1
    session.commit()
    session.refresh(speaker)
    return speaker


def confirm_observation(
    session: Session,
    observation: m.RawIntentObservation,
    payload: IntentConfirmRequest,
) -> tuple[m.RawIntentObservation, m.FormalIntentStatus]:
    if observation.version != payload.version:
        raise DomainError(
            409, f"意向观察已被更新，请刷新后重试（当前版本{observation.version}）"
        )
    if observation.function_id is None:
        raise DomainError(422, "客户职能尚未确认，不能形成正式职能状态")
    if observation.analysis_run_id is not None:
        source_run = session.get(m.AnalysisRun, observation.analysis_run_id)
        if (
            source_run is None
            or source_run.status != "已完成"
            or source_run.cancel_requested_at is not None
        ):
            raise DomainError(
                422, "来源分析尚未成功完成或已被取消，不能形成正式职能状态"
            )
    if observation.speaker_id is None:
        raise DomainError(422, "缺少可回溯的说话人身份，不能形成正式职能状态")
    speaker = session.get(m.SpeakerCluster, observation.speaker_id)
    if speaker is None or speaker.package_id != observation.package_id:
        raise DomainError(422, "说话人未关联本会议，不能形成正式职能状态")
    if speaker.status != "已确认":
        raise DomainError(422, "说话人身份仍待确认，不能形成正式职能状态")
    evidence = (
        session.get(m.EvidenceFragment, observation.evidence_id)
        if observation.evidence_id
        else None
    )
    if evidence is None:
        raise DomainError(422, "缺少可追溯证据，不能形成正式意向状态")
    material = (
        session.get(m.MaterialFile, evidence.material_file_id)
        if evidence.material_file_id
        else None
    )
    if (
        evidence.package_id != observation.package_id
        or material is None
        or material.package_id != observation.package_id
        or material.is_deleted
    ):
        raise DomainError(
            422, "证据未关联本会议的原始材料，不能形成正式意向状态"
        )
    has_valid_time_range = (
        evidence.start_ms is not None
        and evidence.end_ms is not None
        and evidence.start_ms >= 0
        and evidence.end_ms > evidence.start_ms
    )
    has_valid_page = evidence.page_no is not None and evidence.page_no > 0
    has_valid_frame = evidence.frame_no is not None and evidence.frame_no >= 0
    has_text_locator = bool(evidence.locator and evidence.locator.strip())
    if not (
        has_valid_time_range
        or has_valid_page
        or has_valid_frame
        or has_text_locator
    ):
        raise DomainError(
            422, "证据缺少时间戳、页码或文本位置，不能形成正式意向状态"
        )
    if not evidence.excerpt or not evidence.excerpt.strip():
        raise DomainError(422, "证据原文为空，不能形成正式意向状态")

    # A reviewed platform proposal remains pending until this exact formal
    # confirmation.  Activating it here keeps the dictionary publication,
    # concrete platform FK and formal status in one database transaction.
    platform_draft = session.scalar(
        select(m.ObservationPlatformDraft).where(
            m.ObservationPlatformDraft.observation_id == observation.id,
            m.ObservationPlatformDraft.status == "pending",
        )
    )
    if platform_draft is not None:
        confirm_observation_platform(
            session,
            observation_id=observation.id,
            actor=payload.actor,
        )

    package = session.get(m.MeetingPackage, observation.package_id)
    if package is None:
        raise DomainError(404, "会议资料包不存在")
    ensure_ai_snapshot(observation)
    if payload.is_supporter and not _has_supporter_basis(observation, payload):
        raise DomainError(
            422,
            "支持者需要主动推荐、协调资源或推动验证的证据；"
            "如由负责人独立判断，请填写supporter_evidence_note。",
        )

    adoption_key = adoption_status_key(
        observation.customer_id,
        observation.technology_id,
        observation.generic_platform_id,
        observation.customer_project_id,
    )
    existing_adoptions = session.scalars(
        select(m.AdoptionStageState)
        .where(m.AdoptionStageState.status_key == adoption_key)
        .order_by(m.AdoptionStageState.version_no.desc())
    ).all()
    current_adoption = next(
        (item for item in existing_adoptions if item.is_current), None
    )
    # The adoption stage belongs to customer+technology+platform/project, not
    # to a customer function.  An explicit Unknown functional attitude must
    # therefore never roll that shared stage backwards.  For a determinate
    # attitude, confirming the reviewed draft also confirms its displayed
    # stage when the request omits the field.
    requested_adoption_stage = (
        None
        if payload.attitude_level is None
        else (
            payload.adoption_stage
            or observation.suggested_adoption_stage
        )
    )
    current_adoption_same_package = False
    if current_adoption:
        for source_id in current_adoption.source_observation_ids or []:
            source = session.get(m.RawIntentObservation, source_id)
            if source and source.package_id == observation.package_id:
                current_adoption_same_package = True
                break
    if (
        requested_adoption_stage
        and current_adoption
        and current_adoption_same_package
        and current_adoption.stage != requested_adoption_stage
    ):
        raise DomainError(
            409,
            "同一会议同一客户／技术／平台范围已确认采用阶段"
            f"“{current_adoption.stage}”，本职能建议为"
            f"“{requested_adoption_stage}”。职能逐条确认不得相互覆盖；"
            "请先统一采用阶段后再确认。",
        )

    key = formal_status_key(
        observation.customer_id,
        observation.technology_id,
        observation.generic_platform_id,
        observation.customer_project_id,
        observation.function_id,
    )
    existing_statuses = session.scalars(
        select(m.FormalIntentStatus)
        .where(m.FormalIntentStatus.status_key == key)
        .order_by(m.FormalIntentStatus.version_no.desc())
    ).all()
    previous = next(
        (item for item in existing_statuses if item.is_current), None
    )
    version_no = (
        max((item.version_no for item in existing_statuses), default=0) + 1
    )
    becomes_current = (
        previous is None or package.meeting_date >= previous.effective_at
    )
    if previous and becomes_current:
        previous.is_current = False
        for previous_observation_id in previous.source_observation_ids or []:
            previous_observation = session.get(
                m.RawIntentObservation, previous_observation_id
            )
            if (
                previous_observation
                and previous_observation.id != observation.id
                and previous_observation.status in {"已确认", "无法判断"}
            ):
                previous_observation.status = "已被后续版本取代"
                previous_observation.version += 1
    previous_by_effective_date = max(
        (
            item
            for item in existing_statuses
            if item.effective_at <= package.meeting_date
        ),
        key=lambda item: (item.effective_at, item.version_no),
        default=None,
    )

    previous_formal_snapshot = (
        _snapshot(
            previous,
            [
                "attitude_level",
                "is_supporter",
                "is_resistance",
                "conditions",
                "bottlenecks",
                "version_no",
            ],
        )
        if previous
        else None
    )
    before = {
        "previous_formal_status": previous_formal_snapshot,
        "reviewed_observation": observation_review_snapshot(observation),
        "current_adoption_stage": (
            current_adoption.stage if current_adoption else None
        ),
    }
    formal = m.FormalIntentStatus(
        status_key=key,
        customer_id=observation.customer_id,
        technology_id=observation.technology_id,
        generic_platform_id=observation.generic_platform_id,
        customer_project_id=observation.customer_project_id,
        function_id=observation.function_id,
        attitude_level=payload.attitude_level,
        is_supporter=payload.is_supporter,
        is_resistance=payload.is_resistance,
        conditions=payload.conditions,
        bottlenecks=payload.bottlenecks,
        source_observation_ids=[observation.id],
        effective_at=package.meeting_date,
        confirmed_by=payload.actor,
        change_note=payload.note,
        version_no=version_no,
        previous_version_id=(
            previous_by_effective_date.id
            if previous_by_effective_date
            else None
        ),
        is_current=becomes_current,
    )
    session.add(formal)
    session.flush()
    if formal.generic_platform_id:
        capture_path_snapshot(
            session,
            target_type="formal_intent_status",
            target_id=formal.id,
            technology_id=formal.technology_id,
            generic_platform_id=formal.generic_platform_id,
            actor=payload.actor,
            version_no=formal.version_no,
        )
    after = {
        "attitude_level": payload.attitude_level,
        "is_supporter": payload.is_supporter,
        "is_resistance": payload.is_resistance,
        "conditions": payload.conditions,
        "bottlenecks": payload.bottlenecks,
        "adoption_stage": (
            requested_adoption_stage
            or (current_adoption.stage if current_adoption else None)
        ),
        "support_reason": observation.support_reason,
        "resistance_reason": observation.resistance_reason,
        "decision_influence": observation.decision_influence,
        "uncertainty_reason": observation.uncertainty_reason,
        "supporter_evidence_note": payload.supporter_evidence_note,
        "evidence": {
            "id": observation.evidence_id,
            "type": session.get(
                m.EvidenceFragment, observation.evidence_id
            ).evidence_type,
            "locator": session.get(
                m.EvidenceFragment, observation.evidence_id
            ).locator,
        },
        "model_metadata": {
            "analysis_run_id": observation.analysis_run_id,
            "model_name": observation.model_name,
            "prompt_version": observation.prompt_version,
            "rule_version": observation.rule_version,
        },
        "ai_original": ai_original_snapshot(observation),
        "version_no": version_no,
        "effective_at": package.meeting_date.isoformat(),
        "source_observation_ids": [observation.id],
        "is_current": becomes_current,
    }
    session.add(
        m.StatusVersion(
            entity_type="formal_intent_status",
            entity_id=formal.id,
            version_no=version_no,
            snapshot=after,
            changed_by=payload.actor,
            change_reason=payload.note or "人工确认意向",
        )
    )
    session.add(
        m.ConfirmationRecord(
            target_type="raw_intent_observation",
            target_id=observation.id,
            action=(
                "无法判断"
                if payload.attitude_level is None
                else "确认"
            ),
            before_snapshot=before,
            after_snapshot=after,
            actor=payload.actor,
            note=payload.note,
        )
    )

    if (
        requested_adoption_stage
        and not (
            current_adoption_same_package
            and current_adoption
            and current_adoption.stage == requested_adoption_stage
        )
    ):
        previous_adoption = current_adoption
        adoption_version = (
            max((item.version_no for item in existing_adoptions), default=0) + 1
        )
        adoption_becomes_current = (
            previous_adoption is None
            or package.meeting_date >= previous_adoption.effective_at
        )
        if previous_adoption and adoption_becomes_current:
            previous_adoption.is_current = False
        previous_adoption_by_effective_date = max(
            (
                item
                for item in existing_adoptions
                if item.effective_at <= package.meeting_date
            ),
            key=lambda item: (item.effective_at, item.version_no),
            default=None,
        )
        adoption = m.AdoptionStageState(
            status_key=adoption_key,
            customer_id=observation.customer_id,
            technology_id=observation.technology_id,
            generic_platform_id=observation.generic_platform_id,
            customer_project_id=observation.customer_project_id,
            stage=requested_adoption_stage,
            source_observation_ids=[observation.id],
            effective_at=package.meeting_date,
            confirmed_by=payload.actor,
            change_note=payload.note,
            version_no=adoption_version,
            previous_version_id=(
                previous_adoption_by_effective_date.id
                if previous_adoption_by_effective_date
                else None
            ),
            is_current=adoption_becomes_current,
        )
        session.add(adoption)
        session.flush()
        session.add(
            m.StatusVersion(
                entity_type="adoption_stage",
                entity_id=adoption.id,
                version_no=adoption_version,
                snapshot={
                    "stage": requested_adoption_stage,
                    "effective_at": package.meeting_date.isoformat(),
                    "source_observation_ids": [observation.id],
                    "is_current": adoption_becomes_current,
                    "evidence_id": observation.evidence_id,
                    "support_reason": observation.support_reason,
                    "resistance_reason": observation.resistance_reason,
                    "bottlenecks": list(payload.bottlenecks),
                    "model_metadata": {
                        "analysis_run_id": observation.analysis_run_id,
                        "model_name": observation.model_name,
                        "prompt_version": observation.prompt_version,
                        "rule_version": observation.rule_version,
                    },
                },
                changed_by=payload.actor,
                change_reason=payload.note or "人工确认采用阶段",
            )
        )

    observation.status = (
        "无法判断" if payload.attitude_level is None else "已确认"
    )
    # Keep the reviewed draft and immutable AI baseline untouched.  The final
    # human decision lives in FormalIntentStatus/AdoptionStageState and their
    # audit records rather than being written back into the AI suggestion.
    observation.version += 1

    pending_count = session.scalar(
        select(func.count())
        .select_from(m.RawIntentObservation)
        .where(
            m.RawIntentObservation.package_id == package.id,
            ~m.RawIntentObservation.status.in_(TERMINAL_OBSERVATION_STATUSES),
            m.RawIntentObservation.id != observation.id,
        )
    )
    if pending_count == 0:
        package.status = "已确认"
        package.confirmed_by = payload.actor
        package.confirmed_at = m.utcnow()
        package.version += 1
    session.commit()
    session.refresh(observation)
    session.refresh(formal)
    return observation, formal
