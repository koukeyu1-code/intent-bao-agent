from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
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

    def __init__(self, message: str, *, category: str = "invalid_response", retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


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
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        if timeout_seconds is None:
            # 默认 180 秒；可用 INTENT_BAO_ASR_TIMEOUT_SECONDS 覆盖。长音频里的
            # 非语音段（配乐/转场）会让 ASR 长时间不返回，调小可加快置空跳过。
            try:
                timeout_seconds = float(
                    os.environ.get("INTENT_BAO_ASR_TIMEOUT_SECONDS") or 180
                )
            except (TypeError, ValueError):
                timeout_seconds = 180.0
        timeout_seconds = max(10.0, float(timeout_seconds))
        self.base_url = (base_url or "").rstrip("/")
        self.model_name = model_name
        self._timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(timeout_seconds, 10),
        )
        self._transport = transport
        self._total_timeout_seconds = max(0.01, timeout_seconds)
        self._check_cancelled: Callable[[], None] | None = None
        self._on_progress: Callable[[dict[str, Any]], None] | None = None

    def set_runtime_hooks(
        self,
        *,
        check_cancelled: Callable[[], None] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._check_cancelled = check_cancelled
        self._on_progress = on_progress

    async def _post_transcription(
        self, audio_path: Path, *, language: str | None, hotwords: list[str]
    ) -> Any:
        started = time.monotonic()
        deadline = started + self._total_timeout_seconds

        def report_activity(phase: str) -> None:
            if self._check_cancelled:
                self._check_cancelled()
            if self._on_progress:
                self._on_progress({
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "attempt": 1,
                    "phase": phase,
                    "timeout_seconds": self._total_timeout_seconds,
                })

        report_activity("requesting")
        with audio_path.open("rb") as audio_file:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self._timeout,
                trust_env=False, transport=self._transport,
            ) as client:
                pending = asyncio.create_task(client.post(
                    "/v1/audio/transcriptions",
                    files={"file": (audio_path.name, audio_file, "audio/wav")},
                    data={
                        "model": self.model_name, "language": language or "zh",
                        "hotwords": ",".join(hotwords), "response_format": "json",
                    },
                ))
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AsrError(
                                f"ASR转写超过总耗时上限（{self._total_timeout_seconds:g}秒），已停止等待",
                                category="timeout", retryable=True,
                            )
                        done, _ = await asyncio.wait({pending}, timeout=min(1.0, remaining))
                        report_activity("waiting")
                        if done:
                            response = pending.result()
                            response.raise_for_status()
                            return response.json()
                finally:
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

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
            return AsrError("ASR转写超时，请确认模型服务已启动", category="timeout", retryable=True)
        if isinstance(exc, httpx.ConnectError):
            return AsrError("无法连接本地ASR服务，请检查地址和端口", category="connection", retryable=True)
        if isinstance(exc, httpx.HTTPStatusError):
            # Only inspect provider details to categorize them; never persist
            # the raw response, which can contain transcript or file contents.
            details = exc.response.text[:2000].lower()
            if any(marker in details for marker in ("audio too short", "audio is too short", "input is too short", "n_frames must", "zero-length", "empty audio")):
                return AsrError("ASR输入音频过短或为空，请扩展有效音频窗口后重试", category="audio_too_short", retryable=True)
            return AsrError(
                f"ASR服务返回HTTP {exc.response.status_code}",
                category="http",
                retryable=exc.response.status_code >= 500,
            )
        if isinstance(exc, httpx.RequestError):
            return AsrError("ASR请求失败，请检查本地服务状态", category="connection", retryable=True)
        if isinstance(exc, OSError):
            return AsrError("ASR音频文件无法读取，请检查切片结果", category="file_error")
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
            body = asyncio.run(self._post_transcription(audio_path, language=language, hotwords=hotwords))
        except (
            httpx.RequestError,
            httpx.HTTPStatusError,
            json.JSONDecodeError,
            ValueError,
            OSError,
        ) as exc:
            raise self._safe_error(exc) from exc
        finally:
            logger.info(
                "LlamaCpp ASR 请求结束 file=%s",
                audio_path.name,
            )

        segments: list[AsrSegment] = []
        if not isinstance(body, dict) or not ({"text", "segments"} & body.keys()):
            raise AsrError("ASR响应格式无效", category="invalid_response")
        if isinstance(body, dict):
            raw_segments = body.get("segments")
            if isinstance(raw_segments, list):
                for raw in raw_segments:
                    if not isinstance(raw, dict):
                        continue
                    raw_text = raw.get("text")
                    text = _clean_transcript_text(raw_text if isinstance(raw_text, str) else "")
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
            if not segments:
                # llama.cpp server returns a single transcript text:
                # {"type": "transcript.text.done", "text": "...", "usage": {...}}
                raw_text = body.get("text")
                text = _clean_transcript_text(raw_text if isinstance(raw_text, str) else "")
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
            raise AsrError("ASR未返回任何可用的转写片段", category="empty_response", retryable=True)
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
