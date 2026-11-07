import dataclasses

from kraken.conductor.commands import Command
from kraken.conductor.state import Signal, stage_room, step
from kraken.config import Config
from kraken.hallmark.ledger import Request

ALL_AUTO = dict(brief="auto", pick="auto", merge="auto", storm_result="auto")


def req(**kw) -> Request:
    base = dict(id=1, owner="sam", text="build reset", origin_room="req-room", state="SPARK")
    base.update(kw)
    return Request(**base)


def cfg_with(**kw) -> Config:
    return Config(**kw)


def apply(r: Request, updates: dict, actions) -> Request:
    return dataclasses.replace(r, **updates)


def cmd(kind, arg=None, tag=None):
    return Command(kind=kind, arg=arg, tag=tag, user="u")


def gated(state, gate, **kw):
    return req(state=state, data={"gate": gate}, **kw)


def kinds(acts):
    return [a.kind for a in acts]


_AUTO_RUN = [Signal(kind="brief_posted", data={"brief": "b"}), Signal(kind="preflight_clear"),
             Signal(kind="bid", data={"agent": "codex-coder"}),
             Signal(kind="bids_done", data={"judged_winner": "codex-coder"}),
             Signal(kind="accepted"), Signal(kind="survived")]


def test_full_happy_path_auto_ends_on_a_branch():
    cfg = cfg_with(autonomy=ALL_AUTO, deliver="branch")
    r = req(state="SPARK")
    for s in _AUTO_RUN:
        r = apply(r, *step(r, cfg, s))
    assert r.state == "CLOSED" and r.winner == "codex-coder"


def test_full_auto_still_asks_a_person_before_a_pull_request():
    cfg = cfg_with(autonomy=ALL_AUTO)  # deliver: pr is the default
    r = req(state="SPARK")
    for s in _AUTO_RUN:
        r = apply(r, *step(r, cfg, s))
    assert r.state == "DELIVER" and r.data["gate"] == "deliver"


def test_deliver_approve_asks_the_coder_then_pr_link_closes():
    r = gated("DELIVER", "deliver", winner="codex-coder")
    upd, acts = step(r, Config(), cmd("approve"))
    assert upd["data"]["delivering"] and "gate" not in upd["data"]
    (ask,) = acts
    assert ask.stage == "forge" and ask.text.startswith("@codex-coder [K-1] approved")
    assert "kraken/K-1" in ask.text and "PR: <url>" in ask.text
    # Storm's fixes live in the defender's clone (live K-5): the PR must include them
    assert "storm-defender" in ask.text and ask.text.index("fixes") < ask.text.index("push the branch")
    r = apply(r, *(upd, acts))
    upd, acts = step(r, Config(), Signal(kind="delivered", data={"url": "https://gh/o/r/pull/9"}))
    assert upd["state"] == "CLOSED" and upd["data"]["pr"] == "https://gh/o/r/pull/9"
    assert kinds(acts) == ["publish_hallmark", "close"] and "pull/9" in acts[1].text


def test_deliver_reject_closes_without_a_pull_request():
    upd, acts = step(gated("DELIVER", "deliver", winner="c"), Config(), cmd("reject"))
    assert upd["state"] == "CLOSED" and "no pull request" in acts[0].text
    assert acts[0].room == "origin" and kinds(acts)[1:] == ["publish_hallmark", "close"]


def test_deliver_failure_pings_and_retry_reasks():
    r = req(state="DELIVER", winner="c", data={"delivering": True})
    upd, acts = step(r, Config(), Signal(kind="deliver_failed", data={"reason": "no gh auth"}))
    assert kinds(acts) == ["ping_owner"] and "no gh auth" in acts[0].text
    upd, acts = step(r, Config(), cmd("retry"))
    assert acts[0].stage == "forge" and "push the branch" in acts[0].text


def test_survived_with_pr_delivery_asks_in_the_storm_room():
    upd, acts = step(req(state="STORM"), cfg_with(autonomy=ALL_AUTO), Signal(kind="survived"))
    assert upd["state"] == "DELIVER" and upd["data"]["gate"] == "deliver"
    assert acts[0].stage == "storm" and "Open a pull request?" in acts[0].text


def test_storm_fail_loops_then_pings_owner():
    cfg = cfg_with(autonomy=ALL_AUTO)
    r = req(state="STORM")
    for i in range(2):
        upd, acts = step(r, cfg, Signal(kind="failed", data={"findings": "bad"}))
        r = apply(r, upd, acts)
        assert r.state == "FORGE" and r.storm_round == i + 1
        assert acts[0].kind == "post" and acts[0].stage == "forge"
        assert acts[0].text.endswith(f"[K-1] Storm round {i + 1} failed:\nbad")
        r = apply(r, *step(r, cfg, Signal(kind="accepted")))
        assert r.state == "STORM"
    upd, acts = step(r, cfg, Signal(kind="failed"))
    r = apply(r, upd, acts)
    assert r.state == "STORM" and r.storm_round == 3
    assert kinds(acts) == ["ping_owner"]


