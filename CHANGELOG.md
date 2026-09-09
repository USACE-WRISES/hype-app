# Changelog

## v1.2.2 (2026-09-09)
- Breakthrough curves are a live chart: hover to read the concentration at any day, and copy or save the still image.
- Observation wells are one row per well: name, screen elevation, observed, computed and residual in labelled columns.

## v1.2.1 (2026-09-08)
- Fixed: a transport run could hang for minutes after solving, then fail while writing its results.
- Fixed: the transport size warning and the particle count read the whole grid box instead of the cells the model solves.
- Injection points are one row each: name, screen elevation, type, value and duration in labelled columns.
- A screen elevation can be set back to the water table from the injection points and observation wells tables.

## v1.2.0 (2026-09-07)
- Transport Modeling: solute transport models (MODFLOW 6 GWT) run on the finished groundwater solution and save with the project.
- New transport model: start from a species preset or blank; reaction, sorption and mobile-immobile transfer are independent choices.
- Injection points on the map, concentration maps by layer and step, breakthrough curves at the observation wells, and a mass budget.
- Boundaries: the model area is any drawn polygon, split into named arcs; surface and groundwater conditions hang off each arc.
- Groundwater boundary conditions: one table per boundary, a gradient category or spatially varying points entered as gradient or head.
- 2D mesh: breaklines with a cell spacing and refinement regions with a cell size, drawn on the map and saved with the project.
- Tree: a Transport & Functions branch holds Transport Modeling and the screening; Hydraulic Alternatives sits under Particle Tracking.
- Many smaller changes: standard-width panes, the report no longer pops up after particle tracking, shorter labels, a probe unit fix.

## v1.1.5 (2026-09-04)
- Boundary conditions: each line spans its whole boundary side by default, or pick two points on the map for a custom line.
- A tributary can be a stage boundary (a constant water surface elevation) instead of a flow; the width inputs are gone.
- The boundary condition overlay is yellow on a dark casing with amber value pills, and map labels no longer overlap.
- NRCS soils review: Fetch beside the map, one table of map units whose rows open their profiles, and two ways out: K field or K-zones.
- Fixed: the soils aggregation method no longer flips back on its own, and a long soil profile no longer spills past the footer.
- Subsurface properties: one hydraulic conductivity table, the base model KH and KV on its first row and each K-zone under it.
- K-zones apply whenever any exist; the use-zones checkbox is gone. Runs saved with it off now report changed inputs.
- K-zone names show as small tags on the map, and a drawn zone can be renamed in the table.
- The layer tree checks off input rows once they are ready, and selecting a boundary no longer ticks its checkbox.
- Many smaller changes: figure tips on how soils become K, a Remove button for the soils K, and the soils line only while in use.

## v1.1.4 (2026-09-02)
- USGS flow lookup: only the statistics that apply at the outlet, grouped by statistic and regression region with its publication.
- Pick which USGS statistic groups to compute (peak flow, flow duration, bankfull); hover a row's icon for its equation and limits.
- The USGS flow review is one screen: outlet and Fetch beside the map, one line per statistic group, only the results scroll.
- Fixed: the USGS, soils and NAIP review maps could lose their overlays (watershed, reach, domain, markers) after a fetch.
- Dialogs open near the top of the window instead of a tenth of the way down; the StreamStats review uses the room for its results.

## v1.1.3 (2026-09-01)
- Land cover: Get NLCD works on networks that block certificate revocation checks (the USACE "revocation status" error).
- Groundwater: sites below sea level lost every stream cell (the zone analysis then failed); a run with no stream cells now stops.

## v1.1.2 (2026-08-31)
- Land cover: Manning's n is its own map layer now, with one-click NLCD 2021 data and drawn override regions the run uses.
- Set Manning's n per NLCD class, or look it up by channel or floodplain condition after HEC's Table 3-1.
- Boundary conditions: inflow and outflow widths, placed on the model boundary where the reach crosses it; blank uses 100 ft.
- The downstream boundary can now be a stage, a constant water surface elevation, instead of a normal depth slope.
- A tributary that enters at the model boundary now runs as a boundary inflow instead of blocking the run.
- The map draws each boundary condition where the run places it, with its value, and map labels no longer sit on each other.
- Fixed: switching unit systems could rewrite tributary flows, well readings, K zones and terrain modifications.
- Smaller changes: drawing a second zone keeps the first one's values, projects open zoomed in, and reruns after reopening work.

