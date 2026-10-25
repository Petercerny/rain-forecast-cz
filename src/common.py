"""Small helpers shared by all pipeline steps: paths, config loading, env vars."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SQL_DIR = ROOT / "sql"

# Snapshots + cache state live in data/ (a separate git branch in CI, ignored on main).
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
SNAPSHOT_DIR = DATA_DIR / "snapshots"
CACHE_DIR = DATA_DIR / "cache"

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
