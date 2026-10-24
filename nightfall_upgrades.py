"""
Gargand modular upgrades.
Loaded automatically by sitecustomize without replacing main.py.
Adds Auto-Mod, XP/levels, analytics, AI channel memory, and extra admin tooling.
"""
import asyncio
import importlib
import ipaddress
import os
import re
import sqlite3
import sys
import time
import random
import unicodedata

import requests
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

INSTALLED = False
WINDOWS = defaultdict(deque)
XP_COOLDOWNS = {}
NSFW_TEXT_PATTERNS = (
    re.compile(r"\b(?:onlyfans|pornhub|xvideos|xnxx|xhamster|redtube|rule34|e621)\b", re.I),
    re.compile(r"\b(?:send|share|trade|drop|post|dm)\s+(?:(?:me|your|ur|their)\s+)?(?:nudes?|nude pics?|nude photos?|explicit pics?|explicit photos?|lewd pics?)\b", re.I),
    re.compile(r"\b(?:nudes?|nude pics?|nude photos?|explicit pics?|explicit photos?|lewd pics?)\b", re.I),
)
NSFW_MAX_IMAGES = 3
NSFW_MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")
URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.I)
SUSPICIOUS_HOSTS = frozenset({
    "grabify.link", "iplogger.org", "iplogger.com", "iplogger.ru",
    "2no.co", "yip.su", "discordnitro.com", "discord-nitro.com",
    "discordgift.site", "discord-gift.com", "discord-gifts.com",
    "discordapp-gift.com", "discordnitro.net", "steamcommunity.xyz",
})
OFFICIAL_DISCORD_HOSTS = (
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net",
    "discord.media", "discordstatus.com",
)
PHISHING_BRAND_WORDS = ("nitro", "gift", "verify", "login", "secure", "free", "support", "claim")


def _normalise_nsfw_text(text):
    """Normalise punctuation, Unicode compatibility forms and common number substitutions."""
    value = unicodedata.normalize("NFKC", str(text or "")[:6000]).casefold()
    leetspeak = value.translate(str.maketrans({
        "0": "o", "1": "i", "3": "e", "4": "a",
        "5": "s", "7": "t", "@": "a", "$": "s",
    }))
    compact = re.sub(r"[^a-z0-9]+", "", leetspeak)
    return value, compact


def _fallback_nsfw_text(text):
    """Local text fallback if the remote moderation API is not configured."""
    value, compact = _normalise_nsfw_text(text)
    if any(pattern.search(value) for pattern in NSFW_TEXT_PATTERNS):
        return True
    compact_terms = (
        "onlyfans", "pornhub", "xvideos", "xnxx", "xhamster", "redtube",
        "rule34", "e621", "sendnudes", "nudephoto", "nudepic",
        "explicitpic", "explicitphoto", "lewdpic",
    )
    return any(term in compact for term in compact_terms)


def _append_unique_image(urls, url, *, force=False):
    url = str(url or "").strip().strip("<>").rstrip(".,!?;:)]}")
    if not url.startswith("https://") or url in urls or len(url) > 2000:
        return
    path = urlparse(url).path.casefold()
    if force or path.endswith(IMAGE_EXTENSIONS):
        urls.append(url)


def _message_image_urls(message):
    """Collect image attachments and image embeds, capped to protect API and bandwidth."""
    urls = []
    for attachment in list(getattr(message, "attachments", []) or []):
        filename = str(getattr(attachment, "filename", "") or "").casefold()
        content_type = str(getattr(attachment, "content_type", "") or "").casefold()
        is_image = content_type.startswith("image/") or filename.endswith(IMAGE_EXTENSIONS)
        size = int(getattr(attachment, "size", 0) or 0)
        if is_image and (not size or size <= NSFW_MAX_IMAGE_BYTES):
            _append_unique_image(urls, getattr(attachment, "url", ""), force=True)
        if len(urls) >= NSFW_MAX_IMAGES:
            return urls

    # Direct image links in the message text are checked too.
    for match in URL_PATTERN.finditer(str(getattr(message, "content", "") or "")):
        _append_unique_image(urls, match.group(0))
        if len(urls) >= NSFW_MAX_IMAGES:
            return urls

    # Discord often exposes preview images as embeds instead of attachments.
    for item in list(getattr(message, "embeds", []) or []):
        for attribute in ("image", "thumbnail"):
            try:
                proxy = getattr(item, attribute, None)
                url = getattr(proxy, "url", None) if proxy else None
                _append_unique_image(urls, url, force=bool(url))
            except Exception:
                pass
        if len(urls) >= NSFW_MAX_IMAGES:
            break
    return urls[:NSFW_MAX_IMAGES]


