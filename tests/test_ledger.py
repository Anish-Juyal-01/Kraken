import pytest

from kraken.hallmark.ledger import Ledger
from kraken.models import Claim


def mk(subject="auth", attribute="method", value="jwt", **kw):
    base = dict(subject=subject, attribute=attribute, value=value, speaker="sam",
                room_id="r1", message_id="m1", timestamp=1, confidence=0.9)
    base.update(kw)
    return Claim(**base)


def test_pair_is_unordered(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    L.mark_pair(3, 9, "raised")
    assert L.pair_seen(9, 3)
    assert not L.pair_seen(1, 2)


def test_mark_pair_upserts(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    a = L.add_claim(mk()); b = L.add_claim(mk(value="session"))
    L.mark_pair(a, b, "raised")
    L.mark_pair(b, a, "dismissed")
    assert L.dismissal_count("auth", "method") == 1
    L.mark_pair(a, b, "resolved")
    assert L.dismissal_count("auth", "method") == 0
    assert L.dismissal_count("auth", "other") == 0


def test_state_survives_reopen(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    rid = L.new_request("sam", "build reset", "r1")
    L.update_request(rid, state="CHARTER", bids=[{"agent": "codex-coder"}], data={"pair": [1, 2]})
    L.bind_room(rid, "charter", "room-c")
    L2 = Ledger(str(tmp_path / "k.db"))
    r = L2.get_request(rid)
    assert r.state == "CHARTER" and r.bids[0]["agent"] == "codex-coder"
    assert r.rooms == {"charter": "room-c"} and r.data == {"pair": [1, 2]}
    assert L2.request_for_room("room-c") == rid
    assert L2.request_for_room("nope") is None


def test_request_defaults_and_errors(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    r = L.get_request(L.new_request("sam", "t", "r1"))
    assert (r.state, r.storm_mode, r.storm_round, r.brief, r.bids, r.winner, r.rooms, r.data) == \
        ("SPARK", "auto", 0, None, [], None, {}, {})
    assert L.get_request(L.new_request("sam", "t", "r1", state="RESOLVING")).state == "RESOLVING"
    with pytest.raises(KeyError):
        L.get_request(999)
    with pytest.raises(ValueError):
        L.update_request(r.id, bogus=1)


def test_claims_for_filters_by_subject_and_attribute(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    i1 = L.add_claim(mk(path="a.py", line=3))
    L.add_claim(mk(subject="db"))
    L.add_claim(mk(attribute="expiry"))
    got = L.claims_for("auth", "method")
    assert [i for i, _ in got] == [i1] and got[0][1].path == "a.py" and got[0][1].line == 3
    assert L.get_claim(i1) == got[0][1]


def test_aliases_and_subjects(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    assert L.alias("login") is None
    L.set_alias("login", "auth")
    L.set_alias("login", "authentication")
    assert L.alias("login") == "authentication"
    L.add_claim(mk()); L.add_claim(mk(subject="db"))
    assert sorted(L.known_subjects()) == ["auth", "db"]


def test_decisions_supersede(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    d1 = L.add_decision(mk(value="jwt"), "sam", None)
    d2 = L.add_decision(mk(value="session"), "ann", d1)
    d3 = L.add_decision(mk(subject="db"), "ann", None)
    out = L.decisions()
    assert [d[0] for d in out] == [d3, d2]
    assert out[1][1].value == "session" and out[1][2] == "ann"


def test_meta_and_last_sequence_roundtrip(tmp_path):
    L = Ledger(str(tmp_path / "k.db"))
    assert L.last_sequence() is None and L.get_meta("x") is None
    L.set_last_sequence(41); L.set_last_sequence(42)
    L.set_meta("x", "y")
    L2 = Ledger(str(tmp_path / "k.db"))
    assert L2.last_sequence() == 42 and L2.get_meta("x") == "y"


def test_open_requests_excludes_closed(ledger):
    a = ledger.new_request("sam", "a", "r")
    b = ledger.new_request("sam", "b", "r")
    ledger.update_request(a, state="CLOSED")
    assert [r.id for r in ledger.open_requests()] == [b]
