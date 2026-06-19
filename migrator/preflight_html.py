"""Render preflight reports as self-contained HTML.

Two outputs, both single files with inline CSS (no network/CDN, safe to open
from disk or commit):

* ``render_report_html`` -- one run's full graded checklist.
* ``render_dashboard_html`` -- an index across every preflight run with a
  summary row + verdict badge per run, linking to each run's page.

Pure functions (no I/O) so they are easy to unit-test; the CLI wires them to
the run directories.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Dict, List, Tuple

# status -> (label, css class)
_STATUS = {
    "PASS": ("PASS", "pass"),
    "WARN": ("WARN", "warn"),
    "FAIL": ("FAIL", "fail"),
    "INFO": ("INFO", "info"),
}
_RANK = {"INFO": 0, "PASS": 1, "WARN": 2, "FAIL": 3}

_CSS = """
:root {
  --bg: #f6f7f9; --card: #fff; --text: #1c1e21; --muted: #6b7280;
  --border: #e4e7eb; --pass: #1a7f37; --warn: #b7791f; --fail: #cf222e;
  --info: #57606a; --pass-bg: #e8f5ec; --warn-bg: #fdf3e2; --fail-bg: #fde8e8;
  --info-bg: #eef1f4; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --card: #161b22; --text: #e6edf3; --muted: #8b949e;
    --border: #30363d; --pass: #3fb950; --warn: #d29922; --fail: #f85149;
    --info: #8b949e; --pass-bg: #12261a; --warn-bg: #2b2310; --fail-bg: #2d1213;
    --info-bg: #1c2128; --accent: #58a6ff;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 12px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.badge { display: inline-block; padding: 4px 12px; border-radius: 999px;
  font-weight: 700; font-size: 13px; letter-spacing: .3px; }
.badge.pass { background: var(--pass-bg); color: var(--pass); }
.badge.warn { background: var(--warn-bg); color: var(--warn); }
.badge.fail { background: var(--fail-bg); color: var(--fail); }
.badge.info { background: var(--info-bg); color: var(--info); }
.summary { display: flex; gap: 10px; flex-wrap: wrap; margin: 16px 0 8px; }
.chip { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 14px; font-size: 13px; }
.chip b { font-size: 18px; display: block; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  overflow: hidden; margin-bottom: 16px; }
.row { display: flex; gap: 12px; align-items: flex-start; padding: 12px 16px;
  border-top: 1px solid var(--border); }
.row:first-child { border-top: none; }
.pill { flex: 0 0 auto; min-width: 52px; text-align: center; padding: 2px 8px;
  border-radius: 6px; font-size: 11px; font-weight: 700; }
