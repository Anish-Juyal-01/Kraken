import pytest

from kraken.config import Config
from kraken.crew import AgentInfo, Crew, lookup, pick_bidders
from kraken.switch_client import SwitchError


def seeded(ledger, **cfg):
    crew = Crew(ledger)
    crew.seed(Config(bidders=["a", "b"], **cfg))
    return crew


def test_seed_from_config(ledger):
    crew = seeded(ledger)
    assert crew.members("bidder") == ["a", "b"]
    assert crew.members("planner") == ["kraken-planner"]
    assert crew.members("attacker") == ["squall"]
    assert crew.members("defender") == ["storm-defender"]
    assert crew.members("coach") == ["storm-coach"]
    assert crew.primary("planner") == "kraken-planner"
    assert crew.all_names() == {"a", "b", "kraken-planner", "squall", "storm-defender",
                                "storm-coach"}


def test_seed_runs_once(ledger):
    crew = seeded(ledger)
    crew.retire("a")
    crew.seed(Config(bidders=["x", "y"]))
    assert crew.members("bidder") == ["b"]


def test_empty_roster_has_no_primary(ledger):
    crew = Crew(ledger)
    assert crew.members("bidder") == [] and crew.primary("coach") is None
    assert crew.all_names() == set() and crew.role_of("a") is None


def test_seed_skips_name_already_seated(ledger):
    crew = Crew(ledger)
    crew.seed(Config(bidders=["squall", "a"]))
    assert crew.role_of("squall") == "bidder" and crew.members("attacker") == []


def test_enlist_and_retire(ledger):
    crew = seeded(ledger)
    assert crew.enlist("c", "bidder") is True
    assert crew.members("bidder") == ["a", "b", "c"]
    assert crew.enlist("c", "bidder") is False
    assert crew.role_of("c") == "bidder"
    assert crew.retire("c") is True and crew.retire("c") is False
    assert crew.role_of("c") is None and crew.members("bidder") == ["a", "b"]


def test_enlist_under_new_role_moves_it(ledger):
    crew = seeded(ledger)
    assert crew.enlist("a", "coach") is True
    assert crew.members("bidder") == ["b"]
    assert crew.members("coach") == ["storm-coach", "a"]
    assert crew.primary("coach") == "storm-coach"
    assert crew.role_of("a") == "coach"


def test_persists_across_reopen(tmp_path, open_ledger):
    path = str(tmp_path / "again.db")
    first = open_ledger(path)
    crew = Crew(first)
    crew.seed(Config(bidders=["a"]))
    crew.enlist("z", "bidder")
    first.close()
    again = Crew(open_ledger(path))
    again.seed(Config(bidders=["other"]))
    assert again.members("bidder") == ["a", "z"]


def test_rotation_counter(ledger):
    crew = Crew(ledger)
    assert [crew.next_rotation() for _ in range(3)] == [0, 1, 2]


LIVE = [("c1", "claude-code"), ("c2", "claude-code"), ("x1", "codex"), ("g1", "gemini"),
        ("k1", "opencode:moonshot/kimi-k2"), ("q1", "opencode:qwen/qwen3")]


def test_pick_under_cap_keeps_everyone():
    assert pick_bidders(LIVE[:3], 5, 0) == ["c1", "c2", "x1"]
    assert pick_bidders(LIVE, 6, 4) == [n for n, _ in LIVE]


def test_pick_empty_and_cap_of_one():
    assert pick_bidders([], 3, 7) == []
    assert pick_bidders(LIVE, 1, 0) == ["c1"]


def test_pick_prefers_distinct_vendors():
    assert pick_bidders(LIVE, 4, 0) == ["c1", "x1", "g1", "k1"]


def test_pick_distinct_opencode_models_are_distinct_vendors():
    live = [("k1", "opencode:a"), ("k2", "opencode:a"), ("q1", "opencode:b")]
    assert pick_bidders(live, 2, 0) == ["k1", "q1"]


