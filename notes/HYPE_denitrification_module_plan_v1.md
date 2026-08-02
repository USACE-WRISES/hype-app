---
title: "Denitrification in the Hyporheic Zone: Reactive Transport Module Plan"
subtitle: "An optional Hyporheic Functions branch for the HYPE application, built on MODFLOW 6 GWT"
status: "Approved design, not yet implemented"
version: "1.0"
revision_date: "2026-07-28"
scope: "Nutrient cycling and pollutant buffering (nitrate removal). Thermal regulation, habitat, and additional functions are explicitly deferred."
intended_audience:
  - "HYPE application developers"
  - "Groundwater and reactive-transport modelers"
  - "Project scientists"
---

# Denitrification in the Hyporheic Zone: Reactive Transport Module Plan

> **How to use this document.** It is written to be implemented without the conversation that
> produced it. Every claim about the existing codebase carries a file path and a line number,
> every claim about the reference GMS project carries the file and value it came from, and the
> parameter appendix is complete enough that no re-audit of the example project should be needed.

---

## 1. Purpose and outcome

HYPE currently models hyporheic **hydraulics** and reports three dimensions: Frequency of
Hyporheic Exchange, Duration in Hyporheic Zone, and Extent of Hyporheic Zone. This plan adds the
first of the four hyporheic functions, nutrient cycling and pollutant buffering, expressed as
nitrate mass removed by denitrification.

Target user workflow:

1. Run the hydraulics as today (surface water, then groundwater, then hyporheic zone).
2. Optionally run a denitrification analysis.
3. Read the pounds per day of nitrate removed, and the share of that removal attributable to the
   hyporheic zone.

Denitrification is gated on dissolved oxygen. Only suboxic or anoxic cells, defined as dissolved
oxygen below 0.1 mg/L, denitrify. Carbon is assumed abundant and non-limiting, so reactions run
to completion where the oxygen condition is met.

### 1.1 Why this is consistent with the project's own doctrine

The framework document
`notes/hyporheic_hydraulic_metrics_web_app_and_journal_paper_framework_v1_1.md` forbids
denitrification language, but only while the application is hydraulics-only:

- §3.2 (line 101) states that hydraulic modeling alone does not demonstrate denitrification.
- §10.3 (line 734) says not to label a metric as denitrification *"unless the application
  includes the additional data and validated model required to support that claim."*
- §37.8.12 (line 2297) lists "coupled reactive transport or temperature models" as named future
  work.
- §14.2 (lines 825 to 840) already specifies an unimplemented threshold library whose
  `process_name` field gives "Denitrification, thermal exchange, contaminant transformation" as
  its examples.

A real reactive-transport model is exactly the "additional model" that licenses the language.
The constraint that survives is editorial, not architectural: the three hydraulic dimensions
remain the spine of the report, and denitrification appears as a clearly separated optional
result, never as a fourth hydraulic dimension.

---

## 2. Findings from the reference project audit

The reference project is `notes/Example_GMS_Project_with_MT3DMS`, a GMS 10.9.1 project built on
MODFLOW-2005 plus MT3DMS. Two findings materially change the design.

### 2.1 The reference project does not implement the oxygen gate

The project was understood to contain a separate oxygen run whose results switch denitrification
off wherever dissolved oxygen exceeds 0.1 mg/L. The input files do not support that reading.

Evidence:

- **The nitrate decay array carries no redox zonation.** `LL01096_BASE_MT3DMS/LL01096_BASE.rct`
  has `ISOTHM 0, IREACT 1, IRCTOP 2, IGETSC 0` and writes RC1 for species 1 as a full
  cell-by-cell array. That array contains exactly two distinct values: `1.220000` in 343,214
  cells and `0.000000` in 134,186 cells. A cell-by-cell comparison of the mask `RC1 != 0` against
  `ICBUND != 0` produces **zero mismatching cells**, and the per-layer counts match as well. The
  only structure in the array is the active-domain mask.
- **Oxygen is inert and identically zero.** Species 2 is named `O2` but has `SCONC` 0, `RC1` 0,
  `RC2` 0, and no SSM source. `MT3D002.MAS` is all zeros across all 168 rows. Nothing in the run
  consumes or produces oxygen, and nothing reads it.
- **No constant-concentration trick.** `ICBUND` holds only the values 0 and 1. There are no
  negative entries, and every budget block reports `CONSTANT CONCENTRATION: 0.000000 / 0.000000`.
- **No chained run.** There is no second MT3DMS folder, no script or batch file, and no
  reaction-rate dataset inside the `.gpr`. The 3D grid dataset list contains only `elevation`.
- **The GMS conceptual model was never switched to a reaction.**
  `Map Data/GMS Conceptual Models/ConMod1/.../Reaction` reads
  `No reaction (tracer transport)`. The decay was applied grid-side, directly in the MT3D model.

What the project actually implements is **paired-species differencing**:

| Species | RC1 (1/day) | SCONC | SSM load (mg/day) | Role |
|---|---|---|---|---|
| 1 `NO3` | 1.22 | 0 | 7.0032e7 | reactive nitrate |
| 2 `O2` | 0 | 0 | 0 | placeholder, never activated |
| 3 `NO3UNREACTIVE` | 0 | 0 | 7.0032e7 | conservative twin, identical source |
| 4 `NO3_BaseFlow` | 0 | 0 | 0 | placeholder for a second nitrate source |

Species 1 and species 3 receive an identical mass load at the same cell, so the difference
between `MT3D001` and `MT3D003` is precisely the decay signal.

**Consequence for this plan.** The oxygen gate has to be designed here rather than ported. The
reference project still supplies every other parameter, and its conservative-twin method is worth
keeping as a validation device (see §5.4). No `k_O2` value exists anywhere in the project, so the
oxygen consumption rate is a genuinely new input that must be exposed and documented.

### 2.2 The reference project's concentrations are in mg per cubic foot, not mg/L

MT3DMS carries concentration as MUNIT per LUNIT cubed. That project declares `mg` and `ft`
(BTN line 4: `   d  ft  mg`), so its concentrations are mg/ft3.

Verified by mass balance. Summing `C x DELR x DELC x DZ x PRSITY` over the final UCN record and
comparing against the `.MAS` total:

| Species | Sum of C x V, treating C as mg/ft3 | `.MAS` total mass in aquifer | ratio |
|---|---|---|---|
| 3 (`NO3UNREACTIVE`) | 14,148.7 mg | 14,078 mg | 1.005 |
| 1 (`NO3`) | 2,561.8 mg | 2,548.8 mg | 1.005 |

A ratio near 1.0 rather than near 28.32 proves the interpretation. Concrete effect: the
final-step maximum for the conservative species is 0.5371 mg/ft3, which is **0.019 mg/L**, not
0.537 mg/L. Anyone reading those UCN values as mg/L has been high by a factor of 28.3168.

**Consequence for this plan.** HYPE works in metres. Choose mass units of **grams**, so
concentration in g/m3 is numerically identical to mg/L and no conversion is ever required, in the
solver, in the UI, or in the GMS export. This single choice removes the entire class of error.

### 2.3 Other properties of the reference run worth knowing

- Flow is MODFLOW-2005, **steady state**, single stress period, `40 x 77 x 155` cells, days and
  feet, CHD as the only boundary type (13,314 cells), constant-head in equals out at
  4,318,951 ft3/day with 0.00% discrepancy.
- Transport is a **pulse**, not a continuous source: stress period 1 is 0.0104167 days, which is
  exactly 15 minutes, with mass loading active; stress period 2 is 1.5 days with the source shut
  off. Total simulated time 1.5104167 days across 168 output times.
