from __future__ import annotations

import asyncio
import sys

import aiohttp

from config import load_config
from subsonic import SubsonicClient


def _safe_name(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in " -_().").strip() or "track"


async def run(query: str) -> None:
    config = load_config()
    async with aiohttp.ClientSession() as session:
        client = SubsonicClient(
            config.navidrome_url,
            config.navidrome_user,
            config.navidrome_pass,
            session,
        )
        result = await client.search(query, song_count=5)
        songs = result.get("song") or []
        if isinstance(songs, dict):
            songs = [songs]
        if not songs:
            print(f"No match for {query!r}")
            return

        song = songs[0]
        song_id = song["id"]
        full = await client.get_song(song_id)
        suffix = full.get("suffix") or "bin"
        artist = full.get("artist") or "Unknown"
        title = full.get("title") or song_id
        filename = f"{_safe_name(f'{artist} - {title}')}.{suffix}"

        print(f"Downloading: {artist} - {title}")
        data = await client.download(song_id)
        with open(filename, "wb") as out:
            out.write(data)
        print(f"Saved {filename} ({len(data) // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pull.py <search terms>")
        sys.exit(1)
    asyncio.run(run(" ".join(sys.argv[1:])))
