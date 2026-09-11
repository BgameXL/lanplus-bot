from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(
            f"Missing environment variable {name}. "
            "Copy .env.example to .env and complete it."
        )
    return value


def _parse_color(raw: str | None, default: int = 0x5865F2) -> int:
    if not raw:
        return default
    cleaned = raw.strip().lstrip("#").lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    try:
        return int(cleaned, 16)
    except ValueError:
        return default


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} needs to be an integer (got: {raw!r}).")


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    navidrome_url: str
    navidrome_user: str
    navidrome_pass: str
    username_filter: str | None
    poll_interval: int
    idle_after: int
    embed_color: int
    cover_size: int
    state_file: str


def load_config() -> Config:
    token = _required("DISCORD_TOKEN")

    channel_raw = _required("DISCORD_CHANNEL_ID")
    try:
        channel_id = int(channel_raw)
    except ValueError:
        raise SystemExit("DISCORD_CHANNEL_ID needs to be the numeric ID of the channel.")

    user = _required("NAVIDROME_USER")

    return Config(
        discord_token=token,
        channel_id=channel_id,
        navidrome_url=_required("NAVIDROME_URL").rstrip("/"),
        navidrome_user=user,
        navidrome_pass=_required("NAVIDROME_PASS"),
        username_filter=os.getenv("NAVIDROME_USERNAME_FILTER") or user,
        poll_interval=max(5, _parse_int("POLL_INTERVAL", 15)),
        idle_after=_parse_int("IDLE_AFTER_MINUTES", 10),
        embed_color=_parse_color(os.getenv("EMBED_COLOR")),
        cover_size=_parse_int("COVER_SIZE", 512),
        state_file=os.getenv("STATE_FILE", "state.json"),
    )
