"""Site Summary Report generation (revision spec §11).

Every output — the in-app modal, self-contained HTML, native PDF, and the two CSVs — reads ONLY
the canonical `AssessmentResultsV2` model (§11.2), and all of them render from ONE flat
`metric_rows()` list, so the numbers agree across formats by construction (§13.6). User-entered
text is HTML-escaped. Report generation is a pure function of the results model, so it can be
retried without rerunning the model (§11.5).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from jinja2 import Environment, select_autoescape

from .contracts import AssessmentResultsV2

REPORT_METHOD_VERSION = "site-report/1.0"


def fmt(value, digits: int = 3) -> str:
    """Uniform numeric formatting shared by every output format (§11.4)."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:                 # NaN
            return "—"
        if value == 0:
            return "0"
        a = abs(value)
        if a < 0.001:
            return f"{value:.3g}"           # scientific only for tiny magnitudes
        if a >= 10000:
            return f"{value:.0f}"           # 16093, 8200000 — plain integer, CSV-safe (no commas)
        return f"{round(value, digits):g}"  # 0.736, 2460, 1.4
    return str(value)


def metric_rows(results: AssessmentResultsV2) -> list[dict]:
    """The single canonical (section, name, value, unit) list every format renders from."""
    c, r, z, h = results.connectivity, results.residence_time, results.zone, results.hfci
    raw: list[tuple[str, str, object, str]] = [
        ("Connectivity", "Streamflow", c.streamflow_cms, "m³/s"),
        ("Connectivity", "Total downwelling", c.total_downwelling_cms, "m³/s"),
        ("Connectivity", "Returning hyporheic flux", c.returning_hyporheic_cms, "m³/s"),
        ("Connectivity", "Losing flux", c.losing_cms, "m³/s"),
        ("Connectivity", "Unresolved flux", c.unresolved_cms, "m³/s"),
        ("Connectivity", "Excursions per mile", c.excursions_per_mile, "1/mi"),
        ("Connectivity", "Turnover length", c.turnover_length_m, "m"),
        ("Connectivity", "Mass-balance error", c.mass_balance_error, "fraction"),
        ("Residence time", "Weighted mean", r.weighted_mean_days, "days"),
        ("Residence time", "Weighted median", r.weighted_median_days, "days"),
        ("Residence time", "p05 / p95", None if r.p05_days is None else f"{fmt(r.p05_days)} / {fmt(r.p95_days)}", "days"),
        ("Residence time", "Fraction > 1 day", r.frac_above_1d, "fraction"),
        ("Residence time", "Censored fraction", r.censored_fraction, "fraction"),
        ("Zone", "Bulk saturated volume", z.bulk_saturated_volume_m3, "m³"),
        ("Zone", "Mobile pore-water storage", z.mobile_pore_storage_m3, "m³"),
        ("Zone", "Binary footprint", z.footprint_binary_m2, "m²"),
        ("Zone", "Fraction-weighted footprint", z.footprint_weighted_m2, "m²"),
        ("Zone", "Mean / max thickness", None if z.thickness_mean_m is None else f"{fmt(z.thickness_mean_m)} / {fmt(z.thickness_max_m)}", "m"),
        ("Functional capacity", "Exchange score", h.exchange.score, "0–15"),
        ("Functional capacity", "Storage score", h.storage.score, "0–15"),
        ("Functional capacity", "Processing score", h.processing.score, "0–15"),
        ("Functional capacity", "HFCI", h.hfci, "0–1"),
        ("Functional capacity", "HFCI class", h.hfci_class, ""),
    ]
    return [{"section": s, "name": n, "value_raw": v,
             "value": v if isinstance(v, str) else fmt(v), "unit": u}
            for s, n, v, u in raw]


def results_to_json(results: AssessmentResultsV2) -> str:
    return results.model_dump_json(indent=2)


def write_site_metrics_csv(results: AssessmentResultsV2, path) -> str:
    rows = metric_rows(results)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "metric", "value", "unit"])
        for r in rows:
            w.writerow([r["section"], r["name"], r["value"], r["unit"]])
    return str(path)


