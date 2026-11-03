import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
import respx

from kraken.switch_client import SwitchClient, SwitchError, parse_sse

BASE = "http://switch.test"
AGENT = "agent-1"


def frame(seq, kind, data, with_id=True):
    lines = []
    if with_id:
        lines.append(f"id: {seq}")
    lines += [f"event: {kind}", f"data: {json.dumps(data)}", "", ""]
    return "\n".join(lines).encode()


def msg(seq, room="r"):
    return frame(seq, "message", {"type": "message", "room_id": room, "payload": {"text": "hi"}, "sequence": seq})


def sse(*chunks: bytes) -> httpx.Response:
    return httpx.Response(200, content=b"".join(chunks), headers={"content-type": "text/event-stream"})


@pytest.fixture
def switch():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        yield m


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield SwitchClient(BASE, AGENT, "key", "conn-1", http=http, reconnect_delay=0)


def test_parse_sse_frames():
    text = ["id: 7", "event: message", 'data: {"type":"message","room_id":"r","payload":{},"sequence":7}', "", ": keepalive", ""]
    assert list(parse_sse(text)) == [("7", "message", {"type": "message", "room_id": "r", "payload": {}, "sequence": 7})]


def test_parse_sse_control_frame_has_no_id():
    assert list(parse_sse(["event: gap", 'data: {"room_id": "r"}', ""])) == [(None, "gap", {"room_id": "r"})]


async def test_events_yields_and_tracks_cursor(switch, client):
    route = switch.get(f"/agents/{AGENT}/events").mock(
        return_value=sse(
            b": keepalive\n\n",
            frame(None, "connection_state", {"rooms": ["r", "s"], "cursor": 0}, with_id=False),
            msg(1),
            msg(2),
        )
    )
    got = []
    async for ev in client.events():
        got.append(ev)
        if len(got) == 2:
            break
    assert [(e.type, e.room_id, e.sequence) for e in got] == [("message", "r", 1), ("message", "r", 2)]
    assert client.cursor == 2
    assert client.rooms == ["r", "s"]
    req = route.calls[0].request
    assert req.url.params["connection_id"] == "conn-1"
    assert req.url.params["scope"] == "all"
    assert req.url.params["filter"] == "all"
    assert req.url.params["start_from"] == "head"
    assert req.headers["Accept"] == "text/event-stream"
    assert req.headers["Authorization"] == "Bearer key"
    assert "Last-Event-ID" not in req.headers


async def test_events_resume_with_last_event_id(switch, client):
    opens = []

    def handler(request):
        opens.append(request)
        return sse(msg(7)) if len(opens) == 1 else sse(msg(8))

    switch.get(f"/agents/{AGENT}/events").mock(side_effect=handler)
    seqs = []
    async for ev in client.events():
        seqs.append(ev.sequence)
        if len(seqs) == 2:
            break
    assert seqs == [7, 8]
    assert opens[1].headers["Last-Event-ID"] == "7"
    assert "start_from" not in opens[1].url.params
    assert opens[1].url.params["connection_id"] == "conn-1"


async def test_evicted_triggers_reconnect(switch, client):
    opens = []

    def handler(request):
        opens.append(request)
        if len(opens) == 1:
            return sse(msg(3), frame(None, "evicted", {}, with_id=False), msg(99))
        return sse(msg(4))

    switch.get(f"/agents/{AGENT}/events").mock(side_effect=handler)
    seqs = []
    async for ev in client.events():
        seqs.append(ev.sequence)
        if len(seqs) == 2:
            break
    assert seqs == [3, 4]
    assert opens[1].headers["Last-Event-ID"] == "3"


async def test_gap_frame_is_surfaced(switch, client):
    gap = {"room_id": "r", "from": 3, "to": 9}
    switch.get(f"/agents/{AGENT}/events").mock(return_value=sse(frame(None, "gap", gap, with_id=False)))
    async for ev in client.events():
        assert ev.type == "gap"
        assert ev.room_id == "r"
        assert ev.sequence is None
        assert ev.payload == gap
        break


async def test_reopen_flag_forces_reconnect(switch, client):
    opens = []

    async def blocking_body():
        yield msg(1)
        await asyncio.Event().wait()  # never fires: stream stays open

    def handler(request):
        opens.append(request)
        if len(opens) == 1:
            return httpx.Response(200, content=blocking_body(), headers={"content-type": "text/event-stream"})
        return sse(msg(2))

    switch.get(f"/agents/{AGENT}/events").mock(side_effect=handler)
    setter = None
    seqs = []
    async with aclosing(client.events()) as stream:
        async for ev in stream:
            seqs.append(ev.sequence)
            if len(seqs) == 1:
                async def later():
                    await asyncio.sleep(0.01)
                    client._reopen.set()

                setter = asyncio.create_task(later())
            else:
                break
    await setter
    assert seqs == [1, 2]
    assert len(opens) == 2
    assert opens[1].headers["Last-Event-ID"] == "1"


