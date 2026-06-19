"""Build a page-id -> file map and rewrite internal Confluence links.

cme already converts most links; this pass repairs any remaining absolute
Confluence URLs, tiny links (``/x/<token>``), ``pageId=`` query links, and
in-page anchors. Output style follows config.yml `markdown.links`.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional

from .settings import LinkSettings

log = logging.getLogger("migrator.links")

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_PAGE_ID_IN_URL = re.compile(r"/pages/(\d+)")

# Markdown links whose target points at a Confluence page in various shapes.
_MD_LINK_PAGES = re.compile(
    r"\[(?P<text>[^\]]*)\]\((?P<href>[^)]*?/pages/(?P<pid>\d+)(?P<frag>[^)]*))\)"
)
_MD_LINK_PAGEID = re.compile(
    r"\[(?P<text>[^\]]*)\]\((?P<href>[^)]*?[?&]pageId=(?P<pid>\d+)(?P<frag>[^)]*))\)"
)
_MD_LINK_TINY = re.compile(
    r"\[(?P<text>[^\]]*)\]\((?P<href>[^)]*?/x/(?P<token>[A-Za-z0-9_-]+)(?P<frag>[^)]*))\)"
)


def parse_frontmatter(text: str) -> Dict[str, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    fields: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip("'\"")
    return fields


def _page_id_from(fields: Dict[str, str]) -> str:
    pid = fields.get("source_id") or fields.get("id") or ""
    if pid:
        return str(pid).strip()
    match = _PAGE_ID_IN_URL.search(fields.get("source", "") or fields.get("url", ""))
    return match.group(1) if match else ""


def decode_tiny_link(token: str) -> str:
    """Best-effort decode of a Confluence tiny link token to a page id.

    Tiny links encode the content id as URL-safe base64 of the little-endian
    8-byte id. Returns "" on any failure -- callers only use the result if it
    matches a known page, so a wrong guess is harmless (link left unchanged).
    """
    try:
        token = token.split("/")[0]
        padding = "=" * (-len(token) % 4)
        data = base64.urlsafe_b64decode(token + padding)
        data = data + b"\x00" * (8 - len(data))
        return str(int.from_bytes(data[:8], "little"))
    except Exception:
        return ""


def build_page_index(vault: Path) -> Dict[str, Path]:
    """Map Confluence page id -> markdown file path."""
    index: Dict[str, Path] = {}
    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pid = _page_id_from(parse_frontmatter(text))
        if pid:
            index[pid] = md
    return index


def _anchor(frag: str, keep_anchor: bool) -> str:
    if not keep_anchor:
        return ""
    pos = frag.find("#")
    return frag[pos:] if pos != -1 else ""


def _format_link(label: str, target: Path, anchor: str, md: Path, opts: LinkSettings) -> str:
    if opts.style == "wikilink":
        return f"[[{target.stem}{anchor}|{label}]]"
    rel = os.path.relpath(target, md.parent)
    return f"[{label}]({rel}{anchor})"


def rewrite_file(md: Path, index: Dict[str, Path], opts: LinkSettings) -> int:
    text = md.read_text(encoding="utf-8", errors="replace")
    total = 0

    def repl_by_pid(match: "re.Match[str]") -> str:
        target = index.get(match.group("pid"))
        if not target:
            return match.group(0)
        anchor = _anchor(match.group("frag") or "", opts.rewrite_anchors)
        return _format_link(match.group("text"), target, anchor, md, opts)

    def repl_tiny(match: "re.Match[str]") -> str:
        pid = decode_tiny_link(match.group("token"))
        target = index.get(pid)
        if not target:
            return match.group(0)
        anchor = _anchor(match.group("frag") or "", opts.rewrite_anchors)
        return _format_link(match.group("text"), target, anchor, md, opts)

    for pattern, fn in (
        (_MD_LINK_PAGES, repl_by_pid),
        (_MD_LINK_PAGEID, repl_by_pid),
        (_MD_LINK_TINY, repl_tiny),
    ):
        text, n = pattern.subn(fn, text)
        total += n

    if total:
        md.write_text(text, encoding="utf-8")
    return total


def rewrite_vault(
    vault: Path,
    opts: Optional[LinkSettings] = None,
    index: Optional[Dict[str, Path]] = None,
) -> Dict[str, int]:
    opts = opts or LinkSettings()
    stats = {"pages_indexed": 0, "links_rewritten": 0, "files_touched": 0}
    if not opts.rewrite_internal:
        log.info("internal link rewriting disabled in config.yml")
        return stats
    if index is None:
        index = build_page_index(vault)
    stats["pages_indexed"] = len(index)
    for md in vault.rglob("*.md"):
        changed = rewrite_file(md, index, opts)
        if changed:
            stats["links_rewritten"] += changed
            stats["files_touched"] += 1
    return stats


def write_page_id_map(
    vault: Path, meta_dir: Path, index: Optional[Dict[str, Path]] = None
) -> Path:
    if index is None:
        index = build_page_index(vault)
    meta_dir.mkdir(parents=True, exist_ok=True)
    out = meta_dir / "pageid_map.csv"
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("page_id,relative_path\n")
        for pid, path in sorted(index.items()):
            rel = os.path.relpath(path, vault)
            handle.write(f"{pid},{rel}\n")
    return out
