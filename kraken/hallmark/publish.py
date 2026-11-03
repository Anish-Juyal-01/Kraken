from datetime import datetime, timezone

from kraken.hallmark.ledger import Ledger
from kraken.models import Claim
from kraken.switch_client import SwitchClient, SwitchError

DOC_ID_KEY = "hallmark_doc_id"
_INSTRUCTIONS = ("This is the current truth. Cite it; never contradict it without raising it "
                 "in the room.")


def _line(claim: Claim, decided_by: str) -> str:
    if claim.source == "code":
        source = f"{claim.path}:{claim.line}" if claim.line is not None else str(claim.path)
        when = ""
    else:
        source = f"#{claim.room_id}"
        day = datetime.fromtimestamp(claim.timestamp / 1000, timezone.utc).strftime("%Y-%m-%d")
        when = f"{day}, "
    return (f"- **{claim.subject}.{claim.attribute}** = {claim.value} — "
            f"decided by {decided_by}, {when}source {source}")


def render(decisions: list[tuple[int, Claim, str]]) -> str:
    if not decisions:
        return "_No decisions yet._"
    return "\n".join(_line(claim, by) for _, claim, by in decisions)


async def _create(client: SwitchClient, ledger: Ledger, content: str) -> None:
    doc = await client.op("create_room_document", name="Hallmark",
                          description="Kraken decision ledger", instructions=_INSTRUCTIONS,
                          content=content)
    ledger.set_meta(DOC_ID_KEY, doc["document_id"])


async def publish(client: SwitchClient, ledger: Ledger, hallmark_room_id: str) -> None:
    content = render(ledger.decisions())
    await client.op("connect_to_room", room_id=hallmark_room_id)
    doc_id = ledger.get_meta(DOC_ID_KEY)
    if doc_id is None:
        await _create(client, ledger, content)
        return
    try:
        await client.op("update_room_document", document_id=doc_id, content=content)
    except SwitchError as err:
        if err.status != 404:
            raise
        await _create(client, ledger, content)