async def test_cursor_seeded_from_connection_state(switch, client):
    opens = []

    def handler(request):
        opens.append(request)
        if len(opens) == 1:
            return sse(frame(None, "connection_state", {"rooms": ["r"], "cursor": 42}, with_id=False))
        return sse(msg(43))

    switch.get(f"/agents/{AGENT}/events").mock(side_effect=handler)
    async with aclosing(client.events()) as stream:
        async for ev in stream:
            assert ev.sequence == 43
            break
    assert opens[1].headers["Last-Event-ID"] == "42"
    assert "start_from" not in opens[1].url.params


@pytest.mark.parametrize("first", [httpx.ConnectError("boom"), httpx.Response(503)])
async def test_reconnects_after_error_or_non_200(switch, client, first):
    switch.get(f"/agents/{AGENT}/events").mock(side_effect=[first, sse(msg(1))])
    async with aclosing(client.events()) as stream:
        async for ev in stream:
            assert ev.sequence == 1
            break


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_on_stream_raises(switch, client, status):
    switch.get(f"/agents/{AGENT}/events").mock(return_value=httpx.Response(status, json={"detail": "no"}))
    with pytest.raises(SwitchError) as ei:
        async for _ in client.events():
            pass
    assert ei.value.status == status


async def test_break_leaves_no_pending_tasks(switch, client):
    switch.get(f"/agents/{AGENT}/events").mock(return_value=sse(msg(1), msg(2)))
    before = asyncio.all_tasks() - {asyncio.current_task()}
    stream = client.events()
    async for _ in stream:
        break
    await stream.aclose()
    after = asyncio.all_tasks() - {asyncio.current_task()}
    assert after == before


async def test_heartbeat_reopen_on_409(switch, client):
    route = switch.post(f"/agents/{AGENT}/connection/beat").mock(
        side_effect=[httpx.Response(200, json={}), httpx.Response(409, json={"detail": "x"})]
    )
    client.cursor = 5
    task = asyncio.create_task(client.heartbeat_forever(interval=0.01))
    await asyncio.wait_for(client._reopen.wait(), 2)
    task.cancel()
    assert json.loads(route.calls[0].request.content) == {"connection_id": "conn-1", "cursor": 5}
    assert route.calls[0].request.headers["Authorization"] == "Bearer key"


async def test_op_unwraps_result_and_sends_connection_header(switch, client):
    route = switch.post(f"/agents/{AGENT}/ops/list_rooms").mock(
        return_value=httpx.Response(200, json={"result": [{"room_id": "r"}]})
    )
    assert await client.op("list_rooms", limit=3) == [{"room_id": "r"}]
    req = route.calls.last.request
    assert req.headers["X-Switch-Connection-Id"] == client.connection_id
    assert req.headers["Authorization"] == "Bearer key"
    assert json.loads(req.content) == {"limit": 3}


async def test_op_error_raises(switch, client):
    switch.post(f"/agents/{AGENT}/ops/read_context").mock(return_value=httpx.Response(403, json={"detail": "nope"}))
    with pytest.raises(SwitchError) as ei:
        await client.op("read_context", room_id="r")
    assert ei.value.status == 403
    assert ei.value.detail == "nope"


async def test_send_returns_event_id(switch, client):
    route = switch.post(f"/agents/{AGENT}/message").mock(
        return_value=httpx.Response(200, json={"ok": True, "event_id": "e1"})
    )
    assert await client.send("r", "hello") == "e1"
    assert json.loads(route.calls.last.request.content) == {"room_id": "r", "content": "hello"}


def test_parse_sse_skips_malformed_data_with_warning(caplog):
    lines = ["id: 3", "event: message", "data: {not json", "",
             "id: 4", "event: message", 'data: {"sequence": 4}', ""]
    with caplog.at_level("WARNING", logger="kraken.switch_client"):
        assert list(parse_sse(lines)) == [("4", "message", {"sequence": 4})]
    assert any("malformed" in r.message for r in caplog.records)


@pytest.mark.parametrize("first", [httpx.ConnectError("boom"), httpx.Response(503)])
async def test_stream_failure_is_logged(switch, client, first, caplog):
    switch.get(f"/agents/{AGENT}/events").mock(side_effect=[first, sse(msg(1))])
    with caplog.at_level("WARNING", logger="kraken.switch_client"):
        async with aclosing(client.events()) as stream:
            async for _ in stream:
                break
    assert any("event stream" in r.message for r in caplog.records)


async def test_heartbeat_failures_are_logged_and_beats_continue(switch, client, caplog):
    route = switch.post(f"/agents/{AGENT}/connection/beat").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(500, json={"detail": "x"}),
                     httpx.Response(409, json={"detail": "x"})])
    with caplog.at_level("WARNING", logger="kraken.switch_client"):
        task = asyncio.create_task(client.heartbeat_forever(interval=0.01))
        await asyncio.wait_for(client._reopen.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    msgs = [r.message for r in caplog.records]
    assert any("heartbeat failed" in m for m in msgs) and any("500" in m for m in msgs)
    assert len(route.calls) == 3
