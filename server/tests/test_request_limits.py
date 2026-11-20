from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.audio_adapters import AsrError, LlamaCppAsrAdapter
from app.model_adapters import ModelAdapterError, ModelContext, OllamaIntentModelAdapter, OpenAICompatibleModelAdapter
from app.request_budget import estimate_tokens, split_items_for_request
from app.stage_adapters import (
    _IDENTITY_GENERATION_SCHEMA,
    _identity_output_budget,
    OpenAICompatibleLlmStageAdapter,
)


def context(content: str = "客户支持新产品。", master_data: dict | None = None) -> ModelContext:
    return ModelContext("package", "customer", [{"id": "material", "content": content}], master_data or {})


def adapter(handler, **kwargs) -> OpenAICompatibleModelAdapter:
    return OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234", model_name="local", transport=httpx.MockTransport(handler), **kwargs
    )


def response(content: str = '{"observations":[]}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_complete_prompt_budget_splits_single_material_without_losing_tail():
    service = adapter(lambda request: response(), num_ctx=8192)
    original = context("会议原文abc123。" * 2400, {"technologies": [{"id": "t", "name": "产品方向" * 100}]})
    batches = service.split_context(original)
    assert len(batches) > 1
    fragments = [item for batch in batches for item in batch.material_summaries]
    assert "".join(item["content"] for item in fragments) == original.material_summaries[0]["content"]
    assert {item["id"] for item in fragments} == {"material"}
    assert fragments[0]["content_offset"] == 0
    assert fragments[-1]["content_end"] == len(original.material_summaries[0]["content"])
    assert all(service.request_fits(service._build_messages(batch)) for batch in batches)


@pytest.mark.parametrize("adapter_type", [OllamaIntentModelAdapter, OpenAICompatibleModelAdapter])
def test_full_batches_reserve_json_fallback_and_really_send_it(adapter_type):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        output_budget = body.get("max_tokens") or body["options"]["num_predict"]
        assert estimate_tokens(json.dumps(body, ensure_ascii=False)) + output_budget + 128 <= int(32768 * 0.85)
        if len(requests) % 2:
            return httpx.Response(400, json={"error": "failed to parse grammar"})
        assert "response_format" not in body and "format" not in body
        assert "只输出JSON对象" in body["messages"][-1]["content"]
        return httpx.Response(200, json={"message": {"content": '{"observations":[]}'}}) if adapter_type is OllamaIntentModelAdapter else response()

    service = adapter_type(base_url="http://127.0.0.1:1234", model_name="unit", num_ctx=32768,
                           transport=httpx.MockTransport(handler))
    original = context("中文长会议材料" * 12000)
    batches = service.split_context(original)
    assert len(batches) > 1
    assert "".join(item["content"] for batch in batches for item in batch.material_summaries) == original.material_summaries[0]["content"]
    for batch in batches:
        messages = service._build_messages(batch)
        assert service.request_fits(messages, use_format=True)
        assert service.request_fits(messages, use_format=False)
        assert service.analyze(batch)["observations"] == []
    assert len(requests) == len(batches) * 2


@pytest.mark.parametrize("stage_name", ["alignment", "identity"])
def test_stage_budget_matches_actual_stage_schema_request(stage_name):
    requests = []
    output_key = "alignments" if stage_name == "alignment" else "speakers"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert estimate_tokens(json.dumps(body, ensure_ascii=False)) + body["max_tokens"] + 128 <= int(8192 * 0.85)
        assert output_key in body["response_format"]["json_schema"]["schema"]["properties"]
        return response(json.dumps({output_key: []}))

    service = OpenAICompatibleLlmStageAdapter(base_url="http://127.0.0.1:1234", model_name="unit",
                                             transport=httpx.MockTransport(handler))
    service._chat._num_ctx = 8192
    fixed = {"pages": [], "frames": []} if stage_name == "alignment" else {"attendees": [], "master_data": {}}
    fits = service.alignment_request_fits if stage_name == "alignment" else service.identity_request_fits
    run = service.align_materials if stage_name == "alignment" else service.recommend_identities
    original = [{"id": "segment", "speaker_label": "A", "text": "完整发言" * 3000}]
    groups = split_items_for_request(original, text_key="text", fits=lambda items: fits(segments=items, **fixed))
    assert len(groups) > 1
    assert "".join(item["text"] for group in groups for item in group) == original[0]["text"]
    for group in groups:
        assert run(segments=group, **fixed) == []
    assert len(requests) == len(groups)


def test_identity_fits_uses_dynamic_budget_consistent_with_generation(monkeypatch):
    """回归：身份匹配的批次预估与生成校验预算口径不一致（2048 vs 动态），
    且输出预算未按片段数估算——大会议大批次生成时在输出上限处截断
    （done_reason=length → 硬失败）。修复后：fits 按片段数估算预算并
    限制批内片段数，生成 num_predict 与 fits 完全同值。"""
    monkeypatch.setenv("INTENT_BAO_LLM_MAX_OUTPUT_TOKENS", "6144")
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert estimate_tokens(json.dumps(body, ensure_ascii=False)) + body["max_tokens"] + 128 <= int(65536 * 0.85)
        return response(json.dumps({"speakers": []}))

    service = OpenAICompatibleLlmStageAdapter(base_url="http://127.0.0.1:1234", model_name="unit",
                                              transport=httpx.MockTransport(handler))
    service._chat._num_ctx = 65536
    fixed = {"attendees": [], "master_data": {}}
    segments = [{"id": f"s{index}", "speaker_label": "A", "text": "会议发言内容" * 67}
                for index in range(16)]

    # 1) 批内片段数超过上限 → fits 判 False，驱动分批
    oversized = segments + [{"id": "extra", "speaker_label": "B", "text": "会议发言内容"}]
    assert service.identity_request_fits(segments=oversized, **fixed) is False

    # 2) 批内 ≤ 上限且输入装得下 → fits True，预算 = 16×250+320 = 4320
    budget = _identity_output_budget(segments)
    assert budget == 4320
    assert service.identity_request_fits(segments=segments, **fixed) is True

    # 3) 生成请求的 num_predict 与 fits 预算同值（口径一致，不会截断）
    assert service.recommend_identities(segments=segments, **fixed) == []
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == budget

    # 4) 超大批次被分批器按 fits 切碎，每组仍通过 fits
    many = [{"id": f"m{index}", "speaker_label": "A", "text": "会议发言内容" * 400}
            for index in range(40)]
    groups = split_items_for_request(many, text_key="text",
                                     fits=lambda items: service.identity_request_fits(segments=items, **fixed))
    assert len(groups) > 1
    for group in groups:
        assert service.identity_request_fits(segments=group, **fixed) is True


def test_master_data_alone_too_large_is_explicit_error():
    service = adapter(lambda request: response(), num_ctx=8192)
    with pytest.raises(ModelAdapterError, match="主数据") as raised:
        service.split_context(context("短材料", {"technologies": [{"name": "过大" * 6000}]}))
    assert raised.value.category == "context"


def test_prompt_does_not_silently_truncate_global_materials():
    materials = [{"id": "a", "content": "a" * 210_000}, {"id": "b", "content": "尾部证据"}]
    prepared = adapter(lambda request: response())._prompt_materials(materials)
    assert prepared[0]["content"] == materials[0]["content"]
    assert prepared[1]["content"] == "尾部证据"


def test_generic_split_keeps_metadata_and_text_key():
    parts = split_items_for_request(
        [{"id": "page-1", "page": 7, "text": "abcdefghijk"}],
        text_key="text", fits=lambda items: sum(len(item["text"]) for item in items) <= 4,
    )
    assert "".join(item["text"] for group in parts for item in group) == "abcdefghijk"
    assert all(item["page"] == 7 and item["id"] == "page-1" for group in parts for item in group)


def test_context_error_reports_service_capacity_without_retry_or_raw_data():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "request exceeds context size (4096 tokens); PRIVATE_CUSTOMER_DATA"})

    with pytest.raises(ModelAdapterError) as raised:
        adapter(handler).analyze(context())
    assert len(calls) == 1
    assert raised.value.category == "context"
    assert "4096" in str(raised.value)
    assert "Context Length" in str(raised.value)
    assert "PRIVATE_CUSTOMER_DATA" not in str(raised.value)


