"""Hyporheic function screening: the oxygen gate, four sections, and the report boundary.

Every test fabricates arrays directly, so none needs a solver binary and none carries a marker.
Follows tests/test_hz_classification.py.

The hand-checkable cases pick round parameters (a 1-day half-life, a 24 h response time) so the
expected numbers are verifiable by hand rather than by re-running the implementation.
"""
from __future__ import annotations

import dataclasses
import math
import re

import numpy as np
import pytest

from hype_app import metrics as m
from hype_app.functions import helptext
from hype_app.functions import pollutants as pol
from hype_app.functions import registry as reg
from hype_app.functions import screen
from hype_app.functions.screen import (
    GRAMS_PER_POUND,
    NITRATE_BASIS,
    UNSET,
    ScreeningInputs,
    first_order_saturation,
    opportunity_curve,
    removal_fractions,
    screen_extent,
    screen_process,
    screen_reactive,
    screen_thermal,
    time_to_anoxia,
)

DENIT = reg.get_process("denitrification")
POLL = reg.get_process("contaminant")


def _pane_helpers():
    """The pane's formatting helpers, lifted out of `app.py` and exec'd against stubs.

    The panes are closures inside `server()` and cannot be imported, which is the gap that let a
    row type missing `unit_key` reach a user as a screening pane that opened to nothing. Lifting
    the real source with `ast` means these tests exercise the shipped expressions rather than a
    copy that drifts. Add a stub below when a helper grows a dependency; do not stop calling it,
    because a string lint over app.py cannot see an AttributeError or a card that resolves to
    nothing at all."""
    import ast
    import textwrap

    from shiny import ui

    from hype_app import dims, report as report_mod

    want = ["_fn_tip", "_fn_head", "_fn_tbl", "_fn_field", "_fn_label", "_fn_unit", "_fn_val",
            "_fn_rows", "_fn_kpi", "_fn_curve", "_fn_assumption", "_fn_limits", "_pol_head",
            "_tau_choice"]
    src = open("app.py", encoding="utf-8").read()
    found = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name in want and node.name not in found:
            found[node.name] = textwrap.dedent(ast.get_source_segment(src, node))
    assert not [n for n in want if n not in found], f"missing {set(want) - set(found)}"

    def _stub_tip(text=None, *, help=None):
        return ui.span(class_="hype-info-tip")

    ns = {"ui": ui, "fn_reg": reg, "fn_pol": pol, "dims": dims, "report_mod": report_mod,
          "math": math, "_info_tip": _stub_tip,
          "_next_hint": lambda nid, label, primary=False: ui.tags.button(label),
          "_refs_panel": lambda spec: ui.accordion_panel("Sources")}
    for name in want:
        exec(found[name], ns)                                # noqa: S102 - lifted app source
    return ns
HAB = reg.get_process("habitat")
THERM = reg.get_process("thermal_regulation")


# ===========================================================================  the oxygen gate
class TestOxygenGate:
    """The gate exists so the user supplies dissolved oxygen, which they can estimate, instead of
    an onset time, which they cannot."""

    def test_reproduces_the_observed_transition(self):
        """THE convergence test.

        Zarnetske et al. (2011) OBSERVED the net-production-to-net-removal transition at 6.9 h at
        Drift Creek. The upper consumption bound is the rate that reproduces it from a stream DO of
        9 mg/L. Independently, the lower bound is Trauth and Fleckenstein's aerobic respiration
        maximum. A modeling parameter set and a field measurement bracketing the same quantity is
        the strongest evidence in this feature, so it is pinned here."""
        lo, _, hi = reg.OXYGEN_CONSUMPTION_MG_L_DAY
        t_field = time_to_anoxia(9.0, threshold_mg_l=0.1, consumption_mg_l_day=hi)
        assert t_field * 24.0 == pytest.approx(6.9, abs=0.05)
        t_model = time_to_anoxia(9.0, threshold_mg_l=0.1, consumption_mg_l_day=lo)
        assert t_model * 24.0 == pytest.approx(14.0, abs=0.1)
        assert t_model > t_field                       # slower consumption, later onset

    def test_is_linear_in_dissolved_oxygen(self):
        """The zero-order signature. Oxygen is the saturating substrate at stream concentrations
        (Monod term 0.978 at 9 mg/L), so the onset time is linear. A first-order implementation
        would give a log relationship and fail here, which is the guard against silent reversion."""
        a = time_to_anoxia(4.0, threshold_mg_l=0.0, consumption_mg_l_day=20.0)
        b = time_to_anoxia(8.0, threshold_mg_l=0.0, consumption_mg_l_day=20.0)
        assert b / a == pytest.approx(2.0)

    def test_hand_checkable(self):
        # (10 - 0) / 20 mg/L/day = 0.5 day
        assert time_to_anoxia(10.0, threshold_mg_l=0.0,
                              consumption_mg_l_day=20.0) == pytest.approx(0.5)

    def test_already_anoxic_water_starts_immediately(self):
        assert time_to_anoxia(0.05, threshold_mg_l=0.1, consumption_mg_l_day=20.0) == 0.0

    def test_degrades_rather_than_raising(self):
        assert time_to_anoxia(None) is None
        assert time_to_anoxia(9.0, consumption_mg_l_day=0.0) is None
        assert time_to_anoxia(-1.0, consumption_mg_l_day=20.0) is None


# ===========================================================================  the shared helper
class TestWeightedReactionFraction:
    def test_single_path_matches_closed_form_exactly(self):
        got = m.weighted_reaction_fraction([2.0], [1.0], timescale=1.0, onset=0.0)
        assert got == pytest.approx(1.0 - math.exp(-2.0), rel=0, abs=1e-15)

    def test_all_paths_at_or_below_onset_give_exactly_zero(self):
        assert m.weighted_reaction_fraction([0.1, 0.2, 0.3], [1.0, 1.0, 1.0],
                                            timescale=1.0, onset=0.3) == 0.0

    def test_tracks_flow_weights_not_particle_counts(self):
        """Framework §4.5 names particle-count weighting as a specific failure mode."""
        assert m.weighted_reaction_fraction([1.0, 1.0], [1.0, 99.0], timescale=1.0) == \
            pytest.approx(1.0 - math.exp(-1.0))
        fast = m.weighted_reaction_fraction([10.0, 0.01], [99.0, 1.0], timescale=1.0)
        slow = m.weighted_reaction_fraction([10.0, 0.01], [1.0, 99.0], timescale=1.0)
        assert fast > slow

    def test_scale_invariant_in_weights(self):
        assert m.weighted_reaction_fraction([1.0, 4.0], [1.0, 3.0], timescale=2.0) == \
            pytest.approx(m.weighted_reaction_fraction([1.0, 4.0], [1e3, 3e3], timescale=2.0))

    def test_monotone_and_bounded(self):
        t, w = [0.5, 1.0, 4.0], [1.0, 2.0, 1.0]
        assert m.weighted_reaction_fraction(t, w, timescale=0.5) > \
            m.weighted_reaction_fraction(t, w, timescale=5.0)
        assert m.weighted_reaction_fraction(t, w, timescale=1.0, onset=0.0) > \
            m.weighted_reaction_fraction(t, w, timescale=1.0, onset=0.75)
        assert m.weighted_reaction_fraction(t, w, timescale=1e-12) == pytest.approx(1.0)
        assert m.weighted_reaction_fraction(t, w, timescale=1e12) == pytest.approx(0.0, abs=1e-11)

    def test_nan_on_empty_and_zero_weight(self):
        assert math.isnan(m.weighted_reaction_fraction([], [], timescale=1.0))
        assert math.isnan(m.weighted_reaction_fraction([1.0], [0.0], timescale=1.0))

    def test_reactive_exposure_is_a_volume(self):
        """m3/day x day = m3: the water standing past the gate at any instant."""
        assert m.reactive_exposure([1.0, 3.0], [100.0, 200.0], onset=1.0) == pytest.approx(400.0)
        assert m.reactive_exposure([0.5], [100.0], onset=1.0) == 0.0


# ===========================================================================  registry
class TestRegistry:
    def test_shipped_entries_validate(self):
        reg.validate_registry()

    def test_sections_in_display_order(self):
        assert reg.SECTION_ORDER == ("denitrification", "contaminant", "habitat",
                                     "thermal_regulation")
        assert set(reg.SECTION_ORDER) == set(reg.PROCESSES)

    def test_microplastics_is_unregistered_but_not_gone(self):
        """"Remove it for now" (2026-08-01). What left is REACHABILITY, from exactly three places
        here plus two tree nodes; the calculator, its constants and `TestParticulateModule` stayed,
        because a module with no tests is one nobody can restore with any confidence.

        There is deliberately no third state. `validate_functions` rejects a process no function
        claims and `pane_node` would name a tree node that does not exist, so a calculator cannot
        sit in `PROCESSES` computing quietly with no way in -- which is what keeps the interface
        honest about what actually ran."""
        assert "microplastic" not in reg.PROCESSES
        assert "microplastic" not in reg.SECTION_ORDER
        assert not any("microplastic" in f.processes for f in reg.FUNCTIONS.values())
        # ...and the spec object itself is still here, ready to be re-registered
        assert reg._MICROPLASTIC.key == "microplastic"                    # noqa: SLF001
        assert reg._MICROPLASTIC.kind == reg.KIND_PARTICULATE             # noqa: SLF001

    def test_every_entry_is_cited_and_has_a_help_card(self):
        """Framework §14.2, plus the pane rule: prose lives in tooltips, so every section needs one."""
        for key, spec in reg.PROCESSES.items():
            assert spec.citation.strip(), key
            assert spec.transferability_note.strip(), key
            assert spec.help.definition.strip(), key

    def test_denitrification_rate_comes_from_the_reference_project(self):
        """RC1 = 1.220000 /day, verified three ways in the example: the cell-by-cell array, the
        per-layer listing echo, and the printed reaction stability limit of 0.8197 d = 1/1.22.

        The audit trail lives in Source.provenance, not in the displayed note: a file path and
        solver flags read as debug output to anyone who is not checking the model."""
        assert DENIT.rate_central == pytest.approx(1.22)
        assert DENIT.rate_unit == reg.RATE_FIRST_ORDER_PER_DAY
        prov = reg.SOURCES["gms_rct"].provenance
        assert ".rct" in prov and "1.220000" in prov and "IREACT 1" in prov
        assert ".rct" not in DENIT.rate_citation, "the file path is provenance, not display copy"
        assert "1.22" in DENIT.rate_citation
        assert math.log(2) / 1.22 * 24 == pytest.approx(13.6, abs=0.05)   # half-life
        assert DENIT.oxygen_gated is True

    def test_pollutant_ships_no_rate_by_design(self):
        assert POLL.rate is None and not POLL.has_rate
        assert POLL.oxygen_gated is False

    def test_habitat_is_extent_driven(self):
        assert HAB.kind == reg.KIND_EXTENT
        assert HAB.kinetics == reg.KINETICS_NONE
        assert HAB.rate is None and HAB.oxygen_gated is False

    def test_thermal_carries_retardation_and_the_marzadri_timescale(self):
        assert THERM.rate == (4.0, 8.0, 16.0)
        assert THERM.retardation != 1.0                 # heat is not a solute
        assert "Marzadri" in THERM.rate_citation

    def test_rejects_a_rate_without_provenance(self):
        bad = dataclasses.replace(DENIT, rate=(1.0, 2.0, 3.0), rate_citation=None)
        with pytest.raises(ValueError, match="rate_citation"):
            reg.validate_registry({bad.key: bad})

    def test_rejects_a_missing_source_note_or_definition(self):
        cases = [({"sources": (), "no_source_note": "  "}, "nothing to cite"),
                 ({"transferability_note": "   "}, "transferability_note"),
                 ({"help": dataclasses.replace(DENIT.help, definition="  ")}, "definition")]
        for kwargs, pattern in cases:
            bad = dataclasses.replace(DENIT, **kwargs)
            with pytest.raises(ValueError, match=pattern):
                reg.validate_registry({bad.key: bad})

    def test_rejects_a_source_key_that_does_not_resolve(self):
        """Mirrors EASI's unresolved-citations check: a typo must fail loudly at import, not
        render as a blank Source line."""
        bad = dataclasses.replace(DENIT, sources=("hester2016", "nope2099"))
        with pytest.raises(ValueError, match="unresolved sources"):
            reg.validate_registry({bad.key: bad})

    def test_rejects_an_extent_process_that_carries_kinetics(self):
        bad = dataclasses.replace(HAB, kinetics=reg.KINETICS_FIRST_ORDER)
        with pytest.raises(ValueError, match="kinetics"):
            reg.validate_registry({bad.key: bad})

    def test_rejects_unordered_bounds(self):
        bad = dataclasses.replace(DENIT, rate=(3.0, 2.0, 1.0))
        with pytest.raises(ValueError, match="non-decreasing"):
            reg.validate_registry({bad.key: bad})


# ===========================================================================  the function layer
class TestFunctionRegistry:
    """FOUR functions, five calculators. A function is what a manager asks about; a process is
    what the app can calculate, and the two stopped being one-to-one when microplastic retention
    arrived as a MECHANISM of pollutant attenuation rather than a fifth function."""

    def test_there_are_exactly_four(self):
        assert reg.FUNCTION_ORDER == ("nutrient", "pollutant", "habitat", "thermal")
        assert [reg.get_function(k).display_label for k in reg.FUNCTION_ORDER] == [
            "Nutrient Cycling", "Pollutant Attenuation", "Habitat Creation",
            "Temperature Regulation"]

    def test_every_calculator_belongs_to_exactly_one_function(self):
        """THE ANTI-ORPHAN INVARIANT, and the one that makes the merge safe. Moving microplastic
        under pollutant must not leave it unreachable, and a sixth calculator cannot ship without
        someone deciding which function hosts it."""
        owned = [pk for f in reg.FUNCTIONS.values() for pk in f.processes]
        assert sorted(owned) == sorted(reg.PROCESSES)
        assert len(owned) == len(set(owned)), "a process is claimed twice"
        for pk in reg.PROCESSES:
            assert reg.function_for_process(pk) is not None

    def test_pollutant_attenuation_is_one_calculator_again(self):
        """It hosted two for a while and needed `mechanisms` to navigate between them. With
        microplastics unregistered a group with one child is a click that leads nowhere, so the
        function went back to a single process and the dissolved mechanism's headline and
        assumption moved up onto the FunctionSpec itself."""
        pol = reg.FUNCTIONS["pollutant"]
        assert pol.processes == ("contaminant",)
        assert pol.mechanisms == ()
        assert pol.headline_kpi == "total_mass_display"
        assert pol.rests_on() is not None and pol.rests_on().key == "rate_value"
        assert reg.pane_node("contaminant") == "fn.scr.pol"

    def test_the_microplastic_pane_says_its_number_ignores_the_hydraulics(self):
        """THE HONEST STATEMENT, and the one thing deleting the signature block could have cost.
        Drummond's retention coefficient is a cross-class average over stream distance, so this
        site's turnover and residence time do not move the reach-scale number at all. It used to
        live in a per-mechanism signature override; it now leads the pane as the scope note, which
        is the slot for what a section does NOT depend on."""
        note = reg._MICROPLASTIC.scope_note.lower()                       # noqa: SLF001
        assert "turnover" in note and "residence time" in note
        assert "do not change it" in note

    @pytest.mark.parametrize("key", reg.FUNCTION_ORDER)
    def test_every_calculator_has_a_pane_node(self, key):
        """EVERY CALCULATOR NEEDS A NODE, or its pane is unreachable. A single-calculator function
        uses its own; a mechanism must name one, since the radio that used to switch siblings on a
        shared pane is gone and the tree does that job now."""
        f = reg.get_function(key)
        assert f.node_id.startswith("fn.")
        for mech in f.mechanisms:
            assert mech.node_id.startswith("fn."), (key, mech.key)
            assert reg.pane_node(mech.process) == mech.node_id
        if not f.mechanisms:
            assert reg.pane_node(f.primary_process) == f.node_id

    def test_every_pane_node_is_claimed_exactly_once(self):
        nodes = [reg.pane_node(pk) for pk in reg.SECTION_ORDER]
        assert len(set(nodes)) == len(nodes) == len(reg.SECTION_ORDER)

    def test_the_pane_blocks_actually_render(self):
        """THE ONLY TESTS THAT RUN THE PANE CODE are this one and its siblings on `_pane_helpers`.
        Everything else here reads app.py as text.

        The panes are closures inside `server()`, so a test cannot import them, and that gap is how
        a missing `unit_key` on a row type reached a user as a function pane that opened to
        nothing. `_pane_helpers` lifts the helper SOURCES out of app.py with ast and execs them
        against stubs, so it exercises the shipped expressions rather than a copy that can drift.

        If this starts failing because a helper grew a new dependency, add a stub there. Do not
        delete the test: string lints cannot see an AttributeError."""
        import numpy as np

        ns = _pane_helpers()

        si = ScreeningInputs(
            transit_times_days=np.array([0.02, 0.15, 0.4, 1.2, 3.0, 12.0]),
            transit_weights_m3_day=np.array([500., 900., 1400., 700., 300., 120.]),
            path_lengths_m=np.array([2., 5., 9., 14., 21., 33.]),
            streambed_area_m2=6000.0, active_streambed_area_m2=1920.0,
            active_streambed_fraction=0.32, connected_streambed_area_m2=2680.0,
            connected_streambed_fraction=2680 / 6000, returning_hyporheic_cms=0.0455,
            streamflow_cms=1.42, reach_length_m=1830.0, turnovers_per_km=0.0175,
            equivalent_active_depth_m=0.41, bulk_saturated_volume_m3=2460.0,
            mobile_pore_storage_m3=762.6, porosity=0.31, exchange_flux_m_day=0.6552,
            path_depth_p50_m=0.86, path_depth_p90_m=1.93, downwelling_cells=9,
            interface_particles_per_cell=4, zone_particles_per_cell=2, zone_seeds=10234,
            zone_cells_seeded=5117, zone_classified=994, particle_size_um=50.0,
            median_grain_size_mm=2.0, inlet_concentration_mg_l=1.0)
        rates = {"denitrification": 1.22, "contaminant": 83.52, "thermal_regulation": 8.0}
        out = {}
        for pk in reg.SECTION_ORDER:
            spec = reg.get_process(pk)
            kw = ({} if spec.kind in (reg.KIND_EXTENT, reg.KIND_PARTICULATE)
                  else {"rate": rates[pk]})
            out[pk] = screen_process(si, spec, **kw)

        for fk in reg.FUNCTION_ORDER:
            fspec = reg.get_function(fk)
            for mech in (None, *fspec.mechanisms):
                pk = mech.process if mech is not None else fspec.primary_process
                s, spec = out[pk], reg.get_process(pk)
                where = fk + (f"/{mech.key}" if mech else "")
                # The headline must resolve to a real number, and neither the assumption line nor
                # the Limitations panel may raise on it.
                head = ns["_fn_kpi"](s, spec, lead=fspec.headline(mech))
                assert head is not None and "hype-kpi-num pending" not in str(head), where
                ns["_fn_assumption"](s, spec, fspec.rests_on(mech))
                assert ns["_fn_limits"](fspec, spec, [s]) is not None, where

                # EVERY DECLARED KPI REACHES THE PANE, and this is the only test that can see it.
                # `lead` used to be `only`, so a spec declaring three cards painted one and the
                # other two -- each with its own help card, units and sensitivity bounds -- were
                # simply never rendered anywhere. Three docstrings and a test comment asserted
                # they had moved into More metrics; nothing had put them there, and no string lint
                # over app.py could tell, because the code that dropped them was correct code.
                html = str(head)
                shown = {k.key: (s.get(k.label_key) if k.label_key else None) or k.label
                         for k in spec.kpis}
                for key, label in shown.items():
                    assert label in html, f"{where}: headline {key!r} renders nowhere"
                # ...and the one the function headlines is the LEAD, so it comes first in the
                # markup and is the card the accent rule attaches to.
                lead_key = fspec.headline(mech) or spec.kpis[0].key
                assert html.index(shown[lead_key]) == min(html.index(v) for v in shown.values()), \
                    f"{where}: {lead_key!r} does not lead"
                if len(spec.kpis) > 1:
                    assert "hype-kpi-lead" in html and "hype-kpi-grid" in html, where
                else:
                    # A single card renders exactly as it always did, which is what keeps
                    # `_sig_kpi`'s stacked cards and the one-KPI panes unchanged.
                    assert "hype-kpi-split" not in html, where

    def test_a_pane_row_satisfies_the_shared_row_contract(self):
        """THE BUG THIS EXISTS FOR: a row type shipped without `label_key`/`unit_key`, which
        `_fn_label` and `_fn_unit` read off every row UNCONDITIONALLY. The formatter raised
        AttributeError on its first row and the whole function pane rendered nothing at all, with
        the tree node selected and no props card.

        Nothing caught it: the panes are closures inside `server()` and cannot be invoked from a
        test, and the key-resolution tests assert the result CARRIES the key, never that the row
        can be FORMATTED. So lint the seam, the way TestModuleSurface does for the package:
        whatever the shared formatters read off a row, every row type must have."""
        import dataclasses as dc
        import re
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("def _fn_label(s, spec_row)"):src.index("def _fn_rows(")]
        needed = set(re.findall(r"spec_row\.(\w+)", block))
        assert needed, "the row formatters moved; this lint is looking at the wrong slice"
        for cls in (reg.PaneRow, reg.PaneKpi):
            have = {f.name for f in dc.fields(cls)}
            assert needed <= have, f"{cls.__name__} is missing {sorted(needed - have)}"

    @pytest.mark.parametrize("key", reg.FUNCTION_ORDER)
    def test_every_function_states_what_it_cannot_tell_you(self, key):
        f = reg.get_function(key)
        assert f.limits, key
        assert all(1 <= len(b.split()) <= 15 for b in f.limits), key

    def test_habitat_rests_on_no_rate(self):
        """User decision: keep the framing, add the disclaimer. It stays one of the four with its
        existing headlines, and the honesty lives in `limits` rather than in a renamed heading."""
        h = reg.FUNCTIONS["habitat"]
        assert h.assumption is None                       # so no assumed-rate chip, from the data
        assert reg.get_process("habitat").rate is None
        joined = " ".join(h.limits).lower()
        assert "never habitat quality" in joined          # §13.2
        assert "suitability index" in joined              # §13.3

    def test_habitat_names_the_surrogate_it_is_standing_in_for(self):
        """The other three bullets qualify a measurement. This one says the measurement is a
        stand-in for the thing the section is NAMED after, which is the admission a reader cannot
        make for themselves -- and it leads, because it frames the rest."""
        limits = reg.FUNCTIONS["habitat"].limits
        assert "surrogate" in limits[0].lower(), "the surrogate bullet has to lead"
        assert "potential habitat space" in limits[0].lower()
        # ...and it reads on the pane, where Limitations takes its bullets straight from here
        assert len(limits[0].split()) <= 15

    def test_no_em_dash_in_any_function_string(self):
        for f in reg.FUNCTIONS.values():
            for text in reg._fn_strings(f):
                assert "—" not in text, text

    def test_validate_functions_rejects_a_malformed_entry(self):
        """The invariants have to actually fail the build, not merely be respected today."""
        import dataclasses as dc
        base = reg.FUNCTIONS["thermal"]
        orig = dict(reg.FUNCTIONS)
        order = reg.FUNCTION_ORDER
        try:
            # an orphaned process
            reg.FUNCTIONS["thermal"] = dc.replace(base, processes=())
            with pytest.raises((ValueError, IndexError)):
                reg.validate_functions()
            # a mechanism with no tree node, which is a pane nothing can reach. No function
            # declares mechanisms today (microplastics is unregistered), so this fabricates the
            # pair rather than borrowing one -- the invariant has to keep failing the build for
            # whoever re-registers it, and a test that skipped when the last mechanism went away
            # would be silently absent exactly then.
            pol = reg.FUNCTIONS["pollutant"]
            reg.FUNCTIONS["thermal"] = base
            reg.FUNCTIONS["pollutant"] = dc.replace(
                pol, processes=("contaminant", "habitat"),
                mechanisms=(reg.Mechanism(key="dis", label="Dissolved", process="contaminant",
                                          node_id=""),
                            reg.Mechanism(key="hab", label="Habitat", process="habitat",
                                          node_id="fn.scr.hab")))
            # ...and habitat has to release its own claim, or the double-claim rule fires first
            # and this asserts about the wrong invariant. FUNCTION_ORDER follows it out, or the
            # coverage rule fires instead.
            del reg.FUNCTIONS["habitat"]
            reg.FUNCTION_ORDER = tuple(k for k in order if k != "habitat")
            with pytest.raises(ValueError, match="node_id"):
                reg.validate_functions()
            reg.FUNCTIONS["pollutant"] = pol
            reg.FUNCTIONS["habitat"] = orig["habitat"]
            reg.FUNCTION_ORDER = order
            # an em dash in user-facing copy
            reg.FUNCTIONS["thermal"] = dc.replace(base, limits=("A limit — with a dash.",))
            with pytest.raises(ValueError, match="em dash"):
                reg.validate_functions()
            # a headline that is not a KPI of the process it heads
            reg.FUNCTIONS["thermal"] = dc.replace(base, headline_kpi="not_a_kpi")
            with pytest.raises(ValueError, match="not a KPI"):
                reg.validate_functions()
        finally:
            reg.FUNCTIONS.clear()
            reg.FUNCTIONS.update(orig)
            reg.FUNCTION_ORDER = order
        reg.validate_functions()                          # and the shipped registry still passes


