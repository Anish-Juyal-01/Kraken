from typing import Any

from pydantic import BaseModel

from kraken.llm import LLM, T


class FakeLLM(LLM):
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def json(self, model: str, system: str, user: str, schema: type[T]) -> T:
        self.calls.append({"model": model, "system": system, "user": user, "schema": schema})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, str):
            return schema.model_validate_json(resp)
        assert isinstance(resp, BaseModel)
        return resp
