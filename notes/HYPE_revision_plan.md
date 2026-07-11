# HYPE Revision Implementation Plan

**Date:** 2026-07-10  
**Source brief:** HYPE_brainstorming.md  
**Plan status:** Detailed planning specification  
**Implementation status:** Not started by this document  
**Target application:** HYPE - Hyporheic Exchange Explorer

## 1. Purpose

This document converts the HYPE brainstorming items into a coordinated, implementation-ready revision plan. It defines the intended behavior, scientific methods, data contracts, persistence requirements, failure handling, delivery sequence, and acceptance tests for:

1. USGS StreamStats and National Streamflow Statistics integration.
2. NRCS Soil Data Access integration and depth-aware hydraulic conductivity.
3. Qualitative and quantitative groundwater-gradient controls.
4. Expanded hyporheic metrics and default Results behavior.
5. A literature-derived Hyporheic Functional Capacity Index.
6. Groundwater-gradient sensitivity and range analysis.
7. A printable and exportable site Summary Report.

The implementation must preserve manual workflows and all current uncommitted HYPE development. It must use the current working tree as its baseline rather than resetting to the repository's main branch.

## 2. Current-State Findings

Repository investigation identified substantial work that already exists and should be completed or extended instead of replaced.

### 2.1 Existing capabilities

- The app already contains:
  - A manual HEC-RAS flow input in cfs.
  - Four-corner groundwater-gradient inputs.
  - Raw left/right spatial gradient profile strings.
  - Manual KH, KV, porosity, and K-zone inputs.
  - MODFLOW 6 and MODPATH 7 execution.
  - Hyporheic, losing, gaining, and throughflow particle classification.
  - Fraction-weighted saturated zone volume.
  - A binary plan-view footprint.
  - Residence-time mean, median, minimum, and maximum.
  - Sampled classed flow paths and 3D zone shells.
  - Project archive scaffolding.
- The existing gradient engine computes heads at endpoints or profile anchors and interpolates heads between them. It does not provide structured control-point editing, map markers, strict validation, or realized-head diagnostics.
- The existing K-zone mapper:
  - Uses one shared KH/KV pair for all app-drawn polygons.
  - Assigns the largest-overlap polygon to each horizontal cell.
  - Can use absolute top/bottom elevations but cannot correctly interpret SSURGO horizon depths below local land surface.
- Existing hyporheic-zone volume is saturated bulk sediment volume. It is not pore-water storage because porosity is not applied.
- Existing particle statistics are not flux weighted. Equal particles are seeded throughout active interior cells, so particle counts and residence-time summaries cannot be used directly as river-water exchange fluxes or return-flow transit-time distributions.
- All four path classes currently become visible after HZ delineation. The requested default is only hyporheic paths and hyporheic volume.
- Project download currently derives some configuration from live UI values at download time. Those values can differ from the inputs used in the completed run.
- Project save/open support is incomplete: archive code supports a state manifest, but the app does not yet persist and restore the complete session state.
- There is no established first-party automated test suite.

### 2.2 Baseline handling

Before implementation:

- Record the current modified and untracked files.
- Do not reset, discard, or rewrite unrelated existing work.
- Add new behavior through focused modules and narrow app integration points.
- Run import and syntax checks before and after each implementation phase.
- Establish tests before changing numerical behavior.

## 3. Locked Product and Scientific Decisions

The following decisions were made during plan development and should not be reopened during implementation unless new evidence shows they are infeasible.

### 3.1 Common workflow

- HYPE will have one workflow.
- It will not have separate Screening and Engineering modes or automated analysis tiers.
- Every result will instead report:
  - Data source.
  - Retrieval date.
  - User modifications.
  - Applied defaults.
  - Missing or fallback data.
  - Scientific-method version.
  - Model warnings and limitations.

### 3.2 USGS flow lookup

- Request and show all usable approved regional discharge statistics.
- Query the national catalog automatically only when no usable regional discharge result exists.
- Also let the user explicitly request national results for comparison.
- Do not invent generic area weighting across multiple regression regions.
- Permit otherwise valid national, extrapolated, incomplete, stale, or user-edited values after prominent warnings.
- Invalid, excluded, nonfinite, nonpositive, or non-discharge results cannot populate the flow input.

### 3.3 NRCS conductivity

- Treat NRCS representative Ksat as vertical saturated hydraulic conductivity.
- Convert it to model KV.
- Derive KH using a visible anisotropy ratio.
- Prefill that ratio from the current project KH divided by KV, which is 10:1 with current defaults.
- Require the user to review and confirm the aggregation method.
- Preserve manual K zones as the highest-priority spatial override.

### 3.4 Groundwater gradients

- Qualitative categories are selected independently for the left and right floodplain boundaries.
- Category centers are:
  - Strongly gaining: +1.0 times reference slope.
  - Slightly gaining: +0.5 times reference slope.
  - Neutral: 0.
  - Slightly losing: -0.5 times reference slope.
  - Strongly losing: -1.0 times reference slope.