- The source is a single `ITYPE=15` mass-loading point at layer 4, row 40, column 34, at
  7.0032e7 mg/day, applied identically to species 1 and species 3.
- Result: 437,515.1 mg destroyed by reaction against 729,502.4 mg loaded, which is **59.98% of
  the load**, or 0.9646 lb removed out of 1.6083 lb loaded.
- **Decay mass appears only in the listing file.** `.MAS` has nine columns and no decay column;
  decay is lumped into `SINKS` along with boundary outflow. The explicit term
  `1ST/0TH ORDER REACTION` appears only in the per-component `CUMMULATIVE MASS BUDGETS` block of
  `LL01096_BASE.out`. This is a significant argument against MT3DMS as the compute engine.

---

## 3. Engine decision

**HYPE runs MODFLOW 6 GWT. HYPE does not run MT3DMS. The GMS exporter writes an MT3DMS dataset
so the project still opens and runs in GMS.**

### 3.1 Why MF6 GWT for compute

- **No new binary.** `bin/win/mf6.exe` and `bin/linux/mf6` are already shipped, and `mfsim.lst`
  from a real run reports version **6.6.2 (05/12/2025)**, which supports GWT and GWE. The dev
  virtual environment has flopy 3.10.0 and the desktop payload pins flopy 3.9.5
  (`desktop/payload/env.lock:99`); both expose the complete transport stack, verified present:
  `ModflowGwt`, `ModflowGwtdis`, `ModflowGwtic`, `ModflowGwtadv`, `ModflowGwtdsp`,
  `ModflowGwtmst`, `ModflowGwtssm`, `ModflowGwtsrc`, `ModflowGwtcnc`, `ModflowGwtist`,
  `ModflowGwtfmi`, `ModflowGwtoc`, and the `ModflowGwfgwt` exchange.
- **Adding a solver is expensive.** A new binary needs four touchpoints: LFS commits to
  `bin/win/` and `bin/linux/`, a new `desktop-tools-N` release zip plus a `desktop/payload/tools.lock`
  entry, the `Test-Path` assertion in `desktop/scripts/build-env-payload.ps1:156`, and possibly
  `desktop/src/Hype.Desktop.Core/Processes/AppEnvironment.cs:43-55`. Critically, ENV_VERSION is a
  hash over `tools.lock` among other files (`build-env-payload.ps1:35-52`), so **any edit there
  re-ships the roughly 450 MB environment zip**.
- **Per-cell decay budget.** The MF6 MST package writes a `DECAY-AQ` budget term as a per-cell
  array readable with `flopy.utils.CellBudgetFile`. MT3DMS reports the equivalent only as a
  domain total in a listing file. Per-cell decay is exactly what is needed to attribute removal
  to the hyporheic zone, so this is the decisive technical difference.
- **Heat transport later is nearly free.** The same flopy build carries the full GWE stack
  (`ModflowGwe`, `ModflowGweest`, `ModflowGwecnd`, `ModflowGwessm`, `ModflowGwectp`, and the
  `ModflowGwfgwe` exchange). Thermal regulation reuses this entire branch with a different model
  type rather than a new engine.

### 3.2 Why MT3DMS still gets written

The requirement is that the project open in GMS. GMS has mature MT3DMS support and limited MF6
transport support, so the export writes MT3DMS input files. This mirrors what the exporter
already does for flow: `hype_app/gms/modflow_files.py` writes a complete MF2005 dataset that HYPE
itself never executes. GMS runs MODFLOW (producing the `.hff` link file via LMT6) and then runs
MT3DMS. See §12 for the writer specification.

---

## 4. What the existing codebase already provides

These are the load-bearing facts the design depends on. All paths are relative to
`D:\Code\Work\hype-app`.

### 4.1 The flow model

Built by `hypetool/functions/my_utils.py:1226-1401` (`build_gwf_model`). Note that a second,
**dead** `build_gwf_model` exists at `hypetool/functions/model_utils.py:629-789` with no call
sites anywhere; ignore it.

- MODFLOW 6 via `flopy.mf6`, structured DIS, **steady state**, single stress period
  (`nper=1, nstp=1, perlen=1.0 day, tsmult=1.0`, hardcoded at `app.py:2279`).
- Packages present: DIS, IC, NPF, CHD (twice: `CHD_RIVER` and `CHD_SIDES`), OC. Nothing else.
  No STO, no RIV, no WEL, no RCH.
- Typical size: `nlay=24` (from `gw_mod_depth=6.0 m` over `z=0.25 m`, `app.py:2273`),
  with an observed real run at `24 x 127 x 138`, roughly 420,000 cells. Layer 0 is a wedge from
  terrain down to a flat datum; layers 1 and below are uniform slabs.
- Large arrays are externalised to binary at `model/gwf_workspace/arrays/*.bin`.
- `save_flows=True` on the model and both CHD packages; NPF has
  `save_flows=True, save_saturation=True, save_specific_discharge=True`.

Files left on disk after a run, in `model/gwf_workspace/`: `gwf_model.hds`, `gwf_model.cbb`,
`gwf_model.dis.grb`, the package files, `arrays/`, and the listings. Budget record texts actually
written, read from a real `.cbb`: `['CHD', 'DATA-SAT', 'DATA-SPDIS', 'FLOW-JA-FACE']`. That is
exactly what MF6 FMI needs.

`DATA-SPDIS` is currently **written and never read** by any code in the repository. §6.3 puts it
to use.

### 4.2 The hyporheic zone analysis

`hypetool/functions/hz_analysis.py`, entry point `run_hz_analysis()` at line 1147.

- `load_flow_model` at line 83 does `flopy.mf6.MFSimulation.load(sim_ws=...)`, which is the
  existing pattern for re-reading a finished flow model from disk.
- `read_boundary_flows` at lines 343 to 394 reads the CHD budget with
  `CellBudgetFile(..., precision="double")` and `get_data(text="CHD")`. Note the documented
  1-based to 0-based node conversion at line 380 (`n = int(node) - 1`), an off-by-one trap also
  called out at `hype_app/metrics.py:241-243`.
- `cell_class_fractions` at lines 806 to 822 returns float32 arrays of shape
  `(nlay, nrow, ncol)` keyed `hyporheic`, `losing`, `gaining`, `throughflow`, plus
  `n_classified`. Persisted at lines 1312 to 1315 as
  `summary/hz/hz_cell_fractions.npz` with keys `frac_hyporheic`, `frac_losing`, `frac_gaining`,
  `frac_throughflow`, `n_classified`.

  **This file is written and read by nothing.** It is the ready-made per-cell weight array for
  attributing mass removal to the hyporheic zone, and this feature is its first consumer.
- `cell_volumes` at lines 793 to 803 returns saturated **bulk** volume in m3, with porosity
  applied downstream at `app.py:3625` and `app.py:3798`.
- `summary/hz/hz_flux.npz` holds the flux-weighted particle ledger: per released particle
  `source_node`, `weight` in m3/day, `cls`, `time_days`, `status`, `exit_code`, `origin_code`.
  Documented at `hype_app/hz_results.py:91-103`.
- `summary/hz/hz_flow_down.geojson` and `hz_flow_up.geojson` carry per-stream-cell `q_m3d`.

### 4.3 Porosity

Already a first-class user input, and the only transport-relevant parameter that exists today.

