from pydantic import BaseModel

from kraken.hallmark.ledger import Ledger
from kraken.llm import LLM, SMART
from kraken.models import Claim
from kraken.sonar.extract import canonical

ALIAS_SYSTEM = (
    "You decide whether a name refers to the same entity as one of a list of known names "
    '(e.g. "account_id" and "user_id" may be the same field). Reply with JSON only: '
    '{"match": "<one of the known names>" | null}. Use null if none is the same entity.'
)


class AliasAnswer(BaseModel):
    match: str | None


async def normalize(llm: LLM, ledger: Ledger, c: Claim) -> Claim:
    name = canonical(c.subject)
    known = ledger.alias(name)
    if known is None:
        known = name
        subjects = ledger.known_subjects()
        if subjects and name not in subjects:
            user = f"Is {name!r} the same entity as one of {subjects}?"
            match = (await llm.json(SMART, ALIAS_SYSTEM, user, AliasAnswer)).match
            if match in subjects:
                known = match
            ledger.set_alias(name, known)
    return c.model_copy(update={"subject": known})
