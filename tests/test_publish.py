from kraken.hallmark.publish import publish, render
from kraken.models import Claim
from kraken.switch_client import SwitchError


def claim(value, **kw):
    base = dict(subject="user_id", attribute="type", value=value, speaker="sam", room_id="r1",
                message_id="m1", timestamp=1_793_750_400_000, confidence=0.9)
    return Claim(**{**base, **kw})


class DocClient:
    def __init__(self, fail_update=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_update = fail_update
        self.n = 0

    async def op(self, name, /, **args):
        self.calls.append((name, args))
        if name == "create_room_document":
            self.n += 1
            return {"document_id": f"doc-{self.n}"}
        if name == "update_room_document" and self.fail_update:
            raise self.fail_update
        return {"ok": True}

    def named(self, name):
        return [a for n, a in self.calls if n == name]


def test_render_newest_first_and_hides_superseded(ledger):
    old = ledger.add_decision(claim("int"), "ann", None)
    ledger.add_decision(claim("uuid", subject="order_id"), "bob", None)
    ledger.add_decision(claim("uuid string"), "carol", supersedes=old)
    ledger.add_decision(claim("pkg.Foo", source="code", path="a/b.py", line=7, timestamp=0),
                        "dan", None)
    lines = render(ledger.decisions()).splitlines()
    assert "= int" not in "\n".join(lines)
    assert lines[0].endswith("decided by dan, source a/b.py:7")
    assert lines[1] == "- **user_id.type** = uuid string — decided by carol, 2026-11-04, source #r1"
    assert lines[2].startswith("- **order_id.type** = uuid ")
    assert len(lines) == 3


def test_render_empty():
    assert render([]) == "_No decisions yet._"


async def test_publish_creates_then_updates(ledger):
    client = DocClient()
    ledger.add_decision(claim("uuid"), "carol", None)
    await publish(client, ledger, "hm")
    assert client.calls[0] == ("connect_to_room", {"room_id": "hm"})
    (created,) = client.named("create_room_document")
    assert created["name"] == "Hallmark" and created["description"] == "Kraken decision ledger"
    assert "never contradict" in created["instructions"] and "uuid" in created["content"]
    assert ledger.get_meta("hallmark_doc_id") == "doc-1"
    await publish(client, ledger, "hm")
    (upd,) = client.named("update_room_document")
    assert upd["document_id"] == "doc-1" and "uuid" in upd["content"]
    assert len(client.named("create_room_document")) == 1


async def test_update_404_falls_back_to_create(ledger):
    client = DocClient(fail_update=SwitchError(404, "gone"))
    ledger.set_meta("hallmark_doc_id", "old")
    await publish(client, ledger, "hm")
    assert len(client.named("create_room_document")) == 1
    assert ledger.get_meta("hallmark_doc_id") == "doc-1"
