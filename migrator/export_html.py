"""Self-contained export dashboard for completed migration runs."""
from __future__ import annotations

import csv
import html
import json
import os
from pathlib import Path
from typing import Dict, List

from .manifest import MANIFEST_NAME
from .report import scan_vault


_CSS = """
:root {
  --bg: #f6f7f9; --card: #fff; --text: #1c1e21; --muted: #6b7280;
  --border: #e4e7eb; --accent: #0969da; --ok: #1a7f37; --warn: #b7791f;
  --fail: #cf222e; --info-bg: #eef1f4; --ok-bg: #e8f5ec;
  --warn-bg: #fdf3e2; --fail-bg: #fde8e8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --card: #161b22; --text: #e6edf3; --muted: #8b949e;
    --border: #30363d; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922;
    --fail: #f85149; --info-bg: #1c2128; --ok-bg: #12261a;
    --warn-bg: #2b2310; --fail-bg: #2d1213;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 28px 0 12px; }
h3 { font-size: 15px; margin: 18px 0 8px; }
a { color: var(--accent); text-decoration: none; font-weight: 600; }
a:hover { text-decoration: underline; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 18px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.metric { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.metric b { display: block; font-size: 22px; }
.metric span { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .35px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin: 14px 0; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
th, td { text-align: left; padding: 10px 12px; border-top: 1px solid var(--border); vertical-align: top; }
th { border-top: 0; background: var(--info-bg); color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .35px; }
.badge { display: inline-block; min-width: 78px; text-align: center; padding: 3px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }
.completed { color: var(--ok); background: var(--ok-bg); }
.running { color: var(--warn); background: var(--warn-bg); }
.failed { color: var(--fail); background: var(--fail-bg); }
.muted { color: var(--muted); }
.files { max-height: 340px; overflow: auto; border: 1px solid var(--border); border-radius: 10px; }
.files table { border: 0; border-radius: 0; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
footer { margin-top: 36px; color: var(--muted); font-size: 12px; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _href(path: Path, base: Path) -> str:
    return html.escape(os.path.relpath(path, base).replace(os.sep, "/"))


def _read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _count_csv(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            return sum(1 for _ in csv.DictReader(handle))
    except OSError:
        return 0


def _list_files(run_dir: Path, limit: int = 200) -> List[Dict[str, object]]:
    files: List[Dict[str, object]] = []
    if not run_dir.exists():
        return files
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        if ".git" in path.parts:
            continue
        rel = os.path.relpath(path, run_dir).replace(os.sep, "/")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        files.append({"path": rel, "href": f"{run_dir.name}/{rel}", "size": size})
        if len(files) >= limit:
            break
    return files


def collect_export_runs(export_root: Path) -> List[Dict[str, object]]:
    """Collect dashboard data from every export run, newest run_id first."""
    runs: List[Dict[str, object]] = []
    if not export_root.exists():
        return runs
    for run_dir in sorted((p for p in export_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        if run_dir.name == "latest":
            continue
        meta = run_dir / "_meta"
        manifest = _read_json(meta / MANIFEST_NAME)
        qa = scan_vault(run_dir)
        md_files = [p for p in run_dir.rglob("*.md") if ".git" not in p.parts]
        asset_files = [p for p in run_dir.rglob("assets/*") if p.is_file()]
        diagram_files = [p for p in run_dir.rglob("diagrams/*") if p.is_file()]
        runs.append({
            "run_id": run_dir.name,
            "run_dir": run_dir,
            "manifest": manifest,
            "counts": {
                "markdown": len(md_files),
                "assets": len(asset_files),
                "diagrams": len(diagram_files),
                "inventory": _count_csv(meta / "attachments_inventory.csv"),
                "page_map": _count_csv(meta / "pageid_map.csv"),
                "diagram_map": _count_csv(meta / "diagrams_map.csv"),
            },
            "qa": {
                "broken_links": len(qa.get("broken_links", [])),
                "missing_assets": len(qa.get("missing_assets", [])),
                "lossy_macros": len(qa.get("lossy_macros", [])),
            },
            "files": _list_files(run_dir),
        })
    return runs


def _status_class(status: str) -> str:
    return status if status in {"completed", "running", "failed"} else "running"


def _render_steps(run: Dict[str, object]) -> str:
    manifest = run.get("manifest") or {}
    steps = manifest.get("steps") or []
    if not steps:
        return "<p class=\"muted\">No run manifest found yet.</p>"
    rows = [
        "<table><tr><th>Step</th><th>Status</th><th>Started</th><th>Duration</th><th>Outputs</th></tr>"
    ]
    for step in steps:
        status = step.get("status", "")
        outputs = step.get("outputs") or {}
        output_links = ", ".join(_esc(name) for name in outputs) or "<span class=\"muted\">none</span>"
        rows.append(
            f"<tr><td><code>{_esc(step.get('name'))}</code></td>"
            f"<td><span class=\"badge {_status_class(status)}\">{_esc(status)}</span></td>"
            f"<td>{_esc(step.get('started_at'))}</td>"
            f"<td>{_esc(step.get('duration_seconds'))}</td>"
            f"<td>{output_links}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def _render_events(run: Dict[str, object]) -> str:
    events = (run.get("manifest") or {}).get("events") or []
    if not events:
        return "<p class=\"muted\">No checkpoint events recorded.</p>"
    rows = ["<table><tr><th>Time</th><th>Level</th><th>Message</th></tr>"]
    for event in events[-40:]:
        rows.append(
            f"<tr><td>{_esc(event.get('time'))}</td>"
            f"<td>{_esc(event.get('level'))}</td>"
            f"<td>{_esc(event.get('message'))}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def _render_files(run: Dict[str, object]) -> str:
    files = run.get("files") or []
    if not files:
        return "<p class=\"muted\">No files found in this run.</p>"
    rows = ["<div class=\"files\"><table><tr><th>File</th><th>Size</th></tr>"]
    for item in files:
        rows.append(
            f"<tr><td><a href=\"{_esc(item['href'])}\">{_esc(item['path'])}</a></td>"
            f"<td>{_esc(item['size'])} B</td></tr>"
        )
    rows.append("</table></div>")
    return "\n".join(rows)


def render_export_dashboard(runs: List[Dict[str, object]]) -> str:
    parts: List[str] = [
        "<h1>Export visualizer dashboard</h1>",
        f"<div class=\"meta\">{len(runs)} export run(s) &middot; newest first &middot; generated locally</div>",
    ]
    if not runs:
        parts.append("<p>No export runs found yet.</p>")
    for run in runs:
        counts = run["counts"]
        qa = run["qa"]
        parts.append(f"<h2>Run <code>{_esc(run['run_id'])}</code></h2>")
        parts.append("<div class=\"grid\">")
        for label, key in (
            ("Markdown files", "markdown"),
            ("Assets", "assets"),
            ("Diagram files", "diagrams"),
            ("Inventory rows", "inventory"),
            ("Mapped pages", "page_map"),
            ("Mapped diagrams", "diagram_map"),
        ):
            parts.append(f"<div class=\"metric\"><b>{_esc(counts[key])}</b><span>{_esc(label)}</span></div>")
        for label, key in (
            ("Broken links", "broken_links"),
            ("Missing assets", "missing_assets"),
            ("Lossy macros", "lossy_macros"),
        ):
            parts.append(f"<div class=\"metric\"><b>{_esc(qa[key])}</b><span>{_esc(label)}</span></div>")
        parts.append("</div>")
        parts.append("<div class=\"card\"><h3>Checkpoints</h3>" + _render_steps(run) + "</div>")
        parts.append("<div class=\"card\"><h3>Step log</h3>" + _render_events(run) + "</div>")
        parts.append("<div class=\"card\"><h3>File browser</h3>" + _render_files(run) + "</div>")
    parts.append("<footer>Generated by the Confluence migrator export visualizer.</footer>")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Export visualizer dashboard</title><style>{_CSS}</style></head>"
        f"<body><div class=\"wrap\">{''.join(parts)}</div></body></html>\n"
    )
