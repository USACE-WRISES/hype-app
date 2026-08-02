# HYPE Hyporheic Hydraulic Signature and Functional Screening Framework

## Detailed App Revision Specification

**Document purpose:** Provide a self-contained scientific, analytical, interface, and reporting framework for revising the HYPE application.

**Primary audience:** Developers or AI coding assistants revising HYPE, technical reviewers, and future users interpreting HYPE results.

**Status:** Recommended framework for implementation and refinement.

**Date:** July 31, 2026

---

## 1. Executive Summary

HYPE models surface-water and groundwater hydraulics to characterize hyporheic exchange in the shallow subsurface of rivers. Its primary scientific contribution is the direct modeling of where water travels through the hyporheic zone, how much water is exchanged, how often exchange occurs, and how long water remains in the subsurface.

The app should organize its core hydraulic results around three complementary metrics:

1. **Frequency:** How often water is delivered to and travels through the hyporheic zone.
2. **Duration:** How long exchanged water remains in the hyporheic zone.
3. **Extent:** How much of the subsurface actively participates in hyporheic exchange.

Together, these three metrics define the **hyporheic hydraulic signature** of a modeled reach, site, scenario, season, or restoration alternative.

The three metrics should remain separate rather than being collapsed into a universal score. They describe different aspects of exchange, can move in opposing directions, and have different implications for different functions. For example, increasing hydraulic conductivity could increase exchange throughput while reducing residence time. Neither result is inherently good or bad.

HYPE may then use the hydraulic outputs as inputs to **literature-informed, process-based functional screening estimates**. These optional calculations can estimate plausible nutrient transformation, dissolved-contaminant attenuation, temperature-related opportunity, or other potential functions under explicit assumptions. Examples include:

- Estimating nitrate reduction through a first-order denitrification relationship applied to modeled residence times.
- Estimating dissolved-zinc attenuation using a published apparent first-order attenuation rate.
- Calculating flux-weighted concentrations, fractional attenuation, or estimated mass transformed.

These calculations form a distinct **functional interpretation layer**. They are useful for screening and comparing alternatives, but they are not calibrated predictions of site-specific ecological or biogeochemical performance unless supported by site-specific observations and calibration.

The app should therefore:

- Treat the hydraulic model and hydraulic signature as the required core workflow.
- Present frequency, duration, and extent as separate, complementary outputs.
- Offer functional screening as a visible but optional final step.
- Clearly identify modeled, user-entered, literature-derived, and assumed inputs.
- Present low, central, and high parameter scenarios where the literature supports them.
- Avoid universal labels such as “good” and “bad.”
- Compare sites using their complete hydraulic signatures and function-specific screening results.
- Avoid a composite hydraulic index unless a future, validated decision need justifies one.

---

## 2. Purpose of the HYPE Revision

The revised HYPE application should make it easier for users to:

- Understand the primary hydraulic behavior of a hyporheic zone.
- Interpret tradeoffs among exchange frequency, residence time, and spatial extent.
- Compare hyporheic exchange among sites or scenarios using common units and normalizations.
- Explore how hydraulic conditions may support selected functions.
- Understand the assumptions and uncertainty associated with literature-derived functional calculations.
- Export a clear summary that distinguishes direct model results from inferred outcomes.

The revision should not imply that hydraulics alone establish that a specific ecological or biogeochemical function is occurring. Instead, it should communicate that HYPE quantifies the **hydraulic opportunity for function**.

### 2.1 Functions that may be discussed

The broader functions of the hyporheic zone include:

- Stream-temperature regulation.
- Nutrient cycling and transformation.
- Pollutant or contaminant attenuation.
- Habitat creation or support.

HYPE may describe how its modeled hydraulics relate to these functions, but the strength of the relationship differs by function:

- Some functions can be approximated using simplified process equations and literature parameters.
- Some require additional environmental inputs, such as temperature, dissolved oxygen, carbon availability, pH, redox state, or sediment properties.
- Some, especially habitat functions, cannot be represented credibly by hydraulics alone.

---

## 3. Governing Scientific Framework

The revised application should separate the analysis into four conceptual layers.

### 3.1 Layer 1 — Hydraulic Process Model

This is the direct mechanistic modeling layer. HYPE simulates the movement of water between the river channel and the shallow subsurface.

Relevant outputs may include:

- Hyporheic exchange pathways.
- Pathway-specific flow or flux.
- Exchange into and through the hyporheic zone.
- Residence time along each modeled pathway.
- Exchange throughput or turnover.
- Spatial depth, width, volume, and footprint of the exchanging domain.
- The points or areas where water enters and leaves the subsurface.

This layer is the scientific foundation of HYPE.

### 3.2 Layer 2 — Hydraulic Characterization

The direct model outputs are summarized using frequency, duration, and extent. The combined three-metric description is the **hyporheic hydraulic signature**.

This layer should answer:

- How frequently is water delivered to the subsurface?
- How long does it remain there?
- How much of the subsurface participates?

### 3.3 Layer 3 — Functional Interpretation

The modeled hydraulic signature is translated into plausible functional outcomes using simplified equations and literature-derived parameters.

This layer may use:

- User-provided starting concentrations.
- Literature-derived reaction or attenuation rates.
- Low, central, and high rate scenarios.
- Temperature-dependent or geochemical relationships, where supported.
- Modeled pathway residence times.
- Modeled pathway flow or flux.

The resulting values are **literature-parameterized, process-based interpretations**, not direct measurements of function.

### 3.4 Layer 4 — Decision and Comparison

Users may compare:

- Different river reaches.
- Existing and proposed conditions.
- Restoration alternatives.
- Seasonal or flow scenarios.
- Sensitivity runs.
- Different hydraulic-conductivity or boundary-gradient assumptions.

The comparison should proceed in this order:

1. Compare the hydraulic signatures.
2. Identify the hydraulic tradeoffs.
3. Compare function-specific screening results under consistent assumptions.
4. Discuss additional nonhydraulic factors that could change actual function.

---

## 4. Core Concept: The Hyporheic Hydraulic Signature

### 4.1 Recommended definition

> **The hyporheic hydraulic signature is the combined characterization of exchange frequency, duration, and extent for a modeled river reach or scenario.**

