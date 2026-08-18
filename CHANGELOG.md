# Changelog

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
