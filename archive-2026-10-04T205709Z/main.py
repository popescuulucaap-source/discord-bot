import asyncio
import base64
import io
import json
import os
import random
import re
import secrets
import sqlite3
import time
import requests
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENROUTER_TEXT_MODEL = os.getenv(
    "OPENROUTER_TEXT_MODEL",
    "openrouter/free"
)

# ============================================================
#  Gargand — all-in-one Discord community/moderation bot
#  Prefix: !
#  Database: SQLite (bot_data.sqlite3)
#  Library: discord.py 2.x
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
BOT_CLIENT_ID = os.getenv("BOT_CLIENT_ID")  # optional; automatically discovered after login
DB_PATH = os.getenv("BOT_DB", "bot_data.sqlite3")
OFFICIAL_GUILD_ID = int(os.getenv("GARGAND_OFFICIAL_GUILD_ID", "1561076743236747387"))
OFFICIAL_ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("GARGAND_ANNOUNCEMENT_CHANNEL_ID", "1561345082316038244"))

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN (or BOT_TOKEN) in your Fadehost environment variables.")

PREFIX = "!"
GARGAND_WEBSITE_URL = (os.getenv("GARGAND_WEBSITE_URL") or os.getenv("NIGHTFALL_WEBSITE_URL") or "https://gargand-bot.onrender.com").strip().rstrip("/")
GARGAND_BRIDGE_SECRET = (os.getenv("GARGAND_BRIDGE_SECRET") or os.getenv("NIGHTFALL_BRIDGE_SECRET") or "").strip()
BRIDGE_POLL_SECONDS = 10
bridge_task = None


async def send_telemetry(event_type: str, guild=None, user=None, command_name: str = "", metadata=None):
    if not GARGAND_WEBSITE_URL or not GARGAND_BRIDGE_SECRET:
        return
    payload = {
        "events": [{
            "event_type": event_type,
            "guild_id": getattr(guild, "id", None),
            "guild_name": getattr(guild, "name", ""),
            "user_id": getattr(user, "id", None),
            "username": str(user) if user else "",
            "command_name": command_name,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }]
    }
    def post():
        try:
            requests.post(
                f"{GARGAND_WEBSITE_URL}/api/bot/events",
                headers={"X-Gargand-Bridge-Key": GARGAND_BRIDGE_SECRET},
                json=payload,
                timeout=8,
            )
        except requests.RequestException:
            pass
    await asyncio.to_thread(post)

OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
AI_TEXT_COOLDOWN_SECONDS = 20
AI_TEXT_LAST_USED: dict[int, float] = {}
AI_TEXT_LOCK = asyncio.Lock()


async def command_prefix_for(message_bot, message):
    """Return the configured prefix for features that inspect message text."""
    if message.guild is None:
        return PREFIX
    return str(setting(message.guild, "prefix", PREFIX))


async def command_prefixes_for(message_bot, message):
    """Accept the configured prefix and preserve ! as a backwards-compatible prefix."""
    configured = await command_prefix_for(message_bot, message)
    # discord.py supports a list of prefixes from an async prefix callback.
    return list(dict.fromkeys(prefix for prefix in (configured, PREFIX) if prefix))


def make_bot_invite() -> str:
    client_id = BOT_CLIENT_ID or (str(bot.user.id) if bot.user else "YOUR_CLIENT_ID")
    perms = discord.Permissions()
    for flag in (
        "view_channel", "send_messages", "embed_links", "attach_files",
        "read_message_history", "manage_messages", "manage_channels",
        "manage_roles", "kick_members", "ban_members", "moderate_members",
        "view_audit_log", "manage_guild", "create_instant_invite",
        "mention_everyone",
    ):
        setattr(perms, flag, True)
    return f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot&permissions={perms.value}"


EMBED_COLOR = discord.Color.from_rgb(255, 197, 35)
SUCCESS = discord.Color.from_rgb(255, 213, 79)
WARNING = discord.Color.from_rgb(241, 196, 15)
DANGER = discord.Color.from_rgb(231, 76, 60)
INFO = discord.Color.from_rgb(255, 197, 35)
DARK = discord.Color.from_rgb(44, 47, 51)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.presences = False

bot = commands.Bot(
    command_prefix=command_prefixes_for,
    intents=INTENTS,
    help_command=None,
    case_insensitive=True,
    strip_after_prefix=True,
)

# -----------------------------
# Database
# -----------------------------

db_lock = asyncio.Lock()

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init():
    conn = db_connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            data TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS warnings (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_reason TEXT,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS afk (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            set_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS invites (
            guild_id INTEGER NOT NULL,
            invite_code TEXT NOT NULL,
            inviter_id INTEGER,
            uses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, invite_code)
        );

        CREATE TABLE IF NOT EXISTS invite_members (
            guild_id INTEGER NOT NULL,
            invited_user_id INTEGER NOT NULL,
            inviter_id INTEGER,
            invite_code TEXT,
            category TEXT NOT NULL DEFAULT 'clean',
            joined_at INTEGER NOT NULL,
            left_at INTEGER,
            rejoined INTEGER NOT NULL DEFAULT 0,
            j4j INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, invited_user_id)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winners INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            ended INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS sticks (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            message_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS gambling (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            coins INTEGER NOT NULL DEFAULT 0,
            last_daily INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS appeal_links (
            main_guild_id INTEGER PRIMARY KEY,
            appeal_guild_id INTEGER NOT NULL,
            invite_url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS jails (
            jail_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            jailed_at INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            appeal_created INTEGER NOT NULL DEFAULT 0,
            appeal_channel_id INTEGER,
            original_roles TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_jails_active ON jails(guild_id, user_id, active);
        CREATE TABLE IF NOT EXISTS moderation_cases (
            case_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'moderation_case',
            action TEXT NOT NULL,
            actor_id INTEGER,
            actor_name TEXT NOT NULL,
            target_id INTEGER,
            target_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT 'success',
            details TEXT NOT NULL DEFAULT '',
            channel_id INTEGER,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_moderation_cases_guild ON moderation_cases(guild_id, case_id DESC);
        CREATE INDEX IF NOT EXISTS idx_moderation_cases_target ON moderation_cases(guild_id, target_id, case_id DESC);
        CREATE TABLE IF NOT EXISTS case_appeals (
            appeal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            case_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            ticket_channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            reviewed_by INTEGER,
            reviewed_at INTEGER,
            decision_note TEXT NOT NULL DEFAULT '',
            UNIQUE(guild_id, case_id)
        );
        CREATE INDEX IF NOT EXISTS idx_case_appeals_pending ON case_appeals(guild_id, status, appeal_id DESC);
        CREATE TABLE IF NOT EXISTS verification_risk (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            risk_score INTEGER NOT NULL DEFAULT 0,
            reasons TEXT NOT NULL DEFAULT '[]',
            assessed_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS raid_lockdowns (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            previous_send_messages TEXT NOT NULL,
            previous_send_messages_in_threads TEXT NOT NULL,
            restore_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS recovery_snapshots (
            case_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            object_type TEXT NOT NULL,
            object_id INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            recovered_object_id INTEGER,
            recovered_by INTEGER
        );
CREATE TABLE IF NOT EXISTS speech_counts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            metric TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, metric)
        );
        """
    )
    conn.commit()
    conn.close()


def get_settings(guild_id: int) -> dict[str, Any]:
    conn = db_connect()
    row = conn.execute("SELECT data FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else {}


def save_settings(guild_id: int, data: dict[str, Any]):
    conn = db_connect()
    conn.execute(
        "INSERT INTO guild_settings(guild_id,data) VALUES(?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
        (guild_id, json.dumps(data)),
    )
    conn.commit()
    conn.close()


def setting(guild: discord.Guild, key: str, default=None):
    return get_settings(guild.id).get(key, default)


def bounded_setting_int(guild: discord.Guild, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(setting(guild, key, default) or default)))
    except (TypeError, ValueError):
        return default


def set_setting(guild: discord.Guild, key: str, value: Any):
    data = get_settings(guild.id)
    data[key] = value
    save_settings(guild.id, data)


def add_speech_count(guild_id: int, user_id: int, metric: str, amount: int):
    if amount <= 0:
        return
    conn = db_connect()
    conn.execute(
        "INSERT INTO speech_counts(guild_id,user_id,metric,count) VALUES(?,?,?,?) "
        "ON CONFLICT(guild_id,user_id,metric) DO UPDATE SET count=count+excluded.count",
        (guild_id, user_id, metric, amount),
    )
    conn.commit()
    conn.close()


def get_speech_count(guild_id: int, user_id: int, metric: str) -> int:
    conn = db_connect()
    row = conn.execute("SELECT count FROM speech_counts WHERE guild_id=? AND user_id=? AND metric=?", (guild_id, user_id, metric)).fetchone()
    conn.close()
    return int(row["count"]) if row else 0


def swear_word_pattern(word: str):
    parts = word.split()
    if not parts:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(part) for part in parts) + r"(?!\w)", re.IGNORECASE)


# -----------------------------
# Helpers
# -----------------------------

def utc_ts() -> int:
    return int(time.time())


def embed(title: str, description: str, color: discord.Color = EMBED_COLOR) -> discord.Embed:
    e = discord.Embed(title=title, description=(description or "")[:4096], color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text="✦ GARGAND • COMMUNITY SYSTEMS ✦")
    # Prefer the official GARGAND lion hosted by the website, with a safe fallback.
    pfp_url = (os.getenv("GARGAND_PFP_URL") or "").strip()
    if not pfp_url and GARGAND_WEBSITE_URL:
        pfp_url = f"{GARGAND_WEBSITE_URL}/gargand-lion.jpg"
    e.set_thumbnail(url=pfp_url or "https://www.svgrepo.com/show/17505/lion-head.svg")
    # Keep the embed compact; the lion thumbnail and gold accent carry the branding.
    if bot.user:
        e.set_author(name="GARGAND  /  COMMUNITY SYSTEMS", icon_url=bot.user.display_avatar.url)
    return e


def mentionable_name(user: discord.abc.User) -> str:
    return getattr(user, "mention", user.name)


def parse_duration(value: str) -> int:
    value = value.lower().strip()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", value)
    if not m:
        raise ValueError("Use a duration like `30s`, `10m`, `2h`, `1d`, or `1w`.")
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


def human_duration(seconds: int) -> str:
    parts = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        q, seconds = divmod(seconds, size)
        if q:
            parts.append(f"{q}{name}")
    return " ".join(parts) or "0s"


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild.owner_id == member.id


def is_staff(member: discord.Member) -> bool:
    if is_admin(member):
        return True
    role_id = setting(member.guild, "staff_role_id")
    return bool(role_id and any(r.id == role_id for r in member.roles))


def staff_only():
    async def predicate(ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
            raise commands.CheckFailure("This is a staff-only command.")
        return True
    return commands.check(predicate)


def admin_only():
    async def predicate(ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.author, discord.Member) or not is_admin(ctx.author):
            raise commands.CheckFailure("Administrator permissions are required.")
        return True
    return commands.check(predicate)


def member_is_protected(member: discord.Member, actor: discord.Member, guild: discord.Guild) -> bool:
    if member.id == guild.owner_id:
        return True
    if member.id == actor.id:
        return True
    me = guild.me
    if not me:
        return True
    return member.top_role >= me.top_role


async def safe_dm(user: discord.abc.User, *, embed_obj: discord.Embed, view: Optional[discord.ui.View] = None):
    try:
        await user.send(embed=embed_obj, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def retry_after_from_http(exc: discord.HTTPException, default: float = 2.0) -> float:
    """Read Retry-After from Discord/KataProxy when available."""
    retry_after = default
    try:
        headers = exc.response.headers
        retry_after = float(headers.get("Retry-After", default))
    except Exception:
        pass
    return min(max(retry_after, 1.0), 10.0)


async def send_followup_resilient(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
    attempts: int = 4,
):
    """Send a deferred interaction follow-up with short 429 retries."""
    for attempt in range(attempts):
        try:
            return await interaction.followup.send(content, ephemeral=ephemeral)
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) != 429 or attempt == attempts - 1:
                raise
            await asyncio.sleep(await retry_after_from_http(exc, 1.5) + 0.25 * attempt)


async def ensure_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Use the configured log channel or create a private Gargand log channel."""
    channel_id = setting(guild, "logs_channel_id")
    channel = guild.get_channel(int(channel_id)) if channel_id and str(channel_id).isdigit() else None
    if isinstance(channel, discord.TextChannel):
        return channel
    existing = discord.utils.get(guild.text_channels, name="gargand-logs")
    if existing:
        set_setting(guild, "logs_channel_id", existing.id)
        return existing
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        return None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
    }
    staff_id = setting(guild, "staff_role_id")
    staff = guild.get_role(int(staff_id)) if staff_id and str(staff_id).isdigit() else None
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    try:
        channel = await guild.create_text_channel(
            "gargand-logs",
            overwrites=overwrites,
            topic="Private Gargand moderation and security log. Configure another channel with !setup anytime.",
            reason="Gargand automatic logging setup",
        )
        set_setting(guild, "logs_channel_id", channel.id)
        try:
            await channel.send(embed=embed("🟢 Gargand logging is ready", "This private channel is now the server's default moderation and security log. Change it any time in !setup.", SUCCESS))
        except discord.HTTPException:
            pass
        return channel
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Could not create Gargand log channel in {guild.id}: {type(exc).__name__}")
        return None


async def log_action(guild: discord.Guild, title: str, description: str, color: discord.Color = INFO):
    channel = await ensure_log_channel(guild)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed(title, description, color))
    except discord.HTTPException:
        pass


async def log_automod_action(guild: discord.Guild, title: str, description: str, color: discord.Color = WARNING):
    """Send Auto-Mod events to the dedicated channel, falling back to normal logs."""
    try:
        configured_id = int(get_settings(guild.id).get("automod_log_channel_id") or 0)
    except (TypeError, ValueError):
        configured_id = 0
    channel = guild.get_channel(configured_id) if configured_id else None
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed(title, description, color))
            return
        except (discord.Forbidden, discord.HTTPException):
            pass
    await log_action(guild, title, description, color)


def record_moderation_case(
    guild: discord.Guild,
    action: str,
    actor=None,
    target=None,
    reason: str = "No reason provided",
    *,
    outcome: str = "success",
    details: str = "",
    expires_at: int | None = None,
    channel=None,
    event_type: str = "moderation_case",
) -> int:
    """Persist a searchable case locally and mirror it to the website bridge."""
    actor = actor or bot.user
    actor_id = getattr(actor, "id", None)
    actor_name = str(actor) if actor else "Gargand"
    target_id = getattr(target, "id", None)
    target_name = getattr(target, "display_name", None) or (str(target) if target else "Server")
    created_at = utc_ts()
    conn = db_connect()
    cursor = conn.execute(
        "INSERT INTO moderation_cases(guild_id,event_type,action,actor_id,actor_name,target_id,target_name,reason,outcome,details,channel_id,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            guild.id, event_type[:40], action[:80], actor_id, actor_name[:120], target_id,
            str(target_name)[:120], str(reason or "No reason provided")[:1000], outcome[:40],
            str(details or "")[:1000], getattr(channel, "id", None), created_at, expires_at,
        ),
    )
    case_id = int(cursor.lastrowid)
    conn.commit()
    conn.close()

    metadata = {
        "case_id": case_id,
        "record_type": "incident" if event_type == "security_incident" else "case",
        "action": action[:80],
        "target_id": str(target_id) if target_id is not None else "",
        "target_name": str(target_name)[:120],
        "reason": str(reason or "No reason provided")[:1000],
        "outcome": outcome[:40],
        "details": str(details or "")[:1000],
        "channel_id": str(getattr(channel, "id", "") or ""),
        "expires_at": expires_at,
    }
    asyncio.create_task(send_telemetry(
        event_type,
        guild=guild,
        user=actor,
        command_name=action[:120],
        metadata=metadata,
    ))
    return case_id


def recovery_overwrites(channel: discord.abc.GuildChannel) -> list[dict[str, Any]]:
    saved = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        saved.append({
            "target_type": "role" if isinstance(target, discord.Role) else "member",
            "target_id": int(target.id),
            "allow": int(allow.value),
            "deny": int(deny.value),
        })
    return saved


def channel_recovery_snapshot(channel: discord.abc.GuildChannel) -> dict[str, Any] | None:
    if isinstance(channel, discord.CategoryChannel):
        channel_type = "category"
    elif isinstance(channel, discord.ForumChannel):
        channel_type = "forum"
    elif isinstance(channel, discord.StageChannel):
        channel_type = "stage"
    elif isinstance(channel, discord.VoiceChannel):
        channel_type = "voice"
    elif isinstance(channel, discord.TextChannel):
        channel_type = "text"
    else:
        return None
    snapshot: dict[str, Any] = {
        "id": int(channel.id),
        "type": channel_type,
        "name": str(channel.name),
        "position": int(channel.position),
        "category_id": int(channel.category_id) if channel.category_id else None,
        "overwrites": recovery_overwrites(channel),
    }
    for attribute in ("topic", "slowmode_delay", "nsfw", "default_auto_archive_duration", "bitrate", "user_limit", "rtc_region"):
        value = getattr(channel, attribute, None)
        if value is not None:
            snapshot[attribute] = value
    quality = getattr(channel, "video_quality_mode", None)
    if quality is not None:
        snapshot["video_quality_mode"] = int(quality.value)
    if isinstance(channel, discord.TextChannel):
        snapshot["news"] = bool(channel.is_news())
    return snapshot


def role_recovery_snapshot(role: discord.Role) -> dict[str, Any]:
    return {
        "id": int(role.id),
        "name": str(role.name),
        "permissions": int(role.permissions.value),
        "colour": int(role.colour.value),
        "hoist": bool(role.hoist),
        "mentionable": bool(role.mentionable),
        "position": int(role.position),
    }


def recovery_changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return [
        key for key in before
        if key != "id" and json.dumps(before.get(key), sort_keys=True) != json.dumps(after.get(key), sort_keys=True)
    ]


def save_recovery_snapshot(case_id: int, guild_id: int, object_type: str, object_id: int, snapshot: dict[str, Any]):
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO recovery_snapshots(case_id,guild_id,object_type,object_id,snapshot,created_at,status) VALUES(?,?,?,?,?,?, 'available')",
        (case_id, guild_id, object_type, object_id, json.dumps(snapshot, separators=(",", ":")), utc_ts()),
    )
    conn.commit()
    conn.close()


def restored_channel_overwrites(guild: discord.Guild, saved: list[dict[str, Any]]) -> tuple[dict[Any, discord.PermissionOverwrite], int]:
    overwrites: dict[Any, discord.PermissionOverwrite] = {}
    skipped = 0
    for item in saved:
        target = guild.get_role(int(item["target_id"])) if item.get("target_type") == "role" else guild.get_member(int(item["target_id"]))
        if target is None:
            skipped += 1
            continue
        allow = discord.Permissions(int(item.get("allow", 0)))
        deny = discord.Permissions(int(item.get("deny", 0)))
        overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
    return overwrites, skipped


async def restore_recovery_snapshot(guild: discord.Guild, case_id: int, actor: discord.Member) -> tuple[bool, str]:
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM recovery_snapshots WHERE guild_id=? AND case_id=?",
        (guild.id, case_id),
    ).fetchone()
    conn.close()
    if not row:
        return False, f"Case `#{case_id}` has no recovery snapshot."
    if row["status"] != "available":
        return False, f"Recovery for case `#{case_id}` was already used ({row['status']})."
    snapshot = json.loads(row["snapshot"])
    object_type = row["object_type"]
    object_id = int(row["object_id"])
    restored_id = object_id
    skipped_overwrites = 0
    try:
        if object_type == "role":
            role = guild.get_role(object_id)
            role_kwargs = {
                "name": snapshot["name"],
                "permissions": discord.Permissions(int(snapshot["permissions"])),
                "colour": discord.Colour(int(snapshot["colour"])),
                "hoist": bool(snapshot["hoist"]),
                "mentionable": bool(snapshot["mentionable"]),
                "reason": f"Gargand recovery of case #{case_id} by {actor}",
            }
            if role:
                await role.edit(**role_kwargs)
                await role.edit(position=max(1, int(snapshot.get("position", role.position))), reason=f"Gargand recovery of case #{case_id}")
            else:
                role = await guild.create_role(**role_kwargs)
                restored_id = role.id
                await role.edit(position=max(1, int(snapshot.get("position", role.position))), reason=f"Gargand recovery of case #{case_id}")
            summary = f"Restored role **@{role.name}**. Discord role membership assignments are not recreated."
        elif object_type == "channel":
            current = guild.get_channel(object_id)
            channel_type = snapshot.get("type")
            category_id = snapshot.get("category_id")
            category = guild.get_channel(int(category_id)) if category_id else None
            if category is not None and not isinstance(category, discord.CategoryChannel):
                category = None
            overwrites, skipped_overwrites = restored_channel_overwrites(guild, snapshot.get("overwrites") or [])
            if current is None:
                common = {
                    "name": snapshot["name"], "overwrites": overwrites,
                    "position": max(0, int(snapshot.get("position", 0))),
                    "reason": f"Gargand recovery of case #{case_id} by {actor}",
                }
                if channel_type == "category":
                    current = await guild.create_category(**common)
                elif channel_type == "text":
                    common.update({"category": category, "topic": snapshot.get("topic"), "slowmode_delay": int(snapshot.get("slowmode_delay", 0)), "nsfw": bool(snapshot.get("nsfw", False))})
                    common["news"] = bool(snapshot.get("news", False))
                    if snapshot.get("default_auto_archive_duration") is not None:
                        common["default_auto_archive_duration"] = int(snapshot["default_auto_archive_duration"])
                    current = await guild.create_text_channel(**common)
                elif channel_type == "voice":
                    common.update({"category": category, "bitrate": int(snapshot.get("bitrate", 64000)), "user_limit": int(snapshot.get("user_limit", 0))})
                    if snapshot.get("rtc_region") is not None:
                        common["rtc_region"] = snapshot["rtc_region"]
                    current = await guild.create_voice_channel(**common)
                elif channel_type == "stage":
                    common.update({"category": category, "topic": snapshot.get("topic"), "bitrate": int(snapshot.get("bitrate", 64000))})
                    if snapshot.get("rtc_region") is not None:
                        common["rtc_region"] = snapshot["rtc_region"]
                    current = await guild.create_stage_channel(**common)
                elif channel_type == "forum":
                    common.update({"category": category, "topic": snapshot.get("topic"), "slowmode_delay": int(snapshot.get("slowmode_delay", 0)), "nsfw": bool(snapshot.get("nsfw", False))})
                    current = await guild.create_forum(**common)
                else:
                    return False, f"Channel type `{channel_type}` cannot be recreated automatically. The snapshot remains available."
                restored_id = current.id
            else:
                values = {"name": snapshot["name"], "position": max(0, int(snapshot.get("position", 0))), "overwrites": overwrites, "category": category}
                if hasattr(current, "topic"):
                    values["topic"] = snapshot.get("topic")
                if hasattr(current, "slowmode_delay"):
                    values["slowmode_delay"] = int(snapshot.get("slowmode_delay", 0))
                if hasattr(current, "nsfw"):
                    values["nsfw"] = bool(snapshot.get("nsfw", False))
                if hasattr(current, "bitrate"):
                    values["bitrate"] = int(snapshot.get("bitrate", current.bitrate))
                if hasattr(current, "user_limit"):
                    values["user_limit"] = int(snapshot.get("user_limit", current.user_limit))
                if hasattr(current, "rtc_region"):
                    values["rtc_region"] = snapshot.get("rtc_region")
                await current.edit(**values, reason=f"Gargand recovery of case #{case_id} by {actor}")
            summary = f"Restored **#{current.name}** ({current.type})."
            if skipped_overwrites:
                summary += f" Skipped {skipped_overwrites} permission overwrite(s) for roles or members no longer in the server."
        else:
            return False, "This snapshot type is not supported."
    except (discord.Forbidden, discord.HTTPException, discord.NotFound, ValueError, TypeError, KeyError) as exc:
        return False, f"Discord could not restore case `#{case_id}` ({type(exc).__name__}). Check Gargand's permissions and role order; the snapshot remains available."

    conn = db_connect()
    conn.execute(
        "UPDATE recovery_snapshots SET status='recovered',recovered_object_id=?,recovered_by=? WHERE guild_id=? AND case_id=? AND status='available'",
        (restored_id, actor.id, guild.id, case_id),
    )
    conn.commit()
    conn.close()
    record_moderation_case(guild, "recovery", actor, None, f"Recovery of case #{case_id}", outcome="success", details=summary)
    await log_action(guild, "♻️ Security recovery complete", f"{summary}\n**Source case:** `#{case_id}`\n**Recovered by:** {actor.mention}", SUCCESS)
    return True, summary


@bot.listen("on_member_join")
async def gargand_log_member_join(member: discord.Member):
    await log_action(member.guild, "↗ Member joined", f"**{member}** ({member.id}) joined the server.", SUCCESS)


@bot.listen("on_member_remove")
async def gargand_log_member_remove(member: discord.Member):
    await log_action(member.guild, "↘ Member left", f"**{member}** ({member.id}) left the server.", WARNING)


@bot.listen("on_member_ban")
async def gargand_log_member_ban(guild: discord.Guild, user: discord.User):
    await log_action(guild, "⛔ Member banned", f"**{user}** ({user.id}) was banned.", DANGER)


@bot.listen("on_member_unban")
async def gargand_log_member_unban(guild: discord.Guild, user: discord.User):
    await log_action(guild, "🟢 Member unbanned", f"**{user}** ({user.id}) was unbanned.", SUCCESS)


@bot.listen("on_guild_channel_delete")
async def gargand_log_channel_delete(channel: discord.abc.GuildChannel):
    if channel.name != "gargand-logs":
        await log_action(channel.guild, "⚠ Channel deleted", f"**#{channel.name}** ({channel.id}) was deleted.", WARNING)


@bot.listen("on_guild_role_delete")
async def gargand_log_role_delete(role: discord.Role):
    await log_action(role.guild, "⚠ Role deleted", f"**{role.name}** ({role.id}) was deleted.", WARNING)


async def get_or_create_role(guild: discord.Guild, name: str, *, color=discord.Color.default()) -> discord.Role:
    role = discord.utils.get(guild.roles, name=name)
    if role:
        return role
    return await guild.create_role(name=name, color=color, reason="Gargand automatic setup")


async def configure_verification_roles(guild: discord.Guild):
    unverified = await get_or_create_role(guild, "Unverified", color=discord.Color.dark_grey())
    verified = await get_or_create_role(guild, "Verified", color=SUCCESS)
    set_setting(guild, "unverified_role_id", unverified.id)
    set_setting(guild, "verified_role_id", verified.id)
    return unverified, verified


async def apply_external_app_lock(guild: discord.Guild):
    """Disable @everyone's external-app permission across the server.

    Katabump's proxy can return 429s when many permission edits happen quickly,
    so edits are deliberately paced and retried. The setup interaction is
    deferred before this function is called, so a slow but successful lockdown
    does not produce an Unknown interaction error.
    """
    changed = 0
    rate_limited = 0
    targets = [
        ch for ch in guild.channels
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel, discord.CategoryChannel))
    ]

    for index, channel in enumerate(targets):
        success = False
        for attempt in range(4):
            try:
                ow = channel.overwrites_for(guild.default_role)
                ow.use_external_apps = False
                await channel.set_permissions(
                    guild.default_role,
                    overwrite=ow,
                    reason="Gargand anti-raid",
                )
                changed += 1
                success = True
                break
            except discord.Forbidden:
                break
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) != 429:
                    break
                rate_limited += 1
                retry_after = 2.0
                try:
                    retry_after = float(getattr(exc, "response", None).headers.get("Retry-After", retry_after))
                except Exception:
                    pass
                await asyncio.sleep(min(max(retry_after, 1.0), 8.0) + (0.25 * attempt))
            except AttributeError:
                break

        # Keep requests spread out so KataProxy/Discord do not receive a burst.
        if index < len(targets) - 1:
            await asyncio.sleep(0.45 if success else 0.2)

    return changed, rate_limited, len(targets)


async def emergency_lockdown(guild: discord.Guild):
    # Safer fallback than deleting every role: remove @everyone's ability to send
    # and use external apps, while preserving the server's role structure.
    for channel in guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
            try:
                ow = channel.overwrites_for(guild.default_role)
                ow.send_messages = False
                ow.use_external_apps = False
                await channel.set_permissions(guild.default_role, overwrite=ow, reason="Gargand emergency anti-nuke lockdown")
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass


def _decode_overwrite_flag(value: str):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


async def apply_raid_lockdown_to_channel(guild: discord.Guild, channel, restore_at: int, reason: str) -> bool:
    conn = db_connect()
    saved = conn.execute(
        "SELECT 1 FROM raid_lockdowns WHERE guild_id=? AND channel_id=?",
        (guild.id, channel.id),
    ).fetchone()
    if not saved:
        previous = channel.overwrites_for(guild.default_role)
        conn.execute(
            "INSERT OR IGNORE INTO raid_lockdowns(guild_id,channel_id,previous_send_messages,previous_send_messages_in_threads,restore_at) VALUES(?,?,?,?,?)",
            (guild.id, channel.id, json.dumps(previous.send_messages), json.dumps(previous.send_messages_in_threads), restore_at),
        )
    else:
        conn.execute("UPDATE raid_lockdowns SET restore_at=? WHERE guild_id=? AND channel_id=?", (restore_at, guild.id, channel.id))
    conn.commit()
    conn.close()

    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = False
    overwrite.send_messages_in_threads = False
    try:
        await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"Gargand temporary raid lockdown: {reason[:350]}")
        return True
    except (discord.Forbidden, discord.HTTPException):
        if not saved:
            conn = db_connect()
            conn.execute("DELETE FROM raid_lockdowns WHERE guild_id=? AND channel_id=?", (guild.id, channel.id))
            conn.commit()
            conn.close()
        return False


async def start_raid_lockdown(guild: discord.Guild, minutes: int, *, actor=None, reason="join surge"):
    """Temporarily pause @everyone posting while keeping an exact restore snapshot."""
    restore_at = utc_ts() + max(1, min(int(minutes), 60)) * 60
    conn = db_connect()
    conn.execute("UPDATE raid_lockdowns SET restore_at=? WHERE guild_id=?", (restore_at, guild.id))
    conn.commit()
    conn.close()

    locked = 0
    failed = 0
    channels = [ch for ch in guild.channels if isinstance(ch, (discord.TextChannel, discord.ForumChannel))]
    for channel in channels:
        if await apply_raid_lockdown_to_channel(guild, channel, restore_at, reason):
            locked += 1
        else:
            failed += 1
        await asyncio.sleep(0.12)

    if locked:
        set_setting(guild, "raid_lockdown_until", restore_at)
        set_setting(guild, "raid_lockdown_reason", str(reason)[:200])
        case_id = record_moderation_case(
            guild, "raid_lockdown", actor, None,
            reason=f"Temporary posting lockdown: {reason}",
            details=f"Locked {locked} channels; {failed} could not be changed. Automatic expiry in {minutes} minutes.",
            expires_at=restore_at,
        )
        await log_action(
            guild,
            "🚨 Temporary raid lockdown",
            f"@everyone posting is paused in **{locked}** channels for **{minutes} minutes**.\n**Case:** `#{case_id}`\n**Reason:** {reason}\nThe original channel permissions will be restored automatically.",
            DANGER,
        )
    return locked, failed, restore_at


async def restore_raid_lockdown_channel(guild: discord.Guild, channel_id: int) -> bool:
    conn = db_connect()
    saved = conn.execute(
        "SELECT previous_send_messages,previous_send_messages_in_threads FROM raid_lockdowns WHERE guild_id=? AND channel_id=?",
        (guild.id, channel_id),
    ).fetchone()
    conn.close()
    if not saved:
        return True
    channel = guild.get_channel(channel_id)
    if not channel:
        conn = db_connect()
        conn.execute("DELETE FROM raid_lockdowns WHERE guild_id=? AND channel_id=?", (guild.id, channel_id))
        conn.commit()
        conn.close()
        return True
    overwrite = channel.overwrites_for(guild.default_role)
    overwrite.send_messages = _decode_overwrite_flag(saved["previous_send_messages"])
    overwrite.send_messages_in_threads = _decode_overwrite_flag(saved["previous_send_messages_in_threads"])
    try:
        await channel.set_permissions(
            guild.default_role,
            overwrite=None if overwrite.is_empty() else overwrite,
            reason="Gargand raid lockdown ended; restore original permissions",
        )
    except (discord.Forbidden, discord.HTTPException):
        return False
    conn = db_connect()
    conn.execute("DELETE FROM raid_lockdowns WHERE guild_id=? AND channel_id=?", (guild.id, channel_id))
    conn.commit()
    conn.close()
    return True


