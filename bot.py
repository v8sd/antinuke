import discord
from discord.ext import commands, tasks
from collections import defaultdict, deque
from datetime import datetime, timezone
import asyncio
import os

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

# ========== CONFIGURATION ==========
DEFAULT_CONFIG = {
    "enabled": True,
    "max_bans": 3,
    "max_kicks": 5,
    "max_channel_deletes": 4,
    "max_role_deletes": 4,
    "max_webhook_creates": 3,
    "max_integration_create": 2,      # Authorized apps (OAuth2)
    "max_channel_creates": 5,
    "max_role_creates": 5,
    "max_permission_updates": 3,
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
start_time = datetime.now(timezone.utc)

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
        title="🚨 **ANTI-NUKE TRIGGERED** 🚨",
        description=f"**User:** {user.mention}\n**Action:** `{action_type}`\n**Reason:** `{reason}`",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"User ID: {user.id}")
    embed.add_field(name="Punishment", value=f"**{punishment.upper()}**", inline=False)

    if punishment == "ban":
        try:
            await guild.ban(user, reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Status", value="✅ Banned", inline=False)
        except Exception as e:
            embed.add_field(name="Status", value=f"❌ Ban failed: {e}", inline=False)
    elif punishment == "kick":
        try:
            await guild.kick(user, reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Status", value="✅ Kicked", inline=False)
        except Exception as e:
            embed.add_field(name="Status", value=f"❌ Kick failed: {e}", inline=False)
    elif punishment == "strip_roles":
        try:
            member = await guild.fetch_member(user.id)
            await member.edit(roles=[], reason=f"Anti-nuke: {reason}")
            embed.add_field(name="Status", value="✅ Roles stripped", inline=False)
        except Exception as e:
            embed.add_field(name="Status", value=f"❌ Could not strip roles: {e}", inline=False)
    else:
        embed.add_field(name="Status", value="⚠️ Alert only", inline=False)

    await log_event(guild.id, embed)

async def check_action(guild, user, action_type, extra_count=1):
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
        "integration_create": cfg["max_integration_create"],
        "channel_create": cfg["max_channel_creates"],
        "role_create": cfg["max_role_creates"],
        "permission_update": cfg["max_permission_updates"],
    }
    limit = limit_map.get(action_type, 0)
    if limit == 0:
        return

    tracker = guild_action_trackers[guild.id][user.id]
    for _ in range(extra_count):
        tracker.append((now, action_type))
    while tracker and tracker[0][0] < now - window:
        tracker.popleft()
    count = sum(1 for _, t in tracker if t == action_type)
    if count >= limit:
        await punish(guild, user, action_type, f"Exceeded {limit} {action_type}s in {window}s")
        guild_action_trackers[guild.id][user.id].clear()

async def restore_channel(guild, entry):
    try:
        name = getattr(entry.extra, 'name', 'restored-channel')
        category_id = getattr(entry.extra, 'category_id', None)
        overwrites = getattr(entry.extra, 'overwrites', {})
        new_channel = await guild.create_text_channel(
            name=name,
            category=guild.get_channel(category_id) if category_id else None,
            overwrites=overwrites
        )
        await log_event(guild.id, discord.Embed(
            description=f"🔄 Restored channel `{new_channel.name}`",
            color=discord.Color.green()
        ))
    except Exception as e:
        await log_event(guild.id, discord.Embed(description=f"❌ Failed to restore channel: {e}", color=discord.Color.red()))

async def restore_role(guild, entry):
    try:
        name = getattr(entry.extra, 'name', 'restored-role')
        permissions = getattr(entry.extra, 'permissions', discord.Permissions.none())
        colour = getattr(entry.extra, 'colour', discord.Color.default())
        hoist = getattr(entry.extra, 'hoist', False)
        mentionable = getattr(entry.extra, 'mentionable', False)
        new_role = await guild.create_role(
            name=name, permissions=permissions, colour=colour, hoist=hoist, mentionable=mentionable
        )
        await log_event(guild.id, discord.Embed(
            description=f"🔄 Restored role `{new_role.name}`",
            color=discord.Color.green()
        ))
    except Exception as e:
        await log_event(guild.id, discord.Embed(description=f"❌ Failed to restore role: {e}", color=discord.Color.red()))

async def purge_all_webhooks(guild):
    count = 0
    for channel in guild.text_channels:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            try:
                await webhook.delete()
                count += 1
            except:
                pass
    if count:
        await log_event(guild.id, discord.Embed(description=f"🧹 Purged {count} webhooks", color=discord.Color.orange()))

# -------------------- AUDIT LOG LISTENER --------------------
@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    guild = entry.guild
    user = entry.user
    if user == bot.user:
        return

    cfg = get_config(guild.id)

    # ---- Detection & Punishment ----
    action_map = {
        discord.AuditLogAction.ban: ("ban", 1),
        discord.AuditLogAction.kick: ("kick", 1),
        discord.AuditLogAction.channel_delete: ("channel_delete", 1),
        discord.AuditLogAction.role_delete: ("role_delete", 1),
        discord.AuditLogAction.webhook_create: ("webhook_create", 1),
        discord.AuditLogAction.integration_create: ("integration_create", 1),
        discord.AuditLogAction.channel_create: ("channel_create", 1),
        discord.AuditLogAction.role_create: ("role_create", 1),
        discord.AuditLogAction.overwrite_create: ("permission_update", 1),
        discord.AuditLogAction.overwrite_update: ("permission_update", 1),
        discord.AuditLogAction.overwrite_delete: ("permission_update", 1),
    }
    if entry.action in action_map:
        action_type, weight = action_map[entry.action]
        await check_action(guild, user, action_type, weight)
        # If it's an integration creation, also send an informative alert (even if under threshold)
        if entry.action == discord.AuditLogAction.integration_create:
            embed = discord.Embed(
                title="🔌 **Integration (Authorized App) Created**",
                description=f"**User:** {user.mention}\n**Integration Name:** {entry.target.name if entry.target else 'Unknown'}",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            await log_event(guild.id, embed)

    # ---- Auto-restore ----
    if cfg["restore_channels"] and entry.action == discord.AuditLogAction.channel_delete:
        await restore_channel(guild, entry)
    if cfg["restore_roles"] and entry.action == discord.AuditLogAction.role_delete:
        await restore_role(guild, entry)

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
            description=f"{len(tracker)} members joined in 10 seconds. Raid mode enabled.",
            color=discord.Color.red()
        ))
    elif len(tracker) < cfg["raid_join_threshold"] // 2 and cfg["raid_mode"]:
        cfg["raid_mode"] = False
        await log_event(guild.id, discord.Embed(description="✅ Raid mode disabled", color=discord.Color.green()))

