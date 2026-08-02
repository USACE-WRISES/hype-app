# HYPE Thermal-Regulation Opportunity Screening Plan, Version 1.1

**Date:** July 28, 2026  
**Status:** Proposed implementation plan  
**Primary use:** Hydraulics-based screening for planning, restoration, and research prioritization  
**Revision 1.1:** Corrected the Fogg et al. (2023) citation, added citation verification status
(§13.4) and codebase alignment (§13.5). Section 6 is blocked pending a small engine change to
persist the return cell per particle. The results-schema bump in §9 is reconciled with the nutrient
screen so there is one migration rather than two.  
**Companion:** `HYPE_hyporheic_functions_plan_v1.md` is the master plan. This document is the
thermal specification it references; the nutrient (denitrification) specification lives there.

## 1. Executive recommendation

HYPE should remain hydraulics-first for its current journal-paper and planning applications. The hydraulic model can support a defensible screening-level assessment of thermal-regulation opportunity using flux-weighted residence-time distributions (RTDs), exchange-flow magnitude, return locations, and mobile-storage estimates.

The screening assessment should answer:

> How much returning hyporheic flow has sufficient residence time to attenuate diel temperature variations, where does that water return, and how much exchange flow and mobile storage support that opportunity?

It should not claim to predict actual stream or return-flow temperatures. Without measured temperature boundary conditions, sediment thermal properties, atmospheric forcing, and calibration data, HYPE cannot defensibly calculate degrees of cooling, whole-reach temperature changes, or thermal-refuge habitat.

A two-tier strategy is recommended:

1. **Screening tier:** Apply the calculations in this plan to existing hydraulic results. Use literature-based thermal-response times as explicitly labeled scenarios.
2. **Research tier:** At future intensively monitored sites, use temperature loggers, tracer data, and a calibrated heat-transport model to test and replace the screening assumptions.

The screening tier can be implemented without waiting for field chemistry or temperature data. The research tier is a strong potential role for collaboration with Texas State University.

## 2. Evidence and interpretation boundary

The screening results represent **thermal-regulation opportunity**, not a predicted thermal benefit in degrees Celsius.

