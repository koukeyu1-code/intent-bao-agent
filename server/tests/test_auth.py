from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import auth_admin
from app import main as main_module
from app.auth import (
    LOGIN_TRACKED_KEYS_LIMIT,
    AppSession,
    AppUser,
    ROLE_ADMIN,
    ROLE_USER,
    ROLE_VIEWER,
    audit_actor,
    _prune_login_failure_entries,
    _token_digest,
    create_auth_router,
    initial_credentials_directory,
    require_roles,
    require_user,
    reset_local_password,
    seed_default_users,
    utcnow,
    verify_password,
    write_generated_credentials,
)
from app.database import Database
from app.main import create_app


ADMIN_PASSWORD = "Admin-Test-2026!"
USER_PASSWORD = "Planner-Test-2026!"


@pytest.fixture()
def auth_app(tmp_path: Path) -> FastAPI:
    database = Database(f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    # Importing app.auth registers AppUser/AppSession on the shared metadata.
    database.create_all()
    with database.session() as session:
        seed_default_users(
            session,
            admin_password=ADMIN_PASSWORD,
            user_password=USER_PASSWORD,
        )

    app = FastAPI()
    app.state.database = database
    app.include_router(create_auth_router(session_hours=1))

    @app.get("/protected")
    def protected(user: Annotated[AppUser, Depends(require_user)]) -> dict:
        return {"actor": audit_actor(user), "role": user.role}

    @app.get("/admin-only")
    def admin_only(
        user: Annotated[AppUser, Depends(require_roles(ROLE_ADMIN))],
    ) -> dict:
        return {"username": user.username}

    return app


@pytest.fixture()
def client(auth_app: FastAPI):
    with TestClient(auth_app) as test_client:
        yield test_client


def test_password_hash_and_default_seed_are_safe_and_idempotent(
    auth_app: FastAPI,
) -> None:
    database: Database = auth_app.state.database
    with database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        assert admin.password_hash != ADMIN_PASSWORD
        assert verify_password(ADMIN_PASSWORD, admin.password_hash)
        assert not verify_password("wrong-password", admin.password_hash)
        original_password_hash = admin.password_hash
        admin.display_name = "停用的原管理员"
        admin.role = ROLE_VIEWER
        admin.is_active = False
        session.commit()

        repeated = seed_default_users(
            session,
            admin_password="Ignored-New-Password!",
            user_password="Ignored-New-Password!",
        )
        assert session.scalar(select(func.count()).select_from(AppUser)) == 2
        refreshed = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert refreshed is not None
        assert refreshed.password_hash == original_password_hash
        assert refreshed.display_name == "停用的原管理员"
        assert refreshed.role == ROLE_VIEWER
        assert refreshed.is_active is False
        assert repeated.created_usernames == ()
        assert repeated.generated_credentials == {}


def test_missing_passwords_generate_local_ignored_credentials(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'generated.db').as_posix()}")
    database.create_all()
    with database.session() as session:
        result = seed_default_users(session)
        assert result.created_usernames == ("admin", "planner")
        assert set(result.generated_credentials) == {"admin", "planner"}
        assert result.generated_credentials["admin"] != result.generated_credentials[
            "planner"
        ]
        assert all(
            len(password) >= 32
            for password in result.generated_credentials.values()
        )
        for role, username in ((ROLE_ADMIN, "admin"), (ROLE_USER, "planner")):
            assert verify_password(
                result.generated_credentials[username],
                result.users[role].password_hash,
            )

        credentials_dir = write_generated_credentials(tmp_path / "data", result)
        assert credentials_dir is not None
        for username, password in result.generated_credentials.items():
            content = (credentials_dir / f"{username}.txt").read_text(
                encoding="utf-8"
            )
            assert f"账号：{username}" in content
            assert f"初始密码：{password}" in content
            assert "保存到个人密码管理器后删除此文件" in content
    database.engine.dispose()


def test_create_app_restart_does_not_rotate_or_reactivate_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'restart.db').as_posix()}"
    data_dir = tmp_path / "data"
    first_admin_password = "First-Test-Admin-Password!"
    first_user_password = "First-Test-User-Password!"
    monkeypatch.setenv("INTENT_BAO_ADMIN_PASSWORD", first_admin_password)
    monkeypatch.setenv("INTENT_BAO_USER_PASSWORD", first_user_password)
    first_app = create_app(
        database_url=database_url,
        data_dir=data_dir,
        seed=False,
    )
    with first_app.state.database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        admin.display_name = "已停用管理员"
        admin.role = ROLE_VIEWER
        admin.is_active = False
        session.commit()
    first_app.state.database.engine.dispose()

    monkeypatch.setenv(
        "INTENT_BAO_ADMIN_PASSWORD", "Second-Test-Admin-Password!"
    )
    monkeypatch.setenv(
        "INTENT_BAO_USER_PASSWORD", "Second-Test-User-Password!"
    )
    second_app = create_app(
        database_url=database_url,
        data_dir=data_dir,
        seed=False,
    )
    with second_app.state.database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        assert admin.display_name == "已停用管理员"
        assert admin.role == ROLE_VIEWER
        assert admin.is_active is False
        assert verify_password(first_admin_password, admin.password_hash)
        assert not verify_password(
            "Second-Test-Admin-Password!", admin.password_hash
        )
    second_app.state.database.engine.dispose()


