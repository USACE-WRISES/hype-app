# HYPE Screening-Level Hyporheic Pollutant Removal Reference

**Application:** HYPE hyporheic-zone modeling and reporting
**Purpose:** Literature-supported screening calculations for pollutant attenuation during hyporheic exchange
**Version:** 2.0
**Supersedes:** v1.0 (July 30, 2026)

---

## 0. What changed from v1.0

| Change | Reason |
|---|---|
| **Nitrate removed** | Removed at project direction. The v1.0 nitrate content (Pittroff et al. 2017, `k = 0.888 d⁻¹`; Frei et al. 2019 exposure-time treatment) remains technically sound and can be reinstated unmodified. Nothing below depends on it. |
| **Microplastics rebuilt as a path-length filtration module** | v1.0 treated microplastics as reach-distance retention only. Munz et al. (2024) provide measured filter coefficients that make a flowpath-length formulation implementable. See §5. |
| **Trace organics expanded** | Added Schaper et al. (2019) in-situ river half-lives alongside the Jaeger et al. (2021) flume acesulfame range, so the endpoint spans field and flume evidence and includes recalcitrant compounds. |
| **Added exchange-limitation layer** | v1.0 §9 computed removal per flowpath correctly but had no diagnostic for whether the reach is exchange-limited or reaction-limited. Added Damköhler regime logic (§4.4) and the reach-scale processing length (§4.3). |
| **Two rate values revised** | See §1. |

### Primary endpoint hierarchy (v2.0)

1. **Dissolved zinc** — conditional dissolved-phase attenuation, streams with Pinal Creek-like geochemistry
2. **Trace organic compounds** — acesulfame (flume) and pharmaceutical/contrast-agent range (in-situ river)
3. **Cobalt, nickel, manganese** — companion metals, same eligibility conditions as zinc
4. **Hexavalent chromium** — supplemental sensitivity scenario, derived rates only
5. **Microplastics** — separate particulate retention module, distance-based, never `d⁻¹`

With nitrate removed there is no endpoint in this set that represents *permanent destruction* of the pollutant. Every dissolved endpoint here is sorption, uptake, or transformation, and microplastics are physical retention. §7 specifies the required labeling.

---

## 1. Reconciliation notes

Two values from earlier drafts were wrong or ambiguous. Both are resolved in favor of the v1.0 HYPE document.

### 1.1 Zinc rate: use 83.52 d⁻¹, not 63.2 d⁻¹

Fuller & Harvey (2000) report two different statistics, and they are not interchangeable:

| Statistic | Value | Converted | Use |
|---|---|---|---|
| Mean of individual first-order rate constants | 0.058 ± 0.037 min⁻¹ | **83.52 ± 53.28 d⁻¹** | **Use this** |
| Mean of individual reaction-time constants (`1/λ`) | 0.38 h | 63.16 d⁻¹ | Do not use |

The reciprocal of an averaged time constant is not the average of the rate constants; by the arithmetic–harmonic mean inequality the mean of rates is always the larger. `1/0.058 min⁻¹ = 17.2 min`, whereas the separately reported mean time constant is `0.38 h = 22.8 min` — the gap is the inequality, not an error in the paper. Since the calculator applies `exp(−kt)`, the mean **rate** is the correct input. v1.0 was right; the 63.2 d⁻¹ figure in my earlier draft came from inverting the mean time constant and should be discarded.

### 1.2 Zinc reach-scale benchmark: 45% / 38% over 5.3 km

Use the v1.0 figures. My earlier draft cited "12–68% over 7 km," which conflated the multi-metal range with the zinc-specific result and used the wrong reach length. Correct values, after accounting for groundwater metal inputs over the 5.3-km perennial reach:

| Metal | 1994 load decrease | 1995 load decrease |
|---|---:|---:|
| Manganese | 17% | 26% |
| Cobalt | 68% | 37% |
| Nickel | 12% | 22% |
| Zinc | 45% | 38% |

Because these are corrected for groundwater inputs, they *are* comparable to a modeled attenuation fraction. This is the calibration target in §4.6.

---

## 2. Nomenclature

| Symbol | Definition | Units |
|---|---|---|
| `C₀` | Pollutant concentration entering the hyporheic zone | mg/L (or µg/L) |
| `k` | First-order attenuation rate for the dissolved endpoint | d⁻¹ |
| `tᵢ` | Effective reactive residence time, flowpath `i` | d |
| `wᵢ` | Hydraulic (flow) weight of flowpath `i` | m³/s or – |
| `f̄` | Flow-weighted fraction attenuated per pass | – |
| `T₅₀` | Median hyporheic residence time | d |
| `Da` | Damköhler number, `k · T₅₀` | – |
| `k_ex` | Hyporheic exchange rate constant (recirculating hyporheic flow per unit channel volume) | d⁻¹ |
| `Q_HZ` | Hyporheic return flow | m³/s |
| `Q_str` | Stream discharge | m³/s |
| `k_eff` | Apparent **reach-scale** first-order decay constant | d⁻¹ |
| `Λ` | Processing length (distance for 1/e reduction) | m |
| `U` | Mean stream velocity | m/d |
| `H` | Mean stream depth | m |
| `Lᵢ` | Subsurface **flowpath length**, flowpath `i` | cm |
| `λ_f` | Microplastic filter coefficient | **cm⁻¹** |
| `α_MP` | Microplastic **reach-scale** spatial retention coefficient | **km⁻¹** |
| `D` | Particle-to-grain size ratio, `d_p / d₅₀` | – |
| `d_p` | Microplastic particle size (minimum Feret diameter) | µm |
| `d₅₀` | Median sediment grain size | mm |
| `v_a` | Seepage velocity | m/d |

> **Symbol collision warnings.**
> 1. v1.0 used `α` for the microplastic spatial coefficient. This document uses `α_MP` for that and `k_ex` for the hyporheic exchange rate constant. They are unrelated quantities with different units.
> 2. `λ_f` (cm⁻¹) and `α_MP` (km⁻¹) are **both** distance coefficients for microplastics but describe different distances and differ by roughly six orders of magnitude. See §5.2 — this is the single most likely implementation error in the microplastics module.

---

## 3. Two calculation families

