"""The hyporheic hydraulic signature: one derivation, three dimensions, no score.

The load-bearing test in this file is `test_derive_matches_the_previous_inline_arithmetic`. Every
number here used to be computed in three separate places (assess.build_results, app._screening_now,
app._scenario_metrics) and they agreed only by coincidence. That test reimplements the old
expressions and asserts EXACT float equality against the single derivation, which is the revision
spec §27 guarantee that the move preserved the validated calculations.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest

from hype_app import dims, metrics as m, signature as sg

# One fixture, hand-checkable. Times in days, weights in m3/day, depths and lengths in metres.
T = np.array([0.02, 0.15, 0.4, 1.2, 3.0, 12.0])
W = np.array([500.0, 900.0, 1400.0, 700.0, 300.0, 120.0])
D = np.array([0.3, 0.55, 0.9, 1.4, 2.1, 3.3])
L = np.array([2.0, 5.0, 9.0, 14.0, 21.0, 33.0])

Q_STREAM = 1.42
REACH_M = 1830.0
A_BED = 6000.0
A_ACTIVE = 1920.0
A_CONN = 2680.0
VOL = 2460.0
POROSITY = 0.31


def _exchange():
    return m.ExchangeAccounting(total_downwelling=0.0500, returning_hyporheic=0.0455,
                                losing_to_sides=0.0031, unresolved=0.0014)


def _inputs(**over):
    kw = dict(streamflow_cms=Q_STREAM, reach_length_m=REACH_M, porosity=POROSITY,
              exchange=_exchange(), transit_times_days=T, transit_weights_m3_day=W,
              path_depths_m=D, path_lengths_m=L, bulk_volume_m3=VOL,
              streambed_area_m2=A_BED, active_streambed_area_m2=A_ACTIVE,
              connected_streambed_area_m2=A_CONN, censored_fraction=0.028)
    kw.update(over)
    return sg.SignatureInputs(**kw)


@pytest.fixture
def sig():
    return sg.derive(_inputs())


# =========================================================================== the spec-27 guarantee
def test_derive_matches_the_previous_inline_arithmetic(sig):
    """THE GUARANTEE. Revision spec §27: preserve the existing validated hydraulic calculations.

    These are the expressions that used to live at assess.py:268-269, app.py:3841-3859 and
    app.py:4200-4226, reimplemented here rather than referenced, so this test fails if the
    derivation is quietly redefined. EXACT equality, not approx: a relocation that changes a float
    in the last bit has changed a published number.

    Delete after one release; by then the old expressions are gone from history's reach anyway."""
    ex = _exchange()
    old_conn = m.connectivity(streamflow=Q_STREAM, returning_hyporheic=ex.returning_hyporheic,
                              total_downwelling=ex.total_downwelling, losing=ex.losing_to_sides,
                              unresolved=ex.unresolved, reach_length_m=REACH_M)
    old_flux = m.exchange_flux(ex.returning_hyporheic, A_BED)
    old_depths = m.path_depth_metrics(D, W)
    old_rtd = m.residence_time_metrics(T, W, porosity=POROSITY, censored_fraction=0.028)

    assert sig.extent["equivalent_active_depth_m"] == VOL / A_BED          # assess.py:268-269
    assert sig.extent["mobile_pore_storage_m3"] == VOL * POROSITY          # app.py:3858 / 4019
    assert sig.extent["path_depth_p50_m"] == old_depths["p50_m"]
    assert sig.extent["path_depth_p90_m"] == old_depths["p90_m"]
    assert sig.extent["path_depth_max_m"] == old_depths["max_m"]
    assert sig.frequency["exchange_flux_m_day"] == old_flux["m_per_day"]
    assert sig.frequency["exchange_flux_mm_day"] == old_flux["mm_per_day"]
    assert sig.frequency["turnovers_per_km"] == old_conn.turnovers_per_km
    assert sig.frequency["turnover_length_km"] == old_conn.turnover_length_km
    assert sig.frequency["excursions_per_mile"] == old_conn.excursions_per_mile
    assert sig.frequency["active_streambed_fraction"] == A_ACTIVE / A_BED  # app.py:3850
    assert sig.frequency["connected_streambed_fraction"] == A_CONN / A_BED
    for key, want in old_rtd.items():
        got = sig.duration[key]
        if isinstance(want, float) and math.isnan(want):
            assert math.isnan(got), key
        else:
            assert got == want, key


