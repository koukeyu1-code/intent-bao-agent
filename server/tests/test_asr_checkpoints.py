"""Malformed private checkpoints must be misses, so retries can recover them."""

import json
from dataclasses import asdict

import pytest

from app.asr_checkpoints import AsrCheckpoints
from app.audio_adapters import AsrSegment


@pytest.fixture
def checkpoint(tmp_path):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"test audio")
    store = AsrCheckpoints(tmp_path / "cache", source, config={"model": "test"},
                           run_id="test-run", check_cancelled=lambda: None)
    return store


@pytest.mark.parametrize("value", [None, [], "invalid", 42, True])
def test_non_object_checkpoint_is_a_cache_miss(checkpoint, value):
    job = (0, 1000, "A")
    checkpoint._path(job).write_text(json.dumps(value), encoding="utf-8")
    assert checkpoint.load(job) is None


@pytest.mark.parametrize("invalid_row", [
    None, [], {"text": None}, {"text": "  "},
    {"start_ms": True}, {"end_ms": "1000"}, {"end_ms": 1001},
    {"confidence": None}, {"confidence": "0.9"}, {"confidence": float("nan")}, {"confidence": 10 ** 1000},
    {"speaker_label": []}, {"language": 4},
])
def test_invalid_segment_is_retried_and_can_be_replaced(checkpoint, invalid_row):
    job = (0, 1000, "A")
    valid = AsrSegment(0, 1000, "保留原始发言", "A", .9, "zh")
    checkpoint.save(job, [valid])
    path = checkpoint._path(job)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["segments"] = [({**asdict(valid), **invalid_row}
                            if isinstance(invalid_row, dict) else invalid_row)]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert checkpoint.load(job) is None
    checkpoint.save(job, [valid])
    assert checkpoint.load(job) == [valid]