The app needs two structurally different calculators. Do not route a pollutant through the wrong one.

| | **Dissolved / solute module** | **Particulate module** |
|---|---|---|
| Endpoints | Zn, Co, Ni, Mn, acesulfame, TrOCs, Cr(VI) | Microplastics |
| Independent variable | **Residence time** `t` | **Path length** `L` and stream distance `x` |
| Coefficient | `k` [d⁻¹] | `λ_f` [cm⁻¹], `α_MP` [km⁻¹] |
| Governing form | `exp(−k·t)` | `exp(−λ_f·L)`, `exp(−α_MP·x)` |
| Rate-limiting variable in practice | Exchange flux (see §4.4) | Delivery and size exclusion (see §5.5) |
| Section | §4 | §5 |

---

## 4. Dissolved / solute module

### 4.1 Per-flowpath and flow-weighted removal

Retain the v1.0 formulation; it is correct and should remain primary.

For flowpath `i` with effective reactive time `tᵢ` in days:

    Cᵢ = C₀ · exp(−k · tᵢ)                                            (1)

Flow-weighted across the residence-time distribution:

    f̄ = 1 − [ Σᵢ wᵢ · exp(−k·tᵢ) ] / [ Σᵢ wᵢ ]                        (2)

Always use the full distribution when available. Applying `k` to the mean residence time overestimates removal, because by Jensen's inequality

    1 − exp(−k·E[t]) ≥ E[1 − exp(−k·t)]                               (3)

If MODPATH particles represent equal flow, equal weights are acceptable; otherwise use the associated water flux as `wᵢ`.

**Analytic fallbacks** when no distribution is available — use only if (2) cannot be evaluated, and label the result as distribution-free:

    f̄ ≈ 1 − exp(−Da)          plug flow, upper bound                 (4a)
    f̄ ≈ Da / (1 + Da)         exponential RTD, lower bound            (4b)

with `Da = k · T₅₀`. Prefer (4b). Equation (2) supersedes both.

### 4.2 Mass and concentration benefit

    M_removed [kg/d] = 86.4 · C₀ · Q_HZ · f̄                           (5)

    ΔC_stream ≈ C₀ · (Q_HZ / Q_str) · f̄                               (6)

    C_stream,out ≈ C₀ − ΔC_stream                                     (7)

The factor 86.4 converts mg/L × m³/s to kg/d. Equations (6)–(7) assume steady flow and concentration, complete mixing on hyporheic return, no additional sources between entry and return, `Q_HZ` counting unique exchanged water without double-counting repeated exchange cycles, and attenuation occurring only within modeled hyporheic flow.

### 4.3 Reach-scale rate and processing length

Report these alongside (6) so users can see whether the reach is long enough to matter:

    k_ex = Q_HZ / (Q_str · t_reach)   or equivalently  k_ex = q_hz / H (8)

    k_eff = k_ex · f̄                                                  (9)

    Λ = U / k_eff                                                      (10)

    Removal over reach length L_reach = 1 − exp(−k_eff · L_reach / U)  (11)

`k_eff` is the only rate in this document that may legitimately be applied to the stream water column. It is typically one to three orders of magnitude smaller than `k`. If the app ever displays `k` next to a stream concentration, that is a bug.

`Λ` is the reality check. Grant et al. (2014) computed a processing length of 275 km for a medium sand-bed stream with a contaminant half-life of 1.6 d, and concluded that hyporheic treatment systems operating outside their optimal state confer little water-quality improvement over distances under 1 km. A screening tool that routinely returns large benefits for sub-kilometre reaches is misparameterized.

### 4.4 Damköhler regimes and the `Da ≈ 1` target

`Da = k · T₅₀` determines which variable actually controls the answer. Display the regime.

| Regime | Interpretation | Consequence for the calculation |
|---|---|---|
| `Da < 0.01` | Reaction-limited. Water exits before reacting. | Removal ≈ 0. Result insensitive to exchange. |
| `Da ≈ 1` | **Optimum.** | Both `k` and `T₅₀` matter. Most informative regime. |
| `Da > 100` | **Transport / mass-transfer limited.** Reaction completes early on the flowpath. | Result is set entirely by exchange flux; `k` is irrelevant. Additional residence time buys nothing. |

Harvey et al. (2013), using ¹⁵NO₃⁻, Br⁻ and SF₆ tracer injections at Sugar Creek, Indiana, found hyporheic denitrification was most efficient in the subset of flowpaths where the Damköhler number was approximately 1 — excluding both deep pathways where substrate is exhausted and shallow pathways requiring repeated entries and exits. The same logic applies to any first-order endpoint.

Harvey et al. also found the effective zone of significant reaction often differs from the full hyporheic depth, which is one reason whole-stream rates have not been explained using bulk metrics such as total zone size or mean residence time. **Do not use total hyporheic depth or total residence time as the reactive volume** unless the literature rate was itself fitted that way.

**Zinc sits at the transport-limited end.** With `k = 83.52 d⁻¹` and `T₅₀ = 20 min`, `Da ≈ 1.16`; at `T₅₀ = 80 min`, `Da ≈ 4.6`. Beyond roughly an hour of residence time the kinetic result saturates and the rate constant stops carrying information. This is why §4.6 calibrates against effective passes rather than propagating `k` forward.

### 4.5 Parameter table — dissolved endpoints

All rates in d⁻¹. **R** = reported by the authors; **D** = derived here or in v1.0 by unit conversion or from reported endpoints.

#### 4.5.1 Trace metals — Fuller & Harvey (2000), Pinal Creek, AZ

| Metal | Reported rate | `k` (d⁻¹) | ± SD | Src | Mean uptake | Uptake range | Lab conc. |
|---|---:|---:|---:|:--:|---:|---:|---:|
| Zinc | 0.058 min⁻¹ | **83.52** | 53.28 | D | 36 ± 24% | 7–92% | 0.602 mg/L |
| Cobalt | 0.041 min⁻¹ | **59.04** | 50.40 | D | 52 ± 25% | 8–100% | 0.424 mg/L |
| Nickel | 0.020 min⁻¹ | **28.80** | 31.68 | D | 27 ± 19% | 7–74% | 0.440 mg/L |
| Manganese | 0.013 min⁻¹ | **18.72** | 20.16 | D | 22 ± 19% | 5–94% | — |

