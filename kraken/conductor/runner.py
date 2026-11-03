import logging
import time
from collections.abc import Awaitable, Callable

from kraken.conductor.stages import StageError, open_stage, stage_inputs
from kraken.conductor.state import Action, stage_room
from kraken.config import Config
from kraken.hallmark.ledger import Ledger, Request
from kraken.switch_client import SwitchClient, SwitchError

log = logging.getLogger(__name__)

_LIVE_STATES = ("live", "open")


def merge_data(ledger: Ledger, rid: int, **kv) -> None:
    """Merge keys into the request's data blob; a None value removes the key."""
    data = dict(ledger.get_request(rid).data)
    for k, v in kv.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    ledger.update_request(rid, data=data)


class ActionRunner:
    """Executes the conductor's Actions against Switch."""

    def __init__(self, client: SwitchClient, ledger: Ledger, cfg: Config,
                 publish: Callable[[], Awaitable[None]]) -> None:
        self.client = client
        self.ledger = ledger
        self.cfg = cfg
        self._publish = publish

    async def live_bidders(self) -> tuple[list[str], list[str]]:
        """(live, missing) subsets of cfg.bidders. An agent is live when
        get_agent_detail reports a session in state live/open (list_agents has no
        liveness field)."""
        try:
            by_name = {a["name"]: a["id"] for a in await self.client.op("list_agents")}
            live: list[str] = []
            for name in self.cfg.bidders:
                if name not in by_name:
                    continue
                detail = await self.client.op("get_agent_detail", agent_id=by_name[name])
                if any(s.get("state") in _LIVE_STATES for s in detail.get("sessions", [])):
                    live.append(name)
        except SwitchError as err:
            raise StageError("charter", f"could not list agents: {err.detail}") from err
        return live, [b for b in self.cfg.bidders if b not in live]

    async def say(self, req: Request, text: str, stage: str | None = None) -> None:
        """Post to the stage room (when bound) or else the origin room."""
        room = (req.rooms.get(stage) if stage else None) or req.origin_room
        await self.client.send(room, text)

    async def run(self, req: Request, actions: list[Action], extra: dict | None = None) -> None:
        for a in actions:
            try:
                await self._one(req, a, extra or {})
            except StageError as err:
                how = f"[K-{req.id}] retry" if err.stage == "sonar-resolution" else "retry"
                await self.client.send(
                    req.origin_room, f"[K-{req.id}] {err.stage} failed: {err.detail} — reply {how}")
                return

    async def _one(self, req: Request, a: Action, extra: dict) -> None:
        if a.kind == "open_stage":
            await self._open(req, a.stage or stage_room(req.state), extra)
        elif a.kind == "post":
            stage = a.stage or stage_room(req.state)
            room = req.origin_room if a.room == "origin" else req.rooms.get(stage, req.origin_room)
            await self.client.send(room, a.text or "")
        elif a.kind == "ping_owner":
            await self.client.send(req.origin_room, f"@{req.owner} {a.text or ''}")
        elif a.kind == "publish_hallmark":
            await self._publish()
        elif a.kind == "close":
            await self.client.send(req.origin_room, a.text or "")

    async def _open(self, req: Request, stage: str, extra: dict) -> None:
        extra = dict(extra)
        invitees: list[str] = []
        if stage == "charter":
            live, missing = await self.live_bidders()
            if missing:
                await self.client.send(
                    req.origin_room,
                    f"[K-{req.id}] bidding with {', '.join(live) or 'none'}; "
                    f"offline: {', '.join(missing)}")
            if not live:
                await self.client.send(
                    req.origin_room, f"@{req.owner} [K-{req.id}] needs you: no bidders online")
                return
            merge_data(self.ledger, req.id, live_bidders=live)
            extra["bidders"], invitees = live, live[1:]
        elif stage == "forge":
            pool = req.data.get("live_bidders") or [b["agent"] for b in req.bids]
            reviewers = [x for x in pool if x != req.winner]
            extra["reviewers"], invitees = reviewers, reviewers[1:]
        room_id = await open_stage(self.client, stage, stage_inputs(stage, req, self.cfg, **extra))
        self.ledger.bind_room(req.id, stage, room_id)
        merge_data(self.ledger, req.id, stage_started=time.time(), timed_out=None)
        for name in invitees:
            try:
                await self.client.op("invite_agent_to_room", room_id=room_id, agent_name=name)
            except SwitchError as err:
                log.warning("could not invite %s to %s: %s", name, room_id, err)
                await self.client.send(
                    req.origin_room, f"[K-{req.id}] could not invite {name}: {err.detail}")
