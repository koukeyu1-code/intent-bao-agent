"""Resumption and completeness checks, using disposable databases and fake models."""
from collections import Counter
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m, services
from app.audio_adapters import AsrError, AsrResult
from app.diarization import SpeakerTurn
from app.media_processing import MediaProcessingUnavailable
from test_media_completeness import (
    pipeline, FakeAsr, FakeIntent, _install_asr, _install_diarization,
    _transcript, _asr_settings,
)


def _new_run(pipeline):
    pipeline.run.status = "处理失败"
    pipeline.run.finished_at = m.utcnow()
    pipeline.run = m.AnalysisRun(package_id=pipeline.package.id, adapter="ollama", status="运行中")
    pipeline.session.add(pipeline.run)
    pipeline.session.flush()
    pipeline.session.add_all([
        m.ProcessingTask(package_id=pipeline.package.id, analysis_run_id=pipeline.run.id,
                         stage=stage, stage_order=index)
        for index, stage in enumerate(services.TASK_STAGES, 1)
    ])
    pipeline.session.commit()


def test_successful_chunks_survive_failure_and_only_failed_chunk_is_retried(pipeline, monkeypatch):
    material = pipeline.add_file("recover.wav")
    _install_diarization(monkeypatch)
    calls = Counter()
    unhealthy = [True]

    def transcribe(path, **kwargs):
        start = int(path.read_text("utf-8"))
        calls[start] += 1
        if start == 5000 and unhealthy[0]:
            raise AsrError("暂时无法连接", category="connection", retryable=True)
        return _transcript(f"发言 {start}")

    _install_asr(monkeypatch, FakeAsr(transcribe))
    with pytest.raises(services.DomainError):
        pipeline.audio([material])
    assert pipeline.session.scalars(select(m.TranscriptSegment)).all() == []
    checkpoints = list((pipeline.settings.data_dir / "temp" / "asr-checkpoints").rglob("*.json"))
    assert len(checkpoints) == 1
    assert json.loads(checkpoints[0].read_text("utf-8"))["source_run_id"] == pipeline.run.id
    unhealthy[0] = False
    _new_run(pipeline)
    done, rows = pipeline.audio([material])
    assert done and len(rows) == 2
    assert calls == {0: 1, 5000: 3}
    assert json.loads(pipeline.task("音轨与转写").result_json)["files"][0]["transcription"]["reused"] == 1
    assert {row.analysis_run_id for row in rows} == {pipeline.run.id}


@pytest.mark.parametrize("change", ["source", "model", "hotwords", "jobs"])
def test_checkpoint_invalidates_on_input_or_configuration_changes(pipeline, monkeypatch, change):
    material = pipeline.add_file("config.wav")
    asr = FakeAsr()
    _install_asr(monkeypatch, asr)
    pipeline.audio([material])
    _new_run(pipeline)
    config = _asr_settings()
    if change == "source":
        (pipeline.settings.data_dir / material.relative_path).write_text("changed source", encoding="utf-8")
    elif change == "model":
        config.model_name = "new-model"
    elif change == "hotwords":
        monkeypatch.setattr(services, "_build_hotwords", lambda *_: ["new term"])
    else:
        monkeypatch.setattr(services, "probe_media_duration", lambda _: 7000)
    services._run_audio_stage(pipeline.session, pipeline.settings, pipeline.run,
                              pipeline.package, [material], config)
    assert len(asr.calls) == 2


def test_long_audio_without_diarization_is_still_limited_to_thirty_seconds(pipeline, monkeypatch):
    material = pipeline.add_file("long.wav")
    monkeypatch.setattr(services, "probe_media_duration", lambda _: 95_000)
    cuts = []
    def cut(_source, start, end, destination):
        cuts.append((start, end))
        destination.write_text("speech", encoding="utf-8")
        return True
    monkeypatch.setattr(services, "slice_audio_segment", cut)
    durations = []
    def transcribe(_path, **kwargs):
        durations.append(kwargs["duration_ms"])
        return _transcript()
    _install_asr(monkeypatch, FakeAsr(transcribe))
    _, rows = pipeline.audio([material])
    assert cuts == [(0, 30_000), (30_000, 60_000), (60_000, 90_000), (90_000, 95_000)]
    assert durations == [30_000, 30_000, 30_000, 5000]
    assert [(row.start_ms, row.end_ms) for row in rows] == cuts


def test_short_speech_retry_adds_only_context_before_adjacent_speaker(pipeline, monkeypatch):
    material = pipeline.add_file("short.wav")
    turns = [SpeakerTurn(1000, 1400, "A"), SpeakerTurn(1600, 4000, "B")]
    monkeypatch.setattr(services, "get_diarizer", lambda _: SimpleNamespace(diarize=lambda *_a, **_kw: turns))
    cuts = []
    def cut(_source, start, end, destination):
        cuts.append((start, end))
        destination.write_text(str(start), encoding="utf-8")
        return True
    monkeypatch.setattr(services, "slice_audio_segment", cut)
    calls = [0]
    def transcribe(_path, **kwargs):
        calls[0] += 1
        return AsrResult([], "asr") if calls[0] == 1 else _transcript("短发言")
    _install_asr(monkeypatch, FakeAsr(transcribe))
    _, rows = pipeline.audio([material])
    assert cuts[:2] == [(1000, 1400), (250, 1600)]
    assert (rows[0].start_ms, rows[0].end_ms) == (1000, 1400)
    assert rows[0].speaker_label != rows[1].speaker_label


