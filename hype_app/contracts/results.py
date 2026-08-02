"""Canonical assessment-results contract (revision spec §4.2, §8, §11; hydraulic report §5-7, §10).

`AssessmentResultsV2` is the immutable model generated right after a completed analysis. The report
modal and EVERY exported format (HTML/PDF/CSV/JSON) read only this model (§11.2), so numbers agree
across formats by construction. It references the frozen input snapshot and carries connectivity,
residence-time, zone, and threshold functional-opportunity metrics with full provenance, warnings,
and artifact paths. The report leads with the three hydraulic dimensions (frequency of hyporheic
exchange, duration in hyporheic zone, extent of hyporheic zone); there is no combined index.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ..provenance import HypeModel, HypeWarning
from .inputs import AssessmentInputSnapshot
from .alternatives import HydraulicAlternativesManifest

RESULTS_SCHEMA_VERSION = "assessment-results/2.3"


class ConnectivityMetrics(HypeModel):
    """Flux accounting + hyporheic connectivity (report §5). The headline is streamflow-equivalent
    turnovers per km; excursions-per-mile is retained as a supporting value. None when the inputs
    make connectivity undefined (with a reason)."""

    streamflow_cms: float | None = None
    total_downwelling_cms: float | None = None
    returning_hyporheic_cms: float | None = None            # Q_HEF
    losing_cms: float | None = None
    unresolved_cms: float | None = None
    net_stream_exchange_cms: float | None = None            # gross upwelling - gross downwelling
    # frequency of hyporheic exchange (report §5.1-5.5)
    turnovers_per_km: float | None = None                   # C_1km, headline
    turnover_length_km: float | None = None                 # L_T
    gross_exchange_ratio_reach: float | None = None         # E_reach = Q_HEF / Q_stream
    exchange_flux_m_day: float | None = None                # q_HEF = Q_HEF / A_bed
    exchange_flux_mm_day: float | None = None
    streambed_area_m2: float | None = None                  # A_bed (modeled stream-cell area)
    active_streambed_area_m2: float | None = None           # A_active (returning downwelling cells)
    active_streambed_fraction: float | None = None          # F_active,bed
    # A_active is ENTRY ONLY by framework §4.7. These add the discharge side and the union, which
    # is what "how much of this bed is engaged" asks; on a gaining reach the return is spread far
    # wider than the inflow. None on runs delineated before the engine computed them.
    return_streambed_area_m2: float | None = None           # bed where returning water re-emerges
    connected_streambed_area_m2: float | None = None        # entry OR exit, each cell once
    connected_streambed_fraction: float | None = None
    # supporting / legacy
    turnover_length_m: float | None = None
    excursions_per_mile: float | None = None                # supporting (backward compat)
    mass_balance_error: float | None = None
    returning_flow_fraction: float | None = None            # returning / total downwelling
    censored_flow_fraction: float | None = None             # unresolved / total downwelling
    unavailable_reason: str | None = None


class ResidenceTimeMetrics(HypeModel):
    """Flux-weighted returning-particle residence-time distribution (report §6)."""

    weighted_mean_days: float | None = None
    weighted_median_days: float | None = None
    p05_days: float | None = None
    p10_days: float | None = None
    p25_days: float | None = None
    p75_days: float | None = None
    p90_days: float | None = None
    p95_days: float | None = None
    min_days: float | None = None
    max_days: float | None = None
    frac_above_1h: float | None = None
    frac_1h_to_1d: float | None = None
    frac_above_1d: float | None = None
    returning_flux_represented_cms: float | None = None
    censored_fraction: float | None = None
    effective_particle_count: float | None = None
    max_tracking_time_days: float | None = None
    porosity: float | None = None


class ZoneMetrics(HypeModel):
    """Extent of hyporheic zone (report §7): active capacity + spatial extent. Bulk sediment volume is the headline
    basis; mobile pore-water storage is kept distinct and correctly labeled."""

    bulk_saturated_volume_m3: float | None = None           # V_HZ headline (bulk sediment basis)
    mobile_pore_storage_m3: float | None = None             # supporting pore-water volume
    equivalent_active_depth_m: float | None = None          # D_HZ = V_HZ / A_bed
    active_volume_basis: str | None = "bulk sediment"
    footprint_binary_m2: float | None = None                # grid/particle-resolution dependent
    footprint_weighted_m2: float | None = None
    thickness_mean_m: float | None = None
    thickness_max_m: float | None = None
    path_depth_p50_m: float | None = None                   # flow-weighted median max path depth
    path_depth_p90_m: float | None = None                   # flow-weighted P90 (preferred summary)
    path_depth_max_m: float | None = None                   # supplemental single-path maximum


class ThresholdResult(HypeModel):
    """One residence-time scenario's functional-opportunity result (report §10, §24). Hydraulic
    opportunity only, never a direct ecological outcome."""

    threshold_value_h: float
    threshold_label: str | None = None
    threshold_source: str | None = None
    flow_exceedance_fraction: float | None = None           # P(T >= t*)
    functional_exchange_m3_s: float | None = None           # Q_HEF * P
    functional_connectivity_per_km: float | None = None     # C_1km * P
    interpretation_note: str | None = None


class OpportunityPoint(HypeModel):
    """One point on the rate-free R(tau) curve (framework §13)."""

    tau_hours: float
    opportunity: float | None = None


class _ScreeningBase(HypeModel):
    """Fields every screening section carries, whatever its kind."""

    process_key: str | None = None
    process_label: str | None = None
    process_kind: str | None = None                         # "residence_time" | "extent"
    kinetics: str | None = None
    method_version: str | None = None
    censored_flow_fraction: float | None = None
    n_paths: int | None = None
    #: Provenance of `n_paths`: it is `downwelling_cells x interface_particles_per_cell`. Carried
    #: so a small path count reads as site hydrology (few streambed cells downwell) rather than as
    #: lost data -- the separate zone-extent pass counts thousands of particles for a different
    #: question, and the two are reported side by side.
    downwelling_cells: int | None = None
    interface_particles_per_cell: int | None = None
    #: The three hyporheic-hydraulic-signature dimensions, echoed onto every section so an
    #: inferred functional estimate can name the DIRECT MODEL OUTPUTS it rests on (§9.3) using the
    #: same numbers the scorecards publish. Computed nowhere else: two are passthroughs from the
    #: caller and the third is the flow-weighted median residence time. Optional with defaults, so
    #: they are additive and need no schema bump.
    signature_turnovers_per_km: float | None = None         # frequency: delivery
    signature_t50_days: float | None = None                 # duration: contact time
    signature_equivalent_depth_m: float | None = None       # extent: participating capacity
    weight_identity_rel_diff: float | None = None
    unavailable_reason: str | None = None
    citation: str | None = None
    #: Keys into `functions.helptext.SOURCES`, so the report can render a real reference list
    #: instead of re-splitting `citation` back into entries. Optional, so an older payload that
    #: carries only the flat string still validates.
    source_keys: list[str] = Field(default_factory=list)
    transferability_note: str | None = None


class ReactiveScreening(_ScreeningBase):
    """A residence-time section that carries mass: denitrification or contaminant attenuation.

    The four-metric chain is `removal_efficiency` -> `areal_removal_rate_g_m2_day` ->
    `reference_area_m2` -> `total_removed_kg_day`, related by `total = areal rate x area` and
    `areal rate = exchange flux x inlet concentration x efficiency`.

    `fraction_above_threshold` is the flow that clears the reaction gate. For denitrification the
    gate is the time oxygen takes to run out, so the complement is flow that stays oxic and is
    expected to behave as a nitrate source rather than a sink."""

    # inputs echoed for traceability
    inlet_concentration_mg_l: float | None = None
    rate_value: float | None = None
    rate_unit: str | None = None
    # the chain
    removal_efficiency: float | None = None                 # E
    areal_removal_rate_g_m2_day: float | None = None        # r
    reference_area_m2: float | None = None                  # A
    reference_area_basis: str | None = None                 # "total streambed" | "active streambed"
    total_removed_kg_day: float | None = None               # M
    total_removed_lb_day: float | None = None
    total_removed_low_kg_day: float | None = None           # sensitivity corners, not an interval
    total_removed_high_kg_day: float | None = None
    areal_removal_rate_low_g_m2_day: float | None = None    # the same corners, per unit bed area
    areal_removal_rate_high_g_m2_day: float | None = None
    #: Flow-weighted concentration leaving the bed, `C_in (1 - E)`. Carried so the efficiency can be
    #: presented as the concentration reduction it algebraically is, rather than a bare fraction.
    outlet_concentration_mg_l: float | None = None
    #: Removal normalized per kilometre of channel: the scale a manager extrapolates with, and the
    #: same normalization the connectivity headline (turnovers per km) already uses.
    removal_per_km_kg_day: float | None = None
    removal_per_km_low_kg_day: float | None = None
    removal_per_km_high_kg_day: float | None = None
    reach_length_m: float | None = None
    # rate-free outputs, which stand alone if the rate constants are disowned
    fraction_above_threshold: float | None = None
    fraction_below_threshold: float | None = None
    reactive_exposure_m3: float | None = None
    opportunity_curve: list[OpportunityPoint] = Field(default_factory=list)
    # supporting
    areal_rate_active_g_m2_day: float | None = None
    chain_closure_rel_diff: float | None = None


class NutrientScreening(ReactiveScreening):
    """Denitrification, which is the oxygen-gated case.

    `time_to_anoxia_hours` is DERIVED from the dissolved oxygen the user supplies and a cited
    consumption rate; it is not an input. That is the point of the oxygen gate: onset time is not a
    quantity anyone can estimate, but stream dissolved oxygen is."""

    process_key: str = "denitrification"
    #: Whether the gate was applied at all. False is a deliberate screening posture -- what the
    #: reach could transform if oxygen never had to be consumed first -- and it makes the three
    #: fields below and `time_to_anoxia_hours` all None, so a reader cannot mistake the absence of
    #: an onset for missing data. `oxygen_gate_note` says so in words wherever the numbers appear.
    oxygen_gate: bool | None = None
    oxygen_gate_note: str | None = None
    dissolved_oxygen_mg_l: float | None = None
    anoxic_threshold_mg_l: float | None = None
    oxygen_consumption_mg_l_day: float | None = None
    time_to_anoxia_hours: float | None = None               # derived, not entered
    nitrate_basis: str | None = None                        # always "N" from the app; see below
    nitrate_basis_label: str | None = None
    #: Nitrogen-equivalent mass. The app pins the basis to nitrogen, so this equals
    #: `total_removed_kg_day`; it stays distinct because the calculation still accepts an as-NO3
    #: basis from an API caller, and a cross-site table must never mix the two silently.
    total_removed_kg_n_day: float | None = None
    #: First-order validity. `k · C` has no ceiling while real denitrification saturates, so the
    #: Monod half-saturation constant bounds where the fit is still being interpolated rather than
    #: extrapolated. `first_order_validity_note` is None whenever the ratio is at or below 1.
    monod_half_saturation_mg_l: float | None = None
    saturation_ratio: float | None = None
    implied_zero_order_rate_mg_l_day: float | None = None
    first_order_validity_note: str | None = None


class ContaminantScreening(ReactiveScreening):
    """A contaminant attenuating first-order with no redox gate, from the cited endpoint library
    or from a rate the user supplies.

    NOTHING HERE IS DESTRUCTION. The metals endpoints are sorption to newly forming manganese
    oxides, which Fuller and Bargar observed reversing as pH fell; the organics are
    biotransformation. The screening reference's §7 terminology table is carried in
    `pollutants.TERMS` and reaches the pane through `headline_label` and its siblings, so a metal
    never renders the word removal."""

    process_key: str = "contaminant"
    contaminant_name: str | None = None
    #: Which cited endpoint produced this, and what kind it is. None for a user-supplied rate.
    preset_key: str | None = None
    preset_label: str | None = None
    endpoint_type: str | None = None                        # "metal" | "organic"
    #: Reference rule 2. False only where the authors reported the rate in /day themselves; almost
    #: everything here is a unit conversion the screening reference performed.
    rate_derived: bool | None = None
    endpoint_stable: bool | None = None                     # cited as non-degrading; k = 0 is real
    concentration_unit: str | None = None
    concentration_basis: str | None = None
    #: The §7 vocabulary in effect, carried so the report renders the same words as the pane.
    headline_label: str | None = None
    areal_label: str | None = None
    per_km_label: str | None = None
    mass_label: str | None = None
    total_mass_unit: str | None = None
    areal_rate_unit: str | None = None
    per_km_unit: str | None = None
    # ---- exchange limitation (reference §4.3-§4.4, rule 14) -------------------
    #: `Da = k * T50`. Above ~100 the answer is the exchange flux restated and the rate constant
    #: carries no information; below ~0.01 water leaves before reacting.
    t50_days: float | None = None
    damkohler: float | None = None
    damkohler_regime: str | None = None
    damkohler_note: str | None = None
    #: `Q_HZ / Q_str`, and what it does to the stream. `outlet_concentration_mg_l` above is the
    #: RETURNING water; the stream sees this instead, and rule 5 forbids conflating them.
    exchange_ratio: float | None = None
    reach_removal_fraction: float | None = None
    stream_concentration_change_mg_l: float | None = None
    stream_outlet_concentration_mg_l: float | None = None
    #: Distance for a 1/e reduction, and the same in reach lengths. Grant et al. computed 275 km
    #: for a medium sand-bed stream, so a screening tool returning large sub-kilometre benefits is
    #: misparameterized.
    processing_length_m: float | None = None
    processing_length_reaches: float | None = None
    # ---- guards the reference will not let a result ship without --------------
    eligibility_conditions: list[str] = Field(default_factory=list)   # rule 6
    calibration_note: str | None = None                               # rule 7
    depth_note: str | None = None                                     # rule 9
    preset_note: str | None = None


class MicroplasticRetention(_ScreeningBase):
    """Physical retention of microplastic in the bed. A SEPARATE CALCULATION FAMILY.

    Distance-driven, never time-driven: deep-bed filtration removes a fixed fraction per unit of
    travel through the medium, and Munz et al. (2024) measured retention profiles that did not
    change with flow duration beyond about two pore volumes. Reference rule 1 forbids a per-day
    coefficient anywhere near this model, and `registry.validate_registry` enforces it.

    TWO READINGS THAT ARE NEVER SUMMED (rule 11). `retained_fraction` is the reported number, a
    reach-scale empirical coefficient on stream distance. `path_capture_fraction` is a capability
    diagnostic on subsurface path length, and it saturates: even the weakest measured filtering
    captures 83% within 10 cm. The gap between them is remobilization by bed turnover, which
    nothing here represents, and is why every label says retention rather than removal."""

    process_key: str = "microplastic"
    module: str = "particulate"
    independent_variable: str = "distance"
    # ---- Tier A: reach scale, the reported number ----------------------------
    retained_fraction: float | None = None
    retained_fraction_low: float | None = None
    retained_fraction_high: float | None = None
    alpha_mp_per_km: float | None = None
    reach_length_m: float | None = None
    # ---- Tier B: size gate, then capture along a flow path -------------------
    particle_size_um: float | None = None
    median_grain_size_mm: float | None = None
    size_ratio: float | None = None                     # d_p / d50
    size_gate: str | None = None                        # attachment | straining | excluded
    size_gate_note: str | None = None
    #: Too large to enter the pore network. These deposit at the interface by a different and far
    #: more remobilizable mechanism, and are reported separately rather than folded in (rule 13).
    interface_deposition: bool | None = None
    lambda_f_per_cm: float | None = None
    path_capture_fraction: float | None = None
    path_capture_low: float | None = None
    path_capture_high: float | None = None
    capture_cap: float | None = None                    # 0.977, rule 12
    path_length_p50_m: float | None = None
    tier_b_reason: str | None = None


class HabitatScreening(_ScreeningBase):
    """Physical extent of the hydraulically connected zone. No kinetics, no rate, no mass.

    The headline is PORE-WATER volume, not bulk sediment: the water-filled space is what an organism
    could occupy. Framework §4.6 names mixing the two bases as a specific failure, so both ship with
    the basis stated. This is potential habitat volume, never habitat quality (framework §7.5)."""

    process_key: str = "habitat"
    habitable_pore_volume_m3: float | None = None           # headline
    bulk_volume_m3: float | None = None
    volume_basis: str | None = None
    porosity: float | None = None
    # The two pore-basis normalizations, which are what the pane headlines. They differ only in
    # their denominator, and the identity between them is worth knowing:
    #   pore_equivalent_depth_m == pore_depth_active_m * connected_streambed_fraction
    # CONNECTED, not active: this comment named A_active until the coverage headline became the
    # union of the entry and return sides, and the depth's denominator followed it there so the
    # identity would keep holding. `screen.screen_extent` divides by the connected area.
    pore_equivalent_depth_m: float | None = None            # pore volume / A_bed
    pore_depth_active_m: float | None = None                # pore volume / A_connected
    equivalent_active_depth_m: float | None = None          # D_HZ, BULK basis (framework §8)
    active_streambed_area_m2: float | None = None
    return_streambed_area_m2: float | None = None
    connected_streambed_area_m2: float | None = None
    streambed_area_m2: float | None = None
    active_streambed_fraction: float | None = None          # framework F_active,bed, entry only
    connected_streambed_fraction: float | None = None       # the pane headline: entry OR exit
    path_depth_p50_m: float | None = None
    path_depth_p90_m: float | None = None
    # Resolution of the ZONE pass, which is what the volume rests on -- never the interface pass
    # whose path count `_ScreeningBase` carries.
    zone_particles_per_cell: int | None = None
    zone_seeds: int | None = None
    zone_cells_seeded: int | None = None
    zone_classified_fraction: float | None = None


class ThermalOpportunity(_ScreeningBase):
    """Temperature regulation (thermal plan §5, §9). Buffering opportunity only.

    Carries NO degrees and NO reach temperature change, by design: stream temperature is set mainly
    by the surface energy budget, which this does not model (thermal plan §10.1-§10.2). Every value
    here is a fraction, a flow, or a dimensionless ratio."""

    process_key: str = "thermal_regulation"
    response_time_hours: float | None = None
    retardation_factor: float | None = None
    buffering_opportunity: float | None = None              # B_Q
    buffering_opportunity_low: float | None = None
    buffering_opportunity_high: float | None = None
    remaining_anomaly_fraction: float | None = None         # A_Q
    attenuation_weighted_flow_cms: float | None = None      # Q_TB
    attenuation_weighted_flow_l_s: float | None = None
    attenuation_weighted_connectivity_per_km: float | None = None   # C_TB
    attenuation_weighted_exchange_ratio: float | None = None        # L_TB
    fraction_above_1tau: float | None = None
    fraction_above_2tau: float | None = None
    fraction_above_3tau: float | None = None
    #: Full-diel storage opportunity (thermal plan §5.5): flow held past 24 h, FIXED rather than a
    #: multiple of the response time, so it is the one persistence number that survives changing
    #: the scenario. Storage opportunity, never a predicted 24-hour temperature lag.
    fraction_above_diel: float | None = None
    thermal_damkohler_median: float | None = None
    #: `Da_T = T50 / tau` read back as a sentence, same field names as the solute sections so the
    #: pane and the report render it with no per-section branch. Above ~3 the damped share is
    #: pinned at its ceiling and only the returning flow beside it can tell two reaches apart.
    damkohler_regime: str | None = None
    damkohler_note: str | None = None
    response_bands: list[dict] = Field(default_factory=list)
    rtd_storage_m3: float | None = None
    attenuation_weighted_storage_m3: float | None = None
    storage_buffered_fraction: float | None = None
    storage_cross_check_rel_diff: float | None = None


class FunctionScreening(HypeModel):
    """Container for the screening-tier sections, one field per process.

    All optional with defaults, so a section that was not run serialises cleanly and adding the
    next function needs no schema bump -- which is exactly what happened when `microplastic`
    joined: an older payload simply carries None there."""

    nutrient: NutrientScreening | None = None
    pollutant: ContaminantScreening | None = None
    #: EVERY dissolved endpoint that was screened, in checklist order. `pollutant` above is the
    #: first of them, kept because every existing reader and every saved payload names it; a
    #: payload written before the section became a multi-select carries that field and an empty
    #: list, which `dissolved_endpoints()` resolves. Each element self-identifies by `preset_key`,
    #: so nothing outside needs a parallel list of names.
    pollutants: list[ContaminantScreening] = Field(default_factory=list)
    microplastic: MicroplasticRetention | None = None
    habitat: HabitatScreening | None = None
    thermal: ThermalOpportunity | None = None

    def dissolved_endpoints(self) -> list[ContaminantScreening]:
        """The dissolved-phase sections to display, oldest payloads included."""
        if self.pollutants:
            return list(self.pollutants)
        return [self.pollutant] if self.pollutant is not None else []


class AssessmentResultsV2(HypeModel):
    """The single source of truth read by the report modal and all exports (§11.2)."""

    schema_version: str = RESULTS_SCHEMA_VERSION
    assessment_id: str
    input_hash: str
    input_snapshot: AssessmentInputSnapshot | None = None
    group_hashes: dict = Field(default_factory=dict)    # frozen §4.3 hashes for staleness

    connectivity: ConnectivityMetrics = Field(default_factory=ConnectivityMetrics)
    residence_time: ResidenceTimeMetrics = Field(default_factory=ResidenceTimeMetrics)
    zone: ZoneMetrics = Field(default_factory=ZoneMetrics)
    thresholds: list[ThresholdResult] = Field(default_factory=list)
    functions: FunctionScreening | None = None

    alternatives: HydraulicAlternativesManifest | None = None

    warnings: list[HypeWarning] = Field(default_factory=list)
    untested_uncertainty: list[str] = Field(default_factory=list)   # §10.6
    quality_diagnostics: dict = Field(default_factory=dict)
    artifact_paths: dict = Field(default_factory=dict)              # figures + tables
    report_status: str | None = None                                # "generated" | "failed" | None
    created_at: datetime | None = None


__all__ = [
    "ConnectivityMetrics", "ResidenceTimeMetrics", "ZoneMetrics", "ThresholdResult",
    "OpportunityPoint", "ReactiveScreening", "NutrientScreening", "ContaminantScreening",
    "HabitatScreening", "ThermalOpportunity", "FunctionScreening",
    "AssessmentResultsV2", "RESULTS_SCHEMA_VERSION",
]
