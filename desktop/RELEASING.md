# HYPE Desktop — release model

Two independent release streams share this repo's GitHub Releases, plus two rolling/pinned
prereleases. **The one hard rule:** payload releases (`desktop-payload-*`), tool releases
(`desktop-tools-*`), and `desktop-current` are **always prereleases**. Only `v*` shell releases
are normal releases — GitHub's `releases/latest` must resolve to an installer, because both
Velopack's updater and humans use it.

| Stream | Tag | Kind | Contains | Cadence |
|---|---|---|---|---|
| Shell | `v*` (e.g. `v0.1.0`) | **normal** release | `HypeDesktop-win-Setup.exe`, portable zip, Velopack deltas + `RELEASES` feed | rarely — only when the C# shell changes |
| Payload | `desktop-payload-YYYY.MM.DD-<sha>` | prerelease | `hype-apps-….zip` (~3 MB, every run) and `hype-env-cp312-….zip` (~450 MB, only when locks changed) | **automatic on every push to `main`** touching app code |
| Tools | `desktop-tools-N` | prerelease | Windows HEC-RAS 2025 CLI + mf6/mp7 zips, pinned by `desktop/payload/tools.lock` | manual, ~never |
| Manifest | `desktop-current` | rolling prerelease | `latest-desktop.json` — the one URL every installed shell polls | refreshed by both workflows |

## Routine app update (the whole point)

```
git push origin main
```

That's it. The `desktop-payload` workflow (paths-filtered to `app.py`, `hype_app/`, `hypetool/`,
`www/`, `CHANGELOG.md`, `requirements.txt`, `desktop/payload|scripts/`) builds a fresh apps zip from the tracked
tree (`git archive`), publishes the prerelease, and refreshes `desktop-current`. ~8 minutes
later every installed desktop's next update check shows the native banner — "A HYPE update is
ready (3 MB). Install & restart app". The same push is what you deploy to Connect Cloud from,
so web and desktop stay the same code by construction.

The env component (python-build-standalone + wheels + `tools\` solver runtimes) is rebuilt only
when `desktop/payload/{env.lock,pbs.lock,prune.txt,tools.lock}` change — ENV_VERSION is a
content hash over those four files — and always passes the **relocation smoke gate** (move the
tree, import the heavy stack, boot the real app against the relocated tools) before publishing.
Never build with `-SkipSmoke` for anything that ships.

## Versioned release (vX.Y.Z)

One user-facing number covers app and shell: `APP_VERSION` in `app.py` = csproj `<Version>` =
the tag. Patch (x.y.Z) for routine updates (fixes, speedups, small/experimental features);
minor (x.Y.0) for a headline capability or a project-format change; major for reworks.

1. Add the release's `## vX.Y.Z (YYYY-MM-DD)` section to `CHANGELOG.md` — it ships inside the
   apps payload and feeds the in-app What's new dialog (`tests/test_changelog.py` pins the
   app.py/csproj/changelog lockstep).
2. Bump `APP_VERSION` in `app.py` and `<Version>` in
   `desktop/src/Hype.Desktop/Hype.Desktop.csproj` together.
3. `git push origin main` and **wait for `desktop-payload` to finish** (both workflows write
   `latest-desktop.json`).
4. `git tag vX.Y.Z && git push origin vX.Y.Z`.

`desktop-shell.yml` runs the unit tests, publishes self-contained win-x64, packs with Velopack
(delta against the previous release), uploads a **normal** release with its body filled from
the tag's CHANGELOG.md section, and stamps the new installer URLs onto `desktop-current`'s
manifest. Installed shells offer "Restart & update".

## Tool runtimes (HEC-RAS / MODFLOW) update

The Windows HEC-RAS runtime lives in the gitignored `reference/` folder on the dev machine, so
CI cannot rebuild it — the zips are uploaded by hand once and pinned by hash:

1. Zip the runtime contents (`ras.exe` at zip root): `tar -a -c -f hype-tools-ras2025-win-2.zip -C "<install dir>" *`
2. `gh release create desktop-tools-2 --prerelease --title "HYPE Desktop tool binaries 2" <zips>`
3. Update `desktop/payload/tools.lock` (url + sha256), push to main.

The tools.lock change bumps ENV_VERSION → CI rebuilds and re-ships the env automatically.

## Dependency (wheel) update

```
uv pip compile requirements.txt --python-version 3.12 --python-platform windows --no-header -o desktop/payload/env.lock
git add desktop/payload/env.lock && git push
```

The lock-consistency gate in CI verifies every direct requirement in `requirements.txt` is
satisfied by `env.lock` (floors, not exact pins — see `desktop/scripts/check_lock_consistency.py`).

## First-time bootstrap (already done once; recorded for posterity)

1. Upload `desktop-tools-1` prerelease; commit `tools.lock` with its sha256s.
2. Tag `v0.1.0` → shell release (its manifest-stamp step no-ops until a payload exists).
3. Push to main → first payload run builds env (~20-30 min) + apps, creates `desktop-current`.
4. Install `HypeDesktop-win-Setup.exe` on a clean machine → first-run setup downloads ~450 MB
   (resumable, sha256-verified) → the app opens.

## Local dev & QA

- `dotnet test desktop/Hype.Desktop.slnx` — unit tests. **This does NOT rebuild the shell
  exe** (only Core + the test project): after any shell change, run
  `dotnet build desktop/src/Hype.Desktop -c Debug` before launching `HypeDesktop.exe`, or
  you'll be running yesterday's build.
- Running `HypeDesktop.exe` from a checkout auto-detects **dev mode** (repo `.venv`, payload
  machinery off). `HYPE_DESKTOP_DEV=0` disables; `HYPE_REPO_ROOT` points elsewhere.
- `HYPE_FORCE_PAYLOAD=1` — exercise the installed-payload path from a checkout.
- `HYPE_MANIFEST_URL=http://127.0.0.1:8020/latest-desktop.json` — point at a locally served
  manifest (build zips with the two build-*.ps1 scripts, compose with `gen_latest_manifest.py`,
  `python -m http.server 8020`).
- `HYPE_DATA_ROOT` — relocate all shell state (default `%LOCALAPPDATA%\HYPE`).

## Prerequisites & recovery

- Users need the **Microsoft WebView2 Runtime** (ships with Edge on Win10/11); the shell shows
  a clear error if missing. The installer is per-user — no admin rights needed.
- If a payload release was accidentally published as a normal release, edit it to prerelease
  immediately (`gh release edit <tag> --prerelease`) — otherwise `releases/latest` stops being
  an installer and shell self-update breaks.
- Offline/air-gapped install: download the shell installer + both payload zips +
  `latest-desktop.json` to a folder, then use "Install from file…" on the setup screen.