def test_deadline_cancels_blocking_request_and_does_not_retry():
    calls = 0
    cancelled = False

    async def handler(request):
        nonlocal calls, cancelled
        calls += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return response()

    started = time.monotonic()
    with pytest.raises(ModelAdapterError, match="总耗时上限") as raised:
        adapter(handler, timeout_seconds=0.03).analyze(context())
    assert time.monotonic() - started < 1
    assert raised.value.category == "timeout"
    assert cancelled and calls == 1


def test_schema_fallback_shares_original_deadline():
    calls = 0
    deadlines = []

    async def handler(request):
        nonlocal calls
        calls += 1
        deadlines.append(service._request_deadline)
        if calls > 1:
            await asyncio.sleep(10)
        return response("invalid" if calls == 1 else '{"observations":[]}')

    service = adapter(handler, timeout_seconds=0.3)
    with pytest.raises(ModelAdapterError, match="总耗时上限"):
        service.analyze(context())
    assert calls == 2
    assert deadlines[0] == deadlines[1]


def test_progress_callback_can_cancel_pending_network_request():
    class CancelRequested(Exception):
        pass

    cancelled = False
    progress = []

    async def handler(request):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return response()

    def check_cancelled():
        if progress:
            raise CancelRequested()

    service = adapter(handler)
    service.set_runtime_hooks(check_cancelled=check_cancelled, on_progress=progress.append)
    with pytest.raises(CancelRequested):
        service.analyze(context())
    assert cancelled
    assert progress[0]["attempt"] == 1
    assert progress[0]["phase"] == "requesting"


