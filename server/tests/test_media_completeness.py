"""Regression coverage for complete, attributable media processing."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app import services
from app.audio_adapters import AsrError, AsrResult, AsrSegment
from app.config import Settings
from app.database import Database
from app.diarization import DiarizationCancelled, SpeakerTurn


@dataclass
class MediaPipeline:
    session: Session
    settings: Settings
    package: m.MeetingPackage
    run: m.AnalysisRun

    def add_file(self, name: str, material_type: str = "录音") -> m.MaterialFile:
        relative = f"projects/{self.package.project_id}/{self.package.id}/{name}"
        path = self.settings.data_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
        material = m.MaterialFile(
            package_id=self.package.id,
            original_name=name,
            safe_name=name,
            material_type=material_type,
            relative_path=relative,
            sha256="a" * 64,
            size_bytes=path.stat().st_size,
            parse_status="预检通过",
        )
        self.session.add(material)
        self.session.commit()
        return material

    def task(self, stage: str) -> m.ProcessingTask:
        task = self.session.scalar(
            select(m.ProcessingTask).where(
                m.ProcessingTask.analysis_run_id == self.run.id,
                m.ProcessingTask.stage == stage,
            )
        )
        assert task is not None
        return task

    def audio(self, files: list[m.MaterialFile]):
        return services._run_audio_stage(
            self.session, self.settings, self.run, self.package, files,
            _asr_settings(),
        )

    def execute(self, adapter):
        return services.execute_analysis_run(
            self.session, self.run, settings=self.settings, adapter=adapter,
            asr_settings=_asr_settings(),
        )


def _asr_settings():
    return SimpleNamespace(
        enabled=True, provider="llamacpp", base_url="http://127.0.0.1:8099",
        model_name="test-asr",
    )


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = Database(f"sqlite:///{(tmp_path / 'media.db').as_posix()}")
    database.create_all()
    settings = Settings(
        server_dir=tmp_path, data_dir=tmp_path / "data",
        database_url=str(database.engine.url),
    )
    monkeypatch.setattr(services, "probe_media_duration", lambda _path: 8_000)
    monkeypatch.setattr(services, "get_diarizer", lambda _settings: None)
    with database.session() as session:
        project = m.Project(name="Media regression")
        customer = m.Customer(code="media", name="Media", display_name="Media")
        session.add_all([project, customer])
        session.flush()
        package = m.MeetingPackage(
            project_id=project.id, customer_id=customer.id,
            title="Media regression", meeting_date=date(2026, 9, 8),
            status="分析中",
        )
        session.add(package)
        session.flush()
        run = m.AnalysisRun(
            package_id=package.id, adapter="ollama", model_name="test-intent",
            status="运行中",
        )
        session.add(run)
        session.flush()
        session.add_all([
            m.ProcessingTask(
                package_id=package.id, analysis_run_id=run.id,
                stage=stage, stage_order=index,
            )
            for index, stage in enumerate(services.TASK_STAGES, 1)
        ])
        session.commit()
        yield MediaPipeline(session, settings, package, run)
    database.engine.dispose()


class FakeAsr:
    model_name = "test-asr"

    def __init__(self, transcribe=None):
        self.calls: list[Path] = []
        self._transcribe = transcribe

    def transcribe(self, path: Path, **kwargs):
        self.calls.append(path)
        if self._transcribe is not None:
            return self._transcribe(path, **kwargs)
        return _transcript(path.read_text("utf-8"), kwargs["duration_ms"])


class FakeIntent:
    name = "ollama"
    model_name = "test-intent"

    def __init__(self):
        self.calls = []

    def analyze(self, context):
        self.calls.append(context)
        return {"observations": []}


def _transcript(text: str = "保留这一段发言", duration_ms: int = 3_000):
    return AsrResult(
        segments=[AsrSegment(
            start_ms=0, end_ms=duration_ms, text=text,
            speaker_label="SPEAKER_00", confidence=0.9, language="zh",
        )],
        model_name="test-asr",
    )


def _install_asr(monkeypatch, adapter):
    monkeypatch.setattr(services, "get_asr_adapter", lambda *_a, **_kw: adapter)


def _install_diarization(monkeypatch):
    turns = [
        SpeakerTurn(0, 3_000, "SPEAKER_00"),
        SpeakerTurn(5_000, 8_000, "SPEAKER_01"),
    ]
    monkeypatch.setattr(
        services, "get_diarizer",
        lambda _settings: SimpleNamespace(diarize=lambda *_a, **_kw: turns),
    )

    def slice_segment(_source, start_ms, _end_ms, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(start_ms), encoding="utf-8")
        return True

    monkeypatch.setattr(services, "slice_audio_segment", slice_segment)
    return turns


@pytest.mark.parametrize("second_type", ["录音", "录屏"])
def test_every_audio_source_is_transcribed_with_distinct_speakers(
    pipeline, monkeypatch, second_type,
):
    files = [
        pipeline.add_file("first.wav"),
        pipeline.add_file(
            "second.mp4" if second_type == "录屏" else "second.wav",
            second_type,
        ),
    ]
    adapter = FakeAsr()
    _install_asr(monkeypatch, adapter)
    extracted = []

    def extract(source, target):
        extracted.append(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return True

    monkeypatch.setattr(services, "extract_audio_track", extract)
    done, transcripts = pipeline.audio(files)
    pipeline.session.commit()

    assert done is True
    assert len(adapter.calls) == 2
    assert {item.material_file_id for item in transcripts} == {
        item.id for item in files
    }
    assert {item.text for item in transcripts} == {item.safe_name for item in files}
    assert len({item.speaker_label for item in transcripts}) == 2
    assert all(item.speaker_label and len(item.speaker_label) <= 32 for item in transcripts)
    assert len(pipeline.session.scalars(select(m.SpeakerCluster)).all()) == 2
    assert all(item.parse_status == "解析完成" for item in files)
    assert len(extracted) == (1 if second_type == "录屏" else 0)
    task = pipeline.task("音轨与转写")
    assert (task.status, task.progress) == ("已完成", 100)
    assert {item["material_file_id"] for item in json.loads(task.result_json)["files"]} == {
        item.id for item in files
    }


def test_all_recordings_keep_their_own_key_frames(pipeline, monkeypatch):
    files = [
        pipeline.add_file("screen-1.mp4", "录屏"),
        pipeline.add_file("screen-2.mp4", "录屏"),
    ]
    destinations = []

    def extract(source, target, **_kwargs):
        destinations.append(target)
        target.mkdir(parents=True, exist_ok=True)
        path = target / "frame_0001.jpg"
        path.write_bytes(source.read_bytes())
        return [{"path": path, "time_ms": 1_000, "sha256": "a" * 64}]

    monkeypatch.setattr(services, "extract_key_frames", extract)
    monkeypatch.setattr(services, "ocr_image", lambda path: path.read_text("utf-8"))
    frames = services._run_visual_stage(
        pipeline.session, pipeline.settings, pipeline.run, pipeline.package, files,
    )
    pipeline.session.commit()

    assert {item.material_file_id for item in frames} == {item.id for item in files}
    assert len(set(destinations)) == 2
    assert len({item.image_relative_path for item in frames}) == 2
    for frame in frames:
        source = next(item for item in files if item.id == frame.material_file_id)
        stored_image = pipeline.settings.data_dir / frame.image_relative_path
        assert stored_image.read_text("utf-8") == source.safe_name
        assert frame.ocr_text == source.safe_name
    assert pipeline.task("画面与PDF解析").status == "已完成"


@pytest.mark.parametrize("all_fail", [False, True])
def test_segment_asr_failures_fail_run_and_keep_diagnostics(
    pipeline, monkeypatch, all_fail,
):
    material = pipeline.add_file("partial.wav")
    turns = _install_diarization(monkeypatch)
    attempts = Counter()

    def transcribe(path, **_kwargs):
        start = int(path.read_text("utf-8"))
        attempts[start] += 1
        if all_fail or start == 5_000:
            raise AsrError("受控 ASR 失败")
        return _transcript()

    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(intent)
    pipeline.session.refresh(pipeline.run)
    pipeline.session.refresh(material)

    task = pipeline.task("音轨与转写")
    assert pipeline.run.status == "处理失败"
    assert task.status == "处理失败" and task.progress < 100
    assert material.parse_status == "解析失败"
    assert material.safe_name in task.error_message
    failures = json.loads(task.result_json)["failed_segments"]
    failed_turns = turns if all_fail else turns[1:]
    assert {(item["start_ms"], item["end_ms"]) for item in failures} == {
        (turn.start_ms, turn.end_ms) for turn in failed_turns
    }
    assert all(item["material_file_id"] == material.id for item in failures)
    assert all(item["attempts"] == 2 and item["error"] for item in failures)
    assert all(attempts[turn.start_ms] == 2 for turn in failed_turns)
    assert intent.calls == []
    assert pipeline.session.scalars(select(m.RawIntentObservation)).all() == []


def test_slice_failure_fails_run_instead_of_skipping_speech(pipeline, monkeypatch):
    material = pipeline.add_file("cannot-slice.wav")
    turns = _install_diarization(monkeypatch)
    attempts = Counter()

    def fail_slice(_source, start_ms, _end_ms, _target):
        attempts[start_ms] += 1
        return False

    monkeypatch.setattr(services, "slice_audio_segment", fail_slice)
    adapter = FakeAsr()
    _install_asr(monkeypatch, adapter)
    intent = FakeIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(intent)

    assert adapter.calls == [] and intent.calls == []
    assert pipeline.run.status == "处理失败"
    task = pipeline.task("音轨与转写")
    assert task.status == "处理失败" and task.progress < 100
    failures = json.loads(task.result_json)["failed_segments"]
    assert {item["start_ms"] for item in failures} == {turn.start_ms for turn in turns}
    assert all(item["material_file_id"] == material.id for item in failures)
    assert all(item["attempts"] == 2 for item in failures)
    assert all(attempts[turn.start_ms] == 2 for turn in turns)


@pytest.mark.parametrize("retry_stage", ["asr", "slice"])
def test_transient_segment_failure_retries_and_completes(
    pipeline, monkeypatch, retry_stage,
):
    material = pipeline.add_file("retry.wav")
    turns = _install_diarization(monkeypatch)
    attempts = Counter()

    def transcribe(path, **_kwargs):
        start = int(path.read_text("utf-8"))
        if retry_stage == "asr":
            attempts[start] += 1
            if attempts[start] == 1:
                raise AsrError("暂时失败")
        return _transcript(f"发言 {start}")

    if retry_stage == "slice":
        def slice_once(_source, start_ms, _end_ms, target):
            attempts[start_ms] += 1
            if attempts[start_ms] == 1:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(start_ms), encoding="utf-8")
            return True

        monkeypatch.setattr(services, "slice_audio_segment", slice_once)
    _install_asr(monkeypatch, FakeAsr(transcribe))
    intent = FakeIntent()
    result = pipeline.execute(intent)

    assert result.status == "已完成"
    assert len(intent.calls) == 1
    assert all(attempts[turn.start_ms] == 2 for turn in turns)
    transcripts = pipeline.session.scalars(select(m.TranscriptSegment)).all()
    assert {(item.start_ms, item.end_ms) for item in transcripts} == {
        (turn.start_ms, turn.end_ms) for turn in turns
    }
    assert len(transcripts) == 2
    assert all(item.material_file_id == material.id for item in transcripts)
    task = pipeline.task("音轨与转写")
    assert (task.status, task.progress) == ("已完成", 100)
    assert not json.loads(task.result_json).get("failed_segments")


@pytest.mark.parametrize("has_blank_segment", [False, True])
def test_empty_segment_transcription_is_not_success(
    pipeline, monkeypatch, has_blank_segment,
):
    pipeline.add_file("empty.wav")
    _install_diarization(monkeypatch)
    empty_result = _transcript("  ") if has_blank_segment else AsrResult([], "test-asr")
    adapter = FakeAsr(lambda *_a, **_kw: empty_result)
    _install_asr(monkeypatch, adapter)
    intent = FakeIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(intent)

    assert pipeline.run.status == "处理失败"
    task = pipeline.task("音轨与转写")
    assert task.status == "处理失败" and task.progress < 100
    assert len(json.loads(task.result_json)["failed_segments"]) == 2
    assert len(adapter.calls) == 4
    assert intent.calls == []


@pytest.mark.parametrize("cancellation_stage", ["diarization", "asr", "slice"])
def test_cancellation_during_media_work_stops_without_retry_or_fallback(
    pipeline, monkeypatch, cancellation_stage,
):
    material = pipeline.add_file("cancel.wav")
    _install_diarization(monkeypatch)
    calls = []

    def cancel(*_a, **_kw):
        calls.append(cancellation_stage)
        services.request_analysis_run_cancellation(pipeline.session, pipeline.run)
        if cancellation_stage == "diarization":
            raise DiarizationCancelled()
        raise services._AnalysisCancellationRequested()

    adapter = FakeAsr(cancel if cancellation_stage == "asr" else None)
    _install_asr(monkeypatch, adapter)
    if cancellation_stage == "diarization":
        monkeypatch.setattr(
            services, "get_diarizer",
            lambda _settings: SimpleNamespace(diarize=cancel),
        )
    elif cancellation_stage == "slice":
        monkeypatch.setattr(services, "slice_audio_segment", cancel)
    intent = FakeIntent()
    result = pipeline.execute(intent)

    assert result.status == "已取消"
    assert calls == [cancellation_stage]
    assert len(adapter.calls) == (1 if cancellation_stage == "asr" else 0)
    assert intent.calls == []
    assert pipeline.task("音轨与转写").status == "已取消"
    assert material.parse_status != "解析完成"
    assert pipeline.session.scalars(select(m.TranscriptSegment)).all() == []


def test_missing_frames_fail_the_visual_stage(pipeline, monkeypatch):
    material = pipeline.add_file("no-frames.mp4", "录屏")
    monkeypatch.setattr(services, "extract_key_frames", lambda *_a, **_kw: [])
    with pytest.raises(services.DomainError):
        services._run_visual_stage(
            pipeline.session, pipeline.settings, pipeline.run, pipeline.package,
            [material],
        )
    assert pipeline.task("画面与PDF解析").status == "处理失败"
    assert material.parse_status == "解析失败"


def test_all_audio_transcripts_reach_intent_analysis(pipeline, monkeypatch):
    files = [pipeline.add_file("first.wav"), pipeline.add_file("second.wav")]
    asr = FakeAsr()
    _install_asr(monkeypatch, asr)
    intent = FakeIntent()

    result = pipeline.execute(intent)

    assert result.status == "已完成"
    assert len(asr.calls) == 2
    assert len(intent.calls) == 1
    transcripts = [
        item for item in intent.calls[0].material_summaries
        if item.get("type") == "转写片段"
    ]
    assert len(transcripts) == 2
    assert {item["source_material_file_id"] for item in transcripts} == {
        material.id for material in files
    }
    for material in files:
        item = next(
            item for item in transcripts
            if item["source_material_file_id"] == material.id
        )
        assert material.safe_name in item["content"]
    assert pipeline.task("音轨与转写").progress == 100


def test_same_audio_file_uses_distinct_speaker_clusters_in_each_analysis_run(
    pipeline, monkeypatch,
):
    material = pipeline.add_file("same-recording.wav")
    asr = FakeAsr()
    _install_asr(monkeypatch, asr)
    done, first_transcripts = pipeline.audio([material])
    assert done is True
    first_speaker = pipeline.session.scalar(select(m.SpeakerCluster))
    assert first_speaker is not None
    first_speaker.status = "已确认"
    first_speaker.confirmed_name = "第一批人工确认身份"
    pipeline.run.status = "已完成"
    pipeline.run.finished_at = m.utcnow()

    second_run = m.AnalysisRun(
        package_id=pipeline.package.id, adapter="ollama", status="运行中"
    )
    pipeline.session.add(second_run)
    pipeline.session.flush()
    pipeline.session.add_all([
        m.ProcessingTask(
            package_id=pipeline.package.id, analysis_run_id=second_run.id,
            stage=stage, stage_order=index,
        )
        for index, stage in enumerate(services.TASK_STAGES, 1)
    ])
    pipeline.session.commit()
    done, second_transcripts = services._run_audio_stage(
        pipeline.session, pipeline.settings, second_run, pipeline.package,
        [material], _asr_settings(),
    )

    assert done is True
    assert len(asr.calls) == 2
    assert first_transcripts[0].material_file_id == second_transcripts[0].material_file_id
    assert first_transcripts[0].text == second_transcripts[0].text
    assert first_transcripts[0].speaker_label != second_transcripts[0].speaker_label
    clusters = pipeline.session.scalars(select(m.SpeakerCluster)).all()
    assert len(clusters) == 2
    second_speaker = next(item for item in clusters if item.id != first_speaker.id)
    assert second_speaker.label == second_transcripts[0].speaker_label
    assert second_speaker.status == "待确认"
    assert second_speaker.confirmed_name is None
    assert first_speaker.status == "已确认"
    assert first_speaker.confirmed_name == "第一批人工确认身份"
    speech_segments = pipeline.session.scalars(select(m.SpeakerSegment)).all()
    assert len(speech_segments) == 2
    assert {item.speaker_id for item in speech_segments} == {
        first_speaker.id, second_speaker.id,
    }
