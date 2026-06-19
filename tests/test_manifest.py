import json
from pathlib import Path

from migrator.config import Config
from migrator.manifest import finish_step, start_step
from migrator.settings import Settings


def _config(tmp_path: Path) -> Config:
    return Config(
        confluence_url="https://x.atlassian.net/wiki",
        username="u",
        api_token="t",
        settings=Settings(),
        run_id="run1",
        run_dir=tmp_path / "run1",
    )


def test_manifest_records_step_lifecycle(tmp_path: Path):
    cfg = _config(tmp_path)
    start_step(cfg, "inventory")
    finish_step(
        cfg,
        "inventory",
        details={"rows": 3},
        outputs={"attachments_inventory.csv": "run1/_meta/attachments_inventory.csv"},
    )

    payload = json.loads((cfg.meta_dir / "run_manifest.json").read_text(encoding="utf-8"))
    step = payload["steps"][0]
    assert step["name"] == "inventory"
    assert step["status"] == "completed"
    assert step["details"]["rows"] == 3
    assert payload["events"][0]["message"] == "Step 'inventory' started."
