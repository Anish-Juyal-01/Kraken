"""Standalone Discord bot: forwards human emoji reactions on [K-n] prompts to Kraken core."""
import logging
import os
import re

import discord
import httpx

from kraken.config import load_config

log = logging.getLogger("kraken.relay")
_TAG = re.compile(r"\[K-(\d+)\]")


def extract_tag(content: str) -> int | None:
    m = _TAG.search(content or "")
    return int(m.group(1)) if m else None


def build_payload(content: str, emoji: str, user: str) -> dict | None:
    tag = extract_tag(content)
    return None if tag is None else {"tag": tag, "emoji": emoji, "user": user}


def make_client(url: str, secret: str) -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_reactions = True
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
        try:
            member = payload.member
            if member is None or member.bot:
                return
            channel = client.get_channel(payload.channel_id) or await client.fetch_channel(
                payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            body = build_payload(message.content, str(payload.emoji), member.display_name)
            if body is None:
                return
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(url, json=body, headers={"X-Kraken-Secret": secret})
            if resp.status_code not in (200, 204):
                log.warning("relay POST returned %s", resp.status_code)
        except Exception:
            log.exception("reaction relay failed")

    return client


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = load_config()
    url = f"http://127.0.0.1:{cfg.relay_port}/relay/reaction"
    make_client(url, os.environ["KRAKEN_RELAY_SECRET"]).run(os.environ["DISCORD_RELAY_TOKEN"])


if __name__ == "__main__":
    main()
