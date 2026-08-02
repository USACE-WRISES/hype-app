---
title: "Hyporheic Hydraulic Metrics, Web-App Reporting, and Journal-Paper Framework"
subtitle: "A practical specification for web-app reports and a placeholder manuscript framework for a 5–10-site hydraulic comparison"
status: "Working specification and placeholder manuscript plan"
version: "1.1"
revision_date: "2026-07-22"
revision_summary: "Expanded the journal-paper section with analysis, result, table, figure, and discussion placeholders for 5–10 modeled sites."
intended_audience:
  - "Web-app developers"
  - "Groundwater and hyporheic-zone modelers"
  - "Project scientists"
  - "Journal-article authors"
---

# Hyporheic Hydraulic Metrics, Web-App Reporting, and Journal-Paper Framework

> **Document basis and evidence status:** This specification develops the project concepts and recommendations in the supplied planning text. It is an implementation and reporting guide, not a completed literature review. Before publication, any process-specific residence-time threshold or ecological mechanism should be checked against the original source and cited with its environmental context and limitations.

> **Revision 1.1:** Part VIII now contains a paper-ready analysis plan and placeholder manuscript framework for 5–10 sites. All double-braced fields, such as `{{C_MIN}}`, are intentionally unresolved until the final model runs, quality-control review, and ecological-scope decisions are complete.

## 1. Purpose of this document

This document defines a clear, consistent framework for converting modeled hyporheic-flow results into:

1. A standardized **single-site report** produced by the web application.
2. A **cross-site comparison report** for 5–10 modeled sites.
3. A defensible set of **primary hydraulic metrics** for a journal article.
4. A detailed **journal-paper analysis and drafting scaffold** with explicit placeholders until the model results are complete.
5. Carefully bounded interpretations of the potential ecological and biogeochemical significance of those hydraulic results.

The core recommendation is to organize all reporting around three distinct hydraulic dimensions:

1. **Exchange frequency** — How much streamwater enters and moves through returning hyporheic flow paths?
2. **Exposure duration** — How long does exchanged water remain in the hyporheic zone?
3. **Active hyporheic capacity** — How much hydraulically connected subsurface space is associated with returning hyporheic exchange?

These dimensions can be summarized conceptually as:

> **Frequency × duration × capacity**

This expression is a conceptual framework, not a mathematical instruction to multiply the three primary metrics into one universal score. The three dimensions should remain separate in the primary results because they answer different questions and may have different ecological meanings.

The web application may derive **process-specific functional opportunity metrics** by combining exchange flow with a selected residence-time threshold. These secondary outputs should be clearly labeled as hydraulic opportunity, not as direct estimates of denitrification, nutrient removal, habitat quality, or another ecological outcome.

---

## 2. Core reporting decision

### 2.1 Primary reporting framework

The application and journal paper should emphasize the following three primary outputs:

| Hydraulic dimension | Primary output | Plain-language question |
|---|---|---|
| **Exchange frequency** | Streamflow-equivalent turnovers per kilometer, \(C_{1\mathrm{km}}\) | How frequently is streamwater exchanged with the hyporheic zone along a standardized length of stream? |
| **Exposure duration** | Flux-weighted residence-time distribution, summarized by \(T_{50}\), \(T_{10}\), and \(T_{90}\) | How long does exchanged water remain in the subsurface? |
| **Active hyporheic capacity** | Active hyporheic volume normalized by streambed area, \(D_{HZ}\) | How much hydraulically active subsurface space is associated with the modeled reach? |

### 2.2 Secondary supporting outputs

Each primary output should be supported by additional values that preserve the physical meaning of the model results:

- Gross hyporheic exchange flow, \(Q_{HEF}\).
- Streambed-area-normalized exchange flux, \(q_{HEF}\).
- River turnover length, \(L_T\).
- Total active hyporheic volume, \(V_{HZ}\).
- Percentage of streambed area connected to returning hyporheic flow paths.
- Flow-weighted depth statistics, including the 90th-percentile maximum path depth.
- Percentage and quantity of exchange flow exceeding selected residence-time thresholds.

### 2.3 What should not be the primary result

The application should not lead with:

- A universal combined “hyporheic function” index.
- A normalized score forced onto a 0–1 scale.
- “Percent hyporheic flow” without a precise reach-length and flow definition.
- Mean residence time as the sole residence-time statistic.
- Maximum hyporheic depth as the primary measure of extent.
- A direct label such as “denitrification potential,” “nutrient removal,” or “habitat quality” when the model only represents hydraulics.

A universal combined index would require choices about normalization, ecological weighting, reaction thresholds, and whether a larger value is always preferable. Those choices are not yet supported by the hydraulic model alone and could hide important tradeoffs among exchange frequency, residence time, and active volume.

---

## 3. Scope and interpretation boundaries

### 3.1 What the model and report are intended to describe

The reporting framework is intended to quantify:

- The amount of streamwater entering returning hyporheic flow paths.
- The frequency of hyporheic exchange relative to stream discharge and reach length.
- The distribution of modeled travel times through the hyporheic zone.
- The spatial volume, depth, and streambed footprint associated with hydraulically active exchange.
- The amount of exchanged water meeting selected residence-time scenarios.
- The sensitivity of these quantities to uncertain hydraulic inputs and model assumptions.

### 3.2 What the model and report do not directly establish

Hydraulic modeling alone does not demonstrate that a particular biological or biogeochemical process occurred. The report should not claim direct measurement or simulation of:

- Denitrification rate or nitrate removal.
- Nitrification rate.
- Oxygen consumption.
- Dissolved organic carbon processing.
- Thermal buffering magnitude.
- Contaminant transformation.
- Macroinvertebrate habitat quality.
- Macroinvertebrate abundance, richness, survival, or community composition.

The model instead identifies **hydraulic conditions that may create opportunities** for these processes. Whether those opportunities are realized depends on additional physical, chemical, and biological controls.

### 3.3 Recommended ecological language

Preferred terms include:

- **Hydraulic opportunity**
- **Potential functional opportunity**
- **Potential reactive exposure**
- **Potential active habitat volume**
- **Hydraulically connected subsurface capacity**
- **Residence-time scenario**
- **Process-relevant exchange under an assumed timescale**

Avoid unqualified terms such as:

- “Denitrifying flow”
- “Nutrient removal flow”
- “High-quality habitat”
- “Ecological performance score”
- “Percent of water denitrified”

---

## 4. Required terminology and definitions

The application must use consistent definitions across every site and every model run.

### 4.1 Returning hyporheic flow path

A **returning hyporheic flow path** is a modeled flow path that:

1. Originates as streamwater entering the subsurface through the streambed or another explicitly defined stream boundary.
2. Travels through the subsurface within the modeled domain.
3. Returns to the stream within the modeled domain and analysis period.

The application must distinguish returning hyporheic paths from:

- Streamwater that enters the subsurface but exits the model domain without returning to the stream.
- Regional groundwater that discharges to the stream.
- Net stream leakage to the groundwater system.
- Numerical particles or paths that terminate because of model boundaries, tracking limits, or incomplete simulation time.

### 4.2 Gross hyporheic exchange flow

\(Q_{HEF}\) is the total streamwater downwelling flow associated with returning hyporheic flow paths.

It is a **gross exchange** quantity. It is not the same as net groundwater gain or loss from the stream.

Where possible, the application should report the following separately:

- **Gross returning hyporheic exchange**, \(Q_{HEF}\).
- **Streamwater loss that does not return within the modeled domain**.
- **Groundwater discharge to the stream**.
- **Net stream gain or loss**.

### 4.3 Stream discharge

\(Q_{stream}\) is the stream discharge used to normalize hyporheic exchange.

The project must select one discharge basis and apply it consistently across sites. Possible choices include:

- Upstream discharge at the start of the modeled reach.
- Mean discharge across the modeled reach.
- Another explicitly documented representative discharge.

The report must state which discharge definition is used. Mixing upstream discharge at some sites with mean reach discharge at others would undermine cross-site comparison.

### 4.4 Modeled reach length

\(L_{model}\) is the along-channel length represented by the calculation of \(Q_{HEF}\). It must correspond to the same spatial domain over which gross exchange is summed.

### 4.5 Flux weighting

A residence-time distribution is **flux weighted** when each modeled flow path contributes according to the volume of water it represents.

Particle count alone must not be treated as flow weighting unless the tracking method guarantees that every particle represents the same flow rate.

### 4.6 Active hyporheic volume

\(V_{HZ}\) is the non-duplicated volume associated with returning hyporheic exchange according to a consistently applied spatial classification method.

The application must document whether \(V_{HZ}\) represents:

- Bulk sediment volume, or
- Pore-water volume after applying porosity.

These two quantities must not be mixed across sites. The preferred label should explicitly identify the basis, for example:

- `active_hz_bulk_volume_m3`, or
- `active_hz_porewater_volume_m3`.

### 4.7 Active streambed area

\(A_{active}\) is the streambed area through which water enters returning hyporheic flow paths. The total modeled streambed area is \(A_{bed}\).

The active streambed fraction is:

\[
F_{active,bed}=\frac{A_{active}}{A_{bed}}
\]

---

# Part I — Primary Hydraulic Metrics

## 5. Exchange frequency and hyporheic connectivity

### 5.1 Primary metric: streamflow-equivalent turnovers per kilometer

The recommended primary connectivity metric is:

\[
C_{1\mathrm{km}}
=
\frac{Q_{HEF}}{Q_{stream}}
\times
\frac{1\ \mathrm{km}}{L_{model}}
\]

where:

- \(Q_{HEF}\) = gross returning hyporheic exchange flow.
- \(Q_{stream}\) = representative stream discharge.
- \(L_{model}\) = modeled reach length.

The result is expressed as:

> **Streamflow-equivalent hyporheic turnovers per kilometer**

This metric answers:

> How many streamflow-equivalent volumes are exchanged with returning hyporheic flow paths over one kilometer of channel?

The metric should not be restricted to a 0–1 scale. Values greater than one are physically meaningful because gross exchange can represent more than one streamflow-equivalent volume over a kilometer, particularly when water is repeatedly exchanged.

### 5.2 River turnover length

The equivalent turnover-length formulation is:

\[
L_T
=
L_{model}\frac{Q_{stream}}{Q_{HEF}}
\]

and therefore:

\[
C_{1\mathrm{km}}=\frac{1\ \mathrm{km}}{L_T}
\]

\(L_T\) represents the downstream distance associated with one streamflow-equivalent turnover under the modeled conditions.

Interpretation:

- Smaller \(L_T\) means more frequent exchange.
- Larger \(L_T\) means less frequent exchange.
- If \(Q_{HEF}=0\), \(L_T\) is infinite and \(C_{1\mathrm{km}}=0\).

### 5.3 Supporting metric: gross hyporheic exchange flow

Report:

\[
Q_{HEF}
\]

Preferred units:

- L/s for small streams and compact report tables.
- m³/s for model calculations and machine-readable outputs.

This is the actual modeled volume of returning exchange per unit time. It is essential for water-balance interpretation and for calculating threshold-based functional exchange.

### 5.4 Supporting metric: streambed-normalized exchange flux

Calculate:

\[
q_{HEF}=\frac{Q_{HEF}}{A_{bed}}
\]

Preferred display units:

- mm/day, or
- m/day.

This metric describes exchange intensity per unit streambed area. It is useful when comparing reaches of different widths or modeled areas.

### 5.5 Optional supporting metric: gross exchange ratio over the modeled reach

Calculate:

\[
E_{reach}=\frac{Q_{HEF}}{Q_{stream}}
\]

This is the number of streamflow-equivalent volumes exchanged over the modeled reach. It may be displayed as a ratio or percentage, but it must not be labeled simply as “percent hyporheic flow.”

Preferred labels:

- **Gross exchange ratio over modeled reach**
- **Streamflow-equivalent exchange over modeled reach**

The report should state the modeled reach length next to this value.

### 5.6 Why connectivity is preferable to an undefined “percent hyporheic flow”

A generic percentage can be misleading because it depends on:

- The length of the modeled reach.
- The selected stream-discharge denominator.
- Whether exchange is gross or net.
- Whether repeated cycling is included.
- Whether non-returning stream loss is included.

Connectivity per kilometer makes the spatial basis explicit and supports direct cross-site comparison.

### 5.7 Ecological interpretation

Exchange frequency describes the rate at which the stream may deliver the following to the subsurface:

- Dissolved oxygen.
- Nitrate and other dissolved nutrients.
- Dissolved organic carbon.
- Fine particulate material.
- Organisms or propagules capable of entering interstitial spaces.
- Surface-water temperature signals.

High connectivity does not automatically imply high ecological or biogeochemical function. A site may exchange a large quantity of water, but the associated residence times may be too short for a process requiring prolonged exposure. Conversely, a site may have long residence times but exchange very little water.

### 5.8 Web-app display requirements

The exchange-frequency report card should show:

**Headline value**

- \(C_{1\mathrm{km}}\), displayed as turnovers/km.

**Supporting values**

- \(Q_{HEF}\), L/s.
- \(q_{HEF}\), mm/day.
- \(L_T\), km.
- Modeled reach length, m or km.
- Stream discharge used for normalization, L/s or m³/s.

**Suggested explanatory text**

> The modeled reach exchanges **X streamflow-equivalent volumes per kilometer** with returning hyporheic flow paths. This describes exchange frequency relative to stream discharge and reach length; it does not by itself indicate whether residence times are sufficient for a particular ecological or biogeochemical process.

### 5.9 Calculation and quality-control checks

The application should verify that:

- \(Q_{HEF}\ge 0\).
- \(Q_{stream}>0\).
- \(L_{model}>0\).
- \(A_{bed}>0\) when \(q_{HEF}\) is calculated.
- \(C_{1\mathrm{km}}\) and \(L_T\) satisfy the reciprocal relationship.
- The sum of flow-path weights is consistent with \(Q_{HEF}\).
- Gross hyporheic exchange is not silently substituted for net groundwater exchange.

If one of these conditions is not met, the metric should be withheld and the report should display a clear data-quality warning.

---

## 6. Exposure duration and residence-time distribution

### 6.1 Primary metric: flux-weighted residence-time distribution

The residence-time distribution should be calculated from all valid returning hyporheic flow paths, with each path weighted by the flow it represents.

For flow path \(i\):

- \(T_i\) = residence time.
- \(w_i\) = represented flow rate or equivalent flow weight.

The flux-weighted cumulative distribution is:

\[
F_Q(t)
=
\frac{\sum_i w_i I(T_i\le t)}{\sum_i w_i}
\]

where \(I(\cdot)\) is an indicator function.

The exceedance fraction is:

\[
P_Q(T\ge t^*)
=
\frac{\sum_i w_i I(T_i\ge t^*)}{\sum_i w_i}
\]

### 6.2 Required summary statistics

The main report should display:

- \(T_{50}\): flux-weighted median residence time.
- \(T_{10}\): flux-weighted 10th-percentile residence time.
- \(T_{90}\): flux-weighted 90th-percentile residence time.

Recommended compact format:

> **Median [P10–P90]**, for example, `8.2 [0.7–46] hr`

The arithmetic mean may be calculated as a supporting statistic, but it should not be the sole or headline residence-time value because residence-time distributions are often highly skewed.

### 6.3 Recommended optional distribution statistics

The application may also calculate:

- Interquartile range, \(T_{25}\) to \(T_{75}\).
- Distribution breadth, such as \(T_{90}/T_{10}\), when \(T_{10}>0\).
- Percentage of exchange in predefined time bins.
- Fraction of flow paths that are censored or incomplete.

