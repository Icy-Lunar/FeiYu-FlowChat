"""Application paths and immutable runtime metadata.

All writable paths derive from ``FLOWCHAT_DATA_DIR``.  Keeping that decision
in one module makes tests and portable deployments independent from the source
tree without scattering environment-variable lookups across the application.
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DATA_DIR = Path(os.getenv("FLOWCHAT_DATA_DIR", PROJECT_ROOT / "data")).resolve()
ARCHIVE_DIR = DATA_DIR / "archives"
RAW_DIR = DATA_DIR / "raw"
VERSIONS_DIR = DATA_DIR / "versions"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
SETTINGS_PATH = DATA_DIR / "settings.json"
PROFILES_PATH = DATA_DIR / "provider_profiles.json"

APP_NAME = "肥鱼 FlowChat"
APP_VERSION = "0.5.1"


def ensure_data_dirs() -> None:
    for directory in (ARCHIVE_DIR, RAW_DIR, VERSIONS_DIR, ATTACHMENTS_DIR, BACKUP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    (ARCHIVE_DIR / ".trash").mkdir(parents=True, exist_ok=True)
