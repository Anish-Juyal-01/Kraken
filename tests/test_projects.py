import json
from pathlib import Path

import pytest

import kraken.dispatch as dispatch_mod
from kraken.conductor.commands import parse
from kraken.config import Config, ProjectConfig
from kraken.init import detect, write_into
from kraken.projects import Projects, render_project
from kraken.workspace import GitError, project_name, valid_repo_url
from tests.test_dispatch import REQ_ROOM, ev, make


# ---- store ---------------------------------------------------------------
def test_seed_put_use_and_set(ledger):
    store = Projects(ledger)
    store.seed(Config(projects=[ProjectConfig(name="a"), ProjectConfig(name="b")], default_project="b"))
    assert set(store.all()) == {"a", "b"} and store.default_name() == "b"
    store.put(ProjectConfig(name="c", repo="https://github.com/o/c"))
    assert store.get("c").workdir == "projects/c" and store.get().name == "b"
    assert store.use("c") and store.get().name == "c" and not store.use("nope")
    p = store.set_field("c", "endpoints", "/health, /items/1")
    assert p.endpoints == ["/health", "/items/1"]
    assert store.set_field("c", "run", "none").run is None
    with pytest.raises(ValueError):
        store.set_field("c", "name", "x")
    with pytest.raises(KeyError):
        store.set_field("zzz", "test", "pytest")


def test_seed_keeps_chat_projects_and_adds_new_config_ones(ledger):
    store = Projects(ledger)
    store.seed(Config(projects=[ProjectConfig(name="a")]))
    store.put(ProjectConfig(name="from-chat"))
    store.seed(Config(projects=[ProjectConfig(name="a"), ProjectConfig(name="new")]))
    assert set(store.all()) == {"a", "from-chat", "new"} and store.default_name() == "a"


def test_render_mentions_mode_and_tests():
    lib = render_project(ProjectConfig(name="lib", test="pytest"), default=True)
    assert "(default)" in lib and "library" in lib and "`pytest`" in lib
    web = render_project(ProjectConfig(name="api", run="npm start", base_url="http://x:3000"))
    assert "npm start" in web and "not found" in web  # no test command yet


# ---- parsing ---------------------------------------------------------------
@pytest.mark.parametrize("body, kind, arg, extra", [
    ("@kraken connect https://github.com/o/repo", "connect", "https://github.com/o/repo", None),
    ("@kraken connect <https://github.com/o/repo.git> as api", "connect", "https://github.com/o/repo.git", "api"),
    ("@kraken connect git@github.com:o/repo.git web", "connect", "git@github.com:o/repo.git", "web"),
    ("@kraken projects", "projects", None, None),
    ("@kraken use api", "use", "api", None),
])
def test_parse_project_commands(body, kind, arg, extra):
    cmd = parse(body, user="sam")
    assert cmd.kind == kind and cmd.arg == arg and cmd.role == extra


def test_parse_project_set_keeps_value_with_spaces():
    cmd = parse("@kraken project set api test npm run test:ci", user="sam")
    assert cmd.kind == "project_set" and cmd.arg == "api" and cmd.args == ["test", "npm run test:ci"]


def test_ordinary_requests_are_not_project_commands():
    assert parse("@kraken build api: add rate limiting", user="sam") is None
    assert parse("@kraken connect the dots in the login flow", user="sam") is None


def test_repo_url_safety():
    assert valid_repo_url("https://github.com/o/r") and valid_repo_url("git@github.com:o/r.git")
    for bad in ("/etc/passwd", "file:///x", "--upload-pack=x", "https://x/r;rm -rf /", "C:\\repo"):
        assert not valid_repo_url(bad), bad
    assert project_name("https://github.com/o/My Repo.git") == "My-Repo"


# ---- chat commands through the dispatcher -----------------------------------
async def test_connect_clones_detects_and_reports(ledger, tmp_path, monkeypatch):
    client, d = make(ledger)
    d.cfg.workspace = str(tmp_path / "ws")

    async def fake_checkout(url, dest):
        (dest / "src").mkdir(parents=True)
        (dest / "requirements.txt").write_text("fastapi\n")
        (dest / "tests").mkdir()
        (dest / "src" / "main.py").write_text(
            'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/health")\ndef h(): ...\n'
            "class Item:\n    price: int\n")
        return dest

    monkeypatch.setattr(dispatch_mod, "checkout", fake_checkout)
    await d.handle(ev(1, "@kraken connect https://github.com/o/shop-api", addressed=True))
    await asyncio_drain(d)
    p = d.projects.get("shop-api")
    assert p.repo == "https://github.com/o/shop-api" and p.test == "python -m pytest"
    assert p.run.startswith("python -m uvicorn src.main:app") and p.health == "/health"
    assert any(c.subject == "price" for c in d.code_index)  # Sonar indexed the new repo
    texts = client.texts(REQ_ROOM)
    assert texts[0].startswith("Cloning") and "Connected." in texts[1] and "shop-api" in texts[1]


async def test_connect_reports_git_errors(ledger, monkeypatch):
    client, d = make(ledger)

    async def boom(url, dest):
        raise GitError("repository not found")

    monkeypatch.setattr(dispatch_mod, "checkout", boom)
    await d.handle(ev(1, "@kraken connect https://github.com/o/missing", addressed=True))
    await asyncio_drain(d)
    assert "repository not found" in client.texts(REQ_ROOM)[-1]
    assert d.projects.get("missing") is None


