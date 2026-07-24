"""Generate desktop-manifest.json (ships inside the apps payload).

HYPE is a single app at the repo/payload root, so the app list is a fixed literal here —
stdlib-only, no yaml. The shell reads {id, dir, entry} to spawn the server and webUrl for
"open the hosted version" affordances.

Usage:
    python gen_desktop_manifest.py --apps-version <v> --env-version <v> --commit <sha> --out <file>
"""
from __future__ import annotations

import argparse
import json

APPS = [
    {
        "id": "hype",
        "dir": ".",
        "entry": "app.py",
        "name": "HYPE",
        "fullName": "Hyporheic Exchange Explorer",
        "tier": "",
        "tierNum": 0,
        "role": "Hyporheic exchange modeling app",
        "description": "Builds and runs a HEC-RAS + MODFLOW 6 + MODPATH 7 hyporheic model "
                       "from a map-defined reach.",
        "status": "live",
        "webUrl": "https://019f019d-998f-1edd-cf76-f3d4c9e3248e.share.connect.posit.cloud",
    }
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps-version", required=True)
    parser.add_argument("--env-version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    manifest = {
        "schemaVersion": 1,
        "version": args.apps_version,
        "builtFromCommit": args.commit,
        "requiresEnv": args.env_version,
        "apps": APPS,
    }
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print(f"[manifest] wrote {args.out} ({len(APPS)} app)")


if __name__ == "__main__":
    main()
