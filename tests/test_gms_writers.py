"""Binary/text writers for the GMS MODFLOW + MODPATH folders.

Each binary format gets an INDEPENDENT struct-based reader written from the decoded
spec (not from the writer code), so a writer bug cannot hide behind its own reader.
Setting HYPE_GMS_EXAMPLE=1 additionally runs the same readers against the real
example project's files, proving the readers (and hence the spec) against ground
truth without committing the 400 MB example.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np
import pytest

from hype_app.gms import modflow_files as mff
from hype_app.gms import modpath_files as mpf

EXAMPLE_MODFLOW = (Path(__file__).resolve().parents[1] / "notes"
                   / "Example_GMS_10_7_Project" / "LL01096_MODFLOW")
EXAMPLE_MODPATH = EXAMPLE_MODFLOW.parent / "LL01096_MODPATH_particle set"
example = pytest.mark.skipif(os.getenv("HYPE_GMS_EXAMPLE") != "1",
                             reason="set HYPE_GMS_EXAMPLE=1 (needs the untracked "
                                    "example project) to cross-check readers")


# ---------------------------------------------------------------------------
# independent readers (from the spec)
# ---------------------------------------------------------------------------

def read_hed(path: Path):
    """Classic single-precision binary head file -> (headers, layer arrays)."""
    raw = Path(path).read_bytes()
    off, headers, layers = 0, [], []
    while off < len(raw):
        kstp, kper, pertim, totim = struct.unpack_from("<2i2f", raw, off)
        text = raw[off + 16:off + 32]
        ncol, nrow, ilay = struct.unpack_from("<3i", raw, off + 32)
        off += 44
        vals = np.frombuffer(raw, dtype="<f4", count=ncol * nrow, offset=off)
        off += 4 * ncol * nrow
        headers.append((kstp, kper, pertim, totim, text, ncol, nrow, ilay))
        layers.append(vals.reshape(nrow, ncol))
    return headers, layers


def read_ccf(path: Path):
    """Classic COMPACT single-precision budget -> list of parsed records."""
    raw = Path(path).read_bytes()
    off, out = 0, []
    while off < len(raw):
        kstp, kper = struct.unpack_from("<2i", raw, off)
        text = raw[off + 8:off + 24]
        ncol, nrow, nlay = struct.unpack_from("<3i", raw, off + 24)
        assert nlay < 0, "expected COMPACT budget (negative nlay)"
        imeth, delt, pertim, totim = struct.unpack_from("<i3f", raw, off + 36)
        off += 52
        rec = {"kstp": kstp, "kper": kper, "text": text, "ncol": ncol, "nrow": nrow,
               "nlay": -nlay, "imeth": imeth, "delt": delt, "pertim": pertim,
               "totim": totim}
        if imeth == 2:
            nlist, = struct.unpack_from("<i", raw, off)
            off += 4
            pairs = np.frombuffer(raw, dtype=np.dtype([("node", "<i4"), ("q", "<f4")]),
                                  count=nlist, offset=off)
            off += 8 * nlist
            rec["list"] = pairs
        elif imeth == 1:
            n = ncol * nrow * (-nlay)
            rec["array"] = np.frombuffer(raw, dtype="<f4", count=n,
                                         offset=off).reshape(-nlay, nrow, ncol)
            off += 4 * n
        else:
            raise AssertionError(f"unexpected imeth {imeth}")
        out.append(rec)
    return out


PTH_DTYPE = np.dtype([("flag", "<i4"), ("pid", "<i4"), ("x", "<f4"), ("y", "<f4"),
                      ("zloc", "<f4"), ("zelev", "<f4"), ("time", "<f4"),
                      ("node", "<i4")])


def read_pth(path: Path):
    raw = Path(path).read_bytes()
    assert raw[:11] == b"MODPATH 5.0"
    assert len(raw) >= 84 and (len(raw) - 84) % 32 == 0
    recs = np.frombuffer(raw, dtype=PTH_DTYPE, count=(len(raw) - 84) // 32, offset=80)
    trailer = struct.unpack_from("<i", raw, len(raw) - 4)[0]
    return recs, trailer


# ---------------------------------------------------------------------------
# writer round-trips on synthetic data
# ---------------------------------------------------------------------------

@pytest.fixture()
def head_case():
    nlay, nrow, ncol = 2, 3, 4
    head = np.arange(nlay * nrow * ncol, dtype=float).reshape(nlay, nrow, ncol) + 100.0
    ibound = np.ones((nlay, nrow, ncol), dtype=np.int32)
    ibound[0, 0, 0] = 0
    head[0, 0, 0] = 1e30                    # MF6 inactive marker
    head[1, 2, 3] = -1e30                   # dry marker, negative flavor
    ibound[1, 2, 3] = 0
    return head, ibound


def test_write_hed_roundtrip(tmp_path, head_case):
    head, ibound = head_case
    mff.write_hed(tmp_path, "T", head_g=head, ibound_g=ibound, pertim=1.0, totim=1.0)
    headers, layers = read_hed(tmp_path / "T.hed")
    assert len(headers) == 2
    for k, (kstp, kper, pertim, totim, text, ncol, nrow, ilay) in enumerate(headers):
        assert (kstp, kper, pertim, totim) == (1, 1, 1.0, 1.0)
        assert text == b"            HEAD"
        assert (ncol, nrow, ilay) == (4, 3, k + 1)
    assert layers[0][0, 0] == mff.HNOFLO           # marker clamped BEFORE f4 cast
    assert layers[1][2, 3] == mff.HNOFLO
    assert layers[1][0, 0] == np.float32(head[1, 0, 0])
    assert np.isfinite(np.concatenate([a.ravel() for a in layers])).all()


def test_write_ccf_roundtrip(tmp_path):
    nlay, nrow, ncol = 2, 3, 4
    rng = np.random.default_rng(7)
    frf, fff, flf = (rng.normal(size=(nlay, nrow, ncol)).astype(float)
                     for _ in range(3))
    nodes = np.array([5, 2, 17], dtype=np.int64)
    q = np.array([1.5, -2.5, 0.25])
    mff.write_ccf(tmp_path, "T", chd_node1_g=nodes, chd_q=q, frf_g=frf, fff_g=fff,
                  flf_g=flf, delt=1.0, pertim=1.0, totim=1.0)
    recs = read_ccf(tmp_path / "T.ccf")
    assert [r["text"] for r in recs] == [b"   CONSTANT HEAD", b"FLOW RIGHT FACE ",
                                         b"FLOW FRONT FACE ", b"FLOW LOWER FACE "]
    assert all(r["nlay"] == nlay and r["imeth"] == (2 if i == 0 else 1)
               for i, r in enumerate(recs))
    chd = recs[0]["list"]
    assert chd["node"].tolist() == [2, 5, 17]      # sorted ascending
    assert chd["q"].tolist() == [np.float32(-2.5), np.float32(1.5), np.float32(0.25)]
    np.testing.assert_array_equal(recs[1]["array"], frf.astype(np.float32))
    np.testing.assert_array_equal(recs[2]["array"], fff.astype(np.float32))
    np.testing.assert_array_equal(recs[3]["array"], flf.astype(np.float32))


def _tiny_set():
    return mpf.ModpathSet(
        set_name="forward particles", direction_code=0, rsp_direction=1,
        pid=np.array([1, 1, 2], dtype=np.int32),
        x=np.array([0.5, 1.5, 2.0]), y=np.array([3.0, 2.0, 1.0]),
        zloc=np.array([1.0, 0.5, 0.25]), zelev=np.array([10.0, 9.5, 9.0]),
        time=np.array([0.0, 1.0, 0.0]), node1=np.array([1, 2, 7], dtype=np.int64),
        seed_node1=np.array([1, 7], dtype=np.int64),
        seed_locx=np.array([0.5, 0.5]), seed_locy=np.array([0.5, 0.25]),
        seed_locz=np.array([1.0, 1.0]))


def test_write_pth_roundtrip(tmp_path):
    ms = _tiny_set()
    mpf.write_pth(tmp_path, ms.set_name, ms)
    recs, trailer = read_pth(tmp_path / "forward particles.pth")
    assert trailer == 1
    assert recs["flag"].tolist() == [0, 1, 1]      # 0 only on the file's first record
    assert recs["pid"].tolist() == [1, 1, 2]
    assert recs["node"].tolist() == [1, 2, 7]
    np.testing.assert_allclose(recs["y"], [3.0, 2.0, 1.0])


def test_write_loc_and_mdf_and_nam(tmp_path):
    ms = _tiny_set()
    nlay, nrow, ncol = 2, 3, 4
    ibound = np.ones((nlay, nrow, ncol), dtype=np.int32)
    ibound[0, 1, 2] = 0
    mpf.write_set(tmp_path / "setdir", ms, model_name="Site", ibound_g=ibound,
                  porosity=0.3)
    d = tmp_path / "setdir"
    loc = (d / "forward particles.loc").read_text().splitlines()
    assert len(loc) == 2
    # GMS node 1 = (layer 1, GMS row 1, col 1) -> engine row nrow-1; loc row = 1
    assert loc[0].split()[:3] == ["1", "1", "1"]
    assert loc[0].split()[6:9] == ["0", "0", "0"]
    nam = (d / "forward particles.nam").read_text()
    assert '"..\\Site_MODFLOW\\Site.dis"' in nam
    assert '"..\\Site_MODFLOW\\Site.hed"' in nam and "730" in nam
    assert '"..\\Site_MODFLOW\\Site.ccf"' in nam and "budget 40" in nam
    mdf = (d / "forward particles.mdf").read_text().splitlines()
    assert mdf[0] == "0 -999.0 -888.0 0 1 1"
    assert mdf[1] == "COMPACT BINARY"
    assert mdf.count("INTERNAL 1 (free) -1") == nlay
    assert mdf.count("CONSTANT 0.3") == nlay
    rsp = (d / "forward particles.rsp").read_text()
    assert '"forward particles.nam"' in rsp
    assert rsp.splitlines()[0].startswith("@MODPATH Version 4.00")


def test_text_package_writers(tmp_path):
    mff.write_dis(tmp_path, "T", nlay=2, nrow=3, ncol=4, delr=np.full(4, 1.5),
                  delc=np.full(3, 2.0), perlen=1.0)
    dis = (tmp_path / "T.dis").read_text().splitlines()
    assert dis[4] == "2 3 4 1 4 2"                 # days, metres
    assert dis[6] == "INTERNAL 1.0 (free) -1"
    assert dis[7].split() == ["1.5"] * 4
    assert dis[9].split() == ["2"] * 3
    assert 'HDF5 1.0 -1 "T.h5" "Arrays/top1" 1 0 12' in dis
    assert dis[-1] == "1 1 1.0 SS"

    mff.write_ba6(tmp_path, "T", nlay=2, nrow=3, ncol=4)
    ba6 = (tmp_path / "T.ba6").read_text()
    assert ba6.count("Arrays/ibound") == 2 and ba6.count("Arrays/StartHead") == 2
    assert "\n-999\n" in ba6

    mff.write_lpf(tmp_path, "T", nlay=2, nrow=3, ncol=4)
    lpf = (tmp_path / "T.lpf").read_text().splitlines()
    assert lpf[0].startswith("40 -888")
    assert lpf[1].split() == ["1", "1"]            # LAYTYP
    assert lpf[3].split() == ["-1", "-1"]          # CHANI -> HANI arrays
    assert lpf[4].split() == ["1", "1"]            # LAYVKA -> VANI ratios

    mff.write_chd(tmp_path, "T", nbc=42)
    chd = (tmp_path / "T.chd").read_text().splitlines()
    assert chd[0] == "#GMS_HDF5_01"
    assert chd[2] == f"{42:>10}{0:>10}{0:>10}"
    assert chd[3] == 'GMS_HDF5_01 "T.h5" "Specified Head" 1'

    mff.write_mfs(tmp_path, "T", orig=(10.0, 20.0, 5.0), rotz=0.0,
                  porosity=0.25, vani=3.0)
    mfs = (tmp_path / "T.mfs").read_text()
    assert "STARTING_HEADS_EQUAL_TOPS" not in mfs
    assert "IJK -y +x -z" in mfs and "ORIG 10 20 5" in mfs
    assert "MPOR 1 0 0.25" in mfs and "MVANI 1 0 3" in mfs
    assert 'UNITS "m"' in mfs

    mff.write_mfn(tmp_path, "T")
    mfn = (tmp_path / "T.mfn").read_text()
    for token in ('LIST            2 "T.out"', 'DATA(BINARY)    3 "T.hed"',
                  'DATA(BINARY)   40 "T.ccf"', 'DIS             9 "T.dis"',
                  'CHD            13 "T.chd"'):
        assert token in mfn
    assert "HOB" not in mfn and "LMT6" not in mfn


# ---------------------------------------------------------------------------
# ground-truth cross-checks against the untracked example (opt-in)
# ---------------------------------------------------------------------------

@example
def test_readers_against_example_hed():
    headers, layers = read_hed(EXAMPLE_MODFLOW / "LL01096.hed")
    assert len(headers) == 20
    assert headers[0][4] == b"            HEAD"
    assert all(h[5] == 155 and h[6] == 77 for h in headers)
    assert layers[0].min() == pytest.approx(-999.0)


@example
def test_readers_against_example_ccf():
    recs = read_ccf(EXAMPLE_MODFLOW / "LL01096.ccf")
    assert [r["text"] for r in recs] == [b"   CONSTANT HEAD", b"FLOW RIGHT FACE ",
                                         b"FLOW FRONT FACE ", b"FLOW LOWER FACE "]
    assert recs[0]["list"].shape[0] == 7817


@example
def test_readers_against_example_pth():
    recs, trailer = read_pth(EXAMPLE_MODPATH / "particle set.pth")
    assert trailer == 1
    assert recs.shape[0] == 46618
    assert recs["flag"][0] == 0 and recs["flag"][1:].min() == 1
    assert recs["pid"].min() == 1 and recs["pid"].max() == 1499
    for pid in (1, 2, 1499):
        t = recs["time"][recs["pid"] == pid]
        assert (np.diff(t) >= 0).all()