The signature is not a single score. It is a compact, three-part description of the exchange regime.

### 4.2 Recommended concise explanation

> **Frequency controls delivery, duration controls contact time, and extent controls the size of the participating domain.**

### 4.3 Plain-language analogy

> **The river sends water into a subsurface playground. Frequency tells us how often water makes a trip into the playground. Duration tells us how long it stays there. Extent tells us how large the participating playground is.**

The analogy can be used in tooltips, onboarding text, presentations, or an expandable “What do these metrics mean?” panel.

### 4.4 Why the metrics should remain separate

Frequency, duration, and extent should not initially be combined into a single hydraulic functional capacity index because:

- Each metric describes a different physical property.
- The metrics do not share units.
- They do not naturally sum to a whole.
- They may respond differently to changes in hydraulic conductivity, gradient, sediment structure, channel geometry, or flow.
- A change that increases exchange frequency may shorten residence time.
- A site with long residence times may exchange very little water.
- A site with moderate exchange behavior across a very large subsurface volume may have substantial cumulative influence.
- Different functions require different combinations of delivery, contact time, and extent.
- A universal weighting scheme would be arbitrary unless justified by a specific decision and validated against observations.
- A single score could conceal the physical reason that two sites differ.

Use **hyporheic hydraulic signature** instead of **hydraulic functional capacity index** unless a future project develops and validates a specific composite index.

---

## 5. Metric 1 — Exchange Frequency

### 5.1 Conceptual definition

Exchange frequency describes how often stream water enters and completes an exchange trip through the hyporheic zone.

In functional terms, frequency represents:

- Delivery of stream water to the subsurface.
- Delivery of dissolved nutrients, contaminants, oxygen, heat, or other constituents.
- Replenishment of water available for subsurface interaction.
- The throughput component of hyporheic exchange.

### 5.2 Recommended formal phrasing

> **Exchange frequency describes the recurrence of hyporheic circulation, expressed as the number of water turnovers per unit channel length. It represents the rate at which surface water and associated constituents are delivered to the hyporheic zone.**

### 5.3 Proposed primary output

- **Turnovers per kilometer of channel**

This provides a spatially normalized connectivity or exchange-frequency measure that can be compared among reaches of different lengths.

Because turnovers per kilometer is normalized by distance rather than time, it is more precisely a **spatial exchange frequency**, **turnover intensity**, or **exchange turnover per unit length**. If the shorter term **frequency** remains in the interface, the units and tooltip should make clear that it is not a temporal event frequency such as turnovers per day. It should also be distinguished from exchange discharge or volumetric flow rate.

The method is conceptually related to connectivity or turnover approaches discussed by Harvey and coauthors. The complete source and the precise definition used by HYPE must be verified and documented in the app’s technical references.

### 5.4 Required definition of “turnover”

Before implementation is finalized, the app documentation must define exactly what constitutes one turnover. Potential meanings that must not be conflated include:

- An exchanged hyporheic volume equal to a defined reference stream-water volume.
- A cumulative exchanged volume equal to the modeled hyporheic-zone volume.
- A completed modeled subsurface pathway.
- Another exchange-volume or flow-based definition inherited from the existing HYPE calculation.

The app should:

- Display the governing equation.
- Identify the reference volume or denominator.
- State whether the quantity is instantaneous, event-based, steady-state, or time-normalized.
- State how reach length is incorporated.
- State whether incomplete or domain-exiting pathways are included.
- State whether flows are counted once or can contribute repeatedly.

Until this definition is finalized, the interface must not use “turnover” as if its meaning were self-evident.

### 5.5 Interpretation

High frequency means that water and associated constituents are delivered to the hyporheic zone repeatedly or at relatively high throughput.

However:

- High exchange frequency does not guarantee high transformation.
- Water may return too quickly for slow reactions.
- Actual processing also depends on environmental conditions and reaction kinetics.

Recommended interpretation:

> **Frequency represents delivery or replenishment, not reaction completeness.**

---

## 6. Metric 2 — Exchange Duration

### 6.1 Conceptual definition

Exchange duration describes how long a parcel of exchanged water remains in the hyporheic zone before:

- Returning to the stream.
- Discharging elsewhere.
- Leaving the modeled domain.
- Reaching another defined pathway endpoint.

### 6.2 Recommended formal phrasing

> **Exchange duration is characterized by the residence-time distribution of water moving through the hyporheic zone. It represents the contact time available for thermal, chemical, and biological interactions.**

### 6.3 Functional meaning

Residence time determines the time available for:

- Heat transfer.
- Microbial activity.
- Nutrient transformation.
- Sorption.
- Precipitation or dissolution.
- Redox changes.
- Interaction with sediment and porewater.
- Other biological or geochemical processes.

### 6.4 Required outputs

Residence time should be represented as a distribution, not only as one mean value. At minimum, consider reporting:

- Flux-weighted mean.
- Flux-weighted median.
- Interquartile range.
- 10th and 90th percentiles.
- Minimum and maximum, with caution regarding outliers.
- Total number or total flux of included pathways.
- Fractions of exchanged flow exceeding selected residence-time thresholds.

If pathway \(i\) has flow \(Q_i\) and residence time \(t_i\), its flow weight is:

\[
w_i = \frac{Q_i}{\sum_i Q_i}
\]

The flux-weighted mean residence time is:

\[
\bar{t}_{Q} = \sum_i w_i t_i
\]

Any reported weighted percentile should use the same pathway-flow weights.

### 6.5 Threshold exceedance

For a selected threshold \(t^*\), the fraction of exchanged flow with a residence time equal to or greater than that threshold can be calculated as:

\[
F_{t \geq t^*}
=
\frac{\sum_{i:t_i \geq t^*}Q_i}{\sum_i Q_i}
\]

This is useful when literature identifies a residence-time range that may support a specific process. However, the threshold should be treated as function-specific and literature-informed, not as a universal definition of a good hyporheic zone.

### 6.6 Interpretation

Long residence time means that an exchanged water parcel has more time for interaction. It does not necessarily imply a large total reach-scale benefit because:

- The exchanged flow may be small.
- The participating volume may be limited.
- Required biogeochemical conditions may be absent.

Recommended interpretation:

> **Duration represents contact opportunity, not proof of reaction or ecological response.**

---

## 7. Metric 3 — Exchange Extent

### 7.1 Conceptual definition

Extent describes how much of the riverbed, banks, floodplain, or shallow subsurface actively participates in modeled hyporheic circulation.

It helps distinguish among:

- Exchange confined to shallow bed sediments.
- Deeper vertical circulation.
- Lateral circulation through banks.
- Flow paths extending across a meander.
- Broad river-corridor exchange.

### 7.2 Recommended formal phrasing

> **Exchange extent describes the volume and spatial footprint of the subsurface actively participating in hyporheic circulation. It provides context for how broadly exchange processes are distributed through the river corridor.**

### 7.3 Required absolute outputs

Where supported by the model, report:

- Absolute hyporheic-zone volume.
- Maximum and representative exchange depth.
- Maximum and representative lateral extent.
- Exchanging area or footprint.
- Reach length associated with the calculation.

### 7.4 Required normalized output

Absolute volume is useful for understanding the total size of the modeled exchanging domain. A normalized measure is also needed for comparing channels of different widths or modeled reaches of different lengths.

Recommended primary normalization:

\[
T_{\mathrm{eq}}
=
\frac{V_{\mathrm{HZ}}}{L_{\mathrm{reach}}W_{\mathrm{channel}}}
\]

where:

- \(T_{\mathrm{eq}}\) = equivalent hyporheic thickness.
- \(V_{\mathrm{HZ}}\) = active modeled hyporheic volume.
- \(L_{\mathrm{reach}}\) = modeled reach length.
- \(W_{\mathrm{channel}}\) = representative channel width.

This is equivalent to dividing hyporheic volume by channel planform area. It produces an intuitive length-scale measure of participating subsurface volume.

The resulting equivalent thickness is a volume-normalization measure. It is not necessarily the physical depth of the hyporheic zone at any point and should not be labeled as actual hyporheic depth.

Other potentially useful normalizations include:

- Volume per unit channel length:

\[
\frac{V_{\mathrm{HZ}}}{L_{\mathrm{reach}}}
\]

- Hyporheic volume divided by a defined surface-water volume:

\[
\frac{V_{\mathrm{HZ}}}{V_{\mathrm{SW}}}
\]

- Exchanging volume divided by total modeled subsurface-corridor volume:

\[
\frac{V_{\mathrm{HZ}}}{V_{\mathrm{domain}}}
\]

### 7.5 Important normalization note

Dividing hyporheic volume by channel width alone does not fully normalize for site size unless all modeled reaches have the same length. The app should include reach length explicitly or require a standardized modeled reach length.

### 7.6 Interpretation

Extent represents the size of the participating domain. It can modify the implications of frequency and duration:

- A high-frequency, long-duration exchange regime confined to a small volume may still have limited reach-scale influence.
- Moderate processing distributed across a large volume may yield substantial cumulative effects.

Recommended interpretation:

> **Extent represents participating capacity or the spatial multiplier of the exchange regime.**

“Spatial multiplier” should be understood conceptually. The app should not literally multiply extent by frequency or duration unless a governing, validated calculation specifically requires that operation.

### 7.7 Model dependence and reproducibility

Modeled extent can be sensitive to:

- The criterion used to classify water or cells as part of the hyporheic zone.
- Particle or pathway seeding density and placement.
- Grid resolution.
- Model-domain depth and lateral boundaries.
- Minimum flux or residence-time thresholds.
- Treatment of groundwater gains, permanent losses, and domain-exiting paths.

The app should store and report these settings so that extent values can be reproduced and compared fairly.

---

## 8. Interpreting the Complete Hydraulic Signature

There is no universally good or bad hydraulic signature. Interpretation should be function-specific and should recognize tradeoffs.

The guiding question should be:

> **What type of hyporheic hydraulic opportunity does this reach provide, and which functions could that opportunity support?**

### 8.1 High frequency and short duration

**Recommended label:** High delivery with limited contact time.

Interpretation:

- Water and constituents are repeatedly delivered to the subsurface.
- Individual parcels have limited time for slow reactions.
- This condition may favor rapid processes or functions strongly influenced by throughput.
- Transformation per parcel may be relatively low for slow processes.
- Reach-scale transformation may still be meaningful if exchanged flow is large.

### 8.2 Low frequency and long duration

**Recommended label:** Limited delivery with strong per-parcel reaction opportunity.

Interpretation:

- Relatively little water enters or moves through the hyporheic zone.
- Exchanged parcels have substantial time to interact with sediment, microbes, and geochemical conditions.
- Percent transformation per parcel may be high.
- Total reach-scale benefit may remain limited because relatively little water is treated.

### 8.3 High frequency and long duration

**Recommended label:** High delivery with substantial contact time.

Interpretation:

- Water is delivered frequently.
- Exchanged water remains in the subsurface for a meaningful period.
- If the participating extent is also large, this condition may provide substantial hydraulic opportunity for several functions.
- It should not automatically be labeled “best” because actual function depends on nonhydraulic conditions.

### 8.4 Low frequency and short duration

**Recommended label:** Limited delivery and limited contact time.

Interpretation:

- Little water is exchanged.
- The exchanged water returns quickly.
- Hydraulic opportunity may be limited for processes that require sustained contact.
- Fast reactions or localized habitat conditions could still matter.

### 8.5 Extent as a modifier

For every frequency-duration combination, characterize extent separately:

- Localized.
- Moderate.
- Broad.

These categories should initially be based on project-specific or regional reference distributions, not arbitrary universal thresholds.

Example descriptions:

- Localized, high-delivery, short-contact exchange.
- Broad, low-delivery, long-contact exchange.
- Moderate-extent exchange with balanced delivery and contact.
- Broad, high-delivery exchange with substantial contact time.

### 8.6 Avoiding false rankings

The app should not state:

- “This site has good hyporheic hydraulics.”
- “This site has a poor hyporheic zone.”
- “Longer residence time is always better.”
- “Higher turnover is always better.”

Instead, it should state:

- What the hydraulic signature is.
- How that signature differs from a comparison condition.
- What functions the signature may plausibly support.
- What additional information is required to infer actual function.

