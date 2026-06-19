import json
from pathlib import Path

from migrator.export_html import collect_export_runs, render_export_dashboard


def test_collect_export_runs_and_render_dashboard(tmp_path: Path):
    run = tmp_path / "20260619-120000"
    meta = run / "_meta"
    meta.mkdir(parents=True)
    (meta / "run_manifest.json").write_text(
        json.dumps({
            "steps": [{"name": "export", "status": "completed", "started_at": "now"}],
            "events": [{"time": "now", "level": "INFO", "message": "done"}],
        }),
        encoding="utf-8",
    )
    (meta / "attachments_inventory.csv").write_text("attachment_id,title\n1,a.png\n", encoding="utf-8")
    (meta / "pageid_map.csv").write_text("page_id,path\n42,Page.md\n", encoding="utf-8")
    (run / "Page.md").write_text("[missing](nope.md)\n", encoding="utf-8")

    runs = collect_export_runs(tmp_path)
    assert runs[0]["counts"]["inventory"] == 1
    assert runs[0]["qa"]["broken_links"] == 1

    html = render_export_dashboard(runs)
    assert "Export visualizer dashboard" in html
    assert "20260619-120000/Page.md" in html
    assert "Broken links" in html
