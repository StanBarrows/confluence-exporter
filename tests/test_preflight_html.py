from pathlib import Path

from migrator.preflight_html import (
    collect_runs,
    render_dashboard_html,
    render_report_html,
    summarize,
)

PAYLOAD = {
    "generated": "2026-06-19 10:19 UTC",
    "verdict": "WARN",
    "sections": [
        {"title": "Tooling", "results": [
            {"name": "cme installed", "status": "PASS", "detail": "ok", "count": None, "items": None},
        ]},
        {"title": "Content & Macros", "results": [
            {"name": "Lossy macros", "status": "WARN", "detail": "3 type(s)",
             "count": 3, "items": ["jira", "contributors", "recently-updated"]},
        ]},
    ],
}


def test_summarize_counts_and_verdict():
    s = summarize(PAYLOAD)
    assert s["verdict"] == "WARN"
    assert s["counts"]["PASS"] == 1
    assert s["counts"]["WARN"] == 1
    assert s["generated"] == "2026-06-19 10:19 UTC"


def test_render_report_html_contains_key_bits():
    html = render_report_html(PAYLOAD, "20260619-121124")
    assert "<!doctype html>" in html
    assert "Overall: WARN" in html
    assert "20260619-121124" in html
    assert "Lossy macros" in html
    # full item list is rendered as tags
    assert "contributors" in html and "recently-updated" in html


def test_render_report_html_escapes():
    payload = {"verdict": "PASS", "generated": "", "sections": [
        {"title": "X", "results": [
            {"name": "<script>", "status": "PASS", "detail": "a & b <z>", "count": None, "items": None}]}]}
    html = render_report_html(payload)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "a &amp; b" in html


def test_render_dashboard_html():
    runs = [{"run_id": "r1", "href": "./r1/preflight_report.html", "summary": summarize(PAYLOAD)}]
    html = render_dashboard_html(runs)
    assert "Preflight dashboard" in html
    assert "./r1/preflight_report.html" in html
    assert "WARN" in html


def test_render_dashboard_empty():
    assert "No preflight runs" in render_dashboard_html([])


def test_collect_runs(tmp_path: Path):
    import json
    for rid in ("20260619-100000", "20260619-120000"):
        meta = tmp_path / rid / "_meta"
        meta.mkdir(parents=True)
        (meta / "preflight.json").write_text(json.dumps(PAYLOAD), encoding="utf-8")
    runs = collect_runs(tmp_path)
    assert [r[0] for r in runs] == ["20260619-120000", "20260619-100000"]  # newest first