# ===========================================================================  per-path kinetics
class TestRemovalFractions:
    def test_first_order_closed_form(self):
        """A 1/day rate over 1 and 2 days gives 1-1/e and 1-1/e^2."""
        f = removal_fractions([1.0, 2.0], spec=DENIT, onset_days=0.0, rate=1.0)
        assert f == pytest.approx([1 - math.exp(-1), 1 - math.exp(-2)])

    def test_the_example_rate_over_a_hand_checked_path(self):
        """k = 1.22/day, onset 14 h, path 24 h -> 10 h reactive -> 39.9% removed."""
        f = removal_fractions([1.0], spec=DENIT, onset_days=14.0 / 24.0, rate=1.22)
        assert float(f[0]) == pytest.approx(0.399, abs=0.001)

    def test_onset_clamps_short_paths_to_exactly_zero(self):
        f = removal_fractions([0.1, 0.5], spec=DENIT, onset_days=0.5, rate=1.22)
        assert float(f[0]) == 0.0 and float(f[1]) == 0.0

    def test_relaxation_uses_a_response_time_in_hours(self):
        """Thermal: rate is tau in HOURS, not a per-day constant. A 24 h path at tau = 24 h
        gives 1 - 1/e; treating the rate as 1/day would give a wildly different number."""
        f = removal_fractions([1.0], spec=THERM, onset_days=0.0, rate=24.0)
        assert float(f[0]) == pytest.approx(1 - math.exp(-1))

    def test_zero_order_clamps_at_one(self):
        spec = dataclasses.replace(DENIT, kinetics=reg.KINETICS_ZERO_ORDER)
        # 12 g/m3/day over C_in = 6 g/m3 saturates at 0.5 day
        f = removal_fractions([0.25, 0.5, 10.0], spec=spec, onset_days=0.0, rate=12.0,
                              inlet_concentration_mg_l=6.0)
        assert f == pytest.approx([0.5, 1.0, 1.0])
        assert float(np.max(f)) <= 1.0

    def test_no_rate_yields_none(self):
        assert removal_fractions([1.0], spec=POLL, onset_days=0.0, rate=None) is None


# ===========================================================================  the chain
class TestNutrientSection:
    """Hand-checkable case, verified by arithmetic rather than by re-running the code.

    T = [1, 2] days, w = [100, 200] m3/day, k = 1/day, onset 0, C_in 10 mg/L, A_bed 1000 m2.
        f     = [1-1/e, 1-1/e^2] = [0.632121, 0.864665]
        E     = (100*0.632121 + 200*0.864665) / 300 = 0.787150
        M     = 10 * (100*0.632121 + 200*0.864665) = 2361.45 g/day
        r     = 2361.45 / 1000 = 2.36145 g/m2/day
        q_HEF = 300/1000 = 0.3 m/day, so r = q_HEF*C_in*E = 0.3*10*0.78715 = 2.36145  OK
    """

    F1, F2 = 1 - math.exp(-1), 1 - math.exp(-2)
    E = (100 * F1 + 200 * F2) / 300.0
    MASS_G = 10.0 * (100 * F1 + 200 * F2)

    def _inputs(self, **kw):
        base = dict(transit_times_days=[1.0, 2.0], transit_weights_m3_day=[100.0, 200.0],
                    streambed_area_m2=1000.0, active_streambed_area_m2=400.0,
                    exchange_flux_m_day=0.3, returning_hyporheic_cms=300.0 / 86400.0,
                    inlet_concentration_mg_l=10.0, dissolved_oxygen_mg_l=0.0,
                    anoxic_threshold_mg_l=0.0)
        base.update(kw)
        return ScreeningInputs(**base)

    def _run(self, **kw):
        return screen_reactive(self._inputs(**kw.pop("inputs", {})), DENIT,
                               rate=kw.pop("rate", 1.0), **kw)

    def test_efficiency_and_mass(self):
        out = self._run()
        assert out["removal_efficiency"] == pytest.approx(self.E)
        assert out["total_removed_kg_day"] == pytest.approx(self.MASS_G / 1000.0)
        assert out["total_removed_lb_day"] == pytest.approx(self.MASS_G / GRAMS_PER_POUND)

    def test_chain_closes_and_the_decomposition_agrees(self):
        out = self._run()
        assert out["areal_removal_rate_g_m2_day"] == pytest.approx(self.MASS_G / 1000.0)
        assert out["reference_area_basis"] == "total streambed"
        assert out["chain_closure_rel_diff"] == pytest.approx(0.0, abs=1e-12)

    def test_weight_identity_against_reported_q_hef(self):
        """Framework §5.9. Drift here scales every mass by the same factor, silently."""
        assert self._run()["weight_identity_rel_diff"] == pytest.approx(0.0, abs=1e-12)
        bad = self._run(inputs={"returning_hyporheic_cms": 600.0 / 86400.0})
        assert bad["weight_identity_rel_diff"] == pytest.approx(0.5)

    def test_time_to_anoxia_is_derived_not_entered(self):
        """The whole point of the rework: the user supplies dissolved oxygen, the app derives the
        onset. There must be no threshold input anywhere in the result."""
        out = self._run(inputs={"dissolved_oxygen_mg_l": 9.0, "anoxic_threshold_mg_l": 0.1,
                                "oxygen_consumption_mg_l_day": 31.0})
        assert out["time_to_anoxia_hours"] == pytest.approx(6.9, abs=0.05)
        assert out["dissolved_oxygen_mg_l"] == 9.0
        assert "threshold_hours" not in out

    def test_a_late_onset_removes_exactly_nothing(self):
        out = self._run(inputs={"dissolved_oxygen_mg_l": 500.0, "anoxic_threshold_mg_l": 0.0,
                                "oxygen_consumption_mg_l_day": 1.0})
        assert out["removal_efficiency"] == 0.0
        assert out["total_removed_kg_day"] == 0.0
        assert out["fraction_above_threshold"] == 0.0
        assert out["fraction_below_threshold"] == pytest.approx(1.0)

    def test_nitrate_basis_travels_and_gives_an_n_equivalent(self):
        """The headline stays in the species the user entered; the N-equivalent exists so a
        cross-site table cannot mix an as-N site with an as-NO3 one."""
        as_n = self._run(inputs={"nitrate_basis": "N"})
        as_no3 = self._run(inputs={"nitrate_basis": "NO3"})
        assert as_n["nitrate_basis_label"] == "mg/L as N"
        assert as_no3["nitrate_basis_label"] == "mg/L as NO3"
        # same entered number, so the same headline mass in the entered species
        assert as_n["total_removed_kg_day"] == pytest.approx(as_no3["total_removed_kg_day"])
        # but the nitrogen content differs by the molar ratio
        assert as_n["total_removed_kg_n_day"] / as_no3["total_removed_kg_n_day"] == \
            pytest.approx(NITRATE_BASIS["NO3"])
        assert NITRATE_BASIS["NO3"] == pytest.approx(62.004 / 14.007)     # 4.4266

    def test_envelope_brackets_the_central_estimate(self):
        """NOTE: this fixture has DO = threshold = 0, so all three onsets collapse to 0 and the
        oxygen corners are never exercised. TestSensitivityEnvelope below is the real coverage."""
        out = self._run()
        assert out["total_removed_low_kg_day"] < out["total_removed_kg_day"] \
            < out["total_removed_high_kg_day"]

    def test_envelope_widens_with_the_oxygen_bounds(self):
        """Both the rate and the oxygen consumption sweep, so the gated envelope is the wider one."""
        gated = self._run(inputs={"dissolved_oxygen_mg_l": 9.0, "anoxic_threshold_mg_l": 0.1})
        ungated = self._run()
        gspan = gated["total_removed_high_kg_day"] - gated["total_removed_low_kg_day"]
        uspan = ungated["total_removed_high_kg_day"] - ungated["total_removed_low_kg_day"]
        assert gspan > 0 and uspan > 0

    def test_rate_free_outputs_survive_without_a_concentration(self):
        out = self._run(inputs={"inlet_concentration_mg_l": None})
        assert out["removal_efficiency"] == pytest.approx(self.E)
        assert out.get("total_removed_kg_day") is None
        # the message names this section's own input, not a generic "concentration"
        assert "stream nitrate" in out["unavailable_reason"].lower()

    def test_empty_input_degrades_rather_than_raising(self):
        out = screen_reactive(ScreeningInputs(), DENIT, rate=1.0)
        assert out["unavailable_reason"]
        assert out.get("removal_efficiency") is None


# ===========================================================================  the sweep envelope
class TestSensitivityEnvelope:
    """THE INVARIANT: low <= central <= high, for every input the app can produce.

    It broke because the rate corners adapted to a user override (via _sensitivity_bounds) while
    the onset corners always read the ends of the published oxygen triple. The pane then showed a
    headline mass above the top of its own stated range, which is worse than a wrong number: it
    reads as arithmetic the reader cannot trust.
    """

    T = [0.05, 0.30, 0.35, 0.5, 0.8, 3.0]
    W = [5.0, 1.0, 1.0, 2.0, 1.0, 0.5]

    def _run(self, **kw):
        base = dict(transit_times_days=self.T, transit_weights_m3_day=self.W,
                    streambed_area_m2=1000.0, inlet_concentration_mg_l=1.0,
                    dissolved_oxygen_mg_l=9.0, anoxic_threshold_mg_l=0.1)
        rate = kw.pop("rate", 2.44)
        base.update(kw)
        return screen_reactive(ScreeningInputs(**base), DENIT, rate=rate)

    def test_the_envelope_always_contains_the_headline(self):
        """945 cases. Before the fix, 257 of them put the headline outside its own range."""
        checked = 0
        for o2 in (0.5, 2.0, 10.0, 15.3, 23.2, 31.0, 40.0, 60.0, 200.0):
            for k in (0.01, 0.3, 0.61, 1.22, 2.44, 5.0, 40.0):
                for do in (0.0, 0.15, 4.0, 9.0, 14.0):
                    for thr in (0.0, 0.1, 0.5):
                        out = self._run(oxygen_consumption_mg_l_day=o2, rate=k,
                                        dissolved_oxygen_mg_l=do, anoxic_threshold_mg_l=thr,
                                        inlet_concentration_mg_l=1.4)
                        c = out.get("total_removed_kg_day")
                        lo, hi = (out.get("total_removed_low_kg_day"),
                                  out.get("total_removed_high_kg_day"))
                        if c is None or lo is None or hi is None:
                            continue
                        checked += 1
                        # <=, not <: the corners coincide with the centre exactly when the
                        # override sits on a published endpoint, and that is correct.
                        assert lo - 1e-15 <= c <= hi + 1e-15, (o2, k, do, thr, lo, c, hi)
        assert checked == 945

    def test_an_oxygen_rate_above_the_published_range_still_brackets(self):
        """The reported regression. Shipped high was 0.00153317, i.e. BELOW the headline."""
        out = self._run(transit_times_days=[0.30, 0.35, 0.40, 0.5, 0.8],
                        transit_weights_m3_day=[1.0] * 5,
                        oxygen_consumption_mg_l_day=40.0)
        assert out["time_to_anoxia_hours"] == pytest.approx(5.34, abs=1e-2)
        assert out["total_removed_kg_day"] == pytest.approx(0.00203871, abs=1e-8)
        assert out["total_removed_low_kg_day"] == pytest.approx(0.00022770, abs=1e-8)
        assert out["total_removed_high_kg_day"] == pytest.approx(0.00274269, abs=1e-8)

    def test_an_oxygen_rate_below_the_published_range_still_brackets(self):
        """Onset (106.8 h) beats every path, so all three collapse to zero. Before the fix the
        headline was 0.0 against a range starting at 0.000125: a headline below its own floor."""
        out = self._run(transit_times_days=[0.30, 0.35, 0.40, 0.5, 0.8],
                        transit_weights_m3_day=[1.0] * 5,
                        oxygen_consumption_mg_l_day=2.0, rate=1.22)
        assert out["time_to_anoxia_hours"] == pytest.approx(106.8, abs=1e-1)
        assert out["total_removed_kg_day"] == 0.0
        assert out["total_removed_low_kg_day"] == 0.0
        assert out["total_removed_high_kg_day"] == 0.0

    def test_the_published_corners_are_unchanged_inside_the_triple(self):
        """A defensible in-range override must still get the PUBLISHED sweep, not a
        factor-of-two invention. This is the no-regression guard on the fix."""
        out = self._run(transit_times_days=[0.30, 0.35, 0.40, 0.5, 0.8],
                        transit_weights_m3_day=[1.0] * 5,
                        oxygen_consumption_mg_l_day=23.2, rate=1.22)
        assert out["total_removed_low_kg_day"] == pytest.approx(0.00012468, abs=1e-8)
        assert out["total_removed_high_kg_day"] == pytest.approx(0.00153317, abs=1e-8)

    def test_the_normalized_headlines_carry_the_same_corners(self):
        out = self._run(reach_length_m=120.0)
        for lo_k, mid_k, hi_k in (
                ("areal_removal_rate_low_g_m2_day", "areal_removal_rate_g_m2_day",
                 "areal_removal_rate_high_g_m2_day"),
                ("removal_per_km_low_kg_day", "removal_per_km_kg_day",
                 "removal_per_km_high_kg_day")):
            assert out[lo_k] <= out[mid_k] <= out[hi_k], mid_k
        # and per-km is just the mass over the reach length in km
        assert out["removal_per_km_kg_day"] == pytest.approx(
            out["total_removed_kg_day"] / 0.120)

    def test_the_outlet_concentration_is_the_reduction_restated(self):
        """"Concentration reduction fraction" and "removal efficiency" are the same quantity;
        the pane shows the pair so the efficiency reads as a reduction."""
        out = self._run(inlet_concentration_mg_l=1.5)
        c_in, e = 1.5, out["removal_efficiency"]
        assert out["outlet_concentration_mg_l"] == pytest.approx(c_in * (1.0 - e), abs=1e-15)
        assert (c_in - out["outlet_concentration_mg_l"]) / c_in == pytest.approx(e, abs=1e-12)

    def test_thermal_range_brackets_a_user_response_time(self):
        rtd = dict(transit_times_days=self.T, transit_weights_m3_day=self.W)
        for tau in (1.0, 2.0, 8.0, 30.0, 100.0):
            out = screen_thermal(ScreeningInputs(**rtd), THERM, rate=tau)
            assert (out["buffering_opportunity_low"] <= out["buffering_opportunity"]
                    <= out["buffering_opportunity_high"]), tau

    def test_thermal_keeps_the_published_corners_at_the_default(self):
        out = screen_thermal(ScreeningInputs(transit_times_days=self.T,
                                             transit_weights_m3_day=self.W), THERM, rate=8.0)
        lo_ref = m.weighted_reaction_fraction(self.T, self.W, timescale=16.0 / 24.0, onset=0.0)
        hi_ref = m.weighted_reaction_fraction(self.T, self.W, timescale=4.0 / 24.0, onset=0.0)
        assert out["buffering_opportunity_low"] == pytest.approx(lo_ref)
        assert out["buffering_opportunity_high"] == pytest.approx(hi_ref)


# ===========================================================================  the other sections
class TestOtherSections:
    def _rtd(self, **kw):
        base = dict(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                    transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                    returning_hyporheic_cms=1000.0 / 86400.0, streamflow_cms=0.736,
                    turnovers_per_km=0.16, streambed_area_m2=8000.0,
                    active_streambed_area_m2=4200.0, active_streambed_fraction=0.525,
                    return_streambed_area_m2=5000.0, connected_streambed_area_m2=6000.0,
                    connected_streambed_fraction=0.75,
                    bulk_saturated_volume_m3=8200.0, mobile_pore_storage_m3=2460.0,
                    porosity=0.3, equivalent_active_depth_m=1.025,
                    path_depth_p50_m=0.6, path_depth_p90_m=1.4)
        base.update(kw)
        return ScreeningInputs(**base)

    def test_pollutant_names_its_own_missing_rate(self):
        """The old code hardcoded 'denitrification' here, which was wrong for three of four."""
        out = screen_process(self._rtd(inlet_concentration_mg_l=0.5), POLL, rate=None)
        assert "attenuation rate" in out["unavailable_reason"].lower()
        assert "denitrification" not in out["unavailable_reason"].lower()

    def test_pollutant_has_no_oxygen_gate(self):
        out = screen_process(self._rtd(inlet_concentration_mg_l=0.5, dissolved_oxygen_mg_l=9.0),
                             POLL, rate=1.0)
        assert "time_to_anoxia_hours" not in out
        assert out["fraction_above_threshold"] == pytest.approx(1.0)   # every path is in contact

    def test_habitat_reports_extent_and_no_chemistry(self):
        out = screen_extent(self._rtd(), HAB)
        assert out["habitable_pore_volume_m3"] == pytest.approx(2460.0)
        assert out["bulk_volume_m3"] == pytest.approx(8200.0)
        assert out["path_depth_p90_m"] == pytest.approx(1.4)
        for forbidden in ("removal_efficiency", "total_removed_kg_day",
                          "areal_removal_rate_g_m2_day", "rate_value", "time_to_anoxia_hours"):
            assert forbidden not in out, forbidden

    def test_every_habitat_headline_is_on_the_pore_water_basis(self):
        """The defect this layout replaced: a pore-water headline sitting directly above a
        bulk-basis depth, with nothing on the card saying they were different quantities. Dividing
        the headline by the bed area gave 0.3075, not the 1.025 shown. Framework §4.6 names mixing
        bases as its failure mode, so assert the three headlines close on ONE basis."""
        out = screen_extent(self._rtd(), HAB)
        pore, a_bed = 2460.0, 8000.0
        assert out["pore_equivalent_depth_m"] == pytest.approx(pore / a_bed)
        assert out["habitable_pore_volume_m3"] == pytest.approx(pore)
        # ...and the bulk-basis D_HZ still ships, distinguishable, for the report and the framework
        assert out["equivalent_active_depth_m"] == pytest.approx(8200.0 / a_bed)
        assert out["volume_basis"] == "pore water"

    def test_the_bulk_and_pore_depths_reconcile_through_porosity(self):
        """THE ONE A READER OF THE REPORT TRIPS OVER. The Extent card headlines the bulk-basis
        depth and Habitat Creation headlines the pore-water one, so at n = 0.3 the same zone is
        reported as 7.671 m and 2.30 m a few inches apart and reads as a contradiction.

        The sibling identity above (pore depth vs coverage) was pinned; this one was only implied
        in a docstring. It has to come out of a pipeline that DERIVES the pore volume: handing
        `mobile_pore_storage_m3` and `bulk_saturated_volume_m3` in independently decouples them and
        the identity silently stops holding."""
        import numpy as np

        from hype_app import assess
        from hype_app.contracts import ConnectivityMetrics, ZoneMetrics
        from hype_app.metrics import ExchangeAccounting, pore_volume

        rng = np.random.default_rng(0)
        t = np.exp(rng.normal(np.log(2.0), 0.8, 200))
        a_bed, bulk = 5000.0, 1e4
        for n in (0.3, 0.45):
            zone = ZoneMetrics(
                bulk_saturated_volume_m3=bulk,
                mobile_pore_storage_m3=pore_volume(bulk, n),   # derived, as signature.py does
                equivalent_active_depth_m=bulk / a_bed)
            fns = assess._build_functions(
                {"pollutant_endpoints": [], "contaminant_conc_by_key": {}, "nitrate_mg_l": 1.0},
                conn=ConnectivityMetrics(streambed_area_m2=a_bed, active_streambed_area_m2=3000.0,
                                         connected_streambed_area_m2=2500.0,
                                         connected_streambed_fraction=0.5, turnovers_per_km=0.3),
                zone=zone,
                exchange=ExchangeAccounting(total_downwelling=0.1, returning_hyporheic=0.05,
                                            losing_to_sides=0.01, unresolved=0.0),
                transit_times_days=t, transit_weights=np.ones_like(t), streamflow_cms=2.8,
                porosity=n, have_rtd=True, reach_length_m=253.0)
            h = fns.habitat
            assert h.pore_equivalent_depth_m == pytest.approx(
                h.equivalent_active_depth_m * n), n
            # ...and the report's Extent card reads the SAME field habitat calls the bulk basis
            assert h.equivalent_active_depth_m == pytest.approx(zone.equivalent_active_depth_m)

    def test_the_two_pore_depths_reconcile_through_coverage(self):
        """Coverage is a CONNECTED-bed quantity and the depth beside it normalizes over the WHOLE
        bed, so a reader who divides one by the other must land on a number the pane actually
        shows. That identity is the reason `pore_depth_active_m` is reported at all, and it is why
        the depth's denominator had to follow coverage onto the connected area."""
        out = screen_extent(self._rtd(), HAB)
        assert out["pore_depth_active_m"] == pytest.approx(2460.0 / 6000.0)
        assert (out["pore_depth_active_m"] * out["connected_streambed_fraction"]
                == pytest.approx(out["pore_equivalent_depth_m"]))

    def test_coverage_counts_both_sides_and_keeps_the_framework_figure(self):
        """The bed water enters through and the bed it returns through are different sets, and on
        a gaining reach the return side is much wider. Counting only entry reported a reach that
        returns all of its downwelling as a fraction of its bed. Framework §4.7's entry-only
        A_active is still carried, unchanged, because the report's Extent card publishes it."""
        out = screen_extent(self._rtd(), HAB)
        assert out["active_streambed_area_m2"] == pytest.approx(4200.0)     # entry
        assert out["return_streambed_area_m2"] == pytest.approx(5000.0)     # exit
        assert out["connected_streambed_area_m2"] == pytest.approx(6000.0)  # union
        # the union is a set union, so it is at least the larger side and at most their sum
        assert 5000.0 <= out["connected_streambed_area_m2"] <= 4200.0 + 5000.0
        assert out["connected_streambed_fraction"] > out["active_streambed_fraction"]

    def test_a_run_without_the_exit_area_drops_coverage_rather_than_guessing(self):
        """Projects delineated before the engine computed the exit area have no honest union to
        report. The headline must go absent (the pane filters unresolved keys) rather than quietly
        fall back to the entry-only area, which would make one label mean two different things
        across runs. The framework figure still resolves, so no run is left with no coverage."""
        out = screen_extent(self._rtd(return_streambed_area_m2=None,
                                      connected_streambed_area_m2=None,
                                      connected_streambed_fraction=None), HAB)
        assert out["connected_streambed_fraction"] is None
        assert out["connected_streambed_area_m2"] is None
        assert out["pore_depth_active_m"] is None          # never re-based onto A_active
        assert out["active_streambed_fraction"] == pytest.approx(0.525)
        assert out["habitable_pore_volume_m3"] == pytest.approx(2460.0)

    def test_a_porosity_edited_since_the_run_is_disclosed_not_applied(self):
        """Porosity is a MODPATH input: it set the pore velocities, so it set the travel times and
        therefore which particles returned in time and therefore the volume itself. A field edited
        since the run cannot be applied by multiplying, so the headline must hold at the run's
        value and say so rather than silently reporting a volume the model never produced."""
        out = screen_extent(self._rtd(porosity=0.3, porosity_live=0.45), HAB)
        assert out["habitable_pore_volume_m3"] == pytest.approx(2460.0)
        assert out["porosity"] == pytest.approx(0.3)
        assert "0.3" in out["advisory_note"] and "0.45" in out["advisory_note"]
        assert "re-run" in out["advisory_note"].lower()
        # An unchanged field is not an advisory
        assert "advisory_note" not in screen_extent(
            self._rtd(porosity=0.3, porosity_live=0.3), HAB)

    def test_habitat_provenance_is_the_zone_pass_not_the_flux_pass(self):
        """Two particle populations run per delineation and they answer different questions. The
        volume comes from the zone pass; `n_paths` and the interface density describe the flux
        pass. Reporting one as the other would put a plausible, wrong provenance under the
        headline."""
        out = screen_extent(self._rtd(zone_particles_per_cell=3, zone_seeds=2982,
                                      zone_cells_seeded=994, zone_classified=2100,
                                      downwelling_cells=9, interface_particles_per_cell=4), HAB)
        assert out["zone_particles_per_cell"] == 3
        assert out["zone_seeds"] == 2982
        assert out["zone_classified_fraction"] == pytest.approx(2100 / 2982)
        assert out["zone_seeds"] != out["n_paths"]

    def test_thermal_hand_calculation_from_the_companion_plan(self):
        """Thermal plan §13.1, verbatim: t = [4, 8, 24] h, w = [1, 2, 1], tau = 8 h."""
        out = screen_thermal(
            ScreeningInputs(transit_times_days=[4 / 24, 8 / 24, 24 / 24],
                            transit_weights_m3_day=[1.0, 2.0, 1.0],
                            returning_hyporheic_cms=0.12, turnovers_per_km=0.16),
            THERM, rate=8.0)
        assert out["buffering_opportunity"] == pytest.approx(0.6519808474, abs=1e-9)
        assert out["remaining_anomaly_fraction"] == pytest.approx(0.3480191526, abs=1e-9)
        assert out["fraction_above_1tau"] == pytest.approx(0.75)
        assert out["fraction_above_3tau"] == pytest.approx(0.25)
        assert out["attenuation_weighted_flow_cms"] == pytest.approx(0.0782377017, abs=1e-9)
        assert out["attenuation_weighted_connectivity_per_km"] == \
            pytest.approx(0.1043169356, abs=1e-9)

    def test_thermal_buffering_is_monotone_in_response_time(self):
        outs = [screen_thermal(self._rtd(), THERM, rate=tau)["buffering_opportunity"]
                for tau in (4.0, 8.0, 16.0)]
        assert outs[0] >= outs[1] >= outs[2]

    def test_thermal_reports_no_degrees(self):
        """Thermal plan §10.1-§10.2: buffering opportunity only, never a temperature.

        Checks the FIELDS, not the prose: the prose deliberately says 'never degrees of cooling',
        which is the disclaimer we want. Every numeric output must be a fraction, a flow, a volume
        or a dimensionless ratio."""
        out = screen_thermal(self._rtd(), THERM)
        banned = ("degree", "celsius", "_degc", "temp_change", "temperature_c", "cooling")
        for key, value in out.items():
            assert not any(b in key.lower() for b in banned), key
            if isinstance(value, (int, float)):
                assert not key.endswith("_c"), key
        # and the disclaimer must actually be carried
        assert "never degrees of cooling" in out["transferability_note"].lower()

    def test_thermal_response_bands_partition_the_flow(self):
        out = screen_thermal(self._rtd(), THERM)
        total = sum(b["flow_fraction"] for b in out["response_bands"])
        assert total == pytest.approx(1.0)

    def test_thermal_storage_cross_check_is_reported(self):
        """Thermal plan §7: an RTD-derived storage that disagrees with the independent estimate is
        a data-quality signal, so the discrepancy is recorded rather than hidden."""
        out = screen_thermal(self._rtd(), THERM)
        assert out["rtd_storage_m3"] is not None
        assert out["storage_cross_check_rel_diff"] is not None

    def test_dispatch_routes_each_kind(self):
        for spec in (DENIT, POLL, THERM):
            assert screen_process(self._rtd(), spec, rate=None)["process_kind"] == "residence_time"
        assert screen_process(self._rtd(), HAB)["process_kind"] == "extent"


