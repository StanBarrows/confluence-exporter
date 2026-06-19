"""draw.io diagram handling: download mxfile sources and convert to editable SVG.

A ``.drawio.svg`` is a valid SVG (renders as an image everywhere) that also
embeds the editable draw.io XML, so a single file is both the picture and the
source you keep editing locally. Behaviour is driven by config.yml `diagrams:`.

Downloads and conversions run in parallel (``runtime.max_workers``), unchanged
sources are skipped via a content-hash cache, and -- once converted -- the
diagram macros in the page bodies are rewritten to embed the ``.drawio.svg``.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .confluence import ConfluenceClient
from .config import Config
from .settings import LinkSettings
from .utils import file_sha256, safe_join, safe_name, strip_ext

log = logging.getLogger("migrator.diagrams")

# draw.io export format per config `diagrams.output_format`.
_FORMAT_EXT = {
    "drawio_svg": ".drawio.svg",
    "drawio_png": ".drawio.png",
}


@dataclass
class DiagramResult:
    page_id: str
    space: str
    base: str
    out_path: Path


def is_temp_artifact(title: str) -> bool:
    title = title or ""
    return title.endswith(".tmp") or title.startswith("~drawio~")


def build_convert_cmd(drawio_bin: str, mxfile: Path, out: Path, fmt: str, embed: bool) -> List[str]:
    """draw.io CLI argv. ``--embed-diagram`` must precede ``--output`` so the
    output flag does not consume it as its value."""
    export_fmt = "png" if fmt == "drawio_png" else "svg"
    cmd = [drawio_bin, "--export", "--format", export_fmt]
    if embed:
        cmd.append("--embed-diagram")
    cmd += ["--output", str(out), str(mxfile)]
    return cmd


def convert(drawio_bin: str, mxfile: Path, out: Path, fmt: str, embed: bool) -> Path:
    """Convert an mxfile to svg/png, optionally embedding the editable XML."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_convert_cmd(drawio_bin, mxfile, out, fmt, embed)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"draw.io export failed for {mxfile.name}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return out


def process_diagrams(
    client: ConfluenceClient,
    config: Config,
    inventory: List[Dict[str, str]],
) -> Dict[str, int]:
    """Download every in-scope diagram source and convert per config.

    Layout: ``<run>/<space>/diagrams/<name>.drawio.svg`` (or .png / raw mxfile).
    """
    dcfg = config.settings.diagrams
    stats = {
        "found": 0, "converted": 0, "skipped_temp": 0,
        "skipped_policy": 0, "skipped_cached": 0, "failed": 0,
        "kept_mxfile_only": 0,
    }
    if not dcfg.enabled:
        log.info("diagrams disabled in config.yml")
        return stats

    keep_only_mxfile = dcfg.output_format == "keep_mxfile"
    drawio_bin = None if keep_only_mxfile else config.resolve_drawio()
    out_ext = _FORMAT_EXT.get(dcfg.output_format, ".drawio.svg")

    # Filter to the diagrams we actually intend to process.
    todo = []
    for att in inventory:
        if att.get("media_type") != dcfg.source_media_type:
            continue
        stats["found"] += 1
        title = att.get("title", "")
        if att.get("skip_temp") or is_temp_artifact(title):
            stats["skipped_temp"] += 1
            continue
        if str(att.get("allowed", "true")).lower() == "false":
            stats["skipped_policy"] += 1
            continue
        if not att.get("download_url"):
            stats["failed"] += 1
            continue
        todo.append(att)

    lock = threading.Lock()
    results: List[DiagramResult] = []

    def handle(att: Dict[str, str]) -> None:
        title = att.get("title", "")
        space = att.get("space") or "_unknown"
        space_dir = safe_join(config.output_path, safe_name(space))
        diagrams_dir = space_dir / "diagrams"
        base = safe_name(strip_ext(title))
        # Namespace the file by a stable attachment id so duplicate diagram
        # titles (common across pages) don't overwrite each other on disk.
        att_id = att.get("attachment_id") or att.get("file_id") or ""
        file_base = safe_name(f"{strip_ext(title)}-{att_id}") if att_id else base
        mxfile_path = safe_join(diagrams_dir, f"{file_base}.drawio")
        try:
            try:
                expected = int(att.get("file_size") or 0)
            except ValueError:
                expected = 0
            client.download(att["download_url"], mxfile_path, expected_size=expected)
            if keep_only_mxfile:
                with lock:
                    stats["kept_mxfile_only"] += 1
                return

            out_path = safe_join(diagrams_dir, f"{file_base}{out_ext}")
            hash_path = mxfile_path.with_suffix(mxfile_path.suffix + ".sha256")
            src_hash = file_sha256(mxfile_path)

            # Cache: skip conversion if output exists and source is unchanged.
            cached = (
                out_path.exists()
                and hash_path.exists()
                and hash_path.read_text(encoding="utf-8").strip() == src_hash
            )
            if cached:
                with lock:
                    stats["skipped_cached"] += 1
                results.append(DiagramResult(att.get("page_id", ""), space, base, out_path))
                return

            if config.dry_run:
                log.info("[dry-run] would convert %s -> %s", mxfile_path.name, out_path.name)
            else:
                convert(drawio_bin, mxfile_path, out_path, dcfg.output_format, dcfg.embed_xml)
                hash_path.write_text(src_hash, encoding="utf-8")
                if not dcfg.keep_mxfile:
                    mxfile_path.unlink(missing_ok=True)
            with lock:
                stats["converted"] += 1
            results.append(DiagramResult(att.get("page_id", ""), space, base, out_path))
        except Exception as exc:  # keep going; report at the end
            with lock:
                stats["failed"] += 1
            log.warning("diagram '%s' failed: %s", title, exc)

    workers = max(1, min(config.max_workers, len(todo))) if todo else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(handle, att) for att in todo]
        for fut in as_completed(futures):
            fut.result()

    _write_diagram_map(config, results)
    if not keep_only_mxfile and results:
        rewritten = rewrite_diagram_refs(
            config.output_path, results, config.settings.markdown.links,
            dry_run=config.dry_run,
        )
        stats["refs_rewritten"] = rewritten

    return stats