- UI at `app.py:9039`, `input_numeric("porosity", "Porosity", value=_keep("porosity", 0.3), min=0.01, max=0.6, step=0.05)`.
- Threaded through `app.py:2276` and `app.py:5289`, declared at `hypetool/inputs.py:75`.
- Stored in `hz_stats.json["knobs"]["porosity"]` (`hz_analysis.py:1382`).
- It is a **single scalar** everywhere. There is no per-cell porosity array. It currently reaches
  MODPATH 7 only, never the flow model, through `Modpath7Bas(mp, porosity=...)` at
  `hz_analysis.py:304`.

**Dispersivity does not exist anywhere in the repository.** Zero hits for `dispersivity`,
`alphal`, `TRPT`, `TRPV`, or `dmcoef` in code, notes, or tests. It is entirely new input.

### 4.4 The optional side-branch precedent

The Sensitivity node is the pattern this feature copies:

- Tree node with `check=False` and no layers, at `hype_app/ui_tree.py:68-69`.
- Mapped through `NODE_STEP` to an existing step (`ui_tree.py:144` maps it to `_RUN`), so it adds
  **no entry to `STAGES`** and requires no edits to `_stage_states()`.
- Hidden until it has something to show, at `app.py:6523-6524`:
  ```python
  if _task_state(sens_task) == "initial" and sens_result() is None:
      hidden.add("gw.sens")
  ```
- Owns its own extended task, its own pane, and its own artifact directory.

The results contract has the matching precedent: `AssessmentResultsV2` at
`hype_app/contracts/results.py:105` carries `sensitivity: SensitivityScenarioManifest | None = None`,
`report.py:245-262` has `sensitivity_rows()` returning `[]` when it is None, and the HTML template
wraps that section in `{% if sensitivity_rows %}` at `report.py:658-666`.

---

## 5. Scientific formulation

### 5.1 Governing approach

Steady-state flow, transient solute transport run to quasi-steady, with first-order irreversible
decay. Two species solved **sequentially**, with the oxygen result freezing a spatial mask that
controls where nitrate decays.

### 5.2 Pass A, dissolved oxygen

Transport of dissolved oxygen with uniform first-order consumption representing aerobic
respiration:

```
dC_O2/dt = -div(v C_O2) + div(D grad C_O2) - k_O2 * C_O2
```

Source: stream dissolved oxygen `C_O2,stream` entering through downwelling streambed cells.
Optionally a different value at the lateral boundaries for regional groundwater, which is
typically lower.

Solve to quasi-steady, then take the concentration field.

### 5.3 The anoxic mask

```
anoxic[k,i,j] = (C_O2[k,i,j] < DO_threshold)      # default DO_threshold = 0.1 mg/L = 0.1 g/m3
```

### 5.4 Pass B, nitrate, and Pass C, the conservative twin

Nitrate transport with a **cell-by-cell decay array**:

```
decay[k,i,j] = k_denit  if anoxic[k,i,j] else 0.0
```

Pass C repeats Pass B with `decay = 0` everywhere. It costs one additional solve and buys the
reference project's own validation method: removal efficiency computed as
`1 - (reactive boundary outflow / conservative boundary outflow)` must agree with the total
`DECAY-AQ` mass. Default this on. It is the cheapest meaningful check on a multi-minute solve.

### 5.5 Assumptions that must be stated in the UI and the report

1. The oxygen field is treated as steady and independent of nitrate. There is no feedback from
   denitrification back to oxygen.
2. Carbon is non-limiting, so reactions proceed to completion wherever the oxygen condition is
   met.
3. The redox gate is a hard threshold, not a smooth inhibition function. Cells straddling the
   threshold flip discontinuously.
4. Rate constants are scenario inputs, not calibrated values.
5. Sequential decoupling of the two species is an approximation, standard in practice but an
   approximation nonetheless.

### 5.6 Independent analytic cross-check

`summary/hz/hz_flux.npz` already stores, per particle, a residence time and an m3/day flow
weight. Along a single flow path, oxygen falls as `C_O2,in * exp(-k_O2 * t)`, so the time to
reach the anoxic threshold is

```
t_lag = ln(C_O2,in / DO_threshold) / k_O2
```

and the nitrate removed on a path of residence time `T` is

```
removed_frac(T) = 1 - exp(-k_denit * max(0, T - t_lag))
```

Flux-weighting that across all returning paths gives an analytic estimate of lb/day in
milliseconds, with no solver:

```
M_removed = sum_i ( w_i * C_NO3,in * removed_frac(T_i) )     # w_i in m3/day, C in g/m3 -> g/day
```

This is roughly 50 lines and it is §13 of the framework document, already specified there. Put it
in the quality-control panel next to the mass-balance discrepancy. It is a plausibility check on
the solver, not a substitute for it: it ignores dispersion, mixing between paths, and the spatial
structure of the oxygen field.

---

## 6. Coupling the transport run to the existing flow run

**Design rule: the transport module is strictly downstream of the flow run. It reads finished
flow output from disk and never modifies, re-runs, or invalidates the flow model.**

### 6.1 FMI rather than a GWF-GWT exchange

Two options exist in MF6. A `ModflowGwfgwt` exchange solves flow and transport in one simulation,
which would mean re-solving flow on every transport run. `ModflowGwtfmi` instead reads a finished
flow budget from disk. Use FMI.

```python
flopy.mf6.ModflowGwtfmi(
    gwt,
    packagedata=[
        ("GWFBUDGET", str(gwf_ws / "gwf_model.cbb"), None),
        ("GWFHEAD",   str(gwf_ws / "gwf_model.hds"), None),
    ],
)
```

The existing budget already contains `FLOW-JA-FACE`, `CHD`, and `DATA-SAT`, which is everything
FMI requires. If the flow budget ever shows a nonzero imbalance, FMI's
`flow_imbalance_correction` option is available; the observed real run has 0.00% discrepancy, so
leave it off by default.

The transport DIS must match the flow DIS exactly, including `idomain`. Rebuild it by loading the
finished flow model with the existing `hz_analysis.load_flow_model` (`hz_analysis.py:83`) and
copying `gwf.dis` arrays, or by reading `arrays/*.bin` the way `hype_app/gms/loaders.py:90-163`
already does.

### 6.2 SRC mass loading rather than SSM auxiliary concentrations

MF6's SSM package assigns inflow concentrations to flow boundaries through an auxiliary variable
on the boundary package. The existing `CHD_RIVER` package carries only `auxiliary=["IFACE"]`
(`my_utils.py:1332`), so using SSM would require adding a second auxiliary variable and
re-running flow, which violates the design rule above.

Use the **SRC** package instead. For a cell with net inflow `q` m3/day, loading `q * C` g/day
into a cell whose boundary water arrives at zero concentration is mathematically identical to
injecting water at concentration `C`. This is precisely the device the reference project uses
(`ITYPE=15` mass loading), so it also keeps the GMS export faithful to a known-working GMS
pattern.

```python
# spd entries are [cellid, smassrate]; smassrate in g/day
spd = [[(k, i, j), q_m3d * c_g_per_m3] for (k, i, j), q_m3d in downwelling_cells.items()]
flopy.mf6.ModflowGwtsrc(gwt, stress_period_data={0: spd})
```

Apply SRC only at cells with **net inflow**. Upwelling cells must not receive loading; their
outflow correctly carries the evolved cell concentration. Per-cell inflow rates come from
`read_boundary_flows` (`hz_analysis.py:343-394`), respecting its 1-based node convention, or from
`summary/hz/hz_flow_down.geojson` which already carries `q_m3d` per stream cell.

Lateral inflow from `CHD_SIDES` gets the same treatment when regional groundwater concentrations
are nonzero. Default those to 0.0 and make them optional.

Do **not** use the CNC (constant concentration) package on stream cells. CNC pins the cell
concentration, which would destroy the signal in upwelling cells.