- Positive gradient means the floodplain boundary head is above the adjacent stream WSE and represents gaining conditions.
- The selected numerical method is head-anchor interpolation:
  1. Calculate a head at each gradient control point.
  2. Interpolate those heads by boundary arc length between controls.
- The plan does not use gradient-first calculation at every boundary cell.

### 3.5 Sensitivity

- The default sensitivity design is three linked scenarios: lower, preferred, and upper.
- Advanced one-at-a-time, crossed left/right, percentage, absolute, and custom scenario designs remain available.
- Only the preferred scenario retains full display and model artifacts.
- Alternative scenarios retain complete numerical metrics and compact diagnostics.

### 3.6 HFCI

- Harvey et al. (2019) is the controlling connectivity reference.
- The shipped method is labeled **Literature-derived HFCI v1 - validation ongoing**.
- Capacity scores are whole numbers from 0 through 15.
- Capacity classes are:

| Score | Class | Display color |
|---:|---|---|
| 0-5 | Low | Red |
| 6-10 | Moderate | Yellow |
| 11-15 | High | Blue |

- The HFCI is an equal arithmetic mean of normalized component scores:

  HFCI = ((Exchange / 15) + (Storage / 15) + (Processing / 15)) / 3

- HFCI ranges from 0.00 through 1.00.
- HFCI and its components represent hydraulic functional capacity or potential, not direct ecological health or measured chemical, thermal, habitat, or biological performance.

### 3.7 Reporting

- Produce both self-contained HTML and native PDF.
- Also export CSV and JSON data.
- HTML, PDF, modal content, and CSV must read from the same immutable result snapshot.

## 4. Shared Architecture

## 4.1 Modular responsibilities

Keep app.py focused on Shiny orchestration. Add focused modules for:

- Versioned contracts and validation.
- HTTP service reliability.
- StreamStats/NSS workflow.
- NRCS spatial and tabular acquisition.
- Soil-profile normalization and grid conductivity mapping.
- Gradient configuration and realized boundary heads.
- Flux-weighted exchange and transit-time metrics.
- HFCI scoring.
- Sensitivity scenario generation and execution.
- Canonical result snapshots.
- HTML/PDF report rendering.

Scientific functions must be callable without a live Shiny session and tested with deterministic inputs.

## 4.2 Versioned contracts

Implement Pydantic models for the following public contracts.

### AssessmentInputSnapshot

Freeze this object when a run begins. It contains:

- Schema version and assessment ID.
- Site name, analyst/organization, notes, and assessment date.
- Reach geometry, upstream/downstream endpoints, outlet, and length.
- Terrain and WSE source metadata.
- Canonical streamflow value and provenance.
- Soil snapshot ID and effective K policy.
- Manual K values and K-zone overrides.
- Structured gradient configuration.
- Grid, model-depth, layer, porosity, and particle settings.
- Model and application versions.
- Source timestamps and citations.
- Canonical input hash.

No report or project download may reconstruct run inputs from current UI values when this snapshot exists.

### FlowLookupSnapshot

Contains:

- Requested lookup point.
- USGS-snapped point and snap distance.
- Selected region.
- Watershed geometry.
- Regression regions and weights.
- Basin characteristics.
- All normalized flow candidates.
- Raw-response artifact paths.
- Service endpoints and retrieval timestamp.
- Warnings, exclusions, methods, approval status, and citations.
- Selected candidate ID, if any.

### FlowCandidate

Contains:

- Stable candidate ID.
- Statistic group, statistic code, result code, and description.
- Original value/unit.
- Normalized cfs and m3/s values.
- Recurrence, duration, annual-exceedance, or nonexceedance metadata where explicit.
- Regression region and weight.
- Equation and parameter/range information.
- Approval and applicability status.
- Uncertainty fields.
- Warnings and citations.
- Boolean insertable status.

### SoilDataSnapshot

Contains:

- Spatial and tabular retrieval timestamps.
- Service endpoints and survey versions.
- Clipped soil polygons.
- Map units, components, horizons, textures, and restrictions.
- Missing-data diagnostics.
- Overrides.
- Selected conductivity derivation policy.
- Raw-response artifact paths.

### DerivedConductivityProfile

Contains:

- Source map-unit, component, horizon, and polygon keys.
- Depth interval below local ground.
- Original Ksat and unit.
- Converted KV.
- Anisotropy ratio and derived KH.
- Aggregation and fallback method.
- Override provenance.

### GridConductivityAssignment

Contains one row per cell/layer assignment:

- Row, column, and layer.
- KH and KV.
- Source keys.
- Polygon overlap and horizon-depth fractions.
- Direct, derived, override, or fallback origin.
- Warnings.

### GradientBoundaryConfigV2

Contains:

- Schema and method version.
- Units and sign convention.
- Qualitative or quantitative source mode.
- Reference slope value, source, and method.
- Left/right controls.
- Control ID, station, preferred gradient, lower gradient, upper gradient, and provenance.
- Legacy-method metadata when applicable.

### SensitivityScenarioManifest

Contains:

