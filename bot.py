import os
import discord
from discord.ext import commands, tasks
import requests
import json
import sqlite3
import logging
from datetime import datetime
from gtts import gTTS
import openai
from dotenv import load_dotenv
from keep_alive import keep_alive  # ensure you have this module
import asyncio
import random

# -------------------- Logging Setup --------------------
logging.basicConfig(level=logging.INFO)

# -------------------- Environment Variables --------------------
load_dotenv("bot_token.env")
TOKEN = os.getenv("DC_TOKEN")
if not TOKEN:
    raise ValueError("DC_TOKEN not found in environment variables.")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY
else:
    logging.warning("OPENAI_API_KEY not set; AI coaching command may not work.")

# -------------------- Load Config --------------------
# Your config.json should include:
# {
#   "default_voice_channel_id": "123456789012345678",
#   "feedback_channel_id": "123456789012345678",
#   "auto_moderation": true,
#   "bad_words": ["badword1", "badword2", "spam"]
# }
with open("config.json") as f:
    config = json.load(f)

DEFAULT_VOICE_CHANNEL_ID = config.get("default_voice_channel_id")
if DEFAULT_VOICE_CHANNEL_ID:
    DEFAULT_VOICE_CHANNEL_ID = int(DEFAULT_VOICE_CHANNEL_ID)
else:
    raise ValueError("default_voice_channel_id not found in config.json")

FEEDBACK_CHANNEL_ID = config.get("feedback_channel_id")
if FEEDBACK_CHANNEL_ID:
    FEEDBACK_CHANNEL_ID = int(FEEDBACK_CHANNEL_ID)
else:
    raise ValueError("feedback_channel_id not found in config.json")

AUTO_MODERATION_ENABLED = config.get("auto_moderation", False)
BAD_WORDS = config.get("bad_words", ["badword1", "badword2", "spam"])

# -------------------- Database Setup --------------------
DB_NAME = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Table for scrims
    c.execute('''CREATE TABLE IF NOT EXISTS scrims (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 date TEXT,
                 time TEXT,
                 event TEXT)''')
    # Table for team stats
    c.execute('''CREATE TABLE IF NOT EXISTS team_stats (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 kills INTEGER,
                 damage INTEGER,
                 placement INTEGER)''')
    # Table for warnings
    c.execute('''CREATE TABLE IF NOT EXISTS warnings (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 user_id INTEGER,
                 reason TEXT,
                 timestamp DATETIME)''')
    # Table for moderation logs
    c.execute('''CREATE TABLE IF NOT EXISTS mod_logs (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 action TEXT,
                 timestamp DATETIME)''')
    conn.commit()
    conn.close()

init_db()

# -------------------- Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global auto moderation flag (can be toggled via command)
auto_moderation = AUTO_MODERATION_ENABLED

# -------------------- Database Helper Functions --------------------
def add_scrim(date, time_str, event):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO scrims (date, time, event) VALUES (?, ?, ?)", (date, time_str, event))
    conn.commit()
    conn.close()

def get_scrims():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT date, time, event FROM scrims ORDER BY date, time")
    scrims = c.fetchall()
    conn.close()
    return scrims

def log_match(kills, damage, placement):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO team_stats (kills, damage, placement) VALUES (?, ?, ?)", (kills, damage, placement))
    conn.commit()
    conn.close()

def get_team_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT kills, damage, placement FROM team_stats")
    stats = c.fetchall()
    conn.close()
    return stats

def add_mod_log(action):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO mod_logs (action, timestamp) VALUES (?, ?)", (action, datetime.now()))
    conn.commit()
    conn.close()

def add_warning(user_id, reason):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO warnings (user_id, reason, timestamp) VALUES (?, ?, ?)", (user_id, reason, datetime.now()))
    conn.commit()
    conn.close()

