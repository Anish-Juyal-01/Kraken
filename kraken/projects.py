import json

from kraken.config import Config, ProjectConfig
from kraken.hallmark.ledger import Ledger

_META_KEY = "projects"
_DEFAULT_KEY = "default_project"
# Fields `@kraken project set <name> <field> <value>` may change; lists are comma-separated.
SETTABLE = ("description", "branch", "test", "run", "base_url", "health", "endpoints",
            "config_files", "code_paths", "chaos_endpoint")
_LISTS = {"endpoints", "config_files", "code_paths"}


class Projects:
    """The codebases Kraken works on, kept in the ledger's meta table.
    kraken.yaml only seeds them; `@kraken connect` adds more from chat."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def _load(self) -> dict[str, ProjectConfig]:
        raw = self.ledger.get_meta(_META_KEY)
        data = json.loads(raw) if raw else {}
        return {name: ProjectConfig.model_validate(p) for name, p in data.items()}

    def _save(self, projects: dict[str, ProjectConfig]) -> None:
        self.ledger.set_meta(_META_KEY, json.dumps({n: p.model_dump() for n, p in projects.items()}))

    def seed(self, cfg: Config) -> None:
        """Write the projects from config, once; later config projects are added too, so a
        project added to kraken.yaml shows up without losing ones connected from chat."""
        projects = self._load()
        for p in cfg.projects:
            projects.setdefault(p.name, p)
        self._save(projects)
        if self.ledger.get_meta(_DEFAULT_KEY) is None and projects:
            wanted = cfg.default_project if cfg.default_project in projects else next(iter(projects))
            self.ledger.set_meta(_DEFAULT_KEY, wanted)

    def all(self) -> dict[str, ProjectConfig]:
        return self._load()

    def default_name(self) -> str | None:
        name = self.ledger.get_meta(_DEFAULT_KEY)
        return name if name in self._load() else None

    def get(self, name: str | None = None) -> ProjectConfig | None:
        """A project by name; None means the default."""
        projects = self._load()
        return projects.get(name or self.default_name() or "")

    def put(self, project: ProjectConfig) -> None:
        projects = self._load()
        projects[project.name] = project
        self._save(projects)
        if self.default_name() is None:
            self.ledger.set_meta(_DEFAULT_KEY, project.name)

    def use(self, name: str) -> bool:
        if name not in self._load():
            return False
        self.ledger.set_meta(_DEFAULT_KEY, name)
        return True

    def set_field(self, name: str, field: str, value: str) -> ProjectConfig:
        """Change one field from chat. Raises KeyError (no project) or ValueError (bad field)."""
        projects = self._load()
        if name not in projects:
            raise KeyError(name)
        if field not in SETTABLE:
            raise ValueError(f"{field} can't be set; settable: {', '.join(SETTABLE)}")
        parsed = [v.strip() for v in value.split(",") if v.strip()] if field in _LISTS else value
        if value.strip().lower() in ("none", "-", "") and field not in _LISTS:
            parsed = None
        updated = ProjectConfig.model_validate({**projects[name].model_dump(), field: parsed})
        projects[name] = updated
        self._save(projects)
        return updated


def render_project(p: ProjectConfig, default: bool = False) -> str:
    """A short, checkable summary for chat."""
    mode = (f"web service: `{p.run}` on {p.base_url or '?'}" if p.mode == "http"
            else "library (no server): Storm runs the tests under faults")
    lines = [f"**{p.name}**{' (default)' if default else ''}"
             f" · {p.language or 'unknown language'} · {p.repo or 'this repo'} @ {p.branch}",
             f"tests: `{p.test or 'not found — set it with: project set ' + p.name + ' test <cmd>'}`",
             f"mode: {mode}",
             f"Sonar reads: {', '.join(p.code_paths) or 'the whole repo'}"]
    if p.endpoints:
        lines.append(f"Storm endpoints: {', '.join(p.endpoints)}")
    if p.config_files:
        lines.append(f"config files: {', '.join(p.config_files)}")
    return "\n".join(lines)
