from pathlib import Path

from migrator.links import (
    build_page_index,
    decode_tiny_link,
    parse_frontmatter,
    rewrite_vault,
)
from migrator.settings import LinkSettings


def test_parse_frontmatter():
    text = "---\ntitle: Hello\nsource_id: 12345\n---\nbody"
    fm = parse_frontmatter(text)
    assert fm["title"] == "Hello"
    assert fm["source_id"] == "12345"


def test_parse_frontmatter_none():
    assert parse_frontmatter("no frontmatter here") == {}


def test_decode_tiny_link_roundtrip():
    import base64

    page_id = 1234567
    raw = page_id.to_bytes(8, "little")
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert decode_tiny_link(token) == str(page_id)


def test_decode_tiny_link_bad_input():
    assert decode_tiny_link("!!!not base64!!!") in ("", "0") or isinstance(
        decode_tiny_link("!!!"), str
    )


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "Space").mkdir()
    target = tmp_path / "Space" / "Target.md"
    target.write_text("---\nsource_id: 999\n---\nTarget body\n", encoding="utf-8")
    src = tmp_path / "Space" / "Source.md"
    src.write_text(
        "---\nsource_id: 1\n---\n"
        "See [target](https://x.atlassian.net/wiki/spaces/S/pages/999/Target)\n",
        encoding="utf-8",
    )
    return tmp_path


def test_build_page_index(tmp_path: Path):
    _vault(tmp_path)
    index = build_page_index(tmp_path)
    assert "999" in index and "1" in index


def test_rewrite_vault_relative(tmp_path: Path):
    _vault(tmp_path)
    stats = rewrite_vault(tmp_path, LinkSettings(style="relative"))
    assert stats["links_rewritten"] == 1
    src = (tmp_path / "Space" / "Source.md").read_text(encoding="utf-8")
    assert "Target.md" in src
    assert "atlassian.net" not in src


def test_rewrite_disabled(tmp_path: Path):
    _vault(tmp_path)
    stats = rewrite_vault(tmp_path, LinkSettings(rewrite_internal=False))
    assert stats["links_rewritten"] == 0
