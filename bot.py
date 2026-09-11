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


def _progress_bar(elapsed: int, total: int, length: int = 18) -> str:
    if not total or total <= 0:
        return ""
    frac = min(max(elapsed / total, 0.0), 1.0)
    pos = round(frac * (length - 1))
    return "─" * pos + "🔘" + "─" * (length - 1 - pos)


def make_embed(
        config: Config,
        track: Track | None,
        image_url: str | None,
        elapsed: int,
        color: int,
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

    if track.duration:
        bar = _progress_bar(elapsed, track.duration)
        embed.description = f"`{_mmss(elapsed)}` {bar} `{_mmss(track.duration)}`"

    embed.add_field(name="Artist", value=track.artist or "—", inline=True)
    if track.album:
        embed.add_field(name="Album", value=track.album, inline=True)
    if track.year:
        embed.add_field(name="Year", value=str(track.year), inline=True)

    if image_url:
        embed.set_image(url=image_url)

    footer = []
    if track.player_name:
        footer.append(f"▶ {track.player_name}")
    footer.append("Navidrome")
    embed.set_footer(text="  •  ".join(footer))
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
        self.cover_url: str | None = None
        self.song_started_at: float | None = None
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
        if self.current_song_id == track.id and self.song_started_at is not None:
            elapsed = self._elapsed()
        else:
            elapsed = min(track.minutes_ago * 60, track.duration or 0)

        filename = None
        file = None
        if cover:
            filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover), filename=filename)

        image_url = f"attachment://{filename}" if filename else None
        embed = make_embed(self.config, track, image_url, elapsed, color)
        if file:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)

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

    def _elapsed(self) -> int:
        if self.song_started_at is None:
            return 0
        elapsed = time.monotonic() - self.song_started_at
        if self.current_track and self.current_track.duration:
            elapsed = min(elapsed, self.current_track.duration)
        return max(0, int(elapsed))

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
            await self._on_song_change(track, is_playing, song_id)
        elif is_playing and self.now_playing_message is not None:
            embed = make_embed(
                self.config, self.current_track, self.cover_url,
                self._elapsed(), self.current_color,
            )
            try:
                await self.now_playing_message.edit(embed=embed)
            except discord.NotFound:
                self.now_playing_message = None
            except discord.HTTPException as exc:
                log.warning("HTTP error editing entry: %s", exc)

    async def _on_song_change(self, track, is_playing, song_id) -> None:
        self.current_song_id = song_id

        if not (is_playing and track):
            self.current_track = None
            self.song_started_at = None
            self.cover_url = None
            self.now_playing_message = None
            await self._clear_presence()
            log.info("Idle (nothing playing).")
            return

        self.current_track = track
        self.song_started_at = time.monotonic() - track.minutes_ago * 60

        cover_bytes = None
        if track.cover_art:
            try:
                cover_bytes = await self.subsonic.cover_art(track.cover_art, self.config.cover_size)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("Couldn't download cover: %s", exc)
        self.current_color = dominant_color(cover_bytes, self.config.embed_color)
        await self._set_presence(track)

        resume = self._resume_message if (
                self._resume_message is not None and song_id == self._resume_song_id
        ) else None
        self._resume_message = None
        self._resume_song_id = None

        file = None
        image_url = None
        if cover_bytes:
            filename = f"cover_{_safe_id(track.id)}.jpg"
            file = discord.File(io.BytesIO(cover_bytes), filename=filename)
            image_url = f"attachment://{filename}"
        embed = make_embed(self.config, track, image_url, self._elapsed(), self.current_color)

        try:
            if resume is not None:
                self.now_playing_message = await resume.edit(
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
            if file is not None:
                file = discord.File(io.BytesIO(cover_bytes), filename=f"cover_{_safe_id(track.id)}.jpg")
                kwargs["file"] = file
            self.now_playing_message = await self.target_channel.send(**kwargs)
        except discord.HTTPException as exc:
            log.warning("HTTP error posting entry: %s", exc)
            self.now_playing_message = None
            return

        self.cover_url = (
            self.now_playing_message.attachments[0].url
            if self.now_playing_message.attachments else None
        )
        _save_state(self.config.state_file, {
            "channel_id": self.target_channel.id,
            "message_id": self.now_playing_message.id,
            "song_id": song_id,
        })
        log.info("Now listening: %s - %s", track.artist, track.title)

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
