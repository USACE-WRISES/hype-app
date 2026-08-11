"""Stable hydraulic metric registry shared by comparison readers, plots, and exports.

Metric IDs are contract paths, never display labels.  Values are read from the canonical
``AssessmentResultsV2`` sections and converted once into the presentation units named here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable


DIM_FREQUENCY = "Frequency of Hyporheic Exchange"
DIM_DURATION = "Duration in Hyporheic Zone"
DIM_EXTENT = "Extent of Hyporheic Zone"


@dataclass(frozen=True, slots=True)
class HydraulicMetric:
    id: str
    section: str
    field: str
    dimension: str
    label: str
    canonical_unit: str
    presentation_unit: str
    presentation_factor: float = 1.0
    log_eligible: bool = True

    def present(self, value: Any) -> float | None:
        """Return a finite, non-boolean presentation value or ``None``."""
        if not is_finite_number(value):
            return None
        return float(value) * self.presentation_factor

    def extract(self, source: Any) -> float | None:
        """Read this metric from an AssessmentResults model or a section mapping."""
        if isinstance(source, dict):
            section = source.get(self.section)
            if section is None and self.field in source:
                section = source
        else:
            section = getattr(source, self.section, None)
        if section is None:
            return None
        value = section.get(self.field) if isinstance(section, dict) else getattr(section, self.field, None)
        return self.present(value)


def _m(section: str, field: str, dimension: str, label: str, canonical_unit: str,
       presentation_unit: str | None = None, factor: float = 1.0,
       log: bool = True) -> HydraulicMetric:
    return HydraulicMetric(
        id=f"{section}.{field}", section=section, field=field, dimension=dimension,
        label=label, canonical_unit=canonical_unit,
        presentation_unit=presentation_unit or canonical_unit,
        presentation_factor=factor, log_eligible=log,
    )


# Deliberately ordered for the UI picker and deterministic exports.  The three primaries lead each
# group; the remaining values are the numeric rows a hydraulic analyst can compare meaningfully.
HYDRAULIC_METRICS: tuple[HydraulicMetric, ...] = (
    _m("connectivity", "turnovers_per_km", DIM_FREQUENCY,
       "Streamflow-equivalent turnovers", "turnovers/km"),
    _m("connectivity", "turnover_length_km", DIM_FREQUENCY, "River turnover length", "km"),
    _m("connectivity", "returning_hyporheic_cms", DIM_FREQUENCY,
       "Gross hyporheic exchange", "m3/s", "L/s", 1000.0),
    _m("connectivity", "exchange_flux_mm_day", DIM_FREQUENCY,
       "Exchange intensity", "mm/day"),
    _m("connectivity", "gross_exchange_ratio_reach", DIM_FREQUENCY,
       "Gross exchange ratio (reach)", "fraction"),
    _m("connectivity", "streamflow_cms", DIM_FREQUENCY, "Stream discharge", "m3/s"),
    _m("connectivity", "total_downwelling_cms", DIM_FREQUENCY,
       "Total downwelling flow", "m3/s"),
    _m("connectivity", "losing_cms", DIM_FREQUENCY, "Losing flow", "m3/s"),
    _m("connectivity", "unresolved_cms", DIM_FREQUENCY, "Unresolved flow", "m3/s"),
    _m("connectivity", "net_stream_exchange_cms", DIM_FREQUENCY,
       "Net groundwater exchange", "m3/s", log=False),
    _m("connectivity", "streambed_area_m2", DIM_FREQUENCY, "Streambed area", "m2"),
    _m("connectivity", "active_streambed_area_m2", DIM_FREQUENCY,
       "Active streambed area", "m2"),
    _m("connectivity", "active_streambed_fraction", DIM_FREQUENCY,
       "Active streambed fraction", "fraction"),
    _m("connectivity", "return_streambed_area_m2", DIM_FREQUENCY,
       "Return streambed area", "m2"),
    _m("connectivity", "connected_streambed_area_m2", DIM_FREQUENCY,
       "Connected streambed area", "m2"),
    _m("connectivity", "connected_streambed_fraction", DIM_FREQUENCY,
       "Connected streambed fraction", "fraction"),
    _m("connectivity", "excursions_per_mile", DIM_FREQUENCY,
       "Excursions per mile", "1/mi"),
    _m("connectivity", "mass_balance_error", DIM_FREQUENCY,
       "Mass-balance error", "fraction", log=False),
    _m("connectivity", "returning_flow_fraction", DIM_FREQUENCY,
       "Returning flow fraction", "fraction"),
    _m("connectivity", "censored_flow_fraction", DIM_FREQUENCY,
       "Censored flow fraction", "fraction"),

    _m("residence_time", "weighted_median_days", DIM_DURATION,
       "Flux-weighted median residence time", "day", "hr", 24.0),
    _m("residence_time", "weighted_mean_days", DIM_DURATION,
       "Flux-weighted mean residence time", "day", "hr", 24.0),
    _m("residence_time", "p05_days", DIM_DURATION, "Residence time P05", "day", "hr", 24.0),
    _m("residence_time", "p10_days", DIM_DURATION, "Residence time P10", "day", "hr", 24.0),
    _m("residence_time", "p25_days", DIM_DURATION, "Residence time P25", "day", "hr", 24.0),
    _m("residence_time", "p75_days", DIM_DURATION, "Residence time P75", "day", "hr", 24.0),
    _m("residence_time", "p90_days", DIM_DURATION, "Residence time P90", "day", "hr", 24.0),
    _m("residence_time", "p95_days", DIM_DURATION, "Residence time P95", "day", "hr", 24.0),
    _m("residence_time", "min_days", DIM_DURATION, "Minimum residence time", "day", "hr", 24.0),
    _m("residence_time", "max_days", DIM_DURATION, "Maximum residence time", "day", "hr", 24.0),
    _m("residence_time", "frac_above_1h", DIM_DURATION,
       "Fraction above 1 hour", "fraction"),
    _m("residence_time", "frac_1h_to_1d", DIM_DURATION,
       "Fraction from 1 hour to 1 day", "fraction"),
    _m("residence_time", "frac_above_1d", DIM_DURATION,
       "Fraction above 1 day", "fraction"),
    _m("residence_time", "returning_flux_represented_cms", DIM_DURATION,
       "Returning flux represented", "m3/s"),
    _m("residence_time", "censored_fraction", DIM_DURATION,
       "Censored residence-time fraction", "fraction"),
    _m("residence_time", "effective_particle_count", DIM_DURATION,
       "Effective particle count", "count"),
    _m("residence_time", "max_tracking_time_days", DIM_DURATION,
       "Maximum tracking time", "day", "hr", 24.0),
    _m("residence_time", "porosity", DIM_DURATION, "Porosity", "fraction"),

    _m("zone", "equivalent_active_depth_m", DIM_EXTENT, "Equivalent active depth", "m"),
    _m("zone", "bulk_saturated_volume_m3", DIM_EXTENT,
       "Active hyporheic volume", "m3"),
    _m("zone", "mobile_pore_storage_m3", DIM_EXTENT,
       "Mobile pore-water storage", "m3"),
    _m("zone", "footprint_binary_m2", DIM_EXTENT, "Binary footprint", "m2"),
    _m("zone", "footprint_weighted_m2", DIM_EXTENT, "Weighted footprint", "m2"),
    _m("zone", "thickness_mean_m", DIM_EXTENT, "Mean active thickness", "m"),
    _m("zone", "thickness_max_m", DIM_EXTENT, "Maximum active thickness", "m"),
    _m("zone", "path_depth_p50_m", DIM_EXTENT, "P50 maximum path depth", "m"),
    _m("zone", "path_depth_p90_m", DIM_EXTENT, "P90 maximum path depth", "m"),
    _m("zone", "path_depth_max_m", DIM_EXTENT, "Maximum path depth", "m"),
)


METRICS_BY_ID: dict[str, HydraulicMetric] = {metric.id: metric for metric in HYDRAULIC_METRICS}
if len(METRICS_BY_ID) != len(HYDRAULIC_METRICS):  # import-time registry integrity guard
    raise RuntimeError("duplicate hydraulic comparison metric ID")

PRIMARY_METRIC_IDS: tuple[str, str, str] = (
    "connectivity.turnovers_per_km",
    "residence_time.weighted_median_days",
    "zone.equivalent_active_depth_m",
)


def is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def metric(metric_id: str) -> HydraulicMetric:
    try:
        return METRICS_BY_ID[metric_id]
    except KeyError as exc:
        raise KeyError(f"unknown hydraulic comparison metric: {metric_id}") from exc


def extract_metrics(source: Any, metrics: Iterable[HydraulicMetric] = HYDRAULIC_METRICS) \
        -> dict[str, float]:
    """Extract all finite values, already converted to the registry's presentation units."""
    out: dict[str, float] = {}
    for definition in metrics:
        value = definition.extract(source)
        if value is not None:
            out[definition.id] = value
    return out


def grouped_metrics() -> dict[str, tuple[HydraulicMetric, ...]]:
    return {
        dimension: tuple(m for m in HYDRAULIC_METRICS if m.dimension == dimension)
        for dimension in (DIM_FREQUENCY, DIM_DURATION, DIM_EXTENT)
    }


def default_scale(metric_id: str, values: Iterable[float | None]) -> str:
    """Use log only for an eligible, all-positive range spanning at least one decade."""
    definition = metric(metric_id)
    finite = [float(value) for value in values if is_finite_number(value)]
    if (not definition.log_eligible or not finite or min(finite) <= 0):
        return "linear"
    return "log" if max(finite) / min(finite) >= 10.0 else "linear"


__all__ = [
    "DIM_FREQUENCY", "DIM_DURATION", "DIM_EXTENT", "HydraulicMetric",
    "HYDRAULIC_METRICS", "METRICS_BY_ID", "PRIMARY_METRIC_IDS", "is_finite_number",
    "metric", "extract_metrics", "grouped_metrics", "default_scale",
]
