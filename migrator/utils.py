"""Small shared helpers."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_ILLEGAL = re.compile(r'[\\/:*?"<>|]+')


def safe_name(name: str, maxlen: int = 120) -> str:
    """Make a string safe to use as a file/folder name."""
    cleaned = _ILLEGAL.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned[:maxlen] or "untitled"


def strip_ext(name: str) -> str:
    """Drop a trailing file extension (incl. compound like .drawio.tmp)."""
    for suffix in (".drawio.tmp", ".drawio", ".tmp"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


class PathTraversalError(Exception):
    """Raised when a derived path would escape its base directory."""


def safe_join(base: Path, *parts: str) -> Path:
    """Join path components under ``base`` and guarantee containment.

    Guards against ``..`` / absolute components in attacker-controlled names
    (e.g. page titles, space names) escaping the run directory.
    """
    base = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if base != candidate and base not in candidate.parents:
        raise PathTraversalError(
            f"Refusing to write outside the run directory: {candidate}"
        )
    return candidate


def stable_pseudonym(value: str, prefix: str = "user") -> str:
    """Deterministic, non-reversible pseudonym for a name/email."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def file_sha256(path: Path) -> str:
    """Hex SHA-256 of a file's contents (empty string if unreadable)."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""
