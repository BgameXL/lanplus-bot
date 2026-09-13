from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from colors import dominant_color
from config import Config, load_config
from lyrics import fetch_lrclib
from subsonic import SubsonicClient, SubsonicError, Track

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("navidrome-discord")


def _load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(path: str, data: dict) -> None:
    try:
        Path(path).write_text(json.dumps(data))
    except OSError as exc:
        log.warning("Error saving state to %s: %s", path, exc)


def _safe_id(track_id: str) -> str:
    return "".join(c for c in track_id if c.isalnum()) or "cover"


def _mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _chunk_text(text: str, size: int) -> list[str]:
    chunks: list[str] = []
    while len(text) > size:
        split_at = text.rfind("\n", 0, size)
        if split_at <= 0:
            split_at = size
        else:
            split_at += 1
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks


def make_embed(
        config: Config,
        track: Track | None,
        image_url: str | None,
        color: int,
        liked: bool,
        share_url: str | None = None,
) -> discord.Embed:
    if track is None:
        embed = discord.Embed(
            title="Nothing playing right now",
            description="Silence is also music :P",
            color=config.embed_color,
        )
        embed.set_author(name="Navidrome")
        embed.timestamp = discord.utils.utcnow()
        return embed

    embed = discord.Embed(title=track.title, url=share_url or None, color=color)
    embed.set_author(name="Now listening")
    embed.add_field(name="Artist", value=track.artist or "—", inline=True)
    if track.album:
        embed.add_field(name="Album", value=track.album, inline=True)
    if track.year:
        embed.add_field(name="Year", value=str(track.year), inline=True)
    if track.duration:
        embed.add_field(name="Duration", value=_mmss(track.duration), inline=True)

    if liked:
        embed.add_field(
            name="​",
            value=f"{config.display_name} likes this song!",
            inline=False,
        )

    if image_url:
        embed.set_image(url=image_url)

    footer = []
    if track.player_name:
        footer.append(track.player_name)
    footer.append("Navidrome")
    embed.set_footer(text="  •  ".join(footer))
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_metadata_embed(config: Config, track: Track, song: dict) -> discord.Embed:
    embed = discord.Embed(title=song.get("title") or track.title, color=config.embed_color)
    embed.set_author(name="Metadata")

    def add(name: str, value, inline: bool = True) -> None:
        if value not in (None, "", [], 0):
            embed.add_field(name=name, value=str(value), inline=inline)

    add("Artist", song.get("displayArtist") or song.get("artist") or track.artist)
    add("Album", song.get("album") or track.album)
    album_artist = song.get("displayAlbumArtist")
    if album_artist and album_artist != (song.get("displayArtist") or song.get("artist")):
        add("Album Artist", album_artist)
    add("Year", song.get("year") or track.year)

    genre = song.get("genre")
    if not genre:
        genre = ", ".join(
            g.get("name", "") for g in (song.get("genres") or []) if isinstance(g, dict)
        )
    add("Genre", genre)

    add("Track", song.get("track"))
    add("Disc", song.get("discNumber"))
    add("Duration", _mmss(song["duration"]) if song.get("duration") else None)

    fmt = " / ".join(x for x in [(song.get("suffix") or "").upper(), song.get("contentType") or ""] if x)
    add("Format", fmt)
    add("Bitrate", f"{song['bitRate']} kbps" if song.get("bitRate") else None)
    if song.get("samplingRate"):
        rate = f"{song['samplingRate'] / 1000:.1f} kHz"
        if song.get("bitDepth"):
            rate += f" / {song['bitDepth']}-bit"
        add("Sample rate", rate)
    add("Channels", song.get("channelCount"))
    add("Size", f"{song['size'] / 1048576:.1f} MB" if song.get("size") else None)
    add("BPM", song.get("bpm"))

    isrc = song.get("isrc")
    add("ISRC", ", ".join(isrc) if isinstance(isrc, list) else isrc)
    add("MusicBrainz", song.get("musicBrainzId"))
    add("Comment", song.get("comment"))
    add("Explicit", song.get("explicitStatus"))

    starred = song.get("starred")
    add("Starred", f"{starred[:10]}" if starred else "No")
    add("Added", (song.get("created") or "")[:10])

    embed.set_footer(text="Navidrome")
    embed.timestamp = discord.utils.utcnow()
    return embed


