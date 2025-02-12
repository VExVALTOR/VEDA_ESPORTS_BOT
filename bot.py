import os
import discord
from discord.ext import commands, tasks
import requests
import json
import sqlite3
import logging
from datetime import datetime
from gtts import gTTS
from dotenv import load_dotenv
from keep_alive import keep_alive  # Ensure you have this module if hosting 24/7.
import asyncio
import random
from io import BytesIO
import re
import shutil  # Used to check for FFmpeg
import time

# -------------------- Logging Setup --------------------
logging.basicConfig(level=logging.INFO)

# -------------------- Environment Variables & Config --------------------
load_dotenv("bot_token.env")
TOKEN = os.getenv("DC_TOKEN")
if not TOKEN:
    raise ValueError("DC_TOKEN not found in environment variables.")

try:
    with open("config.json") as f:
        config = json.load(f)
except Exception as e:
    raise ValueError(f"Error loading config.json: {e}")

# Load configurable prefix (default: "!")
CURRENT_PREFIX = config.get("prefix", "!")
# Load default voice channel ID and feedback channel ID
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

# Compile a regex for bad words (using word boundaries to avoid false positives)
bad_words_pattern = re.compile(r'\b(?:' + '|'.join(map(re.escape, BAD_WORDS)) + r')\b', re.IGNORECASE)

# Warning cooldown: prevent spamming warnings (in seconds)
WARNING_COOLDOWN = 60
last_warning_times = {}  # Dict mapping user_id to last warning timestamp

# -------------------- Bot Prefix Helper --------------------
def get_prefix(bot, message):
    # Allows the prefix to be changed dynamically
    return CURRENT_PREFIX

# -------------------- Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=get_prefix, intents=intents)

# -------------------- Database Helper Class --------------------
DB_NAME = "bot_data.db"

