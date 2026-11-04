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
    for d in ("node_modules", "dist", "vendor"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.json").write_text('{"a": 1}')
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (tmp_path / "huge.json").write_text('{"a": "' + "x" * 600_000 + '"}')
    assert index_code([str(tmp_path)]) == []


def test_typescript_go_toml_and_env_example(tmp_path):
    (tmp_path / "user.ts").write_text(
        "export interface User {\n  userId: string;\n  readonly age?: number\n}\n"
        "type Price = { amountCents: bigint }\n")
    (tmp_path / "user.go").write_text(
        "package m\ntype User struct {\n\tID   int64  `json:\"user_id\"`\n\tName string\n}\n")
    (tmp_path / "pyproject.toml").write_text('[tool.app]\nrate_limit = 100\nregion = "eu"\n')
    (tmp_path / ".env.example").write_text("# keys\nTIMEOUT_MS=3000\nAPI_KEY=\n")
    idx = index_code([str(tmp_path)])
    got = {(c.subject, c.attribute, c.value) for c in idx}
    assert {("user_id", "type", "string"), ("age", "type", "number"),
            ("amount_cents", "type", "bigint")} <= got  # TypeScript
    assert {("user_id", "type", "int64"), ("name", "type", "string")} <= got  # Go, json name wins
    assert {("rate_limit", "value", "100"), ("region", "value", "eu")} <= got  # TOML
    assert ("timeout_ms", "value", "3000") in got and not any(c.subject == "api_key" for c in idx)
    ts = next(c for c in idx if c.subject == "age")
    assert ts.path.endswith("user.ts") and ts.line == 3
    assert {c.value for c in code_claims_for(idx, "user_id", "type")} == {"string", "int64"}