def _classify_nsfw(api_key, text, image_urls):
    """Use the multimodal moderation API; only sexual-content categories trigger this filter."""
    inputs = []
    if str(text or "").strip():
        inputs.append({"type": "text", "text": str(text)[:4000]})
    for url in list(image_urls or [])[:NSFW_MAX_IMAGES]:
        inputs.append({"type": "image_url", "image_url": {"url": url}})
    if not inputs:
        return False
    response = requests.post(
        "https://api.openai.com/v1/moderations",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json={"model": "omni-moderation-latest", "input": inputs},
        timeout=(4, 12),
    )
    response.raise_for_status()
    payload = response.json()
    for result in payload.get("results", []) or []:
        categories = result.get("categories", {}) or {}
        if categories.get("sexual") is True or categories.get("sexual/minors") is True:
            return True
    return False


def _moderation_key():
    return (os.getenv("OPENAI_MODERATION_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _suspicious_url(url):
    """Return True for high-confidence phishing patterns without fetching the URL."""
    raw = str(url or "").strip().rstrip(".,!?;:)]}")
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if parsed.username or parsed.password:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host.startswith("xn--") or ".xn--" in host:
        return True

    if any(host == bad or host.endswith("." + bad) for bad in SUSPICIOUS_HOSTS):
        return True
    official_discord = any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DISCORD_HOSTS)
    if not official_discord and any(root in host for root in ("discord.com", "discord.gg", "discordapp.com")):
        return True
    if not official_discord and "discord" in host and any(word in host for word in PHISHING_BRAND_WORDS):
        return True
    official_steam = host == "steampowered.com" or host.endswith(".steampowered.com") or host == "steamcommunity.com" or host.endswith(".steamcommunity.com")
    if not official_steam and "steam" in host and any(word in host for word in ("gift", "free", "verify", "login", "trade")):
        return True
    return False


def _find_suspicious_url(text):
    for match in URL_PATTERN.finditer(str(text or "")[:6000]):
        url = match.group(0).rstrip(".,!?;:)]}")
        if _suspicious_url(url):
            return url[:300]
    return ""

def install(main):
    global INSTALLED
    if INSTALLED or not hasattr(main, "bot"):
        return
    INSTALLED = True
    bot = main.bot

    def db():
        return main.db_connect()

    def setting(guild, key, default=None):
        return main.setting(guild, key, default)

    def embed(*args, **kwargs):
        return main.embed(*args, **kwargs)

    conn = db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS nightfall_xp (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id,user_id)
      );
      CREATE TABLE IF NOT EXISTS nightfall_analytics (
        guild_id INTEGER NOT NULL, metric TEXT NOT NULL, day TEXT NOT NULL,
        value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id,metric,day)
      );
      CREATE TABLE IF NOT EXISTS nightfall_ai_memory (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS gargand_reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        due_at INTEGER NOT NULL,
        message TEXT NOT NULL,
        created_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_gargand_reminders_due ON gargand_reminders(due_at);
    """)
    conn.commit(); conn.close()

    def analytics(guild_id, metric):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = db()
        conn.execute(
          "INSERT INTO nightfall_analytics(guild_id,metric,day,value) VALUES(?,?,?,1) "
          "ON CONFLICT(guild_id,metric,day) DO UPDATE SET value=value+1",
          (guild_id, metric, day)
        )
        conn.commit(); conn.close()

    def add_xp(guild_id, user_id, amount):
        conn = db()
        row = conn.execute("SELECT xp,level FROM nightfall_xp WHERE guild_id=? AND user_id=?", (guild_id,user_id)).fetchone()
        old_xp = int(row["xp"]) if row else 0
        old_level = int(row["level"]) if row else 0
        xp = old_xp + amount
        level = int((xp / 100) ** 0.5)
        conn.execute(
          "INSERT INTO nightfall_xp(guild_id,user_id,xp,level) VALUES(?,?,?,?) "
          "ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp,level=excluded.level",
          (guild_id,user_id,xp,level)
        )
        conn.commit(); conn.close()
        return xp, level, level > old_level

    async def upgraded_message(message):
        if message.author.bot or not message.guild:
            return
        guild = message.guild
        member = message.author

        # Anti-NSFW: check message text and uploaded images before XP or other message features.
        if setting(guild, "antinsfw_enabled", False) and isinstance(member, main.discord.Member) and not main.is_staff(member):
            message_text = str(message.content or "")
            image_urls = _message_image_urls(message)
            api_key = _moderation_key()
            detected = _fallback_nsfw_text(message_text)
            detection_source = "local text rules" if detected else ""

            if api_key and (message_text.strip() or image_urls):
                try:
                    detected = await asyncio.wait_for(
                        asyncio.to_thread(_classify_nsfw, api_key, message_text, image_urls),
                        timeout=15,
                    )
                    detection_source = "multimodal moderation" if detected else ""
                except Exception as exc:
                    # A failed remote scan must not crash on_message. Keep the local text fallback.
                    print("Gargand anti-NSFW moderation check failed:", type(exc).__name__)
                    detected = _fallback_nsfw_text(message_text)
                    detection_source = "local text rules" if detected else ""

            if detected:
                try:
                    await message.delete()
                except (main.discord.Forbidden, main.discord.NotFound, main.discord.HTTPException):
                    pass

                try:
                    notice = embed(
                        "🛡️ Gargand · Content filter",
                        "Your message was removed because the server's anti-NSFW filter flagged it. "
                        "If you think this was a mistake, contact a server moderator.",
                        main.WARNING,
                    )
                    await member.send(embed=notice)
                except (main.discord.Forbidden, main.discord.HTTPException):
                    pass

                raw_log_id = setting(guild, "antinsfw_log_channel_id") or setting(guild, "automod_log_channel_id") or setting(guild, "logs_channel_id")
                try:
                    log_id = int(raw_log_id) if raw_log_id is not None else 0
                except (TypeError, ValueError):
                    log_id = 0
                log_channel = guild.get_channel(log_id) if log_id else None
                if isinstance(log_channel, main.discord.TextChannel):
                    detail = "Uploaded image(s)" if image_urls else "Message text"
                    try:
                        report = embed(
                            "🛡️ Anti-NSFW action",
                            f"**Member:** {member.mention} ({member.id})\n"
                            f"**Channel:** {message.channel.mention}\n"
                            f"**Content type:** {detail}\n"
                            f"**Detection:** {detection_source or 'moderation rules'}\n"
                            f"[Open message]({message.jump_url})",
                            main.WARNING,
                        )
                        await log_channel.send(
                            embed=report,
                            allowed_mentions=main.discord.AllowedMentions.none(),
                        )
                    except (main.discord.Forbidden, main.discord.HTTPException):
                        pass
                return

        # Anti-phishing: high-confidence suspicious URLs only; no remote URL is fetched.
        if setting(guild, "anti_phishing_enabled", False) and isinstance(member, main.discord.Member) and not main.is_staff(member):
            try:
                active_prefix = await main.command_prefix_for(bot, message)
                is_command_message = bool(active_prefix) and str(message.content or "").startswith(active_prefix)
            except Exception:
                is_command_message = str(message.content or "").lstrip().startswith("!")
            suspicious_url = "" if is_command_message else _find_suspicious_url(message.content)
            if suspicious_url:
                try:
                    await message.delete()
                except (main.discord.Forbidden, main.discord.NotFound, main.discord.HTTPException):
                    pass

                try:
                    await member.send(embed=embed(
                        "🛡️ Gargand · Anti-phishing",
                        f"A message you sent in **{guild.name}** contained a link that matched Gargand's high-risk link rules. "
                        "It was removed. If you believe this was a mistake, ask a server moderator to review it.",
                        main.WARNING,
                    ))
                except (main.discord.Forbidden, main.discord.HTTPException):
                    pass

                log_id = setting(guild, "automod_log_channel_id") or setting(guild, "logs_channel_id")
                try:
                    log_channel = guild.get_channel(int(log_id)) if log_id else None
                except (TypeError, ValueError):
                    log_channel = None
                if isinstance(log_channel, main.discord.TextChannel):
                    try:
                        report = embed(
                            "🛡️ Anti-phishing action",
                            f"**Member:** {member.mention} ({member.id})\n"
                            f"**Channel:** {message.channel.mention}\n"
                            f"**Matched URL:** {suspicious_url[:220]}\n"
                            f"[Open message]({message.jump_url})",
                            main.WARNING,
                        )
                        await log_channel.send(
                            embed=report,
                            allowed_mentions=main.discord.AllowedMentions.none(),
                        )
                    except (main.discord.Forbidden, main.discord.HTTPException):
                        pass
                return

        if setting(guild, "analytics_enabled", False):
            analytics(guild.id, "messages")

        # Advanced Auto-Mod.
        if isinstance(member, main.discord.Member) and not main.is_staff(member):
            now = time.monotonic()
            key = (guild.id, member.id)
            q = WINDOWS[key]
            q.append(now)
            window_seconds = max(3, int(setting(guild, "automod_window_seconds", 8) or 8))
            max_messages = max(3, int(setting(guild, "automod_max_messages", 6) or 6))
            while q and now-q[0] > window_seconds:
                q.popleft()
            reason = None
            if setting(guild, "automod_spam", False) and len(q) > max_messages:
                reason = "spam"
            if setting(guild, "automod_flood", False) and len(message.content) > 1800:
                reason = "flood"
            mention_limit = max(3, int(setting(guild, "automod_mention_limit", 6) or 6))
            mentions = len(message.mentions) + len(message.role_mentions) + (1 if message.mention_everyone else 0)
            if setting(guild, "automod_mentions", False) and mentions >= mention_limit:
                reason = "mention spam"
            if setting(guild, "automod_bad_words", False):
                for word in setting(guild, "swear_words", []):
                    if str(word).casefold() in message.content.casefold():
                        reason = "filtered word"; break
            if reason:
                try: await message.delete()
                except Exception: pass
                punishment = str(setting(guild, "automod_punishment", "timeout") or "timeout").lower()
                try:
                    if punishment == "kick":
                        await member.kick(reason="Gargand Auto-Mod: "+reason)
                    elif punishment == "ban":
                        await guild.ban(member, reason="Gargand Auto-Mod: "+reason, delete_message_seconds=0)
                    else:
                        await member.timeout(main.timedelta(minutes=5), reason="Gargand Auto-Mod: "+reason)
                except Exception: pass
                log_id = setting(guild, "automod_log_channel_id") or setting(guild, "logs_channel_id")
                log_ch = guild.get_channel(log_id) if log_id else None
                if isinstance(log_ch, main.discord.TextChannel):
                    await log_ch.send(embed=embed("🛡️ Auto-Mod", f"{member.mention} triggered **{reason}**. Punishment: **{punishment}**.", main.WARNING))
                return

        # XP and level-ups.
        if setting(guild, "xp_enabled", False) and isinstance(member, main.discord.Member):
            key = (guild.id, member.id)
            now = time.monotonic()
            cooldown = max(5, int(setting(guild, "xp_cooldown_seconds", 30) or 30))
            if now-XP_COOLDOWNS.get(key, 0) >= cooldown:
                XP_COOLDOWNS[key] = now
                try:
                    xp_amount = max(0, min(100, int(setting(guild, "xp_per_message", 10) or 0)))
                except (TypeError, ValueError):
                    xp_amount = 10
                if xp_amount <= 0:
                    return
                _, level, leveled = add_xp(guild.id, member.id, xp_amount)
                if leveled:
                    target = guild.get_channel(setting(guild, "levelup_channel_id") or message.channel.id)
                    if isinstance(target, main.discord.TextChannel):
                        await target.send(embed=embed("✨ Level up!", f"{member.mention} reached **level {level}**!", main.SUCCESS))

    bot.add_listener(upgraded_message, "on_message")

    @bot.hybrid_command(name="antinsfw", description="Enable or inspect the server's NSFW text and image filter.")
    @main.admin_only()
    @main.commands.guild_only()
    async def anti_nsfw_command(ctx, mode: str = "status"):
        mode = str(mode or "status").strip().casefold()
        api_key_ready = bool(_moderation_key())
        if mode in ("on", "enable", "enabled"):
            main.set_setting(ctx.guild, "antinsfw_enabled", True)
            if api_key_ready:
                message = "Anti-NSFW is enabled. Text and eligible image attachments will be checked by the moderation service."
            else:
                message = (
                    "Anti-NSFW is enabled, but only the limited local text-pattern fallback is active. "
                    "Image classification and broader text moderation need OPENAI_API_KEY (or OPENAI_MODERATION_API_KEY) "
                    "in the bot host's environment, followed by a bot restart."
                )
        elif mode in ("off", "disable", "disabled"):
            main.set_setting(ctx.guild, "antinsfw_enabled", False)
            message = "Anti-NSFW is disabled for this server."
        elif mode == "status":
            enabled = bool(setting(ctx.guild, "antinsfw_enabled", False))
            if api_key_ready:
                scanner = "Multimodal text and image moderation key detected."
            else:
                scanner = "No moderation API key detected; image contents cannot be classified yet."
            message = f"Anti-NSFW is {'enabled' if enabled else 'disabled'}.\n{scanner}\nUse !antinsfw on or !antinsfw off."
        else:
            message = "Usage: !antinsfw status, !antinsfw on, or !antinsfw off."
        await ctx.send(embed=embed("🛡️ Gargand · Anti-NSFW", message, main.INFO))

    @bot.hybrid_command(name="antiphishing", description="Enable or inspect suspicious-link protection.")
    @main.admin_only()
    @main.commands.guild_only()
    async def anti_phishing_command(ctx, mode: str = "status"):
        mode = str(mode or "status").strip().casefold()
        if mode in ("on", "enable", "enabled"):
            main.set_setting(ctx.guild, "anti_phishing_enabled", True)
            message = (
                "Anti-phishing is enabled. Gargand will remove high-confidence suspicious URLs, "
                "fake Discord/Nitro domains, URL credential tricks, punycode hosts and raw-IP links. "
                "Staff are exempt; review the Auto-Mod log channel for actions."
            )
        elif mode in ("off", "disable", "disabled"):
            main.set_setting(ctx.guild, "anti_phishing_enabled", False)
            message = "Anti-phishing is disabled for this server."
        elif mode == "status":
            enabled = bool(setting(ctx.guild, "anti_phishing_enabled", False))
            message = (
                f"Anti-phishing is {'enabled' if enabled else 'disabled'}.\n"
                "Protection uses a local high-confidence rule list and does not visit suspicious URLs.\n"
                "Use !antiphishing on or !antiphishing off."
            )
        else:
            message = "Usage: !antiphishing status, !antiphishing on, or !antiphishing off."
        await ctx.send(embed=embed("🛡️ Gargand · Anti-phishing", message, main.INFO))

    @bot.hybrid_command(name="reportchannel", description="Set the private channel used for member reports.")
    @main.admin_only()
    @main.commands.guild_only()
    async def report_channel_command(ctx, channel: main.discord.TextChannel):
        main.set_setting(ctx.guild, "reports_channel_id", channel.id)
        await ctx.send(embed=embed(
            "📝 Report channel saved",
            f"Member reports will be sent to {channel.mention}. Staff should restrict access to this channel.",
            main.SUCCESS,
        ))

    @bot.hybrid_command(name="level")
    @main.commands.guild_only()
    async def level(ctx, member=None):
        target = member or ctx.author
        conn = db()
        row = conn.execute("SELECT xp,level FROM nightfall_xp WHERE guild_id=? AND user_id=?", (ctx.guild.id,target.id)).fetchone()
        conn.close()
        xp = int(row["xp"]) if row else 0
        lvl = int(row["level"]) if row else 0
        await ctx.send(embed=embed("✨ Gargand level", f"**{target.display_name}** is level **{lvl}** with **{xp} XP**.", main.INFO))

    @bot.hybrid_command(name="analytics")
    @main.admin_only()
    @main.commands.guild_only()
    async def analytics_command(ctx):
        conn = db()
        rows = conn.execute("SELECT metric,SUM(value) total FROM nightfall_analytics WHERE guild_id=? GROUP BY metric", (ctx.guild.id,)).fetchall()
        conn.close()
        body = "\n".join(f"**{r['metric'].title()}:** {r['total']:,}" for r in rows) or "No analytics yet."
        await ctx.send(embed=embed("📊 Server analytics", body, main.INFO))


    @bot.hybrid_command(name="leaderboard", description="Show the top members by Gargand XP.")
    @main.commands.guild_only()
    async def leaderboard(ctx):
        conn = db()
        rows = conn.execute(
            "SELECT user_id,xp,level FROM nightfall_xp WHERE guild_id=? ORDER BY xp DESC,level DESC LIMIT 10",
            (ctx.guild.id,),
        ).fetchall()
        conn.close()
        if not rows:
            await ctx.send(embed=embed("🏆 Gargand leaderboard", "No XP has been earned yet. Enable XP in setup and chat to get started.", main.INFO))
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, row in enumerate(rows, start=1):
            member = ctx.guild.get_member(int(row["user_id"]))
            label = member.display_name if member else "Member " + str(row["user_id"])[-4:]
            marker = medals[index - 1] if index <= len(medals) else str(index) + "."
            lines.append(f"{marker} **{label}** — Level {int(row['level'])} · {int(row['xp']):,} XP")
        await ctx.send(embed=embed("🏆 Gargand leaderboard", "\n".join(lines), main.INFO))

    @bot.hybrid_command(name="suggest", description="Send a suggestion to the configured staff channel.")
    @main.commands.guild_only()
    async def suggest(ctx, *, idea: str):
        idea = str(idea or "").strip()
        if len(idea) < 8:
            await ctx.send("Please describe your suggestion in at least 8 characters.")
            return
        idea = idea[:1500]
        raw_id = setting(ctx.guild, "suggestions_channel_id") or setting(ctx.guild, "feedback_channel_id")
        try:
            channel = ctx.guild.get_channel(int(raw_id)) if raw_id else None
        except (TypeError, ValueError):
            channel = None
        if not isinstance(channel, main.discord.TextChannel):
            await ctx.send("Suggestions are not configured yet. Ask an administrator to choose a Suggestions or Feedback channel in Gargand setup.")
            return
        card = embed("💡 Community suggestion", idea, main.INFO)
        card.add_field(name="Submitted by", value=f"{ctx.author.mention} (ID {ctx.author.id})", inline=False)
        card.set_footer(text=f"GARGAND • Suggestions • {ctx.guild.name}")
        try:
            message = await channel.send(embed=card)
            for emoji in ("✅", "❌"):
                try:
                    await message.add_reaction(emoji)
                except main.discord.HTTPException:
                    pass
        except main.discord.Forbidden:
            await ctx.send("I can't post in the configured suggestions channel. Check my channel permissions.")
            return
        await ctx.send(f"Your suggestion was sent to {channel.mention}.")

    @bot.hybrid_command(name="serverstats", description="Show server activity and member statistics.")
    @main.commands.guild_only()
    async def serverstats(ctx):
        guild = ctx.guild
        humans = sum(1 for member in guild.members if not member.bot)
        bots = sum(1 for member in guild.members if member.bot)
        card = embed(
            "📈 Server overview",
            f"**Members:** {guild.member_count or len(guild.members):,}\n**Humans cached:** {humans:,}\n**Bots cached:** {bots:,}\n**Text channels:** {len(guild.text_channels)}\n**Voice channels:** {len(guild.voice_channels)}\n**Categories:** {len(guild.categories)}",
            main.INFO,
        )
        card.add_field(name="Created", value=main.discord.utils.format_dt(guild.created_at, style="D"), inline=True)
        card.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        await ctx.send(embed=card)

    @bot.hybrid_command(name="securitycheck", description="Check Gargand permissions and essential setup.")
    @main.commands.guild_only()
    @main.staff_only()
    async def securitycheck(ctx):
        me = ctx.guild.me
        if me is None:
            await ctx.send("I couldn't resolve Gargand's server member record. Try again shortly.")
            return
        checks = [
            ("View audit log", me.guild_permissions.view_audit_log),
            ("Manage channels", me.guild_permissions.manage_channels),
            ("Manage roles", me.guild_permissions.manage_roles),
            ("Manage messages", me.guild_permissions.manage_messages),
            ("Kick members", me.guild_permissions.kick_members),
            ("Ban members", me.guild_permissions.ban_members),
            ("Timeout members", me.guild_permissions.moderate_members),
            ("View channels", me.guild_permissions.view_channel),
            ("Send messages", me.guild_permissions.send_messages),
            ("Embed links", me.guild_permissions.embed_links),
        ]
        lines = [f"{'✅' if allowed else '⚠️'} **{label}:** {'Available' if allowed else 'Missing'}" for label, allowed in checks]
        raw_logs = setting(ctx.guild, "logs_channel_id")
        raw_suggest = setting(ctx.guild, "suggestions_channel_id") or setting(ctx.guild, "feedback_channel_id")
        logs_channel = ctx.guild.get_channel(int(raw_logs)) if str(raw_logs or "").isdigit() else None
        suggestions_channel = ctx.guild.get_channel(int(raw_suggest)) if str(raw_suggest or "").isdigit() else None
        lines.append(f"{'✅' if isinstance(logs_channel, main.discord.TextChannel) else '⚠️'} **Logging channel:** {'Configured' if isinstance(logs_channel, main.discord.TextChannel) else 'Not configured'}")
        lines.append(f"{'✅' if isinstance(suggestions_channel, main.discord.TextChannel) else '⚠️'} **Suggestions channel:** {'Configured' if isinstance(suggestions_channel, main.discord.TextChannel) else 'Not configured'}")
        if not me.guild_permissions.view_audit_log:
            lines.append("⚠️ Anti-nuke audit-log detection may be limited without View Audit Log.")
        await ctx.send(embed=embed("🛡️ Gargand security check", "\n".join(lines), main.INFO))

    @bot.hybrid_command(name="remind", description="Set a persistent reminder for 1 minute to 7 days from now.")
    @main.commands.guild_only()
    async def remind(ctx, duration: str, *, message: str):
        try:
            delay = int(main.parse_duration(duration))
        except (ValueError, TypeError):
            await ctx.send("Use a duration like 10m, 2h, or 1d.")
            return
        if delay < 60 or delay > 604800:
            await ctx.send("Reminders must be between 1 minute and 7 days from now.")
            return
        message = str(message or "").strip()[:800]
        if not message:
            await ctx.send("Add the reminder message after the duration.")
            return
        due_at = int(time.time()) + delay
        conn = db()
        conn.execute(
            "INSERT INTO gargand_reminders(guild_id,user_id,channel_id,due_at,message,created_at) VALUES(?,?,?,?,?,?)",
            (ctx.guild.id, ctx.author.id, ctx.channel.id, due_at, message, int(time.time())),
        )
        conn.commit()
        conn.close()
        await ctx.send(
            embed=embed(
                "⏰ Reminder saved",
                f"I'll remind {ctx.author.mention} in **{main.human_duration(delay)}**.\n**Reminder:** {message}",
                main.SUCCESS,
            )
        )

    original_ai = getattr(main, "generate_ai_text", None)
    if original_ai:
        async def memory_ai(ctx, task, prompt, *, max_prompt=700):
            if ctx.guild and setting(ctx.guild, "ai_memory_enabled", False):
                conn = db()
                rows = conn.execute(
                  "SELECT role,content FROM nightfall_ai_memory WHERE guild_id=? AND channel_id=? ORDER BY created_at DESC LIMIT 8",
                  (ctx.guild.id,ctx.channel.id)
                ).fetchall()
                conn.close()
                context = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(rows))
                if context:
                    prompt = "Recent channel context:\n"+context+"\n\nCurrent request:\n"+prompt
                conn = db()
                conn.execute(
                  "INSERT INTO nightfall_ai_memory(guild_id,channel_id,role,content,created_at) VALUES(?,?,?,?,?)",
                  (ctx.guild.id,ctx.channel.id,"user",str(prompt)[:1800],int(time.time()))
                )
                conn.commit(); conn.close()
            await original_ai(ctx, task, prompt, max_prompt=max_prompt)
        main.generate_ai_text = memory_ai

    @main.tasks.loop(seconds=15)
    async def reminder_worker():
        conn = db()
        due = conn.execute(
            "SELECT id,guild_id,user_id,channel_id,message FROM gargand_reminders WHERE due_at<=? ORDER BY due_at LIMIT 20",
            (int(time.time()),),
        ).fetchall()
        conn.close()
        for row in due:
            user_id = int(row["user_id"])
            reminder_message = str(row["message"] or "")[:800]
            sent = False
            channel = bot.get_channel(int(row["channel_id"]))
            allowed_mentions = main.discord.AllowedMentions(users=[main.discord.Object(id=user_id)], roles=False, everyone=False)
            if channel is not None and hasattr(channel, "send"):
                try:
                    await channel.send(f"<@{user_id}> ⏰ Reminder: {reminder_message}", allowed_mentions=allowed_mentions)
                    sent = True
                except (main.discord.Forbidden, main.discord.HTTPException):
                    sent = False
            if not sent:
                try:
                    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                    await user.send(embed=embed("⏰ Gargand reminder", reminder_message, main.INFO))
                except (main.discord.Forbidden, main.discord.HTTPException, AttributeError):
                    pass
            conn = db()
            conn.execute("DELETE FROM gargand_reminders WHERE id=?", (int(row["id"]),))
            conn.commit()
            conn.close()

    async def startup():
        await asyncio.sleep(3)
        try:
            await bot.tree.sync()
        except Exception as exc:
            print("Gargand upgrades slash sync:", repr(exc))
        try:
            if not reminder_worker.is_running():
                reminder_worker.start()
        except Exception as exc:
            print("Gargand reminder worker startup:", type(exc).__name__)
    asyncio.get_running_loop().create_task(startup())
    print("Gargand modular upgrades loaded: Auto-Mod, XP, analytics, AI memory.")

def wait_for_main():
    for _ in range(240):
        main = sys.modules.get("__main__")
        if main and hasattr(main, "bot") and hasattr(main, "db_connect"):
            try:
                install(main)
            except Exception as exc:
                print("Gargand upgrades failed:", repr(exc))
            return
        time.sleep(0.25)

