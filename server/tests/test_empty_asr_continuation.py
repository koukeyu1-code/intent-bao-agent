"""Empty recognition intervals must not block the remaining meeting pipeline."""

from collections import Counter
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m, services
from app.audio_adapters import AsrError, AsrResult
from app.model_adapters import ModelAdapterError
from app.diarization import SpeakerTurn
from test_media_completeness import (
    pipeline, FakeAsr, FakeIntent, _install_asr, _install_diarization, _transcript,
)
from test_pipeline_recovery import _new_run


@pytest.mark.parametrize("category", ["empty_response", "audio_too_short"])
def test_unrecognized_interval_is_blank_and_other_speech_reaches_intent(pipeline, monkeypatch, category):
    material = pipeline.add_file("partial-empty.wav")
    _install_diarization(monkeypatch)
    calls = Counter()

    def transcribe(path, **_kwargs):
        start = int(path.read_text("utf-8"))
        calls[start] += 1
        if start == 5000:
            raise AsrError("这段音频没有可用正文", category=category, retryable=False)
        return _transcript("需要安排客户样机验证")

    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    assert pipeline.execute(intent).status == "已完成"
    assert calls == {0: 1, 5000: 2}
    rows = pipeline.session.scalars(select(m.TranscriptSegment).order_by(m.TranscriptSegment.order_index)).all()
    assert [(row.start_ms, row.end_ms, row.text) for row in rows] == [
        (0, 3000, "需要安排客户样机验证"), (5000, 8000, ""),
    ]
    assert rows[1].speaker_label is None
    assert len(pipeline.session.scalars(select(m.SpeakerSegment)).all()) == 1
    assert len(pipeline.session.scalars(select(m.SpeakerCluster)).all()) == 1
    assert len(intent.calls) == 1
    evidence_materials = [item for item in intent.calls[0].material_summaries if item.get("type") == "转写片段"]
    assert [item["transcript_segment_id"] for item in evidence_materials] == [rows[0].id]
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert result["transcription"] == {
        "jobs": 2, "ok": 1, "failed": 0, "empty": 1, "reused": 0, "skipped": 1,
    }
    skipped = result["skipped_segments"][0]
    assert skipped == result["files"][0]["skipped_segments"][0]
    assert skipped["material_file_id"] == material.id
    assert skipped["material_name"] == material.safe_name
    assert (skipped["start_ms"], skipped["end_ms"], skipped["category"], skipped["attempts"]) == (5000, 8000, category, 2)
    assert skipped["error"]


def test_more_than_three_empty_intervals_do_not_stop_later_speech(pipeline, monkeypatch):
    material = pipeline.add_file("empty-prefix.wav")
    turns = [SpeakerTurn(index * 2000, index * 2000 + 2000, f"S{index}") for index in range(5)]
    _install_diarization(monkeypatch)
    monkeypatch.setattr(services, "probe_media_duration", lambda _: 10_000)
    monkeypatch.setattr(services, "get_diarizer", lambda _: SimpleNamespace(diarize=lambda *_a, **_kw: turns))
    asr = FakeAsr(lambda path, **_kw: _transcript("最后一段仍然保留") if path.read_text("utf-8") == "8000" else AsrResult([], "asr"))
    _install_asr(monkeypatch, asr)
    done, rows = pipeline.audio([material])
    assert done and len(asr.calls) == 9
    assert [row.text for row in rows] == ["", "", "", "", "最后一段仍然保留"]
    assert json.loads(pipeline.task("音轨与转写").result_json)["transcription"]["skipped"] == 4


def test_retryable_timeout_is_blank_and_pipeline_continues(pipeline, monkeypatch):
    """Retryable timeouts (e.g. music looping the ASR model) blank their
    interval and let the remaining speech reach intent extraction."""
    material = pipeline.add_file("music-intro.wav")
    _install_diarization(monkeypatch)
    calls = Counter()

    def transcribe(path, **_kwargs):
        start = int(path.read_text("utf-8"))
        calls[start] += 1
        if start == 5000:
            raise AsrError("ASR转写超过总耗时上限（60秒），已停止等待", category="timeout", retryable=True)
        return _transcript("需要安排客户样机验证")

    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    assert pipeline.execute(intent).status == "已完成"
    assert calls == {0: 1, 5000: 2}
    rows = pipeline.session.scalars(select(m.TranscriptSegment).order_by(m.TranscriptSegment.order_index)).all()
    assert [(row.start_ms, row.end_ms, row.text) for row in rows] == [
        (0, 3000, "需要安排客户样机验证"), (5000, 8000, ""),
    ]
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert result["transcription"] == {
        "jobs": 2, "ok": 1, "failed": 0, "empty": 0, "reused": 0, "skipped": 1,
    }
    skipped = result["skipped_segments"][0]
    assert skipped["category"] == "timeout"
    assert skipped["attempts"] == 2
    assert len(intent.calls) == 1


