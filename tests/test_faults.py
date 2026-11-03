import json

import pytest
from fastapi.testclient import TestClient

from demo.shopfront.api.main import create_app
from storm import faults


def test_bad_config_restores_file(tmp_path):
    p = tmp_path / "limits.yaml"
    original = "limits:\n  free_tier: 1000\n"
    p.write_text(original)
    seen = []
    faults.bad_config(p, 5, sleep=lambda s: seen.append(p.read_text()))
    assert "not-a-number" in seen[0]
    assert p.read_text() == original


def test_bad_config_restores_on_interrupt(tmp_path):
    p = tmp_path / "limits.yaml"
    original = "limits:\n  free_tier: 1000\n"
    p.write_text(original)

    def boom(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        faults.bad_config(p, 5, sleep=boom)
    assert p.read_text() == original


def test_start_and_clear_fault(monkeypatch):
    monkeypatch.setenv("SHOPFRONT_CHAOS", "1")
    c = TestClient(create_app())
    faults.start_fault(c, "kill", "api")
    assert c.get("/v1/users/1").status_code == 503
    faults.clear_faults(c)
    assert c.get("/v1/users/1").status_code == 200


def test_cli_prints_one_json_line_for_bad_config(tmp_path, capsys):
    p = tmp_path / "limits.yaml"
    p.write_text("limits:\n  free_tier: 1000\n")
    faults.main(["bad_config", "--seconds", "0", "--limits", str(p)])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    line = json.loads(out[0])
    assert set(line) == {"fault", "target", "started", "ends"}
    assert line["fault"] == "bad_config" and line["target"] == "api"
    assert "1000" in p.read_text()