def _write_diagram_map(config: Config, results: List[DiagramResult]) -> None:
    if config.dry_run:
        return
    config.meta_dir.mkdir(parents=True, exist_ok=True)
    out = config.meta_dir / "diagrams_map.csv"
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["page_id", "space", "base", "relative_path"])
        for r in results:
            rel = os.path.relpath(r.out_path, config.output_path)
            writer.writerow([r.page_id, r.space, r.base, rel])


# --- rewrite diagram references in page bodies ----------------------------- #

def _embed_ref(target: Path, page: Path, opts: LinkSettings) -> str:
    if opts.style == "wikilink":
        return f"![[{target.stem}]]"
    rel = os.path.relpath(target, page.parent)
    return f"![{target.stem}]({rel})"


def rewrite_diagram_refs(
    vault: Path,
    results: List[DiagramResult],
    opts: Optional[LinkSettings] = None,
    dry_run: bool = False,
) -> int:
    """Point each page's diagram image at the editable ``.drawio.svg``.

    cme renders a diagram macro as a preview image (e.g. ``assets/<id>.png``)
    or a bare reference to the source name. We replace any markdown image whose
    basename matches the diagram with an embed of the converted file. Diagrams
    we cannot locate in their page are appended under a "Diagrams" section so
    nothing is silently dropped.
    """
    from .links import build_page_index

    opts = opts or LinkSettings()
    index = build_page_index(vault)
    rewritten = 0

    by_page: Dict[str, List[DiagramResult]] = {}
    for r in results:
        by_page.setdefault(r.page_id, []).append(r)

    for page_id, diagrams in by_page.items():
        page = index.get(page_id)
        if not page or not page.exists():
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        original = text
        appended: List[str] = []
        for d in diagrams:
            embed = _embed_ref(d.out_path, page, opts)
            # Replace any image whose target basename matches this diagram.
            pattern = re.compile(
                r"!\[[^\]]*\]\([^)]*"
                + re.escape(d.base)
                + r"[^)]*\)"
            )
            text, n = pattern.subn(embed, text)
            if n:
                rewritten += n
            else:
                appended.append(embed)
        if appended:
            block = "\n\n## Diagrams\n\n" + "\n\n".join(appended) + "\n"
            if block.strip() not in text:
                text = text.rstrip() + "\n" + block
                rewritten += len(appended)
        if text != original and not dry_run:
            page.write_text(text, encoding="utf-8")

    return rewritten
