from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_importing_app_package_has_no_runtime_data_side_effect(tmp_path: Path) -> None:
    data_dir = tmp_path / "must-not-be-created"
    environment = os.environ.copy()
    environment["INTENT_BAO_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        [sys.executable, "-c", "import app; assert callable(app.create_app)"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not data_dir.exists()


def test_importing_app_main_has_no_default_data_side_effect(tmp_path: Path) -> None:
    """`import app.main` 不得实例化应用：模块级 `app = create_app()` 会让
    pytest 收集时对仓库内 server/data 正式库做迁移、种子和建用户，
    把公开测试密码写进正式库（红线 1）。"""
    data_dir = tmp_path / "must-not-be-created"
    environment = os.environ.copy()
    environment["INTENT_BAO_DATA_DIR"] = str(data_dir)

    result = subprocess.run(
        [sys.executable, "-c", "import app.main; assert not hasattr(app.main, 'app')"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not data_dir.exists()
