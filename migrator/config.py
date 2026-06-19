"""Runtime configuration.

Two layers:
  * secrets / connection  -> .env        (CONFLUENCE_URL/USERNAME/API_TOKEN)
  * structure / policy     -> config.yml  (see migrator/settings.py)

Each invocation gets a timestamped run directory: ``<export.root>/<run_id>/``.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .settings import Settings

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


_MACOS_DRAWIO = "/Applications/draw.io.app/Contents/MacOS/draw.io"


def _normalize_wiki_url(url: str) -> str:
    """Ensure an Atlassian Cloud URL includes the ``/wiki`` context path.

    Confluence Cloud's REST API lives under ``<site>/wiki/...``; users commonly
    set ``CONFLUENCE_URL`` to the bare site (no ``/wiki``), which 404s. We fix
    that for ``*.atlassian.net`` hosts and leave self-hosted URLs untouched.
    """
    if not url:
        return url
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if parts.netloc.endswith("atlassian.net") and not path.endswith("/wiki"):
        path = f"{path}/wiki"
    return f"{parts.scheme}://{parts.netloc}{path}"


class ConfigError(Exception):
    """Raised when required configuration is missing."""


@dataclass
class Config:
    confluence_url: str
    username: str
    api_token: str
    settings: Settings
    run_id: str
    run_dir: Path
    dry_run: bool = False

    # -- derived paths ------------------------------------------------------
    @property
    def output_path(self) -> Path:
        """The run directory (staging vault) for this invocation."""
        return self.run_dir

    @property
    def meta_dir(self) -> Path:
        return self.run_dir / self.settings.export.layout.meta_dir

    @property
    def export_root(self) -> Path:
        return Path(self.settings.export.root).expanduser()

    @property
    def site_root(self) -> str:
        parts = urlsplit(self.confluence_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def http_timeout(self) -> int:
        return self.settings.runtime.http_timeout

    @property
    def max_retries(self) -> int:
        return self.settings.runtime.max_retries

    @property
    def max_workers(self) -> int:
        return self.settings.runtime.max_workers

    # -- validation / resolution -------------------------------------------
    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("CONFLUENCE_URL", self.confluence_url),
                ("CONFLUENCE_USERNAME", self.username),
                ("CONFLUENCE_API_TOKEN", self.api_token),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )
        self._require_secure_url()

    def _require_secure_url(self) -> None:
        """Refuse to send the API token over a cleartext connection.

        Basic-auth credentials would otherwise leak on a plain ``http://``
        endpoint. ``localhost``/loopback is allowed for local proxies/tests.
        """
        parts = urlsplit(self.confluence_url)
        if parts.scheme == "https":
            return
        host = (parts.hostname or "").lower()
        if parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
            return
        raise ConfigError(
            f"CONFLUENCE_URL must use https (got '{parts.scheme or 'no scheme'}'). "
            "Sending an API token over a non-TLS connection would leak it."
        )

    def resolve_drawio(self) -> str:
        """Return a usable draw.io binary path or raise."""
        candidate = self.settings.runtime.drawio_bin or os.getenv("DRAWIO_BIN")
        if candidate:
            return candidate
        if os.path.exists(_MACOS_DRAWIO):
            return _MACOS_DRAWIO
        found = shutil.which("drawio")
        if found:
            return found
        raise ConfigError(
            "draw.io binary not found. Install draw.io Desktop or set "
            "runtime.drawio_bin in config.yml (or DRAWIO_BIN in .env)."
        )

    def update_latest_symlink(self) -> None:
        """Point <export.root>/latest at the current run (best effort)."""
        if not self.settings.export.keep_latest_symlink:
            return
        link = self.export_root / "latest"
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(self.run_dir.name)
        except OSError:
            # Symlinks may be unavailable (e.g. Windows without privilege).
            pass

    # -- construction -------------------------------------------------------
    @classmethod
    def load(
        cls,
        env_path: str = ".env",
        config_path: str = "config.yml",
        run_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> "Config":
        if load_dotenv and Path(env_path).exists():
            load_dotenv(env_path)

        settings = Settings.load(config_path)

        rid = run_id or datetime.now().strftime(settings.export.run_id_format)
        export_root = Path(settings.export.root).expanduser()
        run_dir = export_root / rid

        return cls(
            confluence_url=_normalize_wiki_url(os.getenv("CONFLUENCE_URL", "").rstrip("/")),
            username=os.getenv("CONFLUENCE_USERNAME", ""),
            api_token=os.getenv("CONFLUENCE_API_TOKEN", ""),
            settings=settings,
            run_id=rid,
            run_dir=run_dir,
            dry_run=dry_run,
        )

    # Backwards-compatible alias.
    @classmethod
    def from_env(cls, env_path: str = ".env") -> "Config":
        return cls.load(env_path=env_path)