@pytest.mark.parametrize("kind,expected_calls", [("dependency", 0), ("connection", 6)])
def test_systematic_asr_failure_stops_early_with_unprocessed_count(pipeline, monkeypatch, kind, expected_calls):
    material = pipeline.add_file("early.wav")
    monkeypatch.setattr(services, "probe_media_duration", lambda _: 3_000_000)
    cuts = []
    def cut(_source, start, _end, destination):
        cuts.append(start)
        if kind == "dependency":
            raise MediaProcessingUnavailable("请安装 FFmpeg")
        destination.write_text("speech", encoding="utf-8")
        return True
    monkeypatch.setattr(services, "slice_audio_segment", cut)
    def fail_transcription(*_a, **_kw):
        raise AsrError("语音服务无法连接", category="connection", retryable=True)
    asr = FakeAsr(fail_transcription)
    _install_asr(monkeypatch, asr)
    with pytest.raises(services.DomainError):
        pipeline.audio([material])
    failure = json.loads(pipeline.task("音轨与转写").result_json)["failed_segments"][-1]
    assert failure["stopped_early"] and failure["remaining_jobs"] >= 97
    assert len(asr.calls) == expected_calls
    assert len(cuts) == (1 if kind == "dependency" else 6)
    assert pipeline.session.scalars(select(m.TranscriptSegment)).all() == []


def _alignment_data(pipeline):
    material = pipeline.add_file("alignment.wav")
    speech = [m.TranscriptSegment(analysis_run_id=pipeline.run.id, package_id=pipeline.package.id,
              material_file_id=material.id, order_index=i, start_ms=i*1000, end_ms=(i+1)*1000,
              text=text) for i, text in enumerate([" ", "客户的发言"*120, "最后的重要结论"], 1)]
    pages = [m.PdfPageText(material_file_id=material.id, page_no=1, text="页面正文"*100,
                          char_count=400, extracted_by="test")]
    pipeline.session.add_all(speech + pages)
    pipeline.session.commit()
    return speech, pages


def test_alignment_filters_empty_text_and_preserves_all_long_text(pipeline):
    speech, pages = _alignment_data(pipeline)
    calls = []
    def fits(**kwargs):
        return len(json.dumps(kwargs, ensure_ascii=False)) < 1300
    def align(**kwargs):
        assert fits(**kwargs)
        assert all(row["text"].strip() for row in kwargs["segments"])
        calls.append(kwargs)
        return []
    adapter = SimpleNamespace(alignment_request_fits=fits, align_materials=align)
    services._run_alignment_stage(pipeline.session, adapter, pipeline.run, pipeline.package, speech, pages, [])
    assert len(calls) > 1
    for row in speech[1:]:
        parts = {item.get("content_offset", 0): item["text"] for call in calls
                 for item in call["segments"] if item["id"] == row.id}
        assert "".join(parts[key] for key in sorted(parts)) == row.text
    assert pipeline.task("资料对齐").status == "已完成"
    assert pipeline.task("资料对齐").progress == 100


def test_alignment_failure_is_visible_and_not_marked_completed(pipeline):
    speech, pages = _alignment_data(pipeline)
    def fail(**_kwargs):
        raise RuntimeError("context length exceeded")
    with pytest.raises(services.DomainError, match="资料对齐失败"):
        services._run_alignment_stage(pipeline.session, SimpleNamespace(align_materials=fail),
                                      pipeline.run, pipeline.package, speech, pages, [])
    task = pipeline.task("资料对齐")
    assert task.status == "处理失败" and task.progress < 100
    assert "context length exceeded" in task.error_message


def test_long_single_material_and_derived_text_are_not_truncated(pipeline):
    material = pipeline.add_file("long.txt", "转写稿")
    text = "会议后半段也必须保留" * 4000
    (pipeline.settings.data_dir / material.relative_path).write_text(text, encoding="utf-8")
    items, _ = services._build_model_materials(pipeline.settings, pipeline.package, [material])
    assert items[0]["content"] == text
    batches = services._split_model_materials_for_analysis(items)
    assert len(batches) > 1
    assert "".join(item["content"] for batch in batches for item in batch) == text
    assert {item["source_material_file_id"] for batch in batches for item in batch} == {material.id}