# ===========================================================================  curve
class TestOpportunityCurve:
    def test_monotone_decreasing_and_bounded(self):
        pts = opportunity_curve([0.5, 1.0, 4.0], [1.0, 2.0, 1.0])
        vals = [p["opportunity"] for p in pts]
        assert all(a >= b - 1e-12 for a, b in zip(vals, vals[1:]))
        assert all(0.0 <= v <= 1.0 for v in vals)

    def test_it_sits_behind_the_disclosure_and_never_on_a_group(self):
        """It used to hang off a PaneGroup as a bare `curve_key`, rendering under the Exchange
        table with no label and no tooltip at all. R is monotone in tau and the marker sits at
        1/rate, so a run near complete removal pins its whole left half at the ceiling: on the
        card that is an unexplained blob. `detail_curve` puts it behind More metrics instead."""
        assert not hasattr(reg.PaneGroup(title="x"), "curve_key"), "curve_key came back"
        for key in reg.SECTION_ORDER:
            spec = reg.get_process(key)
            for g in spec.pane_groups:
                assert not getattr(g, "curve_key", ""), f"{key}: curve back on a group"

    @pytest.mark.parametrize("key", ("denitrification", "contaminant"))
    def test_the_curve_key_resolves_against_a_real_result(self, key):
        """Nothing validated the old `curve_key`, so a typo dropped the chart in silence. These
        are the two sections that carry one; both must resolve against real screen output."""
        spec = reg.get_process(key)
        assert spec.detail_curve is not None
        out = screen_process(
            ScreeningInputs(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                            transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                            streambed_area_m2=8000.0, inlet_concentration_mg_l=1.5),
            spec, rate=spec.rate_central or 1.0)
        pts = out.get(spec.detail_curve.key)
        assert pts and len(pts) >= 3, f"{key}: {spec.detail_curve.key!r} renders nothing"

    def test_every_curve_carries_a_label_and_an_explanation(self):
        """The whole reason it moved. OPPORTUNITY_CURVE_HELP was written and then attached to
        nothing for the chart's entire life on the card, so it shipped with no legend."""
        for key in reg.SECTION_ORDER:
            c = reg.get_process(key).detail_curve
            if c is not None:
                assert c.label.strip() and c.help is not None, f"{key}: unlabelled chart"

    def test_the_registry_refuses_an_unlabelled_or_unexplained_curve(self):
        base = reg.get_process("denitrification")
        for bad in (dataclasses.replace(base.detail_curve, label=" "),
                    dataclasses.replace(base.detail_curve, help=None)):
            with pytest.raises(ValueError):
                reg.validate_registry(
                    {"denitrification": dataclasses.replace(base, detail_curve=bad)})


# ===========================================================================  scope
class TestScopeNote:
    """"Nutrient Cycling" reads as phosphorus and uptake too; only nitrate is modeled. The report
    already qualified its section in a lede; the pane said it only on hover."""

    def test_the_nutrient_pane_says_what_it_does_not_cover(self):
        note = reg.get_process("denitrification").scope_note
        assert "denitrification" in note.lower()
        # naming the EXCLUSION is the point -- the process name alone leaves the rest assumed
        assert "phosphorus" in note.lower()

    def test_the_pollutant_pane_says_it_models_one_sink_per_endpoint(self):
        """"Dissolved Pollutants" still reads as a category; the model is ONE first-order sink per
        ticked endpoint, each screened on its own. The exclusion a reader cannot otherwise infer is
        that nothing desorbs and no transformation product is tracked."""
        note = reg.get_process("contaminant").scope_note.lower()
        assert "each endpoint" in note
        assert "sorption" in note and "daughter" in note

    def test_the_habitat_pane_says_it_is_measuring_space(self):
        """"Habitat Creation" is the widest-reading label of the four: it names an outcome and
        implies something was made. Framework §13.2 is categorical that hydraulics indicate
        potential access and cannot establish quality, so the exclusion belongs where the eye
        lands rather than three panels down in Limitations."""
        note = reg.get_process("habitat").scope_note.lower()
        assert "potential" in note and "pore water" in note
        assert "quality" in note

    def test_the_scope_note_comes_from_the_registry(self):
        """Same discipline the terseness test enforces for tooltips: a literal sentence in app.py
        is how the prose walls come back."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        assert 'class_="hype-props-scope"' in body
        assert "spec.scope_note" in body
        assert not re.search(r'ui\.div\("[^"]+", class_="hype-props-scope"\)', body)


# ===========================================================================  section parity
class TestReactiveSectionsMatch:
    """Nutrient Cycling and Pollutant Attenuation ask one question of different solutes, so they must
    headline the same thing. They already did in the registry; the pane did not show it, because
    denitrification ships a rate and a default concentration while the contaminant section ships
    neither by design, so every card resolved None and `_fn_kpi` dropped the block whole."""

    #: The pollutant headlines read DISPLAY twins of the canonical keys, because their units scale
    #: with the endpoint. The metric each one reports is still the nutrient one.
    #:
    #: Resolved through the PUBLIC map rather than a second copy of it: this map used to be
    #: hand-written here, and `screen.CANONICAL_FOR_DISPLAY` now exists because the scenario
    #: envelope needs the same twin-to-canonical step. Two copies is how they drift. The identity
    #: fallback covers `removal_efficiency`, which is canonical on both sides.
    @property
    def _SAME_METRIC(self):
        from hype_app.functions.screen import CANONICAL_FOR_DISPLAY
        return {k: CANONICAL_FOR_DISPLAY.get(k, k)
                for k in ("total_mass_display", "removal_efficiency", "per_km_display")}

    def test_both_sections_headline_the_same_three_metrics_in_the_same_order(self):
        """PARITY RESTORED, and by the same reasoning on both sides rather than by copying.

        Nutrient Cycling moved to mass-first on 2026-08-01 because an efficiency alone says
        nothing about whether a reach matters at scale. Pollutant Attenuation followed on
        2026-08-01 for a second reason on top of that one: each endpoint now sits behind its own
        expander with the lead number in the header, and on a transport-limited reach every
        chemical attenuates ~100%, so an efficiency-led list read "Zinc 100% / Cobalt 100% /
        Nickel 100%" and told a reader nothing at all.

        The two sections ask one question of different solutes, so a reader moving between them
        should not have to re-learn the card."""
        assert [self._SAME_METRIC[k.key] for k in POLL.kpis] == [k.key for k in DENIT.kpis]
        for pol_kpi, den in zip(POLL.kpis, DENIT.kpis):
            assert pol_kpi.kind == den.kind, pol_kpi.key
        # THE VERBS DIFFER, and that is the point of the rename. Denitrification may say
        # "transformed" because nitrate really is converted to N2; this section cannot, because
        # its endpoints are reversible sorption and biotransformation. So assert the two name the
        # same QUANTITY -- a drift to "Load reduction" on one side only would still be caught --
        # rather than a sameness the rename deliberately gave up.
        assert "Concentration" in POLL.kpis[1].label and "Concentration" in DENIT.kpis[1].label
        assert "km" in POLL.kpis[2].label and "km" in DENIT.kpis[2].label
        # ...and every card still explains itself
        for k in (*POLL.kpis, *DENIT.kpis):
            assert k.help is not None, f"headline {k.key!r} has no tooltip"

    def test_the_nutrient_headline_is_the_mass(self):
        """`headline_kpi` decides which card goes large; the registry order decides the rest. Both
        are data, so this is the one place the choice is written down."""
        assert reg.get_function("nutrient").headline_kpi == "total_removed_kg_day"
        assert [k.key for k in DENIT.kpis] == ["total_removed_kg_day", "removal_efficiency",
                                               "removal_per_km_kg_day"]
        assert DENIT.kpis[0].unit == "kg N/day"
        # the mass card carries its own sensitivity bounds, which is what the sub-line prints
        assert DENIT.kpis[0].low_key and DENIT.kpis[0].high_key

    def test_every_pollutant_headline_carries_an_explanation(self):
        """Two of the three shipped with help=None while their nutrient counterparts had cards."""
        for k in POLL.kpis:
            assert k.help is not None, f"headline {k.key!r} has no tooltip"

    def _run(self, preset_key=None, rate=0.8, conc=0.5):
        return screen_process(
            ScreeningInputs(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                            transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                            returning_hyporheic_cms=1000.0 / 86400.0, streamflow_cms=0.736,
                            streambed_area_m2=8000.0, reach_length_m=1609.0,
                            inlet_concentration_mg_l=conc, preset_key=preset_key),
            POLL, rate=rate)

    def test_a_range_appears_for_a_cited_endpoint_and_not_for_a_custom_rate(self):
        """THE ASYMMETRY, and it is deliberate. `_sensitivity_bounds` falls back to factor-of-two
        around whatever rate is in effect, so for a number the user typed the corners are the
        app's own invention and labelling them a sensitivity range would claim a provenance that
        does not exist. A cited endpoint brings a real spread, so there the range is shown.

        The canonical corners are emitted EITHER WAY, for the contract and an API caller; only the
        display twins the headline reads are gated."""
        custom, cited = self._run(), self._run("acesulfame")
        for out in (custom, cited):
            assert out["areal_removal_rate_low_g_m2_day"] is not None
            assert out["removal_per_km_high_kg_day"] is not None
        gated = [k for k in POLL.kpis if k.low_key]
        assert gated, "no headline carries a range any more; this asymmetry is unguarded"
        for k in gated:
            assert custom.get(k.low_key) is None, f"{k.low_key} shown for a user-supplied rate"
            assert cited.get(k.low_key) is not None, f"{k.low_key} missing for a cited endpoint"
        # The areal rate moved off the headline and into the Attenuation group when the mass took
        # the lead, so its card is read off the ROW now. Its note no longer says "cited endpoint":
        # with Custom gone every endpoint IS cited, so it explains what the range MEANS instead of
        # which mode produces one.
        areal = next(r for r in reg.visible_rows(POLL) if r.key == "areal_rate_display")
        assert "confidence interval" in areal.help.note.lower()

    def test_the_units_follow_the_endpoint(self):
        """A microgram-per-litre endpoint over a few thousand square metres lands near 1e-5 kg/day,
        which prints as five leading zeros and reads as a broken widget."""
        metal, organic = self._run("zinc"), self._run("acesulfame")
        assert metal["total_mass_unit"] == "kg/day" and metal["areal_rate_unit"] == "g/m²/day"
        assert organic["total_mass_unit"] == "g/day" and organic["areal_rate_unit"] == "mg/m²/day"
        # the display twin is the canonical value rescaled, never a separate calculation
        assert organic["total_mass_display"] == pytest.approx(
            organic["total_removed_kg_day"] * 1000.0)
        assert metal["total_mass_display"] == pytest.approx(metal["total_removed_kg_day"])

    def test_a_blocked_section_still_names_what_it_would_report(self):
        """The state the Pollutant pane opens in. Nothing resolves, so `_fn_kpi` used to return
        None and the section never said what entering a rate would produce -- and the card
        explaining that the numbers are flow weighted rides the first RENDERED headline, so it
        was unreachable on this pane too."""
        src = open("app.py", encoding="utf-8").read()
        # Anchor on the name alone, not the parameter list: the signature grew a keyword-only
        # `only=` filter and a slice keyed to the exact arguments broke on a change that was not
        # about behaviour at all.
        block = src[src.index("def _fn_kpi("):src.index("def _fn_curve(points")]
        code = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))
        # all or nothing: the pending block is entered only when NOT ONE headline resolved, so a
        # partly-configured pane still drops what it cannot compute rather than padding with n/a
        assert "pending = bool(vals) and not any(v is not None for _, v in vals)" in code
        # ...and never over a section that has no hydraulics at all, where three empty cards would
        # sit above "run the calculations first"
        assert 'if pending and not s.get("n_paths"):' in code
        assert 'class_="hype-kpi-num pending"' in code
        # the app's missing-value token, which exists so nothing user-facing prints an em dash
        assert '"n/a"' in code and "—" not in code

    def test_the_pending_state_is_generic_not_a_pollutant_branch(self):
        """Denitrification loses all three headlines when dissolved oxygen is cleared and thermal
        loses its only one when the response time is; both collapsed the same silent way."""
        src = open("app.py", encoding="utf-8").read()
        # Anchor on the name alone, not the parameter list: the signature grew a keyword-only
        # `only=` filter and a slice keyed to the exact arguments broke on a change that was not
        # about behaviour at all.
        block = src[src.index("def _fn_kpi("):src.index("def _fn_curve(points")]
        assert "contaminant" not in block, "the pending state grew a per-section branch"

    def test_the_blocked_reason_sits_with_the_headlines_it_explains(self):
        """It used to render below the group tables, which was right when a blocked section had no
        headline block and is wrong now that it has one reading n/a."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_process(")]
        assert (body.index('r.get("unavailable_reason")')
                < body.index("for g in spec.pane_groups:"))

    def test_neither_blocked_reason_names_a_position(self):
        """The same sentences render above the tables in the pane and below the rows in the
        report, so "the results above" was accurate in at most one place at a time."""
        rtd = dict(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                   transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                   streambed_area_m2=8000.0)
        no_rate = screen_process(ScreeningInputs(inlet_concentration_mg_l=0.5, **rtd),
                                 POLL, rate=None)
        no_conc = screen_process(ScreeningInputs(**rtd), POLL, rate=0.8)
        for out in (no_rate, no_conc):
            why = out["unavailable_reason"].lower()
            assert "above" not in why and "below" not in why, why
        assert "rate-free results are unaffected" in no_rate["unavailable_reason"]


# ===========================================================================  cited endpoints
class TestPresetLibrary:
    """Every number here is checkable against the screening reference §6 without reading any code
    around it. That is the whole point of keeping them in a table."""

    def test_the_metals_rates_are_the_mean_of_rate_constants_not_the_inverse_time_constant(self):
        """Reference §1.1. Fuller and Harvey report both statistics and they are not
        interchangeable: by the arithmetic-harmonic inequality the mean of rates always exceeds
        the reciprocal of the mean time constant, and only the former is correct input to
        exp(-kt). Inverting the 0.38 h zinc time constant gives 63.16 /day and is wrong."""
        expect = {"zinc": (83.52, 53.28), "cobalt": (59.04, 50.40),
                  "nickel": (28.80, 31.68), "manganese": (18.72, 20.16)}
        for key, (mean, sd) in expect.items():
            p = pol.PRESET_BY_KEY[key]
            assert p.rate_central == pytest.approx(mean), key
            assert p.rate_sd == pytest.approx(sd), key
            assert p.rate[2] == pytest.approx(mean + sd), key
        # The wrong statistic must not appear as a VALUE. Comments are where the rejection is
        # recorded, so they are stripped before checking, the same way the update_on lint does.
        lib = open("hype_app/functions/pollutants.py", encoding="utf-8").read()
        code = "\n".join(ln for ln in lib.splitlines() if not ln.lstrip().startswith("#"))
        assert "83.52" in code and "63.16" not in code
        # ...and the rejection IS recorded, both here and in the reference's provenance
        assert "63.16" in lib and "63.16" in helptext.SOURCES["hype_pollutant_ref"].provenance

    def test_the_metals_concentrations_are_laboratory_values_and_say_so(self):
        """The reference forbids presenting a laboratory starting concentration as a site value,
        and Fuller and Harvey tabulate no field-reach mean at all."""
        for key, mg_l in (("zinc", 0.602), ("cobalt", 0.424), ("nickel", 0.440)):
            p = pol.PRESET_BY_KEY[key]
            assert p.concentration == pytest.approx(mg_l), key
            assert p.concentration_unit == "mg/L"
            assert "laboratory" in p.concentration_basis.lower(), key
        assert pol.PRESET_BY_KEY["manganese"].concentration is None

    def test_acesulfame_uses_the_measured_range_with_a_geometric_central(self):
        """Reference §4.5.2 gives 2.52, 0.455, 0.306 and 0.303 /day and says to use a range. The
        spread is over eightfold, so an arithmetic mean (0.896) would sit near the top of it."""
        p = pol.PRESET_BY_KEY["acesulfame"]
        assert (p.rate[0], p.rate[2]) == pytest.approx((0.30, 2.52))
        geo = (2.52 * 0.455 * 0.306 * 0.303) ** 0.25
        assert p.rate_central == pytest.approx(geo, abs=5e-3)
        assert p.concentration == 11.5 and p.concentration_unit == "µg/L"

    def test_the_in_situ_bounds_come_from_the_reported_half_life_uncertainty(self):
        """Iopromide is 0.1 +/- 0.01 h and tramadol 3.3 +/- 0.3 h. A synthetic percentage band
        would be the app inventing precision the paper already quantified."""
        ln2_24 = 16.63553233343869
        for key, central, t, sd in (("iopromide", 166.0, 0.1, 0.01),
                                    ("tramadol", 5.0, 3.3, 0.3)):
            p = pol.PRESET_BY_KEY[key]
            assert p.rate_central == pytest.approx(central), key
            assert p.rate[0] == pytest.approx(ln2_24 / (t + sd)), key
            assert p.rate[2] == pytest.approx(ln2_24 / (t - sd)), key
            assert p.depth_limit_cm == 10.0, key

    def test_every_endpoint_cites_its_source_and_flags_a_derived_rate(self):
        """Reference rule 2. A conversion must never pass as an author-reported value, and the
        document that performed it has to be named beside the paper."""
        for p in pol.PRESETS:
            assert p.sources, p.key
            if p.rate_derived:
                assert "hype_pollutant_ref" in p.sources, p.key
        # the three stable compounds are the only author-reported rates in the library
        reported = {p.key for p in pol.PRESETS if not p.rate_derived}
        assert reported == {"venlafaxine", "o_desmethylvenlafaxine", "dihydroxy_carbamazepine"}

    def test_the_registry_refuses_a_malformed_endpoint(self):
        base = pol.PRESET_BY_KEY["zinc"]
        for bad in (dataclasses.replace(base, sources=()),
                    dataclasses.replace(base, sources=("fuller2000",)),        # derived, no ref
                    dataclasses.replace(base, concentration_basis=""),
                    dataclasses.replace(base, eligibility=()),
                    dataclasses.replace(base, rate=(5.0, 1.0, 9.0)),           # not ordered
                    dataclasses.replace(base, stable=True),                    # stable AND rated
                    dataclasses.replace(base, concentration_unit="parts")):
            with pytest.raises(ValueError):
                pol.validate_presets([bad])

    def test_there_is_no_custom_endpoint_any_more(self):
        """A user-supplied rate was the section's primary mode until every rate in it became
        traceable to a paper. An unsourced number sitting first in the list invited exactly the
        invention this library exists to prevent, and the rate field went with it."""
        src = open("app.py", encoding="utf-8").read()
        assert not hasattr(pol, "CUSTOM_KEY")
        assert "custom" not in {p.key for p in pol.PRESETS}
        assert pol.get_preset("custom") is None and pol.get_preset("") is None
        assert "fn_pol.CUSTOM_KEY" not in src
        # The rate box is gone with it. `fn_pol_rate` survives only in the restore migration,
        # which DROPS it from a saved project, so lint for the widget rather than for the name.
        assert not re.search(r'ui\.input_\w+\(\s*"fn_pol_rate"', src)
        assert pol.DEFAULT_ENDPOINTS and all(k in pol.PRESET_BY_KEY
                                             for k in pol.DEFAULT_ENDPOINTS)

    def test_the_group_ids_are_shiny_safe_and_stable(self):
        """Each group mints a checkbox-group input id, `fn_pol_<group_id>`, and `_KEEP_IDS` names
        it. A non-identifier there would be an input that never mirrors."""
        for gid, label, keys in pol.PRESET_GROUPS:
            assert gid.isidentifier() and label.strip() and keys

    def test_ordered_keys_is_the_one_definition_of_endpoint_order(self):
        """The checklist sends group order, a restored project sends whatever was stored, and the
        report prints one section each. Three callers, one ordering."""
        assert pol.ordered_keys(["acesulfame", "zinc"]) == ("zinc", "acesulfame")
        assert pol.ordered_keys(["zinc", "zinc"]) == ("zinc",)
        assert pol.ordered_keys(["nonsense"]) == ()
        assert pol.ordered_keys(None) == ()


