import re
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

FAST = "claude-haiku-5-5"
SMART = "claude-sonnet-5-5"

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


class LLMParseError(Exception):
    """The model's reply failed schema validation twice."""


class LLM(Protocol):
    async def json(self, model: str, system: str, user: str, schema: type[T]) -> T: ...


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


class ClaudeLLM:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._client = client or anthropic.AsyncAnthropic()

    async def _ask(self, model: str, system: str, user: str) -> str:
        resp = await self._client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    async def json(self, model: str, system: str, user: str, schema: type[T]) -> T:
        try:
            return schema.model_validate_json(_strip_fence(await self._ask(model, system, user)))
        except ValidationError as err:
            retry_user = (
                f"{user}\n\nYour previous reply failed validation: {err}. Reply with JSON only."
            )
        try:
            return schema.model_validate_json(_strip_fence(await self._ask(model, system, retry_user)))
        except ValidationError as err:
            raise LLMParseError(str(err)) from err
