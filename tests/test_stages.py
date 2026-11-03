import pytest

from kraken.config import Config
from kraken.conductor.stages import StageError, open_stage, stage_inputs
from kraken.hallmark.ledger import Request
from kraken.switch_client import SwitchError


class MockClient:
    def __init__(self, result=None, error: SwitchError | None = None):
        self.result = result if result is not None else {"room_id": "r-1"}
        self.error = error
        self.ops: dict[str, list[dict]] = {}

    async def op(self, name, **args):
        self.ops.setdefault(name, []).append(args)
        if self.error:
            raise self.error
        return self.result


def req(**kw) -> Request:
    base = dict(id=7, owner="@sam", text="build password reset", origin_room="requests",
                brief="the brief", winner="claude-coder",
                bids=[{"agent": "claude-coder"}, {"agent": "codex-coder"}, {"agent": "opencode-coder"}])
    return Request(**{**base, **kw})


async def test_open_stage_calls_create_room_from_yaml():
    c = MockClient()
    room = await open_stage(c, "spark", {"tag": "7"})
    assert room == "r-1"
    call = c.ops["create_room_from_yaml"][0]
    assert call["inputs"] == {"tag": "7"}
    assert "version: 1" in call["yaml"] and "Spark [K-{tag}]" in call["yaml"]


async def test_open_stage_wraps_switch_error():
    c = MockClient(error=SwitchError(404, "no such agent"))
    with pytest.raises(StageError) as e:
        await open_stage(c, "spark", {})
    assert e.value.stage == "spark" and "no such agent" in e.value.detail


async def test_open_stage_unknown_stage():
    with pytest.raises(StageError):
        await open_stage(MockClient(), "nope", {})


def test_inputs_spark():
    got = stage_inputs("spark", req(), Config())
    assert got == {"tag": "7", "request": "build password reset", "planner": "kraken-planner",
                   "owner": "@sam", "kraken": "kraken"}


def test_inputs_charter_uses_live_bidders():
    got = stage_inputs("charter", req(), Config(),
                       bidders=["claude-coder", "codex-coder", "opencode-coder"])
    assert got["bidder_1"] == "claude-coder"
    assert got["other_bidders"] == "codex-coder, opencode-coder"
    assert got["other_mentions"] == "@codex-coder @opencode-coder"
    assert got["brief"] == "the brief" and got["tag"] == "7"


def test_inputs_charter_single_bidder_has_empty_extras():
    got = stage_inputs("charter", req(), Config(), bidders=["claude-coder"])
    assert got["other_bidders"] == "" and got["other_mentions"] == ""


def test_inputs_charter_without_bidders_is_an_error():
    with pytest.raises(StageError):
        stage_inputs("charter", req(), Config(), bidders=[])


def test_inputs_forge_reviewers_are_other_bidders():
    got = stage_inputs("forge", req(), Config())
    assert got["coder"] == "claude-coder" and got["reviewer_1"] == "codex-coder"
    assert got["other_reviewers"] == "opencode-coder"
    assert got["other_reviewer_mentions"] == "@opencode-coder"


def test_inputs_forge_needs_a_reviewer():
    with pytest.raises(StageError):
        stage_inputs("forge", req(bids=[{"agent": "claude-coder"}]), Config())


def test_inputs_storm():
    got = stage_inputs("storm", req(storm_mode="drill"), Config())
    assert got == {"tag": "7", "brief": "the brief", "diff_ref": "branch kraken/K-7",
                   "mode": "drill", "attacker": "squall", "defender": "storm-defender",
                   "coach": "storm-coach", "kraken": "kraken"}


def test_extra_overrides_and_resolution_passthrough():
    got = stage_inputs("sonar-resolution", req(), Config(), subject="user_id", prompt_text="p",
                       quote_a="a", link_a="la", user_a="@a", quote_b="b", link_b="lb", user_b="@b")
    assert got["tag"] == "7" and got["kraken"] == "kraken" and got["prompt_text"] == "p"
    assert stage_inputs("spark", req(), Config(), planner="x")["planner"] == "x"


def test_unknown_stage_inputs():
    with pytest.raises(StageError):
        stage_inputs("nope", req(), Config())
