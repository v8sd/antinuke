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
    "raid_auto_kick": True,
    "raid_mode": False,
    "log_channel_id": None,
    "bypass_role_id": None,          # Owner‑set role immune to anti‑nuke & lockdown
    "admin_role_id": None,           # Role that can edit settings
    "lockdown_exception_role_id": None,  # Role that can speak during lockdown
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

def has_bypass(guild_id, member):
    """Check if member has the owner‑set bypass role."""
    cfg = get_config(guild_id)
    bypass_role_id = cfg.get("bypass_role_id")
    if bypass_role_id and member.get_role(bypass_role_id):
        return True
    return False

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
            try:
                await channel.send(embed=embed)
            except:
                pass

async def punish_user(guild, user, action, reason):
    cfg = get_config(guild.id)
    # Check bypass before punishing
    try:
        member = await guild.fetch_member(user.id)
        if has_bypass(guild.id, member):
            await log_to_channel(guild.id, discord.Embed(
                description=f"⚠️ **Bypass role prevented punishment** for {user.mention} ({action})",
                color=discord.Color.orange()
            ))
            return
    except:
        pass

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
        if has_bypass(guild.id, member) or is_whitelisted(guild.id, user.id, member.roles):
            return
    except:
        return

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

# -------------------- RAID DETECTION --------------------
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

    if cfg["raid_mode"] and cfg.get("raid_auto_kick", True):
        try:
            if not has_bypass(guild.id, member):
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

# -------------------- LOCKDOWN (ULTIMATE) --------------------
@bot.tree.command(name="lockdown", description="Lock down the server: disable messages, delete webhooks")
async def lockdown_cmd(interaction: discord.Interaction):
    # Owner only (or bypass role? we'll keep owner only for safety)
    if interaction.user.id != interaction.guild.owner_id and not has_bypass(interaction.guild.id, interaction.user):
        await interaction.response.send_message("❌ Only the server owner or bypass role can use this command.", ephemeral=True)
        return

    me = interaction.guild.me
    if not me.guild_permissions.manage_channels or not me.guild_permissions.manage_webhooks:
        await interaction.response.send_message("❌ Bot missing permissions (Manage Channels, Manage Webhooks).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    cfg = get_config(guild.id)
    exception_role_id = cfg.get("lockdown_exception_role_id")
    exception_role = guild.get_role(exception_role_id) if exception_role_id else None

    locked_channels = 0
    failed = 0
    deleted_webhooks = 0
    errors = []

    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)

            if exception_role:
                ex_overwrite = channel.overwrites_for(exception_role)
                ex_overwrite.send_messages = True
                await channel.set_permissions(exception_role, ex_overwrite)

            locked_channels += 1
        except Exception as e:
            failed += 1
            errors.append(f"#{channel.name}: {str(e)[:60]}")

    for channel in guild.text_channels:
        try:
            webhooks = await channel.webhooks()
            for webhook in webhooks:
                await webhook.delete()
                deleted_webhooks += 1
        except:
            pass

    for channel in guild.voice_channels:
        try:
            for member in channel.members:
                await member.move_to(None)
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.connect = False
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
        except:
            pass

    embed = discord.Embed(
        title="🔒 **SERVER LOCKDOWN COMPLETE**",
        description=f"**Issued by:** {interaction.user.mention}\n"
                    f"🔒 **Channels locked:** {locked_channels} (failed: {failed})\n"
                    f"🧹 **Webhooks deleted:** {deleted_webhooks}\n"
                    f"🔊 **Voice channels disabled**",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )
    if exception_role:
        embed.add_field(name="🔑 Exception role", value=exception_role.mention, inline=False)
    if errors:
        embed.add_field(name="⚠️ Errors", value="\n".join(errors[:5]), inline=False)

    await interaction.followup.send(embed=embed)
    await log_to_channel(guild.id, embed)

