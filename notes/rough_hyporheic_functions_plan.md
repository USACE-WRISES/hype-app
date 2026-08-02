# Hyporheic Exchange App: Plan for Incorporating Hyporheic Zone Functions

## Purpose

This document lays out the path forward for extending the Hyporheic Exchange app from a hydraulic metrics calculator into a tool that also estimates hyporheic zone functions. It captures the design decisions, the reporting structure, the manager decision framework, and how the whole thing feeds the journal paper. It is meant to be a reference you can return to as you build.

## Current State of the App

The app currently predicts three key hydraulic metrics for the hyporheic zone, all derived from the MODFLOW / MODPATH / FloPy flow-path solution:

1. **Frequency**: how often water interacts with the hyporheic zone.
2. **Duration**: residence time of water within the hyporheic zone.
3. **Volume / Area**: the physical extent of the hyporheic zone.

These metrics are the raw material. Each one weighs into hyporheic functions in a different way, and the plan below is about turning them into functional estimates.

## The Core Decision: A Tiered Functions Approach

We do not ignore functions and leave them for the paper, and we do not try to build a fully calibrated modeling suite either. Instead the app gains a **Hyporheic Functions** capability with two distinct modes:

- **Screening**: fast, flow-path-based, first-order calculations built directly on the hydraulic metrics already produced. Zero friction to run. This is the flagship contribution.
- **Detailed**: user-supplied contaminant transport (or temperature) models run on top of the hydraulic solution. The user provides concentrations, transformation rates, and boundary conditions. Outputs are uncalibrated by default.

Denitrification (nitrate removal) is the first function built out as a worked example. Other functions follow the same structure later (see Function Roadmap).

## Architecture and Interface Structure

The left-hand layer panel gains a new **Hyporheic Functions** layer. Under it sit two rows (sublayers):

```
Hyporheic Functions
├── Screening      (flow-path first-order calculations, feeds the screening report)
└── Detailed       (build, parameterize, and run a detailed transport/temperature model)
```

- The **Screening** row walks the user through the flow-path-based calculations and automatically populates a screening report.
- The **Detailed** row lets the user build, parameterize, and run a detailed model, gated by an acknowledgment and guardrail step (described below).

## The Conceptual Wall Between Screening and Detailed

Both modes live inside the app. The wall between them is conceptual, not physical. The point is that the two modes make very different promises to the user, and the interface should enforce that difference.

**Screening mode design principles:**

- One action to run. Get a first-order estimate immediately.
- Every assumption stated openly in the output and in the screening report.
- Fully generalizable and defensible. No field data required.

**Detailed mode design principles:**

- The user supplies all inputs: concentrations, reaction and transformation rates, boundary conditions.
- The user runs a full transport or temperature model layered on the hydraulic solution.
- Outputs are **uncalibrated by default**. The interface should require the user to actively acknowledge that they own the inputs and the calibration, and outputs should carry an "uncalibrated" label until the user confirms calibration against field data.

**Why the wall matters:** if the two modes sit side by side and look equally polished, a user can run the detailed model uncalibrated and treat that number as truth. The guardrails are not just a disclaimer in the documentation. They should live in the interface itself, as a required assumptions or acknowledgment step, and as output labeling.

## Screening Output: The Reporting Chain for Nitrate Removal

For denitrification, the screening mode reports a chain of four metrics. These are not competing numbers. They are links in a chain, and each serves a different reader. Reporting all four keeps the calculation transparent so a reviewer can trace the logic from intensity all the way to total impact.

### 1. Removal Efficiency (load-based)

The fraction of incoming nitrate mass removed as water transits a hyporheic flow path. Along a single flow path, water enters the subsurface with an initial nitrate concentration, spends its residence time undergoing denitrification, and returns to the channel with a lower concentration. The fraction removed is that path's efficiency.

This is where efficiency physically lives, because residence time (duration) is what drives the removal, and residence time is a property of the flow path. It is **not** an area-based quantity. Aggregated across all flow paths, this becomes a system removal efficiency: total nitrate mass removed divided by total nitrate mass that entered the hyporheic zone.

This is the scientifically honest number.

### 2. Areal Removal Rate (flux)

Mass of nitrate removed per unit streambed area per unit time. This is a flux, and it represents the **intensity** of the process. It answers how hard denitrification is working per unit of sediment-water interface.

This is the standard reporting unit across the hyporheic and biogeochemistry literature, which means reporting it makes your results directly comparable to published values.

### 3. Hyporheic Streambed Area