def test_stale_command_rejected():
    upd, acts = step(req(state="FORGE", id=4), Config(), Command(kind="pick", arg="1", user="u", tag=4))
    assert upd == {} and "already past" in acts[0].text


def test_wrong_tag_rejected():
    upd, acts = step(gated("SPARK", "brief", id=2), Config(), cmd("approve", tag=9))
    assert upd == {} and acts[0].text == "[K-2] is already past that step"
    assert acts[0].room == "stage"


def test_gate_command_without_gate_rejected():
    upd, acts = step(req(state="SPARK"), Config(), cmd("approve"))
    assert upd == {} and acts[0].text == "[K-1] is already past that step"


def test_gate_command_wrong_gate_rejected():
    upd, acts = step(gated("FORGE", "pick"), Config(), cmd("approve"))
    assert upd == {} and "already past" in acts[0].text


def test_spark_brief_human_gates():
    upd, acts = step(req(), Config(), Signal(kind="brief_posted", data={"brief": "b"}))
    assert upd["brief"] == "b" and upd["data"]["gate"] == "brief" and "state" not in upd
    assert acts[0].kind == "post" and acts[0].text.startswith("[K-1] Brief ready.")
    assert "\U0001f44d approve · \U0001f44e reject" in acts[0].text


def test_spark_brief_auto_goes_preflight():
    upd, acts = step(req(), cfg_with(autonomy=ALL_AUTO), Signal(kind="brief_posted", data={"brief": "b"}))
    assert upd["state"] == "PREFLIGHT" and upd["brief"] == "b"
    assert acts[0].text == "running Sonar pre-flight"


def test_spark_approve():
    upd, acts = step(gated("SPARK", "brief"), Config(), cmd("approve"))
    assert upd["state"] == "PREFLIGHT" and "gate" not in upd["data"]
    assert acts[0].text == "running Sonar pre-flight"


def test_spark_reject():
    upd, acts = step(gated("SPARK", "brief"), Config(), cmd("reject", "too vague"))
    assert "state" not in upd and "gate" not in upd["data"]
    assert acts[0].stage == "spark" and acts[0].text.startswith("@kraken-planner ") and acts[0].text.endswith("Revise the brief: too vague")
    _, acts = step(gated("SPARK", "brief"), Config(), cmd("reject"))
    assert acts[0].text.endswith("Revise the brief: no reason given")


def test_preflight_clear():
    upd, acts = step(req(state="PREFLIGHT"), Config(), Signal(kind="preflight_clear"))
    assert upd["state"] == "CHARTER"
    assert acts[0].kind == "open_stage" and acts[0].stage == "charter"


def test_preflight_conflicts_post_and_gate():
    upd, acts = step(req(state="PREFLIGHT"), Config(),
                     Signal(kind="preflight_conflicts", data={"conflicts": ["c1", "c2"]}))
    assert "state" not in upd and upd["data"]["gate"] == "preflight"
    assert [a.text for a in acts[:2]] == ["c1", "c2"]
    assert acts[2].text.startswith("[K-1] Resolve the conflicts above, then approve.")
    assert acts[2].text.endswith("\U0001f44d approve")


def test_preflight_approve():
    upd, acts = step(gated("PREFLIGHT", "preflight"), Config(), cmd("approve"))
    assert upd["state"] == "CHARTER" and "gate" not in upd["data"]
    assert acts[0].kind == "open_stage" and acts[0].stage == "charter"


def test_charter_bid_appends():
    r = req(state="CHARTER", bids=[{"agent": "a"}])
    upd, acts = step(r, Config(), Signal(kind="bid", data={"agent": "b", "plan": "x"}))
    assert upd == {"bids": [{"agent": "a"}, {"agent": "b", "plan": "x"}]} and acts == []
    assert r.bids == [{"agent": "a"}]


def test_charter_bids_done_human_prompt():
    r = req(state="CHARTER", bids=[{"agent": "a"}, {"agent": "b"}])
    for kind in ("bids_done", "timeout"):
        upd, acts = step(r, Config(), Signal(kind=kind))
        assert upd["data"]["gate"] == "pick" and "state" not in upd
        assert acts[0].text.startswith("[K-1] Bids are in. Pick one. ⏳ Who's handling this?")
        assert "No answer in 2 min: the agent takes it." in acts[0].text
        assert acts[0].text.endswith("\n1️⃣ a · 2️⃣ b")


