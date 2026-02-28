# Importing libraries and modules
import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp # NEW
from collections import deque # NEW
import asyncio # NEW
import random # NEW
import re # NEW

# Environment variables for tokens and other sensitive data
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Create the structure for queueing songs - Dictionary of queues
SONG_QUEUES = {}
AUTOPLAY = {}
LAST_PLAYED = {}
IDLE_TASKS = {}
LAST_VOICE_CHANNELS = {}
PLAY_HISTORY = {} # Tracks normalized titles to prevent duplicates

def cancel_idle(guild_id):
    task = IDLE_TASKS.get(guild_id)
    if task:
        task.cancel()
        IDLE_TASKS.pop(guild_id, None)

def schedule_idle_disconnect(voice_client, guild_id):
    cancel_idle(guild_id)
    async def _idle():
        await asyncio.sleep(900)
        if not SONG_QUEUES.get(guild_id):
            if voice_client and voice_client.is_connected() and not voice_client.is_playing() and not voice_client.is_paused():
                await voice_client.disconnect()
    IDLE_TASKS[guild_id] = asyncio.create_task(_idle())

def normalize_title(title):
    """
    Cleans up song titles to help identify duplicate tracks.
    Removes common tags like (Official Video), [Lyrics], HD, etc.
    """
    if not title:
        return ""
    # Convert to lowercase
    title = title.lower()
    # Remove content in brackets or parentheses
    title = re.sub(r'[\(\[][^\]\)]*[\]\)]', '', title)
    # Remove common filler words/tags
    fillers = [
        'official video', 'official audio', 'official music video', 'official lyric video',
        'lyric video', 'lyrics', 'hd', '4k', 'mv', 'high quality', 'hq', 'audio'
    ]
    for filler in fillers:
        title = title.replace(filler, '')
    # Remove extra whitespace and special characters
    title = re.sub(r'[^\w\s]', '', title)
    return " ".join(title.split())

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)


# Setup of intents. Intents are permissions the bot has on the server
intents = discord.Intents.default()
intents.message_content = True

# Bot setup
bot = commands.Bot(command_prefix="!", intents=intents)

# Bot ready-up code
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is online!")
    await bot.change_presence(activity=discord.Streaming(name="/help | Freya Wardana", url="https://www.twitch.tv/discord"))

def clear_guild_data(guild_id):
    """
    Clears all temporary session data for a guild.
    """
    guild_id_str = str(guild_id)
    SONG_QUEUES.pop(guild_id_str, None)
    AUTOPLAY.pop(guild_id_str, None)
    LAST_PLAYED.pop(guild_id_str, None)
    PLAY_HISTORY.pop(guild_id_str, None)
    cancel_idle(guild_id_str)
    print(f"Cleared session data for guild {guild_id_str}")

@bot.event
async def on_voice_state_update(member, before, after):
    # If the bot itself leaves or is kicked from a voice channel
    if member.id == bot.user.id and before.channel is not None and after.channel is None:
        clear_guild_data(member.guild.id)


