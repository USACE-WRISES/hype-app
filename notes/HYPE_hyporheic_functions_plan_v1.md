---
title: "Hyporheic Functions: Screening and Detailed"
subtitle: "Master implementation plan for nutrient cycling and thermal regulation in the HYPE application"
status: "Proposed design, not yet implemented"
version: "1.3"
revision_date: "2026-07-28"
revision_summary: "IMPLEMENTED. The nutrient method is an oxygen gate, not a residence-time threshold input: the user supplies stream dissolved oxygen, which they can estimate, and the app derives time-to-anoxia from a cited zero-order consumption rate. Nitrate then decays first-order at 1.22/day, the value verified in the reference project. Four sections ship (Nutrient Cycling, Pollutant Removal, Habitat Creation, Temperature Regulation) behind one pane factory. v1.2's §1 and §4 are superseded; see the correction note at the head of Part I."
scope: "Denitrification and thermal regulation. Invertebrate habitat is scoped as the extent-driven contrast case but deferred."
supersedes:
  - "notes/rough_hyporheic_functions_plan.md (fully incorporated)"
  - "notes/HYPE_denitrification_module_plan_v1.md (§9, §11.4, §11.5 deferred; §2, §5, §6, §10, §12, §13, §17 retained)"
companion: "notes/HYPE_thermal_regulation_screening_plan_v1.md (v1.1) is the thermal specification"
intended_audience:
  - "HYPE application developers"
  - "Groundwater and reactive-transport modelers"
  - "Project scientists"
---

# Hyporheic Functions: Screening and Detailed

HYPE gains a Hyporheic Functions branch with two modes, separated by a wall that lives in the
interface rather than the documentation.

- **Screening**: fast, flow-path-based, first-order calculations on the hydraulic metrics already
  produced. One action to run, no field data required. **The flagship contribution and the backbone
  of the journal paper.**
- **Detailed**: a full MODFLOW 6 transport or heat model on the hydraulic solution, with the user
  supplying concentrations, rates, and boundary conditions. Uncalibrated by default and labeled so.
  The validation and case-study instrument.

Denitrification is the first worked example. Thermal regulation follows the same structure and is
specified in the companion document.

---

# Part 0: Decisions

| # | Decision | Resolution |
|---|---|---|
| 1 | Screening kinetics | First-order **and** zero-order, selectable per process. See §2. |
| 2 | Anoxic onset parameter | Replaced by Zarnetske's **observed 6.9 h source-to-sink threshold**. See §1. |
| 3 | Sub-threshold paths | Reported as **net nitrification behavior**, not as zero. Never netted out numerically. See §1.2. |
| 4 | Reported metrics | The four-metric chain, decomposed. See §3. |
| 5 | Area denominator | `A_bed` primary, `A_active` supporting. Never mixed. See §3.2. |
| 6 | Inlet nitrate | User-entered, swept low/central/high. No service integration. |
| 7 | Function kinds | Registry carries `kind`: `residence_time` or `extent`. See §5. |
| 8 | Detailed engine | MF6 GWT/GWE, run in-app. MT3DMS written for GMS interchange, never run. See §12. |
| 9 | Results schema | **One** bump 2.1 to 2.2, one container covering both processes. See §7. |
| 10 | Report boundary | Screening enters the report. Detailed does not, until calibrated. See §11. |
| 11 | First slice | Phases 1 and 2: engine, contract, interface, report. |
| 12 | Thermal output | No degrees, no reach temperature. Opportunity only. Companion doc §10. |

---

# Part I: Scientific formulation, nutrient cycling

> **Correction, v1.3.** §1 and §4 below describe an earlier design in which the residence-time
> threshold was a user input. That was wrong: nobody can estimate an onset time. **What shipped is
> an oxygen gate.** The user supplies stream dissolved oxygen, and the app derives the onset:
>
> ```
> t_anox = (C_O2_in - C_O2_threshold) / R_O2        zero-order, so linear in the DO entered
> tau_i  = max(0, T_i - t_anox)                     per path
> f_i    = 1 - exp(-k_denit * tau_i)                first-order, as the reference project
> ```
>
> **Shipped parameters** (`hype_app/functions/registry.py`):
>
> | Parameter | Value | Source |
> |---|---|---|
> | Stream dissolved oxygen | 9.0 mg/L, user-entered | near saturation at 20 °C |
> | Anoxic threshold | 0.1 mg/L | denitrification plan §1 |
> | Oxygen consumption `R_O2` | 15.3 / 23.2 / 31.0 mg/L/day | Trauth and Fleckenstein (2017) `mumax,AR` at the low end; the rate reproducing Zarnetske's observed 6.9 h at the high end |
> | Denitrification `k_denit` | 0.61 / **1.22** / 2.44 per day | the reference project `.rct`, RC1 verified three ways |
>
> Zero-order oxygen consumption is the physically correct form, not a simplification: with
> `K_O2 = 0.200 mg/L` the Monod term at 9 mg/L is 0.978, so consumption is substrate-saturated.
> The two independent anchors bracket time-to-anoxia at 6.9 to 14.0 h, and that agreement is pinned
> by a test. Zarnetske's 6.9 h survives as the **validation anchor** rather than as an input.

