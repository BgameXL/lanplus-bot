# Lanplus Music

Discord bot that posts what you are listening to on Navidrome.

## Behaviour

- One entry per song.
- While a song plays it stays as a single entry. When a song ends normally the
  entry updates in place and when you skip or go back it posts a new entry.
- Adds a "<name> likes this song" line when the track is a Navidrome favorite.
- The song title links to a public Navidrome share.
- Bot status shows "Listening to <song>".

## Commands

- `/nowplaying` - current song card
- `/metadata` - full metadata of the current song
- `/share` - public share link for the current song
- `/lyrics` - lyrics
- `/search <query>` - search the library
- `/list` - browse albums

## Requirements

- Python 3.14+.
- A Navidrome server.
- A client that reports to Navidrome.

## Setup

1. Create an application at the Discord Developer Portal and copy the bot token.
2. Invite the bot with scopes `bot applications.commands` and permissions
   View Channel, Send Messages, Embed Links, Attach Files.
3. Get the channel ID.
4. Create your `.env`:

   ```bash
   cp .env.example .env
   ```
    Fill `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `NAVIDROME_URL`, `NAVIDROME_USER`,`NAVIDROME_PASS`.

## Run

Local:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python check.py
.venv/bin/python bot.py
```

Docker:

```bash
docker compose up -d --build
docker compose logs -f
```

## Configuration (.env)

| Variable                            | Default          | Description                                 |
|-------------------------------------|------------------|---------------------------------------------|
| `DISCORD_TOKEN`                     | -                | Bot token                                   |
| `DISCORD_CHANNEL_ID`                | -                | Channel to post in                          |
| `NAVIDROME_URL`                     | -                | Navidrome base URL                          |
| `NAVIDROME_USER` / `NAVIDROME_PASS` | -                | Navidrome credentials                       |
| `NAVIDROME_USERNAME_FILTER`         | `NAVIDROME_USER` | Only show this user plays                   |
| `DISPLAY_NAME`                      | `NAVIDROME_USER` | Name in the "likes this song" line          |
| `POLL_INTERVAL`                     | `15`             | Seconds between checks                      |
| `IDLE_AFTER_MINUTES`                | `10`             | Minutes without a report before idle        |
| `EMBED_COLOR`                       | `5865F2`         | Fallback embed color                        |
| `COVER_SIZE`                        | `1000`           | Cover art size in px (0 = original)         |
| `SHARE_EXPIRES_DAYS`                | `0`              | Share link expiry in days (0 = never)       |
| `STATE_FILE`                        | `state.json`     | Remembers the current entry across restarts |

## Notes

- Slash commands sync to the channel's server on startup, restart the bot after changes.
- Cover quality is limited by the art embedded in your files
- Lyrics use LRCLIB as a fallback.
