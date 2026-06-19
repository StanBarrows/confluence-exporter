#!/usr/bin/env python3
"""Set up the Python environment for the Confluence migrator in one shot.

Creates (or reuses) a project-local virtual environment and installs every
Python dependency into it -- the runtime deps, optionally the dev/test deps,
and the ``confluence-markdown-exporter`` (``cme``) engine. Installing into a
venv sidesteps macOS Homebrew's PEP 668 "externally-managed-environment" error.

Usage (run with the system Python; it bootstraps its own venv):

    python3 scripts/setup-python.py            # runtime deps + cme
    python3 scripts/setup-python.py --dev       # also install pytest
    python3 scripts/setup-python.py --no-cme    # skip the cme engine
    python3 scripts/setup-python.py --venv .venv

Afterwards, activate the venv before running the tool:

    source .venv/bin/activate
    python -m migrator spaces
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CME_PACKAGE = "confluence-markdown-exporter"


def _venv_python(venv_dir: Path) -> Path:
    """Return the python executable inside a venv (cross-platform)."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True)


def ensure_venv(venv_dir: Path) -> Path:
    py = _venv_python(venv_dir)
    if py.exists():
        print(f"==> Reusing existing virtual environment: {venv_dir}")
    else:
        print(f"==> Creating virtual environment: {venv_dir}")
        venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(venv_dir)
    return py


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venv", default=".venv",
                        help="Virtual environment directory (default: .venv)")
    parser.add_argument("--dev", action="store_true",
                        help="Also install dev/test dependencies (pytest)")
    parser.add_argument("--no-cme", action="store_true",
                        help="Do not install the confluence-markdown-exporter (cme) engine")
    args = parser.parse_args(argv)

    venv_dir = (REPO_ROOT / args.venv).resolve() if not Path(args.venv).is_absolute() \
        else Path(args.venv)

    req = REPO_ROOT / "requirements.txt"
    req_dev = REPO_ROOT / "requirements-dev.txt"
    if not req.exists():
        print(f"!! {req} not found; run this from the repo.", file=sys.stderr)
        return 1

    try:
        py = ensure_venv(venv_dir)

        print("==> Upgrading pip")
        _run([str(py), "-m", "pip", "install", "--upgrade", "pip"])

        target = req_dev if (args.dev and req_dev.exists()) else req
        print(f"==> Installing dependencies from {target.name}")
        _run([str(py), "-m", "pip", "install", "-r", str(target)])

        if not args.no_cme:
            print(f"==> Installing the {CME_PACKAGE} engine (cme)")
            _run([str(py), "-m", "pip", "install", CME_PACKAGE])
    except subprocess.CalledProcessError as exc:
        print(f"!! A step failed (exit {exc.returncode}). See output above.", file=sys.stderr)
        return exc.returncode

    activate = "Scripts\\activate" if sys.platform == "win32" else "bin/activate"
    print("\n==> Done. Next steps:")
    print(f"     source {venv_dir}/{activate}")
    print("     python -m migrator spaces")
    if not args.no_cme:
        print("     # 'cme' is installed inside the venv (available once activated)")
    print("\nStill needed outside Python (not handled here): draw.io Desktop, git, git-lfs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
