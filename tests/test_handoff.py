"""Human first: each step is a person's until they hand it to an agent (🤖 / agent)."""
import pytest

from kraken.conductor.commands import parse
from kraken.conductor.state import Signal, effective, step
from kraken.config import Config
from kraken.hallmark.ledger import Request
from tests.fakes import FakeLLM
from tests.test_dispatch import REQ_ROOM, ev, make, request_in


@pytest.mark.parametrize("body, kind", [
    ("🤖", "handoff"), ("agent", "handoff"), ("@kraken take over", "handoff"),
    ("agent, do it", "handoff"), ("you decide", "handoff"), ("you defend it", "handoff"),
    ("🙋", "reclaim"), ("me", "reclaim"), ("I'll do it", "reclaim"), ("my turn", "reclaim"),
])
def test_parse_handoff_and_reclaim(body, kind):
    assert parse(body, user="sam").kind == kind


def test_handoff_words_inside_sentences_are_not_commands():
    assert parse("the agent should cache this", user="sam") is None
    assert parse("me too, the free tier is 100 a day", user="sam") is None


def test_defaults_are_human_first():
    cfg = Config()
    assert set(cfg.autonomy.values()) == {"human"} and cfg.storm.default_mode == "drill"


def test_effective_turns_only_handed_steps_auto():
    r = Request(id=1, owner="sam", text="x", origin_room="r", state="FORGE",
                data={"handoff": ["CHARTER"]})
    eff = effective(Config(), r).autonomy
    assert eff["pick"] == "auto" and eff["merge"] == "human" and eff["brief"] == "human"
    assert effective(Config(), Request(id=2, owner="s", text="x", origin_room="r")).autonomy["pick"] == "human"


def test_a_handed_charter_picks_by_judge_without_asking():
    r = Request(id=1, owner="s", text="x", origin_room="r", state="CHARTER",
                bids=[{"agent": "a"}, {"agent": "b"}], data={"handoff": ["CHARTER"]})
    upd, _ = step(r, Config(), Signal(kind="bids_done", data={"judged_winner": "b"}))
    assert upd["state"] == "FORGE" and upd["winner"] == "b"


async def test_spark_handoff_tells_the_planner_to_decide(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    await d.handle(ev(1, "🤖", room="sp", name="sam"))
    req = ledger.get_request(rid)
    assert req.data["handoff"] == ["SPARK"] and req.data["handoff_by"] == {"SPARK": "sam"}
    texts = client.texts("sp")
    assert "handed to the agent by sam" in texts[0]
    assert texts[1].startswith("@kraken-planner [K-1] sam handed this step to you")
    assert "Assumptions" in texts[1]


async def test_spark_handoff_with_brief_waiting_approves_it(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, brief="B", data={"gate": "brief"})
    await d.handle(ev(1, "agent", room="sp", name="sam"))
    assert ledger.get_request(rid).state in ("PREFLIGHT", "CHARTER")


async def test_charter_handoff_at_the_pick_lets_the_judge_choose(ledger):
    llm = FakeLLM(['{"winner": "codex-coder", "reason": "covers the criteria"}'])
    client, d = make(ledger, llm=llm)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     bids=[{"agent": "claude-coder", "text": "a"}, {"agent": "codex-coder", "text": "b"}],
                     data={"gate": "pick", "live_bidders": ["claude-coder", "codex-coder"]})
    await d.handle(ev(1, "you pick", room="ch", name="sam"))
    req = ledger.get_request(rid)
    assert req.state == "FORGE" and req.winner == "codex-coder"
    assert req.data["pick"]["reason"] == "covers the criteria"


async def test_forge_handoff_early_lets_accepted_move_on(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}])
    await d.handle(ev(1, "🤖", room="fg", name="sam"))
    await d.handle(ev(2, "all good\nACCEPTED", room="fg", name="codex-coder"))
    assert ledger.get_request(rid).state == "STORM"  # no merge question


async def test_storm_handoff_switches_the_defender_to_auto_and_back(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="drill")
    await d.handle(ev(1, "🤖", room="st", name="sam"))
    req = ledger.get_request(rid)
    assert req.storm_mode == "auto" and req.data["handoff"] == ["STORM"]
    assert client.texts("st")[-1].startswith("@storm-defender [K-1] sam handed defending to you")
    await d.handle(ev(2, "🤖", room="st", name="sam"))
    assert "already with the agent" in client.texts("st")[-1]
    await d.handle(ev(3, "me", room="st", name="sam"))
    req = ledger.get_request(rid)
    assert req.storm_mode == "drill" and req.data["handoff"] == []
    assert "back with sam" in client.texts("st")[-1] and "drill mode" in client.texts("st")[-1]


async def test_storm_handoff_with_result_waiting_accepts_it(ledger):
    client, d = make(ledger, deliver="branch")
    rid = request_in(ledger, "STORM", rooms={"storm": "st"}, data={"gate": "storm_result"})
    await d.handle(ev(1, "🤖", room="st", name="sam"))
    assert ledger.get_request(rid).state == "CLOSED"


async def test_the_pull_request_cannot_be_handed_off(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "DELIVER", rooms={"storm": "st"}, data={"gate": "deliver"})
    await d.handle(ev(1, "🤖", room="st", name="sam"))
    req = ledger.get_request(rid)
    assert req.state == "DELIVER" and "handoff" not in req.data
    assert any("always needs a person" in t for t in client.texts())


