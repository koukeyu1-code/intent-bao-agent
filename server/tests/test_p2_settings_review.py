"""HTTP regressions for key-only deletion and reviewed specification persistence."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.auth import AppUser, ROLE_VIEWER
from app.main import create_app


TEST_KEY = "disposable-test-key-not-a-live-credential"
SETTINGS_PAYLOAD = {
    "provider": "openai-compatible",
    "base_url": "http://127.0.0.1:8090/v1",
    "model_name": "local-intent-test",
    "api_key": TEST_KEY,
    "asr_provider": "llamacpp",
    "asr_base_url": "http://127.0.0.1:8099",
    "asr_model_name": "local-asr-test",
}


@pytest.fixture
def settings_client(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'settings.db').as_posix()}",
        data_dir=tmp_path / "data", seed=False,
    )
    with TestClient(app) as client:
        yield client
    app.state.database.engine.dispose()


def _login(client, username):
    password_env = "INTENT_BAO_ADMIN_PASSWORD" if username == "admin" else "INTENT_BAO_USER_PASSWORD"
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": os.environ[password_env]},
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"


def test_admin_deletes_only_api_key_and_repeat_is_idempotent(settings_client):
    client = settings_client
    _login(client, "admin")
    saved = client.put("/api/model-settings", json=SETTINGS_PAYLOAD)
    assert saved.status_code == 200, saved.text
    before = saved.json()
    assert before["api_key_configured"] is True
    store = client.app.state.model_settings
    persisted_before = store.config_path.read_bytes()
    assert store.snapshot().api_key == TEST_KEY

    first = client.delete("/api/model-settings/api-key")
    assert first.status_code == 200, first.text
    cleared = first.json()
    assert cleared["api_key_configured"] is False
    for field in ("provider", "base_url", "model_name", "asr"):
        assert cleared[field] == before[field]
    assert store.snapshot().api_key is None
    assert store.config_path.read_bytes() == persisted_before

    second = client.delete("/api/model-settings/api-key")
    assert second.status_code == 200, second.text
    assert second.json() == cleared
    refreshed = client.get("/api/model-settings")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == cleared
    assert TEST_KEY not in saved.text + first.text + second.text + refreshed.text
    assert TEST_KEY.encode() not in persisted_before


@pytest.mark.parametrize("role,status", [(None, 401), ("planner", 403), ("viewer", 403)])
def test_api_key_deletion_requires_admin_and_keeps_key_on_denial(settings_client, role, status):
    client = settings_client
    store = client.app.state.model_settings
    store.update(**SETTINGS_PAYLOAD)
    original = store.snapshot()
    persisted_before = store.config_path.read_bytes()
    if role == "viewer":
        with client.app.state.database.session() as session:
            user = session.scalar(select(AppUser).where(AppUser.username == "planner"))
            user.role = ROLE_VIEWER
            session.commit()
    if role is not None:
        _login(client, "planner")

    denied = client.delete("/api/model-settings/api-key")
    assert denied.status_code == status, denied.text
    assert store.snapshot() == original
    assert store.snapshot().api_key == TEST_KEY
    assert store.config_path.read_bytes() == persisted_before
    assert TEST_KEY not in denied.text


def test_review_highlight_spec_survives_add_edit_clear_and_bootstrap(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'review.db').as_posix()}",
        data_dir=tmp_path / "data", seed=True,
    )
    observation_id = "obs-b-product"
    with app.state.database.session() as session:
        session.get(m.RawIntentObservation, observation_id).highlight_spec = None
        session.commit()
    with TestClient(app) as client:
        _login(client, "planner")
        path = f"/api/intent-observations/{observation_id}"
        before = client.get(path)
        assert before.status_code == 200, before.text
        version = before.json()["version"]
        assert before.json()["highlight_spec"] is None
        for value in ("27英寸 QHD 240Hz，优先验证动态清晰度", "31.5英寸 UHD 180Hz，补充响应时间要求", None):
            saved = client.put(
                f"{path}/review-draft",
                json={"version": version, "highlight_spec": value},
            )
            assert saved.status_code == 200, saved.text
            observation = saved.json()["observation"]
            assert observation["highlight_spec"] == value
            assert observation["version"] == version + 1
            version += 1

            refreshed = client.get(path)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["highlight_spec"] == value
            assert refreshed.json()["version"] == version
            bootstrap = client.get("/api/bootstrap")
            assert bootstrap.status_code == 200, bootstrap.text
            listed = next(item for item in bootstrap.json()["intents"] if item["id"] == observation_id)
            assert listed["highlightSpec"] == (value or "")
            with app.state.database.session() as session:
                assert session.get(m.RawIntentObservation, observation_id).highlight_spec == value
    app.state.database.engine.dispose()
