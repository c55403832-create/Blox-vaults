# bot.py
import discord
from discord.ext import commands
import json
import os
from datetime import datetime, timedelta, timezone

# ==================== CONFIGURATION ====================

TOKEN = os.getenv('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
SETTINGS_FILE = 'channel_settings.json'
STATS_FILE = 'violation_stats.json'

DEFAULT_TIMEOUT_HOURS = 24

# ==================== DATA STORAGE ====================

class DataManager:
    def __init__(self, settings_file=SETTINGS_FILE, stats_file=STATS_FILE):
        self.settings_file = settings_file
        self.stats_file = stats_file
        self.settings = self._load_json(settings_file, {})
        self.stats = self._load_json(stats_file, {})
    
    def _load_json(self, filename, default):
        try:
            with open(filename, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default
    
    def _save_json(self, filename, data):
        with open(filename, 'w') as f:
            json.dump(data, f, indent=4)
    
    def get_guild_settings(self, guild_id):
        gid = str(guild_id)
        if gid not in self.settings:
            self.settings[gid] = {
                'restricted_channels': [],
                'allowed_channels': [],
                'timeout_hours': DEFAULT_TIMEOUT_HOURS,
                'action': 'timeout',
                'log_channel': None,
                'notification_channel': None,
                'dm_enabled': True,
                'delete_violation': True,
                'immune_roles': [],
                'max_violations_before_ban': 5,
                'custom_dm_message': None,
                'created_at': datetime.now(timezone.utc).isoformat()
            }
            self.save_settings()
        return self.settings[gid]
    
    def update_guild_settings(self, guild_id, data):
        self.settings[str(guild_id)] = data
        self.save_settings()
    
    def save_settings(self):
        self._save_json(self.settings_file, self.settings)
    
    def get_user_stats(self, guild_id, user_id):
        gid, uid = str(guild_id), str(user_id)
        if gid not in self.stats:
            self.stats[gid] = {}
        if uid not in self.stats[gid]:
            self.stats[gid][uid] = {
                'violations': 0,
                'timeouts': 0,
                'kicks': 0,
                'bans': 0,
                'last_violation': None,
                'first_seen': datetime.now(timezone.utc).isoformat()
            }
        return self.stats[gid][uid]
    
    def record_violation(self, guild_id, user_id, action_type):
        gid, uid = str(guild_id), str(user_id)
        stats = self.get_user_stats(guild_id, user_id)
        stats['violations'] += 1
        stats['last_violation'] = datetime.now(timezone.utc).isoformat()
        if action_type == 'timeout':
            stats['timeouts'] += 1
        elif action_type == 'kick':
            stats['kicks'] += 1
        elif action_type == 'ban':
            stats['bans'] += 1
        self.save_stats()
    
    def save_stats(self):
        self._save_json(self.stats_file, self.stats)
    
    def get_leaderboard(self, guild_id, limit=10):
        gid = str(guild_id)
        if gid not in self.stats:
            return []
        users = [(uid, data) for uid, data in self.stats[gid].items()]
        users.sort(key=lambda x: x[1]['violations'], reverse=True)
        return users[:limit]

data = DataManager()

# ==================== BOT SETUP ====================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    help_command=None,
    case_insensitive=True
)

# ==================== UTILITY FUNCTIONS ====================

def is_immune(member, settings):
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.manage_messages:
        return True
    if member.guild_permissions.moderate_members:
        return True
    immune_role_ids = settings.get('immune_roles', [])
    for role in member.roles:
        if role.id in immune_role_ids:
            return True
    return False

def format_duration(hours):
    if hours >= 24:
        days = hours // 24
        rem_hours = hours % 24
        if rem_hours == 0:
            return f"{days} day{'s' if days != 1 else ''}"
        return f"{days}d {rem_hours}h"
    return f"{hours} hour{'s' if hours != 1 else ''}"

async def send_dm(member, embed):
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

async def send_notification(guild, settings, content=None, embed=None):
    """Send to the designated notification channel."""
    notif_id = settings.get('notification_channel')
    if not notif_id:
        return None
    
    channel = guild.get_channel(notif_id)
    if not channel:
        return None
    
    perms = channel.permissions_for(guild.me)
    if not perms.send_messages:
        return None
    
    try:
        if embed:
            return await channel.send(content=content, embed=embed)
        else:
            return await channel.send(content=content)
    except:
        return None

async def log_action(guild, settings, embed):
    """Send to the log channel (separate from notifications)."""
    log_id = settings.get('log_channel')
    if not log_id:
        return
    channel = guild.get_channel(log_id)
    if channel and channel.permissions_for(guild.me).send_messages:
        try:
            await channel.send(embed=embed)
        except:
            pass

# ==================== EVENTS ====================

@bot.event
async def on_ready():
    print(f'✅ {bot.user.name} is online!')
    print(f'   ID: {bot.user.id}')
    print(f'   Servers: {len(bot.guilds)}')
    print(f'   Default timeout: {DEFAULT_TIMEOUT_HOURS} hours')
    print('─' * 40)
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="for rule breakers 👀 | !help"
        )
    )