async def test_projects_use_and_set_over_chat(ledger):
    client, d = make(ledger)
    d.projects.put(ProjectConfig(name="web"))
    await d.handle(ev(1, "@kraken projects", addressed=True))
    assert "**app** (default)" in client.texts(REQ_ROOM)[-1] and "**web**" in client.texts(REQ_ROOM)[-1]
    await d.handle(ev(2, "@kraken use web", addressed=True))
    assert d.projects.default_name() == "web"
    await d.handle(ev(3, "@kraken project set web test go test ./...", addressed=True))
    assert d.projects.get("web").test == "go test ./..."
    await d.handle(ev(4, "@kraken project set web name x", addressed=True))
    assert "Can't set that" in client.texts(REQ_ROOM)[-1]


async def test_project_commands_only_in_requests_room(ledger):
    client, d = make(ledger, llm=None)
    await d.handle(ev(1, "@kraken use web", room="backend", addressed=True))
    assert d.projects.default_name() == "app"


async def test_request_can_name_its_project(ledger):
    client, d = make(ledger)
    d.projects.put(ProjectConfig(name="web", repo="https://github.com/o/web", test="npm test"))
    await d.handle(ev(1, "@kraken build web: add a /health route", addressed=True))
    (req,) = ledger.open_requests()
    assert req.data["project"] == "web" and req.text == "add a /health route"
    inputs = client.created("Spark")[0]["inputs"]
    assert "web" in inputs["project"] and "npm test" in inputs["project"]
    await d.handle(ev(2, "@kraken build nosuch: thing", addressed=True))
    assert ledger.open_requests()[-1].data["project"] == "app"  # unknown name: the default


async def test_request_without_any_project_explains(ledger):
    client, d = make(ledger)
    d.projects._save({})
    await d.handle(ev(1, "@kraken build it", addressed=True))
    assert ledger.open_requests() == [] and "connect" in client.texts(REQ_ROOM)[-1]


async def asyncio_drain(d):
    import asyncio
    while d._tasks:
        await asyncio.gather(*list(d._tasks))


# ---- detection ---------------------------------------------------------------
def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def test_detect_python_entry_script(tmp_path):
    root = _write(tmp_path, {
        "pyproject.toml": '[project]\nname="svc"\ndescription="Orders service"\n',
        "tests/test_x.py": "", "config/app.yaml": "timeout: 3\n",
        "run.py": "import uvicorn\nif __name__ == '__main__':\n    uvicorn.run('app:app', port=9000)\n",
        "app/routes.py": '@router.get("/orders/{oid}")\ndef o(): ...\n'})
    p = detect(root, repo="https://github.com/o/svc.git")
    assert p.name == "svc" and p.language == "python" and p.test == "python -m pytest"
    assert p.run == "python run.py" and p.base_url == "http://127.0.0.1:9000"
    assert p.code_paths == ["app"] and p.config_files == ["config/app.yaml"]
    assert p.description == "Orders service"


def test_detect_node_express(tmp_path):
    root = _write(tmp_path, {
        "package.json": json.dumps({"scripts": {"start": "node server.js", "test": "jest"},
                                    "dependencies": {"express": "^4"}}),
        "server.js": "const app = require('express')();\napp.get('/api/items/:id', h);\n"
                     "app.listen(process.env.PORT || 4000);\n",
        ".env.example": "DB_URL=\n", "README.md": "# Items API\nStores items.\n"})
    p = detect(root)
    assert p.language == "javascript" and p.test == "npm test" and p.run == "npm start"
    assert p.base_url == "http://127.0.0.1:4000" and p.endpoints == ["/api/items/1"]
    assert p.config_files == [".env.example"] and p.description == "Items API"


def test_detect_go_service_and_library(tmp_path):
    svc = _write(tmp_path / "svc", {
        "go.mod": "module x\n",
        "cmd/api/main.go": 'package main\nimport "net/http"\nfunc main(){\n'
                           'http.HandleFunc("/healthz", h)\nhttp.ListenAndServe(":8085", nil)}\n'})
    p = detect(svc)
    assert p.test == "go test ./..." and p.run == "go run ./cmd/api"
    assert p.base_url == "http://127.0.0.1:8085" and p.health == "/healthz"
    lib = detect(_write(tmp_path / "lib", {"Cargo.toml": "[package]\nname='x'\n", "src/lib.rs": ""}))
    assert lib.language == "rust" and lib.mode == "library" and lib.code_paths == ["src"]


def test_detect_unknown_repo_is_library_with_no_test(tmp_path):
    p = detect(_write(tmp_path, {"notes.txt": "hi"}))
    assert p.language is None and p.test is None and p.mode == "library"


def test_write_into_keeps_other_keys_and_refuses_overwrite(tmp_path):
    cfg = tmp_path / "kraken.yaml"
    cfg.write_text("bidders: [a]\nprojects:\n  - name: shop\n")
    write_into(cfg, ProjectConfig(name="api", test="pytest"))
    text = cfg.read_text()
    assert "bidders" in text and "name: api" in text and "name: shop" in text
    with pytest.raises(FileExistsError):
        write_into(cfg, ProjectConfig(name="api"))
    write_into(cfg, ProjectConfig(name="api", test="tox"), force=True)
    assert "tox" in cfg.read_text()
