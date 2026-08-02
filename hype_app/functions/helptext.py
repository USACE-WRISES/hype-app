"""Structured help text for the screening panes, on the EASI pattern.

WHY THIS IS A DATACLASS AND NOT A STRING. The first version of these tooltips was one prose
string per control, up to 255 characters of run-on sentence. `apps/easi` in the STAF repo solved
this already, and the fix is structural rather than cosmetic: **tooltip text is never a
paragraph**. Every fact goes in a named slot, each slot is one short sentence under a small
uppercase label, and anything quantitative becomes a key-value row instead of a clause. Measured
across EASI's twenty metrics a slot runs about 17 words and a whole card 40 to 70. Restyling a
prose string only produces a prettier wall, so the structure has to live in the data.

`validate_help` enforces the length envelope at import. EASI keeps that discipline with an
authoring comment; a check is harder to drift past.

WHAT DOES NOT GO IN A TOOLTIP. Full citations. The card carries a short source label only
("Hester et al. 2016") and `SOURCES` holds the reference, which the pane footer and the report
render as a real list where a reader can select the text and follow the DOI. Tooltips are
`pointer-events: none`, so a link inside one could not be clicked anyway.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

#: Longest a single slot may run, and longest a whole card may run, in words. Both sit just above
#: EASI's measured envelope so there is room to be precise without room to ramble.
MAX_SLOT_WORDS = 25
MAX_CARD_WORDS = 70


@dataclass(frozen=True)
class Source:
    """One reference, in fields rather than a run-on sentence.

    `short` is all a tooltip ever shows. `provenance` is the audit trail (file paths, solver
    flags) that makes a number checkable by a reviewer; it is deliberately never displayed as
    help text, because it reads as debug output to everyone else."""

    short: str
    title: str
    where: str
    url: str = ""
    provenance: str = ""

    def reference(self) -> str:
        """One formatted reference line: author-year, title, where, then the link."""
        parts = [self.short.rstrip("."), self.title.rstrip("."), self.where.rstrip(".")]
        out = ". ".join(p for p in parts if p) + "."
        return f"{out} {self.url}" if self.url else out


SOURCES: dict[str, Source] = {
    "gms_rct": Source(
        short="Reference GMS project",
        title="MT3DMS first-order reaction package, RC1 = 1.22 /day (half-life 13.6 h)",
        where="LL01096_BASE, uniform over the active domain",
        provenance=("notes/Example_GMS_Project_with_MT3DMS/LL01096_BASE_MT3DMS/LL01096_BASE.rct: "
                    "ISOTHM 0, IREACT 1 (first-order irreversible), IRCTOP 2, IGETSC 0; RC1 = "
                    "1.220000 /day across all 343,214 active cells. Confirmed three ways: the "
                    "cell-by-cell array, the per-layer listing echo, and MT3DMS printing the "
                    "reaction stability limit as 0.8197 d, which is 1/1.22."),
    ),
    "zarnetske2011": Source(
        short="Zarnetske et al. 2011",
        title=("Dynamics of nitrate production and removal as a function of residence time in the "
               "hyporheic zone"),
        where="Journal of Geophysical Research: Biogeosciences",
        url="https://doi.org/10.1029/2010JG001356",
        provenance=("Net-production-to-net-removal transition observed at 6.9 h at Drift Creek, "
                    "Oregon. Used here as the upper anchor of the oxygen consumption range: "
                    "31.0 mg/L/day is the rate that reproduces it at a stream DO of 9 mg/L."),
    ),
    "zarnetske2012": Source(
        short="Zarnetske et al. 2012",
        title=("Coupled transport and reaction kinetics control the nitrate source-sink function "
               "of hyporheic zones"),
        where="Water Resources Research 48, W11508",
        url="https://doi.org/10.1029/2012WR011894",
        provenance=("Nitrate half-saturation constant 1.64 mg/L NO3-N. Lotts and Hester (2022) "
                    "cite this same value as their reason for adopting first-order kinetics, so "
                    "it both licenses the model and bounds it."),
    ),
    "hester2016": Source(
        short="Hester et al. 2016",
        title=("Effects of inset floodplains and hyporheic exchange induced by in-stream "
               "structures on nitrate removal in a headwater stream"),
        where="Ecological Engineering 97:452-464",
        provenance=("Table 1: saturated-zone decay rate 6 /day base case, 0.6 and 36 /day "
                    "sensitivity cases; influent nitrate 1 mg/L NO3-N base case, 0.5 to 3 mg/L "
                    "range, stated verbatim as 'all NO3- concentrations expressed as N'. "
                    "Concentration range attributed to Dubrovsky et al. (2010)."),
    ),
    "lotts2022": Source(
        short="Lotts and Hester 2022",
        title=("Pipe Dreams: The Effects of Stream Bank Soil Pipes on Hyporheic Denitrification "
               "Caused by a Peak Flow Event"),
        where="Water Resources Research 58(4), e2021WR030312",
        url="https://doi.org/10.1029/2021WR030312",
        provenance=("Reuses the Hester et al. (2016) rate and concentration values. Justifies "
                    "first-order kinetics on the grounds that its nitrate sits below the Monod "
                    "half-saturation constant of a typical riparian soil."),
    ),
    "trauth2017": Source(
        short="Trauth and Fleckenstein 2017",
        title="Single discharge events increase reactive efficiency of the hyporheic zone",
        where="Water Resources Research",
        url="https://doi.org/10.1002/2016WR019488",
        provenance=("Maximum aerobic respiration rate 4.78e-1 mmol/L/day, which is 15.3 mg "
                    "O2/L/day, and half-saturation K_O2 = 6.25e-3 mmol/L (0.200 mg/L). The Monod "
                    "term at a stream DO of 9 mg/L is 0.978, so oxygen consumption is "
                    "substrate-saturated and zero order is the correct form, not a shortcut. "
                    "Tabulated in Singh et al. (2022), https://doi.org/10.1029/2021WR031407."),
    ),
    "marzadri2013": Source(
        short="Marzadri et al. 2013",
        title="Effects of stream morphodynamics on hyporheic zone thermal regime",
        where="Water Resources Research",
        url="https://doi.org/10.1002/wrcr.20199",
        provenance=("Thermal response time for gravel-bed streams, validated on Bear Valley "
                    "Creek, Idaho. The timescale already embeds conduction and exchange with the "
                    "solid matrix, so the retardation factor must NOT be applied on top of it."),
    ),
    "fogg2023": Source(
        short="Fogg et al. 2023",
        title="Thermal insulation versus capacitance",
        where="Hydrological Processes 37(9), e14973",
        url="https://doi.org/10.1002/hyp.14973",
        provenance=("Compares shade against hyporheic exchange as competing thermal controls. "
                    "Without the surface energy budget, hyporheic influence cannot be converted "
                    "into a temperature."),
    ),
    "boulton1998": Source(
        short="Boulton et al. 1998",
        title="The functional significance of the hyporheic zone in streams and rivers",
        where="Annual Review of Ecology and Systematics",
    ),
    "usgs_c1350": Source(
        short="USGS Circular 1350",
        title=("The Quality of Our Nation's Water: Nutrients in the Nation's Streams and "
               "Groundwater, 1992-2004"),
        where="Dubrovsky et al. 2010",
        url="https://pubs.usgs.gov/circ/1350/",
        provenance=("Source of the mixed-land-use nitrate range used by Hester et al. (2016). "
                    "Reports the drinking-water limit as 10 mg/L as nitrogen, and notes that "
                    "nearly 30 percent of agricultural streams exceeded it in at least one "
                    "sample."),
    ),
    "schilling2000": Source(
        short="Schilling and Libra 2000",
        title="The relationship of nitrate concentrations in streams to row crop land use in Iowa",
        where="Journal of Environmental Quality 29(6):1846-1851",
        provenance=("Mean annual 3.0 to 10.5 mg/L NO3-N in row-crop watersheds. Rule of thumb: "
                    "average annual stream nitrate is roughly 0.1 times percent row crop."),
    ),
    "vanmetre2016": Source(
        short="Van Metre et al. 2016",
        title=("High Nitrate Concentrations in Some Midwest United States Streams in 2013 after "
               "the 2012 Drought"),
        where="Journal of Environmental Quality 45(5):1696-1704",
        url="https://doi.org/10.2134/jeq2015.12.0591",
        provenance=("Midwest wadeable-stream mean 3.6 mg/L NO3-N excluding drought-recovery "
                    "anomalies. The study's high end is anomalous by design, so the "
                    "non-anomalous mean is the better typical-condition anchor."),
    ),
    "framework": Source(
        short="Assessment framework §7.5",
        title="Active hyporheic capacity is reported as potential habitat volume, never quality",
        where="Project framework document",
    ),

    # ------------------------------------------------------- the hyporheic hydraulic signature
    "framework_signature": Source(
        short="Assessment framework §5.1",
        title="Streamflow-equivalent turnover, normalized per kilometre of channel",
        where="Project framework document",
        provenance=("THE GOVERNING DEFINITION for C_1km, and the one the app computes: "
                    "C_1km = (Q_HEF / Q_stream) x (1000 / L_reach), implemented in "
                    "metrics.Connectivity.__post_init__. One turnover is one streamflow-equivalent "
                    "VOLUME exchanged, not one hyporheic-zone volume and not one completed flow "
                    "path. Only downwelling flow whose particle returned to the river is counted; "
                    "lateral-boundary exits, gaining and throughflow paths, and unresolved "
                    "particles are all excluded, with the unresolved share reported separately as "
                    "censored flow."),
    ),
    "harvey2019": Source(
        short="Harvey et al. 2019",
        title="How hydrologic connectivity regulates water quality in river corridors",
        where="U.S. Geological Survey",
        url="https://pubs.usgs.gov/publication/70205454",
        provenance=("Conceptual antecedent for expressing hyporheic exchange as a connectivity or "
                    "turnover quantity rather than a bare flux. NOT VERIFIED as the source of "
                    "HYPE's specific equation: whether Harvey and coauthors define a quantity "
                    "identical to C_1km, with the same denominator and the same pathway-inclusion "
                    "rule, has not been checked against the paper. Revision spec §25 decision 3 "
                    "records this as open. Until someone resolves it, user-facing text may say "
                    "the method is Harvey-style and must never attribute the equation itself."),
    ),

    # ------------------------------------------------------------------ pollutant endpoints
    # The screening reference below is the curated bridge between these papers and the numbers
    # this app ships. Every DERIVED rate in `pollutants.py` cites it alongside the primary paper,
    # because the conversion (and in two cases the choice between two statistics the primary
    # paper reports) is the reference document's work, not the original authors'.
    "hype_pollutant_ref": Source(
        short="HYPE pollutant screening reference v2.0",
        title="Screening-Level Hyporheic Pollutant Removal Reference",
        where="notes/HYPE_Hyporheic_Pollutant_Removal_Screening_Reference_v2.md",
        provenance=("Source of every unit conversion and rate-triple construction in "
                    "hype_app/functions/pollutants.py. §1.1 resolves the zinc rate in favour of "
                    "83.52 /day (mean of individual rate constants) over 63.16 /day (reciprocal "
                    "of the mean time constant); the two differ by the arithmetic-harmonic mean "
                    "inequality and only the former is correct input to exp(-kt). §7 supplies the "
                    "terminology table the KPI labels are generated from."),
    ),
    "fuller2000": Source(
        short="Fuller and Harvey 2000",
        title=("Reactive uptake of trace metals in the hyporheic zone of a mining-contaminated "
               "stream, Pinal Creek, Arizona"),
        where="Environmental Science and Technology 34(7):1150-1155",
        url="https://doi.org/10.1021/es990714d",
        provenance=("Mean of individual first-order rate constants, x1440 to /day: Zn 0.058 "
                    "min-1 -> 83.52 +/- 53.28; Co 0.041 -> 59.04 +/- 50.40; Ni 0.020 -> 28.80 "
                    "+/- 31.68; Mn 0.013 -> 18.72 +/- 20.16. Observed fractional uptake per pass "
                    "Zn 36 +/- 24% (range 7-92), Co 52 +/- 25, Ni 27 +/- 19, Mn 22 +/- 19. "
                    "Reference concentrations are LABORATORY starting values the authors "
                    "described as similar to reach surface water (Zn 9.2, Co 7.2, Ni 7.5 "
                    "umol/L); the paper tabulates no field-reach mean. Field calibration travel "
                    "times <2 to 80 minutes. Reach load decrease over 5.3 km: Zn 45%/38%, Co "
                    "68%/37%, Ni 12%/22%, Mn 17%/26% for 1994/1995 after correcting for "
                    "groundwater inputs."),
    ),
    "fuller2014": Source(
        short="Fuller and Bargar 2014",
        title=("Processes of zinc attenuation by biogenic manganese oxides forming in the "
               "hyporheic zone of Pinal Creek, Arizona"),
        where="Environmental Science and Technology 48(4):2165-2172",
        url="https://doi.org/10.1021/es402576f",
        provenance=("Establishes the MECHANISM behind the Fuller and Harvey rates: sorption to "
                    "microbial Mn oxides forming during hyporheic exchange, with desorption "
                    "observed as pH decreased. This is why the metals endpoints are labelled "
                    "attenuation rather than removal, why they carry an eligibility gate, and "
                    "why they must not be extrapolated past the calibration travel times: "
                    "sorption capacity is finite and the model has no breakthrough term."),
    ),
    "jaeger2021": Source(
        short="Jaeger et al. 2021",
        title=("Transformation of organic micropollutants along hyporheic flow in bedforms of "
               "river-simulating flumes"),
        where="Scientific Reports 11:13034",
        url="https://doi.org/10.1038/s41598-021-91519-2",
        provenance=("Acesulfame half-lives 6.6, 36.6, 54.4 and 55.0 h across four flumes; "
                    "k = ln2/t_half gives 2.52, 0.455, 0.306 and 0.303 /day. Shipped as the "
                    "range 0.30 to 2.52 with a GEOMETRIC central of 0.571, because the spread is "
                    "over eightfold and an arithmetic mean would sit near the top. Median "
                    "flowpath travel times 11.5, 20.1, 24.3 and 43.3 h; surface water spiked to "
                    "11.5 ug/L; River Erpe sediment diluted 1:10 with sand."),
    ),
    "schaper2018": Source(
        short="Schaper et al. 2018",
        title="The fate of polar trace organic compounds in the hyporheic zone",
        where="Water Research 140:158-166",
        url="https://doi.org/10.1016/j.watres.2018.04.040",
        provenance=("Removal rate constants significantly higher under suboxic (denitrifying) "
                    "than under anoxic (Fe and Mn reducing) conditions. Cited as the redox "
                    "dependence behind the in-situ rates, which is why a single rate per "
                    "compound is a screening value and not a site property."),
    ),
    "schaper2019": Source(
        short="Schaper et al. 2019",
        title=("Fate of trace organic compounds in the hyporheic zone: influence of retardation, "
               "the benthic biolayer, and organic carbon"),
        where="Environmental Science and Technology 53(8):4224-4234",
        url="https://doi.org/10.1021/acs.est.8b06231",
        provenance=("In-situ River Erpe, 28 compounds, 1-D reactive transport fit. Half-lives in "
                    "the TOP 10 CM: iopromide 0.1 +/- 0.01 h -> 166 /day; tramadol 3.3 +/- 0.3 h "
                    "-> 5.0 /day. Venlafaxine, O-desmethylvenlafaxine and dihydroxy-carbamazepine "
                    "reported stable, which is the one rate here that is author-reported rather "
                    "than derived. The 10 cm limit is structural: removal of biodegradable "
                    "dissolved organic matter peaks in the same layer, so these rates must not be "
                    "extrapolated over a full flowpath."),
    ),
    "grant2014": Source(
        short="Grant et al. 2014",
        title=("First-order contaminant removal in the hyporheic zone of streams: physical "
               "insights from a simple analytical model"),
        where="Environmental Science and Technology 48(19):11369-11378",
        url="https://doi.org/10.1021/es501694k",
        provenance=("Source of the processing-length reality check: 275 km for a medium sand-bed "
                    "stream at a 1.6 d contaminant half-life, and the conclusion that hyporheic "
                    "treatment outside its optimal state confers little improvement under 1 km. "
                    "A screening tool returning large benefits over sub-kilometre reaches is "
                    "misparameterized."),
    ),
    "harvey2013": Source(
        short="Harvey et al. 2013",
        title=("Hyporheic zone denitrification: controls on effective reaction depth and "
               "contribution to whole-stream mass balance"),
        where="Water Resources Research 49(10):6298-6316",
        url="https://doi.org/10.1002/wrcr.20492",
        provenance=("Sugar Creek, Indiana, 15NO3/Br/SF6 injections. Reaction was most efficient "
                    "where the Damkohler number was near 1, excluding both deep substrate-"
                    "exhausted paths and shallow paths needing repeated entries. Also the source "
                    "of the warning that the effective reactive zone differs from the full "
                    "hyporheic depth, so bulk zone size and mean residence time are the wrong "
                    "reactive volume."),
    ),
    "drummond2020": Source(
        short="Drummond et al. 2020",
        title="Significance of hyporheic exchange for predicting microplastic fate in rivers",
        where="Environmental Science and Technology Letters 7(10):727-732",
        url="https://doi.org/10.1021/acs.estlett.0c00595",
        provenance=("Hyporheic exchange, not settling, dominates delivery of small microplastics: "
                    "23% of size-density combinations exchanged faster than they settled, rising "
                    "to 42% for low-density polymers, and important below 100 um regardless of "
                    "polymer. Justifies the module's existence and its applicability limit."),
    ),
    "drummond2022": Source(
        short="Drummond et al. 2022",
        title=("Microplastic accumulation in riverbed sediment via hyporheic exchange from "
               "headwaters to mainstems"),
        where="Science Advances 8(2):eabi9305",
        url="https://doi.org/10.1126/sciadv.abi9305",
        provenance=("Long-term accumulation, defined as storage beyond roughly 317 years, "
                    "averaged about 5% of input per river kilometre for particles mainly at or "
                    "below 100 um, ranging 3 to 8% across stream classes. alpha_MP = -ln(1-0.05) "
                    "= 0.0513 /km, with 0.0305 and 0.0834 from the 3% and 8% ends. This is a "
                    "DELIVERY-AND-RETENTION limit, not a capture-efficiency limit."),
    ),
    "munz2024": Source(
        short="Munz et al. 2024",
        title=("Transport and retention of micro-polystyrene in coarse riverbed sediments: "
               "effects of flow velocity, particle and sediment sizes"),
        where="Microplastics and Nanoplastics 4:2",
        url="https://doi.org/10.1186/s43591-023-00077-z",
        provenance=("Filter coefficients 0.18 to 1.00 /cm (geometric mid 0.42), 50 cm saturated "
                    "columns, polystyrene 100-2000 um, d50 1.51 and 6.60 mm, seepage 1.8-27 m/d, "
                    "mean fit R2 0.86. Retention profiles were INDEPENDENT of flow duration "
                    "beyond about 2 pore volumes, which is the empirical basis for refusing a "
                    "time-based decay here. Maximum d_p/d50 permitting infiltration below 20 cm "
                    "was about 0.08. Profiles stop declining exponentially below a relative "
                    "abundance of 0.023, hence the 97.7% capture cap. Their Eq. 4 regression is "
                    "NOT implemented: the log base is unconfirmed and the normalization means are "
                    "not reproduced in the reference document. Dataset "
                    "https://doi.org/10.5281/zenodo.8055599."),
    ),
    "bradford2002": Source(
        short="Bradford et al. 2002",
        title="Physical factors affecting the transport and fate of colloids in saturated porous media",
        where="Water Resources Research 38(12):63-1-63-12",
        url="https://doi.org/10.1029/2002WR001340",
        provenance="Onset of mechanical straining at a particle-to-grain size ratio above 0.002.",
    ),
    "waldschlager2020": Source(
        short="Waldschlager and Schuttrumpf 2020",
        title=("Infiltration behavior of microplastic particles with different densities, sizes, "
               "and shapes, from glass spheres to natural sediments"),
        where="Environmental Science and Technology 54(15):9366-9373",
        url="https://doi.org/10.1021/acs.est.0c01722",
        provenance=("Proposed a size-exclusion ratio of 0.11 from unsaturated glass-bead "
                    "experiments. Munz et al. measured 0.08 in saturated columns and that is the "
                    "value used here; the difference is attributed partly to differing water "
                    "fluxes. Recorded so the choice between the two is visible."),
    ),
}


@dataclass(frozen=True)
class Help:
    """One tooltip, in named slots. Never a paragraph.

    Rendering is a flat stack: title, then each non-empty slot under its own uppercase label.
    `rows` is where anything quantitative belongs, so a range never has to be written as a
    sentence. `sources` are SOURCES keys and render as short labels only."""

    title: str = ""
    definition: str = ""                                    # what it is
    method: str = ""                                        # how it is computed
    rows: tuple[tuple[str, str], ...] = ()                  # key -> value
    rows_label: str = "Typical values"
    default: str = ""                                       # muted italic beside the rows label
    note: str = ""                                          # muted caveat sub-line
    sources: tuple[str, ...] = field(default_factory=tuple)

    def slots(self) -> tuple[tuple[str, str], ...]:
        """(name, prose) for every free-text slot, for the length check and for tests."""
        return tuple((n, v) for n, v in (("definition", self.definition),
                                         ("method", self.method),
                                         ("note", self.note)) if v)

    def word_count(self) -> int:
        text = " ".join([self.title, self.definition, self.method, self.default, self.note]
                        + [f"{k} {v}" for k, v in self.rows])
        return len(text.split())

    def reference_lines(self) -> list[str]:
        return [SOURCES[k].reference() for k in self.sources]


def source_labels(keys) -> str:
    """The short labels a card shows, e.g. 'Hester et al. 2016; USGS Circular 1350'."""
    return "; ".join(SOURCES[k].short for k in keys)


def format_sources(keys) -> str:
    """Full references as one string, for the results contract and the report.

    The contract carries `citation` as text, so this is the bridge between the structured
    registry and everything downstream that still expects a sentence."""
    return " ".join(SOURCES[k].reference() for k in keys)


def render_card(h: Help) -> str:
    """A `Help` as tooltip HTML, following EASI's `_metric_tip_html`.

    A flat stack of labelled sections rather than a paragraph: each slot is one short sentence
    under a small uppercase label, and anything quantitative is a key-value row. Empty slots are
    omitted, never rendered blank. Everything is escaped, and the markup is generated here rather
    than authored in the registry, so a stray angle bracket in a value cannot break the card.

    Lives beside the model rather than in `app.py` so it is importable and testable without Shiny."""
    e = html.escape
    parts = []
    if h.title:
        parts.append(f'<div class="hype-tip-title">{e(h.title)}</div>')

    def sec(label, body, default=""):
        """One labelled section. The default rides on the LABEL line, not on the first value row.

        It used to be a `float: right` span emitted ahead of the rows; because `.hype-tip-lbl` is
        `display: block`, the float attached to the next line box -- the first row -- so it sat
        beside a value instead of the label, and sat high because that row is baseline-aligned
        against a larger font. A flex row pairs the two properly."""
        head = (f'<div class="hype-tip-lblrow"><span class="hype-tip-lbl">{e(label)}</span>'
                f'<span class="hype-tip-default">{e(default)}</span></div>' if default
                else f'<span class="hype-tip-lbl">{e(label)}</span>')
        parts.append(f'<div class="hype-tip-sec">{head}{body}</div>')

    if h.definition:
        sec("Definition", e(h.definition))
    if h.method:
        sec("Method", e(h.method))
    if h.rows:
        rows = "".join(f'<div class="hype-tip-row"><span class="hype-tip-k">{e(k)}</span>'
                       f'<span class="hype-tip-v">{e(v)}</span></div>' for k, v in h.rows)
        sec(h.rows_label, rows, default=h.default)
    elif h.default:
        sec("Default", e(h.default.split(":", 1)[-1].strip()))
    if h.note:
        parts.append(f'<div class="hype-tip-sub">{e(h.note)}</div>')
    if h.sources:
        sec("Source", e(source_labels(h.sources)))
    return "".join(parts)


def flat_text(h: Help) -> str:
    """The card as one line, for `aria-label`: a screen reader cannot use the layout."""
    return " ".join(x for x in (h.title, h.definition, h.method, h.note) if x)


def validate_help(help_obj: Help, where: str) -> None:
    """Length and reference-resolution checks, run at import from `registry.validate_registry`."""
    for name, prose in help_obj.slots():
        n = len(prose.split())
        if n > MAX_SLOT_WORDS:
            raise ValueError(
                f"{where}: {name} is {n} words, over the {MAX_SLOT_WORDS}-word slot limit. "
                f"Split it into another slot or a `rows` entry rather than lengthening it.")
    total = help_obj.word_count()
    if total > MAX_CARD_WORDS:
        raise ValueError(f"{where}: card is {total} words, over the {MAX_CARD_WORDS}-word limit")
    unknown = [k for k in help_obj.sources if k not in SOURCES]
    if unknown:
        raise ValueError(f"{where}: unresolved sources {sorted(unknown)}")
    # A citation in the body is the exact failure this split exists to prevent.
    body = " ".join(p for _, p in help_obj.slots()) + " " + " ".join(
        f"{k} {v}" for k, v in help_obj.rows)
    for banned in ("http://", "https://", "doi.org"):
        if banned in body:
            raise ValueError(f"{where}: links belong in SOURCES, not in tooltip text ({banned})")


__all__ = ["Source", "Help", "SOURCES", "MAX_SLOT_WORDS", "MAX_CARD_WORDS",
           "source_labels", "format_sources", "validate_help", "render_card", "flat_text"]