- Generator type.
- Preferred scenario ID.
- Ordered scenarios.
- Canonical scenario hashes.
- Status, timings, errors, and warnings.
- Paths to compact or full outputs.
- Cancellation/resume metadata.

### AssessmentResultsV2

Contains:

- Frozen input snapshot reference.
- Preferred and alternative scenario results.
- Exchange, storage, processing, and HFCI metrics.
- Complete provenance and warnings.
- Result quality and censoring diagnostics.
- Figure and table artifact paths.
- Report-generation status.

### HFCIScoringProfileV1

Contains:

- Profile ID and semantic version.
- Literature-derived validation label.
- Applicable domain.
- Exchange, storage, and processing curves.
- Curve knots/equations and raw units.
- Rounding rule.
- Class boundaries and colors.
- Citations, evidence notes, and change log.

## 4.3 Staleness and dependency invalidation

Compute hashes for:

- Reach and boundary geometry.
- DEM and WSE.
- Streamflow and source.
- Soil/K assignments.
- Gradients.
- Grid and model depth.
- Porosity and particle settings.

Any change invalidates every dependent result. The UI must distinguish:

- Current.
- Stale but retained.
- Missing.
- Failed.

Stale results remain viewable but are labeled prominently and cannot be silently presented as current in a new report.

## 4.4 Project archive revision

Bump the archive format from version 1 to version 2.

The version-2 project contains:

~~~text
hype_workspace/
  1_Reach_Centerline/
  2_Terrain/
  3_Boundaries/
  4_Surface_Water/
  5_Groundwater/
  6_Site_Report/
  data_sources/
    usgs/
    nrcs/
  sensitivity/
  config/
    state.json
    assessment_input.json
    run_config.json
    scoring_profile.json
~~~

Requirements:

- Save actual run snapshots.
- Restore all new UI selections and provenance.
- Open version-1 projects with explicit legacy adapters.
- Never rewrite old scoring/profile versions during restore.
- Use offline saved service snapshots until the user explicitly refreshes.

## 5. USGS Streamflow Integration

## 5.1 User workflow

1. Keep a canonical flow input available even when the WSE source is uploaded or drawn.
2. Add **Get USGS Flow** beside it.
3. Open a modal showing:
   - Reach and domain.
   - Requested outlet marker.
   - USGS-snapped marker after delineation.
   - Editable coordinates.
   - Suggested StreamStats region and manual override.
   - Workflow progress and cancellation.
4. Initialize the requested point from the oriented reach's downstream endpoint or its intersection with the downstream cap.
5. Allow the user to drag the marker and rerun.
6. Display delineated watershed, snap distance, warnings, and all returned discharge candidates.
7. Let the user select a candidate and insert it into the canonical flow input.
8. Preserve manual editing.

## 5.2 Service workflow

Implement the documented current sequence:

1. Call SS-Delineate for the selected point and region.
2. Validate that a global watershed feature exists.
3. Discover regression regions and available scenarios through NSS.
4. Collect the union of all basin-characteristic requirements across all scenarios and regions.
5. Calculate basin characteristics with SS-Hydro.
6. Match characteristics to scenario parameters case-insensitively.
7. Calculate scenario estimates through NSS.
8. Retrieve regional limitations, methods, status metadata, citations, and general information.
9. If no regional discharge candidate is usable, query the national catalog.
10. Let the user request national comparison results even when regional candidates exist.

## 5.3 Candidate presentation

For each result display:

- Statistic name and code.
- Result name and code.
- Original value/unit.
- Normalized cfs and m3/s.
- Recurrence/duration/exceedance information.
- Regression region and returned weight.
- Approval status and method.
- Equation, parameters, parameter limits, and in-range status.
- Standard error, intervals, and equivalent years when returned.
- Warnings, limitations, and citation.

Never guess recurrence metadata from an opaque code when the service does not describe it unambiguously.

## 5.4 Selection rules

A result is insertable only when:

- Its physical dimension is discharge.
- Its value is finite and positive.
- It is not explicitly excluded or disabled.
- Unit conversion is recognized.

National, extrapolated, incomplete, stale, or user-edited results:

- Remain insertable.
- Show prominent warnings.
- Preserve that status in project/report provenance.

Do not automatically select a "best" statistic.

## 5.5 Multiple regression regions

- Display each regional result separately.
- Do not generic-area-weight region results.
- Only show a combined result when:
  - USGS provides one, or
  - A cited state-specific combination rule is explicitly implemented and tested.

## 5.6 Reliability

- Run the service chain sequentially.
- Limit process-wide concurrent USGS requests.
- Configure separate connection and read timeouts.
- Retry only connection/read timeouts, rate limiting, and transient server errors.
- Honor Retry-After.
- Add bounded exponential backoff and jitter.
- Validate payload shape even for HTTP 200.
- Allow cancellation between stages.
- Cache complete immutable snapshots by point, region, and request/service version.

## 5.7 Failure handling

Provide actionable messages for:

