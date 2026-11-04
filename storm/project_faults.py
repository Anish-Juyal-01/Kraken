"""Faults for any project, driven by its project file (Kraken writes it into each Storm room).

HTTP mode (the project has a `run` command): Storm starts the app itself and attacks it from
the outside, through `storm.proxy` and plain requests, so the app needs no changes.
Library mode (no `run`): Storm runs the project's tests under a fault and compares the
result with a clean run. Every fault restores what it changed, even when interrupted.
"""

import asyncio
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx
import yaml

from storm.proxy import FaultProxy
from storm.target import Target, healthy

HTTP_FAULTS = {
    "kill": "stop the app hard, check it goes down, restart it and time the recovery",
    "latency": "add --ms of delay in front of the app and probe every endpoint",
    "drop": "drop --share of connections in front of the app and probe every endpoint",
    "bad_payload": "send malformed, wrong-typed and oversized input to every endpoint (5xx = bug)",
    "flood": "fire --count concurrent requests across the endpoints (5xx or errors = finding)",
    "bad_config": "corrupt each config file, restart the app, check its health, restore",
    "missing_env": "start the app with the variables from .env.example unset",
}
LIBRARY_FAULTS = {
    "bad_config": "run the tests with each config file corrupted; compare with a clean run",
    "missing_env": "run the tests with the variables from .env.example unset; compare with a clean run",
}
NATIVE = ("kill", "latency")  # with a chaos_endpoint, these use the app's own fault switch
_GARBAGE = "{{ storm: this is not valid config ::: [\n"
_OWN = ("kill", "bad_config", "missing_env")  # faults that need Storm to run the app itself


def load_project(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a project file")
    return data


def workdir(p: dict) -> Path:
    return Path(p.get("workdir") or ".")


def env_keys(p: dict) -> list[str]:
    example = workdir(p) / ".env.example"
    if not example.exists():
        return []
    keys = []
    for line in example.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].removeprefix("export ").strip())
    return keys


def available(p: dict) -> dict[str, str]:
    """The faults that fit this project, with what each does."""
    has_cfg, has_env = bool(p.get("config_files")), bool(env_keys(p))
    if p.get("run"):
        faults = dict(HTTP_FAULTS)
    elif p.get("test"):
        faults = dict(LIBRARY_FAULTS)
    else:
        return {}
    if not has_cfg:
        faults.pop("bad_config", None)
    if not has_env:
        faults.pop("missing_env", None)
    return faults


# ---- helpers -------------------------------------------------------------------
def _endpoints(p: dict) -> list[str]:
    return p.get("endpoints") or [p.get("health") or "/"]


def _probe(base: str, paths: list[str], timeout: float) -> dict:
    statuses: Counter = Counter()
    times = []
    for path in paths:
        began = time.monotonic()
        try:
            r = httpx.get(base.rstrip("/") + path, timeout=timeout)
            statuses[str(r.status_code)] += 1
        except httpx.TimeoutException:
            statuses["timeout"] += 1
        except httpx.HTTPError:
            statuses["error"] += 1
        times.append(round((time.monotonic() - began) * 1000))
    return {"statuses": dict(sorted(statuses.items())), "ms": times}


def _target(p: dict, env: dict | None = None) -> Target:
    return Target(p["run"], workdir(p), p["base_url"], p.get("health"), env=env)


def _tail(path: Path, n: int = 8) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _run_tests(p: dict, env: dict | None, timeout: float) -> dict:
    began = time.monotonic()
    try:
        done = subprocess.run(p["test"], shell=True, cwd=workdir(p), env=env, timeout=timeout,  # noqa: S602
                              capture_output=True, text=True, errors="replace")
        rc, out = done.returncode, (done.stdout + done.stderr).strip().splitlines()
    except subprocess.TimeoutExpired:
        rc, out = None, [f"timed out after {timeout:g}s"]
    return {"exit": rc, "seconds": round(time.monotonic() - began, 1), "tail": out[-6:]}


def _without(keys: list[str]) -> dict:
    return {k: v for k, v in os.environ.items() if k not in keys}


class _Swap:
    """Replace a file's contents for a while; always put the original back."""

    def __init__(self, path: Path, text: str) -> None:
        self.path, self.text, self.original = path, text, None

    def __enter__(self):
        self.original = self.path.read_bytes() if self.path.exists() else None
        self.path.write_text(self.text, encoding="utf-8")
        return self

    def __exit__(self, *exc):
        if self.original is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_bytes(self.original)


