"""Thin wrapper around `confluence-markdown-exporter` (cme).

We do not reimplement Markdown conversion; cme is the engine. This module
derives cme's configuration from our ``config.yml`` so the layout / attachment /
frontmatter policy the user set is actually honored (cme owns the output):

* export/layout options are injected as ``CME_*`` environment variables
  (validated against cme's own enums) so they apply per-run and never persist;
* credentials are merged into cme's JSON config store (cme keys auth by
  instance base URL, which cannot be expressed as an env var), so a separate
  interactive ``cme config`` step is not required.

A secret-free copy of the derived config is written to the run's ``_meta`` dir
for transparency.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config

log = logging.getLogger("migrator.exporter")


class ExporterError(Exception):
    pass


def cme_available() -> bool:
    return shutil.which("cme") is not None


# Map our config.yml page_path placeholders -> cme's placeholders.
_PLACEHOLDERS = {
    "{space}": "{space_name}",
    "{ancestors}": "{ancestor_titles}",
    "{title}": "{page_title}",
}

# Map our policy values onto cme's accepted enum values.
_ATTACHMENTS_EXPORT = {"all": "all", "referenced": "referenced", "none": "disabled"}
_PROPERTIES_REPORT = {"snapshot": "frozen", "frozen": "frozen", "dataview": "dataview", "both": "frozen"}


def _translate(template: str) -> str:
    for ours, theirs in _PLACEHOLDERS.items():
        template = template.replace(ours, theirs)
    return template


def build_cme_config(config: Config, with_auth: bool = False) -> dict:
    """A representation of what we tell cme (for the sanitized _meta copy/tests).

    Auth is included only when ``with_auth`` is set; the persisted copy in the
    (committable) run dir must never contain the API token.
    """
    cfg: dict = {"export": _export_overrides(config), "connection_config": {
        "max_workers": config.max_workers,
    }}
    if with_auth:
        cfg["auth"] = {
            "confluence": {
                config.site_root: {
                    "username": config.username,
                    "api_token": config.api_token,
                }
            }
        }
    return cfg


def _export_overrides(config: Config) -> Dict[str, object]:
    s = config.settings
    href = s.markdown.links.style if s.markdown.links.style in {"relative", "absolute", "wiki"} else "relative"
    assets_dir = _translate(s.export.layout.assets_dir)
    return {
        "output_path": str(config.output_path),
        "page_path": _translate(s.export.layout.page_path),
        "page_href": href,
        "attachment_path": f"{assets_dir}/{{attachment_file_id}}{{attachment_extension}}",
        "attachment_href": href,
        "attachments_export": _ATTACHMENTS_EXPORT.get(s.attachments.download, "referenced"),
        "page_properties_format": "frontmatter" if s.markdown.frontmatter_enabled else "table",
        "page_properties_report_format": _PROPERTIES_REPORT.get(
            s.markdown.page_properties_report, "frozen"
        ),
        "confluence_url_in_frontmatter": "webui" if s.markdown.confluence_url else "none",
        "include_macro": "transclusion" if s.markdown.includes == "transclusion" else "inline",
        "comments_export": "all" if s.scope.include_comments else "none",
        "skip_unchanged": bool(s.runtime.incremental),
    }


def write_cme_config(config: Config) -> Path:
    """Persist a SANITIZED (no-secrets) view of the cme config into _meta."""
    cfg = build_cme_config(config, with_auth=False)
    config.meta_dir.mkdir(parents=True, exist_ok=True)
    path = config.meta_dir / "cme_config.json"
    if config.dry_run:
        log.info("[dry-run] would write cme config -> %s", path)
        return path
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _env_for_cme(config: Config) -> dict:
    """Process env with CME_* overrides for export + connection settings."""
    env = os.environ.copy()
    for key, value in _export_overrides(config).items():
        env[f"CME_EXPORT__{key.upper()}"] = _env_value(value)
    env["CME_CONNECTION_CONFIG__MAX_WORKERS"] = str(config.max_workers)
    return env


def _cme_config_path() -> Optional[Path]:
    try:
        out = subprocess.run(
            ["cme", "config", "path"], capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    line = (out.stdout or "").strip().splitlines()[-1].strip() if out.stdout else ""
    return Path(line) if line else None


def _ensure_auth_in_store(config: Config) -> None:
    """Merge our credentials into cme's JSON store, keyed by base URL.

    cme selects the right account by matching the host of the URL being
    exported, so the base URL (no ``/wiki``) is the correct key.
    """
    path = _cme_config_path()
    if path is None:
        log.warning(
            "could not locate cme config; run `cme config edit auth.confluence` "
            "once if export cannot authenticate"
        )
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    auth = data.setdefault("auth", {}).setdefault("confluence", {})
    entry = auth.setdefault(config.site_root, {})
    entry["username"] = config.username
    entry["api_token"] = config.api_token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("updated cme credentials for %s (stored in %s)", config.site_root, path)


def _run(args: List[str], config: Config) -> None:
    if not cme_available():
        raise ExporterError(
            "`cme` not found. Install it with: pipx install confluence-markdown-exporter"
        )
    write_cme_config(config)  # sanitized reference copy
    log.info("$ cme %s", " ".join(args))
    if config.dry_run:
        log.info("[dry-run] skipping cme invocation")
        return
    _ensure_auth_in_store(config)
    proc = subprocess.run(["cme", *args], env=_env_for_cme(config))
    if proc.returncode != 0:
        raise ExporterError(f"cme exited with code {proc.returncode}")


def export_org(config: Config) -> None:
    """Export every space cme can see (base URL, no /wiki)."""
    _run(["orgs", config.site_root], config)


def export_space(config: Config, space_url_or_key: str) -> None:
    """Export a single space (use for archived/personal stragglers)."""
    _run(["spaces", space_url_or_key], config)
