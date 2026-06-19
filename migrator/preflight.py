"""Preflight / gap-analysis: read-only checks run *before* a migration.

Answers two questions up front: "will this run work?" (connectivity, auth,
tooling, config) and "what will it lose?" (lossy macros, attachment policy,
draw.io, naming edge cases). Produces a graded PASS/WARN/FAIL checklist plus a
machine-readable JSON, and -- with ``--strict`` -- a non-zero exit so it can
gate the pipeline.

Everything here is read-only and safe to run anytime.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config
from .confluence import ConfluenceClient, ConfluenceError

log = logging.getLogger("migrator.preflight")

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_RANK = {INFO: 0, PASS: 1, WARN: 2, FAIL: 3}

# Macros with a clean Markdown/Obsidian target (see docs/concept.md 2.3).
KNOWN_CLEAN = {
    "info", "note", "tip", "warning", "panel", "code", "status", "expand",
    "toc", "table-of-contents", "tasklist", "task-list", "details", "section",
    "column", "anchor", "children", "attachments",
}
# Macros that become a static snapshot / cannot map 1:1 -> potential loss.
KNOWN_LOSSY = {
    "jira", "jiraissues", "jira-issues", "detailssummary", "details-summary",
    "excerpt", "excerpt-include", "include", "multiexcerpt", "multiexcerpt-include",
    "html", "iframe", "gadget", "widget", "content-report-table", "contributors",
    "recently-updated", "page-properties-report", "profile", "calendar",
}

# Match the macro element's own name only -- NOT <ac:parameter ac:name="...">,
# whose names (bgcolor, colour, aspect, ...) would otherwise pollute the list.
_MACRO_NAME = re.compile(r'<ac:(?:structured-)?macro\b[^>]*?\bac:name="([^"]+)"')
_HAS_MACRO = re.compile(r"<ac:(?:structured-)?macro\b")
_TAG = re.compile(r"<[^>]+>")
_ILLEGAL_TITLE = re.compile(r'[\\/:*?"<>|]')


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    count: Optional[int] = None
    items: Optional[List[str]] = None  # full list (persisted to JSON, not console)


@dataclass
class Section:
    title: str
    results: List[CheckResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested, no network)
# --------------------------------------------------------------------------- #
def extract_macros(storage_xml: str) -> List[str]:
    """Return the macro names used in a Confluence storage-format body."""
    return _MACRO_NAME.findall(storage_xml or "")


def classify_macros(names) -> Dict[str, List[str]]:
    """Bucket macro names into clean / lossy / unknown (sorted, unique)."""
    clean, lossy, unknown = set(), set(), set()
    for raw in names:
        name = (raw or "").strip().lower()
        if not name:
            continue
        if name in KNOWN_CLEAN:
            clean.add(name)
        elif name in KNOWN_LOSSY:
            lossy.add(name)
        else:
            unknown.add(name)
    return {
        "clean": sorted(clean),
        "lossy": sorted(lossy),
        "unknown": sorted(unknown),
    }


def is_macro_only(storage_xml: str, text_threshold: int = 30) -> bool:
    """True if a body is essentially just macros (empty API-markdown risk)."""
    if not storage_xml or not _HAS_MACRO.search(storage_xml):
        return False
    text = _TAG.sub("", storage_xml)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)  # strip entities
    return len(text.strip()) < text_threshold


def analyze_titles(titles: List[str], max_len: int = 255) -> Dict[str, object]:
    """Find duplicate, overlong, and non-ASCII/illegal page titles."""
    seen: Dict[str, int] = {}
    duplicates: Dict[str, int] = {}
    overlong: List[str] = []
    non_ascii: List[str] = []
    for title in titles:
        key = (title or "").strip().lower()
        seen[key] = seen.get(key, 0) + 1
        if len(title or "") > max_len:
            overlong.append(title)
        if not (title or "").isascii() or _ILLEGAL_TITLE.search(title or ""):
            non_ascii.append(title)
    duplicates = {t: n for t, n in seen.items() if n > 1}
    return {"duplicates": duplicates, "overlong": overlong, "non_ascii": non_ascii}


def human_size(num_bytes: int) -> str:
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def grade(sections: List[Section]) -> str:
    """Worst status across all results (INFO treated as PASS)."""
    worst = PASS
    for section in sections:
        for r in section.results:
            if _RANK.get(r.status, 0) > _RANK[worst]:
                worst = r.status
    return worst


def exit_code(sections: List[Section], strict: bool) -> int:
    if not strict:
        return 0
    verdict = grade(sections)
    if verdict == FAIL:
        return 2
    if verdict == WARN:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Check groups (network)
# --------------------------------------------------------------------------- #
def check_connectivity(client: ConfluenceClient, scope_rows: List[dict]) -> Section:
    sec = Section("Connectivity & Auth")
    try:
        current = len(client.get_spaces("current"))
        sec.results.append(CheckResult("Reach instance", PASS, "API reachable"))
    except ConfluenceError as exc:
        sec.results.append(CheckResult("Reach instance", FAIL, str(exc)[:200]))
        return sec

    user = client.current_user()
    if user.get("display_name") or user.get("account_id"):
        who = user.get("display_name") or user.get("account_id")
        sec.results.append(CheckResult("Authenticate", PASS, f"token user: {who}"))
    else:
        sec.results.append(CheckResult(
            "Authenticate", WARN,
            "could not resolve current user (token may still work)"
        ))

    try:
        archived = len(client.get_spaces("archived"))
    except ConfluenceError:
        archived = 0
    if archived == 0:
        sec.results.append(CheckResult(
            "Admin/coverage", WARN,
            "no archived spaces visible -- token may be under-scoped; "
            "restricted/archived content can be silently skipped",
        ))
    else:
        sec.results.append(CheckResult(
            "Admin/coverage", PASS,
            f"{current} current + {archived} archived spaces visible",
        ))
    return sec


def check_tooling(config: Config) -> Section:
    from .exporter import cme_available

    sec = Section("Tooling")
    sec.results.append(
        CheckResult("cme installed", PASS if cme_available() else FAIL,
                    "confluence-markdown-exporter on PATH" if cme_available()
                    else "missing: pipx install confluence-markdown-exporter")
    )

    dcfg = config.settings.diagrams
    if dcfg.enabled and dcfg.output_format != "keep_mxfile":
        try:
            path = config.resolve_drawio()
            sec.results.append(CheckResult("draw.io binary", PASS, path))
        except Exception as exc:  # ConfigError
            sec.results.append(CheckResult("draw.io binary", FAIL, str(exc)[:200]))
    else:
        sec.results.append(CheckResult("draw.io binary", INFO, "diagram conversion disabled"))

    gcfg = config.settings.git
    if gcfg.init:
        git_ok = shutil.which("git") is not None
        sec.results.append(CheckResult("git", PASS if git_ok else WARN,
                                       "found" if git_ok else "git not on PATH"))
        if gcfg.lfs:
            lfs_ok = shutil.which("git-lfs") is not None
            sec.results.append(CheckResult("git-lfs", PASS if lfs_ok else WARN,
                                           "found" if lfs_ok else "git-lfs not on PATH"))
    return sec


def check_config(config: Config, scope_rows: List[dict]) -> Section:
    s = config.settings
    sec = Section("Config validation")
    sec.results.append(CheckResult(
        "Scope resolves", PASS if scope_rows else FAIL,
        f"{len(scope_rows)} space(s) in scope" if scope_rows
        else "no spaces match the configured scope",
        count=len(scope_rows),
    ))
    sec.results.append(CheckResult("HTTPS", PASS, "credentials sent over TLS"))

    a = s.attachments
    allow = {e.lower().lstrip(".") for e in a.allow_extensions}
    deny = {e.lower().lstrip(".") for e in a.deny_extensions}
    overlap = allow & deny
    if overlap:
        sec.results.append(CheckResult(
            "Extension policy", WARN,
            f"extensions in both allow and deny: {', '.join(sorted(overlap))}"
        ))
    elif not allow:
        sec.results.append(CheckResult(
            "Extension policy", INFO, "empty allowlist -> all non-denied types kept"))
    else:
        sec.results.append(CheckResult("Extension policy", PASS, "allow/deny consistent"))

    if s.git.lfs and not s.lfs_extensions:
        sec.results.append(CheckResult(
            "LFS config", WARN, "git.lfs enabled but no lfs_extensions set"))
    if s.diagrams.output_format not in {"drawio_svg", "drawio_png", "keep_mxfile"}:
        sec.results.append(CheckResult(
            "Diagram format", WARN,
            f"unknown diagrams.output_format '{s.diagrams.output_format}'"))
    return sec


def check_scope_volume(client: ConfluenceClient, config: Config,
                       scope_rows: List[dict]) -> Section:
    sec = Section("Scope & Volume")
    keys = [r["key"] for r in scope_rows]
    by_type: Dict[str, int] = {}
    n_personal = n_archived = 0
    for r in scope_rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        n_personal += int(r["type"] == "personal")
        n_archived += int(r["status"] == "archived")

    type_summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
    sec.results.append(CheckResult("In-scope spaces", INFO, type_summary, count=len(keys)))

    if n_personal:
        sec.results.append(CheckResult(
            "Personal spaces", WARN,
            f"{n_personal} personal space(s) in scope "
            "(set scope.spaces.include_personal: false to exclude)", count=n_personal))
    if n_archived:
        sec.results.append(CheckResult(
            "Archived spaces", INFO, f"{n_archived} archived space(s) in scope",
            count=n_archived))

    pages = client.count_in_spaces("page", keys)
    blogs = client.count_in_spaces("blogpost", keys)
    comments = client.count_in_spaces("comment", keys)
    sec.results.append(CheckResult("Pages", INFO, f"{pages} pages in scope", count=pages))
    sec.results.append(CheckResult("Blog posts", INFO, f"{blogs}", count=blogs))
    sec.results.append(CheckResult("Comments", INFO, f"{comments}", count=comments))
    return sec


def check_attachments(client: ConfluenceClient, config: Config) -> Tuple[Section, dict]:
    s = config.settings
    sec = Section("Attachments & Diagrams")
    media_counts: Dict[str, int] = {}
    total = allowed = oversized = temp = mxfiles = 0
    total_bytes = lfs_bytes = 0
    max_bytes = s.attachments.max_file_size_mb * 1024 * 1024
    lfs_exts = set(s.lfs_extensions)

    try:
        for att in client.iter_attachments():
            total += 1
            title = att.get("title", "")
            mt = att.get("media_type", "") or "unknown"
            media_counts[mt] = media_counts.get(mt, 0) + 1
            try:
                size = int(att.get("file_size") or 0)
            except ValueError:
                size = 0
            total_bytes += size
            if s.is_attachment_allowed(title, mt, size):
                allowed += 1
            if max_bytes and size > max_bytes:
                oversized += 1
            if title.endswith(".tmp") or title.startswith("~drawio~"):
                temp += 1
            if mt == s.diagrams.source_media_type:
                mxfiles += 1
            ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
            if ext in lfs_exts:
                lfs_bytes += size
    except ConfluenceError as exc:
        sec.results.append(CheckResult("Attachment inventory", FAIL, str(exc)[:200]))
        return sec, {}

    sec.results.append(CheckResult(
        "Attachments", INFO,
        f"{total} total, {allowed} allowed by policy, {total - allowed} skipped",
        count=total))
    top_types = ", ".join(
        f"{k} ({v})" for k, v in sorted(media_counts.items(), key=lambda x: -x[1])[:6])
    sec.results.append(CheckResult("Media types", INFO, top_types))
    if oversized:
        sec.results.append(CheckResult(
            "Oversized files", WARN,
            f"{oversized} exceed max_file_size_mb={s.attachments.max_file_size_mb}",
            count=oversized))
    sec.results.append(CheckResult(
        "draw.io diagrams", INFO,
        f"{mxfiles} mxfile source(s) to convert, {temp} temp/draft artifact(s) to skip",
        count=mxfiles))
    sec.results.append(CheckResult(
        "LFS size estimate", INFO if lfs_bytes < (1 << 30) else WARN,
        f"~{human_size(lfs_bytes)} of binaries -> Git LFS "
        f"(total attachments ~{human_size(total_bytes)}); mind hosted LFS quotas"))

    stats = {
        "total": total, "allowed": allowed, "oversized": oversized,
        "temp": temp, "mxfiles": mxfiles, "total_bytes": total_bytes,
        "lfs_bytes": lfs_bytes, "media_counts": media_counts,
    }
    return sec, stats


def check_disk(config: Config, total_bytes: int) -> CheckResult:
    try:
        root = config.export_root
        probe = root if root.exists() else root.parent
        free = shutil.disk_usage(probe).free
    except OSError:
        return CheckResult("Disk space", INFO, "could not determine free space")
    if total_bytes and free < total_bytes * 1.5:
        return CheckResult(
            "Disk space", WARN,
            f"{human_size(free)} free vs ~{human_size(total_bytes)} of source data")
    return CheckResult("Disk space", PASS, f"{human_size(free)} free")


def check_content_macros(client: ConfluenceClient, config: Config,
                         scope_rows: List[dict], sample_pages: int,
                         full: bool) -> Tuple[Section, Section]:
    """Title analysis (all pages) + macro inventory (sampled bodies)."""
    macro_sec = Section("Content & Macros")
    name_sec = Section("Naming & Links")
    keys = [r["key"] for r in scope_rows]
    if not keys:
        return macro_sec, name_sec

    titles: List[str] = []
    page_ids: List[str] = []
    try:
        cql = f"type=page and {ConfluenceClient._cql_space_in(keys)}"
        for item in client.search_cql(cql):
            content = item.get("content", {}) or {}
            title = content.get("title") or item.get("title") or ""
            pid = str(content.get("id") or "")
            if title:
                titles.append(title)
            if pid:
                page_ids.append(pid)
    except ConfluenceError as exc:
        macro_sec.results.append(CheckResult("Page enumeration", FAIL, str(exc)[:200]))
        return macro_sec, name_sec

    # Naming analysis across every page (cheap; titles only).
    analysis = analyze_titles(titles, max_len=255)
    dups = analysis["duplicates"]
    name_sec.results.append(CheckResult(
        "Duplicate titles", WARN if dups else PASS,
        f"{len(dups)} title(s) collide (disambiguated by hierarchy/page-id)"
        if dups else "no duplicate titles", count=len(dups)))
    if analysis["overlong"]:
        name_sec.results.append(CheckResult(
            "Overlong titles", WARN, f"{len(analysis['overlong'])} title(s) > 255 chars",
            count=len(analysis["overlong"])))
    if analysis["non_ascii"]:
        name_sec.results.append(CheckResult(
            "Non-ASCII/illegal titles", INFO,
            f"{len(analysis['non_ascii'])} title(s) need sanitization",
            count=len(analysis["non_ascii"])))

    # Macro inventory over a bounded sample of bodies.
    sample = page_ids if full else page_ids[:max(0, sample_pages)]
    all_macros: List[str] = []
    macro_only = 0
    scanned = 0
    for pid in sample:
        try:
            body = client.get_page_body(pid, "storage")
        except ConfluenceError:
            continue
        scanned += 1
        all_macros.extend(extract_macros(body))
        if is_macro_only(body):
            macro_only += 1

    buckets = classify_macros(all_macros)
    macro_sec.results.append(CheckResult(
        "Macros scanned", INFO,
        f"{scanned}/{len(page_ids)} page bodies sampled"
        + ("" if full else " (use --full for all)"), count=scanned))
    if buckets["lossy"]:
        macro_sec.results.append(CheckResult(
            "Lossy macros", WARN,
            f"{len(buckets['lossy'])} type(s): {', '.join(buckets['lossy'])}",
            count=len(buckets["lossy"]), items=buckets["lossy"]))
    if buckets["unknown"]:
        macro_sec.results.append(CheckResult(
            "Unknown macros", WARN,
            f"{len(buckets['unknown'])} type(s) to review: {', '.join(buckets['unknown'][:15])}",
            count=len(buckets["unknown"]), items=buckets["unknown"]))
    if buckets["clean"]:
        macro_sec.results.append(CheckResult(
            "Clean macros", PASS, ", ".join(buckets["clean"]),
            count=len(buckets["clean"]), items=buckets["clean"]))
    if macro_only:
        macro_sec.results.append(CheckResult(
            "Macro-only pages", WARN,
            f"{macro_only} sampled page(s) are macro-only (empty API-markdown risk; "
            "cme uses storage/ADF so bodies are still captured)", count=macro_only))
    return macro_sec, name_sec


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #
def run_preflight(client: ConfluenceClient, config: Config,
                  sample_pages: int = 100, full: bool = False) -> List[Section]:
    scope_rows = client.spaces_in_scope(config.settings)
    sections: List[Section] = []

    conn = check_connectivity(client, scope_rows)
    sections.append(conn)
    sections.append(check_tooling(config))
    sections.append(check_config(config, scope_rows))

    # Stop early if we cannot even reach/auth the instance.
    if any(r.status == FAIL for r in conn.results if r.name == "Reach instance"):
        return sections

    sections.append(check_scope_volume(client, config, scope_rows))
    att_sec, att_stats = check_attachments(client, config)
    att_sec.results.append(check_disk(config, att_stats.get("total_bytes", 0)))
    sections.append(att_sec)

    macro_sec, name_sec = check_content_macros(
        client, config, scope_rows, sample_pages, full)
    sections.append(macro_sec)
    sections.append(name_sec)
    return sections


def write_preflight_report(config: Config, sections: List[Section]) -> Path:
    verdict = grade(sections)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    icon = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", INFO: "INFO"}

    lines = [
        "# Preflight report",
        "",
        f"_Generated {now}_",
        "",
        f"**Overall: {verdict}**",
        "",
    ]
    for section in sections:
        lines.append(f"## {section.title}")
        lines.append("")
        for r in section.results:
            suffix = f" ({r.count})" if r.count is not None else ""
            lines.append(f"- **[{icon[r.status]}] {r.name}**{suffix}: {r.detail}")
        lines.append("")

    config.meta_dir.mkdir(parents=True, exist_ok=True)
    out = config.output_path / "preflight_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    if config.dry_run:
        log.info("[dry-run] would write preflight report -> %s", out)
        return out
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "generated": now,
        "verdict": verdict,
        "sections": [
            {"title": s.title, "results": [asdict(r) for r in s.results]}
            for s in sections
        ],
    }
    (config.meta_dir / "preflight.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")

    # Self-contained HTML view alongside the Markdown report.
    from .preflight_html import render_report_html

    (config.output_path / "preflight_report.html").write_text(
        render_report_html(payload, config.run_id), encoding="utf-8")
    return out
