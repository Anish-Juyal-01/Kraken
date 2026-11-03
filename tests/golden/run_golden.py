"""Golden-set evaluation of Sonar with the real Claude model.

Usage (from the repo root): python tests/golden/run_golden.py
Needs ANTHROPIC_API_KEY (env or .env). Exit codes: 0 ok, 1 precision below 0.9, 2 no API key.
"""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kraken.config import load_config  # noqa: E402
from kraken.hallmark.ledger import Ledger  # noqa: E402
from kraken.llm import ClaudeLLM  # noqa: E402
from kraken.main import load_env  # noqa: E402
from kraken.models import Event  # noqa: E402
from kraken.sonar.detect import detect, judge, norm_value  # noqa: E402
from kraken.sonar.extract import extract_claims  # noqa: E402
from kraken.sonar.normalize import normalize  # noqa: E402

LABELS = ("conflict", "update", "compatible", "chitchat")
PRECISION_FLOOR = 0.9
PAIRS_PATH = ROOT / "tests" / "golden" / "pairs.yaml"


def classify(conflict_raised: bool, b_claim_count: int, verdicts: list[str]) -> str:
    """Map a pipeline outcome for message B to a label.

    verdicts are the judge verdicts for B's claims against earlier claims on the same
    subject/attribute that did not raise a conflict. A below-threshold conflict verdict
    counts as compatible because Sonar stays silent on it.
    """
    if conflict_raised:
        return "conflict"
    if b_claim_count == 0:
        return "chitchat"
    return "update" if "update" in verdicts else "compatible"


def score(confusion: Counter, positive: str = "conflict") -> tuple[float, float]:
    """Precision and recall for `positive` from a Counter keyed by (expected, predicted)."""
    tp = confusion[(positive, positive)]
    predicted = sum(n for (_, p), n in confusion.items() if p == positive)
    actual = sum(n for (e, _), n in confusion.items() if e == positive)
    return (tp / predicted if predicted else 0.0, tp / actual if actual else 0.0)


def format_confusion(confusion: Counter) -> str:
    width = 12
    rows = ["expected \\ got".ljust(width + 2) + "".join(p.rjust(width) for p in LABELS)]
    for e in LABELS:
        rows.append(e.ljust(width + 2) + "".join(str(confusion[(e, p)]).rjust(width) for p in LABELS))
    return "\n".join(rows)


def _event(body: str, sender: str, n: int) -> Event:
    return Event(type="message", room_id="golden", sequence=n,
                 payload={"body": body, "sender": sender, "message_id": f"m{n}", "timestamp": n})


async def predict(llm, pair: dict, threshold: float, min_words: int) -> str:
    ledger = Ledger(":memory:")
    for n, key in enumerate(("a", "b"), start=1):
        claims = await extract_claims(llm, _event(pair[key], f"user_{key}", n), min_words)
        added = []
        for c in claims:
            c = await normalize(llm, ledger, c)
            added.append((ledger.add_claim(c), c))
        if key == "a":
            continue
        raised, verdicts = [], []
        for cid, c in added:
            found = await detect(llm, ledger, cid, c, threshold)
            raised += found
            if found:
                continue
            for earlier_id, earlier in ledger.claims_for(c.subject, c.attribute):
                if earlier_id != cid and norm_value(earlier.value) != norm_value(c.value):
                    verdicts.append((await judge(llm, earlier, c)).verdict)
        return classify(bool(raised), len(added), verdicts)
    return "chitchat"


async def run(pairs: list[dict]) -> Counter:
    cfg = load_config(str(ROOT / "kraken.yaml"))
    llm = ClaudeLLM()
    confusion: Counter = Counter()
    for i, pair in enumerate(pairs, start=1):
        got = await predict(llm, pair, cfg.sonar.threshold, cfg.sonar.min_words)
        confusion[(pair["label"], got)] += 1
        mark = "ok  " if got == pair["label"] else "MISS"
        print(f"{mark} {i:2d} expected={pair['label']:<10} got={got:<10} {pair['b'][:60]}")
    return confusion


def main() -> int:
    load_env(str(ROOT / ".env"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (env or .env); the golden set needs the real model.",
              file=sys.stderr)
        return 2
    pairs = yaml.safe_load(PAIRS_PATH.read_text(encoding="utf-8"))
    confusion = asyncio.run(run(pairs))
    print()
    print(format_confusion(confusion))
    precision, recall = score(confusion)
    print(f"\nconflict precision={precision:.2f} recall={recall:.2f} (floor {PRECISION_FLOOR})")
    return 0 if precision >= PRECISION_FLOOR else 1


if __name__ == "__main__":
    sys.exit(main())