def test_three_consecutive_timeouts_still_stop_the_material(pipeline, monkeypatch):
    """A hanging ASR service must not silently blank an entire meeting."""
    material = pipeline.add_file("all-hang.wav")
    turns = [SpeakerTurn(index * 2000, index * 2000 + 2000, f"S{index}") for index in range(5)]
    _install_diarization(monkeypatch)
    monkeypatch.setattr(services, "probe_media_duration", lambda _: 10_000)
    monkeypatch.setattr(services, "get_diarizer", lambda _: SimpleNamespace(diarize=lambda *_a, **_kw: turns))
    calls = Counter()

    def transcribe(path, **_kwargs):
        calls[int(path.read_text("utf-8"))] += 1
        raise AsrError("ASR转写超过总耗时上限（60秒），已停止等待", category="timeout", retryable=True)

    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(intent)
    assert pipeline.run.status == "处理失败" and not intent.calls
    assert calls == {0: 2, 2000: 2, 4000: 2}
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert result["transcription"]["failed"] == 1
    assert result["transcription"]["skipped"] == 3
    assert result["failed_segments"][0]["stopped_early"] is True


def test_intent_output_limit_splits_batch_and_merges(pipeline, monkeypatch):
    """回归：意向批次输出超出 num_predict（output_limit）时，按材料项
    二分重试并合并观察，而不是整场分析硬失败。"""
    for index in range(4):
        pipeline.add_file(f"intent-split-{index}.wav")
    _install_diarization(monkeypatch)
    _install_asr(monkeypatch, FakeAsr(lambda path, **_kw: _transcript()))
    calls = []

    class SplitLimitIntent(FakeIntent):
        def analyze(self, context, output_budget=None):
            size = len(context.material_summaries)
            calls.append(size)
            if size > 2:
                raise ModelAdapterError("达到输出上限", category="output_limit")
            return {"observations": [{"material_id": context.material_summaries[0]["id"]}]}

    intent = SplitLimitIntent()
    assert pipeline.execute(intent).status == "已完成"
    # 初始整批 6 项触发 output_limit；二分后每片 ≤ 2 项成功生成
    # 12 项材料按输出需求切成 2 批（每批 6 项）；每批：6 触发上限 →
    # 3 仍超限 → 二分为 1+2 成功。两批共 14 次调用全部按此形态。
    assert calls.count(6) == 2
    assert calls.count(3) == 4
    assert calls.count(1) == 4
    assert calls.count(2) == 4
    assert max(calls) == 6


def test_completed_retry_reuses_success_and_retries_empty_interval(pipeline, monkeypatch):
    material = pipeline.add_file("retry-empty.wav")
    _install_diarization(monkeypatch)
    calls = Counter()
    recovered = [False]

    def transcribe(path, **_kwargs):
        start = int(path.read_text("utf-8"))
        calls[start] += 1
        return AsrResult([], "asr") if start == 5000 and not recovered[0] else _transcript(f"正文 {start}")

    _install_asr(monkeypatch, FakeAsr(transcribe))
    done, rows = pipeline.audio([material])
    assert done and rows[1].text == ""
    assert len(list((pipeline.settings.data_dir / "temp" / "asr-checkpoints").rglob("*.json"))) == 1
    recovered[0] = True
    _new_run(pipeline)
    done, rows = pipeline.audio([material])
    assert done and [row.text for row in rows] == ["正文 0", "正文 5000"]
    assert calls == {0: 1, 5000: 3}
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert result["transcription"] == {"jobs": 2, "ok": 2, "failed": 0, "empty": 0, "reused": 1, "skipped": 0}
    assert result["skipped_segments"] == []


def test_multiple_files_aggregate_skipped_counts_and_keep_each_source(pipeline, monkeypatch):
    files = [pipeline.add_file("first.wav"), pipeline.add_file("second.wav")]
    _install_diarization(monkeypatch)
    _install_asr(monkeypatch, FakeAsr(lambda path, **_kw: AsrResult([], "asr") if path.read_text("utf-8") == "5000" else _transcript()))
    done, rows = pipeline.audio(files)
    assert done and len(rows) == 4
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert result["transcription"] == {"jobs": 4, "ok": 2, "failed": 0, "empty": 2, "reused": 0, "skipped": 2}
    assert result["files_done"] == result["files_total"] == 2
    assert {item["material_file_id"] for item in result["skipped_segments"]} == {item.id for item in files}
    assert all(item["transcription"]["skipped"] == 1 for item in result["files"])
    assert [row.order_index for row in rows] == [1, 2, 3, 4]


@pytest.mark.parametrize("category,retryable,attempts", [("connection", True, 2), ("timeout", False, 1), ("invalid_response", False, 1)])
def test_infrastructure_failure_after_empty_interval_is_still_failure(pipeline, monkeypatch, category, retryable, attempts):
    material = pipeline.add_file("unavailable.wav")
    _install_diarization(monkeypatch)

    def transcribe(path, **_kwargs):
        if path.read_text("utf-8") == "0":
            return AsrResult([], "asr")
        raise AsrError("服务故障", category=category, retryable=retryable)

    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(intent)
    result = json.loads(pipeline.task("音轨与转写").result_json)
    assert pipeline.run.status == "处理失败" and not intent.calls
    assert result["transcription"]["failed"] == result["transcription"]["skipped"] == 1
    assert result["failed_segments"][0]["category"] == category
    assert result["failed_segments"][0]["attempts"] == attempts
    assert result["skipped_segments"][0]["category"] == "empty_response"
    assert result["skipped_segments"][0]["material_file_id"] == material.id