def test_the_contract_field_names_still_resolve(sig):
    """The three dicts are keyed by contract field names, which is why the mappers are one-liners.
    A typo would silently drop a metric from the report, so let pydantic be the check."""
    from hype_app.contracts import ConnectivityMetrics, ResidenceTimeMetrics, ZoneMetrics
    ConnectivityMetrics(**sg.connectivity_fields(sig))
    ResidenceTimeMetrics(**sg.residence_fields(sig))
    ZoneMetrics(**sg.zone_fields(sig))
    assert set(sig.frequency) <= set(ConnectivityMetrics.model_fields)
    assert set(sig.extent) <= set(ZoneMetrics.model_fields)


# =========================================================================== turnover, §5.4
def test_turnovers_per_km_equals_the_displayed_equation(sig):
    """The printed equation and the computed number cannot separate: parse the value back out of
    the string the report shows and check it against the derivation."""
    hand = (0.0455 / Q_STREAM) * (1000.0 / REACH_M)
    assert sig.frequency["turnovers_per_km"] == hand
    eq = sg.TURNOVER_DEFINITION.equation
    assert eq == "C_1km = (Q_HEF / Q_stream) x (1000 / L_reach)"
    # ...and evaluated from the equation's own shape, not from a second copy of the code
    q_hef, q_str, l_reach = 0.0455, Q_STREAM, REACH_M
    assert eval(eq.split("=", 1)[1].replace("x", "*")                     # noqa: S307 - test only
                .replace("Q_HEF", repr(q_hef)).replace("Q_stream", repr(q_str))
                .replace("L_reach", repr(l_reach))) == hand


def test_turnover_length_is_the_reciprocal(sig):
    c = sig.frequency
    assert c["turnover_length_km"] == pytest.approx(1.0 / c["turnovers_per_km"], rel=1e-12)


def test_the_turnover_definition_answers_every_required_question():
    """§5.4 lists what must be answerable before the interface may use the word "turnover" at all.
    One assertion per bullet, so a missing answer fails the build rather than shipping silently."""
    td = sg.TURNOVER_DEFINITION
    assert "streamflow-equivalent" in td.denominator.lower()
    assert "not one hyporheic-zone volume" in td.denominator
    assert "not one completed flow path" in td.denominator
    assert "spatial" in td.basis.lower() and "per day" in td.basis
    assert "1000 / L_reach" in td.reach_length
    assert "returned to the river" in td.inclusion
    assert "censored" in td.inclusion                       # what happens to the unresolved share
    assert "at most once" in td.repeat_rule
    assert "L_T" in td.reciprocal
    assert len(td.answers()) == 6
    assert td.sources


def test_the_turnover_citation_does_not_claim_an_unverified_source():
    """§27 forbids inventing the citation and §25 decision 3 records the Harvey source as open.
    The provenance field must say so plainly, and no user-facing string may attribute the equation."""
    from hype_app.functions.helptext import SOURCES
    harvey = SOURCES["harvey2019"]
    assert "NOT VERIFIED" in harvey.provenance
    for _where, text in sg._user_strings():
        assert "Harvey" not in text, text
    # The governing definition is the project framework's own, and says which code computes it.
    assert "metrics.Connectivity" in SOURCES["framework_signature"].provenance


def test_the_turnover_help_is_generated_from_the_definition():
    """A tooltip that restates a definition in its own words is one that will disagree with it.

    The card splits the equation across a key/value pair because the tip's key column is narrow,
    so assert the two halves REJOIN into the definition's string rather than that either half
    appears verbatim."""
    lhs, rhs = sg.TURNOVER_HELP.rows[0]
    assert f"{lhs} = {rhs}" == sg.TURNOVER_DEFINITION.equation
    assert sg.TURNOVER_HELP.sources == sg.TURNOVER_DEFINITION.sources
    # ...and no row duplicates a slot the card already renders under its own label
    assert sg.TURNOVER_HELP.rows_label.lower() not in ("definition", "method")


