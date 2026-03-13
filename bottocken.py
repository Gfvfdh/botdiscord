"""
╔══════════════════════════════════════════════════════════╗
║           🛡️  GuardBot v2 — Discord Python               ║
║   Protection ultra-complète en 1 seul fichier            ║
║                                                          ║
║   pip install discord.py                                 ║
║   DISCORD_TOKEN=xxx python guardbot.py                   ║
╚══════════════════════════════════════════════════════════╝
"""

import discord
from discord.ext import commands
import asyncio, re, os, logging
from collections import defaultdict, deque
from datetime    import datetime, timedelta, timezone
from dataclasses import dataclass
from typing      import Optional

# ══════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("GuardBot")

# ══════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════

@dataclass
class Config:
    token      : str           = os.getenv("DISCORD_TOKEN", "w")
    prefix     : str           = "!"
    log_channel: Optional[int] = int(os.getenv("LOG_CHANNEL", "0")) or None
    mute_role  : Optional[int] = int(os.getenv("MUTE_ROLE",   "0")) or None

    # Anti-Spam
    spam_enabled  : bool  = True
    spam_max_msg  : int   = 5
    spam_window   : float = 3.0
    spam_punish   : str   = "mute"
    spam_mute_min : int   = 5

    # Anti-Lien (TOUS les liens bloqués, aucune exception)
    link_enabled  : bool  = True
    link_allowed  : tuple = ()   # vide = zéro exception, tout est bloqué
    link_punish   : str   = "none"  # "none" = juste supprimer + message, pas de sanction

    # Anti-Invite Discord
    invite_enabled: bool  = True
    invite_punish : str   = "none"

    # Anti-Raid
    raid_enabled  : bool  = True
    raid_threshold: int   = 10
    raid_window   : float = 10.0
    raid_action   : str   = "kick"
    auto_slowmode : bool  = True
    auto_slowmode_s: int  = 10
    auto_lockdown : bool  = False

    # Anti-Caps
    caps_enabled  : bool  = True
    caps_pct      : float = 70.0
    caps_min_len  : int   = 10

    # Anti-Mention Spam
    mention_enabled: bool = True
    mention_max    : int  = 5
    mention_punish : str  = "mute"

    # Anti-Duplicate
    dup_enabled   : bool  = True
    dup_max       : int   = 3
    dup_punish    : str   = "mute"

    # Anti-Swear
    swear_enabled : bool  = False
    swear_words   : tuple = ("motinsultant1", "motinsultant2")
    swear_punish  : str   = "warn"

    # Anti-Bot
    antibot_enabled: bool = True
    bot_whitelist  : tuple= ()

    # Warn escalade
    warn_mute_at  : int   = 3
    warn_kick_at  : int   = 5
    warn_ban_at   : int   = 7

    # Logs
    log_msg_delete: bool  = True
    log_msg_edit  : bool  = True
    log_bans      : bool  = True
    log_joins     : bool  = True
    log_leaves    : bool  = True
    log_roles     : bool  = True
    log_channels  : bool  = True
    log_nicknames : bool  = True
    log_voice     : bool  = True

cfg = Config()

# ══════════════════════════════════════════════════════════
#  ÉTAT EN MÉMOIRE
# ══════════════════════════════════════════════════════════

spam_tracker : dict[int, deque] = defaultdict(deque)
dup_tracker  : dict[int, list]  = defaultdict(list)
warn_counts  : dict[int, int]   = defaultdict(int)
raid_tracker : deque            = deque()
muted_tasks  : dict[int, asyncio.Task] = {}
raid_active  : bool             = False

# ══════════════════════════════════════════════════════════
#  INTENTS & BOT
# ══════════════════════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=cfg.prefix, intents=intents, help_command=None)

# ══════════════════════════════════════════════════════════
#  EMBED HELPERS
# ══════════════════════════════════════════════════════════

