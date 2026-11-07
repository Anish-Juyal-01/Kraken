"""Agents added in Switch join the crew on their own: no Kraken change per agent."""
from kraken.config import Config
from kraken.conductor.runner import ActionRunner
from kraken.crew import Crew


class Client:
    def __init__(self, agents):
        self.agents, self.sent = agents, []

    async def op(self, name, **args):
        assert name == "list_agents"
        return self.agents

    async def send(self, room, text):
        self.sent.append((room, text))


AGENTS = [
    {"name": "claude-coder", "known_agent_type": "claude-code"},
    {"name": "gemini-coder", "known_agent_type": "antigravity"},
    {"name": "codex-coder", "known_agent_type": "codex"},
    {"name": "kimi-coder", "known_agent_type": "opencode"},
    {"name": "kraken", "known_agent_type": None},          # Kraken itself
    {"name": "kraken-smoke", "known_agent_type": None},    # a custom agent: never auto-joins
    {"name": "squall", "known_agent_type": "claude-code"},  # already the attacker
]


def runner(ledger, agents, **cfg):
    c = Config(bidders=["claude-coder"], **cfg)
    Crew(ledger).seed(c)
    return ActionRunner(Client(agents), ledger, c, publish=lambda: None)


async def test_any_vendor_added_in_switch_becomes_a_bidder(ledger):
    r = runner(ledger, AGENTS)
    joined = await r.auto_join()
    assert joined == ["gemini-coder", "codex-coder", "kimi-coder"]
    assert set(r.crew.members("bidder")) == {"claude-coder", "gemini-coder", "codex-coder", "kimi-coder"}
    assert r.crew.role_of("squall") == "attacker"  # agents with a role keep it
    assert await r.auto_join() == []  # nothing new the second time


async def test_retired_agents_stay_off_until_enlisted_again(ledger):
    r = runner(ledger, AGENTS)
    await r.auto_join()
    assert r.crew.retire("codex-coder")
    assert await r.auto_join() == [] and "codex-coder" not in r.crew.members("bidder")
    r.crew.enlist("codex-coder", "bidder")
    assert "codex-coder" in r.crew.members("bidder") and "codex-coder" not in r.crew.retired()


async def test_auto_join_can_be_turned_off_or_skip_names(ledger):
    assert await runner(ledger, AGENTS, charter={"auto_join": False}).auto_join() == []


async def test_an_agent_that_refuses_kraken_is_dropped_and_bidding_goes_on(ledger):
    from tests.test_dispatch import ev, make, request_in
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     data={"live_bidders": ["claude-coder", "gemini-coder"]})
    await d.handle(ev(1, "BID: claude-coder\nplan: x", room="ch", name="claude-coder"))
    assert ledger.get_request(rid).data.get("gate") is None  # still waiting for gemini
    refusal = ("@kraken You're not permitted to direct messages to me in this room — "
               "my operator has restricted who can address me here.")
    await d.handle(ev(2, refusal, room="ch", name="gemini-coder"))
    req = ledger.get_request(rid)
    assert req.data["live_bidders"] == ["claude-coder"]
    assert req.data["gate"] == "pick"  # the round closed with the bids it has
    assert any("gemini-coder can't bid" in t and "addressing to Anyone" in t for t in client.texts("ch"))


async def test_a_bidder_whose_turn_failed_is_dropped_with_the_reason(ledger):
    from tests.test_dispatch import ev, make, request_in
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     data={"live_bidders": ["claude-coder", "gemini-coder"]})
    await d.handle(ev(1, "BID: claude-coder\nplan: x", room="ch", name="claude-coder"))
    crash = ("I finished without replying. The turn failed: APIError: You exceeded your current "
             "quota, please check your plan and billing details.\n* Quota exceeded for metric: ...")
    await d.handle(ev(2, crash, room="ch", name="gemini-coder"))
    req = ledger.get_request(rid)
    assert req.data["live_bidders"] == ["claude-coder"] and req.data["gate"] == "pick"
    note = next(t for t in client.texts("ch") if "can't bid this round" in t)
    assert "APIError: You exceeded your current quota" in note and "Quota exceeded for metric" not in note


async def test_never_join_keeps_an_agent_out(ledger):
    r = runner(ledger, AGENTS, charter={"never_join": ["kimi-coder"]})
    assert "kimi-coder" not in await r.auto_join()