async def release_raid_lockdown(guild: discord.Guild, *, actor=None, reason="manual recovery"):
    conn = db_connect()
    rows = conn.execute("SELECT channel_id FROM raid_lockdowns WHERE guild_id=? ORDER BY channel_id", (guild.id,)).fetchall()
    conn.close()
    restored = 0
    failed = 0
    for row in rows:
        if await restore_raid_lockdown_channel(guild, int(row["channel_id"])):
            restored += 1
        else:
            failed += 1
    conn = db_connect()
    remaining = conn.execute("SELECT COUNT(*) FROM raid_lockdowns WHERE guild_id=?", (guild.id,)).fetchone()[0]
    conn.close()
    if not remaining:
        set_setting(guild, "raid_lockdown_until", 0)
        set_setting(guild, "raid_lockdown_reason", "")
    if restored:
        case_id = record_moderation_case(
            guild, "raid_recovery", actor, None,
            reason=reason,
            details=f"Restored original posting permissions in {restored} channels; {failed} still need retry.",
        )
        await log_action(
            guild,
            "🟢 Raid lockdown recovery",
            f"Original permissions restored in **{restored}** channels. **{failed}** channels could not be restored yet.\n**Case:** `#{case_id}`",
            SUCCESS if not failed else WARNING,
        )
    return restored, failed, int(remaining)


@tasks.loop(seconds=30)
async def raid_lockdown_restore_watcher():
    conn = db_connect()
    rows = conn.execute("SELECT DISTINCT guild_id FROM raid_lockdowns WHERE restore_at<=?", (utc_ts(),)).fetchall()
    conn.close()
    for row in rows:
        guild = bot.get_guild(int(row["guild_id"]))
        if not guild:
            continue
        due = db_connect()
        channels = due.execute("SELECT channel_id FROM raid_lockdowns WHERE guild_id=? AND restore_at<=?", (guild.id, utc_ts())).fetchall()
        due.close()
        for item in channels:
            await restore_raid_lockdown_channel(guild, int(item["channel_id"]))
        check = db_connect()
        remaining = check.execute("SELECT COUNT(*) FROM raid_lockdowns WHERE guild_id=?", (guild.id,)).fetchone()[0]
        check.close()
        if not remaining:
            set_setting(guild, "raid_lockdown_until", 0)
            set_setting(guild, "raid_lockdown_reason", "")
            case_id = record_moderation_case(
                guild, "raid_recovery", bot.user, None,
                reason="Automatic expiry",
                details="The temporary raid lockdown expired and original channel permissions were restored.",
                event_type="security_incident",
            )
            await log_action(guild, "🟢 Raid lockdown expired", f"Gargand restored the saved channel permissions.\n**Case:** `#{case_id}`", SUCCESS)


@raid_lockdown_restore_watcher.before_loop
async def before_raid_lockdown_restore_watcher():
    await bot.wait_until_ready()


async def create_ticket(guild: discord.Guild, user: discord.Member, ticket_type: str, answers: list[tuple[str, str]] | None = None):
    normalized_type = re.sub(r"[^a-z0-9_-]", "", str(ticket_type).lower())[:32] or "support"
    marker = f"gargand-ticket-owner:{user.id};type:{normalized_type}"
    # Avoid accidentally creating multiple active tickets when a user double-clicks
    # the panel or submits a modal twice. Closed tickets are deliberately excluded.
    existing = next(
        (ch for ch in guild.text_channels
         if ((ch.topic or "") == marker or (ch.topic or "").startswith(marker + ";"))
         and not ch.name.startswith("closed-")),
        None,
    )
    if existing:
        return existing
    category_id = setting(guild, "ticket_category_id")
    staff_role_id = setting(guild, "staff_role_id")
    category = guild.get_channel(category_id) if category_id else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
    name = f"ticket-{normalized_type}-{user.name[:12]}".lower().replace(" ", "-")[:90]
    try:
        channel = await guild.create_text_channel(
            name,
            topic=marker,
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="Gargand ticket system",
        )
    except discord.Forbidden:
        return None
    qtext = ""
    if answers:
        qtext = "\n".join(f"**{i+1}. {q}**\n{a or '—'}" for i, (q, a) in enumerate(answers))
    ticket_embed = embed(
        f"🎫 {ticket_type.title()} ticket",
        f"Welcome {user.mention}! A member of staff will be with you shortly.\n\n{qtext}" if qtext else f"Welcome {user.mention}! A member of staff will be with you shortly.",
        EMBED_COLOR,
    )
    ticket_embed.set_thumbnail(url=user.display_avatar.url)
    try:
        await channel.send(embed=ticket_embed, view=TicketControlView())
        if staff_role:
            await channel.send(content=staff_role.mention, allowed_mentions=discord.AllowedMentions(roles=True))
        if ticket_type.lower() == "claim":
            await channel.send(embed=embed("📸 Proof required", f"{user.mention}, please upload a clear photo/screenshot of your claim here. Staff can use `!proof please` after you post it.", WARNING))
    except (discord.Forbidden, discord.HTTPException) as exc:
        # Do not leave an inaccessible or half-initialized private ticket behind.
        try:
            await channel.delete(reason="Gargand ticket initialization failed")
        except (discord.Forbidden, discord.HTTPException):
            pass
        print(f"Ticket initialization failed in guild {guild.id}: {type(exc).__name__}")
        return None
    asyncio.create_task(send_telemetry(
        "ticket_created",
        guild=guild,
        user=user,
        # Keep analytics metadata minimal; ticket answers can contain private
        # information and belong in the private ticket channel, not telemetry.
        metadata={
            "ticket_type": str(ticket_type)[:80],
            "channel_id": channel.id,
            "question_count": min(len(answers or []), 5),
        },
    ))
    return channel


# -----------------------------
# Verification challenge image
# -----------------------------

def make_challenge_image(phrase: str) -> discord.File:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required for verification images") from exc
    img = Image.new("RGB", (900, 300), (20, 22, 35))
    draw = ImageDraw.Draw(img)
    for x in range(0, 900, 30):
        draw.line((x, 0, x + 260, 300), fill=(55, 47 + (x // 30) % 20, 90), width=3)
    try:
        font_path = "DejaVuSans-Bold.ttf"
        small = ImageFont.truetype("DejaVuSans.ttf", 22)
    except Exception:
        font_path = None
        small = ImageFont.load_default()
    draw.text((42, 38), "GARGAND VERIFICATION", font=small, fill=(180, 175, 215))
    words = phrase.split()
    split_at = max(1, (len(words) + 1) // 2)
    lines = [" ".join(words[:split_at]), " ".join(words[split_at:])]
    if font_path:
        font = ImageFont.truetype(font_path, 46)
        for size in range(46, 25, -2):
            candidate = ImageFont.truetype(font_path, size)
            if all(draw.textbbox((0, 0), line, font=candidate)[2] <= 820 for line in lines):
                font = candidate
                break
    else:
        font = ImageFont.load_default()
    for line, y in zip(lines, (96, 158)):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((900 - tw) / 2, y), line, font=font, fill=(246, 244, 255))
    draw.text((42, 254), "This challenge is private and changes every attempt.", font=small, fill=(155, 160, 180))
    out = io.BytesIO(); img.save(out, "PNG"); out.seek(0)
    return discord.File(out, filename="verification.png")


# -----------------------------
# Animated welcome/leave card
# -----------------------------

def make_member_card(member: discord.Member, title: str, subtitle: str) -> discord.File:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        raise RuntimeError("Pillow is required for welcome/leave cards. Install `pillow`.")

    w, h = 1000, 420
    base = Image.new("RGB", (w, h), (18, 20, 30))
    px = base.load()
    for x in range(w):
        for y in range(h):
            t = x / w
            px[x, y] = (int(20 + 65 * t), int(22 + 30 * (1-t)), int(38 + 75 * t))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(160, 0, -12):
        a = max(0, int(60 * (1 - r / 160)))
        gd.ellipse((690-r, 170-r, 690+r, 170+r), fill=(120, 100, 255, a))
    glow = glow.filter(ImageFilter.GaussianBlur(10))
    base = Image.alpha_composite(base.convert("RGBA"), glow)
    draw = ImageDraw.Draw(base)

    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 54)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 28)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 22)
    except Exception:
        font_big = ImageFont.load_default()
        font_mid = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # Avatar
    avatar_bytes = None
    try:
        # The avatar is downloaded later by the async wrapper; this placeholder is intentional.
        pass
    except Exception:
        pass

    # Decorative circle + text.
    cx, cy, rad = 150, 210, 105
    draw.ellipse((cx-rad, cy-rad, cx+rad, cy+rad), outline=(170, 150, 255, 255), width=6)
    draw.ellipse((cx-rad+12, cy-rad+12, cx+rad-12, cy+rad-12), fill=(50, 44, 78, 255))
    draw.text((305, 95), title, font=font_big, fill=(245, 245, 255, 255))
    draw.text((305, 170), member.display_name[:30], font=font_mid, fill=(195, 190, 230, 255))
    draw.text((305, 220), subtitle[:70], font=font_small, fill=(170, 170, 185, 255))
    draw.text((305, 285), "✦ Gargand Community ✦", font=font_small, fill=(150, 135, 240, 255))

    out = io.BytesIO()
    base.convert("RGB").save(out, "PNG")
    out.seek(0)
    return discord.File(out, filename="member_card.png")


async def make_member_card_with_avatar(member: discord.Member, title: str, subtitle: str) -> discord.File:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        return make_member_card(member, title, subtitle)

    w, h = 1000, 420
    base = Image.new("RGB", (w, h), (18, 20, 30))
    for x in range(w):
        shade = int(20 + 65 * x / w)
        ImageDraw.Draw(base).line((x, 0, x, h), fill=(shade, 22, min(125, 38 + int(70 * x / w))))
    base = base.convert("RGBA")
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse((520, -170, 920, 230), fill=(130, 100, 255, 38))
    overlay = overlay.filter(ImageFilter.GaussianBlur(32))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    avatar_size = 190
    try:
        avatar_data = await member.display_avatar.read()
        avatar = Image.open(io.BytesIO(avatar_data)).convert("RGB").resize((avatar_size, avatar_size))
        mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
        base.paste(avatar, (55, 115), mask)
    except Exception:
        draw.ellipse((55, 115, 245, 305), fill=(55, 55, 75, 255), outline=(170, 150, 255, 255), width=6)

    try:
        font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 54)
        font_mid = ImageFont.truetype("DejaVuSans.ttf", 28)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 22)
    except Exception:
        font_big = font_mid = font_small = ImageFont.load_default()
    draw.text((305, 90), title, font=font_big, fill=(245, 245, 255, 255))
    draw.text((305, 170), member.display_name[:30], font=font_mid, fill=(205, 200, 235, 255))
    draw.text((305, 220), subtitle[:80], font=font_small, fill=(175, 175, 190, 255))
    draw.text((305, 285), "✦ Gargand Community ✦", font=font_small, fill=(150, 135, 240, 255))
    out = io.BytesIO()
    base.convert("RGB").save(out, "PNG")
    out.seek(0)
    return discord.File(out, filename="member_card.png")


# -----------------------------
# Persistent-ish UI classes
# -----------------------------

VERIFICATION_FAILURES: dict[tuple[int, int], list[float]] = {}
VERIFICATION_LOCKED_UNTIL: dict[tuple[int, int], float] = {}
VERIFICATION_NEXT_CHALLENGE: dict[tuple[int, int], float] = {}
VERIFICATION_VIEW_REGISTERED = False
CASE_APPEAL_VIEWS_REGISTERED = False


