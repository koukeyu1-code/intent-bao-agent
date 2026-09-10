from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app import media_processing as media


def _showinfo(*pts: int, time_base: str = "1/1000") -> str:
    lines = [
        f"[Parsed_showinfo_2 @ test] config in time_base: {time_base}, frame_rate: 25/1"
    ]
    lines.extend(
        f"[Parsed_showinfo_2 @ test] n: {index:3d} pts: {value:8d} "
        "pts_time:0 fmt:yuv420p"
        for index, value in enumerate(pts)
    )
    return "\n".join(lines)


def _mock_extraction(
    monkeypatch: pytest.MonkeyPatch,
    *,
    log: str,
    filenames: tuple[str, ...] = ("frame-001.jpg", "frame-002.jpg"),
    returncode: int = 0,
) -> None:
    monkeypatch.setattr(media, "_resolve_media_executable", lambda _names: "ffmpeg")

    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess:
        target = Path(command[-1]).parent
        for name in filenames:
            (target / name).write_bytes(name.encode())
        return subprocess.CompletedProcess(command, returncode, "", log)

    monkeypatch.setattr(media.subprocess, "run", run)


def test_key_frames_retain_source_pts_without_an_image_metadata_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_extraction(monkeypatch, log=_showinfo(0, 93_579, time_base="1/90000"))
    frames = media.extract_key_frames(tmp_path / "source.mp4", tmp_path / "frames")

    assert [frame["time_ms"] for frame in frames] == [0, 1040]
    assert all(len(frame["sha256"]) == 64 for frame in frames)


def test_integer_pts_preserve_precision_for_long_recordings() -> None:
    # showinfo's decimal pts_time may round this to 7200.12; integer PTS
    # retains the actual millisecond after rescaling the stream time base.
    assert media._selected_frame_times(_showinfo(648_011_070, time_base="1/90000")) == [
        7_200_123
    ]


@pytest.mark.parametrize(
    "log",
    [
        "",
        "[Parsed_showinfo_2 @ test] n: 0 pts: 0 pts_time:0",
        _showinfo(0, time_base="1/0"),
        _showinfo(0, 1000).replace("n:   1", "n:   2"),
        _showinfo(0, 1000).replace("n:   1", "n:   0"),
    ],
)
def test_missing_or_ambiguous_pts_are_not_estimated(log: str) -> None:
    assert media._selected_frame_times(log) is None


@pytest.mark.parametrize(
    ("log", "filenames", "returncode"),
    [
        (_showinfo(0), ("frame-001.jpg", "frame-002.jpg"), 0),
        (_showinfo(0, 1300, 3100), ("frame-001.jpg", "frame-002.jpg"), 0),
        (_showinfo(0, 1300), ("frame-001.jpg", "frame-003.jpg"), 0),
        (_showinfo(0, 1300), ("frame-001.jpg", "frame-002.jpg"), 1),
    ],
)
def test_incomplete_frame_mapping_or_failed_ffmpeg_does_not_publish_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log: str,
    filenames: tuple[str, ...],
    returncode: int,
) -> None:
    _mock_extraction(monkeypatch, log=log, filenames=filenames, returncode=returncode)
    assert media.extract_key_frames(tmp_path / "source.mp4", tmp_path / "frames") == []


def test_timed_out_extraction_does_not_publish_partial_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media, "_resolve_media_executable", lambda _names: "ffmpeg")

    def timeout(command: list[str], **_kwargs) -> None:
        (Path(command[-1]).parent / "frame-001.jpg").write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 1800)

    monkeypatch.setattr(media.subprocess, "run", timeout)
    assert media.extract_key_frames(tmp_path / "source.mp4", tmp_path / "frames") == []


@pytest.fixture
def ffmpeg() -> str:
    executable = media._resolve_media_executable(("ffmpeg",))
    if executable is None:
        pytest.skip("FFmpeg is unavailable for the real media smoke test")
    return executable


def _run_ffmpeg(ffmpeg: str, arguments: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("source_start_seconds", [0, 5])
def test_real_ffmpeg_scene_times_match_playback_for_nonzero_source_start(
    tmp_path: Path, ffmpeg: str, source_start_seconds: int,
) -> None:
    from PIL import Image

    source = tmp_path / "known-scenes.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=black:s=64x64:r=10:d=4,"
            "drawbox=color=white:t=fill:enable='gte(t,1.3)*lt(t,3.1)'",
            "-vf", f"setpts=PTS+{source_start_seconds}/TB",
            "-fps_mode", "passthrough", "-c:v", "mpeg4", "-bf", "0",
            str(source),
        ],
    )
    # Verify that the fixture really has an absolute nonzero PTS; merely
    # asking the encoder for one does not prove that the muxer preserved it.
    probe = _run_ffmpeg(
        ffmpeg,
        [
            "-copyts", "-i", str(source), "-vf", "showinfo",
            "-frames:v", "1", "-fps_mode", "passthrough", "-f", "null", "-",
        ],
    )
    first_time = re.search(r"\bpts_time:([^\s]+)", probe.stderr)
    assert first_time is not None
    assert float(first_time.group(1)) == source_start_seconds

    frames = media.extract_key_frames(source, tmp_path / "frames")
    assert [frame["time_ms"] for frame in frames] == [0, 1300, 3100]
    for frame, expected_white in zip(frames, [False, True, False]):
        with Image.open(frame["path"]) as image:
            pixel = image.convert("RGB").getpixel((10, 10))
        assert all(value > 245 if expected_white else value < 10 for value in pixel)

    limited = media.extract_key_frames(source, tmp_path / "limited", max_frames=2)
    assert [frame["time_ms"] for frame in limited] == [0, 1300]


def test_real_ffmpeg_static_video_keeps_its_opening_frame(
    tmp_path: Path, ffmpeg: str,
) -> None:
    source = tmp_path / "static.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=64x64:r=10:d=1",
            "-c:v", "mpeg4", str(source),
        ],
    )
    frames = media.extract_key_frames(source, tmp_path / "frames")
    assert len(frames) == 1
    assert frames[0]["time_ms"] == 0
