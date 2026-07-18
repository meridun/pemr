"""Enable `python -m pemr`, delegating to the console-script entry point.

Mirrors the `[project.scripts] pemr = "pemr.cli:main"` wiring so the CLI is
reachable without a generated `pemr.exe` (see issue #22).
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
