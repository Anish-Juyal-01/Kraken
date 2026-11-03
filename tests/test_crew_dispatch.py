from kraken.conductor.state import Signal
from kraken.crew import Crew
from tests.test_dispatch import BIDDERS, REQ_ROOM, MockClient, ev, make, request_in


def agent(kind, state="live", models=None):
    d = {"known_agent_type": kind, "sessions": [{"state": state}] if state else []}
    if models:
        d["models"] = models
    return d


KIMI = [{"name": "moonshot/kimi-k2"}]


def roster(**extra):
    return {"claude-coder": agent("claude-code"), "codex-coder": agent("codex"),
            "opencode-coder": agent("opencode", state="closed", models=KIMI), **extra}


async def say(d, seq, body, room=REQ_ROOM, name="sam", **kw):
    await d.handle(ev(seq, body, room=room, name=name, **kw))


async def test_enlist_unknown_agent(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "enlist ghost")
    assert client.texts(REQ_ROOM) == ["No Switch agent named ghost."]
    assert Crew(ledger).role_of("ghost") is None


async def test_enlist_kraken_itself_rejected(ledger):
    client, d = make(ledger, MockClient(agents=roster(kraken=agent("custom"))))
    await say(d, 1, "enlist kraken")
    assert client.texts(REQ_ROOM) == ["Kraken runs the crew; it can't join it."]
    assert Crew(ledger).role_of("kraken") is None


async def test_enlist_ok_default_role_and_repeat(ledger):
    client, d = make(ledger, MockClient(agents=roster(**{"kimi-coder": agent("opencode")})))
    await say(d, 1, "@kraken enlist kimi-coder", addressed=True)
    await say(d, 2, "enlist kimi-coder")
    assert client.texts(REQ_ROOM) == ["kimi-coder enlisted as bidder.",
                                      "kimi-coder is already a bidder."]
    assert Crew(ledger).members("bidder")[-1] == "kimi-coder"
    assert client.created("Spark") == []  # an addressed command is not a new request


async def test_enlist_with_role_moves_agent(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "enlist claude-coder as coach")
    assert client.texts(REQ_ROOM) == ["claude-coder enlisted as coach."]
    crew = Crew(ledger)
    assert crew.role_of("claude-coder") == "coach" and "claude-coder" not in crew.members("bidder")


async def test_retire(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "retire claude-coder")
    await say(d, 2, "retire claude-coder")
    assert client.texts(REQ_ROOM) == ["claude-coder retired.", "claude-coder isn't on the crew."]
    assert "claude-coder" not in Crew(ledger).all_names()


async def test_crew_listing_shows_vendor_and_liveness(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "crew")
    (text,) = client.texts(REQ_ROOM)
    assert text.splitlines() == [
        "bidder: claude-coder (claude-code) \u25cf \u00b7 codex-coder (codex) \u25cf \u00b7 "
        "opencode-coder (opencode \u00b7 moonshot/kimi-k2) \u25cb",
        "planner: kraken-planner (not in Switch) \u25cb",
        "attacker: squall (not in Switch) \u25cb",
        "defender: storm-defender (not in Switch) \u25cb",
        "coach: storm-coach (not in Switch) \u25cb",
    ]


async def test_crew_listing_empty_role(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    Crew(ledger).retire("storm-coach")
    await say(d, 1, "crew")
    assert client.texts(REQ_ROOM)[0].splitlines()[-1] == "coach: none"


async def test_crew_commands_outside_requests_room_ignored(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, data={"live_bidders": BIDDERS})
    for seq, (room, body) in enumerate([("chat", "crew"), ("chat", "retire claude-coder"),
                                        ("ch", "enlist codex-coder as coach"),
                                        ("ch", "retire codex-coder")], 1):
        await say(d, seq, body, room=room)
    assert client.sent == [] and "get_agent_detail" not in client.ops
    assert Crew(ledger).members("bidder") == BIDDERS
    assert ledger.get_request(rid).state == "CHARTER"


async def test_crew_commands_from_known_agents_ignored(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "retire opencode-coder", name="codex-coder")
    await say(d, 2, "crew", name="storm-coach")
    assert client.sent == [] and Crew(ledger).members("bidder") == BIDDERS


async def test_enlisted_agent_is_filtered_like_any_known_agent(ledger):
    client, d = make(ledger, MockClient(agents=roster(**{"kimi-coder": agent("opencode")})))
    await say(d, 1, "enlist kimi-coder")
    client.sent.clear()
    await say(d, 2, "retire claude-coder", name="kimi-coder")
    await say(d, 3, "crew", name="kimi-coder")
    assert client.sent == [] and "claude-coder" in Crew(ledger).members("bidder")
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "live_bidders": BIDDERS})
    await say(d, 4, "\U0001f44d", room="fg", name="kimi-coder")
    assert ledger.get_request(rid).state == "FORGE"
    assert await d.handle_reaction("\U0001f44d", "kimi-coder", rid) is False


