from types import SimpleNamespace

import pytest


class StubClient:
    """Stands in for anthropic.AsyncAnthropic: pops text from .replies, records calls."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        user = kwargs["messages"][-1]["content"]
        self.requests.append({**kwargs, "user": user})
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture
def stub_client() -> StubClient:
    return StubClient()