@bot.tree.command(name="skip", description="Skips the current playing song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and (interaction.guild.voice_client.is_playing() or interaction.guild.voice_client.is_paused()):
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("Skipped the current song.")
    else:
        await interaction.response.send_message("Not playing anything to skip.")


@bot.tree.command(name="pause", description="Pause the currently playing song.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    # Check if something is actually playing
    if not voice_client.is_playing():
        return await interaction.response.send_message("Nothing is currently playing.")
    
    # Pause the track
    voice_client.pause()
    await interaction.response.send_message("Playback paused!")


@bot.tree.command(name="resume", description="Resume the currently paused song.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if voice_client is None:
        return await interaction.response.send_message("I'm not in a voice channel.")

    # Check if it's actually paused
    if not voice_client.is_paused():
        return await interaction.response.send_message("I’m not paused right now.")
    
    # Resume playback
    voice_client.resume()
    await interaction.response.send_message("Playback resumed!")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client

    # Check if the bot is in a voice channel
    if not voice_client or not voice_client.is_connected():
        return await interaction.response.send_message("I'm not connected to any voice channel.")

    # Clear the guild's queue
    guild_id_str = str(interaction.guild_id)
    if guild_id_str in SONG_QUEUES:
        SONG_QUEUES[guild_id_str].clear()

    # If something is playing or paused, stop it
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    # Disconnect will trigger on_voice_state_update and clear_guild_data
    await voice_client.disconnect()

    await interaction.response.send_message("Stopped playback, cleared data, and disconnected!")


@bot.tree.command(name="play", description="Play a song or add it to the queue.")
@app_commands.describe(song_query="Search query")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel

    if voice_channel is None:
        await interaction.followup.send("You must be in a voice channel.")
        return

    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)
    LAST_VOICE_CHANNELS[str(interaction.guild_id)] = voice_channel.id
    LAST_VOICE_CHANNELS[str(interaction.guild_id)] = voice_channel.id

    ydl_options = {
        "format": "bestaudio[abr<=96]/bestaudio",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    query = "ytsearch1: " + song_query
    results = await search_ytdlp_async(query, ydl_options)
    tracks = results.get("entries", [])

    if tracks is None:
        await interaction.followup.send("No results found.")
        return

    first_track = tracks[0]
    audio_url = first_track["url"]
    title = first_track.get("title", "Untitled")
    video_id = first_track.get("id")
    thumbnail = first_track.get("thumbnail") or (first_track.get("thumbnails") or [{}])[0].get("url")

    guild_id = str(interaction.guild_id)
    if SONG_QUEUES.get(guild_id) is None:
        SONG_QUEUES[guild_id] = deque()

    SONG_QUEUES[guild_id].append((audio_url, title, video_id, thumbnail))

    if voice_client.is_playing() or voice_client.is_paused():
        cancel_idle(guild_id)
        await interaction.followup.send(f"Added to queue: **{title}**")
    else:
        cancel_idle(guild_id)
        await interaction.delete_original_response()
        await play_next_song(voice_client, guild_id, interaction.channel)


class SearchSelect(discord.ui.Select):
    def __init__(self, results):
        options = []
        # Increase limit to 25 (discord max)
        for idx, item in enumerate(results[:25]):
            title = item.get("title", "Untitled")
            uploader = item.get("uploader", "") or ""
            label = f"{idx+1}. {title}"[:100]
            description = uploader[:100]
            options.append(discord.SelectOption(label=label, description=description, value=str(idx)))
        super().__init__(placeholder="Select a song (Top 25)", min_values=1, max_values=1, options=options)
        self.results = results

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        track = self.results[idx]
        audio_url = track["url"]
        title = track.get("title", "Untitled")
        video_id = track.get("id")
        thumbnail = track.get("thumbnail") or (track.get("thumbnails") or [{}])[0].get("url")
        voice_channel = interaction.user.voice.channel
        if voice_channel is None:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
            return
        voice_client = interaction.guild.voice_client
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_channel != voice_client.channel:
            await voice_client.move_to(voice_channel)
        guild_id = str(interaction.guild_id)
        if SONG_QUEUES.get(guild_id) is None:
            SONG_QUEUES[guild_id] = deque()
        SONG_QUEUES[guild_id].append((audio_url, title, video_id, thumbnail))
        if voice_client.is_playing() or voice_client.is_paused():
            cancel_idle(guild_id)
            await interaction.response.send_message(f"Added to queue: **{title}**")
        else:
            cancel_idle(guild_id)
            await interaction.response.defer()
            await interaction.delete_original_response()
            await play_next_song(voice_client, guild_id, interaction.channel)


class SearchView(discord.ui.View):
    def __init__(self, results):
        super().__init__(timeout=120)
        self.add_item(SearchSelect(results))


@bot.tree.command(name="search", description="Search top 10 results and select to play.")
@app_commands.describe(query="Search query")
async def search(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    ydl_options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
        "extract_flat": True,
    }
    results = await search_ytdlp_async("ytsearch25: " + query, ydl_options)
    tracks = results.get("entries", [])
    if not tracks:
        await interaction.followup.send("No results found.")
        return
    
    # Process the first 10 for the embed, but allow selecting from them.
    # To support more, we'd need pagination, but the user asked about the limit issue.
    # The error was format-related; extract_flat avoids downloading formats during search.
    embed = discord.Embed(title="Search Results", description=f'Query: "{query}"', color=discord.Color.blue())
    for i, item in enumerate(tracks[:10], start=1):
        title = item.get("title", "Untitled")
        uploader = item.get("uploader", "") or ""
        embed.add_field(name=f"{i}. {title}", value=uploader or "\u200b", inline=False)
    view = SearchView(tracks[:25]) # Pass up to 25 results to the view if we wanted, but Select limits to 25 options max.

    await interaction.followup.send(embed=embed, view=view)


async def play_next_song(voice_client, guild_id, channel):
    if SONG_QUEUES.get(guild_id):
        audio_url, title, video_id, thumbnail = SONG_QUEUES[guild_id].popleft()
        if not voice_client or not voice_client.is_connected():
            vc_id = LAST_VOICE_CHANNELS.get(guild_id)
            if vc_id:
                target_vc = discord.utils.get(channel.guild.voice_channels, id=vc_id)
                if target_vc:
                    voice_client = await target_vc.connect()

        # Re-resolve the URL right before playing to ensure freshness and bypass potential 403 errors
        # This is critical for autoplay items that might have stale or indirect URLs
        try:
            meta = await search_ytdlp_async(video_id or audio_url, {
                "format": "bestaudio/best",
                "noplaylist": True,
                "youtube_include_dash_manifest": False,
                "youtube_include_hls_manifest": False,
            })
            if meta:
                audio_url = meta.get("url", audio_url)
                title = meta.get("title", title)
                video_id = meta.get("id", video_id)
        except Exception as e:
            print(f"Failed to re-resolve URL for {title}: {e}")
            meta = {}
            
        LAST_PLAYED[guild_id] = {
            "title": title,
            "url": audio_url,
            "id": video_id or (meta or {}).get("id"),
            "uploader": (meta or {}).get("uploader"),
            "uploader_id": (meta or {}).get("uploader_id"),
            "channel_id": (meta or {}).get("channel_id"),
            "thumbnail": (meta or {}).get("thumbnail") or thumbnail,
        }

        # Track history to prevent duplicates
        if guild_id not in PLAY_HISTORY:
            PLAY_HISTORY[guild_id] = deque(maxlen=20)
        
        norm_title = normalize_title(title)
        if norm_title and norm_title not in PLAY_HISTORY[guild_id]:
            PLAY_HISTORY[guild_id].append(norm_title)

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn -c:a libopus -b:a 96k",
            # Remove executable if FFmpeg is in PATH
        }

        source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="bin\\ffmpeg\\ffmpeg.exe")

        def after_play(error):
            if error:
                print(f"Error playing {title}: {error}")
            asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop)

        voice_client.play(source, after=after_play)
        asyncio.create_task(channel.send(f"Now playing: **{title}**"))
        cancel_idle(guild_id)
    else:
        if AUTOPLAY.get(guild_id):
            seed = LAST_PLAYED.get(guild_id, {})
            seed_id = seed.get("id")
            seed_title = (seed.get("title") or "").strip()
            if seed_id:
                ydl_options = {
                    "format": "bestaudio/best",
                    "noplaylist": False,
                    "extract_flat": "in_playlist",
                    "youtube_include_dash_manifest": False,
                    "youtube_include_hls_manifest": False,
                }
                radio_url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
                radio = await search_ytdlp_async(radio_url, ydl_options)
                entries = radio.get("entries", []) if radio else []
                patterns = ["live", "tutorial", "podcast"]
                filtered = []
                guild_history = PLAY_HISTORY.get(guild_id, [])
                
                for e in entries:
                    eid = e.get("id")
                    etitle = (e.get("title") or "")
                    etitle_lower = etitle.lower()
                    dur = e.get("duration") or 0
                    
                    if not eid or eid == seed_id:
                        continue
                    
                    # Filter by normalized title variety
                    norm_etitle = normalize_title(etitle)
                    if norm_etitle in guild_history:
                        continue
                        
                    if any(p in etitle_lower for p in patterns):
                        continue
                    if "official video" in etitle_lower and dur > 900:
                        continue
                    filtered.append(e)
                
                # Use a larger pool for variety
                if filtered:
                    choose_pool = filtered[:10] # Look at top 10 instead of 5
                    choice = random.choice(choose_pool)
                    cid = choice.get("id")
                    curl = choice.get("url")
                    ctitle = choice.get("title", "Untitled")
                    if not curl or "videoplayback" not in str(curl):
                        info = await search_ytdlp_async(f"https://www.youtube.com/watch?v={cid}", ydl_options)
                        thumbnail = (info or {}).get("thumbnail") or (info or {}).get("thumbnails", [{}])[0].get("url")
                        SONG_QUEUES[guild_id].append((curl, ctitle, cid, thumbnail))
                        await play_next_song(voice_client, guild_id, channel)
                        return
                fallback_query = "ytsearch1: " + (seed_title or "")
                f_results = await search_ytdlp_async(fallback_query, ydl_options)
                f_tracks = f_results.get("entries", []) if f_results else []
                f_item = f_tracks[0] if f_tracks else None
                if f_item:
                    fttl = (f_item.get("title") or "").lower()
                    fid = f_item.get("id")
                    if not any(p in fttl for p in patterns) and fid and fid != seed_id:
                        f_thumb = f_item.get("thumbnail") or (f_item.get("thumbnails") or [{}])[0].get("url")
                        SONG_QUEUES[guild_id].append((f_item["url"], f_item.get("title", "Untitled"), fid, f_thumb))
                        await play_next_song(voice_client, guild_id, channel)
                        return
        schedule_idle_disconnect(voice_client, guild_id)
        # SONG_QUEUES[guild_id] = deque() # Handled by clear_guild_data or already empty