COLORS = {
    "ok"   : 0x57f287, "warn" : 0xfee75c, "mute" : 0xff9a3c,
    "kick" : 0xff6b6b, "ban"  : 0xed4245, "info" : 0x5865f2,
    "log"  : 0x99aab5, "raid" : 0xff0000, "join" : 0x57f287,
    "leave": 0x747f8d,
}
EMOJIS = {"warn":"⚠️","mute":"🔇","kick":"👢","ban":"🔨","ok":"✅","info":"ℹ️","raid":"🚨"}

def make_embed(title, desc="", color="info", *, fields=(), thumb=""):
    c = COLORS.get(color, color) if isinstance(color, str) else color
    e = discord.Embed(title=title, description=desc, color=c,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text="🛡️ GuardBot v2")
    for f in fields:
        name, value, *inline = f
        e.add_field(name=name, value=str(value), inline=bool(inline and inline[0]))
    if thumb:
        e.set_thumbnail(url=thumb)
    return e

async def send_log(guild, emb):
    if not cfg.log_channel:
        return
    ch = guild.get_channel(cfg.log_channel)
    if ch:
        try:
            await ch.send(embed=emb)
        except Exception:
            pass

async def dm_user(user, text):
    try:
        await user.send(text)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
#  SYSTÈME DE SANCTIONS
# ══════════════════════════════════════════════════════════

async def punish(member, action, reason, mute_minutes=5, silent=False):
    guild = member.guild
    user  = member
    try:
        if action == "warn":
            warn_counts[user.id] += 1
            count = warn_counts[user.id]
            if not silent:
                await dm_user(user, f"⚠️ **Avertissement** sur **{guild.name}**\nRaison : {reason}\nTotal : {count}")
            # Escalade automatique
            if count >= cfg.warn_ban_at:
                await punish(member, "ban",  f"Trop d'avertissements ({count})", silent=True); return
            if count >= cfg.warn_kick_at:
                await punish(member, "kick", f"Trop d'avertissements ({count})", silent=True); return
            if count >= cfg.warn_mute_at:
                await punish(member, "mute", f"Trop d'avertissements ({count})", 10, silent=True)
            emb = make_embed(f"⚠️ AutoMod — WARN", color="warn",
                fields=[("👤 Utilisateur", f"{user.mention} (`{user.id}`)", True),
                        ("📋 Raison", reason, False), ("📊 Warns", count, True)],
                thumb=user.display_avatar.url)
            await send_log(guild, emb)
            return

        if action == "mute":
            mute_role = guild.get_role(cfg.mute_role) if cfg.mute_role else None
            if mute_role:
                await member.add_roles(mute_role, reason=reason)
                async def _unmute():
                    await asyncio.sleep(mute_minutes * 60)
                    try: await member.remove_roles(mute_role, reason="Mute expiré")
                    except Exception: pass
                    muted_tasks.pop(user.id, None)
                if user.id in muted_tasks:
                    muted_tasks[user.id].cancel()
                muted_tasks[user.id] = asyncio.create_task(_unmute())
            else:
                until = discord.utils.utcnow() + timedelta(minutes=mute_minutes)
                await member.timeout(until, reason=reason)
            if not silent:
                await dm_user(user, f"🔇 Mute sur **{guild.name}** ({mute_minutes} min)\nRaison : {reason}")

        elif action == "kick":
            if not silent: await dm_user(user, f"👢 Kick de **{guild.name}**\nRaison : {reason}")
            await member.kick(reason=reason)

        elif action == "ban":
            if not silent: await dm_user(user, f"🔨 Banni de **{guild.name}**\nRaison : {reason}")
            await member.ban(reason=reason, delete_message_days=7)

    except discord.Forbidden:
        log.warning(f"Permission refusée : {action} sur {user}")
        return
    except Exception as ex:
        log.error(f"punish({action}) : {ex}"); return

    emb = make_embed(f"{EMOJIS.get(action,'🛡️')} AutoMod — {action.upper()}", color=action,
        fields=[("👤 Utilisateur", f"{user.mention} (`{user.id}`)", True),
                ("⚖️ Action", action.upper(), True), ("📋 Raison", reason, False)],
        thumb=user.display_avatar.url)
    await send_log(guild, emb)

# ══════════════════════════════════════════════════════════
#  DÉTECTEURS
# ══════════════════════════════════════════════════════════

