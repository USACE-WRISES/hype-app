# HYPE test suite

First-party tests for the HYPE revision (`notes/HYPE_revision_plan.md`). Established in Phase 0
to pin current behavior *before* the numerical changes in later phases.

## Running

```bash
# from the repo root, using the project venv
.venv/Scripts/python.exe -m pytest            # fast: unit + import (live/engine auto-skipped)
.venv/Scripts/python.exe -m pytest -m "not slow"
HYPE_LIVE_TESTS=1 .venv/Scripts/python.exe -m pytest -m live      # real USGS/NRCS calls
HYPE_MODFLOW_BIN=/path/to/bin .venv/Scripts/python.exe -m pytest -m engine   # real mf6/mp7
```

## Markers (see `pytest.ini`, gated in `conftest.py`)

- `live` — hits real external services; skipped unless `HYPE_LIVE_TESTS=1`.
- `engine` — needs native MODFLOW6/MODPATH7 binaries; skipped unless `HYPE_MODFLOW_BIN` is set
  (the bundled `bin/linux` binaries only run on Linux; on Windows fetch native ones via
  `python -m flopy.utils.get_modflow <dir>`).
- `slow` — long-running (full app import, model runs).

## Current output contracts (documented so later phases don't silently break them)

### Units — the `_ft` column labels are cosmetic; values are METRIC
The engine (`hypetool`) hardcodes `feet` defaults, but the app forces `length_units="meters",
time_units="days"` (`app.py` `params()`), and the pipeline never converts. So despite names like
`hyporheic_volume_cubic_ft`, `particle_velocity_ft_per_day`, `total_length_ft`, the values are
**metres / m³ / m·day⁻¹**. See `hype_app/results.py` (unit note near the top of `flowpath_stats`).
The newer hyporheic-zone path already uses honest metric names (`volume_m3`, `footprint_m2`,
`residence_time_days`, `length_m`).

### Hyporheic-zone classification (`hz_analysis.py`)
`classify_particles` joins forward+backward MODPATH7 endpoint records per particle and classifies
by origin (backward terminus) × exit (forward terminus) membership:

| origin | exit | class | code |
|---|---|---|---|
| top | top | hyporheic | `CLS["hyporheic"]` = 1 |
| top | side | losing | 2 |
| side | top | gaining | 3 |
| side | side | throughflow | 4 |
| (unresolved status or internal terminus) | | unresolved | 0 |

`top` = `CHD_RIVER` (stream) membership; `side` = a `CHD_SIDES` face. Resolved requires MP7 status
∈ {2,3,4,5,6} on both directions. Pinned in `test_hz_classification.py`.

### Volumes / footprint (`hz_analysis.py`)
`cell_volumes` returns **saturated bulk** cell volume (thickness clipped to head × cell area),
**with no porosity applied** — Phase 5 adds a separate mobile pore-water storage
(= Σ fraction · saturated cell volume · effective porosity), keeping bulk volume alongside it.
`cell_class_fractions` splits each cell's volume by classified-particle share (the streamtube
rule; unresolved seeds don't dilute). `class_stats` reports `volume_m3`, `footprint_m2` (binary,
grid/particle-resolution dependent), and thickness. Pinned in `test_hz_classification.py`.

### Gradient profiles (`my_utils.py`)
`parse_fraction_gradient_profile` parses a `"frac,grad frac,grad …"` string into sorted pairs and
requires fractions 0 and 1. `compute_boundary_heads_from_profile` builds anchor heads
(`head = WSE_edge + gradient · distance`) then interpolates along the boundary; the corner mode uses
`interpolate_gw_elevation_first_layer_only` (straight lerp between two corner heads). Phase 4
replaces the raw strings with structured control lists but must reproduce these heads for a given
equivalent profile. Pinned in `test_gradient_profile.py`.

## Fixtures

- `fixtures/usgs/`, `fixtures/nrcs/` — recorded service responses (Phase 2).
- `fixtures/model/` — small deterministic MODFLOW6/MODPATH7 workspace for `engine` tests
  (Phase 0/5).
