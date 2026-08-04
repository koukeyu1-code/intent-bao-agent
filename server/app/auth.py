from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Final, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, String, delete, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .database import Base, get_db


ROLE_ADMIN: Final = "admin"
ROLE_USER: Final = "user"
ROLE_VIEWER: Final = "viewer"
VALID_ROLES: Final = frozenset({ROLE_ADMIN, ROLE_USER, ROLE_VIEWER})

DEFAULT_SESSION_HOURS: Final = 12
PASSWORD_SCHEME: Final = "pbkdf2_sha256"
PASSWORD_ITERATIONS: Final = 260_000
PASSWORD_SALT_BYTES: Final = 16
SESSION_TOKEN_BYTES: Final = 32
INITIAL_PASSWORD_TOKEN_BYTES: Final = 24
INITIAL_CREDENTIALS_DIRECTORY: Final = "initial-login-credentials"
LOGIN_FAILURE_LIMIT: Final = 5
LOGIN_FAILURE_WINDOW_SECONDS: Final = 5 * 60
LOGIN_LOCK_SECONDS: Final = 60
LOGIN_TRACKED_KEYS_LIMIT: Final = 2048


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class AppUser(Base):
    """A local application identity.

    Passwords are never stored directly. ``password_hash`` contains a
    versioned PBKDF2-HMAC-SHA256 string so the iteration count can be upgraded
    without a schema change.
    """

    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSession(Base):
    """A revocable opaque login session.

    Only a SHA-256 digest of the random browser token is persisted. A copied
    database therefore cannot be used directly as a browser session.
    """

    __tablename__ = "app_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    display_name: str
    role: Literal["admin", "user", "viewer"]


class LoginResponse(BaseModel):
    user: UserResponse
    expires_at: datetime
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class MessageResponse(BaseModel):
    message: str


@dataclass(frozen=True)
class DefaultUserSeedResult:
    """Result of the create-only bootstrap for local default identities."""

    users: dict[str, AppUser]
    created_usernames: tuple[str, ...]
    generated_credentials: dict[str, str]


