from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AsrSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str | None
    confidence: float
    language: str | None = None


@dataclass
class AsrResult:
    segments: list[AsrSegment]
    model_name: str
    prompt_version: str = "asr-prompt-v1"
    usage: dict[str, Any] | None = None


class AsrError(RuntimeError):
    """Sanitized ASR provider error safe to persist or return to a local UI."""


_SPECIAL_TOKEN_PATTERN = r"<\|[^>]*\|>"


def _clean_transcript_text(raw: str) -> str:
    """Strip Qwen3-ASR formatting artifacts from a transcript.

    The model reports the detected language first and then emits the
    transcription after an ``<asr_text>`` marker, e.g.::

        language Chinese<asr_text>这是转写正文...

    It may also emit special tokens such as ``<|startofthink|>``.  Those
    markers belong to the model's output protocol, not to the meeting
    content, so they are removed before the text is persisted as evidence.
    """
    text = re.sub(_SPECIAL_TOKEN_PATTERN, "", raw or "")
    marker = "<asr_text>"
    marker_index = text.find(marker)
    if marker_index != -1:
        text = text[marker_index + len(marker):]
    else:
        # Fallback: drop a leading "language <lang>" phrase when present.
        text = re.sub(
            r"^\s*language\s+[A-Za-z_-]+[\s,;:]*",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return " ".join(text.split())


class AsrAdapter(ABC):
    name: str
    model_name: str

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        hotwords: list[str],
        duration_ms: int | None = None,
    ) -> AsrResult:
        raise NotImplementedError


class MockAsrAdapter(AsrAdapter):
    """Rule-based adapter used for simulated demo runs only."""

    name = "mock-asr"
    model_name = "mock-asr-v1"

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        hotwords: list[str],
    ) -> AsrResult:
        segments = [
            AsrSegment(
                start_ms=0,
                end_ms=30_000,
                text="模拟转写片段：该材料为模拟数据，不代表真实语音内容。",
                speaker_label="Speaker A",
                confidence=0.5,
                language=language or "zh",
            )
        ]
        return AsrResult(
            segments=segments,
            model_name=self.model_name,
            usage={"notice": "规则化模拟结果"},
        )


class LlamaCppAsrAdapter(AsrAdapter):
    """Calls a local llama.cpp qwen3-asr service (OpenAI-compatible endpoint).

    Expected request shape follows the llama.cpp server audio transcription
    endpoint.  The adapter parses a JSON result of the form:

        {"segments": [{"start": ms, "end": ms, "text": "...",
                       "speaker": "Speaker A"|null, "confidence": 0-1}]}

    Exact field names depend on the deployed llama.cpp version and are
    normalized defensively here.
    """

    name = "llamacpp-asr"

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        timeout_seconds: float = 1800,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model_name = model_name
        self._timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(timeout_seconds, 10),
        )
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self._timeout,
            trust_env=False,
            transport=self._transport,
        )

    @staticmethod
    def _safe_error(exc: Exception) -> AsrError:
        if isinstance(exc, httpx.TimeoutException):
            return AsrError("ASR转写超时，请确认模型服务已启动")
        if isinstance(exc, httpx.ConnectError):
            return AsrError("无法连接本地ASR服务，请检查地址和端口")
        if isinstance(exc, httpx.HTTPStatusError):
            return AsrError(
                f"ASR服务返回HTTP {exc.response.status_code}"
            )
        if isinstance(exc, httpx.RequestError):
            return AsrError("ASR请求失败，请检查本地服务状态")
        return AsrError("ASR响应格式无效")

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None,
        hotwords: list[str],
        duration_ms: int | None = None,
    ) -> AsrResult:
        logger.info(
            "LlamaCpp ASR 请求开始 file=%s duration_ms=%s",
            audio_path.name,
            duration_ms,
        )
        try:
            with self._client() as client:
                response = client.post(
                    "/v1/audio/transcriptions",
                    files={
                        "file": (
                            audio_path.name,
                            audio_path.open("rb"),
                            "audio/wav",
                        )
                    },
                    data={
                        "model": self.model_name,
                        "language": language or "zh",
                        "hotwords": ",".join(hotwords),
                        "response_format": "json",
                    },
                )
                response.raise_for_status()
                body = response.json()
        except (
            httpx.RequestError,
            httpx.HTTPStatusError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise self._safe_error(exc) from exc
        finally:
            logger.info(
                "LlamaCpp ASR 请求结束 file=%s",
                audio_path.name,
            )

        segments: list[AsrSegment] = []
        if isinstance(body, dict):
            raw_segments = body.get("segments")
            if isinstance(raw_segments, list):
                for raw in raw_segments:
                    if not isinstance(raw, dict):
                        continue
                    text = _clean_transcript_text(str(raw.get("text") or ""))
                    if not text:
                        continue
                    try:
                        start_ms = int(raw.get("start", raw.get("start_ms", 0)))
                        end_ms = int(raw.get("end", raw.get("end_ms", start_ms)))
                    except (TypeError, ValueError):
                        continue
                    try:
                        confidence = min(
                            1.0, max(0.0, float(raw.get("confidence", 0.5)))
                        )
                    except (TypeError, ValueError):
                        confidence = 0.5
                    speaker = raw.get("speaker") or raw.get("speaker_label")
                    segments.append(
                        AsrSegment(
                            start_ms=start_ms,
                            end_ms=max(end_ms, start_ms),
                            text=text,
                            speaker_label=(
                                str(speaker).strip() if speaker else None
                            ),
                            confidence=confidence,
                            language=(
                                str(raw["language"]).strip()
                                if raw.get("language")
                                else language
                            ),
                        )
                    )
            else:
                # llama.cpp server returns a single transcript text:
                # {"type": "transcript.text.done", "text": "...", "usage": {...}}
                text = _clean_transcript_text(str(body.get("text") or ""))
                if text:
                    segments.append(
                        AsrSegment(
                            start_ms=0,
                            end_ms=max(duration_ms or 0, 0),
                            text=text,
                            speaker_label=None,
                            confidence=0.5,
                            language=language,
                        )
                    )
        if not segments:
            logger.warning(
                "LlamaCpp ASR 未返回可用转写 file=%s duration_ms=%s",
                audio_path.name,
                duration_ms,
            )
            raise AsrError("ASR未返回任何可用的转写片段")
        logger.info(
            "LlamaCpp ASR 成功 file=%s segments=%s",
            audio_path.name,
            len(segments),
        )
        return AsrResult(
            segments=segments,
            model_name=self.model_name,
            usage=(
                body.get("usage")
                if isinstance(body, dict)
                and isinstance(body.get("usage"), dict)
                else None
            ),
        )


def get_asr_adapter(
    provider: str,
    *,
    base_url: str | None = None,
    model_name: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> AsrAdapter:
    normalized = (provider or "").strip().lower()
    if normalized in {"", "mock", "mock-asr"}:
        return MockAsrAdapter()
    if normalized in {"llamacpp", "llamacpp-asr"}:
        if not base_url or not model_name:
            raise ValueError("ASR适配器缺少服务地址或模型名称")
        return LlamaCppAsrAdapter(
            base_url=base_url,
            model_name=model_name,
            transport=transport,
        )
    raise ValueError(
        f"ASR适配器“{normalized}”尚未配置；当前仅支持mock或llamacpp。"
    )
