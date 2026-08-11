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
    function_sections,
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
    # site_report.html is the hydraulics-only build, so it now titles itself as such rather
    # than as the combined document it is not.
    assert "Hydraulics Report" in html and "Hyporheic Exchange Assessment" in html
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
    both, hyd, scr = _docs(screened)
    # The "Part B" eyebrow marks the modelled-versus-inferred break, so it belongs to the COMBINED
    # document only: standing alone, the screening report has nothing above it to break from and
    # its own title already names it.
    assert ">Part B<" in both
    assert ">Part B<" not in hyd and ">Part B<" not in scr
    # the sections themselves, not just the part headers. The screening document DOES carry the
    # three signature cards, under the same heading Part A gives them, because it would otherwise
    # assert transformation rates with no visible basis. What it must not carry is the derivation.
    assert "<h2>Key Hyporheic Hydraulic Metrics</h2>" in hyd
    assert "<h2>Key Functional Results</h2>" in scr
    assert "<h2>Key Functional Results</h2>" not in hyd
    assert 'class="function-card"' in scr and 'class="function-card"' not in hyd
    assert "Residence Time Exceedance" in hyd and "Residence Time Exceedance" not in scr


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


# ==================================================  pollutant units say what the value is
#
# THE DEFECT THESE EXIST FOR (found 2026-08-02, live in every organic run): `function_sections`
# paired DISPLAY unit strings with CANONICAL values. `p.areal_rate_unit` and `p.per_km_unit` carry
# the endpoint's display scale, and every organic preset is mass_scale="g" with factor 1000, so
# "Per streambed area (mg/m²/day) 0.126" described a figure that is 126 mg/m²/day. The same for
# per-km, and "Stream concentration" paired the endpoint's ENTRY unit with the converted mg/L
# value. Six of the ten presets were affected and the pane disagreed with the report by 1000x.
#
# A METALS FIXTURE CANNOT CATCH ANY OF IT: their factor is 1.0, so display and canonical coincide.
# Every test below therefore runs a real organic endpoint.

_ORGANIC, _METAL = "acesulfame", "zinc"


def _screened_endpoints():
    """A real screening of one metal and one organic, through `assess._build_functions` rather
    than a hand-built model, so the preset-derived labels and units are the shipped ones."""
    import numpy as np

    from hype_app import assess
    from hype_app.contracts import ConnectivityMetrics, ZoneMetrics
    from hype_app.metrics import ExchangeAccounting
    rng = np.random.default_rng(0)
    t = np.exp(rng.normal(np.log(2.0), 0.8, 300))
    w = rng.uniform(50.0, 500.0, t.size)
    return assess._build_functions(
        {"pollutant_endpoints": [_METAL, _ORGANIC], "contaminant_conc_by_key": {},
         "nitrate_mg_l": 1.0},
        conn=ConnectivityMetrics(streambed_area_m2=5000.0, active_streambed_area_m2=3000.0,
                                 connected_streambed_area_m2=2500.0,
                                 connected_streambed_fraction=0.5, turnovers_per_km=0.3),
        zone=ZoneMetrics(bulk_saturated_volume_m3=1e4, mobile_pore_storage_m3=3e3),
        exchange=ExchangeAccounting(total_downwelling=0.1, returning_hyporheic=0.05,
                                    losing_to_sides=0.01, unresolved=0.0),
        transit_times_days=t, transit_weights=w, streamflow_cms=2.8, porosity=0.3,
        have_rtd=True, reach_length_m=253.0)


def _pollutant_section(key):
    from hype_app.contracts import AssessmentResultsV2
    from hype_app.report import function_sections
    fns = _screened_endpoints()
    res = AssessmentResultsV2(assessment_id="a", input_hash="h", functions=fns)
    sec = next(s for s in function_sections(res) if s["key"] == f"pollutant.{key}")
    model = next(p for p in fns.pollutants if p.preset_key == key)
    return sec, model


def _unit_of(row):
    """A row's unit, from its own column when it has one and from its label otherwise."""
    import re
    if row.get("unit"):
        return row["unit"]
    m = re.search(r"\(([^)]*)\)\s*$", row["name"])
    return m.group(1) if m else ""


def _printed(sec, prefix):
    """(unit, printed value) for one row, wherever in the section it lives.

    Two row shapes, because the Inputs table carries its unit in a column of its own while the
    output tables carry it in a trailing parenthesis of the label. Both are searched: "Stream
    concentration" moved to Inputs in the 2026-08-02 layout and this test must follow it there
    rather than quietly stop covering it.

    The value comes back as a float, or as the raw string when the row printed the "under" floor
    token instead of a number."""
    cands = [r for grp in ("rows", "chain", "inputs") for r in sec[grp]
             if r["name"].startswith(prefix)]
    # Exact wins: "Stream concentration" must not resolve to "Stream concentration change".
    row = next((r for r in cands if r["name"] == prefix), None) or cands[0]
    unit = _unit_of(row)
    try:
        val = float(row["value"])
    except ValueError:
        val = row["value"]
    return unit, val


#: (row prefix, contract attribute, the unit that row must print, mg/L-to-unit multiplier).
_UNIT_CONTRACT = [
    ("Stream concentration", "inlet_concentration_mg_l", "µg/L", 1000.0),
    ("Returning water concentration", "outlet_concentration_mg_l", "µg/L", 1000.0),
    ("Stream concentration change", "stream_concentration_change_mg_l", "µg/L", 1000.0),
    (None, "areal_removal_rate_g_m2_day", "g m⁻² day⁻¹", 1.0),
    (None, "removal_per_km_kg_day", "kg day⁻¹ km⁻¹", 1.0),
]