Conversion: `k[d⁻¹] = k[min⁻¹] × 1440`.

Concentrations are **laboratory starting concentrations** (Zn 9.2 µmol/L, Co 7.2 µmol/L, Ni 7.5 µmol/L) that the authors described as similar to surface water in the study reach. The paper did not tabulate a field-reach mean. Label as *laboratory benchmark representative of the study reach*, never as a measured reach average.

**Eligibility gate — all must be true before displaying a metals result:**
- Circumneutral pH (study reach rose from ≈6.5 to ≈7.5)
- Dissolved manganese present, or active Mn-oxide coatings
- Modeled residence times near the calibration range (<2 to 80 minutes)

Uptake was predominantly sorption to, or incorporation into, newly forming Mn-oxide coatings, with strong spatial variability over distances under 10 m. Fuller & Bargar (2014) confirmed Zn attenuation was largely controlled by sorption to microbial Mn oxides and observed desorption as pH decreased. **Reversible — not destruction.**

For residence times well beyond 80 minutes the mean rate predicts near-complete uptake, which extrapolates outside the field calibration and ignores finite sorption capacity and later remobilization. Present as an upper bound and compare against the observed 7–92% distribution. **Do not apply the kinetic calculation and the empirical 36% sequentially.**

#### 4.5.2 Acesulfame — Jaeger et al. (2021), river-simulating flumes

| Reported half-life | `k` (d⁻¹) | Src |
|---:|---:|:--:|
| 6.6 h | 2.52 | D |
| 36.6 h | 0.455 | D |
| 54.4 h | 0.306 | D |
| 55.0 h | 0.303 | D |

Conversion: `k = ln(2)/t₁⁄₂`. Use **0.30–2.52 d⁻¹ as a range**, not a single value. Reference concentration 11.5 µg/L (spiked flume surface water). Median flowpath travel times in the four flumes were ≈11.5, 20.1, 24.3 and 43.3 h. The authors also reported a median fitted rate of ≈0.11 h⁻¹ (2.64 d⁻¹) for the shortest flowpath; the small difference from 2.52 reflects rounding.

Caveats to display: spiked water, controlled flume not in-situ river, River Erpe sediment diluted 1:10 with sand, shallow bedform-driven exchange.

#### 4.5.3 Pharmaceuticals and contrast agents — Schaper et al. (2019), River Erpe

In-situ field counterpart to the flume data above. 28 compounds, hourly sampling over 17 h at three depths, first-order removal rates and retardation coefficients from a 1-D reactive transport model.

| Compound | Half-life (top 10 cm) | `k` (d⁻¹) | Src |
|---|---:|---:|:--:|
| Iopromide | 0.1 ± 0.01 h | **166** | D |
| Tramadol | 3.3 ± 0.3 h | **5.0** | D |
| Venlafaxine | stable | **≈ 0** | R |
| O-desmethylvenlafaxine | stable | **≈ 0** | R |
| Dihydroxy-carbamazepine | stable | **≈ 0** | R |

Two structural caveats:
1. These half-lives apply to the **first 10 cm** of the bed, where removal of biodegradable dissolved organic matter was also highest — not to a full hyporheic flowpath. Do not extrapolate over a 1 m path.
2. Rates are strongly redox-dependent. Schaper et al. (2018) found significantly higher removal under suboxic (denitrifying) than under anoxic (Fe/Mn-reducing) conditions.

**Keep the recalcitrant compounds in the selectable list.** They are the honest demonstration that hyporheic exchange is not universally a treatment process, and a tool that only offers reactive compounds will systematically overstate benefit.

#### 4.5.4 Hexavalent chromium — Jung et al. (2020), derived only

| Sediment | Initial Cr(VI) | ~2-day removal | `k` (d⁻¹) | Src |
|---|---:|---:|---:|:--:|
| Organic-rich Passaic River | 0.1 mg/L | ~98% | 1.956 | D |
| Hackensack River | 0.1 mg/L | ~68% | 0.570 | D |
| Newark Bay | 0.1 mg/L | ~30% | 0.178 | D |
| Organic-rich Passaic River | 0.5 mg/L | ~92% | 1.263 | D |
| Hackensack River | 0.5 mg/L | ~58% | 0.434 | D |
| Newark Bay | 0.5 mg/L | ~25% | 0.144 | D |

Derivation: `k = −ln(C_t/C₀)/2 d`. **The authors did not fit or report these rates.** They described biphasic kinetics, and a batch sediment-to-water ratio does not represent an advective field flowpath. Organic-rich sediment supported greater reduction to Cr(III) and less remobilization; removal in lower-organic sediment was more strongly associated with reversible adsorption. Sensitivity scenario only, always labeled *apparent endpoint-equivalent attenuation*.

#### 4.5.5 Generic placeholder

| Endpoint | `k` (d⁻¹) | Basis | Src |
|---|---:|---|:--:|
| Low-persistence organic / labile DOC | 0.43 (t₁⁄₂ = 1.6 d) | Grant et al. (2014) reference simulation, `k = 5×10⁻⁶ s⁻¹` | D |

Conservative default when no compound-specific value exists.

### 4.6 Calibration target — zinc

Rather than propagating `k = 83.52 d⁻¹` forward across a whole reach (which saturates, §4.4), check the model against Fuller & Harvey's two independent observations.

**Check 1 — per-pass kinetics reproduce the observed uptake spread.** At `k = 83.52 d⁻¹` over the field calibration travel-time range:

| Travel time | Predicted uptake |
|---:|---:|
| 2 min | 11.0% |
| 8 min | 37.1% |
| 20 min | 68.7% |
| 40 min | 90.2% |
| 80 min | 99.0% |

Observed: mean 36%, range 7–92%. The predicted 11–99% band over the <2–80 min calibration range brackets the observed 7–92% band, and the 36% mean corresponds to a travel time of 7.7 min — inside the calibration range. The kinetic model is internally consistent with the field data.

**Check 2 — effective passes over the reach.** At 36% uptake per pass, the observed reach-scale zinc load decreases imply:

| Year | Observed decrease over 5.3 km | Implied effective passes |
|---|---:|---:|
| 1994 | 45% | 1.34 |
| 1995 | 38% | 1.07 |

