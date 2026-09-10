"""Alignment output limits must preserve evidence and allow later stages to run."""

from collections import Counter
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m, services
from app.model_adapters import ModelAdapterError
from app.schemas import MaterialAlignment
from test_media_completeness import (
    pipeline, FakeAsr, FakeIntent, _asr_settings, _install_asr, _transcript,
)


def _data(pipeline, *, segments=1, pages=1, frames=0, interval=1000):
    recording = pipeline.add_file("alignment-source.wav")
    document = pipeline.add_file("alignment-pages.pdf", "PDF")
    speech = [
        m.TranscriptSegment(
            analysis_run_id=pipeline.run.id, package_id=pipeline.package.id,
            material_file_id=recording.id, order_index=index + 1,
            start_ms=index * interval, end_ms=index * interval + 1000,
            text=f"第 {index + 1} 段需要安排样机验证",
        )
        for index in range(segments)
    ]
    page_rows = [
        m.PdfPageText(
            material_file_id=document.id, page_no=index + 1,
            text=f"第 {index + 1} 页样机验证计划", char_count=16,
            extracted_by="test",
        )
        for index in range(pages)
    ]
    frame_rows = [
        m.KeyFrame(
            material_file_id=recording.id, time_ms=index * 1000,
            image_relative_path=f"test-frame-{index}.jpg",
            ocr_text=f"第 {index + 1} 帧验证要求",
        )
        for index in range(frames)
    ]
    pipeline.session.add_all(speech + page_rows + frame_rows)
    pipeline.session.commit()
    return speech, page_rows, frame_rows


def _run(pipeline, align, data, **adapter_kwargs):
    adapter = SimpleNamespace(
        alignment_request_fits=lambda **_kwargs: True,
        align_materials=align,
        **adapter_kwargs,
    )
    services._run_alignment_stage(
        pipeline.session, adapter, pipeline.run, pipeline.package, *data,
    )
    pipeline.session.commit()
    return json.loads(pipeline.task("资料对齐").result_json)


def _matches(request):
    return [
        MaterialAlignment(
            segment_id=segment["id"], target_type=kind, target_id=target["id"],
            confidence=0.8, reason="两处均讨论样机验证",
        )
        for segment in request["segments"]
        for field, kind in (("pages", "pdf_page"), ("frames", "key_frame"))
        for target in request[field]
    ]


def _links(pipeline):
    return pipeline.session.scalars(
        select(m.AlignmentLink).where(m.AlignmentLink.analysis_run_id == pipeline.run.id)
    ).all()


@pytest.mark.parametrize("split_dimension", ["segments", "targets"])
def test_output_limit_splits_and_recovers_every_relationship(pipeline, split_dimension):
    data = _data(
        pipeline, segments=4 if split_dimension == "segments" else 1,
        pages=2 if split_dimension == "segments" else 4,
    )
    calls = []

    def align(**request):
        calls.append(request)
        size = (len(request["segments"]) if split_dimension == "segments"
                else len(request["pages"]) + len(request["frames"]))
        if size > 1:
            raise ModelAdapterError("输出达到上限", category="output_limit")
        return _matches(request)

    summary = _run(pipeline, align, data)

    assert len(calls) == 7
    assert summary["batches"] == summary["processed_batches"] == summary["completed_batches"] == 1
    assert summary["retry_count"] == 6
    assert summary["skipped_batches"] == summary["unmatched_count"] == 0
    assert summary["unaligned_batches"] == [] and summary["has_warnings"] is False
    expected = {(segment.id, "pdf_page", page.id) for segment in data[0] for page in data[1]}
    assert {(row.segment_id, row.target_type, row.target_id) for row in _links(pipeline)} == expected
    assert summary["alignments"] == len(expected)
    task = pipeline.task("资料对齐")
    assert (task.status, task.progress) == ("已完成", 100)


def test_single_pair_output_limit_records_a_gap_without_fabricating_a_link(pipeline):
    data = _data(pipeline)
    calls = []

    def align(**request):
        calls.append(request)
        raise ModelAdapterError("输出达到上限", category="output_limit")

    summary = _run(pipeline, align, data)

    assert len(calls) == 1
    assert summary["batches"] == summary["processed_batches"] == 1
    assert summary["completed_batches"] == summary["retry_count"] == 0
    assert summary["skipped_batches"] == summary["unmatched_count"] == 1
    assert summary["has_warnings"] is True
    failure, = summary["unaligned_batches"]
    assert failure["batch"] == 1
    assert failure["segment_ids"] == [data[0][0].id]
    assert failure["target_ids"] == [data[1][0].id]
    assert (failure["start_ms"], failure["end_ms"]) == (0, 1000)
    assert "alignment-source.wav" in failure["material_names"]
    assert failure["category"] == "output_limit" and failure["error"]
    assert _links(pipeline) == []
    task = pipeline.task("资料对齐")
    assert (task.status, task.progress) == ("已完成", 100)
    assert not task.error_message


