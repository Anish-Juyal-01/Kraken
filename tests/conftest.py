from types import SimpleNamespace

import pytest


class StubClient:
    """Stands in for anthropic.AsyncAnthropic: pops text from .replies, records calls."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.stop_reasons: list[str] = []  # popped per call; "end_turn" when empty
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        user = kwargs["messages"][-1]["content"]
        self.requests.append({**kwargs, "user": user})
        text = self.replies.pop(0)
        stop = self.stop_reasons.pop(0) if self.stop_reasons else "end_turn"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               stop_reason=stop)


@pytest.fixture
def stub_client() -> StubClient:
    return StubClient()


@pytest.fixture
def open_ledger():
    """Factory for extra Ledgers; every one is closed at teardown."""
    from kraken.hallmark.ledger import Ledger

    opened = []

    def make(path: str) -> Ledger:
        opened.append(Ledger(path))
        return opened[-1]

    yield make
    for led in opened:
        led.close()


@pytest.fixture
def ledger(tmp_path, open_ledger):
    return open_ledger(str(tmp_path / "ledger.db"))