**A correctly parameterized model should produce roughly 1–1.4 effective hyporheic passes over ~5 km for a Pinal Creek-like reach.** If the app produces 10 passes, `k_ex` is far too high. If it produces 0.1, too low. This is the most useful single calibration diagnostic in the document, because it tests the exchange parameterization independently of the rate constant.

---

## 5. Particulate module — microplastics

### 5.1 Why residence time is the wrong independent variable

Microplastic retention in a streambed is **deep-bed filtration**, described since Iwasaki (1937) by an exponential decline in particle abundance with *distance* through the medium:

    C(z)/C₀ = exp(−λ_f · z)                                          (12)

where `λ_f` [cm⁻¹] is the filter coefficient and `z` is distance travelled through the sediment. `λ_f` depends on grain size, particle size and seepage velocity — **not on elapsed time**. Two particles that travel the same path length have the same capture probability whether they took one hour or one week.

Munz et al. (2024) tested this directly. In saturated column experiments (50 cm columns, polystyrene fragments 100–2000 µm, medium gravel `d₅₀` 6.60 mm and coarse sand `d₅₀` 1.51 mm, seepage velocities 1.8–27 m/d) they found that retention profiles were **independent of flow duration beyond about 2 pore volumes** — a steady retention profile established within the first exchanged pore volumes and no substantial further relocation occurred. Extending the infiltration duration had negligible effect compared with grain size and flow velocity. Model fits to (12) averaged R² = 0.86.

That is direct empirical support for the position: **do not use `exp(−k·t)` for microplastics.** Confirms and strengthens the v1.0 prohibition.

### 5.2 The two distance coefficients are not interchangeable

This is the critical implementation hazard.

| Coefficient | Applies to | Value | In m⁻¹ |
|---|---|---|---|
| `α_MP` | **Stream distance** `x` downstream, reach scale | 0.0513 km⁻¹ | 5.13×10⁻⁵ |
| `λ_f` | **Subsurface flowpath length** `L` within the bed | 0.18–1.0 cm⁻¹ | 18–100 |

They differ by roughly **six orders of magnitude** because they describe different geometry. `α_MP` is a lumped reach coefficient that already bundles how much water enters the bed per km, how far it travels, capture efficiency, and remobilization. `λ_f` is the capture efficiency alone, per unit of subsurface travel. Never substitute one for the other, and never multiply them together.

### 5.3 Tier A — reach-scale empirical retention (default)

Retain the v1.0 formulation.

    α_MP = −ln(1 − 0.05) = 0.0513 km⁻¹                                (13)

    f_retained = 1 − exp(−α_MP · L_reach[km])                         (14)

Basis: Drummond et al. (2022) modeled microplastic accumulation from hyporheic exchange across stream classes from headwaters to mainstems. Long-term accumulation — defined in that study as storage exceeding approximately 317 years — averaged approximately **5% of microplastic input per river-kilometre** for small and lightweight particles, primarily ≤100 µm, varying from roughly 3% to 8% per km among stream classes. Headwater residence times averaged ≈5 h/km, increasing to as much as 7 years/km at low flow.

Use `α_MP` derived from 3% and 8% as the low/high sensitivity bounds: 0.0305 and 0.0834 km⁻¹.

**Strength:** directly citable, needs nothing from the flow model. **Weakness:** insensitive to site conditions — an armored bed and a loose sand bed return the same answer. Tier B exists to fix that.

### 5.4 Tier B — flowpath-length filtration (site-sensitive)

Use when MODPATH flowpath lengths are available. Three steps.

**Step 1 — Size-exclusion gate.** Particles too large to enter the pore network do not filter; they deposit at the interface instead, by a different and more remobilizable mechanism.

    D = d_p / d₅₀                                                    (15)

| `D` | Behaviour | Route to |
|---|---|---|
| `D < 0.002` | Straining negligible; attachment-controlled | Tier B, low `λ_f` |
| `0.002 ≤ D ≲ 0.08` | Enters pore network, straining active | **Tier B, Eq. (16)–(17)** |
| `D ≳ 0.08` | Size-excluded from the pore network | Interface deposition — report separately, flag as highly remobilizable |

Munz et al. found the maximum `D` permitting infiltration below 20 cm in saturated columns was about **0.08**, somewhat smaller than the 0.11 proposed by Waldschläger & Schüttrumpf (2020) from unsaturated glass-bead experiments (a factor of 0.77), attributed partly to differing water fluxes. Bradford et al. (2002) place the onset of mechanical straining at `D > 0.002`.

The 0.08 gate reproduces the observed column behaviour closely:

| Sediment | Observation | Gate at `D` = 0.08 |
|---|---|---|
| Gravel, `d₅₀` 6.60 mm | All >1000 µm retained in upper 5 cm | 1000 µm → `D` = 0.15, excluded ✔ |
| Gravel, `d₅₀` 6.60 mm | 500–1000 µm penetrated ~20 cm average | 500 µm → `D` = 0.076, borderline-included ✔ |
| Gravel, `d₅₀` 6.60 mm | <500 µm found through full 50 cm at high flow | 250 µm → `D` = 0.038, included ✔ |
| Sand, `d₅₀` 1.51 mm | >500 µm retained in upper 2.5 cm | 500 µm → `D` = 0.33, excluded ✔ |
| Sand, `d₅₀` 1.51 mm | <500 µm infiltrated ~15 cm average | 100 µm → `D` = 0.066, included ✔ |

**Step 2 — Per-flowpath capture.**

    f_cap,i = 1 − exp(−λ_f · Lᵢ)      Lᵢ in cm                        (16)

**Step 3 — Flow-weighted capture per pass.**

    f̄_cap = [ Σᵢ wᵢ · (1 − exp(−λ_f·Lᵢ)) ] / [ Σᵢ wᵢ ]                (17)

#### Filter coefficient values

Measured range, Munz et al. (2024), Table 3:

| Bound | `λ_f` (cm⁻¹) | Condition |
|---|---:|---|
| Minimum | **0.18** | Gravel, highest flow (≈38 mL/min), 100–250 µm |
| Central (geometric) | **≈0.42** | — |
| Maximum | **1.00** | Coarse sand, lowest flow (2 mL/min), 250–500 µm |