def test_create_app_without_password_env_writes_working_initial_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INTENT_BAO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("INTENT_BAO_USER_PASSWORD", raising=False)
    app = create_app(
        database_url=f"sqlite:///{(tmp_path / 'generated-app.db').as_posix()}",
        data_dir=tmp_path / "data",
        seed=False,
    )
    credentials_dir = app.state.initial_credentials_directory
    credentials: dict[str, str] = {}
    for username in ("admin", "planner"):
        path = credentials_dir / f"{username}.txt"
        assert path.is_file()
        password_line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("初始密码：")
        )
        credentials[username] = password_line.removeprefix("初始密码：")

    with TestClient(app) as client:
        for username in ("admin", "planner"):
            client.cookies.clear()
            response = client.post(
                "/api/auth/login",
                json={"username": username, "password": credentials[username]},
            )
            assert response.status_code == 200, response.text
    app.state.database.engine.dispose()


def test_credential_write_failure_does_not_strand_random_password_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INTENT_BAO_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("INTENT_BAO_USER_PASSWORD", raising=False)
    database_url = f"sqlite:///{(tmp_path / 'write-failure.db').as_posix()}"

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated credential storage failure")

    monkeypatch.setattr(main_module, "write_generated_credentials", fail_write)
    with pytest.raises(OSError, match="credential storage failure"):
        main_module.create_app(
            database_url=database_url,
            data_dir=tmp_path / "data",
            seed=False,
        )

    database = Database(database_url)
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(AppUser)) == 0
    database.engine.dispose()


def test_auth_admin_generate_writes_password_only_to_controlled_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_password = "Legacy-Test-Admin-Password!"
    monkeypatch.setenv("INTENT_BAO_ADMIN_PASSWORD", old_password)
    monkeypatch.setenv(
        "INTENT_BAO_USER_PASSWORD", "Legacy-Test-Planner-Password!"
    )
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, seed=False)
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": old_password},
        )
        assert logged_in.status_code == 200
    with app.state.database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        admin.role = ROLE_VIEWER
        admin.is_active = False
        session.commit()
    app.state.database.engine.dispose()
    capsys.readouterr()

    exit_code = auth_admin.main(
        ["admin", "--data-dir", str(data_dir), "--generate"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    credentials_file = (
        initial_credentials_directory(data_dir) / "admin.txt"
    )
    assert credentials_file.is_file()
    generated_password = next(
        line.removeprefix("初始密码：")
        for line in credentials_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("初始密码：")
    )
    assert len(generated_password) >= 32
    assert str(credentials_file) in captured.out
    assert generated_password not in captured.out
    assert "初始密码：" not in captured.out

    database = Database(
        f"sqlite:///{(data_dir / 'database' / 'intent_bao.db').as_posix()}"
    )
    with database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        assert admin.role == ROLE_VIEWER
        assert admin.is_active is False
        assert verify_password(generated_password, admin.password_hash)
        assert not verify_password(old_password, admin.password_hash)
        sessions = session.scalars(
            select(AppSession).where(AppSession.user_id == admin.id)
        ).all()
        assert sessions
        assert all(item.revoked_at is not None for item in sessions)
    database.engine.dispose()


def test_login_returns_one_time_bearer_without_setting_cookie_and_me_uses_it(
    client: TestClient,
    auth_app: FastAPI,
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": " PLANNER ", "password": USER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"] == {
        "id": response.json()["user"]["id"],
        "username": "planner",
        "display_name": "产品规划负责人",
        "role": "user",
    }
    token = response.json()["access_token"]
    assert response.json()["token_type"] == "bearer"
    assert len(token) >= 40
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert not client.cookies

    # A browser cookie is deliberately ignored; only the explicit bearer works.
    client.cookies.set("intent_bao_test_session", token)
    cookie_only = client.get("/api/auth/me")
    client.cookies.clear()
    assert cookie_only.status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "planner"

    protected = client.get("/protected", headers=headers)
    assert protected.status_code == 200
    assert protected.json() == {
        "actor": "产品规划负责人（planner）",
        "role": "user",
    }
    with auth_app.state.database.session() as session:
        stored = session.scalar(select(AppSession))
        assert stored is not None
        assert stored.token_hash != token


def test_malformed_non_ascii_bearer_token_stays_on_safe_digest_path() -> None:
    digest = _token_digest("非ASCII伪造令牌")
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)


def test_bad_credentials_and_missing_session_return_401(
    client: TestClient,
) -> None:
    missing = client.get("/api/auth/me")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"

    unknown = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "Any-Password-2026!"},
    )
    bad_password = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": "Wrong-Password-2026!"},
    )
    assert unknown.status_code == 401
    assert bad_password.status_code == 401
    assert unknown.json() == bad_password.json() == {
        "detail": "用户名或密码错误"
    }


