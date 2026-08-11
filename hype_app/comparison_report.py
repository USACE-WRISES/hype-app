"""Deterministic exports for a frozen cross-site hydraulic comparison collection."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Iterable, Mapping

from .comparison import (
    BASECASE_LABEL,
    SourceInspection,
    collection_findings,
    member_display_label,
    plottable_members,
)
from .comparison_metrics import METRICS_BY_ID, PRIMARY_METRIC_IDS, default_scale
from .contracts import AltStatus
from .contracts.comparison import (
    ComparisonCollectionV1,
    ComparisonMemberV1,
    ComparisonMetricObservationV1,
)


REPORT_STEM = "cross_site_hydraulic_comparison"


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4g}"


def _range_note(observation: ComparisonMetricObservationV1) -> str:
    if observation.completeness == "unavailable":
        return "Unavailable"
    if observation.baseline is None:
        return "Baseline unavailable; alternative values are not plotted"
    if not observation.has_range:
        return "Baseline only"
    if observation.low == observation.high:
        return f"Unchanged across {observation.finite_case_count} cases"
    prefix = "Partial sensitivity range" if observation.completeness == "partial" \
        else "Range across hydraulic alternatives"
    return (f"{prefix}; {observation.finite_case_count} finite cases"
            + (f"; not completed/available: {', '.join(observation.incomplete_scenarios)}"
               if observation.incomplete_scenarios else ""))


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _site_summary_rows(collection: ComparisonCollectionV1) -> list[dict]:
    rows: list[dict] = []
    for order, member in enumerate(plottable_members(collection), start=1):
        snapshot = member.snapshot
        row = {
            "site_order": order,
            "member_id": str(member.member_id),
            "project_id": snapshot.project_id or "",
            "site_id": snapshot.site_id or "",
            "site": member_display_label(member),
            "site_name": snapshot.site_name or "",
            "project_name": snapshot.project_name or "",
            "assessment_id": snapshot.assessment_id,
            "input_hash": snapshot.input_hash,
            "captured_at": snapshot.captured_at.isoformat(),
            "run_date": snapshot.run_date.isoformat() if snapshot.run_date else "",
            "readiness": snapshot.readiness,
            "source_status": member.source_status.value,
        }
        for metric_id in PRIMARY_METRIC_IDS:
            key = metric_id.replace(".", "__")
            observation = snapshot.observations[metric_id]
            row[f"{key}__baseline"] = observation.baseline
            row[f"{key}__low"] = observation.low
            row[f"{key}__high"] = observation.high
            row[f"{key}__unit"] = observation.unit
            row[f"{key}__range_status"] = observation.completeness
        rows.append(row)
    return rows


def _paper_rows(collection: ComparisonCollectionV1) -> list[dict]:
    rows: list[dict] = []
    for order, member in enumerate(plottable_members(collection), start=1):
        snapshot = member.snapshot
        for metric_id in PRIMARY_METRIC_IDS:
            definition = METRICS_BY_ID[metric_id]
            observation = snapshot.observations[metric_id]
            rows.append({
                "site_order": order,
                "member_id": str(member.member_id),
                "site": member_display_label(member),
                "metric_id": metric_id,
                "dimension": definition.dimension,
                "metric": definition.label,
                "unit": observation.unit,
                "baseline": observation.baseline,
                "low": observation.low,
                "high": observation.high,
                "finite_case_count": observation.finite_case_count,
                "completed_scenarios": observation.completed_scenario_count,
                "configured_scenarios": observation.configured_scenario_count,
                "range_status": observation.completeness,
                "range_note": _range_note(observation),
            })
    return rows


def _sensitivity_rows(collection: ComparisonCollectionV1) -> list[dict]:
    rows: list[dict] = []
    for site_order, member in enumerate(plottable_members(collection), start=1):
        snapshot = member.snapshot
        cases = [("base", BASECASE_LABEL, AltStatus.completed, 1.0, 1.0,
                  snapshot.baseline_metrics, None)]
        cases.extend((scenario.scenario_id, scenario.label, scenario.status,
                      scenario.k_factor, scenario.gradient_factor, scenario.metrics,
                      scenario.error) for scenario in snapshot.scenarios)
        for scenario_id, scenario_label, status, k_factor, g_factor, values, error in cases:
            for definition in METRICS_BY_ID.values():
                rows.append({
                    "site_order": site_order,
                    "member_id": str(member.member_id),
                    "site": member_display_label(member),
                    "scenario_id": scenario_id,
                    "scenario": scenario_label,
                    "status": status.value,
                    "k_factor": k_factor,
                    "gradient_factor": g_factor,
                    "metric_id": definition.id,
                    "dimension": definition.dimension,
                    "metric": definition.label,
                    "unit": definition.presentation_unit,
                    "value": values.get(definition.id),
                    "error": error or "",
                })
    return rows


def _provenance_rows(collection: ComparisonCollectionV1) -> list[dict]:
    rows = []
    for order, member in enumerate(collection.members, start=1):
        snapshot = member.snapshot
        if snapshot is None:
            continue
        row = {
            "site_order": order,
            "member_id": str(member.member_id),
            "site": member_display_label(member),
            "assessment_id": snapshot.assessment_id,
            "input_hash": snapshot.input_hash,
            "source_revision": snapshot.source_revision,
            "results_schema_version": snapshot.results_schema_version,
            "alternatives_schema_version": snapshot.alternatives_schema_version or "",
        }
        for key, value in sorted(snapshot.compatibility.items()):
            row[key] = (json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
                        if isinstance(value, (dict, list)) else value)
        row["provenance"] = json.dumps(snapshot.provenance, sort_keys=True,
                                       separators=(",", ":"), default=str)
        rows.append(row)
    return rows


def _quality_rows(collection: ComparisonCollectionV1) -> list[dict]:
    rows: list[dict] = []
    for order, member in enumerate(collection.members, start=1):
        snapshot = member.snapshot
        findings = list(member.source_findings)
        if snapshot:
            findings = [*snapshot.findings, *findings]
        unique = []
        seen = set()
        for finding in findings:
            key = (finding.code, finding.message, finding.severity.value,
                   json.dumps(finding.context, sort_keys=True, default=str))
            if key not in seen:
                unique.append(finding)
                seen.add(key)
        findings = unique
        if not findings:
            rows.append({"scope": "site", "site_order": order,
                         "member_id": str(member.member_id),
                         "site": member_display_label(member), "severity": "info",
                         "code": "no_findings", "message": "No findings.", "context": "{}"})
        for finding in findings:
            rows.append({
                "scope": "site",
                "site_order": order,
                "member_id": str(member.member_id),
                "site": member_display_label(member),
                "severity": finding.severity.value,
                "code": finding.code,
                "message": finding.message,
                "context": json.dumps(finding.context, sort_keys=True, default=str),
            })
    for finding in collection_findings(collection):
        rows.append({"scope": "collection", "site_order": "", "member_id": "", "site": "",
                     "severity": finding.severity.value, "code": finding.code,
                     "message": finding.message,
                     "context": json.dumps(finding.context, sort_keys=True, default=str)})
    return rows


def render_overview_figures(collection: ComparisonCollectionV1, svg_path, png_path) -> None:
    """Write aligned frequency/duration/extent dot-and-whisker panels."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Stable SVG element IDs and no wall-clock metadata make repeat exports byte-reproducible.
    old_hashsalt = matplotlib.rcParams.get("svg.hashsalt")
    matplotlib.rcParams["svg.hashsalt"] = "hype-cross-project-comparison-v1"

    members = plottable_members(collection)
    height = max(3.2, 1.25 + 0.48 * max(2, len(members)))
    fig, axes = plt.subplots(1, 3, figsize=(12.5, height), sharey=True,
                             constrained_layout=True)
    labels = [member_display_label(member) for member in members]
    y = list(range(len(members)))
    color = "#176B73"
    for panel, (axis, metric_id) in enumerate(zip(axes, PRIMARY_METRIC_IDS)):
        definition = METRICS_BY_ID[metric_id]
        observations = [member.snapshot.observations[metric_id] for member in members]
        scale_values = [value for obs in observations for value in (obs.low, obs.high)
                        if value is not None]
        if default_scale(metric_id, scale_values) == "log":
            axis.set_xscale("log")
        for yi, observation in zip(y, observations):
            if observation.baseline is None:
                continue
            if observation.has_range:
                linestyle = "--" if observation.completeness == "partial" else "-"
                axis.hlines(yi, observation.low, observation.high, color=color,
                            linewidth=2.1, linestyle=linestyle, alpha=0.72)
            axis.scatter(observation.baseline, yi, s=38, color=color, edgecolor="white",
                         linewidth=0.7, zorder=3)
        axis.set_title(definition.dimension.replace(" of Hyporheic Zone", ""),
                       fontsize=10, fontweight=600)
        axis.set_xlabel(f"{definition.label}\n({definition.presentation_unit})", fontsize=8.5)
        axis.grid(axis="x", color="#D9E1E5", linewidth=0.7)
        axis.set_axisbelow(True)
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.tick_params(axis="both", labelsize=8)
        if panel == 0:
            axis.set_yticks(y, labels)
        if not members:
            axis.text(0.5, 0.5, "No valid included sites", transform=axis.transAxes,
                      ha="center", va="center", color="#607078")
    if members:
        axes[0].invert_yaxis()
    fig.suptitle("Cross-Site Hydraulic Comparison", fontsize=13, fontweight=600)
    fig.savefig(svg_path, format="svg", bbox_inches="tight", metadata={"Date": None})
    fig.savefig(png_path, format="png", dpi=300, bbox_inches="tight")
    plt.close(fig)
    matplotlib.rcParams["svg.hashsalt"] = old_hashsalt


