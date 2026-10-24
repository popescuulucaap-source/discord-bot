"""Gargand music player for Discord voice channels.

Uses yt-dlp for metadata/stream URL extraction and FFmpeg for audio playback.
All queue state is in memory; music queues intentionally reset when the bot restarts.
"""
import asyncio
import os
import platform
import shutil
import shlex
import stat
import sys
import tempfile
import urllib.request
import zipfile
from collections import deque
from urllib.parse import urlparse

import discord
import imageio_ffmpeg
import yt_dlp

INSTALLED = False
PLAYERS = {}
MAX_QUEUE = 100
DASHBOARD_ACTION_HANDLER = None
DASHBOARD_SNAPSHOT_PROVIDER = None


def dashboard_snapshot(guild):
    if DASHBOARD_SNAPSHOT_PROVIDER:
        try:
            return DASHBOARD_SNAPSHOT_PROVIDER(guild)
        except Exception as exc:
            print("Gargand music snapshot warning:", type(exc).__name__)
    return {"connected": False, "channel_id": "", "channel_name": "", "playing": False, "paused": False, "volume": 50, "current": None, "queue": [], "queue_count": 0}


async def handle_dashboard_action(guild, payload):
    if not DASHBOARD_ACTION_HANDLER:
        raise RuntimeError("Music module is not initialized yet.")
    return await DASHBOARD_ACTION_HANDLER(guild, payload)

def prepare_youtube_runtime():
    """Find Deno or bootstrap the official Linux x86_64 runtime for YouTube EJS."""
    configured = os.getenv("GARGAND_DENO_PATH", "").strip()
    existing = configured if configured and os.path.isfile(configured) else shutil.which("deno")
    if existing:
        runtime_dir = os.path.dirname(os.path.abspath(existing))
        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
        print("Gargand music: YouTube JavaScript runtime is available.")
        return existing

    machine = platform.machine().lower()
    if not sys.platform.startswith("linux") or machine not in {"x86_64", "amd64"}:
        print("Gargand music warning: Deno was not found and automatic setup supports Linux x86_64 only. Set GARGAND_DENO_PATH on this host.")
        return None

    cache_root = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    runtime_dir = os.path.join(cache_root, "gargand", "deno")
    target = os.path.join(runtime_dir, "deno")
    if os.path.isfile(target) and os.access(target, os.X_OK):
        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
        return target

    os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
    archive_path = None
    try:
        fd, archive_path = tempfile.mkstemp(prefix="deno-", suffix=".zip", dir=runtime_dir)
        total = 0
        url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
        request = urllib.request.Request(url, headers={"User-Agent": "Gargand-Music/1.0"})
        with os.fdopen(fd, "wb") as destination, urllib.request.urlopen(request, timeout=25) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 120 * 1024 * 1024:
                    raise RuntimeError("Deno download was larger than expected.")
                destination.write(chunk)

        with zipfile.ZipFile(archive_path) as archive:
            member = next(
                (item for item in archive.infolist()
                 if not item.is_dir() and os.path.basename(item.filename) == "deno"),
                None,
            )
            if member is None or member.file_size < 1024 * 1024 or member.file_size > 150 * 1024 * 1024:
                raise RuntimeError("Official Deno archive did not contain a valid executable.")
            temporary_binary = target + ".tmp"
            with archive.open(member, "r") as source, open(temporary_binary, "wb") as destination:
                shutil.copyfileobj(source, destination)
            os.chmod(temporary_binary, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            os.replace(temporary_binary, target)

        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
        print("Gargand music: installed the official Deno runtime for YouTube extraction.")
        return target
    except Exception as exc:
        print("Gargand music warning: YouTube JavaScript runtime setup failed:", type(exc).__name__, str(exc)[:180])
        print("If YouTube searches fail, install Deno on the bot host or set GARGAND_DENO_PATH.")
        return None
    finally:
        if archive_path:
            try:
                os.remove(archive_path)
            except OSError:
                pass


YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": False,
    "socket_timeout": 12,
    "retries": 2,
    "extractor_retries": 2,
    "default_search": "ytsearch1",
}


