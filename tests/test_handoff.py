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


async def test_a_slow_planner_never_hands_spark_off_before_anyone_is_asked(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, data={"stage_started": 0.0})
    await d.tick(10_000)  # the planner is still reading the code: nobody has been asked
    req = ledger.get_request(rid)
    assert "handoff" not in req.data and not req.data.get("answers_handed")


async def test_planner_question_with_no_answer_goes_to_the_planner_after_2_min(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, data={"stage_started": 0.0})
    await d.handle(ev(1, "Should /v2/ping also report the version?", room="sp", name="kraken-planner"))
    asked = ledger.get_request(rid).data["asked_at"]
    note = client.texts("sp")[-1]
    assert note.startswith("@sam [K-1] the planner is asking you something")
    assert "Who's handling this" in note and "No answer in 2 min: the agent takes it" in note
    await d.tick(asked + 119)
    assert not ledger.get_request(rid).data.get("answers_handed")
    await d.tick(asked + 121)
    req = ledger.get_request(rid)
    assert req.data["answers_handed"] and "SPARK answers" in req.data["handoff_by"]
    assert "decide the open answers yourself" in client.texts("sp")[-1]
    assert "handoff" not in req.data  # the brief is NOT handed off: a person still approves it


async def test_answering_the_planner_claims_it_with_no_time_limit(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, data={"stage_started": 0.0})
    await d.handle(ev(1, "Should /v2/ping also report the version?", room="sp", name="kraken-planner"))
    await d.handle(ev(2, "no, just pong", room="sp", name="sam"))
    req = ledger.get_request(rid)
    assert req.data["claimed"] == {"answers": "sam"}
    assert "sam is handling answering the planner: no time limit" in client.texts("sp")[-1]
    await d.tick(req.data["asked_at"] + 100_000)  # however long it takes
    assert not ledger.get_request(rid).data.get("answers_handed")


async def test_after_spark_answers_are_handed_off_the_brief_still_waits_for_a_person(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, brief="B",
                     data={"gate": "brief", "gate_since": 1000.0, "answers_handed": True})
    await d.tick(1000 + 119)
    assert ledger.get_request(rid).state == "SPARK"  # its own fresh 2 minutes
    await d.tick(1000 + 121)
    assert ledger.get_request(rid).state in ("PREFLIGHT", "CHARTER")


async def test_drill_storm_waits_for_squall_then_gives_people_their_turn(ledger):
    client, d = make(ledger, human_wait_s=120, people=["anish_2007"])
    rid = request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="drill",
                     data={"stage_started": 0.0})
    await d.tick(10_000)  # Squall is still attacking: nothing to defend yet
    assert ledger.get_request(rid).storm_mode == "drill"
    await d.handle(ev(1, "3 faults done, /v2/ping 500s on bad input. @storm-defender @storm-coach",
                      room="st", name="squall"))
    req = ledger.get_request(rid)
    since = req.data["defend_since"]
    note = client.texts("st")[-1]
    assert note.startswith("@anish_2007 @sam [K-1] Squall has broken things") and "No answer in 2 min" in note
    await d.tick(since + 119)
    assert ledger.get_request(rid).storm_mode == "drill"
    await d.tick(since + 121)
    req = ledger.get_request(rid)
    assert req.storm_mode == "auto" and req.data["defend_handed"]
    assert client.texts("st")[-1].startswith("@storm-defender [K-1] Kraken (no reply from a person in 2 min)")
    assert "handoff" not in req.data  # the coach's result still goes to a person


async def test_gate_questions_say_the_person_decides_first(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}])
    await d.handle(ev(1, "ok\nACCEPTED", room="fg", name="codex-coder"))
    q = client.texts("fg")[-1]
    assert q.startswith("[K-1] Reviewers accepted. Merge? ⏳ Who's handling this?")
    assert "🙋 or `me` to do it yourself (no time limit)" in q and "No answer in 2 min" in q