---

## 9. Functional Screening Framework

### 9.1 Recommended name

Use the full term:

> **Literature-informed, process-based functional screening estimates**

Use the shorter interface label:

> **Functional Screening Estimates**

### 9.2 Recommended description

> **HYPE uses modeled exchange pathways and residence times, together with user-provided concentrations and literature-derived process rates, to estimate the potential magnitude of selected hyporheic functions. These calculations are intended for screening and comparison rather than site-specific performance prediction.**

The terms **potential functional outcome**, **functional opportunity**, and **screening estimate** are generally preferable to an unqualified use of **benefit**, particularly when attenuation could be temporary or the ecological consequence has not been measured.

### 9.3 Directly modeled versus inferred

The distinction should not be described simply as “process-based” versus “non-process-based,” because the functional calculations may use legitimate process equations.

The more precise distinction is:

- **Direct model outputs:** The hydraulic pathways, flows, residence times, and exchange extent simulated by HYPE.
- **Inferred functional outcomes:** Literature-parameterized calculations applied to those hydraulic outputs.

### 9.4 Role in the application

Functional screening should be:

- Part of the normal visible sequence of the app.
- Clearly separated from core hydraulic results.
- Optional to configure and run.
- Accessible through a final tab, expandable panel, or guided step.
- Excluded from any requirement to complete the hydraulic analysis.

Users should be able to obtain and export a complete hydraulic signature without entering:

- Constituent concentrations.
- Reaction rates.
- Literature assumptions.
- Biogeochemical data.

---

## 10. General First-Order Screening Calculation

### 10.1 Pathway concentration

For a first-order transformation or apparent attenuation relationship:

\[
C_{\mathrm{out},i}
=
C_{\mathrm{in},i}e^{-kt_i}
\]

where:

- \(C_{\mathrm{in},i}\) = starting concentration entering pathway \(i\).
- \(C_{\mathrm{out},i}\) = estimated concentration leaving pathway \(i\).
- \(k\) = first-order reaction or apparent attenuation rate constant.
- \(t_i\) = residence time along pathway \(i\).

The units of \(k\) and \(t_i\) must be compatible. If \(k\) is expressed per day, residence time must be converted to days before calculation.

### 10.2 Fractional transformation

The estimated fraction transformed or attenuated along pathway \(i\) is:

\[
f_i = 1-e^{-kt_i}
\]

The estimated percent transformation is:

\[
100\left(1-e^{-kt_i}\right)
\]

### 10.3 Flux-weighted outlet concentration

If pathway \(i\) carries flow \(Q_i\):

\[
C_{\mathrm{out},Q}
=
\frac{\sum_i Q_iC_{\mathrm{out},i}}{\sum_i Q_i}
\]

This provides the flow-weighted concentration of water returning from the modeled hyporheic pathways.

### 10.4 Estimated mass transformation rate

When starting concentration is constant among pathways:

\[
\dot{M}_{\mathrm{transformed}}
=
\sum_i Q_iC_{\mathrm{in}}\left(1-e^{-kt_i}\right)
\]

More generally:

\[
\dot{M}_{\mathrm{transformed}}
=
\sum_i Q_i
\left(
C_{\mathrm{in},i}-C_{\mathrm{out},i}
\right)
\]

This metric captures the tradeoff between:

- Exchange throughput.
- Residence time.
- Starting concentration.
- Reaction or attenuation rate.

It is often more useful for comparing reaches than percent reduction alone.

### 10.5 Recommended reported results

For each functional screening module, report:

- Flow-weighted starting concentration.
- Flow-weighted estimated outlet concentration.
- Flow-weighted percent transformation or attenuation.
- Estimated mass transformed per unit time.
- Estimated mass transformed per unit channel length.
- Optionally, estimated mass transformed per unit channel planform area.
- Low, central, and high results when multiple rate scenarios are available.
- Fraction of exchanged flow within or above relevant residence-time ranges.

### 10.6 Necessary cautions

The equation assumes that:

- The selected kinetic form is appropriate.
- The rate can be applied to the modeled residence-time range.
- Rate-limiting constituents remain available.
- Environmental conditions are sufficiently similar to those in the source study.
- Mixing and pathway behavior are represented adequately for screening.

The app must not imply that these assumptions are universally valid.

---

## 11. Nutrient-Cycling Example: Denitrification

### 11.1 Purpose

The denitrification module should estimate plausible nitrate transformation within modeled hyporheic pathways.

### 11.2 Required inputs

- Modeled pathway residence time.
- Modeled pathway flow.
- User-provided or default nitrate concentration.
- Literature-derived first-order denitrification rate.
- Rate units.
- Low, central, and high rate scenarios, where available.
- Citation and applicability notes for each rate.

### 11.3 Recommended outputs

- Percent nitrate reduction for each rate scenario.
- Flow-weighted returning nitrate concentration.
- Estimated nitrate mass transformed per unit time.
- Estimated mass transformed per unit reach length.
- Residence-time distribution and threshold-exceedance fractions.

### 11.4 Interpretation

Residence time is a key hydraulic control on the opportunity for denitrification, but actual rates may also depend on:

- Dissolved oxygen and redox conditions.
- Organic-carbon availability.
- Nitrate availability.
- Sediment texture and microbial community.
- Temperature.
- pH.
- Competing electron acceptors.

Recommended language:

> **The denitrification result represents a plausible screening estimate under the selected kinetic and concentration assumptions. It does not confirm that site conditions support the assumed rate.**

### 11.5 Threshold-based context

If published literature supports meaningful residence-time ranges, HYPE may report:

- Fraction of exchanged flow exceeding one hour.
- Fraction exceeding one day.
- Other process-relevant thresholds.

These should supplement, not replace, the continuous first-order calculation. A hard score such as “residence time greater than one hour equals 1” can obscure the continuous nature of both residence times and reaction kinetics.

---

## 12. Pollutant-Attenuation Example: Dissolved Zinc

### 12.1 Purpose

The dissolved-zinc module should estimate potential attenuation of zinc from the dissolved phase along modeled hyporheic pathways.

### 12.2 Preferred terminology

Use:

- Dissolved-phase attenuation.
- Removal from the dissolved phase.
- Immobilization.
- Apparent first-order attenuation.

