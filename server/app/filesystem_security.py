from __future__ import annotations

import csv
import io
import os
import subprocess
from pathlib import Path
from typing import Callable


class DataDirectorySecurityError(RuntimeError):
    """Raised when the local sensitive-data directory cannot be protected."""


def _is_windows() -> bool:
    return os.name == "nt"


def _current_windows_identity() -> str:
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DataDirectorySecurityError(
            "无法识别当前 Windows 用户，已停止打开本地数据目录。"
        ) from exc
    try:
        row = next(csv.reader(io.StringIO(result.stdout)))
        sid = row[1].strip() if len(row) >= 2 else ""
    except (csv.Error, StopIteration):
        sid = ""
    if not sid.startswith("S-1-") or any(character.isspace() for character in sid):
        raise DataDirectorySecurityError(
            "当前 Windows 用户标识无效，已停止打开本地数据目录。"
        )
    # Prefixing a SID with * tells icacls not to resolve it as an account name.
    return f"*{sid}"


def _configured_windows_owner() -> str | None:
    """Return the interactive account carried into the local process.

    Sandboxed launchers can run the child process under a restricted token
    while retaining the desktop user's USERNAME/USERDOMAIN.  Granting both the
    configured owner and the effective process SID keeps the files available
    to the user who owns the local workspace without granting generic Users.
    """

    username = os.getenv("USERNAME", "").strip()
    domain = os.getenv("USERDOMAIN", "").strip()
    if not username:
        return None
    identity = f"{domain}\\{username}" if domain else username
    if any(character in identity for character in ":()\r\n"):
        raise DataDirectorySecurityError(
            "Windows 数据目录所有者标识无效，已停止启动。"
        )
    return identity


def harden_data_directory(
    data_dir: str | Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Restrict a Windows data tree to its owner and trusted system accounts.

    Browser roles cannot protect SQLite files and uploaded meeting materials
    from another operating-system user.  On Windows, remove inherited ACLs and
    grant access only to the current identity, SYSTEM and Administrators before
    the database or uploaded files are opened.  Tests may opt out explicitly;
    production startup fails closed if ACL hardening is unavailable.
    """

    if not _is_windows():
        return
    if os.getenv("INTENT_BAO_SKIP_WINDOWS_ACL_HARDENING") == "1":
        return

    path = Path(data_dir).resolve()
    if not path.is_dir():
        raise DataDirectorySecurityError("本地数据目录不存在，无法设置安全权限。")
    identities = [
        item
        for item in (
            _configured_windows_owner(),
            _current_windows_identity(),
            "*S-1-5-18",
            "*S-1-5-32-544",
        )
        if item
    ]
    identities = list(dict.fromkeys(identities))
    # First give every existing file an explicit ACE while removing inherited
    # generic Users entries.  A second pass adds inheritable ACEs to every
    # directory so future SQLite and upload files are protected on creation.
    commands = [
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            *(f"{identity}:F" for identity in identities),
            "/T",
            "/Q",
        ],
        [
            "icacls.exe",
            str(path),
            "/grant",
            *(f"{identity}:(OI)(CI)F" for identity in identities),
            "/T",
            "/Q",
        ],
    ]
    for command in commands:
        try:
            result = runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DataDirectorySecurityError(
                "无法收紧本地数据目录权限，已为安全起见停止启动。"
            ) from exc
        if result.returncode != 0:
            raise DataDirectorySecurityError(
                "本地数据目录权限设置失败，已为安全起见停止启动。"
            )


__all__ = ["DataDirectorySecurityError", "harden_data_directory"]
