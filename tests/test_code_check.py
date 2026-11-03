from kraken.sonar.code_check import code_claims_for, index_code


def test_index_code_finds_limits_and_types():
    idx = index_code(["tests/fixtures/code"])
    assert any(c.subject == "free_tier" and c.value == "1000" and c.line for c in idx)
    assert any(c.subject == "user_id" and c.attribute == "type" and c.value == "integer" for c in idx)
    assert any(c.subject == "user_id" and c.attribute == "type" and c.value == "int"
               and c.path.endswith("models.py") for c in idx)
    assert all(c.source == "code" and c.speaker == "code" for c in idx)


def test_openapi_has_no_generic_leaves():
    idx = index_code(["tests/fixtures/code/openapi.yaml"])
    assert {c.attribute for c in idx} == {"type"}
    assert len(idx) == 2


def test_code_claims_for():
    idx = index_code(["tests/fixtures/code"])
    assert {c.value for c in code_claims_for(idx, "user_id", "type")} == {"integer", "int"}


def test_bad_file_skipped(tmp_path, caplog):
    (tmp_path / "bad.json").write_text("{nope")
    (tmp_path / "ok.json").write_text('{"a": {"max_size": 5, "on": true}, "l": [1]}')
    idx = index_code([str(tmp_path)])
    assert {(c.subject, c.value) for c in idx} == {("max_size", "5"), ("on", "true")}
    assert "bad.json" in caplog.text


def test_skips_ignored_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.json").write_text('{"a": 1}')
    assert index_code([str(tmp_path)]) == []
