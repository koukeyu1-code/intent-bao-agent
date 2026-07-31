from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.model_adapters import (
    ModelAdapterError,
    ModelContext,
    OllamaIntentModelAdapter,
)
from app.model_settings import (
    ModelSettingsError,
    ModelSettingsStore,
    normalize_ollama_base_url,
)
from app.schemas import ModelSettingsUpdateRequest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:11434/", "http://localhost:11434"),
        ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_loopback_ollama_url_is_normalized(value: str, expected: str) -> None:
    assert normalize_ollama_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:11434",
        "http://192.168.1.8:11434",
        "http://localhost.evil.example:11434",
        "http://user:pass@localhost:11434",
        "http://localhost:11434/api",
        "http://localhost:11434?token=secret",
        "not-a-url",
    ],
)
def test_non_loopback_or_ambiguous_ollama_url_is_rejected(value: str) -> None:
    with pytest.raises(ModelSettingsError):
        normalize_ollama_base_url(value)


def test_settings_store_never_persists_or_returns_api_key(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path, environ={})
    updated = store.update(
        provider="ollama",
        base_url="http://localhost:11434/",
        model_name="qwen3:8b",
        api_key="super-secret-key",
    )

    assert updated.api_key == "super-secret-key"
    assert "super-secret-key" not in repr(updated)
    public = store.public_dict()
    assert public["api_key_configured"] is True
    assert "api_key" not in public
    assert "super-secret-key" not in json.dumps(public, ensure_ascii=False)

    persisted = store.config_path.read_text(encoding="utf-8")
    assert "super-secret-key" not in persisted
    assert '"provider": "ollama"' in persisted
    assert '"model_name": "qwen3:8b"' in persisted

    restarted = ModelSettingsStore(tmp_path, environ={})
    assert restarted.snapshot().provider == "ollama"
    assert restarted.snapshot().api_key is None
    assert restarted.public_dict()["api_key_configured"] is False


def test_settings_store_can_clear_in_memory_api_key(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path, environ={})
    store.update(
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        api_key="temporary-key",
    )
    cleared = store.update(
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        clear_api_key=True,
    )
    assert cleared.api_key is None
    assert store.public_dict()["api_key_configured"] is False


def test_settings_validation_error_does_not_echo_api_key() -> None:
    secret = "validation-secret-must-not-appear"
    with pytest.raises(ValidationError) as raised:
        ModelSettingsUpdateRequest.model_validate(
            {
                "provider": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model_name": "qwen3:8b",
                "api_key": secret,
                "clear_api_key": True,
            }
        )
    assert secret not in raised.value.json()


def test_ollama_lists_models_and_uses_optional_bearer_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tags"
        assert request.headers["authorization"] == "Bearer local-proxy-key"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "qwen3:8b",
                        "model": "qwen3:8b",
                        "size": 5_000_000_000,
                        "details": {
                            "parameter_size": "8B",
                            "quantization_level": "Q4_K_M",
                        },
                    }
                ]
            },
        )

    adapter = OllamaIntentModelAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        api_key="local-proxy-key",
        transport=httpx.MockTransport(handler),
    )
    models = adapter.list_models()
    assert models == [
        {
            "name": "qwen3:8b",
            "model": "qwen3:8b",
            "size": 5_000_000_000,
            "parameter_size": "8B",
            "quantization_level": "Q4_K_M",
        }
    ]
    probe = adapter.test_connection()
    assert probe["ok"] is True
    assert probe["models"][0]["name"] == "qwen3:8b"
    assert probe["latency_ms"] >= 0


def test_ollama_chat_uses_json_schema_and_returns_validated_dict() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/chat"
        captured.update(json.loads(request.content))
        content = {
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
        }
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": json.dumps(content)},
                "prompt_eval_count": 100,
                "eval_count": 50,
            },
        )

    adapter = OllamaIntentModelAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        transport=httpx.MockTransport(handler),
    )
    result = adapter.analyze(
        ModelContext(
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
    )

    assert captured["model"] == "qwen3:8b"
    assert captured["stream"] is False
    assert captured["options"]["temperature"] == 0
    assert captured["format"]["type"] == "object"
    assert "minutes.txt" in captured["messages"][1]["content"]
    assert result["mode"] == "ollama"
    assert result["model"] == "qwen3:8b"
    assert result["observations"][0]["attitude_level"] == 5
    assert result["usage"] == {"prompt_eval_count": 100, "eval_count": 50}


def test_ollama_rejects_invalid_structured_output_without_echoing_content() -> None:
    secret_response = "customer-secret-should-not-be-returned"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "observations": [
                                {
                                    "technology_id": "tech-mini-led",
                                    "quote": secret_response,
                                    "attitude_level": 9,
                                    "confidence": 0.8,
                                }
                            ]
                        }
                    ),
                }
            },
        )

    adapter = OllamaIntentModelAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        api_key="api-secret-should-not-be-returned",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelAdapterError) as raised:
        adapter.analyze(
            ModelContext(
                package_id="package-1",
                customer_id="customer-a",
                material_summaries=[],
                master_data={},
            )
        )
    message = str(raised.value)
    assert "结构契约" in message
    assert secret_response not in message
    assert "api-secret-should-not-be-returned" not in message


def test_ollama_http_error_is_sanitized() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": "server leaked api-key=provider-secret"},
        )

    adapter = OllamaIntentModelAdapter(
        base_url="http://127.0.0.1:11434",
        model_name="qwen3:8b",
        api_key="provider-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelAdapterError) as raised:
        adapter.list_models()
    assert "HTTP 401" in str(raised.value)
    assert "provider-secret" not in str(raised.value)