def get_warnings(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

# -------------------- Persistent 24/7 Voice Connection --------------------
@tasks.loop(minutes=5)
async def maintain_default_voice_connection():
    """Ensure the bot stays connected to the default voice channel 24/7."""
    default_channel = bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
    if not default_channel or not isinstance(default_channel, discord.VoiceChannel):
        logging.error("Default voice channel not found or invalid")
        return
    # Check if already connected to default channel.
    connected = any(vc.channel.id == DEFAULT_VOICE_CHANNEL_ID for vc in bot.voice_clients)
    if not connected:
        try:
            await default_channel.connect()
            logging.info(f"Connected to default voice channel: {default_channel.name}")
        except Exception as e:
            logging.error(f"Error connecting to default voice channel: {e}")

# -------------------- Commands --------------------

# 1. Scrim Scheduler Commands
@bot.command(name="schedule")
async def schedule_scrim(ctx, date: str, time: str, *, event: str):
    """Schedule a scrim/event. Usage: !schedule YYYY-MM-DD HH:MM Event Description"""
    try:
        add_scrim(date, time, event)
        await ctx.send(f"✅ Scrim scheduled: {date} {time} - {event}")
    except Exception as e:
        await ctx.send(f"❌ Error scheduling scrim: {e}")
        logging.error(f"Schedule scrim error: {e}")

@bot.command(name="scrims")
async def scrims_list(ctx):
    """List all scheduled scrims."""
    try:
        scrims = get_scrims()
        if not scrims:
            await ctx.send("📅 No scrims scheduled.")
            return
        msg = "\n".join([f"{date} {time} - {event}" for date, time, event in scrims])
        await ctx.send(f"📅 **Upcoming Scrims:**\n{msg}")
    except Exception as e:
        await ctx.send(f"❌ Error fetching scrims: {e}")
        logging.error(f"Scrims list error: {e}")

# 2. Team Performance Tracker Commands
@bot.command(name="logmatch")
async def log_match_command(ctx, kills: int, damage: int, placement: int):
    """Log a match performance. Usage: !logmatch <kills> <damage> <placement>"""
    try:
        log_match(kills, damage, placement)
        await ctx.send(f"📊 Match logged: {kills} kills, {damage} damage, placement {placement}")
    except Exception as e:
        await ctx.send(f"❌ Error logging match: {e}")
        logging.error(f"Log match error: {e}")

@bot.command(name="teamstats")
async def team_stats_command(ctx):
    """Display team stats summary."""
    try:
        stats = get_team_stats()
        if not stats:
            await ctx.send("📊 No matches logged yet.")
            return
        total_kills = sum(s[0] for s in stats)
        total_damage = sum(s[1] for s in stats)
        avg_placement = sum(s[2] for s in stats) / len(stats)
        await ctx.send(f"📊 **Team Stats Summary:**\nKills: {total_kills}\nDamage: {total_damage}\nAvg Placement: {avg_placement:.2f}")
    except Exception as e:
        await ctx.send(f"❌ Error fetching team stats: {e}")
        logging.error(f"Team stats error: {e}")

# 3. AI Coaching Commands
@bot.command(name="coach")
async def ai_coach(ctx, *, question: str):
    """
    Provide AI-powered gaming coaching.
    Usage: !coach <your question/prompt>
    If you are in a voice channel (other than the default 24/7 channel), the bot will join, play the advice as audio, and then disconnect.
    """
    try:
        if not openai.api_key:
            await ctx.send("❌ OpenAI API key not configured.")
            return
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": f"Provide gaming coaching for: {question}"}],
            temperature=0.7,
            max_tokens=150
        )
        coach_text = response.choices[0].message.content.strip()
        # Check if the author is in a voice channel (and not in the default channel)
        user_vc = ctx.author.voice.channel if (ctx.author and ctx.author.voice) else None
        if user_vc and user_vc.id != DEFAULT_VOICE_CHANNEL_ID:
            # Join the user's channel temporarily
            vc = await user_vc.connect()
            # Convert text to speech using gTTS
            tts = gTTS(coach_text, lang='en')
            audio_file = "coach.mp3"
            tts.save(audio_file)
            vc.play(discord.FFmpegPCMAudio(executable="ffmpeg", source=audio_file))
            while vc.is_playing():
                await asyncio.sleep(1)
            await vc.disconnect()
        else:
            # Otherwise, simply send the text
            await ctx.send(f"🎮 **Coach Says:** {coach_text}")
    except Exception as e:
        await ctx.send(f"❌ Error with AI coaching: {e}")
        logging.error(f"AI coaching error: {e}")

