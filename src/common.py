"""Small helpers shared by all pipeline steps: paths, config loading, env vars."""
import re
import unicodedata
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SQL_DIR = ROOT / "sql"

# Intermediate files between pipeline steps (rebuilt every run, not versioned).
BUILD_DIR = ROOT / "build"
SITE_DIR = ROOT / "site"
WEB_DIR = ROOT / "web"

load_dotenv(ROOT / ".env")  # optional local .env; in CI the values come from repo variables


def load_locations() -> list[dict]:
    with open(CONFIG_DIR / "locations.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["locations"]


def load_settings() -> dict:
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def slugify(name: str) -> str:
    """'České Budějovice' -> 'ceske-budejovice' (used for page folders and URLs)."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
