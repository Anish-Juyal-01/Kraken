import json

import pytest
from pydantic import BaseModel

import kraken.llm as llm_mod
from kraken.llm import FAST, SMART, ClaudeCodeError, ClaudeCodeLLM, ClaudeLLM, LLMParseError
from tests.fakes import FakeLLM


class OkModel(BaseModel):
    ok: bool


async def test_retry_once_then_succeed(stub_client):
    stub_client.replies = ["not json", '{"ok": true}']
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is True and len(stub_client.requests) == 2
    assert "ValidationError" in stub_client.requests[1]["user"] or "validation" in stub_client.requests[1]["user"].lower()


async def test_two_failures_raise(stub_client):
    stub_client.replies = ["x", "y"]
    with pytest.raises(LLMParseError):
        await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert len(stub_client.requests) == 2


async def test_first_try_and_request_shape(stub_client):
    stub_client.replies = ['{"ok": false}']
    out = await ClaudeLLM(client=stub_client).json(SMART, "sys", "usr", OkModel)
    assert out.ok is False
    req = stub_client.requests[0]
    assert req["model"] == SMART and req["system"] == "sys" and req["max_tokens"] == 4096
    assert req["messages"] == [{"role": "user", "content": "usr"}]


async def test_strips_code_fence(stub_client):
    stub_client.replies = ['```json\n{"ok": true}\n```']
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is True and len(stub_client.requests) == 1


async def test_fake_llm():
    fake = FakeLLM([OkModel(ok=True), '{"ok": false}', LLMParseError()])
    assert (await fake.json(FAST, "s", "u", OkModel)).ok is True
    assert (await fake.json(FAST, "s2", "u2", OkModel)).ok is False
    with pytest.raises(LLMParseError):
        await fake.json(FAST, "s3", "u3", OkModel)
    assert fake.calls[1] == {"model": FAST, "system": "s2", "user": "u2", "schema": OkModel}


async def test_truncated_reply_is_retried(stub_client):
    stub_client.replies = ['{"ok": true}', '{"ok": false}']
    stub_client.stop_reasons = ["max_tokens", "end_turn"]
    out = await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert out.ok is False and len(stub_client.requests) == 2


async def test_two_bad_stop_reasons_raise(stub_client):
    stub_client.replies = ['{"ok": true}', '{"ok": true}']
    stub_client.stop_reasons = ["max_tokens", "refusal"]
    with pytest.raises(LLMParseError):
        await ClaudeLLM(client=stub_client).json(FAST, "s", "u", OkModel)
    assert len(stub_client.requests) == 2
    assert set(stub_client.requests[0]) == {"model", "max_tokens", "system", "messages", "user"}


def cli_reply(result: str, **over) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "stop_reason": "end_turn", "result": result, **over})


class FakeRunner:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], str]] = []

    async def __call__(self, argv, stdin):
        self.calls.append((argv, stdin))
        return self.outputs.pop(0)


async def test_claude_code_request_shape():
    run = FakeRunner((0, cli_reply('{"ok": true}'), ""))
    out = await ClaudeCodeLLM(cli="claude", runner=run).json(SMART, "sys 100% & \"q\"", "usr", OkModel)
    assert out.ok is True
    argv, stdin = run.calls[0]
    assert stdin == "usr"  # the prompt goes on stdin, not the command line
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--model") + 1] == SMART
    assert argv[argv.index("--system-prompt") + 1] == "sys 100% & \"q\""
    assert argv[argv.index("--tools") + 1] == ""  # no tools: a plain completion
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv


async def test_claude_code_retries_bad_json_then_raises():
    run = FakeRunner((0, cli_reply("nope"), ""), (0, cli_reply('{"ok": false}'), ""))
    assert (await ClaudeCodeLLM(cli="c", runner=run).json(FAST, "s", "u", OkModel)).ok is False
    assert "validation" in run.calls[1][1].lower()
    run = FakeRunner((0, cli_reply("x"), ""), (0, cli_reply("y"), ""))
    with pytest.raises(LLMParseError):
        await ClaudeCodeLLM(cli="c", runner=run).json(FAST, "s", "u", OkModel)


async def test_claude_code_truncation_is_retried():
    run = FakeRunner((0, cli_reply('{"ok": true}', stop_reason="max_tokens"), ""),
                     (0, cli_reply('{"ok": false}'), ""))
    assert (await ClaudeCodeLLM(cli="c", runner=run).json(FAST, "s", "u", OkModel)).ok is False


@pytest.mark.parametrize("output", [
    (1, "", "Not logged in"),
    (0, "garbage", ""),
    (1, cli_reply("Credit balance too low", subtype="error_during_execution", is_error=True), ""),
])
async def test_claude_code_cli_failures_raise(output):
    with pytest.raises(ClaudeCodeError):
        await ClaudeCodeLLM(cli="c", runner=FakeRunner(output)).json(FAST, "s", "u", OkModel)


def test_claude_code_without_cli_raises(monkeypatch):
    monkeypatch.delenv("KRAKEN_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: None)
    with pytest.raises(ClaudeCodeError):
        ClaudeCodeLLM()


def test_find_cli_prefers_env_override(monkeypatch):
    monkeypatch.setenv("KRAKEN_CLAUDE_BIN", "/opt/claude")
    assert llm_mod.find_claude_cli() == "/opt/claude"


def test_find_cli_uses_native_exe_behind_windows_shim(monkeypatch, tmp_path):
    monkeypatch.delenv("KRAKEN_CLAUDE_BIN", raising=False)
    exe = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(llm_mod.os, "name", "nt")
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _: str(tmp_path / "claude.cmd"))
    assert llm_mod.find_claude_cli() == str(exe)


