import pytest
from pydantic import ValidationError

from kraken.config import load_config


def test_defaults_load(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("{}")
    c = load_config(str(p))
    assert c.sonar.threshold == 0.75 and c.storm.max_rounds == 3
    assert c.autonomy["brief"] == "human"


def test_empty_file_loads_defaults(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("")
    c = load_config(str(p))
    assert c.rooms.requests == "kraken-requests"
    assert c.timeouts_s["forge"] == 3600
    assert c.bidders == []


def test_repo_config_parses():
    c = load_config("kraken.yaml")
    assert "demo/shopfront" in c.sonar.code_paths
    assert c.bidders == ["claude-coder", "codex-coder", "opencode-coder"]


def test_bad_autonomy_value_rejected(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("autonomy: {brief: maybe}")
    with pytest.raises(ValidationError):
        load_config(str(p))


def test_partial_autonomy_keeps_defaults(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("autonomy: {brief: auto}")
    c = load_config(str(p))
    assert c.autonomy["brief"] == "auto" and c.autonomy["merge"] == "human"
    assert set(c.autonomy) == {"brief", "pick", "merge", "storm_result"}


def test_unknown_autonomy_key_rejected(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("autonomy: {bogus: auto}")
    with pytest.raises(ValidationError):
        load_config(str(p))


def test_non_mapping_top_level_rejected(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("- a\n- b")
    with pytest.raises(ValidationError):
        load_config(str(p))