def build_share_embed(track: Track, url: str, image_url: str | None, color: int) -> discord.Embed:
    embed = discord.Embed(title=track.title, url=url, color=color)
    embed.set_author(name="Shared")
    embed.description = url
    embed.add_field(name="Artist", value=track.artist or "—", inline=True)
    if track.album:
        embed.add_field(name="Album", value=track.album, inline=True)
    if track.year:
        embed.add_field(name="Year", value=str(track.year), inline=True)
    if track.duration:
        embed.add_field(name="Duration", value=_mmss(track.duration), inline=True)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="Navidrome")
    embed.timestamp = discord.utils.utcnow()
    return embed


class LibraryView(discord.ui.View):
    def __init__(self, bot: "NowPlayingBot", author_id: int) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author_id
        self.offset = 0
        self.page_size = 10
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your list.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _update(self, interaction: discord.Interaction) -> None:
        embed, has_more = await self.bot._library_page(self.offset, self.page_size)
        self.prev.disabled = self.offset == 0
        self.next.disabled = not has_more
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.offset = max(0, self.offset - self.page_size)
        await self._update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.offset += self.page_size
        await self._update(interaction)


class NowPlayingBot(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
        self.subsonic: SubsonicClient | None = None
        self.target_channel: discord.abc.Messageable | None = None
        self.current_song_id: str | None = None
        self.current_track: Track | None = None
        self.current_color: int = config.embed_color
        self.cover_filename: str | None = None
        self.cover_bytes: bytes | None = None
        self.current_share_url: str | None = None
        self.current_liked: bool = False
        self.last_position_ms: int = 0
        self.now_playing_message: discord.Message | None = None
        self._resume_message: discord.Message | None = None
        self._resume_song_id: str | None = None
        self._restored = False
        self._synced = False

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        self.subsonic = SubsonicClient(
            self.config.navidrome_url,
            self.config.navidrome_user,
            self.config.navidrome_pass,
            self.http_session,
        )
        self._register_commands()
        self.update_now_playing.change_interval(seconds=self.config.poll_interval)
        self.update_now_playing.start()

    async def close(self) -> None:
        self.update_now_playing.cancel()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    def _register_commands(self) -> None:
        @self.tree.command(name="nowplaying", description="What's playing now on Navidrome")
        async def nowplaying(interaction: discord.Interaction) -> None:
            await self._handle_nowplaying(interaction)

        @self.tree.command(name="metadata", description="Full metadata of the current song")
        async def metadata(interaction: discord.Interaction) -> None:
            await self._handle_metadata(interaction)

        @self.tree.command(name="share", description="Public share link for the current song")
        async def share(interaction: discord.Interaction) -> None:
            await self._handle_share(interaction)

        @self.tree.command(name="lyrics", description="Lyrics of the current song")
        async def lyrics(interaction: discord.Interaction) -> None:
            await self._handle_lyrics(interaction)

        @self.tree.command(name="search", description="Search the library")
        @app_commands.describe(query="Title, artist or album to search for")
        async def search(interaction: discord.Interaction, query: str) -> None:
            await self._handle_search(interaction, query)

        @self.tree.command(name="list", description="Browse the library")
        async def list_cmd(interaction: discord.Interaction) -> None:
            await self._handle_list(interaction)

    async def _handle_nowplaying(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            track = await self.subsonic.now_playing(self.config.username_filter)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"Could not reach Navidrome: {exc}")
            return

        is_playing = track is not None and track.minutes_ago <= self.config.idle_after
        if not is_playing or track is None:
            await interaction.followup.send("Nothing playing right now.")
            return

        cover = None
        if track.cover_art:
            try:
                cover = await self.subsonic.cover_art(track.cover_art, self.config.cover_size)
            except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError):
                pass

        color = dominant_color(cover, self.config.embed_color)
        liked = await self._safe_is_starred(track.id)

        filename = None
        file = None
        if cover:
            filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover), filename=filename)

        image_url = f"attachment://{filename}" if filename else None
        share_url = self.current_share_url if track.id == self.current_song_id else None
        embed = make_embed(self.config, track, image_url, color, liked, share_url)
        if file:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

    async def _handle_metadata(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            track = await self.subsonic.now_playing(self.config.username_filter)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"Could not reach Navidrome: {exc}")
            return

        is_playing = track is not None and track.minutes_ago <= self.config.idle_after
        if not is_playing or track is None:
            await interaction.followup.send("Nothing playing right now.")
            return

        try:
            song = await self.subsonic.get_song(track.id)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError):
            song = {}
        await interaction.followup.send(embed=build_metadata_embed(self.config, track, song))

    async def on_ready(self) -> None:
        log.info("Connected as %s (id %s)", self.user, self.user.id)
        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.config.channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                log.error(
                    "Can't access channel %s: %s. Is the bot in the server?",
                    self.config.channel_id,
                    exc,
                )
                return
        self.target_channel = channel

        if not self._restored:
            self._restored = True
            state = _load_state(self.config.state_file)
            if state.get("channel_id") == channel.id and state.get("message_id"):
                try:
                    self._resume_message = await channel.fetch_message(state["message_id"])
                    self._resume_song_id = state.get("song_id")
                except discord.NotFound:
                    self._resume_message = None

        guild = getattr(channel, "guild", None)
        if guild is not None and not self._synced:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                self._synced = True
                log.info("Slash commands synced to guild %s", guild.id)
            except discord.HTTPException as exc:
                log.warning(
                    "Could not sync slash commands (re-invite with the "
                    "applications.commands scope): %s",
                    exc,
                )

    async def _handle_search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        try:
            res = await self.subsonic.search(query)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"Could not reach Navidrome: {exc}")
            return

        def _as_list(value) -> list:
            return [value] if isinstance(value, dict) else (value or [])

        songs = _as_list(res.get("song"))
        albums = _as_list(res.get("album"))
        artists = _as_list(res.get("artist"))
        if not songs and not albums and not artists:
            await interaction.followup.send(f"No results for **{query}**.")
            return

        embed = discord.Embed(title=f"Search: {query}", color=self.config.embed_color)
        if songs:
            lines = [
                f"**{s.get('title')}** — {s.get('artist')} · _{s.get('album')}_"
                f"  `{_mmss(s.get('duration') or 0)}`"
                for s in songs[:10]
            ]
            embed.add_field(name="Songs", value="\n".join(lines)[:1024], inline=False)
        if albums:
            embed.add_field(
                name="Albums",
                value="\n".join(f"**{a.get('name')}** — {a.get('artist')}" for a in albums[:5])[:1024],
                inline=False,
            )
        if artists:
            embed.add_field(
                name="Artists",
                value=", ".join(a.get("name", "") for a in artists[:5])[:1024],
                inline=False,
            )
        embed.set_footer(text="Navidrome")
        await interaction.followup.send(embed=embed)

    async def _handle_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        view = LibraryView(self, interaction.user.id)
        embed, has_more = await self._library_page(view.offset, view.page_size)
        view.prev.disabled = True
        view.next.disabled = not has_more
        view.message = await interaction.followup.send(embed=embed, view=view)

    async def _library_page(self, offset: int, size: int) -> tuple[discord.Embed, bool]:
        try:
            albums = await self.subsonic.album_list(offset, size)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Error listing library: %s", exc)
            albums = []
        embed = discord.Embed(title="Library - albums", color=self.config.embed_color)
        if not albums:
            embed.description = "No albums here."
        else:
            lines = []
            for i, album in enumerate(albums, start=offset + 1):
                year = f" ({album.get('year')})" if album.get("year") else ""
                count = album.get("songCount")
                tail = f" · {count} tracks" if count else ""
                lines.append(
                    f"`{i:>4}.` **{album.get('name')}** — {album.get('artist')}{year}{tail}"
                )
            embed.description = "\n".join(lines)[:4096]
        embed.set_footer(text=f"Navidrome • from #{offset + 1}")
        return embed, len(albums) == size

    async def _safe_is_starred(self, song_id: str) -> bool:
        try:
            return await self.subsonic.is_starred(song_id)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Couldn't check favorite: %s", exc)
            return self.current_liked

    async def _safe_create_share(self, song_id: str) -> str | None:
        expires_ms = None
        if self.config.share_expires_days > 0:
            expires_ms = int((time.time() + self.config.share_expires_days * 86400) * 1000)
        try:
            return await self.subsonic.create_share(song_id, expires_ms)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Couldn't create share: %s", exc)
            return None

    async def _handle_share(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            track = await self.subsonic.now_playing(self.config.username_filter)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"Could not reach Navidrome: {exc}")
            return
        is_playing = track is not None and track.minutes_ago <= self.config.idle_after
        if not is_playing or track is None:
            await interaction.followup.send("Nothing playing right now.")
            return
        if track.id == self.current_song_id and self.current_share_url:
            url = self.current_share_url
        else:
            url = await self._safe_create_share(track.id)
        if not url:
            await interaction.followup.send(
                "Couldn't create a share link (is sharing enabled in Navidrome?)."
            )
            return

        cover = None
        if track.cover_art:
            try:
                cover = await self.subsonic.cover_art(track.cover_art, self.config.cover_size)
            except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError):
                pass
        color = dominant_color(cover, self.config.embed_color)

        file = None
        image_url = None
        if cover:
            filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover), filename=filename)
            image_url = f"attachment://{filename}"
        embed = build_share_embed(track, url, image_url, color)
        if file:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

    async def _handle_lyrics(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            track = await self.subsonic.now_playing(self.config.username_filter)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"Could not reach Navidrome: {exc}")
            return
        is_playing = track is not None and track.minutes_ago <= self.config.idle_after
        if not is_playing or track is None:
            await interaction.followup.send("Nothing playing right now.")
            return

        try:
            text = await self.subsonic.get_lyrics(track.id)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            text = None
        if not text:
            text = await fetch_lrclib(
                self.http_session, track.artist, track.title, track.album, track.duration
            )
        if not text:
            await interaction.followup.send(
                f"No lyrics found for **{track.artist} — {track.title}**."
            )
            return

        for page, chunk in enumerate(_chunk_text(text, 4000)):
            embed = discord.Embed(description=chunk, color=self.current_color)
            if page == 0:
                embed.set_author(name="Lyrics")
                await interaction.followup.send(
                    content=f"**{track.artist} — {track.title}**", embed=embed
                )
            else:
                await interaction.followup.send(embed=embed)

    def _previous_ended_normally(self) -> bool:
        if self.current_track is None or not self.current_track.duration:
            return False
        duration_ms = self.current_track.duration * 1000
        return self.last_position_ms >= duration_ms - 20000

    @tasks.loop(seconds=15)
    async def update_now_playing(self) -> None:
        if self.target_channel is None or self.subsonic is None:
            return
        try:
            track = await self.subsonic.now_playing(self.config.username_filter)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Error consulting Navidrome: %s", exc)
            return

        is_playing = track is not None and track.minutes_ago <= self.config.idle_after
        song_id = track.id if (is_playing and track) else None

        if song_id != self.current_song_id:
            ended_normally = self._previous_ended_normally()
            await self._on_song_change(track, is_playing, song_id, ended_normally)
        elif is_playing and track is not None:
            self.last_position_ms = track.position_ms
            if self.now_playing_message is not None:
                liked = await self._safe_is_starred(song_id)
                if liked != self.current_liked:
                    self.current_liked = liked
                    image_url = (
                        f"attachment://{self.cover_filename}" if self.cover_filename else None
                    )
                    embed = make_embed(
                        self.config, self.current_track, image_url,
                        self.current_color, liked, self.current_share_url,
                    )
                    attachments = []
                    if self.cover_bytes and self.cover_filename:
                        attachments = [
                            discord.File(io.BytesIO(self.cover_bytes), filename=self.cover_filename)
                        ]
                    try:
                        self.now_playing_message = await self.now_playing_message.edit(
                            embed=embed, attachments=attachments
                        )
                    except discord.NotFound:
                        self.now_playing_message = None
                    except discord.HTTPException as exc:
                        log.warning("HTTP error editing entry: %s", exc)

    async def _on_song_change(self, track, is_playing, song_id, ended_normally) -> None:
        if not (is_playing and track):
            self.current_song_id = None
            self.current_track = None
            self.cover_filename = None
            self.cover_bytes = None
            self.current_share_url = None
            self.current_liked = False
            self.last_position_ms = 0
            self.now_playing_message = None
            await self._clear_presence()
            log.info("Idle (nothing playing).")
            return

        resume = self._resume_message if (
                self._resume_message is not None and song_id == self._resume_song_id
        ) else None
        self._resume_message = None
        self._resume_song_id = None
        target = resume
        if target is None and ended_normally and self.now_playing_message is not None:
            target = self.now_playing_message

        retrying_song = self.current_track is not None and self.current_track.id == song_id
        self.current_track = track
        self.last_position_ms = track.position_ms
        cover_bytes = None
        if track.cover_art:
            try:
                cover_bytes = await self.subsonic.cover_art(track.cover_art, self.config.cover_size)
            except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Couldn't download cover: %s", exc)
        self.cover_bytes = cover_bytes
        self.current_color = dominant_color(cover_bytes, self.config.embed_color)
        self.current_liked = await self._safe_is_starred(song_id)
        if not retrying_song:
            self.current_share_url = await self._safe_create_share(song_id)
        await self._set_presence(track)

        file = None
        self.cover_filename = None
        image_url = None
        if cover_bytes:
            self.cover_filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover_bytes), filename=self.cover_filename)
            image_url = f"attachment://{self.cover_filename}"
        embed = make_embed(
            self.config, track, image_url, self.current_color,
            self.current_liked, self.current_share_url,
        )

        try:
            if target is not None:
                try:
                    self.now_playing_message = await target.edit(
                        embed=embed, attachments=[file] if file else []
                    )
                except discord.NotFound:
                    kwargs = {"embed": embed}
                    if cover_bytes:
                        kwargs["file"] = discord.File(
                            io.BytesIO(cover_bytes), filename=self.cover_filename or "cover.jpg"
                        )
                    self.now_playing_message = await self.target_channel.send(**kwargs)
            else:
                kwargs = {"embed": embed}
                if file is not None:
                    kwargs["file"] = file
                self.now_playing_message = await self.target_channel.send(**kwargs)
        except discord.Forbidden as exc:
            log.error("No permissions to post in the channel: %s", exc)
            self.now_playing_message = None
            return
        except discord.HTTPException as exc:
            log.warning("HTTP error posting entry: %s", exc)
            self.now_playing_message = None
            return

        self.current_song_id = song_id
        _save_state(self.config.state_file, {
            "channel_id": self.target_channel.id,
            "message_id": self.now_playing_message.id,
            "song_id": song_id,
        })
        log.info(
            "Now listening: %s - %s%s",
            track.artist, track.title, " (fav)" if self.current_liked else "",
        )

    async def _set_presence(self, track: Track) -> None:
        name = f"{track.title} · {track.artist}"[:128]
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name=name)
            )
        except discord.HTTPException as exc:
            log.warning("Couldn't set presence: %s", exc)

    async def _clear_presence(self) -> None:
        try:
            await self.change_presence(activity=None)
        except discord.HTTPException as exc:
            log.warning("Couldn't clear presence: %s", exc)

    @update_now_playing.before_loop
    async def _before_update(self) -> None:
        await self.wait_until_ready()


def main() -> None:
    config = load_config()
    bot = NowPlayingBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
