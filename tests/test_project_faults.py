"""Storm against apps with no chaos hooks: a real server started from its run command."""
import shutil
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from storm import faults as cli
from storm import project_faults as pf
from storm.proxy import free_port
from storm.target import Target, healthy

FIXTURE = Path(__file__).parent / "fixtures" / "plain_app"


def opts(**kw) -> Namespace:
    base = dict(ms=400, share=0.3, rounds=2, timeout=2.0, count=60, seconds=0, test_timeout=120)
    return Namespace(**{**base, **kw})


@pytest.fixture
def app_project(tmp_path):
    work = tmp_path / "plain_app"
    shutil.copytree(FIXTURE, work)
    port = free_port()
    return {"name": "plain", "workdir": str(work),
            "run": f'"{sys.executable}" -m uvicorn app:app --port {port} --log-level warning',
            "base_url": f"http://127.0.0.1:{port}", "health": "/health",
            "endpoints": ["/items/1"], "config_files": ["config/settings.yaml"]}


def test_available_depends_on_the_project(tmp_path):
    empty = str(tmp_path / "empty")
    assert set(pf.available({"run": "x", "config_files": ["a"], "workdir": empty})) == {
        "kill", "latency", "drop", "bad_payload", "flood", "bad_config"}
    assert pf.available({"run": "x", "workdir": empty}).keys().isdisjoint({"bad_config", "missing_env"})
    (tmp_path / ".env.example").write_text("API_KEY=\n# comment\nexport REGION=eu\n")
    lib = {"test": "pytest", "workdir": str(tmp_path), "config_files": ["c.yaml"]}
    assert set(pf.available(lib)) == {"bad_config", "missing_env"}
    assert pf.env_keys(lib) == ["API_KEY", "REGION"]
    assert pf.available({"name": "nothing"}) == {}


def test_target_starts_and_stops(app_project):
    p = app_project
    with Target(p["run"], Path(p["workdir"]), p["base_url"], p["health"]) as t:
        assert t.start() < 60 and healthy(p["base_url"], p["health"])
        t.stop()
        assert not healthy(p["base_url"], p["health"], timeout=1)


def test_bad_payload_finds_the_real_bug(app_project):
    r = pf.run_fault("bad_payload", app_project, opts())
    assert r["mode"] == "http" and r["requests"] > 10
    assert any("POST /items/1" in f and "500" in f for f in r["findings"])
    assert not healthy(app_project["base_url"], "/health", timeout=1)  # Storm stopped its copy


def test_kill_restarts_and_times_recovery(app_project):
    r = pf.run_fault("kill", app_project, opts())
    assert r["down_after_kill"] and r["recovered"] and r["recovery_s"] is not None


def test_latency_and_drop_through_the_proxy(app_project):
    slow = pf.run_fault("latency", app_project, opts(ms=300))
    assert min(slow["ms"]) >= 300 and slow["statuses"] == {"200": 2}
    dropped = pf.run_fault("drop", app_project, opts(share=1.0))
    assert "200" not in dropped["statuses"] and dropped["findings"]


def test_flood_reports_statuses(app_project):
    r = pf.run_fault("flood", app_project, opts(count=40))
    assert r["statuses"] == {"200": 40} and r["findings"] == []


def test_bad_config_restarts_and_restores(app_project):
    cfg = Path(app_project["workdir"]) / "config" / "settings.yaml"
    before = cfg.read_text()
    r = pf.run_fault("bad_config", app_project, opts())
    (f,) = r["files"]
    assert f["file"] == "config/settings.yaml" and f["started_with_corrupt_config"] is False
    assert cfg.read_text() == before


def test_own_faults_refuse_when_another_copy_is_running(app_project):
    p = app_project
    with Target(p["run"], Path(p["workdir"]), p["base_url"], p["health"]) as t:
        t.start()
        with pytest.raises(SystemExit, match="already answers"):
            pf.run_fault("kill", p, opts())
        # a copy already on the port may be stale code (live K-6): refuse unless asked
        with pytest.raises(SystemExit, match="--use-running"):
            pf.run_fault("flood", p, opts(count=10))
        assert pf.run_fault("flood", p, opts(count=10, use_running=True))["statuses"] == {"200": 10}


def test_library_mode_compares_with_a_clean_run(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("API_KEY=\n")
    (tmp_path / "settings.toml").write_text("x = 1\n")
    check = (f'"{sys.executable}" -c "import os,sys,tomllib;'
             "tomllib.load(open('settings.toml','rb'));sys.exit(0 if os.environ.get('API_KEY') else 3)\"")
    p = {"name": "lib", "workdir": str(tmp_path), "test": check, "config_files": ["settings.toml"]}
    monkeypatch.setenv("API_KEY", "k")
    env = pf.run_fault("missing_env", p, opts())
    assert env["mode"] == "library" and env["clean"]["exit"] == 0 and env["faulted"]["exit"] == 3
    assert env["findings"] and "API_KEY" in env["findings"][0]
    cfg = pf.run_fault("bad_config", p, opts())
    assert cfg["faulted"][0]["exit"] not in (0, None)
    assert (tmp_path / "settings.toml").read_text() == "x = 1\n"


def test_unfitting_fault_is_refused():
    with pytest.raises(SystemExit, match="doesn't fit"):
        pf.run_fault("kill", {"test": "pytest"}, opts())


def test_cli_list_and_project_file(app_project, tmp_path, capsys):
    import json
    f = tmp_path / "storm-project.json"
    f.write_text(json.dumps(app_project))
    cli.main(["--project", str(f), "--list"])
    out = capsys.readouterr().out
    assert "plain: web service" in out and "bad_payload" in out and "missing_env" not in out
    cli.main(["flood", "--project", str(f), "--count", "5"])
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["fault"] == "flood" and line["statuses"] == {"200": 5}


def test_cli_without_project_still_runs_the_demo_faults(capsys):
    cli.main(["--list"])
    assert "shopfront demo faults" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["missing_env"])  # project-only fault