- Unsupported point or region.
- Missing watershed in a successful response.
- Excessive snap distance.
- Delineation exclusion/warning.
- No regional scenarios.
- Missing basin characteristic.
- Characteristic outside regression limits.
- National results that are dimensions other than discharge.
- Partial metadata/citation failure.
- Timeout, rate limit, malformed response, cancellation, or outage.

## 6. NRCS Soils Integration

## 6.1 Spatial acquisition

1. Convert the HYPE domain to a WFS query extent.
2. Tile large extents.
3. Request MapunitPoly features.
4. Detect truncation using returned counts.
5. Deduplicate by mupolygonkey.
6. Repair geometries only where safe.
7. Reproject and clip polygons exactly to the domain.
8. Preserve distinct polygons even when they share a mukey.

## 6.2 Tabular acquisition

Batch digit-validated mukey values through SDA Tabular and retrieve normalized result sets for:

- Map units and symbols.
- Soil survey/version metadata.
- Components and representative percentages.
- Horizons and depth ranges.
- Representative Ksat.
- Texture groups.
- Restrictions and bedrock-related kinds.

Support both current suffix-based column names, such as ksat_r and hzdept_r, and future logical names through a schema adapter. Record which source columns were used.

## 6.3 Review interface

Add a separate NRCS soils tree node and map layer.

Selecting a polygon shows:

- Map-unit name and symbol.
- Survey area and version.
- Component names and percentages.
- Major-component flags.
- Horizon names and top/bottom depths.
- Textures.
- Ksat and converted units.
- Restrictions and interpreted bedrock depth.
- Missing values.
- Domain overlap.

Clearly distinguish original, converted, aggregated, overridden, and fallback values.

## 6.4 Overrides

Allow overrides of:

- Component percentage.
- Horizon top/bottom depth.
- Ksat.
- Texture selection where multiple representative textures exist.
- Restriction interpretation.

Every override records:

- Source key.
- Original value.
- Effective value.
- Reason/note.
- Timestamp.

## 6.5 Conductivity derivation

Use:

KV in m/day = representative Ksat in micrometers/second times 0.0864

KH = anisotropy ratio times KV

The anisotropy ratio:

- Prefills from current manual KH divided by KV.
- Is shown explicitly.
- Is editable.
- Is stored in provenance.

## 6.6 Aggregation policies

Require confirmation of one of these methods:

### Dominant method

- Select the highest-percentage major component.
- Use largest-overlap polygon for a cell crossing multiple polygons.
- Preserve its horizons by depth.

### Weighted method

- Weight components by representative component percentage.
- Weight polygons by cell overlap area.
- Use arithmetic aggregation for lateral KH.
- Use harmonic aggregation for vertical KV.

### User-selected component

- User selects the component to represent each map unit.
- Apply that component's horizon profile.
- Use largest-overlap or selected polygon handling as recorded.

When a single model layer spans multiple vertical horizons:

- KH uses thickness-weighted arithmetic aggregation.
- KV uses thickness-weighted harmonic aggregation.

## 6.7 Depth mapping

- Convert horizon depth below ground to each cell's local elevation interval.
- Intersect each model layer with horizon intervals.
- Never treat all horizons as independent horizontal polygons.
- Do not extend the deepest horizon silently.
- Below the known profile, use global manual KH/KV fallback and record the affected layer volume.

## 6.8 Precedence

Apply K values in this order:

1. Manual K-zone polygon.
2. Explicit NRCS override.
3. Derived NRCS profile.
4. Global manual KH/KV fallback.

## 6.9 Bedrock

- Treat only explicit lithic, paralithic, and densic restrictions as bedrock candidates.
- Show a warning where model depth exceeds interpreted bedrock depth.
- Do not change model geometry or assign a bedrock conductivity automatically in this revision.

## 6.10 Coverage reporting

Report percentages of:

- Domain area covered by NRCS polygons.
- Active model-layer volume assigned from direct representative values.
- Volume assigned through component/polygon aggregation.
- Volume assigned through overrides.
- Volume using global fallback.
- Missing or unresolved source coverage.

## 7. Groundwater-Gradient Revision

## 7.1 Structured controls

Replace raw profile strings with left/right control lists.

Every control has:

- Stable ID.
- Side.
- Normalized station from 0 to 1.
- Preferred gradient.
- Optional lower and upper gradients.
- Source/provenance.

Station 0 and 1 controls are mandatory and cannot be removed.

## 7.2 Map interaction

- Label upstream-left, downstream-left, upstream-right, and downstream-right.
- Allow users to add a point by selecting a side and clicking near its line.
- Project the point to the nearest station on that side.
- Support drag, table edit, and removal for interior points.
- Synchronize map and table changes.
- Display a color ramp for input gradient and a separate realized-head profile.

## 7.3 Units and sign

- Store dimensionless m/m.
- Display decimal and percent forms.
- State:
  - Positive = higher floodplain head, gaining tendency.
  - Negative = lower floodplain head, losing tendency.

## 7.4 Qualitative mapping

Choose a category per side.

Reference slope priority:

1. Robust slope from the modeled water-surface raster along the reach.
2. Average active-DEM bed-elevation drop along the reach.
3. User-entered reference slope.

