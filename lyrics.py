from __future__ import annotations

import aiohttp

_BASE = "https://lrclib.net"
_UA = "navidrome-discord (https://github.com/BgameXL/lanplus-bot)"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _plain(entry: dict | None) -> str | None:
    if not entry:
        return None
    text = (entry.get("plainLyrics") or "").strip()
    return text or None


async def fetch_lrclib(
        session: aiohttp.ClientSession,
        artist: str,
        title: str,
        album: str | None = None,
        duration: int | None = None,
) -> str | None:
    headers = {"User-Agent": _UA}
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(duration)
    async with session.get(
            f"{_BASE}/api/get", params=params, headers=headers, timeout=_TIMEOUT
    ) as resp:
        if resp.status == 200:
            text = _plain(await resp.json())
            if text:
                return text
    async with session.get(
            f"{_BASE}/api/search",
            params={"track_name": title, "artist_name": artist},
            headers=headers,
            timeout=_TIMEOUT,
    ) as resp:
        if resp.status == 200:
            for entry in (await resp.json()) or []:
                text = _plain(entry)
                if text:
                    return text

    return None
