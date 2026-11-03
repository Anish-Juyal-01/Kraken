import asyncio
import logging
import os
import time
import uuid

from aiohttp import web

from kraken.config import load_config
from kraken.dispatch import Dispatcher
from kraken.hallmark.ledger import Ledger
from kraken.llm import ClaudeLLM
from kraken.relay.server import make_app
from kraken.sonar.code_check import index_code
from kraken.switch_client import SwitchClient

log = logging.getLogger("kraken")
TICK_SECONDS = 60


def load_env(path: str = ".env") -> None:
    """Minimal KEY=VALUE loader; blanks and # comments ignored; real environment wins."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def connection_id(ledger: Ledger) -> str:
    cid = ledger.get_meta("connection_id")
    if cid is None:
        cid = str(uuid.uuid4())
        ledger.set_meta("connection_id", cid)
    return cid


async def ticker(dispatcher: Dispatcher, interval: float = TICK_SECONDS) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await dispatcher.tick(time.time())
        except Exception:
            log.exception("timeout tick failed")


async def consume(client: SwitchClient, dispatcher: Dispatcher) -> None:
    async for ev in client.events():
        await dispatcher.handle(ev)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env()
    cfg = load_config()
    ledger = Ledger(cfg.db_path)
    client = SwitchClient(os.environ["SWITCH_URL"], os.environ["SWITCH_AGENT_ID"],
                          os.environ["SWITCH_API_KEY"], connection_id(ledger))
    code_index = index_code(cfg.sonar.code_paths)
    log.info("indexed %d code claims", len(code_index))
    # Switch gives the agent's own participant id as its agent id.
    dispatcher = Dispatcher(client, ClaudeLLM(), ledger, cfg, code_index, self_id=client.agent_id)
    wanted = {cfg.rooms.requests, cfg.rooms.hallmark}
    for room in await client.op("list_rooms"):
        if room["name"] in wanted:
            dispatcher.room_ids[room["name"]] = room["room_id"]
    missing = wanted - set(dispatcher.room_ids)
    if missing:
        log.warning("rooms not found for this agent: %s", ", ".join(sorted(missing)))
    runner = None
    secret = os.environ.get("KRAKEN_RELAY_SECRET")
    if secret:
        runner = web.AppRunner(make_app(dispatcher.handle_reaction, secret))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", cfg.relay_port).start()
        log.info("reaction relay listening on 127.0.0.1:%d", cfg.relay_port)
    else:
        log.info("KRAKEN_RELAY_SECRET not set; reaction relay disabled")
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(client.heartbeat_forever())
            tg.create_task(ticker(dispatcher))
            tg.create_task(consume(client, dispatcher))
    finally:
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
