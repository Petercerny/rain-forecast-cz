"""Vercel entrypoint: the FastAPI app lives in src/web_app.py (so tests and `uvicorn` can import it too)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `import src...` work in the function

from src.web_app import app  # noqa: E402,F401  (Vercel looks for a module-level `app`)
