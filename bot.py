import discord
from discord.ext import commands, tasks
from collections import defaultdict, deque
from datetime import datetime, timezone
import asyncio
import os

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

DEFAULT_CONFIG = {
    "enabled": True,
    "max_bans": 3,
    "max_kicks": 5,
    "max_channel_deletes": 4,
    "max_role_deletes": 4,
    "max_webhook_creates": 10,
    "time_window": 10,
    "punishment": "ban",
    "restore_channels": True,
    "restore_roles": True,
    "purge_webhooks": True,
    "raid_join_threshold": 15,
    "raid_mode": False,
    "log_channel_id": None,
}

guild_configs = {}
guild_action_trackers = defaultdict(lambda: defaultdict(lambda: deque(maxlen=100)))
guild_join_tracker = defaultdict(lambda: deque(maxlen=200))

intents = discord.Intents.default()
intents.members = True
intents.bans = True
intents.guilds = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------- HELPERS --------------------
def get_config(guild_id):
    if guild_id not in guild_configs:
        guild_configs[guild_id] = DEFAULT_CONFIG.copy()
    return guild_configs[guild_id]

def is_whitelisted(guild_id, user_id, roles):
    cfg = get_config(guild_id)
    if user_id in cfg.get("whitelist_users", []):
        return True
    for role in roles:
        if role.id in cfg.get("whitelist_roles", []):
            return True
    return False

async def log_event(guild_id, embed):
    cfg = get_config(guild_id)
    channel_id = cfg.get("log_channel_id")
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send(embed=embed)