@bot.tree.command(name="autoplay", description="Toggle autoplay for similar songs (resets when bot leaves).")
async def autoplay(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    # Toggle logic
    enabled = not AUTOPLAY.get(guild_id, False)
    AUTOPLAY[guild_id] = enabled
    
    status = "Autoplay enabled" if enabled else "Autoplay disabled"
    color = discord.Color.yellow() if enabled else discord.Color.yellow()
    embed = discord.Embed(title="Autoplay", description=status, color=color)
    await interaction.response.send_message(embed=embed)


# --- Queue UI Components ---

class DeleteSongModal(discord.ui.Modal, title="Remove Song from Queue"):
    song_index = discord.ui.TextInput(
        label="Song Number",
        placeholder="Enter song number (e.g., 1)",
        min_length=1,
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.song_index.value) - 1
            guild_id = str(interaction.guild_id)
            queue = SONG_QUEUES.get(guild_id)
            
            if not queue or idx < 0 or idx >= len(queue):
                return await interaction.response.send_message("Invalid song number.", ephemeral=True)
            
            removed = queue[idx]
            # Deque doesn't support direct removal by index easily without conversion or rotation
            # but for small queues, conversion is fine
            temp = list(queue)
            song_title = temp.pop(idx)[1]
            SONG_QUEUES[guild_id] = deque(temp)
            
            await interaction.response.send_message(f"Removed **{song_title}** from queue.")
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)

class QueueActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Remove Song", value="delete", description="Remove a song from the queue"),
            discord.SelectOption(label="Shuffle Queue", value="shuffle", description="Shuffle the queue"),
            discord.SelectOption(label="Clear Queue", value="clear", description="Clear all songs")
        ]
        super().__init__(placeholder="Choose Queue Action", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        action = self.values[0]

        if action == "delete":
            if not SONG_QUEUES.get(guild_id):
                return await interaction.response.send_message("Queue is empty.", ephemeral=True)
            await interaction.response.send_modal(DeleteSongModal())
        
        elif action == "shuffle":
            queue = SONG_QUEUES.get(guild_id)
            if not queue or len(queue) < 2:
                return await interaction.response.send_message("Not enough items to shuffle.", ephemeral=True)
            
            temp = list(queue)
            random.shuffle(temp)
            SONG_QUEUES[guild_id] = deque(temp)
            await interaction.response.send_message("Queue shuffled.")
            
        elif action == "clear":
            if guild_id in SONG_QUEUES:
                SONG_QUEUES[guild_id].clear()
            await interaction.response.send_message("Queue cleared.")

class QueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(QueueActionSelect())

@bot.tree.command(name="queue", description="Show the current music queue.")
async def queue(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    current = LAST_PLAYED.get(guild_id)
    queue = SONG_QUEUES.get(guild_id, [])

    if not current and not queue:
        return await interaction.response.send_message("Nothing is playing and the queue is empty.")

    embed = discord.Embed(
        title="Music Queue",
        color=discord.Color.blurple(),
        description="Now playing and upcoming tracks."
    )

    if current:
        embed.add_field(
            name="Now Playing",
            value=f"**{current['title']}**\nRequested by: {interaction.user.mention}",
            inline=False
        )
        if current.get("thumbnail"):
            embed.set_thumbnail(url=current["thumbnail"])
        elif current.get("id"):
            # Fallback thumbnail if not stored
            embed.set_thumbnail(url=f"https://img.youtube.com/vi/{current['id']}/mqdefault.jpg")

    if queue:
        queue_list = ""
        for i, (_, title, _, _) in enumerate(list(queue)[:10], start=1):
            queue_list += f"{i}. {title}\n"
        
        if len(queue) > 10:
            queue_list += f"...and {len(queue) - 10} more tracks."
        
        embed.add_field(name="Up Next", value=queue_list, inline=False)
    else:
        embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

    embed.set_footer(text=f"Total Tracks: {len(queue) + (1 if current else 0)} | Autoplay: {'On' if AUTOPLAY.get(guild_id) else 'Off'}")

    await interaction.response.send_message(embed=embed, view=QueueView())


# Run the bot
if not TOKEN:
    raise TypeError("DISCORD_TOKEN tidak ditemukan di environment")
bot.run(TOKEN)