The derived value records:

- Source raster.
- Sampling method.
- Upstream/downstream samples.
- Reach distance.
- Calculated slope.
- Policy version.

If the source produces a flat, adverse, invalid, or indeterminate slope, require manual numerical input rather than applying an artificial floor.

## 7.5 Head-anchor computation

For every gradient control:

1. Determine its model-coordinate location.
2. Find the nearest valid WSE edge and its WSE.
3. Calculate distance from the control to that WSE edge.
4. Calculate anchor head:

   head = WSE + gradient times distance

5. Interpolate anchor heads by arc-length station to boundary cells.
6. Interpolate upstream/downstream cap heads between the left/right endpoint heads.

Persist a diagnostics table containing:

- Side and station.
- Gradient.
- WSE.
- Distance.
- Anchor/interpolated head.
- Terrain elevation.
- Active/skipped layers.
- Warning flags.

## 7.6 Validation

Block:

- Missing endpoints.
- Duplicate stations.
- Stations outside 0 through 1.
- Nonfinite values.
- Zero-length side.
- Invalid WSE.
- Entire side without usable constant-head cells.

Warn:

- Near-zero WSE distance.
- Gradient sign changes.
- Sparse boundary-cell coverage.
- Head above terrain.
- Head below much of the active model.
- Controls very close together.

## 7.7 Legacy projects

- Preserve recorded legacy string profiles.
- Label the legacy interpolation method.
- Offer an explicit upgrade preview.
- Never silently reinterpret an old project.

## 8. Hyporheic Metrics

## 8.1 Default Results visibility

After successful delineation, default on:

- Hyporheic paths.
- Hyporheic entry/return points.
- Hyporheic footprint/volume.

Default off:

- Losing.
- Gaining.
- Throughflow.

## 8.2 Zone size

Report:

- Bulk saturated hyporheic-zone volume in m3.
- Mobile pore-water storage in m3.
- Binary presence footprint in m2.
- Fraction-weighted equivalent footprint in m2.
- Mean and maximum thickness.

Mobile pore storage is:

sum of hyporheic fraction times saturated cell volume times effective porosity

The binary footprint remains labeled as grid and particle-resolution dependent.

## 8.3 Flux-weighted exchange analysis

Add a distinct stream-interface particle pass:

1. Read CHD_RIVER package flows from the MODFLOW budget.
2. Identify downwelling stream cells.
3. Seed one or more forward particles at inflow faces.
4. Give each particle a source-flow weight equal to its share of the cell inflow.
5. Track to first terminal boundary.
6. Classify weighted flow as:
   - Returning to CHD_RIVER.
   - Leaving through a model side.
   - Unresolved/censored.
7. Verify weighted mass balance.

Do not use equal interior-particle counts as discharge fractions.

## 8.4 Connectivity

Use:

excursions per mile = (returning hyporheic flux / streamflow) times (1609.344 / reach length in meters)

Report:

- Streamflow.
- Total downwelling flow.
- Returning hyporheic flow.
- Losing flow.
- Unresolved flow.
- Turnover length.
- Excursions per mile.
- Mass-balance error.

The result is a continuous equivalent excursion count and can be noninteger or exceed one.

Connectivity is unavailable when:

- Streamflow is missing, nonpositive, or belongs to a different scenario.
- Reach length is invalid.
- Returning-flow classification or mass balance fails.

## 8.5 Residence-time distribution

Use returning, flux-weighted particles.

Persist one row per release particle with:

- Source cell.
- Flow weight.
- Endpoint class.
- Transit time.
- Termination status.

Report:

- Weighted mean.
- Weighted median.
- p05, p10, p25, p75, p90, and p95.
- Minimum and maximum.
- Proportion above one hour.
- Proportion between one hour and one day.
- Proportion above one day.
- Returning flux represented.
- Unresolved/censored fraction.
- Effective particle count.
- Maximum tracking time.
- Porosity.

Plot:

- Weighted empirical cumulative distribution.
- Log-time histogram.

Do not derive the full-site distribution from display-path samples.

## 9. Hyporheic Functional Capacity Index

## 9.1 Scientific-method artifact

Before freezing v1:

1. Perform a structured literature synthesis anchored on Harvey et al. (2019).
2. Add supporting residence-time and storage studies.
3. Record candidate thresholds, applicable settings, and contradictory evidence.
4. Define exchange, storage, and processing curve knots.
5. Test curves against model ensembles spanning stream size, slope, K, flow, and gradient.
6. Freeze the accepted profile as a versioned JSON artifact.

The application loads the scoring profile; equations and thresholds are not scattered through UI code.

## 9.2 Exchange capacity

Raw driver:

- Excursions per mile.

Scoring:

- Use a monotonic continuous curve from 0 through 15.
- Store knots and citations in the scoring profile.
- Flag extrapolation beyond the literature-supported range.

## 9.3 Storage capacity

Raw driver:

- Mobile pore-water storage divided by reference plan area.

Reference area:

1. HEC-RAS modeled inundation area when available.
2. Otherwise bankfull width times modeled reach length.

This produces an equivalent storage depth. Also report raw volume and denominator area.

The user can select the denominator when both are available. The scoring profile records which denominator methods it supports.

## 9.4 Processing capacity

Raw driver:

- Flux-weighted expected value of a continuous residence-time opportunity curve.

Initial candidate:

- Zero opportunity at or below one hour.
- Smooth increase in log time between one hour and one day.
- Plateau at or above one day.

The final v1 knots remain versioned and literature derived. The report must not claim a universal nutrient-removal threshold.

## 9.5 Score calculation

For each component:

1. Evaluate the continuous raw-to-score curve.
2. Clamp to 0 through 15.
3. Round half-up to a whole number.
4. Apply Low, Moderate, or High class.

HFCI:

- Normalize each component by dividing by 15.
- Take the equal arithmetic mean.
- Display two decimals.

If any component is unavailable:

- Show the available component scores.
- Set HFCI to Not computable.
- Explain the missing requirement.

## 9.6 HFCI categories and colors

Capacity colors use versioned theme tokens:

- Low: red, default #d73027.
- Moderate: yellow, default #fdbf11.
- High: blue, default #2c7bb6.

Overall HFCI bands use the normalized equivalents:

- Low: 0 through 5/15.
- Moderate: above 5/15 through 10/15.
- High: above 10/15 through 1.

## 9.7 Claims and validation

Use the label:

**Literature-derived HFCI v1 - validation ongoing**

Permitted description:

- Hydraulic functional capacity.
- Hydraulic functional potential.
- Potential to support broader hyporheic functions.

Prohibited description:

- Direct measurement of water-quality improvement.
- Direct measure of nutrient removal.
- Direct measure of thermal buffering.
- Direct measure of pollutant attenuation.
- Direct measure of habitat quality or ecosystem health.

Future validation should use:

- Reference and restored sites.
- Field exchange/RTD constraints.
- Model calibration.
- Grid/particle convergence.
- Seasonal repeats.
- Independent scientific review.

New evidence creates a new scoring-profile version. Existing reports do not change automatically.

## 10. Gradient Sensitivity

## 10.1 Scenario inputs

Every control supports:

- Lower.
- Preferred.
- Upper.

Also support:

- Absolute plus/minus variation.
- Percentage variation when preferred is nonzero.
- Explicit discrete values.

## 10.2 Scenario generators

### Default linked design

- Lower values at all controls.
- Preferred values at all controls.
- Upper values at all controls.

### One-at-a-time

- Preferred baseline.
- One lower and one upper scenario per control.

### Left/right crossed

- Low, preferred, and high left profile.
- Low, preferred, and high right profile.
- Nine combinations.

### Custom

- User-defined scenario set.

Deduplicate by canonical scenario hash.

## 10.3 Execution

- Run sequentially.
- Run preferred first.
- Stop if preferred fails.
- Continue after alternative failure.
- Preserve completed scenario status.
- Allow cancellation and resume.
- Reuse immutable preprocessing where safe.
- Never run multiple memory-intensive MODFLOW/HZ scenarios concurrently by default.
- Default maximum is 25 scenarios, configurable by deployment.
- Block scenario sets whose estimated runtime exceeds the deployment budget unless reduced.

## 10.4 Artifact retention

Preferred scenario retains:

- Full MODFLOW/MODPATH workspaces.
- Head and WSE rasters.
- Display paths.
- 2D/3D zone artifacts.
- Full report figures.

Alternative scenarios retain:

- Frozen inputs.
- Realized boundary heads.
- Solver status.
- Complete metric/HFCI outputs.
- Compact endpoint/classification artifacts needed to reproduce metrics.
- Errors and warnings.

## 10.5 Aggregation

For every metric report:

- Preferred value.
- Minimum and producing scenario.
- Maximum and producing scenario.
- Absolute range.
- Percent change from preferred where defined.
- Successful and failed scenario count.

Include:

- Excursions per mile.
- RTD statistics.
- Bulk volume.
- Mobile pore storage.
- 2D area.
- Exchange, storage, and processing scores.
- HFCI.

The dominant capacity contributor is the component with the largest absolute 0-15 score range across successful scenarios.

## 10.6 Visualizations

Provide:

- Preferred point and scenario range.
- Individual scenario points.
- Residence-time ECDF envelope.
- Scenario comparison table.
- One-dimensional response curve for linked designs.
- Left/right heatmap for crossed designs.

Always call the result sensitivity to tested gradient assumptions, not a confidence interval.

List untested uncertainty:

- K and soil configuration.
- Streamflow.
- Geometry.
- Grid resolution.
- Porosity.
- Thermal, chemical, and biological conditions.

## 11. Site Summary Report

## 11.1 Site metadata

Add:

- Site name.
- Analyst/organization.
- Optional notes.
- Assessment date.
- Primary downstream-outlet coordinate.
- Upstream/downstream endpoints.
- Reach length.

## 11.2 Canonical report model