Avoid describing zinc itself as being destroyed or chemically “decaying.”

Zinc may:

- Sorb to sediments.
- Precipitate.
- Associate with organic matter.
- Enter another phase.
- Be remobilized if conditions change.

### 12.3 Required inputs

- Modeled pathway residence time.
- Modeled pathway flow.
- User-provided or literature-example dissolved-zinc concentration.
- Published apparent first-order attenuation rate.
- Rate units.
- Citation.
- Source-study environmental conditions.
- Low, central, and high rates, if defensible.

### 12.4 Recommended outputs

- Estimated dissolved-zinc concentration returning from the hyporheic zone.
- Flow-weighted percent attenuation.
- Estimated dissolved-zinc mass removed from the dissolved phase per unit time.
- Length- or area-normalized attenuation.
- Sensitivity to rate and starting concentration.

### 12.5 Interpretation

Actual zinc attenuation may depend on:

- Sediment mineralogy.
- pH.
- Redox conditions.
- Dissolved organic matter.
- Competing ions.
- Sorption capacity.
- Precipitation conditions.
- The potential for later remobilization.

Recommended language:

> **The zinc result estimates potential removal from the dissolved phase under the selected literature-based attenuation assumptions. It does not represent destruction of zinc and may not represent permanent sequestration.**

---

## 13. Other Potential Functional Modules

### 13.1 Stream-temperature regulation

Temperature regulation may depend on:

- Exchanged flow.
- Residence time.
- Hyporheic extent.
- Sediment thermal properties.
- Surface-water temperature.
- Groundwater or porewater temperature.
- Daily and seasonal temperature cycles.
- Depth and timing of exchange.

A temperature module should not be implemented as a simple score unless supported by an appropriate heat-transfer relationship. If HYPE later models heat transport directly, those outputs should be distinguished from literature-based screening.

### 13.2 Habitat support

Habitat support may depend on:

- Hydraulic accessibility.
- Interstitial space.
- Sediment grain size.
- Fine-sediment content.
- Dissolved oxygen.
- Temperature.
- Water quality.
- Stability.
- Depth.
- Biological requirements of the target organism or life stage.

Hydraulics can indicate potential access, renewal, persistence, and spatial availability. Hydraulics alone cannot establish habitat quality.

### 13.3 Habitat suitability index curves

Habitat suitability curves should not replace the process-based screening calculations by default.

Potential problems with a generic suitability curve include:

- Assigning an index of 1 above a residence-time threshold may imply an unsupported optimum.
- A curve may ignore exchange flow and extent.
- The curve may obscure continuous reaction behavior.
- A curve may appear more certain than the literature supports.
- Different organisms and functions require different curves.

A suitability index may be appropriate later if:

- It is tied to a specific function, organism, or management objective.
- The curve is supported by empirical observations.
- The metric definitions and scaling are documented.
- The index is validated independently.
- The underlying frequency, duration, and extent values remain visible.

Recommended approach:

- Retain continuous process-based calculations for nutrient and contaminant modules.
- Use threshold exceedance as supplemental context.
- Treat empirically supported habitat-suitability indices as a separate future module.

---

## 14. Uncertainty and Assumption Management

### 14.1 Input provenance

Every input used in a functional screening calculation should be labeled as one of the following:

- **Modeled by HYPE**
- **Entered by the user**
- **Obtained from literature**
- **Default assumption**
- **Derived from another input**

This provenance should be visible in the interface and report.

### 14.2 Rate scenarios

Do not rely on one literature rate when the literature supports a plausible range.

Where possible, provide:

- Low rate.
- Central or representative rate.
- High rate.

The app should display:

- The selected value.
- Units.
- Source citation.
- Source-study environmental conditions.
- Any conversion applied.
- Whether the value is a measured rate, fitted rate, or inferred rate.

### 14.3 Sensitivity results

Show how functional estimates respond to:

- Reaction or attenuation rate.
- Starting concentration.
- Residence time.
- Exchanged flow.
- Inclusion or exclusion of long pathways.
- Treatment of domain-exiting pathways.

### 14.4 Recommended disclaimer

> **Results are sensitive to local biogeochemical and geochemical conditions that are not explicitly simulated. The reported values represent plausible screening ranges under the stated assumptions.**

### 14.5 Additional report language

> **The functional screening estimates should be interpreted as comparative and hypothesis-generating results. Site-specific performance prediction requires field observations, locally appropriate parameters, and, where feasible, model calibration or validation.**

---

## 15. Recommended HYPE User Workflow

### Step 1 — Configure and run the hydraulic model

The user defines the hydraulic, geometric, and subsurface conditions and runs the surface-water/groundwater exchange model.

The model calculates:

- Exchange pathways.
- Pathway flows.
- Residence times.
- Exchange throughput or turnover.
- Spatial extent.

### Step 2 — Review hydraulic model quality

Before interpreting results, the app should display:

- Run status.
- Convergence or solver warnings.
- Number of valid pathways.
- Number and fraction of incomplete or excluded pathways.
- Mass-balance or flow-balance diagnostics.
- Domain-boundary exits.
- Any assumptions that affect result interpretation.

### Step 3 — Review the hydraulic signature

Display frequency, duration, and extent separately, with:

- Values.
- Units.
- Definitions.
- Distributions.
- Normalized metrics.
- Maps or diagrams where available.

### Step 4 — Describe the exchange regime

Provide a neutral qualitative description, such as:

- High delivery with limited contact time.
- Limited delivery with strong per-parcel reaction opportunity.
- Broad but slow exchange.
- Localized rapid exchange.

The app should explain the basis for the description.

### Step 5 — Optionally run functional screening

Allow the user to select a functional module, enter or accept the required inputs, review citations and assumptions, and calculate a range of plausible outcomes.

### Step 6 — Compare sites or scenarios

Compare:

- Hydraulic signatures first.
- Functional estimates second.
- All function-specific estimates using consistent assumptions.

### Step 7 — Export the summary

Generate a report that clearly separates:

1. Direct hydraulic model results.
2. Hydraulic interpretation.
3. Functional screening estimates.
4. Assumptions, uncertainty, and limitations.

---

## 16. Recommended Interface Structure

