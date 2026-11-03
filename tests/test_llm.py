import pytest
from pydantic import BaseModel

from kraken.llm import FAST, SMART, ClaudeLLM, LLMParseError
from tests.fakes import FakeLLM


class OkModel(BaseModel):
    ok: bool


async def test_retry_once_then_succeed(stub_client):
    stub_client.replies = ["not json", '{"ok": true}']
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is True and len(stub_client.requests) == 2
    assert "ValidationError" in stub_client.requests[1]["user"] or "validation" in stub_client.requests[1]["user"].lower()


async def test_two_failures_raise(stub_client):
    stub_client.replies = ["x", "y"]
    with pytest.raises(LLMParseError):
        await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert len(stub_client.requests) == 2


async def test_first_try_and_request_shape(stub_client):
    stub_client.replies = ['{"ok": false}']
    out = await ClaudeLLM(client=stub_client).json(SMART, "sys", "usr", OkModel)
    assert out.ok is False
    req = stub_client.requests[0]
    assert req["model"] == SMART and req["system"] == "sys" and req["max_tokens"] == 4096
    assert req["messages"] == [{"role": "user", "content": "usr"}]


async def test_strips_code_fence(stub_client):
    stub_client.replies = ['```json\n{"ok": true}\n```']
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is True and len(stub_client.requests) == 1


async def test_fake_llm():
    fake = FakeLLM([OkModel(ok=True), '{"ok": false}', LLMParseError()])
    assert (await fake.json(FAST, "s", "u", OkModel)).ok is True
    assert (await fake.json(FAST, "s2", "u2", OkModel)).ok is False
    with pytest.raises(LLMParseError):
        await fake.json(FAST, "s3", "u3", OkModel)
    assert fake.calls[1] == {"model": FAST, "system": "s2", "user": "u2", "schema": OkModel}


async def test_truncated_reply_is_retried(stub_client):
    stub_client.replies = ['{"ok": true}', '{"ok": false}']
    stub_client.stop_reasons = ["max_tokens", "end_turn"]
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is False and len(stub_client.requests) == 2


async def test_two_bad_stop_reasons_raise(stub_client):
    stub_client.replies = ['{"ok": true}', '{"ok": true}']
    stub_client.stop_reasons = ["max_tokens", "refusal"]
    with pytest.raises(LLMParseError):
        await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert len(stub_client.requests) == 2
    assert set(stub_client.requests[0]) == {"model", "max_tokens", "system", "messages", "user"}