def write_transit_times_csv(rtd_rows: list[dict], path) -> str:
    """Per-release-particle RTD rows (§8.5): source cell, flow weight, class, transit time, status."""
    fields = ["particle_id", "source_cell", "flow_weight", "endpoint_class",
              "transit_time_days", "termination"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rtd_rows or []:
            w.writerow(row)
    return str(path)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HYPE Site Summary — {{ site.site_name or 'Unnamed site' }}</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;color:#1a1a1a;max-width:52rem;margin:2rem auto;line-height:1.45;padding:0 1rem}
 h1{font-size:1.6rem;margin:.2rem 0} h2{font-size:1.15rem;border-bottom:2px solid #2c7bb6;padding-bottom:.2rem;margin-top:1.6rem}
 table{border-collapse:collapse;width:100%;margin:.5rem 0;font-size:.92rem}
 th,td{border:1px solid #ccc;padding:.3rem .5rem;text-align:left} th{background:#f0f4f8}
 .hfci{display:inline-block;padding:.15rem .6rem;border-radius:.3rem;color:#fff;font-weight:600}
 .warn{color:#8a1c1c} .muted{color:#666;font-size:.85rem}
 .label{background:#fff3cd;border:1px solid #ffd24d;padding:.3rem .5rem;border-radius:.3rem;font-size:.85rem}
 @media print{h2{page-break-after:avoid} table{page-break-inside:avoid}}
</style></head><body>
<h1>Hyporheic Exchange — Site Summary</h1>
<div class="muted">Report {{ results.assessment_id }} · generated {{ generated_at }} · {{ method_version }}</div>
<div class="label">{{ results.hfci.validation_label }}</div>

<h2>1 · Site identity</h2>
<table>
 <tr><th>Site</th><td>{{ site.site_name or '—' }}</td><th>Analyst</th><td>{{ site.analyst or '—' }}{% if site.organization %} ({{ site.organization }}){% endif %}</td></tr>
 <tr><th>Date</th><td>{{ site.assessment_date or '—' }}</td><th>Reach length</th><td>{{ fmt(site.reach_length_m) }} m</td></tr>
</table>
{% if site.notes %}<p class="muted">{{ site.notes }}</p>{% endif %}

<h2>2 · Executive metrics &amp; HFCI</h2>
<p>Hyporheic Functional Capacity Index:
 <span class="hfci" style="background:{{ results.hfci.hfci_color or '#666' }}">{{ fmt(results.hfci.hfci) }}{% if results.hfci.hfci_class %} — {{ results.hfci.hfci_class }}{% endif %}</span>
 {% if results.hfci.not_computable_reason %}<span class="warn">Not computable: {{ results.hfci.not_computable_reason }}</span>{% endif %}
</p>

<h2>3 · Metrics</h2>
{% for section, items in grouped %}
<h3 style="font-size:1rem">{{ section }}</h3>
<table><tr><th>Metric</th><th>Value</th><th>Unit</th></tr>
{% for r in items %}<tr><td>{{ r.name }}</td><td>{{ r.value }}</td><td>{{ r.unit }}</td></tr>{% endfor %}
</table>
{% endfor %}

<h2>4 · Warnings &amp; limitations</h2>
<ul>{% for w in results.warnings %}<li class="warn">{{ w.message }}</li>{% else %}<li class="muted">None recorded.</li>{% endfor %}</ul>
{% if results.untested_uncertainty %}<p class="muted">Untested uncertainty (not represented): {{ results.untested_uncertainty|join(', ') }}.</p>{% endif %}

<h2>5 · Software &amp; references</h2>
<p class="muted">App {{ app_version or '—' }} · model {{ model_version or '—' }} · HFCI profile {{ results.hfci.profile_id }} {{ results.hfci.profile_version }}</p>
</body></html>"""


def render_html(results: AssessmentResultsV2, *, app_version=None,
                model_version=None) -> str:
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    env.filters["fmt"] = fmt
    template = env.from_string(_HTML_TEMPLATE)
    rows = metric_rows(results)
    grouped: list[tuple[str, list]] = []
    for r in rows:
        if not grouped or grouped[-1][0] != r["section"]:
            grouped.append((r["section"], []))
        grouped[-1][1].append(r)
    site = results.input_snapshot.site if results.input_snapshot else None
    from types import SimpleNamespace
    site = site or SimpleNamespace(site_name=None, analyst=None, organization=None,
                                   assessment_date=None, reach_length_m=None, notes=None)
    return template.render(results=results, site=site, grouped=grouped, fmt=fmt,
                           generated_at=(results.created_at.isoformat() if results.created_at else "—"),
                           method_version=REPORT_METHOD_VERSION,
                           app_version=app_version, model_version=model_version)


def render_pdf(results: AssessmentResultsV2, path, *, app_version=None,
               model_version=None) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    styles = getSampleStyleSheet()
    story = [Paragraph("Hyporheic Exchange — Site Summary", styles["Title"])]
    site = results.input_snapshot.site if results.input_snapshot else None
    story.append(Paragraph(results.hfci.validation_label, styles["Italic"]))
    story.append(Paragraph(f"HFCI: <b>{fmt(results.hfci.hfci)}</b>"
                           f"{(' — ' + results.hfci.hfci_class) if results.hfci.hfci_class else ''}",
                           styles["Normal"]))
    story.append(Spacer(1, 0.2 * inch))

    rows = metric_rows(results)
    data = [["Section", "Metric", "Value", "Unit"]]
    for r in rows:
        data.append([r["section"], r["name"], r["value"], r["unit"]])
    table = Table(data, colWidths=[1.4 * inch, 2.4 * inch, 1.6 * inch, 0.9 * inch], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c7bb6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(table)
    if results.warnings:
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph("Warnings &amp; limitations", styles["Heading2"]))
        for w in results.warnings:
            story.append(Paragraph("• " + w.message, styles["Normal"]))

    rid = results.assessment_id

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"HYPE Site Summary · report {rid}")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=letter,
                            title=f"HYPE Site Summary {rid}")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return str(path)


def generate_report(results: AssessmentResultsV2, out_dir, *, transit_rows=None,
                    app_version=None, model_version=None) -> dict:
    """Write every format into out_dir; return {format: path}. Retryable without a model run."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": str(out / "assessment_results.json"),
        "html": str(out / "site_report.html"),
        "csv_metrics": write_site_metrics_csv(results, out / "site_metrics.csv"),
        "csv_transit": write_transit_times_csv(transit_rows or [],
                                               out / "hyporheic_transit_times.csv"),
    }
    Path(paths["json"]).write_text(results_to_json(results), encoding="utf-8")
    Path(paths["html"]).write_text(
        render_html(results, app_version=app_version, model_version=model_version),
        encoding="utf-8")
    try:
        paths["pdf"] = render_pdf(results, out / "site_report.pdf",
                                  app_version=app_version, model_version=model_version)
    except Exception as e:  # noqa: BLE001 — PDF is best-effort; other formats still land
        paths["pdf_error"] = str(e)
    return paths


__all__ = [
    "fmt", "metric_rows", "results_to_json", "write_site_metrics_csv", "write_transit_times_csv",
    "render_html", "render_pdf", "generate_report", "REPORT_METHOD_VERSION",
]
