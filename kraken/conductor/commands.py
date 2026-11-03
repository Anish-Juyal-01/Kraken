import re
from typing import Literal

from pydantic import BaseModel

Kind = Literal["approve", "reject", "pick", "storm", "not_conflict", "retry"]


class Command(BaseModel):
    kind: Kind
    arg: str | None = None
    tag: int | None = None
    user: str


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
    if _NOT_CONFLICT.fullmatch(text):
        return Command(kind="not_conflict", tag=tag, user=user)
    if _RETRY.fullmatch(text):
        return Command(kind="retry", tag=tag, user=user)
    return None


def prompt(tag: int, text: str, accepts: list[str]) -> str:
    return f"[K-{tag}] {text}\n" + " · ".join(accepts)
