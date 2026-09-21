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


async def run() -> None:
    client_id = _require("RPC_CLIENT_ID")
    base_url = _require("NAVIDROME_URL").rstrip("/")
    user = _require("NAVIDROME_USER")
    password = _require("NAVIDROME_PASS")
    username_filter = os.getenv("NAVIDROME_USERNAME_FILTER") or user
    large_image = os.getenv("RPC_LARGE_IMAGE") or None
    idle_after = int(os.getenv("IDLE_AFTER_MINUTES", "5"))
    poll = max(5, int(os.getenv("POLL_INTERVAL", "15")))

    rpc = AioPresence(client_id)
    await _connect(rpc)
    print("Connected to Discord")

    async with aiohttp.ClientSession() as session:
        subsonic = SubsonicClient(base_url, user, password, session)
        current: object = object()
        while True:
            try:
                track = await subsonic.now_playing(username_filter)
            except NETWORK_ERRORS:
                track = None

            playing = track is not None and track.minutes_ago <= idle_after
            key = track.id if playing else None

            if key != current:
                current = key
                try:
                    if playing:
                        start = (
                            int(time.time() - track.position_ms / 1000)
                            if track.position_ms
                            else int(time.time())
                        )
                        fields = {
                            "activity_type": 2,
                            "details": track.title,
                            "state": track.artist,
                            "start": start,
                        }
                        if track.duration:
                            fields["end"] = start + track.duration
                        if large_image:
                            fields["large_image"] = large_image
                            fields["large_text"] = track.album or track.title
                        await rpc.update(**fields)
                        print(f"Now listening: {track.artist} - {track.title}")
                    else:
                        await rpc.clear()
                        print("Idle")
                except RPC_ERRORS:
                    current = object()
                    await _connect(rpc)

            await asyncio.sleep(poll)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