Direction of dependence, all significant at p < 0.01:
- **increases** with increasing particle size
- **decreases** with increasing flow velocity — gravel columns increased on average 0.015 cm⁻¹ per 1 m/d decrease in seepage velocity
- **decreases substantially** with increasing sediment grain size

Regression (Munz et al. Eq. 4, R² = 0.92, all predictors normalized by their means):

    λ_f* = log(λ_f) = −0.24
                     + 0.44 · (minFeret_L / minFeret_mean)
                     − 0.32 · (v_a / v_a,mean)
                     − 0.47 · (d₅₀ / d₅₀,mean)                        (18)

> **Two things must be resolved before coding Eq. (18).**
> 1. **Log base.** The paper states a log link in R's `stats` package, which defaults to natural log. Evaluated at all predictors = 1 the regression gives `λ_f* = −0.59`; `exp(−0.59) = 0.554 cm⁻¹` falls near the middle of the measured 0.18–1.0 range, whereas `10^(−0.59) = 0.257 cm⁻¹` falls near the bottom. Natural log is the better fit and the software default, but confirm against Table 3 / Figure 5 before relying on it.
> 2. **Normalization means.** `minFeret_mean`, `v_a,mean` and `d₅₀,mean` are not reproduced here. Extract them from the paper's Table 1 and Table 3, or from the public dataset at https://doi.org/10.5281/zenodo.8055599. **Eq. (18) is not implementable without them** — until then, use the tabulated 0.18 / 0.42 / 1.00 bracket.

#### Two hard limits on Eq. (16)

**Floor at 0.023.** Munz et al. found that below a relative particle abundance of 0.023 the profile stops declining exponentially and instead stays roughly constant with depth. Cap predicted capture at **97.7%** and do not report deep-bed capture as complete. A small fraction passes through regardless of path length — the mechanism by which pore-scale microplastics (100–500 µm) reach hyporheic zones and alluvial aquifers.

**Validity envelope.** Eq. (18) is valid only for polystyrene fragments within the tested ranges of seepage velocity, particle size and grain size, in organic-free homogeneous sediments with narrow grain-size distributions. Natural beds add organic content, wide grain-size distributions, dynamic flow with lateral forces, bioturbation and biofilm formation, and seasonal variability — which the authors invoke to explain heterogeneous field retention profiles and the occurrence of large particles at depth that infiltration alone cannot explain.

### 5.5 The result that matters: capture saturates, so path length barely does

Evaluating Eq. (16) across the measured `λ_f` range:

| Path length | `λ_f` = 0.18 | `λ_f` = 0.55 | `λ_f` = 1.00 |
|---:|---:|---:|---:|
| 1 cm | 16.5% | 42.3% | 63.2% |
| 2 cm | 30.2% | 66.7% | 86.5% |
| 5 cm | 59.3% | 93.6% | 99.3% |
| 10 cm | 83.5% | 99.6% | ~100% |
| 20 cm | 97.3% | ~100% | ~100% |
| 50 cm | ~100% | ~100% | ~100% |

**Even the most permissive combination captures 83% within 10 cm.** For typical bedform hyporheic path lengths of 0.1–1 m, `λ_f · L` is roughly 2–100, so essentially every particle small enough to enter the bed is captured within the first few centimetres. Path length carries information only below ~10 cm.

So the same conclusion reached for zinc in §4.4 applies here, for a different reason: **flowpath length is not the rate-limiting variable.** What controls the reach-scale answer is (a) how much particle flux is delivered into the bed, (b) the size-exclusion gate of §5.4 Step 1, and (c) remobilization by bed turnover.

**This reconciles Tier A with Tier B.** If per-pass capture is near 100%, then Drummond's ~5%/km cannot be a capture-efficiency limit — it must be a *delivery-and-retention* limit. Many particles enter the bed, are captured, and are later remobilized by bed turnover; only ~5% per km reaches long-term storage. Tier A's small coefficient and Tier B's near-complete capture are consistent, and the difference between them is exactly the remobilization term that neither module represents explicitly.

**Practical consequence for the app:** run Tier A as the reported number. Run Tier B as a diagnostic that answers "is this bed capable of capturing this particle size at all?" — a near-binary gate — rather than as a competing estimate of reach retention. Do not sum them.

### 5.6 Delivery context

Drummond et al. (2020) established that hyporheic exchange, not settling, is the dominant delivery mechanism for small microplastics: in a field stream study 23% of all microplastic size–density combinations had a hyporheic exchange rate exceeding their settling rate, rising to 42% for low-density polymers such as polyethylene, with hyporheic exchange important for particles <100 µm irrespective of polymer type. Models omitting hyporheic exchange substantially underestimate deposition, retention and long-term accumulation. Drummond et al. (2022) note the ratio of hyporheic exchange rate to gravitational settling rate can exceed 100,000 for microplastics.

Use this to justify the module's existence and to set applicability: **below ~100 µm the module is well supported; above ~500 µm the size-exclusion gate dominates and the answer is interface deposition, not filtration.**

---

## 6. Machine-readable parameter block

