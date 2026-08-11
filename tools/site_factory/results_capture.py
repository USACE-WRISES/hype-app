"""Results capture: the headless equivalent of app.py's `_capture_canonical_results`.

A faithful transcription of app.py:4645-4741 minus the Hydraulic Alternatives
attach (alternatives_state=None, Shiny-only by design). Observation-well
calibration is supported since the WELLS-sheet harvest exists: the driver
samples heads via the same hype_app.wells functions the app uses and passes
the built GroundwaterCalibration in. Everything else is the same call into the
same importable builders, so a factory-built project carries the identical
canonical contract the app would produce.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# The same screening knobs the app's panes default to, in the exact shape
# assess.build_results expects (precedent: tests/test_alt_screening.py KNOBS).
DEFAULT_FN_KNOBS = {
    "nitrate_mg_l": 1.0,
    "denit_rate_per_day": 1.22,
    "thermal_response_hours": 8.0,
    "pollutant_endpoints": ["zinc", "acesulfame"],
    "oxygen_gate": True,
    "contaminant_conc_by_key": {},
}


def build_calibration(srows: list[dict], prows: list[dict]):
    """wells.sample_wells rows (+ pair_rows) -> GroundwaterCalibration or None.

    Verbatim transcription of the app's calibration block (app.py:4813-4835):
    every sampled field is optional, so screen-elevation-less wells are legal
    rows whose note says why they could not be sampled.
    """
    if not srows:
        return None
    from hype_app import wells as wells_mod
    from hype_app.contracts import (CalibrationPair, CalibrationStats,
                                    CalibrationWell, GroundwaterCalibration)

    stats_d = wells_mod.residual_stats(srows)
    return GroundwaterCalibration(
        wells=[CalibrationWell(
            well_id=r["id"], name=r["name"], lat=r["lat"], lon=r["lon"],
            screen_elevation_m=r["screen_elev"], observed_head_m=r["obs_head"],
            model_layer=r["layer"], computed_head_m=r["computed"],
            residual_m=r["residual"], note=r["reason"]) for r in srows],
        pairs=[CalibrationPair(
            pair_id=r["id"], well_a=r["name_a"], well_b=r["name_b"],
            distance_m=r["distance"], computed_gradient=r["computed_gradient"],
            observed_gradient=r["observed_gradient"], note=r["reason"])
            for r in prows or []],
        stats=(CalibrationStats(
            n_observed=stats_d["n"], mean_error_m=stats_d["mean_error"],
            mean_absolute_error_m=stats_d["mean_abs_error"],
            rmse_m=stats_d["rmse"]) if stats_d else None))


def capture(snap, hz: dict, *, fn_knobs: dict | None = None, calibration=None):
    """(snapshot, {"hz_dir","stats"}) -> (AssessmentResultsV2, transit_rows)."""
    from hype_app import hz_results, results_lifecycle, signature

    fn_knobs = dict(DEFAULT_FN_KNOBS if fn_knobs is None else fn_knobs)

    full_stats = hz.get("stats") or {}
    stats = full_stats.get("classes") or full_stats or {}
    hyp = stats.get("hyporheic") or {}
    acct = (full_stats.get("flux") or {}).get("accounting") or {}
    net_exch = acct.get("net_stream_exchange")
    domain_vol = (full_stats.get("domain") or {}).get("active_saturated_volume_m3")
    fm = hz_results.flux_metrics(full_stats, hz.get("hz_dir"), transit_rows=True)
    transit_rows = fm["transit_rows"]
    # POROSITY: the hyporheic run's, not the snapshot's (app.py:4673 comment).
    porosity = signature.as_float((full_stats.get("knobs") or {}).get("porosity"))
    if porosity is None:
        porosity = snap.k.porosity

    res = results_lifecycle.build_canonical_results(
        snap, alternatives_state=None,
        hz_stats=stats, streamflow_cms=snap.streamflow.value_cms,
        reach_length_m=snap.site.reach_length_m, exchange=fm["exchange"],
        transit_times_days=fm["transit_times"], transit_weights=fm["transit_weights"],
        path_depths=fm["path_depths"], path_lengths=fm["path_lengths"],
        footprint_weighted_m2=hyp.get("footprint_m2"), porosity=porosity,
        snapshot_porosity=snap.k.porosity,
        censored_fraction=fm["censored"],
        streambed_area_m2=acct.get("streambed_area_m2"),
        active_streambed_area_m2=acct.get("active_streambed_area_m2"),
        return_streambed_area_m2=acct.get("return_streambed_area_m2"),
        connected_streambed_area_m2=acct.get("connected_streambed_area_m2"),
        net_stream_exchange_cms=(net_exch / 86400.0 if net_exch is not None else None),
        domain_volume_m3=domain_vol, hz_accounting=acct,
        function_inputs=fn_knobs, calibration=calibration,
        app_version="site_factory")
    return res, transit_rows


def build_spatial(work_dir: Path, hz_dir, geom: dict, dem_path: str, crs,
                  wells_lonlat: list | None = None) -> dict | None:
    """Transcription of app.py `_report_spatial` (4466-4524): figure inputs for the report."""
    from hype_app import hz_results, results

    if not hz_dir:
        return None
    work_dir = Path(work_dir)
    reach_lonlat = geom["reach"]["geometry"]["coordinates"]
    domain_lonlat = geom["domain"]["geometry"]["coordinates"][0]
    planview = {
        "down_fc": hz_results.flow_exchange_geojson(hz_dir, "down"),
        "up_fc": hz_results.flow_exchange_geojson(hz_dir, "up"),
        "footprint_fc": hz_results.footprint_geojson(hz_dir, "hyporheic"),
        "reach_lonlat": reach_lonlat, "domain_lonlat": domain_lonlat}
    paths_gdf = reach_line = None
    try:
        gdf = hz_results.class_paths_gdf(hz_dir)
        if gdf is not None and len(gdf):
            if "hz_class" in gdf.columns:
                gdf = gdf[gdf["hz_class"] == "hyporheic"]
            if len(gdf) and reach_lonlat:
                import geopandas as gpd
                from shapely.geometry import LineString

                rl = gpd.GeoSeries([LineString(reach_lonlat)], crs="EPSG:4326").to_crs(gdf.crs)
                paths_gdf, reach_line = gdf, rl.iloc[0]
    except Exception:  # noqa: BLE001 — the section figure is optional
        paths_gdf = reach_line = None
    spatial = {"planview": planview, "paths_gdf": paths_gdf, "reach_line": reach_line}
    try:
        spatial["crs_wkt"] = crs.to_wkt() if crs is not None else None
        wse_tif = work_dir / "model" / "cropped_water_surface_raster.tif"
        spatial["wse_tif"] = str(wse_tif) if wse_tif.exists() else None
        head_tifs_l = results.head_rasters(work_dir)
        if head_tifs_l:
            _hl = results.full_coverage_layer(head_tifs_l)
            spatial["head_tif"] = str(head_tifs_l[_hl - 1])
            spatial["head_layer"] = _hl
        else:
            spatial["head_tif"] = None
        gwf = work_dir / "model" / "gwf_workspace"
        spatial["gwf_ws"] = str(gwf) if next(gwf.glob("*.dis.grb"), None) else None
        spatial["dem_path"] = dem_path
        spatial["sides_lonlat"] = {
            k: geom[k]["geometry"]["coordinates"]
            for k in ("up", "left", "right", "down")
            if geom.get(k) and geom[k]["geometry"]["type"] == "LineString"}
        spatial["wells_lonlat"] = [(float(w[0]), float(w[1]), str(w[2] or ""))
                                   for w in (wells_lonlat or [])]
    except Exception:  # noqa: BLE001 — the site maps are optional
        pass
    return spatial