# ---- HTTP faults -----------------------------------------------------------------
def _kill(p: dict, t: Target, opts) -> dict:
    t.stop()
    down = not healthy(p["base_url"], p.get("health"), timeout=1)
    try:
        recovery = t.start()
        back = True
    except RuntimeError as err:
        recovery, back = None, False
        log = str(err)
    findings = [] if down else ["the app still answered after it was killed (another copy running?)"]
    if not back:
        findings.append(f"the app did not come back after a restart: {log}")
    elif recovery and recovery > 30:
        findings.append(f"recovery took {recovery}s")
    return {"down_after_kill": down, "recovered": back, "recovery_s": recovery, "findings": findings}


async def _through_proxy(p: dict, opts, **fault) -> dict:
    async with FaultProxy(p["base_url"], **fault) as proxy:
        paths = _endpoints(p) * max(1, opts.rounds)
        return await asyncio.to_thread(_probe, proxy.url, paths, opts.timeout)


def _latency(p: dict, t: Target, opts) -> dict:
    res = asyncio.run(_through_proxy(p, opts, latency_ms=opts.ms))
    slow = res["statuses"].get("timeout", 0)
    findings = [f"{slow} request(s) exceeded a {opts.timeout:g}s client timeout at +{opts.ms}ms"] if slow else []
    return {"added_ms": opts.ms, **res, "findings": findings}


def _drop(p: dict, t: Target, opts) -> dict:
    res = asyncio.run(_through_proxy(p, opts, drop=opts.share))
    bad = sum(v for k, v in res["statuses"].items() if k in ("error", "timeout") or k.startswith("5"))
    return {"dropped_share": opts.share, **res,
            "findings": [f"{bad} of {sum(res['statuses'].values())} requests failed with "
                         f"{opts.share:.0%} of connections dropped"] if bad else []}


_BAD_BODIES = [
    ("invalid json", b"{not json", "application/json"),
    ("wrong types", json.dumps({"id": [], "name": {"$gt": ""}, "count": "NaN", "email": 12}).encode(),
     "application/json"),
    ("empty body", b"", "application/json"),
    ("oversized", json.dumps({"blob": "A" * 1_000_000}).encode(), "application/json"),
    ("not json at all", "\x00\xff<script>alert(1)</script>".encode("latin-1"), "text/plain"),
]
_BAD_QUERY = "?q=%27%20OR%201%3D1--&limit=-1&page=999999999999&id=..%2F..%2Fetc%2Fpasswd"


def _bad_payload(p: dict, t: Target, opts) -> dict:
    base = p["base_url"].rstrip("/")
    seen: list[dict] = []
    findings = []
    with httpx.Client(timeout=opts.timeout * 5) as http:
        for path in _endpoints(p):
            for method in ("POST", "PUT", "PATCH"):
                for label, body, ctype in _BAD_BODIES:
                    try:
                        r = http.request(method, base + path, content=body,
                                         headers={"content-type": ctype})
                        status = r.status_code
                    except httpx.HTTPError as err:
                        status = type(err).__name__
                    seen.append({"path": path, "method": method, "input": label, "status": status})
                    if not isinstance(status, int) or status >= 500:
                        findings.append(f"{method} {path} with {label} -> {status}")
            try:
                status = http.get(base + path + _BAD_QUERY).status_code
            except httpx.HTTPError as err:
                status = type(err).__name__
            seen.append({"path": path, "method": "GET", "input": "hostile query", "status": status})
            if not isinstance(status, int) or status >= 500:
                findings.append(f"GET {path} with a hostile query -> {status}")
    summary = Counter(str(s["status"]) for s in seen)
    return {"requests": len(seen), "statuses": dict(sorted(summary.items())),
            "findings": findings[:20]}


async def _flood_async(p: dict, opts) -> dict:
    base = p["base_url"].rstrip("/")
    paths = _endpoints(p)
    limits = httpx.Limits(max_connections=min(opts.count, 200))
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as http:
        async def one(i: int):
            began = time.monotonic()
            try:
                r = await http.get(paths[i % len(paths)])
                return str(r.status_code), time.monotonic() - began
            except httpx.HTTPError:
                return "error", time.monotonic() - began
        results = await asyncio.gather(*(one(i) for i in range(opts.count)))
    hist = Counter(s for s, _ in results)
    lat = sorted(d for _, d in results)
    p95 = round(lat[int(len(lat) * 0.95) - 1] * 1000) if lat else None
    bad = sum(v for k, v in hist.items() if k == "error" or k.startswith("5"))
    return {"count": opts.count, "statuses": dict(sorted(hist.items())), "p95_ms": p95,
            "findings": [f"{bad} of {opts.count} requests failed under load"] if bad else []}