def test_failed_split_half_does_not_prevent_the_other_half_from_succeeding(pipeline):
    data = _data(pipeline, segments=2)
    calls = []

    def align(**request):
        calls.append(request)
        if len(request["segments"]) > 1 or request["segments"][0]["id"] == data[0][0].id:
            raise ModelAdapterError("输出达到上限", category="output_limit")
        return _matches(request)

    summary = _run(pipeline, align, data)

    assert len(calls) == 3 and summary["retry_count"] == 2
    assert summary["batches"] == summary["processed_batches"] == 1
    assert summary["completed_batches"] == 0
    assert summary["skipped_batches"] == summary["unmatched_count"] == 1
    failure, = summary["unaligned_batches"]
    assert failure["segment_ids"] == [data[0][0].id]
    link, = _links(pipeline)
    assert (link.segment_id, link.target_id) == (data[0][1].id, data[1][0].id)
    assert pipeline.task("资料对齐").progress == 100


@pytest.mark.parametrize("category", ["invalid_response", "timeout", "connection", "context"])
def test_non_output_model_failure_is_recorded_once_and_keeps_prior_results(pipeline, category):
    data = _data(pipeline, segments=2, interval=180_000)
    calls = []

    def align(**request):
        calls.append(request)
        if len(calls) == 2:
            raise ModelAdapterError("受控模型失败", category=category)
        return _matches(request)

    summary = _run(pipeline, align, data)

    assert len(calls) == 2
    assert summary["batches"] == summary["processed_batches"] == 2
    assert summary["completed_batches"] == summary["skipped_batches"] == 1
    assert summary["retry_count"] == 0
    failure, = summary["unaligned_batches"]
    assert failure["batch"] == 2
    assert failure["segment_ids"] == [data[0][1].id]
    assert failure["category"] == category
    assert summary["unmatched_count"] == 1 and summary["has_warnings"]
    link, = _links(pipeline)
    assert (link.segment_id, link.target_id) == (data[0][0].id, data[1][0].id)
    assert pipeline.task("资料对齐").progress == 100


def test_initial_batches_cover_every_candidate_with_bounded_request_sizes(pipeline):
    data = _data(pipeline, segments=7, pages=10, frames=2)
    calls = []

    def align(**request):
        calls.append(request)
        assert 1 <= len(request["segments"]) <= 4
        assert 1 <= len(request["pages"]) + len(request["frames"]) <= 8
        return _matches(request)

    summary = _run(pipeline, align, data)

    expected = {
        (segment.id, kind, target.id)
        for segment in data[0]
        for kind, targets in (("pdf_page", data[1]), ("key_frame", data[2]))
        for target in targets
    }
    seen = Counter(
        (match.segment_id, match.target_type, match.target_id)
        for request in calls for match in _matches(request)
    )
    assert set(seen) == expected and set(seen.values()) == {1}
    assert len(_links(pipeline)) == len(expected)
    assert summary["batches"] == summary["completed_batches"] == len(calls)
    assert summary["retry_count"] == 0 and summary["has_warnings"] is False


def test_exhausted_retry_budget_marks_every_remaining_part(pipeline):
    data = _data(pipeline, segments=4, pages=8)
    calls = []

    def align(**request):
        calls.append(request)
        raise ModelAdapterError("输出达到上限", category="output_limit")

    summary = _run(pipeline, align, data)

    assert len(calls) == 7
    assert summary["retry_count"] == 6
    assert summary["batches"] == summary["processed_batches"] == 1
    assert summary["completed_batches"] == 0
    assert summary["has_warnings"] and summary["unmatched_count"] == 4
    failed_pairs = {
        (segment_id, target_id)
        for failure in summary["unaligned_batches"]
        for segment_id in failure["segment_ids"]
        for target_id in failure["target_ids"]
    }
    assert failed_pairs == {(segment.id, page.id) for segment in data[0] for page in data[1]}
    assert summary["skipped_batches"] == len(summary["unaligned_batches"])
    assert _links(pipeline) == [] and pipeline.task("资料对齐").progress == 100


def test_root_batch_deadline_prevents_further_requests_and_preserves_gap_coverage(pipeline, monkeypatch):
    data = _data(pipeline, segments=4, pages=2)
    now = [100.0]
    calls = []
    limits = []
    exits = []
    monkeypatch.setattr(services, "time", SimpleNamespace(monotonic=lambda: now[0]))

    @contextmanager
    def request_time_limit(seconds):
        assert 0 < seconds <= 480
        limits.append(seconds)
        try:
            yield
        finally:
            exits.append(seconds)

    def align(**request):
        calls.append(request)
        now[0] += 480.1
        raise ModelAdapterError("输出达到上限", category="output_limit")

    summary = _run(pipeline, align, data, request_time_limit=request_time_limit)

    assert len(calls) == len(limits) == len(exits) == 1
    assert summary["retry_count"] == 0
    assert summary["processed_batches"] == 1 and summary["completed_batches"] == 0
    failed_pairs = {
        (segment_id, target_id)
        for failure in summary["unaligned_batches"]
        for segment_id in failure["segment_ids"]
        for target_id in failure["target_ids"]
    }
    assert failed_pairs == {(segment.id, page.id) for segment in data[0] for page in data[1]}
    assert summary["has_warnings"] and summary["unmatched_count"] == 4
    assert _links(pipeline) == [] and pipeline.task("资料对齐").progress == 100