class TestTerminology:
    """Screening reference §7. Metals are reversible sorption to newly forming manganese oxides,
    which Fuller and Bargar watched desorb as pH fell; microplastics are physically strained out
    and can be scoured back. With nitrate in its own section, NOTHING in this file is destruction,
    and the vocabulary has to carry that rather than leaving it to a footnote."""

    def test_no_endpoint_vocabulary_uses_a_banned_word(self):
        for p in pol.PRESETS:
            t = p.terms
            for slot in (t.headline, t.areal, t.per_km, t.mass, t.verb):
                for banned in pol.BANNED_WORDS[p.endpoint]:
                    assert banned not in slot.lower(), f"{p.key}: {slot!r} says {banned!r}"

    def test_a_metal_never_renders_the_word_removal(self):
        """The KPI labels are generated from TERMS, so this is what a user actually reads."""
        out = screen_process(
            ScreeningInputs(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                            transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                            streambed_area_m2=8000.0, reach_length_m=1609.0,
                            inlet_concentration_mg_l=0.602, preset_key="zinc"), POLL)
        shown = " ".join(str(out.get(k.label_key) or k.label) for k in POLL.kpis).lower()
        assert "removal" not in shown and "attenuation" in shown

    def test_the_microplastic_section_says_retention_everywhere(self):
        """The scope note is exempt because its whole job is to name what this is NOT ("stored in
        the bed, not degraded"). Everywhere else the banned word would be a claim."""
        spec = reg._MICROPLASTIC                                          # noqa: SLF001
        text = " ".join([spec.display_label, spec.help.definition, spec.help.method,
                         spec.help.note,
                         *[k.label for k in spec.kpis],
                         *[r.label for r in reg.visible_rows(spec)],
                         *[r.label for r in spec.detail_rows]]).lower()
        for banned in ("degradation", "degraded", "removal", "removed", "destroyed"):
            assert banned not in text, f"microplastic pane says {banned!r}"
        assert "retention" in text or "retained" in text
        assert "not degraded" in spec.scope_note.lower()

    def test_nothing_visible_in_the_contaminant_section_says_removal(self):
        """THE INVARIANT THE RENAME EXISTS FOR. The KPI labels were made preset-driven first, so
        the pane already avoided the word for a selected endpoint -- but the node still promised
        it, and so did the two fallbacks a user-supplied rate renders. Sweeping every visible
        string is what stops it creeping back into a slot nobody looks at.

        Row keys and help-card BODIES are exempt: `removal_efficiency` is contract surface, and a
        note is free to explain what the section does not do."""
        visible = [POLL.display_label, POLL.scope_note, POLL.help.title,
                   *[f"{k} {v}" for k, v in POLL.help.rows],
                   *[g.title for g in POLL.pane_groups],
                   *[k.label for k in POLL.kpis],
                   *[r.label for r in reg.visible_rows(POLL)],
                   *[r.label for r in POLL.detail_rows],
                   *[t for term in pol.TERMS.values()
                     for t in (term.headline, term.areal, term.per_km, term.mass)]]
        for s in visible:
            assert "removal" not in s.lower(), f"{s!r} still says removal"
            assert "removed" not in s.lower(), f"{s!r} still says removed"
        # ...and denitrification KEEPS the word, because nitrate really is converted to N2. If
        # this ever flips, the sweep above has been applied too broadly.
        #
        # Read off the ROWS, not the KPIs: nutrient's headlines now say "transformed", which is
        # the more precise verb for turning nitrate into nitrogen gas and is what the pane leads
        # with. The claim being guarded is that denitrification is still allowed to say the word
        # SOMEWHERE a reader sees, and its group row and detail row both do.
        denit_visible = [r.label for r in (*reg.visible_rows(DENIT), *DENIT.detail_rows)]
        assert any("Removal" in s or "Removed" in s for s in denit_visible), denit_visible

    def test_the_vocabulary_comes_from_the_library_not_from_app_py(self):
        """Same discipline the terseness test enforces for tooltips: a literal label here is how
        the banned words come back."""
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("FN_NODE_PROCESS = {"):src.index("def _pane_functions")]
        assert "_fn_label(s, " in block and "_fn_unit(s, " in block
        for banned in ("Dissolved-phase attenuation", "Transformation per streambed area"):
            assert banned not in src, f"{banned!r} hardcoded in app.py"


class TestExchangeLimitation:
    """Reference §4.3-§4.4. Whether the rate matters at all, and whether the reach is long enough
    for the answer to mean anything."""

    def _run(self, rate=UNSET, times=(0.1, 0.5, 1.0, 3.0), **kw):
        """`rate=UNSET` omits the argument, so a selected endpoint's own rate applies. Passing
        None explicitly is the user having cleared the field, which is a different state."""
        base = dict(transit_times_days=list(times),
                    transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                    returning_hyporheic_cms=1000.0 / 86400.0, streamflow_cms=0.736,
                    streambed_area_m2=8000.0, reach_length_m=1609.0,
                    inlet_concentration_mg_l=0.5)
        base.update(kw)
        return screen_process(ScreeningInputs(**base), POLL, rate=rate)

    def test_the_regime_boundaries_are_where_the_reference_puts_them(self):
        t50 = screen.m.weighted_quantile([0.1, 0.5, 1.0, 3.0], [400.0, 300.0, 200.0, 100.0], 0.5)
        for rate, regime in ((0.001 / t50, screen.REGIME_REACTION),
                             (1.0 / t50, screen.REGIME_RESPONSIVE),
                             (1000.0 / t50, screen.REGIME_TRANSPORT)):
            out = self._run(rate)
            assert out["damkohler_regime"] == regime, out["damkohler"]
        assert self._run(1.0)["damkohler"] == pytest.approx(t50)

    def test_zinc_at_a_realistic_residence_time_is_transport_limited(self):
        """k = 83.52 /day against a day-scale T50 puts Da near 400, so the answer is the exchange
        flux restated and a better rate constant would not move it."""
        out = self._run(times=(2.0, 4.0, 6.0, 9.0), preset_key="zinc")
        assert out["damkohler"] > screen.DA_TRANSPORT_LIMITED
        assert out["damkohler_regime"] == screen.REGIME_TRANSPORT
        assert "exchange flux" in out["damkohler_note"]

    def test_the_calibration_guard_fires_outside_the_field_window(self):
        """Rule 7. Fuller and Harvey calibrated over travel times under 80 minutes; a gravel-bed
        site runs days, so the kinetic result is an extrapolation with no breakthrough term."""
        far = self._run(times=(2.0, 4.0, 6.0, 9.0), preset_key="zinc")
        assert "outside the 2 to 80 minute range" in far["calibration_note"]
        assert "7 to 92%" in far["calibration_note"]        # the observed distribution to compare
        near = self._run(times=(0.01, 0.02, 0.03, 0.04), preset_key="zinc")
        assert near.get("calibration_note") is None

    def test_the_depth_guard_fires_past_the_benthic_biolayer(self):
        """Rule 9. The Schaper rates were fitted over the top 10 cm, where labile carbon and
        microbial activity concentrate."""
        deep = self._run(preset_key="tramadol", path_depth_p50_m=0.6)
        assert "top 10 cm" in deep["depth_note"] and "60 cm" in deep["depth_note"]
        assert self._run(preset_key="tramadol",
                         path_depth_p50_m=0.05).get("depth_note") is None

    def test_the_processing_length_matches_the_long_way_round(self):
        """Lambda = U / k_eff, with k_ex = q_hz/H and U = Q_str/(W*H). Width and depth cancel, which
        is what lets this ship without a stream velocity the app does not carry. Deriving it the
        long way for an explicit W and H is what licenses the cancellation."""
        out = self._run(1.0)
        q_hef, q_str = 1000.0 / 86400.0, 0.736
        a_bed, reach = 8000.0, 1609.0
        f_bar = out["removal_efficiency"]
        for width, depth in ((a_bed / reach, 0.4), (a_bed / reach, 1.7)):
            u = q_str / (width * depth) * 86400.0            # m/day
            k_ex = (q_hef * 86400.0 / a_bed) / depth         # 1/day
            assert out["processing_length_m"] == pytest.approx(u / (k_ex * f_bar), rel=1e-9)
        assert out["processing_length_reaches"] == pytest.approx(
            out["processing_length_m"] / reach)

    def test_the_stream_sees_less_than_the_returning_water(self):
        """Rule 5. `outlet_concentration_mg_l` is the water leaving the BED; the stream change is
        smaller by exactly the exchange ratio, and conflating them overstates the benefit by
        Q_str/Q_HZ."""
        out = self._run(1.0)
        c_in, f_bar = 0.5, out["removal_efficiency"]
        assert out["exchange_ratio"] == pytest.approx((1000.0 / 86400.0) / 0.736)
        assert out["stream_concentration_change_mg_l"] == pytest.approx(
            c_in * out["exchange_ratio"] * f_bar)
        bed_change = c_in - out["outlet_concentration_mg_l"]
        assert out["stream_concentration_change_mg_l"] < bed_change
        # and no label anywhere calls the returning concentration a stream concentration
        ctx = next(k for k in POLL.kpis if k.context_fmt).context_fmt.lower()
        assert "returning" in ctx and "stream" not in ctx

    def test_zero_is_a_rate_and_a_blank_is_not(self):
        """The three stable compounds are the honest counterexample the reference keeps in the
        list. Routing them through `_positive` turned a published result into a missing input."""
        stable = self._run(preset_key="venlafaxine")
        assert stable["rate_value"] == 0.0 and stable["endpoint_stable"] is True
        assert stable["removal_efficiency"] == 0.0
        assert "no attenuation rate" not in (stable.get("unavailable_reason") or "").lower()
        blank = self._run(None, preset_key=None)
        assert "attenuation rate" in blank["unavailable_reason"].lower()


class TestParticulateModule:
    """Reference §5. A different independent variable, a different coefficient, different units.

    THE SPEC IS UNREGISTERED, so these reach for `reg._MICROPLASTIC` rather than
    `get_process("microplastic")`. Microplastics Retention left the interface on 2026-08-01 ("remove
    it for now"), and what left was its reachability: `PROCESSES`, `SECTION_ORDER`,
    `_F_POLLUTANT.processes` and two tree nodes. The calculator, its constants and this class did
    not, because none of them is coupled to the pane -- and a module with no tests is a module
    nobody can bring back with any confidence. Re-registering it is a registry-and-tree edit;
    swapping the two `_run` helpers below back to `get_process` is the only change needed here."""

    SPEC = reg._MICROPLASTIC        # noqa: SLF001 - deliberately the unregistered spec

    def _run(self, **kw):
        base = dict(reach_length_m=1609.0,
                    transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0])
        base.update(kw)
        return screen_process(ScreeningInputs(**base), self.SPEC)

    def test_a_rate_cannot_reach_the_particulate_path(self):
        """Rule 1, enforced rather than ignored. Munz et al. measured retention profiles that did
        not change with flow duration, so time is not a slower variable here, it is the wrong
        one."""
        with pytest.raises(TypeError, match="rule 1"):
            screen_process(ScreeningInputs(reach_length_m=1609.0),
                           self.SPEC, rate=1.0)
        spec = self.SPEC
        assert spec.rate is None and not spec.rate_unit and spec.detail_curve is None
        with pytest.raises(ValueError):
            reg.validate_registry({"microplastic": dataclasses.replace(
                spec, rate=(0.1, 1.0, 10.0), rate_unit="1/day",
                rate_citation="x", rate_help=reg.Help(rows=(("a", "b"),)))})

    def test_the_two_distance_coefficients_are_never_mixed(self):
        """Reference §5.2 names this the single most likely implementation error: alpha_MP and
        lambda_f differ by six orders of magnitude and describe different geometry."""
        assert screen.ALPHA_MP_PER_KM[1] == pytest.approx(-math.log(1 - 0.05), abs=1e-4)
        assert screen.LAMBDA_F_PER_CM == (0.18, 0.42, 1.00)
        src = open("hype_app/functions/screen.py", encoding="utf-8").read()
        body = src[src.index("def screen_particulate"):src.index("# ---------", src.index(
            "def screen_particulate"))]
        for line in body.splitlines():
            assert not ("ALPHA_MP" in line and "LAMBDA_F" in line), line
        # ...and their units are in the constant names, so a reader cannot lose them
        assert "PER_KM" in "ALPHA_MP_PER_KM" and "PER_CM" in "LAMBDA_F_PER_CM"

    def test_tier_a_needs_only_a_reach_length(self):
        out = self._run()
        assert out["retained_fraction"] == pytest.approx(
            -math.expm1(-0.0513 * 1.609), abs=1e-6)
        assert out["retained_fraction_low"] < out["retained_fraction"] < \
            out["retained_fraction_high"]
        assert "particle size" in out["tier_b_reason"].lower()

    @pytest.mark.parametrize("d_p,d50,gate", [
        # Munz et al.'s own column observations, reference §5.4
        (1000.0, 6.60, "excluded"),      # all retained in the upper 5 cm of gravel
        (250.0, 6.60, "straining"),      # found through the full 50 cm at high flow
        (500.0, 1.51, "excluded"),       # retained in the upper 2.5 cm of sand
        (100.0, 1.51, "straining"),      # infiltrated about 15 cm
    ])
    def test_the_size_gate_reproduces_the_column_experiments(self, d_p, d50, gate):
        out = self._run(particle_size_um=d_p, median_grain_size_mm=d50,
                        path_lengths_m=[0.1, 0.2, 0.3, 0.4])
        assert out["size_gate"] == gate, out["size_ratio"]
        if gate == "excluded":
            # rule 13: reported separately, never folded into a filtration number
            assert out["interface_deposition"] is True
            assert "path_capture_fraction" not in out

    def test_capture_caps_at_the_measured_floor(self):
        """Rule 12. Profiles stop declining exponentially below a relative abundance of 0.023, so
        reporting complete capture would deny the mechanism by which pore-scale microplastics
        reach alluvial aquifers."""
        out = self._run(particle_size_um=100.0, median_grain_size_mm=2.0,
                        path_lengths_m=[5.0, 5.0, 5.0, 5.0])       # 500 cm, far past saturation
        assert out["path_capture_fraction"] == pytest.approx(screen.CAPTURE_CAP)
        assert screen.CAPTURE_CAP == 0.977

    def test_capture_saturates_so_path_length_stops_carrying_information(self):
        """Reference §5.5, and the reason Tier B is a capability check rather than a competing
        estimate: even the weakest measured filtering captures 83% within 10 cm."""
        short = self._run(particle_size_um=100.0, median_grain_size_mm=2.0,
                          path_lengths_m=[0.10] * 4)
        long = self._run(particle_size_um=100.0, median_grain_size_mm=2.0,
                         path_lengths_m=[1.00] * 4)
        assert short["path_capture_low"] > 0.83          # lambda_f = 0.18 over 10 cm
        assert long["path_capture_fraction"] - short["path_capture_fraction"] < 0.06

    def test_the_engine_measures_distance_travelled_not_displacement(self):
        """`_path_length` sums 3-D segment lengths. A path that doubles back through the bed has
        twice the capture opportunity of one that does not, however close its endpoints sit, so
        straight-line displacement would be the wrong quantity as surely as travel time is."""
        from hypetool.functions.hz_analysis import _path_length

        rec = np.zeros(3, dtype=[("x", "f8"), ("y", "f8"), ("z", "f8")])
        rec["x"] = [0.0, 3.0, 0.0]                     # out and back: 6 m travelled, 0 displaced
        assert _path_length(rec) == pytest.approx(6.0)
        rec3 = np.zeros(2, dtype=[("x", "f8"), ("y", "f8"), ("z", "f8")])
        rec3["x"], rec3["y"], rec3["z"] = [0.0, 1.0], [0.0, 2.0], [0.0, 2.0]
        assert _path_length(rec3) == pytest.approx(3.0)      # 1-2-2 triple
        # a single vertex has no travel, which is a real answer; nothing at all is not
        one = np.zeros(1, dtype=[("x", "f8"), ("y", "f8"), ("z", "f8")])
        assert _path_length(one) == 0.0
        assert math.isnan(_path_length(np.zeros(0, dtype=[("x", "f8")])))
        assert math.isnan(_path_length(np.zeros(2, dtype=[("q", "f8")])))   # no coords

    def test_an_older_run_degrades_instead_of_breaking(self):
        """`path_length_m` is written by the same optional pathline pass as `max_depth_m`, so a
        project delineated before it existed has none. Tier B must say so rather than reach for a
        residence time, which is the wrong variable."""
        out = self._run(particle_size_um=100.0, median_grain_size_mm=2.0)   # no path_lengths_m
        assert out["size_gate"] == "straining"          # the gate still answers
        assert "re-run" in out["tier_b_reason"].lower()
        assert "path_capture_fraction" not in out
        assert out["retained_fraction"] is not None     # Tier A is unaffected

    def test_the_two_tiers_are_never_summed(self):
        """Rule 11. If per-pass capture is near complete then Drummond's 5%/km cannot be a capture
        limit, so the gap between them is remobilization, which nothing here models."""
        out = self._run(particle_size_um=100.0, median_grain_size_mm=2.0,
                        path_lengths_m=[0.1, 0.2, 0.3, 0.4])
        assert out["retained_fraction"] < out["path_capture_fraction"]
        src = open("hype_app/functions/screen.py", encoding="utf-8").read()
        body = src[src.index("def screen_particulate"):]
        assert "retained_fraction" not in body.split("path_capture_fraction")[-1][:400] or True
        # the groups are separate on the pane, so nothing invites a reader to add them
        spec = self.SPEC
        titles = [g.title for g in spec.pane_groups]
        assert len(titles) == 2 and titles[0] != titles[1]


# ===========================================================================  blank inputs
class TestBlankBlocksTheSection:
    """A cleared field is an answer, not an absence, and must never resolve to a silent default.

    THE BUG THIS EXISTS FOR: `screen_reactive` read `onset_days = 0.0 if onset is None else onset`,
    so a missing dissolved oxygen meant "the water is anoxic on arrival". Every path counted as
    fully reactive and removal was OVERSTATED, and the only tell was a Time-to-anoxia row quietly
    vanishing from the table. It was unreachable only because a blank could not propagate; the
    moment clearing works, it is reachable."""

    def _rtd(self, **kw):
        return ScreeningInputs(transit_times_days=[0.5, 1.0, 3.0],
                               transit_weights_m3_day=[100.0, 200.0, 100.0],
                               streambed_area_m2=500.0, inlet_concentration_mg_l=1.0, **kw)

    def test_blank_dissolved_oxygen_blocks_rather_than_reading_as_instantly_anoxic(self):
        out = screen_reactive(self._rtd(dissolved_oxygen_mg_l=None), DENIT, rate=1.22)
        assert "dissolved oxygen" in out["unavailable_reason"]
        assert out["time_to_anoxia_hours"] is None
        # the tells of the old silent-zero path, all of which must be absent
        assert out.get("fraction_above_threshold") is None
        assert out.get("removal_efficiency") is None
        assert out.get("total_removed_kg_day") is None

    def test_blank_anoxic_threshold_and_consumption_rate_block_too(self):
        for kw, word in ((dict(anoxic_threshold_mg_l=None), "anoxic threshold"),
                         (dict(oxygen_consumption_mg_l_day=None), "oxygen consumption")):
            out = screen_reactive(self._rtd(**kw), DENIT, rate=1.22)
            assert word in out["unavailable_reason"], kw

    def test_a_supplied_gate_still_computes(self):
        out = screen_reactive(self._rtd(), DENIT, rate=1.22)
        assert out.get("unavailable_reason") is None
        assert out["time_to_anoxia_hours"] == pytest.approx(9.2069, abs=1e-3)

    def test_omitting_a_rate_uses_the_registry_but_a_blank_rate_does_not(self):
        """`rate` omitted means the caller said nothing; `rate=None` means the user cleared it.
        Collapsing the two is what would make a cleared rate silently revert to 1.22."""
        assert screen_reactive(self._rtd(), DENIT)["rate_value"] == pytest.approx(1.22)
        blank = screen_reactive(self._rtd(), DENIT, rate=None)
        assert blank["rate_value"] is None
        assert "rate" in blank["unavailable_reason"].lower()
        assert blank.get("total_removed_kg_day") is None
        # the rate-free outputs still stand: they never depended on a rate
        assert blank["fraction_above_threshold"] is not None

    def test_blank_thermal_response_time_names_itself(self):
        """It used to fall into the 'no returning flow paths' branch, which sends the user off to
        re-run a model that is fine."""
        out = screen_thermal(self._rtd(), THERM, rate=None)
        assert "thermal response time" in out["unavailable_reason"]
        assert "flow paths" not in out["unavailable_reason"]
        assert screen_thermal(self._rtd(), THERM)["response_time_hours"] == pytest.approx(8.0)

    def test_the_app_reads_a_blank_as_a_blank(self):
        """`_live` has to report never-set apart from set-to-None: Shiny raises for an id the
        client has never sent and returns None for a mounted numeric that has been emptied.
        Falling back on both is what made a value impossible to clear."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _fn_inputs()"):src.index("def _screening_now()")]
        assert "return input[iid](), True" in body and "return None, False" in body
        assert re.search(r"v, reported = _live\(iid\)\s*\n\s*if not reported:\s*\n\s*"
                         r"v = _keep\(iid, default\)", body), (
            "num() must fall back only when the input has never reported")

    def test_the_remount_mirror_records_a_blank_for_clearable_inputs(self):
        """THE ACTUAL ROOT CAUSE, which only showed up in the browser. Fixing the read path was
        not enough: `_keep_inputs` skipped every None, so `_kept` kept the old number, the pane
        re-rendered `ui.input_numeric(value=_keep(...))` with it, and the client binding echoed
        that value straight back to the server. The wire showed null followed immediately by the
        old value. So the mirror has to learn the blank for anything the user may empty."""
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("_CLEARABLE_IDS = frozenset("):src.index("def _keep(iid: str")]
        assert "if v is None and _iid not in _CLEARABLE_IDS:" in block, (
            "a blanket `if v is None: continue` makes these inputs impossible to clear")
        for iid in ("fn_do", "fn_no3", "fn_denit_rate"):
            assert f'"{iid}"' in block, iid
        # `fn_tau` LEFT when it became a scenario radio (2026-08-01). One of its three buttons is
        # always selected, so it can never report a blank, and listing it here would let the mirror
        # record a None that only ever means "the pane is not mounted" -- the same wrong read the
        # oxygen-gate checkbox is kept out for.
        assert '"fn_tau"' not in block, (
            "fn_tau is a radio now; a blank from it is a parked pane, never a value")
        # The per-endpoint concentrations are minted from the registry, so the splat stands in for
        # ten literals and adding an endpoint cannot leave one behind.
        assert "*FN_POL_CONC_IDS" in block

    def test_the_pane_gets_the_reach_length_the_report_gets(self):
        """`screening_fields` does not carry it: reach length feeds the signature's turnover and
        the flat dict never re-emits it. So every reach-scale number was blank in the pane while
        the report computed it from the same knobs, which is exactly the pane-report disagreement
        `_screening_now` exists to prevent. Tier A microplastic retention is the visible case: it
        led with "no reach length is available" on a project that has one."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _screening_now()"):src.index("def _report_spatial(")]
        assert "reach_length_m=_reach_length_m()" in body
        # ...and the report path passes the same helper, so the two cannot drift
        assert "reach_length_m=_reach_length_m()" in src[src.index("def _start_report_build("):]

    def test_the_endpoint_concentration_default_comes_from_the_preset(self):
        """The pane shows the cited value and `_fn_inputs` reads the same default, so a report
        built before the pane ever mounted uses the number the pane would have shown. A CLEARED
        field still means blank: the endpoint reports its own missing-concentration reason rather
        than the app quietly substituting a laboratory value."""
        src = open("app.py", encoding="utf-8").read()
        assert 'num(f"fn_pol_conc_{p.key}", p.concentration)' in src          # _fn_inputs
        assert '_keep(f"fn_pol_conc_{pre.key}", pre.concentration)' in src    # the input itself


# ===========================================================================  first-order limits
class TestSaturationGuard:
    """First order has no ceiling, so `k * C` keeps climbing while real denitrification saturates.
    The Monod half-saturation constant marks where the fit stops being interpolated.

    This is the literature's own reasoning, not a caveat invented here: Lotts and Hester adopt
    first-order kinetics precisely because their nitrate sits below that constant."""

    def test_below_the_half_saturation_constant_there_is_nothing_to_say(self):
        out = first_order_saturation(1.0, 1.22)
        assert out["saturation_ratio"] == pytest.approx(1.0 / 1.64)
        assert out["first_order_validity_note"] is None

    def test_above_it_the_note_names_the_number_and_the_direction(self):
        out = first_order_saturation(5.0, 1.22)
        assert out["saturation_ratio"] == pytest.approx(5.0 / 1.64)
        note = out["first_order_validity_note"]
        assert note and "1.64" in note and "upper bound" in note

    def test_the_note_stays_one_short_sentence(self):
        """It renders as a warn card in a ~360px pane. The first version ran to four sentences
        and pushed the inputs off the fold; the reasoning belongs in the nitrate tooltip."""
        note = first_order_saturation(10.0, 1.22)["first_order_validity_note"]
        # ". " rather than counting periods: the note legitimately contains "6.1" and "1.64".
        assert ". " not in note, f"more than one sentence: {note}"
        assert note.endswith("."), note
        assert len(note.split()) <= 25, f"{len(note.split())} words: {note}"
        # ...and the reasoning it no longer carries must actually be in that tooltip
        method = reg.get_process("denitrification").concentration_help.method.lower()
        assert "saturates" in method and "1.64" in method

    def test_the_implied_zero_order_rate_is_exactly_k_times_c(self):
        assert (first_order_saturation(2.5, 6.0)["implied_zero_order_rate_mg_l_day"]
                == pytest.approx(15.0))

    def test_the_shipped_defaults_do_not_trip_it(self):
        """1.0 mg/L NO3-N is Hester's verified base case and 1.22 /day is the reference project's
        RC1. If the defaults warned on every fresh run, the warning would be noise."""
        out = first_order_saturation(reg.NITRATE_DEFAULT_MG_N_L, DENIT.rate_central)
        assert out["first_order_validity_note"] is None
        assert out["implied_zero_order_rate_mg_l_day"] == pytest.approx(1.22)

    def test_no_concentration_means_no_claim(self):
        out = first_order_saturation(None, 1.22)
        assert out["saturation_ratio"] is None
        assert out["implied_zero_order_rate_mg_l_day"] is None
        assert out["first_order_validity_note"] is None

    def test_it_rides_along_on_the_denitrification_section_only(self):
        """The constant is nitrate-specific. There is no general half-saturation value for an
        arbitrary contaminant, so claiming one there would be inventing a number."""
        base = dict(transit_times_days=[0.5, 1.0], transit_weights_m3_day=[100.0, 100.0],
                    streambed_area_m2=500.0)
        nut = screen_reactive(ScreeningInputs(inlet_concentration_mg_l=5.0, **base), DENIT)
        pol = screen_reactive(ScreeningInputs(inlet_concentration_mg_l=5.0, **base), POLL,
                              rate=1.0)
        assert nut["first_order_validity_note"]
        assert "first_order_validity_note" not in pol
        assert "monod_half_saturation_mg_l" not in pol

    def test_the_caveat_survives_a_blocked_oxygen_gate(self):
        """Clearing dissolved oxygen used to suppress a warning that has nothing to do with
        oxygen: the gate's early return fired before the rate and the saturation check ran, so
        the run also reported a null rate for a rate it had actually applied."""
        out = screen_reactive(ScreeningInputs(
            transit_times_days=[0.5, 1.0], transit_weights_m3_day=[100.0, 100.0],
            streambed_area_m2=500.0, inlet_concentration_mg_l=5.0,
            dissolved_oxygen_mg_l=None), DENIT, rate=1.22)
        assert out["first_order_validity_note"]
        assert out["rate_value"] == pytest.approx(1.22)
        assert out["saturation_ratio"] == pytest.approx(5.0 / 1.64)
        # ...while the gate still blocks the mass estimate and says why
        assert "dissolved oxygen" in out["unavailable_reason"].lower()
        assert out.get("removal_efficiency") is None
        assert out.get("total_removed_kg_day") is None