### 6.3 Time discretisation

Single stress period. Two decisions:

- **Length.** Default to `5 * T90`, where `T90` is the 90th-percentile flux-weighted residence
  time already computed and stored in the residence-time metrics. The app can therefore choose a
  physically appropriate horizon with no user input, and it adapts per site.
- **Step size.** `DATA-SPDIS` is already saved in every `.cbb` and currently read by nothing.
  Read it, divide by porosity to get pore velocity, and size the initial time step from a target
  Courant number, `dt <= courant * min(delr, delc) / v_max`. Optionally add `ModflowUtlats`
  adaptive time stepping so the solver can relax the step as the field stabilises.

Advection scheme defaults to `UPSTREAM`, which is unconditionally stable and matches the
reference project's `MIXELM=0`. Offer `TVD` as an accuracy option with a clear note that it is
slower and Courant-sensitive.

### 6.4 Units

| Quantity | Unit | Rationale |
|---|---|---|
| Length | m | matches the flow model |
| Time | day | matches the flow model |
| Mass | **g** | so concentration in g/m3 equals mg/L numerically |
| Concentration | g/m3 (displayed as mg/L) | no conversion anywhere |
| Mass rate | g/day, displayed as lb/day | divide by 453.59237 |

### 6.5 Sketch of the transport model builder

```python
sim = flopy.mf6.MFSimulation(sim_name="tx", exe_name=mf6_exe, sim_ws=str(tx_ws))
flopy.mf6.ModflowTdis(sim, time_units="DAYS", nper=1,
                      perioddata=[(sim_days, nstp, tsmult)])
ims = flopy.mf6.ModflowIms(sim, print_option="SUMMARY", complexity="MODERATE",
                           linear_acceleration="BICGSTAB",
                           outer_dvclose=1e-6, inner_dvclose=1e-6)

gwt = flopy.mf6.ModflowGwt(sim, modelname=name, save_flows=True)
sim.register_ims_package(ims, [gwt.name])

flopy.mf6.ModflowGwtdis(gwt, nlay=nlay, nrow=nrow, ncol=ncol, delr=delr, delc=delc,
                        top=top, botm=botm, idomain=idomain,
                        xorigin=xorigin, yorigin=yorigin)
flopy.mf6.ModflowGwtic(gwt, strt=0.0)
flopy.mf6.ModflowGwtadv(gwt, scheme=scheme)                      # "UPSTREAM" or "TVD"
flopy.mf6.ModflowGwtdsp(gwt, alh=alh, alv=alv, ath1=ath1, ath2=ath2, atv=atv, diffc=diffc)
flopy.mf6.ModflowGwtmst(gwt, porosity=porosity,
                        first_order_decay=(decay is not None), decay=decay)  # decay: 3D array or None
flopy.mf6.ModflowGwtsrc(gwt, stress_period_data={0: spd})
flopy.mf6.ModflowGwtfmi(gwt, packagedata=[("GWFBUDGET", str(cbb), None),
                                          ("GWFHEAD",   str(hds), None)])
flopy.mf6.ModflowGwtoc(gwt,
                       concentration_filerecord=[f"{name}.ucn"],
                       budget_filerecord=[f"{name}.cbb"],
                       saverecord=[("CONCENTRATION", "ALL"), ("BUDGET", "ALL")])
```

MF6 DSP takes dispersivities directly, unlike MT3DMS which takes ratios. Convert:
`alh = alv = AL`, `ath1 = ath2 = TRPT * AL`, `atv = TRPV * AL`.

---

## 7. Inputs

Porosity is reused from the existing UI. Everything else is new. Defaults convert the reference
project to metric where a reference value exists.

| Input | Symbol | Default | Unit | Provenance |
|---|---|---|---|---|
| Porosity | n | 0.3 (existing input) | - | `app.py:9039`; reference BTN `PRSITY` also 0.3 |
| Stream nitrate concentration | C_NO3,stream | none, required | mg/L | site data |
| Stream dissolved oxygen | C_O2,stream | 9.0 | mg/L | typical saturated surface water |
| Regional groundwater nitrate | C_NO3,gw | 0.0 | mg/L | optional, SRC at side inflow cells |
| Regional groundwater oxygen | C_O2,gw | 0.0 | mg/L | optional |
| Denitrification rate | k_denit | 1.22 | 1/day | reference `.rct` RC1; half-life 13.63 h |
| Oxygen consumption rate | k_O2 | none, required | 1/day | **not in the reference project** |
| Anoxic threshold | - | 0.1 | mg/L | project requirement |
| Longitudinal dispersivity | AL | 0.43 | m | reference DSP AL 1.4 ft |
| Transverse ratio, horizontal | TRPT | 0.1 | - | reference DSP |
| Transverse ratio, vertical | TRPV | 0.1 | - | reference DSP |
| Molecular diffusion | DMCOEF | 8.6e-5 | m2/day | reference DSP 9.3e-4 ft2/day |
| Advection scheme | - | Upstream | - | reference ADV `MIXELM 0` |
| Simulation length | - | `5 * T90`, computed | day | from the existing RTD |
| Target Courant number | - | 1.0 | - | reference ADV `PERCEL 1.0` |
| Initial concentration | SCONC | 0.0 | mg/L | reference BTN |
| Run conservative twin | - | on | - | reference method, §5.4 |

Two of these have no reference value and are the weakest links scientifically: `k_O2` and
`C_NO3,stream`. Both must be prominently exposed with tooltips explaining their sensitivity.

**On steady versus pulse.** The reference project injects a 15-minute pulse and tracks its fate.
HYPE should instead run a **continuous source to quasi-steady**, because the natural reporting
currency for a site assessment is a steady removal *rate* in lb/day, not the fate of one slug.
The pulse formulation belongs in a future scenario feature if it is ever wanted.

---

## 8. Outputs and mass accounting

| Output | Derivation | Display |
|---|---|---|
| Total nitrate removed | sum of `DECAY-AQ` from the Pass B budget | lb/day |
| **Hyporheic-attributed removal** | `sum(DECAY_AQ_cell * frac_hyporheic_cell)` | lb/day |
| Removal efficiency | removed / delivered load | % |
| Delivered nitrate load | sum of SRC `smassrate` | lb/day |
| Anoxic volume | `sum(cell_volume * porosity)` over anoxic cells | m3 |
| Anoxic fraction of active HZ | anoxic pore volume / active HZ pore volume | % |
| Conservative-twin check | `1 - (reactive outflow / conservative outflow)` | % |
| Analytic cross-check | §5.6 | lb/day |
| Mass-balance discrepancy | GWT listing | % |

`frac_hyporheic` comes from `summary/hz/hz_cell_fractions.npz`, written by `hz_analysis.py:1312-1315`.

Rasters written for display: dissolved oxygen concentration, nitrate concentration, and per-cell
nitrate removed.

**Reporting discipline.** Present removal as a **range** driven by the rate constants, not as a
single number. The existing sensitivity machinery is the natural vehicle. A single confident
pounds figure derived from an uncalibrated rate constant is the main way this feature could
mislead. Note that the reference project's own 1.22 /day is undocumented and unattributed
anywhere in that project, including its embedded SQLite notes database, which contains only
auto-generated GMS provenance rows.

---

## 9. User interface architecture

### 9.1 Placement: a new top-level Hyporheic Functions branch

The stage bar stays at **seven** chips. This is an optional side-branch, following the Sensitivity
precedent in every respect (see §4.4). A user who never runs a nutrient analysis sees no change
to the existing workflow.