# =========================================================================== no score, §4.4 / §27
def test_the_three_dimensions_never_combine(sig):
    """§4.4: different units, no natural sum, opposite responses to conductivity. A fourth number
    combining them would conceal the physical reason two sites differ."""
    cards = sg.card_view(sig)
    assert len(cards) == 3
    assert [c["dim_id"] for c in cards] == list(dims.SIGNATURE_DIMS)
    assert len({c["primary_unit"] for c in cards}) == 3
    flat = sig.as_dict()
    banned = ("score", "index", "rating", "overall", "composite")
    assert not [k for k in flat if any(b in k.lower() for b in banned)]


def test_no_signature_string_ranks_the_site():
    """§8.6 and §18.5: no universal good/bad judgment, and no fixed low/medium/high thresholds
    until a defensible reference distribution exists. Enforced at import, checked here too."""
    sg.validate_signature()
    for where, text in sg._user_strings():
        low = f" {text.lower()} "
        for word in sg.BANNED_RANKING_WORDS:
            assert f" {word} " not in low, f"{where}: {word!r}"


def test_no_em_dash_in_any_user_facing_string():
    """Project rule, made structural rather than reviewed. Checked on the STRINGS rather than the
    source, because the source legitimately contains one: the guard in validate_signature."""
    for where, text in sg._user_strings():
        assert "—" not in text, where
    assert "—" not in open("hype_app/dims.py", encoding="utf-8").read()


def test_every_dimension_declares_what_it_controls_and_its_caution():
    for d in sg.DIMENSIONS:
        assert d.controls in ("delivery", "contact time", "participating capacity")
        assert d.caution and d.definition and d.relevance
        # Every caution names what its dimension does NOT establish. That negation is the whole
        # job of the field: §5.5, §6.6 and §7.6 each end on one.
        assert " not " in f" {d.caution.lower()} ", d.id


def test_the_help_cards_pass_the_shared_lint():
    """Same 25-word slot and 70-word card budget the function registry uses. Not a parallel lint:
    a second copy would drift, so this imports the one in helptext."""
    from hype_app.functions.helptext import MAX_CARD_WORDS, validate_help
    for d in sg.DIMENSIONS:
        validate_help(d.help, f"signature.{d.id}")
        assert d.help.word_count() <= MAX_CARD_WORDS


# =========================================================================== regime, §8
def test_regime_description_is_derived_from_the_run_thresholds():
    """The contact sentence reads the run's OWN exceedance fractions, so perturbing one changes
    the sentence. Nothing here is a cut point imported from elsewhere."""
    from hype_app.contracts import AssessmentResultsV2, ConnectivityMetrics, ThresholdResult
    res = AssessmentResultsV2(
        assessment_id="A1", input_hash="h",
        connectivity=ConnectivityMetrics(turnovers_per_km=0.16, turnover_length_km=6.25,
                                         active_streambed_fraction=0.32),
        thresholds=[ThresholdResult(threshold_value_h=1.0, flow_exceedance_fraction=0.87),
                    ThresholdResult(threshold_value_h=6.0, flow_exceedance_fraction=0.54),
                    ThresholdResult(threshold_value_h=24.0, flow_exceedance_fraction=0.11)])
    r = sg.regime_description(res)
    # The LARGEST threshold that still holds half the flow, not the first or the last.
    assert "6 hours" in r.contact_statement and "54" in r.contact_statement
    res.thresholds[1].flow_exceedance_fraction = 0.22
    r2 = sg.regime_description(res)
    # 1 h still holds 87 percent, so the sentence steps down rather than giving up.
    assert "1 hour" in r2.contact_statement and "87" in r2.contact_statement
    for t in res.thresholds:                                   # now nothing reaches half
        t.flow_exceedance_fraction = 0.31
    r3 = sg.regime_description(res)
    assert "Under half" in r3.contact_statement and "1 hour" in r3.contact_statement
    assert "31" in r3.contact_statement


