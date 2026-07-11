"""Record real NRCS Soil Data Access (SDA) responses as offline fixtures.

Run once (needs network); saves raw JSON into tests/fixtures/nrcs/. Not collected by pytest.
    py -3.12 tests/record_nrcs.py
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "fixtures" / "nrcs"
SDA = "https://sdmdataaccess.nrcs.usda.gov/Tabular/post.rest"
# small AOI around Mink Brook, Hanover NH (lon/lat, EPSG:4326)
BBOX = ("POLYGON((-72.245 43.682, -72.230 43.682, -72.230 43.692, "
        "-72.245 43.692, -72.245 43.682))")


def _post(sql: str) -> dict:
    r = httpx.post(SDA, json={"query": sql, "format": "JSON+COLUMNNAME"},
                   timeout=httpx.Timeout(90.0, connect=15.0), follow_redirects=True)
    r.raise_for_status()
    return r.json()


def _save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"  saved {name} ({len(json.dumps(data))} bytes, "
          f"{len(data.get('Table', [])) - 1} rows)")


def main():
    print("1. spatial mupolygon ...")
    spatial = _post(
        "SELECT mukey, mupolygonkey, mupolygongeo.STAsText() AS geom "
        f"FROM mupolygon WHERE mupolygongeo.STIntersects("
        f"geometry::STPolyFromText('{BBOX}', 4326)) = 1")
    _save("mupolygon.json", spatial)
    rows = spatial.get("Table", [])[1:]
    mukeys = sorted({r[0] for r in rows})
    print("   mukeys:", mukeys)
    inlist = ",".join(f"'{m}'" for m in mukeys)

    print("2. tabular horizons (mapunit+component+chorizon+texture) ...")
    horizons = _post(
        "SELECT mu.mukey, mu.muname, mu.musym, c.cokey, c.compname, c.comppct_r, "
        "c.majcompflag, ch.chkey, ch.hzname, ch.hzdept_r, ch.hzdepb_r, ch.ksat_r, "
        "ct.texcl AS texture "
        "FROM mapunit mu JOIN component c ON c.mukey = mu.mukey "
        "LEFT JOIN chorizon ch ON ch.cokey = c.cokey "
        "LEFT JOIN chtexturegrp ctg ON ctg.chkey = ch.chkey AND ctg.rvindicator = 'Yes' "
        "LEFT JOIN chtexture ct ON ct.chtgkey = ctg.chtgkey "
        f"WHERE mu.mukey IN ({inlist}) "
        "ORDER BY mu.mukey, c.comppct_r DESC, ch.hzdept_r")
    _save("horizons.json", horizons)

    print("3. restrictions ...")
    restrictions = _post(
        "SELECT c.mukey, c.cokey, cr.reskind, cr.resdept_r "
        "FROM component c JOIN corestrictions cr ON cr.cokey = c.cokey "
        f"WHERE c.mukey IN ({inlist})")
    _save("restrictions.json", restrictions)

    print("4. survey version ...")
    survey = _post(
        "SELECT DISTINCT mu.mukey, l.areasymbol, sac.saverest "
        "FROM mapunit mu JOIN legend l ON l.lkey = mu.lkey "
        "JOIN sacatalog sac ON sac.areasymbol = l.areasymbol "
        f"WHERE mu.mukey IN ({inlist})")
    _save("survey.json", survey)


if __name__ == "__main__":
    main()