@pytest.mark.parametrize("endpoint", [_METAL, _ORGANIC])
def test_every_pollutant_row_prints_a_value_that_matches_its_unit(endpoint):
    """THE ASSERTION THAT WOULD HAVE FAILED, in five places at once for the organic.

    Checks the number against the contract value expressed in the unit actually printed, rather
    than against a hardcoded string, so a future relabel has to keep the magnitude honest."""
    sec, model = _pollutant_section(endpoint)
    for prefix, attr, unit, mult in _UNIT_CONTRACT:
        if prefix is None:      # the two mass rows carry endpoint-specific labels
            prefix = ("Attenuation per streambed area" if attr.startswith("areal")
                      else "Attenuation per stream km")
            prefix = prefix.replace("Attenuation", model.areal_label.split()[0]
                                    if attr.startswith("areal") else
                                    model.per_km_label.split()[0])
        got_unit, got_val = _printed(sec, prefix)
        assert got_unit == unit, f"{endpoint}/{attr}: printed unit {got_unit!r}"
        expected = getattr(model, attr) * mult
        if isinstance(got_val, str):
            # The returning-water row floors when saturated removal drives the outlet to
            # effectively nothing. Assert the floor was justified, not that it parsed.
            assert got_val == "under 0.01" and expected < 0.01, \
                f"{endpoint}/{attr}: printed {got_val!r} for {expected}"
            continue
        assert got_val == pytest.approx(expected, rel=0.01), \
            f"{endpoint}/{attr}: printed {got_val} {unit}, contract says {expected}"


def test_no_display_scaled_unit_reaches_a_report_table():
    """The trap is one attribute away from the value on the same model, so the next reader will
    reach for it. These three strings are the organic display scale and belong to the pane."""
    sec, _ = _pollutant_section(_ORGANIC)
    names = [r["name"] for grp in ("rows", "chain") for r in sec[grp]]
    names += [f"{r['name']} ({r['unit']})" for r in sec["inputs"]]
    for banned in ("mg/m²/day", "g/day/km", "(g/day)"):
        assert not any(banned in n for n in names), banned


@pytest.mark.parametrize("endpoint", [_METAL, _ORGANIC])
def test_one_concentration_unit_across_the_whole_pollutant_block(endpoint):
    """Fixed for the block, never varied by endpoint or scenario. The inlet used to be labeled in
    the endpoint's entry unit while the outlet and the change were mg/L, so the three could not be
    compared with each other even where each was individually right."""
    from hype_app.report import POLLUTANT_CONC_UNIT
    sec, _ = _pollutant_section(endpoint)
    # "Concentration reduction (%)" is a share, not a concentration, so it is excluded by unit.
    # The inlet lives in Inputs and carries its unit in a column, so it is collected separately:
    # a search of the output tables alone would silently stop covering it.
    conc = [(r["name"], _unit_of(r)) for grp in ("rows", "chain", "inputs") for r in sec[grp]
            if "concentration" in r["name"].lower() and _unit_of(r) not in ("", "%")]
    assert len(conc) == 3, conc
    assert any(n.startswith("Stream concentration") and "change" not in n for n, _ in conc), conc
    for name, unit in conc:
        assert unit == POLLUTANT_CONC_UNIT, (name, unit)


def test_bulk_chemistry_stays_in_mg_per_litre(screened):
    """Nitrate and dissolved oxygen are not compared against the trace-contaminant rows, so they
    keep the unit the reader enters them in."""
    from hype_app.contracts import AssessmentResultsV2
    from hype_app.report import function_sections
    fns = _screened_endpoints()
    res = AssessmentResultsV2(assessment_id="a", input_hash="h", functions=fns)
    nut = next(s for s in function_sections(res) if s["key"] == "nutrient")
    names = [r["name"] for grp in ("rows", "chain") for r in nut[grp]]
    assert any("(mg/L)" in n for n in names)
    assert not any("µg/L" in n for n in names)


def test_the_trace_floor_is_scaled_to_the_unit_it_prints_in():
    """`_conc`'s "under 0.01" guard is an mg/L threshold. Carried over unchanged it suppressed a
    genuine 4 µg/L outlet as if it were nothing, which is a real acesulfame result and not a
    contrived one. Only the returning-water row floors at all."""
    from hype_app.report import _ugl
    # 4 µg/L is a number. `_conc`'s mg/L threshold would have swallowed it, because 0.004 mg/L
    # is below 0.01 mg/L while 4 µg/L is nowhere near 0.01 µg/L.
    assert float(_ugl(0.004, floor=True)) == pytest.approx(4.0)
    assert _ugl(2.2e-9, floor=True) == "under 0.01"  # saturated removal still reads as none
    assert float(_ugl(2.2e-9)) > 0                   # unfloored rows never clamp
    assert _ugl(None) is None


def test_the_screening_document_recaps_what_it_rests_on(screened):
    """Standing alone it would otherwise assert transformation rates with no visible basis, which
    is the §9.3 modelled-versus-inferred distinction the panes make on screen."""
    both, hyd, scr = _docs(screened)
    recap = "<h2>Key Hyporheic Hydraulic Metrics</h2>"
    # THE SAME NAME PART A GIVES THESE SAME THREE CARDS. Two names for one block was something a
    # reader moving between the two documents would have had to resolve for themselves.
    assert recap in scr and recap in hyd
    # Once only in the combined document: Part A carries it and Part B does not repeat it.
    assert both.count(recap) == 1
    # The recap points at nothing. It used to close on "Full derivation, figures and diagnostics
    # are in the hydraulics report", whose leading `signature_sentence` was never passed to the
    # template and rendered as a bare space.
    assert "Full derivation" not in scr
    assert "signature_sentence" not in scr


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
    assert scr.index("Key Hyporheic Hydraulic Metrics") < scr.index("Key Functional Results")


def test_the_per_function_signature_table_is_gone(screened):
    """It restated the three dimensions once per section, under a conceptual figure that had just
    explained them. The figure is the frame; the sections are the numbers."""
    from hype_app.report import function_sections

    assert all("signature_reads" not in s for s in function_sections(screened))
    for doc in _docs(screened):
        assert "What it buys this estimate" not in doc


