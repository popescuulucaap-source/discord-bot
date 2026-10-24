"""Gargand community add-ons: achievements, daily quests, seasonal events,
co-op parties, multi-provider image search, RSS feeds, anime airing alerts and staff audit logs.
Loaded from start_nightfall.py; uses the existing SQLite database and discord.py.
"""
import asyncio
import html
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests

INSTALLED = False
MAIN_MODULE = None
DASHBOARD_ACTION_HANDLER = None
ACHIEVEMENTS = {
    "first_message": ("First Steps", "Send your first message."),
    "messages_50": ("Regular", "Send 50 messages."),
    "messages_500": ("Community Pillar", "Send 500 messages."),
}
ANILIST_URL = "https://graphql.anilist.co"


def _season_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _feed_url_is_safe(url):
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 512:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in ("localhost", "metadata.google.internal") or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            return False
    except ValueError:
        pass
    try:
        for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(item[4][0]).is_global:
                return False
    except (OSError, ValueError):
        return False
    return True


def _child_text(element, wanted):
    for child in list(element):
        if child.tag.rsplit("}", 1)[-1].lower() == wanted.lower():
            if wanted.lower() == "link" and child.attrib.get("href"):
                return child.attrib["href"].strip()
            return (child.text or "").strip()
    return ""


def _fetch_feed(url):
    if not _feed_url_is_safe(url):
        raise ValueError("Use a public HTTP or HTTPS RSS/Atom feed URL.")
    data = bytearray()
    with requests.get(url, timeout=(3, 7), stream=True, headers={"User-Agent": "GargandBot/1.0 RSS reader"}) as response:
        response.raise_for_status()
        for chunk in response.iter_content(16384):
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > 1_000_000:
                raise ValueError("This feed is larger than the 1 MB safety limit.")
    root = ET.fromstring(bytes(data))
    items = []
    for element in root.iter():
        kind = element.tag.rsplit("}", 1)[-1].lower()
        if kind not in ("item", "entry"):
            continue
        title = _child_text(element, "title") or "Untitled feed item"
        link = _child_text(element, "link")
        guid = _child_text(element, "guid") or _child_text(element, "id") or link or title
        summary = _child_text(element, "description") or _child_text(element, "summary") or _child_text(element, "content")
        summary = html.unescape(re.sub(r"<[^>]*>", " ", summary))
        summary = re.sub(r"\s+", " ", summary).strip()[:600]
        if link and urlparse(link).scheme not in ("http", "https"):
            link = ""
        items.append({"guid": guid[:500], "title": title[:250], "link": link[:1000], "summary": summary})
        if len(items) >= 30:
            break
    return items


