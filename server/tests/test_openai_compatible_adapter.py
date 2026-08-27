from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.model_adapters import (
    ModelAdapterError,
    ModelContext,
    OpenAICompatibleModelAdapter,
    get_model_adapter,
)
from app.model_settings import (
    ModelSettingsStore,
    normalize_openai_compatible_base_url,
)
from app.stage_adapters import (
    OllamaLlmStageAdapter,
    get_stage_adapter,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:1234", "http://127.0.0.1:1234"),
        ("http://127.0.0.1:1234/", "http://127.0.0.1:1234"),
        ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234"),
        ("http://127.0.0.1:1234/v1/", "http://127.0.0.1:1234"),
        ("http://localhost:1234", "http://localhost:1234"),
    ],
)
def test_openai_compatible_url_accepts_root_and_v1(value: str, expected: str) -> None:
    assert normalize_openai_compatible_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:1234",
        "http://192.168.1.8:1234",
        "http://localhost.evil.example:1234",
        "http://user:pass@localhost:1234",
        "http://127.0.0.1:1234?token=secret",
        "http://127.0.0.1:1234/v2",
        "not-a-url",
    ],
)
def test_openai_compatible_url_rejects_non_loopback_or_unknown_path(
    value: str,
) -> None:
    with pytest.raises(Exception):
        normalize_openai_compatible_base_url(value)


def test_get_model_adapter_dispatches_openai_compatible() -> None:
    adapter = get_model_adapter(
        "OpenAI-Compatible",
        base_url="http://127.0.0.1:1234/v1/",
        model_name="qwen3-8b",
    )
    assert isinstance(adapter, OpenAICompatibleModelAdapter)
    assert adapter.name == "openai-compatible"
    assert adapter.base_url == "http://127.0.0.1:1234"

    with pytest.raises(ValueError):
        get_model_adapter("openai-compatible")


def test_lmstudio_style_model_identifier_is_accepted() -> None:
    adapter = get_model_adapter(
        "openai-compatible",
        base_url="http://127.0.0.1:1234",
        model_name="qwen3.8-9b@q4_k_m",
    )
    assert adapter.model_name == "qwen3.8-9b@q4_k_m"


def test_request_schemas_accept_openai_compatible_provider() -> None:
    from app.schemas import (
        ModelConnectionRequest,
        ModelSettingsUpdateRequest,
    )

    probe = ModelConnectionRequest(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234",
        model_name="qwen3.8-9b@q4_k_m",
    )
    assert probe.provider == "openai-compatible"

    update = ModelSettingsUpdateRequest(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234",
        model_name="qwen3.8-9b@q4_k_m",
    )
    assert update.provider == "openai-compatible"


def _openai_models_response() -> dict:
    return {
        "object": "list",
        "data": [
            {"id": "qwen3-8b", "object": "model"},
            {"id": "llama-3.1-8b", "object": "model"},
            {"object": "model"},
        ],
    }


def test_openai_compatible_list_models_and_test_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer local-key"
        return httpx.Response(200, json=_openai_models_response())

    adapter = OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234",
        model_name="qwen3-8b",
        api_key="local-key",
        transport=httpx.MockTransport(handler),
    )
    models = adapter.list_models()
    assert [item["name"] for item in models] == ["qwen3-8b", "llama-3.1-8b"]
    probe = adapter.test_connection()
    assert probe["ok"] is True
    assert "OpenAI兼容服务已连接" in probe["message"]
    assert probe["latency_ms"] >= 0


def test_openai_compatible_test_connection_flags_missing_model() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_models_response())

    adapter = OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234",
        model_name="missing-model",
        transport=httpx.MockTransport(handler),
    )
    probe = adapter.test_connection()
    assert probe["ok"] is False
    assert "未找到模型missing-model" in probe["message"]


def _openai_chat_response(content: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
        },
    }


_VALID_OBSERVATIONS = {
    "observations": [
        {
            "technology_id": "tech-mini-led",
            "quote": "这是材料中的原文，必须逐字引用",
            "attitude_level": 4,
            "confidence": 0.8,
        }
    ]
}


def test_openai_compatible_analyze_uses_json_object_mode() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=_openai_chat_response(json.dumps(_VALID_OBSERVATIONS)),
        )

    adapter = OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234",
        model_name="qwen3-8b",
        transport=httpx.MockTransport(handler),
    )
    result = adapter.analyze(
        ModelContext(
            package_id="package-1",
            customer_id="customer-a",
            material_summaries=[],
            master_data={},
        )
    )

    assert captured["model"] == "qwen3-8b"
    assert captured["stream"] is False
    assert captured["temperature"] == 0
    assert captured["max_tokens"] > 0
    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "intent_observations"
    assert response_format["json_schema"]["schema"]["type"] == "object"
    assert [msg["role"] for msg in captured["messages"]] == ["system", "user"]

    assert result["mode"] == "openai-compatible"
    assert result["model"] == "qwen3-8b"
    assert result["observations"][0]["technology_id"] == "tech-mini-led"
    assert result["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
    }
    assert result["done_reason"] == "stop"


def test_openai_compatible_falls_back_without_response_format() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "response_format" in body:
            return httpx.Response(
                200,
                json=_openai_chat_response("not-json-at-all"),
            )
        return httpx.Response(
            200,
            json=_openai_chat_response(json.dumps(_VALID_OBSERVATIONS)),
        )

    adapter = OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234",
        model_name="qwen3-8b",
        transport=httpx.MockTransport(handler),
    )
    result = adapter.analyze(
        ModelContext(
            package_id="package-1",
            customer_id="customer-a",
            material_summaries=[],
            master_data={},
        )
    )

    assert len(requests) == 2
    assert "response_format" not in requests[1]
    assert "只输出JSON对象" in requests[1]["messages"][-1]["content"]
    assert result["mode"] == "openai-compatible"


def test_openai_compatible_http_error_is_sanitized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "server leaked api-key=provider-secret"}},
        )

    adapter = OpenAICompatibleModelAdapter(
        base_url="http://127.0.0.1:1234",
        model_name="qwen3-8b",
        api_key="provider-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelAdapterError) as raised:
        adapter.list_models()
    message = str(raised.value)
    assert "HTTP 401" in message
    assert "provider-secret" not in message


def test_stage_adapter_dispatches_openai_compatible() -> None:
    stage = get_stage_adapter(
        "openai-compatible",
        base_url="http://127.0.0.1:1234",
        model_name="qwen3-8b",
    )
    assert isinstance(stage, OllamaLlmStageAdapter)
    assert stage.name == "openai-compatible-llm"
    assert stage.model_name == "qwen3-8b"
    assert stage._chat.name == "openai-compatible"


def test_settings_store_persists_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    store = ModelSettingsStore(tmp_path, environ={})
    updated = store.update(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234/v1/",
        model_name="qwen3-8b",
    )
    assert updated.provider == "openai-compatible"
    assert updated.base_url == "http://127.0.0.1:1234"

    reloaded = ModelSettingsStore(tmp_path, environ={})
    assert reloaded.snapshot().provider == "openai-compatible"
    assert reloaded.snapshot().base_url == "http://127.0.0.1:1234"
