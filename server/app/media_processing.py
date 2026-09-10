from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PdfPageText:
    page_no: int
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class SubtitleSegment:
    start_ms: int
    end_ms: int
    text: str


_WINGET_PACKAGES = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
# Repo-root bundled tools (e.g. models/ffmpeg/bin) so deployments without a
# system-wide install or winget still resolve media binaries.
_PROJECT_TOOL_DIRS = (
    str(Path(__file__).resolve().parents[2] / "models" / "ffmpeg" / "bin"),
)
_COMMON_EXTRA_DIRS = (
    "C:/Program Files/Tesseract-OCR",
    "C:/Program Files/FFmpeg/bin",
) + _PROJECT_TOOL_DIRS


class MediaProcessingUnavailable(RuntimeError):
    """A required local media tool is missing; retrying slices cannot help."""

    category = "dependency"


def _usable_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() == ".exe":
        try:
            with path.open("rb") as handle:
                return handle.read(2) == b"MZ"
        except OSError:
            return False
    return True


def _resolve_media_executable(names: tuple[str, ...]) -> str | None:
    """ffmpeg/ffprobe lookup: PATH, common dirs, bundled repo dir, WinGet."""
    return _resolve_executable(names, _COMMON_EXTRA_DIRS)


@functools.lru_cache(maxsize=32)
def _resolve_executable(
    names: tuple[str, ...], extra_dirs: tuple[str, ...] = ()
) -> str | None:
    """Resolve an executable from PATH, common dirs, or the WinGet store."""

    suffix = ".exe" if os.name == "nt" else ""
    for name in names:
        found = shutil.which(name)
        if found and _usable_executable(Path(found)):
            return found
    for directory in extra_dirs:
        for name in names:
            candidate = Path(directory) / f"{name}{suffix}"
            if _usable_executable(candidate):
                return str(candidate)
    if _WINGET_PACKAGES.is_dir():
        for name in names:
            matches = list(_WINGET_PACKAGES.rglob(f"{name}{suffix}"))
            for candidate in matches:
                if _usable_executable(candidate):
                    return str(candidate)
    return None


def ffprobe_available() -> bool:
    return _resolve_media_executable(("ffprobe",)) is not None


def ffmpeg_available() -> bool:
    return _resolve_media_executable(("ffmpeg",)) is not None


def tesseract_available() -> bool:
    return (
        _resolve_executable(("tesseract",), _COMMON_EXTRA_DIRS) is not None
    )


def ocr_image(path: Path, *, lang: str = "chi_sim+eng") -> str | None:
    """OCR a key frame with Tesseract; returns None when unavailable."""

    tesseract = _resolve_executable(("tesseract",), _COMMON_EXTRA_DIRS)
    if tesseract is None:
        return None
    try:
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = tesseract
        result = pytesseract.image_to_string(
            Image.open(path), lang=lang
        ).strip()
        return result or None
    except Exception:
        return None


def probe_media_duration(path: Path) -> int | None:
    """Return duration in milliseconds using ffprobe, or None if unavailable."""

    ffprobe = _resolve_media_executable(("ffprobe",))
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        seconds = float(value)
        if seconds < 0:
            return None
        return int(seconds * 1000)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def extract_pdf_text(path: Path, *, max_pages: int = 100) -> list[PdfPageText]:
    """Extract per-page text from a PDF using PyMuPDF."""

    try:
        import fitz  # pymupdf
    except ImportError as exc:  # pragma: no cover - dependency guarded
        raise RuntimeError("未安装pymupdf，无法解析PDF文本") from exc

    pages: list[PdfPageText] = []
    with fitz.open(path) as document:
        if document.page_count > max_pages:
            raise ValueError(
                f"PDF页数超过上限（最多{max_pages}页），请拆分后上传"
            )
        for index, page in enumerate(document, start=1):
            text = page.get_text("text").strip()
            pages.append(PdfPageText(page_no=index, text=text))
    return pages


def pdf_is_scanned(path: Path, *, text_ratio: float = 0.005) -> bool | None:
    """Heuristic: a PDF with almost no extractable text is likely scanned.

    Returns None when the PDF cannot be read (treated as unknown).
    """

    try:
        pages = extract_pdf_text(path)
    except (OSError, ValueError, RuntimeError):
        return None
    total_chars = sum(page.char_count for page in pages)
    total_bytes = path.stat().st_size if path.exists() else 0
    if total_bytes <= 0:
        return None
    return (total_chars / total_bytes) < text_ratio


_SRT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})"
)
_VTT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})"
)


