"""Deterministic process-local authentication settings for backend tests."""

from __future__ import annotations

import os


# These values exist only inside the disposable pytest process. Production
# startup has no source-controlled fallback password.
os.environ["INTENT_BAO_ADMIN_PASSWORD"] = "test-only-admin-8f2a41c9"
os.environ["INTENT_BAO_USER_PASSWORD"] = "test-only-planner-6d3b70e5"
# Disposable pytest directories contain no customer material.  Avoid invoking
# icacls for every isolated test database; the ACL command itself has focused
# unit coverage in test_filesystem_security.py.
os.environ["INTENT_BAO_SKIP_WINDOWS_ACL_HARDENING"] = "1"


def wait_for_analysis(client, package_id, run_id=None, timeout=30.0):
    """Poll package tasks until the (latest) analysis run reaches a terminal state.

    ``POST /analyze`` now returns HTTP 202 as soon as the run is persisted;
    the eight stage tasks are executed by a background worker.  Tests that
    previously read results synchronously must wait for terminal tasks.
    """
    import time

    terminal = {"已完成", "不适用", "已取消", "处理失败"}
    deadline = time.monotonic() + timeout
    last_statuses: list[str] = []
    while time.monotonic() < deadline:
        response = client.get(f"/api/meeting-packages/{package_id}/tasks")
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        run_items = (
            [item for item in items if item["analysis_run_id"] == run_id]
            if run_id is not None
            else items
        )
        last_statuses = [item["status"] for item in run_items]
        if run_items and all(item["status"] in terminal for item in run_items):
            return response.json()
        time.sleep(0.1)
    raise AssertionError(
        f"分析任务在 {timeout:.0f}s 内未完成: run_id={run_id} "
        f"statuses={last_statuses}"
    )
