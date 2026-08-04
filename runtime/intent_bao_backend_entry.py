import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(_BACKEND_ROOT))
from app.main import create_app

app = create_app(data_dir=str(_BACKEND_ROOT / "data"))