# =========================================  the screening document is headlines, then disclosures
#
# 2026-08-02 reorganization. Part B rendered three tables, a conditions list and up to five prose
# paragraphs INLINE per module, across up to seven modules, while Part A collapsed all thirty of
# its metric rows behind one disclosure. The asymmetry is what made the screening report unreadable:
# a reader looking for the next answer had to scroll past every number behind the last one.
#
# The tests below each pin one decision from that pass. They are written against the RENDERED
# document rather than against `function_sections`, because every one of them is a claim about what
# a reader sees.


def _multi_endpoint_screening():
    """A real screening of three metals, so the repetition the reorganization removes is present.
    One endpoint cannot show it: the shared prose only repeats when there is something to repeat
    against."""
    import numpy as np

    from hype_app import assess
    from hype_app.contracts import ConnectivityMetrics, ZoneMetrics
    from hype_app.metrics import ExchangeAccounting
    rng = np.random.default_rng(0)
    t = np.exp(rng.normal(np.log(2.0), 0.8, 300))
    w = rng.uniform(50.0, 500.0, t.size)
    return assess._build_functions(
        {"pollutant_endpoints": ["zinc", "cobalt", "nickel"], "contaminant_conc_by_key": {},
         "nitrate_mg_l": 1.0},
        conn=ConnectivityMetrics(streambed_area_m2=5000.0, active_streambed_area_m2=3000.0,
                                 connected_streambed_area_m2=2500.0,
                                 connected_streambed_fraction=0.5, turnovers_per_km=0.3),
        zone=ZoneMetrics(bulk_saturated_volume_m3=1e4, mobile_pore_storage_m3=3e3),
        exchange=ExchangeAccounting(total_downwelling=0.1, returning_hyporheic=0.05,
                                    losing_to_sides=0.01, unresolved=0.0),
        transit_times_days=t, transit_weights=w, streamflow_cms=2.8, porosity=0.3,
        have_rtd=True, reach_length_m=253.0)


def _metals_doc():
    from hype_app.contracts import AssessmentResultsV2
    res = AssessmentResultsV2(assessment_id="a", input_hash="h",
                              functions=_multi_endpoint_screening())
    return res, render_html(res, include_hydraulics=False, app_version="2026.07")


def _body(html):
    """What a reader sees before clicking anything: no appendix disclosures, no tab panels.

    Both have to go. The four function sections are `.tabpanel` sections now rather than
    `<details>`, and a helper that only knew about disclosures would quietly start counting their
    tables as visible."""
    import re
    out = re.sub(r"<details\b.*?</details>", "", html, flags=re.S)
    return re.sub(r'<section class="tabpanel">.*?</section>', "", out, flags=re.S)


def _cards(html):
    """The rendered function cards, split apart."""
    return [c.split("</article>")[0]
            for c in html.split('<article class="function-card">')[1:]]


def test_the_screening_body_carries_results_and_nothing_else(screened):
    """THE POINT OF THE REORGANIZATION. A reader scrolling the screening document should meet
    function names and numbers, full stop. Any table outside a disclosure is detail that escaped."""
    _, _, scr = _docs(screened)
    body = _body(scr)
    start = body.index("<h2>Key Functional Results</h2>")
    end = body.index("<h2>Supporting Information</h2>")
    assert "<table" not in body[start:end], "a metric table is rendering outside a disclosure"
    # ...and the detail is not merely deleted: it is one tab away, under named headings.
    for title in ("Inputs", "Output Metrics", "Limitations", "References"):
        assert f'<p class="paneltitle">{title}</p>' in scr, title


def test_the_four_sections_are_labelled_panels_in_order(screened):
    """TABS, BUILT BY SCRIPT FROM THESE HEADINGS. The template emits no tab markup, so each label
    has exactly one source and the document still reads without scripting. The old form was four
    `<details>` in a flex row, where opening one made it claim the whole row and pushed its
    siblings onto the next line: the labels moved every time a reader opened anything."""
    import re
    _, _, scr = _docs(screened)
    assert "function-detail" not in scr, "the disclosure row is gone"
    for card in _cards(scr):
        region = card.split('<div class="function-tabs">')[1]
        titles = re.findall(r'<p class="paneltitle">([^<]+)</p>', region)
        assert titles == [t for t in ("Inputs", "Output Metrics", "Limitations", "References")
                          if t in titles], titles
        assert titles, card[:60]
        # nothing sits between the results and the tab region
        assert '<section class="tabpanel">' not in card.split('<div class="function-tabs">')[0]


def test_both_equivalent_depths_name_their_basis(screened):
    """The user's question, as a test. The Extent card and Habitat Creation report the SAME zone on
    two bases and differ by exactly the porosity, so a document that names neither reads as two
    answers to one question. The card had no basis anywhere near it: the screening recap renders it
    with no supporting rows and no "What this means", and the PDF header carries no volume chip."""
    from hype_app.report import headline_cards, metric_rows

    _, hyd, scr = _docs(screened)
    extent = next(c for c in headline_cards(screened) if c["dimension"].startswith("Extent"))
    assert extent["primary_name"] == "Equivalent active depth (bulk sediment)"
    # the card names its basis in every document that draws it
    for doc in (hyd, scr):
        assert "Equivalent active depth (bulk sediment)" in doc
    # the pore-water depth only exists where the screening layer ran, and that is the document
    # where the two sit together and the confusion arises
    assert "Equivalent pore-water depth" in scr
    assert "Equivalent pore-water depth" not in hyd
    # ...and the relationship is stated where both numbers actually sit
    habitat = next(s for s in function_sections(screened) if s["key"] == "habitat")
    assert "porosity" in habitat["metrics_note"].lower()
    assert habitat["metrics_note"] in scr
    names = [r["name"] for r in habitat["rows"]]
    assert "Equivalent pore-water depth (m)" in names
    assert "Equivalent depth, bulk basis (m)" in names

    # THE ROW VOCABULARY IS UNTOUCHED. `alternatives.metric_ranges` keys saved scenario ranges by
    # `metric_rows` names, so renaming there would orphan every stored sweep.
    row_names = {r["name"] for r in metric_rows(screened)}
    assert "Equivalent active depth" in row_names
    assert "Equivalent active depth (bulk sediment)" not in row_names


