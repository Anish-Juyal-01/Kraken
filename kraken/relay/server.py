import hmac
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from kraken.conductor.commands import from_reaction

log = logging.getLogger(__name__)

OnReaction = Callable[[str, str, int], Awaitable[bool]]
_HANDLER = web.AppKey("on_reaction", object)
_SECRET = web.AppKey("secret", str)


def _text(v: object) -> bool:
    return isinstance(v, str) and bool(v)


async def _reaction(request: web.Request) -> web.Response:
    given = request.headers.get("X-Kraken-Secret", "")
    if not hmac.compare_digest(given.encode(), request.app[_SECRET].encode()):
        return web.Response(status=401)
    try:
        body = await request.json()
    except ValueError:
        return web.Response(status=400)
    if (not isinstance(body, dict) or type(body.get("tag")) is not int
            or not _text(body.get("emoji")) or not _text(body.get("user"))):
        return web.Response(status=400)
    emoji, user, tag = body["emoji"], body["user"], body["tag"]
    if from_reaction(emoji, user, tag) is None:
        return web.Response(status=204)
    handled = await request.app[_HANDLER](emoji, user, tag)
    return web.json_response({"handled": handled})


def make_app(on_reaction: OnReaction, secret: str) -> web.Application:
    app = web.Application()
    app[_HANDLER] = on_reaction
    app[_SECRET] = secret
    app.router.add_post("/relay/reaction", _reaction)
    return app
