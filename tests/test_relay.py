from aiohttp.test_utils import TestClient, TestServer

from kraken.relay.discord_relay import build_payload, extract_tag
from kraken.relay.server import make_app
from tests.fakes import FakeLLM
from tests.test_dispatch import MockClient, ev, make, request_in

SECRET = "s3cret"
HEADERS = {"X-Kraken-Secret": SECRET}


async def post(calls, body, headers=HEADERS, result=True):
    async def on_reaction(emoji, user, tag):
        calls.append((emoji, user, tag))
        return result

    async with TestClient(TestServer(make_app(on_reaction, SECRET))) as client:
        resp = await client.post("/relay/reaction", json=body, headers=headers)
        return resp.status, (await resp.json() if resp.status == 200 else None)


async def test_wrong_secret_401():
    calls = []
    for headers in ({"X-Kraken-Secret": "nope"}, {}):
        status, _ = await post(calls, {"tag": 12, "emoji": "👍", "user": "sam"}, headers)
        assert status == 401
    assert calls == []


async def test_valid_reaction_calls_command():
    calls = []
    status, body = await post(calls, {"tag": 12, "emoji": "👍", "user": "sam"})
    assert (status, body) == (200, {"handled": True})
    assert calls == [("👍", "sam", 12)]


async def test_unknown_emoji_ignored():
    calls = []
    status, _ = await post(calls, {"tag": 12, "emoji": "🎉", "user": "sam"})
    assert status == 204 and calls == []


async def test_bad_body_400():
    calls = []
    for body in ({"tag": "12", "emoji": "👍", "user": "sam"}, {"tag": 1, "emoji": "", "user": "s"},
                 {"tag": 1, "emoji": "👍"}, {"tag": True, "emoji": "👍", "user": "s"}, [1]):
        status, _ = await post(calls, body)
        assert status == 400
    assert calls == []


def test_extract_tag_and_payload():
    assert extract_tag("[K-12] Approve this brief?\n👍") == 12
    assert extract_tag("no tag here") is None
    assert build_payload("[K-7] hi", "👍", "sam") == {"tag": 7, "emoji": "👍", "user": "sam"}
    assert build_payload("plain", "👍", "sam") is None


async def test_reaction_at_brief_gate_posts_receipt_and_advances(ledger):
    client, d = make(ledger, MockClient(documents=[{"id": "d1", "name": "brief"}]),
                     FakeLLM([RuntimeError("boom")]))
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    await d.handle(ev(1, "BRIEF READY", room="sp", name="kraken-planner"))
    assert ledger.get_request(rid).state == "SPARK"
    assert await d.handle_reaction("👍", "sam", rid) is True
    assert "👍 from sam — received" in client.texts("sp")
    assert ledger.get_request(rid).state == "PREFLIGHT"


async def test_reaction_from_agent_or_unknown_tag_ignored(ledger):
    client, d = make(ledger)
    rid = request_in(ledger, "SPARK", rooms={"spark": "sp"})
    assert await d.handle_reaction("👍", "kraken-planner", rid) is False
    assert await d.handle_reaction("👍", "sam", 999) is False
    assert await d.handle_reaction("🎉", "sam", rid) is False
    assert client.sent == []
