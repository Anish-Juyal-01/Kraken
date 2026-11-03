from kraken.models import Claim
from kraken.sonar.detect import JudgeAnswer, detect
from tests.fakes import FakeLLM


def claim(value: str, attribute: str = "type", subject: str = "user_id") -> Claim:
    return Claim(subject=subject, attribute=attribute, value=value, speaker="@sam", room_id="backend",
                 message_id="m", timestamp=1, confidence=0.9)


def answer(verdict="conflict", confidence=0.9) -> JudgeAnswer:
    return JudgeAnswer(verdict=verdict, confidence=confidence, reason="r")


async def test_equal_values_skip_llm(ledger):
    ledger.add_claim(claim("UUID"))
    b = claim("  uuid ")
    idb = ledger.add_claim(b)
    llm = FakeLLM([])
    assert await detect(llm, ledger, idb, b, 0.75) == []
    assert llm.calls == []


async def test_conflict_raised_and_marked(ledger):
    ida = ledger.add_claim(claim("uuid"))
    b = claim("int")
    idb = ledger.add_claim(b)
    out = await detect(FakeLLM([answer()]), ledger, idb, b, 0.75)
    assert len(out) == 1 and ledger.pair_seen(ida, idb)
    assert out[0].a.value == "uuid" and out[0].b.value == "int"


async def test_update_not_raised(ledger):
    ledger.add_claim(claim("uuid"))
    b = claim("int")
    idb = ledger.add_claim(b)
    assert await detect(FakeLLM([answer("update")]), ledger, idb, b, 0.75) == []


async def test_below_threshold_not_raised(ledger):
    ledger.add_claim(claim("uuid"))
    b = claim("int")
    idb = ledger.add_claim(b)
    assert await detect(FakeLLM([answer(confidence=0.5)]), ledger, idb, b, 0.75) == []


async def test_dedupe_across_instances(tmp_path, open_ledger):
    path = str(tmp_path / "l.db")
    led = open_ledger(path)
    led.add_claim(claim("uuid"))
    b = claim("int")
    idb = led.add_claim(b)
    assert len(await detect(FakeLLM([answer()]), led, idb, b, 0.75)) == 1
    llm = FakeLLM([])
    assert await detect(llm, open_ledger(path), idb, b, 0.75) == []
    assert llm.calls == []


async def test_dismissals_raise_threshold(ledger):
    ids = [ledger.add_claim(claim(f"v{i}")) for i in range(4)]
    ledger.mark_pair(ids[0], ids[1], "dismissed")
    ledger.mark_pair(ids[2], ids[3], "dismissed")
    b = claim("new")
    idb = ledger.add_claim(b)
    # 2 dismissals -> threshold 0.75 + 0.10 = 0.85; 0.8 is not enough
    assert await detect(FakeLLM([answer(confidence=0.8)] * 4), ledger, idb, b, 0.75) == []
    assert await detect(FakeLLM([answer(confidence=0.85)] * 4), ledger, idb, b, 0.75)