def test_no_rule_sits_directly_under_the_document_header(screened):
    """The header already draws its own line. A part break landing immediately beneath it put two
    rules a few millimetres apart with nothing between them, separating the content from nothing.
    Where a document opens on Site Maps instead, that break still divides them and keeps its bar."""
    _, _, scr = _docs(screened)
    css = scr.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".head + .part{border-top:0;padding-top:0}" in css
    # the rule is adjacent-sibling, so it can only ever fire on a section that follows the header
    assert ".part:first-of-type{border-top" not in css
    # ...and the base rule still draws one, for the breaks that do separate two blocks of content
    assert ".part{margin:2.6rem 0 0;padding-top:1.1rem;border-top:3px solid var(--navy)}" in css
    assert '<section class="part supporting">' in scr


def test_supporting_information_draws_only_its_own_line(screened):
    """Its h2 already carries a navy border-bottom, so the part bar landed a few millimetres above
    the heading and put it between two rules. The h2 rule is the one that marks where the answers
    stop and the working begins, and the PDF draws no bar here at all, so dropping it also brings
    the two documents closer together."""
    _, hyd, scr = _docs(screened)
    for doc in (hyd, scr):
        css = doc.split("<style>", 1)[1].split("</style>", 1)[0]
        assert ".part.supporting{border-top:0}" in css
        # PADDING SURVIVES. Without it the h2's own top margin collapses through the borderless
        # section and pulls the whole block up into the text above it.
        assert ".part.supporting{border-top:0;padding-top:0}" not in css
        assert "h2{font-size:1.02rem;color:var(--navy-d);border-bottom:2px solid var(--navy)" in css


def test_a_card_at_rest_shows_no_controls_at_all(screened):
    """The button owns whether a card shows any working. At rest that means not even a row of tab
    labels: title, result, and one way in. Both the row and the button are script-built, so the
    source carries neither and the CSS keeps the row hidden until the wrapper is opened."""
    _, _, scr = _docs(screened)
    # the rendered body, with the stylesheet and scripts out of the way: both name these classes
    markup = _body(scr.split("</head>", 1)[1].split("<script>", 1)[0])
    assert "tab-row" not in markup, "the tab row must not be in the markup"
    assert "detail-toggle" not in markup, "the button must not be in the markup either"
    css = scr.split("<style>", 1)[1].split("</style>", 1)[0]
    # hidden until the wrapper opens, and the wrapper is never opened by the template
    assert ".js .function-tabs .tab-row{display:none}" in css
    assert ".js .function-tabs.open .tab-row{display:flex" in css
    assert 'class="function-tabs open"' not in scr
    # the script starts every card closed and lands the first click on content
    js = scr.rsplit("<script>", 1)[1]
    assert 'tog.textContent="Show details"' in js
    assert 'tog.textContent=open?"Hide details":"Show details"' in js
    assert "if(open){select(0);}" in js, "opening must land on a tab, not an empty row of labels"
    # ...and the button is the only way to close, so the tabs are navigation rather than toggles
    assert "close" not in js.split("function select(")[1].split("}")[0]


def test_the_document_reads_without_scripting_and_on_paper(screened):
    """The panels are hidden only under `.js`, which an inline script in the head sets, and the
    print block puts every one of them back. So a reader with scripting off, or holding a printout,
    gets four plainly headed sections rather than four things they cannot open."""
    _, _, scr = _docs(screened)
    css = scr.split("<style>", 1)[1].split("</style>", 1)[0]
    # nothing hides a panel except a `.js`-scoped rule
    assert ".js .tabpanel{display:none}" in css
    assert ".tabpanel{display:none}" not in css.replace(".js .tabpanel{display:none}", "")
    # and print undoes it, titles included
    printed = css.split("@media print")[1]
    assert ".js .tabpanel{display:block !important}" in printed
    assert ".js .paneltitle{display:block !important}" in printed
    assert ".tab-row{display:none !important}" in printed
    # the class is set before the body renders, so the full document never flashes
    head = scr.split("</head>", 1)[0]
    assert 'document.documentElement.className+=" js"' in head


def test_the_deleted_framing_copy_stays_deleted(screened, tmp_path):
    """Three blocks the user removed. Each was framing rather than a result.

    CHECKED IN THE PDF TOO, because the two renderers carry separate copies of this copy and the
    first pass at this deletion removed only the HTML one."""
    import re

    from reportlab.platypus import Paragraph, SimpleDocTemplate

    from hype_app.report import render_pdf

    both, hyd, scr = _docs(screened)
    dead = ("Published reaction rates applied to the modeled flow paths, under assumptions",
            "Process inputs held constant",
            "recalculated across")
    for gone in dead:
        assert gone not in scr, gone

    seen, real = [], SimpleDocTemplate.build
    SimpleDocTemplate.build = lambda self, story, **kw: (
        seen.extend(story) or real(self, story, **kw))
    try:
        render_pdf(screened, tmp_path / "s.pdf", include_hydraulics=False, app_version="t")
    finally:
        SimpleDocTemplate.build = real
    pdf_text = " ".join(re.sub(r"<[^>]+>", "", f.text) for f in seen
                        if isinstance(f, Paragraph))
    for gone in dead:
        assert gone not in pdf_text, f"PDF: {gone}"
    # The combined document KEEPS its sub-line: there it draws the modelled-versus-inferred break
    # (spec 9.3), which is the one job that sentence does.
    assert "Everything above is direct model output" in both
    assert 'class="partsub"' not in scr
    # and the caution the deleted scope sentence carried is still in the document, from the
    # appendix rather than from the top of the results. The hydraulic-variability half is gated on
    # a range having been folded at all, so it is asserted in test_alt_screening, where one is.
    assert "not a confidence interval" in scr


def test_the_card_head_carries_the_title_and_no_prose(screened):
    """The rule that removes the function subtitle, the "Key limitation." rail and the "Caution."
    rail in one stroke rather than as three special cases: between the card opening and its first
    result there is a heading and nothing else."""
    import re
    _, _, scr = _docs(screened)
    cards = _cards(scr)
    assert len(cards) == 4, [c[:60] for c in cards]
    for card in cards:
        head = card.split('<div class="endpoint-grid')[0]
        assert re.fullmatch(r'\s*<h3 class="function-title">[^<]+</h3>\s*', head), head
    for banned in ("Key limitation", "Caution.", "function-purpose", "function-limit"):
        assert banned not in scr, banned


