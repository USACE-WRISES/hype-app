# Changelog

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
