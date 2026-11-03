from kraken.hallmark.ledger import Ledger
from kraken.models import Claim
from kraken.sonar.normalize import AliasAnswer, normalize
from tests.fakes import FakeLLM


def claim(subject: str) -> Claim:
    return Claim(subject=subject, attribute="type", value="uuid", speaker="@sam", room_id="backend",
                 message_id="m1", timestamp=1, confidence=0.9)


def ledger_with(tmp_path, subject="user_id") -> Ledger:
    led = Ledger(str(tmp_path / "l.db"))
    led.add_claim(claim(subject))
    return led


async def test_canonical_only_when_no_known_subjects(tmp_path):
    llm = FakeLLM([])
    led = Ledger(str(tmp_path / "l.db"))
    assert (await normalize(llm, led, claim("userId"))).subject == "user_id"
    assert llm.calls == []


async def test_alias_matched_and_cached(tmp_path):
    led = ledger_with(tmp_path)
    llm = FakeLLM([AliasAnswer(match="user_id")])
    c = claim("account id")
    out = await normalize(llm, led, c)
    assert out.subject == "user_id" and c.subject == "account id"
    assert led.alias("account_id") == "user_id"
    assert (await normalize(llm, led, claim("Account-ID"))).subject == "user_id"
    assert len(llm.calls) == 1


async def test_miss_cached_as_itself(tmp_path):
    led = ledger_with(tmp_path)
    llm = FakeLLM([AliasAnswer(match=None)])
    assert (await normalize(llm, led, claim("retry_limit"))).subject == "retry_limit"
    assert led.alias("retry_limit") == "retry_limit"
    await normalize(llm, led, claim("retryLimit"))
    assert len(llm.calls) == 1


async def test_match_not_in_known_subjects_is_no_match(tmp_path):
    led = ledger_with(tmp_path)
    llm = FakeLLM([AliasAnswer(match="invented")])
    assert (await normalize(llm, led, claim("retry_limit"))).subject == "retry_limit"
