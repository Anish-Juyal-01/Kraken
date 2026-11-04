"""Describe a codebase for Kraken: `python -m kraken.init <path> [--name N] [--repo URL]`.

detect() reads marker files (pyproject.toml, package.json, go.mod, Cargo.toml, ...) and
guesses the language, test command, how to run it, where the code lives and which config
files Storm may corrupt. Anything it can't tell is left empty for a person to fill in.
The CLI writes the result into kraken.yaml `projects:`; the chat command
`@kraken connect <repo>` uses detect() directly.
"""

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

from kraken.config import ProjectConfig

_CODE_DIRS = ("src", "app", "lib", "pkg", "internal", "cmd", "api", "server")
_CONFIG_GLOBS = ("config/*.yaml", "config/*.yml", "config/*.json", "config/*.toml",
                 "*.config.yaml", "settings.yaml", "settings.toml")
_ROUTE = re.compile(r"""@\w+\.(?:get|route)\(\s*["'](/[^"']*)["']""")  # FastAPI/Flask
_EXPRESS = re.compile(r"""\b(?:app|router)\.get\(\s*["'`](/[^"'`]*)["'`]""")
_GO_ROUTE = re.compile(r"""HandleFunc\(\s*"(/[^"]*)""")
_PARAM = re.compile(r"\{[^}]*\}|<[^>]*>|:\w+|\*\w*")  # /users/{id}, <id>, :id, *path
_PORT = re.compile(r"(?:port\s*[=:]\s*|PORT\s*\|\|\s*|listen\(\s*\"?:?)(\d{4,5})", re.IGNORECASE)
_COMMON_PORT = re.compile(r"\b(80[0-9]{2}|3000|3001|4000|5000|5001|9000)\b")
_MAX_SCAN = 200  # source files scanned for routes and ports


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sources(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    skip = {".git", ".venv", "venv", "node_modules", "dist", "build", "target", "vendor",
            "__pycache__", ".next"}
    out = []
    for f in sorted(root.rglob("*")):
        if len(out) >= _MAX_SCAN:
            break
        if f.is_file() and f.suffix in suffixes and not skip & set(f.relative_to(root).parts):
            out.append(f)
    return out


_ENTRY_NAMES = ("run.py", "main.py", "app.py", "server.py", "serve.py", "wsgi.py", "asgi.py")
_STARTS_SERVER = re.compile(
    r"uvicorn\.(?:run|Server)|serve_forever|make_server|waitress\.serve"
    r"|(?<!asyncio)\b\w+\.run\(\s*(?:host|port|debug|threaded)")


def _python(root: Path) -> dict:
    found: dict = {"language": "python"}
    tests = (root / "tests").is_dir() or (root / "test").is_dir() or any(root.glob("test_*.py"))
    found["test"] = "python -m pytest" if tests or "pytest" in _read(root / "pyproject.toml") else None
    srcs = _sources(root, (".py",))
    found["endpoints"] = _routes(srcs, _ROUTE)
    if (root / "manage.py").exists():  # Django
        found.update(run="python manage.py runserver 8000", base_url="http://127.0.0.1:8000")
        found["test"] = found["test"] or "python manage.py test"
        return found
    # 1. a script that starts its own server (python run.py), shallowest first
    for f in sorted(srcs, key=lambda f: len(f.relative_to(root).parts)):
        text = _read(f)
        if f.name in _ENTRY_NAMES and "__main__" in text and _STARTS_SERVER.search(text):
            found["run"] = f"python {f.relative_to(root).as_posix()}"
            found["base_url"] = f"http://127.0.0.1:{_port([f]) or '8000'}"
            return found
    # 2. an app object a server can load: prefer one named `app`, in an entry-named file
    candidates = []
    for f in srcs:
        text = _read(f)
        mod = f.relative_to(root).with_suffix("").as_posix().replace("/", ".")
        for kind, pat in (("fastapi", r"^(\w+)\s*=\s*FastAPI\("), ("flask", r"^(\w+)\s*=\s*Flask\(")):
            for var in re.findall(pat, text, re.M):
                candidates.append((var != "app", f.name not in _ENTRY_NAMES, kind, mod, var))
    if candidates:
        _, _, kind, mod, var = min(candidates)
        if kind == "fastapi":
            found.update(run=f"python -m uvicorn {mod}:{var} --port 8000", base_url="http://127.0.0.1:8000")
        else:
            found.update(run=f"python -m flask --app {mod}:{var} run --port 5000",
                         base_url="http://127.0.0.1:5000")
    return found


def _node(root: Path) -> dict:
    try:
        pkg = json.loads(_read(root / "package.json") or "{}")
    except json.JSONDecodeError:
        pkg = {}
    scripts = pkg.get("scripts") or {}
    found: dict = {"language": "typescript" if (root / "tsconfig.json").exists() else "javascript"}
    test = scripts.get("test", "")
    found["test"] = "npm test" if test and "no test specified" not in test else None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    web = any(d in deps for d in ("express", "fastify", "koa", "@nestjs/core", "hono"))
    if "start" in scripts and web:
        found["run"] = "npm start"
    elif "dev" in scripts and web:
        found["run"] = "npm run dev"
    srcs = _sources(root, (".js", ".ts", ".mjs", ".cjs"))
    if found.get("run"):
        port = _port(srcs) or "3000"
        found["base_url"] = f"http://127.0.0.1:{port}"
    found["endpoints"] = _routes(srcs, _EXPRESS)
    return found


def _go(root: Path) -> dict:
    found: dict = {"language": "go", "test": "go test ./..."}
    srcs = _sources(root, (".go",))
    if any("net/http" in _read(f) and "ListenAndServe" in _read(f) for f in srcs):
        main = next((f for f in srcs if "package main" in _read(f)), None)
        if main is not None:
            rel = main.parent.relative_to(root).as_posix()
            found["run"] = f"go run ./{rel}" if rel != "." else "go run ."
            found["base_url"] = f"http://127.0.0.1:{_port(srcs) or '8080'}"
    found["endpoints"] = _routes(srcs, _GO_ROUTE)
    return found


def _rust(root: Path) -> dict:
    return {"language": "rust", "test": "cargo test"}


def _routes(srcs: list[Path], pattern: re.Pattern) -> list[str]:
    routes: list[str] = []
    for f in srcs:
        for r in pattern.findall(_read(f)):
            r = _PARAM.sub("1", r)  # a concrete path Storm can call
            if r not in routes and not r.startswith("/__"):
                routes.append(r)
    return routes[:8]


def _port(srcs: list[Path]) -> str | None:
    for pattern in (_PORT, _COMMON_PORT):
        for f in srcs:
            if m := pattern.search(_read(f)):
                return m.group(1)
    return None


def _name_from(root: Path, repo: str | None) -> str:
    raw = (repo.rstrip("/").rsplit("/", 1)[-1] if repo else root.resolve().name)
    raw = raw.removesuffix(".git")
    return re.sub(r"[^\w.-]", "-", raw) or "project"


def detect(root: str | Path, name: str | None = None, repo: str | None = None) -> ProjectConfig:
    """A best-guess ProjectConfig for the checkout at `root`."""
    root = Path(root)
    if (root / "package.json").exists():
        found = _node(root)
    elif (root / "go.mod").exists():
        found = _go(root)
    elif (root / "Cargo.toml").exists():
        found = _rust(root)
    elif any((root / m).exists() for m in ("pyproject.toml", "requirements.txt", "setup.py")) \
            or _sources(root, (".py",)):
        found = _python(root)
    else:
        found = {}
    code = [d for d in _CODE_DIRS if (root / d).is_dir()]
    configs = sorted({p.relative_to(root).as_posix() for g in _CONFIG_GLOBS for p in root.glob(g)})
    if (root / ".env.example").exists():
        configs.append(".env.example")
    endpoints = found.pop("endpoints", [])
    health = next((e for e in endpoints if "health" in e or "ready" in e), None) or (
        endpoints[0] if endpoints else ("/" if found.get("run") else None))
    return ProjectConfig(
        name=name or _name_from(root, repo), repo=repo, root=str(root),
        branch=_default_branch(root), description=_description(root),
        code_paths=code, endpoints=endpoints, health=health,
        config_files=[c for c in configs if c != ".env.example"] + (
            [".env.example"] if ".env.example" in configs else []), **found)


def _default_branch(root: Path) -> str:
    head = _read(root / ".git" / "HEAD").strip()
    return head.rsplit("/", 1)[-1] if head.startswith("ref:") else "main"


def _description(root: Path) -> str:
    for readme in ("README.md", "README.rst", "README.txt", "README"):
        text = _read(root / readme)
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith(("[!", "![", "<", "=")):
                return line[:160]
    try:
        return (tomllib.loads(_read(root / "pyproject.toml")).get("project") or {}).get(
            "description", "")[:160]
    except tomllib.TOMLDecodeError:
        return ""


def write_into(config_path: Path, project: ProjectConfig, force: bool = False) -> None:
    """Add (or with force, replace) the project in kraken.yaml, keeping every other key."""
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    data = data or {}
    projects = data.setdefault("projects", [])
    entry = project.model_dump(exclude_defaults=True, exclude={"root"} if project.root == "." else set())
    for i, p in enumerate(projects):
        if p.get("name") == project.name:
            if not force:
                raise FileExistsError(f"project {project.name} is already in {config_path}; "
                                      "use --force to replace it")
            projects[i] = entry
            break
    else:
        projects.append(entry)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kraken.init", description=__doc__.splitlines()[0])
    ap.add_argument("path", help="the repo checkout to describe")
    ap.add_argument("--name")
    ap.add_argument("--repo", help="its git URL (for the crew to clone and open PRs against)")
    ap.add_argument("--config", default="kraken.yaml")
    ap.add_argument("--force", action="store_true", help="replace an existing entry")
    ap.add_argument("--print", action="store_true", help="only print, don't write")
    args = ap.parse_args(argv)
    project = detect(args.path, args.name, args.repo)
    from kraken.projects import render_project  # noqa: PLC0415  (avoids a cycle at import)
    print(render_project(project))
    if args.print:
        return 0
    try:
        write_into(Path(args.config), project, args.force)
    except FileExistsError as err:
        print(err, file=sys.stderr)
        return 1
    print(f"\nwritten to {args.config} — check it, then restart Kraken")
    return 0


if __name__ == "__main__":
    sys.exit(main())
