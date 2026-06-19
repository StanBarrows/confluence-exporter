"""Optional anonymization pass for shared/public mirrors.

Strips or pseudonymizes author identities and redacts emails and any
caller-supplied sensitive patterns, before the vault is committed. Disabled by
default (``anonymize.enabled: false``); only meaningful for a private KB you
intend to share.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict

from .settings import AnonymizeSettings
from .utils import stable_pseudonym

log = logging.getLogger("migrator.anonymize")

_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _scrub_author_lines(yaml_block: str, fields, pseudonymize: bool) -> str:
    out_lines = []
    field_set = set(fields)
    for line in yaml_block.splitlines():
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in field_set:
            _, _, value = line.partition(":")
            value = value.strip().strip("'\"")
            if value:
                replacement = stable_pseudonym(value) if pseudonymize else "redacted"
                line = f"{key}: {replacement}"
        out_lines.append(line)
    return "\n".join(out_lines)


def anonymize_text(text: str, opts: AnonymizeSettings) -> str:
    match = _FRONTMATTER.match(text)
    if match and opts.author_fields:
        scrubbed = _scrub_author_lines(
            match.group(1), opts.author_fields, opts.pseudonymize_authors
        )
        text = text[: match.start(1)] + scrubbed + text[match.end(1):]

    if opts.redact_emails:
        text = _EMAIL.sub(
            lambda m: stable_pseudonym(m.group(0), "email") + "@example.invalid"
            if opts.pseudonymize_authors
            else "redacted@example.invalid",
            text,
        )

    for pattern in opts.redact_patterns:
        try:
            text = re.sub(pattern, "[REDACTED]", text)
        except re.error:
            log.warning("invalid redact pattern skipped: %s", pattern)
    return text


def anonymize_vault(
    vault: Path, opts: AnonymizeSettings, dry_run: bool = False
) -> Dict[str, int]:
    stats = {"files_changed": 0}
    if not opts.enabled:
        log.info("anonymization disabled in config.yml")
        return stats
    if not vault.exists():
        return stats
    for md in vault.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        new_text = anonymize_text(text, opts)
        if new_text != text:
            stats["files_changed"] += 1
            if not dry_run:
                md.write_text(new_text, encoding="utf-8")
    log.info("anonymized %d files", stats["files_changed"])
    return stats
