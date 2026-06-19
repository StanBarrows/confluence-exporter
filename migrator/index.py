"""Generate ``_index.md`` folder notes so the vault hierarchy is navigable.

Each directory that contains pages or subfolders gets an ``_index.md`` listing
links to its children (subfolders first, then pages). Controlled by
``export.layout.index_files`` in config.yml.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict

log = logging.getLogger("migrator.index")

# Folders that are infrastructure, not content.
_SKIP_DIRS = {"_meta", "assets", "diagrams", ".obsidian", ".git"}
_INDEX_NAME = "_index.md"


def _is_content_dir(path: Path) -> bool:
    return path.is_dir() and path.name not in _SKIP_DIRS


def generate_indexes(vault: Path, dry_run: bool = False) -> Dict[str, int]:
    """Write an ``_index.md`` into every content folder under ``vault``."""
    stats = {"indexes_written": 0}
    if not vault.exists():
        return stats

    for current in sorted(vault.rglob("*")):
        if not _is_content_dir(current):
            continue
        if any(part in _SKIP_DIRS for part in current.relative_to(vault).parts):
            continue

        subdirs = sorted(
            d for d in current.iterdir() if _is_content_dir(d)
        )
        pages = sorted(
            p for p in current.glob("*.md")
            if p.name != _INDEX_NAME and not p.name.startswith("_")
        )
        if not subdirs and not pages:
            continue

        lines = [f"# {current.name}", ""]
        if subdirs:
            lines.append("## Sections")
            lines.append("")
            for d in subdirs:
                child_index = d / _INDEX_NAME
                target = child_index if child_index.exists() else d
                rel = os.path.relpath(target, current)
                lines.append(f"- [{d.name}]({rel})")
            lines.append("")
        if pages:
            lines.append("## Pages")
            lines.append("")
            for p in pages:
                rel = os.path.relpath(p, current)
                lines.append(f"- [{p.stem}]({rel})")
            lines.append("")

        out = current / _INDEX_NAME
        if dry_run:
            log.info("[dry-run] would write %s", out)
        else:
            out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        stats["indexes_written"] += 1

    log.info("wrote %d folder index files", stats["indexes_written"])
    return stats
