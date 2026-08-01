from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import create_app
from app.model_adapters import IntentModelAdapter, OllamaIntentModelAdapter


class FixedOllamaAdapter(IntentModelAdapter):
    name = "ollama"
    model_name = "qwen-test:latest"

    def analyze(self, _context):
        return {
            "mode": "ollama",
            "model": self.model_name,
            "observations": [
                {
                    "technology_id": "tech-mini-led",
                    "generic_platform_id": "platform-27-5k",
                    "function_id": "function-development",
                    "speaker_name": "赵总",
                    "speaker_role": "开发负责人",
                    "speaker_organization": "客户研发",
                    "quote": (
                        "开发负责人表示，Mini LED可以进入样机验证，"
                        "但还需要确认成本。"
                    ),
                    "attitude_level": 3,
                    "adoption_stage": "样品／技术验证",
                    "support_reason": "愿意进入样机验证",
                    "resistance_reason": None,
                    "bottlenecks": ["成本"],
                    "decision_influence": "评估",
                    "confidence": 0.86,
                    "uncertainty_reason": None,
                }
            ],
            "usage": {"eval_count": 42},
        }


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=True,
    )
    with TestClient(app) as test_client:
        login = test_client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": os.environ["INTENT_BAO_ADMIN_PASSWORD"],
            },
        )
        assert login.status_code == 200, login.text
        test_client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )
        yield test_client, app, tmp_path / "data"


def test_model_settings_api_never_echoes_or_persists_api_key(client) -> None:
    test_client, _app, data_dir = client
    initial = test_client.get("/api/model-settings")
    assert initial.status_code == 200
    assert initial.json()["provider"] == "mock"

    updated = test_client.put(
        "/api/model-settings",
        json={
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model_name": "qwen-test:latest",
            "api_key": "do-not-leak-this-key",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["api_key_configured"] is True
    assert "do-not-leak-this-key" not in updated.text
    assert "api_key" not in updated.json()

    config_text = (
        data_dir / "config" / "model-settings.json"
    ).read_text(encoding="utf-8")
    assert "do-not-leak-this-key" not in config_text
    assert "api_key" not in config_text

    invalid = test_client.put(
        "/api/model-settings",
        json={
            "provider": "ollama",
            "base_url": "https://example.com",
            "model_name": "qwen-test:latest",
            "api_key": "must-stay-secret-on-validation-error",
        },
    )
    assert invalid.status_code == 422
    assert "must-stay-secret-on-validation-error" not in invalid.text


def test_model_probe_endpoints_use_backend_and_accept_optional_key(
    client, monkeypatch
) -> None:
    test_client, _app, _data_dir = client
    authorization_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization_headers.append(request.headers.get("Authorization"))
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

    def adapter_factory(name: str, **kwargs):
        assert name == "ollama"
        return OllamaIntentModelAdapter(
            base_url=kwargs["base_url"],
            model_name=kwargs["model_name"],
            api_key=kwargs.get("api_key"),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(main_module, "get_model_adapter", adapter_factory)
    payload = {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model_name": "qwen3:8b",
        "api_key": "temporary-probe-key",
    }
    listed = test_client.post("/api/model-settings/models", json=payload)
    assert listed.status_code == 200, listed.text
    assert listed.json()["models"][0]["name"] == "qwen3:8b"
    checked = test_client.post("/api/model-settings/test", json=payload)
    assert checked.status_code == 200, checked.text
    assert checked.json()["ok"] is True
    assert checked.json()["models"][0]["parameter_size"] == "8B"
    assert authorization_headers == [
        "Bearer temporary-probe-key",
        "Bearer temporary-probe-key",
    ]
    assert "temporary-probe-key" not in listed.text + checked.text


def test_ollama_text_analysis_creates_only_traceable_ai_draft(
    client, monkeypatch
) -> None:
    test_client, _app, _data_dir = client
    formal_count_before = test_client.get(
        "/api/formal-intent-statuses"
    ).json()["total"]
    original_factory = main_module.get_model_adapter

    def adapter_factory(name: str, **kwargs):
        if name == "ollama":
            return FixedOllamaAdapter()
        return original_factory(name, **kwargs)

    monkeypatch.setattr(main_module, "get_model_adapter", adapter_factory)
    configured = test_client.put(
        "/api/model-settings",
        json={
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model_name": "qwen-test:latest",
        },
    )
    assert configured.status_code == 200, configured.text

    created = test_client.post(
        "/api/meeting-packages",
        json={
            "customer_id": "customer-a",
            "title": "Ollama真实文本抽取回归",
            "meeting_date": "2026-07-31",
            "data_scope": "授权脱敏数据",
        },
    )
    assert created.status_code == 201, created.text
    package = created.json()
    quote = (
        "开发负责人表示，Mini LED可以进入样机验证，"
        "但还需要确认成本。"
    )
    uploaded = test_client.post(
        f"/api/meeting-packages/{package['id']}/files",
        data={"material_type": "人工纪要"},
        files={"file": ("minutes.txt", quote.encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text

    analyzed = test_client.post(
        f"/api/meeting-packages/{package['id']}/analyze",
        json={},
    )
    assert analyzed.status_code == 202, analyzed.text
    run = analyzed.json()["analysis_run"]
    assert run["adapter"] == "ollama"
    assert run["model_name"] == "qwen-test:latest"
    assert run["result_summary"]["observation_count"] == 1
    assert "待人工确认" in analyzed.json()["message"]

    observations = test_client.get(
        "/api/intent-observations",
        params={"package_id": package["id"]},
    )
    assert observations.status_code == 200
    items = observations.json()["items"]
    assert len(items) == 1
    assert items[0]["raw_quote"] == quote
    assert items[0]["status"] == "待确认"
    assert items[0]["model_name"] == "qwen-test:latest"
    assert items[0]["ai_original"]["attitude_level"] == 3

    detail = test_client.get(
        f"/api/meeting-packages/{package['id']}"
    ).json()
    assert detail["files"][0]["parse_status"] == "模型已读取"
    assert (
        test_client.get("/api/formal-intent-statuses").json()["total"]
        == formal_count_before
    )
