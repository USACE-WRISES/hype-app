# site_factory - the repeatable from-scratch workflow

Builds HYPE App projects headlessly for the Texas sites, driven by one editable
workbook. Nothing in here edits or monkeypatches the app: every engine call goes
through the same public functions the app itself uses, so a factory-built project
opens in HYPE Desktop as if the app had built it.

All commands run from the repo root with the project venv:

```
.venv\Scripts\python.exe tools\site_factory\<script>.py ...
```

## The pipeline, end to end

```
Sites\<id>\ (source data, 22 GB)          hype_models\_master\               hype_models\<id>\ (the project)
  RAS 2D project (.prj + g01.hdf)   -->   extracted\<id>.json          -->   inputs\  ras\  model\  summary\
  GMS project (.gpr, .hob, GIS)     -->   inputs_master.xlsx (edit me) -->   report\  aerials\  <id>.hype
  KMZ, gradient + slug workbooks
```

1. **Extract** (per-site JSONs from the source tree + legacy Analysis workbook):

   ```
   .venv\Scripts\python.exe tools\site_factory\extract.py            # all sites
   .venv\Scripts\python.exe tools\site_factory\extract.py --site LL01096
   ```

   Reads the legacy workbook's `SW Hydraulics` sheet ONLY (never `OVERALL` - its
   VLOOKUP block is off by one column), pins the column map with an LL01096
   self-test, and harvests the **observation wells** from each site's GMS
   MODFLOW `.hob` (GMSCOMMENT XY + observed head, ftUS). Well names are
   XY-matched against `TransObservation.csv`, the GIS `Spikes` points, or
   `Wells*.shp`, because the hob `hedN` order is scrambled relative to the BR
   numbering. Wells landing outside 3 km of the site KMZ centroid are kept but
   flagged `include=No` (CRS suspicion).

2. **Master workbook** (the input of record the driver reads):

   ```
   .venv\Scripts\python.exe tools\site_factory\master.py build          # first time ONLY
   .venv\Scripts\python.exe tools\site_factory\master.py refresh-wells  # safe re-run
   ```

   `build` recreates the whole workbook and **destroys every hand edit** - use it
   once, then never again casually. `refresh-wells` rewrites only the WELLS sheet
   (SITES and its live formulas untouched) and preserves the hand-editable WELLS
   columns - `name`, `include`, `screen_elev_ft` - keyed by `(site_id, obs_name)`.

   Edit in Excel afterwards: flows and K on SITES, and on WELLS fill
   `screen_elev_ft` as field data becomes available (no source dataset has screen
   elevations - without one a well still shows in the app and the report, but its
   computed head cannot be sampled). **Never hand-edit `obs_name`** - it keys the
   deterministic app well ids and the preserved edits.

3. **Drive** (build one site, resumable per stage):

   ```
   .venv\Scripts\python.exe tools\site_factory\drive.py LL01096                 # from scratch, all stages
   .venv\Scripts\python.exe tools\site_factory\drive.py LL01096 --stages gw,hz  # rerun specific stages
   .venv\Scripts\python.exe tools\site_factory\drive.py LL01096 --auto          # hash-diff minimal rerun
   ```

   Stages, in order: `geometry, terrain, ras, gw, hz, results, aerials, bundle`.
   `--auto` diffs the workbook's dependency-group hashes against
   `_provenance.json` and reruns only invalidated stages (`aerials` is never
   auto-selected; rerun it explicitly with `--stages aerials`).

## Where the wells go

- `results` stage: samples computed heads at each well via the app's own
  `hype_app.wells` functions and attaches a Groundwater Model Calibration
  section to the site report; well markers land on the head-contour site map
  and the Gate 1 `review_card.png`.
- `bundle` stage: WELLS rows with `include=Yes` and coordinates become
  observation wells in the project's `.hype` (heads and screen elevations
  convert ftUS to meters with the US survey foot). Well ids are deterministic
  (`sha1(site_id|obs_name)`), so re-running the bundle merges instead of
  duplicating.

## Aerials

The `aerials` stage copies the site's NAIP GeoTIFFs from the source tree into
`<project>\aerials\` (copy-if-absent) and the `bundle` stage registers every
raster in that folder as an unchecked Map layers row in the app. Drop extra
rasters in the folder and re-run `--stages bundle` to register them. The folder
travels with the app's Save As but is never packed into `.hype` archives.

## Re-running bundle is safe

`stage_bundle` reads the existing `.hype` first and merges: every app-authored
state key passes through untouched, wells merge per id (in-app screen
elevations survive; app-added wells survive), and map layer registrations
dedupe by path. An unreadable `.hype` is renamed `.bak-<timestamp>`, never
silently clobbered.

## Run state

The driver never writes back into the workbook. Per-site run state lives in
`hype_models\<id>\_provenance.json` (git SHA, input hash, group hashes, stages
last run) and failures append to `hype_models\_runs\failures.csv` plus a
`_error_<stage>.txt` traceback in the site folder.

## Gotchas that will bite again

- Key sites by FOLDER name, never KMZ filename (LL01096's KMZ is named
  SS01208.kmz).
- ftUS terrain must be Z-scaled by 0.3048006 after clipping (`dem_vertical_units`).
- The engine's `_ft` column names hold metric values (`length_units="meters"`).
- Some sites nest an extra folder level (`Sites\SS01208\SS01208\...`) - glob,
  never hardcode.
- `master.py build` after hand edits = data loss. `refresh-wells` is the safe one.
