"""USGS StreamStats / NSS flow-lookup client (revision spec §5).

Implements the documented sequence against the current services (all under streamstats.usgs.gov):

    ss-delineate  GET  /ss-delineate/v1/delineate/sshydro/{region}?lat&lon
    nss           GET  /nssservices/scenarios?regions={region}&statisticgroups=2
    ss-hydro      POST /ss-hydro/v1/basin-characteristics/calculate-using-ssdelineate/?region&lat&lon&BCs=
    nss           POST /nssservices/scenarios/estimate?regions={region}   (scenarios w/ values filled)

Estimate `results` are normalized into `FlowCandidate`s (cfs + m3/s, AEP/recurrence parsed only
when the result NAME states it unambiguously, in-range/extrapolation from the regression limits,
equation + parameters, warnings). A result is insertable only if a finite positive discharge (§5.4).
National fallback runs only when no regional discharge is usable, or on explicit request (§5.2/§5.5).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from ..contracts import FlowCandidate, FlowLookupSnapshot, LatLon
from ..provenance import Citation, HypeWarning, Severity
from ..units import cfs_to_cms, cms_to_cfs
from .http import PayloadError, ServiceClient

BASE_URL = "https://streamstats.usgs.gov"
PEAK_FLOW_STATISTIC_GROUP = "2"
METHOD_VERSION = "streamstats-nss/1.0"

# name pattern that unambiguously states annual-exceedance probability, e.g. "50-percent AEP flood"
_AEP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*percent\s+AEP", re.IGNORECASE)


def suggest_region(lat: float, lon: float, *, timeout: float = 8.0) -> str | None:
    """Best-effort 2-letter StreamStats region (US state) for a point via the FCC area API.

    Used only to PREFILL the modal's region field; the user can override (§5.1). Returns None on
    any failure — never raises, never blocks the workflow.
    """
    import httpx
    try:
        r = httpx.get("https://geo.fcc.gov/api/census/area",
                      params={"lat": lat, "lon": lon, "format": "json"},
                      timeout=timeout, follow_redirects=True)
        res = (r.json().get("results") or [{}])[0]
        code = res.get("state_code")
        return code.upper() if code else None
    except Exception:  # noqa: BLE001
        return None


def _validate_list(data) -> None:
    if not isinstance(data, list):
        raise PayloadError("Expected a JSON array from NSS.")


def _aep_and_recurrence(name: str) -> tuple[float | None, float | None]:
    """(annual_exceedance_prob, recurrence_years) parsed ONLY when the name states AEP (§5.3)."""
    m = _AEP_RE.search(name or "")
    if not m:
        return None, None
    pct = float(m.group(1))
    if pct <= 0:
        return None, None
    aep = pct / 100.0
    return aep, (1.0 / aep if aep > 0 else None)


def _normalize_discharge(value: float, unit_abbr: str) -> tuple[float | None, float | None]:
    """(cfs, cms) if the unit is a recognized discharge, else (None, None) -> not insertable."""
    u = (unit_abbr or "").replace(" ", "").lower()
    if u in ("ft^3/s", "ft3/s", "cfs", "cubicfeetpersecond"):
        return float(value), cfs_to_cms(float(value))
    if u in ("m^3/s", "m3/s", "cms", "cubicmeterspersecond"):
        return cms_to_cfs(float(value)), float(value)
    return None, None


def _param_range_check(parameters: list[dict]) -> tuple[bool, dict, dict]:
    """(all_in_range, {code: value}, {code: [min, max]}) for one regression region."""
    in_range = True
    values, ranges = {}, {}
    for p in parameters or []:
        code = str(p.get("code", ""))
        val = p.get("value")
        lim = p.get("limits") or {}
        lo, hi = lim.get("min"), lim.get("max")
        values[code] = val
        if lo is not None and hi is not None:
            ranges[code] = [lo, hi]
            if isinstance(val, (int, float)) and (val < lo or val > hi):
                in_range = False
    return in_range, values, ranges


class StreamStatsClient:
    def __init__(self, client: ServiceClient | None = None, *, cache_dir=None):
        self._client = client or ServiceClient(base_url=BASE_URL, cache_dir=cache_dir,
                                               read_timeout=120.0, connect_timeout=15.0)

    def close(self) -> None:
        self._client.close()

    # ---- individual service calls (each testable in isolation) --------------
    def delineate(self, region: str, lat: float, lon: float, *, cancel=None) -> dict:
        return self._client.get_json(
            f"{BASE_URL}/ss-delineate/v1/delineate/sshydro/{region}",
            params={"lat": lat, "lon": lon}, cancel=cancel,
            cache_key=f"delineate/{region}/{lat},{lon}", service_version=METHOD_VERSION)

    def scenarios(self, region: str, *, statistic_groups: str = PEAK_FLOW_STATISTIC_GROUP,
                  cancel=None) -> list:
        return self._client.get_json(
            f"{BASE_URL}/nssservices/scenarios",
            params={"regions": region, "statisticgroups": statistic_groups},
            validate=_validate_list, cancel=cancel)

    def basin_characteristics(self, region: str, lat: float, lon: float, codes: list[str], *,
                              cancel=None) -> list:
        data = self._client.post_json(
            f"{BASE_URL}/ss-hydro/v1/basin-characteristics/calculate-using-ssdelineate/",
            params={"region": region, "lat": lat, "lon": lon, "BCs": ";".join(codes)},
            cancel=cancel)
        return data.get("parameters", data) if isinstance(data, dict) else data

    def estimate(self, region: str, scenarios: list, *, cancel=None) -> list:
        return self._client.post_json(
            f"{BASE_URL}/nssservices/scenarios/estimate",
            params={"regions": region}, data=scenarios,
            validate=_validate_list, cancel=cancel)

    # ---- orchestration ------------------------------------------------------
    def lookup_flow(self, region: str, lat: float, lon: float, *,
                    statistic_groups: str = PEAK_FLOW_STATISTIC_GROUP,
                    want_national: bool = False,
                    cancel: Callable[[], bool] | None = None) -> FlowLookupSnapshot:
        """Run the full regional workflow (+ national fallback) into a FlowLookupSnapshot."""
        warnings: list[HypeWarning] = []
        endpoints = {
            "delineate": f"{BASE_URL}/ss-delineate/v1/delineate/sshydro/{region}",
            "scenarios": f"{BASE_URL}/nssservices/scenarios",
            "basin_characteristics":
                f"{BASE_URL}/ss-hydro/v1/basin-characteristics/calculate-using-ssdelineate/",
            "estimate": f"{BASE_URL}/nssservices/scenarios/estimate",
        }
        snap = FlowLookupSnapshot(
            requested_point=LatLon(lat=lat, lon=lon), selected_region=region,
            service_endpoints=endpoints, retrieved_at=datetime.now(timezone.utc),
            methods=[f"NSS statistic group {statistic_groups}"])

        dl = self.delineate(region, lat, lon, cancel=cancel)
        ws = (((dl or {}).get("bcrequest") or {}).get("wsresp") or {})
        fc = ws.get("featurecollection") or []
        if not fc:
            warnings.append(HypeWarning(code="no_watershed",
                                        message="StreamStats returned no watershed for this point.",
                                        severity=Severity.error))
            snap.warnings = warnings
            return snap
        snap.watershed_geojson = {"featurecollection": fc, "workspace_id": ws.get("workspace_id")}

        scen = self.scenarios(region, statistic_groups=statistic_groups, cancel=cancel)
        codes = sorted({str(p.get("code")) for s in scen
                        for rr in s.get("regressionRegions", [])
                        for p in rr.get("parameters", []) if p.get("code")})
        snap.regression_regions = sorted({rr.get("name") for s in scen
                                          for rr in s.get("regressionRegions", []) if rr.get("name")})
        if not scen:
            warnings.append(HypeWarning(code="no_scenarios",
                                        message=f"No NSS scenarios for region {region}."))

        bc = self.basin_characteristics(region, lat, lon, codes, cancel=cancel) if codes else []
        values = {str(p.get("code")).upper(): p.get("value") for p in bc
                  if isinstance(p, dict) and p.get("code") is not None}
        snap.basin_characteristics = values
        missing = [c for c in codes if c.upper() not in values]
        if missing:
            warnings.append(HypeWarning(code="missing_basin_char",
                                        message=f"Basin characteristics unavailable: {missing}"))

        for s in scen:                      # fill parameter values case-insensitively (§5.2 step 6)
            for rr in s.get("regressionRegions", []):
                for p in rr.get("parameters", []):
                    if str(p.get("code", "")).upper() in values:
                        p["value"] = values[str(p["code"]).upper()]

        candidates: list[FlowCandidate] = []
        try:
            est = self.estimate(region, scen, cancel=cancel)
            candidates = self._normalize(est, region=region, national=False)
        except PayloadError as e:
            warnings.append(HypeWarning(code="estimate_failed", message=str(e),
                                        severity=Severity.error))

        insertable = [c for c in candidates if c.insertable]
        if want_national or not insertable:
            nat, nat_warn = self._national(region, lat, lon, statistic_groups, cancel=cancel)
            candidates.extend(nat)
            warnings.extend(nat_warn)
            if not insertable and not any(c.insertable for c in nat):
                warnings.append(HypeWarning(
                    code="no_usable_discharge",
                    message="No usable regional or national discharge statistic was found.",
                    severity=Severity.warning))

        snap.candidates = candidates
        snap.warnings = warnings
        return snap

    def _national(self, region, lat, lon, statistic_groups, *, cancel=None):
        """National catalog fallback (§5.2 step 9). Best-effort — flags candidates is_national and
        never raises: a national outage must not sink the regional result."""
        warnings: list[HypeWarning] = []
        try:
            scen = self.scenarios("US", statistic_groups=statistic_groups, cancel=cancel)
            if not scen:
                return [], warnings
            codes = sorted({str(p.get("code")) for s in scen
                            for rr in s.get("regressionRegions", [])
                            for p in rr.get("parameters", []) if p.get("code")})
            bc = self.basin_characteristics("US", lat, lon, codes, cancel=cancel) if codes else []
            values = {str(p.get("code")).upper(): p.get("value") for p in bc
                      if isinstance(p, dict) and p.get("code") is not None}
            for s in scen:
                for rr in s.get("regressionRegions", []):
                    for p in rr.get("parameters", []):
                        if str(p.get("code", "")).upper() in values:
                            p["value"] = values[str(p["code"]).upper()]
            est = self.estimate("US", scen, cancel=cancel)
            return self._normalize(est, region="US", national=True), warnings
        except Exception as e:  # noqa: BLE001
            warnings.append(HypeWarning(code="national_unavailable",
                                        message=f"National fallback unavailable: {e}"))
            return [], warnings

    @staticmethod
    def _normalize(est: list, *, region: str, national: bool) -> list[FlowCandidate]:
        out: list[FlowCandidate] = []
        for group in est or []:
            sg_id = group.get("statisticGroupID")
            sg_name = group.get("statisticGroupName")
            for rr in group.get("regressionRegions", []):
                in_range, values, ranges = _param_range_check(rr.get("parameters", []))
                rr_name = rr.get("name")
                approval = f"statusID={rr.get('statusID')}" if rr.get("statusID") is not None else None
                citations = ([Citation(title=f"NSS citationID {rr['citationID']}")]
                             if rr.get("citationID") is not None else [])
                for res in rr.get("results", []):
                    val = res.get("value")
                    if not isinstance(val, (int, float)):
                        continue
                    unit_abbr = (res.get("unit") or {}).get("abbr", "")
                    cfs, cms = _normalize_discharge(val, unit_abbr)
                    aep, recur = _aep_and_recurrence(res.get("name", ""))
                    w: list[HypeWarning] = []
                    if cfs is None:
                        w.append(HypeWarning(code="non_discharge",
                                             message=f"'{res.get('name')}' is {unit_abbr}, not a "
                                                     "discharge — cannot populate the flow input."))
                    if not in_range:
                        w.append(HypeWarning(code="extrapolated",
                                             message="A basin characteristic is outside the "
                                                     "regression limits — result extrapolated.",
                                             severity=Severity.warning))
                    if national:
                        w.append(HypeWarning(code="national",
                                             message="National estimate — review before use."))
                    out.append(FlowCandidate(
                        id=f"{region}:{rr.get('code')}:{res.get('code')}",
                        statistic_group=sg_name or (str(sg_id) if sg_id is not None else None),
                        statistic_code=res.get("code"), result_code=res.get("code"),
                        description=res.get("description") or res.get("name"),
                        original_value=float(val), original_unit=unit_abbr or "unknown",
                        value_cfs=cfs, value_cms=cms,
                        recurrence_years=recur, annual_exceedance_prob=aep,
                        regression_region=rr_name, equation=res.get("equation"),
                        parameters=values, parameter_ranges=ranges, in_range=in_range,
                        approval_status=approval, is_national=national,
                        is_extrapolated=not in_range, warnings=w, citations=citations))
        return out


__all__ = ["StreamStatsClient", "BASE_URL", "METHOD_VERSION"]