async def test_enlisted_bidder_joins_next_charter(ledger):
    agents = roster(**{"kimi-coder": agent("opencode", models=KIMI)})
    agents["opencode-coder"] = agent("opencode", models=KIMI)
    client, d = make(ledger, MockClient(agents=agents))
    await say(d, 1, "enlist kimi-coder")
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    inputs = client.created("Charter")[0]["inputs"]
    assert inputs["other_bidders"] == "codex-coder, opencode-coder, kimi-coder"
    assert ledger.get_request(rid).data["live_bidders"] == [*BIDDERS, "kimi-coder"]


async def test_retired_bidder_is_not_asked_to_bid(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    await say(d, 1, "retire codex-coder")
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    assert ledger.get_request(rid).data["live_bidders"] == ["claude-coder"]
    notice = next(t for t in client.texts(REQ_ROOM) if "bidding with" in t)
    assert "codex-coder" not in notice


async def test_retiring_mid_charter_keeps_the_chosen_bidders(ledger):
    client, d = make(ledger, MockClient(agents=roster()))
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     data={"live_bidders": BIDDERS[:2]})
    await say(d, 1, "retire codex-coder")
    await d.handle(ev(2, "BID: x", room="ch", sender="id-codex", name="codex-coder"))
    assert [b["agent"] for b in ledger.get_request(rid).bids] == ["codex-coder"]


async def test_brief_ready_marker_follows_the_crew_planner(ledger):
    client, d = make(ledger, MockClient(agents=roster(), documents=[{"id": "d1", "name": "brief"}]))
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    Crew(ledger).enlist("new-planner", "planner")
    Crew(ledger).retire("kraken-planner")
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    assert ledger.get_request(rid).brief is None
    await d.handle(ev(2, "BRIEF READY", room="sp", name="new-planner"))
    assert ledger.get_request(rid).brief == "BRIEF TEXT"


def big_crew(ledger, **cfg):
    agents = {"c1": agent("claude-code"), "c2": agent("claude-code"), "x1": agent("codex"),
              "g1": agent("gemini"), "k1": agent("opencode", models=KIMI),
              "off": agent("gemini", state="closed")}
    client, d = make(ledger, MockClient(agents=agents), **cfg)
    crew = Crew(ledger)
    for name in list(crew.members("bidder")):
        crew.retire(name)
    for name in ("c1", "c2", "x1", "g1", "k1", "off"):
        crew.enlist(name, "bidder")
    return client, d


async def charter_bidders(ledger, d):
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    return ledger.get_request(rid).data["live_bidders"]


async def test_charter_caps_bidders_with_vendor_diversity_and_rotates(ledger):
    client, d = big_crew(ledger, charter={"max_bidders": 3})
    assert await charter_bidders(ledger, d) == ["c1", "x1", "g1"]
    assert await charter_bidders(ledger, d) == ["c2", "x1", "g1"]
    assert await charter_bidders(ledger, d) == ["x1", "g1", "k1"]
    inputs = client.created("Charter")[0]["inputs"]
    assert inputs["bidder_1"] == "c1" and inputs["other_bidders"] == "x1, g1"
    assert [i["agent_name"] for i in client.ops["invite_agent_to_room"][:2]] == ["x1", "g1"]


async def test_charter_cap_ignores_offline_members_and_still_reports_them(ledger):
    client, d = big_crew(ledger, charter={"max_bidders": 2})
    assert await charter_bidders(ledger, d) == ["c1", "x1"]
    assert any("offline: off" in t for t in client.texts(REQ_ROOM))


async def test_charter_under_cap_seats_everyone_online(ledger):
    client, d = big_crew(ledger)
    assert await charter_bidders(ledger, d) == ["c1", "c2", "x1", "g1", "k1"]