def test_endpoints_of_one_function_sit_side_by_side(screened):
    """Three chemicals stacked as three near-identical sections is most of what made this document
    long. They are one row of the same grid now, and a single-endpoint function is not given the
    multi-column treatment it has nothing to fill."""
    _, html = _metals_doc()
    pollutant = next(c for c in _cards(html) if "Pollutant Attenuation" in c)
    assert '<div class="endpoint-grid multi">' in pollutant
    assert pollutant.count('<section class="endpoint">') == 3
    for metal in ("Zinc", "Cobalt", "Nickel"):
        assert f"<h4>{metal}</h4>" in pollutant, metal
    nutrient = next(c for c in _cards(html) if "Nutrient Cycling" in c)
    assert '<div class="endpoint-grid">' in nutrient
    assert "<h4>" not in nutrient      # one endpoint never repeats its function's name


def test_every_function_headlines_one_result_with_or_without_a_sweep(screened):
    """The card used to be printed from the alternatives fold, so a document built without a
    complete sweep opened each function on a metric table and never stated its estimate. The
    headline is resolved from the registry instead, which is the same row the fold folds around."""
    from hype_app.report import function_headline, unit_suffix

    _, _, scr = _docs(screened)          # this fixture carries NO alternatives manifest
    assert 'class="result-value"' in scr
    for s in function_sections(screened):
        if s["key"] == "pollutant":
            continue                     # no concentration entered, so there is no mass to lead on
        h = s["headline"]
        assert h and h["name"] and h["value"], s["key"]
        # the case is welded to the label, which is what lets the explanatory legend be deleted
        assert f'{h["name"]} &middot; Basecase' in scr, s["key"]
        assert (f'<div class="result-value">{h["value"]}'
                f'<small>{unit_suffix(h["unit"])}</small>') in scr, s["key"]
    # a process the registry does not carry resolves to nothing rather than raising
    assert function_headline("not_a_process", object()) is None


def test_each_endpoint_carries_exactly_two_supporting_values(screened):
    """Capped in the builder, not the template. The registry declares three KPIs per process: one
    leads the card and the other two become chips, so density cannot creep in through the data."""
    _, html = _metals_doc()
    for card in _cards(html):
        for ep in card.split('<section class="endpoint">')[1:]:
            assert ep.count('class="support-kpi"') == 2, ep[:150]


def test_the_registry_limits_reach_the_document(screened):
    """`FunctionSpec.limits` is required and word-capped by `validate_functions`, and until the
    Limitations disclosure existed it was rendered ONLY in the app pane: twelve authored statements
    of what each estimate cannot tell you appeared in no report, PDF, CSV or JSON."""
    from hype_app.functions import FUNCTIONS

    _, _, scr = _docs(screened)
    shown = {s.get("function") or s["key"] for s in function_sections(screened)}
    checked = 0
    for key, spec in FUNCTIONS.items():
        if key not in shown:
            continue
        for line in spec.limits:
            assert line in scr, f"{key}: {line}"
            checked += 1
    assert checked >= 10, checked


def test_the_cross_cutting_assumptions_survive_the_fold_into_the_functions(screened):
    """The report-level assumptions card is gone: every bullet with an owner is now that owner's
    registry limit. Three statements have no owner, and they are claims about the report itself, so
    losing them would be a real regression rather than a decluttering."""
    _, _, scr = _docs(screened)
    assert "<h2>Assumptions and Limitations</h2>" not in scr
    block = scr.split("Shared screening assumptions", 1)[1].split("</ul>", 1)[0]
    for kept in ("sensitivity bound, not a confidence interval",
                 "returning flow paths only"):
        assert kept in block, kept
    # and it does NOT hang off the inputs table: a run with no snapshot still states them
    bare = render_html(screened.model_copy(update={"input_snapshot": None}),
                       include_hydraulics=False, app_version="2026.07")
    assert "Shared screening assumptions" in bare
    # the bullets that DID have an owner are gone from the report level, said once by the function
    assert "Carbon is assumed non-limiting for denitrification" not in scr
    assert "Carbon supply, temperature and microbial community are not modeled." in scr


def test_a_grouping_with_nothing_in_it_does_not_render(screened):
    """Four empty disclosures under a function that produced no mass is worse than three: a reader
    who opens one and finds a heading and no table has been told nothing, twice."""
    _, _, scr = _docs(screened)
    cards = _cards(scr)
    pollutant = next(c for c in cards if "Pollutant Attenuation" in c)
    # this fixture enters no concentration, so the endpoint is rate-free and computes no metrics
    assert '<p class="paneltitle">Output Metrics</p>' not in pollutant
    # the endpoint itself is still an input
    assert '<p class="paneltitle">Inputs</p>' in pollutant
    assert '<p class="paneltitle">Limitations</p>' in pollutant
    # ...and a function that DID produce metrics still shows them
    nutrient = next(c for c in cards if "Nutrient Cycling" in c)
    assert '<p class="paneltitle">Output Metrics</p>' in nutrient
    # no disclosure anywhere opens onto nothing
    import re
    for m in re.finditer(r"<summary>[^<]+</summary>(.*?)</details>", scr, re.S):
        assert re.sub(r"<[^>]+>|\s", "", m.group(1)), m.group(0)[:80]


def test_a_row_moved_to_inputs_does_not_also_print_as_an_output(screened):
    """The split only helps if it is a move. A value in both tables is worse than a value in the
    wrong one, because the reader has to work out whether they are the same number."""
    for s in function_sections(screened):
        given = {r["name"] for r in s["inputs"]}
        produced = {r["name"].split(" (")[0] for r in s["rows"] + s["chain"]}
        assert not (given & produced), (s["key"], given & produced)
    # and the rate constant the estimate rests on is now stated, which it never was before
    nut = next(s for s in function_sections(screened) if s["key"] == "nutrient")
    assert any(r["name"] == "Denitrification rate" for r in nut["inputs"]), nut["inputs"]