def test_successful_empty_response_has_no_failure_warning(pipeline):
    summary = _run(pipeline, lambda **_request: [], _data(pipeline))

    assert summary["alignments"] == 0
    assert summary["batches"] == summary["processed_batches"] == summary["completed_batches"] == 1
    assert summary["unmatched_count"] == summary["skipped_batches"] == summary["retry_count"] == 0
    assert summary["has_warnings"] is False and summary["unaligned_batches"] == []
    assert _links(pipeline) == []


def test_alignment_links_are_deduplicated_and_unknown_ids_are_rejected(pipeline):
    data = _data(pipeline)

    def align(**request):
        match, = _matches(request)
        return [
            match, match,
            match.model_copy(update={"segment_id": "not-in-this-request"}),
            match.model_copy(update={"target_id": "not-in-this-request"}),
            match.model_copy(update={"target_type": "key_frame"}),
        ]

    summary = _run(pipeline, align, data)

    assert summary["alignments"] == 1
    link, = _links(pipeline)
    assert (link.segment_id, link.target_id) == (data[0][0].id, data[1][0].id)


def test_cancellation_is_propagated_without_retry_and_clears_hooks(pipeline):
    data = _data(pipeline, segments=4)
    calls = []
    hooks = []

    def align(**request):
        calls.append(request)
        raise services._AnalysisCancellationRequested()

    with pytest.raises(services._AnalysisCancellationRequested):
        _run(pipeline, align, data, set_runtime_hooks=lambda **kwargs: hooks.append(kwargs))

    assert len(calls) == 1 and _links(pipeline) == []
    assert hooks[-1] == {"check_cancelled": None, "on_progress": None}
    assert pipeline.task("资料对齐").status != "已完成"


def test_unexpected_application_error_still_fails_the_stage(pipeline):
    data = _data(pipeline)
    calls = []

    def align(**request):
        calls.append(request)
        raise RuntimeError("unexpected application failure")

    with pytest.raises(services.DomainError, match="unexpected application failure"):
        _run(pipeline, align, data)

    assert len(calls) == 1 and _links(pipeline) == []
    task = pipeline.task("资料对齐")
    assert task.status == "处理失败" and task.progress < 100


@pytest.mark.parametrize("category", ["output_limit", "invalid_response"])
def test_all_alignment_failures_allow_real_pipeline_to_reach_intent(pipeline, monkeypatch, category):
    recording = pipeline.add_file("continue.wav")
    pipeline.add_file("continue.pdf", "PDF")
    monkeypatch.setattr(services, "extract_pdf_text", lambda _path: [
        SimpleNamespace(page_no=1, text="验证计划页面", char_count=6),
    ])
    _install_asr(monkeypatch, FakeAsr(lambda *_args, **_kwargs: _transcript("安排样机验证")))
    technology = m.Technology(code="alignment-tech", name="Alignment technology")
    pipeline.session.add(technology)
    pipeline.session.commit()
    calls = []

    def align(**request):
        calls.append(request)
        raise ModelAdapterError("对齐模型未返回完整结果", category=category)

    stage_adapter = SimpleNamespace(
        align_materials=align, alignment_request_fits=lambda **_kwargs: True,
        recommend_identities=lambda **_kwargs: [],
    )

    class ContinuingIntent(FakeIntent):
        def analyze(self, context):
            self.calls.append(context)
            return {"observations": [{
                "technology_id": technology.id, "quote": "安排样机验证",
                "attitude_level": 3, "confidence": 0.9,
            }]}

    intent = ContinuingIntent()
    result = services.execute_analysis_run(
        pipeline.session, pipeline.run, settings=pipeline.settings,
        adapter=intent, stage_adapter=stage_adapter, asr_settings=_asr_settings(),
    )

    assert result.status == "已完成"
    assert len(calls) == len(intent.calls) == 1
    assert pipeline.task("意向抽取").status == "已完成"
    assert pipeline.task("资料对齐").progress == 100
    assert all(pipeline.task(stage).status in {"已完成", "不适用"} for stage in services.TASK_STAGES)
    summary = json.loads(pipeline.task("资料对齐").result_json)
    assert summary["has_warnings"] and summary["unmatched_count"] == 1
    assert _links(pipeline) == []
    observation, = pipeline.session.scalars(select(m.RawIntentObservation)).all()
    assert observation.raw_quote == "安排样机验证"
    stored_evidence = pipeline.session.get(m.EvidenceFragment, observation.evidence_id)
    assert stored_evidence.material_file_id == recording.id
    evidence = [item for item in intent.calls[0].material_summaries if item.get("type") == "转写片段"]
    assert len(evidence) == 1 and evidence[0]["source_material_file_id"] == recording.id
    assert "安排样机验证" in evidence[0]["content"]