Placing it at top level rather than inside the hyporheic-zone results subtree does two things.
It physically encodes the hydraulics-versus-function boundary the framework document insists on,
and it leaves obvious room for the remaining three functions without a second reorganisation.

New nodes in `hype_app/ui_tree.py:23-127`, inserted between the `gw` subtree and `report`:

| id | label | parent | group | check | layers |
|---|---|---|---|---|---|
| `fn` | Hyporheic Functions | None | yes | no | - |
| `fn.nut` | Nutrient cycling | `fn` | yes | yes | - |
| `fn.nut.o2` | Dissolved oxygen | `fn.nut` | no | yes | `("tx_o2",)` |
| `fn.nut.no3` | Nitrate | `fn.nut` | no | yes | `("tx_no3",)` |
| `fn.nut.rem` | Nitrate removed | `fn.nut` | no | yes | `("tx_rem",)` |

Registration details:

- `NODE_STEP` (`ui_tree.py:134-155`) maps all five ids to `STEP_RESULTS`. No `STAGES` entry, no
  `STEP_STAGE` entry, no `_stage_states()` edits, no `_reachable()` edits.
- Hidden while `hz_result() is None`, in `_push_tree_state` (`app.py:6505-6534`), alongside the
  existing `gw.sens` and `gw.run` hidden rules. The run needs both the flow solution and the
  hyporheic classification.
- `PANE_FOR_NODE` (`app.py:9802-9831`) gets entries for all five ids.
- `PREREQS` (`app.py:9835-9880`) gets a gate on `fn.nut` explaining that the hyporheic zone
  analysis must run first, with a jump target.
- `NODE_3D` (`ui_tree.py:195-212`) optionally gets `fn.nut.no3` mapped to a drape key.

### 9.2 Where controls live

Follow the `_gw_delineate_section()` split (`app.py:9190-9227`): the run button and its inputs
live in the `fn.nut` group pane, and progress plus results land on the child nodes.
`_start_tx` auto-selects `fn.nut` on launch, mirroring `_start_hz`.

### 9.3 Pane composition

Use the established vocabulary so the new pane looks native:

| Element | Helper or class | Reference |
|---|---|---|
| Readiness checklist | `_hub_row(ok, label, detail, jump)` | `app.py:9117-9126` |
| Section header | `class_="hype-subhead"` | `app.py:9197` |
| Sub-title with tooltip | `class_="hype-props-title"` + `_info_tip` | `app.py:9289`, `9600-9602` |
| Assumptions block | `class_="hype-card warn"` | `app.py:9418` |
| Numeric results table | `class_="hype-props-table"` | `app.py:9652` |
| Key-value row | `_kv(label, value)` | `app.py:9596` |
| Button row | `class_="hype-actions"` | throughout |
| Busy row | `class_="hype-busy"` + `hype-spinner` | `app.py:9362-9365` |
| Solver log | `ui.tags.pre(ui.output_text("tx_log"), class_="hype-log")` | `app.py:9232` |
| Next-step chip | `_next_hint(nid, label)` | `app.py:9793-9799` |

Two footguns to respect, both documented in-source:

1. Panes re-render on every tree selection, which resets `input_action_button` counters to zero.
   Use `_clicked_dynamic()` (`app.py:2504-2516`) or `_evt_btn()` (`app.py:2619-2627`). Never
   `@reactive.event(input.<button>)` for a button that lives inside a pane.
2. Any new pane input that must survive a remount has to be added to `_KEEP_IDS`
   (`app.py:2528-2541`) and read through `_keep()`.

### 9.4 Naming

Labels name the **function**, never the package. Nothing in the user interface says MT3DMS, GWT,
MODFLOW, or FMI. "Thermal regulation", "Pollutant buffering", and "Habitat creation" slot in as
siblings of `fn.nut` later.

No em dashes anywhere in user-facing copy: labels, tooltips, cards, notifications, or report
text.

---

## 10. Run orchestration

A `tx_task` extended task shaped exactly like `hz_task` (`app.py:5188-5227`): a **spawn child**
through `mp.get_context("spawn")` with the queue protocol, so a long solve can be hard-cancelled
by terminating the child. New module `hype_app/tx_run.py` mirrors `hype_app/hz_run.py`, which is
55 lines including its diagnostics-on-failure pattern (it tails the solver listing so a failure
explains itself before the traceback goes back over the queue).

Progress uses the existing log-marker convention rather than a progress channel: the child emits
`TX STEP n` lines, and a `_tx_poll` effect polls with `reactive.invalidate_later(0.4)` and a
regex over the last lines of the log, matching `_hz_poll` (`app.py:5307-5318`). Add a `TX_STEPS`
label dict alongside `HZ_STEPS` (`app.py:160-183`). Suggested steps:

```
0 Preparing
1 Loading the flow solution
2 Building source loading
3 Solving dissolved oxygen
4 Mapping anoxic cells
5 Solving nitrate
6 Solving conservative tracer
7 Computing mass balance and writing artifacts
```

### 10.1 Registration checklist

Each item has an existing precedent to copy.

- [ ] `_task_armed["tx"] = False` in the dict at `app.py:550-555`; set `True` immediately before
      launching, consumed at the top of `_tx_done`, cleared by `_reset_memory_state`.
- [ ] `_tx_done` completion handler following `_hz_done` (`app.py:5445-5504`): guard on status,
      consume the armed flag, handle `cancelled` and `error` separately, set the result reactive,
      clear the stale mark, call a shared `_show_tx_layers()`.
- [ ] Add `tx_task` to `_busy_tasks()` (`app.py:7805-7811`) or a work-dir rebind during Open,
      Save As, or New can orphan a running solve.
- [ ] `_cascade_clear` (`app.py:4827-4844`): add `"tx"` as a fifth entry after `"hz"` in `order`,
      add a `_drop_tx_artifacts()`, so that re-running groundwater or the hyporheic zone
      invalidates transport.
- [ ] Add `"fn.nut"` to the `clearresults` tuple at `app.py:6556-6560` and the `clear_btn` tuple
      at `app.py:6643` so the pane header gets a Clear results button.
- [ ] Clear the new reactives in `_reset_memory_state` (`app.py:7180-7254`).
- [ ] `_tree_statuses()` (`app.py:6430-6503`): add the running, error, stale, and done rules for
      `fn.nut` so the tree icon behaves like every other node.

---

## 11. Artifacts, persistence, and the report

### 11.1 Artifacts

Written to `summary/tx/`. Nesting below `summary/` means `bundle.PROJECT_DIRS` needs no change.

```
summary/tx/  tx_stats.json          headline numbers, knobs, mass balance, provenance
             tx_conc_o2.npz         dissolved oxygen field
             tx_conc_no3.npz        nitrate field, reactive
             tx_conc_no3_cons.npz   nitrate field, conservative twin
             tx_decay.npz           per-cell DECAY-AQ mass rate
             tx_anoxic.npz          boolean mask
             tx_*.tif               GeoTIFFs for the map overlays
```

New reader module `hype_app/tx_results.py` follows `hype_app/hz_results.py` exactly: takes the
directory, returns `None` or empty structures when the directory is absent, never raises.

### 11.2 Bundle

`hype_app/bundle.py`:

- One `_RESTORE_TREES` entry (lines 316 to 328), for example
  `("5_Groundwater/Results/transport/", "summary/tx/")`.
- One `_add_tree(...)` call in `zip_workspace()` near lines 252 to 253.
- One line in `_readme()` (lines 147 to 187) describing the new folder.
- `PROJECT_DIRS` unchanged, since the tree nests under `summary/`. The invariant documented at
  `bundle.py:330-333` (that `PROJECT_DIRS` is the union of first path components of the restore
  targets) is preserved, and `tests/test_project_folders.py` tests it.

