# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: build a single-file `ibpnl` binary.

Single source of truth for both the local build (`scripts/build_binary.py`) and
CI. Run from the `backend/` directory:

    pyinstaller ibpnl.spec --clean

Prereqs: the frontend must already be built into `app/static` (the build script
runs `npm run build` first).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

BACKEND = Path(SPECPATH)  # noqa: F821 - SPECPATH injected by PyInstaller
STATIC_DIR = BACKEND / "app" / "static"

if not STATIC_DIR.is_dir() or not (STATIC_DIR / "index.html").is_file():
    raise SystemExit(
        "app/static is missing or empty. Build the frontend first "
        "(cd frontend && npm run build), then re-run PyInstaller."
    )

# --- Data files -----------------------------------------------------------
# Ship the built frontend at the same relative path the app expects
# (app/main.py resolves STATIC_DIR = Path(__file__).parent / "static", which
# under PyInstaller points into the unpacked bundle root/app/static).
datas = [(str(STATIC_DIR), "app/static")]
# zoneinfo has no system tz database on Windows; bundle the tzdata package data.
datas += collect_data_files("tzdata")

# --- Hidden imports -------------------------------------------------------
# uvicorn dynamically imports its protocol/loop/lifespan implementations by
# string, and ib_async pulls in eventkit/nest_asyncio at runtime. None of these
# are visible to PyInstaller's static analysis.
hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "websockets",
    "websockets.legacy",
    "httptools",
    "eventkit",
    "nest_asyncio",
]

# --- Excludes --------------------------------------------------------------
# Modules PyInstaller's static analysis reaches (mainly via ib_async.util's
# nested plotting / DataFrame helpers, which this read-only app never calls)
# but that are provably unused at runtime. Verified by import-blocking every
# name below and confirming app.cli / app.main / both providers / routes / ws
# still import cleanly. Numerical stacks (numpy, pandas, scipy) are KEPT on
# purpose. Excluding an absent package is a harmless no-op, so this also stops
# a fat build environment from bloating the binary.
excludes = [
    "tkinter", "watchfiles",
    # plotting / imaging (ib_async.util.plot* only)
    "matplotlib", "matplotlib_inline", "mpl_toolkits", "PIL",
    "contourpy", "kiwisolver", "cycler",
    # IPython / Jupyter / kernel stack
    "IPython", "ipykernel", "jupyter_client", "jupyter_core", "nbformat",
    "comm", "zmq", "tornado", "debugpy", "psutil",
    "prompt_toolkit", "jedi", "parso",
    "stack_data", "executing", "asttokens", "pure_eval",
    # test / type-check tooling (dev-only)
    "pytest", "_pytest", "py", "pluggy", "iniconfig", "mypy", "mypy_extensions",
    # http client used only in tests, and its transitive deps
    "requests", "urllib3", "charset_normalizer",
    # html / json-schema chain pulled in via nbformat & bs4
    "bs4", "soupsieve", "jsonschema", "fastjsonschema", "referencing", "rpds",
    "isoduration", "fqdn", "uri_template", "webcolors", "arrow", "defusedxml",
]

a = Analysis(  # noqa: F821
    [str(BACKEND / "packaging" / "entry.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ibpnl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
