from __future__ import annotations

import codecs
import hashlib
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from .config import Settings
from .domain import (
    TERMINAL_OBSERVATION_STATUSES,
    adoption_status_key,
    formal_status_key,
)
from .model_adapters import IntentModelAdapter, ModelContext, get_model_adapter
from .schemas import IntentConfirmRequest, SpeakerConfirmRequest
from .seed import TASK_STAGES
from .taxonomy_service import (
    capture_path_snapshot,
    confirm_observation_platform,
)


class DomainError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _human_size(size_bytes: int) -> str:
    if size_bytes % (1024 * 1024 * 1024) == 0:
        return f"{size_bytes // (1024 * 1024 * 1024)}GB"
    if size_bytes % (1024 * 1024) == 0:
        return f"{size_bytes // (1024 * 1024)}MB"
    if size_bytes % 1024 == 0:
        return f"{size_bytes // 1024}KB"
    return f"{size_bytes}字节"


_DIRECT_TEXT_EXTENSIONS = {".txt", ".md", ".srt", ".vtt"}
_MODEL_INPUT_LIMIT = 80_000
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
    remaining = _MODEL_INPUT_LIMIT
    for item in files:
        path = (settings.data_dir / item.relative_path).resolve()
        if settings.data_dir.resolve() not in path.parents or not path.is_file():
            raise DomainError(404, f"材料文件不存在：{item.safe_name}")
        content = _read_model_text(path)
        summary: dict[str, Any] = {
            "id": item.id,
            "type": item.material_type,
            "name": item.safe_name,
        }
        if content is not None and content.strip() and remaining > 0:
            clipped = content.strip()[:remaining]
            summary["content"] = clipped
            remaining -= len(clipped)
            readable_ids.add(item.id)
        materials.append(summary)

    if package.notes and package.notes.strip() and remaining > 0:
        note = package.notes.strip()[:remaining]
        materials.append(
            {
                "id": None,
                "type": "资料包备注",
                "name": "资料包备注",
                "content": note,
            }
        )
        remaining -= len(note)

    if not any(item.get("content") for item in materials):
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
    for material in materials:
        content = material.get("content")
        if not isinstance(content, str):
            continue
        offset = content.find(quote)
        if offset >= 0:
            return material, content.count("\n", 0, offset) + 1
    return None


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
) -> tuple[int, int, list[str]]:
    observations = adapter_result.get("observations")
    if not isinstance(observations, list):
        raise DomainError(502, "模型响应缺少结构化 observations 数组")

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

    created = 0
    speaker_count = 0
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
        located = _locate_exact_quote(materials, quote) if quote else None
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
        speaker = m.SpeakerCluster(
            package_id=package.id,
            label=f"Speaker {index}",
            recommended_name=(
                str(raw.get("speaker_name")).strip()
                if raw.get("speaker_name")
                else f"待确认人员{index}"
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
        speaker_count += 1
        evidence = m.EvidenceFragment(
            package_id=package.id,
            material_file_id=material.get("id"),
            evidence_type=f"{material.get('type', '会议材料')}原文",
            locator=f"{material.get('name', '会议材料')} · 第{line_no}行",
            excerpt=quote,
            confidence=confidence,
            playback_available=False,
        )
        session.add(evidence)
        session.flush()
        snapshot = {
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
    return speaker_count, created, unresolved


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


def ensure_ai_snapshot(observation: m.RawIntentObservation) -> dict[str, Any]:
    """Capture the AI baseline once, before any human mutation."""

    if not observation.ai_snapshot:
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


def run_analysis(
    session: Session,
    package: m.MeetingPackage,
    *,
    adapter_name: str,
    force: bool,
    settings: Settings | None = None,
    adapter: IntentModelAdapter | None = None,
) -> m.AnalysisRun:
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
                m.AnalysisRun.status == "已完成",
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

    try:
        adapter_result = adapter.analyze(
            ModelContext(
                package_id=package.id,
                customer_id=package.customer_id,
                material_summaries=model_materials,
                master_data=master_data,
            )
        )
        tasks = session.scalars(
            select(m.ProcessingTask)
            .where(m.ProcessingTask.analysis_run_id == run.id)
            .order_by(m.ProcessingTask.stage_order)
        ).all()
        _finish_analysis_tasks(run, list(tasks), adapter_name=adapter.name)
        for material in files:
            if adapter.name == "mock":
                material.parse_status = "模拟解析完成"
            elif material.id in readable_material_ids:
                material.parse_status = "模型已读取"
            else:
                material.parse_status = "待专用解析"

        if adapter.name == "mock":
            speaker_count, observation_count, unresolved = (
                _persist_mock_observations(
                    session, package, run, list(files), adapter
                )
            )
        else:
            speaker_count, observation_count, unresolved = (
                _persist_model_observations(
                    session,
                    package,
                    run,
                    model_materials,
                    adapter_result,
                    adapter,
                )
            )

        run.status = "已完成"
        run.finished_at = m.utcnow()
        safe_adapter_summary = {
            key: adapter_result[key]
            for key in ("mode", "model", "usage", "notice")
            if key in adapter_result
        }
        run.result_summary = {
            **safe_adapter_summary,
            "speaker_count": speaker_count,
            "observation_count": observation_count,
            "unresolved_count": len(unresolved),
            "unresolved_reasons": unresolved[:20],
            "data_scope": package.data_scope,
        }
        package.status = (
            "待身份校验" if observation_count > 0 else "待意向确认"
        )
        package.version += 1
        session.commit()
        session.refresh(run)
        return run
    except Exception as exc:
        session.rollback()
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
        session.commit()
        if isinstance(exc, DomainError):
            raise
        raise DomainError(500, f"AI分析失败：{exc}") from exc


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
    if observation.speaker_id:
        speaker = session.get(m.SpeakerCluster, observation.speaker_id)
        if speaker is not None and speaker.status != "已确认":
            raise DomainError(422, "说话人身份仍待确认，不能形成正式职能状态")
    if observation.evidence_id is None or session.get(
        m.EvidenceFragment, observation.evidence_id
    ) is None:
        raise DomainError(422, "缺少可追溯证据，不能形成正式意向状态")

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
