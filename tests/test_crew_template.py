import re
from pathlib import Path

import yaml

from kraken.config import load_config

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "templates" / "kraken-crew.template.yaml"


def load() -> dict:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


def test_seats_match_the_config_and_coders_are_named_after_the_provider():
    cfg = load_config(str(ROOT / "kraken.yaml"))
    a = cfg.agents
    names = {e["name"] for e in load()["agents"]}
    coders = {"{provider}-coder", "{provider}-coder-2", "{provider}-coder-3"}
    assert names == {*coders, a.planner, a.attacker, a.defender, a.coach}
    assert cfg.charter.auto_join  # the coders aren't seeded: they join at the first Charter
    assert load()["params"]["provider"]["default"] == "claude"
    for e in load()["agents"]:
        assert e["provider"] == "{provider}", e["name"]


def test_every_agent_is_runnable_and_reachable():
    for e in load()["agents"]:
        for field in ("provider", "location", "directory", "instructions"):
            assert e.get(field), (e["name"], field)
        assert e["addressing"] == "anyone", e["name"]
        assert e["repo"] == "{repo}", e["name"]
    assert load()["params"]["repo"]["default"].startswith("https://github.com/")


def test_demo_rooms_match_the_seed_chat_and_include_watchers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    seed = smoke.parse_seed((ROOT / "demo" / "seed_chat.md").read_text(encoding="utf-8"))
    doc = yaml.safe_load((ROOT / "templates" / "kraken-demo.template.yaml").read_text(encoding="utf-8"))
    assert {r["name"] for r in doc["rooms"]} == {name for name, _ in seed}
    for room in doc["rooms"]:
        assert room["agents"] == ["{kraken}", "{smoke}"]
        assert "@" not in room["kickoff"]


def test_every_placeholder_is_declared():
    text = PATH.read_text(encoding="utf-8")
    used = set(re.findall(r"\{([a-z_0-9]+)\}", text)) - {"agent"}
    assert used <= set(load()["params"]), used - set(load()["params"])
