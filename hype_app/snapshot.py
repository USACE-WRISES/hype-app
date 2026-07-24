"""Build the frozen AssessmentInputSnapshot from the app's live run inputs (spec §4.2).

Pure and Shiny-independent: `app.py` calls `build_input_snapshot(...)` at run start with the
`params()` dict + geometry + metadata, and the returned snapshot becomes the single source of truth
for the run, the project archive, and the report. Structured gradients arrive in Phase 4; until then
the legacy corner/profile configuration is preserved verbatim in `GradientBoundaryConfigV2.legacy`
so its values still hash into the gradients dependency group (staleness stays correct).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .contracts import (
    AssessmentInputSnapshot,
    GradientBoundaryConfigV2,
    GridSettings,
    KSettings,
    LegacyGradientMeta,
    SiteMetadata,
    StreamflowInput,
    TerrainSource,
)
from .provenance import Provenance
from .units import cfs_to_cms

# Mirrors app.py's BC_CORNER / BC_PROFILE (hypetool Settings.boundary_condition_mode values).
BC_CORNER = "4 Corner Gradients"
BC_PROFILE = "Spatially Varying Gradient"


def gradients_from_params(params: dict) -> GradientBoundaryConfigV2:
    """Wrap the legacy corner/profile gradient config as a v2 config carrying legacy metadata."""
    mode = params.get("boundary_condition_mode", BC_CORNER)
    if mode == BC_PROFILE:
        legacy = LegacyGradientMeta(
            boundary_condition_mode=mode,
            left_profile=params.get("left_boundary_gradient_profile"),
            right_profile=params.get("right_boundary_gradient_profile"),
        )
    else:
        corners = {
            "g_ul": params.get("upstream_left_fpl_gw_gradient"),
            "g_ur": params.get("upstream_right_fpl_gw_gradient"),
            "g_dl": params.get("downstream_left_fpl_gw_gradient"),
            "g_dr": params.get("downstream_right_fpl_gw_gradient"),
        }
        legacy = LegacyGradientMeta(boundary_condition_mode=mode,
                                    corner_gradients={k: float(v) for k, v in corners.items()
                                                      if v is not None})
    return GradientBoundaryConfigV2(mode="quantitative", legacy=legacy)


def build_input_snapshot(
    *,
    assessment_id: str,
    params: dict,
    streamflow_cfs: float | None,
    streamflow_source: str = "manual",
    streamflow_user_modified: bool = False,
    flow_lookup_id: str | None = None,
    reach_geojson: dict | None = None,
    domain_geojson: dict | None = None,
    boundary_geojson: dict | None = None,
    terrain: TerrainSource | None = None,
    site: SiteMetadata | None = None,
    kzone_count: int = 0,
    kzone_kh: float | None = None,
    kzone_kv: float | None = None,
    use_kzones: bool = False,
    soil_snapshot_id: str | None = None,
    soil_aggregation_policy: str | None = None,
    anisotropy_ratio: float | None = None,
    gradients_config: GradientBoundaryConfigV2 | None = None,
    app_version: str | None = None,
    model_version: str | None = None,
    created_at: datetime | None = None,
) -> AssessmentInputSnapshot:
    """Freeze a run's inputs. Numeric fields come straight from the engine `params()` dict."""
    cfs = None if streamflow_cfs is None else float(streamflow_cfs)
    streamflow = StreamflowInput(
        value_cfs=cfs,
        value_cms=None if cfs is None else cfs_to_cms(cfs),
        flow_lookup_id=flow_lookup_id,
        provenance=Provenance(source=streamflow_source, user_modified=streamflow_user_modified),
    )
    from .contracts import AggregationPolicy
    k = KSettings(
        kh_m_day=float(params["kh"]), kv_m_day=float(params["kv"]),
        porosity=float(params["porosity"]),
        use_kzones=bool(use_kzones), kzone_kh=kzone_kh, kzone_kv=kzone_kv,
        kzone_count=int(kzone_count), soil_snapshot_id=soil_snapshot_id,
        aggregation_policy=(AggregationPolicy(soil_aggregation_policy)
                            if soil_aggregation_policy else None),
        anisotropy_ratio=anisotropy_ratio,
    )
    grid = GridSettings(
        cell_size_x=float(params["cell_size_x"]), cell_size_y=float(params["cell_size_y"]),
        gw_mod_depth=float(params["gw_mod_depth"]), layer_thickness=float(params["z"]),
    )
    if terrain is None:
        terrain = TerrainSource(model_origin_elev=params.get("model_origin_elev"))
    elif terrain.model_origin_elev is None:
        terrain = terrain.model_copy(update={"model_origin_elev": params.get("model_origin_elev")})

    return AssessmentInputSnapshot(
        assessment_id=assessment_id,
        site=site or SiteMetadata(),
        reach_geojson=reach_geojson,
        domain_geojson=domain_geojson,
        boundary_geojson=boundary_geojson or {},
        terrain=terrain,
        streamflow=streamflow,
        k=k,
        gradients=(gradients_config if gradients_config is not None
                   else gradients_from_params(params)),
        grid=grid,
        model_version=model_version,
        app_version=app_version,
        created_at=created_at or datetime.now(timezone.utc),
    )


__all__ = ["build_input_snapshot", "gradients_from_params", "BC_CORNER", "BC_PROFILE"]
