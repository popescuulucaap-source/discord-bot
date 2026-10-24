"""Gargand music player for Discord voice channels.

Uses yt-dlp for metadata/stream URL extraction and FFmpeg for audio playback.
All queue state is in memory; music queues intentionally reset when the bot restarts.
"""
import asyncio
import shutil
from collections import deque
from urllib.parse import urlparse

import discord
import imageio_ffmpeg
import yt_dlp

INSTALLED = False
PLAYERS = {}
MAX_QUEUE = 100

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
    return str(info["url"])


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
        return PLAYERS.setdefault(
            guild_id,
            {"queue": deque(), "now": None, "volume": 0.5, "task": None},
        )

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
                    stream_url = await asyncio.to_thread(_extract_stream, track["webpage_url"])
                    audio = discord.FFmpegPCMAudio(
                        stream_url,
                        executable=ffmpeg,
                        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
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
                    await finished.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
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

    @bot.hybrid_command(name="play", description="Play a track or search for music in your voice channel.")
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

    @bot.hybrid_command(name="skip", description="Skip the currently playing track.")
    @main.commands.guild_only()
    async def skip(ctx):
        voice = voice_for(ctx)
        state = player(ctx.guild.id)
        if not voice or not voice.is_connected() or not state["now"]:
            await ctx.send("Nothing is playing right now.")
            return
        voice.stop()
        await ctx.send("⏭️ Skipped the current track.")

    @bot.hybrid_command(name="pause", description="Pause the current music track.")
    @main.commands.guild_only()
    async def pause(ctx):
        voice = voice_for(ctx)
        if not voice or not voice.is_playing():
            await ctx.send("There isn't a playing track to pause.")
            return
        voice.pause()
        await ctx.send("⏸️ Music paused.")

    @bot.hybrid_command(name="resume", description="Resume paused music.")
    @main.commands.guild_only()
    async def resume(ctx):
        voice = voice_for(ctx)
        if not voice or not voice.is_paused():
            await ctx.send("Music isn't paused.")
            return
        voice.resume()
        await ctx.send("▶️ Music resumed.")

    @bot.hybrid_command(name="stop", description="Stop playback and clear the music queue.")
    @main.commands.guild_only()
    async def stop(ctx):
        state = player(ctx.guild.id)
        state["queue"].clear()
        voice = voice_for(ctx)
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        state["now"] = None
        await ctx.send("⏹️ Playback stopped and the queue was cleared.")

    @bot.hybrid_command(name="queue", description="Show the current music track and upcoming queue.")
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

    @bot.hybrid_command(name="nowplaying", description="Show the track currently playing.")
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

    @bot.hybrid_command(name="volume", description="Set the music volume from 5 to 150 percent.")
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

    @bot.hybrid_command(name="leave", description="Disconnect Gargand from your voice channel and clear the queue.")
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

    print("Gargand music commands loaded: play, skip, pause, resume, stop, queue, nowplaying, volume, leave.")