URL_RE    = re.compile(r"(https?://|discord\.gg/|www\.)\S+", re.I)
INVITE_RE = re.compile(r"discord\.(gg|com/invite)/\S+", re.I)

def _is_privileged(member):
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

def detect_spam(msg):
    if not cfg.spam_enabled: return False
    uid, now = msg.author.id, msg.created_at.timestamp()
    dq = spam_tracker[uid]
    while dq and now - dq[0] > cfg.spam_window: dq.popleft()
    dq.append(now)
    return len(dq) >= cfg.spam_max_msg

def detect_link(msg):
    """Bloque TOUS les liens sans aucune exception."""
    if not cfg.link_enabled: return False
    return bool(URL_RE.search(msg.content))

def detect_invite(msg):
    if not cfg.invite_enabled: return False
    return bool(INVITE_RE.search(msg.content))

def detect_caps(msg):
    if not cfg.caps_enabled: return False
    letters = [c for c in msg.content if c.isalpha()]
    if len(letters) < cfg.caps_min_len: return False
    return (sum(c.isupper() for c in letters) / len(letters) * 100) >= cfg.caps_pct

def detect_mention_spam(msg):
    if not cfg.mention_enabled: return False
    return (len(msg.mentions) + len(msg.role_mentions)) >= cfg.mention_max

def detect_swear(msg):
    if not cfg.swear_enabled: return False
    low = msg.content.lower()
    return any(w in low for w in cfg.swear_words)

def detect_duplicate(msg):
    if not cfg.dup_enabled: return False
    uid = msg.author.id
    info = dup_tracker[uid]
    clean = msg.content.strip()
    if not info or info[0] != clean:
        dup_tracker[uid] = [clean, 1]; return False
    info[1] += 1
    return info[1] >= cfg.dup_max

def detect_raid():
    if not cfg.raid_enabled: return False
    now = datetime.now(timezone.utc).timestamp()
    while raid_tracker and now - raid_tracker[0] > cfg.raid_window: raid_tracker.popleft()
    raid_tracker.append(now)
    return len(raid_tracker) >= cfg.raid_threshold

# ══════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}  ({bot.user.id})")
    log.info(f"Serveurs : {len(bot.guilds)}  |  Préfixe : {cfg.prefix}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="🛡️ Votre serveur"),
        status=discord.Status.online,
    )

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild: return
    member = message.guild.get_member(message.author.id)
    if not member or _is_privileged(member):
        await bot.process_commands(message); return

    # ── Anti-Lien : suppression + message custom (aucune sanction) ──
    if detect_link(message) or detect_invite(message):
        try:
            await message.delete()
            await message.channel.send(
                f"🚫 {message.author.mention} **Vous n'avez pas accès à envoyer des liens ici.**",
                delete_after=5,
            )
        except Exception: pass
        return

    checks = [
        (detect_mention_spam, cfg.mention_punish, lambda m: f"Mention spam ({len(m.mentions)+len(m.role_mentions)} mentions)"),
        (detect_duplicate,    cfg.dup_punish,     lambda m: "Messages identiques répétés"),
        (detect_spam,         cfg.spam_punish,    lambda m: "Spam détecté"),
        (detect_swear,        cfg.swear_punish,   lambda m: "Langage inapproprié"),
    ]
    for detector, sanction, reason_fn in checks:
        if detector(message):
            try: await message.delete()
            except Exception: pass
            mute_min = cfg.spam_mute_min if sanction == "mute" else 5
            await punish(member, sanction, reason_fn(message), mute_min)
            return

    if detect_caps(message):
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} Évite les majuscules excessives ! 🔡", delete_after=4)
        except Exception: pass
        return

    await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
    if not cfg.log_msg_edit or not after.guild or after.author.bot: return
    if before.content == after.content: return
    emb = make_embed("✏️ Message modifié", color="log", fields=[
        ("Auteur", after.author.mention, True), ("Salon", after.channel.mention, True),
        ("Avant",  (before.content or "*vide*")[:512], False),
        ("Après",  (after.content  or "*vide*")[:512], False),
    ])
    await send_log(after.guild, emb)

