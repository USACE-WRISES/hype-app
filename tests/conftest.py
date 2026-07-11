"""Shared pytest fixtures + collection hooks for the HYPE test suite.

Two environment gates keep the default `pytest` run fast and offline on any dev box:

* ``@pytest.mark.live``   — skipped unless ``HYPE_LIVE_TESTS=1`` (real USGS/NRCS calls).
* ``@pytest.mark.engine`` — skipped unless ``HYPE_MODFLOW_BIN`` points at native mf6/mp7
                            (the bundled ``bin/linux`` binaries only run on Linux).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable (app.py, hype_app/, hypetool/) regardless of the
# invoking cwd, so `import app` / `import hypetool...` resolve during collection.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


def _has_engine_binaries() -> bool:
    return bool(os.getenv("HYPE_MODFLOW_BIN"))


def pytest_collection_modifyitems(config, items):
    live_on = os.getenv("HYPE_LIVE_TESTS") == "1"
    engine_on = _has_engine_binaries()
    skip_live = pytest.mark.skip(reason="live service test — set HYPE_LIVE_TESTS=1 to run")
    skip_engine = pytest.mark.skip(
        reason="engine test — set HYPE_MODFLOW_BIN to a dir with native mf6/mp7")
    for item in items:
        if "live" in item.keywords and not live_on:
            item.add_marker(skip_live)
        if "engine" in item.keywords and not engine_on:
            item.add_marker(skip_engine)