def test_charter_bids_done_auto():
    r = req(state="CHARTER", bids=[{"agent": "a"}])
    upd, acts = step(r, cfg_with(autonomy=ALL_AUTO), Signal(kind="bids_done", data={"judged_winner": "a"}))
    assert upd["winner"] == "a" and upd["state"] == "FORGE"
    assert acts[0].kind == "open_stage" and acts[0].stage == "forge"


def test_charter_no_bids_pings_owner():
    for kind in ("bids_done", "timeout"):
        upd, acts = step(req(state="CHARTER"), Config(), Signal(kind=kind))
        assert upd == {} and kinds(acts) == ["ping_owner"]
        assert acts[0].room == "origin" and acts[0].text.startswith("[K-1] needs you: ")


def test_charter_pick_by_index_and_name():
    bids = [{"agent": "a"}, {"agent": "b"}]
    for arg, want in (("2", "b"), ("a", "a")):
        upd, acts = step(gated("CHARTER", "pick", bids=bids), Config(), cmd("pick", arg))
        assert upd["winner"] == want and upd["state"] == "FORGE" and "gate" not in upd["data"]
        assert acts[0].kind == "open_stage" and acts[0].stage == "forge"


def test_charter_pick_unknown():
    bids = [{"agent": "a"}]
    for arg in ("zzz", "0", "2"):
        upd, acts = step(gated("CHARTER", "pick", bids=bids), Config(), cmd("pick", arg))
        assert upd == {} and acts[0].text == f"[K-1] no bid matches '{arg}'"


def test_forge_accepted_human_gate():
    upd, acts = step(req(state="FORGE"), Config(), Signal(kind="accepted"))
    assert upd["data"]["gate"] == "merge" and "state" not in upd
    assert acts[0].text.startswith("[K-1] Reviewers accepted. Merge?")


def test_forge_accepted_auto_storm_or_skip():
    cfg = cfg_with(autonomy=ALL_AUTO)
    upd, acts = step(req(state="FORGE"), cfg, Signal(kind="accepted"))
    assert upd["state"] == "STORM" and acts[0].stage == "storm" and acts[0].kind == "open_stage"
    upd, acts = step(req(state="FORGE", storm_mode="skip"), cfg, Signal(kind="accepted"))
    assert upd["state"] == "CLOSED" and acts[0].kind == "close"


def test_forge_approve_and_reject():
    upd, acts = step(gated("FORGE", "merge"), Config(), cmd("approve"))
    assert upd["state"] == "STORM" and "gate" not in upd["data"] and acts[0].stage == "storm"
    upd, acts = step(gated("FORGE", "merge", storm_mode="skip"), Config(), cmd("approve"))
    assert upd["state"] == "CLOSED" and acts[0].kind == "close"
    upd, acts = step(gated("FORGE", "merge"), Config(), cmd("reject", "nope"))
    assert "state" not in upd and "gate" not in upd["data"]
    assert acts[0].stage == "forge" and acts[0].text.endswith("Changes requested: nope")


def test_close_action_shape():
    _, acts = step(req(state="FORGE", storm_mode="skip", id=7), cfg_with(autonomy=ALL_AUTO),
                   Signal(kind="accepted"))
    assert acts[0].room == "origin" and acts[0].text == "[K-7] closed"


def test_storm_survived_human_then_approve():
    upd, acts = step(req(state="STORM"), Config(), Signal(kind="survived"))
    assert upd["data"]["gate"] == "storm_result" and "state" not in upd
    assert acts[0].text.startswith("[K-1] Storm survived. Accept?")
    upd, acts = step(gated("STORM", "storm_result"), Config(deliver="branch"), cmd("approve"))
    assert upd["state"] == "CLOSED" and kinds(acts) == ["publish_hallmark", "close"]
    upd, _ = step(gated("STORM", "storm_result"), Config(), cmd("approve"))
    assert upd["state"] == "DELIVER"


def test_storm_survived_auto():
    upd, acts = step(req(state="STORM"), cfg_with(autonomy=ALL_AUTO, deliver="branch"),
                     Signal(kind="survived"))
    assert upd["state"] == "CLOSED" and kinds(acts) == ["publish_hallmark", "close"]


def test_storm_reject_loops_to_forge():
    r = gated("STORM", "storm_result", storm_round=0)
    upd, acts = step(r, Config(), cmd("reject", "flaky"))
    assert upd["state"] == "FORGE" and upd["storm_round"] == 1 and "gate" not in upd["data"]
    assert acts[0].stage == "forge" and acts[0].text.endswith("Storm round 1 failed:\nflaky")


def test_storm_reject_at_cap_pings():
    r = gated("STORM", "storm_result", storm_round=2)
    upd, acts = step(r, Config(), cmd("reject", "flaky"))
    assert "state" not in upd and upd["storm_round"] == 3 and kinds(acts) == ["ping_owner"]