@bot.tree.command(name="unlockdown", description="Unlock the server")
async def unlockdown_cmd(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id and not has_bypass(interaction.guild.id, interaction.user):
        await interaction.response.send_message("❌ Only the server owner or bypass role can use this command.", ephemeral=True)
        return

    me = interaction.guild.me
    if not me.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ Bot missing Manage Channels permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    unlocked = 0

    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = None
            await channel.set_permissions(guild.default_role, overwrite=overwrite)
            unlocked += 1
        except:
            pass

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
                    f"🔓 **Channels unlocked:** {unlocked}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.followup.send(embed=embed)
    await log_to_channel(guild.id, embed)

# -------------------- ROLE SETUP (OWNER ONLY) --------------------
@bot.tree.command(name="set_bypass_role", description="[OWNER ONLY] Set a role that bypasses all anti-nuke & lockdown")
async def set_bypass_role(interaction: discord.Interaction, role: discord.Role):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Only the server owner can use this command.", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    cfg["bypass_role_id"] = role.id
    await log_to_channel(interaction.guild_id, discord.Embed(
        description=f"🔓 **Bypass role set to {role.mention}** by {interaction.user.mention}",
        color=discord.Color.gold()
    ))
    await interaction.response.send_message(f"✅ `{role.name}` will now bypass all anti-nuke actions and lockdown.", ephemeral=True)

@bot.tree.command(name="set_admin_role", description="Set a role that can edit anti-nuke settings")
@commands.has_permissions(administrator=True)
async def set_admin_role(interaction: discord.Interaction, role: discord.Role):
    cfg = get_config(interaction.guild_id)
    cfg["admin_role_id"] = role.id
    await log_to_channel(interaction.guild_id, discord.Embed(
        description=f"⚙️ **Anti-nuke admin role set to {role.mention}** by {interaction.user.mention}",
        color=discord.Color.blue()
    ))
    await interaction.response.send_message(f"✅ `{role.name}` can now edit anti-nuke settings.", ephemeral=True)

@bot.tree.command(name="set_lockdown_exception", description="Set a role that can speak during lockdown")
@commands.has_permissions(administrator=True)
async def set_lockdown_exception(interaction: discord.Interaction, role: discord.Role):
    cfg = get_config(interaction.guild_id)
    cfg["lockdown_exception_role_id"] = role.id
    await log_to_channel(interaction.guild_id, discord.Embed(
        description=f"🔑 **Lockdown exception role set to {role.mention}**",
        color=discord.Color.blue()
    ))
    await interaction.response.send_message(f"✅ `{role.name}` will be able to talk during lockdown.", ephemeral=True)

# -------------------- COMMAND PERMISSION WRAPPER --------------------
def admin_or_antinuke_role():
    async def predicate(interaction: discord.Interaction):
        cfg = get_config(interaction.guild_id)
        admin_role_id = cfg.get("admin_role_id")
        if admin_role_id and interaction.user.get_role(admin_role_id):
            return True
        return interaction.user.guild_permissions.administrator
    return commands.check(predicate)

# -------------------- CONFIGURATION COMMANDS --------------------
@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ **ULTIMATE ANTI-NUKE BOT**",
        description="**Prefix:** `/` (slash commands)\n"
                    "🔹 **Admin/anti‑nuke role** can use `/enable`, `/disable`, `/set`, `/punishment`, `/setlogs`, `/whitelist`\n"
                    "🔹 **Owner only** can use `/set_bypass_role`, `/set_admin_role`, `/set_lockdown_exception`\n"
                    "🔹 **Lockdown** commands require owner or bypass role",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📊 `/status`", value="Show current settings", inline=False)
    embed.add_field(name="✅ `/enable` / `/disable`", value="Turn protection on/off", inline=False)
    embed.add_field(name="⚙️ `/set <setting> <value>`", value="Change thresholds (max_bans, max_kicks, ...)", inline=False)
    embed.add_field(name="🔨 `/punishment <type>`", value="Set punishment: ban, kick, strip_roles, alert", inline=False)
    embed.add_field(name="📝 `/setlogs #channel`", value="Set log channel", inline=False)
    embed.add_field(name="👥 `/whitelist user/role`", value="Whitelist from anti-nuke", inline=False)
    embed.add_field(name="🔒 `/lockdown`", value="Lock all channels, delete webhooks", inline=False)
    embed.add_field(name="🔓 `/unlockdown`", value="Unlock server", inline=False)
    embed.add_field(name="👑 `/set_bypass_role @role`", value="[OWNER] Set role immune to all actions", inline=False)
    embed.add_field(name="⚙️ `/set_admin_role @role`", value="Set role that can edit settings", inline=False)
    embed.add_field(name="🔑 `/set_lockdown_exception @role`", value="Set role that can talk during lockdown", inline=False)
    embed.add_field(name="🚫 `/toggle_raid_autokick`", value="Enable/disable auto‑kick during raids", inline=False)
    embed.add_field(name="🔍 `/check_perms`", value="Show bot permissions (owner only)", inline=False)
    embed.add_field(name="🏓 `/ping`", value="Check bot latency", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="status", description="Show anti-nuke configuration")
@admin_or_antinuke_role()
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
    embed.add_field(name="Raid Auto‑Kick", value=str(cfg.get("raid_auto_kick", True)), inline=True)
    embed.add_field(name="Raid Mode", value=str(cfg["raid_mode"]), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="enable", description="Turn on anti-nuke protection")
@admin_or_antinuke_role()
async def enable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = True
    await log_setting(interaction.guild_id, interaction.user, "enabled", old, True)
    await interaction.response.send_message("✅ Anti-nuke protection **enabled**", ephemeral=True)

@bot.tree.command(name="disable", description="Turn off anti-nuke protection (not recommended)")
@admin_or_antinuke_role()
async def disable_cmd(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    old = cfg["enabled"]
    cfg["enabled"] = False
    await log_setting(interaction.guild_id, interaction.user, "enabled", old, False)
    await interaction.response.send_message("⚠️ Anti-nuke protection **disabled**", ephemeral=True)

@bot.tree.command(name="set", description="Change a setting (e.g., /set max_bans 5)")
@admin_or_antinuke_role()
async def set_cmd(interaction: discord.Interaction, setting: str, value: str):
    cfg = get_config(interaction.guild_id)
    if setting not in cfg:
        await interaction.response.send_message(f"❌ Unknown setting `{setting}`. Use `/status` to see available settings.", ephemeral=True)
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

@bot.tree.command(name="punishment", description="Set punishment type: ban, kick, strip_roles, alert")
@admin_or_antinuke_role()
async def punishment_cmd(interaction: discord.Interaction, punishment: str):
    if punishment not in ["ban", "kick", "strip_roles", "alert"]:
        await interaction.response.send_message("❌ Invalid punishment. Choose: `ban`, `kick`, `strip_roles`, `alert`", ephemeral=True)
        return
    cfg = get_config(interaction.guild_id)
    old = cfg["punishment"]
    cfg["punishment"] = punishment
    await log_setting(interaction.guild_id, interaction.user, "punishment", old, punishment)
    await interaction.response.send_message(f"✅ Punishment set to `{punishment}`", ephemeral=True)

@bot.tree.command(name="setlogs", description="Set the channel for alert logs")
@admin_or_antinuke_role()
async def setlogs_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_config(interaction.guild_id)
    old = cfg.get("log_channel_id")
    cfg["log_channel_id"] = channel.id
    await log_setting(interaction.guild_id, interaction.user, "log_channel_id", old, channel.id)
    await interaction.response.send_message(f"✅ Logs will be sent to {channel.mention}", ephemeral=True)

@bot.tree.command(name="whitelist", description="Whitelist a user or role")
@admin_or_antinuke_role()
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

@bot.tree.command(name="toggle_raid_autokick", description="Enable/disable auto‑kick during raid mode")
@admin_or_antinuke_role()
async def toggle_raid_autokick(interaction: discord.Interaction, enabled: bool):
    cfg = get_config(interaction.guild_id)
    old = cfg.get("raid_auto_kick", True)
    cfg["raid_auto_kick"] = enabled
    await log_setting(interaction.guild_id, interaction.user, "raid_auto_kick", old, enabled)
    await interaction.response.send_message(f"✅ Raid auto‑kick set to `{enabled}`.", ephemeral=True)

@bot.tree.command(name="check_perms", description="[OWNER ONLY] Check bot permissions")
async def check_perms(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Owner only.", ephemeral=True)
        return
    me = interaction.guild.me
    perms = {
        "Manage Channels": me.guild_permissions.manage_channels,
        "Manage Webhooks": me.guild_permissions.manage_webhooks,
        "Manage Roles": me.guild_permissions.manage_roles,
        "View Audit Log": me.guild_permissions.view_audit_log,
        "Ban Members": me.guild_permissions.ban_members,
        "Kick Members": me.guild_permissions.kick_members,
        "Send Messages": me.guild_permissions.send_messages,
    }
    lines = [f"**{k}:** {'✅' if v else '❌'}" for k, v in perms.items()]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms", ephemeral=True)

# -------------------- LOGGING HELPER --------------------
async def log_setting(guild_id, user, setting, old, new):
    embed = discord.Embed(
        title="⚙️ **Anti-Nuke Setting Changed**",
        description=f"**Admin:** {user.mention}\n**Setting:** `{setting}`\n**Old:** `{old}`\n**New:** `{new}`",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    await log_to_channel(guild_id, embed)

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
    print(f"✅ Ultimate anti-nuke bot online as {bot.user}")
    print("Slash commands synced. Use /help in Discord.")

if __name__ == "__main__":
    bot.run(TOKEN)