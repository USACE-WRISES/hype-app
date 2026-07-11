# HYPE Brainstorming

## Streamflow Input and USGS Flow-Statistics Integration

- **Add a USGS flow lookup button to the flow input**
  - Place a button beside the existing flow input box, such as **Get USGS Flow**.
  - Open a popup modal when the button is selected.
  - Use the site location or model outlet location as the initial lookup point.
  - Allow the user to confirm or adjust the lookup location on a map.

- **Retrieve available StreamStats and National Streamflow Statistics results**
  - Delineate the contributing watershed for the selected point.
  - Retrieve the basin characteristics needed to calculate available flow statistics.
  - Retrieve state or regional StreamStats flow results when they are available for the selected location.
  - Use National Streamflow Statistics regression equations when the needed StreamStats result is not available.
  - Display available results in the modal, including:
    - Flow-statistic name.
    - Estimated flow.
    - Units.
    - Recurrence interval, duration, or exceedance probability where applicable.
    - Regression region or equation used.
    - Applicable warnings, limitations, and uncertainty information.
  - Let the user select the appropriate flow result and insert it into the HYPE flow input.
  - Preserve manual flow entry and allow the imported value to be edited.
  - Save the selected value, source, retrieval date, location, and equation information with the model configuration.

- **Use the current USGS service workflow**
  - Build the integration around the current **SS-Delineate**, **SS-Hydro**, and **National Streamflow Statistics** services.
  - Use the StreamStats delineation API documentation:
    - https://streamstats.usgs.gov/ss-delineate/docs
  - Use the National Streamflow Statistics service documentation:
    - https://streamstats.usgs.gov/docs/nssservices/#/
  - Use the USGS Python workflow notebook as an implementation reference:
    - https://s3.us-east-1.amazonaws.com/streamstats.usgs.gov/StreamStatsFlowStatisticsWorkflow.ipynb
  - Add response validation, retries, timeouts, and a clear error message when a result cannot be obtained.

## NRCS Soils-Data Integration

- **Pull NRCS Soil Data Access information for the model domain**
  - Convert the HYPE model domain into an area of interest.
  - Query every NRCS soil map-unit polygon that intersects the model domain.
  - Clip or associate the returned polygons with the model boundary.
  - Preserve the map-unit key so the spatial polygons can be joined to their tabular soil properties.
  - Use the NRCS Soil Data Access services:
    - https://sdmdataaccess.nrcs.usda.gov/WebServiceHelp.aspx

- **Retrieve soil properties for each intersecting polygon**
  - Pull the following information where available:
    - Soil map-unit name and symbol.
    - Soil component name and component percentage.
    - Soil texture or representative texture class.
    - Top and bottom depth of each soil horizon.
    - Depth to bedrock.
    - Saturated hydraulic conductivity.
  - Use the NRCS `ksat` value for saturated hydraulic conductivity.
  - Retain the vertical soil-horizon information instead of treating each horizon as a separate horizontal polygon.
  - Recognize that each map-unit polygon may contain multiple components and multiple vertical horizons.

- **Display and review the imported soils**
  - Add the soil polygons as a map layer in HYPE.
  - Allow the user to select a polygon and review:
    - Soil type.
    - Component percentages.
    - Horizon depths.
    - Texture.
    - Saturated hydraulic conductivity.
    - Depth to bedrock.
  - Clearly identify polygons or horizons with missing values.
  - Allow the user to override an imported value when better site-specific information is available.

- **Translate NRCS soils into model hydraulic conductivity**
  - Convert NRCS hydraulic-conductivity units into the units used by the HYPE groundwater model.
  - Intersect the soil polygons with the model grid.
  - Assign spatially varying hydraulic conductivity to model cells based on the intersecting soil polygons.
  - Define how to handle cells that intersect more than one polygon, such as:
    - Use the polygon covering the largest portion of the cell.
    - Calculate an area-weighted value.
    - Refine the grid or preserve subcell zones where practical.
  - Define how to handle multiple soil components within one map unit, such as:
    - Dominant component.
    - Component-percentage-weighted conductivity.
    - User-selected component.
  - Define how to handle multiple vertical horizons:
    - Assign horizon-specific conductivity by model layer where the model supports it.
    - Derive a representative conductivity for a 2D or vertically uniform model.
    - Allow the user to choose the aggregation method.
  - Record the original NRCS values, conversion method, aggregation rule, and final assigned model values.

## Floodplain Groundwater-Gradient Inputs

- **Add qualitative gradient options**
  - Allow the user to select:
    - Strongly gaining.
    - Slightly gaining.
    - Neutral.
    - Slightly losing.
    - Strongly losing.
  - Translate each selection into a numerical gradient.
  - Base the assigned numerical values on stream slope and available DEM information.
  - Document how each qualitative category is converted into an actual gradient.
  - Allow the user to review the resulting numerical value before running the model.

