"""Compile/import smoke: every first-party module (and the full Shiny app) imports clean.

This is the cheap regression net for the monolith — a syntax error or a broken top-level
import anywhere in hype_app/ or hypetool/ fails here before any behavioral test runs.
"""
import importlib

import pytest

# hype_app package modules referenced by app.py's import block, plus the untracked
# hyporheic-zone subsystem (hz_run/hz_results/scene/ui_tree/carve) and run/ras.
HYPE_APP_MODULES = [
    "hype_app.bieger",
    "hype_app.bundle",
    "hype_app.carve",
    "hype_app.delineate",
    "hype_app.dem",
    "hype_app.estimate",
    "hype_app.geocode",
    "hype_app.geometry",
    "hype_app.hydro",
    "hype_app.hz_results",
    "hype_app.hz_run",
    "hype_app.mesh",
    "hype_app.ras",
    "hype_app.ras_results",
    "hype_app.results",
    "hype_app.run",
    "hype_app.scene",
    "hype_app.ui_tree",
]

HYPETOOL_MODULES = [
    "hypetool.inputs",
    "hypetool.functions.my_utils",
    "hypetool.functions.hz_analysis",
    "hypetool.core.run_from_yaml",
    "hypetool.core.run_headless",
]


@pytest.mark.parametrize("mod", HYPE_APP_MODULES + HYPETOOL_MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)


@pytest.mark.slow
def test_app_module_imports():
    """The whole Shiny app constructs at import time (App(app_ui, server, ...))."""
    app = importlib.import_module("app")
    assert app.app is not None