def _time_to_ms(match: re.Match[str]) -> int:
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int(match.group(4).ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def parse_srt(text: str) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = next(
            (line for line in lines if "-->" in line), None
        )
        if not time_line:
            continue
        times = _SRT_TIME_RE.findall(time_line)
        if len(times) < 2:
            continue
        start = _time_to_ms(_SRT_TIME_RE.search(time_line))
        end_match = _SRT_TIME_RE.search(time_line, _SRT_TIME_RE.search(time_line).end())
        if end_match is None:
            continue
        end = _time_to_ms(end_match)
        content = " ".join(
            line for line in lines if "-->" not in line and not line.isdigit()
        ).strip()
        if content:
            segments.append(
                SubtitleSegment(start_ms=start, end_ms=end, text=content)
            )
    return segments


def parse_vtt(text: str) -> list[SubtitleSegment]:
    segments: list[SubtitleSegment] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next(
            (line for line in lines if "-->" in line), None
        )
        if not time_line:
            continue
        matches = list(_VTT_TIME_RE.finditer(time_line))
        if len(matches) < 2:
            continue
        start = _time_to_ms(matches[0])
        end = _time_to_ms(matches[1])
        content = " ".join(
            line for line in lines if "-->" not in line
        ).strip()
        if content:
            segments.append(
                SubtitleSegment(start_ms=start, end_ms=end, text=content)
            )
    return segments


def parse_subtitle(path: Path) -> list[SubtitleSegment]:
    """Parse SRT or VTT files into timed segments."""

    suffix = path.suffix.lower()
    if suffix not in {".srt", ".vtt"}:
        raise ValueError(f"不支持的字幕格式：{suffix}")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix == ".srt":
        return parse_srt(text)
    return parse_vtt(text)


def extract_audio_track(
    source: Path, target: Path, *, sample_rate: int = 16000
) -> bool:
    """Extract 16kHz mono WAV audio from a media file via FFmpeg."""

    ffmpeg = _resolve_media_executable(("ffmpeg",))
    if ffmpeg is None:
        return False
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        return result.returncode == 0 and target.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def slice_audio_segment(
    source: Path,
    start_ms: int,
    end_ms: int,
    target: Path,
) -> bool:
    """Slice [start_ms, end_ms) from an audio file into a 16 kHz mono WAV."""
    ffmpeg = _resolve_media_executable(("ffmpeg",))
    if ffmpeg is None:
        raise MediaProcessingUnavailable(
            "未找到有效的 FFmpeg，无法切分录音；请安装 FFmpeg，"
            "或将 ffmpeg.exe 与 ffprobe.exe 放到 models/ffmpeg/bin 后重启后端。"
        )
    duration_ms = max(0, end_ms - start_ms)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-t",
                f"{duration_ms / 1000:.3f}",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and target.is_file() and target.stat().st_size > 0


def extract_key_frames(
    source: Path,
    target_dir: Path,
    *,
    max_frames: int = 20,
    scene_threshold: float = 0.4,
) -> list[dict[str, Any]]:
    """Extract scene-change frames with their original playback timestamps.

    Returns an empty list if extraction or the frame/timestamp mapping fails.
    """

    ffmpeg = _resolve_media_executable(("ffmpeg",))
    if ffmpeg is None or max_frames <= 0:
        return []
    target_dir.mkdir(parents=True, exist_ok=True)
    # A previous run may have left stale frames in the shared derived
    # directory; remove only the frame-*.jpg pattern so the extracted set is
    # exactly the current run's scene changes.
    for stale in target_dir.glob("frame-*.jpg"):
        stale.unlink(missing_ok=True)
    pattern = str(target_dir / "frame-%03d.jpg")
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "info",
                "-nostats",
                # Preserve the source timeline, shifting only the container's
                # start offset to match player.currentTime for this file.
                "-copyts",
                "-start_at_zero",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                (
                    # Keep the opening frame even for a static screen share.
                    f"select='eq(n,0)+gt(scene,{scene_threshold})',"
                    # Limit before showinfo so buffered, unencoded frames
                    # cannot produce extra timestamp records.
                    f"trim=end_frame={max_frames},"
                    "showinfo=checksum=0,scale=960:-2"
                ),
                # One selected frame must produce exactly one numbered JPG;
                # CFR would duplicate/drop frames across gaps between scenes.
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(max_frames),
                "-start_number",
                "1",
                "-q:v",
                "3",
                pattern,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    frame_times = _selected_frame_times(result.stderr)
    images = sorted(target_dir.glob("frame-*.jpg"))
    if frame_times is None or len(frame_times) != len(images):
        return []
    frames: list[dict[str, Any]] = []
    for index, (image, time_ms) in enumerate(zip(images, frame_times), start=1):
        if image.name != f"frame-{index:03d}.jpg":
            return []
        frames.append(
            {
                "path": image,
                "time_ms": time_ms,
                "sha256": _file_sha256(image),
            }
        )
    return frames


_SHOWINFO_TIME_BASE_RE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_SHOWINFO_FRAME_RE = re.compile(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:")


def _selected_frame_times(log: str) -> list[int] | None:
    """Read the same filter's PTS, never infer source time from JPG metadata.

    Integer PTS and the rational time base retain millisecond precision even
    for long recordings where showinfo's decimal pts_time is rounded.
    """

    time_base: Fraction | None = None
    times: list[int] = []
    for line in log.splitlines():
        if "showinfo" not in line:
            continue
        config = _SHOWINFO_TIME_BASE_RE.search(line)
        if config:
            numerator, denominator = map(int, config.groups())
            if numerator <= 0 or denominator <= 0:
                return None
            time_base = Fraction(numerator, denominator)
            continue
        frame = _SHOWINFO_FRAME_RE.search(line)
        if frame:
            index, pts = map(int, frame.groups())
            if time_base is None or index != len(times):
                return None
            times.append(round(pts * time_base * 1000))
    return times if time_base is not None else None


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
