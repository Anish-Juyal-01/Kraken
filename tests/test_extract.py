from kraken.llm import FAST, SMART
from kraken.models import Event
from kraken.sonar.extract import ClaimList, IsDecision, RawClaim, canonical, extract
from tests.fakes import FakeLLM


def msg(**overrides) -> Event:
    fields = {
        "sender": "@sam", "sender_name": "Sam", "body": "we're switching user_id to UUID strings",
        "message_id": "m1", "timestamp": 1730700000000, "thread_id": None,
    }
    room_id = overrides.pop("room_id", "backend")
    fields.update(overrides)
    return Event(type="message", room_id=room_id, sequence=1, payload=fields)


async def test_ignores_own_and_agent_quotes():
    llm = FakeLLM([])
    assert await extract(llm, msg(sender="kraken-id", body="user_id is a uuid string from monday on"), "kraken-id", 6) == []
    assert await extract(llm, msg(body="[K-3] conflict: > user_id is a uuid string"), "kraken-id", 6) == []
    assert await extract(llm, msg(body="> user_id is a uuid string from monday\nok"), "kraken-id", 6) == []
    assert llm.calls == []


async def test_marker_lines_and_short_and_missing_body_skipped():
    llm = FakeLLM([])
    for body in ["BID: I will build the whole api layer now", "ACCEPTED", "x\nFAILED tests did not pass at all today"]:
        assert await extract(llm, msg(body=body), "k", 2) == []
    assert await extract(llm, msg(body="too short"), "k", 6) == []
    ev = msg()
    del ev.payload["body"]
    assert await extract(llm, ev, "k", 6) == []
    assert await extract(llm, Event(type="join", room_id="r", sequence=None, payload={}), "k", 6) == []
    assert llm.calls == []


async def test_chitchat_dropped_by_filter():
    llm = FakeLLM([IsDecision(is_decision=False)])
    assert await extract(llm, msg(body="lol that standup was long today ngl"), "k", 6) == []
    assert [c["model"] for c in llm.calls] == [FAST]


async def test_claim_fields_filled_from_event():
    llm = FakeLLM([IsDecision(is_decision=True), ClaimList(claims=[RawClaim(subject="user_id", attribute="type", value="uuid string", confidence=0.9)])])
    [c] = await extract(llm, msg(body="we're switching user_id to UUID strings from Monday", room_id="backend", message_id="m1", sender="@sam"), "k", 6)
    assert (c.room_id, c.message_id, c.speaker) == ("backend", "m1", "@sam")
    assert (c.subject, c.value, c.timestamp, c.confidence) == ("user_id", "uuid string", 1730700000000, 0.9)
    assert [x["model"] for x in llm.calls] == [FAST, SMART]
    assert llm.calls[1]["schema"] is ClaimList


async def test_quoted_lines_stripped_before_count_and_prompt():
    llm = FakeLLM([IsDecision(is_decision=True), ClaimList(claims=[])])
    body = "> old quoted line here\nwe will ship the new api on friday"
    assert await extract(llm, msg(body=body), "k", 6) == []
    assert "old quoted" not in llm.calls[0]["user"]


def test_canonical():
    assert {canonical(s) for s in ["userId", "user id", "User-ID", "user_id"]} == {"user_id"}
