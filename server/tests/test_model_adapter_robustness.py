from __future__ import annotations

import json

import httpx
import pytest

from app.model_adapters import (
    MAX_OUTPUT_TOKEN_BUDGET,
    MIN_OUTPUT_TOKEN_BUDGET,
    ModelAdapterError,
    ModelContext,
    OllamaIntentModelAdapter,
)


def _adapter(handler: httpx.MockTransport) -> OllamaIntentModelAdapter:
    return OllamaIntentModelAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen-test:latest",
        transport=handler,
    )


def _context() -> ModelContext:
    return ModelContext(
        package_id="package-1",
        customer_id="customer-a",
        material_summaries=[
            {
                "id": "material-1",
                "type": "人工纪要",
                "name": "minutes.txt",
                "content": "产品负责人：建议把Mini LED纳入下一代产品路线图。",
            }
        ],
        master_data={
            "technologies": [{"id": "tech-mini-led", "name": "Mini LED"}],
            "generic_platforms": [
                {"id": "platform-27-5k", "name": "27 5K产品平台"}
            ],
            "customer_functions": [
                {"id": "function-product", "name": "产品"}
            ],
        },
    )


def _valid_content() -> str:
    return json.dumps(
        {
            "observations": [
                {
                    "technology_id": "tech-mini-led",
                    "generic_platform_id": "platform-27-5k",
                    "function_id": "function-product",
                    "speaker_name": "赵总",
                    "speaker_role": "产品规划负责人",
                    "speaker_organization": "产品部",
                    "quote": "建议把Mini LED纳入下一代产品路线图。",
                    "attitude_level": 5,
                    "adoption_stage": "产品项目规划",
                    "support_reason": "主动建议进入路线图",
                    "resistance_reason": None,
                    "bottlenecks": [],
                    "decision_influence": "推动",
                    "confidence": 0.93,
                    "uncertainty_reason": None,
                }
            ]
        },
        ensure_ascii=False,
    )


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": 100,
            "eval_count": 50,
            "done_reason": "stop",
        },
    )


def test_simple_generation_schema_is_compact_and_guided() -> None:
    schema = OllamaIntentModelAdapter._simple_generation_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["observations"]
    item = schema["properties"]["observations"]["items"]
    assert item["required"] == [
        "technology_id",
        "quote",
        "confidence",
        "attitude_level",
    ]
    assert item["additionalProperties"] is False
    assert item["properties"]["attitude_level"]["enum"] == [1, 2, 3, 4, 5, None]
    assert "样品／技术验证" in item["properties"]["adoption_stage"]["enum"]
    assert "推动" in item["properties"]["decision_influence"]["enum"]
    assert set(item["properties"]) == {
        "technology_id",
        "generic_platform_id",
        "function_id",
        "speaker_name",
        "speaker_role",
        "speaker_organization",
        "quote",
        "attitude_level",
        "adoption_stage",
        "support_reason",
        "resistance_reason",
        "bottlenecks",
        "decision_influence",
        "confidence",
        "uncertainty_reason",
    }
    assert "$defs" not in schema


def test_output_token_budget_is_bounded() -> None:
    budget = OllamaIntentModelAdapter._output_token_budget("")
    assert budget == MIN_OUTPUT_TOKEN_BUDGET
    huge = OllamaIntentModelAdapter._output_token_budget("x" * 10_000_000)
    assert huge == MAX_OUTPUT_TOKEN_BUDGET
    assert MIN_OUTPUT_TOKEN_BUDGET <= huge <= MAX_OUTPUT_TOKEN_BUDGET


def test_output_token_budget_env_override(monkeypatch) -> None:
    monkeypatch.setenv("INTENT_BAO_LLM_MAX_OUTPUT_TOKENS", "6144")
    huge = OllamaIntentModelAdapter._output_token_budget("x" * 10_000_000)
    assert huge == 6144
    small = OllamaIntentModelAdapter._output_token_budget("")
    assert small == MIN_OUTPUT_TOKEN_BUDGET
    monkeypatch.setenv("INTENT_BAO_LLM_MAX_OUTPUT_TOKENS", "not-a-number")
    assert (
        OllamaIntentModelAdapter._output_token_budget("x" * 10_000_000)
        == MAX_OUTPUT_TOKEN_BUDGET
    )
    monkeypatch.setenv("INTENT_BAO_LLM_MAX_OUTPUT_TOKENS", "50")
    assert (
        OllamaIntentModelAdapter._output_token_budget("x" * 10_000_000)
        == MIN_OUTPUT_TOKEN_BUDGET
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        (
            '```json\n{"observations": []}\n```',
            '{"observations": []}',
        ),
        (
            '先思考一下：<think>分析中</think>然后输出：\n{"a": 1}\n结束',
            '{"a": 1}',
        ),
        ('{"observations": []}', '{"observations": []}'),
        ("不是JSON", "不是JSON"),
    ],
)
def test_extract_json_object_strips_fences_and_prose(
    content: str, expected: str
) -> None:
    assert OllamaIntentModelAdapter._extract_json_object(content) == expected


def test_analyze_falls_back_when_grammar_is_rejected() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                400,
                json={"error": "Failed to initialize samplers: failed to parse grammar"},
            )
        assert "format" not in body
        assert body["options"]["num_predict"] >= MIN_OUTPUT_TOKEN_BUDGET
        assert body["think"] is False
        return _ok_response(_valid_content())

    result = _adapter(httpx.MockTransport(handler)).analyze(_context())
    assert len(calls) == 2
    assert result["observations"][0]["attitude_level"] == 5
    assert result["done_reason"] == "stop"
    assert result["num_predict"] >= MIN_OUTPUT_TOKEN_BUDGET


def test_analyze_sends_think_disabled_for_direct_json_output() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_response(_valid_content())

    result = _adapter(httpx.MockTransport(handler)).analyze(_context())
    assert captured["think"] is False
    assert captured["options"]["temperature"] == 0
    assert result["observations"][0]["attitude_level"] == 5


def test_analyze_falls_back_when_structured_output_is_invalid() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _ok_response("这不是合法的JSON")
        return _ok_response(f"```json\n{_valid_content()}\n```")

    result = _adapter(httpx.MockTransport(handler)).analyze(_context())
    assert calls == 2
    assert result["observations"][0]["function_id"] == "function-product"


def test_analyze_raises_combined_error_after_all_attempts_fail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _ok_response('{"observations": [{"unknown_field": true}]}')

    adapter = _adapter(httpx.MockTransport(handler))
    with pytest.raises(ModelAdapterError) as raised:
        adapter.analyze(_context())
    message = str(raised.value)
    assert "意向抽取失败" in message
    assert "结构契约" in message
