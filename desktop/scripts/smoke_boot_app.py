"""Boot the HYPE app once from a given interpreter and require an HTTP answer.

This is the payload gate: it runs against the RELOCATED env build in CI (and locally) so that
pruning mistakes, non-relocatable paths, broken wheels, or a bad tools zip are caught before
anything publishes. When --tools-dir is given, the solver exes must exist there and the app
boots with HYPE_RAS_BIN / HYPE_MODFLOW_BIN pointed into it (the shell does the same at runtime).

Usage:
    python smoke_boot_app.py --python <python.exe> --app-root <repo-or-payload-root>
                             [--tools-dir <dir>] [--imports geopandas,rasterio,...] [--timeout 240]

Exit code 0 only if the import check, the tools check, and the app boot succeed.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_IMPORTS = (
    "geopandas,rasterio,shapely,pyproj,shiny,shinywidgets,ipyleaflet,flopy,netCDF4,h5py,"
    "skimage,py3dep,pynhd,rioxarray,xarray,matplotlib,seaborn,reportlab,httpx,pydantic,"
    "yaml,jinja2,tabulate"
)

FORBIDDEN_IMPORTS = ["aiodns", "pycares"]  # pruned from the payload; present => broken Windows DNS

TOOLS_DIR: str | None = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_tools(tools_dir: str) -> None:
    required = [
        os.path.join(tools_dir, "ras2025", "ras.exe"),
        os.path.join(tools_dir, "modflow", "mf6.exe"),
        os.path.join(tools_dir, "modflow", "mp7.exe"),
    ]
    missing = [p for p in required if not os.path.isfile(p)]
    if missing:
        raise SystemExit("[smoke] tools check FAILED - missing:\n  " + "\n  ".join(missing))
    print(f"[smoke] tools OK under {tools_dir}", flush=True)


def check_imports(python: str, imports: list[str]) -> None:
    print(f"[smoke] import check: {', '.join(imports)} (forbidden: {', '.join(FORBIDDEN_IMPORTS)})", flush=True)
    code = "import importlib, sys\n" + "\n".join(
        f"importlib.import_module({mod!r})" for mod in imports
    ) + "\n" + "\n".join(
        "try:\n"
        f"    importlib.import_module({mod!r})\n"
        f"    raise SystemExit('FORBIDDEN module importable: {mod} (prune.txt not applied?)')\n"
        "except ImportError:\n"
        "    pass"
        for mod in FORBIDDEN_IMPORTS
    ) + "\nprint('imports OK', sys.version)"
    result = subprocess.run(
        [python, "-c", code],
        capture_output=True,
        text=True,
        timeout=600,
        env=smoke_env(),
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise SystemExit(f"[smoke] import check FAILED ({result.returncode})")


def smoke_env() -> dict[str, str]:
    env = os.environ.copy()
    cache = os.path.join(tempfile.gettempdir(), "hype-smoke-cache")
    os.makedirs(cache, exist_ok=True)
    env.update(
        HYPE_DESKTOP="1",
        HYRIVER_CACHE_NAME=os.path.join(cache, "smoke_hyriver.sqlite"),
        MPLCONFIGDIR=os.path.join(cache, "mpl"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTHONUTF8="1",
    )
    if TOOLS_DIR:
        env["HYPE_RAS_BIN"] = os.path.join(TOOLS_DIR, "ras2025")
        env["HYPE_MODFLOW_BIN"] = os.path.join(TOOLS_DIR, "modflow")
    return env


def boot_app(python: str, app_root: str, timeout: float) -> None:
    port = free_port()
    if not os.path.isfile(os.path.join(app_root, "app.py")):
        raise SystemExit(f"[smoke] app.py missing in {app_root}")

    print(f"[smoke] booting HYPE on :{port} …", flush=True)
    proc = subprocess.Popen(
        [python, "-u", "-m", "shiny", "run", "--host", "127.0.0.1", "--port", str(port), "app.py"],
        cwd=app_root,
        env=smoke_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise SystemExit(
                    f"[smoke] app exited early (code {proc.returncode})\n{out[-4000:]}"
                )
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
                    if resp.status < 500:
                        print(f"[smoke] app answered HTTP {resp.status}", flush=True)
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            time.sleep(0.5)
        raise SystemExit(f"[smoke] app did not answer within {timeout}s")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def main() -> None:
    global TOOLS_DIR
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--tools-dir")
    parser.add_argument("--imports", default=DEFAULT_IMPORTS)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    python = os.path.abspath(args.python)
    if not os.path.isfile(python):
        raise SystemExit(f"[smoke] python not found: {python}")
    if args.tools_dir:
        TOOLS_DIR = os.path.abspath(args.tools_dir)
        check_tools(TOOLS_DIR)

    if args.imports:
        check_imports(python, [m.strip() for m in args.imports.split(",") if m.strip()])
    boot_app(python, os.path.abspath(args.app_root), args.timeout)
    print("[smoke] ALL OK", flush=True)


if __name__ == "__main__":
    main()
