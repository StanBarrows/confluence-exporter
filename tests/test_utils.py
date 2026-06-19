from pathlib import Path

import pytest

from migrator.utils import (
    PathTraversalError,
    file_sha256,
    safe_join,
    safe_name,
    stable_pseudonym,
    strip_ext,
)


def test_safe_name_replaces_illegal_chars():
    assert safe_name("a/b:c?d") == "a_b_c_d"


def test_safe_name_strips_and_defaults():
    assert safe_name("   ") == "untitled"
    assert safe_name("...x...") == "x"


def test_safe_name_length_limit():
    assert len(safe_name("x" * 500, maxlen=10)) == 10


def test_strip_ext_compound():
    assert strip_ext("diagram.drawio.tmp") == "diagram"
    assert strip_ext("diagram.drawio") == "diagram"
    assert strip_ext("file.png") == "file"
    assert strip_ext("noext") == "noext"


def test_safe_join_contained(tmp_path: Path):
    assert safe_join(tmp_path, "a", "b.md").parent.name == "a"


def test_safe_join_rejects_traversal(tmp_path: Path):
    with pytest.raises(PathTraversalError):
        safe_join(tmp_path, "..", "..", "etc", "passwd")


def test_stable_pseudonym_is_deterministic():
    a = stable_pseudonym("Jane Doe")
    b = stable_pseudonym("jane doe")
    assert a == b and a.startswith("user-")
    assert stable_pseudonym("") == ""


def test_file_sha256(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    assert len(file_sha256(f)) == 64
    assert file_sha256(tmp_path / "missing") == ""
