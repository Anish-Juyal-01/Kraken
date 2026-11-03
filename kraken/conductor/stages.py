from pathlib import Path

from kraken.config import Config
from kraken.hallmark.ledger import Request
from kraken.switch_client import SwitchClient, SwitchError

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


class StageError(Exception):
    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


async def open_stage(client: SwitchClient, stage: str, inputs: dict) -> str:
    path = TEMPLATES_DIR / f"{stage}.template.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise StageError(stage, f"no template: {err}") from err
    try:
        result = await client.op("create_room_from_yaml", yaml=text, inputs=inputs)
    except SwitchError as err:
        raise StageError(stage, str(err.detail)) from err
    return result["room_id"]


def _split(stage: str, names: list[str], what: str) -> dict:
    if not names:
        raise StageError(stage, f"no {what} available")
    return {"first": names[0], "rest": ", ".join(names[1:]),
            "mentions": " ".join(f"@{n}" for n in names[1:])}


def stage_inputs(stage: str, req: Request, cfg: Config, **extra) -> dict:
    tag = str(req.id)
    base = {"tag": tag, "kraken": cfg.kraken_agent}
    if stage == "spark":
        inputs = {**base, "request": req.text, "planner": cfg.agents.planner, "owner": req.owner}
    elif stage == "sonar-resolution":
        inputs = base
    elif stage == "charter":
        parts = _split(stage, list(extra.pop("bidders", cfg.bidders)), "bidders")
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
                  "mode": req.storm_mode, "attacker": cfg.agents.attacker,
                  "defender": cfg.agents.defender, "coach": cfg.agents.coach}
    else:
        raise StageError(stage, "unknown stage")
    return {**inputs, **extra}