### 11.3 Session state

`app.py`:

- One `_tokenize_paths(tx_result())` key in `_project_state()` (`app.py:7399-7448`). Absolute
  paths **must** go through `_tokenize_paths` or a moved or renamed project breaks; the warning
  is at `app.py:7425-7427`.
- One `_present("summary/tx/")` gated block in `_rehydrate()` (`app.py:8006-8013`) that restores
  the result reactive and calls the shared `_show_tx_layers()`.

### 11.4 Results contract

`hype_app/contracts/results.py`:

- Add a `TransportMetrics` model and the field `transport: TransportMetrics | None = None` beside
  the existing `sensitivity: ... | None = None` at line 105.
- Bump `RESULTS_SCHEMA_VERSION` from `assessment-results/2.1` to `assessment-results/2.2`
  (line 20).
- Register a migration in `hype_app/contracts/__init__.py:83-114`. `_drop_hfci_2_0` is the working
  precedent for a version bump with a registered migration.

Suggested `TransportMetrics` fields, all optional so an absent analysis serialises cleanly:
`no3_removed_lb_per_day`, `no3_removed_hz_lb_per_day`, `no3_load_lb_per_day`,
`removal_efficiency`, `anoxic_volume_m3`, `anoxic_fraction_of_hz`, `k_denit_per_day`,
`k_o2_per_day`, `do_threshold_mg_l`, `c_no3_stream_mg_l`, `c_o2_stream_mg_l`,
`mass_balance_discrepancy_pct`, `conservative_check_efficiency`, `analytic_check_lb_per_day`,
`sim_days`, `scheme`, `method_version`.

### 11.5 Report

`hype_app/report.py`:

- A `transport_rows(results)` builder returning `[]` when `results.transport is None`, mirroring
  `sensitivity_rows` (lines 245 to 262).
- A `{% if transport_rows %}` block in `_HTML_TEMPLATE` (lines 475 to 690), placed after the
  three headline dimension sections and before the inputs appendix.
- A matching guarded block in `render_pdf` (line 757 onward). **Sections must be added twice**,
  once in the Jinja template and once in the ReportLab story.
- One additional kwarg threaded through `render_html` (line 708).

**The three headline cards stay three.** Denitrification is a clearly separated optional section
titled around function. The `DIM_FREQUENCY`, `DIM_DURATION`, `DIM_EXTENT` trio at
`report.py:21-30` is the paper's spine and must not absorb a chemistry result.

---

## 12. Map and 3D display

2D display is cheap and follows the existing raster pattern. Per concentration raster:

1. A `layers=` key on the tree node (§9.1).
2. One branch in `_probe_resolve` (`app.py:5905-6005`) for the hover value chip. The `max_dim`
   passed there **must match** the overlay's own warp or hover values misregister against the
   pixels.
3. A `_set_layer(key, ImageOverlay(...))` call inside `_show_tx_layers()`. Always go through
   `_set_layer` (`app.py:569-610`), which handles the hidden-key parking dance; a widget added
   with `visible=False` renders anyway.
4. A pane opacity slider plus a `results.colorbar_datauri(...)` legend. `_pane_sw_raster`
   (`app.py:9463-9487`) is a copyable 25-line factory.

Build overlays with `results.raster_overlay(path, vmin=..., vmax=..., cmap=..., max_dim=...)`
(`hype_app/results.py:122`). Suggested ramps: viridis for oxygen and nitrate to stay consistent
with head and WSE, and a sequential warm ramp for removal so it reads as a different quantity.

For 3D, reuse `scene.drape_payload` with the same overlay PNG, exactly as head and WSE do at
`app.py:5116-5122`. A per-cell-coloured 3D volume would require a new payload kind carrying a
scalar array and lookup table in both `hype_app/scene.py` and `www/mesh3d.js`, and is out of
scope for this module.

---

## 13. GMS and MT3DMS export

Extend `hype_app/gms/` with an MT3DMS writer. The reference project is the byte-level format
target; §16 has its complete parameter dump.

### 13.1 What to write

- A new `<Name>_MT3DMS/` folder containing the `.mts` GMS super file, plus BTN, ADV, DSP, RCT,
  SSM, and GCG.
- The `.mts` super file needs `SPC` lines for each species, `MPOR`, `MLD`, and a `UNITS` card.
  Write `UNITS "m" "d" "g" "lb" "mg/l"`, using **grams with metres** so concentrations are
  numerically mg/L (see §2.2).
- Add `LMT6 4 "<Name>.lmt"` to the name file written by `gms/modflow_files.py:75-93`
  (`write_mfn`), and write the `.lmt` file itself:
  ```
  # MF2K-MT3DMS LINKER FILE
  OUTPUT_FILE_NAME <Name>.hff
  OUTPUT_FILE_HEADER standard
  OUTPUT_FILE_FORMAT unformatted
  ```
  GMS then runs MODFLOW to produce the `.hff` link file, and runs MT3DMS against it. HYPE does
  not need to run either.
- Species to write: `NO3` with the cell-by-cell gated RC1 array, `O2`, and `NO3UNREACTIVE` with
  RC1 zero. RCT header `ISOTHM 0, IREACT 1, IRCTOP 2, IGETSC 0`, matching the reference.
- DSP takes MT3DMS's ratio convention, so convert back from the MF6 dispersivities:
  `AL` as an array, `TRPT = ath1 / alh`, `TRPV = atv / alh`.

### 13.2 What to un-strip from the template

`tools/make_gms_template.py` currently removes MT3DMS from the bundled `.gpr` template:

- `set_mt3d_unused` at lines 132 to 137 sets the MT3DMS model-interface flag to 0.
- Line 149 drops `TIMT3D` tree nodes.
- Line 151 drops `TISOLUTION` nodes ending in `"(MT3DMS)"`.

Those three behaviours must be reversed for transport-enabled exports, and the tests that pin
them updated deliberately: `tests/test_gms_template.py:123` and `:139`, and
`tests/test_gms_writers.py:241`, which currently asserts `"LMT6" not in mfn`.

### 13.3 Sequencing risk

The existing MF2005 GMS export has **not yet been confirmed to open in GMS**. The pending
checkpoints are `notes/GMS_template_check` and `notes/GMS_export_check`. Adding an MT3DMS dataset
on top of an unverified flow export compounds risk in a way that is hard to debug, since a
failure could originate in either layer. **Clear the flow-export checkpoint before starting
§13.**

---

## 14. Implementation phases

**Phase 1: transport core, headless.**
Engine-side model builder, the two-pass oxygen and nitrate solve with the gated decay array,
`DECAY-AQ` extraction, hyporheic attribution via `frac_hyporheic`, the conservative twin, and
`tx_stats.json`. New `hype_app/tx_run.py` child-process wrapper. Fully testable from a script
with no user interface. **Measure runtime here before committing to the UI design.**

**Phase 2: user interface branch.**
Tree nodes, panes, `tx_task` wiring, progress polling, the registration checklist in §10.1, and
map layers.

**Phase 3: persistence and report.**
Bundle entries, `_project_state` and `_rehydrate`, the contract field plus migration, the
optional report section in both HTML and PDF, and the analytic cross-check in the quality-control
panel.

**Phase 4: GMS MT3DMS export.**
Only after the flow-export GMS checkpoint clears.

---

## 15. Risks

