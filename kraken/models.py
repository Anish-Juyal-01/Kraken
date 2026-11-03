from typing import Literal

from pydantic import BaseModel


class Claim(BaseModel):
    subject: str
    attribute: str
    value: str
    speaker: str
    room_id: str
    message_id: str
    timestamp: int
    confidence: float
    source: Literal["chat", "code"] = "chat"
    path: str | None = None
    line: int | None = None


Verdict = Literal["conflict", "update", "compatible"]


class Conflict(BaseModel):
    a: Claim
    b: Claim
    verdict: Verdict
    confidence: float
    reason: str


class Event(BaseModel):
    type: str
    room_id: str
    sequence: int | None
    payload: dict