The exact interface may be adapted to the current HYPE architecture, but the conceptual separation should be retained.

### 16.1 Core result areas

Recommended result sections:

1. **Model Quality and Run Summary**
2. **Hyporheic Hydraulic Signature**
3. **Pathways and Residence-Time Distribution**
4. **Spatial Extent**
5. **Functional Screening Estimates**
6. **Site or Scenario Comparison**
7. **Printable Summary Report**

### 16.2 Hydraulic signature card

Create a concise signature card containing:

- Exchange frequency.
- Flux-weighted residence-time summary.
- Absolute exchange extent.
- Normalized exchange extent.
- Neutral exchange-regime description.
- Links or expanders for equations and definitions.

The card should never reduce the signature to one overall score.

### 16.3 Functional screening panel

Each functional module should show:

- Function or constituent.
- Governing equation.
- Required inputs.
- Current input values.
- Provenance of each input.
- Low, central, and high scenarios.
- Source citation.
- Applicability notes.
- Results.
- Sensitivity display.
- Limitations and disclaimer.

### 16.4 Progressive disclosure

Use clear summary values first, with optional expanders for:

- Detailed equations.
- Pathway-level results.
- Literature details.
- Assumptions.
- Sensitivity settings.
- Technical limitations.

This keeps the standard workflow approachable without hiding the scientific basis.

---

## 17. Recommended Visualizations

### 17.1 Primary cross-site comparison plot

Use a bubble plot:

- **Horizontal axis:** Exchange frequency.
- **Vertical axis:** Representative residence time.
- **Bubble size:** Normalized or absolute hyporheic extent.
- **Bubble color or symbol:** Site, scenario type, season, or alternative.

This plot displays the frequency-duration tradeoff while retaining extent as the third dimension.

Because frequency and residence time may span orders of magnitude, consider:

- Logarithmic-axis options.
- Clear zero-value handling.
- Hover text with exact values and units.
- A legend explaining bubble-size scaling.

### 17.2 Residence-time distribution

Provide one or more of:

- Weighted cumulative distribution.
- Weighted histogram.
- Density plot.
- Box-and-whisker summary.
- Threshold-exceedance markers.

The chart should state whether it is weighted by:

- Pathway count.
- Pathway flow.
- Volume.

Flux weighting should be the preferred basis for functional calculations.

### 17.3 Aligned metric comparison

Use three aligned plots or bars for:

- Frequency.
- Duration.
- Extent.

This provides a transparent alternative to a composite score.

### 17.4 Spatial visualization

Where available, map:

- Flow paths.
- Entry and return points.
- Residence time by pathway.
- Flux by pathway.
- Active hyporheic extent.
- Depth or lateral reach of exchange.

### 17.5 Functional-result visualization

For each module, consider:

- Low-central-high range bars.
- Sensitivity curves.
- Contribution by residence-time class.
- Percent attenuation versus mass attenuation.
- Comparison among sites under the same assumed rate and concentration.

### 17.6 Do not use a ternary plot as the primary quantitative display

A ternary plot normally represents three quantities that sum to 100 percent. Frequency, duration, and extent are independent quantities and do not naturally sum to a whole.

A triangle may be used as a conceptual teaching graphic, but a frequency-versus-duration bubble plot is more mathematically appropriate for results.

---

## 18. Comparison Rules

### 18.1 Compare hydraulics before function

For every comparison:

1. Compare frequency.
2. Compare the residence-time distributions.
3. Compare absolute and normalized extent.
4. Identify tradeoffs.
5. Apply function-specific calculations only after the hydraulic differences are understood.

### 18.2 Normalize appropriately

For reaches of different sizes:

- Report turnover per unit channel length.
- Report volume per unit length or equivalent thickness.
- Retain absolute values alongside normalized values.
- Document channel width and reach length.

Normalization alone does not guarantee comparability. Cross-site comparisons should also use, or explicitly account for, comparable:

- Streamflow or hydraulic conditions.
- Reach definitions.
- Spatial resolution.
- Particle or pathway seeding.
- Domain boundaries.
- Pathway inclusion rules.
- Model version and solver settings.

### 18.3 Use consistent functional assumptions

When comparing sites or alternatives, use the same:

- Starting concentration, unless site-specific concentrations are intentionally being evaluated.
- Rate scenario.
- Units.
- Equation.
- Treatment of incomplete pathways.
- Normalization.

If assumptions differ, the app should flag that the functional results are not directly comparable.

### 18.4 Separate percent and mass outcomes

A site may have:

- High percent transformation per parcel but low mass transformation because exchanged flow is small.
- Lower percent transformation per parcel but high mass transformation because exchange throughput is large.

Both results should be presented.

### 18.5 Reference distributions

Avoid fixed universal low, medium, and high thresholds until sufficient data exist.

Potential future classifications may be based on:

- Regional reference sites.
- Project-specific alternatives.
- Ecoregion-specific distributions.
- Stream-type distributions.
- Percentiles from a sufficiently large HYPE result database.

The comparison population must always be stated.

---

## 19. Reporting Requirements

### 19.1 Required report organization

The printable or downloadable summary should include:

1. **Project and Scenario Information**
2. **Model Configuration**
3. **Model Quality and Diagnostics**
4. **Hyporheic Hydraulic Signature**
5. **Frequency Results**
6. **Residence-Time Results**
7. **Extent Results**
8. **Exchange-Regime Interpretation**
9. **Functional Screening Estimates**
10. **Assumptions and Input Provenance**
11. **Sensitivity and Uncertainty**
12. **Limitations**
13. **Literature References**

### 19.2 Required hydraulic reporting

Report:

- Frequency value and units.
- Exact turnover definition.
- Residence-time statistics and weighting basis.
- Residence-time distribution figure.
- Absolute extent.
- Normalized extent.
- Reach length.
- Representative width.
- Number and total flow of included pathways.
- Treatment of incomplete or domain-exiting pathways.

### 19.3 Required functional reporting

For every calculation, report:

- Governing equation.
- Starting concentration.
- Selected rate.
- Rate range.
- Units.
- Literature source.
- Environmental applicability.
- Flow-weighted percent result.
- Mass result.
- Normalized mass result.
- Sensitivity range.
- Screening-level disclaimer.