def test_one_quantity_never_prints_two_unit_conventions(screened):
    """Thermal's two ranges sit on consecutive lines of one card. They read "87.7% to 99.1%" and
    "83.7 to 99.8 %", which is one number under two treatments looking like two kinds of number."""
    from hype_app.report import unit_suffix

    assert unit_suffix("%") == "%"           # percent sets closed up
    assert unit_suffix("kg N/day") == " kg N/day"
    assert unit_suffix("") == ""
    # the `screened` fixture carries no thermal bounds, so this needs a real screening run
    res, html = _metals_doc()
    thermal = next(s for s in function_sections(res) if s["key"] == "thermal")
    assert thermal["range"] and thermal["range"].count("%") == 1, thermal["range"]
    assert " %" not in html, "a percent sign is being set off from its number"


def test_prose_shared_by_every_endpoint_is_printed_once(screened):
    """Zinc, cobalt and nickel carried the same lede, the same three eligibility bullets, the same
    manganese-oxide caveat and the same transferability note, word for word. Only the numbers
    differ, so only the numbers repeat."""
    _, html = _metals_doc()
    assert html.count("<h4>Zinc</h4>") == 1     # three endpoints really did render
    assert html.count("<h4>Cobalt</h4>") == 1 and html.count("<h4>Nickel</h4>") == 1
    for shared in ("First-order attenuation of one endpoint along returning flow paths",
                   "Circumneutral pH",
                   "Uptake is sorption to newly forming manganese oxides",
                   "<b>Transferability.</b>",
                   # the sensitivity-bound provenance too: it describes the CALCULATOR, so three
                   # chemicals printed the same 40-word paragraph three times
                   "Sensitivity bounds from the published spread of the rate constant"):
        assert html.count(shared) == 1, shared
    # it sits under the function's own Limitations, ahead of anything endpoint-specific
    lim = html.split('<p class="paneltitle">Limitations</p>')[2].split("</section>")[0]
    assert lim.index("Circumneutral pH") < lim.index("Zinc")
    # what is NOT shared still reaches the reader: each metal's own observed uptake range
    for mean in ("mean 36%", "mean 52%", "mean 27%"):
        assert mean in html, mean


def test_hoisting_is_decided_field_by_field_on_agreement(screened):
    """Identity is the test, and it is applied per field rather than to the block. One endpoint
    that disagrees keeps ITS field down with every sibling's copy of it, while the fields they do
    still agree on stay hoisted. That is what keeps the rule correct for endpoint combinations
    nobody wrote it against, such as a metal beside an organic."""
    from hype_app.contracts import AssessmentResultsV2, ContaminantScreening

    fns = _multi_endpoint_screening()
    fns.pollutants[1] = ContaminantScreening(
        process_label="Pollutant Attenuation", contaminant_name="Made up",
        preset_key="madeup", preset_label="Made up",
        eligibility_conditions=["Something else entirely"],
        transferability_note="A different setting.", citation="x")
    res = AssessmentResultsV2(assessment_id="a", input_hash="h", functions=fns)
    pols = [s for s in function_sections(res) if s.get("parent") == "pollutant"]
    assert len(pols) == 3
    shared = pols[0].get("group_shared") or {}
    # the lede is a constant of the calculator, so it is shared no matter which endpoints ran
    assert "lede" in shared
    # these two now disagree, so they stay with their endpoints and NOTHING is lost
    assert "conditions" not in shared and "transferability_note" not in shared
    assert all(s["conditions"] and s["transferability_note"] for s in pols)
    html = render_html(res, include_hydraulics=False, app_version="2026.07")
    assert "Something else entirely" in html and "Circumneutral pH" in html


def test_the_decision_framework_is_gone(screened):
    """One four-row table whose reasoning never varied, re-emitted under every mass-bearing
    section. A document screening nitrate plus three metals printed the same four paragraphs four
    times, which is most of what made the old report long."""
    import hype_app.report as R

    assert not hasattr(R, "_DECISION_FRAMEWORK")
    _, html = _metals_doc()
    for doc in list(_docs(screened)) + [html]:
        assert "Which number to use" not in doc
        assert "Prioritize rivers for restoration" not in doc


def test_no_user_facing_copy_says_envelope(screened):
    """It named nothing a reader could point at. The feature that produces the range is called
    Hydraulic Alternatives everywhere else, and a second word for it read as a second concept."""
    _, html = _metals_doc()
    for doc in list(_docs(screened)) + [html]:
        assert "envelope" not in doc.lower(), "the word survived in rendered copy"


def test_both_documents_name_themselves_and_carry_the_appendix_stack(screened):
    """The screening document titled itself "Site Summary Report" while the node that opened it
    said Functional Screening Report, and it ended with no hydraulic metrics at all: a reader who
    wanted the numbers behind the three summary cards had to open the other document."""
    both, hyd, scr = _docs(screened)
    assert "Functional Screening Report</h1>" in scr
    assert "Hydraulics Report</h1>" in hyd
    assert "Site Summary Report</h1>" in both      # the combined form keeps the old name
    for doc in (hyd, scr):
        for appendix in ("<summary>Detailed hydraulic metrics</summary>",
                         "<summary>Model inputs and assumptions</summary>"):
            assert appendix in doc, appendix
    # The document-level reference list is renamed only where a per-function References tab would
    # otherwise share its name.
    assert "<summary>Shared hydraulic and service references</summary>" in scr
    assert "<summary>References</summary>" in hyd