Generate assessment_results.json immediately after a completed analysis.

It is immutable and contains:

- Frozen input snapshot.
- Preferred and sensitivity scenarios.
- Metrics.
- HFCI profile/version.
- Provenance.
- Warnings.
- Artifacts and citations.

The report modal and every exported format read only this model.

## 11.3 Report sections

1. Site identity and location.
2. Executive metrics and HFCI.
3. Reach and model overview figure.
4. Connectivity and flow accounting.
5. Residence-time distribution.
6. Bulk volume, pore storage, and footprint.
7. Functional-capacity scores and equations.
8. Flow, soil, gradient, grid, and model inputs.
9. Data sources, retrieval dates, and overrides.
10. Sensitivity ranges and scenario status.
11. Warnings, limitations, and untested uncertainty.
12. Software/model versions.
13. Scientific references.

## 11.4 Outputs

Produce:

- In-app modal.
- Self-contained printable HTML.
- Native PDF using ReportLab.
- site_metrics.csv.
- hyporheic_transit_times.csv.
- assessment_results.json.

Use the same numeric formatting and source fields in all formats.

## 11.5 PDF/HTML requirements

- Consistent typography, colors, spacing, and page hierarchy.
- Static maps/plots in PDF.
- Print CSS and embedded figures in HTML.
- Page numbers and report identifier.
- No clipped tables or figures.
- Long warnings and citations wrap cleanly.
- Report generation can be retried without rerunning the model.

## 12. Implementation Sequence

### Phase 0 - Baseline and tests

- Preserve current dirty-tree work.
- Add pytest structure and deterministic fixtures.
- Add import/compile checks.
- Document current output contracts.

### Phase 1 - Contracts, provenance, and persistence

- Add versioned models.
- Freeze run inputs.
- Add hashes/staleness.
- Complete save/open.
- Bump archive format with v1 compatibility.

### Phase 2 - Service clients

- Implement common retry/cancellation/cache behavior.
- Implement StreamStats/NSS client and fixtures.
- Implement NRCS spatial/tabular client and fixtures.

### Phase 3 - Input review UI

- Add USGS flow modal and insertion provenance.
- Add soils layer, selection, profiles, and overrides.
- Do not alter model K until derivation review is complete.

### Phase 4 - K and gradient engine revisions

- Implement depth-aware conductivity.
- Add aggregation review and coverage diagnostics.
- Replace gradient strings with structured controls.
- Add head-anchor preview/diagnostics.

### Phase 5 - Metrics and HFCI

- Add flux-weighted interface tracking.
- Add connectivity and weighted RTD.
- Add pore storage and equivalent area.
- Freeze and implement scoring profile v1.
- Correct Results defaults.

### Phase 6 - Sensitivity

- Add scenario generators.
- Add isolated sequential execution.
- Add compact alternative outputs.
- Add aggregation and resume/cancel.

### Phase 7 - Reporting

- Add canonical result model.
- Add modal, HTML, PDF, CSV, and JSON.
- Extend project archive.
- Perform layout and visual QA.

## 13. Test Plan

## 13.1 Unit tests

- Contract validation and schema migration.
- Stable hashing.
- cfs/m3/s conversion.
- Ksat conversion.
- Weighted arithmetic/harmonic aggregation.
- Local-depth horizon intersection.
- Gradient control ordering and head interpolation.
- Qualitative multiplier mapping.
- Scenario generation/deduplication.
- Weighted quantiles and ECDF.
- Connectivity equation and units.
- Pore-storage calculation.
- HFCI curve bounds, rounding, classes, and arithmetic.
- Missing-versus-zero behavior.

## 13.2 Service contract tests

Using recorded fixtures:

- Complete regional USGS workflow.
- National fallback.
- Multiple regression regions.
- Missing basin characteristics.
- Extrapolated results.
- Non-discharge result filtering.
- Retry, timeout, rate limit, and cancellation.
- WFS tiling/truncation.
- Polygon deduplication and clipping.
- Current and future SDA column schemas.
- Missing component/horizon/texture/restriction cases.

## 13.3 Numerical integration tests

Use small deterministic model fixtures:

- Known gaining, losing, and returning paths.
- Stream-interface flux mass balance.
- Weighted RTD hand checks.
- Grid and particle refinement.
- Depth-varying KH/KV.
- Curved boundary head anchors.
- Three isolated sensitivity scenarios.
- Alternative failure continuation.
- Preferred failure stop.

## 13.4 UI tests

- Flow modal open, move, fetch, select, insert, edit, and stale state.
- Soil layer selection and override.
- Aggregation confirmation.
- Gradient add/drag/edit/remove.
- Map/table synchronization.
- Qualitative-to-numeric preview.
- Validation and warning display.
- Results default visibility.
- Sensitivity cancellation/resume.
- Report modal and downloads.

## 13.5 Persistence tests

- Version-2 archive round trip.
- Version-1 project migration.
- Offline service snapshot restore.
- Override restore.
- Structured gradients.
- Sensitivity manifest.
- Scoring-profile retention.
- Report artifact restore.

