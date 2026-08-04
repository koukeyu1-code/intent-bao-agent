from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import Settings

logger = logging.getLogger(__name__)

_VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
_SPEAKER_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"
_CLUSTER_THRESHOLD = 0.45
_MIN_SPEECH_MS = 300


@dataclass
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker_label: str


class DiarizationUnavailable(RuntimeError):
    """Raised when the local diarization stack (funasr + torch) is missing."""


def _resolve_models_dir(settings: Settings) -> Path:
    """Prefer the bundled ``models/funasr`` cache, otherwise a data-dir cache."""
    # data_dir = <repo>/server/data -> repo root is two parents up.
    repo_root = settings.data_dir.parent.parent
    bundled_cache = repo_root / "models" / "funasr"
    if bundled_cache.is_dir():
        return bundled_cache
    return settings.data_dir / "models" / "funasr"


def _normalize_rows(embeddings: list[np.ndarray]) -> np.ndarray:
    matrix = np.vstack(
        [np.asarray(item, dtype=np.float32).reshape(-1) for item in embeddings]
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class FunAsrDiarizer:
    """Local speaker diarization via FunASR fsmn-vad + cam++ embeddings.

    Pipeline: VAD splits speech into turns -> cam++ extracts a speaker
    embedding per turn -> agglomerative clustering groups turns by voice.
    Models are loaded lazily once and cached for the worker process.
    Runs on CPU so it never competes with Ollama's GPU context.
    """

    def __init__(self, models_dir: Path, device: str = "cpu") -> None:
        self._models_dir = Path(models_dir)
        self._device = device
        self._lock = threading.Lock()
        self._vad: Any | None = None
        self._speaker_model: Any | None = None
        self._loaded = False
        self._load_failed: str | None = None

    @property
    def available(self) -> bool:
        if self._loaded:
            return True
        try:
            import funasr  # noqa: F401
        except Exception:
            return False
        return True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._load_failed is not None:
            raise DiarizationUnavailable(self._load_failed)
        with self._lock:
            if self._loaded:
                return
            try:
                from modelscope import snapshot_download
                from funasr import AutoModel
            except Exception as exc:
                self._load_failed = f"funasr不可用：{exc}"
                raise DiarizationUnavailable(self._load_failed) from exc
            try:
                self._models_dir.mkdir(parents=True, exist_ok=True)
                os.environ.setdefault("MODELSCOPE_CACHE", str(self._models_dir))
                vad_path = snapshot_download(
                    _VAD_MODEL_ID,
                    cache_dir=str(self._models_dir),
                )
                spk_path = snapshot_download(
                    _SPEAKER_MODEL_ID,
                    cache_dir=str(self._models_dir),
                )
                self._vad = AutoModel(
                    model=vad_path,
                    disable_update=True,
                    device=self._device,
                )
                self._speaker_model = AutoModel(
                    model=spk_path,
                    disable_update=True,
                    device=self._device,
                )
                self._loaded = True
                logger.info("FunASR说话人分离模型已加载（CPU）")
            except Exception as exc:
                self._load_failed = f"说话人分离模型加载失败：{exc}"
                logger.exception("说话人分离模型加载失败")
                raise DiarizationUnavailable(self._load_failed) from exc

    def _load_audio(self, path: Path):
        try:
            import librosa

            audio, sr = librosa.load(path, sr=16000, mono=True)
            return audio, sr
        except Exception as exc:
            raise DiarizationUnavailable(f"音频读取失败：{exc}") from exc

    def _speech_segments(self, path: Path) -> list[list[int]]:
        result = self._vad.generate(input=str(path), batch_size_s=60)
        raw = result[0].get("value") if result else []
        return [
            [int(item[0]), int(item[1])]
            for item in raw
            if int(item[1]) - int(item[0]) >= _MIN_SPEECH_MS
        ]

    def _embedding_for_turn(
        self, audio: np.ndarray, sr: int, start_ms: int, end_ms: int
    ) -> np.ndarray:
        import soundfile as sf

        clip = audio[int(start_ms / 1000 * sr): int(end_ms / 1000 * sr)]
        if clip.size == 0:
            raise DiarizationUnavailable("说话人片段为空")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            sf.write(tmp_path, clip, sr)
            result = self._speaker_model.generate(input=tmp_path)[0]
            embedding = result["spk_embedding"]
            if hasattr(embedding, "cpu"):
                embedding = embedding.cpu().numpy()
            return np.asarray(embedding, dtype=np.float32)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def diarize(
        self,
        audio_path: Path,
        *,
        duration_ms: int | None = None,
    ) -> list[SpeakerTurn]:
        """Return speech turns grouped by speaker (``Speaker A/B/C``)."""
        self._ensure_loaded()
        if self._vad is None or self._speaker_model is None:
            raise DiarizationUnavailable("说话人分离模型未加载")
        audio, sr = self._load_audio(audio_path)
        segments = self._speech_segments(audio_path)
        if len(segments) < 1:
            return []
        embeddings = [
            self._embedding_for_turn(audio, sr, start_ms, end_ms)
            for start_ms, end_ms in segments
        ]
        if len(segments) == 1:
            labels = [0]
        else:
            from sklearn.cluster import AgglomerativeClustering

            matrix = _normalize_rows(embeddings)
            clustering = AgglomerativeClustering(
                n_clusters=None,
                metric="cosine",
                linkage="average",
                distance_threshold=_CLUSTER_THRESHOLD,
            ).fit(matrix)
            labels = [int(item) for item in clustering.labels_]

        first_seen: dict[int, int] = {}
        ordered: list[int] = []
        for label in labels:
            if label not in first_seen:
                first_seen[label] = len(ordered)
                ordered.append(label)
        turns: list[SpeakerTurn] = []
        for (start_ms, end_ms), label in zip(segments, labels):
            turns.append(
                SpeakerTurn(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_label=f"Speaker {chr(65 + first_seen[label])}",
                )
            )
        return sorted(turns, key=lambda item: item.start_ms)


_diarizer_singleton: FunAsrDiarizer | None = None


def get_diarizer(settings: Settings) -> FunAsrDiarizer | None:
    """Return the process-wide diarizer, or None when unavailable.

    Auto-enables only when the workspace model cache already exists, so
    disposable test environments keep falling back to whole-file
    transcription without downloading FunASR weights.
    """
    global _diarizer_singleton
    if _diarizer_singleton is not None:
        return _diarizer_singleton
    models_dir = _resolve_models_dir(settings)
    if not (models_dir / "models").is_dir():
        return None
    try:
        diarizer = FunAsrDiarizer(models_dir)
        if not diarizer.available:
            return None
        _diarizer_singleton = diarizer
        return diarizer
    except Exception:
        logger.exception("说话人分离适配器初始化失败")
        return None
