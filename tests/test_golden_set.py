import importlib.util
from collections import Counter
from pathlib import Path

import yaml

from tests.golden.run_golden import LABELS, classify, score

ROOT = Path(__file__).resolve().parents[1]
MINIMUMS = {"conflict": 10, "update": 6, "compatible": 6, "chitchat": 8}


def _load_smoke():
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pairs_file_shape_and_counts():
    pairs = yaml.safe_load((ROOT / "tests" / "golden" / "pairs.yaml").read_text(encoding="utf-8"))
    assert len(pairs) == 30
    for p in pairs:
        assert set(p) == {"a", "b", "label"}
        assert p["label"] in LABELS
        assert p["a"].strip() and p["b"].strip()
    counts = Counter(p["label"] for p in pairs)
    for label, minimum in MINIMUMS.items():
        assert counts[label] >= minimum


def test_score_precision_and_recall():
    confusion = Counter({
        ("conflict", "conflict"): 8, ("conflict", "update"): 2,
        ("update", "conflict"): 1, ("compatible", "compatible"): 5,
        ("chitchat", "chitchat"): 8,
    })
    precision, recall = score(confusion)
    assert precision == 8 / 9
    assert recall == 0.8


def test_score_handles_no_predictions():
    assert score(Counter({("conflict", "compatible"): 3})) == (0.0, 0.0)


def test_classify_outcomes():
    assert classify(True, 1, []) == "conflict"
    assert classify(False, 0, []) == "chitchat"
    assert classify(False, 1, ["update"]) == "update"
    assert classify(False, 2, ["compatible", "update"]) == "update"
    assert classify(False, 1, ["compatible"]) == "compatible"
    assert classify(False, 1, []) == "compatible"
    assert classify(False, 1, ["conflict"]) == "compatible"


def test_parse_seed_matches_demo_script():
    smoke = _load_smoke()
    seed = smoke.parse_seed((ROOT / "demo" / "seed_chat.md").read_text(encoding="utf-8"))
    assert [name for name, _ in seed] == ["backend", "mobile", "product", "partners"]
    assert [len(msgs) for _, msgs in seed] == [3, 2, 2, 2]
    assert seed[1][1][0] == "The mobile app stores user_id as an int in the local database."


def test_resolution_room_prefix_and_lookup():
    smoke = _load_smoke()
    template = (ROOT / "templates" / "sonar-resolution.template.yaml").read_text(encoding="utf-8")
    prefix = smoke.room_prefix(template)
    assert prefix == "Sonar [K-"
    names = ["backend", "Sonar [K-1]: user_id type"]
    stale = {"Sonar [K-1]: user_id type"}
    assert smoke.find_resolution_room(names, prefix, set()) == "Sonar [K-1]: user_id type"
    assert smoke.find_resolution_room(names, prefix, stale) is None
    names.append("Sonar [K-2]: v1 endpoint")
    assert smoke.find_resolution_room(names, prefix, stale) == "Sonar [K-2]: v1 endpoint"
    assert smoke.find_resolution_room(names[:1], prefix, set()) is None


async def test_all_room_names_follows_cursor():
    smoke = _load_smoke()
    pages = {
        None: {"rooms": [{"name": "Sonar [K-1]: a"}], "next_cursor": "c1"},
        "c1": {"rooms": [{"name": "Sonar [K-2]: b"}], "next_cursor": None},
    }
    calls = []

    async def fake_op(name, **args):
        calls.append((name, args))
        return pages[args.get("cursor")]

    assert await smoke.all_room_names(fake_op, "Sonar [K-") == ["Sonar [K-1]: a", "Sonar [K-2]: b"]
    assert [c[0] for c in calls] == ["list_all_rooms", "list_all_rooms"]
    assert calls[0][1]["name_contains"] == "Sonar [K-" and calls[1][1]["cursor"] == "c1"


def test_check_env_requires_smoke_identity():
    smoke = _load_smoke()
    full = {"SWITCH_URL": "u", "SMOKE_AGENT_ID": "a", "SMOKE_API_KEY": "k"}
    assert smoke.check_env(full) is None
    msg = smoke.check_env({"SWITCH_URL": "u", "SWITCH_AGENT_ID": "kraken", "SWITCH_API_KEY": "x"})
    assert "SMOKE_AGENT_ID" in msg and "SMOKE_API_KEY" in msg
    assert "register.py --name kraken-smoke" in msg
