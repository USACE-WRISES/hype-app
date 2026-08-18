"""Pack a finished HYPE desktop project folder into a publishable example.

    python tools/examples/pack.py <project_folder | main.hype> --out <dir> \\
        --id SS01208 --title "San Saba River, SS01208" \\
        --description "..." --tags "san saba,steep,small stream" [--verify] [--thumbnail]

Produces `<out>/<id>.hype` (a COMPLETE standalone bundle: results included, aerials and GMS
never travel by construction) plus `<out>/<id>.json`, a catalog row ready to paste into
`hype_app/data/examples.json` (size_bytes + sha256 filled in, url pointing at the
`examples-N` release asset). `--thumbnail` renders `www/examples/<id>.jpg` from the site's own
terrain, wetted extent, domain and reach.

What the pack changes in the settings (token space, never detokenized, mirroring
tools/site_factory/drive.py stage_bundle):
* `desktop_project` is dropped: the example is a portable bundle, not a folder's main file
  (left in place, Open on the cached copy would try to open the cache directory as a project).
* map_layers rows under `$WORKSPACE$/aerials/` are dropped: aerials never travel.
* a local-file `dem_source` pointer is neutralized: the working copy inputs/dem.tif travels,
  and the pointer would only raise a missing-file card on the recipient's machine.
* format_version = the current bundle format.
Everything else is exactly the project's own state, so Results/Report hydrate on open.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

os.environ.setdefault("HYPE_DESKTOP", "1")

from hype_app import bundle, examples  # noqa: E402

RELEASE_TAG_DEFAULT = "examples-1"


def app_version() -> str:
    m = re.search(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', (REPO / "app.py").read_text(encoding="utf-8"),
                  re.MULTILINE)
    return m.group(1) if m else ""


def find_main_file(target: Path) -> Path:
    if target.is_file():
        return target
    hypes = sorted(p for p in target.glob("*.hype") if p.is_file())
    if len(hypes) != 1:
        raise SystemExit(f"expected exactly one .hype main file in {target}, found {len(hypes)}")
    return hypes[0]


def scrub_state(state: dict) -> dict:
    """The token-space edits listed in the module docstring. Pure; returns a new dict."""
    st = dict(state or {})
    st.pop("desktop_project", None)
    rows = st.get("map_layers")
    if isinstance(rows, list):
        st["map_layers"] = [r for r in rows
                            if not str((r or {}).get("path") or "").startswith("$WORKSPACE$/aerials/")]
    ds = st.get("dem_source")
    if isinstance(ds, dict) and (ds.get("path") or ds.get("kind") in ("file", "local")):
        st.pop("dem_source", None)
    st["format_version"] = bundle.FORMAT_VERSION
    return st


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def pack(main_file: Path, out_dir: Path, ex_id: str) -> Path:
    work_dir = main_file.parent
    payload = bundle.restore_in_place(main_file)
    state = scrub_state(payload.get("state") or {})
    tmp = bundle.zip_workspace(
        work_dir, vectors=payload.get("vectors") or {}, params=payload.get("params"),
        run_config=payload.get("run_config"), state=state,
        assessment_input=payload.get("assessment_input"),
        scoring_profile=payload.get("scoring_profile"), include_computed=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{ex_id}.hype"
    shutil.move(tmp, dest)               # zip_workspace builds on %TEMP%: move, not rename
    return dest


def verify(dest: Path) -> dict:
    """Re-open the packed bundle into a scratch dir and report what hydrates."""
    tmp = Path(tempfile.mkdtemp(prefix="hype_pack_verify_"))
    try:
        payload = bundle.restore_workspace(dest, tmp)
        restored = payload["restored"]
        st = payload["state"] or {}
        checks = {
            "gwf_workspace": any(p.startswith("model/gwf_workspace/") for p in restored),
            "hz": any(p.startswith("summary/hz/") for p in restored),
            "report": any(p.startswith("report/") for p in restored),
            "dem": "inputs/dem.tif" in restored,
            "no_desktop_project": "desktop_project" not in st,
            "no_aerial_rows": not any(str((r or {}).get("path") or "").startswith("$WORKSPACE$/aerials/")
                                      for r in (st.get("map_layers") or [])),
        }
        return {"ok": all(checks.values()), "checks": checks, "extracted": payload["extracted"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def render_thumbnail(work_dir: Path, vectors: dict, state: dict, out_png: Path,
                     size=(640, 400)) -> Path:
    """Hillshade + wetted extent + domain + reach + wells, cropped to the domain with a margin,
    no axes, no text. Written as JPEG (quality 82) so a tile costs tens of KB."""
    import geopandas as gpd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import rasterio
    from matplotlib.colors import LightSource
    from shapely.geometry import shape

    dem_path = work_dir / "inputs" / "dem.tif"
    wse_path = work_dir / "inputs" / "wse_ras.tif"
    with rasterio.open(dem_path) as src:
        z = src.read(1, masked=True)
        crs = src.crs
        b = src.bounds
    extent = [b.left, b.right, b.bottom, b.top]
    hs = LightSource(azdeg=315, altdeg=45).hillshade(np.where(z.mask, np.nan, z.filled(np.nan)),
                                                     vert_exag=2)
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100, facecolor="#e6ecf5")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor("#e6ecf5")           # beyond the DEM footprint: the app's soft tint
    ax.imshow(hs, cmap="gray", extent=extent, alpha=0.85, zorder=0, interpolation="bilinear")

    def gs(feat):
        geoms = feat if isinstance(feat, list) else [feat]
        return gpd.GeoSeries([shape(f["geometry"]) for f in geoms], crs=4326).to_crs(crs)

    if wse_path.is_file():
        with rasterio.open(wse_path) as ws:
            w = ws.read(1, masked=True)
            wb = ws.bounds
        wet = np.where(w.mask, np.nan, 1.0)
        ax.imshow(wet, cmap=matplotlib.colors.ListedColormap(["#4f8fd6"]),
                  extent=[wb.left, wb.right, wb.bottom, wb.top], alpha=0.55, zorder=1,
                  interpolation="nearest")
    dom = vectors.get("domain")
    if dom:
        d = gs(dom)
        d.plot(ax=ax, facecolor="#2f4b7c", edgecolor="#2f4b7c", alpha=0.10, linewidth=0, zorder=2)
        d.boundary.plot(ax=ax, color="#2f4b7c", linewidth=1.8, zorder=3)
        minx, miny, maxx, maxy = d.total_bounds
    else:
        minx, miny, maxx, maxy = b.left, b.bottom, b.right, b.top
    reach = vectors.get("reach")
    if reach:
        gs(reach).plot(ax=ax, color="#0b7285", linewidth=2.2, zorder=4)
    wells = [w for w in (state.get("obs_wells") or []) if w.get("lat") and w.get("lon")]
    if wells:
        pts = gpd.GeoSeries(gpd.points_from_xy([w["lon"] for w in wells], [w["lat"] for w in wells]),
                            crs=4326).to_crs(crs)
        ax.scatter([p.x for p in pts], [p.y for p in pts], s=26, color="#ff9500",
                   edgecolor="#2a3344", linewidth=.8, zorder=5)
    # crop: domain bbox + 12 % margin, forced to the tile's aspect ratio
    mx, my = (maxx - minx) * 0.12, (maxy - miny) * 0.12
    x0, x1, y0, y1 = minx - mx, maxx + mx, miny - my, maxy + my
    aspect = size[0] / size[1]
    w_, h_ = x1 - x0, y1 - y0
    if w_ / h_ > aspect:
        pad = (w_ / aspect - h_) / 2
        y0, y1 = y0 - pad, y1 + pad
    else:
        pad = (h_ * aspect - w_) / 2
        x0, x1 = x0 - pad, x1 + pad
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.axis("off")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=100, facecolor=fig.get_facecolor(),
                pil_kwargs={"quality": 82, "optimize": True})
    plt.close(fig)
    return out_png


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="project folder or its main .hype file")
    ap.add_argument("--out", required=True, help="output directory for <id>.hype + <id>.json")
    ap.add_argument("--id", required=True, help="example id (asset basename), e.g. SS01208")
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--tags", default="", help="comma-separated")
    ap.add_argument("--credit", default="")
    ap.add_argument("--release-tag", default=RELEASE_TAG_DEFAULT,
                    help="GitHub release tag the asset will live under (url in the sidecar)")
    ap.add_argument("--asset-name", default=None,
                    help="asset filename on the release (default <id>.hype; use <id>-2.hype "
                         "when re-publishing a rebuilt example)")
    ap.add_argument("--verify", action="store_true", help="re-open the output and check hydration")
    ap.add_argument("--thumbnail", action="store_true",
                    help="render www/examples/<id>.jpg from the project's own data")
    ap.add_argument("--thumbnail-out", default=None, help="override the thumbnail path")
    a = ap.parse_args(argv)

    main_file = find_main_file(Path(a.target).resolve())
    out_dir = Path(a.out).resolve()
    dest = pack(main_file, out_dir, a.id)
    size = dest.stat().st_size
    sha = sha256_of(dest)
    asset = a.asset_name or f"{a.id}.hype"
    row = {
        "id": a.id,
        "title": a.title,
        "description": a.description,
        "tags": [t.strip() for t in a.tags.split(",") if t.strip()],
        "size_bytes": size,
        "sha256": sha,
        "url": f"{examples.EXAMPLES_URL_PREFIX}{a.release_tag}/{asset}",
        "thumbnail": f"examples/{a.id}.jpg",
        "published": date.today().isoformat(),
        "format_version": bundle.FORMAT_VERSION,
        "min_app_version": "",
        "desktop_only": False,
        "credit": a.credit,
        "app_version": app_version(),
    }
    (out_dir / f"{a.id}.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    print(f"packed {dest}  {size / 1e6:.1f} MB  sha256 {sha[:12]}…")
    if a.verify:
        v = verify(dest)
        print(f"verify: {'OK' if v['ok'] else 'FAILED'}  extracted={v['extracted']}  "
              + "  ".join(f"{k}={'y' if ok else 'N'}" for k, ok in v["checks"].items()))
        if not v["ok"]:
            return 2
    if a.thumbnail:
        payload = bundle.restore_in_place(main_file)
        out_png = Path(a.thumbnail_out) if a.thumbnail_out else REPO / "www" / "examples" / f"{a.id}.jpg"
        render_thumbnail(main_file.parent, payload.get("vectors") or {}, payload.get("state") or {},
                         out_png)
        print(f"thumbnail {out_png}  {out_png.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
