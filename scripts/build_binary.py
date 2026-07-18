# -*- coding: utf-8 -*-
"""Build a standalone single-file `ibpnl` binary for the *current* platform.

Steps: build the frontend into backend/app/static, ensure PyInstaller is
available, run backend/ibpnl.spec, then copy the result to a platform/arch
named file under dist/.

Usage (from the repo root, ideally inside backend/.venv):

    python scripts/build_binary.py            # skip npm if static already fresh
    python scripts/build_binary.py --frontend # force a frontend rebuild

Cross-compiling is not supported: run this on each target OS (or use CI).
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
STATIC = BACKEND / "app" / "static"
SPEC = BACKEND / "ibpnl.spec"
DIST = ROOT / "dist"


def _run(cmd: list[str], cwd: Path) -> None:
    print(f"\n$ {' '.join(cmd)}  (cwd={cwd})", flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def _npm() -> str:
    # npm ships as npm.cmd on Windows; shutil.which finds the right one.
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found on PATH; install Node.js to build the frontend.")
    return npm


def build_frontend(force: bool) -> None:
    if STATIC.joinpath("index.html").is_file() and not force:
        print(f"Frontend already built at {STATIC} (use --frontend to force).")
        return
    npm = _npm()
    if not FRONTEND.joinpath("node_modules").is_dir():
        _run([npm, "ci"], cwd=FRONTEND)
    _run([npm, "run", "build"], cwd=FRONTEND)
    if not STATIC.joinpath("index.html").is_file():
        raise SystemExit(f"Frontend build did not produce {STATIC / 'index.html'}.")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed; installing into the current environment...")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"], cwd=ROOT)


def _target_tag() -> str:
    system = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(
        platform.system().lower(), platform.system().lower()
    )
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return f"{system}-{arch}"


def run_pyinstaller() -> Path:
    _run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--clean", "--noconfirm"],
        cwd=BACKEND,
    )
    exe = "ibpnl.exe" if platform.system().lower() == "windows" else "ibpnl"
    built = BACKEND / "dist" / exe
    if not built.is_file():
        raise SystemExit(f"PyInstaller did not produce {built}.")
    return built


def stage_output(built: Path) -> Path:
    DIST.mkdir(exist_ok=True)
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    dest = DIST / f"ibpnl-{_target_tag()}{suffix}"
    shutil.copy2(built, dest)
    if platform.system().lower() != "windows":
        dest.chmod(0o755)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a single-file ibpnl binary.")
    ap.add_argument(
        "--frontend",
        action="store_true",
        help="Force a frontend rebuild even if app/static already exists",
    )
    args = ap.parse_args()

    build_frontend(force=args.frontend)
    ensure_pyinstaller()
    built = run_pyinstaller()
    dest = stage_output(built)
    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"\nBuilt {_target_tag()} binary: {dest}  ({size_mb:.1f} MB)")
    print("Smoke test it with:")
    print(f"    {dest} --version")


if __name__ == "__main__":
    main()
