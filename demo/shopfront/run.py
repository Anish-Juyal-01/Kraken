"""Start the shopfront API (8080) and a trivial email stub (8081)."""
import asyncio
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api.main import app  # noqa: E402

email_app = FastAPI(title="Email stub")


@email_app.post("/send")
async def send(body: dict):
    return {"queued": True}


async def serve() -> None:
    servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, log_level="warning"))
               for a, p in ((app, 8080), (email_app, 8081))]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(serve())