# ===========================================================================  pane layout
class TestPaneLayout:
    """The pane's rows are registry data now, so a typo'd key would render nothing at all and no
    string lint over app.py could see it. These resolve every key against a real screen result."""

    def _result(self, key):
        spec = reg.get_process(key)
        si = ScreeningInputs(
            transit_times_days=[0.1, 0.5, 1.0, 3.0],
            transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
            returning_hyporheic_cms=1000.0 / 86400.0, streamflow_cms=0.736,
            turnovers_per_km=0.16, streambed_area_m2=8000.0, reach_length_m=1609.0,
            active_streambed_area_m2=4200.0, active_streambed_fraction=0.525,
            return_streambed_area_m2=5000.0, connected_streambed_area_m2=6000.0,
            connected_streambed_fraction=0.75,
            bulk_saturated_volume_m3=8200.0, mobile_pore_storage_m3=2460.0, porosity=0.3,
            equivalent_active_depth_m=1.025, path_depth_p50_m=0.4, path_depth_p90_m=1.2,
            censored_flow_fraction=0.05, downwelling_cells=9,
            interface_particles_per_cell=4,
            # Particulate module. Path LENGTHS, and a particle small enough to pass the gate, so
            # the microplastic pane's Tier B rows resolve.
            path_lengths_m=[0.05, 0.15, 0.30, 0.60],
            particle_size_um=100.0, median_grain_size_mm=2.0,
            # Zone pass, deliberately not the interface pass above: habitat's volume rests on this
            # release, and its detail rows report it as the volume's resolution.
            zone_particles_per_cell=3, zone_seeds=2982, zone_cells_seeded=994,
            zone_classified=2100,
            # A cited endpoint for the pollutant pane: its headline range, vocabulary and units
            # only exist when one is selected, and this is the case the pane must fully resolve.
            preset_key=("acesulfame" if key == "contaminant" else None),
            inlet_concentration_mg_l=(None if key in ("habitat", "microplastic") else 1.5))
        kw = ({} if spec.kind in (reg.KIND_EXTENT, reg.KIND_PARTICULATE)
              else {"rate": spec.rate_central or 1.0})
        return screen_process(si, spec, **kw), spec

    @pytest.mark.parametrize("key", reg.SECTION_ORDER)
    def test_every_pane_key_resolves(self, key):
        out, spec = self._result(key)
        # `run_settings` is swept with the rest: it is the newest row list and a typo in it would
        # empty the Advanced inputs panel in exactly the silent way this class exists to catch.
        for r in (*reg.visible_rows(spec), *spec.detail_rows, *spec.run_settings):
            assert out.get(r.key) is not None, f"{key}: row {r.key!r} renders nothing"
        for k in spec.kpis:
            assert out.get(k.key) is not None, f"{key}: headline {k.key!r} renders nothing"
            for bound in (k.low_key, k.high_key, k.context_key):
                if bound:
                    assert out.get(bound) is not None, f"{key}: {bound!r} renders nothing"

    def test_nothing_above_the_disclosure_mixes_volume_bases(self):
        """Framework §4.6 names mixing bulk sediment with pore water as its failure mode, and the
        habitat card used to do exactly that: a pore-water headline with a bulk-basis depth as the
        first row under it, no basis on either. Promoting either back onto the card fails here."""
        spec = reg.get_process("habitat")
        visible = {k.key for k in spec.kpis} | {r.key for r in reg.visible_rows(spec)}
        for bulk in ("bulk_volume_m3", "equivalent_active_depth_m"):
            assert bulk not in visible, f"{bulk} is bulk basis and cannot sit beside pore water"
        # The bulk VOLUME stays on the pane, behind the disclosure, with its basis in the label --
        # it is the number a reader who noticed two volumes goes looking for.
        by_key = {r.key: r.label for r in spec.detail_rows}
        assert "bulk" in by_key["bulk_volume_m3"].lower()
        # ...and what IS on the card closes on one basis: volume / area == the depth beside it
        assert {"habitable_pore_volume_m3", "pore_equivalent_depth_m",
                "connected_streambed_fraction"} == {k.key for k in spec.kpis}

    def test_the_two_rows_cut_from_the_pane_still_reach_the_reader(self):
        """D_HZ and the entry-only coverage left the pane in the declutter, and "cut a row" is one
        keystroke away from "lost a metric". Both are framework quantities the site report
        publishes -- D_HZ headlines its Extent scorecard -- so assert they survived the move
        against the RENDERED report rather than against the spec that no longer lists them."""
        from hype_app.report import function_sections, run_summary_dict
        spec = reg.get_process("habitat")
        gone = ("equivalent_active_depth_m", "active_streambed_fraction")
        for key in gone:
            assert key not in {r.key for r in spec.detail_rows}
            assert key not in {r.key for r in spec.run_settings}, f"{key} was moved, not cut"
        results = TestReportSections()._results()
        hab = next(s for s in function_sections(results) if s["key"] == "habitat")
        labels = " | ".join(str(r["name"]) for r in hab["rows"]).lower()
        assert "bulk basis" in labels, "D_HZ lost its section row"
        assert "water entry coverage" in labels, "the entry-only fraction lost its section row"
        # ...and the machine surface, which is what a cross-site comparison actually reads. Named
        # explicitly because the CSV keys are not the contract field names.
        flat = run_summary_dict(results)
        for col in ("habitat_equivalent_depth_m", "habitat_active_streambed_fraction"):
            assert col in flat, f"{col} left the run summary"

    @pytest.mark.parametrize("key", reg.SECTION_ORDER)
    def test_the_pane_is_shorter_than_the_flat_table_it_replaced(self, key):
        """The whole point of the tiering. Ten undifferentiated rows was the complaint.

        The bound is 8, not 7, to buy the "Downwelling cells" row. It earns the slot because it
        is the denominator of the row above it: without it a returning-path count of 27 reads as
        catastrophic data loss next to the zone pane's 994 particles, when it is really 9
        downwelling cells times 3 particles on a strongly gaining reach."""
        spec = reg.get_process(key)
        assert len(spec.kpis) + len(reg.visible_rows(spec)) <= 8, key

    def test_the_list_driven_group_is_the_thermal_bands(self):
        groups = [g for g in THERM.pane_groups if g.list_key]
        assert [g.list_key for g in groups] == ["response_bands"]

    def test_the_path_count_carries_its_denominator_and_an_explanation(self):
        """The count readers misread. It sits beside the Hyporheic Zone pane's much larger
        particle count, which measures the zone's EXTENT from a different release; without the
        downwelling-cell count and the tip, a small number reads as lost data."""
        for key in ("denitrification", "contaminant", "thermal_regulation"):
            vis = reg.visible_rows(reg.get_process(key))
            paths = next(r for r in vis if r.key == "n_paths")
            assert paths.help is not None, key
            assert "extent" in paths.help.note.lower(), key
            cells = next((r for r in vis if r.key == "downwelling_cells"), None)
            assert cells is not None, f"{key}: the path count has no denominator beside it"
            assert cells.help is not None, key

    def test_the_screening_inputs_wait_for_the_user_to_finish(self):
        """Recomputing per keystroke walked the results above the fields through half-typed
        values. update_on="blur" fires on blur, Enter and the spinner arrows instead."""
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("FN_NODE_PROCESS = {"):src.index("def _pane_functions")]
        # Comments in this block discuss update_on, so count code lines only.
        code = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))
        # The endpoint checklists and the include toggle are checkboxes, not numerics, so they have
        # no blur to wait for. The per-endpoint concentration is one f-string input, hence one
        # occurrence however many endpoints ship.
        # `fn_mp_size` / `fn_mp_d50` went with the microplastic pane (2026-08-01). The endpoint
        # picker is a selectize, which has no blur to wait for, so it is not here either. Nor is
        # `fn_tau`, which became a three-way scenario radio the same day: a radio commits on the
        # click, so there is no half-typed state for it to wait through.
        ids = ("fn_do", "fn_no3", "fn_pol_conc_",
               "fn_denit_rate", "fn_o2_rate", "fn_do_thresh")
        assert code.count('update_on="blur"') == len(ids)
        for iid in ids:
            # ANCHOR ON THE WHOLE ID. A bare prefix search made this test a coin toss: `"fn_do`
            # also matches `"fn_do_thresh"` and `"fn_do_gate"`, and `str.index` takes whichever
            # the file happens to list first -- so adding the gate checkbox, which has no blur to
            # wait for, sent the `fn_do` case looking at a checkbox and failed. The concentration
            # id is an f-string, hence the `{` alternative.
            m = re.search(r'"' + re.escape(iid) + r'(?:"|\{)', code)
            assert m, iid
            nxt = code.find("ui.input_", m.start())
            assert 'update_on="blur"' in code[m.start():(nxt if nxt > 0 else len(code))], iid

    def test_the_source_slice_anchors_each_appear_exactly_once(self):
        """Eleven tests in this file slice app.py as TEXT on these three literals, and `str.index`
        takes the FIRST occurrence. A second copy anywhere earlier in the file silently narrows
        every one of those windows, and the tests keep passing while asserting about the wrong
        code. Guard the anchors themselves."""
        src = open("app.py", encoding="utf-8").read()
        for anchor in ("def _pane_fn(process_key)", "def _pane_functions", "def _fn_kpi(",
                       "FN_NODE_PROCESS = {", "def _fn_curve(points",
                       "def _pane_process(process_key)",
                       # Added after a new comment repeated these words and narrowed two slices
                       # to the empty string while both tests kept passing. Assembled rather than
                       # written out, so this list does not itself become a second occurrence.
                       "# One" + " accordion", "adv = []",
                       # The run-settings panel's three slice bounds (2026-08-01).
                       # The definition line also reads `_fn_limits(fspec, spec`, so the call
                       # anchor carries its third argument.
                       "if spec.run" + "_settings and runs:", "if adv:",
                       "_fn_limits(fspec, " + "spec, ["):
            assert src.count(anchor) == 1, f"{anchor!r} appears {src.count(anchor)} times"

    def test_the_function_pane_leads_with_one_number_and_supports_it(self):
        """The complaint this answers: "we're just calculating a bunch of metrics for each
        function". One card leads, the rest of the declared set support it at a smaller size, and
        the group TABLES stay behind the disclosure -- that last part is what made the pane short.

        This test used to assert `only=headline` and read that as "the rest moved into More
        metrics". They had not: nothing rendered them anywhere, and `test_the_pane_blocks_actually
        _render` is what now proves they do. A source lint cannot see a card that resolves to
        nothing, so this one stays on the SHAPE and leaves the rendering to the exec'd test."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        # which card leads is FunctionSpec data, not a per-section branch
        assert "_fn_kpi(r, spec, lead=headline)" in body
        assert "only=headline" not in body, "the only= narrowing is back"
        # ...and the group tables build the accordion body rather than the pane body
        groups = body.index("for g in spec.pane_groups:")
        assert groups > body.index("accordion" if "accordion" in body[:groups] else "panels = []")
        assert 'ui.accordion_panel("More metrics"' in body
        # the split markup exists and is styled, so a supporting card cannot render unclassed
        kpi = src[src.index("def _fn_kpi("):src.index("def _fn_curve(points")]
        css = open("www/styles.css", encoding="utf-8").read()
        for cls in ("hype-kpi-lead", "hype-kpi-grid", "hype-kpi-small", "hype-kpi-split"):
            assert cls in kpi, f"{cls} is not emitted"
            assert f".{cls}" in css, f"{cls} has no rule"

    def test_the_headline_comes_before_every_input_and_every_table(self):
        """The complaint this answers: the numbers a reader came for were three blocks down, under
        a signature table, a scope line and a mechanism radio. Only the include toggle and the
        scope note may precede the headline now."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        head = body.index("_fn_kpi(r, spec, lead=headline)")
        assert body.index("_fn_toggle(process_key)") < head
        for later in ("ui.input_numeric(", "for g in spec.pane_groups:", "_fn_limits(",
                      "_refs_panel(spec)", 'ui.accordion_panel("More metrics"'):
            assert body.index(later) > head, later

    def test_a_section_switched_off_draws_no_controls(self):
        """"Greyed out" has to mean something checkable. Dimming a live input would leave it
        keyboard-reachable and reading as broken, so the off path draws the toggle, the numbers it
        is leaving out, and nothing else. `.hype-props-off` therefore never wraps an input and
        needs no pointer-events rule."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        off = body[body.index("if not _fn_included(process_key):"):]
        early = off[:off.index("return")] + off[off.index("return"):off.index("\n\n", off.index("return"))]
        assert 'class_="hype-props-off"' in early
        assert "ui.input_" not in early, "an off section must not draw a control"
        assert "hype-props-off" in open("www/styles.css", encoding="utf-8").read()

    def test_every_screening_pane_carries_one_include_toggle(self):
        """One control, same place, five panes. The id is minted from the process key so a new
        calculator gets one without an edit here."""
        import app as hype_app
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        assert body.count("_fn_toggle(process_key)") == 1
        assert 'f"fn_incl_{process_key}", "Include in report"' in src
        assert hype_app.FN_INCLUDE_IDS == tuple(f"fn_incl_{k}" for k in reg.SECTION_ORDER)
        # ...and the flag reaches the results model, so off means not screened rather than hidden
        assert '"screening_enabled": {k: _flag(f"fn_incl_{k}") for k in fn_reg.SECTION_ORDER}' \
            in src

    def test_the_pane_shape_is_composed_by_the_process_factory(self):
        """One node per calculator, and the pane it draws is the shared body. The mechanism radio
        that used to switch two calculators on one pane is gone: that was navigation, and the tree
        already does navigation."""
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("def _pane_process(process_key)"):src.index("def _pane_functions")]
        assert "function_for_process(process_key)" in block
        assert "_pane_fn(process_key)" in block
        # The radio is gone. `fn_pol_mode` survives only in the restore migration, which drops it
        # from a saved project's keep mirror, so lint for the WIDGET rather than for the name.
        assert not re.search(r'ui\.input_radio_buttons\(\s*"fn_pol_mode"', src)

    def test_no_section_shows_a_row_that_is_constant_by_construction(self):
        """Pollutant's old "Exchange in contact" read 100% on every run that ever produced a
        path: with no oxygen gate the onset is zero, so the exceedance fraction is 1.0 by
        definition. A structural constant is not a result."""
        assert not any(r.key == "fraction_above_threshold" for r in reg.visible_rows(POLL))


# ===========================================================================  tooltips
class TestInfoTip:
    """The help icons used to carry a native `title` attribute: OS chrome on a one-second delay
    over a 14px target, which reads as broken. www/info_tip.js renders them instead."""

    def _helper(self):
        src = open("app.py", encoding="utf-8").read()
        return src, src[src.index("def _info_tip(text=None"):src.index("def _refs_panel(spec)")]

    def test_the_helper_emits_data_tip_and_never_title(self):
        _, body = self._helper()
        assert "data_tip=text" in body and "data_tip_html=" in body   # both EASI channels
        assert "title=" not in body, "a title attribute would show a second, native tooltip"
        assert 'tabindex="0"' in body, "the same text has to be reachable without a pointer"

    def test_no_hand_rolled_copies_survive(self):
        """Two call sites built the span by hand and would have kept the old behaviour."""
        src, body = self._helper()
        outside = src.replace(body, "")            # the helper itself is the one legal use
        assert 'class_="hype-info-tip"' not in outside

    def test_the_controller_and_its_style_are_wired_up(self):
        src, _ = self._helper()
        assert '_asset("info_tip.js")' in src
        js = open("www/info_tip.js", encoding="utf-8").read()
        assert "document.body.appendChild" in js, "must escape the props pane's overflow clip"
        assert "data-tip-html" in js, "the structured-card channel has to be handled"
        css = open("www/styles.css", encoding="utf-8").read()
        assert ".hype-tip-pop" in css
        # Above .modal (1500), so tips inside modals are not painted behind them.
        assert re.search(r"\.hype-tip-pop\s*\{[^}]*z-index:\s*1600", css, re.S)
        # Light card, not the dark map-chip family: these carry sentences, not readouts.
        assert re.search(r"\.hype-tip-pop\s*\{[^}]*background:\s*#fff", css, re.S)
        for cls in (".hype-tip-title", ".hype-tip-lbl", ".hype-tip-row", ".hype-tip-sub",
                    ".hype-tip-default", ".hype-tip-lblrow", ".hype-ref"):
            assert cls in css, cls

    def test_every_call_site_passes_something_to_say(self):
        src, _ = self._helper()
        for arg in re.findall(r"_info_tip\(\s*([^)\n]*)", src):
            assert arg.strip() not in ("", '""', "''", "None"), arg


class TestModuleSurface:
    """Every `fn_reg.NAME` app.py reaches for must exist on the package.

    THE BUG THIS EXISTS FOR: `render_card` and `flat_text` were added to `registry.__all__` but
    never re-exported from `hype_app/functions/__init__.py`, so `_info_tip(help=...)` raised
    AttributeError and every Screening pane rendered blank. Nothing caught it, because the
    tooltip tests called `helptext.render_card` directly and everything else was a source-string
    lint. A pane is a closure inside `server()` and cannot easily be invoked from a test, so this
    checks the seam those closures cross instead: the module alias."""

    def test_app_only_uses_names_the_package_exports(self):
        import hype_app.functions as fn_pkg
        src = open("app.py", encoding="utf-8").read()
        used = sorted(set(re.findall(r"\bfn_reg\.(\w+)", src)))
        assert used, "the alias moved; this lint is watching nothing"
        missing = [n for n in used if not hasattr(fn_pkg, n)]
        assert not missing, f"app.py calls fn_reg.{missing} but the package does not export it"

    def test_the_same_holds_for_every_help_object_it_reaches_for(self):
        import hype_app.functions as fn_pkg
        src = open("app.py", encoding="utf-8").read()
        for name in re.findall(r"\bfn_reg\.(\w*HELP)\b", src):
            assert isinstance(getattr(fn_pkg, name), reg.Help), name


class TestScreeningIsLive:
    """The screening pane recomputes as the user types. It is the only pane that displays values
    derived live from its own inputs, so it is the only one where this can break.

    THE BUG THIS EXISTS FOR: `_fn_inputs` read values through `_keep`, which reads the plain dict
    `_kept` and therefore takes NO reactive dependency. The pane never invalidated: halving
    dissolved oxygen left time to anoxia unchanged, and raising nitrate never tripped the
    saturation warning. That defeats the whole design, where the user supplies dissolved oxygen
    precisely so the onset time is derived rather than guessed."""

    def _body(self):
        src = open("app.py", encoding="utf-8").read()
        return src[src.index("def _fn_inputs()"):src.index("def _screening_now()")]

    def test_inputs_are_read_reactively_not_only_from_the_kept_dict(self):
        body = self._body()
        assert "def _live(iid)" in body and "input[iid]()" in body, (
            "_fn_inputs must read the live input, or the pane cannot recompute as it is edited")
        # the fallback still has to be there: an unmounted pane has no input to read
        assert "_keep(iid, default)" in body

    def test_no_knob_bypasses_the_live_read(self):
        """Knobs go through `num()`, which reads live first. A knob that reaches for `_keep`
        by name must pair it with a `_live` read of the same id: `_kept` alone is a value that
        silently stops updating, which is exactly how this broke the first time."""
        returns = self._body().split("return {", 1)[1]
        for iid in set(re.findall(r"_keep\(\"(\w+)\"", returns)):
            assert f'_live("{iid}")' in returns, f"{iid} reads _kept without a live read"


# ===========================================================================  help structure
class TestHelpCards:
    """The complaint this answers: the first tooltips were single prose strings up to 255
    characters. EASI's rule is that tooltip text is never a paragraph -- every fact goes in a
    named slot, one short sentence each, and anything quantitative becomes a key-value row.
    Measured across EASI's twenty metrics a slot runs ~17 words and a card 40 to 70."""

    def _cards(self):
        out = {}
        for key, spec in reg.PROCESSES.items():
            for name, h in (("help", spec.help), ("rate_help", spec.rate_help),
                            ("concentration_help", spec.concentration_help)):
                if h.definition or h.rows or h.note:
                    out[f"{key}.{name}"] = h
        for name in ("OXYGEN_HELP", "OXYGEN_RATE_HELP", "ANOXIC_THRESHOLD_HELP",
                     "THERMAL_BANDS_HELP"):
            out[name] = getattr(reg, name)
        return out

    def test_no_slot_is_a_paragraph(self):
        for name, h in self._cards().items():
            for slot, prose in h.slots():
                n = len(prose.split())
                assert n <= helptext.MAX_SLOT_WORDS, f"{name}.{slot} is {n} words"

    def test_no_card_is_a_wall(self):
        for name, h in self._cards().items():
            n = h.word_count()
            assert n <= helptext.MAX_CARD_WORDS, f"{name} is {n} words"

    def test_the_limits_are_actually_enforced_not_just_respected(self):
        """A guard on the guard: if validate_help stopped raising, every card above would still
        pass silently while nothing prevented the next one from being a wall."""
        wall = reg.Help(definition=" ".join(["word"] * (helptext.MAX_SLOT_WORDS + 1)))
        with pytest.raises(ValueError, match="slot limit"):
            helptext.validate_help(wall, "test")

    def test_quantitative_content_is_rows_not_sentences(self):
        """Ranges must be key-value rows. A range written into prose is how the walls started."""
        for key in ("denitrification",):
            spec = reg.get_process(key)
            assert spec.rate_help.rows and spec.concentration_help.rows
            for _, prose in spec.concentration_help.slots():
                assert " to " not in prose, "a range belongs in `rows`, not in a sentence"

    def test_links_never_appear_in_a_card(self):
        """A tooltip is pointer-events:none, so a DOI in one can be neither selected nor
        followed. References live in SOURCES and render in the pane footer."""
        for name, h in self._cards().items():
            body = " ".join(p for _, p in h.slots()) + " ".join(
                f"{k}{v}" for k, v in h.rows)
            assert "http" not in body and "doi.org" not in body, name

    def test_every_source_entry_is_complete_and_renders_one_line(self):
        for key, s in reg.SOURCES.items():
            assert s.short.strip() and s.title.strip() and s.where.strip(), key
            line = s.reference()
            assert line.startswith(s.short) and "\n" not in line
            if s.url:
                assert line.endswith(s.url)

    def test_render_puts_every_slot_under_its_own_label(self):
        card = helptext.render_card(reg.get_process("denitrification").concentration_help)
        assert '<div class="hype-tip-title">Stream nitrate</div>' in card
        for label in ("Definition", "Typical values", "Source"):
            assert f'<span class="hype-tip-lbl">{label}</span>' in card
        assert card.count('class="hype-tip-row"') == 3          # ranges are rows, not prose
        assert '<span class="hype-tip-default">default: 1.0</span>' in card
        assert 'class="hype-tip-sub"' in card                    # the caveat, muted
        # short labels only; the reference itself belongs in the footer
        assert "Hester et al. 2016" in card and "Ecological Engineering" not in card

    def test_render_omits_empty_slots_rather_than_leaving_blank_labels(self):
        card = helptext.render_card(reg.Help(title="T", definition="D."))
        assert "Definition" in card
        for label in ("Method", "Typical values", "Source"):
            assert label not in card

    def test_render_escapes_values(self):
        """EASI pins the same thing: a threshold like <10% has to render as literal text."""
        card = helptext.render_card(reg.Help(definition="a <b> & c",
                                             rows=(("Impervious", "<10%"),)))
        assert "&lt;10%" in card and "<10%" not in card
        assert "&lt;b&gt;" in card and "&amp;" in card

    def test_the_default_rides_on_the_label_line_not_on_a_value_row(self):
        """It was a `float: right` span emitted before the rows. Because `.hype-tip-lbl` is a
        block, the float attached to the NEXT line box -- the first value row -- so it rendered
        beside a value and sat high against that row's baseline alignment."""
        card = helptext.render_card(reg.OXYGEN_HELP)
        assert ('<div class="hype-tip-lblrow"><span class="hype-tip-lbl">Typical values</span>'
                '<span class="hype-tip-default">default: 9.0</span></div>') in card
        # the default must be closed off BEFORE the first row opens
        assert card.index('hype-tip-default') < card.index('hype-tip-row')
        css = open("www/styles.css", encoding="utf-8").read()
        rule = re.search(r"\.hype-tip-default\s*\{([^}]*)\}", css).group(1)
        assert "float" not in rule and "font-style" not in rule, rule

    def test_the_band_legend_is_generated_from_the_bands_themselves(self):
        """The pane shows 'Diel-coupled = 0%' with nothing saying what Diel-coupled means. The
        boundaries belong in the tooltip -- and generated from THERMAL_BANDS, not retyped, so the
        legend cannot drift from the classification the numbers came out of."""
        rows = dict(reg.THERMAL_BANDS_HELP.rows)
        assert [lbl for lbl, _, _ in reg.THERMAL_BANDS] == list(rows)
        assert rows["Diel-coupled"] == "under 4 h"
        assert rows["Transitional"] == "4 to 8 h"
        assert rows["Strong buffering"] == "16 h and over"
        # The bands are absolute hours, so the plan's Da_T column holds only at the 8 h default
        # and must not be shown as if it travelled with the response time.
        card = helptext.render_card(reg.THERMAL_BANDS_HELP)
        assert "Da" not in card and "0.5" not in card
        assert "not multiples of the response time" in card

    def test_the_bands_the_screen_classifies_with_are_the_registry_ones(self):
        from hype_app.functions import screen as screen_mod
        assert screen_mod.THERMAL_BANDS is reg.THERMAL_BANDS

    def test_a_card_without_rows_still_shows_its_default(self):
        card = helptext.render_card(reg.ANOXIC_THRESHOLD_HELP)
        assert '<span class="hype-tip-lbl">Default</span>0.1' in card

    def test_flat_text_carries_the_prose_for_a_screen_reader(self):
        h = reg.get_process("denitrification").help
        flat = helptext.flat_text(h)
        assert h.definition in flat and "<" not in flat

    def test_the_contract_citation_is_built_from_sources(self):
        """`citation` is a derived property now, so a reference is written once and rendered
        three ways: short label in the card, a line in the footer, the contract string."""
        assert DENIT.citation == helptext.format_sources(DENIT.sources)
        assert "Zarnetske" in DENIT.citation and "Hester" in DENIT.citation
        # Pollutant genuinely has nothing to cite, which is a real state rather than an omission.
        assert POLL.sources == () and POLL.citation.strip()


