import asyncio
import json
import logging
import re
import time
from pathlib import Path

from pydantic import BaseModel

from kraken.conductor.commands import Command, from_reaction, parse, prompt
from kraken.conductor.runner import ActionRunner, merge_data
from kraken.conductor.state import STAGE_GATE, Action, Signal, effective, stage_room, step
from kraken.config import Config, ProjectConfig
from kraken.crew import lookup, render_roster
from kraken.hallmark.ledger import Ledger, Request
from kraken.hallmark.publish import publish
from kraken.init import detect as detect_project
from kraken.llm import LLM, SMART, LLMParseError
from kraken.models import Claim, Conflict, Event
from kraken.projects import render_project
from kraken.sonar.code_check import code_claims_for, index_code
from kraken.workspace import GitError, checkout, project_name
from kraken.sonar.detect import detect, judge, norm_value
from kraken.sonar.extract import extract
from kraken.sonar.normalize import normalize
from kraken.sonar.preflight import preflight
from kraken.switch_client import SwitchClient, SwitchError

log = logging.getLogger(__name__)

_TIMED_STAGES = ("SPARK", "CHARTER", "FORGE", "STORM", "DELIVER")
_MENTION = re.compile(r"^@kraken\b[:,]?\s*", re.IGNORECASE)
_RESOLVE_HINT = "reply 1️⃣, 2️⃣ or 🙅"
_QUOTE_MAX = 300
_CREW_KINDS = ("crew", "enlist", "retire")
_PROJECT_KINDS = ("connect", "projects", "use", "project_set")
_STAGE_NAMES = {"SPARK": "Spark (answers and brief)", "CHARTER": "Charter (picking the plan)",
                "FORGE": "Forge (merge OK)", "STORM": "Storm (defending)"}
_NAMED_PROJECT = re.compile(r"^(?:build\s+)?([\w.-]+)\s*:\s+(.+)$", re.IGNORECASE | re.DOTALL)
# Status lines Switch posts on an agent's behalf. They look like ordinary messages (an
# offline notice even starts with "@kraken"), so match the text: live, one became request K-4.
_SWITCH_NOTICE = re.compile(
    r"^(@\S+\s+)*(I'm currently offline\b.*connector isn't reporting in"
    r"|Starting a session to handle this\b)", re.IGNORECASE | re.DOTALL)


class BidPick(BaseModel):
    winner: str
    reason: str = ""

PICK_SYSTEM = (
    "You judge bids from coding agents for one brief. Pick the bid whose plan best meets "
    "the brief's acceptance criteria: correct, complete, smallest safe change, honest risks. "
    "Judge the content only; the order the bids are listed in means nothing. "
    'Reply with JSON only: {"winner": "<agent name>", "reason": "<one sentence>"}.'
)


def _render(c: Conflict) -> str:
    a, b = c.a, c.b
    if a.source == "code":
        source = f"{a.path}:{a.line}" if a.line is not None else str(a.path)
    else:
        source = f"#{a.room_id} (message {a.message_id})"
    return f"Brief says {b.subject}.{b.attribute} = {b.value}; {source} says {a.value}"


def _link(c: Claim) -> str:
    if c.source == "code":
        return f"{c.path}:{c.line}" if c.line is not None else str(c.path)
    return f"#{c.room_id} (message {c.message_id})"


