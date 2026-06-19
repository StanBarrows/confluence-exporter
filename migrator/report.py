"""Count reconciliation + QA scans + migration report generation."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .confluence import ConfluenceClient
from .config import Config

log = logging.getLogger("migrator.report")

# Markdown links/images with a local (non-URL) target.
_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
# Raw Confluence storage tags / unsupported-macro markers cme may leave behind.
_LOSSY_MARKERS = (
    re.compile(r"<ac:[a-z-]+", re.IGNORECASE),
    re.compile(r"<ri:[a-z-]+", re.IGNORECASE),
    re.compile(r"<!--\s*(unsupported|unknown|macro)", re.IGNORECASE),
)


def reconcile(client: ConfluenceClient, config: Config) -> Dict[str, object]:
    """Compare source counts (via CQL) with what landed in the vault."""
    vault = config.output_path

    source = {
        "spaces_current": len(client.get_spaces("current")),
        "spaces_archived": len(client.get_spaces("archived")),
        "pages": client.count("type=page"),
        "blogposts": client.count("type=blogpost"),
        "comments": client.count("type=comment"),
        "attachments": client.count("type=attachment"),
    }

    md_files = [
        p for p in vault.rglob("*.md") if not p.name.startswith("_")
    ] if vault.exists() else []
    asset_files = list(vault.rglob("assets/*")) if vault.exists() else []
    diagram_files = list(vault.rglob("diagrams/*.drawio.svg")) if vault.exists() else []

    vaulted = {
        "markdown_files": len(md_files),
        "asset_files": len(asset_files),
        "diagram_svgs": len(diagram_files),
    }
    qa = scan_vault(vault)
    return {"source": source, "vault": vaulted, "qa": qa}


def _is_local_target(target: str) -> bool:
    target = target.strip()
    if not target or target.startswith("#"):
        return False
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "data:")):
        return False
    return True


def scan_vault(vault: Path) -> Dict[str, object]:
    """Find broken local links, missing assets, and lossy-macro leftovers."""
    broken_links: List[str] = []
    missing_assets: List[str] = []
    lossy_macros: List[str] = []
    if not vault.exists():
        return {"broken_links": [], "missing_assets": [], "lossy_macros": []}

    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_md = os.path.relpath(md, vault)

        for marker in _LOSSY_MARKERS:
            if marker.search(text):
                lossy_macros.append(rel_md)
                break

        for match in _LINK.finditer(text):
            raw = match.group("target").split()  # drop optional "title"
            if not raw:
                continue
            target = raw[0]
            if not _is_local_target(target):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (md.parent / path_part).resolve()
            if resolved.exists():
                continue
            entry = f"{rel_md} -> {target}"
            is_asset = (
                "/assets/" in target
                or "/diagrams/" in target
                or not path_part.lower().endswith(".md")
            )
            (missing_assets if is_asset else broken_links).append(entry)

    return {
        "broken_links": broken_links,
        "missing_assets": missing_assets,
        "lossy_macros": lossy_macros,
    }


def _qa_section(qa: Dict[str, object], limit: int = 25) -> List[str]:
    lines = ["## QA scan", ""]
    summary = [
        ("Broken internal links", qa["broken_links"]),
        ("Missing assets", qa["missing_assets"]),
        ("Pages with lossy-macro leftovers", qa["lossy_macros"]),
    ]
    lines += ["| Check | Count |", "|-------|------:|"]
    for label, items in summary:
        lines.append(f"| {label} | {len(items)} |")
    lines.append("")
    for label, items in summary:
        if not items:
            continue
        lines.append(f"### {label} ({len(items)})")
        lines.append("")
        for entry in list(items)[:limit]:
            lines.append(f"- `{entry}`")
        if len(items) > limit:
            lines.append(f"- ... and {len(items) - limit} more")
        lines.append("")
    return lines


def write_report(config: Config, recon: Dict[str, object], extra: str = "") -> Path:
    source = recon["source"]
    vault = recon["vault"]
    qa = recon.get("qa", {"broken_links": [], "missing_assets": [], "lossy_macros": []})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Migration report",
        "",
        f"_Generated {now}_",
        "",
        "## Source (Confluence, via CQL)",
        "",
        "| Object | Count |",
        "|--------|------:|",
        f"| Spaces (current) | {source['spaces_current']} |",
        f"| Spaces (archived) | {source['spaces_archived']} |",
        f"| Pages | {source['pages']} |",
        f"| Blog posts | {source['blogposts']} |",
        f"| Comments | {source['comments']} |",
        f"| Attachments | {source['attachments']} |",
        "",
        "## Vault (produced files)",
        "",
        "| Item | Count |",
        "|------|------:|",
        f"| Markdown pages | {vault['markdown_files']} |",
        f"| Asset files | {vault['asset_files']} |",
        f"| Diagram SVGs | {vault['diagram_svgs']} |",
        "",
    ]
    lines += _qa_section(qa)
    lines += [
        "## Notes",
        "",
        "- Investigate any shortfall in pages/attachments: it usually means an "
        "under-scoped token skipped restricted/archived content.",
        "- Diagram SVG count reflects converted draw.io sources "
        "(temp/draft artifacts are skipped by design).",
        "- Lossy-macro leftovers are pages still containing raw storage tags "
        "(`<ac:.../>`) or unsupported-macro markers; review them by hand.",
    ]
    if extra:
        lines += ["", extra]

    config.meta_dir.mkdir(parents=True, exist_ok=True)
    out = config.output_path / "migration_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if config.dry_run:
        log.info("[dry-run] would write report -> %s", out)
        return out
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