def _fetch_anime(anilist_id):
    query = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        title { english romaji }
        siteUrl
        nextAiringEpisode { airingAt episode }
      }
    }
    """
    response = requests.post(
        ANILIST_URL,
        json={"query": query, "variables": {"id": int(anilist_id)}},
        timeout=(4, 10),
        headers={"Content-Type": "application/json", "User-Agent": "GargandBot/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    media = (payload.get("data") or {}).get("Media")
    if not media:
        raise ValueError("AniList could not find an anime with that ID.")
    title_data = media.get("title") or {}
    title = title_data.get("english") or title_data.get("romaji") or "Anime"
    airing = media.get("nextAiringEpisode") or {}
    return {
        "id": int(media["id"]),
        "title": title[:200],
        "url": media.get("siteUrl") or "https://anilist.co/",
        "episode": int(airing["episode"]) if airing.get("episode") else None,
        "airing_at": int(airing["airingAt"]) if airing.get("airingAt") else None,
    }


def dashboard_snapshot(guild_id):
    """Return safe per-server community and notification state for the website bridge."""
    if MAIN_MODULE is None:
        return {}
    guild_id = int(guild_id)
    conn = MAIN_MODULE.db_connect()
    settings = MAIN_MODULE.get_settings(guild_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    season = _season_key()
    community = {
        "xp_enabled": bool(settings.get("xp_enabled", False)),
        "analytics_enabled": bool(settings.get("analytics_enabled", False)),
        "achievements_enabled": bool(settings.get("achievements_enabled", True)),
        "daily_quests_enabled": bool(settings.get("daily_quests_enabled", True)),
        "seasonal_events_enabled": bool(settings.get("seasonal_events_enabled", True)),
        "party_system_enabled": bool(settings.get("party_system_enabled", True)),
        "xp_cooldown_seconds": int(settings.get("xp_cooldown_seconds", 30) or 30),
        "xp_per_message": int(settings.get("xp_per_message", 10) or 10),
        "members_with_xp": 0,
        "total_xp": 0,
        "total_messages": 0,
        "achievements_earned": 0,
        "quests_completed_today": 0,
        "season_points": 0,
        "party_count": 0,
        "analytics": [],
    }
    notifications = {"rss": [], "anime": []}
    audit = []
    try:
        row = conn.execute("SELECT COUNT(*),COALESCE(SUM(xp),0) FROM nightfall_xp WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            community["members_with_xp"] = int(row[0] or 0)
            community["total_xp"] = int(row[1] or 0)
        row = conn.execute("SELECT COALESCE(SUM(total_messages),0) FROM gargand_user_activity WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            community["total_messages"] = int(row[0] or 0)
        row = conn.execute("SELECT COUNT(*) FROM gargand_achievements WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            community["achievements_earned"] = int(row[0] or 0)
        row = conn.execute("SELECT COUNT(*) FROM gargand_daily_quests WHERE guild_id=? AND quest_date=? AND completed=1", (guild_id, today)).fetchone()
        if row:
            community["quests_completed_today"] = int(row[0] or 0)
        row = conn.execute("SELECT COALESCE(SUM(points),0) FROM gargand_season_points WHERE guild_id=? AND season_key=?", (guild_id, season)).fetchone()
        if row:
            community["season_points"] = int(row[0] or 0)
        row = conn.execute("SELECT COUNT(*) FROM gargand_parties WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            community["party_count"] = int(row[0] or 0)
        try:
            rows = conn.execute("SELECT metric,COALESCE(SUM(value),0) AS total FROM nightfall_analytics WHERE guild_id=? GROUP BY metric ORDER BY metric", (guild_id,)).fetchall()
            community["analytics"] = [{"metric": str(r[0])[:50], "total": int(r[1] or 0)} for r in rows[:20]]
        except Exception:
            pass
        rows = conn.execute("SELECT id,channel_id,url FROM gargand_rss_subscriptions WHERE guild_id=? ORDER BY id DESC LIMIT 50", (guild_id,)).fetchall()
        notifications["rss"] = [{"id": int(r[0]), "channel_id": str(r[1]), "url": str(r[2])[:512]} for r in rows]
        rows = conn.execute("SELECT id,channel_id,anilist_id,title,anime_url,next_episode,next_airing_at FROM gargand_anime_watches WHERE guild_id=? ORDER BY title LIMIT 50", (guild_id,)).fetchall()
        notifications["anime"] = [{
            "id": int(r[0]), "channel_id": str(r[1]), "anilist_id": int(r[2]),
            "title": str(r[3])[:180], "url": str(r[4])[:512],
            "next_episode": int(r[5]) if r[5] is not None else None,
            "next_airing_at": int(r[6]) if r[6] is not None else None,
        } for r in rows]
        rows = conn.execute("SELECT actor_id,command_name,channel_id,created_at FROM gargand_action_audit WHERE guild_id=? ORDER BY id DESC LIMIT 12", (guild_id,)).fetchall()
        audit = [{
            "actor_id": str(r[0]), "command": str(r[1])[:100],
            "channel_id": str(r[2]) if r[2] else "",
            "created_at": int(r[3]),
        } for r in rows]
    except Exception as exc:
        print("Gargand dashboard snapshot warning:", type(exc).__name__, str(exc)[:160])
    finally:
        conn.close()
    return {"community": community, "notifications": notifications, "audit": audit}


async def handle_dashboard_action(guild, payload):
    if not DASHBOARD_ACTION_HANDLER:
        raise RuntimeError("Community/notification features are not initialized yet.")
    return await DASHBOARD_ACTION_HANDLER(guild, payload)


def install(main):
    global INSTALLED, MAIN_MODULE, DASHBOARD_ACTION_HANDLER
    if INSTALLED or not hasattr(main, "bot"):
        return
    INSTALLED = True
    bot = main.bot
    discord = main.discord
    MAIN_MODULE = main

    def db():
        return main.db_connect()

    def setting(guild, key, default=None):
        return main.setting(guild, key, default)

    def card(title, body, color=None):
        return main.embed(title, body, color if color is not None else main.INFO)

    conn = db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS nightfall_xp (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id,user_id)
      );
      CREATE TABLE IF NOT EXISTS gargand_user_activity (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        total_messages INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id)
      );
      CREATE TABLE IF NOT EXISTS gargand_achievements (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        achievement_key TEXT NOT NULL, earned_at INTEGER NOT NULL,
        PRIMARY KEY(guild_id,user_id,achievement_key)
      );
      CREATE TABLE IF NOT EXISTS gargand_daily_quests (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, quest_date TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id,user_id,quest_date)
      );
      CREATE TABLE IF NOT EXISTS gargand_season_points (
        guild_id INTEGER NOT NULL, season_key TEXT NOT NULL, user_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(guild_id,season_key,user_id)
      );
      CREATE TABLE IF NOT EXISTS gargand_parties (
        guild_id INTEGER NOT NULL, name TEXT NOT NULL, created_by INTEGER NOT NULL,
        created_at INTEGER NOT NULL, PRIMARY KEY(guild_id,name)
      );
      CREATE TABLE IF NOT EXISTS gargand_party_members (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, party_name TEXT NOT NULL,
        joined_at INTEGER NOT NULL, PRIMARY KEY(guild_id,user_id)
      );
      CREATE TABLE IF NOT EXISTS gargand_action_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        actor_id INTEGER NOT NULL, command_name TEXT NOT NULL,
        channel_id INTEGER, created_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_gargand_audit_guild ON gargand_action_audit(guild_id,id DESC);
      CREATE TABLE IF NOT EXISTS gargand_rss_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, url TEXT NOT NULL, last_guid TEXT,
        created_at INTEGER NOT NULL, UNIQUE(guild_id,url)
      );
      CREATE TABLE IF NOT EXISTS gargand_anime_watches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, anilist_id INTEGER NOT NULL, title TEXT NOT NULL,
        anime_url TEXT NOT NULL, next_episode INTEGER, next_airing_at INTEGER,
        last_notified_episode INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
        UNIQUE(guild_id,channel_id,anilist_id)
      );
    """)
    conn.commit()
    conn.close()

    def award_bonus_xp(guild_id, user_id, amount):
        conn = db()
        row = conn.execute(
            "SELECT xp FROM nightfall_xp WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        xp = (int(row["xp"]) if row else 0) + int(amount)
        level = int((xp / 100) ** 0.5)
        conn.execute(
            "INSERT INTO nightfall_xp(guild_id,user_id,xp,level) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp,level=excluded.level",
            (guild_id, user_id, xp, level),
        )
        conn.commit()
        conn.close()

    def grant_achievement(guild_id, user_id, key):
        conn = db()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO gargand_achievements(guild_id,user_id,achievement_key,earned_at) VALUES(?,?,?,?)",
            (guild_id, user_id, key, int(time.time())),
        )
        conn.commit()
        conn.close()
        return cursor.rowcount == 1

    async def community_message(message):
        if not message.guild or message.author.bot:
            return
        guild_id, user_id = message.guild.id, message.author.id
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        season = _season_key()
        achievements_enabled = bool(setting(message.guild, "achievements_enabled", True))
        quests_enabled = bool(setting(message.guild, "daily_quests_enabled", True))
        season_enabled = bool(setting(message.guild, "seasonal_events_enabled", True))
        xp_enabled = bool(setting(message.guild, "xp_enabled", False))
        conn = db()
        conn.execute(
            "INSERT INTO gargand_user_activity(guild_id,user_id,total_messages) VALUES(?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET total_messages=total_messages+1",
            (guild_id, user_id),
        )
        row = conn.execute(
            "SELECT total_messages FROM gargand_user_activity WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        total = int(row["total_messages"]) if row else 0

        if season_enabled:
            conn.execute(
                "INSERT INTO gargand_season_points(guild_id,season_key,user_id,points) VALUES(?,?,?,1) "
                "ON CONFLICT(guild_id,season_key,user_id) DO UPDATE SET points=points+1",
                (guild_id, season, user_id),
            )

        quest_completed_now = False
        if quests_enabled:
            conn.execute(
                "INSERT INTO gargand_daily_quests(guild_id,user_id,quest_date,progress,completed) VALUES(?,?,?,1,0) "
                "ON CONFLICT(guild_id,user_id,quest_date) DO UPDATE SET progress=CASE WHEN completed=0 THEN progress+1 ELSE progress END",
                (guild_id, user_id, today),
            )
            quest_row = conn.execute(
                "SELECT progress,completed FROM gargand_daily_quests WHERE guild_id=? AND user_id=? AND quest_date=?",
                (guild_id, user_id, today),
            ).fetchone()
            if quest_row and int(quest_row["progress"]) >= 10 and not int(quest_row["completed"]):
                cursor = conn.execute(
                    "UPDATE gargand_daily_quests SET completed=1 WHERE guild_id=? AND user_id=? AND quest_date=? AND completed=0",
                    (guild_id, user_id, today),
                )
                quest_completed_now = cursor.rowcount == 1
        conn.commit()
        conn.close()

        milestone_keys = []
        if achievements_enabled:
            if total >= 1:
                milestone_keys.append("first_message")
            if total >= 50:
                milestone_keys.append("messages_50")
            if total >= 500:
                milestone_keys.append("messages_500")
        for key in milestone_keys:
            if grant_achievement(guild_id, user_id, key):
                label = ACHIEVEMENTS[key][0]
                try:
                    await message.channel.send(
                        embed=card("🏅 Achievement unlocked", f"{message.author.mention} earned **{label}**!"),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        if quest_completed_now:
            if xp_enabled:
                award_bonus_xp(guild_id, user_id, 30)
                message_text = f"{message.author.mention} completed **Send 10 messages** and earned **30 bonus XP**!"
            else:
                message_text = f"{message.author.mention} completed **Send 10 messages**! XP rewards are disabled in this server."
            try:
                await message.channel.send(
                    embed=card("✅ Daily quest complete", message_text),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    bot.add_listener(community_message, "on_message")

    @bot.hybrid_command(name="achievements", description="View your Gargand achievements.")
    @main.commands.guild_only()
    async def achievements(ctx, member: discord.Member = None):
        if not setting(ctx.guild, "achievements_enabled", True):
            await ctx.send("Achievements are disabled by this server's administrator.")
            return
        target = member or ctx.author
        conn = db()
        rows = conn.execute(
            "SELECT achievement_key,earned_at FROM gargand_achievements WHERE guild_id=? AND user_id=? ORDER BY earned_at",
            (ctx.guild.id, target.id),
        ).fetchall()
        conn.close()
        labels = [f"🏅 **{ACHIEVEMENTS[r['achievement_key']][0]}** — {ACHIEVEMENTS[r['achievement_key']][1]}" for r in rows if r["achievement_key"] in ACHIEVEMENTS]
        description = "\n".join(labels) if labels else "No achievements unlocked yet. Chat to unlock your first badge."
        await ctx.send(embed=card(f"🏅 {target.display_name}'s achievements", description))

    @bot.hybrid_command(name="quest", description="View today's daily community quest.")
    @main.commands.guild_only()
    async def quest(ctx):
        if not setting(ctx.guild, "daily_quests_enabled", True):
            await ctx.send("Daily quests are disabled by this server's administrator.")
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = db()
        row = conn.execute(
            "SELECT progress,completed FROM gargand_daily_quests WHERE guild_id=? AND user_id=? AND quest_date=?",
            (ctx.guild.id, ctx.author.id, today),
        ).fetchone()
        conn.close()
        progress = int(row["progress"]) if row else 0
        done = bool(row and int(row["completed"]))
        body = f"**Today's quest:** Send 10 messages in this server.\n**Progress:** {min(progress, 10)}/10\n**Reward:** 30 bonus XP\n\n" + ("✅ Quest completed today." if done else "Keep chatting to finish it.")
        await ctx.send(embed=card("📜 Daily quest", body))

    @bot.hybrid_command(name="season", description="See the current monthly Gargand community event.")
    @main.commands.guild_only()
    async def season(ctx):
        if not setting(ctx.guild, "seasonal_events_enabled", True):
            await ctx.send("Seasonal events are disabled by this server's administrator.")
            return
        key = _season_key()
        conn = db()
        row = conn.execute(
            "SELECT points FROM gargand_season_points WHERE guild_id=? AND season_key=? AND user_id=?",
            (ctx.guild.id, key, ctx.author.id),
        ).fetchone()
        conn.close()
        points = int(row["points"]) if row else 0
        label = datetime.now(timezone.utc).strftime("%B %Y")
        await ctx.send(embed=card("🌟 Monthly community event", f"**Season:** {label}\n**Your event points:** {points}\nEarn 1 point for each message. Use the seasonleaderboard command to see the leaders."))

    @bot.hybrid_command(name="seasonleaderboard", description="Show this month's community event leaderboard.")
    @main.commands.guild_only()
    async def seasonleaderboard(ctx):
        if not setting(ctx.guild, "seasonal_events_enabled", True):
            await ctx.send("Seasonal events are disabled by this server's administrator.")
            return
        conn = db()
        rows = conn.execute(
            "SELECT user_id,points FROM gargand_season_points WHERE guild_id=? AND season_key=? ORDER BY points DESC LIMIT 10",
            (ctx.guild.id, _season_key()),
        ).fetchall()
        conn.close()
        if not rows:
            await ctx.send(embed=card("🌟 Season leaderboard", "No event points yet. Start chatting to get on the board."))
            return
        lines = []
        for i, row in enumerate(rows, 1):
            member = ctx.guild.get_member(int(row["user_id"]))
            name = member.display_name if member else f"Member {str(row['user_id'])[-4:]}"
            lines.append(f"**{i}. {name}** — {int(row['points']):,} points")
        await ctx.send(embed=card("🌟 Season leaderboard", "\n".join(lines)))

    @bot.hybrid_command(name="globalleaderboard", description="Show the top members by XP across all servers.")
    async def globalleaderboard(ctx):
        conn = db()
        rows = conn.execute(
            "SELECT user_id,SUM(xp) AS total_xp FROM nightfall_xp GROUP BY user_id ORDER BY total_xp DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if not rows:
            await ctx.send(embed=card("🌍 Global XP leaderboard", "No XP has been earned yet."))
            return
        lines = [f"**{i}.** <@{int(row['user_id'])}> — {int(row['total_xp']):,} XP" for i, row in enumerate(rows, 1)]
        await ctx.send(embed=card("🌍 Global XP leaderboard", "\n".join(lines)))

    @bot.hybrid_group(name="party", invoke_without_command=True, description="Create or join a community co-op party.")
    @main.commands.guild_only()
    async def party(ctx):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        await ctx.send("Use party create, party join, party leave, party status, or party leaderboard.")

    @party.command(name="create", description="Create a co-op party.")
    async def party_create(ctx, *, name: str):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        name = re.sub(r"\s+", " ", name).strip()[:24]
        if not name or not re.fullmatch(r"[\w -]+", name, flags=re.UNICODE):
            await ctx.send("Party names can use letters, numbers, spaces, underscores, and hyphens.")
            return
        conn = db()
        try:
            conn.execute("INSERT INTO gargand_parties(guild_id,name,created_by,created_at) VALUES(?,?,?,?)", (ctx.guild.id, name, ctx.author.id, int(time.time())))
            conn.execute(
                "INSERT INTO gargand_party_members(guild_id,user_id,party_name,joined_at) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET party_name=excluded.party_name,joined_at=excluded.joined_at",
                (ctx.guild.id, ctx.author.id, name, int(time.time())),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            await ctx.send("A party with that name already exists. Choose another name.")
            return
        conn.close()
        await ctx.send(embed=card("🤝 Co-op party created", f"**{name}** is ready. Friends can join with the party join command."))

    @party.command(name="join", description="Join a co-op party.")
    async def party_join(ctx, *, name: str):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        conn = db()
        exists = conn.execute("SELECT name FROM gargand_parties WHERE guild_id=? AND name=? COLLATE NOCASE", (ctx.guild.id, name.strip())).fetchone()
        if not exists:
            conn.close()
            await ctx.send("I couldn't find that party. Ask its creator for the exact name.")
            return
        canonical = exists["name"]
        conn.execute(
            "INSERT INTO gargand_party_members(guild_id,user_id,party_name,joined_at) VALUES(?,?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET party_name=excluded.party_name,joined_at=excluded.joined_at",
            (ctx.guild.id, ctx.author.id, canonical, int(time.time())),
        )
        conn.commit()
        conn.close()
        await ctx.send(embed=card("🤝 Joined co-op party", f"You are now part of **{canonical}**. Party points come from members' activity during the current season."))

    @party.command(name="leave", description="Leave your current co-op party.")
    async def party_leave(ctx):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        conn = db()
        cursor = conn.execute("DELETE FROM gargand_party_members WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.author.id))
        conn.commit()
        conn.close()
        await ctx.send("You left your party." if cursor.rowcount else "You're not in a party.")

    @party.command(name="status", description="View your co-op party and its seasonal points.")
    async def party_status(ctx):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        conn = db()
        row = conn.execute("SELECT party_name FROM gargand_party_members WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.author.id)).fetchone()
        if not row:
            conn.close()
            await ctx.send("You're not in a party. Use party create or party join.")
            return
        name = row["party_name"]
        members = conn.execute("SELECT user_id FROM gargand_party_members WHERE guild_id=? AND party_name=? COLLATE NOCASE", (ctx.guild.id, name)).fetchall()
        points = 0
        for m in members:
            found = conn.execute("SELECT points FROM gargand_season_points WHERE guild_id=? AND season_key=? AND user_id=?", (ctx.guild.id, _season_key(), int(m["user_id"]))).fetchone()
            points += int(found["points"]) if found else 0
        conn.close()
        await ctx.send(embed=card("🤝 Co-op party status", f"**Party:** {name}\n**Members:** {len(members)}\n**Current-season team points:** {points:,}\nEvery message from a party member contributes one point."))

    @party.command(name="leaderboard", description="Show the co-op party leaderboard.")
    async def party_leaderboard(ctx):
        if not setting(ctx.guild, "party_system_enabled", True):
            await ctx.send("Co-op parties are disabled by this server's administrator.")
            return
        conn = db()
        rows = conn.execute(
            "SELECT pm.party_name, COUNT(*) AS members, SUM(COALESCE(sp.points,0)) AS points "
            "FROM gargand_party_members pm LEFT JOIN gargand_season_points sp "
            "ON sp.guild_id=pm.guild_id AND sp.user_id=pm.user_id AND sp.season_key=? "
            "WHERE pm.guild_id=? GROUP BY pm.party_name ORDER BY points DESC LIMIT 10",
            (_season_key(), ctx.guild.id),
        ).fetchall()
        conn.close()
        if not rows:
            await ctx.send(embed=card("🤝 Co-op leaderboard", "No parties yet. Create one with party create."))
            return
        lines = [f"**{i}. {row['party_name']}** — {int(row['points'] or 0):,} points · {int(row['members'])} members" for i, row in enumerate(rows, 1)]
        await ctx.send(embed=card("🤝 Co-op leaderboard", "\n".join(lines)))

    @bot.hybrid_command(name="image", description="Search for images across several providers.")
    async def image_search(ctx, *, query: str):
        query = str(query or "").strip()[:180]
        if not query:
            await ctx.send("Add a search term, for example: image space lion.")
            return
        q = quote_plus(query)
        view = discord.ui.View(timeout=None)
        for label, url in [
            ("Google Images", f"https://www.google.com/search?tbm=isch&q={q}"),
            ("Bing Images", f"https://www.bing.com/images/search?q={q}"),
            ("DuckDuckGo", f"https://duckduckgo.com/?q={q}&iax=images&ia=images"),
            ("Brave Search", f"https://search.brave.com/images?q={q}"),
            ("Wikimedia Commons", f"https://commons.wikimedia.org/w/index.php?search={q}&title=Special:MediaSearch&type=image"),
        ]:
            view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
        await ctx.send(embed=card("🖼️ Image search", f"Search results for **{discord.utils.escape_markdown(query)}** across five image providers. Open a provider below."), view=view)

    @bot.hybrid_group(name="rss", invoke_without_command=True, description="Subscribe to RSS or Atom feeds.")
    @main.commands.guild_only()
    async def rss(ctx):
        await ctx.send("Use rss add with a feed URL, rss list, or rss remove with a subscription ID. Feed updates are checked about every 5 minutes.")

    @rss.command(name="add", description="Subscribe this channel to a public RSS/Atom feed.")
    @main.admin_only()
    async def rss_add(ctx, url: str):
        url = str(url or "").strip()
        try:
            items = await asyncio.to_thread(_fetch_feed, url)
        except Exception as exc:
            await ctx.send(f"Couldn't read that feed: {str(exc)[:250]}")
            return
        latest = items[0]["guid"] if items else None
        conn = db()
        try:
            cursor = conn.execute(
                "INSERT INTO gargand_rss_subscriptions(guild_id,channel_id,url,last_guid,created_at) VALUES(?,?,?,?,?)",
                (ctx.guild.id, ctx.channel.id, url, latest, int(time.time())),
            )
            subscription_id = int(cursor.lastrowid)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            await ctx.send("That feed is already subscribed in this server.")
            return
        conn.close()
        await ctx.send(embed=card("📡 RSS subscription added", f"This channel will receive new feed entries. Initial posts are not replayed.\n**Feed:** {url}\n**Subscription ID:** {subscription_id}"))

    @rss.command(name="list", description="List this server's RSS feed subscriptions.")
    @main.admin_only()
    async def rss_list(ctx):
        conn = db()
        rows = conn.execute("SELECT id,channel_id,url FROM gargand_rss_subscriptions WHERE guild_id=? ORDER BY id", (ctx.guild.id,)).fetchall()
        conn.close()
        if not rows:
            await ctx.send("No RSS feeds are subscribed yet. Use rss add with a feed URL.")
            return
        lines = [f"**#{int(r['id'])}** — <#{int(r['channel_id'])}> — {r['url'][:180]}" for r in rows[:15]]
        await ctx.send(embed=card("📡 RSS subscriptions", "\n".join(lines)))

    @rss.command(name="remove", description="Remove an RSS feed subscription by ID.")
    @main.admin_only()
    async def rss_remove(ctx, subscription_id: int):
        conn = db()
        cursor = conn.execute("DELETE FROM gargand_rss_subscriptions WHERE guild_id=? AND id=?", (ctx.guild.id, subscription_id))
        conn.commit()
        conn.close()
        await ctx.send("RSS subscription removed." if cursor.rowcount else "I couldn't find that subscription ID in this server.")

    @bot.hybrid_command(name="animewatch", description="Notify this channel when an AniList-tracked anime episode is released.")
    @main.commands.guild_only()
    @main.admin_only()
    async def animewatch(ctx, anilist_id: int):
        if anilist_id <= 0:
            await ctx.send("Use a positive AniList anime ID.")
            return
        try:
            anime = await asyncio.to_thread(_fetch_anime, anilist_id)
        except Exception as exc:
            await ctx.send(f"Couldn't load that AniList anime: {str(exc)[:250]}")
            return
        conn = db()
        try:
            conn.execute(
                "INSERT INTO gargand_anime_watches(guild_id,channel_id,anilist_id,title,anime_url,next_episode,next_airing_at,last_notified_episode,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ctx.guild.id, ctx.channel.id, anime["id"], anime["title"], anime["url"], anime["episode"], anime["airing_at"], max(0, (anime["episode"] or 1) - 1), int(time.time())),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            await ctx.send("This channel is already watching that anime.")
            return
        conn.close()
        schedule = f"Next scheduled episode: **{anime['episode']}**." if anime["episode"] else "AniList has not announced the next episode time yet; I'll keep checking."
        await ctx.send(embed=card("📺 Anime notifications enabled", f"**{anime['title']}**\n{schedule}\nEpisodes are checked periodically using AniList.\n{anime['url']}"))

    @bot.hybrid_command(name="animeunwatch", description="Stop episode notifications for an AniList anime ID in this channel.")
    @main.commands.guild_only()
    @main.admin_only()
    async def animeunwatch(ctx, anilist_id: int):
        conn = db()
        cursor = conn.execute(
            "DELETE FROM gargand_anime_watches WHERE guild_id=? AND channel_id=? AND anilist_id=?",
            (ctx.guild.id, ctx.channel.id, anilist_id),
        )
        conn.commit()
        conn.close()
        await ctx.send("Anime notifications stopped in this channel." if cursor.rowcount else "This channel wasn't watching that AniList ID.")

    @bot.hybrid_command(name="animewatches", description="List anime episode notifications configured for this server.")
    @main.commands.guild_only()
    @main.admin_only()
    async def animewatches(ctx):
        conn = db()
        rows = conn.execute("SELECT channel_id,anilist_id,title,anime_url FROM gargand_anime_watches WHERE guild_id=? ORDER BY title LIMIT 20", (ctx.guild.id,)).fetchall()
        conn.close()
        if not rows:
            await ctx.send("No anime episode notifications are configured. Use animewatch with an AniList ID.")
            return
        lines = [f"**{r['title']}** (ID {int(r['anilist_id'])}) — <#{int(r['channel_id'])}> — {r['anime_url']}" for r in rows]
        await ctx.send(embed=card("📺 Anime notifications", "\n".join(lines)))

    async def write_audit(guild, actor, command_name, channel_id):
        if not guild or not actor or getattr(actor, "bot", False):
            return
        is_staff = bool(getattr(actor.guild_permissions, "manage_guild", False) or getattr(actor.guild_permissions, "administrator", False))
        try:
            is_staff = is_staff or bool(main.is_staff(actor))
        except Exception:
            pass
        if not is_staff:
            return
        conn = db()
        conn.execute(
            "INSERT INTO gargand_action_audit(guild_id,actor_id,command_name,channel_id,created_at) VALUES(?,?,?,?,?)",
            (guild.id, actor.id, str(command_name)[:100], channel_id, int(time.time())),
        )
        conn.commit()
        conn.close()
        try:
            asyncio.create_task(main.send_telemetry(
                "staff_action", guild=guild, user=actor, command_name=str(command_name)[:100],
                metadata={"channel_id": int(channel_id or 0)}
            ))
        except Exception:
            pass
        raw_channel = setting(guild, "logs_channel_id")
        channel = guild.get_channel(int(raw_channel)) if str(raw_channel or "").isdigit() else None
        if isinstance(channel, discord.TextChannel) and channel.id != channel_id:
            try:
                await channel.send(embed=card("🧾 Staff action audit", f"**Staff member:** {actor.mention}\n**Command:** {str(command_name)[:100]}\n**Channel:** <#{channel_id}>"))
            except (discord.Forbidden, discord.HTTPException):
                pass

    async def on_command_completion(ctx):
        await write_audit(ctx.guild, ctx.author, getattr(ctx.command, "qualified_name", "unknown"), getattr(ctx.channel, "id", None))

    async def on_app_command_completion(interaction, command):
        await write_audit(interaction.guild, interaction.user, getattr(command, "qualified_name", "unknown"), getattr(interaction.channel, "id", None))

    bot.add_listener(on_command_completion, "on_command_completion")
    bot.add_listener(on_app_command_completion, "on_app_command_completion")

    @bot.hybrid_command(name="auditlog", description="View recent staff command actions.")
    @main.commands.guild_only()
    @main.admin_only()
    async def auditlog(ctx, limit: int = 10):
        limit = max(1, min(int(limit), 20))
        conn = db()
        rows = conn.execute(
            "SELECT actor_id,command_name,channel_id,created_at FROM gargand_action_audit WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (ctx.guild.id, limit),
        ).fetchall()
        conn.close()
        if not rows:
            await ctx.send("No staff command actions have been recorded yet.")
            return
        lines = []
        for row in rows:
            timestamp = discord.utils.format_dt(datetime.fromtimestamp(int(row["created_at"]), tz=timezone.utc), style="R")
            channel = f"<#{int(row['channel_id'])}>" if row["channel_id"] else "unknown channel"
            lines.append(f"{timestamp} — <@{int(row['actor_id'])}> ran {row['command_name']} in {channel}")
        await ctx.send(embed=card("🧾 Recent staff audit log", "\n".join(lines)))

    @main.tasks.loop(minutes=5)
    async def rss_worker():
        conn = db()
        rows = conn.execute("SELECT id,guild_id,channel_id,url,last_guid FROM gargand_rss_subscriptions ORDER BY id LIMIT 50").fetchall()
        conn.close()
        for row in rows:
            try:
                items = await asyncio.to_thread(_fetch_feed, row["url"])
                if not items:
                    continue
                latest_guid = items[0]["guid"]
                previous_guid = row["last_guid"]
                if not previous_guid:
                    conn = db()
                    conn.execute("UPDATE gargand_rss_subscriptions SET last_guid=? WHERE id=?", (latest_guid, int(row["id"])))
                    conn.commit()
                    conn.close()
                    continue
                if latest_guid == previous_guid:
                    continue
                new_items = []
                found_previous = False
                for item in items:
                    if item["guid"] == previous_guid:
                        found_previous = True
                        break
                    new_items.append(item)
                if not found_previous:
                    new_items = items[:1]
                channel = bot.get_channel(int(row["channel_id"]))
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(int(row["channel_id"]))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                for item in reversed(new_items[:3]):
                    embed_obj = card(f"📡 {item['title'][:230]}", item["summary"] or "A new item was published.")
                    if item["link"]:
                        embed_obj.url = item["link"]
                    embed_obj.add_field(name="Feed", value=row["url"][:1000], inline=False)
                    await channel.send(embed=embed_obj, allowed_mentions=discord.AllowedMentions.none())
                conn = db()
                conn.execute("UPDATE gargand_rss_subscriptions SET last_guid=? WHERE id=?", (latest_guid, int(row["id"])))
                conn.commit()
                conn.close()
            except Exception as exc:
                print("Gargand RSS poll warning:", type(exc).__name__, str(exc)[:150])

    @main.tasks.loop(minutes=10)
    async def anime_worker():
        conn = db()
        rows = conn.execute(
            "SELECT id,guild_id,channel_id,anilist_id,title,anime_url,next_episode,next_airing_at,last_notified_episode "
            "FROM gargand_anime_watches ORDER BY id LIMIT 50"
        ).fetchall()
        conn.close()
        now = int(time.time())
        for row in rows:
            try:
                anime = await asyncio.to_thread(_fetch_anime, int(row["anilist_id"]))
                old_episode = int(row["next_episode"]) if row["next_episode"] is not None else None
                old_airing = int(row["next_airing_at"]) if row["next_airing_at"] is not None else None
                last_notified = int(row["last_notified_episode"] or 0)
                current_episode = anime["episode"]
                current_airing = anime["airing_at"]
                should_notify = old_episode is not None and old_episode > last_notified and (
                    (old_airing is not None and now >= old_airing) or
                    (current_episode is not None and current_episode > old_episode)
                )
                if should_notify:
                    channel = bot.get_channel(int(row["channel_id"]))
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(int(row["channel_id"]))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            continue
                    await channel.send(
                        embed=card("📺 New anime episode", f"**{anime['title']} — Episode {old_episode}** should now be available.\n{anime['url']}"),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    last_notified = max(last_notified, old_episode)
                conn = db()
                conn.execute(
                    "UPDATE gargand_anime_watches SET title=?,anime_url=?,next_episode=?,next_airing_at=?,last_notified_episode=? WHERE id=?",
                    (anime["title"], anime["url"], current_episode, current_airing, last_notified, int(row["id"])),
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                print("Gargand anime poll warning:", type(exc).__name__, str(exc)[:150])

    async def dashboard_action(guild, payload):
        action = str(payload.get("action") or "")
        guild_id = int(guild.id)
        if action == "notification_rss_add":
            channel_id = str(payload.get("channel_id") or "").strip()
            feed_url = str(payload.get("feed_url") or "").strip()
            channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("Choose a text or announcement channel in this server.")
            items = await asyncio.to_thread(_fetch_feed, feed_url)
            latest = items[0]["guid"] if items else None
            conn = db()
            try:
                cursor = conn.execute(
                    "INSERT INTO gargand_rss_subscriptions(guild_id,channel_id,url,last_guid,created_at) VALUES(?,?,?,?,?)",
                    (guild_id, channel.id, feed_url, latest, int(time.time())),
                )
                subscription_id = int(cursor.lastrowid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise ValueError("That feed is already subscribed in this server.")
            finally:
                conn.close()
            return {"message": f"RSS feed added as subscription #{subscription_id}."}

        if action == "notification_rss_remove":
            try:
                subscription_id = int(payload.get("subscription_id"))
            except (TypeError, ValueError):
                raise ValueError("Choose a valid RSS subscription.")
            conn = db()
            cursor = conn.execute("DELETE FROM gargand_rss_subscriptions WHERE guild_id=? AND id=?", (guild_id, subscription_id))
            conn.commit()
            conn.close()
            if not cursor.rowcount:
                raise ValueError("That RSS subscription was not found.")
            return {"message": f"RSS subscription #{subscription_id} removed."}

        if action == "notification_anime_add":
            channel_id = str(payload.get("channel_id") or "").strip()
            channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
            if not isinstance(channel, discord.TextChannel):
                raise ValueError("Choose a text or announcement channel in this server.")
            try:
                anime_id = int(payload.get("anilist_id"))
            except (TypeError, ValueError):
                raise ValueError("Enter a valid AniList anime ID.")
            if not 0 < anime_id < 2**31:
                raise ValueError("Enter a valid positive AniList anime ID.")
            anime = await asyncio.to_thread(_fetch_anime, anime_id)
            conn = db()
            try:
                conn.execute(
                    "INSERT INTO gargand_anime_watches(guild_id,channel_id,anilist_id,title,anime_url,next_episode,next_airing_at,last_notified_episode,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (guild_id, channel.id, anime["id"], anime["title"], anime["url"], anime["episode"], anime["airing_at"], max(0, (anime["episode"] or 1) - 1), int(time.time())),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise ValueError("This anime is already being watched in that channel.")
            finally:
                conn.close()
            return {"message": f"Episode notifications enabled for {anime['title']}."}

        if action == "notification_anime_remove":
            try:
                anime_id = int(payload.get("anilist_id"))
            except (TypeError, ValueError):
                raise ValueError("Choose a valid AniList anime ID.")
            conn = db()
            cursor = conn.execute("DELETE FROM gargand_anime_watches WHERE guild_id=? AND anilist_id=?", (guild_id, anime_id))
            conn.commit()
            conn.close()
            if not cursor.rowcount:
                raise ValueError("No watched anime with that ID was found in this server.")
            return {"message": "Anime episode notifications removed."}

        raise ValueError("Unknown notification action.")

    DASHBOARD_ACTION_HANDLER = dashboard_action

    async def start_workers():
        await bot.wait_until_ready()
        try:
            if not rss_worker.is_running():
                rss_worker.start()
            if not anime_worker.is_running():
                anime_worker.start()
        except Exception as exc:
            print("Gargand community worker startup warning:", repr(exc))

    try:
        bot.tree.allowed_installs = discord.app_commands.AppInstallationType(guild=True, user=True)
        bot.tree.allowed_contexts = discord.app_commands.AppCommandContext(guild=True, bot_dm=True, private_channel=True)
    except (AttributeError, TypeError):
        pass
    asyncio.get_running_loop().create_task(start_workers())
    print("Gargand community features loaded: achievements, quests, co-op parties, seasonal events, image search, RSS, anime alerts, staff audit logs.")
