"""Typed view over config.yml (export structure & policy; no secrets).

Loads the YAML once and exposes nested dataclasses plus a few policy helpers
used across the pipeline (scope filtering, attachment allow/deny, etc.).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


class SettingsError(Exception):
    pass


def _get(node: dict, path: str, default=None):
    """Safe nested lookup: _get(data, 'export.layout.meta_dir')."""
    cur = node
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _norm_ext(name: str) -> str:
    return name.lower().lstrip(".")


# --------------------------------------------------------------------------- #
# Nested config sections
# --------------------------------------------------------------------------- #
@dataclass
class LayoutSettings:
    group_by: str = "space"
    space_folder: str = "{space_name}"
    mirror_hierarchy: bool = True
    page_path: str = "{space}/{ancestors}/{title}.md"
    assets_dir: str = "{space}/assets"
    diagrams_dir: str = "{space}/diagrams"
    meta_dir: str = "_meta"
    index_files: bool = True


@dataclass
class ExportSettings:
    root: str = "export"
    run_id_format: str = "%Y%m%d-%H%M%S"
    keep_latest_symlink: bool = True
    layout: LayoutSettings = field(default_factory=LayoutSettings)


@dataclass
class ScopeSettings:
    include_spaces: List[str] = field(default_factory=list)
    exclude_spaces: List[str] = field(default_factory=list)
    space_types: List[str] = field(
        default_factory=lambda: ["global", "personal", "collaboration", "knowledge_base"]
    )
    include_archived: bool = True
    include_personal: bool = True
    include_blogposts: bool = True
    include_comments: bool = True


@dataclass
class AttachmentSettings:
    download: str = "referenced"
    allow_extensions: List[str] = field(default_factory=list)
    deny_extensions: List[str] = field(default_factory=list)
    allow_media_types: List[str] = field(default_factory=list)
    deny_media_types: List[str] = field(default_factory=list)
    max_file_size_mb: int = 0
    skip_temp_artifacts: bool = True
    filename: str = "{file_id}{ext}"


@dataclass
class JiraSettings:
    enabled: bool = False


@dataclass
class DiagramSettings:
    enabled: bool = True
    source_media_type: str = "application/vnd.jgraph.mxfile"
    output_format: str = "drawio_svg"
    embed_xml: bool = True
    keep_mxfile: bool = True


@dataclass
class LinkSettings:
    rewrite_internal: bool = True
    style: str = "relative"
    rewrite_anchors: bool = True


@dataclass
class MarkdownSettings:
    frontmatter_enabled: bool = True
    frontmatter_fields: List[str] = field(default_factory=list)
    confluence_url: bool = True
    callouts: bool = True
    page_properties_report: str = "snapshot"
    includes: str = "transclusion"
    links: LinkSettings = field(default_factory=LinkSettings)


@dataclass
class GitSettings:
    init: bool = True
    lfs: bool = True
    lfs_extensions: List[str] = field(default_factory=list)
    obsidian_config: bool = True
    commit: bool = True
    commit_message: str = "Initial Confluence migration import"


@dataclass
class AnonymizeSettings:
    enabled: bool = False
    redact_emails: bool = True
    pseudonymize_authors: bool = False
    author_fields: List[str] = field(
        default_factory=lambda: ["author", "creator", "updated_by"]
    )
    redact_patterns: List[str] = field(default_factory=list)


@dataclass
class RuntimeSettings:
    drawio_bin: str = ""
    http_timeout: int = 30
    max_retries: int = 5
    incremental: bool = True
    max_workers: int = 4


# --------------------------------------------------------------------------- #
# Top-level settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    export: ExportSettings = field(default_factory=ExportSettings)
    scope: ScopeSettings = field(default_factory=ScopeSettings)
    attachments: AttachmentSettings = field(default_factory=AttachmentSettings)
    diagrams: DiagramSettings = field(default_factory=DiagramSettings)
    jira: JiraSettings = field(default_factory=JiraSettings)
    markdown: MarkdownSettings = field(default_factory=MarkdownSettings)
    git: GitSettings = field(default_factory=GitSettings)
    anonymize: AnonymizeSettings = field(default_factory=AnonymizeSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

    # -- policy helpers ----------------------------------------------------
    def space_in_scope(self, key: str, space_type: str, status: str) -> bool:
        sc = self.scope
        if sc.include_spaces and key not in sc.include_spaces:
            return False
        if key in sc.exclude_spaces:
            return False
        if space_type and sc.space_types and space_type not in sc.space_types:
            return False
        if space_type == "personal" and not sc.include_personal:
            return False
        if status == "archived" and not sc.include_archived:
            return False
        return True

    def is_attachment_allowed(
        self, title: str, media_type: str = "", size_bytes: int = 0
    ) -> bool:
        a = self.attachments
        title = title or ""
        if a.skip_temp_artifacts and (
            title.endswith(".tmp") or title.startswith("~drawio~")
        ):
            return False

        ext = _norm_ext(os.path.splitext(title)[1]) if "." in title else ""
        allow = {_norm_ext(e) for e in a.allow_extensions}
        deny = {_norm_ext(e) for e in a.deny_extensions}
        if ext in deny:
            return False
        if allow and ext not in allow:
            return False

        mt = (media_type or "").lower()
        deny_mt = {m.lower() for m in a.deny_media_types}
        allow_mt = {m.lower() for m in a.allow_media_types}
        if mt and mt in deny_mt:
            return False
        if allow_mt and mt not in allow_mt:
            return False

        if a.max_file_size_mb and size_bytes:
            if size_bytes > a.max_file_size_mb * 1024 * 1024:
                return False
        return True

    @property
    def lfs_extensions(self) -> List[str]:
        return [_norm_ext(e) for e in self.git.lfs_extensions]

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(cls, path: str = "config.yml") -> "Settings":
        data: dict = {}
        p = Path(path)
        if p.exists():
            try:
                import yaml
            except ModuleNotFoundError as exc:  # pragma: no cover
                raise SettingsError(
                    "PyYAML is required to read config.yml. "
                    "Run: python3 -m pip install -r requirements.txt"
                ) from exc
            with open(p, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}

        layout = LayoutSettings(
            group_by=_get(data, "export.layout.group_by", "space"),
            space_folder=_get(data, "export.layout.space_folder", "{space_name}"),
            mirror_hierarchy=bool(_get(data, "export.layout.mirror_hierarchy", True)),
            page_path=_get(data, "export.layout.page_path", "{space}/{ancestors}/{title}.md"),
            assets_dir=_get(data, "export.layout.assets_dir", "{space}/assets"),
            diagrams_dir=_get(data, "export.layout.diagrams_dir", "{space}/diagrams"),
            meta_dir=_get(data, "export.layout.meta_dir", "_meta"),
            index_files=bool(_get(data, "export.layout.index_files", True)),
        )
        export = ExportSettings(
            root=_get(data, "export.root", "export"),
            run_id_format=_get(data, "export.run_id_format", "%Y%m%d-%H%M%S"),
            keep_latest_symlink=bool(_get(data, "export.keep_latest_symlink", True)),
            layout=layout,
        )
        scope = ScopeSettings(
            include_spaces=list(_get(data, "scope.spaces.include", []) or []),
            exclude_spaces=list(_get(data, "scope.spaces.exclude", []) or []),
            space_types=list(
                _get(data, "scope.spaces.types",
                     ["global", "personal", "collaboration", "knowledge_base"]) or []
            ),
            include_archived=bool(_get(data, "scope.spaces.include_archived", True)),
            include_personal=bool(_get(data, "scope.spaces.include_personal", True)),
            include_blogposts=bool(_get(data, "scope.pages.include_blogposts", True)),
            include_comments=bool(_get(data, "scope.pages.include_comments", True)),
        )
        attachments = AttachmentSettings(
            download=_get(data, "attachments.download", "referenced"),
            allow_extensions=list(_get(data, "attachments.allow_extensions", []) or []),
            deny_extensions=list(_get(data, "attachments.deny_extensions", []) or []),
            allow_media_types=list(_get(data, "attachments.allow_media_types", []) or []),
            deny_media_types=list(_get(data, "attachments.deny_media_types", []) or []),
            max_file_size_mb=int(_get(data, "attachments.max_file_size_mb", 0) or 0),
            skip_temp_artifacts=bool(_get(data, "attachments.skip_temp_artifacts", True)),
            filename=_get(data, "attachments.filename", "{file_id}{ext}"),
        )
        diagrams = DiagramSettings(
            enabled=bool(_get(data, "diagrams.enabled", True)),
            source_media_type=_get(
                data, "diagrams.source_media_type", "application/vnd.jgraph.mxfile"
            ),
            output_format=_get(data, "diagrams.output_format", "drawio_svg"),
            embed_xml=bool(_get(data, "diagrams.embed_xml", True)),
            keep_mxfile=bool(_get(data, "diagrams.keep_mxfile", True)),
        )
        markdown = MarkdownSettings(
            frontmatter_enabled=bool(_get(data, "markdown.frontmatter.enabled", True)),
            frontmatter_fields=list(_get(data, "markdown.frontmatter.fields", []) or []),
            confluence_url=bool(_get(data, "markdown.frontmatter.confluence_url", True)),
            callouts=bool(_get(data, "markdown.callouts", True)),
            page_properties_report=_get(data, "markdown.page_properties_report", "snapshot"),
            includes=_get(data, "markdown.includes", "transclusion"),
            links=LinkSettings(
                rewrite_internal=bool(_get(data, "markdown.links.rewrite_internal", True)),
                style=_get(data, "markdown.links.style", "relative"),
                rewrite_anchors=bool(_get(data, "markdown.links.rewrite_anchors", True)),
            ),
        )
        git = GitSettings(
            init=bool(_get(data, "git.init", True)),
            lfs=bool(_get(data, "git.lfs", True)),
            lfs_extensions=list(_get(data, "git.lfs_extensions", []) or []),
            obsidian_config=bool(_get(data, "git.obsidian_config", True)),
            commit=bool(_get(data, "git.commit", True)),
            commit_message=_get(
                data, "git.commit_message", "Initial Confluence migration import"
            ),
        )
        anonymize = AnonymizeSettings(
            enabled=bool(_get(data, "anonymize.enabled", False)),
            redact_emails=bool(_get(data, "anonymize.redact_emails", True)),
            pseudonymize_authors=bool(_get(data, "anonymize.pseudonymize_authors", False)),
            author_fields=list(
                _get(data, "anonymize.author_fields",
                     ["author", "creator", "updated_by"]) or []
            ),
            redact_patterns=list(_get(data, "anonymize.redact_patterns", []) or []),
        )
        runtime = RuntimeSettings(
            drawio_bin=_get(data, "runtime.drawio_bin", "") or "",
            http_timeout=int(_get(data, "runtime.http_timeout", 30) or 30),
            max_retries=int(_get(data, "runtime.max_retries", 5) or 5),
            incremental=bool(_get(data, "runtime.incremental", True)),
            max_workers=max(1, int(_get(data, "runtime.max_workers", 4) or 4)),
        )
        jira = JiraSettings(
            enabled=bool(_get(data, "jira.enabled", False)),
        )
        return cls(
            export=export,
            scope=scope,
            attachments=attachments,
            diagrams=diagrams,
            jira=jira,
            markdown=markdown,
            git=git,
            anonymize=anonymize,
            runtime=runtime,
        )
