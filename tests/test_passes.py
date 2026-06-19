from pathlib import Path

from migrator.anonymize import anonymize_text, anonymize_vault
from migrator.config import Config
from migrator.diagrams import (
    DiagramResult,
    build_convert_cmd,
    is_temp_artifact,
    rewrite_diagram_refs,
)
from migrator.index import generate_indexes
from migrator.normalize import normalize_file
from migrator.gitops import scaffold_vault
from migrator.report import render_report_html, scan_vault
from migrator.settings import AnonymizeSettings, LinkSettings, Settings


def test_is_temp_artifact():
    assert is_temp_artifact("~drawio~x")
    assert is_temp_artifact("a.tmp")
    assert not is_temp_artifact("real.drawio")


def test_build_convert_cmd_embed_before_output():
    cmd = build_convert_cmd("drawio", Path("a.drawio"), Path("a.svg"), "drawio_svg", True)
    # --embed-diagram must come before --output so it isn't consumed as its value
    assert cmd.index("--embed-diagram") < cmd.index("--output")
    # --output is immediately followed by the out path, then the input file
    oi = cmd.index("--output")
    assert cmd[oi + 1] == "a.svg"
    assert cmd[-1] == "a.drawio"


def test_build_convert_cmd_no_embed_png():
    cmd = build_convert_cmd("drawio", Path("a.drawio"), Path("a.png"), "drawio_png", False)
    assert "--embed-diagram" not in cmd
    assert "png" in cmd


def test_anonymize_text_redacts_email():
    opts = AnonymizeSettings(enabled=True, redact_emails=True)
    out = anonymize_text("contact me@corp.com", opts)
    assert "me@corp.com" not in out
    assert "example.invalid" in out


def test_anonymize_author_frontmatter():
    opts = AnonymizeSettings(enabled=True, author_fields=["author"])
    text = "---\ntitle: T\nauthor: Jane Doe\n---\nbody"
    out = anonymize_text(text, opts)
    assert "Jane Doe" not in out
    assert "redacted" in out


def test_anonymize_vault_disabled(tmp_path: Path):
    (tmp_path / "a.md").write_text("x@y.com", encoding="utf-8")
    stats = anonymize_vault(tmp_path, AnonymizeSettings(enabled=False))
    assert stats["files_changed"] == 0


def test_normalize_adds_missing_fields(tmp_path: Path):
    md = tmp_path / "p.md"
    md.write_text("---\ntitle: T\n---\nbody\n", encoding="utf-8")
    changed = normalize_file(md, ["title", "tags", "status"])
    assert changed
    text = md.read_text(encoding="utf-8")
    assert "tags:" in text and "status:" in text
    assert text.rstrip().endswith("body")


def test_generate_indexes(tmp_path: Path):
    space = tmp_path / "Space"
    space.mkdir()
    (space / "One.md").write_text("a", encoding="utf-8")
    (space / "Two.md").write_text("b", encoding="utf-8")
    stats = generate_indexes(tmp_path)
    assert stats["indexes_written"] >= 1
    idx = (space / "_index.md").read_text(encoding="utf-8")
    assert "One" in idx and "Two" in idx


def test_generate_indexes_encodes_spaces(tmp_path: Path):
    space = tmp_path / "Space"
    space.mkdir()
    (space / "Marketing And Sales.md").write_text("a", encoding="utf-8")
    generate_indexes(tmp_path)
    idx = (space / "_index.md").read_text(encoding="utf-8")
    # link target must be URL-encoded so the space doesn't break the markdown link
    assert "Marketing%20And%20Sales.md" in idx


def test_scan_vault_decodes_encoded_links(tmp_path: Path):
    (tmp_path / "Real Page.md").write_text("ok", encoding="utf-8")
    src = tmp_path / "src.md"
    src.write_text("[x](Real%20Page.md)\n", encoding="utf-8")
    qa = scan_vault(tmp_path)
    # encoded link to an existing file must NOT be reported broken
    assert not any("Real%20Page.md" in b for b in qa["broken_links"])


def test_scan_vault_finds_broken_and_lossy(tmp_path: Path):
    md = tmp_path / "p.md"
    md.write_text(
        "[gone](./missing.md)\n"
        "![img](assets/nope.png)\n"
        "[empty]()\n"  # empty target must not crash the scanner
        "<ac:structured-macro>leftover</ac:structured-macro>\n",
        encoding="utf-8",
    )
    qa = scan_vault(tmp_path)
    assert any("missing.md" in b for b in qa["broken_links"])
    assert any("nope.png" in a for a in qa["missing_assets"])
    assert qa["lossy_macros"]


def test_rewrite_diagram_refs(tmp_path: Path):
    space = tmp_path / "Space"
    (space / "diagrams").mkdir(parents=True)
    page = space / "Page.md"
    page.write_text(
        "---\nsource_id: 42\n---\n![Flow](assets/Flow.png)\n", encoding="utf-8"
    )
    out = space / "diagrams" / "Flow.drawio.svg"
    out.write_text("<svg/>", encoding="utf-8")
    result = DiagramResult(page_id="42", space="Space", base="Flow", out_path=out)
    n = rewrite_diagram_refs(tmp_path, [result], LinkSettings(style="relative"))
    assert n == 1
    assert "Flow.drawio.svg" in page.read_text(encoding="utf-8")


def test_render_migration_report_html_escapes():
    html = render_report_html({
        "source": {"spaces_in_scope": 1, "pages": 1, "blogposts": 0, "comments": 0, "attachments": 0},
        "vault": {"markdown_files": 1, "asset_files": 0, "diagram_svgs": 0},
        "qa": {"broken_links": ["<bad>.md"], "missing_assets": [], "lossy_macros": []},
    }, "now")
    assert "<bad>.md" not in html
    assert "&lt;bad&gt;.md" in html
    assert "Migration report" in html


def test_scaffold_writes_obsidian_metadata_presets(tmp_path: Path):
    cfg = Config(
        confluence_url="https://x.atlassian.net/wiki",
        username="u",
        api_token="t",
        settings=Settings(),
        run_id="run1",
        run_dir=tmp_path / "run1",
    )
    scaffold_vault(cfg, do_git=False)
    assert (cfg.output_path / ".obsidian" / "types.json").exists()
    template = cfg.output_path / "Templates" / "Confluence page.md"
    assert template.exists()
    assert "type: confluence-page" in template.read_text(encoding="utf-8")
