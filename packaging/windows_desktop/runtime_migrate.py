from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2]


def main() -> None:
    root = resource_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("prepend_sys_path", str(root))
    command.upgrade(config, "head")