# 4. Auto-Moderation Commands
@bot.command(name="automod")
async def automod_command(ctx, status: str):
    """Toggle auto-moderation. Usage: !automod on/off"""
    global auto_moderation
    if status.lower() == "on":
        auto_moderation = True
        await ctx.send("🔒 Auto-moderation enabled.")
    elif status.lower() == "off":
        auto_moderation = False
        await ctx.send("🔓 Auto-moderation disabled.")
    else:
        await ctx.send("❌ Usage: !automod on/off")

# 5. Voice Channel Commands
@bot.command(name="join")
@commands.has_permissions(manage_channels=True)
async def join_channel(ctx, channel_id: int):
    """Make the bot join the specified voice channel. Usage: !join <channel_id>"""
    try:
        channel = bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            await ctx.send("❌ Not a valid voice channel.")
            return
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
        await ctx.send(f"✅ Connected to {channel.name}")
    except Exception as e:
        await ctx.send(f"❌ Error joining channel: {e}")
        logging.error(f"Join channel error: {e}")

# 6. Fun Commands
@bot.command(name="meme")
@commands.cooldown(1, 30, commands.BucketType.user)
async def meme_command(ctx):
    """Fetch a random meme."""
    try:
        response = requests.get("https://meme-api.com/gimme")
        data = response.json()
        meme_url = data.get("url", "No meme found")
        await ctx.send(meme_url)
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a meme right now.")
        logging.error(f"Meme error: {e}")

@bot.command(name="joke")
async def joke_command(ctx):
    """Fetch a random joke."""
    try:
        response = requests.get("https://official-joke-api.appspot.com/random_joke")
        data = response.json()
        joke_text = f"{data['setup']} - {data['punchline']}"
        await ctx.send(joke_text)
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a joke right now.")
        logging.error(f"Joke error: {e}")

@bot.command(name="roast")
async def roast_command(ctx, member: discord.Member):
    """Roast a tagged user. Usage: !roast @member"""
    try:
        response = requests.get("https://evilinsult.com/generate_insult.php?lang=en&type=json")
        data = response.json()
        roast_text = data.get("insult", "Couldn't fetch a roast!")
        await ctx.send(f"🔥 {member.mention}, {roast_text}")
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a roast right now.")
        logging.error(f"Roast error: {e}")

@bot.command(name="funfact")
async def funfact_command(ctx):
    """Fetch a random fun fact."""
    try:
        response = requests.get("https://uselessfacts.jsph.pl/random.json?language=en")
        data = response.json()
        fact = data.get("text", "No fact found")
        await ctx.send(f"💡 Fun Fact: {fact}")
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a fun fact right now.")
        logging.error(f"Funfact error: {e}")

@bot.command(name="trivia")
@commands.cooldown(1, 10, commands.BucketType.user)
async def trivia_command(ctx):
    """Fetch a gaming trivia question."""
    try:
        response = requests.get("https://opentdb.com/api.php?amount=1&category=15")
        data = response.json()
        question_data = data['results'][0]
        question_text = question_data['question']
        incorrect_answers = question_data['incorrect_answers']
        correct_answer = question_data['correct_answer']
        options = incorrect_answers + [correct_answer]
        random.shuffle(options)
        options_str = ", ".join(options)
        await ctx.send(f"❓ {question_text}\nOptions: {options_str}")
    except Exception as e:
        await ctx.send("❌ Couldn't fetch trivia.")
        logging.error(f"Trivia error: {e}")

