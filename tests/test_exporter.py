from pathlib import Path

from migrator.config import Config, _normalize_wiki_url
from migrator.exporter import build_cme_config
from migrator.settings import Settings


def test_normalize_wiki_url_appends_for_cloud():
    base = "https://acme.atlassian.net"
    assert _normalize_wiki_url(base) == base + "/wiki"
    # idempotent
    assert _normalize_wiki_url(base + "/wiki") == base + "/wiki"


def test_normalize_wiki_url_leaves_selfhosted():
    assert _normalize_wiki_url("https://confluence.corp.com") == "https://confluence.corp.com"


def test_site_root_strips_wiki(tmp_path: Path):
    cfg = Config(
        confluence_url="https://acme.atlassian.net/wiki",
        username="u", api_token="t", settings=Settings(),
        run_id="r", run_dir=tmp_path,
    )
    assert cfg.site_root == "https://acme.atlassian.net"


def _config(tmp_path: Path) -> Config:
    return Config(
        confluence_url="https://x.atlassian.net/wiki",
        username="me@x.com",
        api_token="SECRET-TOKEN",
        settings=Settings(),
        run_id="run1",
        run_dir=tmp_path / "run1",
    )


def test_cme_config_omits_secret_by_default(tmp_path: Path):
    cfg = build_cme_config(_config(tmp_path))
    assert "auth" not in cfg
    assert "SECRET-TOKEN" not in str(cfg)


def test_cme_config_includes_auth_when_requested(tmp_path: Path):
    cfg = build_cme_config(_config(tmp_path), with_auth=True)
    # cme keys auth by instance base URL (no /wiki).
    account = cfg["auth"]["confluence"]["https://x.atlassian.net"]
    assert account["api_token"] == "SECRET-TOKEN"
    assert account["username"] == "me@x.com"


def test_cme_config_translates_placeholders(tmp_path: Path):
    cfg = build_cme_config(_config(tmp_path))
    assert "{space_name}" in cfg["export"]["page_path"]
    assert "{page_title}" in cfg["export"]["page_path"]
    assert "{ancestor_titles}" in cfg["export"]["page_path"]


def test_cme_config_maps_enums(tmp_path: Path):
    cfg = build_cme_config(_config(tmp_path))
    exp = cfg["export"]
    assert exp["page_href"] in {"relative", "absolute", "wiki"}
    assert exp["attachments_export"] in {"all", "referenced", "disabled"}
    assert exp["confluence_url_in_frontmatter"] in {"none", "webui", "tinyui", "both"}
    assert exp["page_properties_report_format"] in {"frozen", "dataview"}
