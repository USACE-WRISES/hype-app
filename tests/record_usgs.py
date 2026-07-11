"""Record real USGS StreamStats/NSS responses as offline test fixtures.

Run once (needs network); saves raw JSON into tests/fixtures/usgs/. Not collected by pytest.
    py -3.12 tests/record_usgs.py [region] [lat] [lon]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "fixtures" / "usgs"
SS_DELINEATE = "https://streamstats.usgs.gov/ss-delineate"
SS_HYDRO = "https://streamstats.usgs.gov/ss-hydro"
NSS = "https://streamstats.usgs.gov/nssservices"


def _save(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
    n = len(json.dumps(data))
    print(f"  saved {name} ({n} bytes)")


def main(region="NH", lat=43.686, lon=-72.237) -> None:
    c = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True)

    print("1. delineate ...")
    dl = c.get(f"{SS_DELINEATE}/v1/delineate/sshydro/{region}", params={"lat": lat, "lon": lon})
    print("   status", dl.status_code)
    _save("delineate.json", dl.json())

    print("2. statisticgroups + regressionregions + scenarios ...")
    _save("statisticgroups.json", c.get(f"{NSS}/statisticgroups").json())
    _save("regressionregions.json",
          c.get(f"{NSS}/regressionregions", params={"regions": region}).json())
    scen = c.get(f"{NSS}/scenarios",
                 params={"regions": region, "statisticgroups": "2"}).json()
    _save("scenarios.json", scen)

    # union of required parameter codes across all regression regions/scenarios
    codes = sorted({p["code"] for s in scen for rr in s.get("regressionRegions", [])
                    for p in rr.get("parameters", [])})
    print("   required BC codes:", codes)

    print("3. basin characteristics (ss-hydro) ...")
    bc = c.post(f"{SS_HYDRO}/v1/basin-characteristics/calculate-using-ssdelineate/",
                params={"region": region, "lat": lat, "lon": lon, "BCs": ";".join(codes)})
    print("   status", bc.status_code)
    bc_json = bc.json()
    _save("basin_chars.json", bc_json)
    print("   basin_chars shape:", json.dumps(bc_json, indent=1)[:600])

    print("4. fill parameter values + estimate ...")
    # map computed BC code -> value (shape discovered from basin_chars.json print above)
    values = {}
    parameters = bc_json.get("parameters") if isinstance(bc_json, dict) else bc_json
    if isinstance(parameters, list):
        for p in parameters:
            if isinstance(p, dict) and "code" in p and "value" in p:
                values[str(p["code"]).upper()] = p["value"]
    print("   computed values:", values)
    for s in scen:
        for rr in s.get("regressionRegions", []):
            for p in rr.get("parameters", []):
                if str(p.get("code", "")).upper() in values:
                    p["value"] = values[str(p["code"]).upper()]
    est = c.post(f"{NSS}/scenarios/estimate", params={"regions": region}, json=scen)
    print("   estimate status", est.status_code)
    try:
        est_json = est.json()
        _save("estimate.json", est_json)
        print("   estimate shape:", json.dumps(est_json, indent=1)[:800])
    except Exception as e:  # noqa: BLE001
        print("   estimate not JSON:", est.text[:300], e)
    c.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    main(*(args[:1] or ["NH"]), *(float(a) for a in args[1:3]))
