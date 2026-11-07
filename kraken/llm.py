import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TypeVar

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic
    import httpx
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


# ---- any other model or agent -------------------------------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _extract_json(text: str) -> str:
    """The JSON in a reply that may carry extra lines (CLI banners, colour codes, prose)."""
    text = _strip_fence(_ANSI.sub("", text))
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = text.find(open_), text.rfind(close)
        if a != -1 and b > a:
            chunk = text[a:b + 1]
            try:
                json.loads(chunk)
                return chunk
            except json.JSONDecodeError:
                continue
    return text


class LLMError(RuntimeError):
    """A model API or agent CLI failed."""


class _AnyModel(_JsonLLM):
    """Kraken asks for Claude model names (FAST/SMART); other backends map them to their own."""

    def __init__(self, model: str | None, fast_model: str | None) -> None:
        self._smart = model
        self._fast = fast_model or model

    def _model(self, asked: str) -> str | None:
        if not asked.startswith("claude-"):
            return asked
        return self._fast if asked == FAST else self._smart

    async def _parse(self, model: str, system: str, user: str, schema: type[T]) -> T:
        return schema.model_validate_json(_extract_json(await self._ask(model, system, user)))


class OpenAICompatLLM(_AnyModel):
    """Any OpenAI-compatible chat API: OpenAI, Gemini, OpenRouter, Groq, DeepSeek, Mistral,
    Kimi, a local Ollama or LM Studio, and so on."""

    def __init__(self, base_url: str, api_key: str | None, model: str, fast_model: str | None = None,
                 client: "httpx.AsyncClient | None" = None) -> None:
        super().__init__(model, fast_model)
        if client is None:
            import httpx

            client = httpx.AsyncClient(timeout=CLI_TIMEOUT_S)
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client

    async def _ask(self, model: str, system: str, user: str) -> str:
        body = {"model": self._model(model), "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]}
        resp = await self._client.post(self._url, json=body, headers=self._headers)
        if resp.status_code >= 400:
            raise LLMError(f"{self._url} answered {resp.status_code}: {resp.text[:300]}")
        choice = resp.json()["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise _Incomplete(f"reply stopped early (finish_reason={choice.get('finish_reason')})")
        return choice["message"].get("content") or ""


# Coding-agent CLIs that read a prompt on stdin and print the reply. The prompt never goes
# on the command line, so Windows npm shims can't mangle it.
CLI_PRESETS: dict[str, list[str]] = {
    "codex": ["codex", "exec", "--skip-git-repo-check", "-"],
    "opencode": ["opencode", "run"],
    "gemini-cli": ["gemini", "-p", "Follow the instructions above and reply with JSON only."],
}


def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found and os.name == "nt":  # npm shim: prefer the native binary next to it
        mods = Path(found).parent / "node_modules"
        for exe in (mods / f"{name}-ai" / "bin" / f"{name}.exe", mods / name / "bin" / f"{name}.exe"):
            if exe.is_file():
                return str(exe)
    return found


class AgentCLILLM(_AnyModel):
    """Any coding agent's CLI in one-shot mode (Codex, OpenCode, Gemini CLI, or a command of
    your own). It runs in a fresh empty folder, so there are no project files to touch."""

    def __init__(self, argv: list[str], model: str | None = None, runner: Runner | None = None) -> None:
        super().__init__(model, None)
        if not argv:
            raise LLMError("empty agent command: set KRAKEN_LLM_CMD")
        exe = argv[0] if runner is not None else _find(argv[0])
        if not exe:
            raise LLMError(f"{argv[0]} not found on PATH; install it or set KRAKEN_LLM_CMD")
        self._argv = [exe, *argv[1:]]
        self._run = runner or _run_in_empty_dir

    async def _ask(self, model: str, system: str, user: str) -> str:
        prompt = (f"{system}\n\n---\n\n{user}\n\n---\nReply with the JSON object only: no tools, "
                  "no files, no explanation.")
        argv = list(self._argv)
        if any("{prompt}" in a for a in argv):
            argv, prompt = [a.replace("{prompt}", prompt) for a in argv], ""
        code, out, err = await self._run(argv, prompt)
        if code != 0 and not out.strip():
            raise LLMError(f"{argv[0]} exited {code}: {(err or out).strip()[:300]}")
        return out


async def _run_in_empty_dir(argv: list[str], stdin: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory(prefix="kraken-llm-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8")), CLI_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise LLMError(f"{argv[0]} timed out after {CLI_TIMEOUT_S}s") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
OPENAI_URL = "https://api.openai.com/v1"
BACKENDS = ("auto", "api", "claude-code", "openai", "gemini", "codex", "opencode", "gemini-cli", "cli")


def _auto(env) -> str:
    if env("ANTHROPIC_API_KEY"):
        return "api"
    if env("KRAKEN_LLM_BASE_URL") or env("KRAKEN_LLM_API_KEY") or env("OPENAI_API_KEY"):
        return "openai"
    if env("GEMINI_API_KEY"):
        return "gemini"
    if env("KRAKEN_LLM_CMD"):
        return "cli"
    if env("KRAKEN_CLAUDE_BIN") or find_claude_cli():
        return "claude-code"
    for name, cmd in (("codex", "codex"), ("opencode", "opencode"), ("gemini-cli", "gemini")):
        if shutil.which(cmd):
            return name
    raise LLMError("Kraken needs a model: set an API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                   "GEMINI_API_KEY or KRAKEN_LLM_BASE_URL) or install an agent CLI "
                   "(claude, codex, opencode, gemini). See the README.")


def make_llm() -> _JsonLLM:
    """Pick Kraken's own model from KRAKEN_LLM (README: "Kraken's own model").

    api          Anthropic API (ANTHROPIC_API_KEY)
    claude-code  your signed-in Claude Code CLI
    openai       any OpenAI-compatible API: KRAKEN_LLM_BASE_URL (default OpenAI),
                 KRAKEN_LLM_API_KEY (or OPENAI_API_KEY), KRAKEN_LLM_MODEL
    gemini       Google's Gemini API (GEMINI_API_KEY)
    codex, opencode, gemini-cli   that agent's signed-in CLI
    cli          any command: KRAKEN_LLM_CMD (prompt on stdin, or wherever `{prompt}` is)
    auto         (default) the first one set up: an API key, then an agent CLI on PATH
    """
    env = os.environ.get
    choice = (env("KRAKEN_LLM") or "auto").strip().lower()
    if choice not in BACKENDS:
        raise ValueError(f"KRAKEN_LLM must be one of {', '.join(BACKENDS)}, not {choice!r}")
    if choice == "auto":
        choice = _auto(env)
    model, fast = env("KRAKEN_LLM_MODEL"), env("KRAKEN_LLM_FAST_MODEL")
    if choice == "api":
        return ClaudeLLM()
    if choice == "claude-code":
        return ClaudeCodeLLM()
    if choice == "openai":
        return OpenAICompatLLM(env("KRAKEN_LLM_BASE_URL") or OPENAI_URL,
                               env("KRAKEN_LLM_API_KEY") or env("OPENAI_API_KEY"),
                               model or "gpt-4.1-mini", fast)
    if choice == "gemini":
        return OpenAICompatLLM(GEMINI_URL, env("GEMINI_API_KEY") or env("KRAKEN_LLM_API_KEY"),
                               model or "gemini-2.5-flash", fast)
    if choice == "cli":
        return AgentCLILLM(shlex.split(env("KRAKEN_LLM_CMD") or "", posix=os.name != "nt"), model)
    argv = list(CLI_PRESETS[choice])
    if model:
        argv += ["-m", model]
    return AgentCLILLM(argv, model)