@bot.event
async def on_message_delete(message):
    if not cfg.log_msg_delete or not message.guild or not message.author or message.author.bot: return
    emb = make_embed("🗑️ Message supprimé", color="log", fields=[
        ("Auteur",  f"{message.author.mention} (`{message.author.id}`)", True),
        ("Salon",   message.channel.mention, True),
        ("Contenu", (message.content or "*vide*")[:1020], False),
    ])
    await send_log(message.guild, emb)

@bot.event
async def on_member_join(member):
    guild = member.guild
    global raid_active

    if cfg.antibot_enabled and member.bot:
        if member.id not in cfg.bot_whitelist:
            try: await member.kick(reason="Bot non autorisé")
            except Exception: pass
            await send_log(guild, make_embed("🤖 AntiBot — Bot expulsé",
                f"`{member}` kick automatiquement.", "kick"))
            return

    if detect_raid():
        raid_active = True
        try: await member.kick(reason="Anti-Raid actif")
        except Exception: pass
        if cfg.auto_slowmode:
            for ch in guild.text_channels:
                try: await ch.edit(slowmode_delay=cfg.auto_slowmode_s)
                except Exception: pass
        if cfg.auto_lockdown:
            ow = discord.PermissionOverwrite(send_messages=False)
            for ch in guild.text_channels:
                try: await ch.set_permissions(guild.default_role, overwrite=ow)
                except Exception: pass
        await send_log(guild, make_embed("🚨 Anti-Raid déclenché !",
            f"`{member}` kick. Slowmode {cfg.auto_slowmode_s}s activé.", "raid"))
        return

    if cfg.log_joins:
        emb = make_embed("📥 Nouveau membre", f"{member.mention} a rejoint.", "join",
            fields=[("Compte créé", discord.utils.format_dt(member.created_at, "R"), True)],
            thumb=member.display_avatar.url)
        await send_log(guild, emb)

@bot.event
async def on_member_remove(member):
    if not cfg.log_leaves: return
    emb = make_embed("📤 Membre parti", f"`{member}` a quitté.", "leave",
                     thumb=member.display_avatar.url)
    await send_log(member.guild, emb)

@bot.event
async def on_member_update(before, after):
    if not cfg.log_nicknames: return
    if before.nick != after.nick:
        emb = make_embed("✏️ Pseudo modifié", color="log", fields=[
            ("Membre", after.mention, True),
            ("Avant",  before.nick or "—", True),
            ("Après",  after.nick  or "—", True),
        ])
        await send_log(after.guild, emb)

@bot.event
async def on_member_ban(guild, user):
    if not cfg.log_bans: return
    emb = make_embed("🔨 Membre banni", color="ban",
        fields=[("Utilisateur", f"{user} (`{user.id}`)", True)],
        thumb=user.display_avatar.url)
    await send_log(guild, emb)

@bot.event
async def on_member_unban(guild, user):
    emb = make_embed("🔓 Membre débanni", color="ok",
        fields=[("Utilisateur", f"{user} (`{user.id}`)", True)])
    await send_log(guild, emb)

@bot.event
async def on_guild_role_delete(role):
    if not cfg.log_roles: return
    await send_log(role.guild, make_embed("🔴 Rôle supprimé", color="kick",
        fields=[("Rôle", role.name, True)]))

@bot.event
async def on_guild_role_create(role):
    if not cfg.log_roles: return
    await send_log(role.guild, make_embed("🟢 Rôle créé", color="ok",
        fields=[("Rôle", role.name, True)]))

@bot.event
async def on_guild_channel_delete(channel):
    if not cfg.log_channels: return
    await send_log(channel.guild, make_embed("📛 Salon supprimé", color="kick",
        fields=[("Salon", f"#{channel.name}", True)]))

@bot.event
async def on_guild_channel_create(channel):
    if not cfg.log_channels: return
    await send_log(channel.guild, make_embed("📢 Salon créé", color="ok",
        fields=[("Salon", channel.mention, True)]))

