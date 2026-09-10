import pytest

from app import media_processing as media
from app.model_settings import ModelSettingsStore


def test_missing_ffmpeg_reports_dependency_before_slicing(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_resolve_media_executable", lambda _names: None)
    with pytest.raises(media.MediaProcessingUnavailable, match="FFmpeg") as error:
        media.slice_audio_segment(tmp_path / "input.wav", 0, 1000, tmp_path / "out.wav")
    assert error.value.category == "dependency"
    assert not (tmp_path / "out.wav").exists()


def test_download_error_disguised_as_exe_is_not_a_media_tool(tmp_path):
    fake = tmp_path / "ffprobe.exe"
    fake.write_text('{"error":"Binary not found"}')
    assert not media._usable_executable(fake)


@pytest.mark.skipif(media.os.name != "nt", reason="Windows executable lookup")
def test_bad_path_executable_does_not_hide_bundled_binary(tmp_path, monkeypatch):
    fake = tmp_path / "ffprobe.exe"
    fake.write_text('{"error":"Binary not found"}')
    good_dir = tmp_path / "bundled"
    good_dir.mkdir()
    real = good_dir / "ffprobe.exe"
    real.write_bytes(b"MZ" + b"\0" * 128)
    monkeypatch.setattr(media.shutil, "which", lambda _name: str(fake))
    media._resolve_executable.cache_clear()
    try:
        assert media._resolve_executable(("ffprobe",), (str(good_dir),)) == str(real)
    finally:
        media._resolve_executable.cache_clear()


def test_new_asr_settings_match_bundled_server_port(tmp_path):
    store = ModelSettingsStore(tmp_path, environ={})
    assert store.snapshot().asr.base_url == "http://127.0.0.1:8099"