def _html_report(collection: ComparisonCollectionV1) -> str:
    members = plottable_members(collection)
    warning_html = "".join(
        f'<div class="warning"><b>{html.escape(finding.code)}</b>: '
        f'{html.escape(finding.message)}</div>' for finding in collection_findings(collection))
    table_rows = []
    for member in members:
        snapshot = member.snapshot
        cells = [f"<td>{html.escape(member_display_label(member))}</td>"]
        for metric_id in PRIMARY_METRIC_IDS:
            observation = snapshot.observations[metric_id]
            note = html.escape(_range_note(observation))
            if observation.has_range:
                note += f": {_fmt(observation.low)} to {_fmt(observation.high)}"
            cells.append(
                "<td>"
                f"<strong>{_fmt(observation.baseline)}</strong> {html.escape(observation.unit)}"
                f"<small>{note}</small></td>")
        table_rows.append("<tr>" + "".join(cells) + "</tr>")
    headings = "".join(f"<th>{html.escape(METRICS_BY_ID[mid].label)}</th>"
                       for mid in PRIMARY_METRIC_IDS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(collection.name)} · Cross-Site Hydraulic Comparison</title>
<style>
body{{font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:#18323b;margin:0;background:#f4f7f8}}
main{{max-width:1180px;margin:32px auto;background:white;padding:34px 40px;border:1px solid #dce5e8;border-radius:10px}}
h1{{font-size:26px;margin:0 0 4px}} .eyebrow{{color:#52717a;text-transform:uppercase;letter-spacing:.08em;font-size:12px}}
img{{width:100%;height:auto;margin:22px 0}} table{{width:100%;border-collapse:collapse;margin-top:20px}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid #dfe7e9;vertical-align:top}} th{{background:#eef4f5}}
small{{display:block;color:#587078;margin-top:3px}} .warning{{padding:9px 12px;margin:8px 0;background:#fff7dc;border-left:3px solid #d39c18}}
.note{{color:#587078}} footer{{margin-top:28px;padding-top:14px;border-top:1px solid #dfe7e9;color:#587078;font-size:12px}}
</style></head><body><main>
<div class="eyebrow">HYPE hydraulic comparison</div><h1>{html.escape(collection.name)}</h1>
<p class="note">Points are project baselines. Whiskers are the range across completed hydraulic alternatives; dashed whiskers are partial ranges. They are sensitivity ranges, not confidence intervals.</p>
{warning_html}
<img src="overview.svg" alt="Three aligned hydraulic comparison panels">
<h2>Baseline summary</h2><table><thead><tr><th>Site</th>{headings}</tr></thead>
<tbody>{''.join(table_rows) or '<tr><td colspan="4">No valid included sites.</td></tr>'}</tbody></table>
<footer>Values are rendered exclusively from the frozen comparison snapshot. Source projects are not modified by this export.</footer>
</main></body></html>"""


def _pdf_report(collection: ComparisonCollectionV1, path: Path, overview_png: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter),
                            leftMargin=0.45 * inch, rightMargin=0.45 * inch,
                            topMargin=0.45 * inch, bottomMargin=0.45 * inch,
                            title=collection.name, invariant=1)
    story = [Paragraph("Cross-Site Hydraulic Comparison", styles["Title"]),
             Paragraph(html.escape(collection.name), styles["Heading2"]),
             Paragraph("Points are baselines. Whiskers show the range across completed hydraulic "
                       "alternatives; dashed whiskers are partial sensitivity ranges, not "
                       "confidence intervals.", styles["BodyText"]), Spacer(1, 8)]
    for finding in collection_findings(collection):
        story.extend([Paragraph(f"<b>Review:</b> {html.escape(finding.message)}",
                                styles["BodyText"]), Spacer(1, 3)])
    if overview_png.is_file():
        story += [Image(str(overview_png), width=9.7 * inch, height=3.2 * inch), Spacer(1, 8)]
    data = [["Site", *[METRICS_BY_ID[mid].label for mid in PRIMARY_METRIC_IDS]]]
    for member in plottable_members(collection):
        row = [member_display_label(member)]
        for metric_id in PRIMARY_METRIC_IDS:
            obs = member.snapshot.observations[metric_id]
            note = (f"{_fmt(obs.low)} to {_fmt(obs.high)}" if obs.has_range
                    else _range_note(obs))
            row.append(f"{_fmt(obs.baseline)} {obs.unit}\n{note}")
        data.append(row)
    if len(data) == 1:
        data.append(["No valid included sites", "", "", ""])
    table = Table(data, colWidths=[2.05 * inch, 2.45 * inch, 2.45 * inch, 2.45 * inch],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8F1F2")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#18323B")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CCDADD")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFA")]),
    ]))
    story.append(table)
    doc.build(story)


def _assert_export_location(collection: ComparisonCollectionV1, output_dir: Path) -> None:
    target = output_dir.resolve()
    for member in collection.members:
        report_dir = (Path(member.source_absolute).resolve().parent / "report").resolve()
        if target == report_dir or report_dir in target.parents:
            raise ValueError("Comparison exports must not be written into a source project's report directory.")


def _apply_inspections(collection: ComparisonCollectionV1,
                       inspections: Mapping[str, SourceInspection] | None) \
        -> ComparisonCollectionV1:
    if not inspections:
        return collection
    members = []
    for member in collection.members:
        inspection = inspections.get(str(member.member_id))
        members.append(member if inspection is None else member.model_copy(update={
            "source_status": inspection.status,
            "source_findings": list(inspection.findings),
        }))
    return collection.model_copy(update={"members": members})


def generate_comparison_report(collection: ComparisonCollectionV1, out_dir,
                               *, include_pdf: bool = True,
                               inspections: Mapping[str, SourceInspection] | None = None) \
        -> dict[str, str]:
    """Generate HTML/PDF, CSV/JSON, and SVG/PNG from frozen snapshots only."""
    collection = _apply_inspections(collection, inspections)
    output = Path(out_dir).expanduser().resolve()
    _assert_export_location(collection, output)
    output.mkdir(parents=True, exist_ok=True)

    paths = {
        "html": output / f"{REPORT_STEM}.html",
        "site_summary_csv": output / "site_summary.csv",
        "paper_plot_data_csv": output / "paper_plot_data.csv",
        "sensitivity_results_long_csv": output / "sensitivity_results_long.csv",
        "model_provenance_csv": output / "model_provenance.csv",
        "quality_control_csv": output / "quality_control.csv",
        "comparison_snapshot_json": output / "comparison_snapshot.json",
        "overview_svg": output / "overview.svg",
        "overview_png": output / "overview.png",
    }
    if include_pdf:
        paths["pdf"] = output / f"{REPORT_STEM}.pdf"

    site_rows = _site_summary_rows(collection)
    site_fields = (["site_order", "member_id", "project_id", "site_id", "site", "site_name",
                    "project_name", "assessment_id", "input_hash", "captured_at", "run_date",
                    "readiness", "source_status"]
                   + [f"{mid.replace('.', '__')}__{suffix}" for mid in PRIMARY_METRIC_IDS
                      for suffix in ("baseline", "low", "high", "unit", "range_status")])
    _write_csv(paths["site_summary_csv"], site_fields, site_rows)
    _write_csv(paths["paper_plot_data_csv"],
               ["site_order", "member_id", "site", "metric_id", "dimension", "metric", "unit",
                "baseline", "low", "high", "finite_case_count", "completed_scenarios",
                "configured_scenarios", "range_status", "range_note"], _paper_rows(collection))
    _write_csv(paths["sensitivity_results_long_csv"],
               ["site_order", "member_id", "site", "scenario_id", "scenario", "status",
                "k_factor", "gradient_factor", "metric_id", "dimension", "metric", "unit",
                "value", "error"], _sensitivity_rows(collection))
    provenance_rows = _provenance_rows(collection)
    provenance_fields = ["site_order", "member_id", "site", "assessment_id", "input_hash",
                         "source_revision", "results_schema_version",
                         "alternatives_schema_version",
                         *sorted({key for row in provenance_rows for key in row}
                                 - {"site_order", "member_id", "site", "assessment_id", "input_hash",
                                    "source_revision", "results_schema_version",
                                    "alternatives_schema_version", "provenance"}), "provenance"]
    _write_csv(paths["model_provenance_csv"], provenance_fields, provenance_rows)
    _write_csv(paths["quality_control_csv"],
               ["scope", "site_order", "member_id", "site", "severity", "code", "message",
                "context"], _quality_rows(collection))
    paths["comparison_snapshot_json"].write_text(
        collection.model_dump_json(indent=2) + "\n", encoding="utf-8")
    render_overview_figures(collection, paths["overview_svg"], paths["overview_png"])
    paths["html"].write_text(_html_report(collection), encoding="utf-8")
    if include_pdf:
        _pdf_report(collection, paths["pdf"], paths["overview_png"])
    return {key: str(path) for key, path in paths.items()}


__all__ = ["REPORT_STEM", "render_overview_figures", "generate_comparison_report"]
