from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

AutonomyValue = Literal["human", "auto"]


class SonarConfig(BaseModel):
    threshold: float = 0.75
    code_paths: list[str] = Field(default_factory=list)
    min_words: int = 6


class StormConfig(BaseModel):
    max_rounds: int = 3
    default_mode: Literal["auto", "drill", "skip"] = "auto"


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


AUTONOMY_KEYS = ("brief", "pick", "merge", "storm_result")


def _default_autonomy() -> dict[str, AutonomyValue]:
    return {k: "human" for k in AUTONOMY_KEYS}


def _default_timeouts() -> dict[str, int]:
    return {"spark": 1800, "charter": 900, "forge": 3600, "storm": 1800}


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