@bot.event
async def on_voice_state_update(member, before, after):
    if not cfg.log_voice or before.channel == after.channel: return
    if after.channel:
        desc, color = f"{member.mention} a rejoint `{after.channel.name}`",  "join"
    else:
        desc, color = f"{member.mention} a quitté `{before.channel.name}`", "leave"
    await send_log(member.guild, make_embed("🔊 Vocal", desc, color))

# ══════════════════════════════════════════════════════════
#  CHECKS PERMISSIONS
# ══════════════════════════════════════════════════════════

async def require_mod(ctx):
    ok = ctx.author.guild_permissions.manage_messages or ctx.author.guild_permissions.administrator
    if not ok: await ctx.send("❌ Réservé aux modérateurs.", delete_after=5)
    return ok

async def require_admin(ctx):
    ok = ctx.author.guild_permissions.administrator
    if not ok: await ctx.send("❌ Réservé aux administrateurs.", delete_after=5)
    return ok

# ══════════════════════════════════════════════════════════
#  COMMANDES
# ══════════════════════════════════════════════════════════

@bot.command(name="warn")
async def cmd_warn(ctx, member: discord.Member = None, *, reason="Aucune raison"):
    if not await require_mod(ctx): return
    if not member: return await ctx.send("❌ `!warn @user [raison]`", delete_after=5)
    await punish(member, "warn", reason)
    await ctx.send(f"⚠️ **{member.display_name}** averti. ({warn_counts[member.id]} warn(s))")

@bot.command(name="mute")
async def cmd_mute(ctx, member: discord.Member = None, minutes: int = 10, *, reason="Mute manuel"):
    if not await require_mod(ctx): return
    if not member: return await ctx.send("❌ `!mute @user [minutes] [raison]`", delete_after=5)
    await punish(member, "mute", reason, minutes)
    await ctx.send(f"🔇 **{member.display_name}** mute pour **{minutes} min**.")

@bot.command(name="unmute")
async def cmd_unmute(ctx, member: discord.Member = None):
    if not await require_mod(ctx): return
    if not member: return await ctx.send("❌ `!unmute @user`", delete_after=5)
    if member.id in muted_tasks:
        muted_tasks[member.id].cancel(); del muted_tasks[member.id]
    mute_role = ctx.guild.get_role(cfg.mute_role) if cfg.mute_role else None
    if mute_role and mute_role in member.roles:
        await member.remove_roles(mute_role, reason="Unmute manuel")
    else:
        await member.timeout(None, reason="Unmute manuel")
    await ctx.send(f"🔊 **{member.display_name}** dé-mute.")

@bot.command(name="kick")
async def cmd_kick(ctx, member: discord.Member = None, *, reason="Kick manuel"):
    if not await require_mod(ctx): return
    if not member: return await ctx.send("❌ `!kick @user [raison]`", delete_after=5)
    await punish(member, "kick", reason)
    await ctx.send(f"👢 **{member.display_name}** kick.")

@bot.command(name="ban")
async def cmd_ban(ctx, member: discord.Member = None, *, reason="Ban manuel"):
    if not await require_admin(ctx): return
    if not member: return await ctx.send("❌ `!ban @user [raison]`", delete_after=5)
    await punish(member, "ban", reason)
    await ctx.send(f"🔨 **{member.display_name}** banni.")

@bot.command(name="softban")
async def cmd_softban(ctx, member: discord.Member = None, *, reason="Softban"):
    if not await require_admin(ctx): return
    if not member: return await ctx.send("❌ `!softban @user [raison]`", delete_after=5)
    await dm_user(member, f"👢 Softban de **{ctx.guild.name}** : {reason}")
    await ctx.guild.ban(member, delete_message_days=7, reason=reason)
    await ctx.guild.unban(member, reason="Softban — unban immédiat")
    await ctx.send(f"👢 **{member}** softban (messages supprimés, non banni).")

@bot.command(name="unban")
async def cmd_unban(ctx, user_id: int = None):
    if not await require_admin(ctx): return
    if not user_id: return await ctx.send("❌ `!unban <ID>`", delete_after=5)
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"Unban par {ctx.author}")
        await ctx.send(f"✅ **{user}** débanni.")
    except discord.NotFound:
        await ctx.send("❌ Utilisateur introuvable ou non banni.")

