"""Golden tests pinning the CURRENT hyporheic-zone classification + volume/footprint
numerics before Phase 5 adds the flux-weighted pass and pore-water storage.

Pins:
* hz_analysis.classify_particles   (origin/exit membership -> 4 exchange classes)
* hz_analysis.cell_volumes         (saturated bulk cell volume, m3 — NO porosity today)
* hz_analysis.cell_class_fractions (streamtube per-cell class split)
* hz_analysis.class_stats          (per-class volume_m3 / footprint_m2 / thickness)
"""
import numpy as np

from hypetool.functions.hz_analysis import (
    CLS,
    HZ_CLASSES,
    MEMBER,
    cell_class_fractions,
    cell_volumes,
    class_stats,
    classify_particles,
)

_EP_DTYPE = np.dtype([
    ("particleid", "<i4"), ("node0", "<i4"), ("node", "<i4"),
    ("status", "<i2"), ("time", "<f4"),
])


def _ep(pid, node0, node, status, time):
    a = np.zeros(len(pid), dtype=_EP_DTYPE)
    a["particleid"] = pid
    a["node0"] = node0
    a["node"] = node
    a["status"] = status
    a["time"] = time
    return a


class TestClassifyParticles:
    def test_four_classes_plus_unresolved(self):
        # member[node] -> boundary membership: node 0 = stream top, node 1 = a side face.
        member = np.array([MEMBER["top"], MEMBER["left"], 0, 0, 0, 0, 0, 0, 0, 0])

        # 5 seeds in interior cells 5..9; fwd terminus = exit, bwd terminus = origin.
        # status 2 = terminated at a boundary (resolved); status 1 = pending (unresolved).
        seed_nodes = [5, 6, 7, 8, 9]
        fwd = _ep([0, 1, 2, 3, 4], seed_nodes, node=[0, 1, 0, 1, 0],
                  status=[2, 2, 2, 2, 1], time=[1, 2, 3, 4, 5])
        bwd = _ep([0, 1, 2, 3, 4], seed_nodes, node=[0, 0, 1, 1, 0],
                  status=[2, 2, 2, 2, 2], time=[10, 20, 30, 40, 50])

        out = classify_particles(fwd, bwd, member, n_seeds=5)

        # seed0 top->top hyporheic; seed1 top->side losing; seed2 side->top gaining;
        # seed3 side->side throughflow; seed4 pending forward => unresolved.
        assert list(out["cls"]) == [
            CLS["hyporheic"], CLS["losing"], CLS["gaining"], CLS["throughflow"], CLS["unresolved"]]
        assert list(out["seed_node"]) == seed_nodes
        assert list(out["fwd_time"]) == [1, 2, 3, 4, 5]
        assert list(out["bwd_time"]) == [10, 20, 30, 40, 50]


class TestCellVolumes:
    def test_saturated_clip_uses_head(self):
        T = np.array([[[10.0]]])
        B = np.array([[[0.0]]])
        head = np.array([[[4.0]]])           # saturated thickness = min(10, 4) - 0 = 4
        vol = cell_volumes(T=T, B=B, delr=np.array([2.0]), delc=np.array([3.0]),
                           head=head, saturated_clip=True)
        assert vol.shape == (1, 1, 1)
        assert vol[0, 0, 0] == 4.0 * 2.0 * 3.0     # 24 m3

    def test_full_thickness_when_no_head(self):
        T = np.array([[[10.0]]])
        B = np.array([[[0.0]]])
        vol = cell_volumes(T=T, B=B, delr=np.array([2.0]), delc=np.array([3.0]),
                           head=None, saturated_clip=True)
        assert vol[0, 0, 0] == 10.0 * 2.0 * 3.0    # 60 m3


class TestCellClassFractionsAndStats:
    def test_streamtube_split_and_volume(self):
        shape = (1, 1, 2)  # nlay, nrow, ncol -> 2 cells (node 0 and node 1)
        # cell 0: seeds hyporheic + losing (both classified) -> denom 2
        # cell 1: seeds hyporheic + unresolved              -> denom 1 (unresolved excluded)
        cls = np.array([CLS["hyporheic"], CLS["losing"], CLS["hyporheic"], CLS["unresolved"]])
        seed_node = np.array([0, 0, 1, 1])

        fr = cell_class_fractions(cls, seed_node, shape)
        assert np.allclose(fr["hyporheic"].ravel(), [0.5, 1.0])
        assert np.allclose(fr["losing"].ravel(), [0.5, 0.0])
        assert np.allclose(fr["n_classified"].ravel(), [2.0, 1.0])

        volumes = np.array([[[10.0, 20.0]]])       # per-cell m3
        xe = np.array([0.0, 1.0, 2.0])             # ncol+1 edges (dx = 1,1)
        ye = np.array([0.0, 1.0])                  # nrow+1 edges (dy = 1)
        T = np.array([[[10.0, 10.0]]])
        B = np.array([[[0.0, 0.0]]])
        stats = class_stats(fr, volumes, xe=xe, ye=ye, T=T, B=B, domain_volume_m3=100.0)

        # hyporheic volume = 0.5*10 + 1.0*20 = 25 ; both cells occupied -> footprint 2 m2
        assert stats["hyporheic"]["volume_m3"] == 25.0
        assert stats["hyporheic"]["footprint_m2"] == 2.0
        assert stats["hyporheic"]["pct_domain_volume"] == 25.0
        # losing volume = 0.5*10 + 0*20 = 5 ; only cell 0 occupied -> footprint 1 m2
        assert stats["losing"]["volume_m3"] == 5.0
        assert stats["losing"]["footprint_m2"] == 1.0
        # gaining / throughflow absent
        assert stats["gaining"]["volume_m3"] == 0.0
        assert stats["throughflow"]["volume_m3"] == 0.0

    def test_all_classes_present_in_stats(self):
        shape = (1, 1, 1)
        fr = cell_class_fractions(np.array([CLS["hyporheic"]]), np.array([0]), shape)
        stats = class_stats(fr, np.array([[[5.0]]]),
                            xe=np.array([0.0, 1.0]), ye=np.array([0.0, 1.0]),
                            T=np.array([[[1.0]]]), B=np.array([[[0.0]]]),
                            domain_volume_m3=5.0)
        assert set(stats.keys()) == set(HZ_CLASSES)
