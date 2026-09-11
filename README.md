# Navidrome → Discord "Now Playing"

Bot de Discord que publica en un canal **lo que estás escuchando en Navidrome**.
Mantiene un único mensaje (un embed) que se **actualiza solo**: barra de progreso
que avanza, color tomado de la carátula, artista/álbum/año. Además pone el estado
del bot ("Listening to …") y ofrece el comando `/nowplaying`. Cuando no suena
nada, muestra un estado "en reposo".

```
Navidrome (API Subsonic: getNowPlaying)  ──►  bot.py  ──►  embed en tu canal de Discord
```

## Requisitos

- Python 3.11+ (probado en 3.14).
- Un servidor Navidrome accesible desde donde corra el bot (puede ser `localhost`).
- Un reproductor que **reporte "now playing"** a Navidrome (la web de Navidrome y
  clientes como Feishin, Symfonium, play:Sub, DSub, etc. lo hacen).

## Instalación

Ya hay un entorno virtual en `.venv`. Instala las dependencias:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## Configuración

1. **Crea el bot en Discord**
   - Ve al [Developer Portal](https://discord.com/developers/applications) → *New Application*.
   - Pestaña **Bot** → *Reset Token* → copia el token.
   - No hace falta activar ningún "Privileged Gateway Intent" (el bot solo publica).

2. **Invita el bot a tu servidor** con permisos de *Ver canal, Enviar mensajes,
   Insertar enlaces (Embed Links) y Adjuntar archivos*, y el scope
   `applications.commands` (necesario para el comando `/nowplaying`). Puedes usar
   esta URL (reemplaza `TU_CLIENT_ID`, que está en la pestaña *OAuth2*):

   ```
   https://discord.com/api/oauth2/authorize?client_id=TU_CLIENT_ID&permissions=117760&scope=bot%20applications.commands
   ```

3. **Copia el ID del canal**: en Discord activa *Ajustes → Avanzado → Modo
   desarrollador*, clic derecho al canal → *Copiar ID del canal*.

4. **Crea tu `.env`** a partir de la plantilla y complétalo:

   ```bash
   cp .env.example .env
   ```

   Rellena `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `NAVIDROME_URL`,
   `NAVIDROME_USER` y `NAVIDROME_PASS`. El resto son opcionales.

## Probar la conexión con Navidrome

Antes de lanzar el bot, comprueba que las credenciales funcionan y que tu
reproductor reporta lo que suena:

```bash
.venv/bin/python check.py
```

- `✅ Conexión y credenciales OK` → Navidrome responde.
- Si pone música desde tu reproductor y sale `🎵 Artista — Título`, todo listo.
- Si sale `ℹ️ no hay nada sonando` mientras SÍ estás escuchando, tu cliente no
  está reportando "now playing"; prueba con la web de Navidrome u otro cliente.

## Ejecutar

```bash
.venv/bin/python bot.py
```

Verás en el canal el mensaje del bot, que se irá editando según lo que escuches.

## Ejecutar con Docker (recomendado para el servidor)

Para dejarlo corriendo en tu servidor (otra máquina), lo más cómodo es Docker.
El bot solo hace conexiones **salientes** (a Discord y a tu Navidrome), así que
no expone ningún puerto.

**Requisitos en el servidor:** Docker y el plugin Compose.

1. Lleva el proyecto al servidor (`git clone …` o copiándolo). Como `.env` está en
   `.gitignore` **no viaja con el repo**: créalo allí a partir de la plantilla y
   complétalo.

   ```bash
   cp .env.example .env
   ```

2. Construye y arranca en segundo plano:

   ```bash
   docker compose up -d --build
   ```

3. Mira los logs:

   ```bash
   docker compose logs -f
   ```

El estado (`state.json`, el ID del mensaje) se guarda en el volumen `botdata`, así
que al reiniciar reutiliza el mismo mensaje en vez de crear otro.

Comandos útiles:

```bash
docker compose restart        # reiniciar
docker compose down           # parar y quitar el contenedor
docker compose up -d --build  # aplicar cambios de código (reconstruir)
```

## Ejecutar como servicio (opcional, systemd)

Como ya tienes un servidor local, puedes dejarlo corriendo con un *user service*.
Crea `~/.config/systemd/user/navidrome-discord.service`:

```ini
[Unit]
Description=Navidrome -> Discord Now Playing
After=network-online.target

[Service]
WorkingDirectory=%h/IdeaProjects/bot hosting
ExecStart=%h/IdeaProjects/bot hosting/.venv/bin/python bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Y actívalo:

```bash
systemctl --user daemon-reload
systemctl --user enable --now navidrome-discord.service
journalctl --user -u navidrome-discord -f   # ver logs
```

## Ajustes

Todo se configura por `.env` (ver `.env.example`): intervalo de sondeo, umbral de
"en reposo", color del embed, tamaño de la carátula y filtro por usuario.

## Notas

- La carátula la **descarga el bot y la adjunta** al mensaje, así que funciona
  aunque tu Navidrome sea solo local y no expones ninguna credencial en Discord.
- El bot edita **un solo mensaje** en lugar de publicar uno nuevo por canción.
  El ID se guarda en `state.json` para reusarlo tras reiniciar.
- `.env` y `state.json` están en `.gitignore`: no se suben al repositorio.