def test_regime_description_states_its_comparison_population():
    """§18.5: the population a comparison is made against must always be stated. There isn't one
    yet, so the report has to say that rather than imply a ranking."""
    from hype_app.contracts import AssessmentResultsV2
    r = sg.regime_description(AssessmentResultsV2(assessment_id="A1", input_hash="h"))
    assert "no reference distribution" in r.basis.lower()
    assert "do not rank it" in r.basis


def test_regime_description_defines_no_cut_points():
    """The 2x2 labels §8.1-8.4 suggests ("High delivery with limited contact time") need a cut
    point on turnovers, which §18.5 forbids inventing. They are deliberately not shipped."""
    src = open("hype_app/signature.py", encoding="utf-8").read()
    body = src[src.index("def regime_description"):src.index("def _dur(")]
    for banned in ("High delivery", "Limited delivery", "Localized", "Broad,"):
        assert banned not in body, banned


def test_regime_survives_a_results_model_with_nothing_in_it():
    """The pane calls this before a report has ever been built."""
    from hype_app.contracts import AssessmentResultsV2
    r = sg.regime_description(AssessmentResultsV2(assessment_id="A1", input_hash="h"))
    assert all(isinstance(s, str) and s for s in
               (r.delivery_statement, r.contact_statement, r.extent_statement, r.basis))


# =========================================================================== degraded paths
def test_no_exchange_degrades_rather_than_raising():
    """An absent key and a None value mean the same thing to every consumer: the mappers hand the
    dicts to pydantic models whose fields all default to None, and the card/screening readers use
    .get(). Nothing here may raise, and the other two dimensions must survive intact."""
    sig = sg.derive(_inputs(exchange=None))
    assert sig.frequency.get("turnovers_per_km") is None
    assert sig.frequency["unavailable_reason"]
    assert sig.extent["equivalent_active_depth_m"] == VOL / A_BED      # extent is independent
    assert sig.duration["weighted_median_days"] is not None            # so is duration
    assert len(sg.card_view(sig)) == 3


def test_no_rtd_leaves_duration_empty_but_keeps_the_rest():
    sig = sg.derive(_inputs(transit_times_days=None, transit_weights_m3_day=None))
    assert not sig.have_rtd
    assert sig.duration == {"porosity": POROSITY}
    assert sig.frequency["turnovers_per_km"] is not None
    assert all(t["flow_exceedance_fraction"] is None for t in sig.thresholds)


def test_missing_streambed_area_withholds_the_normalized_metrics():
    sig = sg.derive(_inputs(streambed_area_m2=None))
    assert sig.extent["equivalent_active_depth_m"] is None
    assert sig.frequency["exchange_flux_m_day"] is None
    assert sig.extent["bulk_saturated_volume_m3"] == VOL               # the absolute survives


def test_cards_paint_n_a_rather_than_crashing_on_an_empty_run():
    cards = sg.card_view(sg.derive(sg.SignatureInputs()))
    assert len(cards) == 3
    assert all(c["primary_value"] == "n/a" for c in cards)


# =========================================================================== the hz bundle adapter
def test_from_hz_bundle_prefers_the_hyporheic_runs_porosity():
    """THE BUG THIS FIXES. The report read `snap.k.porosity`, frozen at the groundwater run, while
    the screening pane read the knobs the hyporheic run tracked at. Editing porosity between the
    two made them print different pore volumes. MODPATH tracked at the hyporheic value, so that is
    the one consistent with the volume it produced."""
    stats = {"classes": {"hyporheic": {"volume_m3": VOL}},
             "flux": {"accounting": {"streambed_area_m2": A_BED}},
             "knobs": {"porosity": 0.31}, "counts": {"n_seeds": 994}}
    si = sg.SignatureInputs.from_hz_bundle(stats, {}, snapshot_porosity=0.25)
    assert si.porosity == 0.31
    assert si.provenance.porosity_basis == "hyporheic run"
    assert sg.derive(si).extent["mobile_pore_storage_m3"] == VOL * 0.31
    # ...and the snapshot is the fallback for a run whose knobs predate the field
    stats["knobs"] = {}
    si2 = sg.SignatureInputs.from_hz_bundle(stats, {}, snapshot_porosity=0.25)
    assert si2.porosity == 0.25 and si2.provenance.porosity_basis == "input snapshot"
    si3 = sg.SignatureInputs.from_hz_bundle(stats, {})
    assert si3.porosity == sg.FALLBACK_POROSITY and si3.provenance.porosity_basis == "fallback"


