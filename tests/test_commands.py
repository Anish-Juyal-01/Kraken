import pytest

from kraken.conductor.commands import from_reaction, parse, prompt


@pytest.mark.parametrize("e", ["👍", "👍🏽", "👍️", " ✅ ", "✔️"])
def test_emoji_variants(e):
    assert parse(e, "u").kind == "approve"


def test_pick_number_and_name():
    assert parse("2️⃣", "u").arg == "2"
    assert parse("pick codex-coder", "u").arg == "codex-coder"


def test_pick_requires_arg_and_bare_digit_is_not_command():
    assert parse("pick", "u") is None
    assert parse("1", "u") is None


def test_reject_reason():
    assert parse("@kraken reject too risky", "u").arg == "too risky"
    assert parse("reject", "u").arg is None
    assert parse("👎", "u").arg is None
    c = parse("👎 too risky", "u")
    assert c.kind == "reject" and c.arg == "too risky"


def test_tag_read():
    assert parse("[K-12] 👍", "u").tag == 12
    assert parse("👍", "u").tag is None
    assert parse("approve [K-7]", "u").tag == 7


def test_mention_forms():
    assert parse("@Kraken: approve", "u").kind == "approve"
    assert parse("[K-3] @kraken, retry", "u").kind == "retry"


def test_storm_modes():
    assert parse("🧪", "u").arg == "drill"
    assert parse("🌪", "u").arg == "auto"
    assert parse("⏭", "u").arg == "skip"
    assert parse("storm skip", "u").arg == "skip"
    assert parse("storm later", "u") is None


def test_not_conflict_and_retry():
    assert parse("Not a conflict", "u").kind == "not_conflict"
    assert parse("🙅", "u").kind == "not_conflict"
    assert parse("RETRY", "u").kind == "retry"


def test_user_field():
    assert parse("approve", "bob").user == "bob"


def test_false_positives_rejected():
    for b in ["approve of this idea?", "retry later maybe", "pick up milk", "storm auto please"]:
        assert parse(b, "u") is None


def test_punctuation_and_newlines():
    assert parse("approve.", "u").kind == "approve"
    assert parse("approve\n", "u").kind == "approve"
    assert parse("pick 2!", "u").arg == "2"
    c = parse("reject\nbecause x", "u")
    assert c.kind == "reject" and c.arg == "because x"


def test_non_command():
    assert parse("sounds good, let me check", "u") is None
    assert parse("looks good 👍 but wait", "u") is None
    assert parse("👍 thanks", "u") is None


def test_from_reaction():
    c = from_reaction("3️⃣", "u", 5)
    assert (c.kind, c.arg, c.tag, c.user) == ("pick", "3", 5, "u")
    assert from_reaction("👍🏽", "u", 1).kind == "approve"
    assert from_reaction("🦑", "u", 1) is None


def test_prompt_format():
    assert prompt(12, "Brief ready.", ["👍 approve", "👎 reject"]) == "[K-12] Brief ready.\n👍 approve · 👎 reject"


def test_crew_command():
    c = parse("crew", "u")
    assert (c.kind, c.arg, c.role) == ("crew", None, None)
    assert parse("@Kraken: CREW!", "u").kind == "crew"
    assert parse("[K-3] crew", "u").kind == "crew"


def test_enlist_defaults_to_bidder_and_takes_role():
    c = parse("enlist kimi-coder", "u")
    assert (c.kind, c.arg, c.role) == ("enlist", "kimi-coder", None)
    c = parse("@kraken enlist Qwen.Coder as Coach.", "u")
    assert (c.kind, c.arg, c.role) == ("enlist", "Qwen.Coder", "coach")
    for role in ("bidder", "planner", "attacker", "defender", "coach"):
        assert parse(f"enlist x as {role}", "u").role == role


def test_retire_command():
    c = parse("@kraken retire kimi-coder", "u")
    assert (c.kind, c.arg) == ("retire", "kimi-coder")
    assert parse("RETIRE kimi-coder!", "u").arg == "kimi-coder"


def test_crew_false_positives():
    for b in ["enlist me in the army please", "crew is ready", "enlist", "retire",
              "enlist bob as wizard", "retire bob soon", "enlist a b", "the crew"]:
        assert parse(b, "u") is None, b
