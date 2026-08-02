"""Site-map figure suite tests (report §10): basemap fetch contract, offline degradation,
and the individual map/3-D producers."""
import numpy as np

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _Resp:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_basemap_image_url(monkeypatch):
    """The export request carries the absolute bbox/SR/format, the return is absolute-extent
    raw bytes, and the _fetch_basemap drape wrapper keeps its local-frame data-URI contract."""
    from hype_app import mesh

    seen = {}

    def fake_urlopen(url, timeout=None):
        seen["url"] = url
        return _Resp(b"\xff" * 2048)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = mesh.fetch_basemap_image("EPSG:32617", 500000, 4199000, 500500, 4199400,
                                 service="USGSTopo", fmt="png", max_px=256,
                                 log=lambda *a: None)
    assert r["data"] == b"\xff" * 2048
    assert r["extent"] == (500000.0, 500500.0, 4199000.0, 4199400.0)
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(seen["url"]).query)
    assert q["bbox"][0] == "500000,4199000,500500,4199400"
    assert q["bboxSR"][0] == "32617" and q["imageSR"][0] == "32617"
    assert q["format"][0] == "png"
    assert "/USGSTopo/" in seen["url"]
    # tiny/error responses are rejected
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _Resp(b"err"))
    assert mesh.fetch_basemap_image("EPSG:32617", 0, 0, 100, 100, log=lambda *a: None) is None
    # a CRS with no EPSG code degrades to None (never raises)
    assert mesh.fetch_basemap_image(None, 0, 0, 100, 100, log=lambda *a: None) is None

    # the 3-D drape wrapper: unchanged local-frame payload
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    d = mesh._fetch_basemap("EPSG:32617", 500000, 4199000, 500, 400, service="USGSTopo",
                            log=lambda *a: None)
    assert d["url"].startswith("data:image/jpeg;base64,")
    assert (d["x0"], d["y0"], d["x1"], d["y1"]) == (0.0, 0.0, 500.0, 400.0)


def test_map_suite_offline(monkeypatch, fake_spatial):
    """With the basemap fetch down (offline desktop, endpoint outage), every figure in the
    suite still renders: vector-only maps, raster maps, and the DEM-fallback 3-D view."""
    from hype_app import figures

    monkeypatch.setattr("hype_app.mesh.fetch_basemap_image", lambda *a, **k: None)
    out = figures.render_map_suite(fake_spatial)
    for key in ("map_topo", "map_imagery", "map_wse", "map_head", "map_paths", "map_3d"):
        assert out.get(key) and out[key][:8] == PNG_MAGIC, f"missing figure: {key}"


def test_map_producers_degrade(tmp_path):
    """Every producer returns None (never raises) when its inputs are missing, and
    degenerate residence times do not break the paths map."""
    from hype_app import figures as F

    assert F.render_vector_map() is None
    assert F.render_wse_map(bbox=(0.0, 1.0, 0.0, 1.0), wse_tif=str(tmp_path / "nope.tif")) is None
    assert F.render_head_map(bbox=(0.0, 1.0, 0.0, 1.0), head_tif=None) is None
    assert F.render_paths_map(bbox=(0.0, 1.0, 0.0, 1.0), paths_gdf=None) is None
    assert F.render_iso3d() is None
    assert F.render_map_suite({}) == {}
    assert F.render_map_suite(None) == {}

    import geopandas as gpd
    from shapely.geometry import LineString
    g = gpd.GeoDataFrame({"total_time_d": [0.0, 0.0]},
                         geometry=[LineString([(0, 0), (5, 5)]),
                                   LineString([(1, 0), (6, 5)])], crs="EPSG:32617")
    png = F.render_paths_map(bbox=(-1.0, 7.0, -1.0, 6.0), paths_gdf=g)
    assert png and png[:8] == PNG_MAGIC


