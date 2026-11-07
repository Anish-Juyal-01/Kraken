"""A team, not one person: requesters see their own rooms, teammates can take a step
over, and two requests on one repo build one at a time."""
import yaml

from tests.test_dispatch import BIDDERS, REQ_ROOM, ev, make, request_in


async def test_the_requester_is_in_their_own_rooms_and_mentions(ledger):
    client, d = make(ledger, people=["anish_2007"])
    rid = ledger.new_request("priya", "add rate limiting", REQ_ROOM)
    req = ledger.get_request(rid)
    assert d.runner.people(req) == ["anish_2007", "priya"]
    await d.runner._open(req, "spark", {})
    room = yaml.safe_load(client.ops["create_room_from_yaml"][0]["yaml"])["room"]
    assert set(room["users"]) == {"anish_2007", "priya"}


def test_agents_are_never_added_as_people(ledger):
    client, d = make(ledger, people=["anish_2007", BIDDERS[0]])
    rid = ledger.new_request(BIDDERS[1], "x", REQ_ROOM)  # e.g. a Sonar request raised by an agent
    assert d.runner.people(ledger.get_request(rid)) == ["anish_2007"]


async def test_a_teammate_can_take_a_step_over(ledger):
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "gate_since": 1000.0})
    await d.handle(ev(1, "🙋", room="fg", name="sam"))
    await d.handle(ev(2, "🙋", room="fg", name="priya"))
    assert ledger.get_request(rid).data["claimed"] == {"gate:merge": "priya"}
    assert "priya takes over Forge (merge OK) from sam" in client.texts("fg")[-1]
    await d.handle(ev(3, "me", room="fg", name="priya"))
    assert "already yours" in client.texts("fg")[-1]


async def test_reclaiming_when_nothing_waits_says_so(ledger):
    client, d = make(ledger, human_wait_s=120)
    request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder")  # agents building
    await d.handle(ev(1, "🙋", room="fg", name="priya"))
    assert "waiting on a person right now" in client.texts("fg")[-1]


async def test_two_requests_on_one_repo_build_one_at_a_time(ledger):
    client, d = make(ledger, deliver="branch")
    first = request_in(ledger, "FORGE", rooms={"forge": "fg1"}, winner=BIDDERS[0], storm_mode="skip",
                       data={"gate": "merge", "gate_since": 1000.0, "project": "app"})
    second = request_in(ledger, "CHARTER", rooms={"charter": "ch2"}, bids=[{"agent": BIDDERS[1]}, {"agent": BIDDERS[2]}],
                        data={"gate": "pick", "project": "app"})
    await d.handle(ev(1, "pick 1", room="ch2", name="priya"))
    req = ledger.get_request(second)
    assert req.state == "FORGE" and req.data["queued"] and "forge" not in req.rooms
    assert f"[K-{first}] is still building or testing app" in client.texts(REQ_ROOM)[-1]
    await d.tick(10**9)  # waiting in the queue never times out
    assert not ledger.get_request(second).data.get("timed_out")

    await d.handle(ev(2, "approve", room="fg1", name="sam"))  # the first one finishes
    assert ledger.get_request(first).state == "CLOSED"
    req = ledger.get_request(second)
    assert "forge" in req.rooms and not req.data.get("queued")
    assert any("its turn" in t for t in client.texts(REQ_ROOM))


async def test_requests_on_different_repos_build_side_by_side(ledger):
    client, d = make(ledger)
    request_in(ledger, "STORM", rooms={"storm": "st1"}, data={"project": "other"})
    second = request_in(ledger, "CHARTER", rooms={"charter": "ch2"}, bids=[{"agent": BIDDERS[1]}, {"agent": BIDDERS[2]}],
                        data={"gate": "pick", "project": "app"})
    await d.handle(ev(1, "pick 1", room="ch2", name="priya"))
    req = ledger.get_request(second)
    assert "forge" in req.rooms and not req.data.get("queued")


async def test_an_agent_that_asks_is_not_listed_as_a_person(ledger):
    client, d = make(ledger, people=["anish_2007"])
    rid = ledger.new_request("kraken-smoke", "x", REQ_ROOM)
    client.agents = {**{b: {"sessions": [{"state": "live"}]} for b in BIDDERS}, "kraken-smoke": {}}
    await d.runner._open(ledger.get_request(rid), "spark", {})
    room = yaml.safe_load(client.ops["create_room_from_yaml"][0]["yaml"])["room"]
    assert room["users"] == ["anish_2007"]


async def test_a_switch_warning_never_takes_a_step(ledger):
    """Live K-12: the gateway's "isn't in this room" warning, posted as admin, claimed the
    planner's question and froze the 2-minute fallback."""
    client, d = make(ledger, human_wait_s=120)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"}, data={"asked_at": 1000.0})
    warning = ("⚠️ @kraken **kraken-smoke (`kraken-smoke`)** isn't in this room, so your message "
               "won't reach it. Add the agent from the room's detail page in the gateway.")
    await d.handle(ev(1, warning, room="sp", name="admin"))
    assert "claimed" not in ledger.get_request(rid).data
    await d.tick(1000 + 121)
    assert ledger.get_request(rid).data.get("answers_handed")  # the agent took it as normal
