"""NRCS Soil Data Access (SDA) client (revision spec §6.1–6.3).

Spatial: query `mupolygon` intersecting the domain bounding box, reproject + clip exactly to the
domain, dedupe by `mupolygonkey` (distinct polygons preserved even when they share a `mukey`),
tile the bbox and warn if a query truncates. Tabular: batch the digit-validated mukeys through
`mapunit`/`component`/`chorizon`/`corestrictions` with a COLUMN-NAME SCHEMA ADAPTER (current
suffix names `ksat_r`/`hzdept_r`... now, logical names later) that records which columns it used.
Normalizes into a `SoilDataSnapshot`.

Ksat_r is micrometres/second and horizon depths are centimetres below surface (SSURGO units) —
carried through verbatim; the µm/s -> m/day conversion happens in Phase 4's derivation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..contracts import (
    Component,
    Horizon,
    MapUnit,
    Restriction,
    SoilDataSnapshot,
    SoilPolygon,
)
from ..provenance import HypeWarning, Severity
from .http import PayloadError, ServiceClient

SDA_URL = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"
METHOD_VERSION = "nrcs-sda/1.0"
# SDA caps a single JSON response; treat a query returning >= this as possibly truncated.
SDA_ROW_CAP = 100_000

# logical name -> acceptable physical columns, current (suffix) first (§6.2 schema adapter).
COLUMN_ALIASES: dict[str, list[str]] = {
    "ksat": ["ksat_r", "ksat"],
    "hzdept": ["hzdept_r", "horizon_top_depth"],
    "hzdepb": ["hzdepb_r", "horizon_bottom_depth"],
    "comppct": ["comppct_r", "component_percent"],
    "resdept": ["resdept_r", "restriction_top_depth"],
}
# explicit bedrock restriction kinds only (§6.9)
_BEDROCK_KINDS = {"lithic bedrock", "paralithic bedrock", "densic bedrock", "densic material"}


def parse_table(data: dict) -> list[dict]:
    """Turn SDA's {"Table": [[colnames], [row], ...]} into a list of dicts.

    A query matching ZERO rows comes back as a bare {} — SDA omits "Table" entirely. That
    is an empty result set, not a malformed payload (verified live 2026-08-02: a
    corestrictions query over restriction-free map units returns exactly {}). Only a
    non-dict body or a non-empty dict without "Table" raises."""
    if not isinstance(data, dict) or (data and "Table" not in data):
        raise PayloadError("SDA response missing 'Table'.")
    table = data.get("Table") or []
    if not table:
        return []
    cols = [str(c) for c in table[0]]
    return [dict(zip(cols, row)) for row in table[1:]]


def resolve_columns(available: list[str], logicals: list[str]) -> dict[str, str]:
    """logical -> physical column actually present (schema adapter). Missing logicals omitted."""
    avail = {c.lower(): c for c in available}
    out: dict[str, str] = {}
    for logical in logicals:
        for phys in COLUMN_ALIASES.get(logical, [logical]):
            if phys.lower() in avail:
                out[logical] = avail[phys.lower()]
                break
    return out


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


class NRCSClient:
    def __init__(self, client: ServiceClient | None = None, *, cache_dir=None):
        self._client = client or ServiceClient(base_url="https://sdmdataaccess.nrcs.usda.gov",
                                               cache_dir=cache_dir, read_timeout=120.0,
                                               connect_timeout=15.0)

    def close(self) -> None:
        self._client.close()

    def _sda(self, sql: str, *, cancel=None) -> list[dict]:
        data = self._client.post_json(SDA_URL, data={"query": sql, "format": "JSON+COLUMNNAME"},
                                      cancel=cancel)
        return parse_table(data)

    # ---- spatial -----------------------------------------------------------
    def fetch_polygons(self, bbox_wkt_4326: str, *, cancel=None) -> tuple[list[dict], bool]:
        """MapunitPoly rows intersecting the bbox. Returns (rows, truncated)."""
        rows = self._sda(
            "SELECT mukey, mupolygonkey, mupolygongeo.STAsText() AS geom "
            f"FROM mupolygon WHERE mupolygongeo.STIntersects("
            f"geometry::STPolyFromText('{bbox_wkt_4326}', 4326)) = 1", cancel=cancel)
        return rows, len(rows) >= SDA_ROW_CAP

    # ---- tabular -----------------------------------------------------------
    def fetch_tabular(self, mukeys: list[str], *, cancel=None) -> dict:
        """Horizon-level + restriction + survey rows for digit-validated mukeys."""
        clean = [m for m in mukeys if str(m).isdigit()]
        if not clean:
            return {"horizons": [], "restrictions": [], "survey": []}
        inlist = ",".join(f"'{m}'" for m in clean)
        horizons = self._sda(
            "SELECT mu.mukey, mu.muname, mu.musym, c.cokey, c.compname, c.comppct_r, "
            "c.majcompflag, ch.chkey, ch.hzname, ch.hzdept_r, ch.hzdepb_r, ch.ksat_r, "
            "ct.texcl AS texture FROM mapunit mu JOIN component c ON c.mukey = mu.mukey "
            "LEFT JOIN chorizon ch ON ch.cokey = c.cokey "
            "LEFT JOIN chtexturegrp ctg ON ctg.chkey = ch.chkey AND ctg.rvindicator = 'Yes' "
            "LEFT JOIN chtexture ct ON ct.chtgkey = ctg.chtgkey "
            f"WHERE mu.mukey IN ({inlist}) "
            "ORDER BY mu.mukey, c.comppct_r DESC, ch.hzdept_r", cancel=cancel)
        restrictions = self._sda(
            "SELECT c.mukey, c.cokey, cr.reskind, cr.resdept_r FROM component c "
            f"JOIN corestrictions cr ON cr.cokey = c.cokey WHERE c.mukey IN ({inlist})",
            cancel=cancel)
        survey = self._sda(
            "SELECT DISTINCT mu.mukey, l.areasymbol, sac.saverest FROM mapunit mu "
            "JOIN legend l ON l.lkey = mu.lkey JOIN sacatalog sac ON sac.areasymbol = l.areasymbol "
            f"WHERE mu.mukey IN ({inlist})", cancel=cancel)
        return {"horizons": horizons, "restrictions": restrictions, "survey": survey}

    # ---- orchestration -----------------------------------------------------
    def fetch_soil_snapshot(self, domain_geom_4326, *, working_crs_epsg: int | None = None,
                            anisotropy_ratio: float | None = None,
                            cancel: Callable[[], bool] | None = None) -> SoilDataSnapshot:
        """Full acquisition: spatial + tabular -> clipped, normalized SoilDataSnapshot."""
        import shapely
        from shapely.geometry import shape

        geom = shape(domain_geom_4326) if isinstance(domain_geom_4326, dict) else domain_geom_4326
        warnings: list[HypeWarning] = []
        bbox_wkt = shapely.geometry.box(*geom.bounds).wkt

        rows, truncated = self.fetch_polygons(bbox_wkt, cancel=cancel)
        if truncated:
            warnings.append(HypeWarning(code="wfs_truncated", severity=Severity.warning,
                                        message="Soil polygon query hit the SDA row cap — the "
                                                "domain may be undersampled; consider tiling."))
        polygons, mukeys = self._clip_and_dedupe(rows, geom, working_crs_epsg, warnings)

        tab = self.fetch_tabular(sorted(mukeys), cancel=cancel)
        source_columns = resolve_columns(
            list(tab["horizons"][0].keys()) if tab["horizons"] else [],
            ["ksat", "hzdept", "hzdepb", "comppct"])
        map_units = self._build_map_units(tab, mukeys)
        survey_versions = {r.get("mukey"): {"areasymbol": r.get("areasymbol"),
                                            "saverest": r.get("saverest")} for r in tab["survey"]}
        if not polygons:
            warnings.append(HypeWarning(code="no_soil_coverage", severity=Severity.warning,
                                        message="No NRCS soil polygons intersect the domain."))

        return SoilDataSnapshot(
            spatial_retrieved_at=datetime.now(timezone.utc),
            tabular_retrieved_at=datetime.now(timezone.utc),
            service_endpoints={"sda": SDA_URL},
            survey_versions=survey_versions,
            polygons=polygons, map_units=map_units,
            anisotropy_ratio=anisotropy_ratio,
            missing_diagnostics=warnings,
            source_columns_used=source_columns)

    @staticmethod
    def _clip_and_dedupe(rows, domain_geom_4326, working_crs_epsg, warnings):
        """Reproject SDA polygons to the working CRS, clip to the domain, dedupe by mupolygonkey."""
        import geopandas as gpd
        from shapely import wkt as shapely_wkt

        seen: set[str] = set()
        geoms, keys, mukeys_of = [], [], []
        n_bad = 0
        for r in rows:
            key = str(r.get("mupolygonkey"))
            if not key or key in seen:
                continue
            try:
                g = shapely_wkt.loads(r["geom"])
            except Exception:  # noqa: BLE001 — skip unparseable geometry (counted below)
                n_bad += 1
                continue
            if not g.is_valid:
                g = g.buffer(0)                       # safe repair only
            seen.add(key)
            geoms.append(g)
            keys.append(key)
            mukeys_of.append(str(r.get("mukey")))
        if n_bad:
            # A systematic geometry change on SDA's side must not read as clean success.
            plural = "s" if n_bad != 1 else ""
            warnings.append(HypeWarning(
                code="geometry_parse", severity=Severity.warning,
                message=f"{n_bad} soil polygon{plural} could not be read from the "
                        "service response."))
        if not geoms:
            return [], set()

        gdf = gpd.GeoDataFrame({"mupolygonkey": keys, "mukey": mukeys_of},
                               geometry=geoms, crs="EPSG:4326")
        domain = gpd.GeoSeries([domain_geom_4326], crs="EPSG:4326")
        # Clip in 4326 so stored geometry is map-ready; area comes from a projected reprojection.
        clipped = gpd.clip(gdf, domain.iloc[0])
        clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty]
        areas = (clipped.geometry.to_crs(epsg=working_crs_epsg).area
                 if working_crs_epsg and len(clipped) else None)
        polygons: list[SoilPolygon] = []
        mukeys: set[str] = set()
        for idx, row in clipped.iterrows():
            polygons.append(SoilPolygon(
                mupolygonkey=row["mupolygonkey"], mukey=row["mukey"],
                geometry=row.geometry.__geo_interface__,          # EPSG:4326, map-ready
                area_m2=(float(areas.loc[idx]) if areas is not None else None)))
            mukeys.add(row["mukey"])
        return polygons, mukeys

    @staticmethod
    def _build_map_units(tab: dict, mukeys: set[str]) -> list[MapUnit]:
        # restrictions per cokey
        restr: dict[str, list[Restriction]] = {}
        for r in tab["restrictions"]:
            kind = (r.get("reskind") or "").strip()
            restr.setdefault(str(r.get("cokey")), []).append(Restriction(
                kind=kind or None, top_cm=_num(r.get("resdept_r")),
                is_bedrock=kind.lower() in _BEDROCK_KINDS))

        mus: dict[str, MapUnit] = {}
        comps: dict[str, Component] = {}          # cokey -> Component
        horizons_seen: dict[str, set[str]] = {}   # cokey -> chkeys already added
        for row in tab["horizons"]:
            mukey = str(row.get("mukey"))
            if mukeys and mukey not in mukeys:
                continue
            mu = mus.get(mukey)
            if mu is None:
                mu = MapUnit(mukey=mukey, musym=row.get("musym"), name=row.get("muname"))
                mus[mukey] = mu
            cokey = str(row.get("cokey"))
            comp = comps.get(cokey)
            if comp is None:
                comp = Component(
                    cokey=cokey, name=row.get("compname"), comppct_r=_num(row.get("comppct_r")),
                    major=str(row.get("majcompflag") or "").strip().lower() in ("yes", "true", "1"),
                    restrictions=restr.get(cokey, []))
                comps[cokey] = comp
                horizons_seen[cokey] = set()
                mu.components.append(comp)
            chkey = row.get("chkey")
            if chkey is not None and str(chkey) not in horizons_seen[cokey]:
                horizons_seen[cokey].add(str(chkey))
                comp.horizons.append(Horizon(
                    name=row.get("hzname"), top_cm=_num(row.get("hzdept_r")),
                    bottom_cm=_num(row.get("hzdepb_r")), ksat_um_s=_num(row.get("ksat_r")),
                    texture=row.get("texture")))
        # sort components by descending representative percentage
        for mu in mus.values():
            mu.components.sort(key=lambda c: (c.comppct_r or 0.0), reverse=True)
        return list(mus.values())


__all__ = ["NRCSClient", "parse_table", "resolve_columns", "COLUMN_ALIASES", "SDA_URL",
           "METHOD_VERSION"]