_LLM_ENV = ("KRAKEN_LLM", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "KRAKEN_LLM_BASE_URL",
            "KRAKEN_LLM_API_KEY", "KRAKEN_LLM_CMD", "KRAKEN_LLM_MODEL", "KRAKEN_LLM_FAST_MODEL")


@pytest.fixture
def clean_env(monkeypatch):
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("choice, key, want", [
    (None, "sk-test", ClaudeLLM),
    (None, None, ClaudeCodeLLM),
    ("auto", "", ClaudeCodeLLM),
    ("claude-code", "sk-test", ClaudeCodeLLM),
    ("api", None, ClaudeLLM),
])
def test_make_llm_picks_backend(clean_env, choice, key, want):
    for name, val in (("KRAKEN_LLM", choice), ("ANTHROPIC_API_KEY", key)):
        if val is not None:
            clean_env.setenv(name, val)
    clean_env.setenv("KRAKEN_CLAUDE_BIN", "claude")
    assert isinstance(llm_mod.make_llm(), want)


@pytest.mark.parametrize("env, url, model", [
    ({"OPENAI_API_KEY": "k"}, "https://api.openai.com/v1/chat/completions", "gpt-4.1-mini"),
    ({"GEMINI_API_KEY": "k"}, llm_mod.GEMINI_URL + "/chat/completions", "gemini-2.5-flash"),
    ({"KRAKEN_LLM_BASE_URL": "http://localhost:11434/v1", "KRAKEN_LLM_MODEL": "llama3.1"},
     "http://localhost:11434/v1/chat/completions", "llama3.1"),
])
def test_auto_picks_any_api_without_claude(clean_env, env, url, model):
    clean_env.delenv("KRAKEN_CLAUDE_BIN", raising=False)
    clean_env.setattr(llm_mod.shutil, "which", lambda _: None)  # no Claude Code installed
    for k, v in env.items():
        clean_env.setenv(k, v)
    got = llm_mod.make_llm()
    assert isinstance(got, llm_mod.OpenAICompatLLM)
    assert got._url == url and got._model(SMART) == model


@pytest.mark.parametrize("choice, binary, head", [
    ("codex", "codex", ["codex", "exec", "--skip-git-repo-check", "-"]),
    ("opencode", "opencode", ["opencode", "run"]),
    ("gemini-cli", "gemini", ["gemini", "-p"]),
])
def test_agent_cli_backends(clean_env, choice, binary, head):
    clean_env.setenv("KRAKEN_LLM", choice)
    clean_env.setattr(llm_mod.shutil, "which", lambda name: name if name == binary else None)
    got = llm_mod.make_llm()
    assert isinstance(got, llm_mod.AgentCLILLM) and got._argv[:len(head)] == head


def test_auto_falls_back_to_any_installed_agent_cli(clean_env):
    clean_env.delenv("KRAKEN_CLAUDE_BIN", raising=False)
    clean_env.setattr(llm_mod.shutil, "which", lambda name: name if name == "opencode" else None)
    assert llm_mod.make_llm()._argv[:2] == ["opencode", "run"]


def test_nothing_set_up_says_what_to_do(clean_env):
    clean_env.delenv("KRAKEN_CLAUDE_BIN", raising=False)
    clean_env.setattr(llm_mod.shutil, "which", lambda _: None)
    with pytest.raises(llm_mod.LLMError, match="needs a model"):
        llm_mod.make_llm()


async def test_openai_compat_request_and_model_mapping():
    import httpx

    seen = []

    def reply(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [
            {"finish_reason": "stop", "message": {"content": '```json\n{"ok": true}\n```'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    llm = llm_mod.OpenAICompatLLM("https://x/v1/", "k", "big", "small", client=client)
    assert (await llm.json(SMART, "sys", "usr", OkModel)).ok is True
    assert (await llm.json(FAST, "sys", "usr", OkModel)).ok is True
    assert [b["model"] for b in seen] == ["big", "small"]
    assert seen[0]["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]


async def test_openai_compat_errors_and_truncation():
    import httpx

    replies = [httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}),
               httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": false}'}}]}),
               httpx.Response(429, text="quota")]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: replies.pop(0)))
    llm = llm_mod.OpenAICompatLLM("https://x/v1", None, "m", client=client)
    assert (await llm.json(SMART, "s", "u", OkModel)).ok is False  # truncated once, retried
    with pytest.raises(llm_mod.LLMError, match="429"):
        await llm.json(SMART, "s", "u", OkModel)


async def test_agent_cli_reads_json_out_of_a_chatty_reply():
    banner = '\x1b[0m> build · gemini-3.8-flash\n\nSure! Here it is:\n{"ok": true}\n'
    run = FakeRunner((0, banner, ""))
    assert (await llm_mod.AgentCLILLM(["opencode", "run"], runner=run).json(SMART, "SYS", "USR", OkModel)).ok
    argv, stdin = run.calls[0]
    assert argv == ["opencode", "run"] and "SYS" in stdin and "USR" in stdin  # prompt on stdin


async def test_agent_cli_prompt_placeholder_and_failure():
    run = FakeRunner((0, '{"ok": true}', ""))
    await llm_mod.AgentCLILLM(["my-agent", "--ask", "{prompt}"], runner=run).json(FAST, "S", "U", OkModel)
    argv, stdin = run.calls[0]
    assert "U" in argv[2] and stdin == ""
    with pytest.raises(llm_mod.LLMError, match="exited 1"):
        await llm_mod.AgentCLILLM(["x"], runner=FakeRunner((1, "", "not signed in"))).json(FAST, "s", "u", OkModel)


def test_make_llm_rejects_unknown(monkeypatch):
    monkeypatch.setenv("KRAKEN_LLM", "gpt")
    with pytest.raises(ValueError):
        llm_mod.make_llm()