def verification_cooldown_remaining(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = time.time()
    until = VERIFICATION_LOCKED_UNTIL.get(key, 0)
    if until <= now:
        VERIFICATION_LOCKED_UNTIL.pop(key, None)
        return 0
    return max(1, int(until - now) + 1)


def record_verification_failure(guild_id: int, user_id: int) -> int:
    key = (guild_id, user_id)
    now = time.time()
    attempts = [stamp for stamp in VERIFICATION_FAILURES.get(key, []) if now - stamp < 900]
    attempts.append(now)
    if len(attempts) >= 3:
        VERIFICATION_FAILURES.pop(key, None)
        VERIFICATION_LOCKED_UNTIL[key] = now + 900
        return 900
    VERIFICATION_FAILURES[key] = attempts
    return 0


class AppealLinkView(discord.ui.View):
    def __init__(self, invite_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Appeal", emoji="📝", style=discord.ButtonStyle.link, url=invite_url))


class VerificationView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Start verification", emoji="✅", style=discord.ButtonStyle.success, custom_id="testiny:verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild or (self.guild_id and interaction.guild_id != self.guild_id):
            await interaction.response.send_message("Open this verification panel in its Discord server.", ephemeral=True)
            return
        if not setting(guild, "verification_channel_id"):
            await interaction.response.send_message("Verification is not configured in this server yet.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or member.bot:
            await interaction.response.send_message("Only server members can complete verification.", ephemeral=True)
            return
        unverified_id = setting(guild, "unverified_role_id")
        verified_id = setting(guild, "verified_role_id")
        unverified = guild.get_role(unverified_id) if unverified_id else None
        verified = guild.get_role(verified_id) if verified_id else None
        if not unverified or not verified:
            await interaction.response.send_message("The verification roles are missing. Ask a server administrator to refresh the panel.", ephemeral=True)
            return
        if verified in member.roles:
            await interaction.response.send_message("Your verification is already complete. If you still cannot access a channel, ask a moderator to review your roles.", ephemeral=True)
            return
        if unverified not in member.roles:
            await interaction.response.send_message("You are not marked as waiting for verification. Ask a moderator to check the server setup.", ephemeral=True)
            return

        key = (guild.id, member.id)
        now = time.time()
        locked_for = verification_cooldown_remaining(guild.id, member.id)
        if locked_for:
            await interaction.response.send_message(f"Too many incorrect answers. Try again in about {locked_for // 60}m {locked_for % 60}s.", ephemeral=True)
            return
        next_start = VERIFICATION_NEXT_CHALLENGE.get(key, 0)
        if next_start > now:
            wait_seconds = max(1, int(next_start - now) + 1)
            await interaction.response.send_message(f"Please wait {wait_seconds} seconds before starting another challenge.", ephemeral=True)
            return
        VERIFICATION_NEXT_CHALLENGE[key] = now + 20

        words = ["NIGHT", "STAR", "LUNA", "FOX", "RUNE", "NOVA", "EMBER", "CROWN", "GLORY", "SILVER", "GOLDEN", "COMET", "FROST", "RIVER", "SABLE", "AURORA", "ORBIT", "THUNDER", "PHOENIX"]
        risk_score, _risk_reasons = get_verification_risk(guild.id, member.id)
        word_count = 7 if risk_score >= 5 else 5 if risk_score >= 3 else 4
        digits = 6 if risk_score >= 5 else 4 if risk_score >= 3 else 3
        number = secrets.randbelow(9 * (10 ** (digits - 1))) + 10 ** (digits - 1)
        phrase = " ".join(secrets.choice(words) for _ in range(word_count)) + f" {number}"
        await interaction.response.send_modal(VerificationModal(guild.id, member.id, phrase))
        await asyncio.sleep(0.2)
        try:
            challenge = make_challenge_image(phrase)
            await interaction.followup.send(
                embed=embed("🔐 Private verification challenge", f"Type the {word_count} words and {digits}-digit number from the image into the popup. This challenge expires in five minutes.", INFO),
                file=challenge,
                ephemeral=True,
            )
        except (discord.HTTPException, RuntimeError, OSError) as exc:
            print(f"Verification challenge could not be delivered in guild {guild.id}: {type(exc).__name__}")
            await interaction.followup.send(
                embed=embed("⚠️ Challenge unavailable", "Gargand could not deliver the private challenge image. Please try again shortly; verification will not be skipped.", WARNING),
                ephemeral=True,
            )


class VerificationModal(discord.ui.Modal, title="Verify your account"):
    answer = discord.ui.TextInput(label="Challenge phrase", placeholder="Type the words and number shown in the image", max_length=100)

    def __init__(self, guild_id: int, user_id: int, phrase: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.phrase = phrase

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This verification belongs to another user.", ephemeral=True)
            return
        if interaction.guild_id != self.guild_id or not interaction.guild:
            await interaction.response.send_message("Finish this challenge in the server where you started it.", ephemeral=True)
            return
        locked_for = verification_cooldown_remaining(self.guild_id, self.user_id)
        if locked_for:
            await interaction.response.send_message(f"Too many incorrect answers. Try again in about {locked_for // 60}m {locked_for % 60}s.", ephemeral=True)
            return
        if not secrets.compare_digest(str(self.answer.value).strip().casefold(), self.phrase.casefold()):
            locked_for = record_verification_failure(self.guild_id, self.user_id)
            if locked_for:
                message = "Three incorrect answers. Verification is paused for 15 minutes."
            else:
                message = "That answer was incorrect. Wait 20 seconds, then start a new challenge."
            await interaction.response.send_message("❌ " + message, ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(self.user_id)
        if not member:
            await interaction.response.send_message("Member unavailable. Ask a moderator to check your membership.", ephemeral=True)
            return
        unverified_id = setting(guild, "unverified_role_id")
        verified_id = setting(guild, "verified_role_id")
        unverified = guild.get_role(unverified_id) if unverified_id else None
        verified = guild.get_role(verified_id) if verified_id else None
        if not unverified or not verified:
            await interaction.response.send_message("The verification roles are missing. Ask a server administrator to refresh the panel.", ephemeral=True)
            return
        if verified in member.roles:
            await interaction.response.send_message("Your verification is already complete.", ephemeral=True)
            return
        if unverified not in member.roles:
            await interaction.response.send_message("You are no longer marked as waiting for verification. Ask a moderator to review your roles.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        verified_added = False
        try:
            await member.add_roles(verified, reason="Gargand verification completed")
            verified_added = True
            await member.remove_roles(unverified, reason="Gargand verification completed")
        except discord.HTTPException as exc:
            if verified_added:
                try:
                    await member.remove_roles(verified, reason="Verification role update failed")
                except discord.HTTPException:
                    pass
            print(f"Verification role update failed in guild {guild.id}: {type(exc).__name__}")
            await interaction.followup.send("Gargand could not finish updating your roles. Your access remains restricted; ask a moderator for help.", ephemeral=True)
            return

        autorole_id = setting(guild, "autorole_id")
        autorole = guild.get_role(autorole_id) if autorole_id else None
        if autorole and autorole != verified and autorole not in member.roles:
            try:
                await member.add_roles(autorole, reason="Gargand verification completed")
            except discord.HTTPException as exc:
                print(f"Post-verification auto role update failed in guild {guild.id}: {type(exc).__name__}")
                await log_action(guild, "⚠️ Auto role setup issue", f"{member.mention} passed verification, but Gargand could not add the configured auto role. Check role permissions and order.", WARNING)

        key = (guild.id, member.id)
        VERIFICATION_FAILURES.pop(key, None)
        conn = db_connect()
        conn.execute("DELETE FROM verification_risk WHERE guild_id=? AND user_id=?", (guild.id, member.id))
        conn.commit()
        conn.close()
        VERIFICATION_LOCKED_UNTIL.pop(key, None)
        VERIFICATION_NEXT_CHALLENGE.pop(key, None)
        await interaction.followup.send("✅ Verification successful. Welcome to the server!", ephemeral=True)
        await log_action(guild, "✅ Verification completed", f"{member.mention} passed the private challenge.", SUCCESS)



class TicketTypeSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = []
        templates = get_settings(guild_id).get("ticket_options", ["support", "purchase", "claim", "report", "other"])
        labels = {
            "support": ("Support", "General support", "🛠️"),
            "purchase": ("Purchase", "Orders / purchases", "💳"),
            "claim": ("Claim", "Claim something", "🏆"),
            "report": ("Report", "Report a member/problem", "🚨"),
            "other": ("Other", "Everything else", "✨"),
        }
        for key in templates[:25]:
            label, desc, emoji = labels.get(key, (key.title(), "Open a ticket", "🎫"))
            options.append(discord.SelectOption(label=label, value=key, description=desc, emoji=emoji))
        super().__init__(placeholder="🎫 Choose what you need help with...", options=options, custom_id="testiny:ticket:type")

    async def callback(self, interaction: discord.Interaction):
        cfg = get_settings(self.guild_id)
        questions = cfg.get("ticket_questions", {}).get(self.values[0], [])
        if not questions:
            await interaction.response.defer(ephemeral=True)
            channel = await create_ticket(interaction.guild, interaction.user, self.values[0])
            await interaction.followup.send(f"🎫 Ticket created: {channel.mention if channel else 'I could not create it.'}", ephemeral=True)
            return
        await interaction.response.send_modal(TicketQuestionsModal(self.guild_id, self.values[0], questions[:5]))


class TicketPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect(guild_id))


class TicketQuestionsModal(discord.ui.Modal):
    def __init__(self, guild_id: int, ticket_type: str, questions: list[str]):
        super().__init__(title=f"{ticket_type.title()} ticket")
        self.guild_id = guild_id
        self.ticket_type = ticket_type
        self.inputs = []
        for q in questions:
            inp = discord.ui.TextInput(label=q[:45], placeholder="Your answer...", style=discord.TextStyle.paragraph, required=True, max_length=1000)
            self.inputs.append((q, inp))
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction):
        channel = await create_ticket(interaction.guild, interaction.user, self.ticket_type, [(q, str(inp.value)) for q, inp in self.inputs])
        await interaction.response.send_message(f"🎫 Ticket created: {channel.mention if channel else 'failed to create the ticket.'}", ephemeral=True)


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm close", emoji="🔒", style=discord.ButtonStyle.danger)
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Only server staff can close this ticket.", ephemeral=True)
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not (channel.topic or "").startswith("gargand-ticket-owner:"):
            await interaction.response.send_message("This channel is not a Gargand ticket.", ephemeral=True)
            return
        try:
            overwrites = dict(channel.overwrites)
            for target, overwrite in list(overwrites.items()):
                if isinstance(target, discord.Member) and overwrite.view_channel is True:
                    overwrite.send_messages = False
                    overwrite.add_reactions = False
                    overwrites[target] = overwrite
            new_name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"[:100]
            await channel.edit(name=new_name, overwrites=overwrites, reason=f"Ticket closed by {interaction.user}")
            await interaction.response.edit_message(
                content=f"🔒 Ticket closed by {interaction.user.mention}. Staff can still review the history.",
                view=None,
            )
            await send_telemetry("ticket_closed", guild=channel.guild, user=interaction.user,
                                 metadata={"channel_id": channel.id, "ticket_type": (channel.topic or "")[:100]})
        except discord.Forbidden:
            await interaction.response.send_message("I need Manage Channels and permission to update ticket access.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("Discord could not close this ticket right now. Please try again.", ephemeral=True)

    @discord.ui.button(label="Cancel", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def cancel_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ticket remains open.", view=None)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="testiny:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not (channel.topic or "").startswith("gargand-ticket-owner:"):
            await interaction.response.send_message("This channel is not a Gargand ticket.", ephemeral=True)
            return
        try:
            await channel.edit(topic=f"{channel.topic};claimed-by:{interaction.user.id}"[:1024])
        except discord.HTTPException:
            await interaction.response.send_message("I could not save the claim. Check my Manage Channels permission.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed("🙋 Ticket claimed", f"{interaction.user.mention} has claimed this ticket.", INFO))

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="gargand:ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Only server staff can close this ticket.", ephemeral=True)
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not (channel.topic or "").startswith("gargand-ticket-owner:"):
            await interaction.response.send_message("This channel is not a Gargand ticket.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Close this ticket? The channel will be renamed and the ticket opener will lose send access.",
            view=TicketCloseConfirmView(),
            ephemeral=True,
        )


class CaseAppealReviewView(discord.ui.View):
    def __init__(self, appeal_id: int):
        super().__init__(timeout=None)
        self.appeal_id = int(appeal_id)
        approve = discord.ui.Button(
            label="Approve appeal", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"gargand:case-appeal:{self.appeal_id}:approve",
        )
        deny = discord.ui.Button(
            label="Deny appeal", emoji="⛔", style=discord.ButtonStyle.danger,
            custom_id=f"gargand:case-appeal:{self.appeal_id}:deny",
        )
        approve.callback = self.approve
        deny.callback = self.deny
        self.add_item(approve)
        self.add_item(deny)

    async def approve(self, interaction: discord.Interaction):
        await self.resolve(interaction, "approved")

    async def deny(self, interaction: discord.Interaction):
        await self.resolve(interaction, "denied")

    async def resolve(self, interaction: discord.Interaction, decision: str):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        conn = db_connect()
        appeal = conn.execute("SELECT * FROM case_appeals WHERE appeal_id=?", (self.appeal_id,)).fetchone()
        case = conn.execute("SELECT * FROM moderation_cases WHERE case_id=?", (appeal["case_id"],)).fetchone() if appeal else None
        conn.close()
        if not appeal or not case or appeal["guild_id"] != interaction.guild_id:
            await interaction.followup.send("This case appeal could not be found in this server.", ephemeral=True)
            return
        if appeal["status"] != "pending":
            await interaction.followup.send(f"This appeal is already {appeal['status']}.", ephemeral=True)
            return

        # Claim the review before changing warnings or timeouts so simultaneous
        # staff clicks cannot apply the same appeal twice.
        conn = db_connect()
        cursor = conn.execute(
            "UPDATE case_appeals SET status='reviewing' WHERE appeal_id=? AND status='pending'",
            (self.appeal_id,),
        )
        conn.commit()
        conn.close()
        if cursor.rowcount != 1:
            await interaction.followup.send("Another staff member is already reviewing this appeal.", ephemeral=True)
            return

        if decision == "approved":
            guild = interaction.guild
            try:
                if case["action"] == "warn":
                    conn = db_connect()
                    warning = conn.execute("SELECT count FROM warnings WHERE guild_id=? AND user_id=?", (guild.id, case["target_id"])).fetchone()
                    if warning and warning["count"] <= 1:
                        conn.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (guild.id, case["target_id"]))
                    elif warning:
                        conn.execute("UPDATE warnings SET count=count-1 WHERE guild_id=? AND user_id=?", (guild.id, case["target_id"]))
                    conn.commit()
                    conn.close()
                elif case["action"] == "timeout":
                    member = guild.get_member(int(case["target_id"])) if case["target_id"] else None
                    if member and member.is_timed_out() and case["expires_at"]:
                        current_expiry = member.timed_out_until
                        if current_expiry and abs(int(current_expiry.timestamp()) - int(case["expires_at"])) > 300:
                            conn = db_connect()
                            conn.execute("UPDATE case_appeals SET status='pending' WHERE appeal_id=? AND status='reviewing'", (self.appeal_id,))
                            conn.commit()
                            conn.close()
                            await interaction.followup.send("The member's timeout has since changed. Review the current timeout manually before approving.", ephemeral=True)
                            return
                        await member.timeout(None, reason=f"Case appeal #{self.appeal_id} approved by {interaction.user}")
            except (discord.Forbidden, discord.HTTPException):
                conn = db_connect()
                conn.execute("UPDATE case_appeals SET status='pending' WHERE appeal_id=? AND status='reviewing'", (self.appeal_id,))
                conn.commit()
                conn.close()
                await interaction.followup.send("Gargand could not apply the approved appeal. Check role order and try again.", ephemeral=True)
                return

        conn = db_connect()
        conn.execute(
            "UPDATE case_appeals SET status=?,reviewed_by=?,reviewed_at=?,decision_note=? WHERE appeal_id=? AND status='reviewing'",
            (decision, interaction.user.id, utc_ts(), f"Reviewed by {interaction.user}", self.appeal_id),
        )
        conn.commit()
        conn.close()
        target = interaction.guild.get_member(int(case["target_id"])) if case["target_id"] else None
        case_id = record_moderation_case(
            interaction.guild,
            f"appeal_{decision}",
            interaction.user,
            target,
            f"Appeal for case #{case['case_id']} {decision}",
            details=f"Original action: {case['action']}; appeal ID #{self.appeal_id}.",
            outcome=decision,
        )
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"Appeal {decision}. Review record: `#{case_id}`.", ephemeral=True)
        await log_action(
            interaction.guild,
            "🧑‍⚖️ Case appeal reviewed",
            f"Appeal `#{self.appeal_id}` for case `#{case['case_id']}` was **{decision}** by {interaction.user.mention}.\nReview record: `#{case_id}`.",
            SUCCESS if decision == "approved" else WARNING,
        )

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="testiny:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Closing ticket in a moment...")
        await asyncio.sleep(1.5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


class FeedbackModal(discord.ui.Modal, title="💬 Server feedback"):
    feedback = discord.ui.TextInput(label="Your opinion", placeholder="Tell us what you think...", style=discord.TextStyle.paragraph, max_length=1500)

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Choose your rating ⭐", view=RatingView(self.guild_id, str(self.feedback.value)), ephemeral=True)


class FeedbackPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="Leave feedback", emoji="💬", style=discord.ButtonStyle.primary, custom_id="testiny:feedback:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FeedbackModal(self.guild_id))


class RatingView(discord.ui.View):
    def __init__(self, guild_id: int, feedback: str):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.feedback = feedback
        for stars in range(1, 6):
            button = discord.ui.Button(label=f"{stars} ⭐", style=discord.ButtonStyle.secondary)
            button.callback = self.make_callback(stars)
            self.add_item(button)

    def make_callback(self, stars: int):
        async def callback(interaction: discord.Interaction):
            guild = bot.get_guild(self.guild_id)
            channel_id = setting(guild, "feedback_channel_id") if guild else None
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if isinstance(channel, discord.TextChannel):
                e = embed("💬 New feedback", self.feedback, INFO)
                e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
                e.add_field(name="Rating", value="⭐" * stars, inline=False)
                await channel.send(embed=e)
            await interaction.response.edit_message(content="✅ Thanks for your feedback!", view=None)
        return callback


class J4JView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int):
        super().__init__(timeout=3600)
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="Yes, I joined from J4J", emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel isn't for you.", ephemeral=True)
            return
        guild = bot.get_guild(self.guild_id)
        if guild:
            member = guild.get_member(self.user_id)
            if member:
                set_invite_category(guild.id, member.id, j4j=True)
                channel = await create_j4j_ticket(guild, member)
                await interaction.response.send_message(f"✅ J4J confirmed. Ticket created: {channel.mention if channel else 'failed'}", ephemeral=True)
                return
        await interaction.response.send_message("The server is unavailable.", ephemeral=True)

    @discord.ui.button(label="No, I joined normally", emoji="❌", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This panel isn't for you.", ephemeral=True)
            return
        await interaction.response.send_message("Thanks! Enjoy the server 💜", ephemeral=True)
        self.stop()


class AppealServerSelect(discord.ui.Select):
    def __init__(self, appeal_guild: discord.Guild):
        self.appeal_guild_id = appeal_guild.id
        options = []
        for g in bot.guilds:
            cfg = get_settings(g.id)
            try:
                configured_appeal_guild_id = int(cfg.get("appeal_server_id") or 0)
            except (TypeError, ValueError):
                configured_appeal_guild_id = 0
            if configured_appeal_guild_id == appeal_guild.id:
                options.append(discord.SelectOption(label=g.name[:100], value=str(g.id), description=f"Appeals for {g.name}"[:100], emoji="🏛️"))
        if not options:
            options.append(discord.SelectOption(label="No configured main servers", value="0", description="Run !setup in a main server first.", emoji="⚠️"))
        super().__init__(placeholder="🏛️ Choose the main server...", options=options[:25], custom_id="testiny:appeal:server")

    async def callback(self, interaction: discord.Interaction):
        gid = int(self.values[0])
        if gid == 0:
            await interaction.response.send_message("No main server is configured for this appeal server.", ephemeral=True)
            return
        main_guild = bot.get_guild(gid)
        if not main_guild:
            await interaction.response.send_message("Main server is unavailable.", ephemeral=True)
            return
        # Store the selected main server in the appeal server so the panel
        # can be used by normal banned users (not only administrators).
        appeal_guild = bot.get_guild(self.appeal_guild_id)
        if not appeal_guild:
            await interaction.response.send_message("Appeal server is unavailable.", ephemeral=True)
            return
        set_setting(appeal_guild, "active_appeal_main_guild_id", gid)
        e = embed("📝 Ban appeals", f"Appeals for **{main_guild.name}** are ready.\n\nBanned members can press the button below to open a private appeal ticket.", INFO)
        await interaction.response.send_message(e, view=AppealOpenView(gid, self.appeal_guild_id))


class AppealServerView(discord.ui.View):
    def __init__(self, appeal_guild: discord.Guild):
        super().__init__(timeout=300)
        self.add_item(AppealServerSelect(appeal_guild))


class AppealOpenView(discord.ui.View):
    def __init__(self, main_guild_id: int, appeal_guild_id: int):
        super().__init__(timeout=None)
        self.main_guild_id = main_guild_id
        self.appeal_guild_id = appeal_guild_id

    @discord.ui.button(label="Open appeal", emoji="📝", style=discord.ButtonStyle.primary, custom_id="testiny:appeal:open")
    async def open_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppealTicketReasonModal(self.main_guild_id, self.appeal_guild_id))


class AppealTicketReasonModal(discord.ui.Modal, title="📝 Appeal"):
    reason = discord.ui.TextInput(label="Why should your ban be appealed?", style=discord.TextStyle.paragraph, max_length=1500, required=True)

    def __init__(self, main_guild_id: int, appeal_guild_id: int):
        super().__init__(timeout=300)
        self.main_guild_id = main_guild_id
        self.appeal_guild_id = appeal_guild_id

    async def on_submit(self, interaction: discord.Interaction):
        appeal_guild = bot.get_guild(self.appeal_guild_id)
        main_guild = bot.get_guild(self.main_guild_id)
        if not appeal_guild or not main_guild:
            await interaction.response.send_message("Server unavailable.", ephemeral=True)
            return
        try:
            await main_guild.fetch_ban(interaction.user)
        except discord.NotFound:
            await interaction.response.send_message("You do not currently have a ban on that main server.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.response.send_message("The main server did not allow the bot to verify your ban status.", ephemeral=True)
            return
        cfg = get_settings(main_guild.id)
        category = discord.utils.get(appeal_guild.categories, name="Appeal Tickets")
        if not category:
            try:
                category = await appeal_guild.create_category("Appeal Tickets", reason="Gargand appeal system")
            except discord.HTTPException:
                category = None
        overwrites = {appeal_guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),}
        appeal_staff_id = get_settings(appeal_guild.id).get("staff_role_id")
        try:
            appeal_staff_id = int(appeal_staff_id) if appeal_staff_id else 0
        except (TypeError, ValueError):
            appeal_staff_id = 0
        staff_role = appeal_guild.get_role(appeal_staff_id) if appeal_staff_id else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
        channel = await appeal_guild.create_text_channel(f"appeal-{interaction.user.name[:16]}", category=category if isinstance(category, discord.CategoryChannel) else None, overwrites=overwrites, reason="Gargand appeal ticket")
        e = embed("📝 Ban appeal", f"User: {interaction.user.mention}\n**Main server:** {main_guild.name}\n\n**Reason:**\n{self.reason.value}", WARNING)
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        await channel.send(embed=e, view=AppealTicketView())
        await interaction.response.send_message(f"✅ Appeal ticket created: {channel.mention}", ephemeral=True)


class AppealTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close appeal", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="testiny:appeal:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not (is_admin(interaction.user) or interaction.user.guild_permissions.manage_channels):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Closing appeal...", ephemeral=True)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason="Appeal closed")
        except discord.HTTPException:
            pass


# -----------------------------
# Setup dashboard UI
# -----------------------------

def setup_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    cfg = get_settings(guild.id)
    checks = ["staff_role_id", "logs_channel_id", "welcome_channel_id", "verification_channel_id", "ticket_panel_channel_id", "autorole_id", "jail_role_id"]
    ready = sum(bool(cfg.get(key)) for key in checks)
    bar = "🟨" * ready + "⬛" * (len(checks) - ready)
    e = embed(
        "👑 GARGAND • Server Command Center",
        f"**{guild.name}** · setup readiness **{ready}/{len(checks)}**\n{bar}\nPick a control below; changes save automatically. Use **Templates** to create a complete starter layout.",
        EMBED_COLOR,
    )
    def mention(key, prefix):
        return f"{prefix}{cfg[key]}>" if cfg.get(key) else "—"
    e.add_field(name="🛡️ Staff & safety", value=f"Staff {mention('staff_role_id', '<@&')} · Logs {mention('logs_channel_id', '<#')}\nAnti-raid {'🟢' if cfg.get('anti_raid') else '⚪'} · Anti-nuke {'🟢' if cfg.get('anti_nuke') else '⚪'} · Anti-link {'🟢' if cfg.get('anti_link') else '⚪'}", inline=False)
    e.add_field(name="👋 Member onboarding", value=f"Welcome {mention('welcome_channel_id', '<#')} · Verify {mention('verification_channel_id', '<#')}\nAuto role {mention('autorole_id', '<@&')} · Leave {mention('leave_channel_id', '<#')}", inline=True)
    e.add_field(name="🎫 Support & community", value=f"Tickets {mention('ticket_panel_channel_id', '<#')} · Feedback {mention('feedback_channel_id', '<#')}\nVouches {mention('vouch_channel_id', '<#')} · Applications {mention('staff_application_channel_id', '<#')}", inline=True)
    e.add_field(name="🔒 Jail & appeals", value=f"Jail {'🟢 enabled' if cfg.get('jail_enabled') else '⚪ off'} · Role {mention('jail_role_id', '<@&')}\nChat {mention('jail_chat_channel_id', '<#')} · Appeals {mention('jail_appeal_channel_id', '<#')}", inline=True)
    e.add_field(name="✨ More channels", value=f"Commands {mention('command_channel_id', '<#')} · Proof {mention('proof_channel_id', '<#')}\nGamble {mention('gamble_channel_id', '<#')} · Booster role {mention('booster_role_id', '<@&')}", inline=True)
    e.set_footer(text="GARGAND • setup changes save instantly ✨")
    return e


async def refresh_setup_dashboard(guild: discord.Guild) -> bool:
    cfg = get_settings(guild.id)
    channel_id = cfg.get("setup_dashboard_channel_id")
    message_id = cfg.get("setup_dashboard_message_id")
    if not channel_id or not message_id:
        return False

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False

    for attempt in range(4):
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=setup_dashboard_embed(guild), view=SetupDashboardView(guild.id))
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) != 429 or attempt == 3:
                return False
            await asyncio.sleep(await retry_after_from_http(exc, 1.5) + 0.25 * attempt)
    return False


class SetupChannelSelect(discord.ui.Select):
    def __init__(self, key: str, title: str, guild_id: int, channels, page: int):
        self.key = key
        self.title = title
        self.guild_id = guild_id
        self.channels = channels
        self.page = page
        page_count = max(1, (len(channels) + 24) // 25)
        page_channels = channels[page * 25:(page + 1) * 25]
        options = [
            discord.SelectOption(
                label=ch.name[:100],
                value=str(ch.id),
                description=(f"#{ch.name}" if ch.name else "Text channel")[:100],
                emoji="💬",
            )
            for ch in page_channels
        ]
        if not options:
            options = [discord.SelectOption(label="No text channels found", value="0")]
        super().__init__(
            placeholder=f"Select {title} • page {page + 1}/{page_count}"[:150],
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return

        value = int(self.values[0])
        guild = interaction.guild
        if not guild or not value:
            await interaction.response.send_message("❌ No valid channel selected.", ephemeral=True)
            return

        channel = guild.get_channel(value)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ That channel is no longer available as a text channel. Open `!setup` and choose again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        set_setting(guild, self.key, value)
        extra = ""
        try:
            if self.key == "feedback_channel_id":
                await post_feedback_panel(guild)
                extra = " The feedback panel was posted there."
            elif self.key == "staff_application_channel_id":
                await post_staff_application_panel(guild)
                extra = " The application panel was posted there."
            elif self.key == "verification_channel_id":
                await post_verification_panel(guild)
                extra = " The verification panel was posted there."
        except (discord.Forbidden, discord.HTTPException) as exc:
            extra = f" ⚠️ The setting was saved, but Discord rejected the panel action: `{type(exc).__name__}`."

        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Saved, but I couldn't refresh the setup dashboard."
        await send_followup_resilient(interaction, f"✅ Saved **{self.key}** → <#{value}>.{extra}\n{status}")


class SetupChannelPickerView(discord.ui.View):
    """Paginated picker makes all server text channels reachable despite Discord's 25-option limit."""

    def __init__(self, key: str, title: str, guild_id: int):
        super().__init__(timeout=180)
        self.key = key
        self.title = title
        self.guild_id = guild_id
        guild = bot.get_guild(guild_id)
        self.channels = sorted(
            [ch for ch in guild.text_channels] if guild else [],
            key=lambda ch: (
                ch.category.position if ch.category else -1,
                ch.position,
                ch.name.casefold(),
            ),
        )
        self.page = 0
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        self.add_item(SetupChannelSelect(self.key, self.title, self.guild_id, self.channels, self.page))
        page_count = max(1, (len(self.channels) + 24) // 25)
        if page_count > 1:
            previous = discord.ui.Button(
                label="◀ Previous", style=discord.ButtonStyle.secondary,
                disabled=self.page == 0, row=1,
            )
            next_page = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary,
                disabled=self.page >= page_count - 1, row=1,
            )
            previous.callback = self._previous
            next_page.callback = self._next
            self.add_item(previous)
            self.add_item(next_page)

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        self.page = max(0, min(self.page + delta, max(0, (len(self.channels) - 1) // 25)))
        self._rebuild()
        first = self.page * 25 + 1 if self.channels else 0
        last = min((self.page + 1) * 25, len(self.channels))
        await interaction.response.edit_message(
            content=f"Choose **{self.title}**. Showing {first}–{last} of {len(self.channels)} text channels:",
            view=self,
        )

    async def _previous(self, interaction: discord.Interaction):
        await self._change_page(interaction, -1)

    async def _next(self, interaction: discord.Interaction):
        await self._change_page(interaction, 1)


class SetupRoleSelect(discord.ui.Select):
    def __init__(self, key: str, guild_id: int, label: str):
        self.key = key
        self.guild_id = guild_id
        guild = bot.get_guild(guild_id)
        opts = []
        if guild:
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
                if role.is_default():
                    continue
                opts.append(discord.SelectOption(label=role.name[:100], value=str(role.id), emoji="🛡️"))
        super().__init__(placeholder=label, options=opts[:25] or [discord.SelectOption(label="No roles", value="0")])

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        value = int(self.values[0])
        guild = interaction.guild
        if not guild or not value:
            await interaction.followup.send("❌ No valid selection.", ephemeral=True)
            return

        role = guild.get_role(value)
        if not role:
            await interaction.followup.send("❌ That role no longer exists. Open `!setup` again and choose another role.", ephemeral=True)
            return

        set_setting(guild, self.key, value)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Saved, but I couldn't refresh the setup dashboard."
        await send_followup_resilient(interaction, f"✅ Saved **{self.key}** → {role.mention}.\n{status}")


class SetupAppealModal(discord.ui.Modal, title="📝 Appeal server"):
    server_id = discord.ui.TextInput(label="Appeal server ID", placeholder="Paste the appeal server's ID", max_length=30)
    invite_url = discord.ui.TextInput(label="Appeal invite URL", placeholder="https://discord.gg/...", max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sid = int(self.server_id.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid server ID.", ephemeral=True)
            return
        url = self.invite_url.value.strip()
        if not url.startswith("https://discord.gg/") and not url.startswith("https://discord.com/invite/"):
            await interaction.followup.send("❌ Use a Discord invite link.", ephemeral=True)
            return
        set_setting(interaction.guild, "appeal_server_id", sid)
        set_setting(interaction.guild, "appeal_invite_url", url)
        conn = db_connect()
        conn.execute("INSERT OR REPLACE INTO appeal_links(main_guild_id,appeal_guild_id,invite_url) VALUES(?,?,?)", (interaction.guild.id, sid, url))
        conn.commit()
        conn.close()
        configured = bot.get_guild(sid)
        invite = make_bot_invite()
        if configured:
            msg = f"✅ Appeal server connected: **{configured.name}**\n\nUse `!apeal server` or `!appeal server` inside that server to choose this main server."
        else:
            msg = f"✅ Saved the appeal server. I am **not in that server yet**.\n\nAdd me there first: {invite}\n\nThen run `!apeal server` / `!appeal server` in the appeal server."
        refreshed = await refresh_setup_dashboard(interaction.guild)
        if refreshed:
            msg += "\n\n✨ The setup dashboard was refreshed."
        await send_followup_resilient(interaction, msg)


class SetupTicketModal(discord.ui.Modal, title="🎫 Ticket settings"):
    panel_channel_id = discord.ui.TextInput(label="Panel channel ID", placeholder="Paste text channel ID", max_length=30)
    category_id = discord.ui.TextInput(label="Ticket category ID (optional)", placeholder="Leave blank to create tickets at server root", required=False, max_length=30)
    options = discord.ui.TextInput(label="Options", placeholder="support, purchase, claim, report, other", default="support, purchase, claim, report, other", max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            panel_id = int(self.panel_channel_id.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Invalid panel channel ID.", ephemeral=True)
            return
        category_id = None
        if self.category_id.value.strip():
            try:
                category_id = int(self.category_id.value.strip())
            except ValueError:
                await interaction.followup.send("❌ Invalid category ID.", ephemeral=True)
                return
        set_setting(interaction.guild, "ticket_panel_channel_id", panel_id)
        if category_id:
            set_setting(interaction.guild, "ticket_category_id", category_id)
        opts = [x.strip().lower() for x in self.options.value.split(",") if x.strip()][:10]
        set_setting(interaction.guild, "ticket_options", opts)
        ch = interaction.guild.get_channel(panel_id)
        if isinstance(ch, discord.TextChannel):
            e = embed("🎫 Open a ticket", "Choose the category that matches your request.\n\nA beautiful private ticket will be created and the configured staff team will be pinged.", EMBED_COLOR)
            await ch.send(embed=e, view=TicketPanelView(interaction.guild.id))
        refreshed = await refresh_setup_dashboard(interaction.guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Ticket settings saved, but dashboard refresh failed."
        await send_followup_resilient(interaction, f"✅ Ticket system configured and panel posted.\n{status}")


class SetupSimpleModal(discord.ui.Modal):
    def __init__(self, key: str, label: str, placeholder: str, guild_id: int):
        super().__init__(title=label[:45])
        self.key = key
        self.guild_id = guild_id
        self.value = discord.ui.TextInput(label=label[:45], placeholder=placeholder[:100], required=True, max_length=300)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        val = self.value.value.strip()
        set_setting(interaction.guild, self.key, val)
        refreshed = await refresh_setup_dashboard(interaction.guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Saved, but dashboard refresh failed."
        await send_followup_resilient(interaction, f"✅ Saved **{self.key}**.\n{status}")


class SetupDashboardView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=900)
        self.guild_id = guild_id

    async def ensure_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Staff role", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = discord.ui.View(timeout=180)
        v.add_item(SetupRoleSelect("staff_role_id", self.guild_id, "Select staff role..."))
        await interaction.response.send_message("Choose the staff role:", view=v, ephemeral=True)

    @discord.ui.button(label="Logs", emoji="📜", style=discord.ButtonStyle.secondary, row=0)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("logs_channel_id", "logs channel", self.guild_id)
        await interaction.response.send_message("Choose the logs channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Appeals", emoji="📝", style=discord.ButtonStyle.secondary, row=0)
    async def appeals(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.send_modal(SetupAppealModal())

    @discord.ui.button(label="Tickets", emoji="🎫", style=discord.ButtonStyle.primary, row=0)
    async def tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.send_modal(SetupTicketModal())

    @discord.ui.button(label="Welcome", emoji="👋", style=discord.ButtonStyle.secondary, row=1)
    async def welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("welcome_channel_id", "welcome channel", self.guild_id)
        await interaction.response.send_message("Choose the welcome channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Leave", emoji="🚪", style=discord.ButtonStyle.secondary, row=1)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("leave_channel_id", "leave channel", self.guild_id)
        await interaction.response.send_message("Choose the leave channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Auto role", emoji="🎭", style=discord.ButtonStyle.secondary, row=1)
    async def autorole(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = discord.ui.View(timeout=180); v.add_item(SetupRoleSelect("autorole_id", self.guild_id, "Select automatic role..."))
        await interaction.response.send_message("Choose the role new members should receive:", view=v, ephemeral=True)

    @discord.ui.button(label="Anti-raid", emoji="🧱", style=discord.ButtonStyle.secondary, row=1)
    async def antiraid(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        enabled = not bool(setting(guild, "anti_raid", False))
        set_setting(guild, "anti_raid", enabled)
        changed = rate_limited = total = 0
        if enabled:
            changed, rate_limited, total = await apply_external_app_lock(guild)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Setting saved, but dashboard refresh failed."
        if enabled:
            action = f"🧱 Anti-raid **enabled**. Protected **{changed}/{total}** channels."
            if rate_limited:
                action += f"\n⏳ Katabump rate-limited {rate_limited} request(s); retries were applied."
        else:
            action = "🧱 Anti-raid **disabled**."
        await send_followup_resilient(interaction, f"{action}\n{status}")

    @discord.ui.button(label="Anti-nuke", emoji="🛡️", style=discord.ButtonStyle.secondary, row=1)
    async def antinuke(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        enabled = not bool(setting(guild, "anti_nuke", False))
        set_setting(guild, "anti_nuke", enabled)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Setting saved, but dashboard refresh failed."
        await send_followup_resilient(interaction, f"🛡️ Anti-nuke **{'enabled' if enabled else 'disabled'}**.\n{status}")

    @discord.ui.button(label="Anti-link", emoji="🔗", style=discord.ButtonStyle.secondary, row=2)
    async def antilink(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        enabled = not bool(setting(guild, "anti_link", False))
        set_setting(guild, "anti_link", enabled)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Setting saved, but dashboard refresh failed."
        await send_followup_resilient(interaction, f"🔗 Anti-link **{'enabled' if enabled else 'disabled'}**. GIF links remain allowed.\n{status}")

    @discord.ui.button(label="Verification", emoji="✅", style=discord.ButtonStyle.secondary, row=2)
    async def verification(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("verification_channel_id", "verification channel", self.guild_id)
        await interaction.response.send_message("Select the verification channel:", view=v, ephemeral=True)
        # Roles are auto-created when a member joins / when the panel is posted.

    @discord.ui.button(label="Commands", emoji="⌨️", style=discord.ButtonStyle.secondary, row=2)
    async def commands_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("command_channel_id", "command channel", self.guild_id)
        await interaction.response.send_message("Choose the only channel where prefix commands are permitted:", view=v, ephemeral=True)

    @discord.ui.button(label="Vouch", emoji="⭐", style=discord.ButtonStyle.secondary, row=2)
    async def vouch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("vouch_channel_id", "vouch channel", self.guild_id)
        await interaction.response.send_message("Choose the vouch channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Feedback", emoji="💬", style=discord.ButtonStyle.secondary, row=3)
    async def feedback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("feedback_channel_id", "feedback channel", self.guild_id)
        await interaction.response.send_message("Choose the feedback channel:", view=v, ephemeral=True)

    @discord.ui.button(label="J4J", emoji="🤝", style=discord.ButtonStyle.secondary, row=3)
    async def j4j(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        enabled = not bool(setting(guild, "j4j", False))
        set_setting(guild, "j4j", enabled)
        set_setting(guild, "j4j_dm", enabled)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Setting saved, but dashboard refresh failed."
        await interaction.followup.send(f"🤝 J4J system **{'enabled' if enabled else 'disabled'}**.\n{status}", ephemeral=True)

    @discord.ui.button(label="Proof", emoji="📸", style=discord.ButtonStyle.secondary, row=3)
    async def proof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("proof_channel_id", "proof channel", self.guild_id)
        await interaction.response.send_message("Choose the proof channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Gamble", emoji="🎰", style=discord.ButtonStyle.secondary, row=3)
    async def gamble(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("gamble_channel_id", "gambling channel", self.guild_id)
        await interaction.response.send_message("Choose the gambling channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Server templates", emoji="👑", style=discord.ButtonStyle.primary, row=3)
    async def server_templates(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.send_message(
            embed=embed(
                "👑 GARGAND • Server Templates",
                "Choose a **Community**, **Gaming**, **Creator**, or **Support** layout. GARGAND adds starter channels, roles, a rules post, and a welcome post without deleting existing content.\n\nMake sure the bot has **Manage Channels** and **Manage Roles**. Review the permissions and starter rules before opening the server.",
                INFO,
            ),
            view=ServerTemplateView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Booster role", emoji="🚀", style=discord.ButtonStyle.secondary, row=4)
    async def booster(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = discord.ui.View(timeout=180); v.add_item(SetupRoleSelect("booster_role_id", self.guild_id, "Select booster role..."))
        await interaction.response.send_message("Choose the booster role:", view=v, ephemeral=True)

    @discord.ui.button(label="Staff applications", emoji="📋", style=discord.ButtonStyle.secondary, row=4)
    async def applications(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        v = SetupChannelPickerView("staff_application_channel_id", "staff application panel channel", self.guild_id)
        await interaction.response.send_message("Choose the application panel channel:", view=v, ephemeral=True)

    @discord.ui.button(label="Save verification panel", emoji="🔐", style=discord.ButtonStyle.success, row=4)
    async def verification_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        await post_verification_panel(guild)
        refreshed = await refresh_setup_dashboard(guild)
        status = "✅ Setup dashboard updated." if refreshed else "⚠️ Verification configured, but dashboard refresh failed."
        await interaction.followup.send(f"✅ Verification roles and panel configured.\n{status}", ephemeral=True)

    @discord.ui.button(label="Jail setup", emoji="🔒", style=discord.ButtonStyle.danger, row=4)
    async def jail_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.ensure_admin(interaction): return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        enabled = not bool(setting(guild, "jail_enabled", False))
        if enabled:
            try:
                role, category, chat, appeals = await configure_jail_system(guild)
                message = (
                    "🔒 **Jail system enabled!**\n\n"
                    f"🛡️ **Role:** {role.mention}\n"
                    f"🏠 **Category:** {category.name}\n"
                    f"💬 **Jail chat:** {chat.mention}\n"
                    f"📝 **Jail appeals:** {appeals.mention}\n\n"
                    "Everything was created/configured automatically. ✨"
                )
            except discord.Forbidden:
                set_setting(guild, "jail_enabled", False)
                message = "❌ Jail setup needs **Manage Roles** and **Manage Channels**. Check the bot permissions and try again."
            except discord.HTTPException as exc:
                set_setting(guild, "jail_enabled", False)
                message = f"❌ Jail setup failed: `{exc}`"
        else:
            set_setting(guild, "jail_enabled", False)
            message = "🔒 **Jail system disabled.** Existing jail channels and the role were kept."
        refreshed = await refresh_setup_dashboard(guild)
        message += "\n" + ("✅ Setup dashboard updated." if refreshed else "⚠️ Setting saved, but dashboard refresh failed.")
        await send_followup_resilient(interaction, message)


# -----------------------------
# Staff application panel
# -----------------------------

class StaffApplicationModal(discord.ui.Modal, title="📋 Staff application"):
    age = discord.ui.TextInput(label="Age", max_length=3)
    experience = discord.ui.TextInput(label="Moderation experience", style=discord.TextStyle.paragraph, max_length=600)
    why = discord.ui.TextInput(label="Why should we choose you?", style=discord.TextStyle.paragraph, max_length=800)
    availability = discord.ui.TextInput(label="Availability / timezone", max_length=200)
    game_user = discord.ui.TextInput(label="Main game / username", max_length=200)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel_id = setting(guild, "logs_channel_id") or setting(guild, "staff_application_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        e = embed("📋 New staff application", f"Submitted by {interaction.user.mention}", INFO)
        for name, value in (("Age", self.age.value), ("Experience", self.experience.value), ("Why", self.why.value), ("Availability", self.availability.value), ("Game", self.game_user.value)):
            e.add_field(name=name, value=value[:1024], inline=False)
        if isinstance(channel, discord.TextChannel):
            await channel.send(embed=e)
        await interaction.response.send_message("✅ Your application has been submitted. Thank you!", ephemeral=True)


class StaffApplicationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply for staff", emoji="📋", style=discord.ButtonStyle.primary, custom_id="testiny:staff:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffApplicationModal())


# -----------------------------
# Gamble UI helpers
# -----------------------------

async def add_coins(guild_id: int, user_id: int, delta: int):
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO gambling(guild_id,user_id,coins,last_daily) VALUES(?,?,0,0)", (guild_id, user_id))
    conn.execute("UPDATE gambling SET coins=MAX(0,coins+?) WHERE guild_id=? AND user_id=?", (delta, guild_id, user_id))
    conn.commit()
    row = conn.execute("SELECT coins FROM gambling WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    conn.close()
    return row["coins"] if row else 0


def get_coins(guild_id: int, user_id: int) -> int:
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO gambling(guild_id,user_id,coins,last_daily) VALUES(?,?,0,0)", (guild_id, user_id))
    conn.commit()
    row = conn.execute("SELECT coins FROM gambling WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    conn.close()
    return row["coins"] if row else 0


# -----------------------------
# Invite tracking helpers
# -----------------------------

invite_cache: dict[int, dict[str, int]] = {}
anti_nuke_events: dict[tuple[int, int, str], deque[float]] = defaultdict(deque)
anti_nuke_escalated: set[tuple[int, int, str]] = set()
raid_mentions: dict[tuple[int, int], deque[int]] = defaultdict(deque)
spam_message_windows: dict[tuple[int, int], deque[float]] = defaultdict(deque)
flood_message_windows: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(deque)
raid_join_windows: dict[int, deque[float]] = defaultdict(deque)
raid_watch_last_alert: dict[int, float] = {}
raid_lockdown_starting: set[int] = set()


def record_join_speed(guild_id: int) -> int:
    now = time.monotonic()
    joins = raid_join_windows[guild_id]
    joins.append(now)
    while joins and now - joins[0] > 30:
        joins.popleft()
    return len(joins)


def assess_join_risk(member: discord.Member, joins_in_window: int) -> tuple[int, list[str]]:
    age_hours = max(0, (datetime.now(timezone.utc) - member.created_at).total_seconds() / 3600)
    score = 0
    reasons = []
    if age_hours < 24:
        score += 3
        reasons.append("account created within 24 hours")
    elif age_hours < 24 * 7:
        score += 2
        reasons.append("account created within 7 days")
    elif age_hours < 24 * 30:
        score += 1
        reasons.append("account created within 30 days")
    if joins_in_window >= 12:
        score += 3
        reasons.append(f"{joins_in_window} joins in 30 seconds")
    elif joins_in_window >= 6:
        score += 2
        reasons.append(f"{joins_in_window} joins in 30 seconds")
    return score, reasons


def save_verification_risk(guild_id: int, user_id: int, score: int, reasons: list[str]):
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO verification_risk(guild_id,user_id,risk_score,reasons,assessed_at) VALUES(?,?,?,?,?)",
        (guild_id, user_id, int(score), json.dumps(reasons[:5]), utc_ts()),
    )
    conn.commit()
    conn.close()


def get_verification_risk(guild_id: int, user_id: int) -> tuple[int, list[str]]:
    conn = db_connect()
    row = conn.execute(
        "SELECT risk_score,reasons,assessed_at FROM verification_risk WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()
    conn.close()
    if not row or utc_ts() - int(row["assessed_at"] or 0) > 72 * 3600:
        return 0, []
    try:
        reasons = json.loads(row["reasons"] or "[]")
    except (TypeError, ValueError):
        reasons = []
    return max(0, int(row["risk_score"] or 0)), reasons if isinstance(reasons, list) else []


async def apply_automatic_raid_lockdown(guild: discord.Guild, member: discord.Member, joins: int):
    try:
        await start_raid_lockdown(
            guild,
            int(setting(guild, "raid_lockdown_minutes", 10) or 10),
            actor=bot.user,
            reason=f"automatic join-surge response ({joins} joins / 30s; latest member {member.id})",
        )
    finally:
        raid_lockdown_starting.discard(guild.id)


def set_invite_category(guild_id: int, user_id: int, *, j4j: bool = False):
    conn = db_connect()
    row = conn.execute("SELECT * FROM invite_members WHERE guild_id=? AND invited_user_id=?", (guild_id, user_id)).fetchone()
    if row:
        category = "j4j" if j4j else row["category"]
        conn.execute("UPDATE invite_members SET category=?, j4j=? WHERE guild_id=? AND invited_user_id=?", (category, 1 if j4j else row["j4j"], guild_id, user_id))
    conn.commit(); conn.close()


def invite_category(member: discord.Member, inviter_id: Optional[int], j4j=False, rejoin=False) -> str:
    if j4j:
        return "j4j"
    if rejoin:
        return "rejoin"
    age_days = (datetime.now(timezone.utc) - member.created_at).days
    if age_days < 90:
        return "fake"
    return "clean"


def invite_counts(guild_id: int, user_id: int):
    conn = db_connect()
    rows = conn.execute("SELECT category, COUNT(*) c FROM invite_members WHERE guild_id=? AND inviter_id=? GROUP BY category", (guild_id, user_id)).fetchall()
    conn.close()
    counts = {"clean": 0, "fake": 0, "left": 0, "rejoin": 0, "j4j": 0}
    for r in rows:
        counts[r["category"]] = r["c"]
    return counts


def invited_people(guild_id: int, user_id: int):
    conn = db_connect()
    rows = conn.execute("SELECT invited_user_id FROM invite_members WHERE guild_id=? AND inviter_id=? AND category='clean' ORDER BY joined_at DESC", (guild_id, user_id)).fetchall()
    conn.close()
    return [r["invited_user_id"] for r in rows]


def inviter_of(guild_id: int, invited_user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT inviter_id, category FROM invite_members WHERE guild_id=? AND invited_user_id=?", (guild_id, invited_user_id)).fetchone()
    conn.close()
    return row


async def cache_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses or 0 for i in invites}
        conn = db_connect()
        for i in invites:
            inviter = i.inviter.id if i.inviter else None
            conn.execute("INSERT INTO invites(guild_id,invite_code,inviter_id,uses) VALUES(?,?,?,?) ON CONFLICT(guild_id,invite_code) DO UPDATE SET inviter_id=excluded.inviter_id,uses=excluded.uses", (guild.id, i.code, inviter, i.uses or 0))
        conn.commit(); conn.close()
    except (discord.Forbidden, discord.HTTPException):
        invite_cache.setdefault(guild.id, {})


async def find_inviter(guild: discord.Guild) -> Optional[discord.User]:
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None
    before = invite_cache.get(guild.id, {})
    selected = None
    for inv in invites:
        old = before.get(inv.code, 0)
        if (inv.uses or 0) > old:
            selected = inv
            break
    invite_cache[guild.id] = {i.code: i.uses or 0 for i in invites}
    return selected.inviter if selected and selected.inviter else None


# -----------------------------
# Jail system
# -----------------------------

def get_active_jail(guild_id: int, user_id: int):
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM jails WHERE guild_id=? AND user_id=? AND active=1 ORDER BY jail_id DESC LIMIT 1",
        (guild_id, user_id),
    ).fetchone()
    conn.close()
    return row


def get_jail_case(jail_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM jails WHERE jail_id=?", (jail_id,)).fetchone()
    conn.close()
    return row


def set_jail_appeal_created(jail_id: int, channel_id: int):
    conn = db_connect()
    conn.execute(
        "UPDATE jails SET appeal_created=1, appeal_channel_id=? WHERE jail_id=?",
        (channel_id, jail_id),
    )
    conn.commit()
    conn.close()


def close_active_jail(guild_id: int, user_id: int):
    conn = db_connect()
    conn.execute(
        "UPDATE jails SET active=0 WHERE guild_id=? AND user_id=? AND active=1",
        (guild_id, user_id),
    )
    conn.commit()
    conn.close()


async def get_or_create_jail_role(guild: discord.Guild) -> discord.Role:
    role_id = setting(guild, "jail_role_id")
    role = guild.get_role(role_id) if role_id else None
    if role:
        return role
    role = discord.utils.get(guild.roles, name="🔒 Jailed") or discord.utils.get(guild.roles, name="Jailed")
    if not role:
        role = await guild.create_role(
            name="🔒 Jailed",
            color=DANGER,
            reason="Gargand jail system setup",
        )
    set_setting(guild, "jail_role_id", role.id)
    return role


async def apply_jail_role_permissions(guild: discord.Guild, jail_role: discord.Role):
    """Deny jailed users from normal channels and allow only the jail area.

    Permission edits are deliberately paced/retried because Katabump/KataProxy
    can rate-limit servers that have many channels.
    """
    category_id = setting(guild, "jail_category_id")
    chat_id = setting(guild, "jail_chat_channel_id")
    appeals_id = setting(guild, "jail_appeal_channel_id")
    targets = []
    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            allowed = channel.id == category_id
        elif isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
            allowed = channel.id in {chat_id, appeals_id}
        else:
            continue
        targets.append((channel, allowed))

    for index, (channel, allowed) in enumerate(targets):
        for attempt in range(4):
            try:
                ow = channel.overwrites_for(jail_role)
                ow.view_channel = allowed
                if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                    ow.send_messages = True if (allowed and channel.id == chat_id) else False
                    ow.read_message_history = allowed
                    ow.send_messages_in_threads = True if (allowed and channel.id == chat_id) else False
                await channel.set_permissions(
                    jail_role,
                    overwrite=ow,
                    reason="Gargand jail access control",
                )
                break
            except discord.Forbidden:
                break
            except discord.HTTPException as exc:
                if getattr(exc, "status", None) != 429 or attempt == 3:
                    break
                await asyncio.sleep(await retry_after_from_http(exc, 1.5) + (0.25 * attempt))
            except AttributeError:
                break
        if index < len(targets) - 1:
            await asyncio.sleep(0.35)


async def configure_jail_system(guild: discord.Guild):
    role = await get_or_create_jail_role(guild)

    category = None
    category_id = setting(guild, "jail_category_id")
    if category_id:
        old = guild.get_channel(category_id)
        if isinstance(old, discord.CategoryChannel):
            category = old
    if category is None:
        category = discord.utils.find(
            lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() in {"jail", "🔒 jail"},
            guild.categories,
        )
    if category is None:
        category = await guild.create_category("🔒 Jail", reason="Gargand jail system setup")
    set_setting(guild, "jail_category_id", category.id)

    staff_role_id = setting(guild, "staff_role_id")
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None

    category_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
    }
    if staff_role:
        category_overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
            manage_messages=True,
        )
    try:
        await category.edit(overwrites=category_overwrites, reason="Gargand jail category permissions")
    except (discord.Forbidden, discord.HTTPException):
        pass

    async def ensure_channel(key: str, name: str, appeal: bool = False):
        channel = None
        channel_id = setting(guild, key)
        if channel_id:
            candidate = guild.get_channel(channel_id)
            if isinstance(candidate, discord.TextChannel):
                channel = candidate
        if channel is None:
            channel = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel) and c.name == name and c.category_id == category.id,
                guild.text_channels,
            )
        if channel is None:
            channel = await guild.create_text_channel(
                name,
                category=category,
                reason="Gargand jail system setup",
            )
        set_setting(guild, key, channel.id)

        jail_permissions = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=not appeal,
            send_messages_in_threads=not appeal,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: jail_permissions,
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=True,
                manage_messages=True,
            )
        try:
            await channel.edit(overwrites=overwrites, reason="Gargand jail channel permissions")
        except (discord.Forbidden, discord.HTTPException):
            pass
        return channel

    jail_chat = await ensure_channel("jail_chat_channel_id", "jail-chat")
    jail_appeals = await ensure_channel("jail_appeal_channel_id", "jail-appeals", appeal=True)
    set_setting(guild, "jail_enabled", True)
    await apply_jail_role_permissions(guild, role)

    # Only create one panel. This survives restarts because we detect the bot's component message.
    try:
        recent = [m async for m in jail_appeals.history(limit=15)]
        has_panel = any(
            bot.user and m.author.id == bot.user.id and m.embeds and m.embeds[0].title == "📝 Jail appeal"
            for m in recent
        )
    except discord.HTTPException:
        has_panel = False
    if not has_panel:
        await jail_appeals.send(
            embed=embed(
                "📝 Jail appeal",
                "Are you jailed and want to request an appeal?\n\n"
                "Press **Open jail appeal** below. You may submit **one appeal per jail case**. ✨",
                WARNING,
            ),
            view=JailAppealPanelView(guild.id),
        )
    return role, category, jail_chat, jail_appeals


async def lock_member_to_jail(member: discord.Member, jail_role: discord.Role, original_role_ids: list[int]):
    me = member.guild.me
    removable = []
    if me:
        removable = [r for r in member.roles if not r.is_default() and r != jail_role and r < me.top_role]
    if removable:
        try:
            await member.remove_roles(*removable, reason="Gargand jail")
        except discord.HTTPException:
            pass
    if jail_role not in member.roles:
        await member.add_roles(jail_role, reason="Gargand jail")
    await apply_jail_role_permissions(member.guild, jail_role)


async def restore_member_from_jail(member: discord.Member, original_role_ids: list[int], jail_role: discord.Role):
    if jail_role in member.roles:
        try:
            await member.remove_roles(jail_role, reason="Gargand unjail")
        except discord.HTTPException:
            pass
    me = member.guild.me
    roles = []
    for rid in original_role_ids:
        role = member.guild.get_role(rid)
        if role and not role.is_default() and (not me or role < me.top_role):
            roles.append(role)
    if roles:
        try:
            await member.add_roles(*roles, reason="Gargand unjail restore")
        except discord.HTTPException:
            pass


class JailAppealPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        button = discord.ui.Button(
            label="Open jail appeal",
            emoji="📝",
            style=discord.ButtonStyle.primary,
            custom_id=f"testiny:jail:{guild_id}:appeal",
        )
        button.callback = self.open_appeal
        self.add_item(button)

    async def open_appeal(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This jail appeal panel is unavailable.", ephemeral=True)
            return
        jail = get_active_jail(guild.id, interaction.user.id)
        if not jail:
            await interaction.response.send_message("🔓 You do not have an active jail case.", ephemeral=True)
            return
        if jail["appeal_created"]:
            old = guild.get_channel(jail["appeal_channel_id"]) if jail["appeal_channel_id"] else None
            msg = f"📝 You already used your one appeal for this case: {old.mention}" if old else "📝 You already submitted the one appeal allowed for this jail case."
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.send_modal(JailAppealModal(guild.id, int(jail["jail_id"])))


class JailAppealModal(discord.ui.Modal, title="📝 Jail appeal"):
    reason = discord.ui.TextInput(
        label="Why should you be unjailed?",
        placeholder="Explain your situation...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, guild_id: int, jail_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.jail_id = jail_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = bot.get_guild(self.guild_id)
        case = get_jail_case(self.jail_id)
        if not guild or not case or not case["active"] or case["user_id"] != interaction.user.id:
            await send_followup_resilient(interaction, "🔓 This jail case is no longer active.")
            return
        if case["appeal_created"]:
            await send_followup_resilient(interaction, "📝 You already submitted your one appeal for this jail case.")
            return

        category = guild.get_channel(setting(guild, "jail_category_id")) if setting(guild, "jail_category_id") else None
        if not isinstance(category, discord.CategoryChannel):
            await send_followup_resilient(interaction, "⚠️ Jail setup is incomplete. Ask an administrator to run `!setup` and enable Jail.")
            return
        staff_role_id = setting(guild, "staff_role_id")
        staff_role = guild.get_role(staff_role_id) if staff_role_id else None
        jail_role_id = setting(guild, "jail_role_id")
        jail_role = guild.get_role(jail_role_id) if jail_role_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        }
        if jail_role:
            overwrites[jail_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        try:
            channel = await guild.create_text_channel(
                f"jail-appeal-{interaction.user.name[:16]}".lower(),
                category=category,
                overwrites=overwrites,
                reason=f"Gargand jail appeal #{self.jail_id}",
            )
            set_jail_appeal_created(self.jail_id, channel.id)
            e = embed(
                "📝 Jail appeal ticket",
                f"**Member:** {interaction.user.mention}\n**Case:** `#{self.jail_id}`\n**Jailed for:** {case['reason']}\n\n**Appeal:**\n{self.reason.value}",
                WARNING,
            )
            e.set_thumbnail(url=interaction.user.display_avatar.url)
            await channel.send(embed=e, view=JailAppealControlView(self.jail_id))
            if staff_role:
                await channel.send(staff_role.mention, allowed_mentions=discord.AllowedMentions(roles=True))
            await send_followup_resilient(interaction, f"✅ Your jail appeal was created: {channel.mention}")
        except discord.Forbidden:
            await send_followup_resilient(interaction, "❌ I cannot create the jail appeal ticket. Check **Manage Channels** and role permissions.")
        except discord.HTTPException as exc:
            await send_followup_resilient(interaction, f"❌ Discord rejected the appeal ticket creation: `{exc}`")


class JailAppealControlView(discord.ui.View):
    def __init__(self, jail_id: int):
        super().__init__(timeout=None)
        self.jail_id = jail_id
        approve = discord.ui.Button(
            label="Approve / unjail", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"testiny:jail:case:{jail_id}:approve",
        )
        deny = discord.ui.Button(
            label="Deny / close", emoji="❌", style=discord.ButtonStyle.danger,
            custom_id=f"testiny:jail:case:{jail_id}:deny",
        )
        approve.callback = self.approve
        deny.callback = self.deny
        self.add_item(approve)
        self.add_item(deny)

    async def approve(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        case = get_jail_case(self.jail_id)
        if not case:
            await interaction.response.send_message("Jail case not found.", ephemeral=True)
            return
        member = interaction.guild.get_member(case["user_id"])
        role_id = setting(interaction.guild, "jail_role_id")
        jail_role = interaction.guild.get_role(role_id) if role_id else None
        if member and jail_role:
            await restore_member_from_jail(member, json.loads(case["original_roles"] or "[]"), jail_role)
        close_active_jail(interaction.guild.id, case["user_id"])
        await interaction.response.send_message("✅ Appeal approved — the member has been unjailed.")
        await log_action(interaction.guild, "✅ Jail appeal approved", f"Case `#{self.jail_id}` approved by {interaction.user.mention}.", SUCCESS)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason=f"Gargand jail appeal #{self.jail_id} approved")
        except discord.HTTPException:
            pass

    async def deny(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message("❌ Appeal denied. This ticket is closing.")
        await log_action(interaction.guild, "❌ Jail appeal denied", f"Case `#{self.jail_id}` denied by {interaction.user.mention}.", WARNING)
        await asyncio.sleep(1)
        try:
            await interaction.channel.delete(reason=f"Gargand jail appeal #{self.jail_id} denied")
        except discord.HTTPException:
            pass


# -----------------------------
# Ticket / appeal / setup panels
# -----------------------------

async def post_verification_panel(guild: discord.Guild):
    channel_id = setting(guild, "verification_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    unverified, verified = await configure_verification_roles(guild)
    try:
        ow = channel.overwrites_for(guild.default_role)
        ow.view_channel = False
        await channel.set_permissions(guild.default_role, overwrite=ow)
        await channel.set_permissions(unverified, view_channel=True, send_messages=False, read_message_history=True)
        await channel.set_permissions(verified, view_channel=False)
    except (discord.Forbidden, discord.HTTPException):
        pass
    e = embed("🔐 Welcome verification", "Press the button below to start a private phrase challenge. Each challenge expires after five minutes; three incorrect answers trigger a 15-minute cooldown. ✨\n\nTo gate the rest of your server, set the Unverified role to read-only in your regular channels.", EMBED_COLOR)
    try:
        await channel.send(embed=e, view=VerificationView(guild.id))
    except discord.HTTPException:
        pass


async def post_feedback_panel(guild: discord.Guild):
    ch = guild.get_channel(setting(guild, "feedback_channel_id")) if setting(guild, "feedback_channel_id") else None
    if isinstance(ch, discord.TextChannel):
        e = embed("💬 Tell us what you think", "Tap the button below, write your feedback, then pick a star rating. ⭐", INFO)
        await ch.send(embed=e, view=FeedbackPanelView(guild.id))


async def post_staff_application_panel(guild: discord.Guild):
    ch = guild.get_channel(setting(guild, "staff_application_channel_id")) if setting(guild, "staff_application_channel_id") else None
    if isinstance(ch, discord.TextChannel):
        e = embed("📋 Staff applications", "Think you have what it takes to join the team? Click below and fill out the application.\n\nYour answers will be sent privately to the configured staff/log channel.", EMBED_COLOR)
        await ch.send(embed=e, view=StaffApplicationView())


async def create_j4j_ticket(guild: discord.Guild, member: discord.Member):
    # J4J gets its own auto-created category.
    category_name = "J4J Tickets"
    category = discord.utils.get(guild.categories, name=category_name)
    if not category:
        try:
            category = await guild.create_category(category_name, reason="Gargand J4J system")
        except discord.HTTPException:
            category = None
    staff_role = guild.get_role(setting(guild, "staff_role_id")) if setting(guild, "staff_role_id") else None
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False), member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
    if staff_role: ow[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    channel = await guild.create_text_channel(f"j4j-{member.name[:15]}", category=category, overwrites=ow, reason="Gargand J4J ticket")
    await channel.send(embed=embed("🤝 J4J review", f"{member.mention} reported joining from a J4J. Staff can review this ticket.\n\nUse the buttons below.", WARNING), view=J4JTicketView(member.id))
    if staff_role:
        await channel.send(staff_role.mention, allowed_mentions=discord.AllowedMentions(roles=True))
    return channel


class J4JTicketView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="J4J — ban", emoji="🔨", style=discord.ButtonStyle.danger, custom_id="testiny:j4j:ban")
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True); return
        member = interaction.guild.get_member(self.user_id)
        if member:
            try:
                await member.ban(reason="Gargand J4J review")
                await log_action(interaction.guild, "🔨 J4J ban", f"{member} was banned after J4J review.", DANGER)
            except discord.Forbidden:
                await interaction.response.send_message("I cannot ban that member.", ephemeral=True); return
        await interaction.response.send_message("🔨 J4J action recorded.")

    @discord.ui.button(label="No J4J — dismiss", emoji="✅", style=discord.ButtonStyle.success, custom_id="testiny:j4j:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True); return
        await interaction.response.send_message("✅ No J4J action taken.")


# -----------------------------
# Commands
# -----------------------------

@bot.hybrid_command(name="help")
async def help_command(ctx: commands.Context):
    e = embed("📚 Gargand command center", "Commands support both `/slash` commands and the traditional `!prefix` commands.\n\n**Moderation & cases**\n`!ban @user [reason]` • `!kick @user [reason]` • `!warn @user [reason]` • `!timeout @user <duration>` • `!case <id>` • `!cases [@user]` • `!purge <1–100>`\n\n**Security**\n`!raidlockdown [duration]` • `!raidrelease` • staged join-surge watch and optional automatic lockdown\n\n**Community**\n`!afk [reason]` • `!invites @user` • `!invited @user` • `!inviter @user` • `!reset invites @user`\n\n**Tickets / appeals / setup**\n`!setup` • `!jail @user [reason]` • `!unjail @user [reason]` • `!appeal case <id> <statement>` • `!appeal server` • `!ticket panel` • `!ticket questions <type> q1 | q2 | ...`\n\n**Fun**\n`!giveaway <duration> <winners> <prize> [| image_url]` • `!giveaway reroll <message_id>` • `!giveaway end <message_id>` • `!daily`\n\n**Utilities**\n`!stick <message>` • `!unstick` • `!role give @user @role` • `!role make <name> <hex>` • `!autoreaction #channel 😀` • `!proof please`", EMBED_COLOR)
    e.add_field(name="❔ Help & questions", value="`!questions` • Get answers about Gargand, setup, logging, security, tickets, commands and the website dashboard.", inline=False)
    e.add_field(name="🧾 Case history", value="Successful moderation actions receive a case ID. Use `!case <id>` or `!cases [@user]`; staff can search the shared history on the website dashboard.", inline=False)
    e.add_field(name="🌙 New tools", value="`!warnings @user` • `!clearwarnings @user` (admins) • `!announce #channel <message>` • `!poll Question | Option 1 | Option 2`", inline=False)
    e.add_field(name="✨ More to explore", value="`!8ball <question>` • `!choose a | b` • `!roll 3d8` • `!rps rock` • `!avatar` • `!userinfo` • `!serverinfo` • `!quote` • `!reverse text` • `!mock text` • `!color 8B5CF6` • `!about`", inline=False)
    e.add_field(name="🤖 AI studio", value="`!ask <question>` • `!story <idea>` • `!roast [@member]` • `!compliment [@member]` • `!riddle` • `!poem [topic]` • `!joke [topic]` • `!caption <idea>` • `!namegen <theme>` • `!quiz <topic>` • `!aiimage <description>`", inline=False)
    e.add_field(name="🧼 Counters & anti-swear", value="`!mommycount [@member]` • `!swearcount [@member]` • Admin setup: `!swearwords list/add/remove` and `!antiswear on/off/status`", inline=False)
    e.add_field(name="🎵 Music player", value="`!join` • `!play <song or URL>` • `!skip` • `!pause` • `!resume` • `!stop` • `!queue` • `!nowplaying` • `!volume 5-150` • `!leave`", inline=False)
    e.add_field(name="🏅 Community & achievements", value="`!level [@member]` • `!leaderboard` • `!globalleaderboard` • `!achievements [@member]` • `!quest` • `!season` • `!seasonleaderboard` • `!party create/join/leave/status/leaderboard`", inline=False)
    e.add_field(name="📡 Feeds & anime", value="`!image <search terms>` • `!rss add <feed URL>` • `!rss list` • `!rss remove <ID>` • `!animewatch <AniList ID>` • `!animeunwatch <AniList ID>` • `!animewatches`", inline=False)
    e.add_field(name="🧾 Staff command audit", value="`!auditlog [limit]` lists recent staff commands. This is an additional audit trail alongside Gargand's existing moderation and logging system.", inline=False)
    e.add_field(name="🛠️ Extra server tools", value="`!analytics` • `!suggest <idea>` • `!serverstats` • `!securitycheck` • `!configbackup` • `!remind <duration> <message>`", inline=False)
    e.set_thumbnail(url=bot.user.display_avatar.url if bot.user else discord.Embed.Empty)
    await ctx.send(embed=e)


@bot.hybrid_command(name="questions", aliases=["question", "faq"])
async def questions_command(ctx: commands.Context):
    """Answer common questions about Gargand and how to configure it."""
    e = embed("❔ Gargand • Frequently asked questions",
        "**How do I set up Gargand?**\nRun `!setup` and follow the interactive setup panels.\n\n"
        "**How do I see commands?**\nRun `!help` or visit the Commands page on the website.\n\n"
        "**How do I set up logs?**\nUse `!setup` to choose a logs channel. Gargand can only create or use channels when it has the required Discord permissions.\n\n"
        "**How do I protect my server?**\nConfigure anti-raid, anti-nuke and Auto-Mod in setup. Test changes carefully and make sure the bot role has the needed permissions.\n\n"
        "**How do tickets work?**\nConfigure ticket options and questions, then use `!ticket panel` to post the ticket panel.\n\n"
        "**Can the website manage every setting?**\nThe dashboard exposes supported settings for servers you can manage. Some actions still need to be run in Discord.\n\n"
        "**Where do I get help?**\nOpen the Support page on the Gargand website or join the configured support server.", EMBED_COLOR)
    await ctx.send(embed=e)


@bot.hybrid_command(name="about")
async def about_command(ctx: commands.Context):
    description = (
        "🌙 **Gargand** is your all-in-one Discord guardian: smooth moderation, "
        "smart server safety, tickets, invites, giveaways, games, and creative tools.\n\n"
        "Set up your server with `!setup`, then let the night run smoother. ✨"
    )
    e = embed("👑 GARGAND • 1.0v LAUNCH", description, EMBED_COLOR)
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_command(name="mommycount", aliases=["mommy_count"])
async def mommy_count(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Show persistent server-local counts of the word mommy."""
    target = member or ctx.author
    count = get_speech_count(ctx.guild.id, target.id, "mommy")
    await ctx.send(embed=embed("🌙 Mommy counter", f"**{target.display_name}** has said **mommy** {count} time{'s' if count != 1 else ''} in this server.", INFO))


@bot.hybrid_command(name="swearcount", aliases=["swear_count"])
async def swear_count(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Show matches against this server's admin-configured word list."""
    target = member or ctx.author
    count = get_speech_count(ctx.guild.id, target.id, "swear")
    await ctx.send(embed=embed("🧼 Swear counter", f"**{target.display_name}** has matched configured words **{count}** time{'s' if count != 1 else ''} in this server.", INFO))


@bot.hybrid_command(name="antiswear")
@staff_only()
async def anti_swear_command(ctx: commands.Context, mode: str = "status"):
    mode = mode.lower()
    words = setting(ctx.guild, "swear_words", [])
    enabled = bool(setting(ctx.guild, "anti_swear", False))
    if mode == "status":
        state = "enabled" if enabled else "disabled"
        await ctx.send(embed=embed("🧼 Anti-swear setup", f"Filter is **{state}** with **{len(words)}** configured word(s)/phrase(s).\nUse `!swearwords add <word or phrase>` and `!antiswear on` to configure it.", INFO))
        return
    if mode not in {"on", "off"}:
        await ctx.send("Use `!antiswear on`, `!antiswear off`, or `!antiswear status`.")
        return
    if mode == "on" and not words:
        await ctx.send("Add at least one server-specific word or phrase first with `!swearwords add <word or phrase>`. I don't use a built-in word list.")
        return
    set_setting(ctx.guild, "anti_swear", mode == "on")
    await ctx.send(embed=embed("🧼 Anti-swear updated", f"The filter is now **{mode}** for this server.", SUCCESS))


@bot.hybrid_command(name="swearwords")
@staff_only()
async def swear_words_command(ctx: commands.Context, action: str = "list", *, word: str = ""):
    action = action.lower()
    words = list(setting(ctx.guild, "swear_words", []))
    if action == "list":
        listing = "\n".join(f"• `{item}`" for item in words) if words else "No words or phrases configured."
        await ctx.send(embed=embed("🧼 Configured filter list", listing[:3900], INFO))
        return
    if action not in {"add", "remove"} or not word.strip():
        await ctx.send("Use `!swearwords list`, `!swearwords add <word or phrase>`, or `!swearwords remove <word or phrase>`.")
        return
    word = " ".join(word.split())
    if len(word) > 40 or not re.fullmatch(r"[\w'’\- ]+", word, re.UNICODE):
        await ctx.send("Use a word or short phrase with letters, numbers, spaces, apostrophes, or hyphens (40 characters max).")
        return
    match = next((item for item in words if item.casefold() == word.casefold()), None)
    if action == "add":
        if match:
            await ctx.send("That word or phrase is already on the list.")
            return
        if len(words) >= 30:
            await ctx.send("The list is full (30 entries). Remove an entry before adding another.")
            return
        words.append(word)
        set_setting(ctx.guild, "swear_words", words)
        await ctx.send(f"✅ Added `{word}`. Enable the filter with `!antiswear on`.")
    else:
        if not match:
            await ctx.send("That word or phrase isn't on the list.")
            return
        words.remove(match)
        set_setting(ctx.guild, "swear_words", words)
        if not words and setting(ctx.guild, "anti_swear", False):
            set_setting(ctx.guild, "anti_swear", False)
        await ctx.send(f"✅ Removed `{match}` from the filter list.")


@bot.hybrid_command(name="8ball")
async def eight_ball(ctx: commands.Context, *, question: str):
    if not question.strip():
        await ctx.send("Ask me a question first. 🎱")
        return
    answers = (
        "Without a doubt.", "Absolutely. ✨", "The signs point to yes.",
        "Ask again after a snack.", "I can’t tell yet.", "Probably not.",
        "My sources say no.", "The night says yes. 🌙", "Outlook is looking bright.",
        "I wouldn’t count on it.",
    )
    await ctx.send(embed=embed("🎱 The Gargand 8-Ball", random.choice(answers), INFO))


@bot.hybrid_command(name="choose")
async def choose_command(ctx: commands.Context, *, options: str):
    choices = [item.strip() for item in options.split("|") if item.strip()]
    if len(choices) < 2:
        await ctx.send("Give me at least two choices, separated by `|`. Example: `!choose tea | coffee`")
        return
    if len(choices) > 20:
        await ctx.send("Keep it to 20 choices or fewer.")
        return
    await ctx.send(embed=embed("🌙 Gargand chooses…", random.choice(choices)[:900], EMBED_COLOR))


@bot.hybrid_command(name="roll")
async def roll_command(ctx: commands.Context, dice: str = "1d6"):
    match = re.fullmatch(r"(?:(\d{1,2})d)?(\d{1,4})", dice.strip().lower())
    if not match:
        await ctx.send("Use `!roll`, `!roll 20`, or `!roll 3d8`.")
        return
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    if count < 1 or count > 20 or sides < 2 or sides > 1000:
        await ctx.send("Roll 1–20 dice with 2–1000 sides each.")
        return
    results = [random.randint(1, sides) for _ in range(count)]
    detail = " • ".join(f"**{value}**" for value in results)
    await ctx.send(embed=embed("🎲 The dice are cast", f"{detail}\n\n**Total:** {sum(results)}", INFO))


@bot.hybrid_command(name="rps")
async def rps_command(ctx: commands.Context, choice: str):
    options = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    player = choice.casefold()
    if player not in options:
        await ctx.send("Choose `rock`, `paper`, or `scissors`. Example: `!rps rock`")
        return
    bot_choice = random.choice(tuple(options))
    result = "It's a draw!" if player == bot_choice else (
        "You win! ✨" if (player, bot_choice) in {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")} else "Gargand wins this round. 🌙"
    )
    await ctx.send(embed=embed("✊ Rock, paper, scissors", f"You: {options[player]} **{player.title()}**\nGargand: {options[bot_choice]} **{bot_choice.title()}**\n\n{result}", EMBED_COLOR))


@bot.hybrid_command(name="avatar")
async def avatar_command(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    e = embed(f"🖼️ {target.display_name}'s avatar", f"[Open full-size avatar]({target.display_avatar.url})", EMBED_COLOR)
    e.set_image(url=target.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_command(name="userinfo")
async def userinfo_command(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    roles = [role.mention for role in reversed(target.roles) if role != ctx.guild.default_role]
    role_text = ", ".join(roles[:12]) or "No roles"
    if len(roles) > 12:
        role_text += f" and {len(roles) - 12} more"
    e = embed(f"👤 {target.display_name}", f"**Account created:** <t:{int(target.created_at.timestamp())}:R>\n**Joined server:** {f'<t:{int(target.joined_at.timestamp())}:R>' if target.joined_at else 'Unknown'}\n**Top role:** {target.top_role.mention}\n**Roles:** {role_text}", EMBED_COLOR)
    e.set_thumbnail(url=target.display_avatar.url)
    e.set_footer(text=f"User ID: {target.id}")
    await ctx.send(embed=e)


@bot.hybrid_command(name="serverinfo")
@commands.guild_only()
async def serverinfo_command(ctx: commands.Context):
    guild = ctx.guild
    owner = guild.get_member(guild.owner_id) if guild.owner_id else None
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    e = embed(f"🏰 {guild.name}", f"**Owner:** {owner.mention if owner else 'Unknown'}\n**Members:** {guild.member_count:,}\n**Channels:** {text_channels} text · {voice_channels} voice\n**Roles:** {len(guild.roles)}\n**Created:** <t:{int(guild.created_at.timestamp())}:D>", EMBED_COLOR)
    e.set_thumbnail(url=guild.icon.url if guild.icon else (bot.user.display_avatar.url if bot.user else discord.Embed.Empty))
    e.set_footer(text=f"Server ID: {guild.id}")
    await ctx.send(embed=e)


@bot.hybrid_command(name="quote")
async def quote_command(ctx: commands.Context):
    quotes = (
        "A good community is built one kind message at a time.",
        "Even the longest night makes room for a new beginning.",
        "Be the reason someone enjoys being here.",
        "Small acts of trust make strong communities.",
        "The best server feature is the people in it.",
    )
    await ctx.send(embed=embed("📜 A little Gargand wisdom", f"“{random.choice(quotes)}”", INFO))


@bot.hybrid_command(name="reverse")
async def reverse_command(ctx: commands.Context, *, text: str):
    if not text.strip():
        await ctx.send("Give me something to reverse. Example: `!reverse gargand`")
        return
    await ctx.send(text[::-1][:1900], allowed_mentions=discord.AllowedMentions.none())


@bot.hybrid_command(name="mock")
async def mock_command(ctx: commands.Context, *, text: str):
    if not text.strip():
        await ctx.send("Give me something to mOcK. Example: `!mock that was easy`")
        return
    mocked = "".join(char.upper() if index % 2 else char.lower() for index, char in enumerate(text))
    await ctx.send(mocked[:1900], allowed_mentions=discord.AllowedMentions.none())


@bot.hybrid_command(name="color")
async def color_command(ctx: commands.Context, hex_code: str):
    value = hex_code.strip().removeprefix("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        await ctx.send("Use a six-digit hex color, like `!color 8B5CF6`.")
        return
    color = discord.Color(int(value, 16))
    e = discord.Embed(title=f"🎨 #{value.upper()}", description=f"Preview for **{ctx.author.display_name}**", color=color)
    await ctx.send(embed=e)

def extract_response_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
async def generate_ai_text(ctx: commands.Context, task: str, prompt: str, *, max_prompt: int = 700):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    if not api_key:
        await ctx.send(embed=embed(
            "🤖 AI studio needs a key",
            "AI commands are currently unavailable because no OpenRouter API key is configured.",
            WARNING,
        ))
        return

    prompt = prompt.strip()

    if len(prompt) > max_prompt:
        await ctx.send(f"Keep that prompt under {max_prompt} characters.")
        return

    if not prompt:
        prompt = "Choose something fun and surprising."

    now = time.monotonic()

    async with AI_TEXT_LOCK:
        last_used = AI_TEXT_LAST_USED.get(ctx.author.id, 0.0)
        remaining = AI_TEXT_COOLDOWN_SECONDS - (now - last_used)

        if remaining > 0:
            await ctx.send(
                f"✨ Give the AI studio {remaining:.0f}s to cool down, then try again.",
                delete_after=5
            )
            return

        AI_TEXT_LAST_USED[ctx.author.id] = now

    status = await ctx.send(
        embed=embed(
            "🌙 Gargand is thinking…",
            "A little stardust is on the way.",
            INFO
        )
    )

    instructions = (
        "You are Gargand, a friendly Discord community assistant. "
        "Return ONLY the final answer to the user. "
        "Never reveal chain-of-thought, private reasoning, internal notes, "
        "analysis, or thinking process. "
        "Keep the result concise and safe for a general audience. "
        "Avoid slurs, sexual content, threats, targeted harassment, "
        "and mass mentions."
    )

    try:
        response = await asyncio.to_thread(
            requests.post,
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_TEXT_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": instructions,
                    },
                    {
                        "role": "user",
                        "content": f"Task: {task}\nUser request: {prompt}",
                    },
                ],
                "max_tokens": 260,
            },
            timeout=(12, 50),
        )

        if response.status_code != 200:
            if response.status_code in (401, 403):
                message = "The AI key is invalid or does not have API access."
            elif response.status_code == 429:
                message = "The AI service is busy or out of quota. Try again later."
            else:
                message = f"The AI service returned an error ({response.status_code})."

            await status.edit(
                embed=embed(
                    "⚠️ AI studio unavailable",
                    message,
                    WARNING
                )
            )
            return

        data = response.json()

        choices = data.get("choices", [])

        if not choices:
            await status.edit(
                embed=embed(
                    "⚠️ No answer this time",
                    "Gargand couldn't read the AI response. Try again shortly.",
                    WARNING
                )
            )
            return

        message_data = choices[0].get("message", {})
        result = message_data.get("content", "")

        if not isinstance(result, str):
            result = ""

        result = result.strip()

        if "<think>" in result.lower() and "</think>" in result.lower():
            lower = result.lower()
            end = lower.rfind("</think>")
            result = result[end + len("</think>"):].strip()

        if not result:
            await status.edit(
                embed=embed(
                    "⚠️ No answer this time",
                    "Gargand couldn't read the AI response. Try again shortly.",
                    WARNING
                )
            )
            return

        e = embed(
            "✨ Gargand AI studio",
            result[:3800],
            EMBED_COLOR
        )

        e.set_footer(
            text=f"Requested by {ctx.author.display_name} • AI responses can be imperfect"
        )

        await status.edit(
            embed=e,
            allowed_mentions=discord.AllowedMentions.none()
        )

    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        print(f"Gargand text generation failed: {type(exc).__name__}")

        await status.edit(
            embed=embed(
                "⚠️ AI studio unavailable",
                "I couldn't reach the AI service. Please try again in a moment.",
                WARNING
            )
        )
@bot.hybrid_command(name="ask")
async def ai_ask(ctx: commands.Context, *, question: str):
    """Ask Gargand AI a general question."""
    await generate_ai_text(ctx, "Answer the question clearly and briefly. If uncertain, say so.", question, max_prompt=900)


@bot.hybrid_command(name="story")
async def ai_story(ctx: commands.Context, *, idea: str):
    """Create a short, original story from an idea."""
    await generate_ai_text(ctx, "Write a short, original, all-ages story with a satisfying ending.", idea, max_prompt=500)


@bot.hybrid_command(name="roast")
async def ai_roast(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Give the caller or a member a gentle, playful roast."""
    target = member or ctx.author
    await generate_ai_text(ctx, "Write one gentle, silly roast. Keep it affectionate and never target identity, appearance, disability, or sensitive traits.", f"Target display name: {target.display_name}")


@bot.hybrid_command(name="compliment")
async def ai_compliment(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Generate a kind, upbeat compliment."""
    target = member or ctx.author
    await generate_ai_text(ctx, "Write a warm, specific-sounding but non-personal compliment. Do not invent private facts.", f"Display name: {target.display_name}")


@bot.hybrid_command(name="riddle")
async def ai_riddle(ctx: commands.Context):
    """Generate an original riddle and answer."""
    await generate_ai_text(ctx, "Create one original, solvable riddle. Give the riddle first, then put the answer on a separate line labelled Answer.", "Make it clever and suitable for a general audience.")


@bot.hybrid_command(name="poem")
async def ai_poem(ctx: commands.Context, *, topic: str = "the night sky"):
    """Write a short poem about a topic."""
    await generate_ai_text(ctx, "Write a short, original poem in 4 to 8 lines.", topic, max_prompt=400)


@bot.hybrid_command(name="joke")
async def ai_joke(ctx: commands.Context, *, topic: str = "anything"):
    """Generate a clean, short joke."""
    await generate_ai_text(ctx, "Tell one clean, short joke about the requested topic.", topic, max_prompt=350)


@bot.hybrid_command(name="caption")
async def ai_caption(ctx: commands.Context, *, idea: str):
    """Create a social caption for an idea or image description."""
    await generate_ai_text(ctx, "Write one short, catchy social media caption and up to three relevant hashtags.", idea, max_prompt=500)


@bot.hybrid_command(name="namegen", aliases=["names"])
async def ai_namegen(ctx: commands.Context, *, theme: str):
    """Generate a short list of names for a theme."""
    await generate_ai_text(ctx, "Suggest exactly 8 distinct names for the requested theme. Use a numbered list and keep each name short.", theme, max_prompt=300)


@bot.hybrid_command(name="quiz")
async def ai_quiz(ctx: commands.Context, *, topic: str):
    """Generate a quick question and answer for a quiz."""
    await generate_ai_text(ctx, "Create one multiple-choice trivia question on the topic with four options, mark the correct answer, and include a one-sentence explanation. If the topic is obscure, avoid uncertain claims.", topic, max_prompt=300)


@bot.hybrid_command(name="aiimage", aliases=["aiart"])
@commands.guild_only()
@commands.cooldown(1, 90, commands.BucketType.user)
async def aiimage_command(ctx: commands.Context, *, prompt: str):
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        await ctx.send(embed=embed("🌌 AI image is not connected yet", "The bot owner needs to add `OPENROUTER_API_KEY` to the KataBump environment variables and restart Gargand. Image generations use the OPENROUTER API and may incur usage charges.", WARNING))
        return
    prompt = prompt.strip()
    if len(prompt) < 4 or len(prompt) > 700:
        await ctx.send("Give me an image description between 4 and 700 characters.")
        return
    await ctx.send(embed=embed("🎨 Gargand is creating your image", "This can take a little while. ✨", INFO))
    try:
        response = await asyncio.to_thread(
            requests.post,
            "https://api.openrouter.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "gpt-image-2.5-flare", "prompt": prompt, "size": "1024x1024", "quality": "low", "output_format": "png"},
            timeout=(15, 150),
        )
        if response.status_code != 200:
            if response.status_code in (401, 403):
                message = "The image service key is invalid or not enabled for image generation. Ask the server owner to check it."
            elif response.status_code == 429:
                message = "The image service is busy or out of quota. Please try again later."
            else:
                message = f"The image service returned an error ({response.status_code}). Please try again later."
            await ctx.send(embed=embed("❌ Image generation failed", message, WARNING))
            return
        encoded = response.json()["data"][0]["b64_json"]
        image_bytes = base64.b64decode(encoded, validate=True)
        file = discord.File(io.BytesIO(image_bytes), filename="gargand-ai.png")
        e = embed("🌌 Made with Gargand AI", f"**Prompt:** {prompt[:900]}", EMBED_COLOR)
        await ctx.send(embed=e, file=file)
    except requests.RequestException as exc:
        # Network failures are transient; keep the bot running and avoid noisy logs.
        print(f"Gargand image service temporarily unavailable: {type(exc).__name__}")
        await ctx.send(embed=embed("⏳ Image service unavailable", "The image service did not respond. Please try again in a moment.", WARNING))
    except (KeyError, IndexError, ValueError):
        await ctx.send(embed=embed("❌ Image generation failed", "The image service returned an invalid response. Please try again later.", WARNING))


@bot.hybrid_command(name="ping")
async def ping(ctx: commands.Context):
    """Show Gargand latency and online status."""
    latency = round(bot.latency * 1000)
    await ctx.send(embed=embed("🏓 Gargand Pong!", f"**Latency:** `{latency} ms`\\n**Status:** Online ✨", SUCCESS))


@bot.hybrid_command(name="membercount")
@commands.guild_only()
async def membercount(ctx: commands.Context):
    """Show the server member count."""
    guild = ctx.guild
    humans = sum(1 for member in guild.members if not member.bot)
    bots = sum(1 for member in guild.members if member.bot)
    await ctx.send(embed=embed("👥 Member count", f"**Total:** `{guild.member_count}`\\n**Humans:** `{humans}`\\n**Bots:** `{bots}`", INFO))


@bot.hybrid_command(name="roleinfo")
@commands.guild_only()
async def roleinfo(ctx: commands.Context, role: discord.Role):
    """Show useful information about a role."""
    await ctx.send(embed=embed("🎭 Role information", f"**Name:** {role.name}\\n**ID:** `{role.id}`\\n**Members:** `{len(role.members)}`\\n**Position:** `{role.position}`\\n**Mentionable:** `{role.mentionable}`", INFO))


@bot.hybrid_command(name="channelinfo")
@commands.guild_only()
async def channelinfo(ctx: commands.Context, channel: discord.TextChannel = None):
    """Show useful information about a text channel."""
    channel = channel or ctx.channel
    category_name = channel.category.name if channel.category else "None"
    await ctx.send(embed=embed("📺 Channel information", f"**Name:** {channel.mention}\\n**ID:** `{channel.id}`\\n**Category:** `{category_name}`\\n**Slowmode:** `{channel.slowmode_delay}s`", INFO))

@bot.hybrid_command()
@staff_only()
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if member_is_protected(member, ctx.author, ctx.guild):
        await ctx.send(embed=embed("🛡️ Protected member", "I cannot ban the server owner, yourself, or a member above my role.", WARNING)); return
    invite = setting(ctx.guild, "appeal_invite_url")
    view = AppealLinkView(invite) if invite else None
    await safe_dm(member, embed_obj=embed("🔨 You have been banned", f"You have been banned from **{ctx.guild.name}**.\n\n**Reason:** {reason}\n\nUse the button below to open the appeal server." if invite else f"You have been banned from **{ctx.guild.name}**.\n\n**Reason:** {reason}", DANGER), view=view)
    try:
        await member.ban(reason=reason, delete_message_seconds=0)
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Ban failed", "I need the **Ban Members** permission and a role higher than the target.", DANGER)); return
    case_id = record_moderation_case(ctx.guild, "ban", ctx.author, member, reason)
    await log_action(ctx.guild, "🔨 Member banned", f"{member.mention} was banned by {ctx.author.mention}\n**Case:** `#{case_id}`\n**Reason:** {reason}", DANGER)
    m = await ctx.send(embed=embed("🔨 Banned", f"{member.mention} has been banned.\n\n**Case:** `#{case_id}`", DANGER))
    await asyncio.sleep(5)
    try: await m.delete()
    except discord.HTTPException: pass


@bot.hybrid_command()
@staff_only()
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if member_is_protected(member, ctx.author, ctx.guild):
        await ctx.send(embed=embed("🛡️ Protected member", "I cannot kick the server owner, yourself, or a member above my role.", WARNING)); return
    await safe_dm(member, embed_obj=embed("👢 You have been kicked", f"You have been kicked from **{ctx.guild.name}**.\n\n**Reason:** {reason}", WARNING))
    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Kick failed", "I need the **Kick Members** permission and a role higher than the target.", DANGER)); return
    case_id = record_moderation_case(ctx.guild, "kick", ctx.author, member, reason)
    await log_action(ctx.guild, "👢 Member kicked", f"{member.mention} was kicked by {ctx.author.mention}\n**Case:** `#{case_id}`\n**Reason:** {reason}", WARNING)
    await ctx.send(embed=embed("👢 Kicked", f"{member.mention} has been kicked.\n**Case:** `#{case_id}`", WARNING), delete_after=5)


@bot.hybrid_command(name="report")
@commands.cooldown(1, 30, commands.BucketType.user)
async def report_member(ctx: commands.Context, member: discord.Member, *, reason: str):
    """Submit a report privately to staff."""
    is_interaction = ctx.interaction is not None
    if is_interaction:
        await ctx.defer(ephemeral=True)

    async def report_reply(message_embed: discord.Embed):
        if is_interaction:
            await ctx.send(embed=message_embed, ephemeral=True)
            return
        try:
            await ctx.author.send(embed=message_embed)
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send(embed=message_embed, delete_after=10)

    if not ctx.guild:
        await report_reply(embed("🚩 Reports are server-only", "Use this command inside the server where the issue happened.", WARNING))
        return
    reason = reason.strip()[:500]
    if not reason:
        await report_reply(embed("🚩 Add a reason", "Please include a short reason for the report.", WARNING))
        return
    if member.id == ctx.author.id:
        await report_reply(embed("🚩 Invalid report", "You cannot report yourself.", WARNING))
        return
    # Prefix-command invocations can expose the reason in chat; remove the
    # invocation when possible. Slash-command responses are ephemeral.
    if not is_interaction and ctx.message:
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
    case_id = record_moderation_case(
        ctx.guild, "member_report", ctx.author, member, reason,
        details="Community-submitted report. Review the context before taking action.",
        channel=ctx.channel,
    )
    await log_action(
        ctx.guild, "🚩 Member report received",
        f"**Reported member:** {member.mention} (`{member.id}`)\n"
        f"**Reported by:** {ctx.author.mention} (`{ctx.author.id}`)\n"
        f"**Channel:** {ctx.channel.mention}\n**Case:** `#{case_id}`\n"
        f"**Reason:** {reason}\n\nPlease review the context before taking action.",
        WARNING,
    )
    await report_reply(embed("🚩 Report sent", f"Your report about {member.mention} was sent to the server staff for review.\n**Case:** `#{case_id}`", SUCCESS))


@bot.hybrid_command()
@staff_only()
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if member_is_protected(member, ctx.author, ctx.guild):
        await ctx.send(embed=embed("🛡️ Protected member", "I cannot warn this member.", WARNING)); return
    conn = db_connect()
    row = conn.execute("SELECT count FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)).fetchone()
    count = (row["count"] if row else 0) + 1
    conn.execute("INSERT INTO warnings(guild_id,user_id,count,last_reason) VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET count=excluded.count,last_reason=excluded.last_reason", (ctx.guild.id, member.id, count, reason))
    conn.commit(); conn.close()
    case_id = record_moderation_case(ctx.guild, "warn", ctx.author, member, reason, details=f"Warning {count}/5")
    await log_action(ctx.guild, "⚠️ Warning issued", f"{member.mention} warned by {ctx.author.mention}\n**Case:** `#{case_id}`\n**Count:** {count}/5\n**Reason:** {reason}", WARNING)
    await ctx.send(embed=embed("⚠️ Warning", f"{member.mention} now has **{count}/5** warnings.\n\n**Case:** `#{case_id}`\n**Reason:** {reason}", WARNING), delete_after=7)
    await safe_dm(member, embed_obj=embed("⚠️ You received a warning", f"You were warned in **{ctx.guild.name}**.\n**Warning:** {count}/5\n**Reason:** {reason}", WARNING))
    if count >= 5:
        try:
            await member.kick(reason="Reached 5 warnings")
            kick_case = record_moderation_case(ctx.guild, "auto_kick", ctx.author, member, "Reached 5 warnings", details="Automatic escalation after warning threshold.")
            await ctx.send(embed=embed("👢 5 warnings reached", f"{member.mention} was kicked after reaching **5/5** warnings.\n**Case:** `#{kick_case}`", DANGER), delete_after=8)
        except discord.Forbidden:
            await ctx.send(embed=embed("❌ Auto-kick failed", "I cannot kick that member; check role hierarchy and permissions.", DANGER), delete_after=8)


@bot.hybrid_command(name="purge", aliases=["clear"])
@staff_only()
async def purge(ctx: commands.Context, amount: int):
    """Remove up to 100 recent messages from this channel."""
    if amount < 1 or amount > 100:
        await ctx.send(embed=embed("🧹 Pick 1–100 messages", "For safety, purge is limited to 100 messages at a time.", WARNING), delete_after=6)
        return
    try:
        deleted = await ctx.channel.purge(limit=amount + 1, reason=f"Bulk cleanup by {ctx.author}")
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Purge failed", "I need **Manage Messages** permission in this channel.", DANGER), delete_after=8)
        return
    except discord.HTTPException:
        await ctx.send(embed=embed("❌ Purge failed", "Discord could not remove those messages. Try a smaller amount.", DANGER), delete_after=8)
        return
    removed = max(0, len(deleted) - 1)
    case_id = record_moderation_case(ctx.guild, "purge", ctx.author, None, f"Removed {removed} messages", details=f"Bulk cleanup in #{ctx.channel.name}", channel=ctx.channel)
    await log_action(ctx.guild, "🧹 Messages purged", f"**{removed}** messages removed in {ctx.channel.mention} by {ctx.author.mention}.\n**Case:** `#{case_id}`", WARNING)
    await ctx.send(embed=embed("🧹 Channel cleaned", f"Removed **{removed}** messages.\n**Case:** `#{case_id}`", SUCCESS), delete_after=5)


@bot.hybrid_command(name="warnings", aliases=["warns"])
@staff_only()
async def warnings_command(ctx: commands.Context, member: discord.Member):
    conn = db_connect()
    row = conn.execute("SELECT count,last_reason FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)).fetchone()
    conn.close()
    count = row["count"] if row else 0
    reason = row["last_reason"] if row and row["last_reason"] else "No warning reason recorded."
    await ctx.send(embed=embed(f"⚠️ Warnings • {member.display_name}", f"**Active count:** {count}/5\n**Latest reason:** {reason}", WARNING if count else INFO))


@bot.hybrid_command(name="case")
@staff_only()
async def moderation_case_command(ctx: commands.Context, case_id: int):
    """Look up a Gargand moderation or security case by its ID."""
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM moderation_cases WHERE guild_id=? AND case_id=?",
        (ctx.guild.id, case_id),
    ).fetchone()
    conn.close()
    if not row:
        await ctx.send(embed=embed("🔎 Case not found", f"There is no Gargand case `#{case_id}` in this server.", WARNING), delete_after=8)
        return
    target = f"{row['target_name']} (`{row['target_id']}`)" if row["target_id"] else row["target_name"]
    description = (
        f"**Action:** {row['action'].replace('_', ' ').title()}\n"
        f"**Target:** {target}\n"
        f"**Moderator:** {row['actor_name']}\n"
        f"**Reason:** {row['reason']}\n"
        f"**Result:** {row['outcome'].replace('_', ' ').title()}"
    )
    if row["details"]:
        description += f"\n**Details:** {row['details']}"
    if row["expires_at"]:
        description += f"\n**Expires:** <t:{int(row['expires_at'])}:R>"
    if row["channel_id"]:
        description += f"\n**Channel:** <#{row['channel_id']}>"
    await ctx.send(embed=embed(f"🛡️ Gargand case #{row['case_id']}", description, WARNING if row["outcome"] != "success" else INFO))


@bot.hybrid_command(name="cases")
@staff_only()
async def moderation_cases_command(ctx: commands.Context, member: Optional[discord.Member] = None):
    """List the latest cases, optionally filtered to one member."""
    conn = db_connect()
    if member:
        rows = conn.execute(
            "SELECT case_id,action,target_name,reason,created_at,outcome FROM moderation_cases WHERE guild_id=? AND target_id=? ORDER BY case_id DESC LIMIT 10",
            (ctx.guild.id, member.id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT case_id,action,target_name,reason,created_at,outcome FROM moderation_cases WHERE guild_id=? ORDER BY case_id DESC LIMIT 10",
            (ctx.guild.id,),
        ).fetchall()
    conn.close()
    if not rows:
        await ctx.send(embed=embed("🛡️ No cases yet", "Gargand has not recorded cases for this server.", INFO), delete_after=8)
        return
    lines = []
    for row in rows:
        target = row["target_name"] or "Server"
        reason = (row["reason"] or "No reason")[:90]
        lines.append(f"`#{row['case_id']}` **{row['action'].replace('_', ' ').title()}** · {target} · {reason}")
    title = f"Recent cases · {member.display_name}" if member else "Recent moderation cases"
    await ctx.send(embed=embed(title, "\n".join(lines), INFO))


@bot.hybrid_command(name="recover")
@admin_only()
async def recover_case_command(ctx: commands.Context, case_id: int):
    """Restore a deleted channel/role or undo a saved channel/role edit."""
    if case_id <= 0:
        await ctx.send(embed=embed("♻️ Invalid case", "Enter a positive case number, for example `!recover 42`.", WARNING), delete_after=8)
        return
    restored, message = await restore_recovery_snapshot(ctx.guild, case_id, ctx.author)
    await ctx.send(embed=embed("♻️ Recovery complete" if restored else "♻️ Recovery unavailable", message, SUCCESS if restored else WARNING), delete_after=15)


@bot.hybrid_command(name="antinuke")
@admin_only()
async def anti_nuke_status_command(ctx: commands.Context):
    """Show this server's anti-nuke thresholds and optional response settings."""
    guild = ctx.guild
    cfg = get_settings(guild.id)
    trusted_ids = cfg.get("anti_nuke_trusted_role_ids", [])
    if not isinstance(trusted_ids, list):
        trusted_ids = []
    trusted_names = [role.name for role_id in trusted_ids if str(role_id).isdigit() and (role := guild.get_role(int(role_id)))]
    thresholds = [
        ("Channel deletions", "anti_nuke_threshold_channel_delete", 3),
        ("Role deletions", "anti_nuke_threshold_role_delete", 3),
        ("Channel edits", "anti_nuke_threshold_channel_update", 4),
        ("Role edits", "anti_nuke_threshold_role_update", 4),
    ]
    summary = "\n".join(f"**{label}:** {bounded_setting_int(guild, key, default, 2, 10)}" for label, key, default in thresholds)
    summary += f"\n**Window:** {bounded_setting_int(guild, 'anti_nuke_window_seconds', 10, 5, 60)} seconds"
    summary += f"\n**Temporary lockdown:** {'On' if cfg.get('anti_nuke_auto_lockdown') else 'Off'}"
    summary += f"\n**Automatic ban:** {'On' if cfg.get('anti_nuke_auto_ban') else 'Off'}"
    summary += f"\n**Trusted roles:** {', '.join(trusted_names) if trusted_names else 'None configured'}"
    e = embed("🛡️ Anti-nuke configuration", summary, INFO)
    e.add_field(name="Status", value="Enabled" if cfg.get("anti_nuke") else "Disabled", inline=True)
    e.set_footer(text="Use the web dashboard to change thresholds and trusted roles • !recover <case id> restores saved snapshots")
    await ctx.send(embed=e)


@bot.hybrid_command(name="clearwarnings", aliases=["resetwarnings"])
@admin_only()
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    conn = db_connect()
    conn.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    conn.commit()
    conn.close()
    case_id = record_moderation_case(ctx.guild, "clear_warnings", ctx.author, member, "Warning history reset", details="The active warning count was reset.")
    await log_action(ctx.guild, "🧽 Warnings cleared", f"All warnings for {member.mention} were cleared by {ctx.author.mention}.\n**Case:** `#{case_id}`", INFO)
    await ctx.send(embed=embed("🧽 Warning record cleared", f"Warnings for {member.mention} are now **0/5**.\n**Case:** `#{case_id}`", SUCCESS), delete_after=7)


@bot.hybrid_command(name="announce")
@staff_only()
async def announce(ctx: commands.Context, channel: discord.TextChannel, *, message: str):
    """Post a branded announcement without pinging everyone."""
    try:
        await channel.send(embed=embed(f"📣 {ctx.guild.name}", message[:4000], EMBED_COLOR), allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Announcement failed", "I need **Send Messages** and **Embed Links** in that channel.", DANGER)); return
    await ctx.send(embed=embed("📣 Announcement posted", f"Your announcement was sent to {channel.mention}.", SUCCESS), delete_after=6)
    await log_action(ctx.guild, "📣 Announcement posted", f"Posted by {ctx.author.mention} in {channel.mention}.", INFO)


@bot.hybrid_command(name="poll")
@commands.guild_only()
async def poll(ctx: commands.Context, *, prompt: str):
    """Create a quick reaction poll: question | option 1 | option 2."""
    parts = [part.strip() for part in prompt.split("|") if part.strip()]
    if len(parts) < 3 or len(parts) > 11:
        await ctx.send(embed=embed("📊 Poll format", "Use !poll Question | Option 1 | Option 2 with 2–10 options.", WARNING)); return
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    description = "\n".join(f"{emojis[index]}  {option[:120]}" for index, option in enumerate(parts[1:]))
    message = await ctx.send(embed=embed(f"📊 {parts[0][:240]}", description, EMBED_COLOR), allowed_mentions=discord.AllowedMentions.none())
    for emoji in emojis[:len(parts) - 1]:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            break


@bot.hybrid_command()
@staff_only()
async def timeout(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    try: seconds = parse_duration(duration)
    except ValueError as e:
        await ctx.send(embed=embed("⏱️ Invalid duration", str(e), WARNING)); return
    if seconds > 28 * 86400:
        await ctx.send(embed=embed("⏱️ Too long", "Discord member timeouts cannot exceed 28 days.", WARNING)); return
    if member_is_protected(member, ctx.author, ctx.guild):
        await ctx.send(embed=embed("🛡️ Protected member", "I cannot timeout this member.", WARNING)); return
    try:
        await member.timeout(timedelta(seconds=seconds), reason=reason)
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Timeout failed", "I need **Moderate Members** permission.", DANGER)); return
    expires_at = utc_ts() + seconds
    case_id = record_moderation_case(ctx.guild, "timeout", ctx.author, member, reason, expires_at=expires_at, details=f"Duration: {human_duration(seconds)}")
    await log_action(ctx.guild, "🔇 Member timed out", f"{member.mention} for **{human_duration(seconds)}**\n**Case:** `#{case_id}`\n**Reason:** {reason}", WARNING)
    await ctx.send(embed=embed("🔇 Timed out", f"{member.mention} is timed out for **{human_duration(seconds)}**.\n**Case:** `#{case_id}`", WARNING), delete_after=6)


@bot.hybrid_command()
@staff_only()
async def lock(ctx: commands.Context):
    ch = ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    ow.send_messages_in_threads = False
    try:
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Locked by {ctx.author}")
        await ctx.send(embed=embed("🔒 Channel locked", f"{ch.mention} is now locked for members.", DANGER))
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Lock failed", "I cannot edit this channel's permissions.", DANGER))


@bot.hybrid_command(name="unlock")
@staff_only()
async def unlock(ctx: commands.Context):
    ow = ctx.channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    ow.send_messages_in_threads = None
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Unlocked by {ctx.author}")
        await ctx.send(embed=embed("🔓 Channel unlocked", f"{ctx.channel.mention} is open again.", SUCCESS))
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Unlock failed", "I cannot edit this channel's permissions.", DANGER))


@bot.hybrid_command(name="raidlockdown")
@admin_only()
async def raidlockdown_command(ctx: commands.Context, duration: str = "10m"):
    """Temporarily pause @everyone posting; Gargand restores saved permissions automatically."""
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        await ctx.send(embed=embed("⏱️ Invalid duration", str(exc), WARNING))
        return
    if seconds > 60 * 60:
        await ctx.send(embed=embed("⏱️ Too long", "Raid lockdowns are limited to one hour so the original permissions can be restored automatically.", WARNING))
        return
    minutes = max(1, (seconds + 59) // 60)
    locked, failed, restore_at = await start_raid_lockdown(
        ctx.guild, minutes, actor=ctx.author, reason="Staff initiated lockdown",
    )
    if not locked:
        await ctx.send(embed=embed("⚠️ Lockdown not applied", "Gargand could not change any channel permissions. Check Manage Channels and role position.", DANGER))
        return
    await ctx.send(embed=embed(
        "🚨 Raid lockdown active",
        f"Posting is paused in **{locked}** channels until <t:{restore_at}:R>.\n**Could not change:** {failed} channels.\nUse `!raidrelease` to restore early.",
        DANGER,
    ))


@bot.hybrid_command(name="raidrelease")
@admin_only()
async def raidrelease_command(ctx: commands.Context):
    """Restore the original channel permissions saved by a Gargand raid lockdown."""
    restored, failed, remaining = await release_raid_lockdown(ctx.guild, actor=ctx.author, reason="Staff ended the raid lockdown")
    if not restored and not remaining:
        await ctx.send(embed=embed("🟢 No active lockdown", "Gargand has no saved raid lockdown to release.", INFO))
        return
    await ctx.send(embed=embed(
        "🟢 Raid lockdown released",
        f"Restored **{restored}** channels. **{failed}** could not be restored yet; Gargand will retry them automatically.",
        SUCCESS if not failed else WARNING,
    ))


@bot.hybrid_command(name="slowmode", aliases=["slow"])
@staff_only()
async def slowmode(ctx: commands.Context, seconds: int):
    if seconds < 0 or seconds > 21600:
        await ctx.send(embed=embed("🐢 Invalid slowmode", "Use 0–21600 seconds.", WARNING)); return
    try:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {ctx.author}")
        await ctx.send(embed=embed("🐢 Slowmode updated", f"This channel now has **{seconds}s** slowmode.", INFO))
    except discord.HTTPException:
        await ctx.send(embed=embed("❌ Failed", "Discord rejected the slowmode change.", DANGER))


@bot.hybrid_command()
@commands.guild_only()
async def afk(ctx: commands.Context, *, reason: str = "AFK"):
    conn = db_connect()
    conn.execute("INSERT OR REPLACE INTO afk(guild_id,user_id,reason,set_at) VALUES(?,?,?,?)", (ctx.guild.id, ctx.author.id, reason, utc_ts()))
    conn.commit(); conn.close()
    await ctx.send(embed=embed("💤 AFK enabled", f"{ctx.author.mention} is now AFK.\n**Reason:** {reason}\n\nWhen someone mentions you, Gargand will protect the ping and let them know you're away.", INFO), delete_after=8)


@bot.hybrid_command(name="jail")
@staff_only()
async def jail(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    guild = ctx.guild
    if not setting(guild, "jail_enabled", False):
        await ctx.send(embed=embed("🔒 Jail is disabled", "Enable **Jail setup** from `!setup` first.", WARNING), delete_after=6)
        return
    if member_is_protected(member, ctx.author, guild):
        await ctx.send(embed=embed("🛡️ Protected member", "I cannot jail the server owner, yourself, or a member above my role.", WARNING), delete_after=6)
        return
    jail_role_id = setting(guild, "jail_role_id")
    jail_role = guild.get_role(jail_role_id) if jail_role_id else None
    if not jail_role:
        jail_role, *_ = await configure_jail_system(guild)
    existing = get_active_jail(guild.id, member.id)
    if existing:
        await ctx.send(embed=embed("🔒 Already jailed", f"{member.mention} already has active jail case **#{existing['jail_id']}**.", WARNING), delete_after=6)
        return

    original_roles = [r.id for r in member.roles if not r.is_default() and r != jail_role]
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO jails(guild_id,user_id,reason,jailed_at,active,original_roles) VALUES(?,?,?,?,1,?)",
        (guild.id, member.id, reason, utc_ts(), json.dumps(original_roles)),
    )
    jail_id = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        await lock_member_to_jail(member, jail_role, original_roles)
    except discord.Forbidden:
        close_active_jail(guild.id, member.id)
        await ctx.send(embed=embed("❌ Jail failed", "I could not change this member's roles. Put the bot role above the member's roles.", DANGER))
        return

    case_id = record_moderation_case(guild, "jail", ctx.author, member, reason, details=f"Jail record #{jail_id}")
    await log_action(
        guild,
        "🔒 Member jailed",
        f"{member.mention} was jailed by {ctx.author.mention}.\n**Case:** `#{case_id}` · jail record #{jail_id}\n**Reason:** {reason}",
        DANGER,
    )
    await ctx.send(
        embed=embed(
            "🔒 Member jailed",
            f"{member.mention} is now in jail.\n\n**Case:** `#{case_id}`\n**Reason:** {reason}\n\n📝 They can press **Open jail appeal** in the jail appeal channel.",
            DANGER,
        ),
        delete_after=12,
    )


@bot.hybrid_command(name="unjail")
@staff_only()
async def unjail(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    guild = ctx.guild
    case = get_active_jail(guild.id, member.id)
    if not case:
        await ctx.send(embed=embed("🔓 Not jailed", f"{member.mention} does not have an active jail case.", INFO), delete_after=6)
        return
    role_id = setting(guild, "jail_role_id")
    jail_role = guild.get_role(role_id) if role_id else None
    if not jail_role:
        await ctx.send(embed=embed("❌ Jail role missing", "Run Jail setup again from `!setup`.", DANGER), delete_after=6)
        return
    try:
        await restore_member_from_jail(member, json.loads(case["original_roles"] or "[]"), jail_role)
    except discord.HTTPException:
        await ctx.send(embed=embed("❌ Unjail failed", "I couldn't restore the member's roles.", DANGER), delete_after=6)
        return
    close_active_jail(guild.id, member.id)
    case_id = record_moderation_case(guild, "unjail", ctx.author, member, reason, details=f"Closed jail record #{case['jail_id']}")
    await log_action(
        guild,
        "🔓 Member unjailed",
        f"{member.mention} was unjailed by {ctx.author.mention}.\n**Case:** `#{case_id}` · jail record #{case['jail_id']}\n**Reason:** {reason}",
        SUCCESS,
    )
    await ctx.send(embed=embed("🔓 Member unjailed", f"{member.mention} has been released. ✨\n\n**Case:** `#{case_id}`", SUCCESS), delete_after=8)


@bot.hybrid_command(name="antispam")
@admin_only()
async def antispam_command(ctx: commands.Context, enabled: Optional[bool] = None):
    """Enable or disable the built-in burst-spam protection."""
    if not ctx.guild:
        return
    if enabled is None:
        enabled = bool(setting(ctx.guild, "automod_spam", setting(ctx.guild, "anti_spam", False)))
        state = "enabled" if enabled else "disabled"
        await ctx.send(embed=embed("🛡️ Anti-spam status", f"Anti-spam is currently **{state}**. Use `!antispam true` or `!antispam false` to change it.", INFO))
        return
    set_setting(ctx.guild, "anti_spam", bool(enabled))
    set_setting(ctx.guild, "automod_spam", bool(enabled))
    await ctx.send(embed=embed("🛡️ Anti-spam updated", f"Burst-spam protection is now **{'enabled' if enabled else 'disabled'}**. When enabled, Gargand uses the spam threshold configured in the dashboard and may apply a short timeout to non-staff members.", SUCCESS))


@bot.hybrid_group(name="setup", invoke_without_command=True)
@admin_only()
async def setup(ctx: commands.Context):
    # Keep enabled systems healthy every time !setup is used.
    if setting(ctx.guild, "jail_enabled", False):
        try:
            await configure_jail_system(ctx.guild)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Jail setup refresh failed for {ctx.guild.id}: {exc!r}")
    # Reuse the existing dashboard when possible instead of creating duplicates.
    if await refresh_setup_dashboard(ctx.guild):
        await ctx.send(embed=embed("✨ Setup refreshed", "Your existing Gargand setup dashboard was refreshed with the latest saved settings.", SUCCESS), delete_after=5)
        return

    # The saved dashboard may have been deleted. Clear stale IDs before creating
    # a replacement so future selectors always target the new message.
    cfg = get_settings(ctx.guild.id)
    cfg.pop("setup_dashboard_channel_id", None)
    cfg.pop("setup_dashboard_message_id", None)
    save_settings(ctx.guild.id, cfg)

    e = setup_dashboard_embed(ctx.guild)
    msg = await ctx.send(embed=e, view=SetupDashboardView(ctx.guild.id))
    set_setting(ctx.guild, "setup_dashboard_channel_id", ctx.channel.id)
    set_setting(ctx.guild, "setup_dashboard_message_id", msg.id)
    # Re-render once with the now-persisted dashboard IDs so every future
    # selector can reliably locate and update this exact setup message.
    try:
        await msg.edit(embed=setup_dashboard_embed(ctx.guild), view=SetupDashboardView(ctx.guild.id))
    except discord.HTTPException:
        pass


@setup.command(name="status")
@admin_only()
async def setup_status(ctx: commands.Context):
    """Show a quick, read-only check of this server's saved Gargand setup."""
    if not ctx.guild:
        return
    cfg = get_settings(ctx.guild.id)
    db_state = "✅ reachable"
    try:
        conn = db_connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except sqlite3.Error:
        db_state = "❌ unavailable"
    bridge_state = "⚪ not configured"
    if GARGAND_WEBSITE_URL and GARGAND_BRIDGE_SECRET:
        try:
            response = await asyncio.to_thread(
                requests.get, f"{GARGAND_WEBSITE_URL}/api/bot/status", timeout=6
            )
            if response.ok:
                status = response.json()
                if status.get("online"):
                    age = status.get("last_seen_age_seconds")
                    guilds = status.get("guild_count", "?")
                    freshness = f"{age}s ago" if isinstance(age, int) else "recently"
                    bridge_state = f"✅ online · heartbeat {freshness} · {guilds} servers"
                else:
                    bridge_state = "⚠️ website reachable, bot heartbeat missing"
            else:
                bridge_state = "⚠️ website returned an error"
        except (requests.RequestException, ValueError):
            bridge_state = "❌ website unreachable"
    def channel_label(key):
        raw_id = cfg.get(key)
        channel = ctx.guild.get_channel(int(raw_id)) if str(raw_id or "").isdigit() else None
        return channel.mention if channel else "not set"
    e = embed(
        "🔎 GARGAND • Setup Status",
        f"**Database:** {db_state}\n**Website bridge:** {bridge_state}\n"
        f"**Prefix:** `{cfg.get('prefix', PREFIX)}`\n"
        f"**Logs:** {channel_label('logs_channel_id')}\n"
        f"**Welcome:** {channel_label('welcome_channel_id')}\n"
        f"**Tickets:** {channel_label('ticket_panel_channel_id')}\n"
        f"**Verification:** {channel_label('verification_channel_id')}\n\n"
        "This check is read-only. Use `!setup` to open the setup dashboard or `!setup templates` to add a starter layout.",
        INFO,
    )
    await ctx.send(embed=e)


@bot.hybrid_command(name="configbackup")
@admin_only()
async def configbackup_command(ctx: commands.Context):
    """Export this server's Gargand settings to a JSON attachment."""
    settings = get_settings(ctx.guild.id)
    backup = {
        "format": "gargand-server-config",
        "product": "Gargand",
        "schema_version": 1,
        "guild_id": str(ctx.guild.id),
        "guild_name": ctx.guild.name[:100],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "note": "Contains server configuration only, not bot tokens or database records. Review invite URLs and IDs before sharing.",
    }
    payload = json.dumps(backup, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    if len(payload) > 7_500_000:
        message = embed("❌ Backup too large", "This configuration is too large to attach safely. Remove excessive ticket questions or options and try again.", DANGER)
        if ctx.interaction:
            await ctx.send(embed=message, ephemeral=True)
        else:
            await ctx.send(embed=message)
        return
    attachment = discord.File(io.BytesIO(payload), filename=f"gargand-config-{ctx.guild.id}.json")
    message = embed("📦 Gargand configuration backup", "Your server settings are attached as JSON. Keep this file private: it may include channel/role IDs and an appeal invite URL.", SUCCESS)
    if ctx.interaction:
        await ctx.send(embed=message, file=attachment, ephemeral=True)
    else:
        await ctx.send(embed=message, file=attachment)


@bot.hybrid_command(name="diagnose")
@admin_only()
async def diagnose(ctx: commands.Context):
    """Check bot permissions and the Gargand website bridge."""
    me = ctx.guild.me
    perms = me.guild_permissions if me else discord.Permissions.none()
    bridge_state = "Not configured"
    if GARGAND_WEBSITE_URL and GARGAND_BRIDGE_SECRET:
        try:
            response = await asyncio.to_thread(requests.get, f"{GARGAND_WEBSITE_URL}/api/bot/status", timeout=8)
            status = response.json() if response.ok else {}
            online = bool(status.get("online"))
            age = status.get("last_seen_age_seconds")
            guilds = status.get("guild_count", "?")
            freshness = f"{age}s ago" if isinstance(age, int) else "unknown age"
            bridge_state = (
                f"Website reachable · heartbeat {freshness} · {guilds} servers"
                if online else "Website reachable · bot heartbeat missing"
            )
        except (requests.RequestException, ValueError):
            bridge_state = "Website unreachable"
    e = embed("🔎 Gargand Diagnostics", f"Bridge: **{bridge_state}**\nBridge secret: **{'configured' if GARGAND_BRIDGE_SECRET else 'missing'}**", EMBED_COLOR)
    e.add_field(name="Permissions", value=f"Audit log: {'✅' if perms.view_audit_log else '❌'} · Manage channels: {'✅' if perms.manage_channels else '❌'}\nManage roles: {'✅' if perms.manage_roles else '❌'} · Send messages: {'✅' if perms.send_messages else '❌'} · Embed links: {'✅' if perms.embed_links else '❌'}", inline=False)
    e.set_footer(text="Invite Gargand with the missing permissions, then run !diagnose again.")
    await ctx.send(embed=e)


@bot.hybrid_command(name="official")
@admin_only()
async def official(ctx: commands.Context, action: str = "status", *, value: str = ""):
    """Manage Gargand's official announcement connection."""
    if ctx.guild.id != OFFICIAL_GUILD_ID:
        await ctx.send("This command is only available in Gargand's official server.", delete_after=8)
        return
    action = action.lower().strip()
    cfg = get_settings(ctx.guild.id)
    if action == "status":
        channel_id = int(cfg.get("official_announcement_channel_id") or OFFICIAL_ANNOUNCEMENT_CHANNEL_ID)
        channel = ctx.guild.get_channel(channel_id)
        destination = channel.mention if channel else "not configured"
        bridge = "configured" if GARGAND_BRIDGE_SECRET else "missing secret"
        await ctx.send(embed=embed("📣 Official Updates", f"Destination: {destination}\nWebsite bridge: {bridge}\nPublish with `!official publish Title | update details`.", EMBED_COLOR))
        return
    if action == "channel":
        match = re.search(r"\d{15,22}", value)
        channel = ctx.guild.get_channel(int(match.group())) if match else None
        if not isinstance(channel, discord.TextChannel):
            await ctx.send("Choose a text channel in this server: `!official channel #announcements`.", delete_after=10)
            return
        set_setting(ctx.guild, "official_announcement_channel_id", channel.id)
        await ctx.send(embed=embed("✅ Update channel saved", f"Website and bot announcements will go to {channel.mention}.", SUCCESS))
        return
    if action == "publish":
        parts = value.split("|", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            await ctx.send("Use `!official publish Title | announcement details`.", delete_after=10)
            return
        if not GARGAND_BRIDGE_SECRET:
            await ctx.send("The website bridge secret is not configured on the bot host.", delete_after=10)
            return
        payload = {"title": parts[0].strip()[:180], "body": parts[1].strip()[:3500], "kind": "bot"}
        try:
            response = await asyncio.to_thread(requests.post, f"{GARGAND_WEBSITE_URL}/api/bot/announcements", headers={"X-Gargand-Bridge-Key": GARGAND_BRIDGE_SECRET}, json=payload, timeout=10)
            if response.ok and response.json().get("ok"):
                await ctx.send(embed=embed("✅ Update published", "The update is on the website and queued for the official announcements channel.", SUCCESS))
            else:
                await ctx.send(f"Website rejected the update ({response.status_code}). Check `!diagnose` and the shared bridge secret.", delete_after=12)
        except (requests.RequestException, ValueError):
            await ctx.send("I couldn't reach the website. Check `!diagnose` and try again.", delete_after=12)
        return
    await ctx.send("**Official server controls**\n`!official status` · `!official channel #channel` · `!official publish Title | details`", delete_after=15)


# -----------------------------
# Server starter templates
# -----------------------------

SERVER_TEMPLATE_DATA = {
    "community": {
        "label": "Community",
        "description": "A friendly all-purpose community with rules, announcements, support, and staff tools.",
        "category": "GARGAND • COMMUNITY",
        "roles": [("Administrator", 0xE0AA25), ("Staff", 0xEBCB69), ("Member", 0xA8A8A8), ("Muted", 0x555555)],
        "channels": [
            ("start-here", "text", False, "Welcome and next steps"),
            ("rules", "text", False, "Community rules and expectations"),
            ("announcements", "text", False, "Official community updates"),
            ("general", "text", False, "Main community conversation"),
            ("media", "text", False, "Share images, clips and creations"),
            ("suggestions", "text", False, "Ideas and community feedback"),
            ("support", "text", False, "Questions and help"),
            ("staff-chat", "text", True, "Private staff coordination"),
            ("mod-logs", "text", True, "Private moderation logs"),
            ("Community Lounge", "voice", False, "Community voice hangout"),
        ],
    },
    "gaming": {
        "label": "Gaming",
        "description": "Gaming community channels, team roles, LFG, clips, and private moderation rooms.",
        "category": "GARGAND • GAMING",
        "roles": [("Administrator", 0xE0AA25), ("Staff", 0xEBCB69), ("Tournament Host", 0xE68A25), ("Gamer", 0xC7A84A), ("Muted", 0x555555)],
        "channels": [
            ("start-here", "text", False, "Welcome and server information"),
            ("rules", "text", False, "Gaming and community rules"),
            ("announcements", "text", False, "Game and event announcements"),
            ("general", "text", False, "General gaming chat"),
            ("looking-for-group", "text", False, "Find teammates"),
            ("clips-and-highlights", "text", False, "Share gameplay clips"),
            ("tournaments", "text", False, "Tournament news and sign-ups"),
            ("bot-commands", "text", False, "Use bot commands here"),
            ("staff-chat", "text", True, "Private staff coordination"),
            ("mod-logs", "text", True, "Private moderation logs"),
            ("Gaming Lounge", "voice", False, "Play together"),
        ],
    },
    "creator": {
        "label": "Creator",
        "description": "For streamers and creators: announcements, content drops, suggestions, and fan chat.",
        "category": "GARGAND • CREATOR",
        "roles": [("Administrator", 0xE0AA25), ("Staff", 0xEBCB69), ("Creator", 0xE68A25), ("Community", 0xA8A8A8), ("Muted", 0x555555)],
        "channels": [
            ("start-here", "text", False, "Welcome fans and new members"),
            ("rules", "text", False, "Creator-community rules"),
            ("announcements", "text", False, "Important news and schedules"),
            ("new-content", "text", False, "New videos, streams and uploads"),
            ("community-chat", "text", False, "Chat with the community"),
            ("fan-creations", "text", False, "Artwork, edits and fan projects"),
            ("suggestions", "text", False, "Content ideas and feedback"),
            ("collaborations", "text", False, "Plan creator collaborations"),
            ("staff-chat", "text", True, "Private staff coordination"),
            ("mod-logs", "text", True, "Private moderation logs"),
            ("Watch Party", "voice", False, "Hang out with the community"),
        ],
    },
    "support": {
        "label": "Support",
        "description": "A support-focused server with FAQ, help queues, announcements, and private staff areas.",
        "category": "GARGAND • SUPPORT",
        "roles": [("Administrator", 0xE0AA25), ("Support Team", 0xEBCB69), ("Helper", 0xC7A84A), ("Member", 0xA8A8A8), ("Muted", 0x555555)],
        "channels": [
            ("start-here", "text", False, "How to get support"),
            ("rules", "text", False, "Support and conduct rules"),
            ("announcements", "text", False, "Service status and updates"),
            ("faq", "text", False, "Frequently asked questions"),
            ("open-a-ticket", "text", False, "Open a private support ticket"),
            ("community-chat", "text", False, "General questions and chat"),
            ("resolved-issues", "text", False, "Useful answers and solved cases"),
            ("staff-chat", "text", True, "Private support coordination"),
            ("support-logs", "text", True, "Private support audit logs"),
            ("Staff Voice", "voice", True, "Private staff voice room"),
        ],
    },
}

SERVER_TEMPLATE_RULES = [
    "Be respectful. Harassment, hate speech, and targeted abuse are not allowed.",
    "No spam, raids, scams, phishing, or malicious links.",
    "Do not share private information or impersonate other members.",
    "Post in the right channels and follow each channel's topic.",
    "No advertising or unsolicited promotions unless staff permit them.",
    "Follow Discord's Terms of Service and Community Guidelines.",
    "Listen to staff instructions. Use the support or appeal process if you disagree.",
]


async def apply_server_template(guild: discord.Guild, template_key: str) -> dict:
    """Add a starter layout without deleting or renaming any existing server content."""
    template = SERVER_TEMPLATE_DATA[template_key]
    # Check permissions before making any changes so a misconfigured bot does not
    # leave a half-created template behind.
    me = guild.me
    if me is None:
        raise RuntimeError("I could not verify my server permissions. Try again in a moment.")
    missing = []
    if not me.guild_permissions.manage_roles:
        missing.append("Manage Roles")
    if not me.guild_permissions.manage_channels:
        missing.append("Manage Channels")
    if missing:
        raise RuntimeError("GARGAND needs these permissions before applying the template: " + ", ".join(missing) + ". No changes were made.")
    created = {"categories": 0, "channels": 0, "roles": 0}
    role_objects = {}
    for role_name, hex_colour in template["roles"]:
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            try:
                role = await guild.create_role(
                    name=role_name,
                    colour=discord.Colour(hex_colour),
                    reason="GARGAND server template: " + template_key,
                )
                created["roles"] += 1
            except discord.Forbidden as exc:
                raise RuntimeError("GARGAND needs Manage Roles permission to create template roles.") from exc
        role_objects[role_name] = role

    category = discord.utils.get(guild.categories, name=template["category"])
    if category is None:
        try:
            category = await guild.create_category(
                name=template["category"],
                reason="GARGAND server template: " + template_key,
            )
            created["categories"] += 1
        except discord.Forbidden as exc:
            raise RuntimeError("GARGAND needs Manage Channels permission to create template categories.") from exc

    staff_role = role_objects.get("Staff") or role_objects.get("Support Team") or role_objects.get("Administrator")
    everyone_hidden = discord.PermissionOverwrite(view_channel=False)
    staff_allowed = discord.PermissionOverwrite(
        view_channel=True, send_messages=True, read_message_history=True,
        connect=True, speak=True,
    )
    bot_member = guild.me
    for channel_name, channel_type, private, topic in template["channels"]:
        exists = next(
            (ch for ch in category.channels if ch.name.casefold() == channel_name.casefold()
             and ((channel_type == "text" and isinstance(ch, discord.TextChannel))
                  or (channel_type == "voice" and isinstance(ch, discord.VoiceChannel)))),
            None,
        )
        if exists is not None:
            continue
        # discord.py requires a dictionary even for public channels with no custom overwrites.
        overwrites = {}
        if private:
            overwrites = {guild.default_role: everyone_hidden}
            for role in role_objects.values():
                if role.name in ("Administrator", "Staff", "Support Team", "Helper"):
                    overwrites[role] = staff_allowed
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_channels=True, manage_messages=True, connect=True, speak=True,
                )
        try:
            if channel_type == "voice":
                await guild.create_voice_channel(
                    name=channel_name, category=category, overwrites=overwrites,
                    reason="GARGAND server template: " + template_key,
                )
            else:
                await guild.create_text_channel(
                    name=channel_name, category=category, topic=topic[:1024],
                    overwrites=overwrites, reason="GARGAND server template: " + template_key,
                )
            created["channels"] += 1
        except discord.Forbidden as exc:
            raise RuntimeError("GARGAND needs Manage Channels permission to finish this template.") from exc

    rules_channel = next(
        (ch for ch in category.text_channels if ch.name == "rules"), None
    )
    if rules_channel is not None:
        marker = "gargand-template-rules:" + template_key
        if not (rules_channel.topic or "").startswith(marker):
            try:
                await rules_channel.edit(topic=marker + " • Starter community rules")
            except discord.HTTPException:
                pass
            rules_text = "\n".join("**%s.** %s" % (idx, rule) for idx, rule in enumerate(SERVER_TEMPLATE_RULES, 1))
            rules_embed = embed(
                "📜 " + template["label"] + " • Server Rules",
                "Welcome! Please read and follow these rules so everyone can enjoy the server.\n\n" + rules_text,
                INFO,
            )
            await rules_channel.send(embed=rules_embed)

    welcome_channel = next(
        (ch for ch in category.text_channels if ch.name == "start-here"), None
    )
    if welcome_channel is not None:
        topic_marker = "gargand-template-welcome:" + template_key
        if not (welcome_channel.topic or "").startswith(topic_marker):
            try:
                await welcome_channel.edit(topic=topic_marker + " • Welcome new members")
            except discord.HTTPException:
                pass
            await welcome_channel.send(embed=embed(
                "👑 Welcome to " + guild.name,
                "Welcome to the server! Start by reading #rules, checking #announcements, and introducing yourself in the community channels.",
                SUCCESS,
            ))
    return created


class ServerTemplateSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label=data["label"], value=key, description=data["description"][:100],
                emoji="👑" if key == "community" else "🎮" if key == "gaming" else "🎥" if key == "creator" else "🎫",
            )
            for key, data in SERVER_TEMPLATE_DATA.items()
        ]
        super().__init__(
            placeholder="Choose a GARGAND server template…",
            min_values=1, max_values=1, options=options,
            custom_id="gargand:setup:template",
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This template selector belongs to another server.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message("Only server administrators or members with Manage Server can apply templates.", ephemeral=True)
            return
        template_key = self.values[0]
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await apply_server_template(interaction.guild, template_key)
            data = SERVER_TEMPLATE_DATA[template_key]
            summary = (
                "**Template:** " + data["label"] + "\n"
                "**Added:** " + str(result["categories"]) + " category/categories, "
                + str(result["channels"]) + " channel(s), and " + str(result["roles"]) + " role(s).\n\n"
                "Existing channels and roles were preserved. Review role permissions and adjust the rules before opening the server."
            )
            await interaction.followup.send(embed=embed("✅ GARGAND template applied", summary, SUCCESS), ephemeral=True)
        except RuntimeError as exc:
            await interaction.followup.send(embed=embed("⚠️ Template needs permissions", str(exc), WARNING), ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=embed("⚠️ Template could not finish", "Discord rejected part of the setup. Check GARGAND's role permissions, then run the template again; existing content will not be deleted.", WARNING), ephemeral=True)


class ServerTemplateView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.add_item(ServerTemplateSelect(guild_id))


async def send_server_template_menu(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send(embed=embed("GARGAND • Server Templates", "Run this command inside a Discord server.", WARNING))
        return
    await ctx.send(
        embed=embed(
            "👑 GARGAND • Server Templates",
            "Choose a starter layout below. GARGAND will add a category, channels, starter rules and useful roles without deleting existing server content.\n\n"
            "Templates: **Community**, **Gaming**, **Creator**, and **Support**.\n"
            "GARGAND needs **Manage Channels** and **Manage Roles** to apply the layout.",
            INFO,
        ),
        view=ServerTemplateView(ctx.guild.id),
    )


@setup.command(name="template")
@admin_only()
async def setup_template(ctx: commands.Context):
    """Open the server template picker using the exact !setup template spelling."""
    await send_server_template_menu(ctx)


@setup.command(name="templates")
@admin_only()
async def setup_templates(ctx: commands.Context):
    """Plural alias for backwards compatibility."""
    await send_server_template_menu(ctx)


@bot.hybrid_group(name="ticket", invoke_without_command=True)
async def ticket(ctx: commands.Context):
    if not ctx.guild:
        return
    await ctx.send(embed=embed("🎫 Tickets", "Use `!ticket panel` to post the panel or `!ticket questions <type> question 1 | question 2` to customize ticket questions.", EMBED_COLOR))


@ticket.command(name="panel")
@admin_only()
async def ticket_panel(ctx: commands.Context):
    await ctx.send(embed=embed("🎫 Open a ticket", "Select a ticket type below. Your ticket will be private and your staff role will be pinged.", EMBED_COLOR), view=TicketPanelView(ctx.guild.id))


@ticket.command(name="questions")
@admin_only()
async def ticket_questions(ctx: commands.Context, ticket_type: str, *, questions: str):
    qs = [q.strip() for q in questions.split("|") if q.strip()][:5]
    cfg = get_settings(ctx.guild.id)
    tq = cfg.get("ticket_questions", {})
    tq[ticket_type.lower()] = qs
    set_setting(ctx.guild, "ticket_questions", tq)
    await ctx.send(embed=embed("✅ Ticket questions saved", f"**{ticket_type}** now has {len(qs)} question(s).", SUCCESS))


# Appeal server command names support both spellings.
@bot.hybrid_command(name="appeal_server", aliases=["apeal_server", "appealserver", "apealserver"])
@admin_only()
async def appeal_server(ctx: commands.Context):
    await ctx.send(embed=embed("📝 Appeal setup", "Choose the main server below. Only main servers that configured this guild as their appeal server appear here.", INFO), view=AppealServerView(ctx.guild))


# This lets users literally type: !appeal server / !apeal server.
@bot.hybrid_group(name="appeal", aliases=["apeal"], invoke_without_command=True)
async def appeal_group(ctx: commands.Context):
    await ctx.send(embed=embed("📝 Appeals", "Use `!appeal server` here in the appeal server to choose the main server.", INFO))


@appeal_group.command(name="server")
@admin_only()
async def appeal_server_subcommand(ctx: commands.Context):
    await appeal_server(ctx)


@appeal_group.command(name="case")
@commands.guild_only()
async def appeal_case_subcommand(ctx: commands.Context, case_id: int, *, statement: str):
    """Open a private staff review for a warning or timeout case."""
    if not isinstance(ctx.author, discord.Member):
        return
    if not 10 <= len(statement.strip()) <= 1000:
        await ctx.send(embed=embed("📝 Add a little more detail", "A case appeal needs between 10 and 1,000 characters so staff can review it.", WARNING), delete_after=8)
        return
    conn = db_connect()
    case = conn.execute(
        "SELECT * FROM moderation_cases WHERE guild_id=? AND case_id=?",
        (ctx.guild.id, case_id),
    ).fetchone()
    conn.close()
    if not case or case["target_id"] != ctx.author.id or case["action"] not in {"warn", "timeout"} or case["event_type"] != "moderation_case":
        await ctx.send(embed=embed("🔎 This case cannot be appealed here", "Only the member named in a warning or timeout case can open a private appeal. Ban appeals use the configured appeal server; jail cases use the jail appeal panel.", WARNING), delete_after=10)
        return

    conn = db_connect()
    try:
        cursor = conn.execute(
            "INSERT INTO case_appeals(guild_id,case_id,user_id,created_at) VALUES(?,?,?,?)",
            (ctx.guild.id, case_id, ctx.author.id, utc_ts()),
        )
        appeal_id = int(cursor.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        await ctx.send(embed=embed("📝 Appeal already exists", f"Case `#{case_id}` already has an appeal on record. Please continue in your existing private ticket.", INFO), delete_after=8)
        return
    conn.close()

    channel = await create_ticket(
        ctx.guild,
        ctx.author,
        "Case appeal",
        [
            ("Case", f"#{case_id} · {case['action']} · {case['reason']}"),
            ("Member appeal", statement.strip()),
        ],
    )
    if not channel:
        conn = db_connect()
        conn.execute("DELETE FROM case_appeals WHERE appeal_id=? AND status='pending'", (appeal_id,))
        conn.commit()
        conn.close()
        await ctx.send(embed=embed("❌ Appeal ticket unavailable", "Gargand could not create a private review ticket. Ask a moderator to check the ticket category and bot permissions.", DANGER), delete_after=10)
        return

    conn = db_connect()
    conn.execute("UPDATE case_appeals SET ticket_channel_id=? WHERE appeal_id=?", (channel.id, appeal_id))
    conn.commit()
    conn.close()
    appeal_record = record_moderation_case(
        ctx.guild,
        "appeal_submitted",
        ctx.author,
        ctx.author,
        f"Appeal submitted for case #{case_id}",
        outcome="pending",
        details=f"Private ticket #{channel.name}; appeal ID #{appeal_id}.",
        channel=channel,
    )
    await channel.send(
        embed=embed(
            "🧑‍⚖️ Staff case review",
            f"This private ticket is for appeal `#{appeal_id}` about case `#{case_id}`. Staff can approve or deny below. Approval removes one warning or clears the matching active timeout when it has not changed since the case was issued.",
            INFO,
        ),
        view=CaseAppealReviewView(appeal_id),
    )
    await ctx.send(embed=embed("✅ Appeal sent privately", f"Your appeal for case `#{case_id}` is now in a private staff ticket: {channel.mention}.\nReview record: `#{appeal_record}`.", SUCCESS), delete_after=12)


@bot.hybrid_command()
@admin_only()
async def autoreaction(ctx: commands.Context, channel: discord.TextChannel, emoji: str):
    # Validate emoji before saving. It can be unicode or a Discord custom emoji.
    try:
        str(emoji)
    except Exception:
        await ctx.send(embed=embed("❌ Invalid emoji", "I couldn't use that emoji.", DANGER)); return
    set_setting(ctx.guild, "autoreaction_channel_id", channel.id)
    set_setting(ctx.guild, "autoreaction_emoji", emoji)
    await ctx.send(embed=embed("✨ Auto-reaction enabled", f"Every message in {channel.mention} will receive {emoji}.", SUCCESS))


@bot.hybrid_group(name="role", invoke_without_command=True)
@admin_only()
async def role_group(ctx: commands.Context):
    await ctx.send(embed=embed("🎭 Role manager", "Use `!role give @user @role` or `!role give @everyone @role`, or `!role make Name #8A5CFF`.", EMBED_COLOR))


@role_group.command(name="give")
@admin_only()
async def role_give(ctx: commands.Context, target: str, role: discord.Role):
    if target.lower() in ("@everyone", "everyone"):
        count = 0
        for member in ctx.guild.members:
            try:
                await member.add_roles(role, reason=f"Bulk role by {ctx.author}")
                count += 1
            except discord.HTTPException:
                pass
        await ctx.send(embed=embed("🎭 Role applied", f"Added {role.mention} to **{count}** members.", SUCCESS))
        return
    try:
        member = await commands.MemberConverter().convert(ctx, target)
    except commands.BadArgument:
        await ctx.send(embed=embed("❌ Member not found", "Mention a member or use their user ID.", DANGER)); return
    try:
        await member.add_roles(role, reason=f"Role by {ctx.author}")
        await ctx.send(embed=embed("🎭 Role applied", f"Added {role.mention} to {member.mention}.", SUCCESS))
    except discord.Forbidden:
        await ctx.send(embed=embed("❌ Failed", "That role is above my bot role.", DANGER))


@role_group.command(name="make")
@admin_only()
async def role_make(ctx: commands.Context, name: str, color: str = "#ffffff"):
    color = color.replace("#", "")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        await ctx.send(embed=embed("❌ Invalid color", "Use a hex color like `#8A5CFF`.", DANGER)); return
    role = await ctx.guild.create_role(name=name[:100], color=discord.Color(int(color, 16)), reason=f"Role created by {ctx.author}")
    await ctx.send(embed=embed("✨ Role created", f"Created {role.mention} with color `#{color.upper()}`.", SUCCESS))


@bot.hybrid_group(name="reset", invoke_without_command=True)
@admin_only()
async def reset_group(ctx: commands.Context):
    await ctx.send(embed=embed("🔄 Reset", "Use `!reset invites @user` or `!reset invites @everyone`.", INFO))


@reset_group.command(name="invites")
@admin_only()
async def reset_invites(ctx: commands.Context, target: str):
    conn = db_connect()
    if target.lower() in ("@everyone", "everyone"):
        conn.execute("DELETE FROM invite_members WHERE guild_id=?", (ctx.guild.id,))
        conn.commit(); conn.close()
        await ctx.send(embed=embed("🔄 Invites reset", "All invite tracking for this server has been reset.", SUCCESS)); return
    try:
        member = await commands.MemberConverter().convert(ctx, target)
    except commands.BadArgument:
        await ctx.send(embed=embed("❌ Member not found", "Mention a member or use their ID.", DANGER)); conn.close(); return
    conn.execute("DELETE FROM invite_members WHERE guild_id=? AND inviter_id=?", (ctx.guild.id, member.id))
    conn.commit(); conn.close()
    await ctx.send(embed=embed("🔄 Invites reset", f"Clean invite tracking for {member.mention} has been reset.", SUCCESS))


@bot.hybrid_command()
@commands.guild_only()
async def invites(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    counts = invite_counts(ctx.guild.id, member.id)
    e = embed(f"📊 Invite tracker — {member.display_name}", "Clean invites are 90+ day old accounts that have not left, rejoined, or been marked as J4J.", INFO)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="✅ Clean", value=str(counts["clean"]), inline=True)
    e.add_field(name="🧪 Fake", value=str(counts["fake"]), inline=True)
    e.add_field(name="🚪 Left", value=str(counts["left"]), inline=True)
    e.add_field(name="🔁 Rejoined", value=str(counts["rejoin"]), inline=True)
    e.add_field(name="🤝 J4J", value=str(counts["j4j"]), inline=True)
    await ctx.send(embed=e)


@bot.hybrid_command()
@commands.guild_only()
async def invited(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    ids = invited_people(ctx.guild.id, member.id)
    lines = []
    for uid in ids[:50]:
        m = ctx.guild.get_member(uid)
        lines.append(f"• {m.mention if m else f'<@{uid}>'}")
    if not lines: lines = ["No clean invites tracked yet."]
    e = embed(f"👥 Invited by {member.display_name}", "\n".join(lines), INFO)
    e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_command()
@commands.guild_only()
async def inviter(ctx: commands.Context, member: discord.Member):
    row = inviter_of(ctx.guild.id, member.id)
    e = embed(f"🕵️ Inviter — {member.display_name}", "The tracker could not identify an inviter." if not row or not row["inviter_id"] else f"**Invited by:** <@{row['inviter_id']}>\n**Category:** `{row['category']}`", INFO)
    e.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=e)


@bot.hybrid_group(name="j4j", invoke_without_command=True)
@admin_only()
async def j4j_group(ctx: commands.Context):
    cfg = get_settings(ctx.guild.id)
    await ctx.send(embed=embed("🤝 J4J settings", f"J4J system: **{'ON' if cfg.get('j4j') else 'OFF'}**\nJ4J DM: **{'ON' if cfg.get('j4j_dm') else 'OFF'}**\n\nUse `!j4j allowed` to toggle whether J4J entries are allowed. Use `!j4j dm on` or `!j4j dm off` for the DM check.", INFO))


@j4j_group.command(name="allowed")
@admin_only()
async def j4j_allowed(ctx: commands.Context):
    enabled = not bool(setting(ctx.guild, "j4j", False))
    set_setting(ctx.guild, "j4j", enabled)
    await ctx.send(embed=embed("🤝 J4J allowed", f"J4J entries are now **{'allowed' if enabled else 'not allowed'}**.", SUCCESS if enabled else WARNING))


@j4j_group.command(name="dm")
@admin_only()
async def j4j_dm(ctx: commands.Context, state: str):
    state = state.lower()
    if state not in ("on", "off"):
        await ctx.send(embed=embed("❌ Invalid state", "Use `on` or `off`.", WARNING)); return
    set_setting(ctx.guild, "j4j_dm", state == "on")
    set_setting(ctx.guild, "j4j", state == "on" or bool(setting(ctx.guild, "j4j", False)))
    await ctx.send(embed=embed("🤝 J4J DM", f"J4J welcome DMs are now **{state.upper()}**.", SUCCESS if state == "on" else WARNING))


@bot.hybrid_group(name="giveaway", invoke_without_command=True)
@staff_only()
async def giveaway(ctx: commands.Context, duration: str = None, winners: int = None, *, prize: str = None):
    if not duration or not winners or not prize:
        await ctx.send(embed=embed("🎁 Giveaway", "Use `!giveaway <duration> <winners> <prize> [| image_url]`.", INFO)); return
    try: seconds = parse_duration(duration)
    except ValueError as e:
        await ctx.send(embed=embed("❌ Invalid duration", str(e), DANGER)); return
    if winners < 1 or winners > 20:
        await ctx.send(embed=embed("❌ Invalid winners", "Winners must be 1–20.", DANGER)); return
    image_url = None
    parts = prize.split("|", 1)
    if len(parts) == 2:
        prize = parts[0].strip(); image_url = parts[1].strip()
    ends = utc_ts() + seconds
    e = embed("🎁 GIVEAWAY", f"## 🎉 {prize}\n\n⏳ Ends <t:{ends}:R>\n🏆 Winners: **{winners}**\n\nReact with 🎉 to enter!", EMBED_COLOR)
    if image_url: e.set_image(url=image_url)
    e.set_footer(text="Gargand Giveaway • Good luck! 🍀")
    msg = await ctx.send(embed=e)
    await msg.add_reaction("🎉")
    conn = db_connect(); conn.execute("INSERT INTO giveaways(guild_id,channel_id,message_id,prize,winners,ends_at,ended) VALUES(?,?,?,?,?,?,0)", (ctx.guild.id, ctx.channel.id, msg.id, prize, winners, ends)); conn.commit(); conn.close()


@giveaway.command(name="reroll")
@staff_only()
async def giveaway_reroll(ctx: commands.Context, message_id: int):
    conn = db_connect(); row = conn.execute("SELECT * FROM giveaways WHERE guild_id=? AND message_id=?", (ctx.guild.id, message_id)).fetchone(); conn.close()
    if not row:
        await ctx.send(embed=embed("❌ Giveaway not found", "That message is not a stored giveaway.", DANGER)); return
    channel = ctx.guild.get_channel(row["channel_id"])
    try: msg = await channel.fetch_message(message_id)
    except Exception:
        await ctx.send(embed=embed("❌ Message not found", "I cannot access the giveaway message.", DANGER)); return
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    if not reaction:
        await ctx.send(embed=embed("❌ No entries", "There is no 🎉 reaction on the giveaway.", DANGER)); return
    users = [u async for u in reaction.users() if not u.bot]
    if not users:
        await ctx.send(embed=embed("❌ No eligible entries", "Nobody entered this giveaway.", DANGER)); return
    winner = random.choice(users)
    await ctx.send(embed=embed("🎉 Giveaway rerolled", f"New winner: {winner.mention}\nPrize: **{row['prize']}**", SUCCESS))


@giveaway.command(name="end")
@staff_only()
async def giveaway_end(ctx: commands.Context, message_id: int):
    await finish_giveaway(ctx.guild, message_id, manual=True)
    await ctx.send(embed=embed("🛑 Giveaway ended", f"Giveaway `{message_id}` has been ended.", WARNING))


@bot.hybrid_command()
@staff_only()
async def stick(ctx: commands.Context, *, message: str):
    old = get_settings(ctx.guild.id)
    # Delete command message and create the sticky message.
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    sent = await ctx.channel.send(embed=embed("📌 Sticky", message, INFO))
    conn = db_connect(); conn.execute("INSERT OR REPLACE INTO sticks(guild_id,channel_id,content,message_id) VALUES(?,?,?,?)", (ctx.guild.id, ctx.channel.id, message, sent.id)); conn.commit(); conn.close()


@bot.hybrid_command()
@staff_only()
async def unstick(ctx: commands.Context):
    conn = db_connect(); row = conn.execute("SELECT message_id FROM sticks WHERE guild_id=? AND channel_id=?", (ctx.guild.id, ctx.channel.id)).fetchone(); conn.execute("DELETE FROM sticks WHERE guild_id=? AND channel_id=?", (ctx.guild.id, ctx.channel.id)); conn.commit(); conn.close()
    if row:
        try: await ctx.channel.get_partial_message(row["message_id"]).delete()
        except Exception: pass
    await ctx.send(embed=embed("📌 Sticky removed", "This channel no longer has a sticky message.", SUCCESS), delete_after=4)


@bot.hybrid_command()
@staff_only()
async def proof(ctx: commands.Context, *, action: str):
    if action.lower() != "please":
        await ctx.send(embed=embed("📸 Proof", "Use `!proof please` inside a claim ticket after the user has posted a proof image.", INFO)); return
    proof_channel_id = setting(ctx.guild, "proof_channel_id")
    proof_channel = ctx.guild.get_channel(proof_channel_id) if proof_channel_id else None
    if not isinstance(proof_channel, discord.TextChannel):
        await ctx.send(embed=embed("❌ Proof channel not configured", "Use setup to select one.", DANGER)); return
    found = None
    async for msg in ctx.channel.history(limit=50):
        if msg.attachments:
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    found = (msg, att); break
        if found: break
    if not found:
        await ctx.send(embed=embed("📸 No image found", "Ask the user to post a photo/screenshot in this ticket first.", WARNING)); return
    msg, att = found
    e = embed("📸 New claim proof", f"Ticket: {ctx.channel.mention}\nSubmitted by: {msg.author.mention}", SUCCESS)
    e.set_image(url=att.url)
    await proof_channel.send(embed=e)
    await ctx.send(embed=embed("✅ Proof posted", f"Sent the proof to {proof_channel.mention}.", SUCCESS), delete_after=5)


# -----------------------------
# Gambling commands
# -----------------------------

async def validate_bet(ctx: commands.Context, bet: int):
    if setting(ctx.guild, "gamble_channel_id") and ctx.channel.id != setting(ctx.guild, "gamble_channel_id"):
        await ctx.send(embed=embed("🎰 Gambling channel only", f"Use {ctx.guild.get_channel(setting(ctx.guild,'gamble_channel_id')).mention}.", WARNING), delete_after=5)
        return False
    if bet <= 0:
        await ctx.send(embed=embed("❌ Invalid bet", "Bet must be greater than 0.", DANGER)); return False
    coins = get_coins(ctx.guild.id, ctx.author.id)
    if coins < bet:
        await ctx.send(embed=embed("💸 Not enough coins", f"You have **{coins:,}** coins.", DANGER)); return False
    return True


@bot.command(name="daily")
@commands.guild_only()
async def daily(ctx: commands.Context):
    conn = db_connect(); conn.execute("INSERT OR IGNORE INTO gambling(guild_id,user_id,coins,last_daily) VALUES(?,?,0,0)", (ctx.guild.id, ctx.author.id))
    row = conn.execute("SELECT last_daily FROM gambling WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.author.id)).fetchone()
    last = row["last_daily"] if row else 0
    now = utc_ts()
    if now - last < 86400:
        conn.close(); await ctx.send(embed=embed("🕛 Daily", f"You already claimed it. Come back <t:{last+86400}:R>.", WARNING)); return
    conn.execute("UPDATE gambling SET coins=coins+500,last_daily=? WHERE guild_id=? AND user_id=?", (now, ctx.guild.id, ctx.author.id)); conn.commit(); conn.close()
    await ctx.send(embed=embed("💰 Daily claimed", "You received **500 coins**. ✨", SUCCESS))


@bot.command()
@commands.guild_only()
async def balance(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    await ctx.send(embed=embed("💰 Wallet", f"{member.mention} has **{get_coins(ctx.guild.id, member.id):,}** coins.", INFO))


@bot.command(name="coinflip")
@commands.guild_only()
async def coinflip(ctx: commands.Context, bet: int, choice: str):
    if not await validate_bet(ctx, bet): return
    choice = choice.lower()
    if choice not in ("heads", "tails"):
        await ctx.send(embed=embed("🪙 Choice", "Choose `heads` or `tails`.", WARNING)); return
    msg = await ctx.send(embed=embed("🪙 Coinflip", "Flipping... ✨", EMBED_COLOR))
    for frame in ("🪙 •", "• 🪙", "🪙 •", "• 🪙"):
        await asyncio.sleep(0.35); await msg.edit(embed=embed("🪙 Coinflip", frame, EMBED_COLOR))
    result = random.choice(("heads", "tails"))
    win = result == choice
    delta = bet if win else -bet
    bal = await add_coins(ctx.guild.id, ctx.author.id, delta)
    await msg.edit(embed=embed("🪙 Coinflip", f"Landed on **{result}**!\n\n{'🎉 You won!' if win else '💥 You lost!'}\nBalance: **{bal:,}**", SUCCESS if win else DANGER))


@bot.command(name="highlow")
@commands.guild_only()
async def highlow(ctx: commands.Context, bet: int):
    if not await validate_bet(ctx, bet): return
    current = random.randint(1, 13)
    msg = await ctx.send(embed=embed("🎴 High / Low", f"Current card: **{current}**\n\nPick by editing your next message isn't interactive here, so the bot resolves automatically: higher or lower! 🎲", EMBED_COLOR))
    await asyncio.sleep(1)
    next_card = random.randint(1, 13)
    win = next_card != current and random.random() < 0.5
    if next_card == current: win = False
    delta = bet if win else -bet
    bal = await add_coins(ctx.guild.id, ctx.author.id, delta)
    await msg.edit(embed=embed("🎴 High / Low", f"{current} ➜ **{next_card}**\n\n{'🎉 You won!' if win else '💥 You lost!'}\nBalance: **{bal:,}**", SUCCESS if win else DANGER))


@bot.command(name="blackjack")
@commands.guild_only()
async def blackjack(ctx: commands.Context, bet: int):
    if not await validate_bet(ctx, bet): return
    deck = [r for r in range(2, 11)] * 4 + [10] * 12 + [11] * 4
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    def score(cards):
        s = sum(cards)
        aces = cards.count(11)
        while s > 21 and aces:
            s -= 10; aces -= 1
        return s
    msg = await ctx.send(embed=embed("♠️ Blackjack", "Dealing cards... 🃏", EMBED_COLOR))
    await asyncio.sleep(0.7)
    while score(player) < 17:
        player.append(deck.pop())
    while score(dealer) < 17:
        dealer.append(deck.pop())
    ps, ds = score(player), score(dealer)
    win = ps <= 21 and (ds > 21 or ps > ds)
    push = ps == ds and ps <= 21
    delta = bet if win else 0 if push else -bet
    bal = await add_coins(ctx.guild.id, ctx.author.id, delta)
    result = "🤝 Push" if push else "🎉 Blackjack won" if win else "💥 Dealer wins"
    await msg.edit(embed=embed("♠️ Blackjack", f"**You:** {player} → **{ps}**\n**Dealer:** {dealer} → **{ds}**\n\n{result}\nBalance: **{bal:,}**", SUCCESS if win else WARNING if push else DANGER))


@bot.command(name="roulette")
@commands.guild_only()
async def roulette(ctx: commands.Context, bet: int, pick: str):
    if not await validate_bet(ctx, bet): return
    n = random.randint(0, 36)
    red_numbers = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = "green" if n == 0 else "red" if n in red_numbers else "black"
    msg = await ctx.send(embed=embed("🎡 Roulette", "Spinning... 🔴 ⚫ 🟢", EMBED_COLOR))
    for _ in range(5):
        await asyncio.sleep(0.25); await msg.edit(embed=embed("🎡 Roulette", f"{random.randint(0,36):02d} • spinning • 🎡", EMBED_COLOR))
    pick_l = pick.lower()
    win = False
    payout = 0
    if pick_l in ("red", "black", "green"):
        win = pick_l == color
        payout = bet if win else -bet
    elif pick_l.isdigit() and 0 <= int(pick_l) <= 36:
        win = int(pick_l) == n
        payout = bet * 35 if win else -bet
    else:
        await msg.edit(embed=embed("🎡 Roulette", "Choose `red`, `black`, `green`, or a number 0–36.", WARNING)); return
    bal = await add_coins(ctx.guild.id, ctx.author.id, payout)
    await msg.edit(embed=embed("🎡 Roulette", f"The wheel landed on **{n} — {color}**.\n\n{'🎉 You won!' if win else '💥 You lost!'}\nBalance: **{bal:,}**", SUCCESS if win else DANGER))


# -----------------------------
# Background loops
# -----------------------------

@tasks.loop(seconds=5)
async def giveaway_watcher():
    conn = db_connect(); rows = conn.execute("SELECT * FROM giveaways WHERE ended=0 AND ends_at<=?", (utc_ts(),)).fetchall(); conn.close()
    for row in rows:
        guild = bot.get_guild(row["guild_id"])
        if guild:
            await finish_giveaway(guild, row["message_id"])


async def finish_giveaway(guild: discord.Guild, message_id: int, manual=False):
    conn = db_connect(); row = conn.execute("SELECT * FROM giveaways WHERE guild_id=? AND message_id=?", (guild.id, message_id)).fetchone()
    if not row or row["ended"]: conn.close(); return
    conn.execute("UPDATE giveaways SET ended=1 WHERE guild_id=? AND message_id=?", (guild.id, message_id)); conn.commit(); conn.close()
    channel = guild.get_channel(row["channel_id"])
    if not isinstance(channel, discord.TextChannel): return
    try: msg = await channel.fetch_message(message_id)
    except Exception: return
    reaction = discord.utils.get(msg.reactions, emoji="🎉")
    users = [u async for u in reaction.users() if not u.bot] if reaction else []
    winners = random.sample(users, min(row["winners"], len(users))) if users else []
    names = ", ".join(u.mention for u in winners) if winners else "Nobody entered."
    await channel.send(embed=embed("🎁 Giveaway ended", f"**Prize:** {row['prize']}\n**Winner(s):** {names}", SUCCESS if winners else WARNING))
    e = embed("🎁 GIVEAWAY ENDED", f"**Prize:** {row['prize']}\n**Winner(s):** {names}", DARK)
    try: await msg.edit(embed=e)
    except discord.HTTPException: pass


# -----------------------------
# Events
# -----------------------------

PRESENCE_INDEX = 0


@tasks.loop(seconds=35)
async def nightfall_presence():
    await bot.change_presence(activity=discord.Game(name="!setup to start!"), status=discord.Status.online)

@bot.event
async def on_command(ctx: commands.Context):
    command = getattr(ctx, "command", None)
    if not command:
        return
    name = getattr(command, "qualified_name", "") or getattr(command, "name", "")
    asyncio.create_task(send_telemetry(
        "command_used",
        guild=ctx.guild,
        user=ctx.author,
        command_name=name,
        metadata={"command": name, "invocation_type": "prefix"},
    ))

@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    name = getattr(command, "qualified_name", "") or getattr(command, "name", "")
    asyncio.create_task(send_telemetry(
        "command_used",
        guild=interaction.guild,
        user=interaction.user,
        command_name=name,
        metadata={"command": name, "invocation_type": "slash"},
    ))

@bot.event
async def on_ready():
    # Remove stale server-specific command overrides before syncing the current
    # global hybrid-command definitions. Old guild overrides can cause
    # CommandSignatureMismatch even when the global tree is up to date.
    try:
        for connected_guild in bot.guilds:
            guild_ref = discord.Object(id=connected_guild.id)
            bot.tree.clear_commands(guild=guild_ref)
            await bot.tree.sync(guild=guild_ref)
        await bot.tree.sync()
    except Exception as exc:
        print(f"Gargand slash-command sync failed: {exc!r}")
    global bridge_task, VERIFICATION_VIEW_REGISTERED, CASE_APPEAL_VIEWS_REGISTERED
    db_init()
    if not VERIFICATION_VIEW_REGISTERED:
        try:
            # The generic persistent view routes old panels by interaction.guild after a restart.
            bot.add_view(VerificationView(0))
            VERIFICATION_VIEW_REGISTERED = True
        except Exception as exc:
            print(f"Gargand verification panel restore failed: {type(exc).__name__}")
    if not CASE_APPEAL_VIEWS_REGISTERED:
        try:
            conn = db_connect()
            # A restart during a review should make the appeal available again.
            conn.execute("UPDATE case_appeals SET status='pending' WHERE status='reviewing'")
            conn.commit()
            pending_appeals = conn.execute("SELECT appeal_id FROM case_appeals WHERE status='pending'").fetchall()
            conn.close()
            for appeal in pending_appeals:
                bot.add_view(CaseAppealReviewView(int(appeal["appeal_id"])))
            bot.add_view(TicketControlView())
            CASE_APPEAL_VIEWS_REGISTERED = True
        except Exception as exc:
            print(f"Gargand case appeal buttons could not be restored: {type(exc).__name__}")
    for guild in bot.guilds:
        await cache_invites(guild)
        await ensure_log_channel(guild)
        if setting(guild, "jail_enabled", False):
            try:
                jail_role, _, _, jail_appeals = await configure_jail_system(guild)
                # Restore active jail members after a restart.
                conn = db_connect()
                cases = conn.execute("SELECT user_id FROM jails WHERE guild_id=? AND active=1", (guild.id,)).fetchall()
                conn.close()
                for case in cases:
                    member = guild.get_member(case["user_id"])
                    if member and jail_role not in member.roles:
                        try:
                            await member.add_roles(jail_role, reason="Gargand restore active jail")
                        except discord.HTTPException:
                            pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"Jail restore failed for {guild.id}: {exc!r}")
        # Rebind/refresh any saved setup dashboard after a restart so its
        # buttons are not left pointing at an expired in-memory View.
        try:
            await refresh_setup_dashboard(guild)
        except Exception as exc:
            print(f"Setup dashboard refresh failed for {guild.id}: {exc!r}")
        await asyncio.sleep(0.25)
    if not giveaway_watcher.is_running():
        giveaway_watcher.start()
    if not raid_lockdown_restore_watcher.is_running():
        raid_lockdown_restore_watcher.start()
    if not nightfall_presence.is_running():
        nightfall_presence.start()
    if GARGAND_WEBSITE_URL and GARGAND_BRIDGE_SECRET and (bridge_task is None or bridge_task.done()):
        bridge_task = asyncio.create_task(nightfall_website_bridge(), name="nightfall-website-bridge")
    print(f"Logged in as {bot.user} ({bot.user.id}) on {len(bot.guilds)} server(s)")


@bot.event
async def on_guild_join(guild: discord.Guild):
    db_init()
    await cache_invites(guild)
    await ensure_log_channel(guild)
    try:
        await guild.me.edit(nick="Gargand ✦")
    except Exception:
        pass


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild = channel.guild
    active_lockdown_until = int(setting(guild, "raid_lockdown_until", 0) or 0)
    if active_lockdown_until > utc_ts() and isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        if not await apply_raid_lockdown_to_channel(guild, channel, active_lockdown_until, "channel created during active lockdown"):
            await log_action(guild, "⚠️ New channel outside lockdown", f"Gargand could not pause @everyone posting in **#{channel.name}**. Check Manage Channels and role position.", WARNING)
    if setting(guild, "anti_raid", False):
        try:
            ow = channel.overwrites_for(guild.default_role)
            ow.use_external_apps = False
            await channel.set_permissions(guild.default_role, overwrite=ow, reason="Gargand anti-raid new-channel protection")
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass
    if setting(guild, "jail_enabled", False):
        jail_role_id = setting(guild, "jail_role_id")
        jail_role = guild.get_role(jail_role_id) if jail_role_id else None
        if jail_role:
            await apply_jail_role_permissions(guild, jail_role)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    await detect_destructive_action(guild, "channel_delete", channel, channel_recovery_snapshot(channel))


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    await detect_destructive_action(guild, "role_delete", role, role_recovery_snapshot(role))


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    if not setting(after.guild, "anti_nuke", False):
        return
    before_snapshot = channel_recovery_snapshot(before)
    after_snapshot = channel_recovery_snapshot(after)
    if not before_snapshot or not after_snapshot:
        return
    before_snapshot["changed_fields"] = recovery_changed_fields(before_snapshot, after_snapshot)
    if not before_snapshot["changed_fields"]:
        return
    await detect_destructive_action(after.guild, "channel_update", after, before_snapshot)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    if not setting(after.guild, "anti_nuke", False):
        return
    before_snapshot = role_recovery_snapshot(before)
    after_snapshot = role_recovery_snapshot(after)
    before_snapshot["changed_fields"] = recovery_changed_fields(before_snapshot, after_snapshot)
    if not before_snapshot["changed_fields"]:
        return
    await detect_destructive_action(after.guild, "role_update", after, before_snapshot)


ANTI_NUKE_ACTIONS = {
    "channel_delete": discord.AuditLogAction.channel_delete,
    "role_delete": discord.AuditLogAction.role_delete,
    "channel_update": discord.AuditLogAction.channel_update,
    "role_update": discord.AuditLogAction.role_update,
}
ANTI_NUKE_THRESHOLDS = {
    "channel_delete": ("anti_nuke_threshold_channel_delete", 3),
    "role_delete": ("anti_nuke_threshold_role_delete", 3),
    "channel_update": ("anti_nuke_threshold_channel_update", 4),
    "role_update": ("anti_nuke_threshold_role_update", 4),
}


async def detect_destructive_action(guild: discord.Guild, action_key: str, target, snapshot: dict[str, Any] | None):
    monitoring = bool(setting(guild, "anti_nuke", False))
    if action_key.endswith("_update") and not monitoring:
        return
    audit_error = False
    actor = None
    try:
        target_id = int(getattr(target, "id", 0) or 0)
        # Discord can publish audit entries a moment after the gateway event.
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(0.4)
            audit = [entry async for entry in guild.audit_logs(limit=8, action=ANTI_NUKE_ACTIONS[action_key])]
            now_utc = datetime.now(timezone.utc).timestamp()
            for entry in audit:
                entry_target_id = int(getattr(getattr(entry, "target", None), "id", 0) or 0)
                created_at = getattr(entry, "created_at", None)
                age = abs(now_utc - created_at.timestamp()) if created_at else 999
                if entry_target_id == target_id and age <= 25:
                    actor = entry.user
                    break
            if actor:
                break
    except (discord.Forbidden, discord.HTTPException):
        audit_error = True

    own_bot_edit = bool(action_key.endswith("_update") and actor and bot.user and actor.id == bot.user.id)
    if own_bot_edit:
        return

    window = bounded_setting_int(guild, "anti_nuke_window_seconds", 10, 5, 60)
    threshold_key, default_threshold = ANTI_NUKE_THRESHOLDS[action_key]
    threshold = bounded_setting_int(guild, threshold_key, default_threshold, 2, 10)
    trusted_role_ids = setting(guild, "anti_nuke_trusted_role_ids", [])
    if not isinstance(trusted_role_ids, (list, tuple, set)):
        trusted_role_ids = []
    trusted_role_ids = {int(value) for value in trusted_role_ids if str(value).isdigit() and int(value) != guild.id}
    member = guild.get_member(actor.id) if actor else None
    trusted = bool(actor and (actor.id == guild.owner_id or (member and any(role.id in trusted_role_ids for role in member.roles))))
    now = time.monotonic()
    count = 0
    threshold_reached = False
    if actor and monitoring and not trusted:
        key = (guild.id, actor.id, action_key)
        q = anti_nuke_events[key]
        while q and now - q[0] > window:
            q.popleft()
        if not q:
            anti_nuke_escalated.discard(key)
        q.append(now)
        count = len(q)
        threshold_reached = count >= threshold and key not in anti_nuke_escalated
        if threshold_reached:
            anti_nuke_escalated.add(key)

    action_label = action_key.replace("_", " ")
    details = []
    if audit_error:
        details.append("Discord audit log access failed; actor could not be verified and no automatic response was taken.")
    elif actor:
        details.append(f"Audit log actor: {actor} ({actor.id}).")
    else:
        details.append("No matching recent audit log entry was available for the changed object.")
    if monitoring and actor and trusted:
        details.append("Actor matches a trusted owner/role; threshold counting and automatic escalation were skipped.")
    elif monitoring and actor:
        details.append(f"Observed {count} {action_label} event(s) in {window}s; threshold is {threshold}.")
    elif not monitoring:
        details.append("Anti-nuke monitoring is disabled; this operation was logged for review.")
    if snapshot and snapshot.get("changed_fields"):
        changed = ", ".join(str(item).replace("_", " ") for item in snapshot["changed_fields"])
        details.append(f"Changed fields: {changed}.")
    if threshold_reached:
        details.append("Configured threshold reached; staff were alerted before any optional response.")

    # Count normal edits for burst detection without creating a case or a log
    # message for every routine change. Unknown and trusted actors do not escalate.
    if action_key.endswith("_update") and monitoring and not threshold_reached:
        return

    case_id = record_moderation_case(
        guild,
        action_key,
        actor or bot.user,
        target,
        "Destructive server change detected; admin review recommended",
        outcome="threshold" if threshold_reached else "watch",
        details=" ".join(details),
        event_type="security_incident",
    )
    if snapshot:
        save_recovery_snapshot(case_id, guild.id, "role" if action_key.startswith("role_") else "channel", int(target.id), snapshot)
    await log_action(
        guild,
        "🚨 Anti-nuke security review" if monitoring else "🧾 Recovery snapshot saved",
        f"**Action:** {action_label}\n**Object:** {getattr(target, 'name', 'unknown')} (`{target.id}`)\n**Actor:** {actor.mention if actor else 'Unknown (audit log unavailable)'}\n**Case:** `#{case_id}`\n" + (f"**Burst:** {count}/{threshold} in {window}s" if monitoring and actor and not trusted else "A recovery snapshot is available to administrators with `!recover <case id>`.") + "\nReview the incident in the website dashboard.",
        WARNING if not threshold_reached else DANGER,
    )

    if not threshold_reached:
        return

    responses = []
    if setting(guild, "anti_nuke_auto_lockdown", False):
        locked, failed, restore_at = await start_raid_lockdown(
            guild, 10, actor=bot.user, reason=f"anti-nuke threshold: {action_label} by {actor}",
        )
        responses.append(f"temporary lockdown: {locked} channel(s), {failed} failed, expires <t:{restore_at}:R>")
    if setting(guild, "anti_nuke_auto_ban", False) and not trusted:
        if member and member.id != guild.owner_id:
            try:
                await guild.ban(actor, reason=f"Gargand anti-nuke threshold reached for {action_label}")
                responses.append("actor banned")
            except (discord.Forbidden, discord.HTTPException) as exc:
                responses.append(f"ban failed ({type(exc).__name__})")
        else:
            responses.append("automatic ban skipped because the actor could not be safely resolved as a member")
    if not responses:
        responses.append("staff alert only; automatic lockdown and ban are disabled")
    await log_action(
        guild,
        "🚨 Anti-nuke threshold response",
        f"**Case:** `#{case_id}`\n**Actor:** {actor.mention}\n" + "\n".join(f"• {item}" for item in responses),
        DANGER,
    )


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    # Aggregate lifecycle telemetry contains no message content or invite token.
    asyncio.create_task(send_telemetry(
        "member_joined", guild=guild, user=member,
        metadata={"is_bot": bool(member.bot)},
    ))
    join_count = record_join_speed(guild.id)
    if not member.bot:
        risk_score, risk_reasons = assess_join_risk(member, join_count)
        save_verification_risk(guild.id, member.id, risk_score, risk_reasons)
        if risk_score >= 3 and any("account created" in item for item in risk_reasons):
            case_id = record_moderation_case(
                guild,
                "verification_risk",
                bot.user,
                member,
                "Automated risk review; no action taken",
                outcome="review",
                details=f"Risk score {risk_score}; signals: {', '.join(risk_reasons)}",
                event_type="security_incident",
            )
            await log_action(
                guild,
                "🔎 Join risk review",
                f"{member.mention} is held behind the normal verification gate. Gargand recorded **{len(risk_reasons)}** risk signal(s) for staff review.\n**Case:** `#{case_id}`",
                WARNING,
            )

    if setting(guild, "anti_raid", False) and join_count >= 5:
        now = time.monotonic()
        if now - raid_watch_last_alert.get(guild.id, 0) >= 120:
            raid_watch_last_alert[guild.id] = now
            case_id = record_moderation_case(
                guild,
                "join_surge_watch",
                bot.user,
                member,
                "Join surge watch threshold reached",
                outcome="watch",
                details=f"{join_count} members joined within 30 seconds; no channels were locked at this stage.",
                event_type="security_incident",
            )
            await log_action(
                guild,
                "👀 Join surge watch",
                f"**{join_count}** members joined in 30 seconds. Gargand is watching the surge; channels remain open.\n**Case:** `#{case_id}`",
                WARNING,
            )

    active_lockdown_until = int(setting(guild, "raid_lockdown_until", 0) or 0)
    if (
        not member.bot
        and setting(guild, "anti_raid", False)
        and setting(guild, "raid_auto_lockdown", False)
        and join_count >= 12
        and active_lockdown_until <= utc_ts()
        and guild.id not in raid_lockdown_starting
    ):
        raid_lockdown_starting.add(guild.id)
        asyncio.create_task(apply_automatic_raid_lockdown(guild, member, join_count))

    inviter = await find_inviter(guild)
    await cache_invites(guild)
    conn = db_connect()
    old = conn.execute("SELECT category, left_at FROM invite_members WHERE guild_id=? AND invited_user_id=?", (guild.id, member.id)).fetchone()
    was_left = bool(old and old["left_at"])
    cat = invite_category(member, inviter.id if inviter else None, rejoin=was_left)
    conn.execute("INSERT OR REPLACE INTO invite_members(guild_id,invited_user_id,inviter_id,invite_code,category,joined_at,left_at,rejoined,j4j) VALUES(?,?,?,?,?,?,?,?,?)", (guild.id, member.id, inviter.id if inviter else None, None, cat, utc_ts(), None, 1 if was_left else 0, old["category"] == "j4j" if old else 0))
    conn.commit(); conn.close()

    autorole_id = setting(guild, "autorole_id")
    verification_enabled = bool(setting(guild, "verification_channel_id"))
    if autorole_id and (not verification_enabled or member.bot):
        role = guild.get_role(autorole_id)
        if role:
            try: await member.add_roles(role, reason="Gargand auto role")
            except (discord.Forbidden, discord.HTTPException): pass

    if verification_enabled and not member.bot:
        try:
            unverified, _ = await configure_verification_roles(guild)
            await member.add_roles(unverified, reason="Gargand verification pending")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Could not assign the Unverified role in guild {guild.id}: {type(exc).__name__}")
            await log_action(guild, "⚠️ Verification setup issue", f"Could not give {member.mention} the Unverified role. Check Gargand’s Manage Roles permission and role order.", WARNING)
    welcome_id = setting(guild, "welcome_channel_id")
    if welcome_id:
        ch = guild.get_channel(welcome_id)
        if isinstance(ch, discord.TextChannel):
            inviter_text = inviter.mention if inviter else "OAuth2 / unknown"
            e = embed("👋 Welcome!", f"{member.mention} has joined the gang!\n\n**Invited by:** {inviter_text}\n**We are now:** {guild.member_count}\n**Inviter total:** {invite_counts(guild.id, inviter.id)['clean'] if inviter else 0} clean", SUCCESS)
            file = await make_member_card_with_avatar(member, "WELCOME! ✨", f"Invited by {str(inviter) if inviter else 'OAuth2 / unknown'} • {guild.member_count} members")
            e.set_image(url="attachment://member_card.png")
            await ch.send(embed=e, file=file)

    if setting(guild, "j4j", False) and setting(guild, "j4j_dm", True):
        task = asyncio.create_task(j4j_timeout_task(guild.id, member.id))
        j4j_tasks[member.id] = task
        await safe_dm(member, embed_obj=embed("🤝 Welcome!", "Hi! Welcome to our server 💜\n\n**Quick question:** did you join from a J4J (Join For Join)?\n\nChoose an answer below.", INFO), view=J4JView(guild.id, member.id))


j4j_tasks: dict[int, asyncio.Task] = {}


async def j4j_timeout_task(guild_id: int, user_id: int):
    await asyncio.sleep(3600)
    task = j4j_tasks.pop(user_id, None)
    guild = bot.get_guild(guild_id)
    if not guild: return
    conn = db_connect(); row = conn.execute("SELECT j4j FROM invite_members WHERE guild_id=? AND invited_user_id=?", (guild_id, user_id)).fetchone(); conn.close()
    if row and not row["j4j"]:
        member = guild.get_member(user_id)
        if member:
            try:
                await guild.ban(member, reason="J4J system: no response within 1 hour")
                await log_action(guild, "⏰ J4J timeout", f"Banned <@{user_id}> after no J4J response for 1 hour.", DANGER)
            except discord.Forbidden:
                pass


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    asyncio.create_task(send_telemetry(
        "member_left", guild=guild, user=member,
        metadata={"is_bot": bool(member.bot)},
    ))
    conn = db_connect(); conn.execute("UPDATE invite_members SET category='left',left_at=? WHERE guild_id=? AND invited_user_id=? AND category!='j4j'", (utc_ts(), guild.id, member.id)); conn.commit(); conn.close()
    leave_id = setting(guild, "leave_channel_id")
    if leave_id:
        ch = guild.get_channel(leave_id)
        if isinstance(ch, discord.TextChannel):
            row = inviter_of(guild.id, member.id)
            inviter = guild.get_member(row["inviter_id"]) if row and row["inviter_id"] else None
            inviter_text = inviter.mention if inviter else "OAuth2 / unknown"
            e = embed("🚪 Goodbye...", f"{member.mention} left the gang...\n\n**We are now:** {guild.member_count}\n**Invited by:** {inviter_text}", WARNING)
            file = await make_member_card_with_avatar(member, "GOODBYE... 👋", f"Invited by {str(inviter) if inviter else 'OAuth2 / unknown'} • {guild.member_count} members")
            e.set_image(url="attachment://member_card.png")
            try: await ch.send(embed=e, file=file)
            except discord.HTTPException: pass


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Booster role + animation notification.
    if before.premium_since is None and after.premium_since is not None:
        role_id = setting(after.guild, "booster_role_id")
        if role_id:
            role = after.guild.get_role(role_id)
            if role:
                try: await after.add_roles(role, reason="Gargand booster role")
                except discord.Forbidden: pass
        channel_id = setting(after.guild, "welcome_channel_id") or setting(after.guild, "logs_channel_id")
        channel = after.guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel):
            e = embed("🚀 SERVER BOOST!", f"**{after.mention} JUST BOOSTED OUR SERVER!** 🚀\n\n**Boosts:** {after.guild.premium_subscription_count or 0}\n**Boost level:** {after.guild.premium_tier}", SUCCESS)
            e.set_thumbnail(url=after.display_avatar.url)
            await channel.send(content="@everyone", embed=e, allowed_mentions=discord.AllowedMentions(everyone=True))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    guild = message.guild
    if not guild:
        await bot.process_commands(message)
        return

    active_prefix = await command_prefix_for(bot, message)
    is_command_message = message.content.startswith(active_prefix)

    # Optional burst-spam protection. Keep the window in memory (not message
    # contents), and never enforce it against staff or bot accounts.
    if (setting(guild, "anti_spam", False) or setting(guild, "automod_spam", False)) and not is_command_message and isinstance(message.author, discord.Member) and not is_staff(message.author):
        spam_key = (guild.id, message.author.id)
        spam_now = time.monotonic()
        spam_window = spam_message_windows[spam_key]
        spam_window.append(spam_now)
        spam_window_seconds = bounded_setting_int(guild, "automod_window_seconds", 8, 3, 60)
        spam_max_messages = bounded_setting_int(guild, "automod_max_messages", 6, 3, 20)
        while spam_window and spam_now - spam_window[0] > spam_window_seconds:
            spam_window.popleft()
        if len(spam_window) >= spam_max_messages:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            timed_out = False
            try:
                await message.author.timeout(timedelta(seconds=30), reason="Gargand anti-spam: message burst")
                timed_out = True
            except (discord.Forbidden, discord.HTTPException):
                pass
            if timed_out:
                case_id = record_moderation_case(guild, "anti_spam_timeout", bot.user, message.author, "Sent 6 messages within 8 seconds", details="Automated burst-spam protection; message content was not stored.")
                await log_automod_action(guild, "🛡️ Anti-spam timeout", f"Timed out {message.author.mention} for 30 seconds after {spam_max_messages} messages within {spam_window_seconds} seconds.\\n**Case:** `#{case_id}`", WARNING)
            else:
                try:
                    await message.channel.send(embed=embed("🛡️ Slow down", f"{message.author.mention}, please slow down your messages.", WARNING), delete_after=4)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            spam_window.clear()
            return
    if not is_command_message:
        mommy_matches = len(re.findall(r"(?<!\w)mommy(?!\w)", message.content, re.IGNORECASE))
        add_speech_count(guild.id, message.author.id, "mommy", mommy_matches)

        configured_words = setting(guild, "swear_words", [])
        swear_matches = sum(len(pattern.findall(message.content)) for item in configured_words if (pattern := swear_word_pattern(item)))
        add_speech_count(guild.id, message.author.id, "swear", swear_matches)

        anti_swear_enabled = bool(setting(guild, "anti_swear", False)) or bool(setting(guild, "automod_bad_words", False))
        if anti_swear_enabled and swear_matches and not (isinstance(message.author, discord.Member) and is_staff(message.author)):
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            punishment = str(setting(guild, "automod_punishment", "delete") or "delete").strip().lower()
            if punishment in {"timeout", "mute"} and isinstance(message.author, discord.Member):
                try:
                    await message.author.timeout(timedelta(seconds=30), reason="Gargand Auto-Mod: configured word filter")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            try:
                await message.channel.send(embed=embed("🧼 Keep it clean", f"{message.author.mention}, that word or phrase is filtered in this server.", WARNING), delete_after=5)
            except (discord.Forbidden, discord.HTTPException):
                pass
            case_id = record_moderation_case(guild, "automod_bad_words", bot.user, message.author, "Configured word filter matched", details="Automatic word filter; message content was not stored.", channel=message.channel)
            await log_automod_action(guild, "🧼 Auto-Mod word filter", f"Filtered a message from {message.author.mention} in {message.channel.mention}.\\n**Case:** `#{case_id}`", WARNING)
            return

    # Dashboard-controlled mention protection; staff are exempt.
    if setting(guild, "automod_mentions", False) and isinstance(message.author, discord.Member) and not is_staff(message.author):
        mention_limit = bounded_setting_int(guild, "automod_mention_limit", 5, 2, 20)
        if len(message.mentions) + (1 if message.mention_everyone else 0) >= mention_limit:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                await message.author.timeout(timedelta(seconds=30), reason="Gargand Auto-Mod: mention limit exceeded")
            except (discord.Forbidden, discord.HTTPException):
                pass
            case_id = record_moderation_case(guild, "automod_mentions", bot.user, message.author, f"Exceeded mention limit ({mention_limit})", details="Automatic mention protection; message content was not stored.")
            await log_automod_action(guild, "📣 Auto-Mod mention protection", f"Removed a message from {message.author.mention} for exceeding the mention limit.\\n**Case:** `#{case_id}`", WARNING)
            return

    # Optional repeat-flood protection stores only a short-lived content hash.
    if setting(guild, "automod_flood", False) and not is_command_message and isinstance(message.author, discord.Member) and not is_staff(message.author):
        flood_key = (guild.id, message.author.id)
        flood_now = time.monotonic()
        message_fingerprint = hash(message.content.strip().casefold())
        flood_window = flood_message_windows[flood_key]
        flood_window.append((flood_now, message_fingerprint))
        while flood_window and flood_now - flood_window[0][0] > 15:
            flood_window.popleft()
        repeats = sum(1 for _, fingerprint in flood_window if fingerprint == message_fingerprint)
        if message.content.strip() and repeats >= 4:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            try:
                await message.author.timeout(timedelta(seconds=30), reason="Gargand Auto-Mod: repeated message flood")
            except (discord.Forbidden, discord.HTTPException):
                pass
            case_id = record_moderation_case(guild, "automod_flood", bot.user, message.author, "Repeated identical messages", details="Automatic repeat-flood protection; message content was not stored.")
            await log_automod_action(guild, "🌊 Auto-Mod flood protection", f"Removed repeated messages from {message.author.mention}.\\n**Case:** `#{case_id}`", WARNING)
            flood_window.clear()
            return

    # AFK cleanup when the user speaks.
    conn = db_connect(); afkrow = conn.execute("SELECT reason FROM afk WHERE guild_id=? AND user_id=?", (guild.id, message.author.id)).fetchone()
    if afkrow:
        conn.execute("DELETE FROM afk WHERE guild_id=? AND user_id=?", (guild.id, message.author.id)); conn.commit()
        await message.channel.send(embed=embed("👋 Welcome back", f"{message.author.mention}, your AFK was cleared.", SUCCESS), delete_after=4)
    conn.close()

    # Anti-link, with GIF exceptions.
    if setting(guild, "anti_link", False) and isinstance(message.author, discord.Member) and not is_staff(message.author):
        has_link = bool(re.search(r"https?://\S+|www\.\S+", message.content, re.I))
        is_gif = any((att.content_type or "").lower() == "image/gif" for att in message.attachments) or any(x in message.content.lower() for x in ("tenor.com/view/", "giphy.com/gifs/"))
        if has_link and not is_gif:
            try:
                await message.delete()
                await message.author.timeout(timedelta(minutes=5), reason="Gargand anti-link")
                await message.channel.send(embed=embed("🔗 Link blocked", f"{message.author.mention} was timed out for sending a link. GIFs are allowed.", WARNING), delete_after=5)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

    # Anti-raid @everyone spam.
    if setting(guild, "anti_raid", False) and message.mention_everyone and isinstance(message.author, discord.Member) and not is_staff(message.author):
        key = (guild.id, message.author.id); q = raid_mentions[key]; now = utc_ts(); q.append(now)
        while q and now - q[0] > 30: q.popleft()
        if len(q) >= 3:
            try:
                await guild.ban(message.author, reason="Gargand anti-raid: repeated @everyone")
                case_id = record_moderation_case(guild, "anti_raid_ban", bot.user, message.author, "Repeated @everyone mentions", details="Automated anti-raid enforcement.")
                await log_action(guild, "🚨 Anti-raid ban", f"Banned {message.author.mention} for repeated `@everyone` mentions.\n**Case:** `#{case_id}`", DANGER)
                return
            except discord.Forbidden:
                pass

    # Commands channel enforcement.
    command_channel = setting(guild, "command_channel_id")
    if command_channel and message.content.startswith(active_prefix) and message.channel.id != command_channel and isinstance(message.author, discord.Member) and not is_staff(message.author):
        try:
            await message.delete()
            await message.author.timeout(timedelta(minutes=1), reason="Gargand: command outside command channel")
            ch = guild.get_channel(command_channel)
            await message.channel.send(embed=embed("⌨️ Commands channel only", f"{message.author.mention}, please use {ch.mention if ch else 'the configured command channel'}.", WARNING), delete_after=4)
        except (discord.Forbidden, discord.HTTPException): pass
        return

    # Vouch channel: !vouch @user is the only accepted content.
    vouch_channel = setting(guild, "vouch_channel_id")
    if vouch_channel and message.channel.id == vouch_channel and not is_staff(message.author):
        if message.content.lower().startswith("!vouch "):
            try:
                target = await commands.MemberConverter().convert(commands.Context(bot=bot, message=message), message.content[7:].strip())
            except Exception:
                target = None
            if target:
                try: await message.delete()
                except discord.HTTPException: pass
                e = embed("⭐ New vouch", f"**{message.author.mention} vouched for {target.mention}!**\n\nThanks for keeping the community trustworthy. ✨", SUCCESS)
                e.set_thumbnail(url=target.display_avatar.url)
                await message.channel.send(embed=e)
            else:
                try: await message.delete(); await message.author.timeout(timedelta(minutes=5), reason="Invalid vouch")
                except Exception: pass
                return
        elif message.content.startswith(active_prefix):
            try: await message.delete(); await message.author.timeout(timedelta(minutes=5), reason="Non-vouch content in vouch channel")
            except Exception: pass
            return

    # Auto reaction.
    auto_ch = setting(guild, "autoreaction_channel_id")
    if auto_ch and message.channel.id == auto_ch:
        try: await message.add_reaction(setting(guild, "autoreaction_emoji", "✨"))
        except discord.HTTPException: pass

    # AFK mention protection.
    if message.mentions:
        for user in message.mentions[:5]:
            row = db_connect().execute("SELECT reason FROM afk WHERE guild_id=? AND user_id=?", (guild.id, user.id)).fetchone()
            if row:
                try: await message.delete()
                except discord.HTTPException: pass
                await message.channel.send(embed=embed("💤 AFK user", f"{user.mention} is AFK.\n**Reason:** {row['reason']}", WARNING), delete_after=5)
                break

    # Sticky message handling (re-post after a member speaks).
    conn = db_connect(); stick_row = conn.execute("SELECT content,message_id FROM sticks WHERE guild_id=? AND channel_id=?", (guild.id, message.channel.id)).fetchone(); conn.close()
    if stick_row and stick_row["message_id"] and message.id != stick_row["message_id"]:
        try: await message.channel.get_partial_message(stick_row["message_id"]).delete()
        except Exception: pass
        try:
            sent = await message.channel.send(embed=embed("📌 Sticky", stick_row["content"], INFO))
            conn = db_connect(); conn.execute("UPDATE sticks SET message_id=? WHERE guild_id=? AND channel_id=?", (sent.id, guild.id, message.channel.id)); conn.commit(); conn.close()
        except discord.HTTPException: pass

    await bot.process_commands(message)


# -----------------------------
# Error handler
# -----------------------------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if hasattr(ctx.command, "on_error"):
        return
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=embed("🔒 Access denied", str(error) or "You don't have permission to use that command.", DANGER), delete_after=5)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=embed("🧩 Missing argument", f"You're missing `{error.param.name}`.", WARNING), delete_after=5)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=embed("🧩 Invalid argument", "Check the member, role, channel, or number you entered.", WARNING), delete_after=5)
        return
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        print(f"Command error in {ctx.command}: {original!r}")
        await ctx.send(embed=embed("💥 Something went wrong", "I hit an unexpected error. Check the bot console/logs for details.", DANGER), delete_after=6)
        return
    print(f"Unhandled error in {ctx.command}: {error!r}")


# -----------------------------
# Website bridge (bot-initiated HTTPS)
# -----------------------------

GUILD_INVITE_CACHE: dict[int, str] = {}

async def build_guild_invite(guild):
    """Return a reusable permanent invite without hammering Discord every bridge poll."""
    cached = GUILD_INVITE_CACHE.get(guild.id)
    if cached:
        return cached
    me = guild.me
    if not me:
        return ""
    # Prefer a channel where Gargand can create invites.
    for channel in guild.text_channels:
        try:
            if not channel.permissions_for(me).create_instant_invite:
                continue
            invite = await channel.create_invite(
                max_age=0,
                max_uses=0,
                unique=False,
                reason="Gargand website admin server invite",
            )
            url = str(invite.url)
            GUILD_INVITE_CACHE[guild.id] = url
            return url
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            continue
    return ""

def collect_guild_health(guild, settings):
    """Report the bot's cached permission and setup state to the dashboard."""
    member = guild.me
    if not member:
        return {"available": False}

    required_permissions = (
        "administrator", "view_channel", "send_messages", "embed_links",
        "manage_roles", "manage_channels", "manage_messages",
        "kick_members", "ban_members", "moderate_members",
    )
    permissions = {
        name: bool(getattr(member.guild_permissions, name, False))
        for name in required_permissions
    }
    roles = {}
    for key in ("staff_role_id", "autorole_id", "booster_role_id", "verified_role_id", "unverified_role_id", "muted_role_id", "jail_role_id"):
        try:
            role_id = int(settings.get(key) or 0)
        except (TypeError, ValueError):
            role_id = 0
        role = guild.get_role(role_id) if role_id else None
        assignable = bool(role and not role.managed and member.top_role > role)
        roles[key] = {
            "id": str(role_id) if role_id else "",
            "configured": bool(role_id),
            "exists": bool(role),
            "name": role.name if role else "",
            "assignable": assignable,
        }

    channels = {}
    for key in ("logs_channel_id", "verification_channel_id", "ticket_panel_channel_id", "automod_log_channel_id", "welcome_channel_id", "feedback_channel_id", "suggestions_channel_id"):
        try:
            channel_id = int(settings.get(key) or 0)
        except (TypeError, ValueError):
            channel_id = 0
        channel = guild.get_channel(channel_id) if channel_id else None
        channel_permissions = channel.permissions_for(member) if channel else None
        channels[key] = {
            "id": str(channel_id) if channel_id else "",
            "configured": bool(channel_id),
            "exists": bool(channel),
            "name": getattr(channel, "name", "") if channel else "",
            "can_view": bool(channel_permissions and channel_permissions.view_channel),
            "can_send": bool(channel_permissions and channel_permissions.send_messages),
            "can_embed": bool(channel_permissions and channel_permissions.embed_links),
        }

    return {
        "available": True,
        "permissions": permissions,
        "top_role_position": int(member.top_role.position),
        "roles": roles,
        "channels": channels,
    }


def _command_category(name):
    name = str(name or "").lower()
    if any(token in name for token in ("ban", "kick", "warn", "timeout", "purge", "lock", "jail", "case", "appeal", "recover", "clearwarnings")):
        return "Moderation"
    if any(token in name for token in ("raid", "nuke", "anti", "security", "diagnose")):
        return "Security"
    if any(token in name for token in ("ticket", "application", "proof")):
        return "Tickets"
    if any(token in name for token in ("play", "skip", "pause", "resume", "queue", "nowplaying", "volume", "leave", "join", "stop")):
        return "Music"
    if any(token in name for token in ("rss", "anime", "image")):
        return "Feeds & search"
    if any(token in name for token in ("achievement", "quest", "season", "party", "leaderboard", "level", "xp", "suggest", "vouch", "poll")):
        return "Community"
    if any(token in name for token in ("giveaway", "daily", "coinflip", "blackjack", "roulette", "highlow", "roll", "rps", "8ball", "choose", "joke", "riddle", "poem", "story", "roast", "compliment", "quiz", "namegen", "caption", "ask", "aiimage")):
        return "Fun & AI"
    if any(token in name for token in ("setup", "configbackup", "official", "analytics", "admin")):
        return "Admin tools"
    return "Utility"


def _command_permission(name):
    name = str(name or "").lower()
    admin_only = {
        "setup", "configbackup", "analytics", "securitycheck", "diagnose",
        "rss add", "rss list", "rss remove", "animewatch", "animeunwatch",
        "animewatches", "auditlog", "official", "toggle_jail",
    }
    staff_only = {
        "ban", "kick", "warn", "timeout", "jail", "unjail", "purge",
        "clearwarnings", "announce", "raidlockdown", "raidrelease",
        "securitycheck", "auditlog", "recover", "configbackup",
    }
    if name in admin_only:
        return "Server administrator"
    if name in staff_only:
        return "Server staff"
    return "Server members"


def _command_example(name, usage):
    name = str(name or "").lower()
    examples = {
        "play": "!play lo-fi beats",
        "join": "!join",
        "party create": "!party create Night Owls",
        "party join": "!party join Night Owls",
        "image": "!image space lion",
        "rss add": "!rss add https://example.com/feed.xml",
        "animewatch": "!animewatch 21",
        "quest": "!quest",
        "season": "!season",
        "auditlog": "!auditlog 10",
        "volume": "!volume 80",
        "remind": "!remind 30m check the event",
        "poll": "!poll Movie night | Comedy | Adventure",
    }
    return examples.get(name, usage or ("!" + name))


def collect_command_catalog():
    """Serialize the commands that are actually registered at runtime."""
    result = []
    seen = set()
    for command in bot.walk_commands():
        if getattr(command, "hidden", False):
            continue
        qualified = str(getattr(command, "qualified_name", None) or getattr(command, "name", "")).strip()
        if not qualified:
            continue
        signature = str(getattr(command, "signature", "") or "").strip()
        name = "!" + qualified + ((" " + signature) if signature else "")
        description = str(
            getattr(command, "help", None)
            or getattr(command, "short_doc", None)
            or getattr(command, "brief", None)
            or ""
        ).strip().split("\n", 1)[0][:350]
        if not description and getattr(command, "app_command", None):
            description = str(getattr(command.app_command, "description", "") or "")[:350]
        aliases = list(dict.fromkeys(str(alias)[:80] for alias in (getattr(command, "aliases", []) or []) if str(alias).strip()))
        key = ("prefix", qualified.lower())
        if key not in seen:
            result.append({
                "command": name,
                "name": qualified,
                "mode": "prefix",
                "category": _command_category(qualified),
                "description": description or "Run this Gargand command.",
                "aliases": aliases,
                "permission": _command_permission(qualified),
                "usage": name,
                "example": _command_example(qualified, name),
            })
            seen.add(key)

    try:
        slash_commands = bot.tree.walk_commands()
    except Exception:
        slash_commands = []
    for command in slash_commands:
        qualified = str(getattr(command, "qualified_name", None) or getattr(command, "name", "")).strip()
        if not qualified:
            continue
        key = ("slash", qualified.lower())
        if key in seen:
            continue
        description = str(getattr(command, "description", "") or "").strip()[:350]
        signature = ""
        for parameter in getattr(command, "parameters", []) or []:
            pname = str(getattr(parameter, "name", "") or "")
            if pname:
                signature += f" <{pname}>"
        usage = "/" + qualified + signature
        result.append({
            "command": usage,
            "name": qualified,
            "mode": "slash",
            "category": _command_category(qualified),
            "description": description or "Run this Gargand slash command.",
            "aliases": [],
            "permission": _command_permission(qualified),
            "usage": usage,
            "example": _command_example(qualified, usage),
        })
        seen.add(key)
    result.sort(key=lambda item: (item["category"].casefold(), item["name"].casefold(), item["mode"]))
    return result[:500]


async def collect_guild_snapshot():
    snapshot = []
    command_catalog = collect_command_catalog()
    music_module = None
    feature_module = None
    try:
        import gargand_music as music_module
    except Exception:
        music_module = None
    try:
        import gargand_features as feature_module
    except Exception:
        feature_module = None

    for index, guild in enumerate(bot.guilds):
        settings = get_settings(guild.id)
        try:
            music_state = music_module.dashboard_snapshot(guild) if music_module else {}
        except Exception:
            music_state = {}
        try:
            feature_state = feature_module.dashboard_snapshot(guild.id) if feature_module else {}
        except Exception:
            feature_state = {}
        item = {
            "id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count,
            "invite_url": await build_guild_invite(guild),
            "settings": settings,
            "health": collect_guild_health(guild, settings),
            "music": music_state if isinstance(music_state, dict) else {},
            "community": feature_state.get("community", {}) if isinstance(feature_state, dict) else {},
            "notifications": feature_state.get("notifications", {}) if isinstance(feature_state, dict) else {},
            "audit": feature_state.get("audit", []) if isinstance(feature_state, dict) else [],
            # Publish the bot-authorized resource list through the authenticated bridge.
            "channels": [
                {"id": str(channel.id), "name": channel.name, "type": int(channel.type.value),
                 "position": int(getattr(channel, "position", 0))}
                for channel in guild.channels
                if int(channel.type.value) in {0, 2, 4, 5, 13, 15}
            ],
            "roles": [
                {"id": str(role.id), "name": role.name, "position": int(role.position)}
                for role in guild.roles if role.id != guild.id
            ],
        }
        # The command list is global to Gargand; attach it once rather than duplicating
        # hundreds of rows in every guild's heartbeat.
        if index == 0:
            item["command_catalog"] = command_catalog
        snapshot.append(item)
    return snapshot

def poll_nightfall_website():
    if not GARGAND_WEBSITE_URL or not GARGAND_BRIDGE_SECRET:
        return []
    headers = {"X-Gargand-Bridge-Key": GARGAND_BRIDGE_SECRET}
    # poll_nightfall_website runs in a worker thread, so build the async
    # snapshot before sending it to the website.
    guild_snapshot = asyncio.run_coroutine_threadsafe(
        collect_guild_snapshot(), bot.loop
    ).result(timeout=45)
    heartbeat = {
        "guilds": guild_snapshot
    }
    # Heartbeats are idempotent, so retry brief network/read timeouts. Render
    # can briefly cold-start; a single timeout should not mark the bridge dead.
    response = None
    for attempt in range(3):
        try:
            response = requests.post(
                f"{GARGAND_WEBSITE_URL}/api/bot/heartbeat",
                headers=headers,
                json=heartbeat,
                timeout=(5, 25),
            )
            response.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    # Pull consumes queued jobs on the server, so do not blindly retry it after
    # a read timeout (the first request may already have consumed those jobs).
    response = requests.get(
        f"{GARGAND_WEBSITE_URL}/api/bot/pull",
        headers=headers,
        timeout=(5, 25),
    )
    response.raise_for_status()
    data = response.json()
    return data.get("jobs", []) if data.get("ok") else []


async def handle_website_job(job):
    try:
        guild_id = int(job.get("guild_id", 0))
    except (TypeError, ValueError):
        return
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}

    if job.get("kind") == "official_announcement":
        if guild_id != OFFICIAL_GUILD_ID:
            return
        channel_id = int(get_settings(guild_id).get("official_announcement_channel_id") or OFFICIAL_ANNOUNCEMENT_CHANNEL_ID)
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            print(f"Official announcement destination {channel_id} is missing or not a text channel.")
            return
        # Ignore legacy queued website-update jobs; only bot release notes are posted.
        if str(payload.get("kind") or "bot").strip().lower() != "bot":
            return
        title = str(payload.get("title") or "Gargand update")[:180]
        body = str(payload.get("body") or "")[:3500]
        announcement = embed(f"📣 {title}", body, EMBED_COLOR)
        announcement.add_field(name="Read the update", value=str(payload.get("url") or f"{GARGAND_WEBSITE_URL}/announcements"), inline=False)
        try:
            await channel.send(embed=announcement, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Official announcement delivery failed: {type(exc).__name__}")
        return

    if job.get("kind") == "settings":
        current = get_settings(guild_id)
        # Convert dashboard resource IDs to integers before storing them. Discord.py
        # expects integer IDs; leaving select values as strings breaks channel/role lookups.
        clear_keys = set()
        invalid_keys = set()
        for key, value in list(payload.items()):
            if value is None or value == "":
                clear_keys.add(key)
                payload[key] = None
                continue
            if key.endswith("_channel_id") or key.endswith("_category_id"):
                try:
                    resource_id = int(value)
                except (TypeError, ValueError):
                    print(f"Website setting {key!r} ignored for guild {guild_id}: invalid channel ID.")
                    invalid_keys.add(key)
                    continue
                channel = guild.get_channel(resource_id)
                expected = discord.CategoryChannel if key.endswith("_category_id") else discord.TextChannel
                if not isinstance(channel, expected):
                    print(f"Website setting {key!r} ignored for guild {guild_id}: channel not found or wrong type.")
                    invalid_keys.add(key)
                else:
                    payload[key] = channel.id
            elif key.endswith("_role_id"):
                try:
                    resource_id = int(value)
                except (TypeError, ValueError):
                    print(f"Website setting {key!r} ignored for guild {guild_id}: invalid role ID.")
                    invalid_keys.add(key)
                    continue
                if guild.get_role(resource_id) is None:
                    print(f"Website setting {key!r} ignored for guild {guild_id}: role not found.")
                    invalid_keys.add(key)
                else:
                    payload[key] = resource_id
            elif key == "appeal_server_id":
                try:
                    payload[key] = int(str(value).strip())
                except (TypeError, ValueError):
                    print(f"Website appeal server ID ignored for guild {guild_id}: invalid server ID.")
                    invalid_keys.add(key)
            elif key == "anti_nuke_trusted_role_ids":
                if not isinstance(value, list):
                    payload[key] = []
                else:
                    trusted_ids = []
                    for item in value[:20]:
                        try:
                            role_id = int(item)
                        except (TypeError, ValueError):
                            continue
                        if guild.get_role(role_id) is not None:
                            trusted_ids.append(role_id)
                    payload[key] = trusted_ids
        # Invalid/stale dashboard resource IDs must not overwrite a working saved value.
        for key in invalid_keys:
            payload.pop(key, None)
        previous_anti_raid = bool(current.get("anti_raid", False))
        current.update(payload)
        for key in clear_keys:
            current.pop(key, None)
        if "anti_raid" in payload and payload["anti_raid"] and not previous_anti_raid:
            changed, rate_limited, total = await apply_external_app_lock(guild)
            print(f"Website enabled anti-raid for {guild_id}: protected {changed}/{total} channels.")
        if ("appeal_server_id" in payload or "appeal_invite_url" in payload) and current.get("appeal_server_id") and current.get("appeal_invite_url"):
            conn = db_connect()
            conn.execute(
                "INSERT OR REPLACE INTO appeal_links(main_guild_id,appeal_guild_id,invite_url) VALUES(?,?,?)",
                (guild_id, int(current["appeal_server_id"]), str(current["appeal_invite_url"])),
            )
            conn.commit()
            conn.close()
        save_settings(guild_id, current)
        asyncio.create_task(send_telemetry(
            "settings_updated",
            guild=guild,
            user=bot.user,
            metadata={"setting_keys": sorted(str(key) for key in payload.keys())[:80]},
        ))
        await refresh_setup_dashboard(guild)
        return

    if job.get("kind") == "action" and payload.get("action") == "sync_setup":
        refreshed = await refresh_setup_dashboard(guild)
        asyncio.create_task(send_telemetry(
            "dashboard_action",
            guild=guild,
            user=bot.user,
            metadata={"action": "sync_setup", "result": "success" if refreshed else "no_saved_panel"},
        ))
        if not refreshed:
            print(f"Website requested setup sync for {guild_id}, but no saved !setup dashboard exists.")
    elif job.get("kind") == "action":
        action = str(payload.get("action") or "")
        try:
            if action.startswith("music_"):
                import gargand_music
                try:
                    result = await gargand_music.handle_dashboard_action(guild, payload)
                    asyncio.create_task(send_telemetry(
                        "dashboard_action", guild=guild, user=bot.user,
                        metadata={"action": action, "result": "success", "message": str((result or {}).get("message", ""))[:180]}
                    ))
                    print(f"Website music action {action!r} completed for guild {guild_id}.")
                except Exception as exc:
                    asyncio.create_task(send_telemetry(
                        "dashboard_action", guild=guild, user=bot.user,
                        metadata={"action": action, "result": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:180]}
                    ))
                    print(f"Website music action {action!r} failed for guild {guild_id}: {type(exc).__name__}: {str(exc)[:160]}")
                return
            if action.startswith("notification_"):
                import gargand_features
                try:
                    result = await gargand_features.handle_dashboard_action(guild, payload)
                    asyncio.create_task(send_telemetry(
                        "dashboard_action", guild=guild, user=bot.user,
                        metadata={"action": action, "result": "success", "message": str((result or {}).get("message", ""))[:180]}
                    ))
                    print(f"Website notification action {action!r} completed for guild {guild_id}.")
                except Exception as exc:
                    asyncio.create_task(send_telemetry(
                        "dashboard_action", guild=guild, user=bot.user,
                        metadata={"action": action, "result": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:180]}
                    ))
                    print(f"Website notification action {action!r} failed for guild {guild_id}: {type(exc).__name__}: {str(exc)[:160]}")
                return
            if action == "raid_lockdown":
                locked, failed, restore_at = await start_raid_lockdown(guild, 10, actor=bot.user, reason="Dashboard emergency control")
                print(f"Website requested raid lockdown for {guild_id}: locked {locked} channels, {failed} failed, expiry {restore_at}.")
            elif action == "raid_release":
                restored, failed, remaining = await release_raid_lockdown(guild, actor=bot.user, reason="Website dashboard recovery control")
                print(f"Website requested raid release for {guild_id}: restored {restored} channels, {failed} failed, {remaining} remain.")
            elif action == "post_ticket_panel":
                channel = guild.get_channel(setting(guild, "ticket_panel_channel_id")) if setting(guild, "ticket_panel_channel_id") else None
                if isinstance(channel, discord.TextChannel):
                    await channel.send(embed=embed("🎫 Open a Gargand ticket", "Choose a topic below and our team will be with you shortly."), view=TicketPanelView(guild.id))
            elif action == "post_verification_panel":
                await post_verification_panel(guild)
            elif action == "post_feedback_panel":
                await post_feedback_panel(guild)
            elif action == "post_application_panel":
                await post_staff_application_panel(guild)
            elif action == "toggle_jail":
                if setting(guild, "jail_enabled", False):
                    set_setting(guild, "jail_enabled", False)
                else:
                    await configure_jail_system(guild)
            asyncio.create_task(send_telemetry(
                "dashboard_action",
                guild=guild,
                user=bot.user,
                metadata={"action": str(action or "")[:80], "result": "success"},
            ))
            await refresh_setup_dashboard(guild)
        except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
            asyncio.create_task(send_telemetry(
                "dashboard_action",
                guild=guild,
                user=bot.user,
                metadata={"action": str(action or "")[:80], "result": "failed", "error_type": type(exc).__name__},
            ))
            print(f"Website action {action!r} failed for guild {guild_id}: {type(exc).__name__}")


async def nightfall_website_bridge():
    if not GARGAND_WEBSITE_URL or not GARGAND_BRIDGE_SECRET:
        print("GARGAND website bridge is disabled; set GARGAND_WEBSITE_URL and GARGAND_BRIDGE_SECRET (legacy GARGAND_* names are also accepted).")
        return
    while not bot.is_closed():
        try:
            jobs = await asyncio.to_thread(poll_nightfall_website)
            for job in jobs:
                if isinstance(job, dict):
                    await handle_website_job(job)
        except requests.RequestException as exc:
            # Render can briefly return 502/timeout while the website deploys or wakes.
            # Do not spam the bot console; the next poll will retry automatically.
            if getattr(exc, "response", None) is not None and getattr(exc.response, "status_code", 0) in (502, 503, 504):
                pass
            else:
                print(f"Gargand website bridge temporarily unavailable: {type(exc).__name__}")
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except Exception:
            import traceback
            print("Gargand website bridge poll failed; full traceback follows:")
            traceback.print_exc()
        await asyncio.sleep(BRIDGE_POLL_SECONDS)
# -----------------------------
# Startup
# -----------------------------

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
