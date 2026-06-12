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
    "max_integration_creates": 2,
    "max_channel_creates": 5,
    "max_role_creates": 5,
    "max_permission_updates": 3,
    "time_window": 10,
    "punishment": "ban",
    "restore_channels": True,
    "restore_roles": True,
    "purge_webhooks_on_nuke": True,
    "raid_join_threshold": 15,
    "raid_mode": False,
    "log_channel_id": None,
    "whitelist_users": [],
    "whitelist_roles": [],
}

guild_configs = {}
action_tracker = defaultdict(lambda: defaultdict(lambda: deque(maxlen=200)))
join_tracker = defaultdict(lambda: deque(maxlen=200))

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
    if user_id in cfg["whitelist_users"]:
        return True
    for role in roles:
        if role.id in cfg["whitelist_roles"]:
            return True
    return False

async def log_to_channel(guild_id, embed):
    cfg = get_config(guild_id)
    ch_id = cfg.get("log_channel_id")
    if ch_id:
        channel = bot.get_channel(ch_id)
        if channel:
            await channel.send(embed=embed)

async def punish_user(guild, user, action, reason):
    cfg = get_config(guild.id)
    punishment = cfg["punishment"]
    embed = discord.Embed(
        title="🚨 **ANTI-NUKE TRIGGERED** 🚨",
        description=f"**User:** {user.mention}\n**Action:** `{action}`\n**Reason:** {reason}",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"User ID: {user.id}")
    embed.add_field(name="Punishment", value=f"**{punishment.upper()}**", inline=False)

    success = False
    if punishment == "ban":
        try:
            await guild.ban(user, reason=f"Anti-nuke: {reason}")
            success = True
        except Exception as e:
            embed.add_field(name="Error", value=str(e), inline=False)
    elif punishment == "kick":
        try:
            await guild.kick(user, reason=f"Anti-nuke: {reason}")
            success = True
        except Exception as e:
            embed.add_field(name="Error", value=str(e), inline=False)
    elif punishment == "strip_roles":
        try:
            member = await guild.fetch_member(user.id)
            await member.edit(roles=[], reason=f"Anti-nuke: {reason}")
            success = True
        except Exception as e:
            embed.add_field(name="Error", value=str(e), inline=False)
    else:
        embed.add_field(name="Status", value="⚠️ Alert only (no action)", inline=False)
        success = True

    if success and punishment in ["ban", "kick", "strip_roles"]:
        embed.add_field(name="Status", value="✅ Action applied", inline=False)
    await log_to_channel(guild.id, embed)

async def check_and_punish(guild, user, action_type, count=1):
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
        pass

    limit_map = {
        "ban": cfg["max_bans"],
        "kick": cfg["max_kicks"],
        "channel_delete": cfg["max_channel_deletes"],
        "role_delete": cfg["max_role_deletes"],
        "webhook_create": cfg["max_webhook_creates"],
        "integration_create": cfg["max_integration_creates"],
        "channel_create": cfg["max_channel_creates"],
        "role_create": cfg["max_role_creates"],
        "permission_update": cfg["max_permission_updates"],
    }
    limit = limit_map.get(action_type, 0)
    if limit == 0:
        return

    now = datetime.now(timezone.utc).timestamp()
    window = cfg["time_window"]
    tracker = action_tracker[guild.id][user.id]
    for _ in range(count):
        tracker.append((now, action_type))
    while tracker and tracker[0][0] < now - window:
        tracker.popleft()
    total = sum(1 for _, t in tracker if t == action_type)
    if total >= limit:
        await punish_user(guild, user, action_type, f"Exceeded {limit} {action_type}s in {window}s")
        action_tracker[guild.id][user.id].clear()

