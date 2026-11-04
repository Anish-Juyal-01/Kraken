import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TypeVar

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic
from pydantic import BaseModel, ValidationError

FAST = "claude-haiku-5-5"
SMART = "claude-sonnet-5-5"

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


class LLMParseError(Exception):
    """The model's reply failed schema validation twice."""


class LLM(Protocol):
    async def json(self, model: str, system: str, user: str, schema: type[T]) -> T: ...


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


class _Incomplete(Exception):
    """The reply did not end normally (stop_reason other than end_turn)."""


class _JsonLLM:
    """Shared JSON parsing with one corrective retry; subclasses supply `_ask`."""

    async def _ask(self, model: str, system: str, user: str) -> str:
        raise NotImplementedError

    async def _parse(self, model: str, system: str, user: str, schema: type[T]) -> T:
        return schema.model_validate_json(_strip_fence(await self._ask(model, system, user)))

    async def json(self, model: str, system: str, user: str, schema: type[T]) -> T:
        try:
            return await self._parse(model, system, user, schema)
        except (ValidationError, _Incomplete) as err:
            retry_user = (
                f"{user}\n\nYour previous reply failed validation: {err}. Reply with JSON only."
            )
        try:
            return await self._parse(model, system, retry_user, schema)
        except (ValidationError, _Incomplete) as err:
            raise LLMParseError(str(err)) from err


class ClaudeLLM(_JsonLLM):
    """Calls the Anthropic API directly (needs ANTHROPIC_API_KEY)."""

    def __init__(self, client: "anthropic.AsyncAnthropic | None" = None) -> None:
        if client is None:
            import anthropic  # only the API backend needs the SDK

            client = anthropic.AsyncAnthropic()
        self._client = client

    async def _ask(self, model: str, system: str, user: str) -> str:
        resp = await self._client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason != "end_turn":
            raise _Incomplete(f"reply stopped early (stop_reason={resp.stop_reason})")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


class ClaudeCodeError(RuntimeError):
    """The Claude Code CLI failed or returned an error result."""


Runner = Callable[[list[str], str], Awaitable[tuple[int, str, str]]]


def find_claude_cli() -> str | None:
    """Path to the Claude Code CLI: KRAKEN_CLAUDE_BIN, else `claude` on PATH.

    On Windows the npm shim is a .cmd batch file, which mangles arguments such as
    `%` and newlines; prefer the native claude.exe it wraps.
    """
    if override := os.environ.get("KRAKEN_CLAUDE_BIN"):
        return override
    found = shutil.which("claude")
    if found and os.name == "nt":
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.is_file():
            return str(exe)
    return found


async def _run(argv: list[str], stdin: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=tempfile.gettempdir())
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), CLI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCodeError(f"claude CLI timed out after {CLI_TIMEOUT_S}s") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


CLI_TIMEOUT_S = 180


class ClaudeCodeLLM(_JsonLLM):
    """Calls the signed-in Claude Code CLI in print mode: no API key, uses your plan.

    Each call is a fresh, tool-less, non-persisted session run from the temp dir,
    so no project files, MCP servers or tools are involved.
    """

    def __init__(self, cli: str | None = None, runner: Runner | None = None) -> None:
        self._cli = cli or find_claude_cli()
        if not self._cli:
            raise ClaudeCodeError("Claude Code CLI not found; install it or set KRAKEN_CLAUDE_BIN")
        self._run = runner or _run

    async def _ask(self, model: str, system: str, user: str) -> str:
        argv = [self._cli, "-p", "--model", model, "--system-prompt", system,
                "--tools", "", "--output-format", "json",
                "--no-session-persistence", "--strict-mcp-config"]
        code, out, err = await self._run(argv, user)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise ClaudeCodeError(f"claude CLI exited {code}: {(err or out).strip()[:300]}") from None
        if code != 0 or data.get("is_error") or data.get("subtype") != "success":
            raise ClaudeCodeError(f"claude CLI error ({data.get('subtype')}): {str(data.get('result'))[:300]}")
        if data.get("stop_reason") not in (None, "end_turn"):
            raise _Incomplete(f"reply stopped early (stop_reason={data.get('stop_reason')})")
        return data.get("result") or ""


def make_llm() -> _JsonLLM:
    """Pick the backend from KRAKEN_LLM: `api`, `claude-code`, or unset/`auto`.

    Auto uses the API when ANTHROPIC_API_KEY is set, else the Claude Code CLI.
    """
    choice = (os.environ.get("KRAKEN_LLM") or "auto").strip().lower()
    if choice == "api" or (choice == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        return ClaudeLLM()
    if choice in ("auto", "claude-code"):
        return ClaudeCodeLLM()
    raise ValueError(f"KRAKEN_LLM must be api, claude-code or auto, not {choice!r}")
