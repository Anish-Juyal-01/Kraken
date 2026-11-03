from kraken.config import Config
from kraken.hallmark.ledger import Ledger
from kraken.llm import LLM
from kraken.models import Claim, Conflict, Event
from kraken.sonar.code_check import code_claims_for
from kraken.sonar.detect import norm_value, judge
from kraken.sonar.extract import extract_claims
from kraken.sonar.normalize import normalize


async def preflight(llm: LLM, ledger: Ledger, code_index: list[Claim], brief: str,
                    cfg: Config) -> list[Conflict]:
    """Check a brief's claims against recorded decisions and the code. Stores nothing."""
    ev = Event(type="message", room_id="preflight", sequence=None,
               payload={"sender": "preflight", "body": brief, "message_id": "brief", "timestamp": 0})
    conflicts: list[Conflict] = []
    for raw in await extract_claims(llm, ev, min_words=0):
        claim = await normalize(llm, ledger, raw)
        earlier = [d for _, d, _ in ledger.decisions()
                   if d.subject == claim.subject and d.attribute == claim.attribute]
        earlier += code_claims_for(code_index, claim.subject, claim.attribute)
        for other in earlier:
            if norm_value(other.value) == norm_value(claim.value):
                continue
            result = await judge(llm, other, claim)
            if result.verdict == "conflict" and result.confidence >= cfg.sonar.threshold:
                conflicts.append(result)
    return conflicts