def normalize_username(value: str) -> str:
    return value.strip().casefold()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(
    password: str,
    *,
    iterations: int = PASSWORD_ITERATIONS,
    salt: bytes | None = None,
) -> str:
    """Return a versioned PBKDF2-HMAC-SHA256 password representation."""

    if not password:
        raise ValueError("密码不能为空")
    if iterations < 100_000:
        raise ValueError("PBKDF2迭代次数过低")
    resolved_salt = salt or secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt,
        iterations,
    )
    return "$".join(
        (PASSWORD_SCHEME, str(iterations), _b64encode(resolved_salt), _b64encode(digest))
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a password without leaking malformed-hash or timing errors."""

    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000:
            return False
        salt = _b64decode(raw_salt)
        expected = _b64decode(raw_digest)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


def _token_digest(token: str) -> str:
    # Issued tokens are ASCII, but an attacker controls the Authorization
    # header. UTF-8 keeps malformed non-ASCII input on the normal 401 path
    # instead of turning it into an unhandled UnicodeEncodeError.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _prune_login_failure_entries(
    entries: dict[tuple[str, str], tuple[list[float], float | None]],
    *,
    now: float,
    incoming_key: tuple[str, str],
) -> None:
    """Expire stale throttle keys and reserve bounded space for a new key."""

    for key, (failures, locked_until) in list(entries.items()):
        if locked_until is not None:
            if locked_until <= now:
                entries.pop(key, None)
            continue
        recent = [
            item
            for item in failures
            if now - item <= LOGIN_FAILURE_WINDOW_SECONDS
        ]
        if recent:
            entries[key] = (recent, None)
        else:
            entries.pop(key, None)

    while incoming_key not in entries and len(entries) >= LOGIN_TRACKED_KEYS_LIMIT:
        oldest_key = min(
            entries,
            key=lambda item: entries[item][0][-1]
            if entries[item][0]
            else float("-inf"),
        )
        entries.pop(oldest_key, None)


def _issue_session(
    session: Session,
    user: AppUser,
    *,
    session_hours: int,
) -> tuple[str, AppSession]:
    now = utcnow()
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    login_session = AppSession(
        user_id=user.id,
        token_hash=_token_digest(token),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=session_hours),
    )
    user.last_login_at = now
    session.add(login_session)
    session.commit()
    session.refresh(login_session)
    return token, login_session


def _extract_session_token(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "").strip()
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.casefold() == "bearer" and token.strip():
        return token.strip()
    return None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="请先登录",
        headers={"WWW-Authenticate": "Bearer"},
    )


DbSession = Annotated[Session, Depends(get_db)]


def require_user(
    request: Request,
    session: DbSession,
) -> AppUser:
    """Resolve the authenticated user exclusively from a bearer token."""

    token = _extract_session_token(request)
    if not token:
        raise _unauthorized()

    now = utcnow()
    row = session.execute(
        select(AppSession, AppUser)
        .join(AppUser, AppUser.id == AppSession.user_id)
        .where(
            AppSession.token_hash == _token_digest(token),
            AppSession.revoked_at.is_(None),
            AppSession.expires_at > now,
        )
    ).one_or_none()
    if row is None:
        raise _unauthorized()

    _login_session, user = row
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前账户已停用",
        )
    # Authentication checks stay read-only.  This is important for endpoints
    # such as Excel preflight whose contract promises zero database writes.
    # The session already carries its issue time and absolute expiry.
    return user


CurrentUser = Annotated[AppUser, Depends(require_user)]


def require_roles(*roles: str):
    """Build a FastAPI dependency that permits only the supplied roles."""

    allowed = frozenset(roles)
    if not allowed or not allowed.issubset(VALID_ROLES):
        raise ValueError("require_roles必须包含至少一个有效角色")

    def dependency(user: CurrentUser) -> AppUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="当前账户无权执行此操作",
            )
        return user

    return dependency


def audit_actor(user: AppUser) -> str:
    """Return the server-trusted label stored in business audit records."""

    return f"{user.display_name}（{user.username}）"


def generate_initial_password() -> str:
    """Generate a high-entropy password for an otherwise unconfigured user."""

    return secrets.token_urlsafe(INITIAL_PASSWORD_TOKEN_BYTES)


def upsert_local_user(
    session: Session,
    *,
    username: str,
    display_name: str,
    role: str,
    password: str,
) -> AppUser:
    """Create a missing local user and leave an existing identity untouched.

    The historical name is retained for compatibility.  This function is
    deliberately create-only: application startup must never reactivate an
    account, change its role, or rotate its password as a side effect.
    """

    normalized_username = normalize_username(username)
    if not normalized_username:
        raise ValueError("用户名不能为空")
    if role not in VALID_ROLES:
        raise ValueError(f"无效角色：{role}")
    user = session.scalar(
        select(AppUser).where(AppUser.username == normalized_username)
    )
    if user is not None:
        return user
    if len(password) < 8:
        raise ValueError("密码至少需要8个字符")

    user = AppUser(
        username=normalized_username,
        display_name=display_name.strip() or normalized_username,
        role=role,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _seed_one_default_user(
    session: Session,
    *,
    username: str,
    display_name: str,
    role: str,
    configured_password: str | None,
) -> tuple[AppUser, bool, str | None]:
    normalized_username = normalize_username(username)
    existing = session.scalar(
        select(AppUser).where(AppUser.username == normalized_username)
    )
    if existing is not None:
        return existing, False, None

    password = configured_password or generate_initial_password()
    user = upsert_local_user(
        session,
        username=normalized_username,
        display_name=display_name,
        role=role,
        password=password,
    )
    generated_password = None if configured_password else password
    return user, True, generated_password


def seed_default_users(
    session: Session,
    *,
    admin_password: str | None = None,
    user_password: str | None = None,
    commit: bool = True,
) -> DefaultUserSeedResult:
    """Create missing default identities without mutating existing accounts.

    Environment-provided passwords are used only when an account is first
    created.  Missing values are replaced with strong random passwords and
    returned to the caller for local, ignored credential-file delivery.
    """

    admin, admin_created, generated_admin_password = _seed_one_default_user(
        session,
        username="admin",
        display_name="系统管理员",
        role=ROLE_ADMIN,
        configured_password=admin_password,
    )
    user, user_created, generated_user_password = _seed_one_default_user(
        session,
        username="planner",
        display_name="产品规划负责人",
        role=ROLE_USER,
        configured_password=user_password,
    )
    generated_credentials = {
        username: password
        for username, password in (
            ("admin", generated_admin_password),
            ("planner", generated_user_password),
        )
        if password is not None
    }
    created_usernames = tuple(
        username
        for username, created in (
            ("admin", admin_created),
            ("planner", user_created),
        )
        if created
    )
    result = DefaultUserSeedResult(
        users={ROLE_ADMIN: admin, ROLE_USER: user},
        created_usernames=created_usernames,
        generated_credentials=generated_credentials,
    )
    if commit:
        session.commit()
        for seeded_user in result.users.values():
            session.refresh(seeded_user)
    return result


def seed_default_users_from_env(
    session: Session,
    *,
    commit: bool = True,
) -> DefaultUserSeedResult:
    """Create missing identities from env, generating any absent password."""

    return seed_default_users(
        session,
        admin_password=os.getenv("INTENT_BAO_ADMIN_PASSWORD"),
        user_password=os.getenv("INTENT_BAO_USER_PASSWORD"),
        commit=commit,
    )


def initial_credentials_directory(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve() / "config" / INITIAL_CREDENTIALS_DIRECTORY


def write_generated_credentials(
    data_dir: str | Path,
    result: DefaultUserSeedResult,
) -> Path | None:
    """Persist newly generated passwords outside Git-tracked application code.

    One file per account avoids overwriting another account's still-needed
    initial password if a missing default identity is recreated later.
    Password values are never logged by this helper.
    """

    return write_local_credentials(data_dir, result.generated_credentials)


def write_local_credentials(
    data_dir: str | Path,
    credentials: dict[str, str],
) -> Path | None:
    """Write supplied local credentials without printing their values."""

    if not credentials:
        return None
    directory = initial_credentials_directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    generated_at = utcnow().isoformat()
    for username, password in credentials.items():
        normalized_username = normalize_username(username)
        if normalized_username not in {"admin", "planner"}:
            raise ValueError("只能写入受控的默认本地账号凭据")
        path = directory / f"{normalized_username}.txt"
        temporary_path = (
            directory
            / f".{normalized_username}-{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "意向宝本地初始登录凭据\n"
                    f"账号：{normalized_username}\n"
                    f"初始密码：{password}\n"
                    f"生成时间：{generated_at}\n\n"
                    "请仅在本机使用。保存到个人密码管理器后删除此文件。\n"
                )
            os.replace(temporary_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                # The Windows launcher additionally applies a current-user ACL.
                pass
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return directory


def reset_local_password(
    session: Session,
    *,
    username: str,
    new_password: str,
    commit: bool = True,
) -> AppUser:
    """Explicitly reset one local password and revoke all of its sessions.

    This is intentionally separate from application startup.  It preserves
    the account's role and active flag so recovery cannot silently elevate or
    reactivate an identity.
    """

    if len(new_password) < 12:
        raise ValueError("新密码至少需要12个字符")
    normalized_username = normalize_username(username)
    user = session.scalar(
        select(AppUser).where(AppUser.username == normalized_username)
    )
    if user is None:
        raise ValueError(f"本地账号不存在：{normalized_username}")
    user.password_hash = hash_password(new_password)
    now = utcnow()
    active_sessions = session.scalars(
        select(AppSession).where(
            AppSession.user_id == user.id,
            AppSession.revoked_at.is_(None),
        )
    ).all()
    for login_session in active_sessions:
        login_session.revoked_at = now
    if commit:
        session.commit()
        session.refresh(user)
    return user


def create_auth_router(
    *,
    session_hours: int = DEFAULT_SESSION_HOURS,
) -> APIRouter:
    if session_hours <= 0:
        raise ValueError("session_hours必须大于0")

    router = APIRouter(prefix="/api/auth", tags=["登录与权限"])
    unknown_user_password_hash = hash_password(secrets.token_urlsafe(24))
    failed_logins: dict[tuple[str, str], tuple[list[float], float | None]] = {}
    failed_logins_lock = threading.Lock()

    def login_key(request: Request, username: str) -> tuple[str, str]:
        client_host = request.client.host if request.client else "unknown"
        return client_host, username

    def is_login_limited(key: tuple[str, str], now: float) -> bool:
        with failed_logins_lock:
            failures, locked_until = failed_logins.get(key, ([], None))
            if locked_until is not None:
                if locked_until > now:
                    return True
                failed_logins.pop(key, None)
                return False
            recent = [
                item
                for item in failures
                if now - item <= LOGIN_FAILURE_WINDOW_SECONDS
            ]
            if recent:
                failed_logins[key] = (recent, None)
            else:
                failed_logins.pop(key, None)
            return False

    def record_login_failure(key: tuple[str, str], now: float) -> None:
        with failed_logins_lock:
            _prune_login_failure_entries(
                failed_logins,
                now=now,
                incoming_key=key,
            )
            failures, _locked_until = failed_logins.get(key, ([], None))
            recent = [
                item
                for item in failures
                if now - item <= LOGIN_FAILURE_WINDOW_SECONDS
            ]
            recent.append(now)
            locked_until = (
                now + LOGIN_LOCK_SECONDS
                if len(recent) >= LOGIN_FAILURE_LIMIT
                else None
            )
            failed_logins[key] = (recent, locked_until)

    def clear_login_failures(key: tuple[str, str]) -> None:
        with failed_logins_lock:
            failed_logins.pop(key, None)

    def clear_stale_sessions(session: Session) -> None:
        now = utcnow()
        session.execute(
            delete(AppSession).where(
                or_(
                    AppSession.expires_at <= now,
                    AppSession.revoked_at.is_not(None),
                )
            )
        )
        session.commit()

    @router.post("/login", response_model=LoginResponse)
    def login(
        request: Request,
        response: Response,
        payload: LoginRequest,
        session: DbSession,
    ) -> LoginResponse:
        username = normalize_username(payload.username)
        clear_stale_sessions(session)
        attempt_key = login_key(request, username)
        attempt_time = time.monotonic()
        if is_login_limited(attempt_key, attempt_time):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="登录尝试过于频繁，请稍后重试",
            )
        user = session.scalar(
            select(AppUser).where(AppUser.username == username)
        )
        password_matches = verify_password(
            payload.password,
            user.password_hash if user is not None else unknown_user_password_hash,
        )
        if (
            user is None
            or not password_matches
            or not user.is_active
        ):
            record_login_failure(attempt_key, attempt_time)
            # Keep the error identical for unknown usernames and bad passwords.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )
        clear_login_failures(attempt_key)
        token, login_session = _issue_session(
            session, user, session_hours=session_hours
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return LoginResponse(
            user=UserResponse.model_validate(user),
            expires_at=login_session.expires_at,
            access_token=token,
        )

    @router.post("/logout", response_model=MessageResponse)
    def logout(
        request: Request,
        session: DbSession,
        _user: CurrentUser,
    ) -> MessageResponse:
        token = _extract_session_token(request)
        if token:
            login_session = session.scalar(
                select(AppSession).where(
                    AppSession.token_hash == _token_digest(token),
                    AppSession.revoked_at.is_(None),
                )
            )
            if login_session is not None:
                login_session.revoked_at = utcnow()
                session.commit()
        return MessageResponse(message="已退出登录")

    @router.get("/me", response_model=UserResponse)
    def me(user: CurrentUser) -> UserResponse:
        return UserResponse.model_validate(user)

    return router


auth_router = create_auth_router()


__all__ = [
    "AppSession",
    "AppUser",
    "CurrentUser",
    "DefaultUserSeedResult",
    "ROLE_ADMIN",
    "ROLE_USER",
    "ROLE_VIEWER",
    "audit_actor",
    "auth_router",
    "create_auth_router",
    "generate_initial_password",
    "hash_password",
    "initial_credentials_directory",
    "require_roles",
    "require_user",
    "reset_local_password",
    "seed_default_users",
    "seed_default_users_from_env",
    "upsert_local_user",
    "verify_password",
    "write_generated_credentials",
    "write_local_credentials",
]
