from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from colors import dominant_color
from config import Config, load_config
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


def make_embed(
        config: Config,
        track: Track | None,
        image_url: str | None,
        color: int,
        liked: bool,
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

    embed = discord.Embed(title=track.title, color=color)
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
        footer.append(f"▶ {track.player_name}")
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
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

        color = dominant_color(cover, self.config.embed_color)
        liked = await self._safe_is_starred(track.id)

        filename = None
        file = None
        if cover:
            filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover), filename=filename)

        image_url = f"attachment://{filename}" if filename else None
        embed = make_embed(self.config, track, image_url, color, liked)
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

    async def _safe_is_starred(self, song_id: str) -> bool:
        try:
            return await self.subsonic.is_starred(song_id)
        except (SubsonicError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Couldn't check favorite: %s", exc)
            return self.current_liked

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
                        self.current_color, liked,
                    )
                    try:
                        self.now_playing_message = await self.now_playing_message.edit(
                            embed=embed, attachments=self.now_playing_message.attachments
                        )
                    except discord.NotFound:
                        self.now_playing_message = None
                    except discord.HTTPException as exc:
                        log.warning("HTTP error editing entry: %s", exc)

    async def _on_song_change(self, track, is_playing, song_id, ended_normally) -> None:
        self.current_song_id = song_id

        if not (is_playing and track):
            self.current_track = None
            self.cover_filename = None
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

        self.current_track = track
        self.last_position_ms = track.position_ms
        cover_bytes = None
        if track.cover_art:
            try:
                cover_bytes = await self.subsonic.cover_art(track.cover_art, self.config.cover_size)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Couldn't download cover: %s", exc)
        self.current_color = dominant_color(cover_bytes, self.config.embed_color)
        self.current_liked = await self._safe_is_starred(song_id)
        await self._set_presence(track)

        file = None
        self.cover_filename = None
        image_url = None
        if cover_bytes:
            self.cover_filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover_bytes), filename=self.cover_filename)
            image_url = f"attachment://{self.cover_filename}"
        embed = make_embed(self.config, track, image_url, self.current_color, self.current_liked)

        try:
            if target is not None:
                self.now_playing_message = await target.edit(
                    embed=embed, attachments=[file] if file else []
                )
            else:
                kwargs = {"embed": embed}
                if file is not None:
                    kwargs["file"] = file
                self.now_playing_message = await self.target_channel.send(**kwargs)
        except discord.Forbidden as exc:
            log.error("No permissions to post in the channel: %s", exc)
            self.now_playing_message = None
            return
        except discord.NotFound:
            kwargs = {"embed": embed}
            if cover_bytes:
                kwargs["file"] = discord.File(
                    io.BytesIO(cover_bytes), filename=self.cover_filename or "cover.jpg"
                )
            self.now_playing_message = await self.target_channel.send(**kwargs)
        except discord.HTTPException as exc:
            log.warning("HTTP error posting entry: %s", exc)
            self.now_playing_message = None
            return

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
