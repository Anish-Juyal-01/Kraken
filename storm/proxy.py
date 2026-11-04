"""A small reverse proxy that puts network faults in front of any HTTP app."""

import asyncio
import random
import socket

import aiohttp
from aiohttp import web

_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "host",
        "content-encoding"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FaultProxy:
    """Forwards to `upstream`; `latency_ms` delays every request, `drop` (0..1) aborts that
    share of connections, `error` (0..1) answers that share with 503 without forwarding."""

    def __init__(self, upstream: str, latency_ms: int = 0, drop: float = 0.0,
                 error: float = 0.0, seed: int | None = 7) -> None:
        self.upstream = upstream.rstrip("/")
        self.latency_ms, self.drop, self.error = latency_ms, drop, error
        self._rand = random.Random(seed)
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        roll = self._rand.random()
        if roll < self.drop:
            if request.transport is not None:
                request.transport.close()  # the client sees a reset connection
            raise web.HTTPServiceUnavailable()
        if roll < self.drop + self.error:
            return web.Response(status=503, text="storm proxy: injected error")
        assert self._session is not None
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        async with self._session.request(request.method, self.upstream + request.path_qs,
                                         headers=headers, data=await request.read(),
                                         allow_redirects=False) as up:
            body = await up.read()
            out = {k: v for k, v in up.headers.items() if k.lower() not in _HOP}
            return web.Response(status=up.status, body=body, headers=out)

    async def __aenter__(self) -> "FaultProxy":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self.port = free_port()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()