# 7. Feedback Command
@bot.command(name="feedback")
async def feedback_command(ctx, *, message: str):
    """Submit feedback to the designated feedback channel. Usage: !feedback <message>"""
    try:
        channel = bot.get_channel(FEEDBACK_CHANNEL_ID)
        if not channel:
            await ctx.send("❌ Feedback channel not found.")
            return
        embed = discord.Embed(
            title="New Feedback",
            description=message,
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.set_author(name=str(ctx.author), 
                         icon_url=(ctx.author.avatar.url if ctx.author.avatar else None))
        await channel.send(embed=embed)
        await ctx.send("✅ Feedback submitted!")
    except Exception as e:
        await ctx.send("❌ Couldn't submit feedback.")
        logging.error(f"Feedback error: {e}")

# 8. Server Info Command
@bot.command(name="serverinfo")
async def server_info(ctx):
    """Display detailed server information."""
    try:
        guild = ctx.guild
        embed = discord.Embed(title="🏰 Server Information", color=discord.Color.blurple())
        embed.add_field(name="Server Name", value=guild.name, inline=True)
        embed.add_field(name="Member Count", value=guild.member_count, inline=True)
        embed.add_field(name="Owner", value=(guild.owner.mention if guild.owner else "N/A"), inline=True)
        embed.add_field(name="Created At", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier}", inline=True)
        embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
        roles = [role.mention for role in guild.roles if not role.is_default()]
        role_count = len(roles)
        roles_display = ", ".join(roles[:5]) + (f"\n+{role_count-5} more..." if role_count > 5 else "")
        embed.add_field(name=f"Roles ({role_count})", value=(roles_display if roles_display else "None"), inline=False)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send("❌ Couldn't fetch server info.")
        logging.error(f"Server info error: {e}")

# 9. Moderation Commands
@bot.command(name="mute")
@commands.has_permissions(mute_members=True)
async def mute_command(ctx, member: discord.Member):
    """Mute a member in voice. Usage: !mute @member"""
    try:
        if not member.voice or not member.voice.channel:
            await ctx.send(f"{member.mention} is not in a voice channel!")
            return
        await member.edit(mute=True)
        await ctx.send(f"🔇 {member.mention} has been muted.")
        add_mod_log(f"{member.name} was muted by {ctx.author.name}")
    except discord.Forbidden:
        await ctx.send("⚠️ I don't have permission to mute members!")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")
        logging.error(f"Mute error: {e}")

@bot.command(name="logs")
async def logs_command(ctx):
    """View recent moderation logs (last 5 actions)."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT action, timestamp FROM mod_logs ORDER BY id DESC LIMIT 5")
        logs_data = c.fetchall()
        conn.close()
        if logs_data:
            log_messages = "\n".join([f"{timestamp}: {action}" for action, timestamp in logs_data])
            await ctx.send(f"📜 **Moderation Logs:**\n{log_messages}")
        else:
            await ctx.send("📜 No logs available.")
    except Exception as e:
        await ctx.send(f"❌ Error fetching logs: {e}")
        logging.error(f"Logs error: {e}")

# 10. Utility Command
@bot.command(name="ping")
async def ping_command(ctx):
    """Check the bot's latency."""
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

# -------------------- Global on_message for Auto-Moderation --------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if auto_moderation:
        content_lower = message.content.lower()
        if any(bad_word in content_lower for bad_word in BAD_WORDS):
            try:
                await message.delete()
                add_mod_log(f"Deleted message from {message.author.name}: {message.content}")
                add_warning(message.author.id, "Bad word usage")
                await message.channel.send(f"🚫 {message.author.mention}, that message is not allowed.")
            except Exception as e:
                logging.error(f"Error auto-deleting message: {e}")
    await bot.process_commands(message)

# -------------------- on_voice_state_update Event --------------------
@bot.event
async def on_voice_state_update(member, before, after):
    # If the bot is disconnected from the default 24/7 channel, attempt reconnection.
    if member == bot.user:
        if before.channel and not after.channel and before.channel.id == DEFAULT_VOICE_CHANNEL_ID:
            logging.info("Bot was disconnected from default voice channel; attempting reconnection.")
            await asyncio.sleep(5)
            await maintain_default_voice_connection()

# -------------------- on_ready Event --------------------
@bot.event
async def on_ready():
    logging.info(f"✅ Logged in as {bot.user}")
    maintain_default_voice_connection.start()

# -------------------- Run Bot --------------------
keep_alive()
bot.run(TOKEN)