@pytest.mark.parametrize("status,body,category", [
    (200, {"text": "language Chinese<asr_text>"}, "empty_response"),
    (400, {"error": "audio too short SECRET_TRANSCRIPT"}, "audio_too_short"),
    (503, {"error": "temporarily unavailable SECRET_TRANSCRIPT"}, "http"),
])
def test_asr_errors_are_structured_and_files_are_closed(tmp_path, monkeypatch, status, body, category):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    handles = []
    real_open = Path.open

    def recording_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == audio:
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", recording_open)
    service = LlamaCppAsrAdapter(base_url="http://127.0.0.1:8099", model_name="local-asr",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))
    with pytest.raises(AsrError) as raised:
        service.transcribe(audio, language="zh", hotwords=[], duration_ms=50)
    assert raised.value.category == category
    assert raised.value.retryable
    assert "SECRET_TRANSCRIPT" not in str(raised.value)
    assert handles and all(handle.closed for handle in handles)


def test_asr_uses_full_text_when_timed_segments_are_empty(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    service = LlamaCppAsrAdapter(base_url="http://127.0.0.1:8099", model_name="local-asr",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "segments": [], "text": "language Chinese<asr_text>这是完整转写。"
        })))
    result = service.transcribe(audio, language="zh", hotwords=[], duration_ms=1500)
    assert result.segments[0].text == "这是完整转写。"
    assert result.segments[0].end_ms == 1500


def test_asr_malformed_shape_is_distinct_from_empty_transcript(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    service = LlamaCppAsrAdapter(base_url="http://127.0.0.1:8099", model_name="local-asr",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"unexpected": "value"})))
    with pytest.raises(AsrError) as raised:
        service.transcribe(audio, language="zh", hotwords=[])
    assert raised.value.category == "invalid_response"
    assert not raised.value.retryable


@pytest.mark.parametrize("cancel_from_hook", [False, True])
def test_asr_wait_is_bounded_cancellable_and_closes_audio(tmp_path, monkeypatch, cancel_from_hook):
    class CancelRequested(Exception):
        pass

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    handles = []
    cancelled = False
    real_open = Path.open
    progress = []

    def recording_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == audio:
            handles.append(handle)
        return handle

    async def handler(request):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return httpx.Response(200, json={"text": "完整文本"})

    def check_cancelled():
        if cancel_from_hook and progress:
            raise CancelRequested()

    monkeypatch.setattr(Path, "open", recording_open)
    service = LlamaCppAsrAdapter(base_url="http://127.0.0.1:8099", model_name="local-asr",
        transport=httpx.MockTransport(handler), timeout_seconds=2 if cancel_from_hook else 0.03)
    service.set_runtime_hooks(check_cancelled=check_cancelled, on_progress=progress.append)
    with pytest.raises(CancelRequested if cancel_from_hook else AsrError) as raised:
        service.transcribe(audio, language="zh", hotwords=[])
    if not cancel_from_hook:
        assert raised.value.category == "timeout"
        assert "总耗时上限" in str(raised.value)
    assert cancelled
    assert handles and all(handle.closed for handle in handles)
    assert progress[0]["phase"] == "requesting"
