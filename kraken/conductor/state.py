from typing import Callable, Literal

from pydantic import BaseModel

from kraken.conductor.commands import Command, prompt
from kraken.config import Config
from kraken.hallmark.ledger import Request

State = Literal["SPARK", "PREFLIGHT", "CHARTER", "FORGE", "STORM", "DELIVER", "CLOSED",
                "RESOLVING"]

_APPROVE_REJECT = ["\U0001f44d approve", "\U0001f44e reject"]
_KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
            "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
_GATE_KINDS = {"approve", "reject", "pick"}
_STAGE_ROOMS = {"RESOLVING": "sonar-resolution"}
_RETRYABLE = ("SPARK", "CHARTER", "FORGE", "STORM")
_NO_REASON = "no reason given"
_PR_QUESTION = "Storm survived. Open a pull request? (No time limit: a person decides.)"


class Signal(BaseModel):
    kind: Literal["brief_posted", "preflight_clear", "preflight_conflicts", "bid", "bids_done",
                  "accepted", "survived", "failed", "resolved", "timeout", "delivered",
                  "deliver_failed"]
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


def _yours(cfg: Config, text: str) -> str:
    """A human gate's question, saying the person decides first and for how long."""
    if cfg.human_wait_s <= 0:
        return f"{text} ⏳ Your call."
    mins = max(1, round(cfg.human_wait_s / 60))
    return (f"{text} ⏳ Who's handling this? Reply 🙋 or `me` to do it yourself (no time limit), "
            f"or 🤖 to give it to the agent. No answer in {mins} min: the agent takes it.")


def _ask(req: Request, gate: str, text: str, accepts: list[str], extra: dict | None = None) -> Result:
    return {**(extra or {}), "data": _data(req, gate)}, [_post(prompt(req.id, text, accepts))]


# SPARK
def _brief_posted(req, cfg, inp):
    brief = _sig(inp)["brief"]
    if cfg.autonomy["brief"] == "human":
        return _ask(req, "brief", _yours(cfg, "Brief ready."), _APPROVE_REJECT, {"brief": brief})
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
        upd, posts = _ask(req, "pick", _yours(cfg, "Bids are in. Pick one."), accepts)
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
    if req.data.get("gate") == "merge":
        return {}, []  # a second reviewer's ACCEPTED: the merge question is already out (live K-7)
    if cfg.autonomy["merge"] == "human":
        return _ask(req, "merge", _yours(cfg, "Reviewers accepted. Merge?"), _APPROVE_REJECT)
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


def _after_storm(req, cfg):
    if cfg.deliver != "pr":
        return _publish(req)
    # Pushing reaches outside the team's chat, so a person always approves it,
    # whatever the autonomy settings say. Asked in the Storm room, where people already are,
    # so a plain "approve" there finds the request.
    return ({"state": "DELIVER", "data": _data(req, "deliver")},
            [_post(prompt(req.id, _PR_QUESTION, _APPROVE_REJECT), "storm")])


def _survived(req, cfg, inp):
    if cfg.autonomy["storm_result"] == "human":
        return _ask(req, "storm_result", _yours(cfg, "Storm survived. Accept?"), _APPROVE_REJECT)
    return _after_storm(req, cfg)


def _storm_approved(req, cfg, inp):
    return _after_storm(req, cfg)


# DELIVER
def _deliver_ask(req, cfg):
    # Storm's defender commits its fixes in its own clone (live, K-5): bring them in first.
    return (f"@{req.winner} [K-{req.id}] approved. First bring in any fixes from the Storm round: "
            f"if {cfg.agents.defender} committed on kraken/K-{req.id} (the Storm scorecard names the "
            "commits and the clone), fetch them from its clone and merge them, then rerun the tests. "
            f"Then push the branch kraken/K-{req.id} and open a "
            "pull request against the project's base branch. Give it a clear title and a body "
            "with the brief, what the reviewers checked and the Storm scorecard (for example "
            f"`gh pr create --base <base branch> --head kraken/K-{req.id} --title ... --body-file ...`). "
            "Then reply with `PR: <url>` on its own line, or `PR FAILED: <reason>` if you can't.")


def _deliver_approved(req, cfg, inp):
    return {"data": {**_data(req), "delivering": True}}, [_post(_deliver_ask(req, cfg), "forge")]


def _deliver_declined(req, cfg, inp):
    upd, actions = _publish(req)
    note = _post(f"[K-{req.id}] no pull request: the work stays on kraken/K-{req.id} in "
                 f"{req.winner}'s clone")
    note.room = "origin"
    return upd, [note, *actions]


def _delivered(req, cfg, inp):
    url = _sig(inp).get("url", "")
    upd = {"state": "CLOSED", "data": {**_data(req), "pr": url}}
    return upd, [Action(kind="publish_hallmark", room="origin"),
                 Action(kind="close", room="origin", text=f"[K-{req.id}] pull request: {url}")]


def _deliver_failed(req, cfg, inp):
    reason = _sig(inp).get("reason") or _NO_REASON
    return {"data": _data(req)}, [_ping(req, f"the pull request failed: {reason} — reply retry")]


def _deliver_retry(req, cfg, inp):
    if req.data.get("delivering"):
        return {}, [_post(_deliver_ask(req, cfg), "forge")]
    return {}, [_post(prompt(req.id, _PR_QUESTION, _APPROVE_REJECT), "storm")]


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
    ("DELIVER", "approve"): ("deliver", _deliver_approved),
    ("DELIVER", "reject"): ("deliver", _deliver_declined),
    ("DELIVER", "delivered"): (None, _delivered),
    ("DELIVER", "deliver_failed"): (None, _deliver_failed),
    ("DELIVER", "retry"): (None, _deliver_retry),
    ("RESOLVING", "resolved"): (None, _resolved),
    ("RESOLVING", "not_conflict"): (None, _not_conflict),
}
for _s in ("SPARK", "PREFLIGHT", "CHARTER", "FORGE", "STORM", "DELIVER", "RESOLVING"):
    _TABLE[(_s, "storm")] = (None, _storm_cmd)
for _s in _RETRYABLE:
    _TABLE[(_s, "retry")] = (None, _retry)
for _s in ("SPARK", "FORGE", "STORM", "DELIVER"):
    _TABLE[(_s, "timeout")] = (None, _timeout_ping)


# Which autonomy gate each stage's human part is. Handing a stage to an agent (🤖) makes
# that gate auto for this request only.
STAGE_GATE = {"SPARK": "brief", "PREFLIGHT": "brief", "CHARTER": "pick", "FORGE": "merge",
              "STORM": "storm_result"}


def effective(cfg: Config, req: Request) -> Config:
    """The config for this request: kraken.yaml, plus the steps its people handed off."""
    handed = [s for s in req.data.get("handoff", []) if s in STAGE_GATE]
    if not handed:
        return cfg
    return cfg.model_copy(update={"autonomy": {**cfg.autonomy,
                                               **{STAGE_GATE[s]: "auto" for s in handed}}})


def step(req: Request, cfg: Config, inp: Signal | Command) -> Result:
    """Pure transition: returns (ledger field updates, actions). Never mutates `req`."""
    if req.state == "CLOSED":
        return {}, []
    cfg = effective(cfg, req)
    if isinstance(inp, Command) and inp.tag is not None and inp.tag != req.id:
        return _stale(req)
    entry = _TABLE.get((req.state, inp.kind))
    if inp.kind in _GATE_KINDS and (entry is None or req.data.get("gate") != entry[0]):
        return _stale(req)
    if entry is None:
        return {}, []
    return entry[1](req, cfg, inp)