class Dispatcher:
    def __init__(self, client: SwitchClient, llm: LLM, ledger: Ledger, cfg: Config,
                 code_index: list[Claim], self_id: str) -> None:
        self.client, self.llm, self.ledger, self.cfg = client, llm, ledger, cfg
        self.code_index, self.self_id = code_index, self_id
        self.room_ids: dict[str, str] = {}
        self._room_names: dict[str, str] = {}  # room id -> name, from list_rooms
        self.runner = ActionRunner(client, ledger, cfg, lambda: self.on_decision())
        self.crew = self.runner.crew
        self.projects = self.runner.projects
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()  # background work such as cloning a repo

    @property
    def _agent_names(self) -> set[str]:
        return {self.cfg.kraken_agent, *self.crew.all_names()}

    async def on_decision(self) -> None:
        """A decision was recorded or a publish was requested: refresh the Hallmark document."""
        room_id = self.room_ids.get(self.cfg.rooms.hallmark)
        if room_id is None:
            log.warning("no room id for %s; Hallmark not published", self.cfg.rooms.hallmark)
            return
        try:
            if not self._room_names:
                await self._load_room_names()
            await publish(self.client, self.ledger, room_id, self._room_names)
        except Exception:  # a publish failure must never strand a resolution
            log.exception("Hallmark publish failed")

    # ---- entry points -------------------------------------------------
    async def handle(self, ev: Event) -> None:
        async with self._lock:
            last = self.ledger.last_sequence()
            if ev.sequence is not None and last is not None and ev.sequence <= last:
                return
            try:
                if ev.type == "gap":
                    await self._recover(ev.room_id)
                elif ev.type == "message":
                    await self._message(ev)
            except Exception:
                log.exception("failed handling event %s in %s", ev.sequence, ev.room_id)
            if ev.sequence is not None:
                self.ledger.set_last_sequence(ev.sequence)

    async def handle_command(self, cmd: Command, room_id: str | None = None) -> bool:
        """Route a command to its request. Returns False when no request matches."""
        async with self._lock:
            return await self._command(cmd, room_id)

    async def handle_reaction(self, emoji: str, user: str, tag: int) -> bool:
        """A human emoji reaction on a [K-n] prompt (relayed by the Discord bot)."""
        cmd = from_reaction(emoji, user, tag)
        if cmd is None or user in self._agent_names:  # R15: agents never act via reactions
            return False
        async with self._lock:
            try:
                req = self.ledger.get_request(tag)
            except KeyError:
                return False
            try:
                await self.runner.say(req, f"{emoji} from {user} — received",
                                      stage_room(req.state))
            except Exception:
                log.exception("reaction receipt for K-%s failed", tag)
            return await self._command(cmd, None)

    def _human_wait(self, req: Request) -> tuple[str, float] | None:
        """(what, since) while a person has been asked for something and hasn't answered.
        The clock starts when the person is asked, never when the stage merely opens."""
        if req.state not in _STAGE_NAMES or req.state in req.data.get("handoff", []):
            return None
        d, gate = req.data, req.data.get("gate")
        if gate and gate == STAGE_GATE.get(req.state):  # approve the brief, pick, merge OK, accept
            return "gate", d.get("gate_since") or d.get("stage_started") or 0.0
        if (req.state == "SPARK" and gate is None and d.get("asked_at") is not None
                and not d.get("answers_handed")):
            return "answers", d["asked_at"]  # the planner asked the owner something
        if (req.state == "STORM" and req.storm_mode == "drill" and gate is None
                and d.get("defend_since") is not None and not d.get("defend_handed")):
            return "defend", d["defend_since"]  # Squall broke things: the people's turn to fix
        return None

    def _waiting_on_people(self, req: Request) -> bool:
        return self._human_wait(req) is not None

    def _wait_note(self, what: str) -> str:
        mins = max(1, round(self.cfg.human_wait_s / 60))
        if self.cfg.human_wait_s <= 0:
            return f"⏳ Your call: {what}."
        return f"⏳ Your call: {what}. Reply within {mins} min, or the agent takes over (🤖 hands it over now)."

    async def _auto_handoffs(self, now: float) -> None:
        """Human first, but nobody waits forever: after human_wait_s with no reply from a
        person, the agent takes over that one thing, and Kraken says so in the room."""
        wait = self.cfg.human_wait_s
        if wait <= 0:
            return
        who = f"Kraken (no reply from a person in {max(1, round(wait / 60))} min)"
        for req in self.ledger.open_requests():
            hw = self._human_wait(req)
            if hw is None:
                continue
            what, since = hw
            since = max(since, req.data.get("human_at") or 0.0)  # a person's reply restarts it
            if now - since <= wait:
                continue
            try:
                if what == "gate":
                    await self._handoff(req, Command(kind="handoff", tag=req.id, user=who))
                elif what == "answers":
                    merge_data(self.ledger, req.id, answers_handed=True,
                               handoff_by={**req.data.get("handoff_by", {}), "SPARK answers": who})
                    await self.runner.say(
                        req, f"@{self.crew.primary('planner')} [K-{req.id}] {who}: decide the open "
                        "answers yourself, list them under Assumptions in the brief, then write it. "
                        "The brief still goes to a person for approval.", "spark")
                else:  # defend
                    self.ledger.update_request(req.id, storm_mode="auto")
                    merge_data(self.ledger, req.id, defend_handed=True,
                               handoff_by={**req.data.get("handoff_by", {}), "STORM defending": who})
                    await self.runner.say(
                        req, f"@{self.crew.primary('defender')} [K-{req.id}] {who}: you defend now "
                        "(auto mode). Fix what the faults broke, commit, then post your verdict and "
                        f"mention @{self.crew.primary('coach')}. The result still goes to a person.",
                        "storm")
            except Exception:
                log.exception("auto handoff failed for K-%s", req.id)

    def _waited_on_agents(self, req: Request) -> tuple[str, list[str]] | None:
        """(stage room, agents Kraken is waiting on) while agents should be working."""
        if req.data.get("gate") or self._waiting_on_people(req):
            return None
        if req.state == "CHARTER":
            bid = {b["agent"] for b in req.bids}
            return "charter", [a for a in req.data.get("live_bidders", []) if a not in bid]
        if req.state == "FORGE" and req.winner:
            return "forge", [req.winner]
        if req.state == "STORM":
            return "storm", [n for n in (self.crew.primary(r) for r in ("attacker", "defender", "coach")) if n]
        if req.state == "SPARK":
            return "spark", [n for n in [self.crew.primary("planner")] if n]
        if req.state == "DELIVER" and req.data.get("delivering") and req.winner:
            return "forge", [req.winner]
        return None

    async def _nudge_quiet_agents(self, now: float) -> None:
        quiet = self.cfg.agent_quiet_s
        if quiet <= 0:
            return
        for req in self.ledger.open_requests():
            waited = self._waited_on_agents(req)
            if not waited or not waited[1] or waited[0] not in req.rooms:
                continue
            marks = [req.data[k] for k in ("stage_started", "active_at", "nudged_at")
                     if req.data.get(k) is not None]
            if not marks or now - max(marks) <= quiet:
                continue
            room, agents = waited
            merge_data(self.ledger, req.id, nudged_at=now)
            mentions = " ".join(f"@{a}" for a in agents)
            try:
                await self.runner.say(
                    req, f"{mentions} [K-{req.id}] it's been quiet here for "
                    f"{round((now - max(marks)) / 60)} min. Where are you? Carry on from where you "
                    "left off, and when you hand off, post it here and mention the next agent.", room)
            except Exception:
                log.exception("nudge for K-%s failed", req.id)

    async def _note_asks(self, rid: int, room_id: str, sender: str, body: str) -> None:
        """An agent just asked the people for something: start their clock and tell them."""
        req = self.ledger.get_request(rid)
        stage = next((s for s, r in req.rooms.items() if r == room_id), None)
        d, now = req.data, time.time()
        if req.state in d.get("handoff", []) or d.get("gate"):
            return
        lines = [ln.strip() for ln in body.splitlines()]
        if (stage == "spark" and req.state == "SPARK" and sender == self.crew.primary("planner")
                and "?" in body and "BRIEF READY" not in lines and not d.get("answers_handed")
                and (d.get("asked_at") is None or (d.get("human_at") or 0) > d["asked_at"])):
            merge_data(self.ledger, rid, asked_at=now)
            await self.runner.say(req, f"@{req.owner} [K-{req.id}] the planner is asking you "
                                       "something (above). " + self._wait_note("answer it here"),
                                  "spark")
        elif (stage == "storm" and req.state == "STORM" and req.storm_mode == "drill"
              and sender == self.crew.primary("attacker") and d.get("defend_since") is None
              and f"@{self.crew.primary('defender')}" in body):
            merge_data(self.ledger, rid, defend_since=now)
            people = " ".join(f"@{p}" for p in self.cfg.people)
            await self.runner.say(
                req, f"{people} [K-{req.id}] Squall has broken things (above): it's your turn to "
                f"defend. Fix them on kraken/K-{req.id}, then tell @{self.crew.primary('coach')} "
                "you're done. " + self._wait_note("defend"), "storm")

    async def tick(self, now: float) -> None:
        async with self._lock:
            await self._auto_handoffs(now)
            await self._nudge_quiet_agents(now)
            for req in self.ledger.open_requests():
                started = req.data.get("stage_started")
                if (req.state not in _TIMED_STAGES or started is None
                        or req.data.get("timed_out") == req.state
                        or now - started <= self.cfg.timeouts_s.get(req.state.lower(), 1 << 30)):
                    continue
                try:
                    merge_data(self.ledger, req.id, timed_out=req.state)
                    data = await self._bids_done_data(req) if req.state == "CHARTER" else {}
                    await self.apply(req.id, Signal(kind="timeout", data=data))
                except Exception:
                    log.exception("timeout handling failed for K-%s", req.id)

    # ---- gap recovery -------------------------------------------------
    async def _recover(self, room_id: str) -> None:
        ctx = await self.client.op("read_context", room_id=room_id)
        for thread in ctx.get("threads", []):
            for e in [thread.get("root"), *thread.get("replies", [])]:
                if not e or e.get("kind") != "message" or self._seen(e["id"]):
                    continue
                body = e.get("body") or ""
                payload = {"sender": e.get("sender"), "sender_name": e.get("sender_name"),
                           "body": body, "message_id": e["id"], "timestamp": e.get("timestamp", 0),
                           "addressed": bool(_MENTION.match(body))}
                await self._message(Event(type="message", room_id=room_id, sequence=None,
                                          payload=payload))

    def _seen(self, message_id: str) -> bool:
        return self.ledger.get_meta(f"seen:{message_id}") is not None

    # ---- messages -----------------------------------------------------
    async def _message(self, ev: Event) -> None:
        p = ev.payload
        body = p.get("body")
        if not isinstance(body, str):
            return
        if (p.get("sender") == self.self_id or p.get("sender_name") == self.cfg.kraken_agent
                or p.get("sender_kind") == "platform" or _SWITCH_NOTICE.match(body.strip())):
            return
        if p.get("message_id"):
            if self._seen(p["message_id"]):
                return
            self.ledger.set_meta(f"seen:{p['message_id']}", "1")
        name = p.get("sender_name") or p.get("sender") or ""
        known_agent = name in self._agent_names
        if (rid := self.ledger.request_for_room(ev.room_id)) is not None:
            merge_data(self.ledger, rid, active_at=time.time())  # someone is working here
            if not known_agent:
                merge_data(self.ledger, rid, human_at=time.time())  # a person: reset the wait
            else:
                await self._note_asks(rid, ev.room_id, name, body)
        cmd = None if known_agent else parse(body, user=name)
        if cmd is not None and cmd.kind in (*_CREW_KINDS, *_PROJECT_KINDS):
            if ev.room_id != self.room_ids.get(self.cfg.rooms.requests):
                cmd = None  # crew and project commands only work in the requests room
            elif cmd.kind in _PROJECT_KINDS:
                await self._project_command(cmd, ev.room_id)
                return
            else:
                await self._crew_command(cmd, ev.room_id)
                return
        if cmd is not None and await self._command(cmd, ev.room_id):
            return
        if await self._markers(ev, name, body):
            return
        if self.ledger.request_for_room(ev.room_id) is not None:
            return  # stage-room chatter is never Sonar input (R14)
        if (p.get("addressed") and cmd is None and not known_agent
                and ev.room_id == self.room_ids.get(self.cfg.rooms.requests)):
            await self._new_request(ev, name, body)
            return
        await self._sonar(ev)

    async def _new_request(self, ev: Event, name: str, body: str) -> None:
        text = _MENTION.sub("", body).strip() or body
        project = self.projects.get()
        m = _NAMED_PROJECT.match(text)
        if m and self.projects.get(m.group(1)) is not None:  # "build <project>: <what>"
            project, text = self.projects.get(m.group(1)), m.group(2).strip()
        if project is None:
            await self.client.send(ev.room_id, "No project is connected yet. Start with "
                                   "`@kraken connect https://github.com/<owner>/<repo>`.")
            return
        rid = self.ledger.new_request(owner=name, text=text, origin_room=ev.room_id,
                                      storm_mode=self.cfg.storm.default_mode)
        merge_data(self.ledger, rid, project=project.name)
        await self.runner.run(self.ledger.get_request(rid),
                              [Action(kind="open_stage", stage="spark")])

    # ---- project commands ----------------------------------------------
    async def _project_command(self, cmd: Command, room_id: str) -> None:
        if cmd.kind == "connect":
            # cloning can take minutes: do it outside the event lock
            task = asyncio.create_task(self._connect(cmd.arg or "", cmd.role, room_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if cmd.kind == "projects":
            default = self.projects.default_name()
            listed = self.projects.all()
            text = ("\n\n".join(render_project(p, p.name == default) for p in listed.values())
                    or "No projects yet. `@kraken connect <repo url>` adds one.")
        elif cmd.kind == "use":
            ok = self.projects.use(cmd.arg or "")
            text = f"Default project is now {cmd.arg}." if ok else f"No project named {cmd.arg}."
        else:  # project set <name> <field> <value>
            field, value = cmd.args
            try:
                p = self.projects.set_field(cmd.arg or "", field, value)
                text = f"Updated {p.name}.{field}.\n" + render_project(p)
            except KeyError:
                text = f"No project named {cmd.arg}."
            except ValueError as err:
                text = f"Can't set that: {err}"
        await self.client.send(room_id, text)

    async def _connect(self, url: str, name: str | None, room_id: str) -> None:
        name = name or project_name(url)
        try:
            await self.client.send(room_id, f"Cloning {url} as **{name}**…")
            dest = await checkout(url, Path(self.cfg.workspace) / name)
            project = detect_project(dest, name=name, repo=url)
            self.projects.put(project)
            self.index_project(project)
            default = self.projects.default_name() == name
            await self.client.send(
                room_id, f"Connected.\n{render_project(project, default)}\n\nFix anything wrong "
                f"with `@kraken project set {name} <field> <value>`; then ask "
                f"`@kraken build {name}: <what you need>`.")
        except GitError as err:
            await self.client.send(room_id, f"Couldn't connect {url}: {err}")
        except Exception as err:  # never let a bad repo take Kraken down
            log.exception("connect %s failed", url)
            await self.client.send(room_id, f"Couldn't connect {url}: {type(err).__name__}: {err}")

    def index_project(self, project: ProjectConfig) -> None:
        """(Re)index one project's code for Sonar, replacing its earlier claims."""
        root = Path(project.root).as_posix().rstrip("/") + "/"
        keep = [c for c in self.code_index if not (c.path or "").startswith(root)]
        self.code_index[:] = keep + index_code(project.index_paths())

    # ---- crew commands --------------------------------------------------
    async def _crew_command(self, cmd: Command, room_id: str) -> None:
        try:
            if cmd.kind == "enlist":
                text = await self._enlist(cmd.arg or "", cmd.role or "bidder")
            elif cmd.kind == "retire":
                name = cmd.arg or ""
                text = f"{name} retired." if self.crew.retire(name) else f"{name} isn't on the crew."
            else:
                roster = self.crew.roster()
                infos = await lookup(self.client, [n for names in roster.values() for n in names])
                text = render_roster(roster, infos)
        except SwitchError as err:
            text = f"could not reach Switch: {err.detail}"
        await self.client.send(room_id, text)

    async def _enlist(self, name: str, role: str) -> str:
        if name == self.cfg.kraken_agent:
            return "Kraken runs the crew; it can't join it."
        if not any(a.get("name") == name for a in await self.client.op("list_agents")):
            return f"No Switch agent named {name}."
        if self.crew.enlist(name, role):
            return f"{name} enlisted as {role}."
        return f"{name} is already a {role}."

    # ---- commands -----------------------------------------------------
    async def _command(self, cmd: Command, room_id: str | None) -> bool:
        rid = cmd.tag
        if rid is None and room_id is not None:
            rid = self.ledger.request_for_room(room_id)
            if rid is None and cmd.kind == "retry":
                # "reply retry" after a failed stage open is typed in the origin room
                mine = [r.id for r in self.ledger.open_requests() if r.origin_room == room_id]
                rid = mine[-1] if mine else None
        if rid is None:
            return False
        try:
            req = self.ledger.get_request(rid)
        except KeyError:
            return False
        if cmd.kind in ("handoff", "reclaim"):
            await self._handoff(req, cmd)
        elif req.state == "RESOLVING":
            await self._resolving(req, cmd)
        elif req.state == "PREFLIGHT" and cmd.kind == "retry":
            await self._preflight(req)
        else:
            await self.apply(rid, cmd)
        return True

    async def _resolving(self, req: Request, cmd: Command) -> None:
        a_id, b_id = req.data["a"], req.data["b"]
        if cmd.kind == "retry" and "sonar-resolution" not in req.rooms:
            await self.runner.run(req, [Action(kind="open_stage", stage="sonar-resolution")],
                                  self._resolution_extra(req.id, a_id, b_id))
        elif cmd.kind == "pick" and cmd.arg in ("1", "2"):
            chosen = self.ledger.get_claim(a_id if cmd.arg == "1" else b_id)
            self.ledger.add_decision(chosen, decided_by=cmd.user, supersedes=None)
            self.ledger.mark_pair(a_id, b_id, "resolved")
            await self._close_siblings(req, chosen)
            await self.on_decision()
            what = f"{chosen.subject}.{chosen.attribute}"
            brief = ("Make the system consistent with the decision: "
                     f"{what} = {chosen.value} (decided by {cmd.user}). "
                     f"Acceptance: (1) every place that uses {what} matches the decided value; "
                     "(2) a test asserts it.")
            await self.apply(req.id, Signal(kind="resolved", data={"brief": brief}))
        elif cmd.kind == "not_conflict":
            self.ledger.mark_pair(a_id, b_id, "dismissed")
            await self._close_siblings(req, self.ledger.get_claim(b_id))
            await self.apply(req.id, cmd)
        else:
            await self.runner.say(req, f"[K-{req.id}] {_RESOLVE_HINT}", "sonar-resolution")

    # ---- human first: hand a step to an agent, or take it back -------------
    async def _handoff(self, req: Request, cmd: Command) -> None:
        stage = req.state
        room = stage_room(stage)
        name = _STAGE_NAMES.get(stage)
        if stage == "STORM" and req.data.get("gate") == "storm_result":
            name = "Storm (accepting the result)"
        if name is None:
            why = ("The pull request always needs a person: reply approve or reject."
                   if stage == "DELIVER" else "That step needs a person.")
            await self.runner.say(req, f"[K-{req.id}] {why}", room)
            return
        handed = list(req.data.get("handoff", []))
        if cmd.kind == "reclaim":
            if stage not in handed:
                await self.runner.say(req, f"[K-{req.id}] {name} is already yours.", room)
                return
            handed.remove(stage)
            merge_data(self.ledger, req.id, handoff=handed)
            text = f"[K-{req.id}] {name}: back with {cmd.user}."
            if stage == "STORM":
                self.ledger.update_request(req.id, storm_mode="drill")
                text += (f" @{self.crew.primary('defender')} back to drill mode: hints only, "
                         "the people defend.")
            await self.runner.say(req, text, room)
            return
        if stage in handed:
            await self.runner.say(req, f"[K-{req.id}] {name} is already with the agent.", room)
            return
        merge_data(self.ledger, req.id, handoff=[*handed, stage],
                   handoff_by={**req.data.get("handoff_by", {}), stage: cmd.user})
        await self.runner.say(req, f"[K-{req.id}] {name}: handed to the agent by {cmd.user}. "
                                   "Reply 🙋 or `me` to take it back.", room)
        req = self.ledger.get_request(req.id)
        gate = req.data.get("gate")
        by = f"{cmd.user} (handed to agent)"
        if stage == "SPARK" and gate != "brief":
            await self.runner.say(
                req, f"@{self.crew.primary('planner')} [K-{req.id}] {cmd.user} handed this step "
                "to you: stop waiting for answers, decide them yourself, list them under "
                "Assumptions in the brief, then write it.", "spark")
        elif stage == "CHARTER" and gate == "pick":
            await self.apply(req.id, Signal(kind="bids_done", data=await self._bids_done_data(req)))
        elif stage == "STORM":
            self.ledger.update_request(req.id, storm_mode="auto")
            if gate != "storm_result":
                await self.runner.say(
                    req, f"@{self.crew.primary('defender')} [K-{req.id}] {cmd.user} handed "
                    "defending to you: you are in auto mode now. Fix what the faults broke, "
                    f"commit, then post your verdict and mention @{self.crew.primary('coach')}.",
                    "storm")
        if gate in ("brief", "merge", "storm_result") and STAGE_GATE.get(stage) == gate:
            await self.apply(req.id, Command(kind="approve", tag=req.id, user=by))

    async def _close_siblings(self, req: Request, c: Claim) -> None:
        """Settling one resolution settles every other open one on the same subject (R21)."""
        for other in self.ledger.open_requests():
            if other.id == req.id or other.state != "RESOLVING" or "b" not in other.data:
                continue
            b = self.ledger.get_claim(other.data["b"])
            if (b.subject, b.attribute) != (c.subject, c.attribute):
                continue
            self.ledger.update_request(other.id, state="CLOSED")
            await self.runner.say(other, f"[K-{other.id}] closed: settled in [K-{req.id}]",
                                  "sonar-resolution")

    # ---- the conductor loop -------------------------------------------
    async def apply(self, rid: int, inp: Signal | Command) -> None:
        """step() the request, persist the updates, run the actions, chain pre-flight."""
        req = self.ledger.get_request(rid)
        upd, actions = step(req, self.cfg, inp)
        if upd:
            self.ledger.update_request(rid, **upd)
        new = self.ledger.get_request(rid)
        if new.state != req.state:
            merge_data(self.ledger, rid, stage_started=time.time(), timed_out=None)
            new = self.ledger.get_request(rid)
        if new.data.get("gate") and new.data.get("gate") != req.data.get("gate"):
            merge_data(self.ledger, rid, gate_since=time.time())  # the human wait starts now
            new = self.ledger.get_request(rid)
        await self.runner.run(new, actions)
        if new.state == "PREFLIGHT" and req.state != "PREFLIGHT":
            await self._preflight(new)

    async def _preflight(self, req: Request) -> None:
        try:
            conflicts = await preflight(self.llm, self.ledger, self.code_index,
                                        req.brief or "", self.cfg)
        except LLMParseError:
            log.warning("pre-flight of K-%s unparsable; treating as clear", req.id)
            conflicts = []
        except Exception as err:
            log.exception("pre-flight of K-%s failed", req.id)
            merge_data(self.ledger, req.id, gate="preflight")
            await self.runner.say(
                req, f"[K-{req.id}] preflight failed: {err} — reply retry or approve")
            return
        if conflicts:
            sig = Signal(kind="preflight_conflicts",
                         data={"conflicts": [_render(c) for c in conflicts]})
        else:
            sig = Signal(kind="preflight_clear")
        await self.apply(req.id, sig)

    # ---- stage markers ------------------------------------------------
    async def _markers(self, ev: Event, sender: str, body: str) -> bool:
        rid = self.ledger.request_for_room(ev.room_id)
        if rid is None:
            return False
        req = self.ledger.get_request(rid)
        stage = next((s for s, r in req.rooms.items() if r == ev.room_id), None)
        lines = [ln.strip() for ln in body.splitlines()]
        live = req.data.get("live_bidders") or [b["agent"] for b in req.bids]
        if stage == "spark" and sender == self.crew.primary("planner") and "BRIEF READY" in lines:
            await self._brief_ready(req)
        elif (stage == "charter" and req.state == "CHARTER" and sender in live
              and any(ln.startswith("BID:") for ln in lines)):
            if any(b["agent"] == sender for b in req.bids):
                return True
            await self.apply(rid, Signal(kind="bid", data={"agent": sender, "text": body}))
            req = self.ledger.get_request(rid)
            if len(req.bids) == len(req.data.get("live_bidders", [])):
                await self.apply(rid, Signal(kind="bids_done",
                                             data=await self._bids_done_data(req)))
        elif (stage == "forge" and sender in live and sender != req.winner
              and "ACCEPTED" in lines):
            await self.apply(rid, Signal(kind="accepted"))
        elif stage == "forge" and req.state == "DELIVER" and sender == req.winner and (
                pr := next((ln for ln in lines if ln.startswith(("PR:", "PR FAILED:"))), None)):
            if pr.startswith("PR FAILED:"):
                reason = pr.removeprefix("PR FAILED:").strip()
                await self.apply(rid, Signal(kind="deliver_failed", data={"reason": reason}))
            else:
                url = pr.removeprefix("PR:").strip().strip("<>")
                await self.apply(rid, Signal(kind="delivered", data={"url": url}))
        elif stage == "storm" and sender == self.crew.primary("coach") and (
                "SURVIVED" in lines or "FAILED" in lines):
            if "SURVIVED" in lines:
                await self.apply(rid, Signal(kind="survived"))
            else:
                await self.apply(rid, Signal(kind="failed", data={"findings": body}))
        else:
            return False
        return True

    async def _brief_ready(self, req: Request) -> None:
        try:
            info = await self.client.op("connect_to_room", room_id=req.rooms["spark"])
            doc = next((d for d in info.get("documents", []) if d.get("name") == "brief"), None)
            if doc is None:
                raise LookupError("no brief document")
            loaded = await self.client.op("load_internal_documents", ids=[doc["id"]])
            brief = loaded[0]["content"]
        except (LookupError, SwitchError):
            await self.runner.say(req, f"[K-{req.id}] no `brief` document found", "spark")
            return
        await self.apply(req.id, Signal(kind="brief_posted", data={"brief": brief}))

    async def _bids_done_data(self, req: Request) -> dict:
        if effective(self.cfg, req).autonomy["pick"] != "auto" or not req.bids:
            return {}
        bids = "\n\n".join(f"{i}. {b['agent']}:\n{b.get('text', '')}"
                           for i, b in enumerate(req.bids, 1))
        try:
            pick = await self.llm.json(SMART, PICK_SYSTEM,
                                       f"Brief:\n{req.brief}\n\nBids:\n{bids}", BidPick)
        except LLMParseError:
            log.warning("K-%s: bid pick unparsable; falling back to the first bid", req.id)
            return {}
        log.info("K-%s: judged winner %s (%s)", req.id, pick.winner, pick.reason)
        return {"judged_winner": pick.winner, "pick_reason": pick.reason}

    # ---- Sonar ----------------------------------------------------------
    async def _sonar(self, ev: Event) -> None:
        try:
            for raw in await extract(self.llm, ev, self.self_id, self.cfg.sonar.min_words):
                claim = await normalize(self.llm, self.ledger, raw)
                cid = self.ledger.add_claim(claim)
                await self._store_source(cid, ev)
                for conflict in await detect(self.llm, self.ledger, cid, claim,
                                             self.cfg.sonar.threshold, where=self._where):
                    await self._open_resolution(conflict, cid)
                await self._code_conflicts(cid, claim)
        except LLMParseError:
            log.warning("unparsable LLM reply for message %s; skipped",
                        ev.payload.get("message_id"))

    async def _load_room_names(self) -> None:
        try:
            rooms = await self.client.op("list_rooms")
        except SwitchError as err:
            log.warning("list_rooms failed: %s", err)
            return
        for r in rooms if isinstance(rooms, list) else []:
            if r.get("room_id") and r.get("name"):
                self._room_names[r["room_id"]] = r["name"]

    async def _room_name(self, room_id: str) -> str:
        if room_id not in self._room_names:
            await self._load_room_names()
            self._room_names.setdefault(room_id, room_id)  # unknown: fall back to the id
        return self._room_names[room_id]

    async def _store_source(self, claim_id: int, ev: Event) -> None:
        """Keep the message a chat claim came from, for the resolution room (R22)."""
        p = ev.payload
        src = {"sender_name": p.get("sender_name") or p.get("sender") or "",
               "body": p.get("body") or "", "room_name": await self._room_name(ev.room_id)}
        self.ledger.set_meta(f"claimsrc:{claim_id}", json.dumps(src))

    def _where(self, claim_id: int, c: Claim) -> str | None:
        """Readable origin of a chat claim for the judge, with the message it came from.

        Live, raw ids alone left the judge hedging on scope ("unless the scopes differ"),
        scoring a real UUID-vs-integer clash 0.6-0.72; the quote ("everywhere", "every
        response model") settles scope and lifts it to 0.8, while compatible pairs stay
        compatible."""
        raw = self.ledger.get_meta(f"claimsrc:{claim_id}")
        if raw is None:
            return None
        src = json.loads(raw)
        quote = src["body"][:_QUOTE_MAX].replace('"', "'")
        return f'{src["sender_name"] or c.speaker} in #{src["room_name"]}, who wrote: "{quote}"'

    def _side(self, claim_id: int, c: Claim) -> tuple[str, str, str]:
        """(quote, link, user) for one side of a resolution."""
        if c.source == "code":
            return c.value, _link(c), c.speaker
        raw = self.ledger.get_meta(f"claimsrc:{claim_id}")
        if raw is None:
            return f"{c.value} ({c.speaker})", _link(c), c.speaker
        src = json.loads(raw)
        return src["body"][:_QUOTE_MAX], f"#{src['room_name']}", src["sender_name"]

    async def _code_conflicts(self, chat_id: int, chat: Claim) -> None:
        """Judge a stored chat claim against the code index (R20)."""
        for code in code_claims_for(self.code_index, chat.subject, chat.attribute):
            if norm_value(code.value) == norm_value(chat.value):
                continue
            key = f"codeclaim:{code.path}:{code.line}:{code.subject}:{code.attribute}"
            stored = self.ledger.get_meta(key)
            code_id = int(stored) if stored is not None else None
            if code_id is not None and self.ledger.get_claim(code_id) != code:
                code_id = None  # the code changed since it was stored
            if code_id is not None and self.ledger.pair_seen(code_id, chat_id):
                continue
            conflict = await judge(self.llm, code, chat, where_b=self._where(chat_id, chat))
            if conflict.verdict != "conflict" or conflict.confidence < self.cfg.sonar.threshold:
                continue
            if code_id is None:
                code_id = self.ledger.add_claim(code)
                self.ledger.set_meta(key, str(code_id))
            self.ledger.mark_pair(code_id, chat_id, "raised")
            await self._open_resolution(conflict, chat_id)

    async def _open_resolution(self, conflict: Conflict, b_id: int) -> None:
        a, b = conflict.a, conflict.b
        a_id = next(i for i, c in self.ledger.claims_for(a.subject, a.attribute)
                    if c == a and i != b_id and self.ledger.pair_seen(i, b_id))
        rid = self.ledger.new_request(owner=b.speaker,
                                      text=f"conflict on {b.subject}.{b.attribute}",
                                      origin_room=b.room_id, state="RESOLVING",
                                      storm_mode=self.cfg.storm.default_mode)
        self.ledger.update_request(rid, data={"a": a_id, "b": b_id})
        await self.runner.run(self.ledger.get_request(rid),
                              [Action(kind="open_stage", stage="sonar-resolution")],
                              self._resolution_extra(rid, a_id, b_id))

    def _resolution_extra(self, rid: int, a_id: int, b_id: int) -> dict:
        a, b = self.ledger.get_claim(a_id), self.ledger.get_claim(b_id)
        quote_a, link_a, user_a = self._side(a_id, a)
        quote_b, link_b, user_b = self._side(b_id, b)
        return {
            "subject": f"{b.subject}.{b.attribute}",
            "quote_a": quote_a, "link_a": link_a, "user_a": user_a,
            "quote_b": quote_b, "link_b": link_b, "user_b": user_b,
            "prompt_text": prompt(rid, "Which one is true?",
                                  ["1️⃣ first", "2️⃣ second", "🙅 not a conflict"]),
        }