async def punish(guild, user, action_type, reason):
    cfg = get_config(guild.id)
    punishment = cfg["punishment"]
    embed = discord.Embed(
        title="🚨 ANTI-NUKE TRIGGERED 🚨",
        description=f"**User:** {user.mention}\n**Action:** {action_type}\n**Reason:** {reason}",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"User ID: {user.id}")

    if punishment == "ban":
        try:
            await guild.ban(user, reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Punishment", value="Banned", inline=False)
        except:
            embed.add_field(name="Punishment", value="❌ Ban failed (check permissions)", inline=False)
    elif punishment == "kick":
        try:
            await guild.kick(user, reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Punishment", value="Kicked", inline=False)
        except:
            embed.add_field(name="Punishment", value="❌ Kick failed", inline=False)
    elif punishment == "strip_roles":
        try:
            member = await guild.fetch_member(user.id)
            await member.edit(roles=[], reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Punishment", value="All roles stripped", inline=False)
        except:
            embed.add_field(name="Punishment", value="❌ Could not strip roles", inline=False)
    else:
        embed.add_field(name="Punishment", value="⚠️ Alert only (no action taken)", inline=False)

    await log_event(guild.id, embed)

async def check_action(guild, user, action_type):
    cfg = get_config(guild.id)
    if not cfg["enabled"]:
        return
    if user == guild.owner:
        return
    try:
        member = await guild.fetch_member(user.id)
        if is_whitelisted(guild.id, user.id, member.roles):
            return
    except:
        return

    now = datetime.now(timezone.utc).timestamp()
    window = cfg["time_window"]
    limit_map = {
        "ban": cfg["max_bans"],
        "kick": cfg["max_kicks"],
        "channel_delete": cfg["max_channel_deletes"],
        "role_delete": cfg["max_role_deletes"],
        "webhook_create": cfg["max_webhook_creates"],
    }
    limit = limit_map.get(action_type, 0)
    if limit == 0:
        return

    tracker = guild_action_trackers[guild.id][user.id]
    tracker.append((now, action_type))
    while tracker and tracker[0][0] < now - window:
        tracker.popleft()
    count = sum(1 for _, t in tracker if t == action_type)
    if count >= limit:
        await punish(guild, user, action_type, f"Exceeded {limit} {action_type}s in {window}s")
        guild_action_trackers[guild.id][user.id].clear()

async def create_backup_channel(guild, entry):
    name = getattr(entry.extra, "name", "restored-channel")
    category_id = getattr(entry.extra, "category_id", None)
    try:
        await guild.create_text_channel(name, category=category_id)
        await log_event(guild.id, discord.Embed(description=f"🔄 Restored channel `{name}`", color=discord.Color.green()))
    except:
        pass

async def create_backup_role(guild, entry):
    name = getattr(entry.extra, "name", "restored-role")
    permissions = getattr(entry.extra, "permissions", discord.Permissions.none())
    try:
        await guild.create_role(name=name, permissions=permissions)
        await log_event(guild.id, discord.Embed(description=f"🔄 Restored role `{name}`", color=discord.Color.green()))
    except:
        pass

async def purge_all_webhooks(guild):
    for channel in guild.text_channels:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            await webhook.delete()
    await log_event(guild.id, discord.Embed(description="🧹 Purged all webhooks", color=discord.Color.orange()))

# -------------------- AUDIT LOG LISTENER --------------------
@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    guild = entry.guild
    user = entry.user
    if user == bot.user:
        return

    action_map = {
        discord.AuditLogAction.ban: "ban",
        discord.AuditLogAction.kick: "kick",
        discord.AuditLogAction.channel_delete: "channel_delete",
        discord.AuditLogAction.role_delete: "role_delete",
        discord.AuditLogAction.webhook_create: "webhook_create",
    }
    if entry.action in action_map:
        await check_action(guild, user, action_map[entry.action])
        cfg = get_config(guild.id)
        if entry.action == discord.AuditLogAction.channel_delete and cfg["restore_channels"]:
            await create_backup_channel(guild, entry)
        elif entry.action == discord.AuditLogAction.role_delete and cfg["restore_roles"]:
            await create_backup_role(guild, entry)
        elif entry.action == discord.AuditLogAction.webhook_create and cfg["purge_webhooks"]:
            # If many webhooks created quickly, purge all
            await purge_all_webhooks(guild)

# -------------------- RAID DETECTION --------------------
@bot.event
async def on_member_join(member):
    guild = member.guild
    cfg = get_config(guild.id)
    if not cfg["enabled"]:
        return

    now = datetime.now(timezone.utc).timestamp()
    tracker = guild_join_tracker[guild.id]
    tracker.append(now)
    while tracker and tracker[0] < now - 10:
        tracker.popleft()

    if len(tracker) >= cfg["raid_join_threshold"] and not cfg["raid_mode"]:
        cfg["raid_mode"] = True
        await log_event(guild.id, discord.Embed(
            title="⚠️ RAID DETECTED",
            description=f"{len(tracker)} members joined in last 10 seconds. Raid mode enabled.",
            color=discord.Color.red()
        ))
    elif len(tracker) < cfg["raid_join_threshold"] // 2 and cfg["raid_mode"]:
        cfg["raid_mode"] = False
        await log_event(guild.id, discord.Embed(description="✅ Raid mode disabled", color=discord.Color.green()))

# -------------------- SLASH COMMANDS --------------------
# ----- Main antinuke command group -----
@bot.tree.command(name="antinuke", description="View or change anti-nuke settings")
@commands.has_permissions(administrator=True)
async def antinuke(interaction: discord.Interaction, action: str = None, key: str = None, value: str = None):
    if action is None:
        await interaction.response.send_message("Use `/antinuke status` or `/antinuke set <key> <value>`", ephemeral=True)
        return

    cfg = get_config(interaction.guild_id)

    if action == "status":
        embed = discord.Embed(title="🛡️ Anti-Nuke Configuration", color=discord.Color.blurple())
        for k, v in cfg.items():
            if k not in ["whitelist_users", "whitelist_roles", "admin_roles"]:
                embed.add_field(name=k, value=str(v), inline=False)
        embed.set_footer(text="Use /antinuke set <key> <value> to change a setting")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    elif action == "set" and key and value:
        if key in cfg:
            # Convert value type
            if isinstance(cfg[key], bool):
                val = value.lower() == "true"
            elif isinstance(cfg[key], int):
                try:
                    val = int(value)
                except:
                    await interaction.response.send_message("❌ Value must be a number", ephemeral=True)
                    return
            else:
                val = value
            cfg[key] = val
            await interaction.response.send_message(f"✅ Set `{key}` to `{val}`", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Unknown key: {key}\nValid keys: {', '.join(cfg.keys())}", ephemeral=True)

    elif action == "enable":
        cfg["enabled"] = True
        await interaction.response.send_message("✅ Anti-nuke protection **enabled**", ephemeral=True)

    elif action == "disable":
        cfg["enabled"] = False
        await interaction.response.send_message("⚠️ Anti-nuke protection **disabled**", ephemeral=True)

    elif action == "set_punishment":
        if not key:
            await interaction.response.send_message("Usage: `/antinuke set_punishment ban|kick|strip_roles|alert`", ephemeral=True)
            return
        if key in ["ban", "kick", "strip_roles", "alert"]:
            cfg["punishment"] = key
            await interaction.response.send_message(f"✅ Punishment set to `{key}`", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Invalid punishment. Options: ban, kick, strip_roles, alert", ephemeral=True)

    elif action == "set_logs":
        if not key:
            await interaction.response.send_message("Usage: `/antinuke set_logs #channel` or provide channel ID", ephemeral=True)
            return
        # key can be a channel mention or ID
        channel = None
        if key.isdigit():
            channel = interaction.guild.get_channel(int(key))
        else:
            # try to parse mention
            mention = key.strip('<#>')
            if mention.isdigit():
                channel = interaction.guild.get_channel(int(mention))
        if channel:
            cfg["log_channel_id"] = channel.id
            await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Invalid channel. Please mention a channel or provide its ID.", ephemeral=True)

    else:
        await interaction.response.send_message("Invalid subcommand. Use: status, set, enable, disable, set_punishment, set_logs", ephemeral=True)

# ----- Separate help command -----
@bot.tree.command(name="help", description="Show all commands and what they do")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Anti-Nuke Bot – Command List",
        description="All commands use slash (`/`). You need `Administrator` permission to change settings.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="/antinuke status",
        value="Shows current anti‑nuke settings (thresholds, punishment, etc.)",
        inline=False
    )
    embed.add_field(
        name="/antinuke set <key> <value>",
        value="Change a setting. Example: `/antinuke set max_bans 5`\nKeys: `max_bans`, `max_kicks`, `max_channel_deletes`, `max_role_deletes`, `max_webhook_creates`, `time_window`, `punishment`, `restore_channels`, `restore_roles`, `purge_webhooks`, `raid_join_threshold`, `enabled`",
        inline=False
    )
    embed.add_field(
        name="/antinuke enable",
        value="Turns anti‑nuke protection ON",
        inline=False
    )
    embed.add_field(
        name="/antinuke disable",
        value="Turns anti‑nuke protection OFF (not recommended)",
        inline=False
    )
    embed.add_field(
        name="/antinuke set_punishment <type>",
        value="Change punishment: `ban`, `kick`, `strip_roles`, `alert`",
        inline=False
    )
    embed.add_field(
        name="/antinuke set_logs #channel",
        value="Set a channel for alert logs",
        inline=False
    )
    embed.add_field(
        name="/help",
        value="Shows this message",
        inline=False
    )

    embed.set_footer(text="Your server is protected 24/7 🛡️")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----- Simple ping command -----
@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms")

# -------------------- BACKGROUND TASKS --------------------
@tasks.loop(minutes=5)
async def cleanup_trackers():
    now = datetime.now(timezone.utc).timestamp()
    for guild_id, users in guild_action_trackers.items():
        for user_id, deque_ in list(users.items()):
            while deque_ and deque_[0][0] < now - 60:
                deque_.popleft()
            if not deque_:
                del guild_action_trackers[guild_id][user_id]

@tasks.loop(minutes=1)
async def raid_auto_disable():
    for guild_id, tracker in guild_join_tracker.items():
        cfg = get_config(guild_id)
        if cfg.get("raid_mode", False) and len(tracker) == 0:
            cfg["raid_mode"] = False
            # Optionally send log
            await log_event(guild_id, discord.Embed(description="✅ Raid mode auto-disabled", color=discord.Color.green()))

@bot.event
async def on_ready():
    await bot.tree.sync()
    cleanup_trackers.start()
    raid_auto_disable.start()
    print(f"✅ Anti-nuke bot online as {bot.user}")
    print("Commands synced globally (may take a few minutes to appear)")

# -------------------- RUN --------------------
if __name__ == "__main__":
    bot.run(TOKEN)