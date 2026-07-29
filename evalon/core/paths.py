"""Default local paths used by Evalon."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DB_PATH = Path.home() / ".evalon" / "evalon-runs.sqlite"


def default_db_path() -> Path:
    """Return the configured local trace database path."""
    return Path(os.getenv("EVALON_DB", DEFAULT_DB_PATH)).expanduser()