# ---- nobody waits forever: auto handoff after human_wait_s -----------------
async def test_no_reply_in_time_hands_the_pick_to_the_judge(ledger):
    llm = FakeLLM(['{"winner": "codex-coder", "reason": "best fit"}'])
    client, d = make(ledger, llm=llm, human_wait_s=300)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     bids=[{"agent": "claude-coder", "text": "a"}, {"agent": "codex-coder", "text": "b"}],
                     data={"gate": "pick", "gate_since": 1000.0, "stage_started": 900.0,
                           "live_bidders": ["claude-coder", "codex-coder"]})
    await d.tick(1000.0 + 299)
    assert ledger.get_request(rid).state == "CHARTER"  # still the person's turn
    await d.tick(1000.0 + 301)
    req = ledger.get_request(rid)
    assert req.state == "FORGE" and req.winner == "codex-coder"
    assert req.data["handoff_by"]["CHARTER"].startswith("Kraken (no reply from a person in 5 min)")
    assert any("handed to the agent by Kraken" in t for t in client.texts("ch"))


async def test_a_human_reply_resets_the_wait(ledger):
    client, d = make(ledger, human_wait_s=300)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, data={"stage_started": 0.0})
    await d.handle(ev(1, "it should only cover the v2 routes", room="sp", name="sam"))
    human_at = ledger.get_request(rid).data["human_at"]
    await d.tick(human_at + 200)
    assert "handoff" not in ledger.get_request(rid).data  # sam answered recently
    await d.tick(human_at + 301)
    assert ledger.get_request(rid).data["handoff"] == ["SPARK"]
    assert "decide them yourself" in client.texts("sp")[-1]


async def test_drill_storm_hands_defending_over_after_the_wait(ledger):
    client, d = make(ledger, human_wait_s=60)
    rid = request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="drill",
                     data={"stage_started": 0.0})
    await d.tick(61)
    req = ledger.get_request(rid)
    assert req.storm_mode == "auto" and req.data["handoff"] == ["STORM"]


async def test_steps_not_waiting_on_people_are_left_alone(ledger):
    client, d = make(ledger, human_wait_s=60)
    forge = request_in(ledger, "FORGE", rooms={"forge": "fg"}, data={"stage_started": 0.0})
    charter = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, data={"stage_started": 0.0})
    auto_storm = request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="auto",
                            data={"stage_started": 0.0})
    pr = request_in(ledger, "DELIVER", rooms={"storm": "s2"}, data={"gate": "deliver", "gate_since": 0.0})
    await d.tick(10_000)
    for rid in (forge, charter, auto_storm, pr):
        assert "handoff" not in ledger.get_request(rid).data, rid  # building, bidding, agents, PR


async def test_human_wait_zero_always_waits(ledger):
    client, d = make(ledger, human_wait_s=0)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, data={"gate": "merge", "gate_since": 0.0})
    await d.tick(1e9)
    assert ledger.get_request(rid).state == "FORGE" and "handoff" not in ledger.get_request(rid).data


async def test_gate_opening_records_when_the_wait_started(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}])
    await d.handle(ev(1, "ok\nACCEPTED", room="fg", name="codex-coder"))
    data = ledger.get_request(rid).data
    assert data["gate"] == "merge" and data["gate_since"] > 0


# ---- quiet agents get a nudge -------------------------------------------------
async def test_a_quiet_coder_is_nudged_once_per_quiet_spell(ledger):
    client, d = make(ledger, agent_quiet_s=600)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"stage_started": 0.0, "active_at": 100.0})
    await d.tick(100 + 599)
    assert client.texts("fg") == []
    await d.tick(100 + 601)
    (nudge,) = client.texts("fg")
    assert nudge.startswith("@claude-coder [K-1] it's been quiet here for 10 min")
    await d.tick(100 + 900)
    assert len(client.texts("fg")) == 1  # not again until another quiet spell
    await d.tick(100 + 601 + 601)
    assert len(client.texts("fg")) == 2


async def test_activity_in_the_room_restarts_the_quiet_clock(ledger):
    client, d = make(ledger, agent_quiet_s=600)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}],
                     data={"stage_started": 0.0})
    await d.handle(ev(1, "still running the tests", room="fg", name="claude-coder"))
    active = ledger.get_request(rid).data["active_at"]
    assert "human_at" not in ledger.get_request(rid).data  # an agent, not a person
    await d.tick(active + 300)
    assert client.texts("fg") == []


async def test_charter_nudges_only_bidders_who_have_not_bid(ledger):
    client, d = make(ledger, agent_quiet_s=60)
    request_in(ledger, "CHARTER", rooms={"charter": "ch"}, bids=[{"agent": "claude-coder"}],
               data={"stage_started": 0.0, "live_bidders": ["claude-coder", "codex-coder"]})
    await d.tick(61)
    (nudge,) = client.texts("ch")
    assert nudge.startswith("@codex-coder [K-1]") and "@claude-coder " not in nudge


async def test_no_nudge_while_a_person_is_needed(ledger):
    client, d = make(ledger, agent_quiet_s=60, human_wait_s=0)
    request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="c", data={"gate": "merge", "stage_started": 0.0})
    request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="drill", data={"stage_started": 0.0})
    await d.tick(10_000)
    assert not any("quiet here" in t for t in client.texts())  # stage timeouts may still ping


async def test_agents_cannot_hand_off_for_people(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, data={"gate": "pick"})
    await d.handle(ev(1, "🤖", room="ch", name="claude-coder"))  # a crew agent
    assert "handoff" not in ledger.get_request(rid).data