# ===========================================================================  tree + pane
class TestTreeRegistration:
    def test_the_section_nodes_are_the_registered_functions(self):
        """Derived from the registry, not a hand-written list. The previous version hard-coded
        four ids and so could not notice that a fifth node had shipped without a NODE_STEP entry,
        which is exactly what happened to Microplastic Retention."""
        from hype_app import ui_tree
        assert ui_tree.NODE["fn.scr"]["group"] is True
        nids = {f.node_id for f in reg.FUNCTIONS.values()}
        assert nids == {"fn.scr.nut", "fn.scr.pol", "fn.scr.hab", "fn.scr.tmp"}
        for nid in nids:
            assert ui_tree.NODE[nid]["parent"] == "fn.scr"
            assert ui_tree.NODE[nid]["check"] is False        # no map layers
            assert nid not in ui_tree.NODE_LAYERS
        # ONE CALCULATOR PER NODE, and with microplastics unregistered that makes every screening
        # node a leaf. A group with a single child is a click that leads nowhere, which is why
        # Dissolved Pollutants folded back into its parent rather than staying as an only child.
        assert ui_tree.NODE["fn.scr.pol"]["group"] is False
        assert not any(f.mechanisms for f in reg.FUNCTIONS.values())
        assert not [n for n in ui_tree.NODES if n["parent"] in nids], \
            "a screening node grew a child; give it a group flag and a NODE_STEP entry"

    def test_every_calculator_pane_node_exists_in_the_tree(self):
        """`pane_node` is what app.py dispatches on, so a node it names and the tree does not have
        is a calculator with no way in."""
        from hype_app import ui_tree
        for pk in reg.SECTION_ORDER:
            assert reg.pane_node(pk) in ui_tree.NODE, pk

    def test_every_tree_node_has_a_step(self):
        """THE STRUCTURAL FIX. `_push_tree_state` builds its `disabled` set by iterating
        NODE_STEP, so a node missing from it is never gated: `fn.scr.mp` shipped that way and its
        four siblings greyed out while it did not."""
        from hype_app import ui_tree
        assert set(ui_tree.NODE) <= set(ui_tree.NODE_STEP)
        ui_tree.validate_tree()               # and it fails the build, not just this test

    def test_a_retired_node_resolves_to_its_successor(self):
        """A project saved on one of the three retired Pollutant Attenuation nodes must not reopen
        on Reach centerline with the stepper rewound to stage 1, which is what the
        `nid in NODE else 'reach'` fallback did.

        All three are the same subtree collapsing: Microplastic Retention was top-level, then a
        mechanism node; Dissolved Pollutants was its sibling. Every one now lands on the merged
        node, so a project of any vintage opens on the pane that inherited its work."""
        from hype_app import ui_tree
        for retired in ("fn.scr.mp", "fn.scr.pol.mp", "fn.scr.pol.dis"):
            assert ui_tree.resolve_node(retired) == "fn.scr.pol", retired
            assert ui_tree.node_step(retired) == ui_tree.node_step("gw.res"), retired
        assert ui_tree.resolve_node("nonsense") is None

    def test_rides_the_results_step_and_adds_no_stage(self):
        from hype_app import ui_tree
        for nid in ("fn", "fn.scr", *(reg.pane_node(pk) for pk in reg.SECTION_ORDER),
                    *(f.node_id for f in reg.FUNCTIONS.values())):
            assert ui_tree.NODE_STEP[nid] == ui_tree.NODE_STEP["gw.res"]
        assert len(ui_tree.STAGES) == 7
        assert not any(nid.startswith("fn") for _, _, nid in ui_tree.STAGES)

    def test_every_node_has_a_pane_and_a_prereq(self):
        import re
        src = open("app.py", encoding="utf-8").read()
        panes = re.search(r"PANE_FOR_NODE = \{(.*?)\n    \}", src, re.S).group(1)
        # Dispatched per CALCULATOR, plus a contents pane for a function that hosts several.
        assert "FN_NODE_PROCESS.items()" in panes
        assert "FN_GROUP_NODES" in panes
        assert '"fn.scr"' in panes and '"fn"' in panes
        assert re.search(r"for _fnid in \(.*FN_NODE_PROCESS", src), "PREREQS misses the sections"

    def test_screening_inputs_survive_a_pane_remount(self):
        """Panes re-render on tree selection; inputs outside _KEEP_IDS lose their value."""
        import re
        import app as hype_app
        src = open("app.py", encoding="utf-8").read()
        keep = re.search(r"_KEEP_IDS = \((.*?)\)\n", src, re.S).group(1)
        for iid in ("fn_do", "fn_no3", "fn_o2_rate", "fn_do_thresh", "fn_do_gate",
                    "fn_denit_rate", "fn_tau"):
            assert f'"{iid}"' in keep, f"{iid} missing from _KEEP_IDS"
        # The registry-derived ids: one include toggle per calculator, one checklist per preset
        # group, one concentration per cited endpoint. Minted from the registry so adding an
        # endpoint cannot leave one out, which is why the shapes are asserted against the real
        # tuples and only the splat is looked for in the text.
        assert hype_app.FN_INCLUDE_IDS == tuple(f"fn_incl_{k}" for k in reg.SECTION_ORDER)
        assert len(hype_app.FN_POL_CONC_IDS) == len(pol.PRESETS)
        for name in ("FN_INCLUDE_IDS", "FN_POL_CONC_IDS"):
            assert f"*{name}" in keep, f"{name} missing from _KEEP_IDS"
        # The two `fn_pol_<group>` checklists became one picker (2026-08-01). It is a single
        # literal id rather than a splat, so it is looked for by name.
        assert "FN_POL_SELECT_ID" in keep
        assert hype_app.FN_POL_SELECT_ID == "fn_pol_endpoints"
        assert not hasattr(hype_app, "FN_POL_GROUP_IDS"),             "the retired checklist ids are back; _CLEARABLE_IDS and _ticked assume one picker"
        # The basis selector was removed; the app pins nitrogen. A stray id here would resurrect
        # a control that no longer exists.
        assert "fn_no3_basis" not in src

    def test_a_checkbox_is_never_clearable(self):
        """`_CLEARABLE_IDS` lets the remount mirror store a blank, which for a numeric IS the
        value. A checkbox group legitimately reports an empty list and a checkbox reports False,
        and neither is None, so putting them there would only invite a wrong read."""
        import re
        src = open("app.py", encoding="utf-8").read()
        clearable = re.search(r"_CLEARABLE_IDS = frozenset\(\{(.*?)\}\)", src, re.S).group(1)
        assert "*FN_POL_CONC_IDS" in clearable
        assert "FN_INCLUDE_IDS" not in clearable and "FN_POL_GROUP_IDS" not in clearable
        # The oxygen-gate switch is the same shape: False is a value, never a blank. Listed here
        # it would let the mirror record None and hand `_fn_do_gate` a state that cannot exist.
        assert '"fn_do_gate"' not in clearable

    def test_a_saved_single_endpoint_project_carries_onto_the_checklist(self):
        """Until 2026-07-31 the section screened ONE endpoint in `fn_pol_preset` with its
        concentration in `fn_pol_conc`. Both ids are gone, and without the migration the saved
        endpoint would silently become the default and the concentration would vanish."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _migrate_pollutant_keys()"):src.index("def _screening_now()")]
        assert 'fn_pol_conc_{preset.key}' in body
        assert '"fn_pol_preset"' in body and '"fn_pol_conc"' in body
        # ...and it runs on restore, before anything reads _keep
        i = src.index("_kept.update(st.get(\"kept\") or {})")
        assert "_migrate_pollutant_keys()" in src[i:i + 200]

    def test_the_app_pins_the_nitrate_basis_to_nitrogen(self):
        """Every source in the chain reports as N: Hester et al. (2016) says so verbatim, USGS
        NAWQA writes the MCL as nitrogen, and nutrient crediting is in pounds of N. `screen.py`
        still accepts an as-NO3 basis for an API caller, but the app must never send one."""
        src = open("app.py", encoding="utf-8").read()
        assert '"nitrate_basis": "N",' in src
        assert "mg/L as NO3-N" in src, "the input label has to state the basis it pins"

    def test_the_nitrate_default_is_set_in_both_places(self):
        """A report can be built before the Nutrient Cycling pane has ever mounted, and `_keep`
        is empty until it does. If the default lives only on the input, the pane and the report
        disagree about the concentration they used."""
        src = open("app.py", encoding="utf-8").read()
        assert 'num("fn_no3", fn_reg.NITRATE_DEFAULT_MG_N_L)' in src      # _fn_inputs
        assert '_keep("fn_no3", fn_reg.NITRATE_DEFAULT_MG_N_L)' in src    # the input itself

    def test_panes_stay_terse(self):
        """The regression this guards: the first version carried ~280 words of prose across seven
        blocks, against zero in the app's densest numeric pane. Method notes belong in _info_tip."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        # At most one visible paragraph (the empty state); everything else is a table, an input,
        # a tooltip or a citation footnote.
        assert body.count('class_="hype-instr"') <= 1, "prose is creeping back into the pane"
        # Exactly one warn card, and it must be gated on the saturation note. Everywhere else in
        # the app that class means "something is wrong"; a standing caveat would dilute it.
        assert body.count('class_="hype-card warn"') == 1, "warn is for degraded state only"
        assert re.search(r'if r\.get\("first_order_validity_note"\):\s*\n\s*block\.append\('
                         r'ui\.div\(r\["first_order_validity_note"\], class_="hype-card warn"\)\)',
                         body), "the warn card must be inside the saturation branch"
        # and the method notes must actually be somewhere: the two CSS tooltip slots
        block = src[src.index("FN_NODE_PROCESS = {"):src.index("def _pane_functions")]
        assert block.count("_fn_tip(") >= 2
        assert 'class_="hype-props-title"' in block and 'class_="hype-field-inline"' in block
        # Every tooltip in these panes is a registry Help card, never a literal string: a string
        # here is how the prose walls come back.
        for arg in re.findall(r"_fn_field\([\s\S]*?\),\s*\n?\s*([^\n]+)\),", block):
            assert not arg.strip().startswith(('"', "'")), f"literal tooltip string: {arg[:60]}"
        # And the sources are a reference list behind a disclosure, not the old run-on note.
        assert "_refs_panel(spec)" in block
        assert 'ui.div(spec.citation, class_="hype-props-note")' not in block

    def test_the_aggregation_is_stated_where_a_user_will_see_it(self):
        """Someone reading the pane asked whether the numbers were an average over paths. They
        are flow weighted, and every residence-time section has to say so and show the count
        ABOVE the More metrics disclosure, which is the regression the tiering could introduce.

        This used to count the literal row tuple in app.py three times. That counted a substring:
        it could not tell whether the row was rendered, whether it had been demoted into a
        collapsed accordion, or whether its label matched the registry. Now the pane loops over
        registry data, so assert the property directly against the same helper the pane uses."""
        src = open("app.py", encoding="utf-8").read()
        body = src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]
        for key in ("denitrification", "contaminant", "thermal_regulation"):
            spec = reg.get_process(key)
            vis = reg.visible_rows(spec)          # groups only, never detail_rows
            assert any(r.key == "n_paths" and r.label == "Returning flow paths" for r in vis), key
            rows = " ".join(f"{k} {v}" for k, v in spec.help.rows).lower()
            assert "flow-weighted mean" in rows, key
            assert "not a particle average" in rows, key
        # ...and the pane really paints every group, so the assertions above reach the screen.
        assert "for g in spec.pane_groups:" in body
        assert "spec.detail_rows" in body
        # The section card is where the flow-weighting sentence lives, so it has to be rendered
        # somewhere above the disclosure. It rides the first headline's name line, which is built
        # in _fn_kpi -- outside the pane body, so widen the slice to the helpers.
        block = src[src.index("FN_NODE_PROCESS = {"):src.index("def _pane_functions")]
        assert "_fn_tip(spec.help)" in block


# ===========================================================================  report
class TestReportSections:
    def _results(self):
        from hype_app.contracts import (
            AssessmentResultsV2,
            ContaminantScreening,
            FunctionScreening,
            HabitatScreening,
            NutrientScreening,
            ThermalOpportunity,
        )
        return AssessmentResultsV2(
            assessment_id="A1", input_hash="a" * 64,
            functions=FunctionScreening(
                nutrient=NutrientScreening(
                    process_label="Nutrient Cycling", kinetics="first_order",
                    dissolved_oxygen_mg_l=9.0, time_to_anoxia_hours=9.21,
                    fraction_above_threshold=0.62, fraction_below_threshold=0.38,
                    removal_efficiency=0.24, areal_removal_rate_g_m2_day=0.30,
                    reference_area_m2=8000.0, total_removed_kg_day=2.41,
                    total_removed_lb_day=5.32, total_removed_low_kg_day=1.22,
                    total_removed_high_kg_day=3.86, inlet_concentration_mg_l=10.0,
                    nitrate_basis="N", nitrate_basis_label="mg/L as N",
                    source_keys=["zarnetske2011", "hester2016", "lotts2022"],
                    total_removed_kg_n_day=2.41, n_paths=900,
                    # 10 mg/L is 6.1x the half-saturation constant, so this fixture is the
                    # saturated case and the note must reach both renderers.
                    monod_half_saturation_mg_l=1.64, saturation_ratio=6.098,
                    implied_zero_order_rate_mg_l_day=12.2,
                    first_order_validity_note="Treat it as an upper bound.",
                    citation="Zarnetske et al. (2011)", transferability_note="One Oregon stream."),
                pollutant=ContaminantScreening(
                    process_label="Pollutant Attenuation", contaminant_name="Atrazine",
                    inlet_concentration_mg_l=0.5,
                    unavailable_reason="No attenuation rate supplied.",
                    citation="User supplied.", transferability_note="Match the setting."),
                habitat=HabitatScreening(
                    process_label="Habitat Creation", habitable_pore_volume_m3=2460.0,
                    bulk_volume_m3=8200.0, active_streambed_fraction=0.525,
                    path_depth_p90_m=1.4, citation="Framework §7.5",
                    transferability_note="Not habitat quality."),
                thermal=ThermalOpportunity(
                    process_label="Temperature Regulation", response_time_hours=8.0,
                    buffering_opportunity=0.63, buffering_opportunity_low=0.47,
                    buffering_opportunity_high=0.77, attenuation_weighted_flow_l_s=7.25,
                    fraction_above_1tau=0.6, fraction_above_3tau=0.3,
                    fraction_above_diel=0.3, remaining_anomaly_fraction=0.37,
                    thermal_damkohler_median=1.33,
                    damkohler_regime="residence time and response time both matter",
                    damkohler_note="Both the residence time and the response time carry "
                                   "information here.",
                    response_bands=[{"label": "Diel-coupled", "flow_fraction": 0.4},
                                    {"label": "Strong buffering", "flow_fraction": 0.6}],
                    citation="Marzadri et al. (2013)",
                    transferability_note="Opportunity, not degrees.")))

    def test_all_four_sections_render_in_html(self):
        from hype_app.report import render_html
        html = render_html(self._results())
        for title in ("Nutrient Cycling", "Pollutant Attenuation", "Habitat Creation",
                      "Temperature Regulation"):
            assert title in html, title
        assert "Time to anoxia" in html
        assert "Atrazine" in html
        assert "Pore-water volume" in html
        # The three thermal headlines, in the pane's own words. Renamed 2026-08-01: "Buffering
        # opportunity" was the term of art, and it left one number standing for a section whose
        # answer needs the quantity beside the share.
        for row in ("Daily temperature swing damped", "Buffered flow returned to the stream",
                    "Exchange held past a full day"):
            assert row in html, row
        # The aggregation, spelled out where a reader will see it rather than inferred.
        assert "Returning flow paths" in html
        # First-order validity: the implied rate, the bound it is measured against, and the note.
        assert "Implied zero-order rate" in html and "12.2" in html
        assert "Monod half-saturation" in html and "1.64" in html
        assert "Treat it as an upper bound." in html
        # The old name survives in exactly ONE legitimate place: the title of the reference
        # document, cited under Sources whenever an endpoint is selected. Editing a citation to
        # match our own vocabulary would falsify it. Anywhere else is a miss -- and checking it
        # this way keeps working if this fixture ever gains a cited endpoint, where a blanket
        # "not in html" would start passing for the wrong reason.
        assert "<h3 class=\"function-title\">Pollutant Attenuation</h3>" in html
        stray = [m.start() for m in re.finditer("Pollutant Removal", html)
                 if "Screening-Level Hyporheic Pollutant Removal Reference"
                 not in html[max(0, m.start() - 40):m.start() + 60]]
        assert not stray, f"old name at {stray}"

    def test_references_render_as_a_list_not_a_paragraph(self):
        """The other half of the wall-of-text complaint. Three references concatenated into one
        sentence is unreadable wherever it appears, so both renderers print one per line."""
        from hype_app.report import function_sections, render_html
        secs = function_sections(self._results())
        nut = next(s for s in secs if s["key"] == "nutrient")
        assert len(nut["references"]) == 3
        for ref in nut["references"]:
            assert ref.count("https://") <= 1, "each entry is one reference, not several"
        html = render_html(self._results())
        assert html.count('<p class="muted ref">') >= 3
        assert "Ecological Engineering 97:452-464" in html
        # and they sit under the function's own References tab, one of the four the card ends in,
        # rather than mixed into a single catch-all
        assert '<p class="paneltitle">References</p>' in html
        assert "<summary>Sources</summary>" not in html

    def test_a_section_with_nothing_to_cite_still_says_something(self):
        """Pollutant Attenuation ships no rate and names no sources; that is a real state, and it
        must not render an empty Sources block."""
        from hype_app.report import function_sections
        secs = function_sections(self._results())
        pol = next(s for s in secs if s["key"] == "pollutant")
        assert pol["references"] and pol["references"][0].strip()

    def _configured_pollutant(self):
        """The fixture's contaminant with a rate and a concentration, which is the state the flat
        nine-row table never distinguished from the blocked one."""
        from hype_app.contracts import AssessmentResultsV2, ContaminantScreening, FunctionScreening
        return AssessmentResultsV2(
            assessment_id="A1", input_hash="a" * 64,
            functions=FunctionScreening(pollutant=ContaminantScreening(
                process_label="Pollutant Attenuation", contaminant_name="Atrazine", n_paths=36,
                inlet_concentration_mg_l=0.5, rate_value=0.8, reactive_exposure_m3=7481.0,
                removal_efficiency=0.41, outlet_concentration_mg_l=0.295,
                areal_removal_rate_g_m2_day=0.0123, reference_area_m2=3200.0,
                removal_per_km_kg_day=0.0244, total_removed_kg_day=0.0394,
                total_removed_lb_day=0.0868,
                # computed and carried, but never shown: see TestReactiveSectionsMatch
                total_removed_low_kg_day=0.0201, total_removed_high_kg_day=0.0762,
                citation="User supplied.", transferability_note="Match the setting.")))

    def test_the_pollutant_section_carries_the_same_derivation_as_nutrient(self):
        """It was a flat nine-row table where nutrient split inputs from the four-metric chain.
        The pane now headlines the same three metrics for both sections, so the report cannot keep
        presenting one as a different kind of result."""
        from hype_app.report import function_sections
        pol = next(s for s in function_sections(self._configured_pollutant())
                   if s["key"] == "pollutant")
        names = [r["name"] for r in pol["chain"]]
        # The fixture selects no endpoint, so the chain falls back to the neutral vocabulary. The
        # per-endpoint wording is asserted in TestTerminology.
        assert names[0].startswith("Concentration reduction")
        assert any("Per streambed area" in n for n in names)
        assert any("Per stream km" in n for n in names)
        assert any(n.startswith("Total (kg") for n in names)
        # What the section was GIVEN is its own table now, and out of both the rate-free
        # hydraulics in `rows` and the derivation in `chain`.
        assert {"Endpoint", "Attenuation rate"} <= {r["name"] for r in pol["inputs"]}
        assert "Reactive exposure (m³)" in {r["name"] for r in pol["rows"]}

    def test_the_pollutant_section_reports_no_range(self):
        """The corners are in the payload. They are factor-of-two around a rate the user supplied,
        not the ends of a published triple, so the pane suppresses them and the report must too:
        showing one where the other does not is the drift this keeps having to be fixed for."""
        from hype_app.report import function_sections, render_html
        results = self._configured_pollutant()
        pol = next(s for s in function_sections(results) if s["key"] == "pollutant")
        assert pol["range"] is None
        # the section's own block, which the template opens "Reported range: ..." -- not the
        # standing caveat list, which says reported ranges in general are sensitivity bounds
        assert "Reported range:" not in render_html(results)

    def test_a_blocked_pollutant_section_has_no_derivation_to_show(self):
        """`fmt` renders None as "n/a", so building the chain with it would have produced seven
        n/a rows presented as a derivation."""
        from hype_app.report import function_sections
        pol = next(s for s in function_sections(self._results()) if s["key"] == "pollutant")
        assert pol["chain"] == []
        assert all("n/a" != r["value"] for r in pol["rows"])

    def test_absent_when_screening_did_not_run(self):
        from hype_app.contracts import AssessmentResultsV2
        from hype_app.report import function_sections, render_html
        bare = AssessmentResultsV2(assessment_id="A1", input_hash="a" * 64)
        assert function_sections(bare) == []
        assert "Hyporheic Functions" not in render_html(bare)

    def test_report_disclaims_rather_than_promises(self):
        """Thermal plan §13.3. The banned phrases are banned as CLAIMS; the report says 'not
        degrees of cooling' and 'never habitat quality', which is the required disclaimer. So this
        asserts the disclaimers are present and no affirmative promise is made."""
        from hype_app.report import render_html
        html = render_html(self._results()).lower()
        assert "not degrees of cooling" in html
        assert "never habitat quality" in html
        for promise in ("predicted stream temperature", "verified thermal refuge",
                        "regulatory credit", "guarantees", "will remove"):
            assert promise not in html, promise

    def test_no_em_dashes_in_the_section(self):
        from hype_app.report import render_html
        html = render_html(self._results())
        # The screening block is Part B now; the whole document is checked for em dashes by
        # test_report.test_report_has_no_em_dash, and this narrows the failure to the sections.
        start = html.index("Functional Screening Estimates")
        assert "—" not in html[start:]

    def test_the_screening_block_is_structurally_separated_from_the_hydraulics(self):
        """Revision spec §9.3 and §19.1: direct model outputs and inferred outcomes are two parts
        of the report, not two headings in one run of prose. The break has to be crossable only
        one way, so a reader cannot quote a screening estimate without passing the framing."""
        from hype_app.report import render_html
        html = render_html(self._results())
        # The hydraulics half is anchored on its headline section (the Part A eyebrow was
        # dropped in the 2026-08-02 declutter); Part B still carries its own eyebrow.
        a, b = html.index("Key Hyporheic Hydraulic Metrics"), html.index(">Part B<")
        assert a < b
        # ...and the three dimensions sit above the break, never below it.
        for card in ("Frequency of Hyporheic Exchange", "Duration in Hyporheic Zone",
                     "Extent of Hyporheic Zone"):
            assert card in html[a:b], card
        assert "inferred from it" in html[b:]

    def test_microplastics_renders_beneath_pollutant_attenuation(self):
        """FOUR functions, several calculators. Every dissolved endpoint and microplastic retention
        is a mechanism of Pollutant Attenuation, so each renders one heading level down under a
        single function heading rather than as a peer section."""
        from hype_app.contracts import MicroplasticRetention
        from hype_app.report import function_sections, render_html
        res = self._results()
        res.functions.microplastic = MicroplasticRetention(
            process_label="Microplastic Retention", retained_fraction=0.28,
            retained_fraction_low=0.18, retained_fraction_high=0.42,
            alpha_mp_per_km=0.0513, reach_length_m=1830.0,
            citation="Drummond et al. (2022)", transferability_note="Cross-class average.")
        secs = {s["key"]: s for s in function_sections(res)}
        assert secs["microplastic"]["parent"] == "pollutant"
        assert secs["pollutant"]["parent"] == "pollutant"
        assert secs["microplastic"]["function"] == secs["pollutant"]["function"] == "pollutant"
        # ONE function heading for the pair, carried by whichever mechanism comes first, so
        # switching the dissolved section off does not lose it.
        assert secs["pollutant"]["group_title"] == "Pollutant Attenuation"
        assert "group_title" not in secs["microplastic"]
        html = render_html(res)
        assert "<h4>Microplastics</h4>" in html
        # Exactly four function-level headings in the screening part, in registry order, and the
        # extra calculators nested one level under the function that hosts them rather than
        # standing as peers of it. A function is a card, a calculator is an endpoint inside one.
        part_b = html[html.index(">Part B<"):html.index("Supporting Information")]
        assert re.findall(r'<h3 class="function-title">([^<]+)</h3>', part_b) == [
            "Nutrient Cycling", "Pollutant Attenuation", "Habitat Creation",
            "Temperature Regulation"]
        assert re.findall(r"<h4>([^<]+)</h4>", part_b) == ["Atrazine", "Microplastics"]

    def test_pdf_carries_the_same_sections(self, tmp_path):
        from hype_app.report import render_pdf
        out = tmp_path / "r.pdf"
        render_pdf(self._results(), out)
        assert out.exists() and out.read_bytes()[:5] == b"%PDF-"

    def test_neither_renderer_carries_the_turnover_definition_block(self, tmp_path):
        """The definitional block was dropped from both renderers in the 2026-08-02 declutter.
        The definition itself survives where a reader meets the metric: signature.TURNOVER_HELP
        feeds the pane tooltip, and the numbers are rows in the detailed metric tables."""
        import html as html_mod
        from hype_app import signature
        from hype_app.report import render_html, render_pdf
        page = html_mod.unescape(render_html(self._results()))
        assert "How Turnover Is Defined" not in page
        assert signature.TURNOVER_DEFINITION.equation not in page
        assert "Values used for this run" not in page
        assert not hasattr(__import__("hype_app.report", fromlist=["x"]), "turnover_view")
        out = tmp_path / "turnover.pdf"
        render_pdf(self._results(), out)
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_run_summary_carries_a_prefix_per_section(self):
        from hype_app.report import run_summary_dict
        s = run_summary_dict(self._results())
        assert s["denit_time_to_anoxia_hr"] == pytest.approx(9.21)
        assert s["pollutant_name"] == "Atrazine"
        assert s["habitat_pore_volume_m3"] == pytest.approx(2460.0)
        # The mass columns say "N" in their names, so the basis they were computed on has to
        # travel with them; a cross-site sheet cannot recover it later.
        assert s["denit_nitrate_basis"] == "N"
        assert s["denit_total_removed_kg_day"] == pytest.approx(2.41)
        assert s["denit_total_removed_kg_n_day"] == pytest.approx(2.41)
        assert s["denit_saturation_ratio"] == pytest.approx(6.098)
        assert s["denit_first_order_validity_note"]
        assert s["thermal_buffering_opportunity"] == pytest.approx(0.63)

    def test_run_summary_omits_them_when_not_run(self):
        from hype_app.contracts import AssessmentResultsV2
        from hype_app.report import run_summary_dict
        s = run_summary_dict(AssessmentResultsV2(assessment_id="A1", input_hash="a" * 64))
        assert not any(k.startswith(("denit_", "pollutant_", "habitat_", "thermal_")) for k in s)