These should be supplemental, not substitutes for the full distribution.

### 6.4 Default residence-time scenarios

The initial web-app report should calculate exceedance at the following candidate thresholds:

- \(t^*=1\) hour.
- \(t^*=6\) hours.
- \(t^*=12\) hours.
- \(t^*=24\) hours.

For each threshold, report:

- Percentage of gross hyporheic exchange with \(T\ge t^*\).
- Gross exchange flow with \(T\ge t^*\).
- Connectivity per kilometer associated with flow having \(T\ge t^*\).

These are **scenario thresholds**, not universal ecological criteria. The user interface must make this distinction explicit.

### 6.5 Residence-time figure requirements

The preferred primary residence-time figure is a **flux-weighted cumulative distribution** or exceedance plot.

The report should provide:

- A log-scaled residence-time axis when the distribution spans multiple orders of magnitude.
- Threshold reference markers at selected times.
- A clear legend identifying the site or model scenario.
- A note stating that the distribution is flow weighted.

Additional useful figures include:

- A violin or density plot for cross-site comparison.
- A bar chart showing the fraction of exchange above each threshold.
- A binned histogram of exchange flow by residence-time interval.

### 6.6 Treatment of incomplete or censored paths

The application must not treat an incomplete path as a completed residence time.

A flow path should be flagged as censored if it:

- Reaches the model boundary without returning to the stream.
- Remains in the model at the end of the tracking period.
- Terminates because of a numerical tracking limit.
- Cannot be assigned a valid return location.

The report should show:

- The percentage of total downwelling flow represented by valid returning paths.
- The percentage represented by censored paths.
- The percentage represented by non-returning stream loss, if distinguishable.

If the censored fraction is substantial, residence-time percentiles should be labeled as potentially biased and the site should receive a quality-control warning.

### 6.7 Ecological interpretation

Residence time represents the time available for processes such as:

- Oxygen consumption.
- Nitrification.
- Denitrification.
- Dissolved organic carbon transformation.
- Sorption and contaminant transformation.
- Thermal exchange between water and sediment.

Residence time alone does not demonstrate that these processes occurred. For example, denitrification also depends on oxygen depletion, nitrate availability, labile carbon, temperature, microbial activity, and sediment conditions.

The report should therefore describe residence time as:

> **The duration of hydraulic exposure available for a process to occur, assuming the required chemical and biological conditions are present.**

### 6.8 Web-app display requirements

The exposure-duration report card should show:

**Headline value**

- \(T_{50}\) with \(T_{10}\)–\(T_{90}\) range.

**Supporting values**

- Percentage of exchange above selected thresholds.
- Optional mean and interquartile range.
- Percentage of censored or invalid paths.

**Suggested explanatory text**

> The flux-weighted median residence time is **X hours**, with 80% of returning exchange between **Y and Z hours**. Residence time indicates the duration of water–sediment contact but does not establish that a specific reaction occurred.

---

## 7. Active hyporheic capacity and spatial extent

### 7.1 Primary metric: normalized active hyporheic volume

For a three-dimensional model, calculate:

\[
D_{HZ}=\frac{V_{HZ}}{A_{bed}}
\]

where:

- \(V_{HZ}\) = active hyporheic volume.
- \(A_{bed}\) = total modeled streambed area.

\(D_{HZ}\) has units of length and can be described as:

> **Equivalent active hyporheic depth**

This is a normalization metric. It does not mean that the active hyporheic zone forms a uniform layer of that thickness.

For a two-dimensional cross-sectional model, use:

\[
D_{HZ}=\frac{A_{HZ}}{L_{bed}}
\]

where:

- \(A_{HZ}\) = active cross-sectional hyporheic area.
- \(L_{bed}\) = represented streambed length in the cross section.

### 7.2 Required supporting extent metrics

The application should report:

- Total active hyporheic volume, \(V_{HZ}\), m³.
- Equivalent active hyporheic depth, \(D_{HZ}\), m.
- Active streambed area, \(A_{active}\), m².
- Active streambed fraction, \(F_{active,bed}\), percent.
- Flow-weighted median maximum path depth, m.
- Flow-weighted 90th-percentile maximum path depth, m.
- Maximum modeled path depth, m, as a supplemental diagnostic.
- Lateral extent where it can be defined consistently.

### 7.3 Spatial classification method

The active-volume algorithm must avoid double-counting overlapping flow paths.

A recommended implementation is to classify a model cell or voxel as active if it is associated with at least one valid returning hyporheic flow path under the selected classification rule. The total active volume is then the union of all active cells, not the sum of separately swept path volumes.

The project must document:

- Whether cells are classified by direct path intersection, stream-origin fraction, travel-time field, or another method.
- The minimum threshold used to classify a cell as active, if a threshold is required.
- Whether the volume is bulk sediment volume or pore-water volume.
- Whether disconnected cells or cells associated only with censored paths are excluded.

The same method must be applied across all sites.

### 7.4 Depth statistics

Depth should be reported in a way that is robust to isolated extreme paths.

Recommended supporting statistic:

> **Flow-weighted P90 maximum path depth**

For each returning path, calculate the maximum depth below the streambed reached by that path. Then calculate the 90th percentile using the same flow weights used for the residence-time distribution.

Maximum depth may be retained in the supplemental output, but it should not be a headline result because a single cell or low-flow path may control it.

### 7.5 Ecological interpretation

Active hyporheic capacity represents the amount of hydraulically connected subsurface space that may provide:

- Subsurface habitat.
- Water–sediment contact area.
- Reactive sediment volume.
- Thermal storage.
- Refuge from surface disturbances.

For macroinvertebrates, use the term:

> **Potential active habitat volume**

Do not call this habitat quality. A groundwater model does not by itself resolve:

- Pore-space accessibility.
- Fine-sediment clogging.
- Substrate grain size.
- Dissolved oxygen.
- Food availability.
- Temperature suitability.
- Predation or competition.
- Flow permanence.

### 7.6 Web-app display requirements

The active-capacity report card should show:

**Headline value**

- Equivalent active hyporheic depth, \(D_{HZ}\), m.

**Supporting values**

- Total active volume, m³.
- Active streambed fraction, percent.
- P90 maximum path depth, m.
- Volume basis: bulk sediment or pore water.

**Required visualizations**

- Plan-view map of active exchange, when available.
- Longitudinal or cross-sectional view of returning flow paths.
- Depth-distribution plot or mapped depth classes.

**Suggested explanatory text**

> The modeled reach contains an active hyporheic volume equivalent to a uniform depth of **X m** beneath the modeled streambed area. This value is a normalized measure of hydraulically connected subsurface capacity, not a statement that the active zone is uniformly X m deep.

---

## 8. The three headline metrics in one framework

| Functional dimension | Primary metric | Required supporting outputs | Principal ecological interpretation |
|---|---|---|---|
| **Exchange frequency** | \(C_{1\mathrm{km}}\), streamflow-equivalent turnovers/km | \(Q_{HEF}\), \(q_{HEF}\), \(L_T\), reach length, stream discharge | Delivery of oxygen, nutrients, carbon, organisms, and temperature signals to the subsurface |
| **Exposure duration** | Flux-weighted RTD summarized as \(T_{50}\,[T_{10}\text{–}T_{90}]\) | Threshold exceedance fractions, threshold-specific exchange flow, censored-flow fraction | Time available for biogeochemical, thermal, and ecological processes |
| **Active hyporheic capacity** | \(D_{HZ}=V_{HZ}/A_{bed}\) | Total active volume, active bed fraction, P90 path depth, maps | Potential reactive volume and potential subsurface habitat capacity |

The central interpretive statement for the project is:

> **Hyporheic ecological function depends on how frequently streamwater enters the subsurface, how long it remains there, and how much hydraulically active subsurface space is available.**

These dimensions are complementary rather than interchangeable. A site may have:

- High exchange frequency but short residence times.
- Long residence times but little exchanged flow.
- A large active volume but weak connection to the stream.
- Similar connectivity to another site but a very different residence-time distribution.

These contrasts are likely to be among the most scientifically informative results of the cross-site analysis.

---

# Part II — Process-Specific Functional Opportunity

## 9. Why the application should not create a universal index

A universal index would require decisions about:

- How each hydraulic metric is normalized.
- How exchange frequency, residence time, and volume are weighted.
- Which ecological function is being represented.
- Whether more exchange, longer residence, or larger volume is always beneficial.
- Whether correlated hydraulic metrics are being counted more than once.
- Which thresholds apply across different stream types and environmental conditions.

Because these decisions are process dependent, the application should preserve the three primary dimensions and derive secondary metrics for a **specified process timescale**.

## 10. Threshold-based functional exchange

For a selected residence-time threshold \(t^*\), calculate the flux-weighted exceedance fraction:

\[
P_Q(T\ge t^*)
\]

Then calculate the gross exchange flow meeting that threshold:

\[
Q_{functional}(t^*)
=
Q_{HEF}\times P_Q(T\ge t^*)
\]

This answers:

> How much water enters returning hyporheic flow paths and remains in the subsurface for at least the selected duration?

### 10.1 Threshold-specific functional connectivity

Calculate:

\[
C_{functional,1\mathrm{km}}(t^*)
=
C_{1\mathrm{km}}\times P_Q(T\ge t^*)
\]

This represents streamflow-equivalent turnovers per kilometer associated with flow paths that meet or exceed the selected residence time.

### 10.2 Required threshold outputs

For each selected threshold, display:

| Output | Definition | Preferred unit |
|---|---|---|
| Residence-time exceedance | \(P_Q(T\ge t^*)\) | % |
| Functional exchange flow | \(Q_{functional}(t^*)\) | L/s |
| Functional connectivity | \(C_{functional,1\mathrm{km}}(t^*)\) | turnovers/km |

### 10.3 Recommended label

Use:

> **Potential functional opportunity based on hydraulic residence time**

Do not label the metric as direct denitrification, nutrient removal, thermal buffering, or habitat quality unless the application includes the additional data and validated model required to support that claim.

## 11. Default threshold scenarios

The initial application may include the following default scenarios:

| Scenario label | Residence-time threshold | Output interpretation |
|---|---:|---|
| Rapid-exposure scenario | 1 hour | Exchange remaining in the subsurface for at least 1 hour |
| Intermediate-exposure scenario | 6 hours | Exchange remaining in the subsurface for at least 6 hours |
| Longer-exposure scenario | 12 hours | Exchange remaining in the subsurface for at least 12 hours |
| Extended-exposure scenario | 24 hours | Exchange remaining in the subsurface for at least 24 hours |

These labels should remain process neutral in the default report.

If a threshold is associated with a named ecological or biogeochemical process, the report should also provide:

- The cited source.
- The ecosystem and sediment context in which the timescale was observed or assumed.
- The conditions required for the process.
- A statement that the threshold is not necessarily transferable to all sites.

## 12. User-defined thresholds

The application should allow a user to enter a custom threshold, with the following controls:

- Numeric value.
- Unit selection: minutes, hours, or days.
- Optional process label.
- Optional citation or source note.
- Optional explanatory note.

The output should continue to be labeled as a residence-time scenario unless the process-specific interpretation has been independently supported.

## 13. Optional advanced reaction-timescale formulation

A later version of the application may integrate the full residence-time distribution with a process-specific characteristic reaction time:

\[
R_j
=
\int_0^\infty
\left(1-e^{-t/\tau_{r,j}}\right)
f_Q(t)\,dt
\]

where:

- \(f_Q(t)\) = flux-weighted residence-time probability density.
- \(\tau_{r,j}\) = characteristic reaction time for process \(j\).
- \(R_j\) = expected opportunity for that process during one hyporheic excursion under the assumed first-order formulation.

This is an optional future feature. It should not be implemented as a universal ecological score without process-specific validation, documented assumptions, and appropriate literature support.

---

# Part III — Ecological Interpretation Strategy

## 14. Primary approach: literature-based functional potential

The primary ecological interpretation should be based on existing literature and should use the following statement as a boundary condition:

> **We modeled the hydraulic conditions that create opportunities for ecological and biogeochemical functions. We did not directly simulate or measure those functions.**

The discussion may connect:

- Residence time to the opportunity for oxygen consumption, nitrification, denitrification, carbon processing, thermal exchange, and contaminant transformation.
- Exchange frequency to the delivery of water, oxygen, nutrients, carbon, and temperature signals.
- Active hyporheic volume to potential reactive capacity and potential habitat availability.

### 14.1 How literature thresholds should be used

Literature-derived thresholds should be used as **scenarios**, not as universal cutoffs.

For each threshold, document:

1. The process of interest.
2. The reported or assumed timescale.
3. The environmental setting.
4. The controlling conditions identified in the source.
5. Whether the source reports a threshold, a characteristic time, or a modeling assumption.
6. Why the threshold is relevant to the present sites.
7. The limitations of transferring it to a new setting.

The application should not hard-code a statement such as “residence time greater than one hour causes denitrification.” Instead, it should say:

> Under a selected one-hour residence-time scenario, **X%** of gross exchange and **Y L/s** of returning hyporheic flow meet or exceed the selected duration. Whether denitrification occurs depends on redox conditions, nitrate, labile carbon, temperature, microbial activity, and sediment properties.

### 14.2 Recommended threshold library structure

A literature-threshold library should use fields such as:

| Field | Description |
|---|---|
| `threshold_id` | Unique identifier |
| `process_name` | Denitrification, thermal exchange, contaminant transformation, etc. |
| `threshold_value` | Numeric residence time |
| `threshold_unit` | Minutes, hours, or days |
| `threshold_type` | Observed transition, characteristic time, model assumption, or scenario |
| `ecosystem_context` | Stream type, sediment setting, climate, or study reach |
| `required_conditions` | Chemical, thermal, biological, or sediment conditions |
| `citation` | Full source or persistent identifier |
| `transferability_note` | Limitations on application to other sites |
| `display_label` | User-facing description |

No process-specific threshold should be displayed without its source and transferability note.

---

## 15. Secondary approach: limited Texas State macroinvertebrate example

The Texas State macroinvertebrate dataset may be used as a narrow, exploratory empirical example if the PhD student responsible for the ecological dataset agrees with the scope and leads or co-leads the ecological interpretation.

### 15.1 Recommended focused question

> Are macroinvertebrate abundance, density, richness, or the abundance of strongly hyporheic taxa associated with modeled active hyporheic capacity, connectivity, or residence-time characteristics?

### 15.2 Recommended ecological response variables

Select no more than one or two primary response variables before conducting the analysis. Candidate variables include:

- Macroinvertebrate density standardized by sampled volume.
- Taxonomic richness.
- Abundance or density of obligate or strongly hyporheic taxa, if a defensible classification is available.

Avoid turning this hydraulic paper into a comprehensive community-ecology analysis. Detailed taxonomy, traits, community composition, and ecological mechanisms should remain within the PhD student’s primary work unless she explicitly chooses to include them.

### 15.3 Recommended hydraulic predictors

Candidate hydraulic predictors are:

- \(C_{1\mathrm{km}}\), exchange frequency.
- \(T_{50}\) or a predefined residence-time exceedance fraction.
- \(D_{HZ}\), active capacity.

For macroinvertebrate analysis, a better extent predictor may be the active volume within the actual biological sampling depth and footprint rather than the full modeled reach volume.

### 15.4 Spatial and temporal alignment requirements

Before any ecological analysis, confirm that:

- The modeled reach corresponds to the biological sampling location.
- The hydraulic scenario represents the hydrologic conditions near the sampling date or the intended long-term condition.
- The modeled depth range overlaps the biological sampling depth.
- Abundance or density is standardized appropriately for sampling effort and sampled volume.
- Replicate structure is understood.

### 15.5 Analytical scope

With a small number of sites, the analysis should remain descriptive and exploratory. Appropriate options include:

- Scatterplots with uncertainty and site labels.
- Rank correlations.
- Simple single-predictor or carefully limited regression models.
- Comparison of macroinvertebrate responses with the three hydraulic dimensions.

Avoid an extensive model-selection exercise or a high-dimensional ecological analysis unless the sample size and study design support it.

### 15.6 Required interpretation language

Use:

> **An exploratory test of whether modeled hydraulic opportunity is reflected in observed macroinvertebrate patterns.**

Do not present the macroinvertebrate analysis as complete validation of the hydraulic framework. Macroinvertebrate patterns may also depend on:

- Substrate composition.
- Fine-sediment clogging.
- Dissolved oxygen.
- Organic matter.
- Temperature.
- Water chemistry.
- Flow permanence.
- Disturbance history.
- Sampling efficiency.

### 15.7 Authorship and scope protection

Before including the ecological analysis:

1. Agree on the ecological question with the PhD student.
2. Agree on which response variables are appropriate.
3. Confirm that the analysis does not preempt her planned dissertation or journal articles.
4. Have her lead or co-lead the ecological methods and interpretation.
5. Clearly distinguish the hydraulic paper’s limited illustrative analysis from her broader ecological work.

If these conditions are not met, retain the ecological discussion as literature based and omit the empirical macroinvertebrate analysis.

---

# Part IV — Web-App Report Design

## 16. Report types

The application should produce two complementary report types.

### 16.1 Single-site report

The single-site report should provide:

1. Site and model-run identification.
2. Model-domain and discharge context.
3. Three headline hydraulic metric cards.
4. Supporting hydraulic values.
5. Residence-time distribution and threshold scenarios.
6. Maps and cross sections.
7. Sensitivity and uncertainty results.
8. Automated interpretation text with ecological guardrails.
9. Downloadable machine-readable data.

### 16.2 Cross-site comparison report

The cross-site report should be designed for **5–10 accepted sites** and should provide:

1. A standardized summary table with one row per site.
2. Cross-site range plots showing the magnitude and spread of each primary hydraulic metric.
3. Cross-site ranking and distribution plots.
4. A three-dimensional conceptual comparison of frequency, duration, and capacity.
5. Residence-time exceedance comparisons.
6. Sensitivity of site rankings to alternative thresholds and model scenarios.
7. Optional exploratory ecological overlays.
8. Paper-ready exports that populate the journal tables, plots, and manuscript placeholders in Part VIII.

The interface may display fewer than 10 sites, but its data model and export structure should reserve fields for up to 10 without changing column definitions or calculation logic.

---

## 17. Single-site report structure

### 17.1 Report header

Display:

- Site name and site identifier.
- Model-run identifier and date.
- Model version.
- Modeled reach length.
- Stream discharge used for normalization.
- Streambed area.
- Dimensionality: 2D or 3D.
- Active-volume basis: bulk sediment or pore water.
- Baseline or sensitivity scenario label.

### 17.2 Headline metric cards

#### Card 1 — Exchange frequency

- **Primary value:** \(C_{1\mathrm{km}}\), turnovers/km.
- Gross exchange flow, L/s.
- Exchange flux, mm/day.
- Turnover length, km.

#### Card 2 — Exposure duration

- **Primary value:** \(T_{50}\,[T_{10}\text{–}T_{90}]\), hours or days.
- Percentage above selected threshold.
- Censored-flow percentage.

#### Card 3 — Active hyporheic capacity

- **Primary value:** \(D_{HZ}\), m.
- Total active volume, m³.
- Active streambed fraction, percent.
- P90 maximum path depth, m.

### 17.3 Functional opportunity panel

Allow the user to select a residence-time threshold and display:

- Selected threshold and units.
- Percentage of exchange meeting the threshold.
- Functional exchange flow, L/s.
- Functional connectivity, turnovers/km.
- A caution statement describing the output as hydraulic opportunity.

### 17.4 Required single-site figures

1. Plan-view map of exchange or active hyporheic extent.
2. Cross section or longitudinal view of returning flow paths.
3. Flux-weighted residence-time cumulative distribution.
4. Threshold exceedance summary.
5. Optional sensitivity interval figure.

### 17.5 Model-quality panel

Display:

- Water-balance error.
- Returning-path flow fraction.
- Censored-path flow fraction.
- Non-returning stream-loss fraction, if available.
- Number of modeled particles or paths.
- Confirmation of flow weighting.
- Any parameter or boundary-condition warnings.

---

## 18. Cross-site comparison report

### 18.1 Recommended primary table

| Site | Connectivity \(C_{1\mathrm{km}}\) (turnovers/km) | Gross HEF (L/s) | Residence time \(T_{50}\,[T_{10}\text{–}T_{90}]\) | Flow \(\ge6\) hr (%) | Flow \(\ge24\) hr (%) | Equivalent active depth \(D_{HZ}\) (m) | P90 path depth (m) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Site 01 | — | — | — | — | — | — | — |
| Site 02 | — | — | — | — | — | — | — |
| Site 03 | — | — | — | — | — | — | — |
| Site 04 | — | — | — | — | — | — | — |
| Site 05 | — | — | — | — | — | — | — |
| Site 06 | — | — | — | — | — | — | — |
| Site 07 | — | — | — | — | — | — | — |
| Site 08 | — | — | — | — | — | — | — |
| Site 09 | — | — | — | — | — | — | — |
| Site 10 | — | — | — | — | — | — | — |

The table should not include every possible threshold. Complete threshold results should be shown in a separate figure, interactive panel, or supplemental export.

### 18.2 Supporting comparison table

| Site | Reach length (m) | Stream discharge (L/s) | Streambed area (m²) | Exchange flux (mm/day) | Turnover length (km) | Active volume (m³) | Active bed fraction (%) | Censored flow (%) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Site 01 | — | — | — | — | — | — | — | — |
| Site 02 | — | — | — | — | — | — | — | — |
| Site 03 | — | — | — | — | — | — | — | — |
| Site 04 | — | — | — | — | — | — | — | — |
| Site 05 | — | — | — | — | — | — | — | — |
| Site 06 | — | — | — | — | — | — | — | — |
| Site 07 | — | — | — | — | — | — | — | — |
| Site 08 | — | — | — | — | — | — | — | — |
| Site 09 | — | — | — | — | — | — | — | — |
| Site 10 | — | — | — | — | — | — | — | — |

### 18.3 Recommended comparison figures

#### Figure A — Frequency–duration–capacity plot

Use a scatterplot with:

- X-axis: \(C_{1\mathrm{km}}\).
- Y-axis: \(T_{50}\), preferably on a logarithmic scale if needed.
- Point size: \(D_{HZ}\) or total active volume.

This figure directly shows whether sites with similar connectivity have different residence times or active capacities.

#### Figure B — Residence-time cumulative distributions

Overlay flux-weighted residence-time CDFs or exceedance curves for all sites. Allow the user to highlight one site at a time.

#### Figure C — Threshold opportunity matrix

Display sites by threshold, with values for:

- Percentage of exchange exceeding the threshold.
- Functional exchange flow.
- Functional connectivity.

#### Figure D — Metric-rank comparison

Compare site ranks under:

- Exchange frequency.
- Median residence time.
- Active capacity.
- Threshold-specific functional connectivity.

Differences in rank are a key result because they show that one hydraulic metric does not fully represent hyporheic behavior.

### 18.4 Relative descriptors

Terms such as “high,” “moderate,” and “low” should only be used relative to a defined comparison set.

Recommended rule:

- Low: below the 25th percentile of the selected comparison set.
- Intermediate: 25th to 75th percentile.
- High: above the 75th percentile.

The report must state the reference set. These categories are descriptive and should not be interpreted as ecological quality classes.

---

## 19. Automated interpretation templates

The application may generate concise narrative summaries using the following logic.

### 19.1 High exchange frequency, short residence time

> This site has relatively frequent hyporheic exchange, but most exchanged water follows short residence-time paths. The site may provide rapid water–sediment contact and frequent delivery of surface-water constituents, while offering less hydraulic opportunity for processes requiring prolonged exposure.

### 19.2 Low exchange frequency, long residence time

> This site has comparatively long residence times but limited exchanged flow. Individual flow paths may provide prolonged exposure, while reach-scale function may be constrained by the small quantity of water entering those paths.

### 19.3 Large active capacity, low connectivity

> The site contains a comparatively large hydraulically active subsurface volume, but exchange relative to stream discharge is limited. The available subsurface capacity may not be used frequently under the modeled conditions.

### 19.4 High connectivity and broad residence-time distribution

> The site combines frequent exchange with a broad range of residence times. This creates multiple hydraulic exposure environments, including rapid-return pathways and longer-duration pathways. The ecological significance depends on the chemical and biological conditions associated with those pathways.

### 19.5 Similar connectivity but different residence time

> Although this site has connectivity similar to another modeled reach, its residence-time distribution differs substantially. This demonstrates that bulk connectivity alone does not fully characterize the hydraulic opportunity for hyporheic processes.

### 19.6 Threshold interpretation

> Under the selected **X-hour** scenario, **Y%** of gross hyporheic exchange, equal to **Z L/s**, remains in the subsurface for at least the selected duration. This is a hydraulic opportunity metric and does not establish that a specific reaction or ecological response occurred.

### 19.7 No comparison set available

When only one site is being viewed, avoid relative descriptors. Report absolute values and explanatory definitions instead.

---

# Part V — Data and Calculation Specification

## 20. Required site-level inputs

| Field name | Description | Unit | Required |
|---|---|---:|---|
| `site_id` | Unique site identifier | — | Yes |
| `site_name` | Human-readable site name | — | Yes |
| `model_run_id` | Unique model-run identifier | — | Yes |
| `model_version` | Model or application version | — | Yes |
| `scenario_name` | Baseline or sensitivity scenario | — | Yes |
| `model_dimension` | 2D or 3D | — | Yes |
| `model_reach_length_m` | Along-channel reach length | m | Yes |
| `stream_discharge_m3_s` | Discharge used for normalization | m³/s | Yes |
| `stream_discharge_basis` | Upstream, mean reach, or other | — | Yes |
| `streambed_area_m2` | Modeled streambed area | m² | Yes for 3D |
| `streambed_length_m` | Represented bed length | m | Yes for 2D |
| `effective_porosity` | Porosity used if pore-water volume is reported | fraction | Conditional |
| `active_volume_basis` | Bulk sediment or pore water | — | Yes |
| `water_balance_error_pct` | Model water-balance error | % | Yes |

## 21. Required flow-path-level inputs

| Field name | Description | Unit | Required |
|---|---|---:|---|
| `path_id` | Unique path identifier | — | Yes |
| `site_id` | Site identifier | — | Yes |
| `model_run_id` | Model-run identifier | — | Yes |
| `path_class` | Returning HEF, non-returning loss, groundwater, censored, invalid | — | Yes |
| `flow_weight_m3_s` | Flow represented by the path | m³/s | Yes |
| `residence_time_s` | Subsurface travel time | s | Returning paths |
| `max_depth_below_bed_m` | Maximum depth reached | m | Returning paths |
| `path_length_m` | Total modeled path length | m | Optional |
| `entry_location` | Stream-entry coordinate or cell | — | Recommended |
| `return_location` | Stream-return coordinate or cell | — | Returning paths |
| `termination_reason` | Reason tracking ended | — | Recommended |

## 22. Required spatial inputs for active volume

At minimum, each model cell or spatial unit used to construct active volume should contain:

| Field name | Description | Unit |
|---|---|---:|
| `cell_id` | Unique spatial identifier | — |
| `cell_volume_m3` | Bulk model-cell volume | m³ |
| `cell_area_m2` | Relevant plan or bed area | m² |
| `depth_below_streambed_m` | Representative depth | m |
| `active_returning_path` | Whether cell is associated with a valid returning path | Boolean |
| `active_classification_value` | Stream-origin fraction, path count, or other classification basis | Model specific |

## 23. Required derived outputs

| Field name | Description | Unit |
|---|---|---:|
| `gross_hef_m3_s` | \(Q_{HEF}\) | m³/s |
| `gross_hef_l_s` | \(Q_{HEF}\) | L/s |
| `exchange_flux_m_d` | \(q_{HEF}\) | m/day |
| `exchange_flux_mm_d` | \(q_{HEF}\) | mm/day |
| `connectivity_turnovers_per_km` | \(C_{1\mathrm{km}}\) | turnovers/km |
| `turnover_length_km` | \(L_T\) | km |
| `gross_exchange_ratio_reach` | \(E_{reach}\) | dimensionless |
| `rt_p10_h` | \(T_{10}\) | hr |
| `rt_p50_h` | \(T_{50}\) | hr |
| `rt_p90_h` | \(T_{90}\) | hr |
| `rt_mean_h` | Flux-weighted mean | hr |
| `active_hz_volume_m3` | \(V_{HZ}\) | m³ |
| `equivalent_active_depth_m` | \(D_{HZ}\) | m |
| `active_streambed_area_m2` | \(A_{active}\) | m² |
| `active_streambed_fraction` | \(F_{active,bed}\) | fraction |
| `path_depth_p50_m` | Flow-weighted median maximum path depth | m |
| `path_depth_p90_m` | Flow-weighted P90 maximum path depth | m |
| `returning_flow_fraction` | Returning-path flow / total downwelling flow | fraction |
| `censored_flow_fraction` | Censored-path flow / total downwelling flow | fraction |

## 24. Threshold-output structure

Each threshold result should be stored as a separate record:

| Field name | Description | Unit |
|---|---|---:|
| `site_id` | Site identifier | — |
| `model_run_id` | Model-run identifier | — |
| `threshold_value_h` | Residence-time threshold | hr |
| `threshold_label` | User-facing label | — |
| `threshold_source` | User scenario or literature source | — |
| `flow_exceedance_fraction` | \(P_Q(T\ge t^*)\) | fraction |
| `functional_exchange_m3_s` | \(Q_{functional}(t^*)\) | m³/s |
| `functional_exchange_l_s` | Same value | L/s |
| `functional_connectivity_per_km` | \(C_{functional,1\mathrm{km}}(t^*)\) | turnovers/km |
| `interpretation_note` | Process-neutral or source-specific caution | — |

## 25. Example machine-readable summary

```json
{
  "site_id": "SITE_01",
  "model_run_id": "SITE_01_BASELINE_V1",
  "scenario_name": "baseline",
  "model_reach_length_m": 1000.0,
  "stream_discharge_m3_s": 0.75,
  "stream_discharge_basis": "upstream",
  "streambed_area_m2": 8200.0,
  "gross_hef_m3_s": 0.12,
  "connectivity_turnovers_per_km": 0.16,
  "turnover_length_km": 6.25,
  "exchange_flux_mm_d": 1264.4,
  "rt_p10_h": 0.8,
  "rt_p50_h": 7.4,
  "rt_p90_h": 51.0,
  "active_hz_volume_m3": 11480.0,
  "equivalent_active_depth_m": 1.4,
  "active_streambed_fraction": 0.63,
  "path_depth_p90_m": 2.8,
  "censored_flow_fraction": 0.03,
  "threshold_results": [
    {
      "threshold_value_h": 6.0,
      "threshold_label": "Intermediate-exposure scenario",
      "flow_exceedance_fraction": 0.56,
      "functional_exchange_l_s": 67.2,
      "functional_connectivity_per_km": 0.0896
    },
    {
      "threshold_value_h": 24.0,
      "threshold_label": "Extended-exposure scenario",
      "flow_exceedance_fraction": 0.18,
      "functional_exchange_l_s": 21.6,
      "functional_connectivity_per_km": 0.0288
    }
  ]
}
```

