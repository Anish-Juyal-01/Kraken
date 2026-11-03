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
    assert c.bidders == ["claude-coder", "claude-coder-2", "claude-coder-3"]


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


def test_agent_defaults(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("{}")
    c = load_config(str(p))
    assert c.kraken_agent == "kraken"
    assert (c.agents.planner, c.agents.attacker, c.agents.defender, c.agents.coach) == (
        "kraken-planner", "squall", "storm-defender", "storm-coach")


def test_charter_max_bidders_default_and_repo(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("{}")
    assert load_config(str(p)).charter.max_bidders == 5
    assert load_config("kraken.yaml").charter.max_bidders == 5


def test_charter_max_bidders_override_and_validation(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("charter: {max_bidders: 2}")
    assert load_config(str(p)).charter.max_bidders == 2
    for bad in ("0", "-1"):
        p.write_text(f"charter: {{max_bidders: {bad}}}")
        with pytest.raises(ValidationError):
            load_config(str(p))
