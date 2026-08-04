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
