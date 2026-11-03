import json
from pathlib import Path

import pytest

from kraken.conductor.state import Action, Signal
from kraken.config import Config
from kraken.dispatch import Dispatcher
from kraken.llm import LLMParseError
from kraken.models import Claim, Event
from kraken.sonar.code_check import index_code
from kraken.switch_client import SwitchError
from tests.fakes import FakeLLM

BIDDERS = ["claude-coder", "codex-coder", "opencode-coder"]
REQ_ROOM = "req-room"


class MockClient:
    agent_id = "kraken-id"

    def __init__(self, offline=(), fail_create=None, documents=None):
        self.ops: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []
        self.offline = set(offline)
        self.fail_create = fail_create
        self.documents = documents if documents is not None else []
        self.n = 0
        self.calls: list[tuple[str, dict]] = []

    async def op(self, name, /, **args):
        self.ops.setdefault(name, []).append(args)
        self.calls.append((name, args))
        if name == "create_room_from_yaml":
            if self.fail_create:
                raise self.fail_create
            self.n += 1
            return {"room_id": f"room-{self.n}"}
        if name == "list_agents":
            return [{"id": f"id-{b}", "name": b} for b in BIDDERS if b not in self.offline_listed()]
        if name == "get_agent_detail":
            return {"sessions": [{"state": "live"}]}
        if name == "connect_to_room":
            return {"documents": self.documents}
        if name == "load_internal_documents":
            return [{"id": args["ids"][0], "content": "BRIEF TEXT"}]
        if name == "create_room_document":
            return {"document_id": "doc-1"}
        if name == "list_rooms":
            return [{"room_id": REQ_ROOM, "name": "kraken-requests"},
                    {"room_id": "chat", "name": "product"}]
        return {"ok": True}

    def offline_listed(self):
        return self.offline

    async def send(self, room_id, content):
        self.sent.append((room_id, content))
        return "evt"

    def texts(self, room=None):
        return [t for r, t in self.sent if room is None or r == room]

    def created(self, word):
        return [c for c in self.ops.get("create_room_from_yaml", []) if word in c["yaml"]]


def ev(seq, body, room=REQ_ROOM, sender="u-sam", name="sam", addressed=False, kind="user",
       mid=None):
    return Event(type="message", room_id=room, sequence=seq, payload={
        "sender": sender, "sender_name": name, "sender_kind": kind, "addressed": addressed,
        "body": body, "message_id": mid or f"m{seq}", "timestamp": 1})


def make(ledger, client=None, llm=None, **cfg):
    client = client or MockClient()
    d = Dispatcher(client, llm or FakeLLM([]), ledger, Config(bidders=BIDDERS, **cfg), [],
                   self_id="kraken-id")
    d.room_ids = {"kraken-requests": REQ_ROOM}
    return client, d


def jclaims(*triples):
    return json.dumps({"claims": [dict(subject=s, attribute=a, value=v, confidence=0.9)
                                  for s, a, v in triples]})


YES = '{"is_decision": true}'
CONFLICT = '{"verdict": "conflict", "confidence": 0.9, "reason": "differ"}'
LONG1 = "we decided that user_id is a uuid string everywhere"
LONG2 = "no wait user_id is an integer in the database layer"


def conflict_llm():
    return FakeLLM([YES, jclaims(("user_id", "type", "uuid string")),
                    YES, jclaims(("user_id", "type", "integer")), CONFLICT])


def request_in(ledger, state, rooms=None, data=None, **kw):
    rid = ledger.new_request("sam", "do it", REQ_ROOM, state=state)
    ledger.update_request(rid, rooms=rooms or {}, data=data or {}, **kw)
    return rid


async def open_resolution(ledger, client=None):
    client, d = make(ledger, client, conflict_llm())
    await d.handle(ev(1, LONG1, sender="u-a", name="ann"))
    await d.handle(ev(2, LONG2, sender="u-b", name="bob"))
    return client, d


async def test_dispatch_is_idempotent_by_sequence(ledger):
    client, d = make(ledger)
    for _ in range(2):
        await d.handle(ev(5, "@kraken build password reset", addressed=True))
    assert len(client.created("Spark")) == 1
    assert ledger.last_sequence() == 5
    call = client.ops["create_room_from_yaml"][0]
    assert call["inputs"]["request"] == "build password reset" and call["inputs"]["owner"] == "sam"


