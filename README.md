# HYPE: Hyporheic Exchange Explorer

HYPE is a Windows desktop application for surface water and groundwater interaction modeling.
From a reach you pick on a map it builds and runs a HEC-RAS 2025 2D water surface, a MODFLOW 6
and MODPATH 7 groundwater model and a hyporheic-zone delineation, then produces the site reports
and the ecological function screening that follow. Everything runs and saves on your own
computer.

This repository is the public home of HYPE: the installers and their updates, the example
projects, the changelog and the issue tracker. Development happens in a separate repository.

## Install (Windows)

Download `HypeDesktop-win-Setup.exe` from the
[latest release](https://github.com/USACE-WRISES/hype-app/releases/latest) and run it.
Per-user install, no admin rights; requires the Microsoft WebView2 Runtime (ships with Edge on
Windows 10 and 11). First launch downloads the modeling runtime (about 450 MB, resumable,
checksum-verified). After that the app keeps itself current and offers each update in a banner.

The same release page carries a portable zip: unzip it anywhere and run `HypeDesktop.exe`,
keeping `Update.exe`, `current\` and `packages\` beside it. `Update.exe` starts the app and
applies updates, so the app neither runs nor updates without it.

## Example projects

The start page (**Projects** in the header) offers worked example sites you can download one at
a time; they open with their models already run. The list refreshes itself from this
repository's `examples` release.

## What changed

[CHANGELOG.md](CHANGELOG.md), also shown in the app by clicking the version number in the header
and on the start page.

## Support

Open an issue at <https://github.com/USACE-WRISES/hype-app/issues>. The start page has a
**Report an issue** link that goes to the same place. Include the version from the header chip
and, for a modeling problem, the site id or a copy of the project folder.

## How to cite

See [CITATION.cff](CITATION.cff); GitHub shows a "Cite this repository" button built from it.

## Engines and data

Terrain and streams: USGS 3DEP and NHD. Engines: HEC-RAS 2025 (2D surface water), MODFLOW 6 and
MODPATH 7 (groundwater and particle tracking). Site data: USGS StreamStats, NRCS soils and NLCD
land cover services.

## License

License terms are pending the organization's release decision and will be published here. Until
then, please contact the authors before redistributing or reusing the software.