def _flood(p: dict, t: Target, opts) -> dict:
    return asyncio.run(_flood_async(p, opts))


def _bad_config_http(p: dict, t: Target, opts) -> dict:
    out, findings = [], []
    for rel in p.get("config_files", []):
        path = workdir(p) / rel
        if not path.exists():
            continue
        t.stop()
        with _Swap(path, _GARBAGE):
            try:
                t.start()
                started = True
            except RuntimeError:
                started = False
            out.append({"file": rel, "started_with_corrupt_config": started,
                        "log_tail": "" if started else _tail(t.log)})
            if started:
                findings.append(f"the app started and reports healthy with a corrupt {rel}: "
                                "check it validates config")
            t.stop()
        t.start()  # back on the original config
    return {"files": out, "findings": findings}


def _missing_env_http(p: dict, t: Target, opts) -> dict:
    keys = env_keys(p)
    t.stop()
    probe = _target(p, env=_without(keys))
    try:
        probe.start()
        started, log = True, ""
    except RuntimeError:
        started, log = False, _tail(probe.log)
    finally:
        probe.stop()
    t.start()
    findings = [] if started else [f"the app does not start without {', '.join(keys)}; "
                                   "check the error tells the operator what is missing"]
    return {"unset": keys, "started": started, "log_tail": log, "findings": findings}


# ---- library faults ---------------------------------------------------------------
def _bad_config_lib(p: dict, opts) -> dict:
    clean = _run_tests(p, None, opts.test_timeout)
    runs = []
    for rel in p.get("config_files", []):
        path = workdir(p) / rel
        if path.exists():
            with _Swap(path, _GARBAGE):
                runs.append({"file": rel, **_run_tests(p, None, opts.test_timeout)})
    findings = [f"tests {'pass' if r['exit'] == 0 else 'fail'} with a corrupt {r['file']}"
                for r in runs if r["exit"] != clean["exit"] or r["exit"] == 0]
    return {"clean": clean, "faulted": runs, "findings": findings}


def _missing_env_lib(p: dict, opts) -> dict:
    keys = env_keys(p)
    clean = _run_tests(p, None, opts.test_timeout)
    faulted = _run_tests(p, _without(keys), opts.test_timeout)
    findings = [] if faulted["exit"] == clean["exit"] else [
        f"tests go from exit {clean['exit']} to {faulted['exit']} without {', '.join(keys)}"]
    return {"unset": keys, "clean": clean, "faulted": faulted, "findings": findings}


# ---- entry point ---------------------------------------------------------------------
def run_fault(name: str, p: dict, opts) -> dict:
    faults = available(p)
    if name not in faults:
        raise SystemExit(f"{name} doesn't fit this project; try one of: {', '.join(faults) or 'none'}")
    began = time.time()
    if not p.get("run"):
        result = (_bad_config_lib if name == "bad_config" else _missing_env_lib)(p, opts)
        return {"fault": name, "mode": "library", "seconds": round(time.time() - began, 1), **result}
    if p.get("chaos_endpoint") and name in NATIVE:  # the app's own fault switch
        from storm.faults import clear_faults, start_fault  # noqa: PLC0415
        params = {"ms": opts.ms} if name == "latency" else {}
        with httpx.Client(base_url=p["base_url"], timeout=10) as client:
            start_fault(client, name, "api", params, path=p["chaos_endpoint"])
            try:
                result = {"during": _probe(p["base_url"], _endpoints(p), opts.timeout)}
                time.sleep(opts.seconds)
            finally:
                clear_faults(client, path=p["chaos_endpoint"])
        return {"fault": name, "mode": "native", **result, "findings": []}
    already = healthy(p["base_url"], p.get("health"))
    if already and (name in _OWN or not getattr(opts, "use_running", False)):
        # Live (K-6): a stale copy on the port made the first faults test old code.
        how = ("must run the app itself" if name in _OWN else
               "would test whatever is running there, which may not be the branch under test")
        raise SystemExit(f"something already answers at {p['base_url']}; {name} {how}. Stop that "
                         "copy so Storm starts the branch itself"
                         + ("" if name in _OWN else ", or pass --use-running to attack it on purpose"))
    t = _target(p)
    try:
        if not already:
            t.start()
        handler = {"kill": _kill, "latency": _latency, "drop": _drop, "bad_payload": _bad_payload,
                   "flood": _flood, "bad_config": _bad_config_http,
                   "missing_env": _missing_env_http}[name]
        result = handler(p, t, opts)
    finally:
        t.stop()
    return {"fault": name, "mode": "http", "seconds": round(time.time() - began, 1), **result}
