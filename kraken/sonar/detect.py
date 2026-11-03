from typing import Literal

from pydantic import BaseModel

from kraken.hallmark.ledger import Ledger
from kraken.llm import LLM, SMART
from kraken.models import Claim, Conflict

JUDGE_SYSTEM = (
    "You compare two claims about the same subject and attribute, taken from a team chat or code. "
    'Reply with JSON only: {"verdict": "conflict" | "update" | "compatible", '
    '"confidence": <0..1>, "reason": "<one sentence>"}. '
    '"update" = the newer claim deliberately supersedes the older one in the same scope. '
    '"conflict" = both cannot be true at once and neither supersedes the other. '
    '"compatible" = there is no contradiction (different scope, or both can hold).'
)


class JudgeAnswer(BaseModel):
    verdict: Literal["conflict", "update", "compatible"]
    confidence: float
    reason: str


def norm_value(value: str) -> str:
    return " ".join(value.lower().split())


def _describe(c: Claim) -> str:
    where = c.path if c.source == "code" else f"{c.speaker} in {c.room_id}"
    return f"{c.subject}.{c.attribute} = {c.value!r} ({c.source}, {where})"


async def judge(llm: LLM, a: Claim, b: Claim) -> Conflict:
    user = f"Older claim: {_describe(a)}\nNewer claim: {_describe(b)}"
    ans = await llm.json(SMART, JUDGE_SYSTEM, user, JudgeAnswer)
    return Conflict(a=a, b=b, verdict=ans.verdict, confidence=ans.confidence, reason=ans.reason)


async def detect(llm: LLM, ledger: Ledger, claim_id: int, c: Claim,
                 threshold: float) -> list[Conflict]:
    needed = threshold + 0.05 * min(ledger.dismissal_count(c.subject, c.attribute), 4)
    raised: list[Conflict] = []
    for earlier_id, earlier in ledger.claims_for(c.subject, c.attribute):
        if earlier_id == claim_id or norm_value(earlier.value) == norm_value(c.value):
            continue
        if ledger.pair_seen(earlier_id, claim_id):
            continue
        result = await judge(llm, earlier, c)
        if result.verdict == "conflict" and result.confidence >= needed:
            ledger.mark_pair(earlier_id, claim_id, "raised")
            raised.append(result)
    return raised