@bot.command(name="clear", aliases=["purge", "prune"])
async def cmd_clear(ctx, amount: int = 10, member: discord.Member = None):
    if not await require_mod(ctx): return
    amount = min(max(amount, 1), 200)
    check  = (lambda m: m.author == member) if member else None
    deleted = await ctx.channel.purge(limit=amount + 1, check=check)
    await ctx.send(f"🧹 **{len(deleted)-1}** message(s) supprimé(s).", delete_after=4)

@bot.command(name="warns")
async def cmd_warns(ctx, member: discord.Member = None):
    if not await require_mod(ctx): return
    target = member or ctx.author
    count  = warn_counts[target.id]
    emb = make_embed("📋 Avertissements", color="warn", fields=[
        ("Utilisateur", target.mention, True), ("Total warns", count, True),
        ("Paliers", f"Mute à {cfg.warn_mute_at} / Kick à {cfg.warn_kick_at} / Ban à {cfg.warn_ban_at}", False),
    ], thumb=target.display_avatar.url)
    await ctx.send(embed=emb)

@bot.command(name="clearwarns", aliases=["resetwarns"])
async def cmd_clearwarns(ctx, member: discord.Member = None):
    if not await require_admin(ctx): return
    if not member: return await ctx.send("❌ `!clearwarns @user`", delete_after=5)
    warn_counts[member.id] = 0
    await ctx.send(f"✅ Warns de **{member.display_name}** remis à zéro.")

@bot.command(name="slowmode", aliases=["slow"])
async def cmd_slowmode(ctx, seconds: int = 0):
    if not await require_mod(ctx): return
    seconds = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"🐌 Slowmode : **{seconds}s**." if seconds else "✅ Slowmode désactivé.")

@bot.command(name="lock", aliases=["lockdown"])
async def cmd_lock(ctx, channel: discord.TextChannel = None):
    if not await require_admin(ctx): return
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"🔒 {ch.mention} verrouillé.")

@bot.command(name="unlock")
async def cmd_unlock(ctx, channel: discord.TextChannel = None):
    if not await require_admin(ctx): return
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"🔓 {ch.mention} déverrouillé.")

@bot.command(name="lockall")
async def cmd_lockall(ctx):
    if not await require_admin(ctx): return
    ow, count = discord.PermissionOverwrite(send_messages=False), 0
    for ch in ctx.guild.text_channels:
        try: await ch.set_permissions(ctx.guild.default_role, overwrite=ow); count += 1
        except Exception: pass
    await ctx.send(f"🔒 **{count}** salons verrouillés.")

@bot.command(name="unlockall")
async def cmd_unlockall(ctx):
    if not await require_admin(ctx): return
    count = 0
    for ch in ctx.guild.text_channels:
        try:
            ow = ch.overwrites_for(ctx.guild.default_role)
            ow.send_messages = None
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow); count += 1
        except Exception: pass
    await ctx.send(f"🔓 **{count}** salons déverrouillés.")

@bot.command(name="raidoff")
async def cmd_raidoff(ctx):
    if not await require_admin(ctx): return
    global raid_active
    raid_active = False; raid_tracker.clear()
    for ch in ctx.guild.text_channels:
        try: await ch.edit(slowmode_delay=0)
        except Exception: pass
    await ctx.send("✅ Mode anti-raid désactivé, slowmodes retirés.")

@bot.command(name="userinfo", aliases=["ui", "whois"])
async def cmd_userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    roles = [r.mention for r in reversed(m.roles) if r.name != "@everyone"]
    emb = make_embed(f"👤 {m.display_name}", color="info", fields=[
        ("ID",      f"`{m.id}`",                                True),
        ("Bot ?",   "✅" if m.bot else "❌",                     True),
        ("Warns",   warn_counts[m.id],                          True),
        ("Rejoint", discord.utils.format_dt(m.joined_at,  "R"), True),
        ("Inscrit", discord.utils.format_dt(m.created_at, "R"), True),
        ("Statut",  str(m.status).capitalize(),                 True),
        ("Rôles",   " ".join(roles[:10]) or "Aucun",            False),
    ], thumb=m.display_avatar.url)
    await ctx.send(embed=emb)