def _ffmpeg_path():
    try:
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            return bundled
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _extract_track(query):
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)
    if not info:
        raise ValueError("No music result was found.")
    entries = info.get("entries")
    if entries is not None:
        info = next((item for item in entries if item), None)
    if not info:
        raise ValueError("No playable music result was found.")
    page_url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if not page_url:
        raise ValueError("The music provider did not return a playable page URL.")
    return {
        "title": str(info.get("title") or "Unknown track")[:180],
        "webpage_url": str(page_url),
        "duration": max(0, int(info.get("duration") or 0)),
        "source": str(info.get("extractor_key") or info.get("extractor") or "Music"),
    }


def _extract_stream(page_url):
    # Re-extract at playback time to avoid stale/expired audio URLs while queued.
    with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
        info = ydl.extract_info(page_url, download=False)
    if not info:
        raise ValueError("The music provider did not return a stream.")
    entries = info.get("entries")
    if entries is not None:
        info = next((item for item in entries if item), None)
    if not info or not info.get("url"):
        raise ValueError("Could not resolve an audio stream for this track.")
    # Some providers require the same referer/user-agent for FFmpeg that yt-dlp used.
    raw_headers = info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {}
    allowed_headers = {"user-agent", "referer", "origin", "accept", "accept-language"}
    headers = {
        str(key): str(value).replace("\\r", "").replace("\\n", "")
        for key, value in raw_headers.items()
        if str(key).casefold() in allowed_headers and str(value).strip()
    }
    return {"url": str(info["url"]), "headers": headers}


