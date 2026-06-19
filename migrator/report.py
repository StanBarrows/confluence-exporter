"""Count reconciliation + QA scans + migration report generation."""
from __future__ import annotations

import logging
import os
import re
import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import unquote

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

_HTML_CSS = """
:root {
  --bg: #f6f7f9; --card: #fff; --text: #1c1e21; --muted: #6b7280;
  --border: #e4e7eb; --accent: #0969da; --ok: #1a7f37; --warn: #b7791f;
  --fail: #cf222e; --info-bg: #eef1f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --card: #161b22; --text: #e6edf3; --muted: #8b949e;
    --border: #30363d; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922;
    --fail: #f85149; --info-bg: #1c2128;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1060px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 12px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 18px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.metric { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.metric b { display: block; font-size: 24px; }
.metric span { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .35px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
th, td { text-align: left; padding: 10px 12px; border-top: 1px solid var(--border); }
th { border-top: 0; background: var(--info-bg); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .35px; }
.ok { color: var(--ok); }
.warn { color: var(--warn); }
.fail { color: var(--fail); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
ul { margin: 8px 0 0; padding-left: 20px; }
footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def reconcile(client: ConfluenceClient, config: Config) -> Dict[str, object]:
    """Compare source counts (via CQL) with what landed in the vault.

    Counts are restricted to the in-scope spaces so the comparison is fair
    (otherwise personal/archived spaces excluded by config inflate the source).
    """
    vault = config.output_path
    scope_rows = client.spaces_in_scope(config.settings)
    keys = [r["key"] for r in scope_rows]

    source = {
        "spaces_in_scope": len(keys),
        "pages": client.count_in_spaces("page", keys),
        "blogposts": client.count_in_spaces("blogpost", keys),
        "comments": client.count_in_spaces("comment", keys),
        "attachments": client.count_in_spaces("attachment", keys),
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
            # cme writes URL-encoded links (e.g. %20 for spaces); decode before
            # checking the filesystem or we get false-positive "broken" reports.
            resolved = (md.parent / unquote(path_part)).resolve()
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


def render_report_html(recon: Dict[str, object], generated: str, extra: str = "") -> str:
    """Render a rich self-contained HTML version of the migration report."""
    source = recon["source"]
    vault = recon["vault"]
    qa = recon.get("qa", {"broken_links": [], "missing_assets": [], "lossy_macros": []})
    total_qa = sum(len(qa.get(key, [])) for key in ("broken_links", "missing_assets", "lossy_macros"))
    health_class = "ok" if total_qa == 0 else "warn"
    parts: List[str] = [
        "<h1>Migration report</h1>",
        f"<div class=\"meta\">Generated {_esc(generated)}</div>",
        "<div class=\"grid\">",
    ]
    for label, value in (
        ("Source spaces", source["spaces_in_scope"]),
        ("Source pages", source["pages"]),
        ("Source blog posts", source["blogposts"]),
        ("Source comments", source["comments"]),
        ("Source attachments", source["attachments"]),
        ("Markdown pages", vault["markdown_files"]),
        ("Asset files", vault["asset_files"]),
        ("Diagram SVGs", vault["diagram_svgs"]),
    ):
        parts.append(f"<div class=\"metric\"><b>{_esc(value)}</b><span>{_esc(label)}</span></div>")
    parts.append("</div>")

    parts.append("<h2>QA Summary</h2>")
    parts.append(
        f"<div class=\"card\"><p class=\"{health_class}\"><b>{total_qa}</b> total QA finding(s).</p>"
        "<table><tr><th>Check</th><th>Count</th></tr>"
    )
    for label, key in (
        ("Broken internal links", "broken_links"),
        ("Missing assets", "missing_assets"),
        ("Pages with lossy-macro leftovers", "lossy_macros"),
    ):
        parts.append(f"<tr><td>{_esc(label)}</td><td>{len(qa.get(key, []))}</td></tr>")
    parts.append("</table></div>")

    for label, key in (
        ("Broken internal links", "broken_links"),
        ("Missing assets", "missing_assets"),
        ("Pages with lossy-macro leftovers", "lossy_macros"),
    ):
        items = list(qa.get(key, []))
        if not items:
            continue
        parts.append(f"<h2>{_esc(label)}</h2><div class=\"card\"><ul>")
        for item in items[:100]:
            parts.append(f"<li><code>{_esc(item)}</code></li>")
        if len(items) > 100:
            parts.append(f"<li>... and {len(items) - 100} more</li>")
        parts.append("</ul></div>")

    parts.append(
        "<h2>Notes</h2><div class=\"card\"><ul>"
        "<li>Investigate page or attachment shortfalls; under-scoped tokens commonly skip restricted or archived content.</li>"
        "<li>Diagram SVG count reflects converted draw.io sources; temporary artifacts are skipped by design.</li>"
        "<li>Lossy macro leftovers require manual review.</li>"
        "</ul></div>"
    )
    if extra:
        parts.append(f"<div class=\"card\"><pre>{_esc(extra)}</pre></div>")
    parts.append("<footer>Generated by the Confluence migrator report renderer.</footer>")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Migration report</title><style>{_HTML_CSS}</style></head>"
        f"<body><div class=\"wrap\">{''.join(parts)}</div></body></html>\n"
    )


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
        "## Source (Confluence, in-scope spaces via CQL)",
        "",
        "| Object | Count |",
        "|--------|------:|",
        f"| Spaces (in scope) | {source['spaces_in_scope']} |",
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
    html_out = config.output_path / "migration_report.html"
    html_out.write_text(render_report_html(recon, now, extra=extra), encoding="utf-8")
    return out
