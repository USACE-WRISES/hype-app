"""Site Summary Report tests (spec §11, §13.6)."""
import csv
import json
from datetime import datetime, timezone

import pytest

from hype_app.contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ComponentScore,
    ConnectivityMetrics,
    GradientBoundaryConfigV2,
    GridSettings,
    HFCIResult,
    KSettings,
    ResidenceTimeMetrics,
    SiteMetadata,
    StreamflowInput,
    ZoneMetrics,
)
from hype_app.provenance import HypeWarning, Provenance
from hype_app.report import (
    fmt,
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
        hfci=HFCIResult(exchange=ComponentScore(score=10), storage=ComponentScore(score=8),
                        processing=ComponentScore(score=12), hfci=0.67, hfci_class="Moderate",
                        hfci_color="#fdbf11"),
        warnings=[HypeWarning(code="extrap", message="Flow statistic extrapolated <check>")],
        untested_uncertainty=["K and soil configuration", "Streamflow"],
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc))


def test_metric_rows_cover_sections(results):
    sections = {r["section"] for r in metric_rows(results)}
    assert {"Connectivity", "Residence time", "Zone", "Functional capacity"} <= sections
    exc = next(r for r in metric_rows(results) if r["name"] == "Excursions per mile")
    assert exc["value"] == fmt(0.1)


def test_csv_matches_model(results, tmp_path):
    p = write_site_metrics_csv(results, tmp_path / "m.csv")
    with open(p, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_name = {r["metric"]: r["value"] for r in rows}
    assert by_name["Excursions per mile"] == fmt(0.1)
    assert by_name["HFCI"] == fmt(0.67)
    assert by_name["Bulk saturated volume"] == fmt(1000.0)


def test_html_self_contained_and_escaped(results):
    html = render_html(results, app_version="2026.07")
    # values agree with the canonical model / CSV
    assert fmt(0.1) in html and "Moderate" in html
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
    for heading in ("Model inputs", "Data sources", "Software versions", "References"):
        assert heading in html, f"HTML missing section: {heading}"
    assert "USACE" in html and "500" in html      # analyst org + reach length surfaced


def test_json_roundtrips_and_agrees(results):
    js = results_to_json(results)
    data = json.loads(js)
    assert data["connectivity"]["excursions_per_mile"] == 0.1
    assert data["hfci"]["hfci"] == 0.67
    # rebuild from JSON -> equal model
    assert AssessmentResultsV2.model_validate(data).hfci.hfci == 0.67


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