**Runtime is the biggest unknown.** The grid is roughly 420,000 cells. Three transient transport
solves to quasi-steady is far heavier than anything the application runs today, plausibly tens of
minutes. Mitigations available: upstream advection by default with TVD as opt-in, a simulation
length derived from T90 rather than a fixed long horizon, adaptive time stepping, and making the
conservative twin optional. Measure in Phase 1 and let the number inform the UI. Note that
restricting the transport `idomain` to the hyporheic footprint is **not** a safe optimisation,
since FMI requires the transport grid to match the flow grid.

**Parameter confidence is weak and the output looks precise.** `k_denit` and `k_O2` are poorly
constrained, and the output is a pounds-per-day figure that reads as authoritative. Present a
range driven by the rate constants, reuse the sensitivity machinery, and never imply calibration.

**Sequential decoupling is an approximation.** Solving oxygen first and freezing it ignores
feedback. Standard practice, but state it wherever the number appears.

**Scope discipline.** This is the largest single addition since the groundwater run itself.
Keeping it behind an optional, hidden-until-run branch is what protects the existing seven-step
workflow from getting heavier for users who never touch nutrients.

**Editorial creep.** The moment a denitrification number exists, there will be pressure to fold
it into the headline metrics or to relax the framework's language rules elsewhere. The rules in
§3.3 and §10.3 of the framework document still apply to every hydraulics-only output.

---

## 16. Verification

1. **Offline unit tests.** Golden tests for the gated decay array construction, the `DECAY-AQ` to
   lb/day conversion, and the hyporheic attribution weighting. Follow
   `tests/test_hz_classification.py`, which fabricates arrays directly, needs no binaries, and
   carries no marker so it always runs.
2. **Engine test with an analytic answer.** A `@pytest.mark.engine` test built on the existing
   1-D geometry in `tests/build_model_fixture.py` (`nlay=1, nrow=1, ncol=21`, `delr=delc=1 m`,
   `K=1`, CHD at both ends, with an exact analytic head and flux solution written to
   `fixture_meta.json`). A uniform decay rate over a known residence time has a closed-form
   answer, so the solver can be checked against it. Run with
   `HYPE_MODFLOW_BIN=<path> .venv/Scripts/python.exe -m pytest -m engine`.
   The marker mechanism is at `tests/conftest.py:94-108`; engine tests skip unless
   `HYPE_MODFLOW_BIN` is set.
3. **Mass-balance gate.** The GWT discrepancy must stay under a set tolerance, and the total
   `DECAY-AQ` must agree with the reactive-versus-conservative outflow difference. The reference
   project achieves -0.0065% for the reactive species and +0.0009% for the conservative one,
   which is a good target for what "good" looks like.
4. **Analytic cross-check.** Compare the solver result against the flux-weighted estimate from
   `hz_flux.npz` (§5.6). Order-of-magnitude agreement is the pass condition. Large divergence
   means dispersion or the oxygen field is doing something that deserves understanding before the
   number is trusted.
5. **End to end in the application.** Using the `hype-app-desktop` launch configuration
   (`.claude/launch.json`): open a project with completed groundwater and hyporheic-zone results,
   confirm the Hyporheic Functions branch appears, confirm it stays hidden on a project without
   those results, run the analysis, verify the map layers and hover probe, verify the pane
   numbers against `tx_stats.json`, verify the report section appears in both HTML and PDF and is
   absent when the analysis was not run, verify a save and reopen round trip restores everything,
   and verify that a Clear results cascade from Groundwater wipes the transport outputs.
6. **GMS open check (Phase 4 only).** Open the exported project in GMS, run MODFLOW to produce
   the `.hff`, run MT3DMS, and confirm the mass removed agrees with the MF6 GWT result to within
   a stated tolerance. Differences from the advection scheme and time stepping are expected;
   large differences are not.

---

## 17. Appendix A: complete reference-project parameter dump

From `notes/Example_GMS_Project_with_MT3DMS`. Retained so no re-audit is needed.

### 17.1 Flow, MODFLOW-2005

- Name file declares `LIST 2`, `DATA(BINARY) 3 .hed`, `DATA(BINARY) 40 .ccf`,
  `LMT6 4 .lmt`. Super file: `MF2K5SUP`, `MFPRECISION SINGLE`, `GMSVERS 10.9.1`.
- DIS: `NLAY 40, NROW 77, NCOL 155, NPER 1, ITMUNI 4 (days), LENUNI 1 (feet)`. `LAYCBD` 0.
  `DELR` 9.9544048 ft, `DELC` 9.9314010 ft. 11,935 cells per layer, 477,400 total,
  343,214 active and 134,186 inactive. Layer 40 bottom flat at 1294.56 ft; top range
  1309.88 to 1379.56 ft. Single stress period `1.0 1 1.0 SS`.
- BAS6: `IBOUND` in {0, 1}, `HNOFLO` -999.0.
- LPF: `ILPFCB 40, HDRY -888.0, NPLPF 0`; `LAYTYP` 1 (convertible), `LAYAVG` 0, `CHANI` -1
  (HANI array), `LAYVKA` 1 (VKA is a ratio), `LAYWET` 0. HK has exactly two values,
  13,526.219 and 14,192.36 ft/day. HANI 1.0. VANI 3.0, so Kv = Kh / 3.
- CHD: 13,314 cells, start head equals end head, auxiliary `SHEADFACT`, `EHEADFACT`, `CELLGRP`.
  Every other boundary group in the HDF5 has zero boundary conditions. No recharge, wells, or
  river.
- PCG: `25 50 1 0` then `0.01 0.01 1.0 0 0 2 1.0 0.0`.
- Budget: constant head in equals out at 4,318,951 ft3/day, 0.00% discrepancy.
  Cell summary reports 17,925 dry cells and 1,628 flooded.

### 17.2 Transport, MT3DMS

- BTN: `NLAY 40, NROW 77, NCOL 155, NPER 2, NCOMP 4, MCOMP 4`. `TUNIT d, LUNIT ft, MUNIT mg`.
  `TRNOP` T T T T T F F F F T, enabling ADV, DSP, SSM, RCT, GCG. `LAYCON` 1 for all layers.
  `DZ` is cell-by-cell for layers 1 to 12 and constant 0.5 ft for layers 13 to 40.
  `PRSITY` constant 0.3 in all layers. `ICBUND` in {0, 1} only, matching IBOUND exactly.
  `SCONC` 0.0 for all four species. `CINACT` -999.0. `SAVUCN` T, `NPRS` -1 (save every transport
  step), `CHKMAS` T, `NPRMAS` 1. Species order: `NO3`, `O2`, `NO3UNREACTIVE`, `NO3_BaseFlow`.
- Stress periods: SP1 `PERLEN 0.0104167 d (15 minutes), NSTP 10, TSMULT 1.0`;
  SP2 `PERLEN 1.5 d, NSTP 10, TSMULT 1.0`. Both `DT0 0.01, MXSTRN 100000, TTSMULT 1.0,
  TTSMAX 0.0`. Total 1.5104167 days, 168 output times.
- ADV: whole file is `0 1.0000000 1`, so `MIXELM 0` (upstream finite difference),
  `PERCEL 1.0`, `MXPART 1`.
- DSP: `AL` a full 3D array holding 1.400000 ft in active cells and 0.0 in inactive cells;
  `TRPT 0.1`, `TRPV 0.1`, `DMCOEF 9.3e-4 ft2/day` (one value shared by all species).
- RCT: `ISOTHM 0, IREACT 1, IRCTOP 2, IGETSC 0`. RC1 for species 1 is a 3D array with 1.22 /day
  in the 343,214 active cells and 0.0 elsewhere. RC1 for species 2, 3, and 4 is 0.0. RC2 is 0.0
  for all species. RHOB, PRSITY2, SRCONC, SP1, and SP2 are absent because ISOTHM is 0.