## 13.6 Report tests

- HTML and PDF values match canonical JSON.
- CSV values match JSON.
- User-entered text is escaped.
- HTML is self-contained and printable.
- PDF pages render with Poppler.
- Inspect every PDF page for clipping, overlap, unreadable text, broken tables, and malformed glyphs.

## 13.7 Deployment tests

- Application import and startup.
- Existing smoke application.
- Small complete HYPE assessment.
- Service timeout behavior.
- Scenario runtime guard.
- PDF generation in the pip-only Connect Cloud environment.

## 14. Acceptance Criteria

The revision is complete only when:

1. A regional USGS fixture can return multiple discharge statistics and insert one with complete provenance.
2. National fallback and manual national comparison both work.
3. Manual flow remains usable without USGS.
4. A synthetic NRCS fixture produces expected depth-varying KH/KV arrays.
5. Missing/below-profile K coverage is visibly reported and uses the documented fallback.
6. Manual K zones override NRCS assignments.
7. Structured gradient points produce exact expected anchor and interpolated heads.
8. Qualitative left/right choices produce the locked multipliers and numeric preview.
9. Fresh HZ results show only hyporheic paths and volume by default.
10. Flux-weighted particles pass mass-balance checks.
11. Connectivity and weighted RTD match hand-calculated fixtures.
12. Bulk volume, pore storage, and both area metrics are reported with correct labels.
13. Exchange, storage, and processing scores are whole 0-15 values with correct classes.
14. HFCI matches the locked arithmetic formula.
15. A three-scenario sensitivity run produces isolated metrics, ranges, and HFCI changes.
16. Failed alternative scenarios do not destroy successful results.
17. HTML, PDF, CSV, and JSON agree numerically.
18. A saved project reopens with all sources, overrides, scenarios, scoring versions, and reports intact.
19. Existing manual workflows and version-1 projects remain usable.
20. Documentation clearly labels HFCI v1 as literature derived with validation ongoing.

## 15. Risks and Mitigations

### External-service change

**Risk:** USGS or NRCS schemas/endpoints change.  
**Mitigation:** Versioned adapters, recorded fixtures, payload validation, raw-response preservation, and optional live smoke tests.

### Scientific overclaim

**Risk:** HFCI is interpreted as measured ecosystem health.  
**Mitigation:** Locked terminology, underlying metrics, visible equations/citations, validation label, and prohibited-claim review.

### Sensitivity runtime

**Risk:** Multi-scenario HZ runs exceed Connect Cloud limits.  
**Mitigation:** Sequential execution, metrics-only alternatives, runtime forecasting, scenario caps, cancellation, and resume.

### Soil false precision

**Risk:** SSURGO component/horizon data appear site exact.  
**Mitigation:** Explicit aggregation choice, provenance/coverage, fallback reporting, overrides, and site-investigation limitation.

### Grid/particle dependence

**Risk:** Volume, area, and RTD change with discretization.  
**Mitigation:** Convergence tests, metadata, warnings, and validation datasets.

### Project incompatibility

**Risk:** New schemas break existing archives.  
**Mitigation:** Format-version bump, legacy adapters, immutable historical scoring profiles, and round-trip tests.

### Existing uncommitted work

**Risk:** Revision work overwrites unrelated development.  
**Mitigation:** Work from the current tree, use focused patches, review diffs continuously, and never reset unrelated files.

## 16. Explicit Non-Goals

This revision will not:

- Replace field investigation with SSURGO.
- Automatically calibrate groundwater gradients from DEM resolution alone.
- Apply generic cross-region USGS regression weighting.
- Automatically alter model geometry at bedrock.
- Run separate screening and engineering workflows.
- Create independent temperature, pollutant, nutrient, or habitat scores.
- Claim direct ecosystem health or measured biogeochemical performance.
- Treat sensitivity ranges as statistical confidence intervals.
- Recompute historical HFCI scores silently when a scoring profile changes.

## 17. Primary References

- USGS SS-Delineate API: https://streamstats.usgs.gov/ss-delineate/docs
- USGS SS-Hydro API: https://streamstats.usgs.gov/ss-hydro/docs
- USGS National Streamflow Statistics services: https://streamstats.usgs.gov/docs/nssservices/#/
- USGS StreamStats flow-statistics workflow: https://s3.us-east-1.amazonaws.com/streamstats.usgs.gov/StreamStatsFlowStatisticsWorkflow.pdf
- NRCS Soil Data Access help: https://sdmdataaccess.nrcs.usda.gov/WebServiceHelp.aspx
- NRCS Spatial2 services: https://sdmdataaccess.sc.egov.usda.gov/Spatial2/
- NRCS SDA query guide: https://sdmdataaccess.nrcs.usda.gov/documents/SoilDataAccessQueryGuide.pdf
- Harvey et al. (2019), How hydrologic connectivity regulates water quality in river corridors: https://pubs.usgs.gov/publication/70205454
- Poole et al. (2022), Hyporheic hydraulic geometry: https://doi.org/10.1371/journal.pone.0262080

