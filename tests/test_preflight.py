from kraken.config import Config
from kraken.models import Claim
from kraken.sonar.detect import JudgeAnswer
from kraken.sonar.extract import ClaimList, IsDecision, RawClaim
from kraken.sonar.preflight import preflight
from tests.fakes import FakeLLM

BRIEF = "Store the user_id as an integer column in the users table for every account."


def code(value: str) -> Claim:
    return Claim(subject="user_id", attribute="type", value=value, speaker="code", room_id="",
                 message_id="", timestamp=0, confidence=1.0, source="code", path="api.yaml", line=3)


def extracted(value: str) -> list:
    return [IsDecision(is_decision=True),
            ClaimList(claims=[RawClaim(subject="user_id", attribute="type", value=value,
                                       confidence=0.9)])]


def verdict(v="conflict", c=0.9) -> JudgeAnswer:
    return JudgeAnswer(verdict=v, confidence=c, reason="r")


async def test_brief_contradicting_code_is_a_conflict(ledger):
    llm = FakeLLM([*extracted("integer"), verdict()])
    out = await preflight(llm, ledger, [code("uuid")], BRIEF, Config())
    assert len(out) == 1 and out[0].a.source == "code" and out[0].b.value == "integer"
    assert out[0].b.speaker == "preflight"
    assert ledger.claims_for("user_id", "type") == []


async def test_matching_value_makes_no_judge_call(ledger):
    llm = FakeLLM(extracted("UUID"))
    assert await preflight(llm, ledger, [code("uuid")], BRIEF, Config()) == []
    assert len(llm.calls) == 2


async def test_brief_without_claims(ledger):
    llm = FakeLLM([IsDecision(is_decision=False)])
    assert await preflight(llm, ledger, [code("uuid")], BRIEF, Config()) == []


async def test_brief_contradicting_a_decision(ledger):
    decided = code("uuid").model_copy(update={"source": "chat", "speaker": "@sam", "path": None})
    ledger.add_decision(decided, "@sam", None)
    llm = FakeLLM([*extracted("integer"), verdict()])
    out = await preflight(llm, ledger, [], BRIEF, Config())
    assert len(out) == 1 and out[0].a.speaker == "@sam"


async def test_low_confidence_and_non_conflicts_are_dropped(ledger):
    llm = FakeLLM([*extracted("integer"), verdict(c=0.5)])
    assert await preflight(llm, ledger, [code("uuid")], BRIEF, Config()) == []
    llm = FakeLLM([*extracted("integer"), verdict("compatible")])
    assert await preflight(llm, ledger, [code("uuid")], BRIEF, Config()) == []


async def test_tag_and_marker_lines_do_not_skip_the_brief(ledger):
    brief = BRIEF + " See [K-7]." + chr(10) + "FAILED login returns 401 today."
    llm = FakeLLM([*extracted("integer"), verdict()])
    out = await preflight(llm, ledger, [code("uuid")], brief, Config())
    assert len(out) == 1