## v1.1.1 (2026-08-27)
- The Texas calibration sites now download with their full results and open ready to explore, no rerun needed.
- Opening a downloaded example opens your copy in place; an update asks first, and downloads default to Documents\HYPE Projects.
- Look up hydraulic conductivity by sediment or rock type on Subsurface properties, after Freeze and Cherry (1979).
- The surface water engine choices are now 2D Surface Water or Diffusion Wave (Faster); the GPU option is retired.
- Map tools: streams for reach picking load a zoom level earlier and hide when zoomed out, and the cross-section tool reports slope.
- Smaller changes: an Open folder button on the Project pane, safer reach snapping, and no map zoom when a double-click ends a drawing.

## v1.1.0 (2026-08-26)
- Reaches: a named main reach plus any number of tributaries, each with its own streamflow and inflow line.
- The Main reach sizes itself from a channel width when the NHD has no drainage area.
- Draw a centerline of any length, and edit any map line as a session you can cancel.
- Projects now have their own name, shown in the header, window titles and recents.
- Projects now have a unit system, Metric or US Customary, followed by every input, readout, report and export.
- Terrain modifications: a managed list of carve lines and area edits replaces the single channel carve.
- Contour lines on the Terrain pane, at whatever interval you pick.
- An imported terrain raster now converts to metres from its own unit; before this feet were read as metres.
- The surface water Engine is now a choice: shallow water on the processor or graphics card, or Diffusion Wave.
- Get NAIP imagery from the Map layers pane, no account needed.
- Example projects is now two galleries: finished examples, and the Texas calibration sites as setups to run.
- Both galleries refresh from the releases, so a revised site shows Update available.
- The 3D view has a scale bar, Measure in every view, and a grid preview drawn at full resolution.
- Aquaveo GMS export: true grid location, the model projection, and your project units.
- HYPE now ships only as HYPE Desktop, and the size readouts no longer block a run.
- Many smaller changes: panes, tree groups and header stages regrouped, plus bug fixes and workflow polish.

## v1.0.5 (2026-08-18)
- New start page: New project, Open project and Example projects side by side with your recent projects and what changed in each release, shown once the map is ready and any time from Projects in the header (which replaces the separate New and Open links).
- Example projects: download a finished site from the start page, one at a time, and open it with all of its results to explore before building your own. Works in HYPE Desktop and in the cloud; downloads resume if interrupted.
- A loading screen now covers the app until the map is up, so the start page never appears over a blank window.

## v1.0.4 (2026-08-14)
- The update banner now waits for both update checks before it appears, so updating can no longer split into two steps when the app and the desktop shell update together.

## v1.0.3 (2026-08-14)
- Updating is now one clear step: a single banner covers both the app and the desktop shell, with one download, one restart, and buttons that say what they do (Update and restart, Download and restart, Install update).

## v1.0.2 (2026-08-14)
- Portable installs now clean up the leftover "HYPE Desktop.exe" launcher from the original download, and the portable download ships one standard launcher name (HypeDesktop.exe).

## v1.0.1 (2026-08-14)
- Faster particle tracking results: large MODPATH output now processes in minutes instead of tens of minutes, with progress shown in the run log.
- Desktop runs are no longer bound by the cloud size limits: cell count, DEM resolution, runtime, and particle caps are lifted.
- New experimental rainbow residence-time coloring: color the flow path lines and the animated particles by total or elapsed time, with a legend in the pane, on the map, in the 3D view, and in captures.
- Stalled HEC-RAS runs are now detected and stopped automatically instead of hanging.
- The 2D mesh layer now reappears when you reopen a saved project, drawn from the model's own geometry, and shows on any step while its box is checked.
- Starting a new project now returns the map to the national view and begins fully blank; settings and typed values no longer carry over from the previously open project.
- Manually drawn centerlines now take their direction from the NHD flow direction, so winding reaches no longer come out with upstream and downstream reversed.

## v1.0.0 (2026-08-11)
- First public release.
