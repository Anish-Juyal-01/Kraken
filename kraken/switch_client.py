import asyncio
import json
from contextlib import aclosing
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

import httpx

from kraken.models import Event

CONTROL_KINDS = {"connection_state", "gap", "evicted", "subscription_changed", "room_released"}
REQUEST_TIMEOUT = 10.0


class SwitchError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"Switch error {status}: {detail}")
        self.status = status
        self.detail = detail


class _FrameParser:
    """Accumulates SSE lines; feed() returns (id, event, data) when a frame completes."""

    def __init__(self) -> None:
        self._id: str | None = None
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> tuple[str | None, str, dict] | None:
        line = line.rstrip("\r\n")
        if line == "":
            frame = None
            if self._data:
                data = json.loads("\n".join(self._data))
                frame = (self._id, self._event or "message", data)
            self._id, self._event, self._data = None, None, []
            return frame
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            self._id = value
        elif field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str | None, str, dict]]:
    parser = _FrameParser()
    for line in lines:
        frame = parser.feed(line)
        if frame is not None:
            yield frame


class SwitchClient:
    def __init__(
        self,
        base_url: str,
        agent_id: str,
        api_key: str,
        connection_id: str,
        http: httpx.AsyncClient | None = None,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.connection_id = connection_id
        self.http = http or httpx.AsyncClient()
        self.reconnect_delay = reconnect_delay
        self.cursor: int | None = None
        self.rooms: list[str] = []
        self._reopen = asyncio.Event()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/agents/{self.agent_id}/{path}"

    async def events(self) -> AsyncIterator[Event]:
        while True:
            self._reopen.clear()
            params = {"connection_id": self.connection_id, "scope": "all", "filter": "all"}
            headers = {**self._auth, "Accept": "text/event-stream"}
            if self.cursor is None:
                params["start_from"] = "head"
            else:
                headers["Last-Event-ID"] = str(self.cursor)
            try:
                async with self.http.stream(
                    "GET", self._url("events"), params=params, headers=headers, timeout=None
                ) as resp:
                    if resp.status_code in (401, 403):
                        raise SwitchError(resp.status_code, (await resp.aread()).decode(errors="replace"))
                    if resp.status_code == 200:
                        async with aclosing(self._read_stream(resp)) as stream:
                            async for event in stream:
                                yield event
            except httpx.HTTPError:
                pass
            await asyncio.sleep(self.reconnect_delay)

    async def _read_stream(self, resp: httpx.Response) -> AsyncIterator[Event]:
        parser = _FrameParser()
        lines = resp.aiter_lines().__aiter__()
        reopen_wait = asyncio.ensure_future(self._reopen.wait())
        next_line: asyncio.Future | None = None
        try:
            while True:
                next_line = asyncio.ensure_future(lines.__anext__())
                await asyncio.wait({next_line, reopen_wait}, return_when=asyncio.FIRST_COMPLETED)
                if not next_line.done():
                    return  # heartbeat asked us to reopen
                try:
                    line = next_line.result()
                except StopAsyncIteration:
                    return
                frame = parser.feed(line)
                if frame is None:
                    continue
                id_, kind, data = frame
                if kind == "evicted":
                    return
                event = self._to_event(id_, kind, data)
                if event is not None:
                    yield event
        finally:
            pending = [f for f in (reopen_wait, next_line) if f is not None and not f.done()]
            for fut in pending:
                fut.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _to_event(self, id_: str | None, kind: str, data: dict) -> Event | None:
        if kind == "connection_state":
            self.rooms = list(data.get("rooms", []))
            cursor = data.get("cursor")
            if self.cursor is None and isinstance(cursor, int) and not isinstance(cursor, bool):
                self.cursor = cursor
            return None
        if kind == "gap":
            return Event(type="gap", room_id=data.get("room_id", ""), sequence=None, payload=data)
        if kind in CONTROL_KINDS:
            return None
        sequence = int(id_) if id_ is not None else data.get("sequence")
        if sequence is not None:
            self.cursor = sequence
        return Event(
            type=data.get("type", kind),
            room_id=data.get("room_id", ""),
            sequence=sequence,
            payload=data.get("payload", {}),
        )

    async def heartbeat_forever(self, interval: float = 2.0) -> None:
        while True:
            try:
                resp = await self.http.post(
                    self._url("connection/beat"),
                    json={"connection_id": self.connection_id, "cursor": self.cursor},
                    headers=self._auth,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code in (404, 409):
                    self._reopen.set()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(interval)

    async def op(self, name: str, **args: Any) -> Any:
        resp = await self.http.post(
            self._url(f"ops/{name}"),
            json=args,
            headers={**self._auth, "X-Switch-Connection-Id": self.connection_id},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SwitchError(resp.status_code, _detail(resp))
        return resp.json()["result"]

    async def send(self, room_id: str, content: str) -> str:
        resp = await self.http.post(
            self._url("message"),
            json={"room_id": room_id, "content": content},
            headers=self._auth,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SwitchError(resp.status_code, _detail(resp))
        return resp.json()["event_id"]


def _detail(resp: httpx.Response) -> Any:
    try:
        return resp.json().get("detail", resp.text)
    except (ValueError, AttributeError):
        return resp.text