@bot.event
async def on_guild_join(guild):
    data.get_guild_settings(guild.id)
    print(f'📥 Joined guild: {guild.name} ({guild.id})')

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    await bot.process_commands(message)
    
    settings = data.get_guild_settings(message.guild.id)
    restricted = settings.get('restricted_channels', [])
    allowed = settings.get('allowed_channels', [])
    
    if message.channel.id in allowed:
        return
    if message.channel.id not in restricted:
        return
    
    await handle_violation(message, settings)

async def handle_violation(message, settings):
    member = message.author
    guild = message.guild
    
    if is_immune(member, settings):
        return
    
    # INSTANT DELETE — happens FIRST, before anything else
    if settings.get('delete_violation', True):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[DELETE ERROR] {e}")
    
    stats = data.get_user_stats(guild.id, member.id)
    violation_count = stats['violations'] + 1
    max_violations = settings.get('max_violations_before_ban', 5)
    
    action = settings.get('action', 'timeout')
    hours = settings.get('timeout_hours', DEFAULT_TIMEOUT_HOURS)
    
    if violation_count >= max_violations and action != 'ban':
        action = 'ban'
    
    # Build notification embed
    notif_embed = discord.Embed(
        timestamp=discord.utils.utcnow(),
        color=discord.Color.red()
    )
    
    # Build log embed
    log_embed = discord.Embed(
        timestamp=discord.utils.utcnow(),
        color=discord.Color.red()
    )
    
    try:
        if action == 'timeout':
            duration = timedelta(hours=hours)
            await member.timeout(duration, reason=f"Restricted channel violation in #{message.channel.name}")
            
            notif_embed.title = "🔇 Member Timed Out"
            notif_embed.description = (
                f"**{member.mention}** was timed out for **{format_duration(hours)}**.\n"
                f"Channel: {message.channel.mention}\n"
                f"Violation #{violation_count}"
            )
            notif_embed.set_thumbnail(url=member.display_avatar.url)
            notif_embed.set_footer(text=f"User ID: {member.id}")
            
            log_embed.title = "🔇 Timeout Issued"
            log_embed.description = (
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Duration:** {format_duration(hours)}\n"
                f"**Violation #:** {violation_count}\n"
                f"**Action:** Timeout"
            )
            log_embed.color = discord.Color.orange()
            
            # DM user
            if settings.get('dm_enabled', True):
                custom_msg = settings.get('custom_dm_message')
                dm_embed = discord.Embed(
                    title="⛔ You were timed out",
                    description=custom_msg or (
                        f"You were timed out for **{format_duration(hours)}** because you sent a message in "
                        f"**#{message.channel.name}**, which is a restricted channel."
                    ),
                    color=discord.Color.red()
                )
                dm_embed.add_field(
                    name="What does this mean?",
                    value="You cannot send messages, react, or join voice channels during this time.",
                    inline=False
                )
                dm_embed.add_field(
                    name="Violation Count",
                    value=f"This is violation **#{violation_count}**. "
                          f"{'⚠️ Next violation may result in a ban!' if violation_count >= max_violations - 1 else ''}",
                    inline=False
                )
                await send_dm(member, dm_embed)
            
            data.record_violation(guild.id, member.id, 'timeout')
            
        elif action == 'kick':
            await member.kick(reason=f"Restricted channel violation in #{message.channel.name}")
            
            notif_embed.title = "👢 Member Kicked"
            notif_embed.description = f"**{member.mention}** was kicked for violating channel restrictions."
            notif_embed.set_thumbnail(url=member.display_avatar.url)
            
            log_embed.title = "👢 Kick Issued"
            log_embed.description = (
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Action:** Kick"
            )
            log_embed.color = discord.Color.orange()
            
            data.record_violation(guild.id, member.id, 'kick')
            
        elif action == 'ban':
            await member.ban(
                reason=f"Repeated violations ({violation_count}) in restricted channel #{message.channel.name}",
                delete_message_days=1
            )
            
            notif_embed.title = "🔨 Member Banned"
            notif_embed.description = (
                f"**{member.mention}** was banned after **{violation_count} violations**.\n"
                f"Final violation in {message.channel.mention}"
            )
            notif_embed.color = discord.Color.dark_red()
            notif_embed.set_thumbnail(url=member.display_avatar.url)
            
            log_embed.title = "🔨 Ban Issued"
            log_embed.description = (
                f"**User:** {member.mention} (`{member.id}`)\n"
                f"**Channel:** {message.channel.mention}\n"
                f"**Total Violations:** {violation_count}\n"
                f"**Action:** Ban"
            )
            log_embed.color = discord.Color.dark_red()
            
            data.record_violation(guild.id, member.id, 'ban')
        
        # Send notification to designated channel
        await send_notification(guild, settings, embed=notif_embed)
        
        # Send to log channel (if different)
        await log_action(guild, settings, log_embed)
        
    except discord.Forbidden:
        await send_notification(
            guild, settings,
            content=f"❌ I don't have permission to moderate **{member.mention}**. Check my role position!"
        )
    except Exception as e:
        print(f"[ERROR] Violation handling failed: {e}")

