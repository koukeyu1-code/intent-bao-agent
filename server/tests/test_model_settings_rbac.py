from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


MODEL_SETTINGS_REQUESTS = (
    ("get", "/api/model-settings", None),
    (
        "put",
        "/api/model-settings",
        {
            "provider": "mock",
            "base_url": "http://127.0.0.1:11434",
            "model_name": "mock-intent-v1",
        },
    ),
    ("post", "/api/model-settings/test", {"provider": "mock"}),
    ("post", "/api/model-settings/models", {"provider": "mock"}),
)


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _request(
    client: TestClient, method: str, path: str, payload: dict | None
):
    return client.request(method, path, json=payload)


def _login(client: TestClient, username: str, password_env: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": os.environ[password_env],
        },
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Authorization": f"Bearer {response.json()['access_token']}"}
    )


@pytest.mark.parametrize("method,path,payload", MODEL_SETTINGS_REQUESTS)
def test_model_settings_require_login(
    client: TestClient, method: str, path: str, payload: dict | None
) -> None:
    response = _request(client, method, path, payload)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("method,path,payload", MODEL_SETTINGS_REQUESTS)
def test_planner_cannot_access_model_settings(
    client: TestClient, method: str, path: str, payload: dict | None
) -> None:
    _login(client, "planner", "INTENT_BAO_USER_PASSWORD")

    response = _request(client, method, path, payload)

    assert response.status_code == 403, response.text


def test_admin_can_manage_and_probe_model_settings(client: TestClient) -> None:
    _login(client, "admin", "INTENT_BAO_ADMIN_PASSWORD")

    initial = client.get("/api/model-settings")
    assert initial.status_code == 200, initial.text

    updated = client.put(
        "/api/model-settings",
        json={
            "provider": "mock",
            "base_url": "http://127.0.0.1:11434",
            "model_name": "mock-intent-v1",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["provider"] == "mock"

    tested = client.post(
        "/api/model-settings/test", json={"provider": "mock"}
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True

    models = client.post(
        "/api/model-settings/models", json={"provider": "mock"}
    )
    assert models.status_code == 200, models.text
    assert models.json()["models"] == []
