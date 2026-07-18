"""`python -m pemr` entry point (issue #22).

The console-script `pemr.exe` isn't reliably generated on every install (e.g. a
system Python whose Scripts dir isn't writable), so `python -m pemr` — backed by
`pemr/__main__.py` — is the portable way to invoke the CLI. This guards the
delegation to `cli.main`.
"""

import subprocess
import sys

import pemr


def test_main_module_runs_as_subprocess():
    """`python -m pemr --version` exits 0 and prints the package version."""
    result = subprocess.run(
        [sys.executable, "-m", "pemr", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert pemr.__version__ in result.stdout


def test_main_module_delegates_to_cli_main():
    """`pemr.__main__` re-exports `cli.main` so `-m pemr` runs the same entry point."""
    import pemr.__main__ as entry
    from pemr.cli import main

    assert entry.main is main