```yaml
hyporheic_pollutant_scenarios:
  version: 2.0
  nitrate: removed_at_project_direction   # v1.0 content remains valid if reinstated

  # ---------------- DISSOLVED / SOLUTE MODULE ----------------
  zinc:
    display_name: Dissolved zinc attenuation
    module: solute
    model_type: conditional_first_order_uptake
    recommended_default: true
    independent_variable: residence_time
    literature_rate_reported:
      mean: 0.058
      standard_deviation: 0.037
      units: 1/minute
      statistic: mean_of_individual_rate_constants
    calculator_rate:
      mean: 83.52
      standard_deviation: 53.28
      units: 1/day
      derivation: reported_rate_times_1440
      source_flag: derived
    do_not_use_rate:
      value: 63.16
      units: 1/day
      reason: inverse_of_mean_time_constant_not_mean_of_rates
    reference_concentration:
      value: 0.602
      units: mg_Zn_per_L
      original_value: 9.2
      original_units: micromol_Zn_per_L
      basis: laboratory_starting_concentration_described_as_similar_to_reach_surface_water
    calibration_travel_time:
      range: less_than_2_to_80
      units: minutes
    observed_uptake:
      mean_percent: 36
      standard_deviation_percent: 24
      range_percent: [7, 92]
    reach_scale_load_decrease:
      reach_length_km: 5.3
      percent_1994: 45
      percent_1995: 38
      basis: after_accounting_for_groundwater_metal_inputs
    calibration_target:
      effective_passes_over_5_3_km: [1.07, 1.34]
    permanence: potentially_reversible_sorption
    eligibility:
      circumneutral_pH: required
      manganese_oxide_formation: required
      residence_time_near_calibration_range: required
    primary_source: Fuller_and_Harvey_2000
    mechanism_source: Fuller_and_Bargar_2014

  # Co, Ni, Mn share zinc's eligibility gate, mechanism, and source.
  # All calculator rates derived as reported_rate_per_minute x 1440.
  cobalt:
    module: solute
    calculator_rate: { mean: 59.04, standard_deviation: 50.40, units: 1/day }
    literature_rate_reported: { mean: 0.041, standard_deviation: 0.035, units: 1/minute }
    observed_uptake: { mean_percent: 52, standard_deviation_percent: 25, range_percent: [8, 100] }
    reference_concentration: { value: 0.424, units: mg_per_L, original_value: 7.2, original_units: micromol_per_L }
    reach_load_decrease_percent: { "1994": 68, "1995": 37 }
    eligibility: same_as_zinc
    source_flag: derived
    primary_source: Fuller_and_Harvey_2000

  nickel:
    module: solute
    calculator_rate: { mean: 28.80, standard_deviation: 31.68, units: 1/day }
    literature_rate_reported: { mean: 0.020, standard_deviation: 0.022, units: 1/minute }
    observed_uptake: { mean_percent: 27, standard_deviation_percent: 19, range_percent: [7, 74] }
    reference_concentration: { value: 0.440, units: mg_per_L, original_value: 7.5, original_units: micromol_per_L }
    reach_load_decrease_percent: { "1994": 12, "1995": 22 }
    eligibility: same_as_zinc
    source_flag: derived
    primary_source: Fuller_and_Harvey_2000

  manganese:
    module: solute
    calculator_rate: { mean: 18.72, standard_deviation: 20.16, units: 1/day }
    literature_rate_reported: { mean: 0.013, standard_deviation: 0.014, units: 1/minute }
    observed_uptake: { mean_percent: 22, standard_deviation_percent: 19, range_percent: [5, 94] }
    reach_load_decrease_percent: { "1994": 17, "1995": 26 }
    eligibility: same_as_zinc
    source_flag: derived
    primary_source: Fuller_and_Harvey_2000

  acesulfame:
    display_name: Acesulfame transformation
    module: solute
    model_type: first_order_transformation_range
    recommended_default: false
    calculator_rate_range:
      low: 0.30
      high: 2.52
      units: 1/day
      derivation: ln_2_divided_by_reported_half_life
      source_flag: derived
    individual_values_per_day: [2.52, 0.455, 0.306, 0.303]
    reported_half_lives_hours: [6.6, 36.6, 54.4, 55.0]
    flume_median_travel_times_hours: [11.5, 20.1, 24.3, 43.3]
    reference_concentration: { value: 11.5, units: microgram_per_L,
                               basis: spiked_flume_surface_water }
    evidence_setting: controlled_river_simulating_flumes_sediment_diluted_1_to_10_with_sand
    primary_source: Jaeger_et_al_2021

  trace_organics_insitu:
    display_name: Pharmaceutical and contrast-agent attenuation
    module: solute
    model_type: first_order_transformation_compound_specific
    recommended_default: false
    depth_of_applicability: top_10_cm_benthic_biolayer
    redox_dependence: higher_under_suboxic_than_anoxic
    compounds:
      iopromide:              { half_life_hours: 0.1,  k_per_day: 166.0, source_flag: derived }
      tramadol:               { half_life_hours: 3.3,  k_per_day: 5.0,   source_flag: derived }
      venlafaxine:            { stable: true, k_per_day: 0.0, source_flag: reported }
      o_desmethylvenlafaxine: { stable: true, k_per_day: 0.0, source_flag: reported }
      dihydroxy_carbamazepine: { stable: true, k_per_day: 0.0, source_flag: reported }
    warning: do_not_extrapolate_top_10cm_rates_over_full_flowpath
    primary_source: Schaper_et_al_2019
    redox_source: Schaper_et_al_2018

  chromium_VI:
    display_name: Hexavalent chromium attenuation
    module: solute
    model_type: endpoint_equivalent_first_order_range
    recommended_default: false
    calculator_rate_range:
      low: 0.144
      high: 1.956
      units: 1/day
      derivation: negative_ln_remaining_fraction_divided_by_2_days
      source_flag: derived
    reference_concentration_range: { low: 0.1, high: 0.5, units: mg_per_L }
    evidence_setting: two_day_hyporheic_sediment_batch_experiment
    warning: authors_reported_biphasic_kinetics_and_did_not_fit_these_rates
    primary_source: Jung_et_al_2020

  generic_low_persistence_organic:
    module: solute
    calculator_rate: { value: 0.43, units: 1/day, half_life_days: 1.6,
                       source_flag: derived }
    basis: reference_simulation_value_5e-6_per_second
    primary_source: Grant_et_al_2014

  # ---------------- PARTICULATE MODULE ----------------
  microplastics:
    display_name: Microplastic retention
    module: particulate
    model_type: spatial_retention
    recommended_default: false
    independent_variable: distance_not_time
    warning: never_use_temporal_first_order_decay_in_per_day_units

    tier_a_reach_scale:
      status: reported_default
      coefficient:
        symbol: alpha_MP
        value: 0.0513
        units: 1/km
        derivation: negative_ln_0.95
        low: 0.0305      # from 3 %/km
        high: 0.0834     # from 8 %/km
      applies_to: downstream_stream_distance_km
      reported_accumulation:
        value: approximately_5
        units: percent_input_per_river_km
        stream_class_range_percent_per_km: [3, 8]
        definition: long_term_storage_exceeding_approximately_317_years
        principal_particle_size: less_than_or_equal_to_100_micrometers
      residence_time_context:
        headwater_average: 5_hours_per_km
        headwater_low_flow: up_to_7_years_per_km
      primary_source: Drummond_et_al_2022

    tier_b_flowpath_filtration:
      status: diagnostic_only_do_not_sum_with_tier_a
      coefficient:
        symbol: lambda_f
        units: 1/cm
        measured_min: 0.18
        measured_geometric_mid: 0.42
        measured_max: 1.00
        applies_to: subsurface_flowpath_length_cm
      dependencies:
        increases_with: particle_size
        decreases_with: [flow_velocity, sediment_grain_size]
        gravel_sensitivity: 0.015_per_cm_per_1_m_per_day_decrease_in_velocity
      regression_eq18:
        implementable: false
        blockers:
          - log_base_unconfirmed_natural_log_assumed
          - normalization_means_not_available
        normalization_means_source: >
          paper Table 1 and Table 3, or dataset doi:10.5281/zenodo.8055599
        coefficients: { intercept: -0.24, minFeret: 0.44, v_a: -0.32, d50: -0.47 }
        r_squared: 0.92
      capture_cap_percent: 97.7
      capture_cap_reason: profile_stops_declining_exponentially_below_relative_abundance_0.023
      size_exclusion_gate:
        ratio: d_p_over_d50
        straining_onset: 0.002
        exclusion_threshold: 0.08
        exclusion_threshold_alt: 0.11
        excluded_behaviour: interface_deposition_highly_remobilizable_report_separately
        straining_onset_source: Bradford_et_al_2002
        exclusion_source: Munz_et_al_2024
        exclusion_alt_source: Waldschlager_and_Schuttrumpf_2020
      validity_envelope: >
        polystyrene fragments, organic-free homogeneous narrow-graded sediment,
        seepage velocity 1.8-27 m/d, d50 1.51-6.60 mm, particle size 100-2000 um
      primary_source: Munz_et_al_2024

    key_finding: >
      Per-pass capture saturates above roughly 10 cm of flowpath even at the lowest
      measured filter coefficient, so flowpath length is not rate-limiting. Reach-scale
      retention is controlled by delivery flux, the size-exclusion gate, and
      remobilization by bed turnover.
    delivery_context_source: Drummond_et_al_2020
    permanence: temporary_or_long_term_retention_with_possible_remobilization
```