## 1. The residence-time threshold (SUPERSEDED, see the correction above)

The hyporheic zone is not a nitrate sink at all residence times. [Zarnetske, Haggerty, Wondzell and
Baker (2011)](https://doi.org/10.1029/2010JG001356) injected isotopically labeled nitrate at Drift
Creek, Oregon, and found the zone switches from **net nitrate production to net nitrate removal at a
residence time of 6.9 hours**. Short paths stay oxic, nitrification dominates, and the zone is a
source. Long paths go suboxic, denitrification dominates, and the zone is a sink.

This is a better foundation than the anoxic-onset lag proposed earlier, on three counts. It is an
observed transition rather than a derived one. It removes `k_O2` entirely, a parameter the prior
denitrification plan required and for which its own audit found no reference value anywhere. And it
is directly citable in the paper.

### 1.1 Per-path removal

For returning path `i` with residence time `T_i` and flow weight `w_i` (m3/day), define the
**reactive time** as the portion of residence spent past the threshold:

```
tau_i = max(0, T_i - t_thresh)
```

Removal fraction, first-order:

```
f_i = 1 - exp(-k_denit * tau_i)
```

Removal fraction, zero-order (see §2):

```
f_i = min(1, R_0 * tau_i / C_in)
```

### 1.2 Sub-threshold flow is reported, never netted

Paths with `T_i < t_thresh` have `f_i = 0`, but they are **not inert**. Zarnetske's result is that
they are expected to be net nitrate *producers*. Two rules follow:

- **Report the flux-weighted fraction of exchange below the threshold** as a first-class output,
  labeled as expected net nitrification behavior. A reach whose exchange is overwhelmingly fast is a
  potential nitrate source, and a report that shows only removal would hide that entirely.
- **Do not compute or subtract a production mass.** That needs a cited nitrification rate, which this
  plan does not have. Report the fraction and its interpretation, not a negative number.

This is computed with the existing `exceedance_fraction` (`metrics.py:179`): the sub-threshold
fraction is `1 - exceedance_fraction(T, w, t_thresh)`.

## 2. Two kinetic forms, both offered

The literature does not speak with one voice here, and the plan should not pretend otherwise.

**First-order** decay is what the reference GMS project uses (`RC1 = 1.22/day`, a 13.6 h half-life,
undocumented and unattributed even in its source project). It is the right form when nitrate is the
limiting substrate, which is the low-concentration case.

**Zero-order** is how much of the hyporheic denitrification literature actually reports rates, in
volumetric units of mg N L^-1 h^-1. It is the right form when denitrification is substrate-saturated,
which is common in nutrient-enriched agricultural settings, precisely the settings where a removal
credit matters most.

Carry `kinetics` on the process spec and offer both. Both are one line, both flux-weight identically,
and the choice is a stated assumption rather than a hidden one. Where a site has high nitrate,
first-order will overestimate removal at long residence times, and saying so in the tooltip is worth
more than silently picking one.

## 3. The four-metric chain

### 3.1 The decomposition

Four reported metrics, which collapse into a decomposition rather than four separate calculations.

**Efficiency** is the flux-weighted per-path removal fraction:

```
E = sum_i (w_i * f_i) / sum_i (w_i)
```

When inlet concentration is uniform this is identically the load-based efficiency, mass removed over
mass entering, because `C_in` cancels. It lives on the flow path, which is correct: residence time
drives removal, and residence time is a path property, not an area property.

**Total mass** follows from the path integral:

```
M = sum_i (w_i * C_in * f_i) = Q_HEF * C_in * E          [g/day]
```

**And the areal rate therefore decomposes into three interpretable factors:**

```
r = M / A_bed = q_HEF * C_in * E
```

```
areal removal rate  =  q_HEF      x   C_in     x   E
                       (m/day)        (g/m3)       (fraction)

total mass removed  =  areal rate x   A_bed
                       (g/m2/day)     (m2)
```

A reviewer can check each factor independently: exchange intensity is hydraulics, inlet concentration
is a stated input, efficiency is the kinetics. `g m^-2 day^-1` is the standard unit in the hyporheic
and biogeochemistry literature, so the middle number is directly comparable to published values.

**Everything on the right already exists.** `q_HEF` is `ConnectivityMetrics.exchange_flux_m_day`
(`assess.py:109-111`), `Q_HEF` is `returning_hyporheic_cms`, `A_bed` is `streambed_area_m2`,
`A_active` is `active_streambed_area_m2` (`contracts/results.py:38-42`). Only `E` is new.

**On scale.** `M = Q_HEF * C_in * E` scales with *exchange flow*, not directly with area. Area enters
through `q_HEF = Q_HEF / A_bed`. A large river matters more because it exchanges more water, and it
usually exchanges more water because it has more interface. Keep that framing in the report, but do
not imply area alone drives total mass.

### 3.2 Area denominator

`Total = rate x area` closes only if both use the same basis, and `A_bed` (total modeled streambed)
and `A_active` (the portion in returning exchange) are both defensible.

**Lock `A_bed` as primary.** `q_HEF` is already on that basis, reach-scale literature rates are
normally per total bed area, and `D_HZ` already uses it (`assess.py:142`), so the whole report stays
on one convention. Report the `A_active` version as a supporting value labeled **intensity where
exchange occurs**.

**Never mix them.** Add a QC check beside the threshold-monotonicity test (`validate.py:55-66`)
asserting that `r * A` reproduces `M` to floating tolerance and that the recorded basis string matches
the area used. Framework §4.6 makes exactly this mistake its named failure mode for volumes.

### 3.3 Returning paths only

`M` is computed on returning paths (`cls == 1`) only, which is how the data already arrives
(`app.py:3497-3505`). Water that downwells and leaves the domain removes nitrate from the stream
whether or not it denitrifies, so counting it as denitrification removal would double-count a purely
hydraulic loss. Report non-returning and censored fractions alongside, per framework §6.6.

## 4. Researched parameter values

Values proposed for review, with verification status stated. Low and high are **factor-of-two
sensitivity bounds, not confidence intervals**, following the convention the thermal companion sets.

### 4.1 Residence-time threshold, `t_thresh`

| | Value | Unit |
|---|---|---|
| Low | 3.5 | h |
| **Central** | **6.9** | **h** |
| High | 14 | h |

- **Source:** [Zarnetske et al. (2011)](https://doi.org/10.1029/2010JG001356), *JGR Biogeosciences*,
  10.1029/2010JG001356. **Verified.**
- **Type:** Observed transition.
- **Context:** Drift Creek, Oregon. Forested catchment, gravel streambed, isotopically labeled nitrate
  tracer injection with flow-path sampling.
- **Required conditions:** The threshold is a joint function of water temperature (controlling
  microbial activity and dissolved-oxygen solubility), dissolved-oxygen concentration across the zone
  (set by biological oxygen demand against advected supply), dissolved organic carbon supply and
  quality, nitrate availability, and the physical hydraulics.
- **Transferability note:** Derived at a single forested Oregon stream. Because the threshold depends
  on temperature, oxygen, carbon, and nitrate simultaneously, it should not be transferred to a
  warmer, more enriched, or finer-sediment system without checking those controls. Present it as a
  scenario and sweep it.

### 4.2 Denitrification rate above the threshold

**This parameter is not yet resolved and is the weakest link in the chain.** What is established:

- The reference GMS project uses first-order `RC1 = 1.22 /day` (13.6 h half-life). Undocumented and
  unattributed anywhere in that project, including its embedded notes database. It is a starting
  point, not an authority.
- Field hyporheic denitrification is frequently reported **zero-order**, in mg N L^-1 h^-1, with
  reported values spanning several orders of magnitude across settings. That spread is real, not
  measurement noise: rates correlate with hyporheic exchange rate, dissolved organic carbon and
  nitrate concentration, denitrifier abundance, grain surface area, and the presence of anoxic
  microzones.

**Recommendation:** ship both kinetic forms per §2, seed first-order at the reference project's value
with its provenance stated plainly as unattributed, and treat the zero-order values as requiring
full-text extraction before shipping. Two papers are the priority reads, and both are directly on
point for this method rather than merely adjacent:

- [Harvey et al. (2013)](https://doi.org/10.1002/wrcr.20492), *WRR*, "Hyporheic zone denitrification:
  Controls on effective reaction depth and contribution to whole-stream mass balance." Also the right
  reference for the Protocol 2 box-depth question in §14, since effective reaction depth is precisely
  what that protocol's geometric default is guessing at.
- [Frei et al. (2019)](https://doi.org/10.1029/2019WR025540), *WRR*, "Quantification of Hyporheic
  Nitrate Removal at the Reach Scale: Exposure Times Versus Residence Times." Directly addresses the
  `tau_i = max(0, T_i - t_thresh)` distinction this plan makes.

Also read [Zarnetske et al. (2012)](https://doi.org/10.1029/2012WR011894), *WRR*, "Coupled transport
and reaction kinetics control the nitrate source-sink function of hyporheic zones," which formalizes
the 2011 result in a Damköhler framework. That is the same framework as the `R(tau)` curve in §6, so
it is the natural citation for the paper's method section.

**Until those are read, the registry entry must not ship a central rate.** Ship the threshold, the
rate-free outputs in §6, and require the user to enter a rate with an explicit provenance field.

### 4.3 Thermal response time

Specified in the companion document, §4.2: 8 h reference with 4 and 16 h sensitivity bounds, from
[Marzadri et al. (2013)](https://doi.org/10.1002/wrcr.20199), **verified**.

## 5. Process registry

New module `hype_app/functions/registry.py`. One entry per process; no chemistry hardcoded elsewhere.

```python
@dataclass(frozen=True)
class ProcessSpec:
    key: str                        # "denitrification", "thermal_regulation"
    display_label: str              # user-facing, no em dashes
    kind: str                       # "residence_time" | "extent"
    kinetics: str                   # "first_order" | "zero_order" | "relaxation"
    threshold_hours: tuple          # (low, central, high) source-to-sink or onset
    rate: tuple | None              # (low, central, high); 1/day, mg/L/h, or hours per kinetics
    rate_unit: str | None
    retardation: float = 1.0        # heat exchanges with the solid matrix; solutes do not
    concentration_label: str | None = None
    concentration_unit: str | None = None
    threshold_type: str = ""        # observed transition | characteristic time | model assumption
    ecosystem_context: str = ""
    required_conditions: str = ""
    citation: str = ""
    transferability_note: str = ""
```

`citation` and `transferability_note` are **required non-empty**, enforced by test. Framework §14.2:
"No process-specific threshold should be displayed without its source and transferability note."

`kind` exists from day one although only `residence_time` is implemented. The roadmap's habitat
function is extent-driven, reading `ZoneMetrics` and `A_active` rather than residence time, and
retrofitting a second kind into a timescale-only abstraction is expensive. Leaving a discriminator
unused for one release is not.

## 6. Rate-free outputs

Three outputs that stand alone even if every rate constant is disowned. These carry the paper.

- **Fraction of exchange above the threshold** = `exceedance_fraction(T, w, t_thresh)`, and its
  complement as the sub-threshold nitrification fraction per §1.2. A pure hydraulic statement gated by
  one cited transition.
- **Reactive exposure** = flux-weighted `max(0, T_i - t_thresh)`, in m3-days/day. Reaction opportunity
  with no rate constant at all.
- **`R(tau)` curve**, sweeping a characteristic reaction time across decades:
  `R(tau) = sum_i w_i (1 - exp(-T_i/tau)) / sum_i w_i`, plus the cross-site ranking. This is framework
  §13, and the inference framework §37.8.8 sanctions: "Under a longer assumed reaction timescale,
  fewer sites retain substantial functional connectivity, and the relative ranking of sites changes,"
  which it calls "more defensible than claiming that all flow longer than one or six hours
  denitrifies." **This is the version that belongs in the paper.**

---

# Part II: Contract and interface

## 7. Results contract

**One** schema bump, `assessment-results/2.1` to `2.2` (`contracts/results.py:20`), with one
registered migration in `contracts/__init__.py:83-114`. `_drop_hfci_2_0` is the working precedent.
This resolves the collision where the nutrient and thermal plans each independently claimed 2.2.

```python
class NutrientScreening(HypeModel):
    """Denitrification screening. Hydraulic opportunity under stated kinetics, never a
    calibrated measurement (framework §10.3)."""
    process_key: str = "denitrification"
    kinetics: str | None = None
    # inputs echoed for traceability
    inlet_concentration_mg_l: float | None = None
    threshold_hours: float | None = None
    rate_value: float | None = None
    rate_unit: str | None = None
    # the chain
    removal_efficiency: float | None = None                  # E
    areal_removal_rate_g_m2_day: float | None = None         # r
    reference_area_m2: float | None = None                   # A
    reference_area_basis: str | None = None                  # "total streambed" | "active streambed"
    total_removed_kg_day: float | None = None                # M
    total_removed_lb_day: float | None = None
    total_removed_low_kg_day: float | None = None
    total_removed_high_kg_day: float | None = None
    # rate-free
    fraction_above_threshold: float | None = None
    fraction_below_threshold: float | None = None            # expected net nitrification
    reactive_exposure_m3_days: float | None = None
    # supporting
    areal_rate_active_g_m2_day: float | None = None
    censored_flow_fraction: float | None = None
    # provenance
    citation: str | None = None
    transferability_note: str | None = None
    method_version: str | None = None                        # "rtd_threshold_v1"


class FunctionScreening(HypeModel):
    nutrient: NutrientScreening | None = None
    thermal: ThermalOpportunity | None = None                # companion doc §9
    # habitat: HabitatScreening | None = None                # extent-driven, later
```

On `AssessmentResultsV2`, beside `thresholds` (`results.py:117`):
`functions: FunctionScreening | None = None`. Null default means an absent analysis serializes
cleanly and old projects migrate by doing nothing.

## 8. Tree and panes

```
Hyporheic Functions                 fn        group, no check
├── Screening                       fn.scr    one action, feeds the report
│   └── Flow paths by opportunity   fn.scr.paths
└── Detailed                        fn.det    gated, uncalibrated by default
    ├── Dissolved oxygen            fn.det.o2
    ├── Nitrate                     fn.det.no3
    ├── Nitrate removed             fn.det.rem
    └── Temperature                 fn.det.temp
```

Registration follows the Sensitivity precedent exactly (`ui_tree.py:68-69`): `check=False`, and
`NODE_STEP` (`ui_tree.py:134-155`) maps every id to `STEP_RESULTS` so **no `STAGES` entry, no
`STEP_STAGE` entry, no `_stage_states()` edit, no `_reachable()` edit** is needed. The stage bar stays
at seven chips. Hide the branch while `hz_result() is None` in `_push_tree_state`
(`app.py:6505-6534`), beside the existing `gw.sens` and `gw.run` rules. Add `PANE_FOR_NODE` entries
(`app.py:9802-9831`) and a `PREREQS` gate on `fn` (`app.py:9835-9880`) pointing at the hyporheic run.

**Pane vocabulary**, so it looks native: `_hub_row(ok, label, detail, jump)` for readiness
(`app.py:9117-9126`), `class_="hype-subhead"` for section headers, `hype-props-title` plus `_info_tip`
for tooltips, `class_="hype-card warn"` for assumptions, `hype-props-table` for numbers, `_kv` for
rows, `hype-actions` for buttons, `_next_hint(nid, label)` for the next-step chip.

**Two footguns, both documented in-source.** Panes re-render on every tree selection, resetting
`input_action_button` counters to zero, so use `_clicked_dynamic()` (`app.py:2504-2516`) or
`_evt_btn()` (`app.py:2619-2627`), never `@reactive.event(input.<button>)` for a button inside a pane.
Any pane input that must survive a remount goes in `_KEEP_IDS` (`app.py:2528-2541`), read via `_keep()`.

## 9. Screening report section

Add `function_rows(results)` to `report.py` returning `[]` when `results.functions is None`,
mirroring `sensitivity_rows` (`report.py:245-262`), and a `{% if function_rows %}` block in
`_HTML_TEMPLATE` **after the three headline dimension sections and before the inputs appendix**.
**Sections must be added twice**, once in Jinja and once in the ReportLab story (`report.py:757+`).
Thread one kwarg through `render_html` (`report.py:708`).

Order:

1. **Heading and framing.** "Nutrient cycling that delivers a water quality benefit." Then the tier
   statement: screening estimate, uncalibrated, assumptions below.
2. **The threshold split**, before the chain, because it is the better-evidenced result:
   fraction of exchange above 6.9 h (expected net removal) against fraction below (expected net
   nitrification), with the Zarnetske citation inline.
3. **The chain, shown as a chain**, four rows in sequence so the derivation reads top to bottom:

   | Step | Value | Unit | What it answers |
   |---|---|---|---|
   | Removal efficiency | `E` | % | What fraction of entering nitrate is removed |
   | Areal removal rate | `r` | g N m^-2 day^-1 | How intensely the process works per unit bed |
   | Hyporheic streambed area | `A` | m2 | How much interface the reach offers |
   | Total mass removed | `M` | kg N day^-1 | What the reach contributes at watershed scale |

   With the identities printed beneath: `Total = areal rate x area`, and
   `areal rate = exchange flux x inlet concentration x efficiency`.
4. **The range** as a low-to-high band on `M`, naming the parameter corners that produced it.
5. **Manager decision framework**, §10, with this site's values slotted in.
6. **Assumptions block** in `class_="hype-card warn"`: the kinetic form and why, the threshold and its
   citation and transferability note, carbon assumed non-limiting, no coupling between processes,
   rate constants are scenario inputs and not calibrated values.
7. **Figures.** New `render_function_chain(...)` in `figures.py` beside `render_threshold_bar`
   (`figures.py:51`), plus the `R(tau)` curve. Follow the existing `_agg_pyplot` / `_png` /
   `_autocrop` helpers. The report build is off-loop behind `_REPORT_MPL_LOCK`.

Extend `run_summary_dict` (`report.py:376`) with flat per-process columns, since it is the cross-site
combining vehicle for the 5 to 10 site table.

## 10. Manager decision framework

| Decision context | Primary metric | Reasoning |
|---|---|---|
| Prioritize rivers to **preserve** | Total mass removed per time | Protecting existing function, so the sites doing the most actual work win. |
| Prioritize rivers for **restoration** | Areal removal rate, read as underperformance relative to available area | Hunting for headroom. Large area with weak areal flux has the most room to gain. Total mass alone misleads, because a healthy big river looks attractive but has little room to improve. |
| Compare **restoration alternatives** at one site | Total mass removed, before against after | Same site, so area and efficiency are both in play. The delta is the benefit metric. |
| **Regulatory** decision, for example TMDL | Total mass removed per time | TMDLs are written in mass load currency, so this is the only number that plugs into the accounting. |

Render in-app as a static table with the site's computed values inline, under the chain.

## 11. The Screening / Detailed wall

| Surface | Screening | Detailed |
|---|---|---|
| Tree node, pane, run button | Yes | Yes |
| Map layers, hover probe, 3D drape | Yes | Yes |
| Artifacts on disk, save and reopen | Yes | Yes |
| Results contract field | Yes | **No** |
| Site report section | Yes | **No**, until calibrated |
| Acknowledgment gate before running | No | **Yes, required** |
| Persistent "Uncalibrated" state chip | No | **Yes** |

Screening is generalizable and needs no field data, so it is report-grade. Detailed depends entirely
on user-supplied rates, so its outputs live on disk and on the map but do not enter the PDF. This is
what protects against the failure the prior plan names in its §15: "The moment a denitrification
number exists, there will be pressure to fold it into the headline metrics." A number that never
reaches the PDF cannot be quoted out of a PDF.

**The three headline hydraulic dimensions stay three** (`report.py:28-30`).

---

# Part III: Phase 1, the engine

Headless, fully offline-testable, no interface.

## 12. Modules

```
hype_app/functions/__init__.py
hype_app/functions/registry.py     ProcessSpec, the entries, validation
hype_app/functions/screen.py       the calculations, pure functions over arrays
```

Plus one helper in `hype_app/metrics.py`, beside `exceedance_fraction` (`metrics.py:179`), which
applies a binary `>=` indicator where this needs the same flux weighting with a smooth transform:

```python
def weighted_reaction_fraction(values, weights, *, timescale, onset=0.0) -> float:
    """Flux-weighted mean of 1 - exp(-(t - onset)/timescale), clamped at onset.

    The continuous analogue of exceedance_fraction: instead of counting flow above a
    threshold, it weights flow by how far past the threshold it goes. Monotone
    non-decreasing in 1/timescale, non-increasing in onset. NaN on empty or zero
    total weight, matching exceedance_fraction."""
```

Shared with the thermal screen, where `B_Q(tau)` is this with `onset=0`.

## 13. Data sources and traps

`hz_flux.npz` carries per-particle `time_days` and `weight` (m3/day), written at
`hz_analysis.py:1317`, already pulled into `transit_times` / `transit_weights` at
`app.py:3497-3505` filtered to `cls == 1`.

Three traps, in order of likelihood:

1. **Units.** `app.py:3503` divides weights by `DAY` into m3/s for `ExchangeAccounting`. The raw
   `fx["weight"]` is m3/day, which is what these formulas want. Getting this wrong scales every mass
   by 86400.
2. **Optional keys.** `max_depth_m` is present only when the second pathline pass succeeded
   (`hz_analysis.py:685-686`), and `origin_code` is absent in pre-four-way artifacts. Read both
   defensively.
3. **Weight identity.** `sum_i w_i` must equal `Q_HEF`. Framework §5.9 requires it. Assert it,
   because if it drifts every mass number is wrong by the same factor and nothing else catches it.

## 14. Protocol 2 bridge (phase 3, specified here for completeness)

The Chesapeake Bay Program credits baseflow hyporheic denitrification as, in essence, **(volume of a
"hyporheic box") x (a denitrification rate)**, applied to the exchanging portion of channel. The NCSU
evaluation prepared for CBP found Protocol 2 "overestimate[s] the hyporheic box based on depth," with
an average depth to confining layer of 2 feet against the assumed 5 feet.

That assumed depth is what HYPE computes from physics: `equivalent_active_depth_m`
(`assess.py:142`) is `D_HZ = V_HZ / A_bed`, and `active_streambed_fraction` is the modeled version of
"that portion of the channel." HYPE supplies the geometry, the protocol supplies the rate constant
with its expert-panel provenance and existing regulatory acceptance.

Harvey et al. (2013) on effective reaction depth (§4.2) is the scientific counterpart to the
protocol's geometric guess and should be cited alongside.

**Blocked on a reading step.** The CBP PDFs are image-based and could not be parsed, so the equation,
default box dimensions, and rate constant must be pulled verbatim from the Protocol 2 and 3 memo and
the Unified Guide before implementation. Then: `hype_app/protocol2.py` of pure functions, both credits
computed side by side since **reporting both is the point**, `Citation` reused from
`provenance.py:62-69` (already rendered at `report.py:265-291`).

---

# Part IV: Later phases

## 15. Detailed branch (phases 4 and 5)

Full specification in `HYPE_denitrification_module_plan_v1.md` §5, §6, §10, §12, which remain
accurate. The load-bearing points:

- **FMI, not a GWF-GWT exchange.** `ModflowGwtfmi` reads the finished flow budget and heads from
  disk, so transport never re-solves or invalidates flow. The existing `.cbb` holds `FLOW-JA-FACE`,
  `CHD`, and `DATA-SAT`, all FMI needs. **Do not re-derive `nlay`**: `my_utils.py:149` truncates while
  `estimate.py:31` rounds up, and they diverge on non-integer ratios. Copy `gwf.dis`.
- **SRC, not SSM.** `CHD_RIVER` carries only `auxiliary=["IFACE"]` (`my_utils.py:1332`). Load `q * C`
  at net-inflow cells only. Do **not** use CNC on stream cells; it pins concentration and destroys the
  upwelling signal.
- **Units: grams with metres**, so g/m3 is numerically mg/L. This is the fix for the prior plan's
  §2.2 finding that the reference project's values are mg/ft3, a factor of 28.3168.
- **Guardrail gate**: a modal requiring explicit acknowledgment before the first run, **recorded into
  `tx_stats.json`** so it travels with the output rather than living only in the UI session. Plus the
  persistent Uncalibrated chip, styled like the run-mode chip in `runmode.py`.
- **Orchestration**: `tx_task` shaped like `hz_task` (`app.py:5188-5227`), a spawn child via
  `mp.get_context("spawn")`, `hype_app/tx_run.py` mirroring `hz_run.py`. **Add `tx_task` to
  `_busy_tasks()` (`app.py:7805-7811`)** or a work-dir rebind during Open, Save As, or New orphans a
  running solve.
- **Measure runtime in phase 4, before designing phase 5's pane.** Three transient solves on roughly
  420,000 cells is heavier than anything the app runs today. Restricting the transport `idomain` to
  the hyporheic footprint is **not** a safe optimization, since FMI requires matching grids.

Also write `params.json`, a flopy `run.py`, and a `run.bat` beside the input files. Calibration means
dozens to hundreds of runs and possibly PEST; no GUI serves that, and the files cost nothing extra.

## 16. Engine change for the thermal mosaic

The companion document's §6 needs a return cell per path, and **the ledger does not carry one**.
`hz_analysis.py:682-686` writes `source_node` (the release cell), not the return cell. The endpoint
pass already knows the terminating cell since the returning/losing classification derives from it.
**Add `return_node` to the `per_particle` dict.** Small change, same function, and treat it as
optional on read exactly as `max_depth_m` is.

## 17. Validation with Texas State

Two designs, not equally strong.

**Weaker:** they run MT3D-MS on their own flow field, HYPE screens on its own, the numbers are
compared. Disagreement is ambiguous between flow-model differences and the screening approximation,
which is the one thing the comparison exists to isolate.

**Stronger, and the recommendation:** HYPE exports its flow solution plus a transport setup, and they
build MT3D-MS on that same flow field. Then the only differences are dispersion, spatial oxygen
structure, and numerical scheme, which is exactly the approximation being tested. That yields a
three-way comparison:

| Estimate | Source | What agreement tests |
|---|---|---|
| Screening | HYPE path calculation | The thesis of the paper |
| Detailed, MF6 GWT | HYPE, same flow field | Screening against full 3-D transport |
| Detailed, MT3D-MS | Texas State in GMS, same flow field | Independent implementation and modeler |

Order-of-magnitude agreement is the pass condition (prior plan §5.6). This makes the MT3DMS writer a
paper dependency rather than an optional extra, though it stays behind the GMS checkpoint gate:
`notes/GMS_export_check/HypeCheck_MODFLOW/HypeCheck.out` is a 181-byte HYPE-written stub, so the
existing MF2005 export has never been confirmed to open in GMS.

---

# Part V: Phasing

| Phase | Content | Blocked by | Offline testable |
|---|---|---|---|
| **1** | Registry, `screen.py`, `weighted_reaction_fraction`, contract + migration | none | Yes |
| **2** | Screening pane, tree nodes, path coloring, report section, figures | 1 | Mostly |
| 3 | Protocol 2 adapter | CBP document read | Yes |
| 4 | Transport builder, `tx_run.py`, headless. **Measure runtime.** | none | Engine marker |
| 5 | Detailed pane, guardrail gate, layers, persistence | 4 | Mostly |
| 6 | Thermal screening (companion doc phases 1 and 3) | 1 | Yes |
| 6b | Thermal mosaic (companion doc phase 2) | §16 engine change | Yes |
| 7 | MT3DMS export and Texas State validation | GMS checkpoint | Partly |

**Phases 1 and 2 are the agreed first slice.** They deliver the paper's backbone with no solver and no
runtime risk. Site runs for the paper remain the independent critical path.

---

# Part VI: Verification

Phases 1, 2, 3, and 6 are fully offline-testable. Follow `tests/test_hz_classification.py`, which
fabricates arrays directly, needs no binaries, and carries no marker so it always runs. The suite is
45 files / 422 tests; markers are `live`, `engine`, `slow` under `--strict-markers`
(`pytest.ini:8-11`); engine tests skip unless `HYPE_MODFLOW_BIN` is set (`tests/conftest.py:94-108`).

**Engine** (`tests/test_metrics.py`, new `tests/test_functions.py`), all analytic:

- Single path, `onset = 0`, first-order: equals `1 - exp(-T/tau)` exactly.
- All `T_i <= t_thresh`: `E` is exactly zero, and `fraction_below_threshold` is exactly 1.
- Monotone: non-increasing in `t_thresh`, non-decreasing in `1/tau`.
- Two paths, equal `T`, unequal `w`: result tracks the weights. The guard against particle-count
  weighting returning, which framework §4.5 names as a specific failure.
- Limits: approaches the full delivered load as `tau -> 0`, zero as `tau -> infinity`.
- Zero-order form clamps at 1 and never exceeds `C_in`.
- `fraction_above_threshold + fraction_below_threshold == 1`.
- `R(tau)` monotone decreasing in `tau`.
- **Chain closure:** `r * A` reproduces `M` to floating tolerance, and `reference_area_basis` matches
  the area used.
- **Weight identity:** `sum_i w_i` equals `Q_HEF` within tolerance.
- **Units:** one hand-checkable case where m3/day times g/m3 lands on a known kg/day and lb/day. The
  m3/day against m3/s trap at `app.py:3503` is the likeliest defect in the whole plan.

**Registry:** every entry has non-empty `citation` and `transferability_note`; every `kind` and
`kinetics` is a known value; no entry ships a central rate without provenance.

**Contract:** a 2.1 fixture migrates to 2.2 with `functions is None` and no other field changed.

**Thermal:** the companion document's §13.1 hand calculation, which is fully specified with expected
values (`B_Q = 0.6519808474` for `t = [4, 8, 24]` h, `w = [1, 2, 1]`, `tau = 8` h). Use it verbatim.

**Report boundary:** assert the Screening section appears and **the Detailed results do not**, in both
HTML and PDF. That is the tier line, so it needs a test rather than a convention. Extend
`tests/test_report.py` (261 lines).

**Language:** per the companion's §13.3, assert no report string contains "degrees of cooling",
"predicted temperature", "verified refuge", "habitat created", or "regulatory credit", and that every
function section names its assumed parameters, evidence level, resolved-return basis, censored
fraction, and lack of calibration.

**End to end:** the `hype-app-desktop` launch configuration in `.claude/launch.json`, on a project
with completed groundwater and hyporheic-zone results. Confirm the branch appears, confirm it stays
hidden without those results, run screening, verify pane numbers against the artifacts, verify the
report section in HTML and PDF and its absence when screening was not run, confirm a save and reopen
round trip, and confirm a Clear results cascade from Groundwater wipes it.

---

# Part VII: Open items

1. **Denitrification rate values.** Read Harvey et al. (2013), Frei et al. (2019), and Zarnetske et
   al. (2012) full text and extract rate constants with units and context. **Blocks shipping a central
   rate.** The threshold and all rate-free outputs are unaffected.
2. **CBP Protocol 2 numbers.** Equation, default box dimensions, rate constant, verbatim. Blocks
   phase 3.
3. **Verify three citations** in the companion document, §13.4: Arrigoni et al. (2008), Hester et al.
   (2009), Marzadri et al. (2013) *J. Hydrol.*
4. **Which Texas State dataset and site** becomes the validation case. Blocks phase 7 and the paper's
   methods.
5. **Whether the shared-flow-field validation design is feasible** with the collaborators' workflow.
   Meaningfully stronger than an independent comparison, but requires them to build on HYPE's exported
   flow field.
6. **GMS flow-export checkpoint**, open since `c81aa57`. Blocks phase 7.

## Key references

- Zarnetske, J. P., Haggerty, R., Wondzell, S. M., and Baker, M. A. (2011). Dynamics of nitrate
  production and removal as a function of residence time in the hyporheic zone. *Journal of
  Geophysical Research: Biogeosciences*. https://doi.org/10.1029/2010JG001356
- Zarnetske, J. P., Haggerty, R., Wondzell, S. M., Bokil, V. A., and González-Pinzón, R. (2012).
  Coupled transport and reaction kinetics control the nitrate source-sink function of hyporheic zones.
  *Water Resources Research*. https://doi.org/10.1029/2012WR011894
- Harvey, J. W., Böhlke, J. K., Voytek, M. A., Scott, D., and Tobias, C. R. (2013). Hyporheic zone
  denitrification: Controls on effective reaction depth and contribution to whole-stream mass balance.
  *Water Resources Research*. https://doi.org/10.1002/wrcr.20492
- Frei, S., Durejka, S., Le Lay, H., Thomas, Z., and Gilfedder, B. S. (2019). Quantification of
  hyporheic nitrate removal at the reach scale: Exposure times versus residence times. *Water
  Resources Research*. https://doi.org/10.1029/2019WR025540
- Marzadri, A., Tonina, D., and Bellin, A. (2013). Effects of stream morphodynamics on hyporheic zone
  thermal regime. *Water Resources Research*. https://doi.org/10.1002/wrcr.20199
- [A Unified Guide for Crediting Stream and Floodplain Restoration Projects in the Chesapeake Bay Watershed](https://www.chesapeakebay.net/what/publications/a-unified-guide-for-crediting-stream-and-floodplain-restoration-projects-in-the-chesapeake-bay-watershed)
- [Evaluation of Nutrient Reduction Crediting Strategies for Stream Restoration (NCSU, 2018)](https://www.chesapeakebay.net/files/documents/nutrient_credit_evaluation_final_report_ncsu_9-17-18.pdf)