- SSM: `FWEL FDRN FRCH FEVT FRIV FGHB` all false, so no flow package carries concentration.
  `MXSS 53260`. SP1 has one record: layer 4, row 40, column 34, `ITYPE 15` (mass loading),
  `CSSMS [7.0032e7, 0.0, 7.0032e7, 0.0]` mg/day. SP2 repeats the cell with all zeros.
- GCG: `MXITER 1, ITER1 50, ISOLVE 1 (Jacobi), NCRS 0`; `ACCL 1.0, CCLOSE 1.0e-4, IPRGCG 0`.
- TOB: 33 concentration observations, 11 wells times species 1, 2, and 3, all with observed
  concentration 0.0 and weight -1, so there are no calibration targets.

### 17.3 Reference mass balance, final step

| Quantity | mg | lb |
|---|---|---|
| Mass loaded, each of NO3 and NO3UNREACTIVE | 729,502.4 | 1.6083 |
| **Destroyed by reaction, NO3** | **437,515.1** | **0.9646** |
| NO3 exiting via constant head | 289,637.7 | 0.6386 |
| NO3UNREACTIVE exiting via constant head | 715,415.8 | 1.5773 |
| NO3 remaining in aquifer | 2,548.8 | 0.0056 |
| NO3UNREACTIVE remaining in aquifer | 14,078 | 0.0310 |

Fraction of load destroyed: 59.98%. Discrepancy: -0.0065% for NO3, +0.0009% for NO3UNREACTIVE.

### 17.4 UCN binary layout

44-byte header with **no** Fortran record markers (direct or stream access), then
`NCOL * NROW` float32 values:

```
NTRANS(i4) KSTP(i4) KPER(i4) TIME2(r4) TEXT(16 char = 'CONCENTRATION   ') NCOL(i4) NROW(i4) ILAY(i4)
```

44 + 155 * 77 * 4 = 47,784 bytes per layer record, times 40 layers = 1,911,360 bytes per time
step, times 168 steps = 321,108,480 bytes per species file.

### 17.5 Files not present in the reference project

No `.mtam` (GMS 10.x uses `.mts` instead), no BCF, WEL, RIV, GHB, DRN, RCH, or EVT, no README,
notes, screenshots, or PDF, and no second MT3DMS folder. The embedded `Notes/Notes` SQLite
database inside the `.gpr` contains only auto-generated GMS provenance rows, with no commentary
on rate constants, calibration, half-lives, or the oxygen rule.

The sibling project `notes/Example_GMS_10_7_Project/LL01096_MT3DMS` is **not** an oxygen run.
It is an earlier single-species conservative tracer: 20 layers, `TRNOP T T T F T` with RCT off
and no `.rct` file at all, `MIXELM -1` (TVD), `PERLEN 1.0 d / NSTP 1`, `AL 0.0`, `TRPT 0.1`,
`TRPV 0.01`, `DMCOEF 0.0`, and six `ITYPE 1` constant-concentration cells at 500 in layer 1,
rows 41 to 43, columns 29 to 30.

---

## 18. Appendix B: code location index

| Concern | Location |
|---|---|
| Live flow model builder | `hypetool/functions/my_utils.py:1226-1401` |
| Dead flow model builder, ignore | `hypetool/functions/model_utils.py:629-789` |
| Flow run parameters from the UI | `app.py:2269-2296` |
| Load a finished flow model | `hypetool/functions/hz_analysis.py:83` |
| Read CHD budget, 1-based node trap | `hypetool/functions/hz_analysis.py:343-394` |
| Per-cell class fractions | `hypetool/functions/hz_analysis.py:806-822`, saved at `:1312-1315` |
| Cell volumes, bulk | `hypetool/functions/hz_analysis.py:793-803` |
| Particle flux ledger format | `hype_app/hz_results.py:91-103` |
| Porosity input | `app.py:9039`; threaded at `2276`, `5289`; declared `hypetool/inputs.py:75` |
| Tree node definitions | `hype_app/ui_tree.py:23-127` |
| Node to step mapping | `hype_app/ui_tree.py:134-155` |
| Stage definitions | `hype_app/ui_tree.py:174-186` |
| 3D layer keys | `hype_app/ui_tree.py:195-212` |
| Step constants | `app.py:112-114` |
| Reachability gate | `app.py:6646-6658` |
| Stage states | `app.py:6660-6695` |
| Tree statuses | `app.py:6430-6503` |
| Tree state push, hidden and disabled | `app.py:6505-6534` |
| Tree event dispatch, clearresults | `app.py:6536-6620` |
| Pane dispatch table | `app.py:9802-9831` |
| Prereq gating table | `app.py:9835-9880` |
| Props shell, Clear results button | `app.py:9889-9904` |
| Groundwater run hub | `app.py:9128-9188` |
| Optional sub-run section pattern | `app.py:9190-9227` |
| Numeric results pane pattern | `app.py:9604-9713` |
| Raster pane factory | `app.py:9463-9487` |
| Remount-proof inputs | `app.py:2528-2541`, `2556` |
| Click guards for pane buttons | `app.py:2504-2516`, `2619-2627` |
| Hyporheic zone task | `app.py:5188-5227` |
| Hyporheic zone start and done | `app.py:5250-5305`, `5445-5504` |
| Child process wrapper to copy | `hype_app/hz_run.py` |
| Task armed guards | `app.py:550-555` |
| Busy task list | `app.py:7805-7811` |
| Cascade invalidation | `app.py:4827-4844` |
| Session reset | `app.py:7180-7254` |
| Layer registry | `app.py:569-610` |
| Raster layer render reference | `app.py:719-762` |
| Hover probe resolver | `app.py:5905-6005` |
| 3D drape usage | `app.py:5116-5122` |
| Raster overlay and legend helpers | `hype_app/results.py:122`, `149`, `251` |
| Results reader module pattern | `hype_app/hz_results.py` |
| Results contract | `hype_app/contracts/results.py:20-127` |
| Migration registry | `hype_app/contracts/__init__.py:74-114` |
| Bundle dirs and version | `hype_app/bundle.py:21-23`, `300-345` |
| Bundle zip builder | `hype_app/bundle.py:190-290` |
| Session state and tokenization | `app.py:7399-7448` |
| Restore | `app.py:7887-8076`, presence gate at `8006-8013` |
| Report dimension constants | `hype_app/report.py:21-30` |
| Optional report section precedent | `hype_app/report.py:245-262`, `658-666` |
| Report HTML template | `hype_app/report.py:475-690` |
| Report PDF story | `hype_app/report.py:757+` |
| GMS export orchestrator | `hype_app/gms/export.py:39-156` |
| GMS name file writer | `hype_app/gms/modflow_files.py:75-93` |
| GMS super file writer, units card | `hype_app/gms/modflow_files.py:96-151` |
| GMS array loaders | `hype_app/gms/loaders.py:90-163` |
| GMS template MT3D stripping | `tools/make_gms_template.py:132-137`, `149`, `151` |
| MODFLOW binary resolution | `hype_app/run.py:14-31` |
| Desktop payload tool pinning | `desktop/payload/tools.lock`, `desktop/scripts/build-env-payload.ps1:35-52`, `:156` |
| Test markers and engine gate | `pytest.ini:7-11`, `tests/conftest.py:94-108` |
| Analytic 1-D fixture | `tests/build_model_fixture.py` |
| Offline post-processing test pattern | `tests/test_hz_classification.py` |