### 19.4 Recommended core report wording

> **HYPE characterizes hyporheic exchange using three complementary hydraulic metrics: frequency, duration, and extent. Frequency describes how often water is delivered to the hyporheic zone, duration describes the time available for subsurface interaction, and extent describes the size of the participating domain. Together, these metrics define the hyporheic hydraulic signature of a river reach.**

> **The hydraulic signature is translated into potential functional outcomes through literature-informed, process-based screening calculations. These estimates illustrate how modeled exchange may support functions such as nutrient transformation or dissolved-contaminant attenuation, but they are not substitutes for site-specific biogeochemical measurements or calibration.**

> **Rather than assigning a universal ranking of good or bad, HYPE identifies the type of hydraulic opportunity present at each site and evaluates how that opportunity may support specific functions under stated assumptions.**

---

## 20. Terminology and Writing Guide

| Preferred term | Meaning or use |
| --- | --- |
| Hyporheic hydraulic signature | Combined description of frequency, duration, and extent |
| Exchange frequency | Recurrence or throughput of hyporheic circulation |
| Delivery | Plain-language functional meaning of frequency |
| Exchange duration | Residence-time behavior of exchanged water |
| Contact time | Plain-language functional meaning of duration |
| Exchange extent | Volume or footprint actively participating in exchange |
| Participating capacity | Plain-language functional meaning of extent |
| Hydraulic opportunity for function | What the modeled hydraulics can support, not proof of actual function |
| Functional screening estimate | Literature-parameterized interpretation of modeled hydraulics |
| Dissolved-phase attenuation | Preferred description for zinc leaving the dissolved phase |
| Flux-weighted | Weighted by modeled exchanged flow |
| Plausible screening range | Low-central-high result under stated assumptions |

Avoid or qualify:

| Avoid or qualify | Reason |
| --- | --- |
| Good or bad hyporheic zone | No universal optimum exists |
| Hydraulic functional capacity index | Implies a validated single score that is not yet available |
| Zinc decay | Zinc is not destroyed; it may move out of the dissolved phase |
| Predicted site benefit | Too strong without site-specific calibration |
| Proven denitrification | Hydraulics and literature rates do not prove the process occurred |
| Optimal residence time | Depends on the target function and environmental conditions |
| Habitat quality based only on hydraulics | Habitat depends on nonhydraulic factors |

---

## 21. Functional Module Metadata Requirements

Each functional module should have a structured definition that includes:

- Module name.
- Function category.
- Constituent or response variable.
- Scientific description.
- Governing equation.
- Equation assumptions.
- Required modeled inputs.
- Required user inputs.
- Optional inputs.
- Default values.
- Low, central, and high parameter values.
- Parameter units.
- Required unit conversions.
- Full literature citation.
- Source-study system type.
- Source-study temperature.
- Source-study chemistry or redox conditions, where known.
- Applicable concentration range.
- Applicable residence-time range.
- Output variables.
- Output units.
- Limitations.
- Standard warning text.
- Version or review date.

The app should not embed an undocumented numeric default.

---

## 22. Calculation and Data-Handling Requirements

### 22.1 Pathway inclusion

Document and consistently apply rules for:

- Completed return-flow pathways.
- Pathways leaving the model domain.
- Stagnant or zero-flow pathways.
- Extremely short pathways.
- Extremely long or potentially truncated pathways.
- Duplicate or merged pathways.
- Missing residence times.

The documentation should also define a **hyporheic exchange pathway**. In particular, state whether a pathway must begin in and return to the channel, and how the app classifies:

- Surface water that enters the subsurface and leaves the modeled domain.
- Groundwater that enters the channel without originating as modeled surface water.
- Permanent stream losses.
- Pathways that terminate at a boundary.
- Recirculating or repeatedly counted pathways.

### 22.2 Weighting

Clearly distinguish:

- Unweighted pathway statistics.
- Flux-weighted statistics.
- Volume-weighted statistics.

Use flux-weighted values for mass and returning-concentration calculations unless the method explicitly requires another basis.

### 22.3 Units

The app should:

- Convert all times to the rate constant’s time basis before calculation.
- Convert flow and concentration to compatible mass-per-time units.
- Display original and converted units where helpful.
- Prevent calculations when required unit metadata are missing.
- Avoid silent unit conversion.

### 22.4 Mass balance

For a nonnegative first-order attenuation calculation:

- \(C_{\mathrm{out},i}\) should not exceed \(C_{\mathrm{in},i}\).
- Estimated transformed mass should not be negative.
- Estimated transformed mass should not exceed incoming mass along the included pathways.
- A zero rate should produce zero transformation.
- A zero residence time should produce zero transformation.

### 22.5 Missing inputs

If a functional input is missing:

- Do not block hydraulic results.
- Identify the missing item.
- Explain what is needed.
- Allow the user to continue without the functional module.

---

## 23. Validation and Quality Assurance

### 23.1 Hydraulic-output checks

Confirm that:

- Frequency uses the documented turnover definition.
- Reach-length normalization is correct.
- Residence-time weights sum to one, within numerical tolerance.
- Extent uses a consistent active-domain definition.
- Equivalent thickness uses both reach length and channel width.
- Excluded pathways are counted and reported.

### 23.2 Functional-calculation checks

Test the following:

- \(k=0\) produces no transformation.
- \(t=0\) produces no transformation.
- Increasing \(k\) at constant \(t\) increases fractional transformation.
- Increasing \(t\) at constant \(k\) increases fractional transformation.
- A very large \(kt\) approaches complete transformation without exceeding it.
- Flux-weighted concentration matches a hand calculation.
- Total mass transformed equals incoming mass minus returning mass.
- Low-central-high results are monotonic when rates are ordered.
- Unit conversions give identical results for equivalent units.

### 23.3 Interface checks

Confirm that:

- Core hydraulic results do not require functional inputs.
- Modeled and inferred results are visually distinct.
- Every functional default has a visible source.
- Every result displays units.
- Every comparison states its normalization basis.
- Results using different assumptions are flagged.
- The term “zinc decay” is not used.
- The app does not assign a universal good/bad rating.
- The primary comparison graphic is not a ternary plot.

