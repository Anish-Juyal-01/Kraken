import json
from dataclasses import dataclass
from typing import Literal, get_args

from kraken.config import Config
from kraken.hallmark.ledger import Ledger
from kraken.switch_client import SwitchClient

Role = Literal["bidder", "planner", "attacker", "defender", "coach"]
ROLES: tuple[Role, ...] = get_args(Role)

_META_KEY = "crew"
_ROTATION_KEY = "crew_rotation"
# An agent counts as online when get_agent_detail reports a session in one of these
# states (list_agents has no liveness field).
_LIVE_STATES = ("live", "open")


class Crew:
    """The agent roster, one role per agent, kept in the ledger's meta table.
    kraken.yaml only seeds it; chat commands manage it from then on."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def _load(self) -> dict[str, list[str]]:
        raw = self.ledger.get_meta(_META_KEY)
        data = json.loads(raw) if raw else {}
        return {role: list(data.get(role, [])) for role in ROLES}

    def _save(self, roster: dict[str, list[str]]) -> None:
        self.ledger.set_meta(_META_KEY, json.dumps(roster))

    def seed(self, cfg: Config) -> None:
        """Write the roster from config, once: a no-op when a roster already exists."""
        if self.ledger.get_meta(_META_KEY) is not None:
            return
        a = cfg.agents
        wanted = {"bidder": list(cfg.bidders), "planner": [a.planner], "attacker": [a.attacker],
                  "defender": [a.defender], "coach": [a.coach]}
        seen: set[str] = set()
        roster: dict[str, list[str]] = {}
        for role in ROLES:  # an agent holds one role: the first one listed wins
            roster[role] = [n for n in dict.fromkeys(wanted[role]) if n not in seen]
            seen.update(roster[role])
        self._save(roster)

    def roster(self) -> dict[str, list[str]]:
        return self._load()

    def members(self, role: Role) -> list[str]:
        return self._load()[role]

    def primary(self, role: Role) -> str | None:
        members = self.members(role)
        return members[0] if members else None

    def role_of(self, name: str) -> Role | None:
        for role, names in self._load().items():
            if name in names:
                return role
        return None

    def all_names(self) -> set[str]:
        return {n for names in self._load().values() for n in names}

    def enlist(self, name: str, role: Role) -> bool:
        """Seat `name` in `role`, moving it from any other. False if already there."""
        roster = self._load()
        if name in roster[role]:
            return False
        for names in roster.values():
            if name in names:
                names.remove(name)
        roster[role].append(name)
        self._save(roster)
        return True

    def retire(self, name: str) -> bool:
        roster = self._load()
        for names in roster.values():
            if name in names:
                names.remove(name)
                self._save(roster)
                return True
        return False

    def next_rotation(self) -> int:
        """The bidder-rotation counter; each call returns the current value and advances it."""
        n = int(self.ledger.get_meta(_ROTATION_KEY) or 0)
        self.ledger.set_meta(_ROTATION_KEY, str(n + 1))
        return n


def pick_bidders(live: list[tuple[str, str]], max_n: int, start: int) -> list[str]:
    """Choose up to `max_n` of the live (name, vendor) pairs: one seat per vendor first,
    then fill from the rest. The scan begins at `start % len(live)`, so successive
    Charters (an incrementing `start`) rotate who gets in. Within the cap, everyone sits."""
    if len(live) <= max_n:
        return [name for name, _ in live]
    k = start % len(live)
    scan = live[k:] + live[:k]
    chosen: list[str] = []
    vendors: set[str] = set()
    for name, vendor in scan:
        if len(chosen) < max_n and vendor not in vendors:
            chosen.append(name)
            vendors.add(vendor)
    for name, _ in scan:
        if len(chosen) < max_n and name not in chosen:
            chosen.append(name)
    return chosen


@dataclass(frozen=True)
class AgentInfo:
    name: str
    agent_type: str | None  # Switch's known_agent_type: claude-code, codex, opencode, ...
    model: str | None  # first entry of the agent's models, when it lists any
    online: bool

    @property
    def vendor(self) -> str:
        """Key for diversity: the agent type, split per model for opencode, where one
        agent type fronts many vendors' models. Unknown types never share a vendor."""
        if not self.agent_type:
            return f"custom:{self.name}"
        if self.agent_type == "opencode" and self.model:
            return f"opencode:{self.model}"
        return self.agent_type

    @property
    def label(self) -> str:
        base = self.agent_type or "custom"
        return f"{base} · {self.model}" if self.model else base


def _first_model(detail: dict) -> str | None:
    models = detail.get("models") or []
    if not models:
        return None
    first = models[0]
    return (first.get("name") if isinstance(first, dict) else str(first)) or None


async def lookup(client: SwitchClient, names: list[str]) -> dict[str, AgentInfo]:
    """AgentInfo for each of `names` that exists in Switch (others are left out).
    Raises SwitchError if Switch cannot be asked."""
    if not names:
        return {}
    by_name = {a["name"]: a["id"] for a in await client.op("list_agents")}
    found: dict[str, AgentInfo] = {}
    for name in names:
        if name not in by_name:
            continue
        detail = await client.op("get_agent_detail", agent_id=by_name[name])
        online = any(s.get("state") in _LIVE_STATES for s in detail.get("sessions", []))
        found[name] = AgentInfo(name, detail.get("known_agent_type"), _first_model(detail), online)
    return found


def render_roster(roster: dict[str, list[str]], infos: dict[str, AgentInfo]) -> str:
    """One line per role: `name (vendor) ●` online, `○` offline."""
    lines = []
    for role in ROLES:
        parts = []
        for name in roster.get(role, []):
            info = infos.get(name)
            label = info.label if info else "not in Switch"
            parts.append(f"{name} ({label}) {'●' if info and info.online else '○'}")
        lines.append(f"{role}: {' · '.join(parts) or 'none'}")
    return "\n".join(lines)
