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
