import re
from typing import Literal

from pydantic import BaseModel

Kind = Literal["approve", "reject", "pick", "storm", "not_conflict", "retry",
               "crew", "enlist", "retire", "connect", "projects", "use", "project_set",
               "handoff", "reclaim"]


class Command(BaseModel):
    kind: Kind
    arg: str | None = None
    tag: int | None = None
    user: str
    role: str | None = None
    args: list[str] = []


_EMOJI: dict[str, tuple[Kind, str | None]] = {
    "👍": ("approve", None),
    "✅": ("approve", None),
    "✔": ("approve", None),
    "👎": ("reject", None),
    "❌": ("reject", None),
    "1\u20e3": ("pick", "1"),
    "2\u20e3": ("pick", "2"),
    "3\u20e3": ("pick", "3"),
    "🌪": ("storm", "auto"),
    "🧪": ("storm", "drill"),
    "⏭": ("storm", "skip"),
    "🙅": ("not_conflict", None),
    "🤖": ("handoff", None),
    "🙋": ("reclaim", None),
}

_STRIP = re.compile("[\ufe0f\U0001f3fb-\U0001f3ff]|\u200d[\u2642\u2640]")
_TAG = re.compile(r"\[K-(\d+)\]")
_MENTION = re.compile(r"^@kraken\b[:,]?\s*", re.IGNORECASE)
_THUMBS_DOWN_TEXT = re.compile(r"^👎[️🏻-🏿]*\s+(.+)$", re.DOTALL)
_F = re.IGNORECASE
_APPROVE = re.compile(r"approve\s*[.!]?", _F)
_RETRY = re.compile(r"retry\s*[.!]?", _F)
_NOT_CONFLICT = re.compile(r"not\s+a\s+conflict\s*[.!]?", _F)
_REJECT = re.compile(r"reject(?:\s+(.*))?", _F | re.DOTALL)
_PICK = re.compile(r"pick\s+([\w.-]+?)\s*[.!]?", _F)
_STORM = re.compile(r"storm\s+(auto|drill|skip)\s*[.!]?", _F)
_CREW = re.compile(r"crew\s*[.!]?", _F)
_ENLIST = re.compile(
    r"enlist\s+([\w.-]+?)(?:\s+as\s+(bidder|planner|attacker|defender|coach))?\s*[.!]?", _F)
_RETIRE = re.compile(r"retire\s+([\w.-]+?)\s*[.!]?", _F)
# Project commands. Chat apps wrap pasted links as <url>; accept that form too.
_CONNECT = re.compile(r"connect\s+<?((?:https://|git@)\S+?)>?(?:\s+(?:as\s+)?([\w.-]+))?\s*", _F)
_PROJECTS = re.compile(r"projects\s*[.!?]?", _F)
_USE = re.compile(r"use\s+([\w.-]+?)\s*[.!]?", _F)
_PROJECT_SET = re.compile(r"project\s+set\s+([\w.-]+)\s+(\w+)\s+(.+)", _F | re.DOTALL)
# Human first: hand the current step to an agent, or take it back.
_HANDOFF = re.compile(r"(?:agent|take\s+over|agent,?\s+(?:do|take)\s+(?:it|this|over)"
                      r"|you\s+(?:do|decide|pick|defend)(?:\s+it)?)\s*[.!]?", _F)
_RECLAIM = re.compile(r"(?:me|i'?ll\s+do\s+it|i'?ll\s+(?:pick|defend|answer)|my\s+turn)\s*[.!]?", _F)


def _normalize(emoji: str) -> str:
    return _STRIP.sub("", emoji).strip()


def _emoji_command(emoji: str, user: str, tag: int | None) -> Command | None:
    hit = _EMOJI.get(_normalize(emoji))
    if hit is None:
        return None
    return Command(kind=hit[0], arg=hit[1], tag=tag, user=user)


def from_reaction(emoji: str, user: str, tag: int) -> Command | None:
    return _emoji_command(emoji, user, tag)


def parse(body: str, user: str) -> Command | None:
    m = _TAG.search(body)
    tag = int(m.group(1)) if m else None
    text = _MENTION.sub("", _TAG.sub("", body).strip()).strip()

    cmd = _emoji_command(text, user, tag)
    if cmd:
        return cmd
    m = _THUMBS_DOWN_TEXT.match(text)
    if m:
        return Command(kind="reject", arg=m.group(1).strip(), tag=tag, user=user)
    m = _APPROVE.fullmatch(text)
    if m:
        return Command(kind="approve", tag=tag, user=user)
    m = _REJECT.fullmatch(text)
    if m:
        return Command(kind="reject", arg=(m.group(1) or "").strip() or None, tag=tag, user=user)
    m = _PICK.fullmatch(text)
    if m:
        return Command(kind="pick", arg=m.group(1), tag=tag, user=user)
    m = _STORM.fullmatch(text)
    if m:
        return Command(kind="storm", arg=m.group(1).lower(), tag=tag, user=user)
    if _CREW.fullmatch(text):
        return Command(kind="crew", tag=tag, user=user)
    m = _ENLIST.fullmatch(text)
    if m:
        role = m.group(2).lower() if m.group(2) else None
        return Command(kind="enlist", arg=m.group(1), role=role, tag=tag, user=user)
    m = _RETIRE.fullmatch(text)
    if m:
        return Command(kind="retire", arg=m.group(1), tag=tag, user=user)
    m = _CONNECT.fullmatch(text)
    if m:
        return Command(kind="connect", arg=m.group(1), role=m.group(2), tag=tag, user=user)
    if _PROJECTS.fullmatch(text):
        return Command(kind="projects", tag=tag, user=user)
    m = _USE.fullmatch(text)
    if m:
        return Command(kind="use", arg=m.group(1), tag=tag, user=user)
    m = _PROJECT_SET.fullmatch(text)
    if m:
        return Command(kind="project_set", arg=m.group(1), args=[m.group(2), m.group(3).strip()],
                       tag=tag, user=user)
    if _NOT_CONFLICT.fullmatch(text):
        return Command(kind="not_conflict", tag=tag, user=user)
    if _HANDOFF.fullmatch(text):
        return Command(kind="handoff", tag=tag, user=user)
    if _RECLAIM.fullmatch(text):
        return Command(kind="reclaim", tag=tag, user=user)
    if _RETRY.fullmatch(text):
        return Command(kind="retry", tag=tag, user=user)
    return None


def prompt(tag: int, text: str, accepts: list[str]) -> str:
    return f"[K-{tag}] {text}\n" + " · ".join(accepts)
