import ast
import json
import logging
from pathlib import Path

import yaml

from kraken.models import Claim
from kraken.sonar.extract import canonical

log = logging.getLogger(__name__)

_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git"}
_SUFFIXES = {".yaml", ".yml", ".json", ".py"}


def _claim(subject: str, attribute: str, value: str, path: str, line: int | None) -> Claim:
    return Claim(subject=subject, attribute=attribute, value=value, speaker="code", room_id="",
                 message_id="", timestamp=0, confidence=1.0, source="code", path=path, line=line)


def _scalar(v) -> str:
    return str(v).lower() if isinstance(v, bool) else str(v)


def _leaves(data, node=None, key=None):
    """Yield (last_key, value, line) for scalar leaves; list items are skipped."""
    if isinstance(data, dict):
        children = {}
        if node is not None and isinstance(node, yaml.MappingNode):
            children = {k.value: v for k, v in node.value}
        for k, v in data.items():
            yield from _leaves(v, children.get(k), str(k))
    elif isinstance(data, list):
        return
    elif key is not None and data is not None:
        line = node.start_mark.line + 1 if node is not None else None
        yield key, data, line


def _openapi(data, node, path: str) -> list[Claim]:
    out = []
    schemas = ((data.get("components") or {}).get("schemas") or {})
    cnode = {k.value: v for k, v in node.value} if isinstance(node, yaml.MappingNode) else {}

    def sub(n, *keys):
        for k in keys:
            if not isinstance(n, yaml.MappingNode):
                return None
            n = {kk.value: vv for kk, vv in n.value}.get(k)
        return n

    for sname, schema in schemas.items():
        props = (schema or {}).get("properties") or {}
        for pname, prop in props.items():
            if isinstance(prop, dict) and isinstance(prop.get("type"), str):
                tnode = sub(cnode.get("components"), "schemas", sname, "properties", pname, "type")
                line = tnode.start_mark.line + 1 if tnode is not None else None
                out.append(_claim(canonical(pname), "type", prop["type"], path, line))
    return out


def _is_openapi(data) -> bool:
    return isinstance(data, dict) and (
        "openapi" in data
        or (isinstance(data.get("components"), dict) and "schemas" in data["components"]))


def _index_yaml(text: str, path: str) -> list[Claim]:
    node = yaml.compose(text)
    data = yaml.safe_load(text)
    if node is None or not isinstance(data, dict):
        return []
    if _is_openapi(data):
        return _openapi(data, node, path)
    return [_claim(canonical(k), "value", _scalar(v), path, line)
            for k, v, line in _leaves(data, node=node)]


def _index_json(text: str, path: str) -> list[Claim]:
    data = json.loads(text)
    return [_claim(canonical(k), "value", _scalar(v), path, None)
            for k, v, _ in _leaves(data)]


def _index_python(text: str, path: str) -> list[Claim]:
    out = []
    for cls in ast.walk(ast.parse(text)):
        if isinstance(cls, ast.ClassDef):
            for node in cls.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    out.append(_claim(canonical(node.target.id), "type",
                                      ast.unparse(node.annotation), path, node.lineno))
    return out


def _files(paths: list[str]):
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not _SKIP_DIRS & set(f.relative_to(p).parts[:-1]):
                    yield f


def index_code(paths: list[str]) -> list[Claim]:
    out: list[Claim] = []
    for f in _files(paths):
        suffix = f.suffix.lower()
        if suffix not in _SUFFIXES:
            continue
        shown = f.as_posix()
        try:
            text = f.read_text(encoding="utf-8")
            if suffix == ".json":
                out += _index_json(text, shown)
            elif suffix == ".py":
                out += _index_python(text, shown)
            else:
                out += _index_yaml(text, shown)
        except (OSError, UnicodeDecodeError, ValueError, SyntaxError, yaml.YAMLError) as err:
            log.warning("skipping %s: %s", shown, err)
    return out


def code_claims_for(index: list[Claim], subject: str, attribute: str) -> list[Claim]:
    return [c for c in index if c.subject == subject and c.attribute == attribute]