def test_pick_fills_remaining_seats_in_scan_order():
    # 3 vendors, 5 seats: one per vendor first, then c2, then the next unseated in order
    live = [("c1", "claude-code"), ("c2", "claude-code"), ("c3", "claude-code"),
            ("x1", "codex"), ("g1", "gemini"), ("x2", "codex")]
    assert pick_bidders(live, 5, 0) == ["c1", "x1", "g1", "c2", "c3"]


def test_pick_rotation_moves_the_scan_start():
    assert pick_bidders(LIVE, 3, 0) == ["c1", "x1", "g1"]
    assert pick_bidders(LIVE, 3, 1) == ["c2", "x1", "g1"]
    assert pick_bidders(LIVE, 3, 2) == ["x1", "g1", "k1"]
    assert pick_bidders(LIVE, 3, 5) == ["q1", "c1", "x1"]


def test_pick_rotation_wraps_modulo():
    assert pick_bidders(LIVE, 3, 6) == pick_bidders(LIVE, 3, 0)
    assert pick_bidders(LIVE, 3, 13) == pick_bidders(LIVE, 3, 1)


def test_pick_rotation_gives_every_agent_a_turn():
    seats = [set(pick_bidders(LIVE, 2, i)) for i in range(len(LIVE))]
    assert set().union(*seats) == {n for n, _ in LIVE}


def test_pick_never_exceeds_cap_or_repeats():
    for cap in range(1, 8):
        for start in range(8):
            got = pick_bidders(LIVE, cap, start)
            assert len(got) == min(cap, len(LIVE)) and len(set(got)) == len(got)


class FakeSwitch:
    def __init__(self, agents: dict[str, dict], fail=None):
        self.agents, self.fail, self.calls = agents, fail, []

    async def op(self, name, /, **args):
        self.calls.append(name)
        if self.fail:
            raise self.fail
        if name == "list_agents":
            return [{"id": f"id-{n}", "name": n} for n in self.agents]
        assert name == "get_agent_detail"
        return self.agents[args["agent_id"].removeprefix("id-")]


def detail(kind, sessions=("live",), models=None):
    d = {"known_agent_type": kind, "sessions": [{"state": s} for s in sessions]}
    if models is not None:
        d["models"] = models
    return d


def test_agent_info_vendor_and_label():
    assert AgentInfo("c", "claude-code", None, True).vendor == "claude-code"
    assert AgentInfo("c", "claude-code", None, True).label == "claude-code"
    k = AgentInfo("k", "opencode", "moonshot/kimi-k2", False)
    assert k.vendor == "opencode:moonshot/kimi-k2"
    assert k.label == "opencode · moonshot/kimi-k2"
    assert AgentInfo("o", "opencode", None, True).vendor == "opencode"
    assert AgentInfo("x", "codex", "gpt-5", True).vendor == "codex"
    assert AgentInfo("m", None, None, True).vendor == "custom:m"
    assert AgentInfo("m", None, None, True).label == "custom"
    assert AgentInfo("e", "", None, True).vendor == "custom:e"


async def test_lookup_reads_type_model_and_liveness():
    sw = FakeSwitch({
        "a": detail("claude-code"),
        "b": detail("opencode", sessions=("closed",), models=[{"name": "moonshot/kimi-k2"}]),
        "c": detail(None, sessions=("open",), models=["str-model"]),
        "d": detail("codex", sessions=()),
    })
    got = await lookup(sw, ["a", "b", "c", "d", "ghost"])
    assert got["a"] == AgentInfo("a", "claude-code", None, True)
    assert got["b"] == AgentInfo("b", "opencode", "moonshot/kimi-k2", False)
    assert got["c"] == AgentInfo("c", None, "str-model", True)
    assert got["d"].online is False
    assert "ghost" not in got
    assert sw.calls.count("list_agents") == 1


async def test_lookup_only_fetches_wanted_and_propagates_errors():
    sw = FakeSwitch({"a": detail("codex"), "b": detail("codex")})
    await lookup(sw, ["a"])
    assert sw.calls == ["list_agents", "get_agent_detail"]
    assert await lookup(sw, []) == {}
    with pytest.raises(SwitchError):
        await lookup(FakeSwitch({}, fail=SwitchError(500, "down")), ["a"])
