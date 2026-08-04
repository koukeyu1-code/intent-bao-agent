from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import filesystem_security as security


def test_non_windows_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(security, "_is_windows", lambda: False)
    security.harden_data_directory(tmp_path, runner=runner)
    assert called is False


def test_windows_acl_is_restricted_to_owner_and_system_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[list[str]] = []

    def runner(command, **_kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(security, "_is_windows", lambda: True)
    monkeypatch.setattr(
        security, "_current_windows_identity", lambda: "*S-1-5-21-123-456-789-1001"
    )
    monkeypatch.setattr(
        security, "_configured_windows_owner", lambda: "WORKSTATION\\planner"
    )
    monkeypatch.delenv("INTENT_BAO_SKIP_WINDOWS_ACL_HARDENING", raising=False)

    security.harden_data_directory(tmp_path, runner=runner)

    assert len(captured) == 2
    assert captured[0][:2] == ["icacls.exe", str(tmp_path.resolve())]
    assert "/inheritance:r" in captured[0]
    assert "WORKSTATION\\planner:F" in captured[0]
    assert "*S-1-5-21-123-456-789-1001:F" in captured[0]
    assert "*S-1-5-18:F" in captured[0]
    assert "*S-1-5-32-544:F" in captured[0]
    assert "WORKSTATION\\planner:(OI)(CI)F" in captured[1]
    assert "*S-1-5-21-123-456-789-1001:(OI)(CI)F" in captured[1]
    assert "/T" in captured[1]


def test_windows_acl_failure_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 5, "", "access denied")

    monkeypatch.setattr(security, "_is_windows", lambda: True)
    monkeypatch.setattr(security, "_current_windows_identity", lambda: "user")
    monkeypatch.setattr(security, "_configured_windows_owner", lambda: None)
    monkeypatch.delenv("INTENT_BAO_SKIP_WINDOWS_ACL_HARDENING", raising=False)

    with pytest.raises(security.DataDirectorySecurityError):
        security.harden_data_directory(tmp_path, runner=runner)
