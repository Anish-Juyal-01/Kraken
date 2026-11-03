import asyncio
import os
from datetime import date
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

DEFAULT_LIMITS = Path(__file__).resolve().parent.parent / "config" / "limits.yaml"


def _free_tier(path: Path) -> int:
    """Read on every request so config changes take effect live."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))["limits"]["free_tier"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"free_tier must be an integer, got {value!r}")
    return value


def create_app(limits_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Shopfront")
    limits = Path(limits_path) if limits_path else DEFAULT_LIMITS
    chaos_enabled = os.environ.get("SHOPFRONT_CHAOS") == "1"
    app.state.chaos = {}
    counters: dict[tuple[str, str], int] = {}

    def faulty(name: str, target: str | None = None) -> bool:
        spec = app.state.chaos.get(name)
        return spec is not None and (target is None or spec["target"] == target)

    @app.middleware("http")
    async def chaos_middleware(request: Request, call_next):
        if request.url.path.startswith("/__chaos"):
            return await call_next(request)
        if faulty("kill", "api"):
            return JSONResponse({"error": "service killed"}, status_code=503)
        if faulty("latency"):
            await asyncio.sleep(app.state.chaos["latency"]["params"].get("ms", 2000) / 1000)
        return await call_next(request)

    def get_user(request: Request, user_id: int, version: str):
        client = request.client.host if request.client else "unknown"
        key = (client, date.today().isoformat())
        counters[key] = counters.get(key, 0) + 1
        if counters[key] > _free_tier(limits):
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        if version == "v2" and faulty("bad_payload"):
            return Response('{"user_id": 1, "na', status_code=200, media_type="application/json")
        return {"user_id": user_id, "name": f"user-{user_id}"}

    @app.get("/v1/users/{user_id}")
    def users_v1(request: Request, user_id: int):
        return get_user(request, user_id, "v1")

    @app.get("/v2/users/{user_id}")
    def users_v2(request: Request, user_id: int):
        return get_user(request, user_id, "v2")

    @app.post("/auth/reset")
    async def auth_reset():
        unavailable = JSONResponse({"error": "email service unavailable"}, status_code=502)
        if faulty("kill", "email"):
            return unavailable
        url = os.environ.get("EMAIL_URL", "http://127.0.0.1:8081/send")
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                resp = await http.post(url, json={"template": "password_reset"})
                resp.raise_for_status()
        except httpx.HTTPError:
            return unavailable
        return {"status": "sent"}

    if chaos_enabled:
        @app.post("/__chaos")
        async def chaos_set(body: dict):
            app.state.chaos[body["fault"]] = {"target": body.get("target", "api"),
                                              "params": body.get("params") or {}}
            return {"active": sorted(app.state.chaos)}

        @app.delete("/__chaos")
        async def chaos_clear():
            app.state.chaos.clear()
            return {"active": []}

    return app


app = create_app()