def test_the_screening_pdf_carries_the_appendix_stack_too(screened, tmp_path):
    """It ended after the last function: no metric tables, no model inputs, and the PDF had no
    References section in either document. `Model inputs` sat inside the hydraulics gate, so the
    two formats disagreed about what the SAME screening report contains."""
    import re

    from reportlab.platypus import Paragraph, SimpleDocTemplate

    from hype_app.report import render_pdf

    seen, real = [], SimpleDocTemplate.build
    SimpleDocTemplate.build = lambda self, story, **kw: (
        seen.extend(story) or real(self, story, **kw))
    try:
        render_pdf(screened, tmp_path / "s.pdf", include_hydraulics=False, app_version="2026.07")
    finally:
        SimpleDocTemplate.build = real
    def _heads(*levels):
        return [re.sub(r"<[^>]+>", "", f.text) for f in seen
                if isinstance(f, Paragraph) and f.style.name in levels]

    heads = _heads("Title", "Heading1", "Heading2")
    assert heads[0].endswith("Functional Screening Report")
    assert heads[1:] == ["Key Hyporheic Hydraulic Metrics", "Key Functional Results",
                         "Supporting Information", "Warnings and limitations"], heads
    # the appendix stack sits under Supporting Information, in the HTML's order
    tail = _heads("Heading3")
    assert tail[-3:] == ["Detailed hydraulic metrics", "Model inputs and assumptions",
                         "Shared hydraulic and service references"], tail
    # and the functions get the same four groupings the HTML puts behind disclosures, in order.
    # A grouping with nothing in it is omitted rather than printed empty, so the counts vary with
    # what a run actually produced: this fixture's pollutant computed no mass at all.
    h4 = _heads("Heading4")
    assert h4[:4] == ["Inputs", "Output Metrics", "Limitations", "References"]
    assert set(h4) == {"Inputs", "Output Metrics", "Limitations", "References"}
    assert h4.count("Limitations") == 4      # every function can always say what it cannot do
    assert "Part B. Functional Screening Estimates" not in heads


def test_the_heading_hierarchy_is_three_levels_and_no_deeper(screened):
    """h2 names a part of the document, h3 a function, h4 an endpoint. Table names inside a
    disclosure use `p.subhead` rather than a fifth heading, so working notes never land in the
    outline beside the four functions that ARE the document."""
    import re
    _, html = _metals_doc()
    assert re.findall(r"<h2>([^<]+)</h2>", html) == [
        "Key Hyporheic Hydraulic Metrics", "Key Functional Results", "Supporting Information"]
    # h3 is a function, plus the three dimension groups inside the metrics appendix
    h3 = re.findall(r"<h3[^>]*>([^<]+)</h3>", html)
    assert h3[:4] == ["Nutrient Cycling", "Pollutant Attenuation", "Habitat Creation",
                      "Temperature Regulation"], h3
    assert re.findall(r"<h4>([^<]+)</h4>", html) == ["Zinc", "Cobalt", "Nickel"]
    assert "<h5" not in html and "<h6" not in html
    for doc in _docs(screened):
        assert "<h5" not in doc


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
    # the comparison rides the same group but is NEVER step-gated (the concept precedent):
    # its launcher must be reachable before any local run, since foreign sites supply results
    assert ui_tree.NODE["report.cmp"]["parent"] == "report"
    assert ui_tree.NODE["report.cmp"]["label"] == "Cross-Site Comparison"
    assert ui_tree.NODE["report.cmp"]["group"] is False
    assert ui_tree.NODE_STEP["report.cmp"] is None
    assert "report.cmp" not in ui_tree.NODE_LAYERS
    # the group id survives, so a project last viewed on "report" still reopens somewhere real
    assert "report" in ui_tree.NODE


def test_every_report_node_has_a_pane_and_a_prereq():
    src = open("app.py", encoding="utf-8").read()
    for nid in ("report", "report.hyd", "report.fn", "report.cmp"):
        assert f'"{nid}"' in src, nid
    # each document node names which document it opens, in one table rather than per-branch
    assert "REPORT_DOCS = {" in src
    assert '"report.hyd": ("open_report_hyd", "hydraulics"' in src
    assert '"report.fn": ("open_report_fn", "screening"' in src
    # the comparison is the WORKSPACE, not a fourth document: no REPORT_DOCS entry, a
    # bespoke hub row, and a launcher pane on its own node
    assert '"report.cmp": ("open_report_cmp"' not in src
    assert "def _comparison_hub_row()" in src
    assert '"report.cmp": _pane_report_cmp' in src
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


def test_a_report_pane_is_three_blocks_plus_at_most_one_option():
    """THE DECLUTTERING, as a lint. Each of the three document panes is a blurb, a status, and one
    way forward. It used to be a paragraph, a button, and a Download heading with two more buttons
    under it -- three stacked blocks for what is one action.

    ONE OPTION IS NOW ALLOWED, and only one: the ranges across hydraulic alternatives, on the
    screening document alone. It is bounded here rather than waved through, because the reason this
    pane has a shape rule at all is that report options accumulate."""
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
    # ONE option, gated to the screening document, so it cannot leak onto the hydraulics report
    # or the conceptual model (which have no functional modules to envelope at all).
    assert doc.count("ui.input_checkbox(") == 1, "a second report option arrived unbounded"
    assert doc.count('class_="hype-rep-opt"') == 1
    assert 'doc == "screening"' in doc, "the option is no longer scoped to one document"


def test_the_envelope_option_is_wired_end_to_end():
    """A saved tick that the build ignores, or one that does not rebuild a stale document, are
    both worse than no option: the reader sees a checked box and a report that does not reflect
    it. These four sites are what keep the checkbox honest."""
    src = open("app.py", encoding="utf-8").read()
    assert '"report_fn_envelope",' in src, "the id must persist across a pane remount and a save"
    keep = src[src.index("_KEEP_IDS"):src.index("_CLEARABLE_IDS")]
    assert "report_fn_envelope" in keep
    # ...and NOT clearable: a checkbox is never blank
    clearable = src[src.index("_CLEARABLE_IDS"):src.index("_CLEARABLE_IDS") + 600]
    assert "report_fn_envelope" not in clearable
    sig = src[src.index("def _report_signature()"):src.index("def _report_stale(")]
    assert "_envelope_on()" in sig, "ticking the option must mark a built document stale"
    # the effective selection re-checks the gate, so a tick saved against a wiped or partial
    # sweep cannot linger as a checked box whose value is silently dropped
    eff = src[src.index("def _envelope_on()"):src.index("def _report_signature()")]
    assert "_envelope_state()[0]" in eff


