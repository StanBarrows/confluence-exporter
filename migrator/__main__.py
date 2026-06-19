"""Command-line entrypoint: ``python -m migrator <command>``.

Each run writes to ``<export.root>/<run_id>/`` (run_id defaults to a timestamp;
override/resume with --run-id). Structure & policy come from config.yml;
secrets come from .env.

Pipeline (see docs/plan.md):
  spaces     list spaces (current + archived), filtered by scope
  export     run cme org export (Markdown bodies + attachments)
  inventory  write _meta/attachments_inventory.csv via the REST API
  diagrams   download mxfile sources -> convert to .drawio.svg -> rewrite refs
  links      build pageid map + rewrite internal links
  normalize  normalize frontmatter to the configured schema
  index      generate _index.md folder notes
  anonymize  strip/pseudonymize authors + redact (if enabled)
  scaffold   write .gitattributes/.gitignore/.obsidian + git init/commit
  report     reconcile source vs vault + QA scans -> migration_report.md
  all        the whole pipeline, end to end
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Set

from .config import Config, ConfigError
from .settings import SettingsError

log = logging.getLogger("migrator")

# Commands that create fresh output (new timestamped run when no --run-id).
PRODUCER_COMMANDS = {"export", "inventory", "all", "preflight"}
# Producer commands that converge into the latest run when incremental is on.
# (preflight is a diagnostic and intentionally keeps a fresh run each time so
# its history accumulates for the dashboard.)
INCREMENTAL_COMMANDS = {"export", "inventory", "all"}
# Commands that operate on an existing run (resolve to latest when no --run-id).
CONSUMER_COMMANDS = {
    "diagrams", "links", "normalize", "index", "anonymize", "scaffold", "report",
}

_LINK_TARGET = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(message)s")


def _client(config: Config):
    config.require_credentials()
    try:
        from .confluence import ConfluenceClient
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(
            f"Missing Python dependency ({exc.name}). "
            "Run: python3 -m pip install -r requirements.txt"
        )
    return ConfluenceClient(config)


def _resolve_latest_run(root: Path) -> Optional[str]:
    link = root / "latest"
    if link.is_symlink():
        return Path(os.readlink(link)).name
    if root.exists():
        runs = [d.name for d in root.iterdir() if d.is_dir() and d.name != "latest"]
        return sorted(runs)[-1] if runs else None
    return None


def cmd_preflight(config: Config, args) -> int:
    from .preflight import exit_code, grade, run_preflight, write_preflight_report

    client = _client(config)
    sections = run_preflight(
        client, config,
        sample_pages=getattr(args, "sample_pages", 100),
        full=getattr(args, "full", False),
    )
    icon = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "INFO": "INFO"}
    for section in sections:
        log.info("== %s ==", section.title)
        for r in section.results:
            suffix = f" ({r.count})" if r.count is not None else ""
            log.info("  [%s] %s%s: %s", icon[r.status], r.name, suffix, r.detail)
    out = write_preflight_report(config, sections)
    verdict = grade(sections)
    log.info("Overall: %s", verdict)
    log.info("Preflight report -> %s", out)
    return exit_code(sections, getattr(args, "strict", False))


def cmd_preflight_report(config: Config, args) -> int:
    """Render HTML for every preflight run + an aggregate dashboard (no network)."""
    from .preflight_html import (
        collect_runs,
        render_dashboard_html,
        render_report_html,
        summarize,
    )

    export_root = config.export_root
    runs = collect_runs(export_root)
    if not runs:
        log.warning("No preflight runs found under %s. Run `preflight` first.", export_root)
        return 1

    dashboard_runs = []
    for run_id, run_dir, payload in runs:
        html_path = run_dir / "preflight_report.html"
        html_path.write_text(render_report_html(payload, run_id), encoding="utf-8")
        dashboard_runs.append({
            "run_id": run_id,
            "href": f"./{run_id}/preflight_report.html",
            "summary": summarize(payload),
        })
        log.info("  rendered %s", html_path)

    dashboard = export_root / "preflight-dashboard.html"
    dashboard.write_text(render_dashboard_html(dashboard_runs), encoding="utf-8")
    log.info("Dashboard (%d run(s)) -> %s", len(runs), dashboard)

    if getattr(args, "open", False):
        import webbrowser
        webbrowser.open(dashboard.resolve().as_uri())
    return 0


def cmd_spaces(config: Config, args) -> int:
    client = _client(config)
    rows = client.spaces_in_scope(config.settings)
    width = max((len(r["key"]) for r in rows), default=3)
    for r in rows:
        print(f"{r['key']:<{width}}  {r['type']:<13} {r['status']:<9} {r['name']}")
    print(f"\nIn-scope spaces: {len(rows)}")
    return 0


def cmd_export(config: Config, args) -> int:
    from .exporter import export_org, export_space

    config.require_credentials()
    if args.space:
        export_space(config, args.space)
    else:
        export_org(config)
    return 0


def _referenced_targets(vault: Path) -> str:
    """Concatenate every local link target found in the vault (for ref check)."""
    if not vault.exists():
        return ""
    chunks: List[str] = []
    for md in vault.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.extend(_LINK_TARGET.findall(text))
    return "\n".join(chunks)


def cmd_inventory(config: Config, args) -> int:
    settings = config.settings
    download_mode = settings.attachments.download
    out = config.meta_dir / "attachments_inventory.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Cache: skip re-paging the whole instance if an inventory already exists
    # (e.g. on an incremental re-run). Force a refresh with --refresh.
    if out.exists() and not getattr(args, "refresh", False):
        with open(out, newline="", encoding="utf-8") as handle:
            rows = sum(1 for _ in csv.DictReader(handle))
        log.info("Reusing cached inventory (%d rows) -> %s (use --refresh to rebuild)", rows, out)
        return 0

    client = _client(config)

    # For "referenced" policy, determine which attachments pages actually link.
    ref_blob = _referenced_targets(config.output_path) if download_mode == "referenced" else ""

    fields = [
        "attachment_id", "title", "media_type", "file_size",
        "file_id", "page_id", "space", "referenced", "allowed", "download_url",
    ]
    total = allowed_count = 0
    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for att in client.iter_attachments():
            total += 1
            try:
                size = int(att.get("file_size") or 0)
            except ValueError:
                size = 0
            media_type = att.get("media_type", "")
            title = att.get("title", "")
            file_id = att.get("file_id", "")
            referenced = bool(
                (file_id and file_id in ref_blob) or (title and title in ref_blob)
            )
            # draw.io sources are owned entirely by the diagrams pipeline, which
            # converts them to .drawio.svg -- so they are always "allowed" here
            # regardless of the extension/size/referenced policy that governs
            # ordinary attachments (their titles often lack a .drawio extension).
            is_diagram = media_type == settings.diagrams.source_media_type
            if is_diagram:
                allowed = True
            else:
                allowed = settings.is_attachment_allowed(title, media_type, size)
                if download_mode == "none":
                    allowed = False
                elif download_mode == "referenced":
                    allowed = allowed and referenced
            allowed_count += int(allowed)
            att = dict(
                att,
                referenced=str(referenced).lower(),
                allowed=str(allowed).lower(),
            )
            writer.writerow(att)
    log.info("Wrote %d attachments (%d allowed by policy) -> %s", total, allowed_count, out)
    return 0


def _load_inventory(config: Config) -> List[dict]:
    path = config.meta_dir / "attachments_inventory.csv"
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Run `python -m migrator inventory` first "
            "(use --run-id to target the same run)."
        )
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def cmd_diagrams(config: Config, args) -> int:
    from .diagrams import process_diagrams

    client = _client(config)
    inventory = _load_inventory(config)
    stats = process_diagrams(client, config, inventory)
    log.info("Diagrams: %s", stats)
    return 0


def cmd_links(config: Config, args) -> int:
    from .links import build_page_index, rewrite_vault, write_page_id_map

    # Build the page index once and reuse it for both passes.
    index = build_page_index(config.output_path)
    map_path = write_page_id_map(config.output_path, config.meta_dir, index=index)
    stats = rewrite_vault(config.output_path, config.settings.markdown.links, index=index)
    log.info("Page-id map -> %s", map_path)
    log.info("Links: %s", stats)
    return 0


def cmd_normalize(config: Config, args) -> int:
    from .normalize import normalize_vault

    stats = normalize_vault(
        config.output_path,
        config.settings.markdown.frontmatter_fields,
        dry_run=config.dry_run,
    )
    log.info("Normalize: %s", stats)
    return 0


def cmd_index(config: Config, args) -> int:
    from .index import generate_indexes

    if not config.settings.export.layout.index_files:
        log.info("index files disabled in config.yml")
        return 0
    stats = generate_indexes(config.output_path, dry_run=config.dry_run)
    log.info("Index: %s", stats)
    return 0


def cmd_anonymize(config: Config, args) -> int:
    from .anonymize import anonymize_vault

    stats = anonymize_vault(
        config.output_path, config.settings.anonymize, dry_run=config.dry_run
    )
    log.info("Anonymize: %s", stats)
    return 0


def cmd_scaffold(config: Config, args) -> int:
    from .gitops import scaffold_vault

    scaffold_vault(config, do_git=not args.no_git)
    return 0


def cmd_report(config: Config, args) -> int:
    from .report import reconcile, write_report

    client = _client(config)
    recon = reconcile(client, config)
    out = write_report(config, recon)
    log.info("Report -> %s", out)
    log.info("%s", recon)
    return 0


def cmd_all(config: Config, args) -> int:
    steps = (
        ("export", cmd_export),
        ("inventory", cmd_inventory),
        ("diagrams", cmd_diagrams),
        ("links", cmd_links),
        ("normalize", cmd_normalize),
        ("index", cmd_index),
        ("anonymize", cmd_anonymize),
        ("scaffold", cmd_scaffold),
        ("report", cmd_report),
    )
    for name, fn in steps:
        log.info("== %s ==", name)
        fn(config, args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrator", description=__doc__)
    parser.add_argument("--env", default=".env", help="Path to .env (secrets)")
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Target a specific run dir under export.root (default: new timestamp; "
        "consumer commands default to the latest run)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without writing/downloading")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")
    sub = parser.add_subparsers(dest="command", required=True)

    p_preflight = sub.add_parser("preflight", help="Read-only gap analysis before migrating")
    p_preflight.add_argument("--sample-pages", type=int, default=100, help="Page bodies to scan for macros (default 100)")
    p_preflight.add_argument("--full", action="store_true", help="Scan every page body, not a sample")
    p_preflight.add_argument("--strict", action="store_true", help="Exit non-zero on WARN (1) / FAIL (2)")

    p_pf_report = sub.add_parser("preflight-report", help="Render HTML visualizer for all preflight runs (no network)")
    p_pf_report.add_argument("--open", action="store_true", help="Open the dashboard in a browser")

    sub.add_parser("spaces", help="List in-scope spaces (current + archived)")

    p_export = sub.add_parser("export", help="Run cme export")
    p_export.add_argument("--space", help="Export a single space URL/key instead of the whole org")

    p_inventory = sub.add_parser("inventory", help="Write _meta/attachments_inventory.csv")
    p_inventory.add_argument("--refresh", action="store_true", help="Re-page the API even if a cached inventory exists")
    sub.add_parser("diagrams", help="Download + convert draw.io diagrams + rewrite refs")
    sub.add_parser("links", help="Build pageid map + rewrite internal links")
    sub.add_parser("normalize", help="Normalize frontmatter to the configured schema")
    sub.add_parser("index", help="Generate _index.md folder notes")
    sub.add_parser("anonymize", help="Strip/pseudonymize authors + redact (if enabled)")

    p_scaffold = sub.add_parser("scaffold", help="Write git/LFS/Obsidian config")
    p_scaffold.add_argument("--no-git", action="store_true", help="Only write files, do not git init")

    sub.add_parser("report", help="Reconcile counts + QA scans + write migration_report.md")

    p_all = sub.add_parser("all", help="Run the full pipeline")
    p_all.add_argument("--space", help="Limit export to one space URL/key")
    p_all.add_argument("--no-git", action="store_true")
    p_all.add_argument("--refresh", action="store_true", help="Force inventory re-paging")
    return parser


_DISPATCH = {
    "preflight": cmd_preflight,
    "preflight-report": cmd_preflight_report,
    "spaces": cmd_spaces,
    "export": cmd_export,
    "inventory": cmd_inventory,
    "diagrams": cmd_diagrams,
    "links": cmd_links,
    "normalize": cmd_normalize,
    "index": cmd_index,
    "anonymize": cmd_anonymize,
    "scaffold": cmd_scaffold,
    "report": cmd_report,
    "all": cmd_all,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.quiet)
    if not hasattr(args, "space"):
        args.space = None
    if not hasattr(args, "no_git"):
        args.no_git = False
    try:
        config = Config.load(args.env, args.config, run_id=args.run_id, dry_run=args.dry_run)

        run_id = args.run_id
        if not run_id:
            latest = _resolve_latest_run(config.export_root)
            # Consumers reuse the latest run; producers reuse it too when
            # incremental is enabled (so re-runs converge instead of piling up).
            if args.command in CONSUMER_COMMANDS and latest:
                run_id = latest
            elif (
                args.command in INCREMENTAL_COMMANDS
                and latest
                and config.settings.runtime.incremental
            ):
                run_id = latest
                log.info("incremental: reusing latest run %s", latest)
        if run_id and run_id != config.run_id:
            config = Config.load(args.env, args.config, run_id=run_id, dry_run=args.dry_run)

        no_run_dir = {"spaces", "preflight-report"}
        if args.command not in no_run_dir and not config.dry_run:
            config.run_dir.mkdir(parents=True, exist_ok=True)
            config.meta_dir.mkdir(parents=True, exist_ok=True)
            if args.command in PRODUCER_COMMANDS:
                config.update_latest_symlink()
        if args.command not in no_run_dir:
            log.info("Run directory: %s", config.run_dir)

        return _DISPATCH[args.command](config, args)
    except (ConfigError, SettingsError) as exc:
        log.error("Error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
