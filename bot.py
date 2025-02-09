import os
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load environment variables
load_dotenv("bot_token.env")
TOKEN = os.getenv("DC_TOKEN")
VOICE_CHANNEL_ID = os.getenv("VC_ID")

# Validate environment variables
if not TOKEN:
    raise ValueError("DC_TOKEN not found in environment variables.")
if not VOICE_CHANNEL_ID:
    raise ValueError("VC_ID not found in environment variables.")
try:
    VOICE_CHANNEL_ID = int(VOICE_CHANNEL_ID)
except ValueError:
    raise ValueError("VC_ID must be a valid integer.")

# Set up bot with intents
intents = discord.Intents.default()
intents.guild_messages = True
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
voice_loop_active = True  # Flag for voice channel loop

# -------------------- Voice Channel System --------------------
@tasks.loop(minutes=5)
async def check_voice():
    if voice_loop_active:
        await connect_to_voice()

async def connect_to_voice():
    """Ensures bot stays in the voice channel"""
    try:
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)
        if isinstance(channel, discord.VoiceChannel):
            # Disconnect from other channels if needed
            for vc in bot.voice_clients:
                if vc.channel.id != VOICE_CHANNEL_ID:
                    await vc.disconnect()
            
            # Connect if not already connected
            if not any(vc.channel.id == VOICE_CHANNEL_ID for vc in bot.voice_clients):
                await channel.connect()
                print(f"🔊 Connected to voice channel: {channel.name}")
    except Exception as e:
        print(f"❌ Voice connection error: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    global voice_loop_active
    if member == bot.user and before.channel and not after.channel:
        print("🛑 Manual disconnect detected. Stopping voice loop.")
        voice_loop_active = False
        check_voice.stop()

@bot.event
async def on_ready():
    """Runs when the bot is ready"""
    print(f"✅ Logged in as {bot.user}")
    await connect_to_voice()
    try:
        check_voice.start()
    except RuntimeError:
        print("⚠️ Task already running, skipping start.")

# -------------------- Server Info Command --------------------
@bot.command()
async def serverinfo(ctx):
    """Displays detailed server information"""
    guild = ctx.guild
    embed = discord.Embed(title="🏰 Server Information", color=discord.Color.blurple())
    
    # Basic Information
    embed.add_field(name="Server Name", value=guild.name, inline=True)
    embed.add_field(name="Member Count", value=guild.member_count, inline=True)
    embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
    
    # Dates and Boosts
    embed.add_field(name="Created At", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier}", inline=True)
    embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
    
    # Roles
    roles = [role.mention for role in guild.roles if not role.is_default()]
    role_count = len(roles)
    embed.add_field(
        name=f"Roles ({role_count})", 
        value=", ".join(roles[:5]) + (f"\n+{role_count-5} more..." if role_count > 5 else ""), 
        inline=False
    )
    
    # Server Icon
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    await ctx.send(embed=embed)

# -------------------- Permissions System --------------------
@bot.command()
@commands.has_permissions(mute_members=True)
async def mute(ctx, member: discord.Member):
    """Mutes a member in voice (Requires Mute Members permission)"""
    try:
        if not member.voice or not member.voice.channel:
            await ctx.send(f"{member.mention} is not in a voice channel!")
            return
        await member.edit(mute=True)
        await ctx.send(f"🔇 {member.mention} has been muted.")
    except discord.Forbidden:
        await ctx.send("⚠️ I don't have permission to mute members!")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

# -------------------- Run Bot --------------------
bot.run(TOKEN)
