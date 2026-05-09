"""Shared pytest setup.

Adds ``src/`` to ``sys.path`` so tests can ``from bot.notifier import ...``
without requiring the package to be installed. Also provides minimal env
defaults so ``config.get_settings()`` doesn't fail when imported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test"
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("API_BASE", "https://example.test")
os.environ.setdefault("NOTIFY_COOLDOWN_SEC", "600")
