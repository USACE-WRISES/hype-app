"""Site Summary Report tests (spec §11, §13.6)."""
import csv
import json
from datetime import datetime, timezone

import pytest

from hype_app.contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ConnectivityMetrics,
    GradientBoundaryConfigV2,
    GridSettings,
    KSettings,
    ResidenceTimeMetrics,
    SiteMetadata,
    StreamflowInput,
    ThresholdResult,
    ZoneMetrics,
)
from hype_app.provenance import HypeWarning, Provenance
from hype_app.report import (
    fmt,
    fmt_range,
    fmt_sig,
    generate_report,
    metric_rows,
    render_html,
    results_to_json,
    write_site_metrics_csv,
)


@pytest.fixture
def results():
    snap = AssessmentInputSnapshot(
        assessment_id="A1",
        site=SiteMetadata(site_name="<script>alert(1)</script>", analyst="Ada",
                          organization="USACE", reach_length_m=500.0, notes="pilot reach"),
        streamflow=StreamflowInput(value_cms=2.83, provenance=Provenance(source="USGS StreamStats")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5))
    return AssessmentResultsV2(
        assessment_id="A1", input_hash="a" * 64, input_snapshot=snap,
        connectivity=ConnectivityMetrics(streamflow_cms=2.83, returning_hyporheic_cms=0.283,
                                         excursions_per_mile=0.1, turnover_length_m=16093.0,
                                         mass_balance_error=0.0),
        residence_time=ResidenceTimeMetrics(weighted_median_days=1.5, frac_above_1d=0.6,
                                            p05_days=0.2, p95_days=5.0),
        zone=ZoneMetrics(bulk_saturated_volume_m3=1000.0, mobile_pore_storage_m3=300.0,
                         footprint_binary_m2=500.0, thickness_mean_m=1.2, thickness_max_m=2.4),
        thresholds=[
            ThresholdResult(threshold_value_h=1.0, threshold_label="Rapid-exposure scenario",
                            flow_exceedance_fraction=0.8, functional_exchange_m3_s=0.226,
                            functional_connectivity_per_km=0.12),
            ThresholdResult(threshold_value_h=6.0, threshold_label="Intermediate-exposure scenario",
                            flow_exceedance_fraction=0.5, functional_exchange_m3_s=0.141,
                            functional_connectivity_per_km=0.075)],
        warnings=[HypeWarning(code="extrap", message="Flow statistic extrapolated <check>")],
        untested_uncertainty=["K and soil configuration", "Streamflow"],
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc))


class TestSignificantFigureFormatting:
    """`fmt` rounds to DECIMAL PLACES, which is right for the report's geometry but destroys the
    screening masses: it rendered a genuine 0.5% sensitivity spread as "0.068 to 0.068", which
    reads as a broken widget rather than a narrow range."""

    def test_it_separates_bounds_that_decimal_rounding_collapses(self):
        lo, hi = 0.06811838, 0.06848999           # the real bounds from the reported run
        assert fmt(lo) == fmt(hi) == "0.068"      # why the helper exists
        assert fmt_sig(lo) == "0.0681"
        assert fmt_sig(hi) == "0.0685"

    def test_it_preserves_the_behaviours_fmt_consumers_rely_on(self):
        assert fmt_sig(None) == "n/a"
        assert fmt_sig(float("nan")) == "n/a"
        assert fmt_sig(0) == "0"
        assert fmt_sig(0.0) == "0"
        assert fmt_sig(True) == "yes"
        assert fmt_sig(1.2345e-05) == "1.23e-05"  # scientific only for tiny magnitudes
        assert fmt_sig(16093.0) == "16093"        # plain integer, CSV-safe, no commas
        assert fmt_sig(8200000.0) == "8200000"
        for v in (None, 0, 1.5, float("nan"), 16093.0):
            assert "—" not in fmt_sig(v)     # never an em dash

    def test_it_holds_three_figures_across_orders_of_magnitude(self):
        assert fmt_sig(0.000712) == "0.000712"
        assert fmt_sig(0.0681) == "0.0681"
        assert fmt_sig(2.41) == "2.41"
        assert fmt_sig(51.4321) == "51.4"
        # trailing zeros are intentional: that is what three significant figures means, and it
        # keeps a column of masses aligned
        assert fmt_sig(0.3) == "0.300"
        assert fmt_sig(2.4) == "2.40"

    def test_range_collapses_only_when_the_bounds_really_agree(self):
        assert fmt_range(0.06811838, 0.06848999) == "0.0681 to 0.0685"
        # floats differ in the last bit, strings do not: float equality would print
        # "0.300 to 0.300", which is the exact bug being fixed
        assert fmt_range(0.299999998867, 0.3) == "0.300"
        assert fmt_range(0.0, 0.0) == "0"
        assert fmt_range(None, 1.0) is None
        assert fmt_range(1.0, None) is None

    def test_fmt_itself_is_unchanged(self):
        """A golden list, so the additive helper cannot quietly migrate into `fmt` later and
        change the report, the CSV and the machine summary underneath their own tests."""
        assert [fmt(v) for v in (None, 0, 0.1, 0.736, 2460.0, 16093.0, 0.06811838,
                                 1.2345e-05, 51.4321)] == \
            ["n/a", "0", "0.1", "0.736", "2460", "16093", "0.068", "1.23e-05", "51.432"]


