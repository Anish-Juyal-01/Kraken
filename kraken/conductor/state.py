from typing import Callable, Literal

from pydantic import BaseModel

from kraken.conductor.commands import Command, prompt
from kraken.config import Config
from kraken.hallmark.ledger import Request

State = Literal["SPARK", "PREFLIGHT", "CHARTER", "FORGE", "STORM", "CLOSED", "RESOLVING"]

_APPROVE_REJECT = ["\U0001f44d approve", "\U0001f44e reject"]
_KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
            "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
_GATE_KINDS = {"approve", "reject", "pick"}
_STAGE_ROOMS = {"RESOLVING": "sonar-resolution"}
_RETRYABLE = ("SPARK", "CHARTER", "FORGE", "STORM")
_NO_REASON = "no reason given"


class Signal(BaseModel):
    kind: Literal["brief_posted", "preflight_clear", "preflight_conflicts", "bid", "bids_done",
                  "accepted", "survived", "failed", "resolved", "timeout"]
    data: dict = {}


class Action(BaseModel):
    kind: Literal["open_stage", "post", "ping_owner", "publish_hallmark", "close"]
    stage: str | None = None
    room: Literal["origin", "stage"] = "stage"
    text: str | None = None


Result = tuple[dict, list[Action]]


def stage_room(state: str) -> str:
    return _STAGE_ROOMS.get(state, state.lower())


def _post(text: str, stage: str | None = None) -> Action:
    return Action(kind="post", stage=stage, text=text)


def _open(stage: str) -> Action:
    return Action(kind="open_stage", stage=stage)


def _close(req: Request) -> Action:
    return Action(kind="close", room="origin", text=f"[K-{req.id}] closed")


def _ping(req: Request, reason: str) -> Action:
    return Action(kind="ping_owner", room="origin", text=f"[K-{req.id}] needs you: {reason}")


def _stale(req: Request) -> Result:
    return {}, [_post(f"[K-{req.id}] is already past that step")]


def _data(req: Request, gate: str | None = None) -> dict:
    d = dict(req.data)
    d.pop("gate", None)
    if gate:
        d["gate"] = gate
    return d


def _arg(inp: Signal | Command) -> str:
    return (inp.arg or "") if isinstance(inp, Command) else ""


def _sig(inp: Signal | Command) -> dict:
    return inp.data if isinstance(inp, Signal) else {}


def _ask(req: Request, gate: str, text: str, accepts: list[str], extra: dict | None = None) -> Result:
    return {**(extra or {}), "data": _data(req, gate)}, [_post(prompt(req.id, text, accepts))]


# SPARK
def _brief_posted(req, cfg, inp):
    brief = _sig(inp)["brief"]
    if cfg.autonomy["brief"] == "human":
        return _ask(req, "brief", "Brief ready.", _APPROVE_REJECT, {"brief": brief})
    return {"brief": brief, "state": "PREFLIGHT"}, [_post("running Sonar pre-flight")]


def _brief_approved(req, cfg, inp):
    return {"state": "PREFLIGHT", "data": _data(req)}, [_post("running Sonar pre-flight")]


def _brief_rejected(req, cfg, inp):
    reason = _arg(inp) or _NO_REASON
    return {"data": _data(req)}, [_post(f"Revise the brief: {reason}", "spark")]


# PREFLIGHT
def _to_charter(req, cfg, inp):
    return {"state": "CHARTER", "data": _data(req)}, [_open("charter")]


def _preflight_conflicts(req, cfg, inp):
    posts = [_post(c) for c in _sig(inp).get("conflicts", [])]
    upd, ask = _ask(req, "preflight", "Resolve the conflicts above, then approve.",
                    ["\U0001f44d approve"])
    return upd, posts + ask


# CHARTER
def _bid(req, cfg, inp):
    return {"bids": [*req.bids, dict(_sig(inp))]}, []


def _to_forge(req, winner, pick: dict | None = None):
    data = _data(req)
    if pick is not None:
        data["pick"] = pick  # how an auto pick was made, so it can be audited
    return {"winner": winner, "state": "FORGE", "data": data}, [_open("forge")]


def _bids_closed(req, cfg, inp):
    if not req.bids:
        return {}, [_ping(req, "no bids received")]
    if cfg.autonomy["pick"] == "human":
        accepts = [f"{_KEYCAPS[i]} {b['agent']}" for i, b in enumerate(req.bids[:len(_KEYCAPS)])]
        upd, posts = _ask(req, "pick", "Bids are in. Pick one.", accepts)
        if len(req.bids) > len(_KEYCAPS):
            posts = [_post(posts[0].text + "\n…or reply pick <name>")]
        return upd, posts
    agents = [b["agent"] for b in req.bids]
    sig = _sig(inp)
    judged = sig.get("judged_winner")
    fallback = judged not in agents
    pick = {"judged": judged, "reason": sig.get("pick_reason", ""), "order": agents,
            "fallback_to_first_bid": fallback}
    return _to_forge(req, agents[0] if fallback else judged, pick)