# -------------------- SLASH COMMANDS (with logging) --------------------
async def log_setting_change(guild_id, user, setting, old_value, new_value):
    embed = discord.Embed(
        title="⚙️ **Anti-Nuke Setting Changed**",
        description=f"**Changed by:** {user.mention}\n**Setting:** `{setting}`\n**Old value:** `{old_value}`\n**New value:** `{new_value}`",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    await log_event(guild_id, embed)

@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ **Anti-Nuke Bot – Help**",
        description="All commands are slash commands. You need `Administrator` permission to change settings.",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📊 `/status`", value="Show current anti‑nuke settings", inline=False)
    embed.add_field(name="✅ `/enable`", value="Turn on protection", inline=False)
    embed.add_field(name="❌ `/disable`", value="Turn off protection (not recommended)", inline=False)
    embed.add_field(name="⚙️ `/set`", value="Change a setting. Example: `/set max_bans 5`", inline=False)
    embed.add_field(name="🔨 `/punishment <type>`", value="`ban`, `kick`, `strip_roles`, `alert`", inline=False)
    embed.add_field(name="📝 `/setlogs #channel`", value="Set the log channel", inline=False)
    embed.add_field(name="🏓 `/ping`", value="Check bot latency", inline=False)
    embed.set_footer(text="Your server is protected 24/7 🛡️")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="status", description="Show anti-nuke configuration")
