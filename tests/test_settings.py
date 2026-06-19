from migrator.settings import (
    AttachmentSettings,
    ScopeSettings,
    Settings,
)


def _settings(**attach):
    s = Settings()
    s.attachments = AttachmentSettings(**attach)
    return s


def test_attachment_temp_artifacts_denied():
    s = _settings(skip_temp_artifacts=True)
    assert not s.is_attachment_allowed("~drawio~foo.tmp")
    assert not s.is_attachment_allowed("autosave.tmp")


def test_attachment_extension_allow_deny():
    s = _settings(allow_extensions=["png"], deny_extensions=["exe"])
    assert s.is_attachment_allowed("a.png")
    assert not s.is_attachment_allowed("a.exe")
    assert not s.is_attachment_allowed("a.gif")  # not in allowlist


def test_attachment_size_limit():
    s = _settings(max_file_size_mb=1)
    assert s.is_attachment_allowed("a.png", size_bytes=500_000)
    assert not s.is_attachment_allowed("a.png", size_bytes=2 * 1024 * 1024)


def test_attachment_media_type_deny():
    s = _settings(deny_media_types=["application/x-bad"])
    assert not s.is_attachment_allowed("a.bin", media_type="application/x-bad")


def test_space_in_scope_filters():
    s = Settings()
    s.scope = ScopeSettings(include_spaces=["DEV"])
    assert s.space_in_scope("DEV", "global", "current")
    assert not s.space_in_scope("OPS", "global", "current")


def test_space_in_scope_archived_personal_toggles():
    s = Settings()
    s.scope = ScopeSettings(include_archived=False, include_personal=False)
    assert not s.space_in_scope("X", "global", "archived")
    assert not s.space_in_scope("Y", "personal", "current")
    assert s.space_in_scope("Z", "global", "current")