async def restore_deleted_channel(guild, entry):
    try:
        name = getattr(entry.extra, 'name', 'restored-channel')
        category_id = getattr(entry.extra, 'category_id', None)
        overwrites = getattr(entry.extra, 'overwrites', {})
        new_ch = await guild.create_text_channel(
            name=name,
            category=guild.get_channel(category_id) if category_id else None,
            overwrites=overwrites
        )
        await log_to_channel(guild.id, discord.Embed(
            description=f"🔄 Restored channel `{new_ch.name}` (original name preserved)",
            color=discord.Color.green()
        ))
    except Exception as e:
        await log_to_channel(guild.id, discord.Embed(
            description=f"❌ Failed to restore channel: {e}", color=discord.Color.red()
        ))

async def restore_deleted_role(guild, entry):
    try:
        name = getattr(entry.extra, 'name', 'restored-role')
        perms = getattr(entry.extra, 'permissions', discord.Permissions.none())
        colour = getattr(entry.extra, 'colour', discord.Color.default())
        hoist = getattr(entry.extra, 'hoist', False)
        mentionable = getattr(entry.extra, 'mentionable', False)
        new_role = await guild.create_role(
            name=name, permissions=perms, colour=colour, hoist=hoist, mentionable=mentionable
        )
        await log_to_channel(guild.id, discord.Embed(
            description=f"🔄 Restored role `{new_role.name}` (original name preserved)",
            color=discord.Color.green()
        ))
    except Exception as e:
        await log_to_channel(guild.id, discord.Embed(
            description=f"❌ Failed to restore role: {e}", color=discord.Color.red()
        ))

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
        await log_to_channel(guild.id, discord.Embed(
            description=f"🧹 Purged {count} webhooks (webhook spam detected)",
            color=discord.Color.orange()
        ))

# -------------------- RAID DETECTION & AUTO-KICK --------------------
@bot.event
async def on_member_join(member):
    guild = member.guild
    cfg = get_config(guild.id)
    if not cfg["enabled"]:
        return
    now = datetime.now(timezone.utc).timestamp()
    tracker = join_tracker[guild.id]
    tracker.append(now)
    while tracker and tracker[0] < now - 10:
        tracker.popleft()
    threshold = cfg["raid_join_threshold"]
    if len(tracker) >= threshold and not cfg["raid_mode"]:
        cfg["raid_mode"] = True
        await log_to_channel(guild.id, discord.Embed(
            title="⚠️ **RAID DETECTED**",
            description=f"{len(tracker)} members joined in the last 10 seconds. Raid mode enabled.",
            color=discord.Color.red()
        ))
    elif len(tracker) < threshold // 2 and cfg["raid_mode"]:
        cfg["raid_mode"] = False
        await log_to_channel(guild.id, discord.Embed(
            description="✅ Raid mode disabled – join rate back to normal.",
            color=discord.Color.green()
        ))

    # If raid mode is active, kick the new member
    if cfg["raid_mode"]:
        try:
            await member.kick(reason="Raid mode active – auto protection")
            await log_to_channel(guild.id, discord.Embed(
                description=f"🔨 Kicked {member.mention} during raid mode",
                color=discord.Color.red()
            ))
        except:
            pass