def test_login_throttles_by_client_and_username_without_account_disclosure(
    client: TestClient,
) -> None:
    unknown = client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "Wrong-Password-2026!"},
    )
    existing = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": "Wrong-Password-2026!"},
    )
    assert unknown.status_code == existing.status_code == 401
    assert unknown.json() == existing.json() == {
        "detail": "用户名或密码错误"
    }

    for _ in range(4):
        response = client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": "Wrong-Password-2026!",
            },
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    assert limited.status_code == 429
    assert limited.json() == {"detail": "登录尝试过于频繁，请稍后重试"}


def test_login_throttle_tracking_has_a_hard_global_bound() -> None:
    entries = {
        ("127.0.0.1", f"user-{index}"): ([1900.0 + index / 10_000], None)
        for index in range(LOGIN_TRACKED_KEYS_LIMIT)
    }
    incoming = ("127.0.0.1", "new-user")
    _prune_login_failure_entries(entries, now=2050.0, incoming_key=incoming)
    entries[incoming] = ([2050.0], None)
    assert len(entries) == LOGIN_TRACKED_KEYS_LIMIT
    assert incoming in entries
    assert ("127.0.0.1", "user-0") not in entries


def test_successful_login_clears_prior_failure_counter(client: TestClient) -> None:
    for _ in range(2):
        assert client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": "Wrong-Password-2026!",
            },
        ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    ).status_code == 200
    for _ in range(4):
        assert client.post(
            "/api/auth/login",
            json={
                "username": "planner",
                "password": "Wrong-Password-2026!",
            },
        ).status_code == 401


def test_login_removes_expired_and_revoked_sessions(
    client: TestClient,
    auth_app: FastAPI,
) -> None:
    first = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    first_token = first.json()["access_token"]
    assert client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {first_token}"},
    ).status_code == 200

    second = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    second_token = second.json()["access_token"]
    with auth_app.state.database.session() as session:
        active = session.scalar(
            select(AppSession).where(
                AppSession.token_hash.is_not(None),
                AppSession.revoked_at.is_(None),
            )
        )
        assert active is not None
        active.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()

    third = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    assert third.status_code == 200
    assert third.json()["access_token"] not in {first_token, second_token}
    with auth_app.state.database.session() as session:
        sessions = session.scalars(select(AppSession)).all()
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None


def test_role_dependency_returns_403_for_normal_user(client: TestClient) -> None:
    logged_in = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    assert logged_in.status_code == 200
    planner_headers = {
        "Authorization": f"Bearer {logged_in.json()['access_token']}"
    }

    denied = client.get("/admin-only", headers=planner_headers)
    assert denied.status_code == 403
    assert denied.json() == {"detail": "当前账户无权执行此操作"}

    admin_login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert admin_login.status_code == 200
    admin_headers = {
        "Authorization": f"Bearer {admin_login.json()['access_token']}"
    }
    allowed = client.get("/admin-only", headers=admin_headers)
    assert allowed.status_code == 200
    assert allowed.json() == {"username": "admin"}


def test_logout_revokes_bearer_session(
    client: TestClient,
    auth_app: FastAPI,
) -> None:
    logged_in = client.post(
        "/api/auth/login",
        json={"username": "planner", "password": USER_PASSWORD},
    )
    assert logged_in.status_code == 200
    raw_token = logged_in.json()["access_token"]
    assert raw_token

    bearer = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
    )
    assert bearer.status_code == 200

    # Log out the exact bearer session and verify both API and database state.
    logged_out = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert logged_out.status_code == 200
    assert logged_out.json() == {"message": "已退出登录"}
    denied = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {raw_token}"}
    )
    assert denied.status_code == 401

    database: Database = auth_app.state.database
    with database.session() as session:
        stored_sessions = session.scalars(select(AppSession)).all()
        assert len(stored_sessions) == 1
        assert stored_sessions[0].revoked_at is not None


def test_explicit_password_reset_preserves_role_and_active_state(
    client: TestClient,
    auth_app: FastAPI,
) -> None:
    logged_in = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert logged_in.status_code == 200
    raw_token = logged_in.json()["access_token"]

    database: Database = auth_app.state.database
    with database.session() as session:
        admin = session.scalar(
            select(AppUser).where(AppUser.username == "admin")
        )
        assert admin is not None
        admin.role = ROLE_VIEWER
        admin.is_active = False
        session.commit()
        reset_local_password(
            session,
            username="admin",
            new_password="Replacement-Test-Password!",
        )
        assert admin.role == ROLE_VIEWER
        assert admin.is_active is False
        assert verify_password(
            "Replacement-Test-Password!", admin.password_hash
        )
        assert not verify_password(ADMIN_PASSWORD, admin.password_hash)
        active_sessions = session.scalars(
            select(AppSession).where(AppSession.user_id == admin.id)
        ).all()
        assert active_sessions
        assert all(item.revoked_at is not None for item in active_sessions)

    assert client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    ).status_code == 401