### 23.4 Validation against observations

Where field or published observations are available:

- Compare modeled residence-time distributions with tracer-based estimates.
- Compare exchange-flow or turnover estimates with independent calculations.
- Compare modeled extent with temperature, tracer, piezometer, or geophysical evidence.
- Test whether functional screening estimates reproduce plausible observed ranges.
- Document where agreement is poor and whether the cause is hydraulic, parameter, or biogeochemical.

Functional validation should be treated separately from hydraulic validation.

---

## 24. Recommended Implementation Phases

### Phase 1 — Clarify and strengthen the hydraulic core

- Finalize the turnover definition and equation.
- Report frequency, duration, and extent separately.
- Add absolute and normalized extent.
- Add flux-weighted residence-time statistics.
- Create the hydraulic signature card.
- Add neutral exchange-regime descriptions.
- Add model-quality diagnostics.

### Phase 2 — Formalize functional screening

- Create a consistent functional-module structure.
- Implement low-central-high parameter scenarios.
- Add input provenance labels.
- Report both percent and mass outcomes.
- Rename zinc “decay” as dissolved-phase attenuation.
- Add limitations and standard disclaimers.

### Phase 3 — Add comparison and visualization

- Add the frequency-duration bubble plot with extent represented by bubble size.
- Add aligned frequency, duration, and extent comparisons.
- Add residence-time distribution comparisons.
- Enforce consistent assumptions across functional comparisons.

### Phase 4 — Validate and refine

- Apply the workflow to multiple sites.
- Compare results with available observations.
- Develop regional or project-specific reference distributions.
- Refine exchange-regime descriptions.
- Evaluate whether users have a defensible need for a composite score.

---

## 25. Open Decisions That Must Be Resolved

1. **What exactly constitutes one turnover?**
2. **What equation currently produces turnovers per kilometer?**
3. **What is the authoritative Harvey et al. source for the selected connectivity or turnover method?**
4. **Which pathways are included in frequency and residence-time statistics?**
5. **Should the primary residence-time statistic be the flux-weighted median, mean, or both?**
6. **Which percentiles should always be displayed?**
7. **What defines the active hyporheic-zone boundary for the extent calculation?**
8. **Will equivalent thickness be the primary normalized extent metric?**
9. **How will representative channel width be defined for nonuniform reaches?**
10. **How will HYPE treat pathways that exit the modeled domain?**
11. **Which denitrification rates and source studies are sufficiently applicable for defaults?**
12. **Which zinc attenuation study and values will govern the current module?**
13. **Can low, central, and high parameter values be supported by the literature?**
14. **Which site observations are available for hydraulic and functional validation?**
15. **How should sensitivity results be stored and compared among scenarios?**

These decisions should be documented rather than left implicit in code.

---

## 26. Concrete Next Steps

- Finalize the mathematical definition and units of turnover frequency.
- Add and verify the full citation supporting the turnover-per-kilometer approach.
- Select the residence-time statistics that will always be reported.
- Use flux-weighted statistics for functional interpretation.
- Select one primary normalized extent measure.
- Use volume divided by reach planform area as the leading candidate.
- Report absolute extent alongside normalized extent.
- Adopt **hyporheic hydraulic signature** as the main comparative concept.
- Develop four to six neutral exchange-regime descriptions.
- Treat extent as a separate modifier of the frequency-duration regime.
- Avoid universal low, medium, and high thresholds until a defensible reference distribution exists.
- Build the frequency-versus-duration bubble plot.
- Retain functional screening as an optional but visible final stage.
- Document the governing equation, inputs, source, applicability, and limitations for each functional module.
- Replace single default process rates with low, central, and high scenarios where possible.
- For denitrification, calculate both percent nitrate reduction and estimated mass transformation.
- For zinc, calculate dissolved-phase attenuation and acknowledge possible reversibility.
- Clearly label modeled, user-provided, literature-derived, and assumed inputs.
- Add sensitivity results and uncertainty language to the interface and report.
- Test the calculations against hand-worked examples.
- Test selected functional estimates against observations where available.
- Consider a composite index only after the three-metric signature has been applied across multiple sites and a clear decision need has been established.

---

## 27. Implementation Guardrails for an AI Coding Assistant

When using this document to revise HYPE, the coding assistant should:

- Preserve the existing validated hydraulic calculations unless a change is explicitly required.
- Inspect the current implementation before changing metric definitions.
- Do not invent the turnover equation or literature citation.
- Do not silently change units, pathway filters, or weighting methods.
- Keep direct hydraulic results separate from inferred functional outcomes in code, interface, and reports.
- Make functional screening optional.
- Preserve pathway-level data needed to reproduce aggregate results.
- Centralize rate constants, citations, units, assumptions, and warnings in a structured configuration rather than scattering them through interface code.
- Ensure all summary results can be traced to their inputs and calculation method.
- Use low-central-high scenarios without implying statistical confidence intervals unless they truly are confidence intervals.
- Preserve raw metric values whenever qualitative labels are shown.
- Do not create an overall good/bad score.
- Do not use a ternary plot for independent metrics.
- Add tests for equations, weighting, unit conversion, mass balance, and edge cases.
- Include all assumptions and limitations in exported reports.
- Flag any conflict between this framework and the current implementation for human review.

---

## 28. Final Recommended Framing

The revised HYPE application should communicate the following scientific story:

> **HYPE first models the physical exchange of water between the stream and shallow subsurface. The results are summarized as a hyporheic hydraulic signature consisting of frequency, duration, and extent. Frequency describes delivery, duration describes contact time, and extent describes the size of the participating domain.**

> **The hydraulic signature does not by itself prove that nutrient transformation, pollutant attenuation, temperature regulation, or habitat benefits occur. It quantifies the hydraulic opportunity for those functions.**

> **Optional functional screening calculations then combine modeled pathways and residence times with user inputs and literature-derived process parameters. These calculations provide plausible, transparent, and comparable screening estimates under stated assumptions.**

> **Sites and alternatives should be compared using their complete hydraulic signatures and function-specific outcomes rather than a universal composite score.**

This structure preserves the scientific strength of HYPE’s hydraulic model while giving users a practical, defensible way to interpret potential hyporheic functions.
