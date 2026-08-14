"""The stage runner: build one site's model end to end, resumable per stage.

Every engine call is the same public function the app itself uses. Stage
artifacts persist under the site's work_dir, so stages can run in separate
invocations and a failed stage can be retried without redoing earlier ones.

Usage:
  python tools/site_factory/drive.py LL01096 --stages geometry,terrain
  python tools/site_factory/drive.py LL01096 --stages ras
  python tools/site_factory/drive.py LL01096 --stages gw,hz,results,bundle
  python tools/site_factory/drive.py LL01096 --auto     # hash-diff -> minimal stages
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

os.environ.setdefault("HYPE_DESKTOP", "1")
os.environ.setdefault("HYPE_RAS_BIN", r"D:\Code\Work\hype-app\reference\HEC-RAS_2025\HEC-RAS 2025 Alpha")
os.environ.setdefault("HYPE_MODFLOW_BIN", r"D:\Code\Work\hype-tool\bin\modflow")

HYPE_MODELS = Path(r"D:\Code\Work\hypoerheic-texas-sites\hype_models")
SITES_ROOT = Path(r"D:\Code\Work\hypoerheic-texas-sites\Sites")

STAGE_ORDER = ["geometry", "terrain", "ras", "gw", "hz", "results", "aerials", "bundle"]
# Which stages a changed dependency group invalidates (mirrors the app's cascade).
# aerials is in NO group: a from-scratch run copies once, --auto never re-selects
# it, and `--stages aerials` re-syncs manually.
_INVALIDATABLE = [s for s in STAGE_ORDER if s != "aerials"]
GROUP_STAGES = {
    "geometry": _INVALIDATABLE,
    "terrain": _INVALIDATABLE[1:],
    "streamflow": ["ras", "gw", "hz", "results", "bundle"],
    "soil_k": ["gw", "hz", "results", "bundle"],
    "gradients": ["gw", "hz", "results", "bundle"],
    "grid": ["gw", "hz", "results", "bundle"],
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def make_ras_progress_logger(log, clock=time.time, heartbeat_s: float = 300.0):
    """Progress callback for ras.run_surface_model: stage headers, 10% milestones with
    elapsed/ETA, and a heartbeat line at least every `heartbeat_s` while percent creeps
    inside one band. The engine eats per-percent Progress lines, and big meshes can sit
    below 10% for half an hour; without the heartbeat the log looks frozen and a healthy
    solve gets mistaken for a wedge (SS02107: 24 quiet minutes, then finished fine)."""
    last = {"stage": None, "pct": -10, "t0": clock(), "logged": clock()}

    def _prog(stage, pct):
        now = clock()
        if stage != last["stage"]:
            last.update(stage=stage, pct=-10, t0=now, logged=now)
            log(f"[{stage}]")
        if pct is None:
            return
        el = (now - last["t0"]) / 60.0
        eta = (f", ~{el / pct * (100 - pct):.0f} min left" if 0 < pct < 100 else "")
        if pct >= last["pct"] + 10:
            last["pct"] = pct - (pct % 10)
            last["logged"] = now
            log(f"[{stage}] {pct}% ({el:.1f} min elapsed{eta})")
        elif now - last["logged"] >= heartbeat_s:
            last["logged"] = now
            log(f"[{stage}] still at {pct}% ({el:.1f} min elapsed{eta})")

    return _prog


def jread(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def jwrite(p, obj):
    Path(p).write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def results_state(work_dir: Path, inputs: Path, site_id: str) -> dict:
    """The run/results state keys the app's restore consumes (app.py _rehydrate):
    ras_result re-arms Surface Water and its overlays, run_result unlocks the
    Results stage (restore also requires model/gwf_workspace on disk), hz_result
    unlocks Report, results_model rehydrates the canonical results, wse_used
    records the raster the GW run consumed. Shapes mirror the app's own
    _project_state save (the *_fc fields restore as null there too). Everything
    derives from durable workspace artifacts rather than in-memory run returns,
    so a bundle-only rerun hydrates sites built before this existed. Paths are
    ABSOLUTE here; the caller tokenizes to $WORKSPACE$ form before merging.
    """
    st: dict = {}
    rr = jread(inputs / "ras_result.json") if (inputs / "ras_result.json").exists() else None
    if rr and rr.get("depth_tif") and Path(rr["depth_tif"]).is_file():
        st["ras_result"] = rr
    if (inputs / "wse_filter.json").exists():
        st["wse_used"] = jread(inputs / "wse_filter.json").get("path")
    elif rr:
        st["wse_used"] = rr.get("wse_for_gw")
    head = work_dir / "summary" / "head"
    tifs = sorted(str(p) for p in (head / "per_layer_tif").glob("head_L*.tif"))
    gwj = inputs / "gw_result.json"
    if tifs and (work_dir / "model" / "gwf_workspace").is_dir() and gwj.exists():
        nc, ncm = head / "head_zyx.nc", head / "model_vars.nc"
        st["run_result"] = {
            "points_fc": None, "pathlines_fc": None,
            "pathlines_fc_3d": None, "pathlines_fc_3d_full": None,
            "head": {"netcdf": str(nc) if nc.exists() else None,
                     "mosaic_gdb": None, "mosaic_dataset": None,
                     "geotiffs": tifs,
                     "netcdf_multi": str(ncm) if ncm.exists() else None},
            "contours": {},
            "group_name": f"hyporheic Results {site_id}",
            "grid": (jread(gwj) or {}).get("grid"),
        }
    if (inputs / "hz_result.json").exists():
        hz = jread(inputs / "hz_result.json")
        if hz.get("hz_dir") and Path(hz["hz_dir"]).is_dir():
            st["hz_result"] = hz
    if (work_dir / "assessment_results.json").exists():
        st["results_model"] = jread(work_dir / "assessment_results.json")
    return st


class Driver:
    def __init__(self, site_id: str):
        from tools.site_factory import master

        rows = master.read_sites()
        if site_id not in rows:
            raise SystemExit(f"{site_id} not in inputs_master.xlsx")
        self.site_id = site_id
        self.row = rows[site_id]
        self.site_dir = SITES_ROOT / site_id
        self.work_dir = HYPE_MODELS / site_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.inputs = self.work_dir / "inputs"
        self.inputs.mkdir(exist_ok=True)

    # ---------------------------------------------------------------- pieces
    def geom(self):
        return jread(self.inputs / "geometry.json")

    def crs(self):
        from hype_app import geometry as geo

        dom = geo.single_feature_gdf(self.geom()["domain"])
        crs = geo.pick_projected_crs(dom)
        return crs, (crs.to_epsg() or crs.to_wkt())

    def wells_rows(self):
        """WELLS sheet rows for this site ([] when the sheet is absent or legacy)."""
        from tools.site_factory import master

        return master.read_wells().get(self.site_id) or []

    def wells_lonlat(self):
        """(lon, lat, name) triples for every sheet well with coordinates (any include)."""
        out = []
        for r in self.wells_rows():
            try:
                lon, lat = float(r["lon"]), float(r["lat"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append((lon, lat, str(r.get("name") or r.get("obs_name") or "")))
        return out

    def gradient_config(self):
        from hype_app.contracts.gradients import GradientBoundaryConfigV2, GradientControl

        gl = float(self.row["gradient_left"])
        gr = float(self.row["gradient_right"])
        return GradientBoundaryConfigV2(
            mode="quantitative",
            left_controls=[
                GradientControl(id="L0", side="left", station=0.0, preferred=gl,
                                source=str(self.row.get("gradient_source") or "manual")),
                GradientControl(id="L1", side="left", station=1.0, preferred=gl,
                                source=str(self.row.get("gradient_source") or "manual")),
            ],
            right_controls=[
                GradientControl(id="R0", side="right", station=0.0, preferred=gr,
                                source=str(self.row.get("gradient_source") or "manual")),
                GradientControl(id="R1", side="right", station=1.0, preferred=gr,
                                source=str(self.row.get("gradient_source") or "manual")),
            ],
        )

    def params(self):
        """The engine params dict, replicating app.py:2776-2805 exactly."""
        from hype_app import gradients as grad_mod
        from hype_app.snapshot import BC_PROFILE

        r = self.row
        origin = None
        tinfo = self.inputs / "terrain.json"
        if tinfo.exists():
            origin = jread(tinfo).get("model_origin_elev")
        cfg = self.gradient_config()
        base = dict(
            cell_size_x=float(r["gw_cell_m"]), cell_size_y=float(r["gw_cell_m"]),
            gw_mod_depth=float(r["gw_mod_depth_m"]), z=float(r["layer_thickness_m"]),
            model_origin_elev=origin,
            kh=float(r["kh_m_day"]), kv=float(r["kv_m_day"]),
            porosity=float(r["porosity"]),
            length_units="meters", time_units="days",
            nper=1, nstp=1, perlen=1.0, tsmult=1.0, sim_name="hyporheic",
            boundary_condition_mode=BC_PROFILE,
            left_boundary_gradient_profile=grad_mod.serialize_profile(cfg.left_controls),
            right_boundary_gradient_profile=grad_mod.serialize_profile(cfg.right_controls),
        )
        return base

    def snapshot(self):
        import geopandas as gpd
        from shapely.geometry import shape

        from hype_app.contracts import LatLon, SiteMetadata, TerrainSource
        from hype_app.snapshot import build_input_snapshot

        g = self.geom()
        crs, _ = self.crs()
        reach = shape(g["reach"]["geometry"])
        reach_m = gpd.GeoSeries([reach], crs=4326).to_crs(crs).iloc[0]
        c0, c1 = reach.coords[0], reach.coords[-1]
        tinfo = jread(self.inputs / "terrain.json") if (self.inputs / "terrain.json").exists() else {}
        site = SiteMetadata(
            site_id=self.site_id,
            site_name=f"{self.row.get('river') or ''} {self.site_id}".strip(),
            outlet=LatLon(lat=float(self.row["lat"]), lon=float(self.row["lon"])),
            upstream_point=LatLon(lat=c0[1], lon=c0[0]),
            downstream_point=LatLon(lat=c1[1], lon=c1[0]),
            reach_length_m=float(reach_m.length),
        )
        terrain = TerrainSource(
            dem_source=str(self.row.get("dem_source")),
            dem_resolution_m=tinfo.get("resolution_m"),
            wse_mode="model",
            crs_epsg=crs.to_epsg(),
            model_origin_elev=tinfo.get("model_origin_elev"),
        )
        return build_input_snapshot(
            assessment_id=self.site_id,
            params=self.params(),
            streamflow_cfs=float(self.row["flow_use_cfs"]),
            streamflow_source=str(self.row.get("flow_field_provenance") or "site_factory"),
            reach_geojson=g["reach"], domain_geojson=g["domain"],
            boundary_geojson={k: g[k] for k in ("left", "right", "up", "down")},
            terrain=terrain, site=site,
            anisotropy_ratio=float(self.row.get("anisotropy_ratio") or 10.0),
            gradients_config=self.gradient_config(),
            app_version="site_factory",
        )

    # ---------------------------------------------------------------- stages
    def stage_geometry(self):
        from tools.site_factory import geometry as gmod

        prj = self.row.get("ras_prj_path")
        if not prj or not Path(prj).exists():
            raise FileNotFoundError(f"ras_prj_path missing for {self.site_id}: {prj}")
        geom = gmod.stage_geometry(self.site_dir, self.work_dir, Path(prj))
        wells = next(iter(self.site_dir.rglob("Wells*.shp")), None)
        dem = self.inputs / "dem.tif"
        card = gmod.review_card(self.work_dir, self.row, geom,
                                dem_path=str(dem) if dem.exists() else None,
                                wells_shp=str(wells) if wells else None,
                                wells_lonlat=self.wells_lonlat())
        log(f"geometry: domain + sides written, review card {card}")

    def stage_terrain(self):
        from tools.site_factory import terrain as tmod

        info = tmod.stage_terrain(
            self.work_dir, self.geom(),
            str(self.row["dem_source"]), str(self.row.get("dem_vertical_units") or "m"))
        jwrite(self.inputs / "terrain.json", info)
        log(f"terrain: {info}")
        # regenerate the card with hillshade now that the DEM exists
        from tools.site_factory import geometry as gmod

        wells = next(iter(self.site_dir.rglob("Wells*.shp")), None)
        gmod.review_card(self.work_dir, self.row, self.geom(),
                         dem_path=str(self.inputs / "dem.tif"),
                         wells_shp=str(wells) if wells else None,
                         wells_lonlat=self.wells_lonlat())

    def stage_ras(self):
        from hype_app import ras

        if not ras.ras_available():
            raise RuntimeError("HEC-RAS not available (HYPE_RAS_BIN)")
        g = self.geom()
        payload = {
            "up": g["up"], "left": g["left"], "right": g["right"], "down": g["down"],
            "domain": g["domain"], "dem": str(self.inputs / "dem.tif"),
            "flow_cms": float(self.row["flow_cms"]),
            "friction_slope": float(self.row["friction_slope"]),
            "manning_n": float(self.row.get("manning_n") or 0.06),
            "cell_size_m": float(self.row["ras_cell_m"]),
            "duration_hr": 6.0, "timestep_s": 10.0, "output_interval_s": 900.0,
            "work_dir": str(self.work_dir),
        }
        t0 = time.time()
        res = ras.run_surface_model(payload, log=lambda m: log(f"  ras| {m}"),
                                    progress=make_ras_progress_logger(
                                        lambda m: log(f"  ras| {m}")))
        res = {k: (str(v) if isinstance(v, Path) else v) for k, v in (res or {}).items()}
        jwrite(self.inputs / "ras_result.json", res)
        log(f"ras: done in {time.time() - t0:.0f}s, wse_for_gw={res.get('wse_for_gw')}")

    def stage_gw(self):
        from hype_app import geometry as geo
        from hype_app import run as gw_run

        from tools.site_factory import terrain as tmod

        g = self.geom()
        _, crs_id = self.crs()
        ras_res = jread(self.inputs / "ras_result.json")
        wse = ras_res.get("wse_for_gw")
        if not wse or not Path(wse).exists():
            raise FileNotFoundError(f"wse_for_gw missing: {wse}")
        depth = ras_res.get("depth_tif")
        if depth and Path(depth).exists():
            filt = tmod.mask_shallow_wse(Path(wse), Path(depth),
                                         self.inputs / "wse_gw_filtered.tif")
            jwrite(self.inputs / "wse_filter.json", filt)
            log(f"  gw| wse depth floor {filt['min_depth_m']} m: dropped "
                f"{filt['px_dropped']} of {filt['px_before']} wetted pixels")
            wse = filt["path"]
        t0 = time.time()
        result = gw_run.execute(
            domain_gdf=geo.single_feature_gdf(g["domain"]),
            left_gdf=geo.single_feature_gdf(g["left"]),
            right_gdf=geo.single_feature_gdf(g["right"]),
            crs=crs_id,
            dem_path=str(self.inputs / "dem.tif"),
            wse_path=str(wse), wse_mode="dem", wse_relief_thresh=0.2,
            kh_polygon_gdf=None,
            params=self.params(),
            work_dir=str(self.work_dir),
            log=lambda m: log(f"  gw| {m}"),
        )
        grid = (result or {}).get("grid")
        jwrite(self.inputs / "gw_result.json", {"grid": grid})
        log(f"gw: done in {time.time() - t0:.0f}s, grid={grid}")

    def stage_hz(self):
        from hype_app import geometry as geo
        from hype_app import run as gw_run
        from hypetool.functions.hz_analysis import run_hz_analysis

        g = self.geom()
        _, crs_id = self.crs()
        # Same conversion hz_run.child_run performs: Feature -> gdf in the model CRS.
        lines = {k: geo.single_feature_gdf(g[k]).to_crs(crs_id)
                 for k in ("left", "right", "up", "down")}
        t0 = time.time()
        hz = run_hz_analysis(
            str(self.work_dir), crs=crs_id,
            left_line=lines["left"], right_line=lines["right"],
            up_line=lines["up"], down_line=lines["down"],
            particles_per_cell=int(self.row.get("hz_ppc") or 1),
            sample_per_class=int(self.row.get("sample_per_class") or 300),
            porosity=float(self.row["porosity"]),
            modflow_bin_dir=gw_run.modflow_bin_dir(),
            hard_cap_particles=10 ** 9,
            log=lambda m: log(f"  hz| {m}"),
        )
        jwrite(self.inputs / "hz_result.json", {"hz_dir": str(hz["hz_dir"]), "stats": hz["stats"]})
        cls = (hz["stats"].get("classes") or {})
        counts = {k: (v or {}).get("count") for k, v in cls.items() if isinstance(v, dict)}
        log(f"hz: done in {time.time() - t0:.0f}s, class counts={counts}")

    def stage_results(self):
        from hype_app import results as res_mod
        from hype_app import wells as wells_mod
        from hype_app.report import generate_report
        from tools.site_factory import appstate, results_capture

        hz = jread(self.inputs / "hz_result.json")
        snap = self.snapshot()
        crs, _ = self.crs()
        wells = appstate.app_well_records(self.site_id, self.wells_rows())
        cal = None
        if wells:
            try:
                srows = wells_mod.sample_wells(
                    wells, crs=crs, tifs=res_mod.head_rasters(self.work_dir),
                    grid=wells_mod.load_grid(self.work_dir / "model" / "gwf_workspace"))
                prows = wells_mod.pair_rows([], {r["id"]: r for r in srows})
                cal = results_capture.build_calibration(srows, prows)
            except Exception as e:  # noqa: BLE001 - calibration is observation data, never a blocker
                log(f"  results| calibration skipped: {e}")
        res, transit_rows = results_capture.capture(snap, hz, calibration=cal)
        jwrite(self.work_dir / "assessment_results.json", res.model_dump(mode="json"))
        spatial = results_capture.build_spatial(
            self.work_dir, hz["hz_dir"], self.geom(), str(self.inputs / "dem.tif"), crs,
            wells_lonlat=[(w["lon"], w["lat"], w["name"]) for w in wells])
        out = generate_report(res, self.work_dir / "report", transit_rows=transit_rows,
                              project_name=self.site_id, spatial=spatial,
                              app_version="site_factory")
        n_cal = len(cal.wells) if cal else 0
        log(f"results: report -> {out} (calibration wells: {n_cal})")

    def stage_aerials(self):
        """Copy the site's NAIP aerials into work_dir/aerials (copy-if-absent)."""
        import shutil

        naip = []
        for aer in sorted(self.site_dir.rglob("Aerials")):
            if not aer.is_dir():
                continue
            for sub in sorted(aer.rglob("*")):
                if sub.suffix.lower() not in (".tif", ".tiff"):
                    continue
                rel = sub.relative_to(aer)
                if any("naip" in part.lower() for part in rel.parts):
                    naip.append(sub)
        if not naip:
            log("aerials: no NAIP rasters under the site's Aerials folders")
            return
        dest = self.work_dir / "aerials"
        dest.mkdir(exist_ok=True)
        copied = skipped = 0
        for src in naip:
            tgt = dest / src.name
            if tgt.exists() and tgt.stat().st_size != src.stat().st_size:
                tgt = dest / f"{src.parent.name}_{src.name}"
            if tgt.exists() and tgt.stat().st_size == src.stat().st_size:
                skipped += 1
                continue
            shutil.copy2(src, tgt)
            copied += 1
            log(f"  aerials| {tgt.name} ({src.stat().st_size / 1e6:.1f} MB)")
        log(f"aerials: {copied} copied, {skipped} already present -> {dest}")

    def stage_bundle(self):
        from hype_app import bundle
        from tools.site_factory import appstate

        g = self.geom()
        snap = self.snapshot()
        target = self.work_dir / f"{self.site_id}.hype"
        payload = {}
        if target.exists():
            try:
                payload = bundle.restore_in_place(target)
            except Exception as e:  # noqa: BLE001 - never clobber blind, never crash the batch
                bak = target.with_name(
                    f"{target.name}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
                target.rename(bak)
                log(f"bundle: existing {target.name} unreadable ({e}), moved to {bak.name}")
                payload = {}
        # App-drawn vectors (wse_extent, k_zones) ride through; the factory six win.
        vectors = dict(payload.get("vectors") or {})
        vectors.update({"reach": g["reach"], "upstream": g["up"], "left": g["left"],
                        "right": g["right"], "downstream": g["down"], "domain": g["domain"]})
        aer_dir = self.work_dir / "aerials"
        aer_files = (sorted(p.name for p in aer_dir.iterdir()
                            if p.suffix.lower() in (".tif", ".tiff"))
                     if aer_dir.is_dir() else [])
        factory_wells = appstate.app_well_records(self.site_id, self.wells_rows())
        res_state = bundle.tokenize_paths(
            results_state(self.work_dir, self.inputs, self.site_id), self.work_dir)
        state = appstate.merge_state(
            payload.get("state"), site_id=self.site_id, factory_wells=factory_wells,
            aerial_layers=appstate.aerial_layer_records(self.site_id, aer_files),
            format_version=bundle.FORMAT_VERSION, results_state=res_state)
        bundle.save_bundle_to(self.work_dir, target, vectors=vectors,
                              params=self.params(), run_config=payload.get("run_config"),
                              state=state, assessment_input=snap.model_dump(mode="json"),
                              scoring_profile=payload.get("scoring_profile"))
        log(f"bundle: {target} ({len(factory_wells)} wells, {len(aer_files)} aerials, "
            f"{'merged' if payload.get('state') else 'fresh'} state)")

    # ---------------------------------------------------------------- control
    def provenance(self):
        p = self.work_dir / "_provenance.json"
        return jread(p) if p.exists() else {}

    def save_provenance(self, stages_run):
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                                 capture_output=True, text=True).stdout.strip()
        except Exception:  # noqa: BLE001
            sha = None
        snap = self.snapshot()
        prov = self.provenance()
        prov.update({
            "site_id": self.site_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_sha": sha,
            "input_hash": snap.input_hash,
            "group_hashes": snap.group_hashes(),
            "stages_last_run": stages_run,
            "row": {k: v for k, v in self.row.items() if not k.startswith("hash_")},
        })
        jwrite(self.work_dir / "_provenance.json", prov)

    def auto_stages(self):
        """Diff stored group hashes vs the current sheet -> minimal stage list."""
        prov = self.provenance()
        old = prov.get("group_hashes") or {}
        if not old or not (self.inputs / "geometry.json").exists():
            return STAGE_ORDER
        new = self.snapshot().group_hashes()
        need = set()
        for grp, h in new.items():
            if old.get(grp) != h:
                need.update(GROUP_STAGES.get(grp, STAGE_ORDER))
        return [s for s in STAGE_ORDER if s in need]

    def run(self, stages):
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        done, err = [], None
        for st in stages:
            log(f"=== {self.site_id} :: {st} ===")
            try:
                getattr(self, f"stage_{st}")()
                done.append(st)
                # a stage that now succeeds retires its old failure marker, so the
                # site dir always reflects CURRENT state (history stays in _runs)
                (self.work_dir / f"_error_{st}.txt").unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                err = f"{st}: {e}"
                tb = traceback.format_exc()
                fail = HYPE_MODELS / "_runs" / "failures.csv"
                fail.parent.mkdir(exist_ok=True)
                with open(fail, "a", encoding="utf-8") as f:
                    f.write(f'"{self.site_id}","{st}","{started}","{e}"\n')
                (self.work_dir / f"_error_{st}.txt").write_text(tb, encoding="utf-8")
                log(f"FAILED at {st}: {e}")
                break
        try:
            self.save_provenance(done)
        except Exception as e:  # noqa: BLE001
            log(f"(provenance save failed: {e})")
        return done, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--stages", help="comma list from: " + ",".join(STAGE_ORDER))
    ap.add_argument("--auto", action="store_true", help="hash-diff selective rerun")
    args = ap.parse_args()

    d = Driver(args.site)
    if args.auto:
        stages = d.auto_stages()
        log(f"auto: stages to run = {stages or 'NONE (all hashes match)'}")
    elif args.stages:
        stages = [s.strip() for s in args.stages.split(",") if s.strip()]
        bad = [s for s in stages if s not in STAGE_ORDER]
        if bad:
            raise SystemExit(f"unknown stages: {bad}")
    else:
        stages = STAGE_ORDER
    done, err = d.run(stages)
    log(f"run complete: done={done} error={err}")
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