def test_storm_command_sets_mode():
    for state in ("SPARK", "PREFLIGHT", "CHARTER", "FORGE", "STORM", "RESOLVING"):
        upd, acts = step(req(state=state), Config(), cmd("storm", "drill"))
        assert upd == {"storm_mode": "drill"}
        assert acts[0].kind == "post" and acts[0].text == "Storm mode set to drill"
    assert step(req(state="CLOSED"), Config(), cmd("storm", "drill")) == ({}, [])


def test_retry_opens_current_stage():
    for state in ("SPARK", "CHARTER", "FORGE", "STORM"):
        upd, acts = step(req(state=state), Config(), cmd("retry"))
        assert upd == {} and acts[0].kind == "open_stage" and acts[0].stage == state.lower()
    assert step(req(state="PREFLIGHT"), Config(), cmd("retry")) == ({}, [])


def test_resolving_resolved_and_not_conflict():
    upd, acts = step(req(state="RESOLVING"), Config(), Signal(kind="resolved", data={"brief": "nb"}))
    assert upd["state"] == "CHARTER" and upd["brief"] == "nb" and acts[0].stage == "charter"
    upd, acts = step(req(state="RESOLVING"), Config(), cmd("not_conflict"))
    assert upd["state"] == "CLOSED" and acts[0].kind == "close"


def test_timeout_pings_owner():
    for state in ("SPARK", "FORGE", "STORM"):
        upd, acts = step(req(state=state), Config(), Signal(kind="timeout"))
        assert upd == {} and kinds(acts) == ["ping_owner"]


def test_unlisted_and_closed_ignored():
    assert step(req(state="PREFLIGHT"), Config(), Signal(kind="survived")) == ({}, [])
    assert step(req(state="CLOSED"), Config(), Signal(kind="timeout")) == ({}, [])
    assert step(gated("CLOSED", "pick"), Config(), cmd("pick", "1")) == ({}, [])


def test_stage_room_names():
    assert stage_room("RESOLVING") == "sonar-resolution" and stage_room("FORGE") == "forge"


def test_step_does_not_mutate_request():
    r = gated("SPARK", "brief")
    step(r, Config(), cmd("approve"))
    assert r.data == {"gate": "brief"} and r.state == "SPARK"


def test_charter_timeout_auto_pick_falls_back_to_first_bid():
    r = req(state="CHARTER", bids=[{"agent": "a"}, {"agent": "b"}])
    upd, acts = step(r, cfg_with(autonomy=ALL_AUTO), Signal(kind="timeout"))
    assert upd["winner"] == "a" and upd["state"] == "FORGE"
    assert acts[0].kind == "open_stage" and acts[0].stage == "forge"


def test_charter_auto_pick_unknown_judged_winner_falls_back():
    r = req(state="CHARTER", bids=[{"agent": "a"}, {"agent": "b"}])
    upd, acts = step(r, cfg_with(autonomy=ALL_AUTO),
                     Signal(kind="bids_done", data={"judged_winner": "ghost"}))
    assert upd["winner"] == "a" and upd["state"] == "FORGE"
    assert upd["data"]["pick"]["fallback_to_first_bid"] is True


def test_charter_auto_pick_is_recorded_for_audit():
    r = req(state="CHARTER", bids=[{"agent": "a"}, {"agent": "b"}])
    upd, _ = step(r, cfg_with(autonomy=ALL_AUTO), Signal(kind="bids_done", data={
        "judged_winner": "b", "pick_reason": "smaller change"}))
    assert upd["winner"] == "b"
    assert upd["data"]["pick"] == {"judged": "b", "reason": "smaller change", "order": ["a", "b"],
                                   "fallback_to_first_bid": False}


def test_charter_prompt_beyond_nine_bids_offers_pick_by_name():
    bids = [{"agent": f"a{i}"} for i in range(1, 11)]
    upd, acts = step(req(state="CHARTER", bids=bids), Config(), Signal(kind="bids_done"))
    lines = acts[0].text.splitlines()
    assert lines[-1] == "…or reply pick <name>"
    assert "9️⃣ a9" in lines[1] and "a10" not in lines[1]
    r = dataclasses.replace(req(state="CHARTER", bids=bids), data=upd["data"])
    upd2, _ = step(r, Config(), cmd("pick", "a10"))
    assert upd2["winner"] == "a10"


def test_charter_prompt_with_nine_bids_has_no_extra_line():
    bids = [{"agent": f"a{i}"} for i in range(1, 10)]
    _, acts = step(req(state="CHARTER", bids=bids), Config(), Signal(kind="bids_done"))
    assert "pick <name>" not in acts[0].text


def test_follow_ups_wake_the_agent_by_name():
    upd, acts = step(gated("FORGE", "merge", winner="codex-coder"), Config(), cmd("reject", "add a 404 test"))
    assert acts[0].text == "@codex-coder [K-1] Changes requested: add a 404 test"
