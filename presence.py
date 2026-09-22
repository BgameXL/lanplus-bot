from __future__ import annotations

import asyncio
import os
import time

import aiohttp
from dotenv import load_dotenv
from pypresence import AioPresence
from pypresence.exceptions import PyPresenceException

from subsonic import SubsonicClient, SubsonicError

load_dotenv()

NETWORK_ERRORS = (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError)
RPC_ERRORS = (PyPresenceException, ConnectionError, OSError, RuntimeError)
REASSERT_SECONDS = 30


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name}. Set it in .env")
    return value


async def _connect(rpc: AioPresence) -> None:
    while True:
        try:
            await rpc.connect()
            return
        except RPC_ERRORS:
            print("Waiting for Discord desktop...")
            await asyncio.sleep(10)


def build_fields(subsonic: SubsonicClient, track, large_image: str | None) -> dict:
    fields = {
        "activity_type": 2,
        "details": track.title,
        "state": track.artist,
    }
    if track.cover_art:
        fields["large_image"] = subsonic.cover_art_url(track.cover_art)
        fields["large_text"] = track.album or track.title
    elif large_image:
        fields["large_image"] = large_image
        fields["large_text"] = track.album or track.title
    if track.state == "paused":
        fields["state"] = f"{track.artist} (paused)"
    else:
        start = (
            int(time.time() - track.position_ms / 1000)
            if track.position_ms
            else int(time.time())
        )
        fields["start"] = start
        if track.duration:
            fields["end"] = start + track.duration
    return fields


async def run() -> None:
    client_id = _require("RPC_CLIENT_ID")
    base_url = _require("NAVIDROME_URL").rstrip("/")
    user = _require("NAVIDROME_USER")
    password = _require("NAVIDROME_PASS")
    username_filter = os.getenv("NAVIDROME_USERNAME_FILTER") or user
    large_image = os.getenv("RPC_LARGE_IMAGE") or None
    idle_after = int(os.getenv("IDLE_AFTER_MINUTES", "5"))
    poll = max(1, int(os.getenv("RPC_POLL_INTERVAL", "2")))

    rpc = AioPresence(client_id)
    await _connect(rpc)
    print("Connected to Discord")

    async with aiohttp.ClientSession() as session:
        subsonic = SubsonicClient(base_url, user, password, session)
        current: object = object()
        last_fields: dict | None = None
        last_sent = 0.0
        while True:
            try:
                track = await subsonic.now_playing(username_filter)
            except NETWORK_ERRORS:
                await asyncio.sleep(poll)
                continue

            playing = track is not None and track.minutes_ago <= idle_after
            key = (track.id, track.state) if playing else None
            stale = last_fields is not None and time.monotonic() - last_sent > REASSERT_SECONDS

            if key != current:
                try:
                    if playing:
                        fields = build_fields(subsonic, track, large_image)
                        if track.state == "paused":
                            await rpc.clear()
                        await rpc.update(**fields)
                        last_fields = fields
                        print(f"{track.artist} - {track.title}")
                    else:
                        await rpc.clear()
                        last_fields = None
                        print("Idle")
                    current = key
                    last_sent = time.monotonic()
                except RPC_ERRORS:
                    current = object()
                    last_fields = None
                    await _connect(rpc)
            elif stale:
                try:
                    await rpc.update(**last_fields)
                    last_sent = time.monotonic()
                except RPC_ERRORS:
                    current = object()
                    last_fields = None
                    await _connect(rpc)

            await asyncio.sleep(poll)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