async def test_claiming_a_gate_stops_the_clock_and_agent_can_still_take_it(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "gate_since": 1000.0})
    await d.handle(ev(1, "🙋", room="fg", name="sam"))
    req = ledger.get_request(rid)
    assert req.data["claimed"] == {"gate:merge": "sam"}
    assert "sam is handling Forge (merge OK): no time limit" in client.texts("fg")[-1]
    await d.tick(1000 + 100_000)
    assert ledger.get_request(rid).state == "FORGE"  # still sam's, no takeover
    await d.handle(ev(2, "🙋", room="fg", name="sam"))
    assert "already yours" in client.texts("fg")[-1]
    await d.handle(ev(3, "🤖", room="fg", name="sam"))  # changed their mind
    assert ledger.get_request(rid).state == "STORM"


async def test_deciding_straight_away_needs_no_claim_message(ledger):
    client, d = make(ledger, human_wait_s=120, deliver="branch")
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "gate_since": 1000.0})
    await d.handle(ev(1, "approve", room="fg", name="sam"))
    assert ledger.get_request(rid).state == "STORM"
    assert not any("is handling" in t for t in client.texts())


async def test_a_second_accepted_does_not_ask_for_the_merge_again(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}, {"agent": "opencode-coder"}])
    await d.handle(ev(1, "ok\nACCEPTED", room="fg", name="codex-coder"))
    since = ledger.get_request(rid).data["gate_since"]
    await d.handle(ev(2, "agreed\nACCEPTED", room="fg", name="opencode-coder"))
    assert sum("Merge?" in t for t in client.texts("fg")) == 1
    assert ledger.get_request(rid).data["gate_since"] == since  # the clock didn't restart


async def test_handing_off_the_storm_result_is_labelled_as_such(ledger):
    client, d = make(ledger, deliver="branch")
    request_in(ledger, "STORM", rooms={"storm": "st"}, data={"gate": "storm_result"})
    await d.handle(ev(1, "🤖", room="st", name="sam"))
    assert any("Storm (accepting the result): handed to the agent" in t for t in client.texts("st"))


def test_the_pull_request_question_has_no_deadline():
    from kraken.conductor.state import _PR_QUESTION
    assert "No time limit" in _PR_QUESTION and "within" not in _PR_QUESTION


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
    request_in(ledger, "STORM", rooms={"storm": "st"}, storm_mode="drill",
               data={"stage_started": 0.0, "defend_since": 0.0})  # the people's turn to defend
    await d.tick(10_000)
    assert not any("quiet here" in t for t in client.texts())  # stage timeouts may still ping


async def test_agents_cannot_hand_off_for_people(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, data={"gate": "pick"})
    await d.handle(ev(1, "🤖", room="ch", name="claude-coder"))  # a crew agent
    assert "handoff" not in ledger.get_request(rid).data


async def test_storm_asks_who_defends_as_soon_as_it_opens(ledger):
    client, d = make(ledger, human_wait_s=120, deliver="branch", people=["anish_2007"])
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     storm_mode="drill", data={"gate": "merge", "gate_since": 1000.0})
    await d.handle(ev(1, "approve", room="fg", name="sam"))
    req = ledger.get_request(rid)
    st = req.rooms["storm"]
    ask = client.texts(st)[-1]
    assert req.state == "STORM" and req.data["defend_since"] is not None
    assert ask.startswith("@anish_2007 @sam [K-1] Storm is starting") and "No answer in 2 min" in ask
    await d.handle(ev(2, "🙋", room=st, name="anish_2007"))  # a person defends: no time limit
    assert ledger.get_request(rid).data["claimed"] == {"defend": "anish_2007"}
    await d.handle(ev(3, "/v2/ping 500s on bad input. @storm-defender @storm-coach", room=st, name="squall"))
    note = client.texts(st)[-1]
    assert note.startswith("@anish_2007 @sam [K-1] Squall has broken things") and "No answer" not in note
    await d.tick(10**9)
    assert ledger.get_request(rid).storm_mode == "drill"  # still the person's


async def test_storm_nobody_answers_the_defender_takes_it(ledger):
    client, d = make(ledger, human_wait_s=120, deliver="branch")
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     storm_mode="drill", data={"gate": "merge", "gate_since": 1000.0})
    await d.handle(ev(1, "approve", room="fg", name="sam"))
    since = ledger.get_request(rid).data["defend_since"]
    await d.tick(since + 121)
    req = ledger.get_request(rid)
    assert req.storm_mode == "auto" and req.data["defend_handed"]