The two-dimensional extent of the sediment-water interface participating in hyporheic exchange across the reach. This is the **scaling variable**. Reporting it explicitly makes the jump from intensity to total impact transparent, which is also good defense in a paper. Nobody can accuse you of hiding the scaling step.

### 4. Total Mass Removed per Time

The product of the areal removal rate and the hyporheic streambed area. This is the watershed-scale currency, expressed in mass of nitrogen per unit time (for example, kg N per day across the reach). It is the number that plugs directly into TMDL accounting and nutrient reduction targets, and the number a manager acts on.

### The Chain Relationship

```
Total Mass Removed per Time  =  Areal Removal Rate  ×  Hyporheic Streambed Area
```

Efficiency is derived from the per-path concentration drop. The last metric is just the middle two multiplied, so reporting the full chain adds no real work. It only adds transparency.

**Why a big river outweighs a small one:** total system-scale importance is captured by the total mass, not by any normalized rate. A large river matters more precisely because it has more interface. Its areal flux might be identical or even lower, but integrating that flux over a much larger area gives a larger total mass. The areal flux tells you how intense a site is. The total integrated mass tells you how much it matters at the watershed scale.

## Manager Decision Framework

The four metrics each have a job. The table below is the translation layer that makes the output usable for real decisions. This is worth including in both the app and the paper.

| Decision context | Primary metric to use | Reasoning |
|---|---|---|
| Prioritize rivers to **preserve** | Total mass removed per time | You are protecting existing function. You want the sites doing the most actual work for the watershed right now. Biggest absolute contribution wins. |
| Prioritize rivers for **restoration** | Areal removal rate, read as underperformance relative to available area | You are hunting for headroom. A site with large streambed area but a weak areal flux has the most room to gain from restoration. Total mass alone would mislead here, because a healthy big river looks attractive but has little room to improve. |
| Compare **restoration alternatives** at one site | Total mass removed, before versus after | Same site, so area and efficiency are both in play. The delta in total mass is your benefit metric for a cost-benefit comparison. |
| **Regulatory** decision (for example, TMDL) | Total mass removed per time | TMDLs are written in mass load currency (mass N per day). This is the only number that plugs directly into the accounting. |

## Framing: Nutrient Cycling vs Pollutant Buffering

Denitrification sits in both categories, and that is an advantage to lean on.

- **Mechanism**: it is a nutrient cycling process, the microbial conversion of nitrate to nitrogen gas.
- **Management benefit**: it delivers pollutant buffering and a water quality improvement, by removing excess nitrate load from the system.

Frame it as **nutrient cycling that delivers a water quality benefit**. This dual framing is rhetorically strong because it names the mechanism and the payoff in one move.

## How This Feeds the Journal Paper

**The screening approach is the backbone of the paper, not the detailed models.** The flow-path residence time to denitrification linkage is the novel, generalizable, publishable contribution, because other people can apply the method. The detailed calibrated modeling serves as validation or a case study, showing the screening estimates land in the right ballpark.

**Narrative arc:**

1. Present a hydraulic model that predicts frequency, duration, and volume for the hyporheic zone.
2. Show how those hydraulic metrics map to function, with residence time driving denitrification along flow paths.
3. Provide evidence that the mapping holds up against detailed contaminant transport modeling.

**Validation partner:** colleagues at Texas State are modeling denitrification in GMS using MT3D-MS. Lock in one of their datasets as the validation case for the paper.

## Function Roadmap (Beyond Denitrification)

The three hydraulic metrics weigh into different functions in different ways. Denitrification is the first worked example. Others to build under the same tiered (screening plus detailed) structure:

- **Stream temperature regulation (thermal buffering).** Residence time (duration) and exchange frequency drive heat exchange. A screening estimate could quantify thermal moderation along flow paths.
- **Nutrient cycling more broadly**, beyond nitrate.
- **Habitat area for hyporheic invertebrates.** Here hyporheic volume and area are the dominant metrics, because more physical habitat means more potential for organisms to live. This function leans on physical extent rather than residence time, which makes it a useful contrast to the denitrification case.

## Open Items and Next Steps

1. **Choose the first-order kinetics** for the screening denitrification calculation. The likely form is a first-order decay of nitrate concentration as a function of residence time along each flow path, governed by a rate constant. This needs to be pinned down before the screening calc is final.
2. **Lock in one Texas State denitrification dataset** (GMS / MT3D-MS) as the validation case for the paper.
3. **Design the screening report layout** that surfaces all four output metrics (efficiency, areal rate, streambed area, total mass).
4. **Design the detailed-mode acknowledgment and guardrail step** in the interface, including the uncalibrated output labeling.