@commands.has_permissions(administrator=True)
async def status_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    embed = discord.Embed(
        title="🛡️ **Anti-Nuke Status**",
        color=discord.Color.blue() if cfg["enabled"] else discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Protection", value="✅ **ON**" if cfg["enabled"] else "❌ **OFF**", inline=True)
    embed.add_field(name="Punishment", value=f"**{cfg['punishment'].upper()}**", inline=True)
    embed.add_field(name="Time Window", value=f"{cfg['time_window']} seconds", inline=True)
    embed.add_field(name="Max Bans", value=str(cfg["max_bans"]), inline=True)
    embed.add_field(name="Max Kicks", value=str(cfg["max_kicks"]), inline=True)
    embed.add_field(name="Max Channel Deletes", value=str(cfg["max_channel_deletes"]), inline=True)
    embed.add_field(name="Max Role Deletes", value=str(cfg["max_role_deletes"]), inline=True)
    embed.add_field(name="Max Webhook Creates", value=str(cfg["max_webhook_creates"]), inline=True)
    embed.add_field(name="Max Integration Creates", value=str(cfg["max_integration_create"]), inline=True)
    embed.add_field(name="Max Channel Creates", value=str(cfg["max_channel_creates"]), inline=True)
    embed.add_field(name="Max Role Creates", value=str(cfg["max_role_creates"]), inline=True)
    embed.add_field(name="Max Permission Updates", value=str(cfg["max_permission_updates"]), inline=True)
    embed.add_field(name="Restore Channels", value="✅" if cfg["restore_channels"] else "❌", inline=True)
    embed.add_field(name="Restore Roles", value="✅" if cfg["restore_roles"] else "❌", inline=True)
    embed.add_field(name="Raid Join Threshold", value=f"{cfg['raid_join_threshold']} in 10s", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="enable", description="Turn on anti-nuke protection")
@commands.has_permissions(administrator=True)
async def enable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = True
    await log_setting_change(interaction.guild_id, interaction.user, "enabled", old, True)
    embed = discord.Embed(description="✅ **Anti-nuke protection is now ON**", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="disable", description="Turn off anti-nuke protection (not recommended)")
@commands.has_permissions(administrator=True)
async def disable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = False
    await log_setting_change(interaction.guild_id, interaction.user, "enabled", old, False)
    embed = discord.Embed(description="⚠️ **Anti-nuke protection is now OFF**", color=discord.Color.red())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set", description="Change a setting (e.g., /set max_bans 5)")
@commands.has_permissions(administrator=True)
async def set_cmd(interaction: discord.Interaction, setting: str, value: str):
    cfg = get_config(interaction.guild_id)
    if setting not in cfg:
        await interaction.response.send_message(f"❌ Unknown setting `{setting}`. Use `/status` to see available settings.", ephemeral=True)
        return
    
    old = cfg[setting]
    if isinstance(cfg[setting], bool):
        val = value.lower() == "true"
    elif isinstance(cfg[setting], int):
        try:
            val = int(value)
        except:
            await interaction.response.send_message("❌ Value must be a number", ephemeral=True)
            return
    else:
        val = value
    
    cfg[setting] = val
    await log_setting_change(interaction.guild_id, interaction.user, setting, old, val)
    embed = discord.Embed(description=f"✅ Changed `{setting}` from `{old}` to `{val}`", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="punishment", description="Set punishment type: ban, kick, strip_roles, alert")
@commands.has_permissions(administrator=True)
async def punishment_cmd(interaction: discord.Interaction, punishment: str):
    if punishment not in ["ban", "kick", "strip_roles", "alert"]:
        await interaction.response.send_message("❌ Invalid punishment. Choose: `ban`, `kick`, `strip_roles`, `alert`", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    old = cfg["punishment"]
    cfg["punishment"] = punishment
    await log_setting_change(interaction.guild_id, interaction.user, "punishment", old, punishment)
    embed = discord.Embed(description=f"✅ Punishment set to `{punishment}`", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setlogs", description="Set the channel for alert logs")
@commands.has_permissions(administrator=True)
async def setlogs_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config(interaction.guild_id)
    old = cfg.get("log_channel_id")
    cfg["log_channel_id"] = channel.id
    await log_setting_change(interaction.guild_id, interaction.user, "log_channel_id", old, channel.id)
    embed = discord.Embed(description=f"✅ Logs will be sent to {channel.mention}", color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping_cmd(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{latency}ms**\nBot is **online**",
        color=discord.Color.green() if latency < 200 else discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------- BACKGROUND TASKS --------------------
@tasks.loop(minutes=5)
async def cleanup_trackers():
    now = datetime.now(timezone.utc).timestamp()
    for guild_id, users in list(guild_action_trackers.items()):
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
            await log_event(guild_id, discord.Embed(description="✅ Raid mode auto-disabled", color=discord.Color.green()))

@bot.event
async def on_ready():
    await bot.tree.sync()
    cleanup_trackers.start()
    raid_auto_disable.start()
    print(f"✅ Anti-nuke bot online as {bot.user}")
    print("Slash commands synced! Use /help in Discord.")

# -------------------- RUN --------------------
if __name__ == "__main__":
    bot.run(TOKEN)