Residence time is relevant because heat exchange between pore water and the surrounding sediment can damp diel temperature fluctuations as water travels through the hyporheic zone. Both water age and the amount of exchange flow matter; exchange rate alone is not sufficient to characterize thermal influence ([Fogg et al., 2023](https://doi.org/10.1002/hyp.14973)). That study is also the clearest statement of why this screen must not report degrees: it compares hyporheic exchange against shade as competing controls on stream temperature, so hyporheic influence cannot be converted to a reach temperature without the surface energy budget.

Field studies also show why the results must be described carefully:

- Hyporheic discharge can have a compressed diel temperature range and delayed timing even when its daily mean temperature is close to that of the stream ([Arrigoni et al., 2008](https://doi.org/10.1029/2007WR006480)).
- Local hyporheic temperature differences and thermal heterogeneity do not necessarily produce a substantial whole-stream cooling effect ([Hester et al., 2009](https://doi.org/10.4319/lo.2009.54.1.0355)).

Consequently, the screen should distinguish:

- **Buffering quality:** the potential degree of diel-signal attenuation within returning exchange water.
- **Buffering capacity:** the amount of returning exchange flow associated with that attenuation.
- **Thermal-mosaic opportunity:** spatial diversity in the modeled response of return flows.
- **Storage and persistence:** the amount of mobile exchange water and return flow associated with long residence times.

These components should remain separate. They should not be combined into a universal thermal-benefit score.

## 3. Required hydraulic inputs

The calculations require path-level or RTD-level hydraulic results:

- Residence time, \(t_i\), in hours.
- Exchange-flow weight, \(q_i\), in m³/s.
- Path status identifying resolved returning, censored, unresolved, or non-returning paths.
- Return cell or reporting-zone identifier.
- Maximum path depth, when available.
- Existing exchange-connectivity metric, \(C_{1km}\), when available.
- Stream discharge, \(Q_{stream}\), when available.
- Existing active mobile-water or mobile-pore-volume estimate, when available.

### 3.1 Weighting and quality-control rules

- Calculate all percentages using exchange-flow weights, never particle counts.
- Use only resolved returning paths in completed-return thermal calculations.
- Do not assign an assumed residence time or completed thermal response to censored paths.
- Report the percentage of modeled exchange flux that is censored or unresolved.
- Retain the existing prominent warning when censored or unresolved flux exceeds 20%.
- Require \(q_i>0\), \(t_i\ge0\), and \(\sum q_i>0\).
- Preserve residence-time and flow units in result metadata.
- Describe all results as being on a “resolved-return-flow basis.”

## 4. Literature-based thermal-response scenarios

### 4.1 First-order response calculation

For screening, represent attenuation of a diel temperature anomaly along flow path \(i\) with:

\[
b_i(\tau)=1-\exp(-t_i/\tau)
\]

where:

- \(b_i\) is the idealized buffering-opportunity fraction;
- \(t_i\) is the modeled hydraulic residence time; and
- \(\tau\) is an assumed effective thermal-response time.

The corresponding fraction of the original anomaly remaining is:

\[
a_i(\tau)=\exp(-t_i/\tau)=1-b_i(\tau)
\]

This is a deliberately simplified first-order response model. It is suitable for sensitivity screening but is not a substitute for solving advective, conductive, and dispersive heat transport.

### 4.2 Default response-time scenarios

Use the following scenarios:

| Scenario | Response time, \(\tau\) | Use |
|---|---:|---|
| Fast response | 4 h | Upper buffering-opportunity sensitivity |
| Reference response | 8 h | Primary reporting scenario |
| Slow response | 16 h | Lower buffering-opportunity sensitivity |

The 8-hour reference is an approximate literature-parameter scenario derived from the periodic heat-transport formulation and representative parameters reported by [Marzadri et al. (2013)](https://doi.org/10.1002/wrcr.20199). It is not a universal value. The 4- and 16-hour cases are factor-of-two sensitivity bounds, not confidence intervals.

The assumed response times must remain configurable and be stored with the results so that future site-calibrated values can replace them without changing the calculation framework.

### 4.3 Reference values for implementation

For the 8-hour reference response:

| Residence time | \(t/\tau\) | Idealized attenuation, \(1-e^{-t/\tau}\) |
|---:|---:|---:|
| 1 h | 0.125 | 11.8% |
| 4 h | 0.5 | 39.3% |
| 6 h | 0.75 | 52.8% |
| 8 h | 1.0 | 63.2% |
| 12 h | 1.5 | 77.7% |
| 16 h | 2.0 | 86.5% |
| 24 h | 3.0 | 95.0% |

## 5. Screening calculations

### 5.1 Diel temperature-amplitude buffering opportunity

Calculate the flux-weighted, RTD-integrated buffering opportunity:

\[
B_Q(\tau)=\frac{\sum_i q_i b_i(\tau)}{\sum_i q_i}
\]

Also calculate the remaining idealized anomaly fraction:

\[
A_Q(\tau)=1-B_Q(\tau)
\]

Report \(B_Q\) for the 4-, 8-, and 16-hour scenarios. The 8-hour result is the reference estimate, while the 4- to 16-hour range communicates sensitivity to the uncalibrated response time.

**Interpretation:** \(B_Q\) describes the relative potential for the RTD to damp a diel temperature signal in returning exchange water. It does not predict attenuation in degrees Celsius because the entering temperature signal and thermal properties are not modeled.

### 5.2 Attenuation-weighted exchange flow

Calculate:

\[
Q_{TB}(\tau)=\sum_i q_i b_i(\tau)
\]

Equivalently:

\[
Q_{TB}(\tau)=Q_{HEF}B_Q(\tau)
\]

where \(Q_{HEF}=\sum_i q_i\) is resolved returning hyporheic exchange flow.

Report \(Q_{TB}\) in m³/s and L/s. This combines buffering quality with the amount of returning flow and prevents a site with high attenuation but negligible exchange flow from appearing unduly influential.

Call this result **attenuation-weighted return flow**, not cooled flow.

### 5.3 Attenuation-weighted connectivity

Where the existing connectivity result is available, calculate:

\[
C_{TB}(\tau)=C_{1km}B_Q(\tau)
\]

Report this as **attenuation-weighted exchange connectivity per kilometer**. It is a comparative planning metric, not a reach-temperature prediction.

### 5.4 Stream-leverage indicator

When stream discharge is available, optionally calculate:

\[
L_{TB}(\tau)=\frac{Q_{TB}(\tau)}{Q_{stream}}
\]

Call this the **attenuation-weighted exchange ratio**.

Do not:

- Interpret it as percent stream cooling.
- Clamp it to 100%.
- Convert it directly to a mixed-channel temperature.

Gross exchange can contain recirculating water, and an exchange ratio greater than one does not imply an equivalent reach-scale temperature response.

### 5.5 Thermal-signal storage and persistence

For the 8-hour reference scenario, report the flux-weighted fractions of returning flow with:

- \(t_i\ge8\) h: at least one reference response time and at least 63.2% idealized attenuation.
- \(t_i\ge16\) h: at least two response times and at least 86.5% idealized attenuation.
- \(t_i\ge24\) h: at least three response times and at least 95.0% idealized attenuation.

Describe the \(t_i\ge24\)-hour result as **full-diel storage opportunity**. Do not describe it as a predicted 24-hour temperature lag.

Also report the flux-weighted median thermal Damköhler-style indicator:

\[
Da_{T,50}=\frac{T_{50}}{8\ \mathrm{h}}
\]

This follows the concept of comparing hyporheic residence time with a characteristic thermal-response time ([Marzadri et al., 2013](https://doi.org/10.1016/j.jhydrol.2013.10.030)). Report the numeric value rather than assigning a site-quality grade.

### 5.6 Path and map response bands

Use these reference-response bands for path maps, legends, and supporting tables:

| Residence time | Reference \(Da_T\) | Idealized response | Label |
|---|---:|---:|---|
| <4 h | <0.5 | <39.3% | Diel-coupled |
| 4 to <8 h | 0.5 to <1 | 39.3% to <63.2% | Transitional |
| 8 to <16 h | 1 to <2 | 63.2% to <86.5% | Buffered opportunity |
| ≥16 h | ≥2 | ≥86.5% | Strong buffering opportunity |

These are mathematical response bands, not ecological-quality or regulatory classes.

## 6. Thermal heterogeneity and thermal-mosaic opportunity

Hydraulic results cannot predict actual spatial temperature differences in degrees Celsius. They can, however, identify whether returning flow is concentrated in locations with similar or diverse residence-time-based responses.

### 6.1 Return-zone calculations

Aggregate resolved returning paths by return cell or common reporting zone \(j\):

\[
Q_j=\sum_{i\rightarrow j}q_i
\]

\[
B_j(\tau)=\frac{\sum_{i\rightarrow j}q_i b_i(\tau)}{Q_j}
\]

For the 8-hour reference scenario:

- Map \(B_j\) using color.
- Represent \(Q_j\) using symbol size, line weight, or opacity.
- Preserve the native return-cell identifier and spatial resolution in exported results.
- Allow aggregation to a common reporting-zone layer for comparisons, but do not compare cell-level spreads across different grid resolutions.

### 6.2 Response-diversity statistics

Calculate flow-weighted quantiles of \(B_j\) across return zones:

- \(B_{10}\)
- \(B_{50}\)
- \(B_{90}\)

Calculate:

\[
\Delta B_{80}=B_{90}-B_{10}
\]

Call \(\Delta B_{80}\) **return-flow response diversity**.

Also report the percentage of resolved return flow in each of the four response bands. Do not report \(\Delta B_{80}\) when fewer than three populated return zones are available.

### 6.3 Interpretation

The return map and response spread indicate **thermal-mosaic opportunity**:

- A wide spread indicates that different return locations may respond differently to the diel temperature signal.
- A narrow spread indicates relatively uniform residence-time-based response.
- High spatial diversity is not automatically beneficial; its value depends on actual temperatures, season, connectivity to aquatic organisms, and the location of sensitive habitats.

Do not call the calculated spread actual thermal heterogeneity. Actual heterogeneity requires temperature observations or a calibrated heat-transport model.

## 7. Buffered mobile-water storage opportunity

When particle or path flow weights represent non-overlapping, partitioned streamtubes, estimate RTD-derived mobile-water storage using:

\[
V_{RTD}=3600\sum_i q_i t_i
\]

where flow is in m³/s and residence time is in hours.

Calculate attenuation-weighted mobile storage:

\[
V_{TB}(\tau)=3600\sum_i q_i t_i b_i(\tau)
\]

and its corresponding fraction:

\[
B_V(\tau)=\frac{V_{TB}(\tau)}{V_{RTD}}
\]

Interpret these as:

- \(V_{RTD}\): RTD-derived mobile exchange-water storage.
- \(V_{TB}\): attenuation-weighted mobile-water storage.
- \(B_V\): fraction of modeled mobile storage associated with thermal-buffering opportunity.

If an independent active mobile-water estimate exists, compare it with \(V_{RTD}\). A difference greater than 20% should produce an internal-consistency warning. When no independent check exists, label the results **unverified RTD-derived storage proxies**.

Do not describe \(V_{TB}\) as thermal-refuge or habitat volume.

### 7.1 Optional depth corroboration

If maximum path depth is available, optionally report the flow and storage fractions satisfying both:

- Residence time ≥8 hours; and
- Maximum path depth ≥0.15 m.

The 0.15 m criterion is an approximate diel conductive-penetration sensitivity value, based on representative sediment thermal diffusivity. Label it **diel-isolation opportunity**, not proof of thermal isolation. Report it only as a supporting diagnostic, not a headline result.

## 8. Recommended result presentation

Do not create one composite thermal score. Present four complementary result groups:

1. **Buffering quality**
   - \(B_Q\) for 4-, 8-, and 16-hour response scenarios.
   - Remaining anomaly fraction, \(A_Q\).

2. **Buffering capacity**
   - \(Q_{TB}\) in L/s.
   - \(C_{TB}\) per kilometer.
   - Optional \(L_{TB}\).

3. **Thermal-mosaic opportunity**
   - Return-zone map.
   - \(B_{10}\), \(B_{50}\), \(B_{90}\), and \(\Delta B_{80}\).
   - Flow fractions in each response band.

4. **Persistence and storage**
   - Flow fractions with residence times ≥8, ≥16, and ≥24 hours.
   - \(Da_{T,50}\).
   - \(V_{TB}\) and \(B_V\), when valid.

### 8.1 Standard narrative

Use wording similar to:

> Under the 8-hour literature-parameter scenario, X% of resolved returning exchange exceeded one thermal response time, and the RTD-integrated diel-buffering opportunity was Y% (Z1–Z2% across the 4–16-hour sensitivity scenarios). This represents Q L/s of attenuation-weighted return flow. Return-zone responses ranged from B10 to B90, indicating the modeled spatial diversity in buffering opportunity. These screening results do not estimate degrees of cooling or reach-average temperature change.

### 8.2 Scenario and restoration comparisons

For alternatives, report changes in:

- \(B_Q\), in percentage points.
- \(Q_{TB}\), in L/s.
- \(C_{TB}\), per kilometer.
- Flow fraction with residence time ≥24 hours.
- \(\Delta B_{80}\).
- \(V_{TB}\) and \(B_V\), when valid.

Avoid ranking alternatives solely by \(B_Q\). An alternative should be evaluated using both buffering quality and the quantity and spatial distribution of affected return flow.

## 9. Results contract and exports

When implemented in the application, add a nullable `thermal_opportunity` object to `AssessmentResultsV2` and increment the result schema from version 2.1 to 2.2. Older results should deserialize with `thermal_opportunity: null`.

Each response-time scenario should store:

- Scenario identifier and label.
- Thermal-response time in hours.
- Evidence level and source.
- Required conditions and transferability warning.
- \(B_Q\) and \(A_Q\).
- \(Q_{TB}\), \(C_{TB}\), and optional \(L_{TB}\).
- Flow fractions above scenario and reference thresholds.
- Censored and unresolved flow fractions.

The thermal-opportunity result should also contain:

- Return-zone response statistics.
- Storage metrics and validation status.
- Calculation-basis statement.
- Model/version identifier such as `first_order_rtd_v1`.
- Required limitations text.

Keep the existing hydraulic headline metrics unchanged. Add a separate `thermal_opportunity.csv` export rather than placing all thermal fields in the core site-metrics table.

## 10. Outcomes not included in the screening tier

The following outcomes cannot be predicted defensibly from hydraulic RTDs and literature response times alone and are therefore excluded:

### 10.1 Absolute temperature outcomes

- Return-water temperature in °C.
- Mean warming or cooling.
- Daily maximum reduction or minimum increase.
- Diel temperature-range reduction in °C.
- Actual thermal amplitude attenuation in °C.
- Actual phase lag in hours.

These require measured temperature boundary conditions and site-specific heat-transfer properties.

### 10.2 Heat loads and reach-scale effects

- Heat or thermal-energy flux in W, MJ/day, or equivalent units.
- Reach-average or downstream temperature change.
- Thermal-load reduction.
- Compliance with temperature standards, TMDLs, or other regulatory criteria.

These require a stream heat budget, channel routing, atmospheric exchange, groundwater temperatures, and calibrated mixing calculations.

### 10.3 Ecological outcomes

- Cold- or cool-water refuge area or volume.
- Hours meeting a biological temperature threshold.
- Fish or invertebrate habitat suitability.
- Species occupancy, survival, or productivity.

These require actual temperatures, spatial connectivity, organism-specific thresholds, and biological observations.

### 10.4 Seasonal and external-driver effects

- Seasonal or annual cooling and warming.
- Climate resilience.
- Long-term groundwater-temperature effects.
- Thermopeaking response.
- Effects of groundwater or lateral inflow.
- Effects of shade, radiation, air temperature, humidity, or wind.

These require time-varying boundary conditions and coupled surface-water heat-budget modeling.

### 10.5 Coupled water-quality effects

- Dissolved-oxygen response.
- Metabolic-rate changes.
- Denitrification changes caused by temperature.
- Other coupled thermal-biogeochemical outcomes.

These may be discussed as literature-supported hypotheses or co-benefits, but they should not be assigned numeric values without appropriate observations and calibrated process models.

## 11. Texas State research and modeling pathway

The screening calculations provide a useful base for a future academic research program without making the current HYPE application dependent on intensive data collection.

### 11.1 Field-data progression

At selected research sites:

1. Install continuous stream-temperature and multi-depth streambed-temperature loggers.
2. Measure groundwater, lateral-inflow, and upstream temperature boundary conditions.
3. Conduct tracer tests to constrain exchange flux and residence-time distributions.
4. Characterize porosity, bulk density, thermal conductivity, heat capacity, and dispersivity.
5. Collect discharge, channel-geometry, shade, and meteorological data where reach-scale effects are a research objective.
6. Consider paired nitrate and dissolved-oxygen observations when studying coupled thermal and biogeochemical functions.

### 11.2 Modeling progression

Use the following maturity ladder:

1. **Uncalibrated screening:** Apply the calculations in this plan.
2. **Research heat model:** Develop a native MODFLOW 6 Groundwater Energy model for site-specific heat transport.
3. **Calibrated model:** Calibrate hydraulic and thermal parameters to tracer and temperature data.
4. **Validated model:** Evaluate the model against an independent monitoring period.
5. **Decision model:** Add routed channel heat transport and uncertainty analysis before predicting reach-scale or regulatory outcomes.

MODFLOW 6 GWE should be the preferred future heat-transport platform rather than using an MT3DMS heat analogy as the principal long-term approach.

The calibrated research models should be used to:

- Test whether the 4-, 8-, and 16-hour response scenarios bracket observed behavior.
- Derive site-specific response times.
- Determine when RTD-only screening succeeds or fails.
- Develop transferable screening relationships for future planning applications.
- Establish the additional evidence required before making ecological or regulatory claims.

## 12. Implementation sequence

### Phase 1 — Calculation prototype

- Implement the three response-time scenarios from resolved, flux-weighted RTDs.
- Calculate \(B_Q\), \(A_Q\), \(Q_{TB}\), \(C_{TB}\), and reference residence-time fractions.
- Add calculation metadata and limitations.
- Verify calculations against hand-worked examples.

### Phase 2 — Spatial and storage outputs

- Aggregate responses by return cell.
- Add the thermal-mosaic map and response-diversity statistics.
- Add RTD-derived storage metrics with provenance and consistency checks.
- Add optional depth corroboration.

### Phase 3 — Reporting and comparison

- Add the thermal-opportunity report section.
- Add JSON and separate CSV exports.
- Support comparison of alternatives using changes in quality, capacity, persistence, diversity, and storage.
- Preserve the existing hydraulic headline results.

### Phase 4 — Research calibration

- Use monitored Texas State sites to develop and calibrate MODFLOW 6 GWE models.
- Compare simulated thermal responses with screening results.
- Update response-time profiles while preserving the original literature-based profile for reproducibility.

## 13. Verification and acceptance tests

### 13.1 Hand calculation

For residence times `[4, 8, 24]` hours, flow weights `[1, 2, 1]`, and \(\tau=8\) hours:

- \(B_Q=0.6519808474\).
- \(A_Q=0.3480191526\).
- \(P(t\ge8\ \mathrm{h})=0.75\).
- \(P(t\ge24\ \mathrm{h})=0.25\).

If \(Q_{HEF}=0.12\) m³/s:

- \(Q_{TB}=0.0782377017\) m³/s.

If \(C_{1km}=0.16\) per kilometer:

- \(C_{TB}=0.1043169356\) per kilometer.

### 13.2 Behavioral tests

- \(B_Q(4\ \mathrm{h})\ge B_Q(8\ \mathrm{h})\ge B_Q(16\ \mathrm{h})\).
- All calculated fractions remain between zero and one.
- Multiplying all flow weights by a constant leaves fractional metrics unchanged.
- Multiplying all flow weights by a constant scales \(Q_{TB}\), \(V_{RTD}\), and \(V_{TB}\) by that constant.
- Identical return-zone responses produce \(\Delta B_{80}=0\).
- Fewer than three populated return zones suppress \(\Delta B_{80}\).
- Missing return-location data suppress only thermal-mosaic outputs.
- Missing stream discharge suppresses only \(L_{TB}\).
- Invalid storage provenance suppresses \(V_{TB}\) and \(B_V\) without affecting the other thermal metrics.
- Censored paths are never treated as completed returning paths.
- Existing hydraulic results remain unchanged when thermal screening is unavailable or disabled.

### 13.3 Scientific-language tests

Reports must not describe screening results as:

- Degrees of cooling.
- Predicted stream temperature.
- Verified thermal refuge.
- Habitat created or improved.
- Pollutant removal.
- Regulatory credit.

Every report must identify the assumed response time, its evidence level, the resolved-return basis, the censored fraction, and the lack of temperature-model calibration.

## 13.4 Citation verification status

Checked against the published record on 2026-07-28. Anything marked unverified should be confirmed
against the source before it reaches a manuscript.

| Reference | Status |
|---|---|
| Marzadri, Tonina and Bellin (2013), *WRR*, 10.1002/wrcr.20199 | **Verified.** Title and DOI correct. Develops a Lagrangian heat-transport model including conduction, diffusion and advection, validated against Bear Valley Creek, Idaho. Appropriate basis for the response-time scenarios. |
| Fogg et al. (2023), *Hydrological Processes*, 10.1002/hyp.14973 | **Corrected.** The DOI was right but the author list and title were wrong. Actual: Fogg, S. K., Reinhold, A. M., O'Daniel, S. J., Hyman, A. A., and Poole, G. C., "Thermal insulation versus capacitance: A simulation experiment comparing effects of shade and hyporheic exchange on daily and seasonal stream temperature cycles," 37(9), e14973. Note this is S. K. Fogg, not G. E. Fogg. |
| Arrigoni et al. (2008), *WRR*, 10.1029/2007WR006480 | Unverified. Title and framing are consistent with the known literature. |
| Hester, Doyle and Poole (2009), *L&O*, 10.4319/lo.2009.54.1.0355 | Unverified. Consistent with the known literature. |
| Marzadri, Tonina and Bellin (2013), *J. Hydrol.*, 10.1016/j.jhydrol.2013.10.030 | Unverified. Note its stated implication for dissolved-oxygen dynamics makes it a cross-cutting reference for the nutrient screen's anoxic-onset parameter as well. |

## 13.5 Implementation alignment with the current codebase

Five points where this plan meets the existing application. The first is a blocker.

### 13.5.1 Return-zone data does not exist yet (blocks section 6)

Section 6 requires a return cell identifier per path. **The per-particle ledger does not carry one.**
`hz_analysis.py:682-686` writes exactly `source_node`, `weight`, `cls`, `time_days`, `status`,
`exit_code`, `origin_code`, plus an optional `max_depth_m`. `source_node` is the *release* cell, which
is where the particle downwelled, not where it returned.

The endpoint pass already knows the terminating cell, since that is what the returning/losing
classification is derived from; it simply is not persisted. **The fix is to add `return_node` to the
`per_particle` dict beside `source_node`**, which is a small engine change in the same function that
already computes it. Treat it as optional on read, exactly as `max_depth_m` and `origin_code` are
treated, so older artifacts degrade rather than break.

Until that lands, sections 6.1 through 6.3 and the thermal-mosaic map are not implementable. Every
other calculation in this plan runs on the existing artifacts unchanged.

### 13.5.2 Units differ from the plan's assumption

The plan writes flow weights in m3/s and residence time in hours, hence the 3600 factor in
`V_RTD = 3600 * sum(q_i * t_i)`. **The stored artifacts use m3/day and days.** `hz_flux.npz` carries
`weight` in m3/day and `time_days` in days, so the residence-volume sum is simply
`V_RTD = sum(w_i * T_i)` in m3 with no conversion.

Note also that `app.py:3503` divides those weights by `DAY` into m3/s before handing them to
`ExchangeAccounting`. Read the raw array, not the converted one, or every storage and capacity number
is wrong by 86400.

### 13.5.3 Most of the calculation machinery already exists

| Plan quantity | Existing implementation |
|---|---|
| Flow fractions at t >= 8, 16, 24 h (§5.5) | `metrics.exceedance_fraction(values, weights, threshold)` (`metrics.py:179`), already flux weighted and NaN safe |
| `B_10`, `B_50`, `B_90` across return zones (§6.2) | `metrics.weighted_quantile` (`metrics.py:35`) |
| `T_50` for `Da_T,50` (§5.5) | `ResidenceTimeMetrics.weighted_median_days` |
| `Q_HEF` (§5.2) | `ConnectivityMetrics.returning_hyporheic_cms` |
| `C_1km` for `C_TB` (§5.3) | `ConnectivityMetrics.turnovers_per_km` |
| `Q_stream` for `L_TB` (§5.4) | `ConnectivityMetrics.streamflow_cms` |
| Independent mobile-water estimate for the §7 cross-check | `ZoneMetrics.mobile_pore_storage_m3`, computed by `metrics.mobile_pore_storage` (`metrics.py:226`) |
| Censored fraction (§3.1) | `ConnectivityMetrics.censored_flow_fraction` and `ResidenceTimeMetrics.censored_fraction` |
| Returning-paths-only filter (§3.1) | Already applied upstream: `app.py:3497-3505` selects `cls == 1` |

The genuinely new calculation is `b_i(tau) = 1 - exp(-t_i/tau)` under flux weighting. That is one
helper, and it is the same helper the nutrient screen needs, so build it once in `metrics.py` beside
`exceedance_fraction`:

```python
def weighted_reaction_fraction(values, weights, *, timescale, onset=0.0) -> float:
    """Flux-weighted mean of 1 - exp(-(t - onset)/timescale), clamped at onset.

    The continuous analogue of exceedance_fraction: instead of counting flow above a
    threshold, it weights flow by how far past the threshold it goes. Monotone
    non-decreasing in 1/timescale, non-increasing in onset. B_Q(tau) is this with
    onset=0."""
```

### 13.5.4 Schema version collides with the nutrient screen

Section 9 proposes adding `thermal_opportunity` and incrementing the results schema from 2.1 to 2.2.
The nutrient screening plan independently proposes 2.1 to 2.2 for its own field. **Both cannot claim
2.2.**

Resolution: **one bump to `assessment-results/2.2` adding a single container**, with one registered
migration in `contracts/__init__.py:83-114` (`_drop_hfci_2_0` is the working precedent):

```python
class FunctionScreening(HypeModel):
    """Screening-tier functional estimates. Hydraulic opportunity under stated assumptions,
    never a calibrated measurement (framework §10.3)."""
    nutrient: NutrientScreening | None = None
    thermal: ThermalOpportunity | None = None
    # habitat: HabitatScreening | None = None    # extent-driven, later

# on AssessmentResultsV2, beside `thresholds`:
functions: FunctionScreening | None = None
```

This keeps one migration, leaves room for the extent-driven habitat function, and preserves this
plan's requirement that older results deserialize with the thermal object null. The separate
`thermal_opportunity.csv` export in section 9 is still the right call; do not widen the core
site-metrics table.

### 13.5.5 The 0.15 m diel criterion checks out

Section 7.1's penetration depth is analytically supportable and worth stating with its derivation
rather than as an assertion. The diel thermal penetration depth is `d = sqrt(2*kappa/omega)`, with
thermal diffusivity `kappa` of roughly 1e-6 m2/s for saturated sand and `omega = 2*pi/86400` s^-1,
giving 0.166 m. The 0.15 m criterion is therefore a slightly conservative round number, which is the
right direction for a supporting diagnostic. Put the derivation in the tooltip.

## 14. Framework alignment

The next revision of `hyporheic_hydraulic_metrics_web_app_and_journal_paper_framework_v1_1.md` should:

- Present HYPE as a hydraulics-first assessment of hyporheic function opportunity.
- Add thermal opportunity as a process-specific interpretation of the full flux-weighted RTD.
- Separate hydraulic evidence, screening inference, literature-supported implications, and calibrated predictions.
- Keep calibrated solute and heat transport as optional academic extensions.
- Avoid implying that an uncalibrated screening calculation establishes ecological or regulatory benefits.

## 15. Key references

- Arrigoni, A. S., Poole, G. C., Mertes, L. A. K., O'Daniel, S. J., Woessner, W. W., and Thomas, S. A. (2008). Buffered, lagged, or cooled? Disentangling hyporheic influences on temperature cycles in stream channels. *Water Resources Research*. https://doi.org/10.1029/2007WR006480
- Fogg, S. K., Reinhold, A. M., O'Daniel, S. J., Hyman, A. A., and Poole, G. C. (2023). Thermal insulation versus capacitance: A simulation experiment comparing effects of shade and hyporheic exchange on daily and seasonal stream temperature cycles. *Hydrological Processes*, 37(9), e14973. https://doi.org/10.1002/hyp.14973
- Hester, E. T., Doyle, M. W., and Poole, G. C. (2009). The influence of in-stream structures on summer water temperatures via induced hyporheic exchange. *Limnology and Oceanography*. https://doi.org/10.4319/lo.2009.54.1.0355
- Marzadri, A., Tonina, D., and Bellin, A. (2013). Effects of stream morphodynamics on hyporheic zone thermal regime. *Water Resources Research*. https://doi.org/10.1002/wrcr.20199
- Marzadri, A., Tonina, D., and Bellin, A. (2013). Quantifying the importance of daily stream water temperature fluctuations on the hyporheic thermal regime: Implication for dissolved oxygen dynamics. *Journal of Hydrology*. https://doi.org/10.1016/j.jhydrol.2013.10.030