---

## 7. Implementation rules

Carried forward from v1.0 §12, revised for v2.0.

1. **Route by module.** Solute endpoints use residence time; microplastics use distance. Never apply `exp(−k·t)` to microplastics, and never apply a distance coefficient to a dissolved endpoint.
2. **Preserve the reported/derived distinction.** Every rate in §4.5 and §5 carries a source flag. Display it.
3. **Require concentration units and chemical basis** in the data model.
4. **Apply rates to individual weighted residence times before aggregating** — Eq. (2), not `k` on the mean.
5. **Never display `k` next to a stream concentration.** Only `k_eff` (Eq. 9) applies to the water column.
6. **Enforce the zinc eligibility gate** before showing any metals result, and display the geochemical and time-scale limitations.
7. **Do not extrapolate metals rates beyond ~80 min residence time** without an upper-bound warning and comparison against the 7–92% observed distribution.
8. **Do not apply a metals kinetic result and the empirical uptake percentage sequentially.**
9. **Do not extrapolate the Schaper top-10-cm rates over a full flowpath.**
10. **Label Cr(VI) rates as derived**, never as author-reported.
11. **Do not sum microplastic Tier A and Tier B.** Tier A is the reported number; Tier B is a capability diagnostic.
12. **Cap microplastic per-pass capture at 97.7%.**
13. **Route size-excluded particles (`D ≳ 0.08`) to interface deposition**, reported separately and flagged as highly remobilizable.
14. **Display `Da` and its regime** for every solute calculation, and `Λ` for every reach calculation.
15. **Display the full citation** for every selected scenario.
16. **Allow the user to override literature concentration and rate** with site-specific values.
17. **Label all results screening-level estimates.**

### Required terminology

| Endpoint | Use | Never |
|---|---|---|
| Zn, Co, Ni, Mn | *dissolved-phase attenuation*, *reactive uptake* | destruction, permanent removal |
| Acesulfame, pharmaceuticals | *transformation* | — |
| Cr(VI) | *apparent endpoint-equivalent attenuation* | reported rate, measured kinetics |
| Microplastics | *retention*, *storage*, *delayed transport* | degradation, destruction, removal |

Also avoid calling a literature concentration a site concentration, presenting a derived conversion as author-reported, and presenting one literature rate as universally applicable.

**Note for v2.0 specifically:** with nitrate removed, no endpoint in this set represents permanent destruction of the pollutant. Any summary language implying the river has been permanently cleaned is unsupported by the current endpoint set.

---

## 8. Limitations

- **Steady flow only.** Peak-flow events redistribute reaction rates through the bed by orders of magnitude at sub-metre scales, and mobilize retained particles.
- **Exchange flux is the dominant uncertainty.** Grant et al. (2014) found the Elliott & Brooks relation reproduced the magnitude and trend of 42 measured flume mass-transfer coefficients — which spanned 5.2×10⁻⁷ to 5.8×10⁻⁴ m/s — but over- or under-predicted individual values by up to a factor of 10.
- **Bedform pumping only.** Excludes turbulent-eddy exchange, bioturbation, meander-bend and riffle-pool exchange, and regional groundwater flow.
- **First-order kinetics are an approximation.** Several endpoints here (Cr(VI) explicitly, metals implicitly through finite sorption capacity) are not first-order.
- **Rates are site-derived.** Every value in §4.5 comes from one or a few study reaches or flumes. Transferability to a different bed texture, temperature, redox state or carbon regime is unverified.
- **No temperature correction.** Add an Arrhenius adjustment if seasonal comparison matters.
- **No finite capacity.** Sorption endpoints have no breakthrough term; long-duration or high-load scenarios will be optimistic.
- **No remobilization term.** Both the metals sorption endpoints and the microplastic module represent capture without release. This is the largest structural gap in v2.0 and the main reason results must be labeled retention rather than removal.

---

## 9. References