The values above are illustrative and must not be treated as actual study results.

---

## 26. Calculation sequence

The application should follow the same sequence for every site.

### Step 1 — Validate required inputs

Confirm that reach length, stream discharge, streambed area or length, flow-path weights, and path classifications are present and valid.

### Step 2 — Classify flow paths

Separate:

- Valid returning hyporheic paths.
- Non-returning stream loss.
- Groundwater-origin paths.
- Censored paths.
- Invalid paths.

### Step 3 — Calculate gross returning exchange

\[
Q_{HEF}=\sum_{i\in returning}w_i
\]

### Step 4 — Calculate exchange-frequency metrics

Calculate:

- \(E_{reach}\).
- \(C_{1\mathrm{km}}\).
- \(L_T\).
- \(q_{HEF}\).

### Step 5 — Calculate the flux-weighted RTD

Using returning paths only:

- Calculate weighted quantiles.
- Construct the weighted CDF.
- Calculate default threshold exceedance fractions.
- Calculate censored-flow diagnostics separately.

### Step 6 — Calculate active spatial metrics

Using the approved active-volume classification:

- Construct the non-duplicated union of active cells.
- Calculate total active volume.
- Calculate equivalent active depth.
- Calculate active streambed area and fraction.
- Calculate flow-weighted path-depth statistics.

### Step 7 — Calculate functional opportunity metrics

For each threshold:

- Calculate exceedance fraction.
- Calculate functional exchange flow.
- Calculate functional connectivity.

### Step 8 — Attach sensitivity and provenance information

Store:

- Model version.
- Input parameters.
- Boundary-condition choices.
- Scenario label.
- Calculation version.
- Warnings and quality flags.

### Step 9 — Generate report text and visualizations

Use the approved terminology and avoid ecological overstatement.

---

# Part VI — Quality Assurance, Sensitivity, and Uncertainty

## 27. Required quality-control tests

### 27.1 Model water balance

The application should display the model water-balance error and flag runs exceeding the project’s accepted tolerance.

### 27.2 Flow-path accounting

Verify that the flow represented by all path classes is consistent with the modeled downwelling flow.

At minimum, report:

- Returning hyporheic flow.
- Non-returning stream loss.
- Censored flow.
- Invalid or unclassified flow.

### 27.3 Weighting verification

The application must confirm whether particles are:

- Equal-flow particles, or
- Unequal-flow particles requiring explicit weights.

A warning should appear if the RTD is particle-count weighted rather than flux weighted.

### 27.4 Monotonic threshold checks

For increasing thresholds:

- Exceedance fractions must not increase.
- Functional exchange flow must not increase.
- Functional connectivity must not increase.

### 27.5 Connectivity consistency

Check that:

\[
C_{1\mathrm{km}}=\frac{1\ \mathrm{km}}{L_T}
\]

within numerical tolerance.

### 27.6 Spatial-volume checks

Confirm that:

- Active volume does not exceed the modeled domain volume.
- Active streambed fraction is between zero and one.
- Cells are not double counted.
- Volume basis is reported.

### 27.7 Residence-time checks

Confirm that:

- \(T_{10}\le T_{50}\le T_{90}\).
- Residence times are nonnegative.
- Units are converted correctly.
- Censored paths are excluded from completed-path quantiles and reported separately.

---

## 28. Sensitivity analysis

Every primary value should be accompanied by an uncertainty or sensitivity range when plausible alternative model runs are available.

Priority sensitivity variables include:

- Hydraulic conductivity.
- Hydraulic anisotropy, if modeled.
- Depth to bedrock or lower model boundary.
- Regional groundwater gradient.
- Streambed geometry.
- Stream-stage or hydraulic-head boundary conditions.
- Effective porosity for travel-time and pore-water-volume calculations.
- Active-cell classification threshold.

### 28.1 Recommended reporting format

For a limited set of deterministic scenarios, report:

- Baseline estimate.
- Minimum across accepted scenarios.
- Maximum across accepted scenarios.

For a larger ensemble, report:

- Median.
- 10th–90th percentile or 5th–95th percentile.

The report must state which uncertainty summary is used.

### 28.2 Ranking stability

For the cross-site comparison, calculate whether site rankings are stable across accepted sensitivity runs.

Useful outputs include:

- Rank range for each site and metric.
- Number of scenarios in which each site is in the highest or lowest quartile.
- Whether the main scientific conclusions persist across model assumptions.

### 28.3 Required uncertainty language

> Metric ranges reflect sensitivity to the evaluated hydraulic assumptions and are not a complete representation of all sources of uncertainty.

If no sensitivity analysis is available, display:

> Model-input uncertainty was not evaluated for this result.

---

# Part VII — Recommended Report Tables and Figures

## 29. Main web-app summary table

| Site | Turnovers/km | Gross HEF (L/s) | RTD median [P10–P90] | ≥1 hr (%) | ≥6 hr (%) | ≥12 hr (%) | ≥24 hr (%) | Equivalent active depth (m) | Active bed (%) | P90 depth (m) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Site 01 | — | — | — | — | — | — | — | — | — | — |
| Site 02 | — | — | — | — | — | — | — | — | — | — |
| Site 03 | — | — | — | — | — | — | — | — | — | — |
| Site 04 | — | — | — | — | — | — | — | — | — | — |
| Site 05 | — | — | — | — | — | — | — | — | — | — |
| Site 06 | — | — | — | — | — | — | — | — | — | — |
| Site 07 | — | — | — | — | — | — | — | — | — | — |
| Site 08 | — | — | — | — | — | — | — | — | — | — |
| Site 09 | — | — | — | — | — | — | — | — | — | — |
| Site 10 | — | — | — | — | — | — | — | — | — | — |

For the journal article, reduce the number of threshold columns to avoid an excessively wide table. The full table can remain in the web app or supplemental material.

## 30. Functional-opportunity table

| Site | Threshold | Exchange meeting threshold (%) | Functional exchange (L/s) | Functional connectivity (turnovers/km) | Interpretation status |
|---|---:|---:|---:|---:|---|
| Site 1 | 1 hr | — | — | — | Hydraulic scenario |
| Site 1 | 6 hr | — | — | — | Hydraulic scenario |
| Site 1 | 12 hr | — | — | — | Hydraulic scenario |
| Site 1 | 24 hr | — | — | — | Hydraulic scenario |

## 31. Sensitivity table

| Site | Metric | Baseline | Sensitivity minimum | Sensitivity maximum | Dominant input or assumption |
|---|---|---:|---:|---:|---|
| Site 1 | Turnovers/km | — | — | — | — |
| Site 1 | Median RT | — | — | — | — |
| Site 1 | Equivalent active depth | — | — | — | — |

## 32. Recommended figure sequence for the web report

1. **Site map and model domain.**
2. **Three headline metric cards.**
3. **Plan-view active hyporheic extent.**
4. **Cross section or flow-path visualization.**
5. **Flux-weighted residence-time distribution.**
6. **Functional-opportunity threshold panel.**
7. **Sensitivity summary.**
8. **Cross-site frequency–duration–capacity comparison.**

---

# Part VIII — Journal Article Analysis Plan and Placeholder Manuscript

## 33. Purpose and use of the journal-paper section

This part converts the web-app reporting framework into a practical plan for a journal article based on **5–10 hyporheic hydraulic sites**. It is deliberately written as a **placeholder manuscript scaffold** rather than a completed paper because the final model runs, cross-site values, figures, and ecological-scope decisions are not yet available.

The intended workflow is:

1. Build and validate the hydraulic calculations in the web app.
2. Rerun the accepted sites using consistent model assumptions.
3. Export a paper-ready cross-site dataset and plot-ready files.
4. Replace the placeholders in this section with observed values.
5. Draft the full journal manuscript from the populated tables, figures, and comparison statements.

The manuscript should remain centered on the hydraulic contribution. Ecological outcomes should be used as:

- Literature-supported interpretations of hydraulic opportunity.
- Process-specific residence-time scenarios.
- An optional, tightly scoped macroinvertebrate example developed with the PhD student who owns and leads that ecological dataset.

### 33.1 Placeholder convention

Use double braces for values that will be inserted after the final analyses. Examples include:

- `{{N_SITES}}` — final number of accepted sites, between 5 and 10.
- `{{SITE_LIST}}` — final site names or identifiers.
- `{{C_MIN}}`, `{{C_MAX}}`, and `{{C_MEDIAN}}` — minimum, maximum, and median connectivity.
- `{{SITE_C_MIN}}` and `{{SITE_C_MAX}}` — sites with the minimum and maximum connectivity.
- `{{T50_MIN}}`, `{{T50_MAX}}`, and `{{T50_MEDIAN}}` — cross-site residence-time summaries.
- `{{DHZ_MIN}}`, `{{DHZ_MAX}}`, and `{{DHZ_MEDIAN}}` — active-capacity summaries.
- `{{RHO_C_T50}}` — descriptive rank correlation between connectivity and median residence time.
- `{{RANK_CONTRAST_EXAMPLE}}` — a specific pair of sites that illustrates why the three dimensions should remain separate.
- `{{THRESHOLD_RESULT}}` — result from a selected residence-time scenario.
- `{{SENSITIVITY_RESULT}}` — result describing uncertainty or rank stability.
- `{{ECOLOGY_RESULT}}` — optional result from the limited macroinvertebrate analysis.
- `{{CITATION_REQUIRED}}` — a statement that must be supported by a verified source before submission.
- `{{DECISION_REQUIRED}}` — a methodological or scope decision that has not yet been finalized.

Do not replace a missing value with an invented estimate. Retain the placeholder or use `TBD` in working drafts until the result is available.

### 33.2 Minimum data-completeness gate before drafting Results

The Results section should not be converted from placeholders to final prose until all of the following are complete:

- At least 5 sites have accepted baseline model runs.
- All accepted sites use the same definition of a returning hyporheic flow path.
- Flow weighting has been verified.
- Stream discharge, modeled reach length, and streambed area are documented for every site.
- Gross hyporheic exchange and net groundwater gain or loss are separated.
- The residence-time distribution is calculated consistently for every site.
- Active hyporheic volume is defined consistently as bulk volume or pore-water volume.
- Water-balance and path-accounting checks pass the project’s acceptance criteria.
- Sensitivity scenarios have been run or the lack of sensitivity analysis is disclosed.
- The default residence-time scenarios are finalized.
- Any use of macroinvertebrate data has been approved and co-developed with the PhD student.

### 33.3 Paper dataset inclusion table

Use this table during model review. Keep all 10 rows in the working file and mark unused rows as `Not used`.

