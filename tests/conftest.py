"""Shared pytest fixtures for the copybot test suite.

Goal: each test runs against an isolated SQLite DB so we never touch the real
`data/copybot.db`. The strategy is to monkeypatch `DB_PATH` everywhere it's
been imported (config + schema), then call `init_db()` to materialize the
schema in the temp file.

Note: several modules read config constants at import time (e.g. `risk.py`
reads `EFFECTIVE_CAPITAL_USDC = LIVE_CAPITAL_USDC if LIVE_MODE else BOT_CAPITAL_USDC`).
We accept those defaults (paper mode, BOT_CAPITAL=100, kill threshold = -$10)
because changing LIVE_MODE after import has no effect on those bound constants.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# Ensure the repo root is importable regardless of pytest invocation cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Create an empty SQLite DB in tmp_path and rewire all readers to it.

    Returns the path to the DB file.
    """
    db_file = tmp_path / "copybot_test.db"

    # Patch the DB_PATH everywhere it has been imported. `schema._connect()`
    # reads the module-level binding at call time, so patching the schema
    # module's binding is what actually matters. We patch config too for
    # completeness in case other code paths read it directly.
    import src.config as config_mod
    import src.db.schema as schema_mod

    monkeypatch.setattr(config_mod, "DB_PATH", db_file)
    monkeypatch.setattr(schema_mod, "DB_PATH", db_file)

    schema_mod.init_db()
    return db_file


@pytest.fixture
def now_ts() -> int:
    """Stable 'now' timestamp for inserting fake trade rows."""
    return int(time.time())
