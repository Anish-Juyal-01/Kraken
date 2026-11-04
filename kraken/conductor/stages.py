import asyncio
import re
from pathlib import Path

import httpx
import yaml

from kraken.config import Config
from kraken.crew import Crew, Role
from kraken.hallmark.ledger import Request
from kraken.switch_client import SwitchClient, SwitchError

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


class StageError(Exception):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def with_people(text: str, people: list[str], bridge: str | None = None) -> str:
    """The template with `people` (chat usernames) as the room's users, on `bridge`."""
    if not people and not bridge:
        return text
    doc = yaml.safe_load(text)
    if people:
        doc["room"]["users"] = list(people)
    if bridge:
        doc["room"]["bridge"] = bridge
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=1000)


async def open_stage(client: SwitchClient, stage: str, inputs: dict,
                     people: list[str] | None = None, bridge: str | None = None) -> str:
    path = TEMPLATES_DIR / f"{stage}.template.yaml"
    try:
        text = with_people(path.read_text(encoding="utf-8"), people or [], bridge)
    except OSError as err:
        raise StageError(stage, f"no template: {err}") from err
    try:
        result = await client.op("create_room_from_yaml", yaml=text, inputs=inputs,
                                 _timeout=CREATE_TIMEOUT_S)
    except SwitchError as err:
        raise StageError(stage, str(err.detail)) from err
    except httpx.TimeoutException:
        # The server may still have made the room; find it rather than lose track of it.
        name = room_name(text, inputs)
        room_id = await _find_room(client, name)
        if room_id is None:
            raise StageError(stage, f"Switch did not answer in {CREATE_TIMEOUT_S}s and no room "
                                    f"named {name!r} appeared") from None
        return room_id
    return result["room_id"]


CREATE_TIMEOUT_S = 180
FIND_ATTEMPTS, FIND_INTERVAL_S = 6, 10.0
_PLACEHOLDER = re.compile(r"\{([a-z_0-9]+)\}")


def room_name(text: str, inputs: dict) -> str:
    """The room name a stage template renders to, for these inputs."""
    raw = yaml.safe_load(text)["room"]["name"]
    return _PLACEHOLDER.sub(lambda m: str(inputs.get(m.group(1), m.group(0))), raw)


async def _find_room(client: SwitchClient, name: str) -> str | None:
    for attempt in range(FIND_ATTEMPTS):
        if attempt:
            await asyncio.sleep(FIND_INTERVAL_S)
        try:
            rooms = await client.op("list_rooms")
        except (SwitchError, httpx.HTTPError):
            continue
        for r in rooms if isinstance(rooms, list) else []:
            if r.get("name") == name and r.get("room_id"):
                return r["room_id"]
    return None


def _split(stage: str, names: list[str], what: str) -> dict:
    if not names:
        raise StageError(stage, f"no {what} available")
    return {"first": names[0], "rest": ", ".join(names[1:]),
            "mentions": " ".join(f"@{n}" for n in names[1:])}


def _agent(stage: str, role: Role, cfg: Config, crew: Crew | None) -> str:
    """The agent for a single-seat role: the crew's first member (config when there is no crew)."""
    name = crew.primary(role) if crew is not None else getattr(cfg.agents, role)
    if not name:
        raise StageError(stage, f"no {role} on the crew")
    return name


def stage_inputs(stage: str, req: Request, cfg: Config, crew: Crew | None = None, **extra) -> dict:
    tag = str(req.id)
    base = {"tag": tag, "kraken": cfg.kraken_agent}
    if stage == "spark":
        inputs = {**base, "request": req.text, "planner": _agent(stage, "planner", cfg, crew),
                  "owner": req.owner}
    elif stage == "sonar-resolution":
        inputs = base
    elif stage == "charter":
        default = crew.members("bidder") if crew is not None else cfg.bidders
        parts = _split(stage, list(extra.pop("bidders", default)), "bidders")
        inputs = {**base, "brief": req.brief or "", "bidder_1": parts["first"],
                  "other_bidders": parts["rest"], "other_mentions": parts["mentions"]}
    elif stage == "forge":
        agents = [b["agent"] for b in req.bids]
        reviewers = extra.pop("reviewers", [a for a in agents if a != req.winner])
        parts = _split(stage, list(reviewers), "reviewers")
        inputs = {**base, "brief": req.brief or "", "coder": req.winner,
                  "reviewer_1": parts["first"], "other_reviewers": parts["rest"],
                  "other_reviewer_mentions": parts["mentions"]}
    elif stage == "storm":
        inputs = {**base, "brief": req.brief or "", "diff_ref": f"branch kraken/K-{req.id}",
                  "mode": req.storm_mode, "attacker": _agent(stage, "attacker", cfg, crew),
                  "defender": _agent(stage, "defender", cfg, crew),
                  "coach": _agent(stage, "coach", cfg, crew)}
    else:
        raise StageError(stage, "unknown stage")
    return {**inputs, **extra}
