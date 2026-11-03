"""Storm fault toolkit: `python -m storm.faults <fault> [--target api|email] [--seconds N]`."""
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


def start_fault(client, fault: str, target: str = "api", params: dict | None = None) -> None:
    """Enable a server-side fault via the control endpoint (client: httpx.Client or TestClient)."""
    resp = client.post("/__chaos", json={"fault": fault, "target": target, "params": params or {}})
    resp.raise_for_status()


def clear_faults(client) -> None:
    client.delete("/__chaos").raise_for_status()


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
    p = argparse.ArgumentParser(prog="storm.faults")
    p.add_argument("fault", choices=FAULTS)
    p.add_argument("--target", choices=("api", "email"), default="api")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--ms", type=int, default=2000, help="latency in milliseconds")
    p.add_argument("--count", type=int, default=200, help="flood request count")
    p.add_argument("--limits", default=str(DEFAULT_LIMITS), help="limits.yaml for bad_config")
    return p


def main(argv: list[str] | None = None) -> None:
    try:
        run(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
