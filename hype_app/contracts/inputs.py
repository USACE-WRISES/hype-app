"""The frozen run-input snapshot (revision spec §4.2 AssessmentInputSnapshot).

Frozen when a run BEGINS. Once this exists, no report or project download may reconstruct run
inputs from live UI values (§4.2). It also produces the dependency-group hashes (§4.3) that drive
result staleness: each group maps to a subset of this snapshot, and any change invalidates every
dependent result.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, computed_field

from ..hashing import group_hashes as _group_hashes
from ..hashing import stable_hash
from ..provenance import Citation, HypeModel, Provenance
from .flow import LatLon
from .gradients import GradientBoundaryConfigV2
from .soils import AggregationPolicy

INPUT_SNAPSHOT_SCHEMA_VERSION = "assessment-input-snapshot/2.0"


class SiteMetadata(HypeModel):
    site_name: str | None = None
    analyst: str | None = None
    organization: str | None = None
    notes: str | None = None
    assessment_date: date | None = None
    outlet: LatLon | None = None
    upstream_point: LatLon | None = None
    downstream_point: LatLon | None = None
    reach_length_m: float | None = None


class TerrainSource(HypeModel):
    dem_source: str | None = None                # "3DEP 1m", "upload", ...
    dem_resolution_m: float | None = None
    wse_mode: str | None = None                  # "model" | "upload" | "draw"
    wse_source: str | None = None
    crs_epsg: int | None = None
    model_origin_elev: float | None = None       # upstream streambed elevation


class StreamflowInput(HypeModel):
    value_cfs: float | None = None
    value_cms: float | None = None
    provenance: Provenance
    flow_lookup_id: str | None = None            # FlowLookupSnapshot id when from USGS


class KSettings(HypeModel):
    kh_m_day: float
    kv_m_day: float
    porosity: float
    use_kzones: bool = False
    kzone_kh: float | None = None
    kzone_kv: float | None = None
    kzone_count: int = 0
    soil_snapshot_id: str | None = None
    aggregation_policy: AggregationPolicy | None = None
    anisotropy_ratio: float | None = None
    # Precedence order actually applied (§6.8), most specific first.
    precedence: list[str] = Field(
        default_factory=lambda: ["manual_kzone", "nrcs_override", "nrcs_derived", "global_fallback"])


class GridSettings(HypeModel):
    cell_size_x: float
    cell_size_y: float
    gw_mod_depth: float
    layer_thickness: float                        # `z`
    nlay: int | None = None
    particles_per_cell: int = 1
    min_path_mult: float = 3.0


class AssessmentInputSnapshot(HypeModel):
    """Immutable snapshot of everything a run consumes."""

    schema_version: str = INPUT_SNAPSHOT_SCHEMA_VERSION
    assessment_id: str

    site: SiteMetadata = Field(default_factory=SiteMetadata)

    reach_geojson: dict | None = None
    domain_geojson: dict | None = None
    boundary_geojson: dict = Field(default_factory=dict)   # left/right/up/down lines

    terrain: TerrainSource = Field(default_factory=TerrainSource)
    streamflow: StreamflowInput
    k: KSettings
    gradients: GradientBoundaryConfigV2
    grid: GridSettings

    model_version: str | None = None
    app_version: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime | None = None

    # ---- staleness support (§4.3) --------------------------------------------

    def dependency_groups(self) -> dict:
        """Map snapshot fields onto the §4.3 dependency groups (raw values, pre-hash)."""
        return {
            "geometry": {"reach": self.reach_geojson, "domain": self.domain_geojson,
                         "boundary": self.boundary_geojson},
            "terrain": self.terrain.model_dump(mode="json"),
            "streamflow": {"cms": self.streamflow.value_cms,
                           "source": self.streamflow.provenance.source},
            "soil_k": self.k.model_dump(mode="json"),
            "gradients": self.gradients.model_dump(mode="json"),
            "grid": {k: v for k, v in self.grid.model_dump(mode="json").items()
                     if k not in ("particles_per_cell", "min_path_mult")},
            "particles": {"porosity": self.k.porosity,
                          "particles_per_cell": self.grid.particles_per_cell,
                          "min_path_mult": self.grid.min_path_mult},
        }

    def group_hashes(self) -> dict[str, str]:
        return _group_hashes(self.dependency_groups())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def input_hash(self) -> str:
        """Canonical hash of the whole snapshot (excludes this computed field itself)."""
        return stable_hash(self.model_dump(mode="json", exclude={"input_hash"}))


__all__ = [
    "SiteMetadata", "TerrainSource", "StreamflowInput", "KSettings", "GridSettings",
    "AssessmentInputSnapshot", "INPUT_SNAPSHOT_SCHEMA_VERSION",
]
