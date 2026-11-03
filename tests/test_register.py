import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_register():
    spec = importlib.util.spec_from_file_location("register", ROOT / "scripts" / "register.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kraken_prints_switch_env_names():
    lines = _load_register().env_lines("kraken", "a1", "k1")
    assert lines == ["SWITCH_AGENT_ID=a1", "SWITCH_API_KEY=k1"]


def test_other_name_prints_smoke_env_names():
    lines = _load_register().env_lines("kraken-smoke", "a2", "k2")
    assert lines[-2:] == ["SMOKE_AGENT_ID=a2", "SMOKE_API_KEY=k2"]
    assert lines[0].startswith("#") and "kraken-smoke" in lines[0]
    assert not any(ln.startswith("SWITCH_") for ln in lines)
