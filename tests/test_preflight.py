from migrator.preflight import (
    CheckResult,
    Section,
    analyze_titles,
    classify_macros,
    exit_code,
    extract_macros,
    grade,
    human_size,
    is_macro_only,
)

STORAGE = (
    '<ac:structured-macro ac:name="info"><ac:rich-text-body>hi'
    '</ac:rich-text-body></ac:structured-macro>'
    '<ac:structured-macro ac:name="jira" />'
    '<ac:structured-macro ac:name="some-custom-thing" />'
)


def test_extract_macros():
    names = extract_macros(STORAGE)
    assert set(names) == {"info", "jira", "some-custom-thing"}


def test_extract_macros_ignores_parameter_names():
    # ac:parameter ac:name="..." must NOT be counted as a macro.
    xml = (
        '<ac:structured-macro ac:name="drawio">'
        '<ac:parameter ac:name="bgColor">#fff</ac:parameter>'
        '<ac:parameter ac:name="aspect">1</ac:parameter>'
        '</ac:structured-macro>'
    )
    assert extract_macros(xml) == ["drawio"]


def test_classify_macros():
    buckets = classify_macros(extract_macros(STORAGE))
    assert "info" in buckets["clean"]
    assert "jira" in buckets["lossy"]
    assert "some-custom-thing" in buckets["unknown"]


def test_classify_macros_dedup_and_case():
    buckets = classify_macros(["INFO", "info", "JIRA"])
    assert buckets["clean"] == ["info"]
    assert buckets["lossy"] == ["jira"]


def test_is_macro_only():
    diagram_only = '<ac:structured-macro ac:name="drawio" />'
    assert is_macro_only(diagram_only)
    text_page = "<p>" + ("real content " * 10) + "</p>"
    assert not is_macro_only(text_page)
    assert not is_macro_only("")


def test_analyze_titles():
    res = analyze_titles(["Home", "home", "Über", "x" * 300], max_len=255)
    assert res["duplicates"].get("home") == 2
    assert any(len(t) > 255 for t in res["overlong"])
    assert "Über" in res["non_ascii"]


def test_analyze_titles_illegal_chars():
    res = analyze_titles(["a/b", "ok"], max_len=255)
    assert "a/b" in res["non_ascii"]


def test_human_size():
    assert human_size(0) == "0.0 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def _sections(*statuses):
    return [Section("s", [CheckResult("c", st) for st in statuses])]


def test_grade_worst_wins():
    assert grade(_sections("PASS", "INFO")) == "PASS"
    assert grade(_sections("PASS", "WARN")) == "WARN"
    assert grade(_sections("WARN", "FAIL")) == "FAIL"


def test_exit_code_strict():
    assert exit_code(_sections("PASS"), strict=True) == 0
    assert exit_code(_sections("WARN"), strict=True) == 1
    assert exit_code(_sections("FAIL"), strict=True) == 2
    # non-strict always 0
    assert exit_code(_sections("FAIL"), strict=False) == 0