- **Add quantitative gradient inputs**
  - Allow the user to specify groundwater gradients at:
    - Upstream left.
    - Downstream left.
    - Upstream right.
    - Downstream right.
  - Update the map interface so these four locations are clearly identified.
  - Allow additional gradient control points to be added along the left and right boundary-condition lines.
  - Linearly interpolate the gradient between adjacent control points along each boundary.
  - Display the interpolated gradient along the boundary so the user can review it.
  - Validate that the entered values and point order produce a usable boundary condition.

## Results and Reporting

- **Update the default results display**
  - Show the following by default after an assessment:
    - Hyporheic zone volume.
    - Hyporheic flow paths.

- **Add a site Summary Report**
  - Add a popup modal containing the key HYPE results.
  - Make the report printable and exportable for the individual site.
  - Include the site name, location, model inputs, assessment date, and data sources.
  - Include figures, values, units, and concise explanations of each metric.

- **Report the key hyporheic metrics**
  - **Hyporheic connectivity**
    - Calculate the connectivity metric based on the identified research paper.
    - Represent how frequently water cycles through the hyporheic zone.
    - Report the number of hyporheic “adventures” or exchanges per mile of stream.
    - Document the formula and assumptions used.
  - **Residence-time distribution**
    - Plot the complete residence-time distribution.
    - Report the average residence time.
    - Consider including the median, percentiles, range, and other useful summary statistics.
  - **Hyporheic zone volume**
    - Report the calculated hyporheic volume with units.
  - **Hyporheic zone 2D area**
    - Report the plan-view area of the hyporheic zone with units.

## Hyporheic Functional Capacity Index

* **Develop and report a Hyporheic Functional Capacity Index**

  * Calculate a **Hyporheic Functional Capacity Index (HFCI)** representing the overall hydraulic functional capacity and health of the hyporheic zone.
  * Base the HFCI on three functional capacities derived from metrics directly computed by the HYPE model:

    * **Exchange capacity**

      * Calculate from the hyporheic-connectivity metric.
      * Represent how frequently stream water enters, moves through, and returns from the hyporheic zone.
    * **Storage capacity**

      * Calculate from hyporheic-zone volume and potentially hyporheic-zone 2D area.
      * Represent the amount and spatial extent of hyporheic storage available within the modeled reach.
    * **Processing capacity**

      * Calculate from the residence-time distribution and associated residence-time statistics.
      * Represent the opportunity for physical, chemical, and biological processes to occur while water is within the hyporheic zone.
  * Report the three functional-capacity scores individually in addition to the combined HFCI.
  * Preserve the underlying modeled metrics so users can see how each capacity score and the final index were calculated.
  * Determine how the three capacity scores should be combined, including:

    * Whether they should receive equal or different weights.
    * Whether a very low score for one capacity should limit the overall HFCI.
    * Whether the HFCI should be calculated using an arithmetic mean, geometric mean, minimum-capacity rule, or another aggregation method.
  * Describe the HFCI as an index of **functional capacity or functional potential**, rather than a direct measurement of specific chemical, biological, or thermal functions.

* **Develop scoring criteria for the three functional capacities**

  * Create defensible scoring curves that translate the directly modeled metrics into standardized functional-capacity scores.
  * Prefer continuous scoring curves over simple pass/fail thresholds.
  * Document the scientific basis, equations, assumptions, and uncertainty associated with each scoring curve.
  * Evaluate whether scoring criteria need to vary by:

    * Stream type.
    * Stream size or drainage area.
    * Channel slope.
    * Ecoregion.
    * Geomorphic setting.
    * Hydrologic condition.

* **Develop processing-capacity scoring from residence time**

  * Begin with the working assumption that residence times greater than approximately **one hour** provide meaningful opportunity for nutrient-cycling processes.
  * Treat approximately **one day** as an initial estimate of where nutrient-cycling benefits may approach a maximum or plateau.
  * Develop a smooth scoring curve between these values rather than applying abrupt thresholds.
  * Evaluate the complete residence-time distribution rather than relying only on average residence time.
  * Consider incorporating:

    * The proportion of flow paths exceeding one hour.
    * The proportion of flow paths falling within an identified beneficial processing window.
    * Median residence time.
    * Residence-time percentiles.
    * The presence of both short and long residence-time flow paths.
  * Treat the one-hour and one-day values as preliminary assumptions that require literature review, testing, and validation.

* **Develop exchange-capacity scoring from hyporheic connectivity**

  * Develop a scoring relationship using the number or frequency of hyporheic exchanges along the modeled stream reach.
  * Normalize connectivity where needed so values can be compared among reaches of different lengths or sizes.
  * Determine whether the metric should be expressed as:

    * Exchanges per mile of stream.
    * Exchanges per unit travel distance.
    * Exchanges per unit time.
    * Another normalized measure derived from the selected connectivity research.
  * Establish reference expectations using published studies, model testing, and eventually field-validated sites.