def test_the_porosity_disagreement_raises_a_warning():
    """Picking one value silently is what let the two disagree for as long as they did."""
    from hype_app.assess import build_results
    from hype_app.contracts import (AssessmentInputSnapshot, GradientBoundaryConfigV2,
                                    GridSettings, KSettings, StreamflowInput)
    from hype_app.provenance import Provenance
    snap = AssessmentInputSnapshot(
        assessment_id="A1",
        streamflow=StreamflowInput(value_cms=Q_STREAM, provenance=Provenance(source="USGS")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.25),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5))
    res = build_results(snap, hz_stats={"hyporheic": {"volume_m3": VOL}}, streamflow_cms=Q_STREAM,
                        reach_length_m=REACH_M, exchange=_exchange(), streambed_area_m2=A_BED,
                        porosity=0.31, snapshot_porosity=0.25)
    codes = {w.code for w in res.warnings}
    assert "porosity_freeze_point" in codes
    msg = next(w.message for w in res.warnings if w.code == "porosity_freeze_point")
    assert "0.31" in msg and "0.25" in msg
    # and no warning when they agree
    ok = build_results(snap, hz_stats={"hyporheic": {"volume_m3": VOL}}, streamflow_cms=Q_STREAM,
                       reach_length_m=REACH_M, exchange=_exchange(), streambed_area_m2=A_BED,
                       porosity=0.25, snapshot_porosity=0.25)
    assert "porosity_freeze_point" not in {w.code for w in ok.warnings}


def test_screening_fields_cover_what_screeninginputs_declares(sig):
    """A hydraulic field added to the signature must reach every screening section without an
    edit in app.py, which only works while this mapping stays total."""
    from dataclasses import fields
    from hype_app.functions import ScreeningInputs
    declared = {f.name for f in fields(ScreeningInputs)}
    got = sg.screening_fields(sig)
    assert set(got) <= declared, set(got) - declared
    ScreeningInputs(**got)                                    # constructs without a TypeError
    for key in ("turnovers_per_km", "equivalent_active_depth_m", "exchange_flux_m_day",
                "mobile_pore_storage_m3", "connected_streambed_fraction"):
        assert got[key] is not None, key


def test_scenario_metrics_carry_the_four_the_sweep_aggregates(sig):
    got = sg.scenario_metrics(sig)
    for key in ("turnovers_per_km", "rtd_median_days", "equivalent_active_depth_m", "volume_m3"):
        assert got[key] is not None, key


# ======================================================================  the copy lint
class TestUserFacingCopy:
    """The sweep `validate_signature` runs at import, checked from both ends.

    THE CONCEPTUAL FIGURE'S COPY IS NO LONGER IN SCOPE HERE. It used to be, while the figure was
    drawn in matplotlib and its strings lived in this module. The figure is now a committed SVG,
    and `tests/test_concept.py` runs the same sweep over the text it draws."""

    def test_no_user_facing_string_uses_a_semicolon(self):
        """Standing project rule, enforced rather than remembered. A semicolon is where two
        sentences get welded into the long ones this copy keeps shedding."""
        for where, text in sg._user_strings():
            assert ";" not in text, f"{where}: {text}"

    def test_no_user_facing_string_uses_an_em_dash(self):
        """The other standing rule. Same sweep, and the same reason to check it from outside."""
        for where, text in sg._user_strings():
            assert "\u2014" not in text, f"{where}: {text}"

    def test_the_validator_actually_rejects_one(self):
        """Otherwise the two sweeps above pass for the wrong reason the moment `_user_strings`
        stops reaching something. Retargeted off the figure copy onto `THRESHOLD_NOTE`, which is
        still swept."""
        original = sg.THRESHOLD_NOTE
        sg.THRESHOLD_NOTE = "One clause; another clause."
        try:
            with pytest.raises(ValueError, match="semicolon"):
                sg.validate_signature()
        finally:
            sg.THRESHOLD_NOTE = original