# -------------------- AUDIT LOG MONITORING --------------------
@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    guild = entry.guild
    user = entry.user
    if user == bot.user:
        return

    cfg = get_config(guild.id)

    action_info = {
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

    if entry.action in action_info:
        act, weight = action_info[entry.action]
        await check_and_punish(guild, user, act, weight)

        if entry.action == discord.AuditLogAction.integration_create:
            embed = discord.Embed(
                title="🔌 **Integration (Authorized App) Created**",
                description=f"**User:** {user.mention}\n**Integration Name:** {getattr(entry.target, 'name', 'Unknown')}",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc)
            )
            await log_to_channel(guild.id, embed)

        if entry.action == discord.AuditLogAction.webhook_create:
            embed = discord.Embed(
                title="📡 **Webhook Created**",
                description=f"**User:** {user.mention}\n**Channel:** <#{entry.extra.channel_id if hasattr(entry.extra, 'channel_id') else 'unknown'}>",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc)
            )
            await log_to_channel(guild.id, embed)

    if cfg["restore_channels"] and entry.action == discord.AuditLogAction.channel_delete:
        await restore_deleted_channel(guild, entry)
    if cfg["restore_roles"] and entry.action == discord.AuditLogAction.role_delete:
        await restore_deleted_role(guild, entry)

    if cfg["purge_webhooks_on_nuke"] and entry.action == discord.AuditLogAction.webhook_create:
        now = datetime.now(timezone.utc).timestamp()
        window = cfg["time_window"]
        tracker = action_tracker[guild.id][user.id]
        recent_webhooks = [t for t, a in tracker if a == "webhook_create" and now - t <= window]
        if len(recent_webhooks) >= cfg["max_webhook_creates"]:
            await purge_all_webhooks(guild)