def test_metric_rows_cover_sections(results):
    sections = {r["section"] for r in metric_rows(results)}
    assert {"Frequency of Hyporheic Exchange", "Duration in Hyporheic Zone",
            "Extent of Hyporheic Zone"} <= sections
    exc = next(r for r in metric_rows(results) if r["name"] == "Excursions per mile")
    assert exc["value"] == fmt(0.1)


def test_csv_matches_model(results, tmp_path):
    p = write_site_metrics_csv(results, tmp_path / "m.csv")
    with open(p, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_name = {r["metric"]: r["value"] for r in rows}
    assert by_name["Excursions per mile"] == fmt(0.1)
    assert by_name["Active hyporheic volume"] == fmt(1000.0)


def test_headline_cards_read_only_and_agree(results):
    """Three cards, in the three-dimension order, built purely from the results model."""
    from hype_app.report import headline_cards
    cards = headline_cards(results)
    assert [c["dimension"] for c in cards] == [
        "Frequency of Hyporheic Exchange", "Duration in Hyporheic Zone",
        "Extent of Hyporheic Zone"]
    # supporting values agree with the model (gross exchange = returning_cms * 1000 L/s)
    ex = cards[0]
    gross = next(v for (lab, v, u) in ex["supporting"] if lab == "Gross hyporheic exchange")
    assert gross == fmt(0.283 * 1000.0)
    for c in cards:                                   # every card carries full text
        assert c["definition"] and c["relevance"] and c["primary_unit"]


def test_duration_card_carries_the_percentiles_as_rows(results):
    """2026-08-02: P10 and P90 moved off the headline sub-line into supporting rows, and the
    censored-flow QC number left the card entirely (it stays in the detailed metrics)."""
    from hype_app.report import headline_cards
    dur = headline_cards(results)[1]
    assert dur["primary_range"] is None
    labels = [lab for lab, _v, _u in dur["supporting"]]
    assert labels == ["Fraction over 1 day", "Residence time P10", "Residence time P90"]
    assert "P10 to P90" not in dur["definition"]
    # the row names are the metric_rows vocabulary, so card and table cannot word them apart
    names = {r["name"] for r in metric_rows(results)}
    assert {"Residence time P10", "Residence time P90", "Censored fraction"} <= names


def test_html_self_contained_and_escaped(results):
    html = render_html(results, app_version="2026.07")
    # values agree with the canonical model / CSV
    assert fmt(0.1) in html
    # user text is escaped (no raw script tag)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    # self-contained: nothing is FETCHED from the network — no external stylesheet/script/img/font
    # (citation URLs printed as plain text are fine; the page still renders fully offline).
    import re
    assert not re.search(r'<(?:script|img|link)\b[^>]*\b(?:src|href)\s*=\s*["\']https?://', html)
    assert "@import" not in html and "url(http" not in html
    assert "<style>" in html            # inline CSS


def test_rtd_figure_renders_and_embeds():
    """§8.5: a weighted RTD figure (ECDF + log histogram) is produced from returning transit
    rows and embedded in the HTML as a data URI; too-few points yields no figure (no crash)."""
    import numpy as np

    from hype_app.report import render_rtd_figure
    png = render_rtd_figure(np.geomspace(0.1, 100, 200), np.ones(200))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"      # real PNG magic bytes
    assert render_rtd_figure([1.0], [1.0]) is None       # <2 points -> no figure
    assert render_rtd_figure([], None) is None


def test_rtd_x_ticks_are_plain_and_never_overlap():
    """The RTD x axis must stay legible at any residence-time span. matplotlib 3.11 labels
    minor log ticks (2x10^n, 3x10^n, ...) whenever the span is under ~1.5 decades, which
    smeared the Mink Brook report (~20..500 days) into unreadable overlap. _log_time_ticks
    pins the fix: majors only, plain numbers, no two drawn labels touching."""
    import matplotlib
    matplotlib.use("Agg")
    import re

    import matplotlib.pyplot as plt
    import numpy as np

    from hype_app.report import _log_time_ticks
    for lo, hi in ((20.0, 500.0), (0.2, 550.0), (200.0, 350.0)):
        t = np.geomspace(lo, hi, 300)
        fig, ax = plt.subplots(figsize=(3.2, 2.6))
        ax.step(t, np.linspace(0, 1, t.size), where="post")
        ax.set_xscale("log")
        _log_time_ticks(ax, t.min(), t.max())
        fig.tight_layout()
        fig.canvas.draw()
        assert not [l for l in ax.get_xticklabels(minor=True) if l.get_text()], (lo, hi)
        majors = [l for l in ax.get_xticklabels() if l.get_text()]
        assert majors, (lo, hi)
        for l in majors:
            assert re.fullmatch(r"[0-9.]+", l.get_text()), (lo, hi, l.get_text())
        boxes = [l.get_window_extent() for l in majors if l.get_window_extent().width > 0]
        for i, b1 in enumerate(boxes):
            for b2 in boxes[i + 1:]:
                assert not b1.overlaps(b2), (lo, hi)
        plt.close(fig)


def test_generate_report_embeds_rtd_figure(results, tmp_path):
    from hype_app.report import generate_report
    rows = [{"particle_id": i, "source_cell": i, "flow_weight": 1.0,
             "endpoint_class": "returning", "transit_time_days": 0.1 * (i + 1),
             "termination": 2} for i in range(30)]
    paths = generate_report(results, tmp_path, transit_rows=rows)
    assert (tmp_path / "rtd_distribution.png").exists()
    assert "data:image/png;base64," in (tmp_path / "site_report.html").read_text()
    assert "pdf_error" not in paths


def test_report_covers_all_spec_sections(results):
    """§11.3: the report must document inputs, data sources/provenance, software, and
    references — not just the metric tables."""
    from hype_app.report import (
        data_source_rows, input_rows, report_references)
    irows = input_rows(results)
    assert any(r["name"] == "Porosity" for r in irows)
    assert any(r["section"] == "Grid" for r in irows)
    dsrc = data_source_rows(results)
    assert any(r["item"] == "Streamflow" and "USGS" in r["source"] for r in dsrc)
    refs = report_references(results)
    assert any("Harvey" in (r.get("authors") or "") for r in refs)
    html = render_html(results, app_version="2026.07", model_version="MODFLOW 6")
    for heading in ("Key Hyporheic Hydraulic Metrics", "Residence Time Exceedance",
                    "Model inputs and assumptions"):
        assert heading in html, f"HTML missing section: {heading}"
    # sections removed per user request
    assert "Interpretation and limitations" not in html
    assert "Software and references" not in html
    assert "Potential functional opportunity" not in html
    assert "Data sources and provenance" not in html
    assert "USACE" in html and "500" in html      # analyst org + reach length surfaced


def test_report_has_no_em_dash(results):
    """Standing rule: no em dash (U+2014) in user-facing report copy."""
    html = render_html(results, app_version="2026.07", model_version="MODFLOW 6")
    assert "—" not in html


def test_should_autoopen_fires_once_per_run():
    from hype_app.report import should_autoopen
    assert should_autoopen(None, "hash1") is True       # first run -> open
    assert should_autoopen("hash1", "hash1") is False    # same run -> do not reopen
    assert should_autoopen("hash1", "hash2") is True     # new run -> open
    assert should_autoopen("hash1", None) is False       # no current run -> nothing


def test_threshold_bar_figure():
    from hype_app.contracts import ThresholdResult
    from hype_app.figures import render_threshold_bar
    ths = [ThresholdResult(threshold_value_h=h, flow_exceedance_fraction=f)
           for h, f in ((1.0, 0.8), (6.0, 0.5), (12.0, 0.3), (24.0, 0.1))]
    png = render_threshold_bar(ths)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert render_threshold_bar([]) is None


def test_custom_scenario_widget_removed(results, tmp_path):
    """The interactive custom-threshold recompute widget was removed per user request: its input,
    output spans, embedded RTD blob, and prose must not appear in the report HTML."""
    from hype_app.report import generate_report
    rows = [{"particle_id": i, "source_cell": i, "flow_weight": 1.0,
             "endpoint_class": "returning", "transit_time_days": 0.1 * (i + 1),
             "termination": 2} for i in range(20)]
    generate_report(results, tmp_path, transit_rows=rows)
    html = (tmp_path / "site_report.html").read_text(encoding="utf-8")
    assert 'id="hype-rtd"' not in html and 'id="fx-h"' not in html
    assert "Custom scenario" not in html
    # the residence-time exceedance table survives, under its renamed heading
    assert "Residence Time Exceedance" in html


def test_figures_compact_with_lightbox(results):
    """Report figures render as height-capped previews (tightest on the site-map grid,
    then the 3-D view, then a blanket cap) with a self-contained click-to-enlarge
    lightbox: inline script only, so the downloaded file still works offline."""
    html = render_html(results, app_version="2026.07")
    assert "max-height:330px" in html      # 2x2 site-map grid cells
    assert "max-height:400px" in html      # 3-D isometric (figure.wide)
    assert "max-height:460px" in html      # blanket cap on every figure
    assert "cursor:zoom-in" in html
    assert 'class="lightbox"' in html and 'id="figzoom"' in html
    import re
    assert re.search(r"<script>", html)
    assert not re.search(r"<script[^>]*\bsrc\s*=", html)


def test_generate_report_embeds_site_maps(results, tmp_path, monkeypatch, fake_spatial):
    """§10: with a spatial bundle the report gains the Site Maps section (2x2 grid + 3-D
    view) and the flow-path plan view, writes the PNG sidecars, and keeps rendering with
    the basemap fetch offline. New copy obeys the no-em-dash rule."""
    monkeypatch.setattr("hype_app.mesh.fetch_basemap_image", lambda *a, **k: None)
    rows = [{"particle_id": i, "source_cell": i, "flow_weight": 1.0,
             "endpoint_class": "returning", "transit_time_days": 0.1 * (i + 1),
             "termination": 2} for i in range(20)]
    out = tmp_path / "report"
    paths = generate_report(results, out, transit_rows=rows, spatial=fake_spatial,
                            project_name="MinkTest")
    assert "pdf_error" not in paths, paths.get("pdf_error")
    html = (out / "site_report.html").read_text(encoding="utf-8")
    assert "Site Maps" in html
    assert "Hyporheic flow paths (plan view)" in html
    # 2 site-map grid rows + 2 paired figure rows (planview+paths, section+threshold)
    assert html.count('class="maps"') == 4
    assert "Site Summary Report" in html and "Hyporheic Exchange Assessment" in html
    assert "Modeled discharge" in html
    assert "model layer 1" in html                 # fake_spatial carries head_layer=1
    assert "—" not in html
    for fname in ("site_map_topo.png", "site_map_imagery.png", "site_map_wse.png",
                  "site_map_head.png", "site_map_3d.png", "flowpaths_planview.png"):
        assert (out / fname).exists() and (out / fname).stat().st_size > 0, fname


def test_report_title_chips_and_location(results):
    """Title prefers the site name, falls back to the project name; the chips gain a
    Location entry (mid-reach lat/lon) and a Project entry when it adds information."""
    from hype_app.contracts.flow import LatLon

    site = results.input_snapshot.site
    site.upstream_point = LatLon(lat=38.10000, lon=-80.90000)
    site.downstream_point = LatLon(lat=38.20000, lon=-81.10000)

    html = render_html(results, project_name="MinkTest")
    # fixture site_name is set, so it wins the title; the project shows as a chip
    assert "Site Summary Report</h1>" in html
    assert "MinkTest" in html
    assert "38.15000°N, 81.00000°W" in html

    site.site_name = None                           # no site name -> project takes the title
    html2 = render_html(results, project_name="MinkTest")
    assert "MinkTest: Site Summary Report" in html2


def test_json_roundtrips_and_agrees(results):
    js = results_to_json(results)
    data = json.loads(js)
    assert data["connectivity"]["excursions_per_mile"] == 0.1
    # rebuild from JSON -> equal model
    assert AssessmentResultsV2.model_validate(data).connectivity.excursions_per_mile == 0.1


def test_run_summary_schema_and_derivation(results):
    """§25: flat machine summary derives from the model (units in field names), reserving the
    default-threshold columns + a nested threshold array for cross-site combination."""
    from hype_app.report import RUN_SUMMARY_SCHEMA_VERSION, run_summary_dict
    s = run_summary_dict(results, app_version="2026.07", model_version="MODFLOW 6")
    assert s["schema_version"] == RUN_SUMMARY_SCHEMA_VERSION
    assert s["gross_hyporheic_exchange_l_s"] == pytest.approx(0.283 * 1000.0)
    assert s["residence_time_p50_hr"] == pytest.approx(1.5 * 24.0)   # fixture median 1.5 d
    assert s["app_version"] == "2026.07" and s["model_version"] == "MODFLOW 6"
    assert isinstance(s["threshold_results"], list)
    for key in ("connectivity_turnovers_per_km", "equivalent_active_depth_m",
                "active_streambed_percent", "model_warning_count"):
        assert key in s


def test_run_summary_json_is_strict(results, tmp_path):
    """write_run_summary_json must produce parseable JSON even with inf/NaN metrics present."""
    from pathlib import Path

    from hype_app.report import write_run_summary_json
    results.connectivity.turnover_length_km = float("inf")      # infinite when no exchange
    p = write_run_summary_json(results, tmp_path / "run_summary.json")
    data = json.loads(Path(p).read_text(encoding="utf-8"))      # no ValueError -> strict JSON
    assert data["turnover_length_km"] is None                   # inf sanitized to null


def test_generate_report_writes_all_formats(results, tmp_path):
    transit = [{"particle_id": 0, "source_cell": 5, "flow_weight": 0.5,
                "endpoint_class": "hyporheic", "transit_time_days": 1.2, "termination": "river"}]
    paths = generate_report(results, tmp_path, transit_rows=transit, app_version="2026.07")
    assert "pdf_error" not in paths, paths.get("pdf_error")
    for key in ("json", "html", "csv_metrics", "csv_transit", "pdf"):
        from pathlib import Path
        assert Path(paths[key]).exists() and Path(paths[key]).stat().st_size > 0
    # PDF is a real PDF
    from pathlib import Path
    assert Path(paths["pdf"]).read_bytes().startswith(b"%PDF")
    # transit CSV has the per-particle row
    with open(paths["csv_transit"], encoding="utf-8") as fh:
        trows = list(csv.DictReader(fh))
    assert trows[0]["endpoint_class"] == "hyporheic"


# ======================================================================  two documents
@pytest.fixture
def screened(results):
    """`results` with a screening layer attached. The base fixture has none, so Part B never
    renders from it and the split would be untestable against it."""
    from hype_app.contracts import (ContaminantScreening, FunctionScreening, HabitatScreening,
                                    NutrientScreening, ThermalOpportunity)

    results.functions = FunctionScreening(
        nutrient=NutrientScreening(
            process_label="Nutrient Cycling", kinetics="first_order", n_paths=900,
            removal_efficiency=0.24, areal_removal_rate_g_m2_day=0.30, reference_area_m2=8000.0,
            total_removed_kg_day=2.41, inlet_concentration_mg_l=1.0, rate_value=1.22,
            citation="Zarnetske et al. (2011)", transferability_note="One Oregon stream."),
        pollutant=ContaminantScreening(
            process_label="Pollutant Attenuation", contaminant_name="Atrazine",
            citation="User supplied.", transferability_note="Match the setting."),
        habitat=HabitatScreening(
            process_label="Habitat Creation", habitable_pore_volume_m3=2460.0,
            equivalent_active_depth_m=1.025, connected_streambed_fraction=0.59,
            citation="Framework 7.5", transferability_note="Not habitat quality."),
        thermal=ThermalOpportunity(
            process_label="Temperature Regulation", response_time_hours=8.0,
            buffering_opportunity=0.63, citation="Marzadri et al. (2013)",
            transferability_note="Opportunity, not degrees."))
    return results


def _docs(res):
    """The three renders that matter: combined, hydraulics-only, screening-only."""
    kw = dict(app_version="2026.07", model_version="MODFLOW 6")
    return (render_html(res, **kw),
            render_html(res, include_functions=False, **kw),
            render_html(res, include_hydraulics=False, **kw))


def test_the_two_documents_are_disjoint(screened):
    """Revision spec §9.4: a complete hydraulic signature must never depend on chemistry. The
    split makes that structural, so the hydraulics document must carry no screening estimate and
    the screening document must carry no signature derivation."""
    _, hyd, scr = _docs(screened)
    assert ">Part B<" not in hyd
    assert ">Part B<" in scr
    # the sections themselves, not just the part headers
    assert "<h2>Key Hyporheic Hydraulic Metrics</h2>" in hyd
    assert "<h2>Key Hyporheic Hydraulic Metrics</h2>" not in scr


def test_quality_warnings_show_only_when_there_are_any(results):
    """The model-quality table went away with the declutter, but a real QC warning must still
    reach the reader. It now renders only when the model carries one, so a clean run shows
    nothing instead of a reassuring "No quality warnings recorded." line."""
    warned = render_html(results.model_copy(update={"warnings": [
        HypeWarning(code="qc", message="Mass balance out of range.")]}))
    assert "Mass balance out of range." in warned and 'class="warn"' in warned
    clean = render_html(results.model_copy(update={"warnings": []}))
    assert 'class="warn"' not in clean and "<ul></ul>" not in clean


def test_the_declutter_removed_the_scaffolding_sections(screened):
    """2026-08-02 declutter: the regime narrative, the turnover-definition block, and the
    Part A header + model-quality table are gone from the hydraulics document. The numbers
    they carried live on in the detailed metric tables (metric_rows)."""
    _, hyd, _ = _docs(screened)
    for gone in (">Part A<", "Exchange Regime", "How Turnover Is Defined",
                 "Model Quality and Run Summary", "No quality warnings recorded"):
        assert gone not in hyd, gone
    rows = {(r["section"], r["name"]) for r in metric_rows(screened)}
    from hype_app.report import DIM_DURATION, DIM_FREQUENCY
    assert (DIM_FREQUENCY, "Returning flow fraction") in rows
    assert (DIM_DURATION, "Effective particle count") in rows


def test_both_documents_still_say_which_site_they_are_for(screened):
    """A standalone screening report handed to someone else is worthless without the header."""
    for html in _docs(screened):
        assert 'class="fact"' in html
        assert "USACE" in html


def test_the_screening_document_recaps_what_it_rests_on(screened):
    """Standing alone it would otherwise assert transformation rates with no visible basis, which
    is the §9.3 modelled-versus-inferred distinction the panes make on screen."""
    both, hyd, scr = _docs(screened)
    assert "The Signature These Estimates Rest On" in scr
    # NOT in the combined document: Part A is directly above, so a recap would be a duplicate.
    assert "The Signature These Estimates Rest On" not in both
    assert "The Signature These Estimates Rest On" not in hyd


def test_neither_run_document_carries_the_conceptual_figure(screened):
    """It is its own report product now (`concept_html`), because it is STATIC: it describes the
    framework and says nothing about this run, so reprinting it made the screening document's
    first screen the one part of it that never changed."""
    scr = render_html(screened, include_hydraulics=False, app_version="2026.07")
    hyd = render_html(screened, include_functions=False, app_version="2026.07")
    for doc in (scr, hyd):
        assert 'figure class="diagram"' not in doc
        assert "data:image/svg+xml" not in doc
    # the screening document still opens on the recap of what the estimates rest on
    assert scr.index("The Signature These Estimates Rest On") < scr.index("Nutrient Cycling")


def test_the_per_function_signature_table_is_gone(screened):
    """It restated the three dimensions once per section, under a conceptual figure that had just
    explained them. The figure is the frame; the sections are the numbers."""
    from hype_app.report import function_sections

    assert all("signature_reads" not in s for s in function_sections(screened))
    for doc in _docs(screened):
        assert "What it buys this estimate" not in doc


def test_generate_report_writes_both_documents(screened, tmp_path):
    from pathlib import Path

    paths = generate_report(screened, tmp_path, app_version="2026.07")
    assert not [k for k in paths if k.endswith("_error")], paths
    for key in ("html", "pdf", "screening_html", "screening_pdf"):
        p = Path(paths[key])
        assert p.exists() and p.stat().st_size > 0, key
    assert Path(paths["screening_pdf"]).read_bytes().startswith(b"%PDF")
    # the split reaches the FILES, not only the in-memory renders
    assert ">Part B<" not in Path(paths["html"]).read_text(encoding="utf-8")
    assert ">Part A<" not in Path(paths["screening_html"]).read_text(encoding="utf-8")


def test_the_tree_carries_the_two_reports_as_separate_nodes():
    """The split lives in the tree, so the tree is the switcher and the modal needs no tab strip."""
    from hype_app import ui_tree

    assert ui_tree.NODE["report"]["label"] == "Site Reports"
    assert ui_tree.NODE["report"]["group"] is True
    for nid, label in (("report.hyd", "Hydraulics Report"),
                       ("report.fn", "Functional Screening Report")):
        assert ui_tree.NODE[nid]["parent"] == "report"
        assert ui_tree.NODE[nid]["label"] == label
        assert ui_tree.NODE[nid]["group"] is False
        assert ui_tree.NODE_STEP[nid] == ui_tree.NODE_STEP["report"]
        assert nid not in ui_tree.NODE_LAYERS          # a document is not a map layer
    # the group id survives, so a project last viewed on "report" still reopens somewhere real
    assert "report" in ui_tree.NODE


def test_every_report_node_has_a_pane_and_a_prereq():
    src = open("app.py", encoding="utf-8").read()
    for nid in ("report", "report.hyd", "report.fn"):
        assert f'"{nid}"' in src, nid
    # each document node names which document it opens, in one table rather than per-branch
    assert "REPORT_DOCS = {" in src
    assert '"report.hyd": ("open_report_hyd", "hydraulics"' in src
    assert '"report.fn": ("open_report_fn", "screening"' in src
    # and the modal takes the document rather than showing both behind tabs
    assert "def _report_modal(res, paths, doc=" in src
    assert "hype-doc-tabs" not in src


def test_the_downloads_live_on_one_surface_only():
    """`ui.download_button` registers an OUTPUT, and the report pane stays mounted behind an open
    modal, so an id used in both binds twice and Shiny raises "Duplicate output IDs were found".

    The panes used to carry their own PDF and HTML beside the modal's, which is what forced a
    parallel id set to exist at all. They render none now, so the rule holds by construction
    rather than by keeping two lists apart -- and this asserts the construction, not the lists."""
    import re

    src = open("app.py", encoding="utf-8").read()
    ids = re.findall(r'ui\.download_button\("(dl_\w+)"', src)
    assert len(ids) == len(set(ids)), \
        f"bound twice: {sorted(k for k in set(ids) if ids.count(k) > 1)}"
    for did in ids:
        assert f"def {did}():" in src, did
    # ...and every REPORT download is inside the modal footer. `dl_save` is the project bundle and
    # belongs to the header, so the prefixes scope this to the three documents.
    report_ids = {i for i in ids
                  if i.startswith(("dl_report", "dl_screening", "dl_concept"))}
    modal = src[src.index("def _report_modal"):src.index("def _flux_metrics")]
    in_modal = set(re.findall(r'ui\.download_button\("(dl_\w+)"', modal))
    assert in_modal, "the modal footer lost its downloads"
    assert report_ids == in_modal, \
        f"a report download escaped the modal: {sorted(report_ids - in_modal)}"
    # the report panes render no download of any kind
    panes = src[src.index("def _report_status(nid)"):src.index("def _pane_chanmod()")]
    assert "ui.download_button" not in panes
    assert "dl_" not in panes, "a download id is back on a report pane"
    assert not re.search(r'\("dl_\w+", "(?:PDF|HTML)"\)', src), \
        "REPORT_DOCS grew a download column again"


def test_the_site_reports_hub_is_one_row_per_document():
    """The decluttering, as a lint. A Generate button, an include-functions checkbox and a
    temporary-storage warning all sat around what should be a way into each document."""
    src = open("app.py", encoding="utf-8").read()
    block = src[src.index("def _report_status(nid)"):src.index("def _pane_chanmod()")]
    assert "report_include_functions" not in src, "the checkbox is now one toggle per screening"
    assert "gen_report_evt" not in src, "the Open buttons build when stale"
    assert "temporary storage" not in block
    assert "Two documents are built together" not in src
    # what stays: a row per document off REPORT_DOCS, each document's own Open, and the busy row
    assert "_report_row(nid) for nid in REPORT_DOCS" in block
    assert "_evt_btn(open_id" in block and "_report_busy()" in block


def test_the_site_details_are_rendered_exactly_once():
    """They used to be on all three report panes, so the same five fields appeared four times in
    one branch of the tree. The hub owns them now; the document panes do not."""
    src = open("app.py", encoding="utf-8").read()
    block = src[src.index("def _report_status(nid)"):src.index("def _pane_chanmod()")]
    assert block.count("_report_controls()") == 2, \
        "expected the definition plus exactly one call, from the hub"
    group = block[block.index("def _pane_report_group()"):block.index("def _pane_report_doc(nid)")]
    assert "_report_controls()" in group
    doc = block[block.index("def _pane_report_doc(nid)"):block.index("def _report_busy()")]
    assert "_report_controls()" not in doc


def _doc_pane_block():
    """The `_pane_report_doc` factory as source text, which is the only way to see it.

    The panes are closures inside `server()` and cannot be imported, so the shape rules below are
    lints on the slice. `_report_busy` is the next definition and bounds it."""
    src = open("app.py", encoding="utf-8").read()
    return src[src.index("def _pane_report_doc(nid)"):src.index("def _report_busy()")]


def test_a_report_pane_is_three_blocks_at_most():
    """THE DECLUTTERING, as a lint. Each of the three document panes is a blurb, a status, and one
    way forward. It used to be a paragraph, a button, and a Download heading with two more buttons
    under it -- three stacked blocks for what is one action."""
    doc = _doc_pane_block()
    assert doc.count('class_="hype-instr"') == 1
    assert doc.count('class_="hype-props-note"') == 1
    assert doc.count('class_="hype-actions"') == 1
    # the Download row and its heading, both gone
    assert "hype-report-actions" not in doc
    assert "_fn_head(" not in doc, "a section heading is back on a pane with one button"
    # exactly one of jump / busy / button, so the three cannot stack
    assert doc.count("_next_hint(") == 2, "the two blocked destinations, in one if/else"
    assert doc.count("_report_busy()") == 1 and doc.count("_evt_btn(") == 1


def test_the_status_line_shows_whether_or_not_the_report_is_ready():
    """It used to render only in the blocked branch and return early, so a ready pane said nothing
    about what it was ready WITH. On the screening report that line carries the module count."""
    doc = _doc_pane_block()
    head = doc[:doc.index("if not ok:")]
    assert '_report_status(nid)' in head and 'class_="hype-props-note"' in head, \
        "the status moved back inside a branch"
    assert "return ui.TagList(*parts)" not in doc[:doc.index("elif doc !=")], \
        "the blocked branch returns early again, so the tail cannot be reached"


def test_every_report_blurb_is_one_short_sentence():
    """Driven off the table rather than the three literals, so a fourth document cannot arrive
    with a paragraph. Fifteen words is the cap the screening `limits` bullets already use."""
    import ast

    src = open("app.py", encoding="utf-8").read()
    table = src[src.index("REPORT_DOCS = {"):src.index("def _report_status(nid)")]
    rows = ast.literal_eval(table[table.index("{"):table.rindex("}") + 1])
    assert set(rows) == {"report.concept", "report.hyd", "report.fn"}
    for nid, row in rows.items():
        assert len(row) == 4, f"{nid}: expected (open id, doc, title, blurb), got {len(row)}"
        blurb = row[3]
        assert len(blurb.split()) <= 15, f"{nid}: blurb is {len(blurb.split())} words"
        assert blurb.endswith("."), nid
        assert blurb.count(".") <= 2, f"{nid}: more than two sentences"
        assert "—" not in blurb, nid


def test_the_open_button_rebuilds_a_stale_document():
    """THIS IS WHERE THE GENERATE BUTTON WENT. Freshness is the app's job: a concentration edited
    after the last build changes what the screening report should say, and asking a reader to
    notice that and press a separate button is asking them to track state the app already tracks."""
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _open_built_report(doc)"):src.index("def _REPORT_FILES")
               if "def _REPORT_FILES" in src else src.index("_REPORT_FILES = {")]
    assert "_report_stale(doc)" in body and "_start_report_build(doc)" in body
    # the signature covers the run, the screening knobs and the site metadata
    sig = src[src.index("def _report_signature()"):src.index("def _report_stale(")]
    for part in ("input_hash", "_fn_inputs()", "_site_metadata()"):
        assert part in sig, part
    # ...and the completed build opens whichever document was asked for, not always hydraulics
    done = src[src.index("def _report_done()"):src.index("def _open_built_report(doc)")]
    assert 'out.get("doc")' in done and "doc=doc" in done


def test_the_screening_document_drops_when_nothing_is_switched_on():
    """Spec §9.4 in the other direction: Part B is droppable and Part A never is. An empty
    screening document would assert a heading over nothing."""
    src = open("app.py", encoding="utf-8").read()
    assert '"include_functions": res.functions is not None' in src
    # and the readiness answer says so, rather than offering a button that builds nothing. ONE
    # answer, shared: the hub row and the document's own node used to decide this separately.
    status = src[src.index("def _report_status(nid)"):src.index("def _report_row(nid)")]
    assert "for k in fn_reg.SECTION_ORDER if _fn_included(k)" in status
    assert "No screenings are switched on." in status
    for block in ("def _pane_report_group()", "def _pane_report_doc(nid)"):
        body = src[src.index(block):src.index(block) + 2000]
        assert "_report_status(nid)" in body, block
