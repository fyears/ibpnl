# -*- coding: utf-8 -*-
"""PyInstaller entry point for the frozen `ibpnl` binary.

This is the script PyInstaller compiles as the executable's `__main__`. It does
nothing but delegate to the real CLI so the packaged binary behaves exactly like
the `ibpnl` console script (`app.cli:main`).
"""

from __future__ import annotations

import multiprocessing

from app.cli import main

if __name__ == "__main__":
    # Safe no-op under a single-process server, but required so a frozen build
    # never re-launches the whole app if any dependency spawns a child process.
    multiprocessing.freeze_support()
    main()