def test_report_bbox_pads_and_unions():
    from hype_app.figures import _report_bbox

    assert _report_bbox() is None
    bb = _report_bbox(xy_lists=[[(0.0, 0.0)]])                 # a point still gets a stage
    assert bb[0] < 0.0 < bb[1] and bb[2] < 0.0 < bb[3]

    import geopandas as gpd
    from shapely.geometry import LineString
    g = gpd.GeoDataFrame(geometry=[LineString([(100.0, 200.0), (300.0, 400.0)])],
                         crs="EPSG:32617")
    bb2 = _report_bbox(xy_lists=[[(0.0, 0.0)]], gdfs=[g])
    assert bb2[0] < 0.0 and bb2[1] > 300.0 and bb2[3] > 400.0  # union of both sources


def test_texture_orientation():
    """The imagery texture is flipped to south-first rows: a north-west image pixel must
    land in the LAST texture row, first column."""
    from hype_app.figures import _texture_for_grid

    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[0, 0] = (255, 0, 0)                                    # NW corner red
    ext = (0.0, 100.0, 0.0, 100.0)
    tex = _texture_for_grid(img, ext, ext, (2, 2))
    assert tex.shape == (2, 2, 4)
    assert tex[1, 0, 0] > tex[0, 0, 0]                          # red ends up in the north row
    assert np.all(tex[..., 3] == 1.0)


def test_full_coverage_layer(tmp_path):
    """Picks the first head layer whose valid cells span the whole active footprint;
    falls back to the best-covering layer, and to 1 on empty input."""
    import rasterio
    from rasterio.transform import from_origin

    from hype_app.results import full_coverage_layer

    def _w(name, data):
        p = tmp_path / name
        with rasterio.open(p, "w", driver="GTiff", height=6, width=8, count=1,
                           dtype="float32", crs="EPSG:32617", nodata=-9999.0,
                           transform=from_origin(0.0, 60.0, 10.0, 10.0)) as dst:
            dst.write(data.astype("float32"), 1)
        return str(p)

    full = np.full((6, 8), 5.0)
    partial = full.copy()
    partial[:3, :] = -9999.0                      # top layer clipped (above-ground idomain)
    assert full_coverage_layer([_w("head_L01.tif", partial),
                                _w("head_L02.tif", full),
                                _w("head_L03.tif", full)]) == 2
    partial2 = full.copy()
    partial2[0, 0] = -9999.0                      # nothing covers fully -> best coverage
    assert full_coverage_layer([_w("a.tif", partial), _w("b.tif", partial2)]) == 2
    assert full_coverage_layer([]) == 1


def test_autocrop_trims_white_margins():
    import io as _io

    from PIL import Image

    from hype_app.figures import _autocrop

    img = np.full((200, 300, 3), 255, dtype=np.uint8)
    img[80:120, 100:200] = 30                     # dark box inside a white canvas
    buf = _io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    out = _autocrop(buf.getvalue(), pad=5)
    assert Image.open(_io.BytesIO(out)).size == (110, 50)


def test_vector_map_draws_boundary_sides():
    """Colored boundary-condition lines render (offline, no basemap) without raising."""
    from hype_app.figures import render_vector_map

    sides = {"up": [(0.0, 0.0), (0.0, 10.0)], "down": [(10.0, 0.0), (10.0, 10.0)],
             "left": [(0.0, 0.0), (10.0, 0.0)], "right": [(0.0, 10.0), (10.0, 10.0)]}
    png = render_vector_map(bbox=(-2.0, 12.0, -2.0, 12.0), sides_xy=sides,
                            reach_xy=[(1.0, 5.0), (9.0, 5.0)])
    assert png and png[:8] == PNG_MAGIC


def test_logtime_norm_guards():
    from hype_app.figures import _logtime_norm

    n = _logtime_norm([0.0, -1.0, float("nan")])                # nothing usable -> safe defaults
    assert n.vmin > 0 and n.vmax > n.vmin
    n2 = _logtime_norm([5.0, 5.0])                              # constant times -> widened span
    assert n2.vmax >= n2.vmin * 10
