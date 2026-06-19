"""Scaffold the target run/vault as a Git + Git LFS repository.

LFS extensions, the Obsidian config toggle, and the optional initial commit all
come from config.yml `git:`. Pushing to a remote is always left to the user.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List

from .config import Config

log = logging.getLogger("migrator.gitops")

GITIGNORE = """\
.obsidian/workspace.json
.obsidian/workspace-mobile.json
.obsidian/cache
*.tmp
~drawio~*
.DS_Store
"""

OBSIDIAN_APP_JSON = """\
{
  "newLinkFormat": "relative",
  "useMarkdownLinks": false,
  "attachmentFolderPath": "./assets"
}
"""


def _gitattributes(lfs_extensions: List[str]) -> str:
    lines = [
        f"*.{ext} filter=lfs diff=lfs merge=lfs -text" for ext in lfs_extensions
    ]
    lines.append("# Keep as text (NOT LFS): *.md, *.svg, *.drawio, *.drawio.svg")
    return "\n".join(lines) + "\n"


def _run(args, cwd: Path) -> bool:
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("%s -> %s", " ".join(args), proc.stderr.strip()[:200])
        return False
    return True


def scaffold_vault(config: Config, do_git: bool = True) -> None:
    gcfg = config.settings.git
    lfs_exts = config.settings.lfs_extensions
    vault = config.output_path
    dry = config.dry_run

    if not dry:
        vault.mkdir(parents=True, exist_ok=True)
        if gcfg.lfs and lfs_exts:
            (vault / ".gitattributes").write_text(
                _gitattributes(lfs_exts), encoding="utf-8"
            )
        (vault / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        if gcfg.obsidian_config:
            obsidian = vault / ".obsidian"
            obsidian.mkdir(exist_ok=True)
            (obsidian / "app.json").write_text(OBSIDIAN_APP_JSON, encoding="utf-8")
    log.info("wrote .gitattributes/.gitignore (+.obsidian) in %s", vault)

    if not (do_git and gcfg.init):
        return
    if shutil.which("git") is None:
        log.warning("git not found; skipping repo init")
        return
    if dry:
        log.info("[dry-run] would git init%s in %s",
                 " + commit" if gcfg.commit else "", vault)
        return

    if not (vault / ".git").exists():
        _run(["git", "init"], vault)
    if gcfg.lfs:
        _run(["git", "lfs", "install", "--local"], vault)

    if gcfg.commit:
        _run(["git", "add", "-A"], vault)
        # Only commit if there is something staged (avoid an empty-commit error).
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=str(vault)
        )
        if staged.returncode == 1:
            if _run(["git", "commit", "-m", gcfg.commit_message], vault):
                log.info("created initial commit (push to a remote left to you)")
        else:
            log.info("nothing to commit")
    else:
        log.info("git repo initialized (commit + remote left to you)")
