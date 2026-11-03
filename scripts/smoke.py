"""End-to-end smoke: post the demo seed chat to a live Switch server and wait for Sonar.

Usage (from the repo root): python scripts/smoke.py
Posts as a separate agent (SMOKE_AGENT_ID / SMOKE_API_KEY), because Kraken ignores its own
messages. Register it with `python scripts/register.py --name kraken-smoke` and add it to the demo rooms.
Reads .env like kraken/main.py. Exit codes: 0 ok, 1 no resolution room appeared, 2 a demo room is missing.
"""

import asyncio
import os
import re
import sys
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kraken.main import load_env  # noqa: E402
from kraken.switch_client import SwitchClient  # noqa: E402

SEED_PATH = ROOT / "demo" / "seed_chat.md"
TEMPLATE_PATH = ROOT / "templates" / "sonar-resolution.template.yaml"
POLL_SECONDS = 5
TIMEOUT_SECONDS = 120


def parse_seed(text: str) -> list[tuple[str, list[str]]]:
    """Return [(room_name, [messages])] from the seed markdown, in order."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            sections.append((line[3:].strip().lstrip("#").strip(), []))
        elif sections and (m := re.match(r"\d+\.\s+(.*)", line)):
            sections[-1][1].append(m.group(1).strip())
    return sections


def room_prefix(template_text: str) -> str:
    """The fixed start of the resolution room name, before the first placeholder."""
    return yaml.safe_load(template_text)["room"]["name"].split("{", 1)[0]


def find_resolution_room(names: list[str], prefix: str, existing: set[str]) -> str | None:
    """First matching room name that was not already present before the seed was posted."""
    return next((n for n in names if n.startswith(prefix) and n not in existing), None)


def page_room_names(page: dict) -> list[str]:
    """Room names from one list_all_rooms page."""
    return [r["name"] for r in page.get("rooms", [])]


async def all_room_names(op, prefix: str) -> list[str]:
    """Names of every room on the instance containing prefix, following next_cursor.

    list_rooms only shows rooms this agent is assigned to; the resolution room never
    includes the smoke agent, so the instance-wide list_all_rooms is needed.
    """
    names: list[str] = []
    cursor = None
    while True:
        args = {"name_contains": prefix, "include_archived": True}
        if cursor:
            args["cursor"] = cursor
        page = await op("list_all_rooms", **args)
        names += page_room_names(page)
        cursor = page.get("next_cursor")
        if not cursor:
            return names


def check_env(env) -> str | None:
    """Return an error message if the smoke agent's settings are missing, else None."""
    missing = [k for k in ("SWITCH_URL", "SMOKE_AGENT_ID", "SMOKE_API_KEY") if not env.get(k)]
    if not missing:
        return None
    return (f"Missing {', '.join(missing)}. Smoke posts as its own agent: run "
            "`python scripts/register.py --name kraken-smoke`, add that agent to the demo rooms, "
            "and set SMOKE_AGENT_ID / SMOKE_API_KEY in .env.")


async def run() -> int:
    load_env(str(ROOT / ".env"))
    error = check_env(os.environ)
    if error:
        print(f"SMOKE FAIL: {error}", file=sys.stderr)
        return 2
    client = SwitchClient(os.environ["SWITCH_URL"], os.environ["SMOKE_AGENT_ID"],
                          os.environ["SMOKE_API_KEY"], str(uuid.uuid4()))
    seed = parse_seed(SEED_PATH.read_text(encoding="utf-8"))
    prefix = room_prefix(TEMPLATE_PATH.read_text(encoding="utf-8"))
    rooms = {r["name"]: r["room_id"] for r in await client.op("list_rooms")}
    existing = set(await all_room_names(client.op, prefix))
    missing = [name for name, _ in seed if name not in rooms]
    if missing:
        print(f"SMOKE FAIL: demo room(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    for name, messages in seed:
        for body in messages:
            await client.send(rooms[name], body)
            print(f"posted to {name}: {body}")
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        found = find_resolution_room(await all_room_names(client.op, prefix), prefix, existing)
        if found:
            print(f"SMOKE OK: resolution room {found}")
            return 0
    print(f"SMOKE FAIL: no room starting with {prefix!r} within {TIMEOUT_SECONDS}s")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
