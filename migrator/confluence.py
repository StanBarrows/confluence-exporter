"""Minimal Confluence Cloud REST client (read-only).

Handles the bits cme does not: space enumeration (incl. archived), CQL search,
attachment inventory, and authenticated downloads. Resilient to 429/5xx with
exponential backoff and cursor-based pagination.
"""
from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter

from .config import Config
from .settings import Settings

log = logging.getLogger("migrator.confluence")

MXFILE_MEDIA_TYPE = "application/vnd.jgraph.mxfile"

# Status codes whose response body may carry sensitive detail and must not be
# echoed into logs/stderr.
_SENSITIVE_STATUS = {401, 403}


class ConfluenceError(Exception):
    pass


class ConfluenceClient:
    def __init__(self, config: Config):
        self.cfg = config
        self.session = requests.Session()
        self.session.auth = (config.username, config.api_token)
        self.session.headers.update({"Accept": "application/json"})
        # Size the connection pool for parallel downloads.
        pool = max(10, config.max_workers * 2)
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- low level ----------------------------------------------------------
    def _get(self, url: str, params: Optional[dict] = None, stream: bool = False):
        last_err = None
        for attempt in range(self.cfg.max_retries):
            try:
                resp = self.session.get(
                    url, params=params, timeout=self.cfg.http_timeout, stream=stream
                )
            except requests.RequestException as exc:  # network hiccup
                last_err = exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(max(wait, 1))
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                # Avoid leaking response bodies for auth failures (may echo
                # tokens/headers); other errors get a short, truncated body.
                detail = (
                    "(body suppressed)"
                    if resp.status_code in _SENSITIVE_STATUS
                    else resp.text[:200]
                )
                raise ConfluenceError(
                    f"GET {self._safe_url(url)} -> {resp.status_code}: {detail}"
                )
            return resp
        raise ConfluenceError(
            f"GET {self._safe_url(url)} failed after {self.cfg.max_retries} "
            f"retries ({last_err})"
        )

    @staticmethod
    def _safe_url(url: str) -> str:
        """Strip query strings (cursors/params) from URLs before logging."""
        parts = urlparse(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _absolute(self, link: str) -> str:
        if link.startswith("http"):
            return link
        if link.startswith("/wiki"):
            return self.cfg.site_root + link
        return self.cfg.confluence_url + link

    # -- spaces -------------------------------------------------------------
    def get_spaces(self, status: str = "current") -> List[dict]:
        url = f"{self.cfg.confluence_url}/api/v2/spaces"
        params: Optional[dict] = {"limit": 250, "status": status}
        out: List[dict] = []
        while True:
            data = self._get(url, params=params).json()
            out.extend(data.get("results", []))
            nxt = data.get("_links", {}).get("next")
            if not nxt:
                break
            url = self._absolute(nxt)
            params = None
        return out

    def spaces_in_scope(self, settings: Settings) -> List[dict]:
        """Enumerate current + archived spaces, filtered by config scope."""
        rows: List[dict] = []
        for status in ("current", "archived"):
            for sp in self.get_spaces(status):
                key = sp.get("key", "")
                stype = sp.get("type", "")
                if settings.space_in_scope(key, stype, status):
                    rows.append(
                        {
                            "key": key,
                            "type": stype,
                            "status": status,
                            "name": sp.get("name", ""),
                        }
                    )
        return rows

    # -- search -------------------------------------------------------------
    def search_cql(
        self, cql: str, limit: int = 100, expand: Optional[str] = None
    ) -> Iterator[dict]:
        url = f"{self.cfg.confluence_url}/rest/api/search"
        params: dict = {"cql": cql, "limit": limit}
        if expand:
            params["expand"] = expand
        while True:
            data = self._get(url, params=params).json()
            for item in data.get("results", []):
                yield item
            nxt = data.get("_links", {}).get("next")
            if not nxt:
                break
            cursor = parse_qs(urlparse(nxt).query).get("cursor", [None])[0]
            if not cursor:
                break
            params = {"cql": cql, "limit": limit, "cursor": cursor}
            if expand:
                params["expand"] = expand

    def count(self, cql: str) -> int:
        data = self._get(
            f"{self.cfg.confluence_url}/rest/api/search",
            params={"cql": cql, "limit": 1},
        ).json()
        return int(data.get("totalSize", 0))

    @staticmethod
    def _cql_space_in(keys: List[str]) -> str:
        """Build a ``space in ("A","B")`` CQL clause (keys safely quoted)."""
        quoted = ",".join('"' + k.replace('"', "") + '"' for k in keys if k)
        return f"space in ({quoted})"

    def count_in_spaces(self, content_type: str, keys: List[str]) -> int:
        """Count content of a type restricted to the given space keys."""
        if not keys:
            return 0
        cql = f"type={content_type} and {self._cql_space_in(keys)}"
        return self.count(cql)

    # -- pages / bodies -----------------------------------------------------
    def current_user(self) -> Dict[str, str]:
        """Return the authenticated user (displayName/email) or empty dict."""
        try:
            data = self._get(
                f"{self.cfg.confluence_url}/rest/api/user/current"
            ).json()
        except ConfluenceError:
            return {}
        return {
            "account_id": data.get("accountId", ""),
            "display_name": data.get("displayName", ""),
            "email": data.get("email", "") or data.get("publicName", ""),
        }

    def get_page_body(self, page_id: str, fmt: str = "storage") -> str:
        """Fetch a page body in the given representation (default storage)."""
        data = self._get(
            f"{self.cfg.confluence_url}/rest/api/content/{page_id}",
            params={"expand": f"body.{fmt}"},
        ).json()
        return ((data.get("body") or {}).get(fmt) or {}).get("value", "") or ""

    # -- attachments --------------------------------------------------------
    _ATTACHMENT_EXPAND = "content.extensions,content.metadata,content.container,content.space"

    def iter_attachments(self) -> Iterator[Dict[str, str]]:
        # Expand extensions/metadata/container or mediaType, fileSize, fileId,
        # and the owning page id come back empty.
        for item in self.search_cql("type=attachment", expand=self._ATTACHMENT_EXPAND):
            content = item.get("content", {}) or {}
            ext = content.get("extensions", {}) or {}
            meta = content.get("metadata", {}) or {}
            title = content.get("title", "")

            media_type = ext.get("mediaType") or meta.get("mediaType") or ""
            if not media_type:
                media_type = self._guess_media_type(title)

            container = content.get("container") or {}
            page_id = str(container.get("id") or "")
            if not page_id:
                exp = content.get("_expandable", {}).get("container", "") or ""
                page_id = exp.rsplit("/", 1)[-1] if exp else ""

            download = content.get("_links", {}).get("download", "") or ""
            yield {
                "attachment_id": str(content.get("id", "")).replace("att", ""),
                "title": title,
                "media_type": media_type,
                "file_size": str(ext.get("fileSize", "") or ""),
                "file_id": ext.get("fileId", ""),
                "page_id": page_id,
                "space": (item.get("resultGlobalContainer") or {}).get("title", ""),
                "download_url": self._absolute(download) if download else "",
            }

    @staticmethod
    def _guess_media_type(title: str) -> str:
        """Best-effort media type from a filename when the API omits it."""
        lower = (title or "").lower()
        if lower.endswith(".drawio"):
            return MXFILE_MEDIA_TYPE
        guessed, _ = mimetypes.guess_type(lower)
        return guessed or ""

    def download(
        self, url: str, dest: Path, expected_size: int = 0, skip_existing: bool = True
    ) -> Path:
        # Skip re-downloading when a non-empty file already exists and (if a
        # size is known) matches -- makes re-runs cheap (incremental).
        if skip_existing and dest.exists() and dest.stat().st_size > 0:
            if not expected_size or dest.stat().st_size == expected_size:
                log.debug("skip existing download: %s", dest.name)
                return dest
        if self.cfg.dry_run:
            log.info("[dry-run] would download -> %s", dest)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._get(url, stream=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    handle.write(chunk)
        tmp.replace(dest)
        return dest
