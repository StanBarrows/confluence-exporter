"""Run manifest and checkpoint helpers.

The manifest is a small JSON file in ``_meta/run_manifest.json``. It is durable
state for dashboards and interrupted runs: every pipeline step records when it
started, whether it completed, useful stats, outputs, and any error message.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional


MANIFEST_NAME = "run_manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _path(config) -> Path:
    return config.meta_dir / MANIFEST_NAME


def _empty(config) -> Dict[str, object]:
    return {
        "schema": 1,
        "run_id": config.run_id,
        "run_dir": str(config.run_dir),
        "dry_run": bool(config.dry_run),
        "created_at": _now(),
        "updated_at": _now(),
        "steps": [],
        "events": [],
    }


def load_manifest(config) -> Dict[str, object]:
    path = _path(config)
    if not path.exists():
        return _empty(config)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = _empty(config)
        manifest["events"].append({
            "time": _now(),
            "level": "WARN",
            "message": "Existing run manifest could not be parsed; started a fresh manifest.",
        })
        return manifest


def save_manifest(config, manifest: Dict[str, object]) -> Path:
    path = _path(config)
    manifest["updated_at"] = _now()
    if config.dry_run:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _step(manifest: Dict[str, object], name: str) -> Dict[str, object]:
    steps = manifest.setdefault("steps", [])
    for step in reversed(steps):
        if step.get("name") == name:
            return step
    step = {"name": name}
    steps.append(step)
    return step


def start_step(config, name: str, command: Optional[str] = None) -> None:
    manifest = load_manifest(config)
    step = _step(manifest, name)
    step.update({
        "name": name,
        "command": command or name,
        "status": "running",
        "started_at": _now(),
        "finished_at": "",
        "duration_seconds": None,
        "error": "",
    })
    step["_started_monotonic"] = time.monotonic()
    manifest.setdefault("events", []).append({
        "time": step["started_at"],
        "level": "INFO",
        "message": f"Step '{name}' started.",
    })
    save_manifest(config, manifest)


def finish_step(
    config,
    name: str,
    status: str = "completed",
    details: Optional[Dict[str, object]] = None,
    outputs: Optional[Dict[str, str]] = None,
    error: str = "",
) -> None:
    manifest = load_manifest(config)
    step = _step(manifest, name)
    started = step.pop("_started_monotonic", None)
    finished = _now()
    step.update({
        "status": status,
        "finished_at": finished,
        "error": error,
    })
    if started is not None:
        step["duration_seconds"] = round(max(0.0, time.monotonic() - float(started)), 3)
    elif step.get("duration_seconds") is None:
        step["duration_seconds"] = None
    if details:
        step["details"] = details
    if outputs:
        step["outputs"] = outputs
    level = "ERROR" if status == "failed" else "INFO"
    manifest.setdefault("events", []).append({
        "time": finished,
        "level": level,
        "message": f"Step '{name}' {status}.",
    })
    save_manifest(config, manifest)


@contextmanager
def checkpoint(config, name: str, command: Optional[str] = None) -> Iterator[None]:
    start_step(config, name, command=command)
    try:
        yield
    except Exception as exc:
        finish_step(config, name, status="failed", error=str(exc))
        raise