async def test_bid_markers_drive_charter(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     data={"live_bidders": BIDDERS})
    for i, b in enumerate(BIDDERS):
        await d.handle(ev(10 + i, f"BID: {b}\nplan: x", room="ch", sender=f"id-{b}", name=b))
    assert len(ledger.get_request(rid).bids) == 3
    assert any("Pick one" in t and "[K-%d]" % rid in t for t in client.texts("ch"))
    assert ledger.get_request(rid).data["gate"] == "pick"


async def test_duplicate_bid_from_same_agent_ignored(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, data={"live_bidders": BIDDERS})
    for i in range(2):
        await d.handle(ev(10 + i, "BID: x", room="ch", name="claude-coder"))
    assert len(ledger.get_request(rid).bids) == 1


async def test_conflict_opens_resolution_room(ledger):
    client, d = await open_resolution(ledger)
    assert len(client.created("Sonar")) == 1
    call = client.created("Sonar")[0]["inputs"]
    assert "Which one is true?" in call["prompt_text"] and call["subject"] == "user_id.type"
    req = ledger.get_request(1)
    assert req.state == "RESOLVING" and req.rooms == {"sonar-resolution": "room-1"}
    assert req.data == {"a": 1, "b": 2, "stage_started": req.data["stage_started"]}


async def test_stage_error_posts_retry(ledger):
    client, d = make(ledger, MockClient(fail_create=SwitchError(500, "boom")))
    await d.handle(ev(1, "@kraken build it", addressed=True))
    (text,) = client.texts(REQ_ROOM)
    assert "spark failed: boom" in text and "reply retry" in text and "[K-1]" in text
    assert ledger.get_request(1).state == "SPARK"


async def test_retry_in_origin_room_reopens_stage(ledger):
    client, d = make(ledger, MockClient(fail_create=SwitchError(500, "boom")))
    await d.handle(ev(1, "@kraken build it", addressed=True))
    client.fail_create = None
    await d.handle(ev(2, "retry"))
    assert len(client.created("Spark")) == 2
    assert ledger.get_request(1).rooms == {"spark": "room-1"}


async def test_llm_parse_error_skips_message(ledger):
    client, d = make(ledger, llm=FakeLLM([LLMParseError("bad")]))
    await d.handle(ev(1, LONG1))
    assert ledger.known_subjects() == [] and client.ops == {} and ledger.last_sequence() == 1


async def test_offline_bidder_excluded(ledger):
    client, d = make(ledger, MockClient(offline={"opencode-coder"}))
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    inputs = client.created("Charter")[0]["inputs"]
    assert inputs["bidder_1"] == "claude-coder" and inputs["other_bidders"] == "codex-coder"
    assert any("bidding with claude-coder, codex-coder; offline: opencode-coder" in t
               for t in client.texts(REQ_ROOM))
    assert ledger.get_request(rid).data["live_bidders"] == ["claude-coder", "codex-coder"]