def test_identical_quotes_from_separate_batches_keep_distinct_evidence_sources(pipeline):
    from app.model_adapters import ModelContext
    quote = "客户确认先做样机验证"
    files = [pipeline.add_file(name, "人工纪要") for name in ("first.txt", "second.txt")]
    for file in files:
        (pipeline.settings.data_dir / file.relative_path).write_text(quote, encoding="utf-8")
    technology = m.Technology(code="batch-tech", name="Batch tech")
    pipeline.session.add(technology)
    pipeline.session.commit()

    class BatchIntent(FakeIntent):
        def split_context(self, context):
            return [ModelContext(package_id=context.package_id, customer_id=context.customer_id,
                                 material_summaries=[item], master_data=context.master_data)
                    for item in context.material_summaries]

        def analyze(self, context, output_budget=None):
            return {"observations": [{"technology_id": technology.id, "quote": quote,
                                      "attitude_level": 3, "confidence": .9}]}

    result = pipeline.execute(BatchIntent())
    assert result.status == "已完成"
    observations = pipeline.session.scalars(select(m.RawIntentObservation)).all()
    assert len(observations) == 2
    evidence = [pipeline.session.get(m.EvidenceFragment, row.evidence_id) for row in observations]
    assert {row.material_file_id for row in evidence} == {file.id for file in files}


def test_split_text_evidence_locator_uses_original_line_numbers(pipeline):
    from app.model_adapters import ModelContext
    quote = "客户确认先做样机验证"
    prefix = "会议标题\n参会信息\n"
    material = pipeline.add_file("lines.txt", "人工纪要")
    (pipeline.settings.data_dir / material.relative_path).write_text(prefix + quote, encoding="utf-8")
    technology = m.Technology(code="line-tech", name="Line tech")
    pipeline.session.add(technology)
    pipeline.session.commit()

    class SplitIntent(FakeIntent):
        def split_context(self, context):
            item = context.material_summaries[0]
            return [ModelContext(package_id=context.package_id, customer_id=context.customer_id,
                                 material_summaries=[{**item, "content": quote,
                                                     "content_offset": item["content"].index(quote)}],
                                 master_data=context.master_data)]

        def analyze(self, context, output_budget=None):
            return {"observations": [{"technology_id": technology.id, "quote": quote,
                                      "attitude_level": 3, "confidence": .9}]}

    assert pipeline.execute(SplitIntent()).status == "已完成"
    evidence = pipeline.session.scalar(select(m.EvidenceFragment))
    assert evidence.material_file_id == material.id
    assert "第3行" in evidence.locator


def _pdf_material(pipeline, monkeypatch, quote):
    material = pipeline.add_file("meeting.pdf", "会议资料")
    pipeline.add_file("notes.txt", "人工纪要")
    monkeypatch.setattr(services, "extract_pdf_text", lambda _path: [
        SimpleNamespace(page_no=1, text=quote, char_count=len(quote)),
    ])
    return material


def test_pdf_retry_uses_only_current_pages_without_duplicate_materials(pipeline, monkeypatch):
    quote = "PDF当前页面正文"
    material = _pdf_material(pipeline, monkeypatch, quote)

    class TrackingIntent(FakeIntent):
        fail = True
        def analyze(self, context, output_budget=None):
            self.calls.append(context)
            if self.fail:
                raise RuntimeError("controlled model failure")
            return {"observations": []}

    adapter = TrackingIntent()
    with pytest.raises(services.DomainError):
        pipeline.execute(adapter)
    first_pages = [item for context in adapter.calls for item in context.material_summaries
                   if item.get("type") == "PDF页面"]
    assert len(first_pages) == 1
    assert first_pages[0]["source_material_file_id"] == material.id
    first_id = first_pages[0]["id"]
    adapter.calls.clear()
    adapter.fail = False
    _new_run(pipeline)
    assert pipeline.execute(adapter).status == "已完成"
    retried_pages = [item for context in adapter.calls for item in context.material_summaries
                     if item.get("type") == "PDF页面"]
    assert len(retried_pages) == 1
    assert retried_pages[0]["id"] != first_id
    assert retried_pages[0]["content"] == quote
    # Historical rows remain for audit; they are excluded from the new request.
    assert len(pipeline.session.scalars(select(m.PdfPageText)).all()) == 2


def test_same_pdf_location_in_separate_batches_produces_one_intent(pipeline, monkeypatch):
    from app.model_adapters import ModelContext
    quote = "客户确认先做样机验证"
    material = _pdf_material(pipeline, monkeypatch, quote)
    technology = m.Technology(code="pdf-tech", name="PDF tech")
    pipeline.session.add(technology)
    pipeline.session.commit()

    class RepeatedPageIntent(FakeIntent):
        def split_context(self, context):
            page = next(item for item in context.material_summaries if item.get("type") == "PDF页面")
            return [ModelContext(package_id=context.package_id, customer_id=context.customer_id,
                                 material_summaries=[{**page, "id": f"copy-{index}"}],
                                 master_data=context.master_data) for index in range(2)]

        def analyze(self, context, output_budget=None):
            return {"observations": [{"technology_id": technology.id, "quote": quote,
                                      "attitude_level": 3, "confidence": .9}]}

    assert pipeline.execute(RepeatedPageIntent()).status == "已完成"
    observations = pipeline.session.scalars(select(m.RawIntentObservation)).all()
    assert len(observations) == 1
    evidence = pipeline.session.get(m.EvidenceFragment, observations[0].evidence_id)
    assert evidence.material_file_id == material.id
    assert evidence.page_no == 1