@bot.command(name="serverinfo", aliases=["si", "server"])
async def cmd_serverinfo(ctx):
    g = ctx.guild
    emb = make_embed(f"🏠 {g.name}", color="info", fields=[
        ("ID",         g.id,                                            True),
        ("Membres",    g.member_count,                                  True),
        ("Bots",       sum(1 for m in g.members if m.bot),             True),
        ("Salons",     len(g.text_channels),                            True),
        ("Vocaux",     len(g.voice_channels),                           True),
        ("Rôles",      len(g.roles),                                    True),
        ("Créé",       discord.utils.format_dt(g.created_at, "R"),      True),
        ("Boosts",     g.premium_subscription_count,                    True),
        ("Niveau",     g.premium_tier,                                  True),
        ("Propriétaire", g.owner.mention if g.owner else "?",          False),
    ], thumb=g.icon.url if g.icon else "")
    await ctx.send(embed=emb)

@bot.command(name="help", aliases=["h", "aide"])
async def cmd_help(ctx):
    emb = make_embed("🛡️ GuardBot v2 — Aide", color="info")

    emb.add_field(name="━━ Modération ━━", value="\u200b", inline=False)
    for cmd, desc in [
        ("`!warn @u [raison]`",        "Avertir"),
        ("`!mute @u [min] [raison]`",  "Mute"),
        ("`!unmute @u`",               "Dé-mute"),
        ("`!kick @u [raison]`",        "Kick"),
        ("`!ban @u [raison]`",         "Ban *(admin)*"),
        ("`!softban @u [raison]`",     "Softban *(admin)*"),
        ("`!unban <ID>`",              "Unban *(admin)*"),
        ("`!warns [@u]`",              "Voir warns"),
        ("`!clearwarns @u`",           "Reset warns *(admin)*"),
        ("`!clear [n] [@u]`",          "Supprimer messages (max 200)"),
    ]:
        emb.add_field(name=cmd, value=desc, inline=True)

    emb.add_field(name="━━ Serveur ━━", value="\u200b", inline=False)
    for cmd, desc in [
        ("`!slowmode [sec]`",   "Slowmode"),
        ("`!lock [#salon]`",    "Verrouiller *(admin)*"),
        ("`!unlock [#salon]`",  "Déverrouiller *(admin)*"),
        ("`!lockall`",          "Tout verrouiller *(admin)*"),
        ("`!unlockall`",        "Tout déverrouiller *(admin)*"),
        ("`!raidoff`",          "Désactiver mode raid *(admin)*"),
        ("`!userinfo [@u]`",    "Infos utilisateur"),
        ("`!serverinfo`",       "Infos serveur"),
    ]:
        emb.add_field(name=cmd, value=desc, inline=True)

    emb.add_field(name="━━ Protections auto ━━", value=(
        "🔹 Anti-Spam · Anti-Lien · Anti-Invite Discord\n"
        "🔹 Anti-Raid (slowmode/lockdown auto)\n"
        "🔹 Anti-Caps · Anti-Mention Spam · Anti-Duplicate\n"
        "🔹 Anti-Bot · Anti-Swear\n"
        "🔹 Logs : messages, bans, joins, rôles, vocaux, pseudos"
    ), inline=False)
    await ctx.send(embed=emb)

# ══════════════════════════════════════════════════════════
#  GESTION ERREURS
# ══════════════════════════════════════════════════════════

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Membre introuvable.", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argument invalide.", delete_after=5)
    elif isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        pass
    else:
        log.error(f"[{ctx.command}] {error}")

# ══════════════════════════════════════════════════════════
#  LANCEMENT
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    if cfg.token in ("TON_TOKEN_ICI", ""):
        print("❌  Définis ton token :")
        print("    DISCORD_TOKEN=ton_token python guardbot.py")
        raise SystemExit(1)
    log.info("Démarrage de GuardBot v2…")
    bot.run(cfg.token, log_handler=None)
