import re
from pathlib import Path

import pytest
import yaml

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
STAGES = ["spark", "sonar-resolution", "charter", "forge", "storm"]
ALL = ["kraken-hq", *STAGES]
MARKERS = {
    "spark": ["BRIEF READY", "brief"],
    "charter": ["BID: "],
    "forge": ["ACCEPTED"],
    "storm": ["SURVIVED", "FAILED"],
}


def load(name: str) -> dict:
    return yaml.safe_load((TEMPLATES / f"{name}.template.yaml").read_text(encoding="utf-8"))


def rooms_of(doc: dict) -> list[dict]:
    return doc["rooms"] if "rooms" in doc else [doc["room"]]


def kickoffs_of(doc: dict) -> list[str]:
    return [r["kickoff"] for r in doc["rooms"]] if "rooms" in doc else [doc["kickoff"]]


@pytest.mark.parametrize("name", ALL)
def test_template_is_valid_yaml_with_kickoff_mentions(name):
    doc = load(name)
    assert doc["version"] == 1 and "params" in doc
    for kickoff in kickoffs_of(doc):
        if name in ("kraken-hq", "sonar-resolution"):
            # no working agent to wake: plain text, and never a mention of Kraken itself
            assert "@{kraken}" not in kickoff
        else:
            assert "@{" in kickoff


@pytest.mark.parametrize("name", ALL)
def test_every_placeholder_is_declared(name):
    text = (TEMPLATES / f"{name}.template.yaml").read_text(encoding="utf-8")
    used = set(re.findall(r"\{([a-z_0-9]+)\}", text))
    declared = set(load(name)["params"])
    assert used <= declared, used - declared


@pytest.mark.parametrize("name", ALL)
def test_agent_entries_are_agent_params(name):
    doc = load(name)
    for room in rooms_of(doc):
        for entry in room.get("agents", []):
            assert doc["params"][entry.strip("{}")]["type"] == "agent", entry


@pytest.mark.parametrize("name", STAGES)
def test_stage_room_has_kraken_and_tag(name):
    doc = load(name)
    room = doc["room"]
    assert "{kraken}" in room["agents"]
    assert "[K-{tag}]" in room["name"]
    assert room["channel_type"] == "channel_private"
    # Kraken (an agent, with no chat account) creates stage rooms, so no {$creator};
    # people come from kraken.yaml `people` (see stages.with_people).
    assert "users" not in room and "$creator" not in str(doc)
    assert doc["params"]["kraken"]["type"] == "agent"
    assert doc["params"]["kraken"]["default"] == "kraken"
    assert "@{kraken}" not in doc["kickoff"]


@pytest.mark.parametrize("name,markers", MARKERS.items())
def test_instructions_state_markers(name, markers):
    instructions = load(name)["room"]["instructions"]
    for m in markers:
        assert m in instructions
    assert "own line" in instructions


def test_kraken_hq_group():
    doc = load("kraken-hq")
    assert "kickoff" not in doc
    assert [r["name"] for r in doc["rooms"]] == ["{requests}", "{hallmark}"]
    for room in doc["rooms"]:
        assert room["agents"] == ["{kraken}"] and room["users"] == ["{$creator}"]


def test_spark_params():
    assert set(load("spark")["params"]) == {"tag", "request", "planner", "owner", "kraken"}
    assert "3" in load("spark")["room"]["instructions"]


def test_resolution_room():
    doc = load("sonar-resolution")
    assert doc["params"]["user_a"]["type"] == "string" and doc["params"]["user_b"]["type"] == "string"
    assert doc["kickoff"] == "{prompt_text}"
    assert {"tag", "subject", "quote_a", "link_a", "user_a", "quote_b", "link_b", "user_b",
            "prompt_text"} <= set(doc["params"])


def test_charter_params_and_rules():
    doc = load("charter")
    assert doc["params"]["bidder_1"]["type"] == "agent"
    assert doc["params"]["other_bidders"]["required"] is False
    assert doc["room"]["agents"] == ["{bidder_1}", "{kraken}"]
    text = doc["room"]["instructions"]
    for word in ("plan:", "files:", "estimate:", "risks:", "exactly one", "critique"):
        assert word in text


def test_forge_params_and_rules():
    doc = load("forge")
    assert doc["room"]["agents"] == ["{coder}", "{reviewer_1}", "{kraken}"]
    text = doc["room"]["instructions"]
    assert "plan" in text and "ACCEPTED" in text and "acceptance criterion" in text


def test_storm_mode_and_roles():
    doc = load("storm")
    mode = doc["params"]["mode"]
    assert mode["type"] == "enum" and mode["enum"] == ["auto", "drill", "skip"]
    assert doc["room"]["agents"] == ["{attacker}", "{defender}", "{coach}", "{kraken}"]
    text = doc["room"]["instructions"]
    assert "This round runs in {mode} mode." in text
    assert "python -m storm.faults" in text and "SURVIVED" in text and "FAILED" in text
    for a in ("@{attacker}", "@{defender}", "@{coach}"):
        assert a in doc["kickoff"]
    # agents wake only on mentions: each hand-off must tag the next agent (live stall, K-2)
    assert "mentions @{defender} and @{coach}" in text and "mention @{coach}" in text
