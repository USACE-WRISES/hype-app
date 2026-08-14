# Hyporheic web app (`hype-app`)

A StreamStats-style [Shiny for Python](https://shiny.posit.co/py/) application that runs
the Hyporheic tool's **MODFLOW 6 + MODPATH 7** engine (the vendored headless `hypetool`
core + FloPy) on **Posit Connect Cloud** (Linux). Modeled on the EASI app.

> **Status:** the full interactive app (`app.py`) is built and validated locally — the
> headless engine is parity-tested **bit-identical** to the ArcGIS/CLI engine, and
> K-zones, spatially-varying gradients, and the channel-WSE derivation are all exercised.
> **Still deploy `smoke_app.py` first** to confirm the Connect Cloud sandbox executes the
> bundled Linux binary, then deploy `app.py`.

## Install HYPE Desktop (Windows)

Download `HypeDesktop-win-Setup.exe` from the
[latest release](https://github.com/USACE-WRISES/hype-app/releases/latest) and run it.
Per-user install, no admin rights; requires the Microsoft WebView2 Runtime (ships with
Edge on Windows 10/11). First launch downloads the modeling runtime (~450 MB, resumable,
sha256-verified). After that the app keeps itself current: app updates ship on every push
to `main`, and shell updates arrive with each `v*` release (see `desktop/RELEASING.md`).
What changed in each version: [CHANGELOG.md](CHANGELOG.md), also shown in the app by
clicking the version number.

## What it does

Six stages, shown as the numbered stage bar under the header:

1. **Reach** — Auto: click the upstream and downstream points on a highlighted NHD stream
   (the reach traces itself). Or Manual: draw the centerline from upstream to downstream
   and enter the drainage area (a backwards draw is auto-corrected from the terrain).
2. **Terrain** — the USGS 3DEP DEM downloads **automatically** once the reach is set;
   re-fetch at another resolution or carve a trapezoidal channel under Terrain.
3. **Boundaries** — the four domain sides generate **automatically** from bankfull
   geometry (floodplain width × bankfull depth); edit any side by clicking its line.
4. **Water surface** — run the bundled **HEC-RAS 2025 2D** model, use the auto/drawn
   wetted extent, or upload a WSE raster.
5. **Groundwater** — one run hub: subsurface properties (K, porosity, optional K-zones),
   the model grid (live green/amber/red guardrail), **4-corner** or **spatially-varying**
   gradient boundary conditions, then **Run groundwater model** (MODFLOW 6 + MODPATH 7 on
   the bundled Linux binaries, live log).
6. **Results** — hydraulic head layers, **hyporheic-zone delineation** (particle
   classification into hyporheic / losing / gaining / throughflow, with flow paths and
   zone volumes in 2D + 3D), per-path statistics, and **Download project** (a zip of the
   whole session, organized by stage).

## Layout

```
hype-app/
  smoke_app.py        # ← deploy this FIRST (Connect Cloud subprocess de-risk)
  app.py              # the full interactive app
  hype_app/           # geometry / dem / estimate / run / results / bundle
  hypetool/           # vendored headless engine (no separate install needed)
  bin/linux/          # mf6 (6.7.0), mp7 (7.2.001) — Linux x64, tracked via Git LFS
  requirements.txt    # manylinux wheels only (pip-only; no apt)
  www/styles.css
```

## Deploy to Posit Connect Cloud (VS Code + Posit Publisher)

Deployed straight from VS Code with the **Posit Publisher** extension; the runtime is
pinned to **Python 3.12** by the `.python-version` file at the repo root (the Publisher
reads it into the deployment's Python constraint, and Connect Cloud provisions a matching
3.12 interpreter).

1. **Install** the [Posit Publisher](https://marketplace.visualstudio.com/items?itemName=Posit.publisher)
   VS Code extension and open the `hype-app` folder.
2. **Smoke test first.** In the Publisher panel, **Add Deployment** → select
   **`smoke_app.py`** as the entrypoint (it detects *Shiny / python-shiny*, reads
   `.python-version` = 3.12 and `requirements.txt`), choose **Posit Connect Cloud** as the
   destination, sign in to add the credential, and **Deploy**. Open it, click **Run smoke
   test** → all imports `OK`, `mf6 --version` prints, and the solve reports
   `SUCCESS — heads = [1.0, 0.5, 0.0]` → green light.
3. **Then the app.** Add a second deployment with **`app.py`** as the entrypoint and deploy
   it. It installs `requirements.txt` and imports the vendored `hypetool`.

The Publisher uploads the **local** files (including the materialized `bin/linux/`
binaries), so Git LFS is **not** needed for this path — see *Source control* below.

> If `smoke_app.py` fails with `libgfortran.so.5: cannot open shared object file`, the
> sandbox lacks the gfortran runtime: drop `libgfortran.so.5` / `libquadmath.so.0` /
> `libgcc_s.so.1` into `bin/linux/` (already on `LD_LIBRARY_PATH` via
> `hype_app/run.py::_prepare_linux_bin`) and redeploy, or switch to static MF6 binaries.

### Connect Cloud content settings (for `app.py`)
- **Read/request timeout** → raise toward the 240-min max (a run is minutes).
- **Startup timeout** → raise (first cold import of the geo + FloPy stack is slow).
- **Memory** → 8–16 GB, **CPU** → 2–4 (MODFLOW solve + raster work).
- **Env:** `HYRIVER_CACHE_NAME=/tmp/hype_hyriver.sqlite` (3DEP cache → ephemeral /tmp).

## Source control (GitHub + Git LFS)

The repo lives at **[USACE-WRISES/hype-app](https://github.com/USACE-WRISES/hype-app)**
(public). The ~49 MB Linux `bin/linux/{mf6,mp7}` binaries are tracked with **Git LFS**
(`.gitattributes`), so clones stay lean:
```bash
git lfs install
git add -A && git commit -m "…"
git push
```
Confirm the GitHub file view shows the binaries as "Stored with Git LFS". (LFS matters
only for GitHub and the optional deploy-from-GitHub route; the VS Code Publisher above
bundles the local binaries directly.)

## Local run

```bash
python -m venv .venv && .venv\Scripts\activate      # (POSIX: . .venv/bin/activate)
pip install -r requirements.txt
# Windows dev: point at Windows MODFLOW binaries (bin/linux holds Linux ones):
set HYPE_MODFLOW_BIN=C:\path\to\hype-tool\src\hypetool\bin\modflow
shiny run app.py
```
`bin/linux/` holds **Linux** binaries, so an actual model run only executes on Linux /
Connect Cloud unless `HYPE_MODFLOW_BIN` points at platform-native binaries. The map,
drawing, DEM fetch, and UI work locally on any OS.

## Keeping the engine in sync / refreshing binaries

The `hypetool/` package is vendored from `hype-tool/src/hypetool`. Re-copy it after core
changes (exclude `bin/` and `esri/`). Refresh the Linux binaries with:
```bash
python -m flopy.utils.get_modflow bin/linux --subset mf6,mp7 --ostag linux
```