# -------------------- LOCKDOWN / UNLOCKDOWN (FIXED) --------------------
@bot.tree.command(name="lockdown", description="Lock down the server: disable messages, delete webhooks")
async def lockdown_cmd(interaction: discord.Interaction):
    # Owner only
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return

    # Permission check
    me = interaction.guild.me
    missing = []
    if not me.guild_permissions.manage_channels:
        missing.append("Manage Channels")
    if not me.guild_permissions.manage_webhooks:
        missing.append("Manage Webhooks")
    if missing:
        await interaction.response.send_message(f"❌ Bot is missing permissions: {', '.join(missing)}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    locked_channels = 0
    failed_channels = 0
    deleted_webhooks = 0
    errors = []

    # 1. Lock text channels
    for channel in guild.text_channels:
        try:
            # Get current overwrite for @everyone
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            locked_channels += 1
        except Exception as e:
            failed_channels += 1
            errors.append(f"#{channel.name}: {str(e)[:50]}")

    # 2. Delete all webhooks
    for channel in guild.text_channels:
        try:
            webhooks = await channel.webhooks()
            for webhook in webhooks:
                await webhook.delete()
                deleted_webhooks += 1
        except:
            pass

    # 3. Disconnect all voice users and deny connect
    for channel in guild.voice_channels:
        try:
            # Disconnect members
            for member in channel.members:
                await member.move_to(None)
            # Deny connect for @everyone
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.connect = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except:
            pass

    embed = discord.Embed(
        title="🔒 **SERVER LOCKDOWN COMPLETE**",
        description=f"**Issued by:** {interaction.user.mention}\n"
                    f"🔒 **Channels locked:** {locked_channels} (failed: {failed_channels})\n"
                    f"🧹 **Webhooks deleted:** {deleted_webhooks}\n"
                    f"🔊 **Voice channels disabled**",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )
    if errors:
        embed.add_field(name="⚠️ Some channels failed", value="\n".join(errors[:5]), inline=False)

    await interaction.followup.send(embed=embed)
    await log_to_channel(guild.id, embed)

@bot.tree.command(name="unlockdown", description="Unlock the server (restore @everyone send_messages)")
async def unlockdown_cmd(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return

    me = interaction.guild.me
    if not me.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ Bot is missing `Manage Channels` permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    unlocked_channels = 0

    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            # Remove the explicit deny; revert to default (usually allowed via @everyone)
            overwrite.send_messages = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            unlocked_channels += 1
        except:
            pass

    # Re-enable voice connect
    for channel in guild.voice_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.connect = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except:
            pass

    embed = discord.Embed(
        title="🔓 **SERVER UNLOCKED**",
        description=f"**Issued by:** {interaction.user.mention}\n"
                    f"🔓 **Channels unlocked:** {unlocked_channels}\n"
                    f"🔊 **Voice channels re-enabled**",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.followup.send(embed=embed)
    await log_to_channel(guild.id, embed)

# -------------------- SLASH COMMANDS (CONFIGURATION) --------------------
async def log_setting(guild_id, user, setting, old, new):
    embed = discord.Embed(
        title="⚙️ **Anti-Nuke Setting Changed**",
        description=f"**Admin:** {user.mention}\n**Setting:** `{setting}`\n**Old:** `{old}`\n**New:** `{new}`",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    await log_to_channel(guild_id, embed)

@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ **Anti-Nuke Bot – Command List**",
        description="Use `/` to run these commands. `Administrator` permission required to change settings.\n"
                    "**Lockdown** commands are only for the server owner.",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="`/status`", value="Show current anti‑nuke settings", inline=False)
    embed.add_field(name="`/enable` / `/disable`", value="Turn protection on/off", inline=False)
    embed.add_field(name="`/set <setting> <value>`", value="Change a setting. Example: `/set max_bans 5`", inline=False)
    embed.add_field(name="`/punishment <type>`", value="Set punishment: `ban`, `kick`, `strip_roles`, `alert`", inline=False)
    embed.add_field(name="`/setlogs #channel`", value="Set the log channel (all alerts go here)", inline=False)
    embed.add_field(name="`/whitelist user/role`", value="Whitelist a user or role (ignored by anti-nuke)", inline=False)
    embed.add_field(name="`/lockdown`", value="**Owner only** – lock all channels, delete webhooks", inline=False)
    embed.add_field(name="`/unlockdown`", value="**Owner only** – restore channel permissions", inline=False)
    embed.add_field(name="`/ping`", value="Check bot latency", inline=False)
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
    embed.add_field(name="Enabled", value=str(cfg["enabled"]), inline=True)
    embed.add_field(name="Punishment", value=cfg["punishment"], inline=True)
    embed.add_field(name="Time Window", value=f"{cfg['time_window']}s", inline=True)
    embed.add_field(name="Max Bans", value=str(cfg["max_bans"]), inline=True)
    embed.add_field(name="Max Kicks", value=str(cfg["max_kicks"]), inline=True)
    embed.add_field(name="Max Channel Deletes", value=str(cfg["max_channel_deletes"]), inline=True)
    embed.add_field(name="Max Role Deletes", value=str(cfg["max_role_deletes"]), inline=True)
    embed.add_field(name="Max Webhook Creates", value=str(cfg["max_webhook_creates"]), inline=True)
    embed.add_field(name="Max Integration Creates", value=str(cfg["max_integration_creates"]), inline=True)
    embed.add_field(name="Max Channel Creates", value=str(cfg["max_channel_creates"]), inline=True)
    embed.add_field(name="Max Role Creates", value=str(cfg["max_role_creates"]), inline=True)
    embed.add_field(name="Max Permission Updates", value=str(cfg["max_permission_updates"]), inline=True)
    embed.add_field(name="Restore Channels", value=str(cfg["restore_channels"]), inline=True)
    embed.add_field(name="Restore Roles", value=str(cfg["restore_roles"]), inline=True)
    embed.add_field(name="Raid Threshold", value=f"{cfg['raid_join_threshold']} in 10s", inline=True)
    embed.add_field(name="Raid Mode", value=str(cfg["raid_mode"]), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="enable", description="Turn on anti-nuke protection")
@commands.has_permissions(administrator=True)
async def enable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = True
    await log_setting(interaction.guild_id, interaction.user, "enabled", old, True)
    await interaction.response.send_message("✅ Anti-nuke protection **enabled**", ephemeral=True)

@bot.tree.command(name="disable", description="Turn off anti-nuke protection")
@commands.has_permissions(administrator=True)
async def disable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = False
    await log_setting(interaction.guild_id, interaction.user, "enabled", old, False)
    await interaction.response.send_message("⚠️ Anti-nuke protection **disabled**", ephemeral=True)

@bot.tree.command(name="set", description="Change a setting")
@commands.has_permissions(administrator=True)
async def set_cmd(interaction: discord.Interaction, setting: str, value: str):
    cfg = get_config(interaction.guild_id)
    if setting not in cfg:
        await interaction.response.send_message(f"❌ Unknown setting `{setting}`", ephemeral=True)
        return
    old = cfg[setting]
    try:
        if isinstance(cfg[setting], bool):
            new = value.lower() == "true"
        elif isinstance(cfg[setting], int):
            new = int(value)
        else:
            new = value
    except:
        await interaction.response.send_message("❌ Invalid value type", ephemeral=True)
        return
    cfg[setting] = new
    await log_setting(interaction.guild_id, interaction.user, setting, old, new)
    await interaction.response.send_message(f"✅ `{setting}` changed from `{old}` to `{new}`", ephemeral=True)

@bot.tree.command(name="punishment", description="Set punishment type")
@commands.has_permissions(administrator=True)
async def punish_type_cmd(interaction: discord.Interaction, punishment: str):
    if punishment not in ["ban", "kick", "strip_roles", "alert"]:
        await interaction.response.send_message("❌ Choose: `ban`, `kick`, `strip_roles`, `alert`", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    old = cfg["punishment"]
    cfg["punishment"] = punishment
    await log_setting(interaction.guild_id, interaction.user, "punishment", old, punishment)
    await interaction.response.send_message(f"✅ Punishment set to `{punishment}`", ephemeral=True)

@bot.tree.command(name="setlogs", description="Set the log channel")
@commands.has_permissions(administrator=True)
async def setlogs_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config(interaction.guild_id)
    old = cfg.get("log_channel_id")
    cfg["log_channel_id"] = channel.id
    await log_setting(interaction.guild_id, interaction.user, "log_channel_id", old, channel.id)
    await interaction.response.send_message(f"✅ Logs will be sent to {channel.mention}", ephemeral=True)

@bot.tree.command(name="whitelist", description="Whitelist a user or role")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(interaction: discord.Interaction, target: str, item: str):
    cfg = get_config(interaction.guild_id)
    if target == "user":
        try:
            user_id = int(item.replace("<@", "").replace(">", "").replace("!", ""))
            if user_id not in cfg["whitelist_users"]:
                cfg["whitelist_users"].append(user_id)
                await interaction.response.send_message(f"✅ User <@{user_id}> whitelisted", ephemeral=True)
            else:
                await interaction.response.send_message("User already whitelisted", ephemeral=True)
        except:
            await interaction.response.send_message("❌ Invalid user mention or ID", ephemeral=True)
    elif target == "role":
        try:
            role_id = int(item.replace("<@&", "").replace(">", ""))
            if role_id not in cfg["whitelist_roles"]:
                cfg["whitelist_roles"].append(role_id)
                await interaction.response.send_message(f"✅ Role <@&{role_id}> whitelisted", ephemeral=True)
            else:
                await interaction.response.send_message("Role already whitelisted", ephemeral=True)
        except:
            await interaction.response.send_message("❌ Invalid role mention or ID", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Usage: `/whitelist user @user` or `/whitelist role @role`", ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms", ephemeral=True)

# -------------------- BACKGROUND CLEANUP --------------------
@tasks.loop(minutes=5)
async def clean_trackers():
    now = datetime.now(timezone.utc).timestamp()
    for gid, users in list(action_tracker.items()):
        for uid, dq in list(users.items()):
            while dq and dq[0][0] < now - 60:
                dq.popleft()
            if not dq:
                del action_tracker[gid][uid]
        if not action_tracker[gid]:
            del action_tracker[gid]

@bot.event
async def on_ready():
    await bot.tree.sync()
    clean_trackers.start()
    print(f"✅ Anti-nuke bot online as {bot.user}")
    print("Slash commands synced. Use /help in Discord.")

if __name__ == "__main__":
    bot.run(TOKEN)