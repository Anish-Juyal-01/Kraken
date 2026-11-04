"""A plain app with no chaos hooks, for testing Storm from the outside.
Reads config/settings.yaml at start; POST /items/{i} has a real bug on bad input."""
from pathlib import Path

import yaml
from fastapi import FastAPI, Request

SETTINGS = yaml.safe_load((Path(__file__).parent / "config" / "settings.yaml").read_text())
app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True, "env": SETTINGS["env"]}


@app.get("/items/{i}")
def item(i: int):
    return {"id": i}


@app.post("/items/{i}")
async def rename(i: int, request: Request):
    body = await request.json()  # bug: invalid JSON or a missing/odd "name" -> 500
    return {"id": i, "name": body["name"].upper()}
