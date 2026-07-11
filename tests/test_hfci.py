"""HFCI scoring tests (spec §9, §13.1, §14.13, §14.14)."""
import pytest

from hype_app.hfci import (
    compute_hfci,
    evaluate_curve,
    load_profile,
    residence_opportunity,
    round_half_up,
)


def test_profile_loads_and_labels():
    p = load_profile()
    assert p.profile_id == "hfci-v1"
    assert p.validation_label == "Literature-derived HFCI v1 - validation ongoing"
    assert {c.name for c in p.classes} == {"Low", "Moderate", "High"}


def test_round_half_up():
    assert round_half_up(2.5) == 3
    assert round_half_up(2.4) == 2
    assert round_half_up(10.0) == 10


def test_evaluate_curve_clamp_and_extrapolation():
    curve = load_profile().exchange     # knots [0,1,5,20]->[0,5,10,15], supported [0,20]
    score, extra = evaluate_curve(curve, 1.0)
    assert score == pytest.approx(5.0) and extra is False
    score, extra = evaluate_curve(curve, 25.0)
    assert score == pytest.approx(15.0) and extra is True     # clamped + flagged


def test_residence_opportunity_curve():
    hour, day = 1 / 24, 1.0
    assert residence_opportunity([hour])[0] == pytest.approx(0.0)
    assert residence_opportunity([day])[0] == pytest.approx(1.0)
    assert residence_opportunity([0.001])[0] == 0.0           # below 1h
    mid = residence_opportunity([0.2])[0]                     # ~4.8h, between
    assert 0.0 < mid < 1.0


class TestComputeHFCI:
    def test_whole_scores_classes_and_mean(self):
        """§14.13/§14.14: whole 0–15 scores with classes; HFCI = mean(component/15)."""
        r = compute_hfci(exchange_raw=5.0, storage_raw=0.1, processing_raw=0.5)
        assert r.exchange.score == 10 and r.exchange.class_name == "Moderate"
        assert r.storage.score == 10 and r.processing.score == 10
        assert r.hfci == pytest.approx(0.67)          # (10/15+10/15+10/15)/3 = 0.6667 -> 0.67
        assert r.hfci_class == "Moderate"
        assert r.validation_label.startswith("Literature-derived HFCI v1")

    def test_high_and_low_bands(self):
        high = compute_hfci(exchange_raw=20.0, storage_raw=0.5, processing_raw=1.0)
        assert high.exchange.score == 15 and high.hfci == 1.0 and high.hfci_class == "High"
        low = compute_hfci(exchange_raw=0.0, storage_raw=0.0, processing_raw=0.0)
        assert low.exchange.score == 0 and low.hfci == 0.0 and low.hfci_class == "Low"

    def test_not_computable_when_component_missing(self):
        r = compute_hfci(exchange_raw=5.0, storage_raw=0.1, processing_raw=None)
        assert r.hfci is None
        assert "Processing" in r.not_computable_reason
        assert r.exchange.score == 10          # available components still shown