| Paper site number | Site identifier | Baseline run ID | Include in paper? | Reason for exclusion, if any | QC status | Sensitivity runs complete? | Ecology data available? |
|---|---|---|---|---|---|---|---|
| 01 | `{{SITE_01}}` | `{{RUN_01}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 02 | `{{SITE_02}}` | `{{RUN_02}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 03 | `{{SITE_03}}` | `{{RUN_03}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 04 | `{{SITE_04}}` | `{{RUN_04}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 05 | `{{SITE_05}}` | `{{RUN_05}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 06 | `{{SITE_06}}` | `{{RUN_06}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 07 | `{{SITE_07}}` | `{{RUN_07}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 08 | `{{SITE_08}}` | `{{RUN_08}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 09 | `{{SITE_09}}` | `{{RUN_09}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |
| 10 | `{{SITE_10}}` | `{{RUN_10}}` | `{{YES_NO}}` | `{{REASON}}` | `{{PASS_FLAG}}` | `{{YES_NO}}` | `{{YES_NO}}` |

---

## 34. Central scientific contribution and paper positioning

### 34.1 Core thesis

A defensible central thesis is:

> Hyporheic exchange across stream sites cannot be adequately characterized by a single bulk exchange or connectivity metric. A multidimensional hydraulic description that separates exchange frequency, residence-time distribution, and active hyporheic capacity reveals hydraulic contrasts that have different implications for ecological and biogeochemical opportunity.

The paper should test this thesis using the modeled range across `{{N_SITES}}` sites rather than assuming it is true. The strongest evidence will be sites that appear similar under one metric but differ under one or both of the other dimensions.

### 34.2 Relationship to Harvey-style connectivity

A Harvey-style connectivity or turnover metric should be calculated and reported because it provides:

- A recognizable screening-level measure.
- A direct measure of exchange relative to stream discharge and reach length.
- A useful baseline for testing what additional information is gained from physics-based modeling.

The paper should not characterize the Harvey framework as incorrect. The distinction is:

- Harvey-style connectivity summarizes **how frequently streamwater is exchanged**.
- The present workflow derives that connectivity from modeled returning flow and additionally resolves **where exchanged water travels**, **how long it remains**, and **how much active subsurface volume is involved**.

A central comparison should therefore be:

> Do sites with similar turnover-based connectivity have different flux-weighted residence-time distributions, active hyporheic volumes, or threshold-specific functional opportunity?

### 34.3 Hydraulic-to-ecological bridge

Use the following interpretation structure throughout the paper:

| Hydraulic dimension | Physical meaning | Ecological springboard | Required limitation |
|---|---|---|---|
| Exchange frequency | Rate of streamwater delivery to returning hyporheic paths | Delivery of oxygen, nitrate, carbon, heat, and organisms | Delivery does not establish uptake, reaction, or habitat use |
| Exposure duration | Time available along exchanged-water pathways | Opportunity for redox change, transformation, thermal exchange, and prolonged contact | Residence time alone does not establish process completion |
| Active capacity | Amount and depth of hydraulically connected subsurface space | Potential reactive volume, potential habitat space, storage, and refuge | Hydraulic volume is not equivalent to habitat quality or accessible pore space |

The phrase **ecological opportunity** should be used when the output is based on hydraulics alone.

### 34.4 Decision on a combined index

The paper should not lead with a universal combined index. Primary conclusions should be based on the three separate dimensions.

Secondary process-specific metrics may combine connectivity and the residence-time distribution, for example:

\[
C_{functional}(t^*)=C_{1\mathrm{km}}\,P(T\ge t^*)
\]

This is appropriate only when it is labeled as a threshold-based hydraulic scenario. It should not be treated as a universal ecological score.

---

## 35. Research objectives, questions, and expectations

### 35.1 Overall objective

> Quantify and compare exchange frequency, flux-weighted residence-time distributions, and active hyporheic capacity across `{{N_SITES}}` modeled stream reaches, and evaluate how the multidimensional hydraulic results alter interpretation of potential ecological and biogeochemical function.

### 35.2 Primary research questions

1. **Cross-site range:** How much do exchange frequency, residence-time distribution, and active hyporheic capacity vary among the modeled sites?
2. **Metric complementarity:** Do the three dimensions provide redundant information, or do they produce different site rankings and hydraulic classifications?
3. **Added value beyond connectivity:** Do sites with similar Harvey-style turnover connectivity have substantially different residence-time distributions or active hyporheic capacities?
4. **Process-timescale sensitivity:** How does the quantity of potentially functionally relevant exchange change as the assumed residence-time requirement increases?
5. **Model robustness:** Are the primary cross-site contrasts stable under plausible model-input and boundary-condition scenarios?

### 35.3 Optional ecological question

Include this only if the PhD student agrees to the scope and co-leads the analysis and interpretation:

> Are one or two predefined macroinvertebrate response variables consistent with modeled active hyporheic capacity, exchange frequency, or residence-time characteristics at the subset of sites with matched observations?

### 35.4 Testable expectations

These are working expectations, not results:

- **Expectation 1:** The range and ranking of sites will differ among connectivity, residence time, and active capacity.
- **Expectation 2:** At least one pair of sites with similar connectivity will exhibit meaningfully different residence-time distributions or active volumes.
- **Expectation 3:** Site rankings based on threshold-specific functional connectivity will change as the selected residence-time threshold increases.
- **Expectation 4:** Some hydraulic contrasts will be robust across sensitivity scenarios, while other site rankings may depend on uncertain inputs such as hydraulic conductivity or depth to bedrock.
- **Optional ecological expectation:** Macroinvertebrate responses, if evaluated, will show at most partial correspondence with hydraulic metrics because habitat use also depends on sediment, oxygen, food resources, disturbance, and sampling alignment.

Do not force formal hypotheses if the final sample contains only 5 sites or if the study is primarily comparative and framework oriented. In that case, retain the language of research questions and expectations.

---

## 36. Cross-site hydraulic analysis plan

### 36.1 Analysis dataset structure

Create a paper-ready site-level table with one row per accepted baseline site and, at minimum, the following fields:

- Site identifier and display name.
- Model-run identifier.
- Modeled reach length.
- Stream discharge used for normalization.
- Streambed area.
- Gross returning hyporheic exchange, \(Q_{HEF}\).
- Streambed-normalized exchange flux, \(q_{HEF}\).
- Streamflow-equivalent turnovers per kilometer, \(C_{1\mathrm{km}}\).
- Turnover length, \(L_T\).
- Flux-weighted \(T_{10}\), \(T_{50}\), and \(T_{90}\).
- Percentage of exchange at or above each selected threshold.
- Threshold-specific functional exchange and connectivity.
- Active hyporheic volume, \(V_{HZ}\).
- Equivalent active depth, \(D_{HZ}\).
- Active streambed fraction.
- Median and P90 maximum path depth.
- Censored-flow fraction.
- Baseline and sensitivity ranges for the primary metrics.
- Model-quality flags.

Maintain separate long-format files for:

- Flow-path-level residence times and weights.
- Threshold results by site and threshold.
- Sensitivity results by site, scenario, and metric.
- Optional ecological observations and aggregation rules.

### 36.2 Site inclusion and quality control

Before cross-site comparisons:

1. Confirm that every site passes the agreed water-balance criterion.
2. Confirm that the sum of path weights matches gross returning exchange within tolerance.
3. Quantify censored and non-returning flow.
4. Confirm that all sites use comparable model domains or that normalization adequately addresses differences.
5. Inspect each spatial map for numerical artifacts or boundary-controlled flow paths.
6. Document any site retained with a warning and test whether its inclusion changes the main conclusions.

If a site fails a core quality criterion, exclude it from the primary analysis and report it separately rather than silently retaining it.

### 36.3 Descriptive cross-site range analysis

For each primary metric, report:

- Minimum and maximum.
- Median.
- Interquartile range when `{{N_SITES}}` is large enough to make it informative.
- Fold range, calculated as maximum divided by minimum when all values are positive and the ratio is meaningful.
- Site associated with the minimum and maximum.
- Baseline uncertainty or sensitivity range.

Required primary range summaries are:

1. Connectivity, \(C_{1\mathrm{km}}\).
2. Median residence time, \(T_{50}\).
3. Residence-time spread, such as \(T_{90}/T_{10}\) or the log-width of the central 80% of the RTD.
4. Equivalent active depth, \(D_{HZ}\).
5. Active streambed fraction.
6. P90 path depth.
7. Threshold-specific functional connectivity for selected thresholds.

Because the site count is small, emphasize physical magnitude, site contrasts, and uncertainty rather than relying on null-hypothesis significance tests.

### 36.4 Units, scaling, and transformations

Use original physical units in tables and text. For plots:

- Use a logarithmic axis when values span more than approximately one order of magnitude and all values are positive.
- Clearly label any log transformation.
- Do not add arbitrary constants solely to enable log plotting without documenting the choice.
- Standardized z-scores or percentile ranks may be used in a comparison heatmap, but never replace the raw metrics in the main results.
- Use consistent time units within each figure; convert to hours or days based on the observed range.

### 36.5 Metric association and rank comparison

For each pair of primary dimensions, calculate and display:

- Scatterplot in original or log-scaled units.
- Spearman rank correlation, reported descriptively as `{{RHO}}`.
- Site ranks under each metric.
- Absolute rank difference for every site.
- A rank-concordance summary across all three dimensions.

Recommended pairs are:

- \(C_{1\mathrm{km}}\) versus \(T_{50}\).
- \(C_{1\mathrm{km}}\) versus \(D_{HZ}\).
- \(T_{50}\) versus \(D_{HZ}\).
- \(Q_{HEF}\) versus \(V_{HZ}\), as a supporting physical comparison.

With only 5–10 sites, correlation coefficients should be interpreted as descriptive. Avoid presenting a non-significant p-value as evidence of no relationship. Likewise, do not treat a large coefficient from a very small sample as definitive.

### 36.6 Identify decisive site contrasts

The paper should identify at least two concrete cross-site contrasts, if they occur:

1. **Matched-connectivity contrast:** Two sites with similar \(C_{1\mathrm{km}}\) but different RTDs or active capacities.
2. **Frequency–duration tradeoff:** A high-connectivity, short-residence site compared with a low-connectivity, long-residence site.
3. **Capacity-use contrast:** A site with large active capacity but low exchange frequency compared with a site with smaller capacity but rapid turnover.
4. **Threshold reversal:** A site that ranks highly at a short threshold but much lower at a long threshold.

These examples should become the backbone of the Results and Discussion because they demonstrate why the three metrics are not interchangeable.

### 36.7 Hydraulic regime classification

For descriptive communication, classify sites relative to the study set using the three primary dimensions. A site can be described as:

- High frequency / short duration / small capacity.
- High frequency / broad duration / large capacity.
- Low frequency / long duration / large capacity.
- Intermediate across all dimensions.

Classifications should be based on stated rules, such as study-set medians or quartiles. They are not ecological quality classes.

A standardized heatmap is preferred over a universal composite score. The heatmap can show each site’s relative position for:

- Connectivity.
- Median residence time.
- RTD breadth.
- Equivalent active depth.
- Active streambed fraction.
- Selected threshold-specific opportunity.

### 36.8 Residence-time distribution comparison

The RTD analysis should include more than site medians.

For every site:

- Plot the flux-weighted cumulative distribution or exceedance curve.
- Mark \(T_{10}\), \(T_{50}\), and \(T_{90}\).
- Report censored-flow percentage.
- Calculate threshold exceedance at each selected duration.
- Identify whether the distribution is narrow, broad, bimodal, or strongly right skewed, without overinterpreting the cause.

The manuscript should explicitly identify cases where similar \(T_{50}\) values hide different distribution widths or tails.

### 36.9 Threshold-specific functional opportunity analysis

For each threshold \(t^*\), calculate:

\[
P(T\ge t^*)
\]

\[
Q_{functional}(t^*)=Q_{HEF}\,P(T\ge t^*)
\]

\[
C_{functional}(t^*)=C_{1\mathrm{km}}\,P(T\ge t^*)
\]

Use a set of transparent scenarios, initially 1, 6, 12, and 24 hours, unless the literature review supports a different set.

Analyze:

- The range across sites at each threshold.
- How each site changes as the threshold increases.
- Whether site rankings change with threshold.
- Whether sites with high total connectivity retain high functional connectivity at longer thresholds.

These results should be described as sensitivity to an assumed process timescale, not as measured reaction rates.

### 36.10 Sensitivity and uncertainty analysis

For each primary metric:

- Plot the baseline estimate.
- Add minimum–maximum or percentile intervals from accepted sensitivity scenarios.
- Report the input or assumption producing the largest change.
- Calculate rank range across scenarios.
- Identify conclusions that persist across scenarios.

A useful result statement is not merely that values are uncertain, but whether uncertainty changes the interpretation. For example:

> Although the magnitude of `{{METRIC}}` varied by `{{PERCENT_RANGE}}` across scenarios, `{{SITE_NAME}}` remained among the two highest-ranked sites in `{{N_OF_N}}` accepted runs.

### 36.11 Optional multivariate summary

A formal multivariate analysis is not required for a paper with 5–10 sites and three central metrics. If used, it should remain exploratory.

Acceptable options include:

- A standardized site-by-metric heatmap.
- Hierarchical clustering used only to visualize similarity.
- Principal-component analysis only if the final number of sites and variables is sufficient and the interpretation remains transparent.

Do not let a multivariate method replace the physically interpretable metrics.

### 36.12 Optional macroinvertebrate analysis

Use this analysis only after written agreement on scope with the PhD student.

Recommended minimum approach:

1. Select one primary ecological response before examining the hydraulic relationships, such as density standardized by sampled volume.
2. Select no more than one secondary response, such as taxonomic richness or abundance of a predefined hyporheic group.
3. Match ecological observations to the modeled reach, depth, and sampling period as closely as possible.
4. Use active capacity within the sampled depth as a candidate predictor when feasible.
5. Show raw site-level observations in scatterplots.
6. Use Spearman correlation or a simple, pre-specified model only as an exploratory analysis.
7. Do not fit a multiple-regression model with three hydraulic predictors to only 5–10 independent sites unless the data structure provides substantially more justified replication and an appropriate hierarchical analysis.
8. Report substrate, fine sediment, dissolved oxygen, temperature, and other available covariates descriptively because they may explain deviations from the hydraulic pattern.

The ecological analysis should answer a narrow question and occupy no more than one main figure or one supplemental figure unless the PhD student elects to expand her contribution.

### 36.13 Analysis decisions to pre-register internally

Before viewing the final cross-site patterns, record:

- Primary metrics.
- Threshold scenarios.
- Inclusion and exclusion criteria.
- Baseline model definition.
- Sensitivity scenarios.
- Planned range and rank comparisons.
- The one or two site contrasts that will be selected using objective criteria.
- Whether the macroinvertebrate analysis is included.
- The ecological response and predictor definitions, if included.

This internal record will reduce the risk of selecting only the most favorable results after the fact.

---

## 37. Placeholder journal manuscript framework

### 37.1 Provisional title options

1. **Beyond bulk connectivity: A multidimensional hydraulic framework for comparing hyporheic exchange across stream sites**
2. **Exchange frequency, residence time, and active capacity reveal contrasting hyporheic hydraulic regimes**
3. **Physics-based characterization of hyporheic connectivity, residence time, and spatial extent across stream reaches**
4. **From streamwater turnover to ecological opportunity: Cross-site modeling of hyporheic exchange**
5. **A web-enabled workflow for comparable, model-derived hyporheic hydraulic metrics**

Select the final title after the strongest cross-site result is known. If the web app itself is a major methodological contribution, retain it in the title or subtitle. If the hydraulic comparison is stronger, keep the app in Methods and emphasize the scientific finding in the title.

### 37.2 Running title

`{{RUNNING_TITLE: Multidimensional hyporheic hydraulics}}`

### 37.3 Abstract scaffold

**Background:** Hyporheic exchange supports stream biogeochemical processing, thermal exchange, and subsurface habitat, yet cross-site comparisons are often reduced to a bulk exchange or connectivity measure. Such measures do not separately describe the duration of subsurface exposure or the amount of active subsurface space.

**Objective:** We used a standardized, physics-based modeling workflow to quantify exchange frequency, flux-weighted residence-time distributions, and active hyporheic capacity across `{{N_SITES}}` stream reaches in `{{REGION_OR_PROJECT}}`.

**Methods:** For each site, we identified returning hyporheic flow paths and calculated gross exchange flow, streamflow-equivalent turnovers per kilometer, residence-time quantiles and exceedance, and active hyporheic volume normalized by streambed area. We compared cross-site ranges and rankings, evaluated threshold-specific functional opportunity for residence times of `{{THRESHOLD_LIST}}`, and tested sensitivity to `{{SENSITIVITY_INPUTS}}`.

**Results:** Connectivity ranged from `{{C_MIN}}` to `{{C_MAX}}` turnovers km\(^{-1}\), median residence time ranged from `{{T50_MIN}}` to `{{T50_MAX}}`, and equivalent active depth ranged from `{{DHZ_MIN}}` to `{{DHZ_MAX}}`. Site rankings `{{DID_DID_NOT}}` remain consistent among the three dimensions. In particular, `{{SITE_A}}` and `{{SITE_B}}` had similar connectivity but differed by `{{RT_OR_CAPACITY_CONTRAST}}`, demonstrating `{{CENTRAL_RESULT}}`. The amount of exchange exceeding the selected residence-time scenarios `{{THRESHOLD_SUMMARY}}`. Sensitivity analyses showed `{{SENSITIVITY_SUMMARY}}`.

**Conclusions:** The results indicate that `{{FINAL_CONCLUSION}}`. Separating exchange frequency, exposure duration, and active capacity provides a more informative hydraulic foundation for ecological inference than a single connectivity value, while avoiding claims that hydraulics alone establish ecological function.

**Optional ecological sentence:** At the subset of sites with matched observations, `{{ECOLOGY_RESULT_OR_OMIT}}`.

### 37.4 Keywords

Use 5–8 terms selected from:

- Hyporheic exchange
- Stream–groundwater interaction
- Residence-time distribution
- River turnover length
- Hydrologic connectivity
- Groundwater modeling
- Ecological opportunity
- Denitrification
- Macroinvertebrate habitat
- Decision-support web application

### 37.5 Introduction outline and draft content

#### Paragraph 1 — Why hyporheic hydraulics matter

Draft purpose:

> Introduce the hyporheic zone as a hydrologically connected interface where streamwater, groundwater, sediment, and biological communities interact. Explain that ecological and biogeochemical effects depend not merely on the presence of exchange, but on how much water exchanges, how long it remains below the streambed, and what portion of the subsurface is hydraulically active. `{{CITATION_REQUIRED}}`

#### Paragraph 2 — The cross-site measurement problem

Draft purpose:

> Explain that detailed process studies provide rich site-specific understanding, whereas regional or multi-site comparisons often rely on simplified exchange descriptors. Emphasize the need for standardized outputs that can be generated consistently across sites and interpreted without conflating distinct hydraulic dimensions. `{{CITATION_REQUIRED}}`

#### Paragraph 3 — Value and limitation of turnover-based connectivity

Draft text:

> Turnover-based connectivity provides an intuitive measure of the frequency with which streamwater enters exchange or storage zones relative to downstream transport. This screening-level descriptor is valuable for comparing exchange intensity across streams, but it does not by itself resolve the geometry of hyporheic flow paths, the full residence-time distribution, or the volume and depth of hydraulically active sediment. `{{CITATION_REQUIRED_FOR_HARVEY_FRAMEWORK}}`

#### Paragraph 4 — Study gap and contribution

Draft text:

> Physics-based groundwater modeling can provide these additional dimensions, but model outputs are often reported using site-specific variables that are difficult to compare. A reproducible reporting framework is therefore needed to translate modeled flow fields into a concise set of comparable hydraulic metrics with clearly bounded ecological interpretations.

#### Paragraph 5 — Ecological motivation without overclaiming

Draft text:

> Exchange frequency controls the delivery of streamwater constituents to the subsurface, residence time controls the duration of exposure to sediments and biogeochemical conditions, and active hyporheic capacity describes the amount of connected subsurface space potentially available for reaction, storage, and habitat. These quantities represent necessary hydraulic conditions for many functions, but they do not alone establish process rates or habitat quality. `{{CITATION_REQUIRED}}`

#### Final introduction paragraph — Objectives and questions

Draft text:

> Here, we applied a standardized modeling and reporting workflow to `{{N_SITES}}` stream reaches. We asked: (1) how strongly exchange frequency, residence-time distribution, and active capacity varied among sites; (2) whether the three dimensions produced similar or contrasting site rankings; (3) whether sites with similar turnover-based connectivity differed in their flow-path residence times or spatial extent; and (4) how estimates of hydraulic functional opportunity changed across alternative residence-time scenarios. We additionally `{{INCLUDED_DID_NOT_INCLUDE}}` a limited exploratory comparison with macroinvertebrate observations at `{{N_ECO_SITES}}` sites.

### 37.6 Methods scaffold

#### 37.6.1 Study design and sites

> We analyzed `{{N_SITES}}` stream reaches located in `{{REGION}}`. Sites were selected because `{{SELECTION_CRITERIA}}`. The modeled reaches ranged from `{{L_MIN}}` to `{{L_MAX}}` m in length, stream discharge ranged from `{{QSTREAM_MIN}}` to `{{QSTREAM_MAX}}`, and modeled streambed area ranged from `{{ABED_MIN}}` to `{{ABED_MAX}}` m\(^2\). Site characteristics and model identifiers are summarized in Table 1.

Include enough context to understand differences among sites, but do not overload the paper with every model input. Detailed inputs should be archived in a supplemental table or machine-readable repository.

#### 37.6.2 Standardized modeling workflow and web application

> We used `{{MODEL_NAME_AND_VERSION}}` through a web application that standardized model setup, execution, flow-path tracking, and hydraulic-summary calculations. The application accepted `{{CORE_INPUTS}}` and produced gridded hydraulic heads, returning hyporheic flow paths, flow-weighted residence times, and spatial measures of the active hyporheic zone. All sites were rerun using the same workflow and version `{{APP_VERSION}}`.

Document what the app automates and what still requires expert judgment. The paper should not imply that automation removes uncertainty from site conceptualization or boundary-condition selection.

#### 37.6.3 Model domain, boundaries, and parameterization

> Model domains represented `{{DOMAIN_DESCRIPTION}}`. Boundary conditions included `{{BOUNDARY_CONDITIONS}}`. Hydraulic conductivity was assigned using `{{K_METHOD}}`, effective porosity using `{{POROSITY_METHOD}}`, and the lower hydraulic boundary or depth to bedrock using `{{BEDROCK_METHOD}}`. Baseline values and site-specific inputs are provided in Table S1.

State clearly whether depth to bedrock is measured, inferred, or treated as a modeling constraint.

#### 37.6.4 Definition of returning hyporheic exchange

> Returning hyporheic flow paths were defined as stream-origin water that entered the subsurface through `{{ENTRY_BOUNDARY}}` and returned to the stream within the modeled domain and tracking period. Streamwater that left the domain without returning, regional groundwater discharge, and numerical paths that remained incomplete at the tracking limit were classified separately.

Report the fraction of flow in each category.

#### 37.6.5 Exchange-frequency metrics

> Gross returning hyporheic exchange, \(Q_{HEF}\), was calculated as the sum of downwelling flow associated with returning flow paths. Connectivity was standardized as streamflow-equivalent turnovers per kilometer:

\[
C_{1\mathrm{km}}=\frac{Q_{HEF}}{Q_{stream}}\frac{1\ \mathrm{km}}{L_{model}}.
\]

> We additionally reported streambed-normalized exchange flux, \(q_{HEF}=Q_{HEF}/A_{bed}\), and turnover length, \(L_T=L_{model}Q_{stream}/Q_{HEF}\).

#### 37.6.6 Residence-time metrics

> Residence times were calculated along returning flow paths and weighted by the flow represented by each path. For each site, we reported the 10th, 50th, and 90th percentiles of the flux-weighted distribution and plotted the full cumulative distribution. We also calculated the fraction and quantity of exchange with residence time greater than or equal to `{{THRESHOLD_LIST}}`.

State how effective porosity enters the travel-time calculation and how censored paths are handled.

#### 37.6.7 Active hyporheic capacity

> Active hyporheic capacity was quantified as `{{BULK_OR_POREWATER}}` volume associated with returning hyporheic exchange. To enable comparison across reach sizes, active volume was normalized by streambed area to produce an equivalent active depth, \(D_{HZ}=V_{HZ}/A_{bed}\). We also calculated active streambed fraction and flow-weighted path-depth statistics.

Document the spatial algorithm used to avoid double-counting overlapping flow paths.

#### 37.6.8 Threshold-specific hydraulic opportunity

> For each selected residence-time scenario, we calculated the flow fraction \(P(T\ge t^*)\), functional exchange \(Q_{functional}(t^*)\), and functional connectivity \(C_{functional}(t^*)\). These values describe the amount of exchange meeting an assumed duration and were not interpreted as direct estimates of a particular reaction or ecological response.

#### 37.6.9 Sensitivity analysis

> We evaluated sensitivity to `{{SENSITIVITY_INPUTS}}` using `{{N_SCENARIOS}}` accepted scenarios per site. We summarized baseline, minimum, and maximum values `{{OR_PERCENTILE_METHOD}}` and evaluated whether site rankings and central contrasts persisted across scenarios.

#### 37.6.10 Cross-site comparison

> We summarized the range of each metric, compared site ranks, calculated descriptive Spearman correlations among the three primary dimensions, and identified matched-connectivity contrasts. Because the analysis included only `{{N_SITES}}` independent sites, emphasis was placed on effect magnitude, physical interpretation, and uncertainty rather than statistical significance alone.

#### 37.6.11 Optional macroinvertebrate comparison

Use only if included:

> Hyporheic macroinvertebrate data were collected and analyzed under a separate ecological study led by `{{PHD_STUDENT_NAME}}`. For the present hydraulic paper, we used a limited subset consisting of `{{ECO_RESPONSE_VARIABLES}}` at `{{N_ECO_SITES}}` sites. The analysis was restricted to an exploratory comparison with `{{HYDRAULIC_PREDICTOR_OR_PREDICTORS}}`, selected before examining the relationship. Sampling depths and spatial footprints were aligned with model outputs using `{{ALIGNMENT_METHOD}}`. Detailed community analyses remain outside the scope of this paper.

If omitted, state in Discussion that ecological data were available but intentionally reserved for a separate student-led analysis.

#### 37.6.12 Reproducibility and data export

> The web application exported a site-level summary table, flow-path-level residence-time data, threshold results, sensitivity results, and model-provenance metadata. Analysis scripts and versioned exports are archived at `{{REPOSITORY_OR_ARCHIVE}}`, subject to `{{ACCESS_CONDITIONS}}`.

### 37.7 Results scaffold with explicit data placeholders

#### 37.7.1 Site and model overview

> A total of `{{N_SITES_ATTEMPTED}}` sites were modeled, of which `{{N_SITES}}` met the inclusion criteria for the primary comparison. `{{N_EXCLUDED}}` site(s) were excluded because `{{EXCLUSION_REASONS}}`. Water-balance error among accepted runs ranged from `{{WB_MIN}}` to `{{WB_MAX}}`%, and censored returning-flow fractions ranged from `{{CENSORED_MIN}}` to `{{CENSORED_MAX}}`%.

> The accepted sites represented a `{{L_FOLD}}`-fold range in reach length and a `{{QSTREAM_FOLD}}`-fold range in stream discharge (Table 1). Figure 2 shows site locations and model domains.

#### 37.7.2 Cross-site range in exchange frequency

> Streamflow-equivalent hyporheic connectivity varied from `{{C_MIN}}` to `{{C_MAX}}` turnovers km\(^{-1}\), with a median of `{{C_MEDIAN}}` and a `{{C_FOLD}}`-fold range across sites (Table 2; Figure 3a). `{{SITE_C_MAX}}` had the highest connectivity, whereas `{{SITE_C_MIN}}` had the lowest. Gross returning exchange ranged from `{{QHEF_MIN}}` to `{{QHEF_MAX}}` L s\(^{-1}\), and streambed-normalized exchange flux ranged from `{{QFLUX_MIN}}` to `{{QFLUX_MAX}}` mm d\(^{-1}\).

> Differences between raw exchange and normalized connectivity were evident at `{{SITE_OR_SITES}}`, where `{{INTERPRETATION_OF_DISCHARGE_OR_REACH_EFFECT}}`.

#### 37.7.3 Cross-site range in residence time

> Flux-weighted median residence time ranged from `{{T50_MIN}}` to `{{T50_MAX}}` `{{TIME_UNITS}}`, with `{{SITE_T50_MAX}}` having the longest median residence time and `{{SITE_T50_MIN}}` the shortest (Figure 3b). The central 80% of residence times spanned `{{T10_T90_RANGE_SUMMARY}}` across sites.

> Full RTDs revealed that `{{SITE_A}}` and `{{SITE_B}}` had similar medians but contrasting `{{TAIL_OR_BREADTH_FEATURE}}` (Figure 5). Censored flow represented `{{CENSORED_SUMMARY}}` and `{{DID_DID_NOT}}` alter the interpretation of the site comparisons.

#### 37.7.4 Cross-site range in active hyporheic capacity

> Equivalent active hyporheic depth ranged from `{{DHZ_MIN}}` to `{{DHZ_MAX}}` m, with a median of `{{DHZ_MEDIAN}}` m (Figure 3c). Total active volume ranged from `{{VHZ_MIN}}` to `{{VHZ_MAX}}` m\(^3\), active streambed fraction ranged from `{{ACTIVEBED_MIN}}` to `{{ACTIVEBED_MAX}}`%, and P90 flow-path depth ranged from `{{DEPTH90_MIN}}` to `{{DEPTH90_MAX}}` m.

> `{{SITE_CAPACITY_CONTRAST}}` illustrated the difference between total active volume and normalized active capacity.

#### 37.7.5 Comparison among the three hydraulic dimensions

> Site rankings differed among exchange frequency, residence time, and active capacity (Figure 4; Figure 7). The Spearman rank correlation was `{{RHO_C_T50}}` between connectivity and median residence time, `{{RHO_C_DHZ}}` between connectivity and equivalent active depth, and `{{RHO_T50_DHZ}}` between median residence time and equivalent active depth.

> The clearest matched-connectivity contrast occurred between `{{SITE_MATCHED_1}}` and `{{SITE_MATCHED_2}}`. Their connectivity values differed by only `{{C_PERCENT_DIFFERENCE}}`%, whereas `{{RT_DIFFERENCE}}` and `{{CAPACITY_DIFFERENCE}}`. This contrast shows that `{{INTERPRETIVE_RESULT}}`.

> Sites were descriptively grouped into `{{N_REGIMES}}` hydraulic regimes: `{{REGIME_SUMMARY}}`. These groups describe relative hydraulic behavior within the study set and are not ecological quality classes.

#### 37.7.6 Threshold-specific functional opportunity

> The proportion of exchange exceeding 1 hour ranged from `{{P1_MIN}}` to `{{P1_MAX}}`%, compared with `{{P6_MIN}}` to `{{P6_MAX}}`% at 6 hours, `{{P12_MIN}}` to `{{P12_MAX}}`% at 12 hours, and `{{P24_MIN}}` to `{{P24_MAX}}`% at 24 hours (Table 3; Figure 6).

> Site rankings changed as the assumed duration increased. `{{SITE_SHORT_THRESHOLD}}` ranked `{{RANK_SHORT}}` under the 1-hour scenario but `{{RANK_LONG}}` under the 24-hour scenario because `{{THRESHOLD_RANK_EXPLANATION}}`. In contrast, `{{SITE_STABLE}}` remained `{{STABLE_RANK_DESCRIPTION}}` across thresholds.

> These values represent hydraulic exposure scenarios and do not demonstrate that denitrification or another process occurred.

#### 37.7.7 Sensitivity and uncertainty

> Across the evaluated scenarios, connectivity changed by `{{C_SENSITIVITY_RANGE}}`, median residence time by `{{T_SENSITIVITY_RANGE}}`, and equivalent active depth by `{{D_SENSITIVITY_RANGE}}` relative to baseline values (Figure 8; Table 4). The dominant source of sensitivity was `{{DOMINANT_SENSITIVITY_INPUT}}`.

> The primary contrast between `{{ROBUST_SITE_A}}` and `{{ROBUST_SITE_B}}` persisted in `{{N_ROBUST_SCENARIOS}}` of `{{N_TOTAL_SCENARIOS}}` accepted scenario combinations. By contrast, the rank order of `{{UNSTABLE_SITES_OR_METRIC}}` was sensitive to `{{SENSITIVE_ASSUMPTION}}`.

#### 37.7.8 Optional macroinvertebrate result

Use only if approved:

> At the `{{N_ECO_SITES}}` sites with matched ecological observations, `{{ECO_RESPONSE}}` was `{{DIRECTION_OR_PATTERN}}` in relation to `{{HYDRAULIC_PREDICTOR}}` (Spearman \(\rho={{ECO_RHO}}\); Figure 9). `{{OUTLIER_OR_CONTEXT_SITE}}` deviated from the overall pattern and also differed in `{{SUBSTRATE_OXYGEN_OR_OTHER_CONTEXT}}`.

> Given the limited number of independent sites and the influence of unmodeled habitat variables, this analysis is interpreted as an exploratory consistency check rather than validation of the hydraulic framework.

If not included, replace this subsection with:

> Macroinvertebrate observations were not analyzed in this paper because they form part of a separate student-led ecological study. Their potential relationship to the modeled hydraulic metrics is identified as a future collaborative application.

### 37.8 Discussion scaffold

#### 37.8.1 Principal finding

Opening template:

> Across `{{N_SITES}}` stream reaches, exchange frequency, residence-time distribution, and active hyporheic capacity varied by `{{SUMMARY_OF_RANGES}}` and did not produce identical site rankings. The most important result was `{{CENTRAL_CROSS_SITE_CONTRAST}}`. These findings demonstrate that a single bulk connectivity metric captures only one dimension of hyporheic hydraulics.

#### 37.8.2 Why the three metrics must remain separate

Discuss:

- Connectivity as the frequency or throughput of streamwater exchange.
- RTD as the range of exposure durations, including short and long tails.
- Active capacity as the spatial amount and depth of connected sediment.
- Cases in which one dimension was high while another was low.
- Why multiplying them into one universal score would hide those tradeoffs.

Draft transition:

> The three dimensions are related through the underlying flow field, but they are not interchangeable. `{{SITE_EXAMPLE}}` showed that `{{EXAMPLE_DETAILS}}`, whereas `{{SECOND_SITE_EXAMPLE}}` showed `{{SECOND_DETAILS}}`.

#### 37.8.3 Advancement beyond turnover connectivity

Draft structure:

1. Acknowledge the value of turnover length and turnovers per distance as scalable screening metrics.
2. Explain that the present modeling produces a physically resolved estimate of returning exchange.
3. Show what was learned from the RTD and active-volume outputs that connectivity alone did not reveal.
4. Avoid framing the comparison as a contest between “simple” and “correct.”

Draft conclusion:

> Turnover connectivity remained useful for identifying `{{WHAT_IT_IDENTIFIED}}`, but it did not distinguish `{{WHAT_IT_MISSED}}`. The model-derived RTD and active-capacity metrics therefore strengthen, rather than replace, the screening framework.

#### 37.8.4 Ecological interpretation of exchange frequency

Discuss exchange frequency as the potential rate of delivery of:

- Dissolved oxygen.
- Nitrate and other nutrients.
- Dissolved and particulate carbon.
- Heat and temperature signals.
- Organisms or propagules where physically plausible.

Required caution:

> High exchange frequency does not necessarily imply high processing because rapid-return paths may provide insufficient time or unsuitable conditions for a given process.

Use `{{SITE_HIGH_FREQUENCY}}` as an example only after checking its RTD and capacity.

#### 37.8.5 Ecological interpretation of residence time

Discuss residence time as hydraulic exposure opportunity for:

- Oxygen depletion and redox transitions.
- Nitrification and denitrification sequences.
- Carbon processing.
- Thermal exchange.
- Contaminant transformation.

Use literature-derived thresholds as scenarios, not universal boundaries. For each named process, state that reaction time depends on temperature, substrates, electron donors and acceptors, microbial activity, sediment structure, and antecedent conditions.

Recommended wording:

> `{{PERCENT}}` of exchange at `{{SITE}}` exceeded the `{{T_STAR}}`-hour scenario, indicating that this fraction of modeled exchange had sufficient hydraulic duration to be considered potentially relevant under a process requiring at least that exposure. This does not establish that the process occurred.

#### 37.8.6 Ecological interpretation of active capacity

Discuss equivalent active depth and active volume as indicators of:

- Potential reactive sediment volume.
- Potential subsurface habitat space.
- Spatial storage and thermal buffering capacity.
- Depth and footprint of hydraulic connection.

Required caution:

> Modeled active volume is not synonymous with biologically accessible habitat. Pore-throat size, fine-sediment clogging, oxygen, food resources, temperature, and disturbance may strongly constrain habitat use.

For the macroinvertebrate discussion, active capacity within the sampled depth is more defensible than total modeled volume when that output can be calculated.

#### 37.8.7 Interactions and tradeoffs among dimensions

Organize the cross-site ecological implications around combinations:

| Hydraulic pattern | Potential interpretation | Important limitation |
|---|---|---|
| High frequency + short duration | Frequent delivery and rapid sediment contact | Limited opportunity for slow reactions |
| Low frequency + long duration | Long exposure along a small amount of exchanged flow | Reach-scale effect may be flow limited |
| Large capacity + low frequency | Considerable connected space used infrequently | Volume alone may overstate active function |
| High frequency + broad RTD | Multiple exposure environments and potential functional diversity | Chemistry and sediment conditions remain decisive |
| Similar frequency + different capacity or RTD | Connectivity alone misses important hydraulic structure | Differences must exceed model uncertainty |

Use the observed sites to populate at least two rows of this framework.

#### 37.8.8 Threshold scenarios and denitrification example

The discussion may use denitrification as a worked example because it is intuitive and residence-time sensitive. Before submission:

- Verify every numerical threshold against the original source.
- Describe the sediment, temperature, chemistry, and hydrologic setting of the source study.
- Avoid transferring a site-specific threshold as a universal cutoff.
- Present multiple thresholds to show sensitivity.
- Distinguish hydraulic duration from actual nitrate removal.

The strongest inference is comparative:

> Under a longer assumed reaction timescale, fewer sites retain substantial functional connectivity, and the relative ranking of sites changes.

This is more defensible than claiming that all flow longer than one or six hours denitrifies.

#### 37.8.9 Optional macroinvertebrate implications

If the limited empirical analysis is included:

- Keep the result subordinate to the hydraulic framework.
- Let the PhD student lead interpretation.
- Present raw observations and uncertainty.
- Explain mismatches using habitat factors not represented in the hydraulic model.
- Avoid taxonomic or trait analyses that belong in the separate ecological paper.

Possible discussion template:

> The exploratory association between `{{ECO_RESPONSE}}` and `{{HYDRAULIC_METRIC}}` was `{{PATTERN}}`, suggesting that the modeled metric may capture one component of habitat availability. However, `{{LIMITING_OBSERVATION}}` indicates that hydraulics alone do not determine macroinvertebrate abundance.

If not included, explain the deliberate boundary and identify the student-led analysis as the appropriate next step.

#### 37.8.10 Web-application and transferability implications

Discuss how the app contributes by:

- Standardizing calculations and definitions.
- Producing comparable site reports.
- Preserving full RTD and spatial outputs while presenting concise metrics.
- Allowing user-selected residence-time scenarios.
- Exporting reproducible, paper-ready data.

Do not claim that the app makes all sites directly comparable if model inputs, domain scales, or calibration quality remain inconsistent.

#### 37.8.11 Limitations

At minimum, discuss:

- Small site sample of 5–10 reaches.
- Site-selection limitations.
- Uncertainty in hydraulic conductivity and anisotropy.
- Depth-to-bedrock and lower-boundary assumptions.
- Boundary-condition uncertainty.
- Effective porosity and travel-time uncertainty.
- Flow-path classification and tracking duration.
- Potential scale mismatch among model outputs, literature thresholds, and ecological samples.
- Lack of coupled reaction, thermal, or habitat modeling.
- Hydrologic-condition specificity if each site represents only one flow state.
- Limited ability to make causal ecological inferences.

#### 37.8.12 Future work

Priorities may include:

- Additional sites and hydrologic conditions.
- Calibration with field measurements.
- Coupled reactive transport or temperature models.
- Seasonal or event-scale simulations.
- Direct validation with nitrate, oxygen, temperature, or tracer observations.
- Student-led macroinvertebrate habitat analysis.
- Testing whether the three metrics predict independent ecological outcomes.

### 37.9 Conclusion scaffold

> A standardized, physics-based workflow characterized hyporheic exchange across `{{N_SITES}}` stream reaches using three complementary dimensions: streamwater exchange frequency, flux-weighted exposure duration, and active hyporheic capacity. The sites spanned `{{FINAL_RANGE_SUMMARY}}`, and `{{FINAL_RANKING_OR_CONTRAST_RESULT}}`. These contrasts were not fully represented by turnover connectivity alone. Threshold-specific analyses further showed that `{{FINAL_THRESHOLD_RESULT}}`. The framework therefore provides a transparent hydraulic foundation for evaluating potential ecological and biogeochemical opportunity while retaining the distinction between modeled hydraulic conditions and observed ecological function.

### 37.10 Declarations and end matter placeholders

- **Data availability:** `{{DATA_AVAILABILITY_STATEMENT}}`
- **Code and web app availability:** `{{CODE_AVAILABILITY_STATEMENT}}`
- **Author contributions:** `{{CRediT_AUTHOR_CONTRIBUTIONS}}`
- **Funding:** `{{FUNDING_STATEMENT}}`
- **Acknowledgments:** `{{ACKNOWLEDGMENTS}}`
- **Competing interests:** `{{COMPETING_INTERESTS}}`
- **Permits or data-use restrictions:** `{{PERMITS_OR_RESTRICTIONS}}`

---

## 38. Journal tables, figures, and app-to-paper outputs

### 38.1 Table 1 — Site and model characteristics

Keep the main paper version concise. Move detailed hydraulic parameters to Table S1.

| Site | Include? | Reach length (m) | Stream discharge (L/s) | Streambed area (m²) | Model dimension | Active-volume basis | Baseline run ID |
|---|---:|---:|---:|---:|---|---|---|
| `{{SITE_01}}` | `{{YES_NO}}` | `{{L_01}}` | `{{QS_01}}` | `{{AB_01}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_01}}` |
| `{{SITE_02}}` | `{{YES_NO}}` | `{{L_02}}` | `{{QS_02}}` | `{{AB_02}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_02}}` |
| `{{SITE_03}}` | `{{YES_NO}}` | `{{L_03}}` | `{{QS_03}}` | `{{AB_03}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_03}}` |
| `{{SITE_04}}` | `{{YES_NO}}` | `{{L_04}}` | `{{QS_04}}` | `{{AB_04}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_04}}` |
| `{{SITE_05}}` | `{{YES_NO}}` | `{{L_05}}` | `{{QS_05}}` | `{{AB_05}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_05}}` |
| `{{SITE_06}}` | `{{YES_NO}}` | `{{L_06}}` | `{{QS_06}}` | `{{AB_06}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_06}}` |
| `{{SITE_07}}` | `{{YES_NO}}` | `{{L_07}}` | `{{QS_07}}` | `{{AB_07}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_07}}` |
| `{{SITE_08}}` | `{{YES_NO}}` | `{{L_08}}` | `{{QS_08}}` | `{{AB_08}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_08}}` |
| `{{SITE_09}}` | `{{YES_NO}}` | `{{L_09}}` | `{{QS_09}}` | `{{AB_09}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_09}}` |
| `{{SITE_10}}` | `{{YES_NO}}` | `{{L_10}}` | `{{QS_10}}` | `{{AB_10}}` | `{{2D_3D}}` | `{{BULK_PORE}}` | `{{RUN_10}}` |

### 38.2 Table 2 — Primary hydraulic results

This should be the principal numeric table in the journal paper.

| Site | Connectivity (turnovers/km) | Gross HEF (L/s) | RTD median [P10–P90] | Equivalent active depth (m) | Active bed (%) | P90 path depth (m) | Baseline sensitivity range available? |
|---|---:|---:|---:|---:|---:|---:|---|
| `{{SITE_01}}` | `{{C_01}}` | `{{QHEF_01}}` | `{{RTD_01}}` | `{{DHZ_01}}` | `{{FAB_01}}` | `{{D90_01}}` | `{{YES_NO}}` |
| `{{SITE_02}}` | `{{C_02}}` | `{{QHEF_02}}` | `{{RTD_02}}` | `{{DHZ_02}}` | `{{FAB_02}}` | `{{D90_02}}` | `{{YES_NO}}` |
| `{{SITE_03}}` | `{{C_03}}` | `{{QHEF_03}}` | `{{RTD_03}}` | `{{DHZ_03}}` | `{{FAB_03}}` | `{{D90_03}}` | `{{YES_NO}}` |
| `{{SITE_04}}` | `{{C_04}}` | `{{QHEF_04}}` | `{{RTD_04}}` | `{{DHZ_04}}` | `{{FAB_04}}` | `{{D90_04}}` | `{{YES_NO}}` |
| `{{SITE_05}}` | `{{C_05}}` | `{{QHEF_05}}` | `{{RTD_05}}` | `{{DHZ_05}}` | `{{FAB_05}}` | `{{D90_05}}` | `{{YES_NO}}` |
| `{{SITE_06}}` | `{{C_06}}` | `{{QHEF_06}}` | `{{RTD_06}}` | `{{DHZ_06}}` | `{{FAB_06}}` | `{{D90_06}}` | `{{YES_NO}}` |
| `{{SITE_07}}` | `{{C_07}}` | `{{QHEF_07}}` | `{{RTD_07}}` | `{{DHZ_07}}` | `{{FAB_07}}` | `{{D90_07}}` | `{{YES_NO}}` |
| `{{SITE_08}}` | `{{C_08}}` | `{{QHEF_08}}` | `{{RTD_08}}` | `{{DHZ_08}}` | `{{FAB_08}}` | `{{D90_08}}` | `{{YES_NO}}` |
| `{{SITE_09}}` | `{{C_09}}` | `{{QHEF_09}}` | `{{RTD_09}}` | `{{DHZ_09}}` | `{{FAB_09}}` | `{{D90_09}}` | `{{YES_NO}}` |
| `{{SITE_10}}` | `{{C_10}}` | `{{QHEF_10}}` | `{{RTD_10}}` | `{{DHZ_10}}` | `{{FAB_10}}` | `{{D90_10}}` | `{{YES_NO}}` |

Delete unused rows only in the submitted paper; retain them in the working template.

### 38.3 Table 3 — Residence-time scenario results

A compact main-paper version may include two thresholds. Put the complete 1-, 6-, 12-, and 24-hour matrix in the supplement or web app.

| Site | Flow ≥1 hr (%) | Flow ≥6 hr (%) | Flow ≥12 hr (%) | Flow ≥24 hr (%) | Functional connectivity at 6 hr | Functional connectivity at 24 hr |
|---|---:|---:|---:|---:|---:|---:|
| `{{SITE_01}}` | `{{P1_01}}` | `{{P6_01}}` | `{{P12_01}}` | `{{P24_01}}` | `{{CF6_01}}` | `{{CF24_01}}` |
| `{{SITE_02}}` | `{{P1_02}}` | `{{P6_02}}` | `{{P12_02}}` | `{{P24_02}}` | `{{CF6_02}}` | `{{CF24_02}}` |
| `{{SITE_03}}` | `{{P1_03}}` | `{{P6_03}}` | `{{P12_03}}` | `{{P24_03}}` | `{{CF6_03}}` | `{{CF24_03}}` |
| `{{SITE_04}}` | `{{P1_04}}` | `{{P6_04}}` | `{{P12_04}}` | `{{P24_04}}` | `{{CF6_04}}` | `{{CF24_04}}` |
| `{{SITE_05}}` | `{{P1_05}}` | `{{P6_05}}` | `{{P12_05}}` | `{{P24_05}}` | `{{CF6_05}}` | `{{CF24_05}}` |
| `{{SITE_06}}` | `{{P1_06}}` | `{{P6_06}}` | `{{P12_06}}` | `{{P24_06}}` | `{{CF6_06}}` | `{{CF24_06}}` |
| `{{SITE_07}}` | `{{P1_07}}` | `{{P6_07}}` | `{{P12_07}}` | `{{P24_07}}` | `{{CF6_07}}` | `{{CF24_07}}` |
| `{{SITE_08}}` | `{{P1_08}}` | `{{P6_08}}` | `{{P12_08}}` | `{{P24_08}}` | `{{CF6_08}}` | `{{CF24_08}}` |
| `{{SITE_09}}` | `{{P1_09}}` | `{{P6_09}}` | `{{P12_09}}` | `{{P24_09}}` | `{{CF6_09}}` | `{{CF24_09}}` |
| `{{SITE_10}}` | `{{P1_10}}` | `{{P6_10}}` | `{{P12_10}}` | `{{P24_10}}` | `{{CF6_10}}` | `{{CF24_10}}` |

### 38.4 Table 4 — Sensitivity and ranking stability

| Site | Metric | Baseline | Minimum accepted scenario | Maximum accepted scenario | Baseline rank | Rank range | Dominant assumption |
|---|---|---:|---:|---:|---:|---|---|
| `{{SITE}}` | Connectivity | `{{BASE}}` | `{{MIN}}` | `{{MAX}}` | `{{RANK}}` | `{{RANGE}}` | `{{INPUT}}` |
| `{{SITE}}` | Median residence time | `{{BASE}}` | `{{MIN}}` | `{{MAX}}` | `{{RANK}}` | `{{RANGE}}` | `{{INPUT}}` |
| `{{SITE}}` | Equivalent active depth | `{{BASE}}` | `{{MIN}}` | `{{MAX}}` | `{{RANK}}` | `{{RANGE}}` | `{{INPUT}}` |

Use long format in the supplement rather than repeating this three-row block manually for every site.

### 38.5 Optional Table 5 — Limited ecological comparison

| Site | Ecological response | Sampling depth or volume | Matched hydraulic predictor | Predictor value | Key habitat covariates | Alignment flag |
|---|---:|---:|---|---:|---|---|
| `{{SITE}}` | `{{ECO_VALUE}}` | `{{SAMPLE_BASIS}}` | `{{HYD_METRIC}}` | `{{HYD_VALUE}}` | `{{COVARIATES}}` | `{{GOOD_CAUTION}}` |

### 38.6 Figure 1 — Conceptual framework

Show a stream reach and representative flow paths with three labeled dimensions:

1. **Frequency:** arrows crossing the streambed and a turnover-per-distance symbol.
2. **Duration:** short and long flow paths with residence-time labels.
3. **Capacity:** shaded active subsurface volume and depth.

Add an ecological interpretation band:

- Delivery.
- Exposure.
- Potential reactive or habitat space.

The figure should explicitly state that these create opportunity but do not directly measure ecological function.

### 38.7 Figure 2 — Site map and model domains

Include:

- Locations of all accepted sites.
- Consistent site identifiers used throughout the paper.
- Optional inset showing model reach geometry or representative domain.
- Clear indication of any sites with ecological observations.

### 38.8 Figure 3 — Cross-site hydraulic range plots

This is the essential figure for showing the range of hydraulic results across 5–10 sites.

Use three aligned horizontal dot-and-whisker panels:

- **Panel a:** connectivity, \(C_{1\mathrm{km}}\).
- **Panel b:** flux-weighted median residence time, \(T_{50}\).
- **Panel c:** equivalent active depth, \(D_{HZ}\).

Recommended design:

- One row per site in identical order across all panels.
- Baseline estimate shown as a point.
- Sensitivity minimum–maximum or percentile interval shown as a horizontal line.
- Sites sorted by one predefined metric or grouped by hydraulic regime.
- Raw physical units on each x-axis.
- Logarithmic axes when required by the range.
- A note that whiskers represent model sensitivity, not sampling error.

Optional additional panels:

- Active streambed fraction.
- P90 path depth.
- Gross exchange flow.

Do not overload the main figure. Supporting panels can move to the supplement.

### 38.9 Figure 4 — Frequency–duration–capacity comparison

Use a scatterplot with:

- X-axis: connectivity.
- Y-axis: median residence time.
- Point area: equivalent active depth or active volume.
- Site labels or a non-overlapping identifier legend.
- Optional point outline or symbol for sites with ecological observations.

The figure should visually reveal:

- High-frequency, short-duration sites.
- Low-frequency, long-duration sites.
- Sites with similar connectivity but different capacity.
- Potential matched-connectivity contrasts.

A two-dimensional plot with point size is preferable to a perspective 3D plot because it is easier to interpret and reproduce.

### 38.10 Figure 5 — Flux-weighted residence-time distributions

Plot one cumulative distribution or exceedance curve per site.

Requirements:

- Use a log-scaled time axis if needed.
- Mark selected thresholds.
- Use consistent flow weighting.
- Indicate censored-flow fraction in the caption or accompanying table.
- Consider small multiples if 10 overlaid curves are difficult to distinguish.

The caption should highlight one or two sites with similar medians but different distribution breadth or tails.

### 38.11 Figure 6 — Threshold-specific functional opportunity matrix

Use a site-by-threshold heatmap showing one selected variable:

- Percentage of exchange exceeding the threshold, or
- Functional connectivity.

Recommended companion annotation:

- Numeric values in cells when legible.
- Site rank at each threshold.
- Arrows or notes for large rank changes.

This figure should communicate that ecological interpretation depends on the assumed process timescale.

### 38.12 Figure 7 — Added information beyond connectivity

Use one of the following:

- Rank-slope chart comparing connectivity rank, residence-time rank, and active-capacity rank.
- Paired site-profile plot for matched-connectivity examples.
- Standardized metric heatmap with raw values in an adjacent table.

The figure should answer:

> What would be missed if the paper reported only Harvey-style connectivity?

### 38.13 Figure 8 — Sensitivity and robustness

Show:

- Baseline and scenario ranges for each primary metric.
- Rank stability across scenarios.
- The sensitivity variable responsible for the largest shift.

This may be a main figure if uncertainty is central to the paper or a supplemental figure if the main conclusions are highly stable.

### 38.14 Optional Figure 9 — Macroinvertebrate comparison

Only include after scope agreement.

Recommended design:

- Raw points with site labels.
- One ecological response per panel.
- One hydraulic predictor per panel.
- No fitted curve unless justified by the sample size and analysis plan.
- Annotations for sampling-depth alignment or important habitat differences.

The caption must state that the analysis is exploratory and not a validation of habitat quality.

### 38.15 Supplemental tables and figures

Recommended supplement:

- Table S1: complete site inputs and boundary conditions.
- Table S2: all primary and supporting hydraulic outputs.
- Table S3: complete threshold matrix.
- Table S4: sensitivity scenarios and model provenance.
- Table S5: flow-path accounting and quality-control results.
- Figure S1–S`{{N_SITES}}`: plan-view and cross-sectional outputs for each site.
- Figure S`{{NEXT}}`: supporting range plots.
- Figure S`{{NEXT}}`: active-volume classification sensitivity.
- Figure S`{{NEXT}}`: optional ecological supporting plots.

### 38.16 Required paper-ready exports from the web app

The app should create a download package containing:

1. `site_summary.csv` — one row per site with all primary and supporting metrics.
2. `residence_time_paths.csv` or a compressed equivalent — path-level residence time, flow weight, entry and exit information, and censoring status.
3. `residence_time_quantiles.csv` — site-level weighted quantiles.
4. `threshold_results_long.csv` — one row per site and threshold.
5. `sensitivity_results_long.csv` — one row per site, scenario, and metric.
6. `model_provenance.csv` — app version, model version, run ID, parameter set, and timestamps.
7. `quality_control.csv` — water balance, path accounting, censoring, and warnings.
8. `paper_table_1_sites.csv` — fields required for Table 1.
9. `paper_table_2_metrics.csv` — fields required for Table 2.
10. `paper_plot_data.csv` — a tidy file with metric name, value, units, site, and sensitivity interval.
11. Figure files in vector format where possible and high-resolution raster format as a fallback.
12. A Markdown or text summary containing the cross-site minima, maxima, medians, ranks, and candidate contrast statements.

### 38.17 App field-to-manuscript placeholder map

| Manuscript placeholder | App or export field | Used in |
|---|---|---|
| `{{N_SITES}}` | Count of accepted baseline sites | Abstract, Methods, Results |
| `{{C_MIN}}`, `{{C_MAX}}`, `{{C_MEDIAN}}` | Summary of `connectivity_turnovers_per_km` | Abstract, Results |
| `{{T50_MIN}}`, `{{T50_MAX}}` | Summary of `residence_time_p50` | Abstract, Results |
| `{{DHZ_MIN}}`, `{{DHZ_MAX}}` | Summary of `equivalent_active_depth_m` | Abstract, Results |
| `{{SITE_C_MAX}}`, `{{SITE_C_MIN}}` | Site names at connectivity extrema | Results |
| `{{RHO_C_T50}}` | Cross-site descriptive Spearman correlation | Results |
| `{{P6_MIN}}`, `{{P6_MAX}}` | Range of `flow_fraction_ge_6h` | Results |
| `{{DOMINANT_SENSITIVITY_INPUT}}` | Input causing largest standardized change | Results, Discussion |
| `{{CENTRAL_CROSS_SITE_CONTRAST}}` | Rule-based matched-site comparison | Abstract, Discussion |

### 38.18 Rule-based generation of candidate result statements

The app may generate draft statements for review, not automatic publication.

Examples:

- **Range:** “Across `{{N_SITES}}` sites, `{{METRIC}}` ranged from `{{MIN}}` to `{{MAX}}` `{{UNITS}}`, with a median of `{{MEDIAN}}`.”
- **Extrema:** “`{{SITE_MAX}}` had the highest `{{METRIC}}`, whereas `{{SITE_MIN}}` had the lowest.”
- **Matched connectivity:** “`{{SITE_A}}` and `{{SITE_B}}` differed by less than `{{MATCH_TOLERANCE}}`% in connectivity but by `{{CONTRAST_MAGNITUDE}}` in `{{SECONDARY_METRIC}}`.”
- **Threshold change:** “Increasing the threshold from `{{T_LOW}}` to `{{T_HIGH}}` reduced functional connectivity by `{{PERCENT_CHANGE}}`% at `{{SITE}}`.”
- **Rank stability:** “`{{SITE}}` remained within ranks `{{RANK_MIN}}`–`{{RANK_MAX}}` across accepted sensitivity scenarios.”

Every generated statement should link back to the underlying values and allow manual editing.

### 38.19 Recommended paper drafting order

Draft in this order after the data freeze:

1. Methods, using finalized model definitions.
2. Tables and figure captions.
3. Results, written directly from the plotted and tabulated values.
4. Discussion of the strongest hydraulic contrasts.
5. Literature-supported ecological implications.
6. Introduction, revised to match the actual contribution.
7. Abstract and title last.

### 38.20 Journal-paper decision points that remain open

Before final drafting, resolve:

- Final number of sites.
- Whether the paper emphasizes the web-app method, the hydraulic comparison, or both.
- Which thresholds appear in the main paper.
- Whether the main RTD figure uses CDFs, exceedance curves, or small multiples.
- Whether sensitivity is a main or supplemental figure.
- Whether macroinvertebrate data are included.
- Which journal and corresponding length and figure limits apply.
- Which citations support each ecological mechanism and threshold.

---

# Part IX — Implementation Priorities

## 39. Minimum viable report

The first complete version of the web app should:

1. Apply a consistent definition of returning hyporheic flow.
2. Calculate \(Q_{HEF}\), \(C_{1\mathrm{km}}\), \(q_{HEF}\), and \(L_T\).
3. Calculate a flux-weighted RTD with \(T_{10}\), \(T_{50}\), and \(T_{90}\).
4. Calculate active volume, equivalent active depth, active bed fraction, and P90 path depth.
5. Calculate residence-time exceedance at 1, 6, 12, and 24 hours.
6. Calculate threshold-specific functional exchange and connectivity.
7. Produce the main site table and cross-site table.
8. Produce a residence-time CDF and spatial extent maps.
9. Display model-quality flags.
10. Export all calculations in a documented machine-readable format.
11. Produce a 5–10-site paper-ready summary table with unused site rows suppressed in presentation but retained in the template.
12. Produce plot-ready files for the cross-site range figure, RTD figure, threshold matrix, and sensitivity intervals.
13. Generate a reviewed set of manuscript placeholders and candidate result statements without presenting them as final scientific conclusions.

## 40. Second implementation phase

Add:

- User-defined thresholds.
- Baseline-versus-sensitivity comparison.
- Rank stability across model scenarios.
- Automated narrative interpretation.
- Literature-threshold metadata and citations.
- Optional macroinvertebrate overlay after collaboration and scope approval.

## 41. Later or optional features

Consider later:

- Reaction-timescale integration using the full RTD.
- Spatially variable process thresholds.
- Coupled temperature or solute-reaction modeling.
- Hydrologic-condition ensembles.
- Additional ecological datasets.
- Formal predictive models after sufficient sites and validation data are available.

---

# Part X — Decisions That Must Be Locked Before Final Production

## 42. Modeling and calculation decisions

Before all sites are rerun, document the final decision for each item below:

- Exact definition of a returning hyporheic flow path.
- Treatment of paths that leave the model domain.
- Treatment of censored or incomplete paths.
- Method used to assign flow weights to paths.
- Stream-discharge basis used for normalization.
- Definition and spatial algorithm for active volume.
- Bulk-volume versus pore-water-volume reporting.
- Method for calculating streambed area.
- Method for calculating path depth below the streambed.
- Baseline values for depth to bedrock and hydraulic conductivity.
- Sensitivity scenarios to be run consistently across sites.
- Default residence-time thresholds.
- Cross-site reference set used for relative descriptors.

## 43. Ecological decisions

Before adding ecological interpretation, document:

- Which ecological processes will be discussed using literature.
- Which thresholds are sufficiently supported to include as named process scenarios.
- Whether the Texas State macroinvertebrate data will be used.
- Which ecological variables will be included.
- How the modeled spatial and temporal scales will be aligned with sampling.
- The PhD student’s role in selecting, analyzing, and interpreting the ecological data.
- Which ecological analyses remain reserved for her separate work.

---

# Part XI — Final Recommended Path Forward

## 44. Clear sequence of work

### Step 1 — Finalize metric definitions

Adopt three primary dimensions:

- Exchange frequency: \(C_{1\mathrm{km}}\).
- Exposure duration: flux-weighted RTD.
- Active capacity: \(D_{HZ}\).

Lock the supporting definitions for gross exchange, flow weighting, active volume, and path classification.

### Step 2 — Implement the calculations in the web app

Build the site-level calculation pipeline, quality checks, and machine-readable outputs before adding extensive narrative or ecological interpretation.

### Step 3 — Rerun all sites consistently

Apply the same model assumptions, spatial definitions, and report calculations across the 5–10 accepted study sites.

### Step 4 — Review cross-site hydraulic contrasts

Identify cases of:

- High exchange and short residence.
- Low exchange and long residence.
- Large capacity and low connectivity.
- Similar connectivity but different RTDs.

These contrasts should shape the journal paper’s central results.

### Step 5 — Add process-specific threshold scenarios

Use residence-time thresholds to calculate the percentage and quantity of exchange meeting selected exposure durations. Keep these scenarios process neutral until defensible literature support and required conditions are documented.

### Step 6 — Decide whether to include the macroinvertebrate example

Include it only if:

- The PhD student agrees with the scope.
- The data align with the modeled domain and depth.
- One or two focused response variables can be selected.
- The analysis remains exploratory and does not preempt her primary ecological paper.

### Step 7 — Use the web-app outputs to draft the journal article

The app’s standardized tables, figures, uncertainty summaries, and interpretation language should provide the direct foundation for the methods, results, and discussion.

Populate the Part VIII placeholders only after the site dataset is frozen. Begin with the cross-site range plots and primary results table, identify the strongest matched-connectivity and frequency–duration–capacity contrasts, and then build the ecological discussion around those observed hydraulic patterns.

---

## 45. Final summary

The project should report three separate primary hydraulic dimensions:

1. **Exchange frequency** — streamflow-equivalent turnovers per kilometer.
2. **Exposure duration** — the flux-weighted residence-time distribution.
3. **Active hyporheic capacity** — active hyporheic volume normalized by streambed area.

These metrics should be supported by gross exchange flow, exchange flux, turnover length, active volume, active streambed fraction, and path-depth statistics.

The web app should then calculate process-specific **functional opportunity** by determining how much exchanged water remains in the hyporheic zone longer than a selected residence-time threshold. This approach preserves the physics of the modeled system, avoids an unsupported universal index, and creates a transparent bridge between hydraulic results and ecological hypotheses.

Existing literature should provide the primary ecological interpretation. The Texas State macroinvertebrate dataset may be included only as a focused, collaborative, exploratory application that does not replace or preempt the PhD student’s broader ecological work.

The organizing message for both the web app and the journal article is:

> **Hyporheic ecological opportunity is shaped by how frequently water enters the subsurface, how long it remains there, and how much hydraulically active subsurface space is available.**

Part VIII provides the direct bridge from that organizing framework to a paper based on 5–10 sites, with explicit placeholders for the final hydraulic ranges, cross-site contrasts, figures, ecological implications, and optional collaborative macroinvertebrate example.
