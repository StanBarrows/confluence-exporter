"""Normalize YAML frontmatter to the configured target schema.

Ensures every page carries the fields listed in
``markdown.frontmatter.fields`` (added empty when missing) and orders them
consistently, while preserving any extra keys cme emitted and the page body.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List

log = logging.getLogger("migrator.normalize")

_FRONTMATTER = re.compile(r"^---\n(?P<yaml>.*?)\n---\n(?P<body>.*)$", re.DOTALL)


def _ordered(data: dict, fields: List[str]) -> dict:
    out: dict = {}
    for key in fields:
        out[key] = data.get(key, "")
    for key, value in data.items():
        if key not in out:
            out[key] = value
    return out


def normalize_file(md: Path, fields: List[str]) -> bool:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover
        return False

    text = md.read_text(encoding="utf-8", errors="replace")
    match = _FRONTMATTER.match(text)
    if match:
        try:
            data = yaml.safe_load(match.group("yaml")) or {}
        except yaml.YAMLError:
            return False
        if not isinstance(data, dict):
            return False
        body = match.group("body")
    else:
        data = {}
        body = text

    merged = _ordered(data, fields)
    if match and merged == data:
        return False

    dumped = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True).rstrip("\n")
    new_text = f"---\n{dumped}\n---\n{body}"
    md.write_text(new_text, encoding="utf-8")
    return True


def normalize_vault(
    vault: Path, fields: List[str], dry_run: bool = False
) -> Dict[str, int]:
    stats = {"files_normalized": 0}
    if not fields or not vault.exists():
        log.info("no frontmatter fields configured; skipping normalization")
        return stats
    for md in vault.rglob("*.md"):
        if md.name.startswith("_"):
            continue
        if dry_run:
            continue
        if normalize_file(md, fields):
            stats["files_normalized"] += 1
    log.info("normalized frontmatter in %d files", stats["files_normalized"])
    return stats