def test_the_option_is_labelled_the_way_the_report_labels_it():
    """The document's rows read "Range across hydraulic alternatives", and a test asserts the word
    "envelope" reaches neither rendered document. The checkbox that produces those rows was the
    last place still offering one, which sent a reader looking for a section that does not exist
    under that name.

    The ID keeps the old word deliberately: it is persisted in saved projects and carried by a
    versioned contract, so renaming it would break restore for a wording change."""
    from hype_app.report import ENVELOPE_LABEL

    src = open("app.py", encoding="utf-8").read()
    label = src.split('ui.input_checkbox("report_fn_envelope",', 1)[1].split(",", 1)[0].strip()
    assert label == '"Include ranges across hydraulic alternatives"', label
    assert "envelope" not in label.lower()
    # TIED TO THE CONSTANT, not just spelled like it. A reader ticks this box and then looks for
    # what it produced, so the two have to move together or the option names a row that is not
    # there.
    assert ENVELOPE_LABEL.lower().replace("range ", "ranges ") in label.lower()


def test_the_report_build_stays_bound_to_the_basecase():
    """The report is a Basecase-bound surface. The envelope makes that load-bearing: if the
    document's Basecase could itself be an alternative, the fold would compare a scenario against
    itself and count it twice."""
    src = open("app.py", encoding="utf-8").read()
    body = src[src.index("def _start_report_build(doc"):src.index("def _auto_open_report()")]
    assert "hz_result()" in body
    assert "alt_view()" not in body and "hz_view()" not in body


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

# --------------------------------------------------------------- groundwater calibration table

def _with_calibration(res):
    """The results fixture with an observation-well calibration attached (2 wells, 1 pair,
    stats), including an unsampleable well so the n/a + note path renders."""
    from hype_app.contracts import (CalibrationPair, CalibrationStats, CalibrationWell,
                                    GroundwaterCalibration)
    return res.model_copy(update={"calibration": GroundwaterCalibration(
        wells=[CalibrationWell(well_id="w1", name="OW-1", lat=43.7, lon=-72.29,
                               screen_elevation_m=191.5, observed_head_m=192.1,
                               model_layer=8, computed_head_m=192.34, residual_m=0.24),
               CalibrationWell(well_id="w2", name="OW-2", lat=43.701, lon=-72.291,
                               screen_elevation_m=189.0, note="dry cell")],
        pairs=[CalibrationPair(pair_id="p1", well_a="OW-1", well_b="OW-2",
                               distance_m=141.2, computed_gradient=0.0021)],
        stats=CalibrationStats(n_observed=1, mean_error_m=0.24,
                               mean_absolute_error_m=0.24, rmse_m=0.24))})


def test_calibration_section_is_hydraulics_only(results):
    """The Groundwater Model Calibration table belongs to the hydraulic signature: it renders
    in the combined and hydraulics documents and never in the screening one (the disjointness
    rule), and only when wells exist at all."""
    cal = _with_calibration(results)
    both, hyd, scr = _docs(cal)
    for doc in (both, hyd):
        assert "<h2>Groundwater Model Calibration</h2>" in doc
        assert "OW-1" in doc and "OW-2" in doc
        assert "+0.24" in doc                        # residual carries its sign
        assert "0.0021" in doc                       # gradients are 4-decimal
        assert "dry cell" in doc                     # the note column explains n/a rows
        assert "Residuals over 1 observed well:" in doc
        assert "computed minus observed" in doc      # the sign convention is stated
    assert "Groundwater Model Calibration" not in scr
    # no wells -> no section, in any document
    for doc in _docs(results):
        assert "Groundwater Model Calibration" not in doc
    # the standing no-em-dash rule covers the new section
    assert "\u2014" not in both


def test_calibration_renders_in_the_no_figures_fallback(results):
    """The report modal's fallback calls render_html(res) with NO figures/spatial/project
    kwargs — the calibration table must ride the results model so that render still carries
    it (the reason it is not a render kwarg)."""
    html = render_html(_with_calibration(results), app_version="t")
    assert "<h2>Groundwater Model Calibration</h2>" in html


def test_calibration_pdf_mirrors_the_html(results, tmp_path):
    """Both renderers draw from the shared calibration builders — the classic drift hazard is
    adding a section to one format only, so the PDF is scraped for the same content."""
    import re

    from reportlab.platypus import Paragraph, SimpleDocTemplate

    from hype_app.report import render_pdf

    cal = _with_calibration(results)
    seen, real = [], SimpleDocTemplate.build
    SimpleDocTemplate.build = lambda self, story, **kw: (
        seen.extend(story) or real(self, story, **kw))
    try:
        render_pdf(cal, tmp_path / "c.pdf", include_functions=False, app_version="t")
    finally:
        SimpleDocTemplate.build = real
    pdf_text = " ".join(re.sub(r"<[^>]+>", "", f.text) for f in seen
                        if isinstance(f, Paragraph))
    assert "Groundwater Model Calibration" in pdf_text
    assert "Residuals over 1 observed well:" in pdf_text
    # tables carry the well rows; scrape their cell strings too
    from reportlab.platypus import Table
    cells = " ".join(str(c) for f in seen if isinstance(f, Table)
                     for row in f._cellvalues for c in row)
    assert "OW-1" in cells and "dry cell" in cells

    # and the screening PDF must not carry it
    seen2 = []
    SimpleDocTemplate.build = lambda self, story, **kw: (
        seen2.extend(story) or real(self, story, **kw))
    try:
        render_pdf(cal, tmp_path / "s.pdf", include_hydraulics=False, app_version="t")
    finally:
        SimpleDocTemplate.build = real
    pdf_text2 = " ".join(re.sub(r"<[^>]+>", "", f.text) for f in seen2
                         if isinstance(f, Paragraph))
    assert "Groundwater Model Calibration" not in pdf_text2
