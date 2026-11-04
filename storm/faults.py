"""Storm fault toolkit.

Any project: `python -m storm.faults --project storm-project.json --list`, then
`python -m storm.faults <fault> --project storm-project.json` (see storm.project_faults).
The bundled shopfront demo without a project file:
`python -m storm.faults <fault> [--target api|email] [--seconds N]`.
"""
import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DEFAULT_LIMITS = Path("demo/shopfront/config/limits.yaml")
FAULTS = ("kill", "latency", "bad_payload", "flood", "bad_config")


def start_fault(client, fault: str, target: str = "api", params: dict | None = None,
                path: str = "/__chaos") -> None:
    """Enable a server-side fault via the control endpoint (client: httpx.Client or TestClient)."""
    resp = client.post(path, json={"fault": fault, "target": target, "params": params or {}})
    resp.raise_for_status()


def clear_faults(client, path: str = "/__chaos") -> None:
    client.delete(path).raise_for_status()


async def flood(base_url: str, count: int = 200) -> dict[str, int]:
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
        results = await asyncio.gather(*(http.get("/v1/users/1") for _ in range(count)),
                                       return_exceptions=True)
    hist = Counter("error" if isinstance(r, Exception) else str(r.status_code) for r in results)
    return dict(sorted(hist.items()))


def bad_config(limits_path: Path, seconds: float, sleep=time.sleep) -> None:
    """Write an invalid free_tier, restoring the original after `seconds` (even on Ctrl-C)."""
    original = limits_path.read_bytes()
    try:
        limits_path.write_text("limits:\n  free_tier: not-a-number\n", encoding="utf-8")
        sleep(seconds)
    finally:
        limits_path.write_bytes(original)


def _announce(fault: str, target: str, seconds: float) -> None:
    now = datetime.now(timezone.utc)
    print(json.dumps({"fault": fault, "target": target, "started": now.isoformat(),
                      "ends": (now + timedelta(seconds=seconds)).isoformat()}), flush=True)


def run(args: argparse.Namespace) -> None:
    _announce(args.fault, args.target, args.seconds)
    if args.fault == "bad_config":
        bad_config(Path(args.limits), args.seconds)
    elif args.fault == "flood":
        print(json.dumps(asyncio.run(flood(args.base_url, args.count))), flush=True)
    else:
        params = {"ms": args.ms} if args.fault == "latency" else {}
        with httpx.Client(base_url=args.base_url, timeout=10) as client:
            start_fault(client, args.fault, args.target, params)
            try:
                time.sleep(args.seconds)
            finally:
                clear_faults(client)


def build_parser() -> argparse.ArgumentParser:
    from storm.project_faults import HTTP_FAULTS, LIBRARY_FAULTS  # noqa: PLC0415
    p = argparse.ArgumentParser(prog="storm.faults", description=__doc__.splitlines()[0])
    p.add_argument("fault", nargs="?", choices=sorted({*FAULTS, *HTTP_FAULTS, *LIBRARY_FAULTS}))
    p.add_argument("--project", help="project file (JSON or YAML) from the Storm room")
    p.add_argument("--list", action="store_true", help="list the faults that fit the project")
    p.add_argument("--target", choices=("api", "email"), default="api")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--ms", type=int, default=2000, help="latency in milliseconds")
    p.add_argument("--count", type=int, default=200, help="flood request count")
    p.add_argument("--share", type=float, default=0.3, help="share of connections drop discards")
    p.add_argument("--rounds", type=int, default=3, help="probes per endpoint through the proxy")
    p.add_argument("--timeout", type=float, default=1.0, help="client timeout for probes, seconds")
    p.add_argument("--test-timeout", type=float, default=900, help="max seconds per test run")
    p.add_argument("--use-running", action="store_true",
                   help="attack an app already serving base_url instead of starting the branch")
    p.add_argument("--limits", default=str(DEFAULT_LIMITS), help="limits.yaml for bad_config")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.project:
            from storm import project_faults as pf  # noqa: PLC0415
            project = pf.load_project(args.project)
            if args.list or not args.fault:
                faults = pf.available(project)
                mode = "web service" if project.get("run") else "library (tests under faults)"
                print(f"{project.get('name', 'project')}: {mode}")
                for name, what in faults.items():
                    print(f"  {name:12} {what}")
                if not faults:
                    print("  none: the project needs a run command or a test command")
                return
            print(json.dumps(pf.run_fault(args.fault, project, args)), flush=True)
            return
        if args.list or not args.fault:
            print("shopfront demo faults: " + ", ".join(FAULTS) +
                  "\nfor any other project pass --project <file> (see --help)")
            return
        if args.fault not in FAULTS:
            raise SystemExit(f"{args.fault} needs --project <file>")
        run(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
