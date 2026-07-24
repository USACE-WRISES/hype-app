"""Per-zone editable K (2026-07-16): every K-zone Feature carries its own
{uid, KH, KV, LABEL, src} properties. geometry.normalize_kzone_features assigns them to bare
geometry (fresh draws, pre-revision saves) and passes carried properties through untouched;
geometry.kzones_to_gdf turns the features into the per-row KH/KV/ZONE_ID/LABEL GeoDataFrame
the engine's _kh_arrays_from_polygon already honors (see test_soil_k.py for the overlay
contract)."""
from hype_app.geometry import kzones_to_gdf, normalize_kzone_features

SQ = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def _bare(geom=SQ):
    return {"type": "Feature", "properties": {}, "geometry": geom}


def _zone(kh, kv, uid="abc123", label="Sand", src="nrcs"):
    return {"type": "Feature",
            "properties": {"uid": uid, "KH": kh, "KV": kv, "LABEL": label, "src": src},
            "geometry": SQ}


def test_normalize_assigns_uid_and_defaults_to_bare_features():
    out = normalize_kzone_features([_bare(), _bare()], default_kh=42.0, default_kv=4.2)
    assert len(out) == 2
    uids = [f["properties"]["uid"] for f in out]
    assert all(uids) and len(set(uids)) == 2
    for i, f in enumerate(out):
        p = f["properties"]
        assert p["KH"] == 42.0 and p["KV"] == 4.2
        assert p["LABEL"] == f"Zone {i + 1}" and p["src"] == "drawn"


def test_normalize_passes_carried_properties_through():
    z = _zone(99.0, 9.9)
    out = normalize_kzone_features([z], default_kh=1.0, default_kv=0.1)
    p = out[0]["properties"]
    assert (p["uid"], p["KH"], p["KV"], p["LABEL"], p["src"]) == \
        ("abc123", 99.0, 9.9, "Sand", "nrcs")
    assert z["properties"]["KH"] == 99.0            # input never mutated


def test_normalize_strips_edit_style_and_drops_empty():
    z = _bare()
    z["properties"] = {"style": {"color": "#f00"}}
    out = normalize_kzone_features([z, None, {"type": "Feature", "properties": {}}],
                                   default_kh=5.0, default_kv=0.5)
    assert len(out) == 1
    assert "style" not in out[0]["properties"]


def test_normalize_repairs_bad_values():
    z = _zone("not-a-number", -3.0)
    out = normalize_kzone_features([z], default_kh=7.0, default_kv=0.7)
    p = out[0]["properties"]
    assert p["KH"] == 7.0 and p["KV"] == 0.7        # non-numeric / non-positive -> defaults


def test_gdf_carries_per_row_values():
    gdf = kzones_to_gdf([_zone(99.0, 9.9, uid="a", label="Sand"),
                         _zone(12.0, 1.2, uid="b", label="Silt", src="drawn")],
                        fallback_kh=50.0, fallback_kv=5.0)
    assert list(gdf["KH"]) == [99.0, 12.0]
    assert list(gdf["KV"]) == [9.9, 1.2]
    assert list(gdf["ZONE_ID"]) == ["a", "b"]
    assert list(gdf["LABEL"]) == ["Sand", "Silt"]
    assert str(gdf.crs) == "EPSG:4326" and len(gdf.geometry) == 2


def test_gdf_falls_back_for_bare_features():
    gdf = kzones_to_gdf([_bare()], fallback_kh=50.0, fallback_kv=5.0)
    assert list(gdf["KH"]) == [50.0] and list(gdf["KV"]) == [5.0]
    assert list(gdf["LABEL"]) == ["Zone 1"]
