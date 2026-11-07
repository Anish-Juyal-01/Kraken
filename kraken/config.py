from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

AutonomyValue = Literal["human", "auto"]


class SonarConfig(BaseModel):
    threshold: float = 0.75
    code_paths: list[str] = Field(default_factory=list)
    min_words: int = 6


class StormConfig(BaseModel):
    max_rounds: int = 3
    # drill: people defend and can hand off with 🤖; auto: the defender agent does it
    default_mode: Literal["auto", "drill", "skip"] = "drill"


class RoomsConfig(BaseModel):
    requests: str = "kraken-requests"
    hallmark: str = "kraken-hallmark"


class AgentsConfig(BaseModel):
    planner: str = "kraken-planner"
    attacker: str = "squall"
    defender: str = "storm-defender"
    coach: str = "storm-coach"


class CharterConfig(BaseModel):
    max_bidders: int = Field(default=5, ge=1)
    # Coding agents added in Switch join as bidders on their own at the next Charter
    # (any vendor: Switch reports each agent's type). Custom agents without a coding type
    # never auto-join. `@kraken retire <name>` takes one off; never_join keeps it off.
    auto_join: bool = True
    never_join: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    """One codebase Kraken works on. Seeded from kraken.yaml `projects:`, or added from chat
    with `@kraken connect <repo>` (see kraken.init.detect and kraken.projects)."""

    name: str = Field(pattern=r"^[\w.-]+$")
    repo: str | None = None  # git URL; None = the agents' own checkout (the shopfront demo)
    branch: str = "main"
    root: str = "."  # Kraken's local checkout, for indexing code_paths
    description: str = ""
    language: str | None = None
    code_paths: list[str] = Field(default_factory=list)  # relative to root
    test: str | None = None
    run: str | None = None  # how to start it; None = library mode
    base_url: str | None = None
    health: str | None = None
    endpoints: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)  # relative to the checkout
    chaos_endpoint: str | None = None  # only apps with a built-in fault switch (shopfront)

    @property
    def mode(self) -> Literal["http", "library"]:
        return "http" if self.run else "library"

    @property
    def workdir(self) -> str:
        """Where an agent works on this project, inside its own folder."""
        return f"projects/{self.name}" if self.repo else "."

    def index_paths(self) -> list[str]:
        base = Path(self.root)
        return [str(base / p) for p in self.code_paths] or [str(base)]


AUTONOMY_KEYS = ("brief", "pick", "merge", "storm_result")


def _default_autonomy() -> dict[str, AutonomyValue]:
    return {k: "human" for k in AUTONOMY_KEYS}


def _default_timeouts() -> dict[str, int]:
    return {"spark": 1800, "charter": 900, "forge": 3600, "storm": 1800, "deliver": 3600}


class Config(BaseModel):
    sonar: SonarConfig = Field(default_factory=SonarConfig)
    storm: StormConfig = Field(default_factory=StormConfig)
    autonomy: dict[str, AutonomyValue] = Field(default_factory=_default_autonomy)
    rooms: RoomsConfig = Field(default_factory=RoomsConfig)
    charter: CharterConfig = Field(default_factory=CharterConfig)
    bidders: list[str] = Field(default_factory=list)
    kraken_agent: str = "kraken"
    # Chat-app usernames added to every stage room Kraken opens, so people can answer
    # its prompts. Kraken creates those rooms as an agent, which has no chat account,
    # so the templates cannot use {$creator}.
    people: list[str] = Field(default_factory=list)
    # Display name of the messaging app (Switch "bridge") for stage rooms, e.g. a Discord
    # connection's name. None uses the server's default app.
    bridge: str | None = None
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    timeouts_s: dict[str, int] = Field(default_factory=_default_timeouts)
    db_path: str = "kraken.db"
    relay_port: int = 8765
    # Codebases. The first is the default unless default_project says otherwise; more are
    # added from chat (`@kraken connect <repo>`) and kept in the ledger.
    projects: list[ProjectConfig] = Field(default_factory=list)
    default_project: str | None = None
    workspace: str = "workspace"  # where Kraken clones connected repos
    # After Storm survives: "pr" asks a person before the coder pushes and opens a pull
    # request; "branch" leaves the work on the coder's local branch.
    deliver: Literal["pr", "branch"] = "pr"
    # Human first, but nobody waits forever: if no person replies to a step for this many
    # seconds, Kraken hands it to the agent (as if someone replied 🤖). 0 = wait for people.
    # The pull request is never handed off.
    human_wait_s: int = Field(default=120, ge=0)
    # Agents sometimes go quiet mid-stage (a session ends without a hand-off). If a stage
    # room where agents are working has no message for this long, Kraken asks the agent
    # it waits on for a status, once per quiet spell. 0 = never nudge.
    agent_quiet_s: int = Field(default=600, ge=0)

    @model_validator(mode="after")
    def _legacy_code_paths(self):
        # Before projects existed, sonar.code_paths named the one codebase.
        if not self.projects and self.sonar.code_paths:
            self.projects = [ProjectConfig(name="default", code_paths=self.sonar.code_paths)]
        return self

    @field_validator("autonomy", mode="before")
    @classmethod
    def _merge_autonomy(cls, v):
        if not isinstance(v, dict):
            return v
        unknown = set(v) - set(AUTONOMY_KEYS)
        if unknown:
            raise ValueError(f"unknown autonomy keys: {sorted(unknown)}")
        return {**_default_autonomy(), **v}


def load_config(path: str = "kraken.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config.model_validate(data)
