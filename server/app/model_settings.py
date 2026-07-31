from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"
ALLOWED_PROVIDERS = {"mock", "ollama"}
_ALLOWED_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class ModelSettingsError(ValueError):
    """Safe, user-facing validation error for local model settings."""


def normalize_ollama_base_url(value: str) -> str:
    """Validate and normalize a loopback-only Ollama base URL.

    The settings page is intentionally unable to turn the backend into an
    arbitrary HTTP proxy. Enterprise or remote providers require a separate,
    administratively approved configuration path.
    """

    candidate = (value or "").strip()
    if not candidate:
        raise ModelSettingsError("Ollama地址不能为空")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ModelSettingsError("Ollama地址格式无效") from exc

    if parsed.scheme.lower() != "http":
        raise ModelSettingsError("本地Ollama仅允许使用http地址")
    if (parsed.hostname or "").lower() not in _ALLOWED_LOOPBACK_HOSTS:
        raise ModelSettingsError("本地Ollama地址必须是localhost或回环地址")
    if parsed.username is not None or parsed.password is not None:
        raise ModelSettingsError("Ollama地址不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ModelSettingsError("Ollama地址不得包含查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ModelSettingsError("Ollama地址只填写服务根地址，例如http://127.0.0.1:11434")
    if port is not None and not 1 <= port <= 65535:
        raise ModelSettingsError("Ollama端口必须在1到65535之间")

    host = (parsed.hostname or "").lower()
    display_host = f"[{host}]" if host == "::1" else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    return urlunsplit(("http", netloc, "", "", ""))


def validate_model_name(value: str) -> str:
    model_name = (value or "").strip()
    if not _MODEL_NAME_PATTERN.fullmatch(model_name):
        raise ModelSettingsError(
            "模型名称必须为1到200位，只能包含字母、数字、点、下划线、斜杠、冒号或连字符"
        )
    return model_name


@dataclass(frozen=True)
class RuntimeModelSettings:
    provider: str
    base_url: str
    model_name: str
    api_key: str | None = field(default=None, repr=False, compare=False)

    def public_dict(
        self,
        *,
        connection_status: str = "unknown",
        last_checked_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "api_key_configured": bool(self.api_key),
            "connection_status": connection_status,
            "last_checked_at": last_checked_at,
            "last_error": last_error,
        }


class ModelSettingsStore:
    """Thread-safe runtime model settings with non-secret disk persistence.

    Only provider, base URL and model name are written to disk. The API key is
    retained in process memory (or supplied through an environment variable)
    and is never included in public snapshots.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        self._lock = threading.RLock()
        self._environ = os.environ if environ is None else environ
        self._config_dir = Path(data_dir).resolve() / "config"
        self._config_path = self._config_dir / "model-settings.json"
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._settings = self._load()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def _load(self) -> RuntimeModelSettings:
        provider = self._environ.get("INTENT_BAO_MODEL_ADAPTER", "mock").strip().lower()
        base_url = self._environ.get(
            "INTENT_BAO_OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
        )
        model_name = self._environ.get(
            "INTENT_BAO_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL
        )

        if self._config_path.exists():
            try:
                stored = json.loads(self._config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ModelSettingsError("模型配置文件不可读取或格式错误") from exc
            if not isinstance(stored, dict):
                raise ModelSettingsError("模型配置文件格式错误")
            provider = str(stored.get("provider", provider)).strip().lower()
            base_url = str(stored.get("base_url", base_url))
            model_name = str(stored.get("model_name", model_name))

        if provider not in ALLOWED_PROVIDERS:
            raise ModelSettingsError("模型提供方仅支持mock或ollama")
        base_url = normalize_ollama_base_url(base_url)
        model_name = validate_model_name(model_name)
        api_key = (
            self._environ.get("INTENT_BAO_MODEL_API_KEY")
            or self._environ.get("OLLAMA_API_KEY")
            or None
        )
        return RuntimeModelSettings(
            provider=provider,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
        )

    def snapshot(self) -> RuntimeModelSettings:
        with self._lock:
            return self._settings

    def public_dict(
        self,
        *,
        connection_status: str = "unknown",
        last_checked_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        return self.snapshot().public_dict(
            connection_status=connection_status,
            last_checked_at=last_checked_at,
            last_error=last_error,
        )

    def update(
        self,
        *,
        provider: str,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> RuntimeModelSettings:
        normalized_provider = (provider or "").strip().lower()
        if normalized_provider not in ALLOWED_PROVIDERS:
            raise ModelSettingsError("模型提供方仅支持mock或ollama")
        normalized_url = normalize_ollama_base_url(base_url)
        normalized_model = validate_model_name(model_name)
        if clear_api_key and api_key:
            raise ModelSettingsError("不能同时设置和清除API Key")

        with self._lock:
            current_key = self._settings.api_key
            if clear_api_key:
                next_key = None
            elif api_key is not None:
                next_key = api_key.strip() or None
            else:
                next_key = current_key

            updated = RuntimeModelSettings(
                provider=normalized_provider,
                base_url=normalized_url,
                model_name=normalized_model,
                api_key=next_key,
            )
            self._persist_non_secret(updated)
            self._settings = updated
            return updated

    def _persist_non_secret(self, settings: RuntimeModelSettings) -> None:
        payload = {
            "provider": settings.provider,
            "base_url": settings.base_url,
            "model_name": settings.model_name,
        }
        temporary = self._config_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._config_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ModelSettingsError("模型配置保存失败") from exc