Bradford, S. A., Yates, S. R., Bettahar, M., & Simunek, J. (2002). Physical factors affecting the transport and fate of colloids in saturated porous media. *Water Resources Research, 38*(12), 63-1–63-12. https://doi.org/10.1029/2002WR001340

Drummond, J. D., Nel, H. A., Packman, A. I., & Krause, S. (2020). Significance of hyporheic exchange for predicting microplastic fate in rivers. *Environmental Science & Technology Letters, 7*(10), 727–732. https://doi.org/10.1021/acs.estlett.0c00595

Drummond, J. D., Schneidewind, U., Li, A., Hoellein, T. J., Krause, S., & Packman, A. I. (2022). Microplastic accumulation in riverbed sediment via hyporheic exchange from headwaters to mainstems. *Science Advances, 8*(2), eabi9305. https://doi.org/10.1126/sciadv.abi9305

Elliott, A. H., & Brooks, N. H. (1997). Transfer of nonsorbing solutes to a streambed with bed forms: Theory. *Water Resources Research, 33*(1), 123–136.

Fuller, C. C., & Bargar, J. R. (2014). Processes of zinc attenuation by biogenic manganese oxides forming in the hyporheic zone of Pinal Creek, Arizona. *Environmental Science & Technology, 48*(4), 2165–2172. https://doi.org/10.1021/es402576f

Fuller, C. C., & Harvey, J. W. (2000). Reactive uptake of trace metals in the hyporheic zone of a mining-contaminated stream, Pinal Creek, Arizona. *Environmental Science & Technology, 34*(7), 1150–1155. https://doi.org/10.1021/es990714d

Grant, S. B., Stolzenbach, K., Azizian, M., Stewardson, M. J., Boano, F., & Bardini, L. (2014). First-order contaminant removal in the hyporheic zone of streams: Physical insights from a simple analytical model. *Environmental Science & Technology, 48*(19), 11369–11378. https://doi.org/10.1021/es501694k

Harvey, J. W., Böhlke, J. K., Voytek, M. A., Scott, D., & Tobias, C. R. (2013). Hyporheic zone denitrification: Controls on effective reaction depth and contribution to whole-stream mass balance. *Water Resources Research, 49*(10), 6298–6316. https://doi.org/10.1002/wrcr.20492

Iwasaki, T., Slade, J. J., & Stanley, W. E. (1937). Some notes on sand filtration. *Journal of the American Water Works Association, 29*(10), 1591–1602. https://doi.org/10.1002/j.1551-8833.1937.tb14014.x

Jaeger, A., Posselt, M., Schaper, J. L., Betterle, A., Rutere, C., Coll, C., Mechelke, J., Raza, M., Meinikmann, K., Portmann, A., Blaen, P. J., Horn, M. A., Krause, S., & Lewandowski, J. (2021). Transformation of organic micropollutants along hyporheic flow in bedforms of river-simulating flumes. *Scientific Reports, 11*(1), 13034. https://doi.org/10.1038/s41598-021-91519-2

Jung, H. B., Severini, J., & Hall, E. (2020). Removal of hexavalent chromium by hyporheic zone sediments in an urbanized estuary. *Water Science and Technology, 82*(11), 2389–2399. https://doi.org/10.2166/wst.2020.510

Munz, M., Loui, C., Postler, D., Pittroff, M., & Oswald, S. E. (2024). Transport and retention of micro-polystyrene in coarse riverbed sediments: Effects of flow velocity, particle and sediment sizes. *Microplastics and Nanoplastics, 4*, 2. https://doi.org/10.1186/s43591-023-00077-z — dataset: https://doi.org/10.5281/zenodo.8055599

Schaper, J. L., Seher, W., Nützmann, G., Putschew, A., Jekel, M., & Lewandowski, J. (2018). The fate of polar trace organic compounds in the hyporheic zone. *Water Research, 140*, 158–166. https://doi.org/10.1016/j.watres.2018.04.040

Schaper, J. L., Posselt, M., Bouchez, C., Jaeger, A., Nützmann, G., Putschew, A., Singer, G., & Lewandowski, J. (2019). Fate of trace organic compounds in the hyporheic zone: Influence of retardation, the benthic biolayer, and organic carbon. *Environmental Science & Technology, 53*(8), 4224–4234. https://doi.org/10.1021/acs.est.8b06231

Waldschläger, K., & Schüttrumpf, H. (2020). Infiltration behavior of microplastic particles with different densities, sizes, and shapes — from glass spheres to natural sediments. *Environmental Science & Technology, 54*(15), 9366–9373. https://doi.org/10.1021/acs.est.0c01722

**Retained from v1.0 for nitrate reinstatement:** Pittroff, M., Frei, S., & Gilfedder, B. S. (2017). *Water Resources Research, 53*(1), 563–579. https://doi.org/10.1002/2016WR018917 · Frei, S., Durejka, S., Le Lay, H., Thomas, Z., & Gilfedder, B. S. (2019). *Water Resources Research, 55*(11), 9808–9825. https://doi.org/10.1029/2019WR025540

---

## 10. Bottom line

- **Zinc at `k = 83.52 d⁻¹`** is the primary dissolved endpoint, gated on circumneutral pH, Mn-oxide formation, and residence times near <2–80 min. Not permanent — label as dissolved-phase attenuation.
- **Calibrate against effective passes**, not forward-propagated `k`: roughly 1–1.4 passes over ~5 km reproduces Fuller & Harvey's 45% / 38% zinc load decrease.
- **Trace organics** span 0.30–2.52 d⁻¹ (acesulfame, flume) and ~0 to 166 d⁻¹ (in-situ river). Keep the recalcitrant compounds selectable.
- **Cr(VI)** is a derived sensitivity scenario only.
- **Microplastics are distance-based, and the distance that matters is not the one you would expect.** Retention within the bed follows `exp(−λ_f·L)` with `λ_f` = 0.18–1.0 cm⁻¹, which saturates by about 10 cm of flowpath. Report Tier A (`α_MP` = 0.0513 km⁻¹ on stream distance) and use Tier B as a size-exclusion capability check. Never mix the two coefficients.
- **Display `Da` and `Λ` everywhere.** They tell the user whether the reach is exchange-limited, whether it is long enough to matter, and whether the tool is parameterized sensibly.