async def test_extra_bidders_invited(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    invites = client.ops["invite_agent_to_room"]
    assert [i["agent_name"] for i in invites] == ["codex-coder", "opencode-coder"]
    assert all(i["room_id"] == "room-1" for i in invites)


async def test_marker_from_wrong_sender_ignored(ledger):
    client, d = make(ledger, MockClient(documents=[{"id": "d1", "name": "brief"}]))
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    await d.handle(ev(1, "BRIEF READY", room="sp", name="mallory"))
    assert ledger.get_request(rid).brief is None and "connect_to_room" not in client.ops
    await d.handle(ev(2, "BRIEF READY", room="sp", name="kraken-planner"))
    assert ledger.get_request(rid).brief == "BRIEF TEXT"
    assert ledger.get_request(rid).data["gate"] == "brief"


async def test_missing_brief_document_posts_notice(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    assert client.texts("sp") == [f"[K-{rid}] no `brief` document found"]
    assert ledger.get_request(rid).brief is None


async def test_platform_kickoff_ignored(ledger):
    client, d = make(ledger, llm=FakeLLM([]))
    await d.handle(ev(1, "@kraken " + LONG1, addressed=True, kind="platform"))
    assert client.ops == {} and client.sent == []


async def test_reviewer_accepted_and_coach_markers(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg", "storm": "st"}, winner="claude-coder",
                     data={"live_bidders": BIDDERS})
    await d.handle(ev(1, "ACCEPTED", room="fg", name="claude-coder"))  # the coder, not a reviewer
    assert "gate" not in ledger.get_request(rid).data
    await d.handle(ev(2, "all criteria met\nACCEPTED", room="fg", name="codex-coder"))
    assert ledger.get_request(rid).data["gate"] == "merge"
    ledger.update_request(rid, state="STORM", data={})
    await d.handle(ev(3, "SURVIVED", room="st", name="storm-coach"))
    assert ledger.get_request(rid).data["gate"] == "storm_result"


async def test_resolution_pick_stores_decision_and_opens_charter(ledger):
    client, d = await open_resolution(ledger)
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    ((_, claim, by),) = ledger.decisions()
    assert claim.value == "uuid string" and by == "carol"
    assert ledger.pair_seen(1, 2)
    req = ledger.get_request(1)
    assert req.state == "CHARTER"
    assert req.brief == ("Make the system consistent with the decision: "
                         "user_id.type = uuid string (decided by carol). "
                         "Acceptance: (1) every place that uses user_id.type matches the "
                         "decided value; (2) a test asserts it.")
    assert len(client.created("Charter")) == 1


async def test_resolution_not_a_conflict_dismisses_and_closes(ledger):
    client, d = await open_resolution(ledger)
    await d.handle(ev(3, "🙅", room="room-1", name="carol"))
    assert ledger.get_request(1).state == "CLOSED"
    assert ledger.dismissal_count("user_id", "type") == 1 and ledger.decisions() == []


async def test_other_command_in_resolution_gets_hint(ledger):
    client, d = await open_resolution(ledger)
    await d.handle(ev(3, "approve", room="room-1", name="carol"))
    assert client.texts("room-1")[-1] == "[K-1] reply 1️⃣, 2️⃣ or 🙅"
    assert ledger.get_request(1).state == "RESOLVING"


async def test_preflight_conflicts_rendered_and_posted(ledger):
    decision = Claim(subject="user_id", attribute="type", value="uuid string", speaker="u",
                     room_id="chat", message_id="m9", timestamp=1, confidence=1.0)
    ledger.add_decision(decision, "sam", None)
    llm = FakeLLM([YES, jclaims(("user_id", "type", "integer")), CONFLICT])
    client, d = make(ledger, MockClient(documents=[{"id": "d1", "name": "brief"}]), llm,
                     autonomy={"brief": "auto"})
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    assert ledger.get_request(rid).state == "PREFLIGHT"
    assert ("Brief says user_id.type = integer; #chat (message m9) says uuid string"
            in client.texts(REQ_ROOM))
    assert ledger.get_request(rid).data["gate"] == "preflight"


async def test_timeout_tick_emits_once(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"},
                     data={"live_bidders": BIDDERS, "stage_started": 1000.0})
    await d.tick(1000.0 + 100)
    assert client.sent == []
    await d.tick(1000.0 + 901)
    assert len(client.sent) == 1 and "no bids received" in client.sent[0][1]
    await d.tick(1000.0 + 2000)
    assert len(client.sent) == 1
    assert ledger.get_request(rid).data["timed_out"] == "CHARTER"


async def test_auto_pick_uses_judged_winner(ledger):
    llm = FakeLLM(['{"winner": "codex-coder"}'])
    client, d = make(ledger, llm=llm, autonomy={"pick": "auto"})
    rid = request_in(ledger, "CHARTER", rooms={"charter": "ch"}, brief="B",
                     data={"live_bidders": BIDDERS[:2]})
    await d.handle(ev(1, "BID: a", room="ch", name="claude-coder"))
    await d.handle(ev(2, "BID: b", room="ch", name="codex-coder"))
    req = ledger.get_request(rid)
    assert req.state == "FORGE" and req.winner == "codex-coder"
    assert llm.calls[0]["user"].startswith("Brief:\nB") and "2. codex-coder" in llm.calls[0]["user"]


async def test_gap_recovers_unseen_messages(ledger):
    client, d = make(ledger)

    async def op(name, **args):
        client.ops.setdefault(name, []).append(args)
        if name == "read_context":
            e = {"id": "g1", "kind": "message", "sender": "u-sam", "sender_name": "sam",
                 "body": "@kraken build it", "timestamp": 1}
            return {"threads": [{"root": e, "replies": []}]}
        return {"room_id": "room-1"}

    client.op = op
    gap = Event(type="gap", room_id=REQ_ROOM, sequence=None, payload={})
    await d.handle(gap)
    await d.handle(gap)
    assert len(client.created("Spark")) == 1


async def test_bad_event_does_not_wedge_stream(ledger):
    client, d = make(ledger, llm=FakeLLM([RuntimeError("boom")]))
    await d.handle(ev(1, LONG1))
    assert ledger.last_sequence() == 1


# ---- fix round 1 ----------------------------------------------------------
async def test_live_event_for_gap_handled_message_is_skipped(ledger):
    client, d = make(ledger)

    async def op(name, **args):
        client.ops.setdefault(name, []).append(args)
        if name == "read_context":
            e = {"id": "g1", "kind": "message", "sender": "u-sam", "sender_name": "sam",
                 "body": "@kraken build it", "timestamp": 1}
            return {"threads": [{"root": e, "replies": []}]}
        return {"room_id": "room-1"}

    client.op = op
    await d.handle(Event(type="gap", room_id=REQ_ROOM, sequence=None, payload={}))
    await d.handle(ev(9, "@kraken build it", addressed=True, mid="g1"))
    assert len(client.created("Spark")) == 1


async def test_chat_in_resolution_room_is_not_sonar_input(ledger):
    client, d = await open_resolution(ledger)
    d.llm._responses += [YES, jclaims(("user_id", "type", "string")), CONFLICT]
    before = len(d.llm.calls)
    await d.handle(ev(3, LONG2, room="room-1", name="dave"))
    assert len(d.llm.calls) == before
    assert len(ledger.claims_for("user_id", "type")) == 2
    assert len(client.created("Sonar")) == 1


async def test_failed_resolution_open_recovers_with_retry(ledger):
    client = MockClient(fail_create=SwitchError(500, "boom"))
    client, d = await open_resolution(ledger, client)
    assert any("sonar-resolution failed: boom — reply [K-1] retry" in t
               for t in client.texts(REQ_ROOM))
    client.fail_create = None
    await d.handle(ev(3, "[K-1] retry"))
    call = client.created("Sonar")[-1]  # [0] is the failed attempt
    assert call["inputs"]["subject"] == "user_id.type" and "uuid string" in call["inputs"]["quote_a"]
    assert ledger.get_request(1).rooms == {"sonar-resolution": "room-1"}


def preflight_failure_setup(ledger, llm):
    client, d = make(ledger, MockClient(documents=[{"id": "d1", "name": "brief"}]), llm,
                     autonomy={"brief": "auto"})
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    return client, d, rid


async def test_preflight_exception_posts_notice_and_allows_approve(ledger):
    client, d, rid = preflight_failure_setup(ledger, FakeLLM([RuntimeError("boom")]))
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    assert f"[K-{rid}] preflight failed: boom — reply retry or approve" in client.texts(REQ_ROOM)
    assert ledger.get_request(rid).data["gate"] == "preflight"
    await d.handle(ev(2, f"[K-{rid}] approve", room="sp", name="sam"))
    assert ledger.get_request(rid).state == "CHARTER"


async def test_retry_reruns_preflight(ledger):
    client, d, rid = preflight_failure_setup(
        ledger, FakeLLM([RuntimeError("boom"), YES, jclaims()]))
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    await d.handle(ev(2, f"[K-{rid}] retry", room="sp", name="sam"))
    assert ledger.get_request(rid).state == "CHARTER" and len(client.created("Charter")) == 1


async def test_commands_from_known_agents_ignored(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "live_bidders": BIDDERS})
    await d.handle(ev(1, "👍", room="fg", name="codex-coder"))
    assert ledger.get_request(rid).state == "FORGE" and client.sent == []
    await d.handle(ev(2, "👍", room="fg", name="sam"))
    assert ledger.get_request(rid).state == "STORM"


async def test_resolution_pick_publishes_hallmark(ledger):
    client, d = await open_resolution(ledger)
    d.room_ids["kraken-hallmark"] = "hm"
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    names = [n for n, _ in client.calls]
    i = names.index("create_room_document")
    assert client.calls[i - 1] == ("connect_to_room", {"room_id": "hm"})
    (created,) = client.ops["create_room_document"]
    assert "user_id.type** = uuid string" in created["content"]


async def test_hallmark_failure_does_not_break_dispatch(ledger):
    client, d = await open_resolution(ledger)
    d.room_ids["kraken-hallmark"] = "hm"
    orig = client.op

    async def op(name, /, **args):
        if name == "create_room_document":
            raise SwitchError(500, "boom")
        return await orig(name, **args)

    client.op = op
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    assert ledger.get_request(1).state == "CHARTER"


async def test_missing_hallmark_room_is_skipped(ledger):
    client, d = await open_resolution(ledger)
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    assert "create_room_document" not in client.ops


# ---- final review ----------------------------------------------------------
async def test_invited_bidders_are_woken_with_a_mention(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "PREFLIGHT", brief="B")
    await d.apply(rid, Signal(kind="preflight_clear"))
    wake = [t for t in client.texts("room-1") if "@codex-coder" in t and "@opencode-coder" in t]
    assert len(wake) == 1
    assert f"[K-{rid}]" in wake[0] and "post your BID as described in the room instructions" in wake[0]


async def test_invited_reviewers_are_woken_with_a_mention(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"charter": "ch"}, brief="B", winner="claude-coder",
                     bids=[{"agent": b} for b in BIDDERS], data={"live_bidders": BIDDERS})
    await d.runner.run(ledger.get_request(rid), [Action(kind="open_stage", stage="forge")])
    room = ledger.get_request(rid).rooms["forge"]
    assert [i["agent_name"] for i in client.ops["invite_agent_to_room"]] == ["opencode-coder"]
    wake = [t for t in client.texts(room) if "@opencode-coder" in t]
    assert len(wake) == 1 and f"[K-{rid}]" in wake[0]
    assert "review against the brief; post ACCEPTED on its own line" in wake[0]


