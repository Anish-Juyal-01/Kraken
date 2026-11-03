import re

from pydantic import BaseModel

from kraken.llm import FAST, LLM, SMART
from kraken.models import Claim, Event

FILTER_SYSTEM = (
    "You screen team chat messages. Answer whether the message states or decides a concrete "
    "fact, decision, limit, date, type or deadline about the project (not chit-chat, questions "
    'or greetings). Reply with JSON only: {"is_decision": true|false}.'
)

EXTRACT_SYSTEM = (
    "Extract structured claims from a team chat message. A claim is a subject/attribute/value "
    "triple stating a fact, decision, limit, date, type or deadline, e.g. subject 'user_id', "
    "attribute 'type', value 'uuid string'. Give each a confidence between 0 and 1. If the "
    "message contains none, return an empty list. Reply with JSON only: "
    '{"claims": [{"subject": str, "attribute": str, "value": str, "confidence": float}]}.'
)

_TAG = "[K-"
_MARKERS = ("BID:", "ACCEPTED", "SURVIVED", "FAILED")


class IsDecision(BaseModel):
    is_decision: bool


class RawClaim(BaseModel):
    subject: str
    attribute: str
    value: str
    confidence: float


class ClaimList(BaseModel):
    claims: list[RawClaim]


def canonical(raw: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw.strip())
    return re.sub(r"[\s\-_]+", "_", s).lower()


def _skip_body(body: str) -> bool:
    return _TAG in body or any(ln.lstrip().startswith(_MARKERS) for ln in body.splitlines())


async def extract(llm: LLM, msg: Event, self_id: str, min_words: int) -> list[Claim]:
    body = msg.payload.get("body")
    if msg.type != "message" or not isinstance(body, str):
        return []
    if msg.payload.get("sender") == self_id or _skip_body(body):
        return []
    text = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith(">"))
    if len(text.split()) < min_words:
        return []
    if not (await llm.json(FAST, FILTER_SYSTEM, text, IsDecision)).is_decision:
        return []
    raw = await llm.json(SMART, EXTRACT_SYSTEM, text, ClaimList)
    p = msg.payload
    return [
        Claim(subject=r.subject, attribute=r.attribute, value=r.value, speaker=p["sender"],
              room_id=msg.room_id, message_id=p["message_id"], timestamp=p["timestamp"],
              confidence=r.confidence)
        for r in raw.claims
    ]