.pill.pass { background: var(--pass-bg); color: var(--pass); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.fail { background: var(--fail-bg); color: var(--fail); }
.pill.info { background: var(--info-bg); color: var(--info); }
.body { flex: 1; min-width: 0; }
.name { font-weight: 600; }
.count { color: var(--muted); font-weight: 400; }
.detail { color: var(--muted); font-size: 13px; margin-top: 2px; }
.items { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.tag { font-size: 12px; background: var(--info-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 2px 8px; font-family: ui-monospace, SFMono-Regular, monospace; }
table { width: 100%; border-collapse: collapse; background: var(--card);
  border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
th, td { text-align: left; padding: 12px 16px; border-top: 1px solid var(--border); font-size: 14px; }
th { background: var(--info-bg); border-top: none; font-size: 12px; text-transform: uppercase;
  letter-spacing: .4px; color: var(--muted); }
td a { color: var(--accent); text-decoration: none; font-weight: 600; }
td a:hover { text-decoration: underline; }
.sectiontitle { display: flex; align-items: center; gap: 10px; }
footer { color: var(--muted); font-size: 12px; margin-top: 32px; }
"""


def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def _cls(status: str) -> str:
    return _STATUS.get(status, ("INFO", "info"))[1]


def summarize(payload: Dict) -> Dict[str, object]:
    """Counts per status + worst verdict for a run payload."""
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0}
    worst = "PASS"
    for section in payload.get("sections", []):
        for r in section.get("results", []):
            st = r.get("status", "INFO")
            counts[st] = counts.get(st, 0) + 1
            if _RANK.get(st, 0) > _RANK[worst]:
                worst = st
    return {
        "counts": counts,
        "verdict": payload.get("verdict", worst),
        "generated": payload.get("generated", ""),
    }


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n"
        f"<body>\n<div class=\"wrap\">\n{body}\n</div>\n</body>\n</html>\n"
    )


def render_report_html(payload: Dict, run_id: str = "") -> str:
    """Full standalone HTML page for a single preflight run."""
    info = summarize(payload)
    verdict = info["verdict"]
    counts = info["counts"]

    parts: List[str] = []
    title = f"Preflight report{f' - {run_id}' if run_id else ''}"
    parts.append(f"<h1>Preflight report</h1>")
    sub = []
    if run_id:
        sub.append(f"run <code>{_esc(run_id)}</code>")
    if info["generated"]:
        sub.append(_esc(info["generated"]))
    parts.append(f"<div class=\"meta\">{' &middot; '.join(sub)}</div>")
    parts.append(
        f"<div><span class=\"badge {_cls(verdict)}\">Overall: {_esc(verdict)}</span></div>"
    )
    parts.append("<div class=\"summary\">")
    for st in ("FAIL", "WARN", "PASS", "INFO"):
        parts.append(
            f"<div class=\"chip\"><b>{counts.get(st, 0)}</b>"
            f"<span class=\"badge {_cls(st)}\">{st}</span></div>"
        )
    parts.append("</div>")

    for section in payload.get("sections", []):
        parts.append(f"<h2>{_esc(section.get('title', ''))}</h2>")
        parts.append("<div class=\"card\">")
        for r in section.get("results", []):
            st = r.get("status", "INFO")
            count = r.get("count")
            count_html = f" <span class=\"count\">({_esc(count)})</span>" if count is not None else ""
            items = r.get("items") or []
            items_html = ""
            if items:
                tags = "".join(f"<span class=\"tag\">{_esc(i)}</span>" for i in items)
                items_html = f"<div class=\"items\">{tags}</div>"
            parts.append(
                f"<div class=\"row\"><span class=\"pill {_cls(st)}\">{_esc(st)}</span>"
                f"<div class=\"body\"><div class=\"name\">{_esc(r.get('name',''))}{count_html}</div>"
                f"<div class=\"detail\">{_esc(r.get('detail',''))}</div>{items_html}</div></div>"
            )
        parts.append("</div>")

    parts.append("<footer>Generated by the Confluence migrator preflight visualizer.</footer>")
    return _page(title, "\n".join(parts))


def render_dashboard_html(runs: List[Dict]) -> str:
    """Index page across runs. Each item: {run_id, href, summary}."""
    parts: List[str] = ["<h1>Preflight dashboard</h1>"]
    parts.append(
        f"<div class=\"meta\">{len(runs)} preflight run(s) &middot; newest first</div>"
    )
    if not runs:
        parts.append("<p>No preflight runs found. Run <code>python -m migrator preflight</code> first.</p>")
        return _page("Preflight dashboard", "\n".join(parts))

    parts.append("<table>")
    parts.append(
        "<tr><th>Run</th><th>Generated</th><th>Verdict</th>"
        "<th>Fail</th><th>Warn</th><th>Pass</th></tr>"
    )
    for run in runs:
        s = run["summary"]
        c = s["counts"]
        parts.append(
            f"<tr><td><a href=\"{_esc(run['href'])}\">{_esc(run['run_id'])}</a></td>"
            f"<td>{_esc(s['generated'])}</td>"
            f"<td><span class=\"badge {_cls(s['verdict'])}\">{_esc(s['verdict'])}</span></td>"
            f"<td>{c.get('FAIL', 0)}</td><td>{c.get('WARN', 0)}</td><td>{c.get('PASS', 0)}</td></tr>"
        )
    parts.append("</table>")
    parts.append("<footer>Generated by the Confluence migrator preflight visualizer.</footer>")
    return _page("Preflight dashboard", "\n".join(parts))


# --------------------------------------------------------------------------- #
# I/O helpers used by the CLI
# --------------------------------------------------------------------------- #
def collect_runs(export_root: Path) -> List[Tuple[str, Path, Dict]]:
    """Find every run with a preflight.json, newest run_id first."""
    out: List[Tuple[str, Path, Dict]] = []
    if not export_root.exists():
        return out
    for meta in export_root.glob("*/_meta/preflight.json"):
        run_id = meta.parent.parent.name
        if run_id == "latest":
            continue
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append((run_id, meta.parent.parent, payload))
    out.sort(key=lambda t: t[0], reverse=True)
    return out
