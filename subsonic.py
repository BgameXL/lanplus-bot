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

    async def cover_art(self, cover_id: str, size: int = 1000) -> bytes | None:
        params = self._auth_params()
        params["id"] = cover_id
        if size and size > 0:
            params["size"] = str(size)
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

    async def create_share(self, song_id: str, expires_ms: int | None = None) -> str | None:
        extra = {"id": song_id}
        if expires_ms:
            extra["expires"] = str(expires_ms)
        try:
            body = await self._get_json("createShare.view", extra)
        except SubsonicError:
            return None
        shares = (body.get("shares") or {}).get("share") or []
        if isinstance(shares, dict):
            shares = [shares]
        return shares[0].get("url") if shares else None

    async def get_lyrics(self, song_id: str) -> str | None:
        try:
            body = await self._get_json("getLyricsBySongId.view", {"id": song_id})
        except SubsonicError:
            return None
        structured = (body.get("lyricsList") or {}).get("structuredLyrics") or []
        if not structured:
            return None
        lines = structured[0].get("line") or []
        text = "\n".join(ln.get("value", "") for ln in lines).strip()
        return text or None

    async def search(self, query: str, song_count: int = 10,
                     album_count: int = 5, artist_count: int = 5) -> dict:
        body = await self._get_json("search3.view", {
            "query": query,
            "songCount": str(song_count),
            "albumCount": str(album_count),
            "artistCount": str(artist_count),
        })
        return body.get("searchResult3") or {}

    async def album_list(self, offset: int = 0, size: int = 10,
                         list_type: str = "alphabeticalByName") -> list[dict]:
        body = await self._get_json("getAlbumList2.view", {
            "type": list_type,
            "size": str(size),
            "offset": str(offset),
        })
        albums = (body.get("albumList2") or {}).get("album") or []
        if isinstance(albums, dict):
            albums = [albums]
        return albums

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
