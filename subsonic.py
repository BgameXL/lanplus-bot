from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import aiohttp

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "navidrome-discord"


class SubsonicError(Exception):
    pass


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str
    cover_art: str | None
    duration: int | None
    year: int | None
    username: str
    minutes_ago: int
    player_name: str | None
    position_ms: int = 0


class SubsonicClient:
    def __init__(
            self,
            base_url: str,
            user: str,
            password: str,
            session: aiohttp.ClientSession,
            *,
            timeout: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._user = user
        self._password = password
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5(
            (self._password + salt).encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        return {
            "u": self._user,
            "t": token,
            "s": salt,
            "v": SUBSONIC_API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    async def _get_json(self, endpoint: str, extra: dict | None = None) -> dict:
        params = self._auth_params()
        if extra:
            params.update(extra)
        url = f"{self._base}/rest/{endpoint}"
        async with self._session.get(url, params=params, timeout=self._timeout) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        body = data.get("subsonic-response", {})
        if body.get("status") != "ok":
            error = body.get("error", {})
            raise SubsonicError(error.get("message", "unknown error"))
        return body

    async def ping(self) -> None:
        await self._get_json("ping.view")

    async def now_playing(self, username_filter: str | None = None) -> Track | None:
        body = await self._get_json("getNowPlaying.view")
        now = body.get("nowPlaying") or {}
        entries = now.get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]

        tracks = [self._parse_entry(e) for e in entries]
        if username_filter:
            tracks = [t for t in tracks if t.username.lower() == username_filter.lower()]
        if not tracks:
            return None
        tracks.sort(key=lambda t: t.minutes_ago)
        return tracks[0]

    async def cover_art(self, cover_id: str, size: int = 512) -> bytes | None:
        params = self._auth_params()
        params.update({"id": cover_id, "size": str(size)})
        url = f"{self._base}/rest/getCoverArt.view"
        async with self._session.get(url, params=params, timeout=self._timeout) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                return None
            return await resp.read()

    async def get_song(self, song_id: str) -> dict:
        body = await self._get_json("getSong.view", {"id": song_id})
        return body.get("song") or {}

    async def is_starred(self, song_id: str) -> bool:
        song = await self.get_song(song_id)
        return bool(song.get("starred"))

    @staticmethod
    def _parse_entry(entry: dict) -> Track:
        def _as_int(value) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return Track(
            id=str(entry.get("id", "")),
            title=entry.get("title") or "Unknown title",
            artist=entry.get("artist") or "Unknown artist",
            album=entry.get("album") or "",
            cover_art=entry.get("coverArt"),
            duration=_as_int(entry.get("duration")),
            year=_as_int(entry.get("year")),
            username=entry.get("username") or "",
            minutes_ago=_as_int(entry.get("minutesAgo")) or 0,
            player_name=entry.get("playerName"),
            position_ms=_as_int(entry.get("positionMs")) or 0,
        )