def connect_before_create(client):
    """room_id connected to right before each create_room_from_yaml."""
    out, connected = [], None
    for name, args in client.calls:
        if name == "connect_to_room":
            connected = args["room_id"]
        elif name == "create_room_from_yaml":
            out.append(connected)
    return out


async def test_stage_room_is_filed_under_the_latest_stage_room(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"spark": "sp", "charter": "ch"}, brief="B",
                     winner="claude-coder", bids=[{"agent": b} for b in BIDDERS],
                     data={"live_bidders": BIDDERS})
    await d.runner.run(ledger.get_request(rid), [Action(kind="open_stage", stage="forge")])
    assert connect_before_create(client) == ["ch"]


async def test_first_stage_room_is_filed_under_the_origin_room(ledger):
    client, d = make(ledger)
    await d.handle(ev(1, "@kraken build it", addressed=True))
    assert connect_before_create(client) == [REQ_ROOM]


FREE_TIER_MSG = "#product: free tier is 100 requests per day"
LIMITS = Path(__file__).parent / "fixtures" / "code" / "limits.yaml"


def free_tier_llm():
    return FakeLLM([YES, jclaims(("free_tier", "value", "100 requests per day")), CONFLICT])


async def test_chat_claim_against_code_opens_resolution(ledger):
    client, d = make(ledger, llm=free_tier_llm())
    d.code_index = index_code([str(LIMITS)])
    await d.handle(ev(1, FREE_TIER_MSG, name="pm"))
    (created,) = client.created("Sonar")
    inputs = created["inputs"]
    assert inputs["subject"] == "free_tier.value"
    assert inputs["link_a"].endswith("limits.yaml:2") and "1000" in inputs["quote_a"]
    req = ledger.get_request(1)
    assert req.state == "RESOLVING"
    code = ledger.get_claim(req.data["a"])
    assert code.source == "code" and code.value == "1000" and code.line == 2
    assert ledger.get_claim(req.data["b"]).value == "100 requests per day"
    assert ledger.pair_seen(req.data["a"], req.data["b"])