* **Develop storage-capacity scoring from hyporheic-zone size**

  * Develop a scoring relationship based on hyporheic-zone volume.
  * Evaluate whether hyporheic-zone 2D area should:

    * Be incorporated into the storage-capacity score.
    * Be reported as a separate supporting metric.
    * Be used to distinguish between deep, concentrated storage and broad, laterally distributed storage.
  * Normalize volume or area where necessary to account for differences in:

    * Stream length.
    * Channel width.
    * Drainage area.
    * Streamflow.
    * Valley or floodplain dimensions.
  * Establish reference expectations using modeled distributions, published studies, and field-validated sites.

* **Use functional capacities as surrogates for broader hyporheic functions**

  * Use the three hydraulic capacities to describe the potential to support:

    * Stream-temperature buffering.
    * Pollutant attenuation.
    * Nutrient cycling.
    * Habitat creation and maintenance.
  * Do not claim that the HYPE model directly measures these four ecosystem functions.
  * Avoid creating separate quantitative scores for temperature buffering or pollutant attenuation unless defensible scoring criteria can be developed.
  * Recognize that effective residence times for temperature buffering and pollutant attenuation may vary substantially based on:

    * Pollutant type and reaction rate.
    * Initial concentration.
    * Sediment chemistry.
    * Dissolved oxygen and redox conditions.
    * Water temperature.
    * Thermal boundary conditions.
    * Seasonal and hydrologic conditions.
  * Review relevant literature for reported beneficial residence-time ranges, but avoid applying a universal threshold when the evidence indicates strong site- or constituent-specific variation.

## Gradient Sensitivity Testing and Range Analysis

* **Add groundwater-gradient sensitivity testing**

  * Run the HYPE model across a realistic range of groundwater-gradient assumptions rather than relying on a single deterministic gradient.
  * Allow sensitivity testing for both:

    * Qualitative gradient categories.
    * User-entered quantitative gradients.
  * Define a central or preferred model scenario and a reasonable range of alternative scenarios around it.
  * Allow the tested range to vary by boundary or control point where appropriate.
  * Record every gradient scenario and its associated model outputs.

* **Develop methods for generating gradient scenarios**

  * Allow the user to specify:

    * A minimum, preferred, and maximum gradient.
    * A percentage or absolute variation around the preferred gradient.
    * A set of discrete gradient scenarios.
  * Consider automatically generating sensitivity scenarios from:

    * DEM uncertainty.
    * Stream-slope uncertainty.
    * The range represented by the qualitative gaining and losing categories.
    * User-specified confidence limits.
  * Include combinations of left- and right-boundary gradients when uncertainty differs across the floodplain.
  * Prevent the automated scenarios from producing physically unrealistic or numerically unstable boundary conditions.

* **Report the range of modeled results**

  * Calculate and report the sensitivity of the following outputs to groundwater-gradient assumptions:

    * Hyporheic connectivity.
    * Residence-time distribution.
    * Average and median residence time.
    * Selected residence-time percentiles.
    * Hyporheic-zone volume.
    * Hyporheic-zone 2D area.
  * For each metric, report:

    * The preferred or central estimate.
    * Minimum and maximum modeled values.
    * The total range or spread.
    * Percent change from the preferred estimate.
    * Additional uncertainty statistics where appropriate.

* **Visualize sensitivity and uncertainty**

  * Add figures showing how modeled metrics change across the tested gradient scenarios.
  * Consider displaying:

    * Range bars or uncertainty intervals.
    * Box plots.
    * Distribution plots.
    * Scenario-comparison tables.
    * Response curves relating groundwater gradient to each output metric.
  * Clearly distinguish the preferred scenario from the alternative sensitivity scenarios.
  * Include the range analysis in the site Summary Report.

* **Propagate uncertainty into the HFCI**

  * Recalculate exchange, storage, and processing capacity for each gradient scenario.
  * Report the resulting range of scores for each functional capacity.
  * Calculate the HFCI for each scenario and report:

    * The preferred HFCI.
    * The minimum and maximum HFCI.
    * The overall HFCI spread or uncertainty interval.
  * Identify which functional capacity contributes most to variation in the final HFCI.
  * Flag assessments where the functional-capacity classification or overall HFCI category changes across plausible gradient scenarios.

* **Communicate the limits of the analysis**

  * Explain that the reported spread represents sensitivity to groundwater-gradient assumptions and is not necessarily a complete statistical confidence interval.
  * Identify other sources of uncertainty that are not included unless they are explicitly tested, such as:

    * Hydraulic conductivity.
    * Soil-layer configuration.
    * Streamflow.
    * Model geometry.
    * Grid resolution.
    * Thermal, chemical, and biological conditions.
  * Preserve the tested inputs, model version, assumptions, and outputs so the sensitivity analysis can be reproduced.

