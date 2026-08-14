"""Sequential batch runner: drive.py once per site, resumable.

Complete = hype_models/<site>/<site>.hype exists (bundle is the last stage), so
finished sites are skipped unless --force. A failed site leaves no bundle and is
retried IN FULL on the next batch run: drive's --auto is input-diff based and
selects zero stages after a failure with unchanged inputs, so it cannot serve as
the retry path. Targeted `drive.py SITE --stages ...` reruns stay a manual
triage step; batch.py deliberately has no per-stage intelligence.

Usage:
  python tools/site_factory/batch.py                # every site except LL01096
  python tools/site_factory/batch.py --sites A,B    # explicit subset
  python tools/site_factory/batch.py --force        # rebuild even if complete
  python tools/site_factory/batch.py --dry-run      # show selection and exit

Per-site stdout+stderr goes to hype_models/_runs/batch_<ts>/<site>.log and a
running summary.csv is appended after every site, so a killed batch still
leaves a usable record. Exit code 1 if any site failed or timed out.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.site_factory import master  # noqa: E402
from tools.site_factory.drive import HYPE_MODELS  # noqa: E402

# The pilot: built by hand and since edited in the app; never rebuilt by default.
SKIP_ALWAYS = ("LL01096",)


def pick_sites(all_ids, complete, force=False, explicit=None):
    """Select sites to run, preserving all_ids (workbook) order.

    Returns (run, skipped) with skipped = [(site, reason)]. explicit=None means
    every site minus SKIP_ALWAYS; an explicit list bypasses SKIP_ALWAYS (you
    asked for it by name) but unknown ids raise. The complete-skip applies to
    explicit picks too unless force.
    """
    if explicit is not None:
        unknown = [s for s in explicit if s not in all_ids]
        if unknown:
            raise SystemExit(f"not in inputs_master.xlsx: {unknown}")
        keep = set(explicit)
        wanted = [s for s in all_ids if s in keep]
    else:
        wanted = [s for s in all_ids if s not in SKIP_ALWAYS]
    run, skipped = [], []
    for s in wanted:
        if not force and s in complete:
            skipped.append((s, "complete"))
        else:
            run.append(s)
    return run, skipped


def failed_stage(work_dir: Path) -> str:
    """Name of the most recent _error_<stage>.txt in a site workspace."""
    errs = sorted(work_dir.glob("_error_*.txt"), key=lambda p: p.stat().st_mtime)
    return errs[-1].name[len("_error_"):-len(".txt")] if errs else "?"


def run_one(site: str, log_path: Path, timeout_s: float) -> str:
    """Run drive.py for one site, output to log_path. Returns the outcome."""
    # PYTHONIOENCODING: a stray non-cp1252 char in a stage print must not kill
    # the child when stdout is a pipe/file on Windows.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, str(REPO / "tools" / "site_factory" / "drive.py"), site]
    with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
        p = subprocess.Popen(cmd, cwd=REPO, stdout=lf, stderr=subprocess.STDOUT, env=env)
        try:
            rc = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # kill the whole tree: drive.py's RAS/MF6 children would outlive p.kill()
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True)
            return "timeout"
    return "ok" if rc == 0 else f"failed:{failed_stage(HYPE_MODELS / site)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", help="comma list; default = all except " + ",".join(SKIP_ALWAYS))
    ap.add_argument("--force", action="store_true", help="run even when <site>.hype exists")
    ap.add_argument("--timeout-min", type=float, default=180.0, help="per-site wall-clock cap")
    ap.add_argument("--dry-run", action="store_true", help="print selection and exit")
    args = ap.parse_args()

    all_ids = list(master.read_sites())
    complete = {s for s in all_ids if (HYPE_MODELS / s / f"{s}.hype").exists()}
    explicit = [s.strip() for s in args.sites.split(",") if s.strip()] if args.sites else None
    run, skipped = pick_sites(all_ids, complete, args.force, explicit)

    for s, why in skipped:
        print(f"skip {s}: {why}")
    print(f"{len(run)} site(s) to run: {', '.join(run) or '(none)'}", flush=True)
    if args.dry_run or not run:
        return

    batch_dir = HYPE_MODELS / "_runs" / f"batch_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    summary = batch_dir / "summary.csv"
    with open(summary, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["site", "outcome", "minutes", "log"])

    failures = 0
    for i, site in enumerate(run, 1):
        print(f"[{i}/{len(run)}] {site} ...", flush=True)
        t0 = time.monotonic()
        outcome = run_one(site, batch_dir / f"{site}.log", args.timeout_min * 60)
        mins = (time.monotonic() - t0) / 60
        if outcome != "ok":
            failures += 1
        print(f"[{i}/{len(run)}] {site} -> {outcome} ({mins:.1f} min)", flush=True)
        with open(summary, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([site, outcome, f"{mins:.1f}", f"{site}.log"])

    print(f"batch done: {len(run) - failures} ok, {failures} not ok -> {summary}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