async def test_same_chat_message_again_opens_no_second_room(ledger):
    client, d = make(ledger, llm=free_tier_llm())
    d.code_index = index_code([str(LIMITS)])
    await d.handle(ev(1, FREE_TIER_MSG, name="pm"))
    await d.handle(ev(2, FREE_TIER_MSG, name="pm", mid="m1"))  # redelivered
    assert len(client.created("Sonar")) == 1


async def test_code_claim_is_stored_once(ledger):
    llm = FakeLLM([YES, jclaims(("free_tier", "value", "100 requests per day")), CONFLICT,
                   YES, jclaims(("free_tier", "value", "200 requests per day")), CONFLICT,
                   CONFLICT])
    client, d = make(ledger, llm=llm)
    d.code_index = index_code([str(LIMITS)])
    await d.handle(ev(1, FREE_TIER_MSG, name="pm"))
    await d.handle(ev(2, "#product: actually the free tier is 200 requests per day", name="pm"))
    code = [i for i, c in ledger.claims_for("free_tier", "value") if c.source == "code"]
    assert len(code) == 1
    assert len(client.created("Sonar")) == 3  # chat1-code, chat2-chat1, chat2-code


async def test_matching_code_value_raises_nothing(ledger):
    client, d = make(ledger, llm=FakeLLM([YES, jclaims(("free_tier", "value", "1000"))]))
    d.code_index = index_code([str(LIMITS)])
    await d.handle(ev(1, "#product: the free tier is 1000 requests per day", name="pm"))
    assert client.created("Sonar") == [] and ledger.claims_for("free_tier", "value")[0][1].source == "chat"


