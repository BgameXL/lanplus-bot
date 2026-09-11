from __future__ import annotations

import asyncio

import aiohttp

from config import load_config
from subsonic import SubsonicClient, SubsonicError


async def run() -> None:
    config = load_config()
    print(f"Connected to {config.navidrome_url} as {config.navidrome_user}…")
    async with aiohttp.ClientSession() as session:
        client = SubsonicClient(
            config.navidrome_url,
            config.navidrome_user,
            config.navidrome_pass,
            session,
        )
        try:
            await client.ping()
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"X Could not connect/authenticate: {exc}")
            return
        print("Connection and credentials OK.")

        track = await client.now_playing(config.username_filter)
        if track is None:
            print(
                "Currently nothing is playing "
                "(or your client/player does not report 'now playing')."
            )
        else:
            print(
                f"{track.artist} — {track.title}  [{track.album}]\n"
                f"   user={track.username}  player={track.player_name}  "
                f"played {track.minutes_ago} min ago"
            )


if __name__ == "__main__":
    asyncio.run(run())
