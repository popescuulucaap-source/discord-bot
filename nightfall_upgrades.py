"""
Gargand modular upgrades.
Loaded automatically by sitecustomize without replacing main.py.
Adds Auto-Mod, XP/levels, analytics, AI channel memory, and extra admin tooling.
"""
import asyncio
import importlib
import sqlite3
import sys
import time
import random
from collections import defaultdict, deque
from datetime import datetime, timezone

INSTALLED = False
WINDOWS = defaultdict(deque)
XP_COOLDOWNS = {}

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
                _, level, leveled = add_xp(guild.id, member.id, random.randint(8,15))
                if leveled:
                    target = guild.get_channel(setting(guild, "levelup_channel_id") or message.channel.id)
                    if isinstance(target, main.discord.TextChannel):
                        await target.send(embed=embed("✨ Level up!", f"{member.mention} reached **level {level}**!", main.SUCCESS))

    bot.add_listener(upgraded_message, "on_message")

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

    @bot.hybrid_command(name="poll", description="Create a community poll with up to five options.")
    @main.commands.guild_only()
    @main.staff_only()
    async def poll(ctx, question: str, option1: str, option2: str, option3: str = "", option4: str = "", option5: str = ""):
        options = [str(option).strip()[:180] for option in (option1, option2, option3, option4, option5) if str(option or "").strip()]
        question = str(question or "").strip()[:240]
        if len(options) < 2:
            await ctx.send("A poll needs at least two options.")
            return
        if len(set(option.casefold() for option in options)) != len(options):
            await ctx.send("Each poll option must be different.")
            return
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        card = embed("📊 " + (question or "Community poll"), "\n".join(f"{emojis[i]}  {option}" for i, option in enumerate(options)), main.INFO)
        card.set_footer(text=f"GARGAND • Poll by {ctx.author.display_name} • React to vote")
        message = await ctx.send(embed=card)
        for emoji in emojis[:len(options)]:
            try:
                await message.add_reaction(emoji)
            except main.discord.HTTPException:
                break

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

    @bot.hybrid_command(name="configbackup", description="Export this server's Gargand settings as JSON.")
    @main.admin_only()
    @main.commands.guild_only()
    async def configbackup(ctx):
        data = main.get_settings(ctx.guild.id)
        safe_payload = {
            "format": "gargand-server-config",
            "version": 1,
            "guild_id": str(ctx.guild.id),
            "guild_name": ctx.guild.name,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "settings": data,
            "note": "Settings only; this export does not contain bot tokens or OAuth client secrets.",
        }
        contents = main.json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        file_obj = main.discord.File(main.io.BytesIO(contents), filename=f"gargand-config-{ctx.guild.id}.json")
        await ctx.send("Configuration backup created. Keep this file private because it contains server IDs and settings.", file=file_obj)


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

    async def startup():
        await asyncio.sleep(3)
        try:
            await bot.tree.sync()
        except Exception as exc:
            print("Gargand upgrades slash sync:", repr(exc))
    bot.loop.create_task(startup())
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

