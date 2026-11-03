import time

import pytest
from fastapi.testclient import TestClient

from demo.shopfront.api.main import create_app
from kraken.sonar.code_check import index_code


@pytest.fixture
def limits(tmp_path):
    p = tmp_path / "limits.yaml"
    p.write_text("limits:\n  free_tier: 2\n")
    return p


def test_limits_read_from_yaml(limits):
    c = TestClient(create_app(limits))
    assert c.get("/v2/users/1").status_code == 200
    assert c.get("/v2/users/1").status_code == 200
    assert c.get("/v2/users/1").status_code == 429
    limits.write_text("limits:\n  free_tier: 50\n")  # live reload
    assert c.get("/v2/users/1").status_code == 200


def test_chaos_endpoint_absent_without_flag(monkeypatch):
    monkeypatch.delenv("SHOPFRONT_CHAOS", raising=False)
    c = TestClient(create_app())
    assert c.post("/__chaos", json={"fault": "kill"}).status_code == 404
    assert c.delete("/__chaos").status_code == 404


def test_latency_fault_delays_response(monkeypatch):
    monkeypatch.setenv("SHOPFRONT_CHAOS", "1")
    c = TestClient(create_app())
    assert c.post("/__chaos", json={"fault": "latency", "params": {"ms": 300}}).status_code == 200
    t = time.perf_counter()
    c.get("/v1/users/1")
    assert time.perf_counter() - t >= 0.3
    assert c.delete("/__chaos").status_code == 200
    t = time.perf_counter()
    c.get("/v1/users/1")
    assert time.perf_counter() - t < 0.2


def test_kill_api_returns_503(monkeypatch):
    monkeypatch.setenv("SHOPFRONT_CHAOS", "1")
    c = TestClient(create_app())
    c.post("/__chaos", json={"fault": "kill", "target": "api"})
    assert c.get("/v2/users/1").status_code == 503
    c.delete("/__chaos")
    assert c.get("/v2/users/1").status_code == 200


def test_kill_email_makes_reset_502(monkeypatch):
    monkeypatch.setenv("SHOPFRONT_CHAOS", "1")
    c = TestClient(create_app())
    c.post("/__chaos", json={"fault": "kill", "target": "email"})
    r = c.post("/auth/reset")
    assert r.status_code == 502
    assert r.json() == {"error": "email service unavailable"}


def test_reset_502_when_email_unreachable(monkeypatch):
    monkeypatch.setenv("EMAIL_URL", "http://127.0.0.1:1/send")
    r = TestClient(create_app()).post("/auth/reset")
    assert r.status_code == 502


def test_bad_payload_truncates_v2_only(monkeypatch):
    monkeypatch.setenv("SHOPFRONT_CHAOS", "1")
    c = TestClient(create_app())
    c.post("/__chaos", json={"fault": "bad_payload"})
    r = c.get("/v2/users/1")
    assert r.status_code == 200
    with pytest.raises(ValueError):
        r.json()
    assert c.get("/v1/users/1").json()["user_id"] == 1


def test_index_code_on_shopfront_finds_seeded_drift():
    idx = index_code(["demo/shopfront"])
    assert any(c.subject == "free_tier" and c.value == "1000" for c in idx)
    assert any(c.subject == "user_id" and c.attribute == "type" and c.value == "integer"
               for c in idx)