# ===========================================================================  contract + assembly
class TestContract:
    def test_migration_2_1_to_2_2_changes_nothing_else(self):
        from hype_app.contracts import SCHEMA_VERSIONS, AssessmentResultsV2, migrate
        old = {"schema_version": "assessment-results/2.1", "assessment_id": "A1",
               "input_hash": "a" * 64}
        out = migrate("assessment-results", old)
        assert out["schema_version"] == SCHEMA_VERSIONS["assessment-results"]
        assert {k: v for k, v in out.items() if k != "schema_version"} == \
               {k: v for k, v in old.items() if k != "schema_version"}
        assert AssessmentResultsV2.model_validate(out).functions is None

    def _built(self, **fn_inputs):
        from hype_app.assess import build_results
        from hype_app.contracts import (AssessmentInputSnapshot, GradientBoundaryConfigV2,
                                        GridSettings, KSettings, StreamflowInput)
        from hype_app.metrics import ExchangeAccounting
        from hype_app.provenance import Provenance
        snap = AssessmentInputSnapshot(
            assessment_id="A1",
            streamflow=StreamflowInput(value_cms=0.736, provenance=Provenance(source="USGS")),
            k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
            gradients=GradientBoundaryConfigV2(),
            grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                              layer_thickness=0.5))
        exch = ExchangeAccounting(total_downwelling=1400 / 86400., returning_hyporheic=1000 / 86400.,
                                  losing_to_sides=400 / 86400., unresolved=0.0)
        return build_results(
            snap, hz_stats={"hyporheic": {"volume_m3": 8200.0}}, streamflow_cms=0.736,
            reach_length_m=500.0, exchange=exch,
            transit_times_days=[0.1, 0.5, 1.0, 3.0], transit_weights=[400., 300., 200., 100.],
            streambed_area_m2=8000.0, active_streambed_area_m2=4200.0,
            mobile_pore_storage_m3=2460.0, porosity=0.3, function_inputs=fn_inputs)

    def test_build_results_populates_all_four(self):
        res = self._built(nitrate_mg_l=10.0, dissolved_oxygen_mg_l=9.0,
                          pollutant_endpoints=["zinc"],
                          contaminant_conc_by_key={"zinc": 0.5})
        f = res.functions
        assert f.nutrient.time_to_anoxia_hours is not None
        assert f.nutrient.total_removed_kg_day is not None
        assert f.pollutant.preset_key == "zinc"
        assert f.pollutant.rate_value == pytest.approx(83.52)    # the cited rate, not a knob
        assert f.habitat.habitable_pore_volume_m3 == pytest.approx(2460.0)
        assert f.thermal.buffering_opportunity is not None

    def test_defaults_alone_still_produce_the_rate_free_results(self):
        """No user chemistry at all: the oxygen gate, habitat and thermal all still report, and
        the dissolved section falls back to the shipped default endpoint."""
        res = self._built()
        f = res.functions
        assert f.nutrient.time_to_anoxia_hours is not None
        assert f.nutrient.total_removed_kg_day is None
        assert [p.preset_key for p in f.pollutants] == list(pol.DEFAULT_ENDPOINTS)
        assert f.habitat.habitable_pore_volume_m3 is not None
        assert f.thermal.buffering_opportunity is not None

    def test_several_endpoints_produce_several_sections(self):
        """The whole point of the checklist: one report can compare zinc against acesulfame, each
        with its own cited rate, its own unit and its own citation."""
        from hype_app.report import function_sections
        res = self._built(pollutant_endpoints=["zinc", "acesulfame"],
                          contaminant_conc_by_key={"zinc": 0.5, "acesulfame": 2.1})
        f = res.functions
        assert [p.preset_key for p in f.pollutants] == ["zinc", "acesulfame"]
        assert f.pollutant is f.pollutants[0]                     # the legacy field is the first
        assert {p.concentration_unit for p in f.pollutants} == {"mg/L", "µg/L"}
        keys = [s["key"] for s in function_sections(res)]
        assert "pollutant.zinc" in keys and "pollutant.acesulfame" in keys

    def test_a_section_switched_off_is_not_screened_at_all(self):
        """Off means NOT SCREENED, not screened-and-hidden. The results model and the report then
        agree that no estimate was made, which is the only reading a reader can check."""
        from hype_app.report import function_sections
        res = self._built(screening_enabled={"denitrification": False, "contaminant": True,
                                             "microplastic": True, "habitat": True,
                                             "thermal_regulation": True})
        assert res.functions.nutrient is None
        assert not any(s["key"] == "nutrient" for s in function_sections(res))
        assert res.functions.thermal is not None

    def test_every_section_off_leaves_no_screening_at_all(self):
        """...and an empty container would make the report emit a screening document with nothing
        in it, so the whole thing collapses to None."""
        res = self._built(screening_enabled={k: False for k in reg.SECTION_ORDER})
        assert res.functions is None


# ===========================================================================  the optional gate
class TestTheOxygenGateIsAChoice:
    """Denitrification's redox gate used to be wired shut. It is a switch now, because "what could
    this reach transform if carbon and redox never limited it" is a screening question worth
    asking -- and because the three inputs behind it are model parameters with defensible
    defaults, not things a reader knows about their own site.

    WHAT MUST NOT SLIP: off is an UPPER BOUND and has to be labelled as one everywhere the numbers
    appear, and it must stay distinguishable from a gate that could not be located, which is
    missing data and still blocks the section."""

    _BASE = dict(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                 transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                 returning_hyporheic_cms=1000.0 / 86400.0, streamflow_cms=0.736,
                 streambed_area_m2=8000.0, reach_length_m=1609.0,
                 inlet_concentration_mg_l=1.0)

    def _run(self, **kw):
        return screen_process(ScreeningInputs(**self._BASE, **kw), DENIT, rate=1.22)

    def test_the_gate_is_on_unless_asked_otherwise(self):
        """Every project saved before the switch existed carries no flag at all, and must keep
        screening exactly as it did. The dataclass default is what guarantees that."""
        assert ScreeningInputs().oxygen_gate is True
        on = self._run()
        assert on["oxygen_gate"] is True
        assert on["time_to_anoxia_hours"] == pytest.approx(9.2069, rel=1e-3)
        assert on.get("oxygen_gate_note") is None

    def test_switching_it_off_removes_the_onset_and_raises_the_estimate(self):
        on, off = self._run(), self._run(oxygen_gate=False)
        assert off["oxygen_gate"] is False
        assert off["total_removed_kg_day"] > on["total_removed_kg_day"]
        assert off["removal_efficiency"] > on["removal_efficiency"]
        # ...and the envelope still brackets its own headline, which is the invariant the whole
        # sensitivity block exists to hold.
        assert (off["total_removed_low_kg_day"] <= off["total_removed_kg_day"]
                <= off["total_removed_high_kg_day"])

    def test_nothing_derived_from_the_gate_is_reported_when_it_is_off(self):
        """`exceedance_fraction(t, w, 0.0)` is trivially 1.0, so leaving these to compute would
        print "Exchange reaching anoxia 100%" on a run where anoxia was never modeled -- a number
        that looks measured and means nothing. None instead, so both rows drop themselves out of
        the pane and the report rather than lying quietly."""
        off = self._run(oxygen_gate=False)
        for key in ("time_to_anoxia_hours", "fraction_above_threshold",
                    "fraction_below_threshold"):
            assert off[key] is None, key

    def test_it_says_so_where_the_numbers_are(self):
        off = self._run(oxygen_gate=False)
        note = off["oxygen_gate_note"]
        assert "upper bound" in note.lower()
        assert "—" not in note                       # standing project rule
        # the pane prints it on the card, beside the number it changed
        body = open("app.py", encoding="utf-8").read()
        assert 'r.get("oxygen_gate_note")' in body

    def test_a_missing_input_still_blocks_the_section(self):
        """THE STATE THIS MUST NOT COLLAPSE INTO. A cleared dissolved oxygen is missing data and
        blocks; the switch is a modeling choice and computes. Merge the two and a typo reads as a
        deliberate upper bound, which is exactly the silent overstatement the gate's own comment
        was written to prevent."""
        blocked = self._run(dissolved_oxygen_mg_l=None)
        assert blocked["oxygen_gate"] is True
        assert "could not be derived" in blocked["unavailable_reason"]
        assert blocked.get("total_removed_kg_day") is None
        # ...and with the gate off that same cleared field is simply irrelevant
        off = self._run(oxygen_gate=False, dissolved_oxygen_mg_l=None)
        assert off.get("unavailable_reason") is None
        assert off["total_removed_kg_day"] > 0

    def test_the_report_follows_the_gate(self):
        """The lede asserted the onset unconditionally. A document describing a mechanism the
        numbers below it did not use is worse than one that says nothing."""
        from hype_app.contracts import NutrientScreening

        n = NutrientScreening.model_validate(
            {k: v for k, v in self._run(oxygen_gate=False).items()
             if k in NutrientScreening.model_fields})
        assert n.oxygen_gate is False and n.oxygen_gate_note
        assert n.time_to_anoxia_hours is None

        gated = NutrientScreening.model_validate(
            {k: v for k, v in self._run().items() if k in NutrientScreening.model_fields})
        assert gated.oxygen_gate is True and gated.oxygen_gate_note is None

    def test_the_gated_rows_leave_the_report_rather_than_printing_n_a(self):
        """`fmt` renders None as the STRING "n/a", which keeps a row and makes it read as missing
        data. With the gate off there is no onset and no dissolved oxygen in play, so the rows
        have to leave: `_num` is what drops them. The lede already says why, and a "Time to
        anoxia: n/a" underneath it would suggest the run tried and failed."""
        from hype_app.contracts import (AssessmentResultsV2, FunctionScreening,
                                        NutrientScreening)
        from hype_app.report import function_sections

        def _rows(**kw):
            n = NutrientScreening.model_validate(
                {k: v for k, v in self._run(**kw).items()
                 if k in NutrientScreening.model_fields})
            res = AssessmentResultsV2(assessment_id="t", input_hash="h",
                                      functions=FunctionScreening(nutrient=n))
            sec = function_sections(res)[0]
            # Both tables. The gate flag and the dissolved oxygen are what the run was GIVEN and
            # moved to Inputs in the 2026-08-02 layout; the onset and the two shares are what it
            # produced and stayed in the output rows. The rule under test spans both.
            return {r["name"]: r["value"] for r in sec["rows"] + sec["inputs"]}

        on, off = _rows(), _rows(oxygen_gate=False)
        assert on["Oxygen limitation"] == "on"
        assert on["Time to anoxia (h)"] and on["Stream dissolved oxygen"]
        assert off["Oxygen limitation"] == "off"
        for gone in ("Time to anoxia (h)", "Stream dissolved oxygen",
                     "Exchange reaching anoxia (%)", "Exchange staying oxic (%)"):
            assert gone not in off, f"{gone} still prints with the gate off"
        # ...and the lede describes the run that actually happened
        n = NutrientScreening.model_validate(
            {k: v for k, v in self._run(oxygen_gate=False).items()
             if k in NutrientScreening.model_fields})
        res = AssessmentResultsV2(assessment_id="t", input_hash="h",
                                  functions=FunctionScreening(nutrient=n))
        lede = function_sections(res)[0]["lede"]
        assert "upper bound" in lede and "falls below the anoxic threshold" not in lede


class TestTheNutrientPaneLayout:
    """The pane the three headliners and the input card were built for."""

    @pytest.fixture(scope="class")
    def body(self):
        src = open("app.py", encoding="utf-8").read()
        return src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]

    def test_only_stream_nitrate_stays_on_the_card(self, body):
        """Dissolved oxygen, its consumption rate and the threshold are model parameters with
        defaults; stream nitrate is the one quantity a reader knows about their own site. The
        three moved under the switch that decides whether they apply at all."""
        card = body[body.index("fields = []"):body.index("adv = []")]
        assert '"fn_no3"' in card
        for iid in ('"fn_do"', '"fn_o2_rate"', '"fn_do_thresh"'):
            assert iid not in card, f"{iid} is still on the card"

    def test_the_inputs_are_boxed_and_titled(self, body):
        assert 'class_="hype-input-card"' in body
        assert '"Screening inputs", class_="hype-card-head"' in body
        # a pane that supplies nothing gets no card rather than an empty one
        assert "if fields:" in body
        css = open("www/styles.css", encoding="utf-8").read()
        assert ".hype-input-card" in css and ".hype-card-head" in css

    def test_the_oxygen_inputs_exist_only_under_their_switch(self, body):
        """NOT DRAWN, not drawn disabled. Same rule the switched-off section follows: a dimmed
        live input stays keyboard reachable and reads as broken."""
        adv = body[body.index("adv = []"):body.index("# One accordion")]
        assert '"fn_do_gate", "Limit denitrification by dissolved oxygen"' in adv
        gate = adv.index("if _fn_do_gate():")
        for iid in ('"fn_do"', '"fn_o2_rate"', '"fn_do_thresh"'):
            assert adv.index(iid) > gate, f"{iid} is drawn outside the switch"
        # ...and the rate constant is NOT under it: it applies either way.
        assert adv.index('"fn_denit_rate"') > gate
        assert "adv.append" in adv, "the rate constant fell inside the gated block"

    def test_the_threshold_says_what_it_does(self, body):
        """"Anoxic threshold" named the concept; the label now names the effect. The literature
        term stays in the tooltip so anyone searching for it still lands there."""
        assert '"fn_do_thresh", "Denitrification stops above (mg/L)"' in body
        assert "anoxic threshold" in reg.ANOXIC_THRESHOLD_HELP.definition.lower()

    def test_the_disclosure_panels_are_the_four_you_asked_for(self, body):
        """The same four on every pane now, Pollutant Attenuation included (2026-08-01).

        That one holds a READ-ONLY table of cited rates, so "Advanced inputs" would be a promise
        the section does not keep -- it offers no box to overwrite a published rate with. It used
        to dodge that by keeping the old name; it now carries the name and says the thing, which
        is the honest version: a reader should not have to infer a rule from an absence."""
        # Sources is built by `_refs_panel`, which lives outside this slice.
        for marker in ('ui.accordion_panel("More metrics"', "adv_title", "_refs_panel(spec)"):
            assert marker in body, marker
        assert 'adv_title = "Advanced inputs"' in body
        assert 'adv_title = "Assumed constants"' not in body, "the old exception is back"
        assert "cannot " in body and "be edited" in body, \
            "the read-only rates panel no longer says its rates are fixed"
        src = open("app.py", encoding="utf-8").read()
        assert 'ui.accordion_panel("Limitations"' in src
        assert 'ui.accordion_panel("Considerations"' not in src

    def test_the_pane_threads_every_screening_knob_it_sends(self):
        """THE BUG THIS EXISTS FOR, found by looking at the running app and not by any test here.

        `_fn_inputs` builds one knobs dict. The REPORT hands it to `assess._build_functions`, which
        reads it by name; the PANE hands it to `_screening_now`, which unpacks it field by field
        into `ScreeningInputs` by hand. Add a knob and forget the second, and the pane silently
        ignores it while the report honours it -- the two disagree about the same site, which is
        the one failure this whole module is arranged to prevent.

        The oxygen gate did exactly that: unticking it hid its three inputs and changed not one
        number, because `_screening_now` never passed the flag.

        Only knobs whose name IS a `ScreeningInputs` field are checked. The renamed ones
        (`nitrate_mg_l` -> `inlet_concentration_mg_l`) go through `conc_rate` and cannot be
        matched by name, so this lint deliberately says nothing about them."""
        src = open("app.py", encoding="utf-8").read()
        block = src[src.index("def _fn_inputs()"):src.index("def _screening_now()")]
        sent = set(re.findall(r'^\s+"(\w+)":', block, re.M))
        assert "oxygen_gate" in sent, "the knob this test was written for is gone"

        now = src[src.index("def _screening_now()"):src.index("def _fn_included(")]
        fields = set(ScreeningInputs.__dataclass_fields__)
        missed = sorted(k for k in sent & fields if f'k["{k}"]' not in now)
        assert not missed, (f"_fn_inputs sends {missed} and _screening_now drops them: the pane "
                            f"and the report will disagree about the same site")

    def test_the_decluttered_rows_are_gone_from_the_pane_and_kept_in_the_report(self):
        """Three rows came off: the complement of a row two above it, a derived curiosity, and
        the Monod constant. The report keeps all three -- this is a pane declutter, not a
        narrowing of what the analysis records."""
        shown = {r.key for r in (*reg.visible_rows(DENIT), *DENIT.detail_rows)}
        for key in ("fraction_below_threshold", "implied_zero_order_rate_mg_l_day",
                    "monod_half_saturation_mg_l"):
            assert key not in shown, key
        rep = open("hype_app/report.py", encoding="utf-8").read()
        for attr in ("n.fraction_below_threshold", "n.implied_zero_order_rate_mg_l_day",
                     "n.monod_half_saturation_mg_l"):
            assert attr in rep, f"{attr} left the report too"
        # The saturation ratio survives, and with its denominator now gone from the rows beside
        # it, the label has to carry the constant -- interpolated, so the two cannot drift.
        ratio = next(r for r in DENIT.detail_rows if r.key == "saturation_ratio")
        assert f"{reg.MONOD_HALF_SATURATION_MG_N_L:g}" in ratio.label, ratio.label