class Database:
    def __init__(self, db_name):
        self.db_name = db_name
        self.init_db()
    
    def init_db(self):
        with sqlite3.connect(self.db_name) as conn:
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
            # Table for user-specific stats
            c.execute('''CREATE TABLE IF NOT EXISTS user_stats (
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         user_id INTEGER,
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
    
    def execute(self, query, params=(), fetch=False):
        with sqlite3.connect(self.db_name) as conn:
            c = conn.cursor()
            c.execute(query, params)
            if fetch:
                result = c.fetchall()
                return result
            conn.commit()
    
    # Convenience methods:
    def add_scrim(self, date, time_str, event):
        self.execute("INSERT INTO scrims (date, time, event) VALUES (?, ?, ?)", (date, time_str, event))
    
    def get_scrims(self):
        return self.execute("SELECT date, time, event FROM scrims ORDER BY date, time", fetch=True)
    
    def log_match(self, kills, damage, placement):
        self.execute("INSERT INTO team_stats (kills, damage, placement) VALUES (?, ?, ?)", (kills, damage, placement))
    
    def get_team_stats(self):
        return self.execute("SELECT kills, damage, placement FROM team_stats", fetch=True)
    
    def log_user_match(self, user_id, kills, damage, placement):
        self.execute("INSERT INTO user_stats (user_id, kills, damage, placement) VALUES (?, ?, ?, ?)",
                     (user_id, kills, damage, placement))
    
    def get_user_stats(self, user_id):
        result = self.execute("SELECT SUM(kills), SUM(damage), AVG(placement), COUNT(*) FROM user_stats WHERE user_id = ?",
                              (user_id,), fetch=True)
        return result[0] if result else (0, 0, 0, 0)
    
    def add_mod_log(self, action):
        self.execute("INSERT INTO mod_logs (action, timestamp) VALUES (?, ?)", (action, datetime.now()))
    
    def add_warning(self, user_id, reason):
        self.execute("INSERT INTO warnings (user_id, reason, timestamp) VALUES (?, ?, ?)", (user_id, reason, datetime.now()))
    
    def get_warnings(self, user_id):
        result = self.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (user_id,), fetch=True)
        return result[0][0] if result else 0

    def clear_warnings(self, user_id):
        self.execute("DELETE FROM warnings WHERE user_id = ?", (user_id,))

db = Database(DB_NAME)

# -------------------- Pagination Helper Function --------------------
async def paginate(ctx, pages, timeout=60):
    """Simple reaction-based pagination for a list of embeds."""
    if not pages:
        return
    current = 0
    message = await ctx.send(embed=pages[current])
    if len(pages) == 1:
        return
    await message.add_reaction("⬅️")
    await message.add_reaction("➡️")

    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) in ["⬅️", "➡️"]
            and reaction.message.id == message.id
        )

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=timeout, check=check)
            if str(reaction.emoji) == "⬅️":
                current = (current - 1) % len(pages)
            elif str(reaction.emoji) == "➡️":
                current = (current + 1) % len(pages)
            await message.edit(embed=pages[current])
            await message.remove_reaction(reaction, user)
        except asyncio.TimeoutError:
            break

# -------------------- Persistent 24/7 Voice Connection --------------------
@tasks.loop(minutes=5)
async def maintain_default_voice_connection():
    """Ensure the bot stays connected to the default voice channel 24/7."""
    default_channel = bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
    if not default_channel or not isinstance(default_channel, discord.VoiceChannel):
        logging.error("Default voice channel not found or invalid")
        return

    # Check if already connected to the default channel
    connected = any(vc.channel.id == DEFAULT_VOICE_CHANNEL_ID for vc in bot.voice_clients)
    if not connected:
        try:
            await default_channel.connect()
            logging.info(f"Connected to default voice channel: {default_channel.name}")
        except Exception as e:
            logging.error(f"Error connecting to default voice channel: {e}")

# -------------------- Commands --------------------

# 0. Configurable Prefix Command
@bot.command(name="setprefix")
@commands.has_permissions(administrator=True)
async def set_prefix(ctx, new_prefix: str):
    """Change the bot command prefix. Usage: !setprefix <new_prefix>"""
    global CURRENT_PREFIX, config
    CURRENT_PREFIX = new_prefix
    config["prefix"] = new_prefix
    try:
        with open("config.json", "w") as f:
            json.dump(config, f, indent=4)
        await ctx.send(f"✅ Prefix updated to: `{new_prefix}`")
    except Exception as e:
        await ctx.send("❌ Error updating prefix in config file.")
        logging.error(f"Set prefix error: {e}")

# 1. Scrim Scheduler Commands
@bot.command(name="schedule")
async def schedule_scrim(ctx, date: str, time_str: str, *, event: str):
    """
    Schedule a scrim/event.
    Usage: !schedule YYYY-MM-DD HH:MM Event Description
    (Date and time are validated using datetime.strptime)
    """
    try:
        # Validate date and time format
        datetime.strptime(date, '%Y-%m-%d')
        datetime.strptime(time_str, '%H:%M')
    except ValueError:
        await ctx.send("❌ Invalid date or time format. Please use YYYY-MM-DD for date and HH:MM for time.")
        return
    try:
        db.add_scrim(date, time_str, event)
        await ctx.send(f"✅ Scrim scheduled: {date} {time_str} - {event}")
    except Exception as e:
        await ctx.send(f"❌ Error scheduling scrim: {e}")
        logging.error(f"Schedule scrim error: {e}")

@bot.command(name="scrims")
async def scrims_list(ctx):
    """List all scheduled scrims. Pagination is used if there are many scrims."""
    try:
        scrims = db.get_scrims()
        if not scrims:
            await ctx.send("📅 No scrims scheduled.")
            return

        # Create pages (5 scrims per page)
        per_page = 5
        pages = []
        for i in range(0, len(scrims), per_page):
            page_scrims = scrims[i:i+per_page]
            description = "\n".join([f"**{date} {time_str}** - {event}" for date, time_str, event in page_scrims])
            embed = discord.Embed(title="📅 Upcoming Scrims", description=description, color=discord.Color.blue())
            embed.set_footer(text=f"Page {i//per_page + 1} of {((len(scrims)-1)//per_page)+1}")
            pages.append(embed)
        await paginate(ctx, pages)
    except Exception as e:
        await ctx.send(f"❌ Error fetching scrims: {e}")
        logging.error(f"Scrims list error: {e}")

# 2. Team Performance Tracker & User-Specific Stats Commands
@bot.command(name="logmatch")
async def log_match_command(ctx, kills: int, damage: int, placement: int):
    """
    Log a match performance.
    Usage: !logmatch <kills> <damage> <placement>
    This command logs the match for both team and the user.
    """
    try:
        db.log_match(kills, damage, placement)
        db.log_user_match(ctx.author.id, kills, damage, placement)
        await ctx.send(f"📊 Match logged: {kills} kills, {damage} damage, placement {placement}")
    except Exception as e:
        await ctx.send(f"❌ Error logging match: {e}")
        logging.error(f"Log match error: {e}")

@bot.command(name="teamstats")
async def team_stats_command(ctx):
    """Display team stats summary."""
    try:
        stats = db.get_team_stats()
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

@bot.command(name="mystats")
async def my_stats_command(ctx):
    """Display your personal match stats."""
    try:
        kills, damage, avg_placement, matches = db.get_user_stats(ctx.author.id)
        if matches == 0:
            await ctx.send("📊 You haven't logged any matches yet.")
        else:
            await ctx.send(f"📊 **Your Stats:**\nMatches: {matches}\nKills: {kills}\nDamage: {damage}\nAvg Placement: {avg_placement:.2f}")
    except Exception as e:
        await ctx.send("❌ Error fetching your stats.")
        logging.error(f"MyStats error: {e}")

@bot.command(name="playerstats")
async def player_stats_command(ctx, member: discord.Member):
    """Display the match stats for a mentioned user."""
    try:
        kills, damage, avg_placement, matches = db.get_user_stats(member.id)
        if matches == 0:
            await ctx.send(f"📊 {member.mention} hasn't logged any matches yet.")
        else:
            await ctx.send(f"📊 **Stats for {member.display_name}:**\nMatches: {matches}\nKills: {kills}\nDamage: {damage}\nAvg Placement: {avg_placement:.2f}")
    except Exception as e:
        await ctx.send("❌ Error fetching player stats.")
        logging.error(f"PlayerStats error: {e}")

# 3. AI Coaching Command (with TTS enhancements)
@bot.command(name="coach")
async def coach_command(ctx, *, topic: str = None):
    """
    Provides coaching advice.
    
    **Subcommands:**
    - `aim` → Tips to improve aiming skills.
    - `reflexes` → Drills for better reflexes.
    - `rotation` → Strategies for better positioning.
    - `challenge` → Daily practice drill.
    - `leaderboard` → Tracks challenge completions.
    - `entry` → Entry fragging strategies.
    - `support` → Supporting teammates efficiently.
    - `bgmi` → BGMI-specific pro tips.
    
    If no subcommand is provided, a random inspirational quote is returned.
    """
    coach_advice = {
        "aim": "To improve your aim, try practicing on aim trainers like Aim Lab or Kovaak's FPS Aim Trainer.",
        "reflexes": "Improve your reflexes by engaging in fast-paced shooter games and dedicated reflex training exercises.",
        "rotation": "Work on your rotation strategy by reviewing game replays and practicing map awareness.",
        "challenge": "Today's challenge: Get 50 kills in TDM mode within 10 minutes. Good luck!",
        "leaderboard": "The leaderboard feature is coming soon!",
        "entry": "For entry fragging, focus on quick decision-making and precision. Practice clear entry strategies.",
        "support": "Support roles benefit from map awareness and communication. Position yourself well and support your team.",
        "bgmi": "For BGMI, consider refining your shooting mechanics and movement through regular practice in training grounds."
    }
    if topic:
        topic_lower = topic.lower()
        if topic_lower in coach_advice:
            advice = coach_advice[topic_lower]
            await ctx.send(f"🎮 **Coach Advice on {topic.capitalize()}**: {advice}")
            return

    # Fallback: fetch a random quote from the Quotable API.
    try:
        response = requests.get("https://api.quotable.io/random", timeout=5)
        response.raise_for_status()
        data = response.json()
        advice = data.get("content", "Keep practicing!")
        author = data.get("author", "")
        full_advice = f"{advice} - {author}" if author else advice
    except Exception as e:
        full_advice = "Keep practicing and never give up!"
        logging.error(f"Coach API error: {e}")

    # Use TTS if the user is in a voice channel (and not in the default channel)
    user_vc = ctx.author.voice.channel if (ctx.author and ctx.author.voice) else None
    if user_vc and user_vc.id != DEFAULT_VOICE_CHANNEL_ID:
        try:
            # Check for FFmpeg availability
            if not shutil.which("ffmpeg"):
                await ctx.send("❌ FFmpeg is not installed. Please install FFmpeg to use TTS functionality.")
                return
            if ctx.voice_client:
                vc = await ctx.voice_client.move_to(user_vc)
            else:
                vc = await user_vc.connect()
            tts = gTTS(full_advice, lang='en')
            audio_fp = BytesIO()
            tts.write_to_fp(audio_fp)
            audio_fp.seek(0)
            vc.play(discord.FFmpegPCMAudio(audio_fp, pipe=True))
            while vc.is_playing():
                await asyncio.sleep(1)
            # Added delay before disconnecting
            await asyncio.sleep(5)
            await vc.disconnect()
        except Exception as e:
            await ctx.send(f"❌ Error during TTS playback: {e}")
            logging.error(f"Coach TTS error: {e}")
    else:
        await ctx.send(f"🎮 **Coach Says:** {full_advice}")

# 4. Video Analysis for AI Coaching (with OpenCV check)
@bot.command(name="analyze")
async def analyze_command(ctx):
    """Analyze an uploaded video and provide improvement suggestions."""
    if not ctx.message.attachments:
        await ctx.send("Please attach a video file for analysis.")
        return
    attachment = ctx.message.attachments[0]
    try:
        video_bytes = await attachment.read()
        temp_filename = "temp_video.mp4"
        with open(temp_filename, "wb") as f:
            f.write(video_bytes)
        # Attempt to import cv2 and check for OpenCV installation
        try:
            import cv2
        except ImportError:
            await ctx.send("❌ OpenCV is not installed. Please install it using `pip install opencv-python`.")
            os.remove(temp_filename)
            return
        cap = cv2.VideoCapture(temp_filename)
        if not cap.isOpened():
            await ctx.send("❌ Could not open the video file.")
            os.remove(temp_filename)
            return
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        cap.release()
        os.remove(temp_filename)
        feedback = (f"✅ **Video Analysis Complete:**\n"
                    f"- Resolution: {width}x{height}\n"
                    f"- Total Frames: {frame_count}\n"
                    f"- FPS: {fps:.2f}\n"
                    "Suggestion: Focus on improving your crosshair placement and positioning.")
        await ctx.send(feedback)
    except Exception as e:
        await ctx.send(f"❌ Error processing video: {e}")
        logging.error(f"Video analysis error: {e}")

# 5. Auto-Moderation Commands
@bot.command(name="automod")
async def automod_command(ctx, status: str):
    """Toggle auto-moderation. Usage: !automod on/off"""
    global AUTO_MODERATION_ENABLED
    if status.lower() == "on":
        AUTO_MODERATION_ENABLED = True
        await ctx.send("🔒 Auto-moderation enabled.")
    elif status.lower() == "off":
        AUTO_MODERATION_ENABLED = False
        await ctx.send("🔓 Auto-moderation disabled.")
    else:
        await ctx.send("❌ Usage: !automod on/off")

# 6. Voice Channel Commands
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

@bot.command(name="disconnect")
async def disconnect_command(ctx):
    """Disconnect the bot from its current voice channel."""
    try:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            await ctx.send("✅ Disconnected from voice channel.")
        else:
            await ctx.send("ℹ️ I'm not in a voice channel.")
    except Exception as e:
        await ctx.send(f"❌ Error disconnecting: {e}")
        logging.error(f"Disconnect command error: {e}")

# 7. Fun Commands
@bot.command(name="meme")
@commands.cooldown(1, 30, commands.BucketType.user)
async def meme_command(ctx):
    """Fetch a random meme from the internet."""
    try:
        response = requests.get("https://meme-api.com/gimme", timeout=5)
        response.raise_for_status()
        data = response.json()
        meme_url = data.get("url", "No meme found")
        await ctx.send(meme_url)
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a meme right now.")
        logging.error(f"Meme error: {e}")

@bot.command(name="joke")
@commands.cooldown(1, 30, commands.BucketType.user)
async def joke_command(ctx):
    """Fetch a random joke from the internet."""
    try:
        response = requests.get("https://official-joke-api.appspot.com/random_joke", timeout=5)
        response.raise_for_status()
        data = response.json()
        joke_text = f"{data.get('setup', '')} - {data.get('punchline', '')}"
        await ctx.send(joke_text)
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a joke right now.")
        logging.error(f"Joke error: {e}")

@bot.command(name="roast")
async def roast_command(ctx, member: discord.Member):
    """Roast a tagged user by fetching an insult from the internet."""
    try:
        response = requests.get("https://evilinsult.com/generate_insult.php?lang=en&type=json", timeout=5)
        response.raise_for_status()
        data = response.json()
        roast_text = data.get("insult", "Couldn't fetch a roast!")
        await ctx.send(f"🔥 {member.mention}, {roast_text}")
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a roast right now.")
        logging.error(f"Roast error: {e}")

@bot.command(name="funfact")
@commands.cooldown(1, 30, commands.BucketType.user)
async def funfact_command(ctx):
    """Fetch a random fun fact from the internet."""
    try:
        response = requests.get("https://uselessfacts.jsph.pl/random.json?language=en", timeout=5)
        response.raise_for_status()
        data = response.json()
        fact = data.get("text", "No fact found")
        await ctx.send(f"💡 Fun Fact: {fact}")
    except Exception as e:
        await ctx.send("❌ Couldn't fetch a fun fact right now.")
        logging.error(f"Funfact error: {e}")

@bot.command(name="trivia")
@commands.cooldown(1, 30, commands.BucketType.user)
async def trivia_command(ctx):
    """Fetch a gaming trivia question from the internet."""
    try:
        response = requests.get("https://opentdb.com/api.php?amount=1&category=15", timeout=5)
        response.raise_for_status()
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

# 8. Feedback Command
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

# 9. Server Info Command
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

# 10. Moderation Commands (Mute, Logs, and Warning System)
@bot.command(name="mute")
@commands.has_permissions(mute_members=True)
async def mute_command(ctx, member: discord.Member):
    """Mute a member in voice. Usage: !mute @member"""
    try:
        if not member.voice or not member.voice.channel:
            await ctx.send(f"ℹ️ {member.mention} is not in a voice channel!")
            return
        await member.edit(mute=True)
        await ctx.send(f"🔇 {member.mention} has been muted.")
        db.add_mod_log(f"{member.name} was muted by {ctx.author.name}")
    except discord.Forbidden:
        await ctx.send("⚠️ I don't have permission to mute members!")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")
        logging.error(f"Mute error: {e}")

@bot.command(name="logs")
async def logs_command(ctx):
    """
    View recent moderation logs.
    Pagination is used if there are more than 5 logs.
    """
    try:
        logs_data = db.execute("SELECT action, timestamp FROM mod_logs ORDER BY id DESC", fetch=True)
        if not logs_data:
            await ctx.send("📜 No logs available.")
            return
        
        # Paginate logs: 5 logs per page
        per_page = 5
        pages = []
        for i in range(0, len(logs_data), per_page):
            page_logs = logs_data[i:i+per_page]
            log_messages = "\n".join([f"{timestamp}: {action}" for action, timestamp in page_logs])
            embed = discord.Embed(title="📜 Moderation Logs", description=log_messages, color=discord.Color.dark_gray())
            embed.set_footer(text=f"Page {i//per_page + 1} of {((len(logs_data)-1)//per_page)+1}")
            pages.append(embed)
        await paginate(ctx, pages)
    except Exception as e:
        await ctx.send(f"❌ Error fetching logs: {e}")
        logging.error(f"Logs error: {e}")

@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn_command(ctx, member: discord.Member, *, reason: str):
    """Issues a warning. Usage: !warn @member <reason>"""
    try:
        db.add_mod_log(f"{member.name} was warned by {ctx.author.name} for: {reason}")
        await ctx.send(f"⚠️ {member.mention} has been warned for: {reason}")
        db.add_warning(member.id, reason)
    except Exception as e:
        await ctx.send(f"❌ Error issuing warning: {e}")
        logging.error(f"Warn command error: {e}")

@bot.command(name="warnings")
@commands.has_permissions(manage_messages=True)
async def warnings_command(ctx, member: discord.Member):
    """Displays the number of warnings a user has received. Usage: !warnings @member"""
    try:
        count = db.get_warnings(member.id)
        await ctx.send(f"⚠️ {member.mention} has {count} warning(s).")
    except Exception as e:
        await ctx.send(f"❌ Error fetching warnings: {e}")
        logging.error(f"Warnings command error: {e}")

@bot.command(name="clearwarns")
@commands.has_permissions(manage_messages=True)
async def clearwarns_command(ctx, member: discord.Member):
    """Clears all warnings for the mentioned user. Usage: !clearwarns @member"""
    try:
        db.clear_warnings(member.id)
        await ctx.send(f"✅ Warnings for {member.mention} have been cleared.")
    except Exception as e:
        await ctx.send(f"❌ Error clearing warnings: {e}")
        logging.error(f"Clearwarns command error: {e}")

# 11. Utility Command
@bot.command(name="ping")
async def ping_command(ctx):
    """Check the bot's latency."""
    await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")

# -------------------- Global on_message for Auto-Moderation --------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if AUTO_MODERATION_ENABLED:
        if bad_words_pattern.search(message.content):
            try:
                await message.delete()
                db.add_mod_log(f"Deleted message from {message.author.name}: {message.content}")
                now = time.time()
                last_warn = last_warning_times.get(message.author.id, 0)
                if now - last_warn >= WARNING_COOLDOWN:
                    db.add_warning(message.author.id, "Bad word usage")
                    last_warning_times[message.author.id] = now
                await message.channel.send(f"🚫 {message.author.mention}, that message is not allowed.", delete_after=5)
            except Exception as e:
                logging.error(f"Error auto-deleting message: {e}")
    await bot.process_commands(message)

# -------------------- on_voice_state_update Event --------------------
@bot.event
async def on_voice_state_update(member, before, after):
    # If the bot itself was disconnected from the default 24/7 channel, attempt reconnection.
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
