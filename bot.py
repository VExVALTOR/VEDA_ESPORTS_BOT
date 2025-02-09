import os
import discord
import requests  # Added for API requests
from discord.ext import commands, tasks
from dotenv import load_dotenv
import json
from keep_alive import keep_alive

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
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
voice_loop_active = True  # Flag for voice channel loop

# -------------------- Scrim Scheduler --------------------
scrim_file = "scrims.json"

def load_scrims():
    if os.path.exists(scrim_file):
        with open(scrim_file, "r") as f:
            return json.load(f)
    return {}

def save_scrims(scrims):
    with open(scrim_file, "w") as f:
        json.dump(scrims, f, indent=4)

@bot.command()
async def schedule(ctx, date: str, time: str, *, event: str):
    """Schedule a scrim/tournament."""
    scrims = load_scrims()
    scrims[f"{date} {time}"] = event
    save_scrims(scrims)
    await ctx.send(f"✅ Scrim scheduled: {date} {time} - {event}")

@bot.command()
async def scrims(ctx):
    """View upcoming scrims."""
    scrims = load_scrims()
    if not scrims:
        await ctx.send("📅 No scrims scheduled.")
    else:
        msg = "\n".join([f"{key}: {value}" for key, value in scrims.items()])
        await ctx.send(f"📅 **Upcoming Scrims:**\n{msg}")

# -------------------- Team Performance Tracker --------------------
stats_file = "team_stats.json"

def load_stats():
    if os.path.exists(stats_file):
        with open(stats_file, "r") as f:
            return json.load(f)
    return []

def save_stats(stats):
    with open(stats_file, "w") as f:
        json.dump(stats, f, indent=4)

@bot.command()
async def logmatch(ctx, kills: int, damage: int, placement: int):
    """Log a match performance."""
    stats = load_stats()
    stats.append({"kills": kills, "damage": damage, "placement": placement})
    save_stats(stats)
    await ctx.send(f"📊 Match logged: {kills} kills, {damage} damage, placement {placement}")

@bot.command()
async def teamstats(ctx):
    """View team stats summary."""
    stats = load_stats()
    if not stats:
        await ctx.send("📊 No matches logged yet.")
        return
    
    total_kills = sum(match["kills"] for match in stats)
    total_damage = sum(match["damage"] for match in stats)
    avg_placement = sum(match["placement"] for match in stats) / len(stats)
    
    await ctx.send(f"📊 **Team Stats Summary:**\nKills: {total_kills}\nDamage: {total_damage}\nAvg Placement: {avg_placement:.2f}")

# -------------------- AI Voice Coaching --------------------
@bot.command()
async def coach(ctx, mode: str):
    """AI coaching system."""
    if mode == "voice":
        channel = ctx.author.voice.channel if ctx.author.voice else None
        if channel:
            vc = await channel.connect()
            await ctx.send("🎙️ AI Coach connected to voice channel!")
            # Future: Add AI-generated tips based on live performance.
        else:
            await ctx.send("❌ You must be in a voice channel.")
    else:
        await ctx.send("Available commands: !coach voice")

# -------------------- Auto-Moderation --------------------
auto_moderation = False
bad_words = ["badword1", "badword2", "spam"]  # Replace with actual bad words

@bot.command()
async def automod(ctx, status: str):
    """Toggle auto-moderation."""
    global auto_moderation
    if status.lower() == "on":
        auto_moderation = True
        await ctx.send("🔒 Auto-moderation enabled.")
    elif status.lower() == "off":
        auto_moderation = False
        await ctx.send("🔓 Auto-moderation disabled.")
    else:
        await ctx.send("❌ Use !automod on/off")

@bot.event
async def on_message(message):
    """Moderation check before processing messages."""
    if auto_moderation and any(word in message.content.lower() for word in bad_words):
        await message.delete()
        log_action(f"Deleted message from {message.author.name}: {message.content}")
        await message.channel.send(f"🚫 {message.author.mention}, that message is not allowed.")
    await bot.process_commands(message)

# -------------------- Logging System --------------------
log_file = "moderation_logs.txt"

def log_action(action):
    with open(log_file, "a") as f:
        f.write(f"{action}\n")

@bot.event
async def on_member_remove(member):
    log_action(f"{member.name} left the server.")

@bot.event
async def on_member_ban(guild, member):
    log_action(f"{member.name} was banned.")

@bot.command()
async def logs(ctx):
    """View recent moderation logs."""
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            logs = f.readlines()
        await ctx.send("📜 **Moderation Logs:**\n" + "".join(logs[-5:]))
    else:
        await ctx.send("📜 No logs available.")

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

# -------------------- Fun Commands --------------------
@bot.command()
async def meme(ctx):
    """Fetch a random meme from Reddit."""
    url = "https://meme-api.com/gimme"
    response = requests.get(url).json()
    meme_url = response.get("url", "No meme found")
    await ctx.send(meme_url)

@bot.command()
async def joke(ctx):
    """Fetch a random joke."""
    url = "https://official-joke-api.appspot.com/random_joke"
    response = requests.get(url).json()
    joke_text = f"{response['setup']} - {response['punchline']}"
    await ctx.send(joke_text)

@bot.command()
async def roast(ctx, member: discord.Member):
    """Roast a tagged user."""
    url = "https://evilinsult.com/generate_insult.php?lang=en&type=json"
    response = requests.get(url).json()
    roast_text = response.get("insult", "Couldn't fetch a roast!")
    await ctx.send(f"🔥 {member.mention}, {roast_text}")

@bot.command()
async def funfact(ctx):
    """Fetch a random fun fact."""
    url = "https://uselessfacts.jsph.pl/random.json?language=en"
    response = requests.get(url).json()
    fact = response.get("text", "No fact found")
    await ctx.send(f"💡 Fun Fact: {fact}")

# -------------------- Run Bot --------------------
keep_alive()
bot.run(TOKEN)