def _pick(req, cfg, inp):
    arg = _arg(inp).strip()
    agents = [b["agent"] for b in req.bids]
    if arg.isdigit() and 1 <= int(arg) <= len(agents):
        return _to_forge(req, agents[int(arg) - 1])
    if arg in agents:
        return _to_forge(req, arg)
    return {}, [_post(f"[K-{req.id}] no bid matches '{arg}'")]


# FORGE
def _proceed_after_merge(req):
    if req.storm_mode == "skip":
        return {"state": "CLOSED", "data": _data(req)}, [_close(req)]
    return {"state": "STORM", "data": _data(req)}, [_open("storm")]


def _accepted(req, cfg, inp):
    if cfg.autonomy["merge"] == "human":
        return _ask(req, "merge", "Reviewers accepted. Merge?", _APPROVE_REJECT)
    return _proceed_after_merge(req)


def _merge_approved(req, cfg, inp):
    return _proceed_after_merge(req)


def _merge_rejected(req, cfg, inp):
    reason = _arg(inp) or _NO_REASON
    return {"data": _data(req)}, [_post(f"Changes requested: {reason}", "forge")]


# STORM
def _publish(req):
    return ({"state": "CLOSED", "data": _data(req)},
            [Action(kind="publish_hallmark", room="origin"), _close(req)])


def _survived(req, cfg, inp):
    if cfg.autonomy["storm_result"] == "human":
        return _ask(req, "storm_result", "Storm survived. Accept?", _APPROVE_REJECT)
    return _publish(req)


def _storm_approved(req, cfg, inp):
    return _publish(req)


def _storm_failed(req, cfg, inp):
    n = req.storm_round + 1
    findings = _sig(inp).get("findings", "") if isinstance(inp, Signal) else _arg(inp)
    if n < cfg.storm.max_rounds:
        return ({"storm_round": n, "state": "FORGE", "data": _data(req)},
                [_post(f"Storm round {n} failed:\n{findings}", "forge")])
    return ({"storm_round": n, "data": _data(req)},
            [_ping(req, f"storm failed {n} times")])


# any state
def _storm_cmd(req, cfg, inp):
    mode = _arg(inp)
    return {"storm_mode": mode}, [_post(f"Storm mode set to {mode}")]


def _retry(req, cfg, inp):
    return {}, [_open(stage_room(req.state))]


def _timeout_ping(req, cfg, inp):
    return {}, [_ping(req, f"{req.state.lower()} timed out")]


def _resolved(req, cfg, inp):
    return ({"brief": _sig(inp)["brief"], "state": "CHARTER", "data": _data(req)},
            [_open("charter")])


def _not_conflict(req, cfg, inp):
    return {"state": "CLOSED", "data": _data(req)}, [_close(req)]


Handler = Callable[[Request, Config, "Signal | Command"], Result]

# (state, kind) -> (gate the input answers or None, handler)
_TABLE: dict[tuple[str, str], tuple[str | None, Handler]] = {
    ("SPARK", "brief_posted"): (None, _brief_posted),
    ("SPARK", "approve"): ("brief", _brief_approved),
    ("SPARK", "reject"): ("brief", _brief_rejected),
    ("PREFLIGHT", "preflight_clear"): (None, _to_charter),
    ("PREFLIGHT", "preflight_conflicts"): (None, _preflight_conflicts),
    ("PREFLIGHT", "approve"): ("preflight", _to_charter),
    ("CHARTER", "bid"): (None, _bid),
    ("CHARTER", "bids_done"): (None, _bids_closed),
    ("CHARTER", "timeout"): (None, _bids_closed),
    ("CHARTER", "pick"): ("pick", _pick),
    ("FORGE", "accepted"): (None, _accepted),
    ("FORGE", "approve"): ("merge", _merge_approved),
    ("FORGE", "reject"): ("merge", _merge_rejected),
    ("STORM", "survived"): (None, _survived),
    ("STORM", "approve"): ("storm_result", _storm_approved),
    ("STORM", "failed"): (None, _storm_failed),
    ("STORM", "reject"): ("storm_result", _storm_failed),
    ("RESOLVING", "resolved"): (None, _resolved),
    ("RESOLVING", "not_conflict"): (None, _not_conflict),
}
for _s in ("SPARK", "PREFLIGHT", "CHARTER", "FORGE", "STORM", "RESOLVING"):
    _TABLE[(_s, "storm")] = (None, _storm_cmd)
for _s in _RETRYABLE:
    _TABLE[(_s, "retry")] = (None, _retry)
for _s in ("SPARK", "FORGE", "STORM"):
    _TABLE[(_s, "timeout")] = (None, _timeout_ping)


def step(req: Request, cfg: Config, inp: Signal | Command) -> Result:
    """Pure transition: returns (ledger field updates, actions). Never mutates `req`."""
    if req.state == "CLOSED":
        return {}, []
    if isinstance(inp, Command) and inp.tag is not None and inp.tag != req.id:
        return _stale(req)
    entry = _TABLE.get((req.state, inp.kind))
    if inp.kind in _GATE_KINDS and (entry is None or req.data.get("gate") != entry[0]):
        return _stale(req)
    if entry is None:
        return {}, []
    return entry[1](req, cfg, inp)