async def test_resolution_room_quotes_the_messages(ledger):
    client, d = await open_resolution(ledger)
    inputs = client.created("Sonar")[0]["inputs"]
    assert inputs["quote_a"] == LONG1 and inputs["user_a"] == "ann"
    assert inputs["quote_b"] == LONG2 and inputs["user_b"] == "bob"
    assert inputs["link_a"] == inputs["link_b"] == "#kraken-requests"
    src = json.loads(ledger.get_meta("claimsrc:1"))
    assert src == {"sender_name": "ann", "body": LONG1, "room_name": "kraken-requests"}


async def test_claim_source_truncated_and_unknown_room_falls_back_to_id(ledger):
    long = "we decided that user_id is a uuid string " + "x" * 400
    client, d = make(ledger, llm=FakeLLM([YES, jclaims(("user_id", "type", "uuid string")),
                                          YES, jclaims(("user_id", "type", "integer")),
                                          CONFLICT]))
    await d.handle(ev(1, long, room="elsewhere", sender="u-a", name="ann"))
    await d.handle(ev(2, LONG2, room="chat", sender="u-b", name="bob"))
    inputs = client.created("Sonar")[0]["inputs"]
    assert inputs["quote_a"] == long[:300] and inputs["link_a"] == "#elsewhere"
    assert inputs["link_b"] == "#product"


async def test_hallmark_shows_room_name(ledger):
    client, d = await open_resolution(ledger)
    d.room_ids["kraken-hallmark"] = "hm"
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    (created,) = client.ops["create_room_document"]
    assert "source #kraken-requests" in created["content"]


def resolving(ledger, subject, values, room):
    ids = [ledger.add_claim(Claim(subject=subject, attribute="type", value=v, speaker="u",
                                  room_id="chat", message_id=f"m-{v}", timestamp=1,
                                  confidence=0.9)) for v in values]
    return request_in(ledger, "RESOLVING", rooms={"sonar-resolution": room},
                      data={"a": ids[0], "b": ids[1]})


@pytest.mark.parametrize("answer", ["1️⃣", "🙅"])
async def test_settling_a_resolution_closes_its_siblings(ledger, answer):
    client, d = make(ledger)
    k1 = resolving(ledger, "user_id", ["uuid", "int"], "s1")
    k2 = resolving(ledger, "user_id", ["uuid", "string"], "s2")
    k3 = resolving(ledger, "order_id", ["uuid", "int"], "s3")
    await d.handle(ev(1, answer, room="s1", name="carol"))
    assert ledger.get_request(k2).state == "CLOSED"
    assert client.texts("s2") == [f"[K-{k2}] closed: settled in [K-{k1}]"]
    assert ledger.get_request(k3).state == "RESOLVING" and client.texts("s3") == []


async def test_unexpected_publish_error_does_not_strand_resolution(ledger):
    client, d = await open_resolution(ledger)
    d.room_ids["kraken-hallmark"] = "hm"
    orig = client.op

    async def op(name, /, **args):
        if name == "create_room_document":
            raise RuntimeError("boom")
        return await orig(name, **args)

    client.op = op
    await d.handle(ev(3, "1️⃣", room="room-1", name="carol"))
    assert ledger.get_request(1).state == "CHARTER"


async def test_reaction_runs_command_when_receipt_fails(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "FORGE", rooms={"forge": "fg"}, winner="claude-coder",
                     data={"gate": "merge", "live_bidders": BIDDERS})
    orig = client.send
    calls = []

    async def send(room_id, content):
        calls.append(content)
        if len(calls) == 1:
            raise SwitchError(500, "boom")
        return await orig(room_id, content)

    client.send = send
    assert await d.handle_reaction("👍", "sam", rid) is True
    assert ledger.get_request(rid).state == "STORM"


async def test_new_requests_use_default_storm_mode(ledger):
    client, d = make(ledger, llm=conflict_llm(), storm={"default_mode": "drill"})
    await d.handle(ev(1, "@kraken build it", addressed=True))
    await d.handle(ev(2, LONG1, sender="u-a", name="ann"))
    await d.handle(ev(3, LONG2, sender="u-b", name="bob"))
    assert [r.storm_mode for r in ledger.open_requests()] == ["drill", "drill"]
