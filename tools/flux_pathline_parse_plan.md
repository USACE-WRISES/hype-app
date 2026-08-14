# Plan: flux-pathline parse — progress feedback + 10-20x speedup

Status: **IMPLEMENTED 2026-08-13.** Measured results: 56.5x on the 233 MB synthetic
benchmark (5.1 s vs 287 s flopy); live on LL00726's real 1,688 MB file: **0.6 min**
(was 30+ min), all 121,624 flux particles captured, progress lines every ~10%.
Equivalence pinned by tests/test_hz_flux_parse.py. LL00726 completed (site 33/33).
The rest of this document is the original plan, kept for reference.

## Context

After "MODPATH hz_flux_pl finished", `run_interface_pass`
(`hypetool/functions/hz_analysis.py:729-757`) reads the flux-pass pathline file to compute
two per-particle numbers the report needs: maximum penetration depth (streambed top minus
min z along the path) and travelled path length (the microplastic deep-bed-filtration
metric; comment at :741-745). It runs once per delineation, silently. Cost scales with the
pathline file size = flux particles x points per path:

| Site | Grid | Parse time |
|---|---|---|
| SS02210 | small | ~3 min |
| SS02107 | 132k RAS cells | ~15-20 min |
| LL00726 | 151k RAS cells, **1.69 GB** .mppth, O(300k) particles | 27+ min (stopped) |

Two compounding bottlenecks:
1. `_pathlines_by_pid` (:1135-1145) uses flopy's `PathlineFile(...).get_alldata()`, which
   parses the ENTIRE text file into per-particle recarrays in one gulp (Python-level
   parse, multi-GB RAM).
2. A per-particle Python loop (:747-754) then touches every recarray
   (`path_max_depth` + `_path_length` per pid).

The .mppth is plain text (verified): `MODPATH_PATHLINE_FILE 7 2` header, per-particle
blocks with 4-int sub-headers (particle id + point count), then 11-column point rows in
`0.57500000E+03` float notation.

## Change — streaming aggregator in `hypetool/functions/hz_analysis.py`

New `_flux_path_stats(pl_path, n, log) -> (min_z, length_m, seen)`:

- Read the .mppth with `pandas.read_csv(..., delim_whitespace=True, header=None,
  names=[c0..c10], chunksize=~5M rows, skiprows=3)`. Block sub-header rows (4 columns)
  parse NaN-padded; detect them (`col4` NaN), take the particle id from them,
  forward-fill onto the point rows. **Confirm the sub-header/point column layout against
  flopy's mp7 `PathlineFile` source before coding** (which column is pid, which are
  x/y/z) — the equivalence test enforces it.
- Per chunk, vectorized: `np.minimum.at(min_z, pid, z)`; path length via consecutive
  same-pid row diffs (mirror `_path_length`'s metric exactly — read its impl) with a
  per-pid last-point carry dict across chunk AND block boundaries (a particle split over
  blocks bridges last->first point; rows are time-ordered in the file, matching the
  defensive sort at :1144).
- Progress: every >=10% of bytes or >=60 s,
  `log(f"Flux pathline parse: {pct}% ({mb:.0f} of {tot:.0f} MB, {el:.1f} min elapsed)")`.
  The `log` callback already flows to the batch site log AND the app's live run pane.
  No em dashes in user-facing strings (project copy rule).
- Swap-in at :736-754: vectorized depths (`Tarr_flat[src] - min_z`, clamped >= 0, NaN
  where unseen — exactly `path_max_depth` semantics) and lengths, deleting the
  per-particle loop. Keep the existing best-effort try/except (:758-760) and the
  "captured max penetration depth for N/n" summary line.
- `_pathlines_by_pid` stays untouched for `build_display_paths` (small files, needs full
  geometry, different consumer).

Expected: ~1-2 min/GB parse (pandas C tokenizer) + O(chunk) memory. ~10-20x. Parallel
chunk processing is possible later (min/sum are associative combines) but should wait
until a site is still slow after this change.

## Tests — equivalence is the bar (`tests/test_hz_flux_parse.py`, new)

- Synthesize small .mppth files in the real format: multiple particles, one particle
  split across two blocks, scientific-notation floats, plus a chunk-size override small
  enough to force mid-particle chunk boundaries.
- Assert `_flux_path_stats` == the flopy reference (`_pathlines_by_pid` +
  `path_max_depth` + `_path_length`) per particle (rtol ~1e-6 on length sums; depth
  exact), including NaN for particles absent from the file.
- Progress-callback test: with a tiny byte threshold, the log receives percent lines.
- Full pytest suite green.

## Optional benchmark

Generate a ~200 MB synthetic .mppth; time flopy `get_alldata` vs `_flux_path_stats`;
report the ratio. (The real file is deleted right after parsing at :737-738, so the
synthetic is the only reproducible benchmark.)

## Live verification + finishing site 33

LL00726's completed 45-min RAS solve and GW run survive on disk; only hz onward is
missing. After landing the parser:

```
.venv/Scripts/python.exe tools/site_factory/drive.py LL00726 --stages hz,results,aerials,bundle
```

The hz stage wipes/rebuilds hz_workspace + hz_dir itself (run_hz_analysis:1243-1247),
regenerates the 1.69 GB-class flux file, and must show the percent lines and a
minutes-not-half-hour parse. That completes site 33 of 33. Then the standing wrap-up:
error-class scans (zero "time limit" strings anywhere; any "stalled" verdicts
dispositioned) and the full 33-site verification table + app smoke tests.

## Files touched

- `hypetool/functions/hz_analysis.py`: `_flux_path_stats` + the :736-754 swap. Nothing else.
- `tests/test_hz_flux_parse.py` (new).
- No app.py, drive.py, or contract changes; results numerically equivalent by test.