# ==================== ADMIN COMMANDS ====================

@bot.command(name='restrict')
@commands.has_permissions(administrator=True)
async def restrict_channel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel.id in settings['restricted_channels']:
        return await ctx.send(f"⚠️ {channel.mention} is already restricted.", delete_after=5)
    
    settings['restricted_channels'].append(channel.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="🔒 Channel Restricted",
        description=f"{channel.mention} is now **restricted**.\nUsers who type here will be timed out for **{format_duration(settings['timeout_hours'])}**.",
        color=discord.Color.red()
    )
    embed.set_footer(text=f"Set by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name='unrestrict')
@commands.has_permissions(administrator=True)
async def unrestrict_channel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel.id not in settings['restricted_channels']:
        return await ctx.send(f"⚠️ {channel.mention} is not restricted.", delete_after=5)
    
    settings['restricted_channels'].remove(channel.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="🔓 Channel Unrestricted",
        description=f"{channel.mention} is no longer restricted.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name='allow')
@commands.has_permissions(administrator=True)
async def allow_channel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel.id in settings['allowed_channels']:
        return await ctx.send(f"⚠️ {channel.mention} is already allowed.", delete_after=5)
    
    settings['allowed_channels'].append(channel.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="✅ Channel Allowed",
        description=f"{channel.mention} is now explicitly **allowed** for conversation.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command(name='disallow')
@commands.has_permissions(administrator=True)
async def disallow_channel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel.id not in settings['allowed_channels']:
        return await ctx.send(f"⚠️ {channel.mention} is not in the allowed list.", delete_after=5)
    
    settings['allowed_channels'].remove(channel.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="❌ Channel Disallowed",
        description=f"{channel.mention} removed from allowed list.",
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed)

@bot.command(name='setchannel')
@commands.has_permissions(administrator=True)
async def set_notification_channel(ctx, channel: discord.TextChannel = None):
    """
    Set the channel where bot sends timeout/kick/ban notifications.
    Usage: !setchannel #bot-commands
    """
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel:
        # Verify bot can send messages there
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            return await ctx.send(f"❌ I don't have permission to send messages in {channel.mention}.", delete_after=5)
        if not perms.embed_links:
            return await ctx.send(f"❌ I need **Embed Links** permission in {channel.mention}.", delete_after=5)
        
        settings['notification_channel'] = channel.id
        data.update_guild_settings(ctx.guild.id, settings)
        
        embed = discord.Embed(
            title="📢 Notification Channel Set",
            description=f"All timeout/kick/ban notifications will be sent to {channel.mention}.",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Test",
            value="Try typing in a restricted channel to see it in action!",
            inline=False
        )
    else:
        settings['notification_channel'] = None
        data.update_guild_settings(ctx.guild.id, settings)
        
        embed = discord.Embed(
            title="📢 Notification Channel Removed",
            description="Notifications will no longer be sent to a specific channel.",
            color=discord.Color.orange()
        )
    
    await ctx.send(embed=embed)

@bot.command(name='setlog')
@commands.has_permissions(administrator=True)
async def set_log_channel(ctx, channel: discord.TextChannel = None):
    """Set the detailed log channel (separate from notifications). Usage: !setlog #mod-logs"""
    settings = data.get_guild_settings(ctx.guild.id)
    
    if channel:
        settings['log_channel'] = channel.id
        data.update_guild_settings(ctx.guild.id, settings)
        embed = discord.Embed(
            title="📋 Log Channel Set",
            description=f"Detailed logs → {channel.mention}",
            color=discord.Color.blue()
        )
    else:
        settings['log_channel'] = None
        data.update_guild_settings(ctx.guild.id, settings)
        embed = discord.Embed(
            title="📋 Log Channel Removed",
            description="Detailed logs disabled.",
            color=discord.Color.orange()
        )
    await ctx.send(embed=embed)

@bot.command(name='setaction')
@commands.has_permissions(administrator=True)
async def set_action(ctx, action: str):
    action = action.lower()
    if action not in ['timeout', 'kick', 'ban']:
        return await ctx.send("❌ Use: `timeout`, `kick`, or `ban`", delete_after=5)
    
    settings = data.get_guild_settings(ctx.guild.id)
    settings['action'] = action
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="⚙️ Action Updated",
        description=f"Punishment: **{action.upper()}**",
        color=discord.Color.blue()
    )
    if action == 'timeout':
        embed.add_field(name="Duration", value=format_duration(settings['timeout_hours']), inline=True)
    await ctx.send(embed=embed)

@bot.command(name='settime')
@commands.has_permissions(administrator=True)
async def set_timeout_time(ctx, hours: int):
    if hours < 1 or hours > 672:
        return await ctx.send("❌ Must be between 1 and 672 hours (4 weeks).", delete_after=5)
    
    settings = data.get_guild_settings(ctx.guild.id)
    settings['timeout_hours'] = hours
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="⏱️ Timeout Duration Updated",
        description=f"Timeout set to: **{format_duration(hours)}**",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

@bot.command(name='setdm')
@commands.has_permissions(administrator=True)
async def toggle_dm(ctx, enabled: str):
    enabled = enabled.lower() in ['on', 'true', 'yes', '1']
    settings = data.get_guild_settings(ctx.guild.id)
    settings['dm_enabled'] = enabled
    data.update_guild_settings(ctx.guild.id, settings)
    
    status = "✅ Enabled" if enabled else "❌ Disabled"
    await ctx.send(f"DM notifications: {status}")

@bot.command(name='setmax')
@commands.has_permissions(administrator=True)
async def set_max_violations(ctx, count: int):
    if count < 1 or count > 50:
        return await ctx.send("❌ Must be between 1 and 50.", delete_after=5)
    
    settings = data.get_guild_settings(ctx.guild.id)
    settings['max_violations_before_ban'] = count
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="🚫 Auto-Ban Threshold Updated",
        description=f"Users will be banned after **{count} violations**.",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)

@bot.command(name='immune')
@commands.has_permissions(administrator=True)
async def add_immune_role(ctx, role: discord.Role):
    settings = data.get_guild_settings(ctx.guild.id)
    if role.id in settings.get('immune_roles', []):
        return await ctx.send(f"⚠️ {role.mention} is already immune.", delete_after=5)
    
    settings['immune_roles'].append(role.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    embed = discord.Embed(
        title="🛡️ Immune Role Added",
        description=f"{role.mention} members are now immune to channel restrictions.",
        color=discord.Color.gold()
    )
    await ctx.send(embed=embed)

@bot.command(name='unimmune')
@commands.has_permissions(administrator=True)
async def remove_immune_role(ctx, role: discord.Role):
    settings = data.get_guild_settings(ctx.guild.id)
    if role.id not in settings.get('immune_roles', []):
        return await ctx.send(f"⚠️ {role.mention} is not immune.", delete_after=5)
    
    settings['immune_roles'].remove(role.id)
    data.update_guild_settings(ctx.guild.id, settings)
    
    await ctx.send(f"🛡️ {role.mention} removed from immune roles.")

@bot.command(name='setmessage')
@commands.has_permissions(administrator=True)
async def set_custom_message(ctx, *, message_text: str = None):
    settings = data.get_guild_settings(ctx.guild.id)
    settings['custom_dm_message'] = message_text
    data.update_guild_settings(ctx.guild.id, settings)
    
    if message_text:
        embed = discord.Embed(
            title="💬 Custom DM Message Set",
            description=f"```{message_text}```",
            color=discord.Color.blue()
        )
    else:
        embed = discord.Embed(
            title="💬 Custom DM Message Removed",
            description="Using default message.",
            color=discord.Color.orange()
        )
    await ctx.send(embed=embed)

# ==================== INFO COMMANDS ====================

@bot.command(name='settings')
@commands.has_permissions(administrator=True)
async def show_settings(ctx):
    settings = data.get_guild_settings(ctx.guild.id)
    
    restricted = []
    for ch_id in settings.get('restricted_channels', []):
        ch = ctx.guild.get_channel(ch_id)
        if ch:
            restricted.append(ch.mention)
    
    allowed = []
    for ch_id in settings.get('allowed_channels', []):
        ch = ctx.guild.get_channel(ch_id)
        if ch:
            allowed.append(ch.mention)
    
    immune_roles = []
    for r_id in settings.get('immune_roles', []):
        role = ctx.guild.get_role(r_id)
        if role:
            immune_roles.append(role.mention)
    
    notif_ch = ctx.guild.get_channel(settings['notification_channel']) if settings.get('notification_channel') else None
    log_ch = ctx.guild.get_channel(settings['log_channel']) if settings.get('log_channel') else None
    
    embed = discord.Embed(
        title=f"⚙️ Settings for {ctx.guild.name}",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    
    embed.add_field(name="🔒 Restricted", value="\n".join(restricted) if restricted else "None", inline=False)
    embed.add_field(name="✅ Allowed", value="\n".join(allowed) if allowed else "None", inline=False)
    embed.add_field(name="🔨 Action", value=settings['action'].upper(), inline=True)
    embed.add_field(name="⏱️ Duration", value=format_duration(settings['timeout_hours']), inline=True)
    embed.add_field(name="📢 Notif Channel", value=notif_ch.mention if notif_ch else "Not set (!setchannel)", inline=True)
    embed.add_field(name="📋 Log Channel", value=log_ch.mention if log_ch else "Off", inline=True)
    embed.add_field(name="💬 DMs", value="On" if settings.get('dm_enabled', True) else "Off", inline=True)
    embed.add_field(name="🗑️ Auto-Delete", value="On" if settings.get('delete_violation', True) else "Off", inline=True)
    embed.add_field(name="🚫 Auto-Ban At", value=f"{settings.get('max_violations_before_ban', 5)} violations", inline=True)
    embed.add_field(name="🛡️ Immune Roles", value="\n".join(immune_roles) if immune_roles else "None", inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='stats')
@commands.has_permissions(administrator=True)
async def show_stats(ctx, member: discord.Member = None):
    if member:
        stats = data.get_user_stats(ctx.guild.id, member.id)
        embed = discord.Embed(
            title=f"📊 Stats for {member.display_name}",
            color=discord.Color.teal()
        )
        embed.add_field(name="Total Violations", value=stats['violations'], inline=True)
        embed.add_field(name="Timeouts", value=stats['timeouts'], inline=True)
        embed.add_field(name="Kicks", value=stats['kicks'], inline=True)
        embed.add_field(name="Bans", value=stats['bans'], inline=True)
        if stats['last_violation']:
            embed.add_field(
                name="Last Violation",
                value=f"<t:{int(datetime.fromisoformat(stats['last_violation']).timestamp())}:R>",
                inline=False
            )
        embed.set_thumbnail(url=member.display_avatar.url)
    else:
        leaderboard = data.get_leaderboard(ctx.guild.id, 10)
        embed = discord.Embed(
            title="🏆 Violation Leaderboard",
            description="Top rule breakers in this server",
            color=discord.Color.gold()
        )
        for i, (uid, stats) in enumerate(leaderboard, 1):
            user = ctx.guild.get_member(int(uid))
            name = user.mention if user else f"User {uid[:6]}..."
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
            embed.add_field(
                name=f"{medal} {name}",
                value=f"{stats['violations']} violations | {stats['timeouts']} timeouts | {stats['bans']} bans",
                inline=False
            )
    
    await ctx.send(embed=embed)

@bot.command(name='resetstats')
@commands.has_permissions(administrator=True)
async def reset_stats(ctx, member: discord.Member = None):
    gid = str(ctx.guild.id)
    
    if member:
        uid = str(member.id)
        if gid in data.stats and uid in data.stats[gid]:
            del data.stats[gid][uid]
            data.save_stats()
            await ctx.send(f"✅ Stats reset for {member.mention}.")
        else:
            await ctx.send("⚠️ No stats found for this user.", delete_after=5)
    else:
        await ctx.send("❌ Specify a user or use `all` to reset everything.", delete_after=5)

@bot.command(name='help')
async def help_command(ctx):
    embed = discord.Embed(
        title="📖 Channel Restriction Bot",
        description="Auto-punish users who type in restricted channels. Default: **24-hour timeout**.",
        color=discord.Color.blurple()
    )
    
    embed.add_field(
        name="🔧 Setup Commands",
        value="""
        `!restrict #channel` — Block talking in a channel
        `!unrestrict #channel` — Remove restriction
        `!allow #channel` — Allow talking (exception)
        `!disallow #channel` — Remove exception
        `!setchannel #channel` — **Where bot sends notifications**
        `!setlog #channel` — Detailed mod logs
        `!setaction <timeout/kick/ban>` — Set punishment type
        `!settime <hours>` — Set timeout duration (default: 24)
        `!setdm on/off` — Toggle DM notifications
        `!setmax <number>` — Auto-ban after X violations
        `!immune @Role` — Make a role immune
        `!unimmune @Role` — Remove immunity
        `!setmessage <text>` — Custom DM message
        """,
        inline=False
    )
    
    embed.add_field(
        name="📊 Info Commands",
        value="""
        `!settings` — View all current settings
        `!stats` — Server violation leaderboard
        `!stats @user` — User's violation history
        `!resetstats @user` — Reset a user's stats
        """,
        inline=False
    )
    
    embed.add_field(
        name="💡 Quick Start",
        value="""
        1. `!restrict #announcements`
        2. `!allow #general-chat`
        3. `!setchannel #bot-commands`
        4. Done! The bot handles the rest.
        """,
        inline=False
    )
    
    embed.set_footer(text="Requires Administrator permission for setup • Immune: Admins, Mods, and assigned roles")
    await ctx.send(embed=embed)

# ==================== ERROR HANDLING ====================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need **Administrator** permission.", delete_after=5)
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ Channel not found.", delete_after=5)
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ Role not found.", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ User not found.", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument. Use `!help` for usage.", delete_after=5)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print(f"[ERROR] {error}")

# ==================== RUN ====================

if __name__ == '__main__':
    bot.run(TOKEN)