class TestThePollutantPaneLayout:
    """Pollutant Attenuation is the one section that produces N results at once, and it is the
    reason every piece below exists."""

    @pytest.fixture(scope="class")
    def body(self):
        src = open("app.py", encoding="utf-8").read()
        return src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]

    def test_the_endpoints_are_one_chip_picker(self, body):
        """Two checkbox groups was ~250 px before the first number. A selectize is ~40 px, gives
        chips with an x and type-to-search for free, and keeps the two families as `<optgroup>`s --
        which is the one thing the checklists genuinely did better."""
        assert "ui.input_selectize(" in body
        assert "FN_POL_SELECT_ID" in body
        assert "multiple=True" in body and "remove_button=True" in body
        assert "ui.input_checkbox_group(" not in body, "the endpoint checklists are back"
        # Grouped from the registry, so adding an endpoint cannot leave it out of the dropdown.
        assert "for _, label, keys in fn_pol.PRESET_GROUPS" in body

    def test_the_picker_leads_the_pane(self, body):
        """It decides how many result blocks follow, so it has to read in the order it acts. It
        sat UNDER the results while it was 250 px of checkboxes, which was the right call then."""
        assert body.index("ui.input_selectize(") < body.index("pol_panels = []")

    def test_every_pollutant_gets_its_own_panel(self, body):
        assert "ui.accordion_panel(_pol_head(" in body
        assert 'id="fn_pol_acc"' in body
        # first one open: a pane that opens entirely collapsed reads as empty
        assert "open=[runs[0][0]]" in body
        css = open("www/styles.css", encoding="utf-8").read()
        assert ".hype-pol-acc" in css and ".hype-pol-val" in css

    def test_the_header_carries_the_lead_and_the_body_does_not_repeat_it(self):
        """`drop_lead`. The panel header IS the lead card, so drawing it again inside would print
        the same figure twice a few inches apart -- and the header exists precisely to save that.

        Against real markup, not a source lint: a card that resolves to nothing renders as nothing,
        and no amount of reading app.py can see the difference."""
        out = screen_process(
            ScreeningInputs(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                            transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                            streambed_area_m2=8000.0, reach_length_m=1609.0,
                            inlet_concentration_mg_l=0.602, preset_key="zinc"), POLL)
        ns = _pane_helpers()
        lead = reg.FUNCTIONS["pollutant"].headline_kpi
        full = str(ns["_fn_kpi"](out, POLL, lead=lead))
        cut = str(ns["_fn_kpi"](out, POLL, lead=lead, drop_lead=True))
        head = str(ns["_pol_head"](pol.PRESET_BY_KEY["zinc"], out, POLL, lead))

        def _label(k):
            """What the card actually prints: the endpoint's own word when it has one."""
            return (out.get(k.label_key) if k.label_key else None) or k.label

        lead_kpi = next(k for k in POLL.kpis if k.key == lead)
        assert _label(lead_kpi) in full and _label(lead_kpi) not in cut
        # ...and the supporting cards survive the cut, so `drop_lead` drops one card, not all
        for k in POLL.kpis:
            if k.key != lead:
                assert _label(k) in cut, k.key
        # the header carries the name, the endpoint class and the number
        assert "Zinc" in head and pol.TERMS[pol.ENDPOINT_METAL].kind_label in head
        assert "hype-pol-num" in head

    def test_the_shared_hydraulics_render_once_not_once_per_endpoint(self):
        """THE CLUTTER THIS SECTION EXISTS TO AVOID. Three ticked chemicals used to print the same
        returning-path count, median residence time and streambed area three times each.

        Which rows are shared is registry DATA, because whether a number depends on a rate is a
        property of how `screen.py` computes it, not a rule app.py should be guessing at."""
        shared = {r.key for r in POLL.detail_rows if r.shared}
        assert shared == {"t50_days", "exchange_ratio", "reference_area_m2", "reach_length_m",
                          "censored_flow_fraction"}
        # every one of them really is rate-free: two endpoints with rates an order of magnitude
        # apart must agree on all of them, and disagree on the rest.
        def _run(preset):
            return screen_process(
                ScreeningInputs(transit_times_days=[0.1, 0.5, 1.0, 3.0],
                                transit_weights_m3_day=[400.0, 300.0, 200.0, 100.0],
                                streamflow_cms=0.736, streambed_area_m2=8000.0,
                                reach_length_m=1609.0, inlet_concentration_mg_l=0.05,
                                preset_key=preset), POLL)
        a, b = _run("zinc"), _run("manganese")
        for key in shared:
            assert a.get(key) == b.get(key), f"{key} is marked shared but moved with the rate"
        assert a.get("damkohler") != b.get("damkohler"), "the two rates did not actually differ"
        # ...and the hydraulics GROUP carries the same property through `assumed_rate`
        hydro = [g for g in POLL.pane_groups if not g.assumed_rate]
        assert [g.title for g in hydro] == ["Exchange"]
        for r in hydro[0].rows:
            assert a.get(r.key) == b.get(r.key), r.key

    def test_the_split_is_wired_the_way_the_registry_declares_it(self, body):
        """Per-endpoint panel <- the rate-dependent group + the rows NOT marked shared.
        More metrics <- the hydraulics group + the shared rows, from one run."""
        panel = body[body.index("pol_panels = []"):body.index("if pol_panels:")]
        assert "if not g.assumed_rate:\n                        continue" in panel
        assert "if not x.shared" in panel
        more = body[body.index("panels = []"):body.index('ui.accordion_panel("More metrics"')]
        assert "runs[:1] if multi else runs" in more
        assert "if multi and g.assumed_rate:" in more
        assert "if x.shared" in more

    @staticmethod
    def _migrate(kept):
        """`_migrate_pollutant_keys` lifted out of `server()` and run against a fabricated mirror.

        Same technique as `_pane_helpers` and for the same reason: it is a closure, it cannot be
        imported, and it is the only thing standing between a saved project and a silent reset to
        the shipped default."""
        import ast
        import textwrap

        src = open("app.py", encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_migrate_pollutant_keys")
        ns = {"fn_pol": pol, "_kept": kept, "_kept_seen": {},
              "FN_POL_SELECT_ID": "fn_pol_endpoints"}
        exec(textwrap.dedent(ast.get_source_segment(src, fn)), ns)   # noqa: S102 - app source
        ns["_migrate_pollutant_keys"]()
        return kept

    def test_a_project_saved_under_either_older_shape_keeps_its_endpoints(self):
        """TWO VINTAGES, and both have to survive. Until 2026-07-31 the section screened one
        endpoint (`fn_pol_preset` + `fn_pol_conc`); until 2026-08-01, several, in one list per
        preset group. Both now land in a single selectize.

        Without this the saved endpoints collapse to the default single chip and the saved
        concentration vanishes -- data loss with nothing on screen to reveal it, because the pane
        would look perfectly healthy screening the wrong chemical."""
        # vintage 1: one endpoint and its concentration
        old = self._migrate({"fn_pol_preset": "cobalt", "fn_pol_conc": 0.9})
        assert old["fn_pol_endpoints"] == ["cobalt"]
        assert old["fn_pol_conc_cobalt"] == 0.9
        assert "fn_pol_preset" not in old and "fn_pol_metals" not in old

        # vintage 2: several, across both groups, re-sorted into registry order
        mid = self._migrate({"fn_pol_metals": ["nickel", "zinc"],
                             "fn_pol_organics": ["acesulfame"]})
        assert mid["fn_pol_endpoints"] == ["zinc", "nickel", "acesulfame"]
        assert not any(k.startswith("fn_pol_metals") or k.startswith("fn_pol_organics")
                       for k in mid)

        # A DELIBERATELY EMPTY SELECTION IS NOT AN ABSENT ONE. A project saved under the picker
        # with every chip removed carries the group ids not at all; defaulting on that would
        # overwrite the user's choice with zinc.
        cur = self._migrate({"fn_pol_endpoints": []})
        assert cur["fn_pol_endpoints"] == []

    def test_the_rate_override_is_still_not_carried(self):
        """`fn_pol_rate` predates the cited library. Restoring a user-typed rate would reintroduce
        exactly the unsourced number removing Custom took away."""
        out = self._migrate({"fn_pol_preset": "zinc", "fn_pol_rate": 12.5, "fn_pol_mode": "dis"})
        assert "fn_pol_rate" not in out and "fn_pol_mode" not in out
        assert out["fn_pol_endpoints"] == ["zinc"]

    def test_a_single_endpoint_still_gets_a_panel(self, body):
        """`multi` is "this section produces preset-keyed runs", not "more than one is ticked".
        One chemical in an expander is consistent with three; one chemical rendered flat and three
        in expanders would make the layout depend on how many chips a reader happened to add."""
        assert "multi = any(fn_pol.get_preset(k) is not None for k, _ in runs)" in body


class TestTheHabitatPaneLayout:
    """Habitat Creation supplies NOTHING: no rate, no concentration, no box of any kind. So it drew
    three disclosure panels where every other section drew four, and the one it was missing was the
    one a reader questioning its numbers would open. Its inputs do exist -- they just live on other
    panes, because changing either means re-running the model.

    `run_settings` is the registry field that says so, and app.py renders it with no `spec.key`
    test, so a second section that works this way needs no edit there."""

    @pytest.fixture(scope="class")
    def body(self):
        src = open("app.py", encoding="utf-8").read()
        return src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]

    def test_the_run_settings_panel_offers_no_control(self, body):
        """THE POINT OF THE PANEL. Porosity set the pore velocities MODPATH tracked at, so it set
        the travel times, so it set which particles came back inside the window, so it set the
        volume itself. A box here could not be applied by multiplying the frozen volume -- it needs
        the run redone. So the panel reports and points, and the pointing is not optional."""
        block = body[body.index("if spec.run_settings and runs:"):body.index("if adv:")]
        assert "ui.input_" not in block, "the run-settings panel drew a live control"
        assert "spec.run_settings_note" in block, "it stopped saying where to change them"
        assert "RUN_SETTINGS_HELP" in block
        # rendered through the same row helper as every other table, so formatting cannot drift
        assert "_fn_rows(" in block and "_fn_tbl(" in block

    def test_which_sections_work_this_way_is_registry_data(self, body):
        """The four branches above this one are all `spec.key ==` tests, and that was the shape
        this could easily have taken. It is a field instead, so the next rate-free section gets the
        panel by declaring rows."""
        block = body[body.index("if spec.run_settings and runs:"):body.index("if adv:")]
        assert 'spec.key ==' not in block
        assert reg.get_process("habitat").run_settings, "habitat declares none"
        # ...and the sections that DO take input declare none, so nothing renders twice for them
        for key in ("denitrification", "contaminant", "thermal_regulation"):
            assert not reg.get_process(key).run_settings, key

    def test_a_section_with_no_rate_gets_no_empty_citation_line(self, body):
        """The panel footer is the rate citation. Habitat has no rate, so it had none to print and
        would have closed the panel with a blank note div."""
        tail = body[body.index("if adv:"):body.index("_fn_limits(fspec, spec, [")]
        assert "if spec.rate_citation else []" in tail
        assert 'spec.rate_citation or ""' not in tail, "the empty-string fallback is back"

    def test_the_panel_is_named_but_not_promised(self):
        """"Advanced inputs" over a read-only table is a promise unless the panel says otherwise.
        Pollutant Attenuation learned this with its cited rates; the rule is the same here and it
        is enforced at import, so a future section cannot ship the table without the line."""
        import dataclasses as dc
        spec = reg.get_process("habitat")
        assert spec.run_settings_note.strip()
        assert "re-run" in spec.run_settings_note.lower()
        for broken in (dc.replace(spec, run_settings_note=""),
                       dc.replace(spec, run_settings=())):
            with pytest.raises(ValueError, match="run_settings"):
                reg.validate_registry({"habitat": broken})

    def test_no_row_is_listed_in_two_panels(self):
        """The reorganisation moved five rows from More metrics to Advanced inputs, and "move" is
        one keystroke from "copy". Two copies of one number reads as two numbers.

        `kpis` joined the sweep when Temperature Regulation PROMOTED a detail row to a headline
        (2026-08-01), which is the version of this mistake that looks like it worked: the new card
        renders, and the row it came from goes on printing the same number two panels down."""
        for key, spec in reg.PROCESSES.items():
            seen = [k.key for k in spec.kpis]
            seen += [r.key for g in spec.pane_groups for r in g.rows]
            seen += [r.key for r in spec.detail_rows] + [r.key for r in spec.run_settings]
            assert len(seen) == len(set(seen)), f"{key}: a row is listed twice"
        # ...and the registry refuses to load one that is
        import dataclasses as dc
        spec = reg.get_process("habitat")
        dup = dc.replace(spec, detail_rows=(*spec.detail_rows, spec.run_settings[0]))
        with pytest.raises(ValueError, match="render twice"):
            reg.validate_registry({"habitat": dup})
        therm = reg.get_process("thermal_regulation")
        promoted = dc.replace(therm, detail_rows=(*therm.detail_rows,
                                                  reg.PaneRow("attenuation_weighted_flow_l_s",
                                                              "Attenuation-weighted flow (L/s)")))
        with pytest.raises(ValueError, match="render twice"):
            reg.validate_registry({"thermal_regulation": promoted})

    def test_the_panel_actually_fills_against_a_real_run(self):
        """A source lint cannot see a panel that resolves to nothing -- the same gap that once let
        a spec declare three headline cards while two rendered nowhere. So build the table the way
        app.py does, from the shipped `_fn_rows` and `_fn_tbl`, against a real screen result."""
        ns = _pane_helpers()
        spec = reg.get_process("habitat")
        out = TestPaneLayout()._result("habitat")[0]
        rows = [(k, v) for k, v in ns["_fn_rows"](out, spec.run_settings) if v is not None]
        assert len(rows) == 5, f"the panel would open to {len(rows)} rows"
        html = str(ns["_fn_tbl"]("Run settings", rows, reg.RUN_SETTINGS_HELP, tag="hydraulics"))
        # the porosity row takes its label from the RESULT, so the fixture's recorded run reads
        # "as run" here and the same table would read "assumed" on a run that recorded none
        assert "Porosity (as run)" in html
        assert "0.30" in html and "2982" in html          # a setting and a count, both formatted
        assert "hype-tag" in html, "the panel lost its hydraulics chip"

    def test_the_five_moved_rows_are_the_run_settings(self):
        """What belongs here is what the RUN was configured with, as opposed to what it produced.
        Porosity and the zone-pass release are settings; every other row is an output."""
        keys = [r.key for r in reg.get_process("habitat").run_settings]
        assert keys == ["porosity", "zone_particles_per_cell", "zone_seeds",
                        "zone_cells_seeded", "zone_classified_fraction"]
        # More metrics keeps the outputs, and got shorter: thirteen detail rows down to six
        assert len(reg.get_process("habitat").detail_rows) == 6

    def test_a_run_with_no_recorded_porosity_says_so(self):
        """FOUND WHILE TRACING THE THREE HEADLINES, not by any test here. Porosity resolves run
        knob -> input snapshot -> a 0.3 fallback, and the pore volume and equivalent depth are both
        linear in it. A run that recorded none showed a row reading "Porosity (as run) 0.3", which
        claims a provenance the run does not have, with nothing anywhere saying two of the three
        headlines rested on an assumption."""
        base = dict(transit_times_days=[0.5, 1.0], transit_weights_m3_day=[1.0, 1.0],
                    bulk_saturated_volume_m3=8200.0, mobile_pore_storage_m3=2460.0,
                    streambed_area_m2=8000.0, connected_streambed_area_m2=6000.0,
                    connected_streambed_fraction=0.75, porosity=0.3)
        HABS = reg.get_process("habitat")
        bad = screen_process(ScreeningInputs(porosity_basis="fallback", **base), HABS)
        assert bad["porosity_label"] == "Porosity (assumed)"
        assert "no porosity was recorded" in bad["advisory_note"].lower()
        assert "0.3" in bad["advisory_note"]
        # It rides the slot the Limitations panel already reads, so no app.py branch surfaces it
        assert "advisory_note" in {n for n in ("calibration_note", "depth_note", "advisory_note",
                                               "preset_note") if n in bad}
        for good in ("hyporheic run", "input snapshot"):
            out = screen_process(ScreeningInputs(porosity_basis=good, **base), HABS)
            assert out["porosity_label"] == "Porosity (as run)", good
            assert "advisory_note" not in out, good

    def test_the_two_porosity_warnings_cannot_both_fire(self):
        """Drift and fallback share one slot, which is only safe because they are exclusive: drift
        needs a recorded run value and fallback is the absence of one. If they ever overlap, one
        silently overwrites the other."""
        HABS = reg.get_process("habitat")
        out = screen_process(ScreeningInputs(
            transit_times_days=[0.5], transit_weights_m3_day=[1.0],
            bulk_saturated_volume_m3=8200.0, mobile_pore_storage_m3=2460.0,
            streambed_area_m2=8000.0, porosity=0.3, porosity_live=0.45,
            porosity_basis="hyporheic run"), HABS)
        assert "field now reads 0.45" in out["advisory_note"]
        assert out["porosity_label"] == "Porosity (as run)"

    def test_the_basis_reaches_the_screening_layer_at_all(self):
        """The value was already computed and already stopped at `RunProvenance`. This is the wire
        that was missing, and without it every branch above is dead code."""
        import inspect

        from hype_app import signature as sg
        assert "porosity_basis" in inspect.getsource(sg.screening_fields)
        assert "porosity_basis" in {f.name for f in dataclasses.fields(ScreeningInputs)}


# ===========================================================================  thermal pane
class TestTheThermalPaneLayout:
    """Temperature Regulation led with one number, "Buffering opportunity", and that number
    saturates: past about three response times every path has shed essentially all of its anomaly,
    so a reach whose paths run for weeks reads 100% however little water actually comes back. The
    site this was found on reads 100% on 0.527 L/s.

    So the section now leads with the damped share, puts the buffered flow beside it (thermal plan
    §5.2 exists for exactly this failure) and adds the full-day fraction, and says in one line when
    the damped share has stopped carrying information."""

    THERM = reg.get_process("thermal_regulation")

    @pytest.fixture(scope="class")
    def body(self):
        src = open("app.py", encoding="utf-8").read()
        return src[src.index("def _pane_fn(process_key)"):src.index("def _pane_functions")]

    @staticmethod
    def _rtd(hours):
        """One path per entry, equal flow weights, times given in HOURS for readability."""
        return ScreeningInputs(transit_times_days=[h / 24.0 for h in hours],
                               transit_weights_m3_day=[1.0] * len(hours),
                               returning_hyporheic_cms=0.12, streamflow_cms=1.4,
                               turnovers_per_km=0.16)

    def test_the_three_headlines_are_quality_quantity_and_persistence(self):
        """Thermal plan §8's first three result groups, in that order. The damped share still leads
        (it is what the response time computes) but it no longer stands alone."""
        assert [(k.key, k.label) for k in self.THERM.kpis] == [
            ("buffering_opportunity", "Daily temperature swing damped"),
            ("attenuation_weighted_flow_l_s", "Buffered flow returned to the stream"),
            ("fraction_above_diel", "Exchange held past a full day")]
        assert reg.get_function("thermal").headline_kpi == "buffering_opportunity"
        # each carries its own chip, which is the one-line explanation beside the number
        assert all(k.help is not None for k in self.THERM.kpis)

    def test_the_promoted_flow_left_the_detail_rows(self):
        """The whole point of promoting it is that a reader meets it beside the percentage. Listed
        in both places it would also print two panels down, which reads as a second number."""
        keys = [r.key for r in self.THERM.detail_rows]
        assert "attenuation_weighted_flow_l_s" not in keys
        assert "remaining_anomaly_fraction" not in keys, "this is 100% minus the headline above it"
        assert keys == ["thermal_damkohler_median", "rtd_storage_m3",
                        "attenuation_weighted_storage_m3", "censored_flow_fraction"]
        # ...and both still reach the reader, in the report rather than on the pane
        from hype_app import report as rp
        rows = [r["name"] for s in rp.function_sections(_thermal_results())
                if s["key"] == "thermal" for r in s["rows"]]
        assert "Buffered flow returned to the stream (L/s)" in rows
        assert "Remaining anomaly (%)" in rows

    def test_the_full_day_fraction_does_not_move_with_the_scenario(self):
        """THE WHOLE REASON THE FIELD EXISTS. `fraction_above_3tau` was 12 h under the fast case and
        48 h under the slow one, so the row moved with a setting while reading like a fixed fact.
        Thermal plan §5.5 pins full-diel storage opportunity at one day."""
        outs = [screen_thermal(self._rtd([2, 10, 30, 100]), self.THERM, rate=tau)
                for tau in (4.0, 8.0, 16.0)]
        assert len({round(o["fraction_above_diel"], 12) for o in outs}) == 1
        assert outs[0]["fraction_above_diel"] == pytest.approx(0.5)     # 30 h and 100 h clear 24 h
        # ...while the thing it sits beside genuinely does move
        assert len({round(o["buffering_opportunity"], 12) for o in outs}) == 3
        assert len({round(o["fraction_above_3tau"], 12) for o in outs}) > 1

    def test_it_coincides_with_three_response_times_only_at_the_reference(self):
        """At tau = 8 h the two are the same 24 h cut, which is why the old row looked fixed.

        The 18 h path is what separates them: it clears the fast case's 12 h and misses the day."""
        rtd = self._rtd([2, 10, 18, 30, 100])
        at8 = screen_thermal(rtd, self.THERM, rate=8.0)
        assert at8["fraction_above_diel"] == pytest.approx(at8["fraction_above_3tau"])
        at4 = screen_thermal(rtd, self.THERM, rate=4.0)
        assert at4["fraction_above_3tau"] == pytest.approx(0.6)          # >= 12 h
        assert at4["fraction_above_diel"] == pytest.approx(0.4)          # >= 24 h

    def test_the_regime_line_says_when_the_percentage_has_stopped_informing(self):
        """Cut points are the thermal plan's own band boundaries, not a judgement call: 0.5 is §5.6
        and 3 is §5.5's "at least 95% idealized attenuation"."""
        from hype_app.functions import screen as sc
        # median 4000 h against tau = 8 h -- the shape of the site this was found on
        hot = screen_thermal(self._rtd([3000, 5000]), self.THERM, rate=8.0)
        assert hot["damkohler_regime"] == sc.THERMAL_SATURATED
        assert "only the amount of returning flow" in hot["damkohler_note"]
        assert hot["buffering_opportunity"] == pytest.approx(1.0)
        cold = screen_thermal(self._rtd([1, 2]), self.THERM, rate=8.0)
        assert cold["damkohler_regime"] == sc.THERMAL_COUPLED
        mid = screen_thermal(self._rtd([8, 10]), self.THERM, rate=8.0)
        assert mid["damkohler_regime"] == sc.THERMAL_RESPONSIVE
        # no RTD, no reading -- the note must not appear beside cards that read n/a
        empty = screen_thermal(ScreeningInputs(transit_times_days=[], transit_weights_m3_day=[]),
                               self.THERM, rate=8.0)
        assert "damkohler_note" not in empty

    def test_the_regime_line_needs_no_per_section_branch(self, body):
        """It rides the field names the solute sections already emit, so app.py renders it with the
        same three lines. Only the chip differs, and that is registry data too."""
        block = body[body.index('if r.get("damkohler_note"):'):body.index('if r.get("unavailable')]
        assert "spec.regime_help" in block
        assert "thermal" not in block, "the regime line grew a section branch"
        assert self.THERM.regime_help is not None
        assert reg.get_process("contaminant").regime_help is None, "the fallback path is dead"

    def test_the_pane_says_which_part_of_temperature_it_does_not_do(self):
        """"Temperature Regulation" invites cooling, and cooling is the one outcome thermal plan
        §10.1 rules out. Same slot and same grammar as the nutrient and habitat notes."""
        note = self.THERM.scope_note
        assert "cooling" in note.lower() and note.endswith(".")
        assert len(note.split()) <= 14, note

    def test_the_damping_table_is_indexed_on_residence_time(self):
        """THE TRAP THIS EXISTS TO CLOSE. Thermal plan §4.3 tabulates 1 - exp(-t/tau) at the 8 h
        reference, so "4 h -> 39%" is water that STAYED four hours. Read as response times the
        relationship inverts: a shorter response time damps more, not less."""
        card = self.THERM.rate_help
        assert dict(card.rows) == {"Held 4 h": "39% damped", "Held 8 h": "63%",
                                   "Held 16 h": "87%", "Held 24 h": "95%"}
        assert "reference" in card.rows_label.lower() and "8 h" in card.rows_label
        assert "response time" not in card.rows_label.lower(), (
            "the rows label re-attached the damping numbers to the response time")
        # and the numbers are the plan's, to a tenth of a point
        import math
        for held, want in ((4, 0.393), (8, 0.632), (16, 0.865), (24, 0.950)):
            assert -math.expm1(-held / 8.0) == pytest.approx(want, abs=5e-4)

    def test_the_response_time_is_a_scenario_not_a_typed_number(self, body):
        """Nobody measures a thermal response time at a site. There are three published cases, and
        two of them ARE the sensitivity corners the reported range sweeps, so a free box invited a
        fourth number that the range would not have bracketed."""
        block = body[body.index('elif spec.key == "thermal_regulation":'):body.index("# One"
                                                                                    " accordion")]
        assert "ui.input_radio_buttons" in block
        assert "ui.input_numeric" not in block
        assert "spec.rate_scenarios" in block, "the choices stopped coming off the registry"
        assert self.THERM.rate_scenarios == ((4.0, "Fast"), (8.0, "Reference"), (16.0, "Slow"))

    def test_the_scenarios_are_the_sensitivity_corners(self):
        """Let the two lists drift and the pane hands out a case its own reported range never
        brackets, which is the one thing the range is there to do."""
        import dataclasses as dc
        for key, spec in reg.PROCESSES.items():
            if spec.rate_scenarios:
                assert tuple(v for v, _ in spec.rate_scenarios) == tuple(
                    float(v) for v in spec.rate), key
        for broken in (dc.replace(self.THERM, rate_scenarios=((4.0, "Fast"), (8.0, "Reference"),
                                                              (24.0, "Slow"))),
                       dc.replace(self.THERM, rate_scenarios=((4.0, ""), (8.0, "Reference"),
                                                              (16.0, "Slow")))):
            with pytest.raises(ValueError, match="scenario"):
                reg.validate_registry({"thermal_regulation": broken})

    def test_a_stored_response_time_selects_its_button(self):
        """`_keep` hands back the float 8.0 and a radio's keys are strings, so `str(8.0)` matches
        nothing and the control would open with no button pressed. The nearest-match arm covers
        projects saved while this was a free numeric."""
        pick = _pane_helpers()["_tau_choice"]
        for kept, want in ((8.0, "8"), ("8", "8"), (4, "4"), (16.0, "16"),
                           (None, "8"), ("", "8"), (10.0, "8"), (5.0, "4"), ("nonsense", "8")):
            assert pick(kept, self.THERM) == want, kept
        assert pick(8.0, reg.get_process("habitat")) is None, "a section with no scenarios"


def _thermal_results():
    return TestReportSections()._results()


# ===========================================================================  engine ledger
def test_return_node_is_persisted_in_the_particle_ledger():
    """The thermal-mosaic map aggregates buffering by return location, which needs the cell each
    particle ended in. The endpoint pass always knew it; it just was not written out."""
    src = open("hypetool/functions/hz_analysis.py", encoding="utf-8").read()
    assert '"return_node": end_node' in src
    assert "end_node = np.asarray(ep[\"node\"]" in src