def _duration(seconds):
    seconds = max(0, int(seconds or 0))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def install(main):
    global INSTALLED
    if INSTALLED or not hasattr(main, "bot"):
        return
    INSTALLED = True
    bot = main.bot

    def player(guild_id):
        state = PLAYERS.setdefault(
            guild_id,
            {"queue": deque(), "now": None, "volume": 0.5, "task": None, "last_error": ""},
        )
        state.setdefault("last_error", "")
        return state

    def music_embed(title, description):
        return main.embed(title, description, main.INFO)

    def voice_for(ctx):
        return ctx.guild.voice_client if ctx.guild else None

    async def ensure_voice(ctx):
        member_voice = getattr(ctx.author, "voice", None)
        if not member_voice or not member_voice.channel:
            await ctx.send("Join a voice channel first, then use the music command.")
            return None
        channel = member_voice.channel
        voice = voice_for(ctx)
        try:
            if voice and voice.is_connected():
                if voice.channel != channel:
                    await ctx.send(f"I'm already playing in {voice.channel.name}. Use the leave command there first.")
                    return None
                return voice
            return await channel.connect()
        except discord.Forbidden:
            await ctx.send("I can't connect to or speak in that voice channel. Check my Connect and Speak permissions.")
        except discord.HTTPException:
            await ctx.send("Discord couldn't connect me to that voice channel. Please try again.")
        return None

    async def player_loop(guild_id):
        state = player(guild_id)
        current_task = asyncio.current_task()
        try:
            while state["queue"]:
                guild = bot.get_guild(guild_id)
                voice = guild.voice_client if guild else None
                if not voice or not voice.is_connected():
                    break

                track = state["queue"].popleft()
                state["now"] = track
                try:
                    ffmpeg = _ffmpeg_path()
                    if not ffmpeg:
                        raise RuntimeError("FFmpeg is not available. Install FFmpeg on the bot host and restart Gargand.")
                    stream_info = await asyncio.to_thread(_extract_stream, track["webpage_url"])
                    stream_url = stream_info["url"] if isinstance(stream_info, dict) else str(stream_info)
                    before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                    headers = stream_info.get("headers", {}) if isinstance(stream_info, dict) else {}
                    if headers:
                        header_blob = "".join(f"{key}: {value}\\r\\n" for key, value in headers.items()) + "\\r\\n"
                        before_options += " -headers " + shlex.quote(header_blob)
                    audio = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=ffmpeg,
                        before_options=before_options,
                        options="-vn",
                    )
                    source = discord.PCMVolumeTransformer(audio, volume=float(state["volume"]))
                    finished = asyncio.Event()
                    loop = asyncio.get_running_loop()

                    def after_playback(error):
                        if error:
                            print("Gargand music playback warning:", repr(error))
                        loop.call_soon_threadsafe(finished.set)

                    voice.play(source, after=after_playback)
                    state["last_error"] = ""
                    await finished.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state["last_error"] = str(exc)[:400] or type(exc).__name__
                    print("Gargand music player warning:", type(exc).__name__, str(exc)[:200])
                    text_channel = bot.get_channel(int(track.get("channel_id") or 0))
                    if text_channel:
                        message = str(exc)
                        try:
                            await text_channel.send(f"Couldn't play {discord.utils.escape_markdown(track['title'])}: {message[:250]}")
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                finally:
                    state["now"] = None
                if state["queue"]:
                    await asyncio.sleep(0.25)
        finally:
            if state.get("task") is current_task:
                state["task"] = None
            guild = bot.get_guild(guild_id)
            voice = guild.voice_client if guild else None
            if state["queue"] and voice and voice.is_connected():
                state["task"] = asyncio.create_task(player_loop(guild_id))

    def start_player(guild_id):
        state = player(guild_id)
        task = state.get("task")
        if task is None or task.done():
            state["task"] = asyncio.create_task(player_loop(guild_id))

    @bot.command(name="join", help="Join your current voice channel.")
    @main.commands.guild_only()
    async def join(ctx):
        member_voice = getattr(ctx.author, "voice", None)
        if not member_voice or not member_voice.channel:
            await ctx.send("Join a voice channel first, then use this command.")
            return

        current_voice = voice_for(ctx)
        if current_voice and current_voice.is_connected():
            if current_voice.channel == member_voice.channel:
                await ctx.send(f"✅ I'm already connected to **{current_voice.channel.name}**.")
            else:
                await ctx.send(f"I'm already connected to **{current_voice.channel.name}**. Use `!leave` first if you want me to switch.")
            return

        voice = await ensure_voice(ctx)
        if voice:
            await ctx.send(
                embed=music_embed(
                    "🔊 Joined voice channel",
                    f"I'm now connected to **{voice.channel.name}**. Use `!play <song or URL>` to start music."
                )
            )

    @bot.command(name="play", help="Play a track or search for music in your voice channel.")
    @main.commands.guild_only()
    async def play(ctx, *, query: str):
        query = str(query or "").strip()
        if not query:
            await ctx.send("Give me a music URL or search phrase.")
            return
        state = player(ctx.guild.id)
        if len(state["queue"]) >= MAX_QUEUE:
            await ctx.send(f"The music queue is full ({MAX_QUEUE} upcoming tracks).")
            return
        async with ctx.typing():
            try:
                target = query if urlparse(query).scheme in ("http", "https") else "ytsearch1:" + query
                track = await asyncio.to_thread(_extract_track, target)
            except Exception as exc:
                await ctx.send(f"Couldn't find that track: {str(exc)[:250]}")
                return
        voice = await ensure_voice(ctx)
        if not voice:
            return
        track["requested_by"] = ctx.author.id
        track["channel_id"] = ctx.channel.id
        state["queue"].append(track)
        queued_position = len(state["queue"]) + (1 if state["now"] else 0)
        await ctx.send(
            embed=music_embed(
                "🎵 Added to the music queue",
                f"**[{discord.utils.escape_markdown(track['title'])}]({track['webpage_url']})**\n"
                f"**Duration:** {_duration(track['duration'])}\n"
                f"**Requested by:** {ctx.author.mention}\n"
                f"**Queue position:** {queued_position}",
            )
        )
        start_player(ctx.guild.id)

    @bot.command(name="skip", help="Skip the currently playing track.")
    @main.commands.guild_only()
    async def skip(ctx):
        voice = voice_for(ctx)
        state = player(ctx.guild.id)
        if not voice or not voice.is_connected() or not state["now"]:
            await ctx.send("Nothing is playing right now.")
            return
        voice.stop()
        await ctx.send("⏭️ Skipped the current track.")

    @bot.command(name="pause", help="Pause the current music track.")
    @main.commands.guild_only()
    async def pause(ctx):
        voice = voice_for(ctx)
        if not voice or not voice.is_playing():
            await ctx.send("There isn't a playing track to pause.")
            return
        voice.pause()
        await ctx.send("⏸️ Music paused.")

    @bot.command(name="resume", help="Resume paused music.")
    @main.commands.guild_only()
    async def resume(ctx):
        voice = voice_for(ctx)
        if not voice or not voice.is_paused():
            await ctx.send("Music isn't paused.")
            return
        voice.resume()
        await ctx.send("▶️ Music resumed.")

    @bot.command(name="stop", help="Stop playback and clear the music queue.")
    @main.commands.guild_only()
    async def stop(ctx):
        state = player(ctx.guild.id)
        state["queue"].clear()
        voice = voice_for(ctx)
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        state["now"] = None
        await ctx.send("⏹️ Playback stopped and the queue was cleared.")

    @bot.command(name="queue", help="Show the current music track and upcoming queue.")
    @main.commands.guild_only()
    async def queue(ctx):
        state = player(ctx.guild.id)
        lines = []
        current = state["now"]
        if current:
            lines.append(f"**Now playing:** [{discord.utils.escape_markdown(current['title'])}]({current['webpage_url']}) · {_duration(current['duration'])}")
        tracks = list(state["queue"])[:10]
        for index, track in enumerate(tracks, start=1):
            lines.append(f"**{index}.** [{discord.utils.escape_markdown(track['title'])}]({track['webpage_url']}) · {_duration(track['duration'])}")
        if not lines:
            await ctx.send("The music queue is empty. Use play with a song or URL to add a track.")
            return
        extra = len(state["queue"]) - len(tracks)
        if extra > 0:
            lines.append(f"...and {extra} more track(s).")
        await ctx.send(embed=music_embed("🎶 Music queue", "\n".join(lines)[:4000]))

    @bot.command(name="nowplaying", help="Show the track currently playing.")
    @main.commands.guild_only()
    async def nowplaying(ctx):
        current = player(ctx.guild.id)["now"]
        if not current:
            await ctx.send("Nothing is playing right now.")
            return
        requested_by = f"<@{int(current['requested_by'])}>"
        await ctx.send(
            embed=music_embed(
                "🎧 Now playing",
                f"**[{discord.utils.escape_markdown(current['title'])}]({current['webpage_url']})**\n"
                f"**Duration:** {_duration(current['duration'])}\n"
                f"**Requested by:** {requested_by}",
            )
        )

    @bot.command(name="volume", help="Set the music volume from 5 to 150 percent.")
    @main.commands.guild_only()
    async def volume(ctx, percent: int):
        if percent < 5 or percent > 150:
            await ctx.send("Choose a volume between 5 and 150 percent.")
            return
        state = player(ctx.guild.id)
        state["volume"] = percent / 100
        voice = voice_for(ctx)
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = state["volume"]
        await ctx.send(f"🔊 Music volume set to {percent}%.")

    @bot.command(name="leave", help="Disconnect Gargand from your voice channel and clear the queue.")
    @main.commands.guild_only()
    async def leave(ctx):
        state = player(ctx.guild.id)
        state["queue"].clear()
        voice = voice_for(ctx)
        if voice and voice.is_connected():
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            try:
                await voice.disconnect(force=True)
            except discord.HTTPException:
                pass
        state["now"] = None
        await ctx.send("👋 Left the voice channel and cleared the music queue.")


    async def dashboard_action(guild, payload):
        action = str(payload.get("action") or "")
        state = player(guild.id)
        voice = guild.voice_client

        if action == "music_join":
            raw_channel_id = str(payload.get("channel_id") or "").strip()
            if not raw_channel_id.isdigit():
                raise ValueError("Choose a voice channel first.")
            channel = guild.get_channel(int(raw_channel_id))
            if not isinstance(channel, discord.VoiceChannel):
                raise ValueError("Choose a voice channel that belongs to this server.")
            if voice and voice.is_connected():
                if voice.channel == channel:
                    return {"message": f"Already connected to {channel.name}."}
                await voice.move_to(channel)
            else:
                await channel.connect()
            return {"message": f"Connected to {channel.name}."}

        if action == "music_play":
            query = str(payload.get("query") or "").strip()[:300]
            if not query:
                raise ValueError("Enter a song title or URL.")
            if len(state["queue"]) >= MAX_QUEUE:
                raise ValueError(f"The music queue is full ({MAX_QUEUE} upcoming tracks).")
            raw_channel_id = str(payload.get("channel_id") or "").strip()
            if raw_channel_id.isdigit():
                channel = guild.get_channel(int(raw_channel_id))
                if not isinstance(channel, discord.VoiceChannel):
                    raise ValueError("Choose a voice channel that belongs to this server.")
                if voice and voice.is_connected():
                    if voice.channel != channel:
                        await voice.move_to(channel)
                else:
                    voice = await channel.connect()
            elif voice is None or not voice.is_connected():
                raise ValueError("Choose a voice channel, or use !join in Discord first.")
            target = query if urlparse(query).scheme in ("http", "https") else "ytsearch1:" + query
            track = await asyncio.to_thread(_extract_track, target)
            track["requested_by"] = "website"
            track["channel_id"] = int(guild.system_channel.id) if guild.system_channel else 0
            state["last_error"] = ""
            state["queue"].append(track)
            start_player(guild.id)
            return {"message": f"Added {track['title']} to the queue."}

        if action == "music_volume":
            try:
                percent = int(payload.get("volume"))
            except (TypeError, ValueError):
                raise ValueError("Choose a volume between 5 and 150 percent.")
            if not 5 <= percent <= 150:
                raise ValueError("Choose a volume between 5 and 150 percent.")
            state["volume"] = percent / 100
            if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
                voice.source.volume = state["volume"]
            return {"message": f"Volume set to {percent} percent."}

        if action not in {"music_skip", "music_pause", "music_resume", "music_stop", "music_leave"}:
            raise ValueError("Unknown music action.")

        if action == "music_leave":
            state["queue"].clear()
            if voice and voice.is_connected():
                if voice.is_playing() or voice.is_paused():
                    voice.stop()
                await voice.disconnect(force=True)
            state["now"] = None
            return {"message": "Disconnected from voice and cleared the queue."}

        if action == "music_stop":
            state["queue"].clear()
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
            state["now"] = None
            return {"message": "Stopped playback and cleared the queue."}

        if not voice or not voice.is_connected():
            raise ValueError("Gargand is not connected to a voice channel.")
        if action == "music_skip":
            if not state["now"] or not (voice.is_playing() or voice.is_paused()):
                raise ValueError("Nothing is currently playing.")
            voice.stop()
            return {"message": "Skipped the current track."}
        if action == "music_pause":
            if not voice.is_playing():
                raise ValueError("There is no playing track to pause.")
            voice.pause()
            return {"message": "Music paused."}
        if action == "music_resume":
            if not voice.is_paused():
                raise ValueError("Music is not paused.")
            voice.resume()
            return {"message": "Music resumed."}
        raise ValueError("Unknown music action.")

    def dashboard_snapshot(guild):
        state = player(guild.id)
        voice = guild.voice_client
        current = state.get("now")
        def safe_track(track):
            if not track:
                return None
            return {
                "title": str(track.get("title") or "Unknown track")[:180],
                "url": str(track.get("webpage_url") or "")[:1000],
                "duration": int(track.get("duration") or 0),
                "requested_by": str(track.get("requested_by") or ""),
            }
        queued = [safe_track(track) for track in list(state.get("queue", []))[:10]]
        return {
            "connected": bool(voice and voice.is_connected()),
            "channel_id": str(voice.channel.id) if voice and voice.is_connected() else "",
            "channel_name": str(voice.channel.name) if voice and voice.is_connected() else "",
            "playing": bool(voice and voice.is_playing()),
            "paused": bool(voice and voice.is_paused()),
            "volume": int(round(float(state.get("volume", 0.5)) * 100)),
            "current": safe_track(current),
            "queue": queued,
            "queue_count": len(state.get("queue", [])),
            "last_error": str(state.get("last_error") or "")[:400],
        }

    global DASHBOARD_ACTION_HANDLER, DASHBOARD_SNAPSHOT_PROVIDER
    DASHBOARD_ACTION_HANDLER = dashboard_action
    DASHBOARD_SNAPSHOT_PROVIDER = dashboard_snapshot

    print("Gargand music commands loaded: join, play, skip, pause, resume, stop, queue, nowplaying, volume, leave.")
