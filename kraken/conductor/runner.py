import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from kraken.conductor.stages import StageError, open_stage, stage_inputs
from kraken.conductor.state import Action, stage_room
from kraken.config import Config
from kraken.crew import Crew, lookup, pick_bidders
from kraken.hallmark.ledger import Ledger, Request
from kraken.projects import Projects
from kraken.switch_client import SwitchClient, SwitchError

log = logging.getLogger(__name__)
INVITE_TIMEOUT_S = 60  # the local Switch server is slow to add members

# An invite adds a member without waking it, and the kickoff predates membership:
# invited agents get a mention so they start work.
_WAKE = {
    "charter": "post your BID as described in the room instructions",
    "forge": "review against the brief; post ACCEPTED on its own line when every criterion is met",
}


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
        self.crew = Crew(ledger)
        self.projects = Projects(ledger)

    async def auto_join(self) -> list[str]:
        """Agents added in Switch join the crew on their own: any coding agent (Claude Code,
        Codex, OpenCode, Antigravity, Cursor, ...) that isn't on the crew yet becomes a
        bidder. No Kraken change or chat command per agent. Returns the newcomers."""
        if not self.cfg.charter.auto_join:
            return []
        try:
            agents = await self.client.op("list_agents")
        except SwitchError as err:
            log.warning("auto-join: could not list agents: %s", err)
            return []
        known = self.crew.all_names() | self.crew.retired() | {self.cfg.kraken_agent}
        joined = []
        for a in agents if isinstance(agents, list) else []:
            name, kind = a.get("name"), (a.get("known_agent_type") or "").lower()
            if name and kind and name not in known and name not in self.cfg.charter.never_join:
                self.crew.enlist(name, "bidder")
                joined.append(name)
        return joined

    async def live_bidders(self) -> tuple[list[str], list[str]]:
        """(live, missing) from the crew's bidders. An agent is live when Switch reports
        a session for it in state live/open. When more are live than charter.max_bidders
        allows, pick_bidders seats a vendor-diverse subset, rotating each Charter."""
        members = self.crew.members("bidder")
        try:
            infos = await lookup(self.client, members)
        except SwitchError as err:
            raise StageError("charter", f"could not list agents: {err.detail}") from err
        online = [infos[n] for n in members if n in infos and infos[n].online]
        missing = [n for n in members if n not in infos or not infos[n].online]
        live = [i.name for i in online]
        if online:
            rotation = self.crew.next_rotation()
            if len(online) > self.cfg.charter.max_bidders:
                live = pick_bidders([(i.name, i.vendor) for i in online],
                                    self.cfg.charter.max_bidders, rotation)
        return live, missing

    def people(self, req: Request) -> list[str]:
        """Who belongs in this request's rooms: the team (`people`) plus whoever asked."""
        agents = self.crew.all_names() | {self.cfg.kraken_agent}
        return [p for p in dict.fromkeys([*self.cfg.people, req.owner]) if p and p not in agents]

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

    async def _connect_parent(self, req: Request) -> None:
        """create_room_from_yaml files the new room under the connected room's run, so
        connect to the request's most recently bound stage room (else its origin room)."""
        rooms = list(self.ledger.get_request(req.id).rooms.values())
        parent = rooms[-1] if rooms else req.origin_room
        try:
            await self.client.op("connect_to_room", room_id=parent)
        except SwitchError as err:
            log.warning("could not connect to %s before opening a stage: %s", parent, err)

    async def _open(self, req: Request, stage: str, extra: dict) -> None:
        extra = dict(extra)
        invitees: list[str] = []
        if stage == "charter":
            joined = await self.auto_join()
            if joined:
                await self.client.send(
                    req.origin_room, f"[K-{req.id}] new on the crew (found in Switch): "
                    f"{', '.join(joined)} — they bid from now on. `@kraken retire <name>` removes one.")
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
        await self._connect_parent(req)
        project = self.projects.get(req.data.get("project"))
        inputs = stage_inputs(stage, req, self.cfg, self.crew, project=project, **extra)
        people = self.people(req)
        try:  # a request can come from an agent account too: rooms list people only
            listed = await self.client.op("list_agents")
            switch_agents = {a.get("name") for a in listed if isinstance(a, dict)} \
                if isinstance(listed, list) else set()
            people = [p for p in people if p not in switch_agents]
        except SwitchError as err:
            log.warning("could not list agents to filter room people: %s", err)
        room_id = await open_stage(self.client, stage, inputs, people, self.cfg.bridge)
        self.ledger.bind_room(req.id, stage, room_id)
        merge_data(self.ledger, req.id, stage_started=time.time(), timed_out=None)
        invited: list[str] = []
        for name in invitees:
            try:
                await self.client.op("invite_agent_to_room", room_id=room_id, agent_name=name,
                                     _timeout=INVITE_TIMEOUT_S)
                invited.append(name)
            except (SwitchError, httpx.HTTPError) as err:
                detail = err.detail if isinstance(err, SwitchError) else f"{type(err).__name__}"
                log.warning("could not invite %s to %s: %s", name, room_id, err)
                await self.client.send(
                    req.origin_room, f"[K-{req.id}] could not invite {name}: {detail}")
        if invited:
            mentions = " ".join(f"@{n}" for n in invited)
            try:
                await self.client.send(room_id, f"{mentions} [K-{req.id}] {_WAKE[stage]}")
            except SwitchError as err:
                log.warning("could not wake %s in %s: %s", mentions, room_id, err)
