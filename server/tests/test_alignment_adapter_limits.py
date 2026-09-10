from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app.model_adapters import ModelAdapterError, ModelContext, OllamaIntentModelAdapter, OpenAICompatibleModelAdapter
from app.request_budget import estimate_tokens, split_items_for_request
from app.schemas import MaterialAlignmentOutput
from app.stage_adapters import ALIGNMENT_OUTPUT_TOKEN_BUDGET, OllamaLlmStageAdapter, OpenAICompatibleLlmStageAdapter


def provider_response(adapter_type, content, *, finish_reason="stop"):
    if adapter_type is OllamaIntentModelAdapter:
        return {
            "message": {"content": content, "thinking": "PRIVATE_THINKING"},
            "done_reason": finish_reason,
            "eval_count": 2048,
        }
    return {
        "choices": [{"message": {"content": content, "reasoning_content": "PRIVATE_THINKING"}, "finish_reason": finish_reason}],
        "usage": {"completion_tokens": 2048, "completion_tokens_details": {"reasoning_tokens": 1779}},
    }


@pytest.mark.parametrize("adapter_type", [OllamaIntentModelAdapter, OpenAICompatibleModelAdapter])
@pytest.mark.parametrize("content", ['', '{"alignments":[{"reason":"PRIVATE_CONTENT', '{"alignments":[]}'])
def test_output_limit_precedes_empty_json_and_schema_validation(adapter_type, content):
    body = provider_response(adapter_type, content, finish_reason="length")
    adapter = adapter_type(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ModelAdapterError) as raised:
        adapter.chat_structured([{"role": "user", "content": "test"}], budget=2048, use_format=True, validation_model=MaterialAlignmentOutput)
    assert raised.value.category == "output_limit"
    assert "输出预算 2048 token" in str(raised.value)
    assert "实际输出 2048 token" in str(raised.value)
    if adapter_type is OpenAICompatibleModelAdapter:
        assert "思考 1779 token" in str(raised.value)
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("adapter_type", [OllamaIntentModelAdapter, OpenAICompatibleModelAdapter])
def test_normal_finished_json_still_passes(adapter_type):
    content = json.dumps({"alignments": [{"segment_id": "seg", "target_type": "pdf_page", "target_id": "page", "confidence": 0.9, "reason": "规格一致"}]})
    body = provider_response(adapter_type, content)
    adapter = adapter_type(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    parsed, raw = adapter.chat_structured([{"role": "user", "content": "test"}], budget=2048, use_format=True, validation_model=MaterialAlignmentOutput)
    assert parsed.alignments[0].segment_id == "seg"
    assert parsed.alignments[0].target_id == "page"
    assert raw == body


def test_output_limit_diagnostics_do_not_persist_non_numeric_usage():
    body = provider_response(OpenAICompatibleModelAdapter, "PRIVATE_CONTENT", finish_reason="length")
    body["usage"] = {"completion_tokens": "PRIVATE_USAGE", "completion_tokens_details": {"reasoning_tokens": "PRIVATE_REASONING"}}
    adapter = OpenAICompatibleModelAdapter(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ModelAdapterError) as raised:
        adapter.chat_structured([{"role": "user", "content": "test"}], budget=2048, use_format=True)
    assert raised.value.category == "output_limit"
    assert "PRIVATE" not in str(raised.value)


def test_empty_response_without_length_remains_invalid_response():
    body = provider_response(OpenAICompatibleModelAdapter, "")
    adapter = OpenAICompatibleModelAdapter(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ModelAdapterError) as raised:
        adapter.chat_structured([{"role": "user", "content": "test"}], budget=2048, use_format=True)
    assert raised.value.category == "invalid_response"


@pytest.mark.parametrize("stage_type", [OllamaLlmStageAdapter, OpenAICompatibleLlmStageAdapter])
def test_split_alignment_inputs_keep_full_output_budget_and_fit_actual_request(stage_type, monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        budget = body.get("max_tokens") or body["options"]["num_predict"]
        assert budget == ALIGNMENT_OUTPUT_TOKEN_BUDGET == 2048
        assert estimate_tokens(json.dumps(body, ensure_ascii=False)) + budget + 128 <= int(8192 * 0.85)
        adapter_type = OllamaIntentModelAdapter if stage_type is OllamaLlmStageAdapter else OpenAICompatibleModelAdapter
        return httpx.Response(200, json=provider_response(adapter_type, '{"alignments":[]}'))

    adapter = stage_type(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(handler))
    adapter._chat._num_ctx = 8192
    # A tiny input-based budget must not influence alignment fit or generation.
    monkeypatch.setattr(adapter._chat, "_output_token_budget", lambda prompt: 1)
    original = [{"id": "seg", "text": "完整发言" * 3000}]
    groups = split_items_for_request(original, text_key="text", fits=lambda items: adapter.alignment_request_fits(segments=items, pages=[], frames=[]))
    assert len(groups) > 1
    assert "".join(item["text"] for group in groups for item in group) == original[0]["text"]
    for group in groups + [[{"id": "seg", "text": "短句"}]]:
        assert adapter.alignment_request_fits(segments=group, pages=[], frames=[])
        assert adapter.align_materials(segments=group, pages=[], frames=[]) == []
    assert len(requests) == len(groups) + 1


def test_stage_request_time_limit_restores_after_success_and_nested_failure():
    adapter = OpenAICompatibleLlmStageAdapter(base_url="http://127.0.0.1:1234", model_name="unit", timeout_seconds=7)
    with adapter.request_time_limit(2):
        assert adapter._chat._total_timeout_seconds == 2
        with adapter.request_time_limit(8):
            assert adapter._chat._total_timeout_seconds == 2
        with pytest.raises(RuntimeError):
            with adapter.request_time_limit(1):
                raise RuntimeError("unit failure")
        assert adapter._chat._total_timeout_seconds == 2
    assert adapter._chat._total_timeout_seconds == 7
    with pytest.raises(ModelAdapterError) as raised:
        with adapter.request_time_limit(0):
            pytest.fail("An expired deadline must not execute the request")
    assert raised.value.category == "timeout"
    assert adapter._chat._total_timeout_seconds == 7


def test_stage_remaining_retry_time_cancels_actual_pending_request():
    cancelled = False

    async def handler(request):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return httpx.Response(200, json=provider_response(OpenAICompatibleModelAdapter, '{"alignments":[]}'))

    adapter = OpenAICompatibleLlmStageAdapter(base_url="http://127.0.0.1:1234", model_name="unit", timeout_seconds=7, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(ModelAdapterError) as raised:
        with adapter.request_time_limit(0.03):
            adapter.align_materials(segments=[{"id": "seg", "text": "短句"}], pages=[], frames=[])
    assert raised.value.category == "timeout"
    assert time.monotonic() - started < 1
    assert cancelled
    assert adapter._chat._total_timeout_seconds == 7
    assert adapter._chat._request_deadline is None


def test_intent_extraction_preserves_output_limit_category_without_identical_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=provider_response(OpenAICompatibleModelAdapter, '{"observations":[]}', finish_reason="length"))

    adapter = OpenAICompatibleModelAdapter(base_url="http://127.0.0.1:1234", model_name="unit", transport=httpx.MockTransport(handler))
    with pytest.raises(ModelAdapterError) as raised:
        adapter.analyze(ModelContext("pkg", "customer", [{"id": "m", "content": "测试"}], {}))
    assert raised.value.category == "output_limit"
    assert len(calls) == 1
