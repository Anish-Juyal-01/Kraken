"""One-time registration of Kraken as a Switch agent.

Usage: python scripts/register.py [--url URL] [--token TOKEN] [--name NAME]
Token defaults to env SWITCH_REGISTRATION_TOKEN; URL to env SWITCH_URL (both may be set in .env).
Prints SWITCH_AGENT_ID / SWITCH_API_KEY lines for .env; for any other --name (such as
kraken-smoke) it prints SMOKE_AGENT_ID / SMOKE_API_KEY lines instead.
"""

import argparse
import os
import sys

import httpx

from kraken.main import load_env

BODY = {
    "name": "kraken",
    "display_name": "Kraken",
    "description": "Kraken joins team chat, spots conflicting decisions and helps resolve them.",
    "connector_type": "custom",
    "integration_profile": {
        "connection_model": "always_on",
        "message_exchange": True,
        "pre_invocation_mediation": [],
        "post_invocation_mediation": [],
        "event_reporting": [],
    },
}


def env_lines(name: str, agent_id: str, api_key: str) -> list[str]:
    """The .env lines for a newly registered agent."""
    if name == "kraken":
        return [f"SWITCH_AGENT_ID={agent_id}", f"SWITCH_API_KEY={api_key}"]
    return [f"# agent {name}: these are the env names scripts/smoke.py reads",
            f"SMOKE_AGENT_ID={agent_id}", f"SMOKE_API_KEY={api_key}"]


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("SWITCH_URL"))
    parser.add_argument("--token", default=os.environ.get("SWITCH_REGISTRATION_TOKEN"))
    parser.add_argument("--name", default="kraken", help="agent name (default: kraken)")
    args = parser.parse_args()
    if not args.url or not args.token:
        parser.error("need --url/SWITCH_URL and --token/SWITCH_REGISTRATION_TOKEN")
    resp = httpx.post(
        f"{args.url.rstrip('/')}/agents",
        json={**BODY, "name": args.name,
              "display_name": "Kraken" if args.name == "kraken" else args.name},
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=15,
    )
    if resp.status_code >= 400:
        print(f"Registration failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        if resp.status_code == 400:
            print("If connector_type was rejected, read the valid value above and edit BODY.", file=sys.stderr)
        return 1
    data = resp.json()
    for line in env_lines(args.name, data.get("agent_id") or data.get("id"), data.get("api_key")):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
