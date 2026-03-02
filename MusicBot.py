import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from collections import deque
import asyncio
import random
import re

# Environment variables for tokens and other sensitive data
load_dotenv()

# --- Coordinator ---
class MultiBotCoordinator:
    def __init__(self):
        self.message_locks = set()
        self.vc_bindings = {}
        self.known_bot_ids = set()
        self.lock = asyncio.Lock()

    def claim_vc(self, bot_token_name, vc_id: int):
        self.vc_bindings[vc_id] = bot_token_name
        
    def release_vc(self, vc_id: int):
        self.vc_bindings.pop(vc_id, None)

    async def can_process(self, bot, message: discord.Message) -> bool:
        async with self.lock:
            user_vc = message.author.voice.channel if message.author.voice else None
            
            if user_vc:
                active_bot = self.vc_bindings.get(user_vc.id)
                if active_bot:
                    return bot.token_name == active_bot

                # Prevent a bot from responding if it is already active in a DIFFERENT Voice Channel in the same server
                guild = bot.get_guild(message.guild.id)
                if guild and guild.voice_client:
                    return False
            
            # Use message locks for unassigned VCs or commands outside of VC
            if message.id in self.message_locks:
                return False
            self.message_locks.add(message.id)
            
            loop = asyncio.get_running_loop()
            loop.call_later(5, self.message_locks.discard, message.id)
            return True

COORDINATOR = MultiBotCoordinator()

def normalize_title(title):
    """
    Cleans up song titles to help identify duplicate tracks.
    Removes common tags like (Official Video), [Lyrics], HD, etc.
    """
    if not title:
        return ""
    title = title.lower()
    title = re.sub(r'[\(\[][^\]\)]*[\]\)]', '', title)
    fillers = [
        'official video', 'official audio', 'official music video', 'official lyric video',
        'lyric video', 'lyrics', 'hd', '4k', 'mv', 'high quality', 'hq', 'audio'
    ]
    for filler in fillers:
        title = title.replace(filler, '')
    title = re.sub(r'[^\w\s]', '', title)
    return " ".join(title.split())

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

class MusicBot(commands.Bot):
    def __init__(self, token_name: str, *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=";", intents=intents, help_command=None, *args, **kwargs)
        self.campaign_rate = 0.35
        self.token_name = token_name
        self.song_queues = {}
        self.autoplay_status = {}
        self.last_played = {}
        self.play_history = {}
        self.idle_tasks = {}
        self.last_voice_channels = {}
        self.status_messages = {}
        self.active_owners = {}
        self.active_channels = {}
        self.last_command_channels = {}
        self.empty_vc_tasks = {}
        self.empty_vc_warned = {}

    async def setup_hook(self):
        self.add_check(self.coordinator_check)
        await self.add_cog(MusicCog(self))
        await self.tree.sync()

    async def on_ready(self):
        if self.user:
            COORDINATOR.known_bot_ids.add(self.user.id)
        print(f"[{self.token_name}] {self.user} is online!")
        await self.change_presence(activity=discord.Streaming(name="/help • Love JKT48", url="https://www.twitch.tv/discord"))

    async def coordinator_check(self, ctx):
        if ctx.interaction is not None:
            user_vc = ctx.author.voice.channel if ctx.author.voice else None
            active_bot = COORDINATOR.vc_bindings.get(user_vc.id) if user_vc else None
            
            if active_bot and active_bot != self.token_name:
                try:
                    await ctx.interaction.response.send_message("Access denied: another bot is already active in your voice channel. Use that bot's slash commands instead.", ephemeral=True)
                except Exception:
                    pass
                return False
            
            if user_vc and ctx.guild.voice_client and ctx.guild.voice_client.channel != user_vc:
                try:
                    await ctx.interaction.response.send_message("I am already active in another voice channel. Try using a different bot.", ephemeral=True)
                except Exception:
                    pass
                return False

            return True
        return await COORDINATOR.can_process(self, ctx.message)

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure) or isinstance(error, commands.CommandNotFound):
            return
        await super().on_command_error(ctx, error)

class DeleteSongModal(discord.ui.Modal, title="Remove Song from Queue"):
    song_index = discord.ui.TextInput(
        label="Song Number",
        placeholder="Enter song number (e.g., 1)",
        min_length=1,
        max_length=2
    )
    def __init__(self, bot: MusicBot):
        super().__init__()
        self.bot = bot
    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.song_index.value) - 1
            queue = self.bot.song_queues.get(str(interaction.guild_id))
            if not queue or idx < 0 or idx >= len(queue):
                return await interaction.response.send_message("Invalid song number.", ephemeral=True)
            temp = list(queue)
            song_title = temp.pop(idx)[1]
            self.bot.song_queues[str(interaction.guild_id)] = deque(temp)
            await interaction.response.send_message(f"Removed **{song_title}** from queue.")
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)

class QueueActionSelect(discord.ui.Select):
    def __init__(self, bot: MusicBot):
        options = [
            discord.SelectOption(label="Remove Song", value="delete", description="Remove a song from the queue"),
            discord.SelectOption(label="Shuffle Queue", value="shuffle", description="Shuffle the queue"),
            discord.SelectOption(label="Clear Queue", value="clear", description="Clear all songs")
        ]
        super().__init__(placeholder="Choose Queue Action", min_values=1, max_values=1, options=options)
        self.bot = bot
    async def callback(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        action = self.values[0]
        if action == "delete":
            if not self.bot.song_queues.get(guild_id):
                return await interaction.response.send_message("Queue is empty.", ephemeral=True)
            await interaction.response.send_modal(DeleteSongModal(self.bot))
        elif action == "shuffle":
            queue = self.bot.song_queues.get(guild_id)
            if not queue or len(queue) < 2:
                return await interaction.response.send_message("Not enough items to shuffle.", ephemeral=True)
            temp = list(queue)
            random.shuffle(temp)
            self.bot.song_queues[guild_id] = deque(temp)
            await interaction.response.send_message("Queue shuffled.")
        elif action == "clear":
            if guild_id in self.bot.song_queues:
                self.bot.song_queues[guild_id].clear()
            await interaction.response.send_message("Queue cleared.")

class QueueView(discord.ui.View):
    def __init__(self, bot: MusicBot):
        super().__init__(timeout=60)
        self.add_item(QueueActionSelect(bot))

class SearchSelect(discord.ui.Select):
    def __init__(self, results, bot: MusicBot):
        options = []
        for idx, item in enumerate(results[:25]):
            title = item.get("title", "Untitled")
            label = f"{idx+1}. {title}"[:100]
            options.append(discord.SelectOption(label=label, description=(item.get("uploader") or "")[:100], value=str(idx)))
        super().__init__(placeholder="Select a song (Top 25)", min_values=1, max_values=1, options=options)
        self.results = results
        self.bot = bot
    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        track = self.results[idx]
        thumbnail = track.get("thumbnail") or (track.get("thumbnails") or [{}])[0].get("url")
        voice_channel = interaction.user.voice.channel if interaction.user.voice else None
        if not voice_channel:
            return await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
        voice_client = interaction.guild.voice_client
        if not voice_client:
            voice_client = await voice_channel.connect(self_deaf=True, self_mute=False)
        elif voice_channel != voice_client.channel:
            await voice_client.move_to(voice_channel)
            try:
                await interaction.guild.change_voice_state(channel=voice_channel, self_deaf=True, self_mute=False)
            except Exception:
                pass
        guild_id = str(interaction.guild_id)
        self.bot.last_command_channels[guild_id] = interaction.channel.id
        if self.bot.song_queues.get(guild_id) is None:
            self.bot.song_queues[guild_id] = deque()
        self.bot.song_queues[guild_id].append((track["url"], track.get("title", "Untitled"), track.get("id"), thumbnail))
        cog = self.bot.get_cog("MusicCog")
        if voice_client.is_playing() or voice_client.is_paused():
            cog.cancel_idle(guild_id)
            await interaction.response.send_message(f"Added to queue: **{track.get('title', 'Untitled')}**")
        else:
            cog.cancel_idle(guild_id)
            await interaction.response.defer()
            await interaction.delete_original_response()
            await cog.play_next_song(voice_client, guild_id, interaction.channel)

class SearchView(discord.ui.View):
    def __init__(self, results, bot: MusicBot):
        super().__init__(timeout=120)
        self.add_item(SearchSelect(results, bot))

class MusicCog(commands.Cog):
    def __init__(self, bot: MusicBot):
        self.bot = bot

    def resolve_message_channel(self, source_channel):
        guild = source_channel.guild
        ch_id = self.bot.last_command_channels.get(str(guild.id))
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                return ch
        return source_channel
    
    async def maybe_send_campaign(self, channel):
        if random.random() < self.bot.campaign_rate:
            emb = discord.Embed(
                description="thank you for using our services!\n\n**Did you know?**\nThese bots are free for everyone. If you’ve enjoyed using them, consider supporting our developers with a [donation](https://saweria.co/Michenical) to help us continue building great tools.",
                color=discord.Color(0xA3EB23),
            )
            emb.set_image(url="https://files.catbox.moe/7bd55o.png")
            try:
                await channel.send(embed=emb)
            except Exception:
                pass

    def cancel_idle(self, guild_id):
        guild_id = str(guild_id)
        task = self.bot.idle_tasks.get(guild_id)
        if task:
            task.cancel()
            self.bot.idle_tasks.pop(guild_id, None)

    def schedule_idle_disconnect(self, voice_client, guild_id):
        guild_id = str(guild_id)
        self.cancel_idle(guild_id)
        async def _idle():
            await asyncio.sleep(900)
            if not self.bot.song_queues.get(guild_id):
                if voice_client and voice_client.is_connected() and not voice_client.is_playing() and not voice_client.is_paused():
                    await voice_client.disconnect()
        self.bot.idle_tasks[guild_id] = asyncio.create_task(_idle())

    def cancel_empty_vc_watch(self, guild_id):
        guild_id = str(guild_id)
        task = self.bot.empty_vc_tasks.get(guild_id)
        if task:
            task.cancel()
            self.bot.empty_vc_tasks.pop(guild_id, None)
        self.bot.empty_vc_warned[guild_id] = False

    async def schedule_empty_vc_watch(self, voice_client, guild_id, source_channel):
        guild_id = str(guild_id)
        self.cancel_empty_vc_watch(guild_id)
        async def _watch():
            await asyncio.sleep(180)
            vc = voice_client.channel if voice_client else None
            if vc:
                has_human = any(not m.bot for m in vc.members)
                if not has_human:
                    target_channel = self.resolve_message_channel(source_channel)
                    try:
                        emb = discord.Embed(title="Warning", description="No users in the voice channel for 3 minutes. Disconnecting in 2 minutes if still empty.", color=discord.Color(0xFFFF00))
                        await target_channel.send(embed=emb)
                    except Exception:
                        pass
                    self.bot.empty_vc_warned[guild_id] = True
                    await asyncio.sleep(120)
                    vc = voice_client.channel if voice_client else None
                    if vc:
                        has_human = any(not m.bot for m in vc.members)
                        if not has_human:
                            target_channel = self.resolve_message_channel(source_channel)
                            try:
                                emb2 = discord.Embed(description="👋 Disconnected: no users in the voice channel for 5 minutes", color=discord.Color(0xFF0000))
                                await target_channel.send(embed=emb2)
                            except Exception:
                                pass
                            await self.maybe_send_campaign(target_channel)
                            if voice_client and voice_client.is_connected():
                                await voice_client.disconnect()
        self.bot.empty_vc_tasks[guild_id] = asyncio.create_task(_watch())
    def clear_guild_data(self, guild_id):
        guild_id_str = str(guild_id)
        self.bot.song_queues.pop(guild_id_str, None)
        self.bot.autoplay_status.pop(guild_id_str, None)
        self.bot.last_played.pop(guild_id_str, None)
        self.bot.play_history.pop(guild_id_str, None)
        self.bot.active_owners.pop(guild_id_str, None)
        self.bot.active_channels.pop(guild_id_str, None)
        self.cancel_idle(guild_id_str)
        print(f"[{self.bot.token_name}] Cleared session data for guild {guild_id_str}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            if after.channel is not None:
                COORDINATOR.claim_vc(self.bot.token_name, after.channel.id)
            if before.channel is not None and after.channel is None:
                COORDINATOR.release_vc(before.channel.id)
                self.clear_guild_data(member.guild.id)
                
        # Handle owner leaving Voice Channel
        guild_id_str = str(member.guild.id)
        owner_id = self.bot.active_owners.get(guild_id_str)
        
        # If the member leaving is the owner
        if owner_id == member.id and before.channel is not None and (after.channel is None or after.channel != before.channel):
            bot_member = member.guild.get_member(self.bot.user.id)
            if bot_member and bot_member.voice and bot_member.voice.channel == before.channel:
                new_owner = None
                # Exclude bots and find a new owner
                for m in before.channel.members:
                    if not m.bot:
                        new_owner = m
                        break
                        
                if new_owner:
                    self.bot.active_owners[guild_id_str] = new_owner.id
                    active_text_channel_id = self.bot.active_channels.get(guild_id_str)
                    if active_text_channel_id:
                        text_channel = member.guild.get_channel(active_text_channel_id)
                        if text_channel:
                            # Send DM to simulate "ephemeral" or "visible only to authority"
                            try:
                                await new_owner.send(f"Control authority (owner) over the bot has been transferred to you because the previous owner left the voice channel.")
                            except Exception:
                                pass # DM closed
                else:
                    # No humans left in the voice channel, bot should probably leave? handled by idle check
                    pass
        vc = member.guild.voice_client
        if vc and vc.channel:
            has_human = any(not m.bot for m in vc.channel.members)
            if has_human:
                self.cancel_empty_vc_watch(member.guild.id)
            else:
                guild_id_str = str(member.guild.id)
                ch_id = self.bot.last_command_channels.get(guild_id_str) or self.bot.active_channels.get(guild_id_str)
                src_ch = member.guild.get_channel(ch_id) if ch_id else None
                await self.schedule_empty_vc_watch(vc, member.guild.id, src_ch or vc.channel)

    def check_ownership(self, ctx):
        guild_id = str(ctx.guild.id)
        owner_id = self.bot.active_owners.get(guild_id)
        if owner_id and ctx.author.id != owner_id:
            return False
        return True

    def check_active_channel(self, ctx):
        guild_id = str(ctx.guild.id)
        active_channel_id = self.bot.active_channels.get(guild_id)
        if active_channel_id:
            return True
        # If this bot is not active, check if ANY other bot is active.
        # If another bot is active, this inactive bot should ignore the command
        loop = asyncio.get_running_loop()
        for task in asyncio.all_tasks(loop):
            # This is a bit hacky, normally we'd have a central registry of bots
            # But we can just rely on the fact that if active_channel_id is None, it means the bot isn't playing here.
            pass
        return False

    @commands.hybrid_command(name="skip", description="Skips the current playing song")
    async def skip(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        if not self.check_ownership(ctx):
            return await ctx.send(f"Access denied: only <@{self.bot.active_owners.get(str(ctx.guild.id))}> can control this bot.")
        if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
            ctx.guild.voice_client.stop()
            await ctx.send("Skipped the current song.")
        else:
            await ctx.send("Not playing anything to skip.")

    @commands.hybrid_command(name="pause", description="Pause the currently playing song.")
    async def pause(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        if not self.check_ownership(ctx):
            return await ctx.send(f"Access denied: only <@{self.bot.active_owners.get(str(ctx.guild.id))}> can control this bot.")
        voice_client = ctx.guild.voice_client
        if voice_client is None:
            return await ctx.send("I'm not in a voice channel.")
        if not voice_client.is_playing():
            return await ctx.send("Nothing is currently playing.")
        voice_client.pause()
        await ctx.send("Playback paused!")

    @commands.hybrid_command(name="resume", description="Resume the currently paused song.")
    async def resume(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        if not self.check_ownership(ctx):
            return await ctx.send(f"Access denied: only <@{self.bot.active_owners.get(str(ctx.guild.id))}> can control this bot.")
        voice_client = ctx.guild.voice_client
        if voice_client is None:
            return await ctx.send("I'm not in a voice channel.")
        if not voice_client.is_paused():
            return await ctx.send("I’m not paused right now.")
        voice_client.resume()
        await ctx.send("Playback resumed!")

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue.")
    async def stop(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        if not self.check_ownership(ctx):
            return await ctx.send(f"Access denied: only <@{self.bot.active_owners.get(str(ctx.guild.id))}> can control this bot.")
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")
        guild_id_str = str(ctx.guild.id)
        if guild_id_str in self.bot.song_queues:
            self.bot.song_queues[guild_id_str].clear()
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await voice_client.disconnect()
        await ctx.send("Stopped playback, cleared data, and disconnected!")

    @commands.hybrid_command(name="play", description="Play a song or add it to the queue.")
    @app_commands.describe(song_query="Search query")
    async def play(self, ctx: commands.Context, *, song_query: str):
        guild_id = str(ctx.guild.id)
        
        voice_channel = ctx.author.voice.channel if ctx.author.voice else None
        if not voice_channel:
            return await ctx.send("You must be in a voice channel.")
            
        # --- EXCLUSIVITY CHECK ---
        # If this bot is already playing here, we skip checking other bots.
        # If this bot is NOT playing, check if ANY OTHER bot (from this script) is already in this VC.
        member_bots = [m for m in voice_channel.members if m.id in COORDINATOR.known_bot_ids]
        if member_bots and self.bot.user not in member_bots:
            # There is another bot (from this script) in this channel. Ignore silently if prefix command, otherwise reject if slash.
            if ctx.interaction:
                return await ctx.send("Access denied: another bot is already in this channel (max 1 bot per channel).", ephemeral=True)
            return
            
        # Only active bots or the *first* to respond claim the session
        # If this bot is not active, but another bot is active in THIS GUILD, we also probably want to ignore unless it's a different VC
        # The easiest approach: if we reach here and it's a prefix command, we lock it through the coordinator.
                
        # --- OWNERSHIP BINDING ---
        # Set the owner if the bot isn't currently bound to anyone
        if not self.bot.active_owners.get(guild_id):
            self.bot.active_owners[guild_id] = ctx.author.id
            self.bot.active_channels[guild_id] = ctx.channel.id
        self.bot.last_command_channels[guild_id] = ctx.channel.id
             
        await ctx.defer()
        voice_client = ctx.guild.voice_client
        if not voice_client:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            voice_client = await voice_channel.connect(self_deaf=True, self_mute=False)
        elif voice_channel != voice_client.channel:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            await voice_client.move_to(voice_channel)
            try:
                await ctx.guild.change_voice_state(channel=voice_channel, self_deaf=True, self_mute=False)
            except Exception:
                pass
        
        self.bot.last_voice_channels[guild_id] = voice_channel.id
        msg = await ctx.send("Searching for available songs... Please wait.")
        self.bot.status_messages[guild_id] = msg

        ydl_opts_search = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            "extract_flat": True,
        }

        try:
            results = await search_ytdlp_async(f"ytsearch5:{song_query}", ydl_opts_search)
        except Exception as e:
            return await msg.edit(content=f"Error discovering tracks: {e}")
        tracks = results.get("entries", [])
        if not tracks:
            return await msg.edit(content="No results found.")

        ydl_opts_extract = {
            "format": "bestaudio[abr<=96]/bestaudio",
            "noplaylist": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
        }

        valid_track = None
        for t in tracks:
            try:
                # Coba extract full info agar yakin videonya bisa diputar
                info = await search_ytdlp_async(t["url"], ydl_opts_extract)
                if info:
                    valid_track = info
                    break
            except Exception as e:
                print(f"[{self.bot.token_name}] Skipping unavailable video {t.get('url')}: {e}")
                continue

        if not valid_track:
            return await msg.edit(content="Sorry, all search results are problematic or unavailable.")

        audio_url = valid_track["url"]
        title = valid_track.get("title", "Untitled")
        video_id = valid_track.get("id")
        thumbnail = valid_track.get("thumbnail") or (valid_track.get("thumbnails") or [{}])[0].get("url")

        guild_id = str(ctx.guild.id)
        if self.bot.song_queues.get(guild_id) is None:
            self.bot.song_queues[guild_id] = deque()

        self.bot.song_queues[guild_id].append((audio_url, title, video_id, thumbnail))

        if voice_client.is_playing() or voice_client.is_paused():
            self.cancel_idle(guild_id)
            await msg.edit(content=f"Added to queue: **{title}**")
        else:
            self.cancel_idle(guild_id)
            await msg.edit(content=f"Preparing song: **{title}**")
            await self.play_next_song(voice_client, guild_id, ctx.channel)

    @commands.hybrid_command(name="search", description="Search top 10 results and select to play.")
    @app_commands.describe(query="Search query")
    async def search(self, ctx: commands.Context, *, query: str):
        voice_channel = ctx.author.voice.channel if ctx.author.voice else None
        if not voice_channel:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        
        member_bots = [m for m in voice_channel.members if m.id in COORDINATOR.known_bot_ids]
        if member_bots and self.bot.user not in member_bots:
            if ctx.interaction:
                return await ctx.send("Access denied: another bot is already in this channel (max 1 bot per channel).", ephemeral=True)
            return
        
        guild_id = str(ctx.guild.id)
        if not self.bot.active_owners.get(guild_id):
            self.bot.active_owners[guild_id] = ctx.author.id
            self.bot.active_channels[guild_id] = ctx.channel.id
        self.bot.last_command_channels[guild_id] = ctx.channel.id
        
        await ctx.defer()
        voice_client = ctx.guild.voice_client
        if not voice_client:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            voice_client = await voice_channel.connect(self_deaf=True, self_mute=False)
        elif voice_channel != voice_client.channel:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            await voice_client.move_to(voice_channel)
            try:
                await ctx.guild.change_voice_state(channel=voice_channel, self_deaf=True, self_mute=False)
            except Exception:
                pass
        self.bot.last_voice_channels[guild_id] = voice_channel.id
        ydl_options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            "extract_flat": True,
        }
        try:
            results = await search_ytdlp_async("ytsearch25: " + query, ydl_options)
        except Exception as e:
            return await ctx.send(f"An error occurred while searching: {e}")
            
        tracks = results.get("entries", [])
        if not tracks:
            return await ctx.send("No results found.")
        
        embed = discord.Embed(title="Search Results", description=f'Query: "{query}"', color=discord.Color.blue())
        for i, item in enumerate(tracks[:10], start=1):
            title = item.get("title", "Untitled")
            embed.add_field(name=f"{i}. {title}", value=item.get("uploader", "") or "\u200b", inline=False)
        target_channel = self.resolve_message_channel(ctx.channel)
        try:
            await target_channel.send(embed=embed, view=SearchView(tracks[:25], self.bot))
        except Exception:
            await ctx.send(embed=embed, view=SearchView(tracks[:25], self.bot))

    @commands.hybrid_command(name="queue", description="Show the current music queue.")
    async def queue(self, ctx: commands.Context):
        
        guild_id = str(ctx.guild.id)
        current = self.bot.last_played.get(guild_id)
        queue = self.bot.song_queues.get(guild_id, [])

        if not current and not queue:
            return await ctx.send("Nothing is playing and the queue is empty.")

        embed = discord.Embed(
            title="Music Queue",
            color=discord.Color.blurple(),
            description="Now playing and upcoming tracks."
        )

        if current:
            embed.add_field(
                name="Now Playing",
                value=f"**{current['title']}**\nRequested by: {ctx.author.mention}",
                inline=False
            )
            if current.get("thumbnail"):
                embed.set_thumbnail(url=current["thumbnail"])
            elif current.get("id"):
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

        embed.set_footer(text=f"Total Tracks: {len(queue) + (1 if current else 0)} | Autoplay: {'On' if self.bot.autoplay_status.get(guild_id) else 'Off'}")
        await ctx.send(embed=embed, view=QueueView(self.bot))

    @commands.hybrid_command(name="autoplay", description="Toggle autoplay for similar songs.")
    async def autoplay(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        owner_mention = f"<@{self.bot.active_owners.get(str(ctx.guild.id))}>" if self.bot.active_owners.get(str(ctx.guild.id)) else "the session owner"
        if not self.check_ownership(ctx):
            msg = f"Access denied: only {owner_mention} can control this bot."
            if ctx.interaction:
                return await ctx.send(msg, ephemeral=True)
            return await ctx.send(msg)
        
        guild_id = str(ctx.guild.id)
        enabled = not self.bot.autoplay_status.get(guild_id, False)
        self.bot.autoplay_status[guild_id] = enabled
        status = "Autoplay enabled. type `/autoplay` or `;autoplay` to disable" if enabled else "Autoplay disabled. type `/autoplay` or `;autoplay` to enable"
        embed = discord.Embed(title="Autoplay", description=status, color=discord.Color(0x2F3136))
        if ctx.interaction:
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="join", description="Join your voice channel.")
    async def join(self, ctx: commands.Context):
        voice_channel = ctx.author.voice.channel if ctx.author.voice else None
        if not voice_channel:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        guild_id = str(ctx.guild.id)
        if not self.bot.active_owners.get(guild_id):
            self.bot.active_owners[guild_id] = ctx.author.id
            self.bot.active_channels[guild_id] = ctx.channel.id
        self.bot.last_command_channels[guild_id] = ctx.channel.id
        member_bots = [m for m in voice_channel.members if m.id in COORDINATOR.known_bot_ids]
        if member_bots and self.bot.user not in member_bots:
            if ctx.interaction:
                return await ctx.send("Access denied: another bot is already in this channel (max 1 bot per channel).", ephemeral=True)
            return
        voice_client = ctx.guild.voice_client
        if not voice_client:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            await voice_channel.connect(self_deaf=True, self_mute=False)
        elif voice_channel != voice_client.channel:
            COORDINATOR.claim_vc(self.bot.token_name, voice_channel.id)
            await voice_client.move_to(voice_channel)
            try:
                await ctx.guild.change_voice_state(channel=voice_channel, self_deaf=True, self_mute=False)
            except Exception:
                pass
        self.bot.last_voice_channels[guild_id] = voice_channel.id
        target_channel = self.resolve_message_channel(ctx.channel)
        try:
            await target_channel.send(embed=emb)
        except Exception:
            await ctx.send(embed=emb)

    @commands.hybrid_command(name="leave", description="Leave the current voice channel.")
    async def leave(self, ctx: commands.Context):
        if not ctx.author.voice:
            if ctx.interaction:
                return await ctx.send("You must be in a voice channel.", ephemeral=True)
            return await ctx.send("You must be in a voice channel.")
        if not self.check_ownership(ctx):
            owner_mention = f"<@{self.bot.active_owners.get(str(ctx.guild.id))}>" if self.bot.active_owners.get(str(ctx.guild.id)) else "the session owner"
            msg = f"Access denied: only {owner_mention} can control this bot."
            if ctx.interaction:
                return await ctx.send(msg, ephemeral=True)
            return await ctx.send(msg)
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")
        vc_id = voice_client.channel.id if voice_client and voice_client.channel else None
        await voice_client.disconnect()
        if vc_id:
            COORDINATOR.release_vc(vc_id)
        self.clear_guild_data(ctx.guild.id)
        target_channel = self.resolve_message_channel(ctx.channel)
        emb = discord.Embed(title="Disconnected", description="Left the voice channel.", color=discord.Color(0x2F3136))
        try:
            await target_channel.send(embed=emb)
        except Exception:
            await ctx.send(embed=emb)
        await self.maybe_send_campaign(target_channel)

    @commands.hybrid_command(name="help", description="Show all available commands and their descriptions.")
    async def help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="Help",
            description="Available commands (slash or prefix ';'):",
            color=discord.Color(0x2F3136),
        )
        embed.add_field(
            name="Play",
            value="/play, ;play [query] — Play a song or add to queue",
            inline=False,
        )
        embed.add_field(
            name="Search",
            value="/search, ;search [query] — Show top results and select to play",
            inline=False,
        )
        embed.add_field(
            name="Skip",
            value="/skip, ;skip — Skip the current song",
            inline=False,
        )
        embed.add_field(
            name="Pause",
            value="/pause, ;pause — Pause playback",
            inline=False,
        )
        embed.add_field(
            name="Resume",
            value="/resume, ;resume — Resume playback",
            inline=False,
        )
        embed.add_field(
            name="Stop",
            value="/stop, ;stop — Stop playback and disconnect",
            inline=False,
        )
        embed.add_field(
            name="Join",
            value="/join, ;join — Make the bot join your voice channel",
            inline=False,
        )
        embed.add_field(
            name="Leave",
            value="/leave, ;leave — Make the bot leave the voice channel",
            inline=False,
        )
        embed.add_field(
            name="Queue",
            value="/queue, ;queue — Show the queue and manage items",
            inline=False,
        )
        embed.add_field(
            name="Autoplay",
            value="/autoplay, ;autoplay — Toggle autoplay similar tracks",
            inline=False,
        )
        embed.set_footer(text="Tip: Use /help or >help anytime.")
        await ctx.send(embed=embed)

    async def play_next_song(self, voice_client, guild_id, channel):
        if self.bot.song_queues.get(guild_id):
            audio_url, title, video_id, thumbnail = self.bot.song_queues[guild_id].popleft()
            if not voice_client or not voice_client.is_connected():
                vc_id = self.bot.last_voice_channels.get(guild_id)
                if vc_id:
                    target_vc = discord.utils.get(channel.guild.voice_channels, id=vc_id)
                    if target_vc:
                        voice_client = await target_vc.connect(self_deaf=True, self_mute=False)

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
                print(f"[{self.bot.token_name}] Failed to re-resolve URL for {title}: {e}")
                meta = {}
                
            self.bot.last_played[guild_id] = {
                "title": title,
                "url": audio_url,
                "id": video_id or (meta or {}).get("id"),
                "uploader": (meta or {}).get("uploader"),
                "uploader_id": (meta or {}).get("uploader_id"),
                "channel_id": (meta or {}).get("channel_id"),
                "thumbnail": (meta or {}).get("thumbnail") or thumbnail,
            }

            if guild_id not in self.bot.play_history:
                self.bot.play_history[guild_id] = deque(maxlen=20)
            
            norm_title = normalize_title(title)
            if norm_title and norm_title not in self.bot.play_history[guild_id]:
                self.bot.play_history[guild_id].append(norm_title)

            ffmpeg_options = {
                "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                "options": "-vn -c:a libopus -b:a 96k",
            }

            try:
                if os.name == "nt" and os.path.exists("bin\\ffmpeg\\ffmpeg.exe"):
                    source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="bin\\ffmpeg\\ffmpeg.exe")
                else:
                    source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="ffmpeg")
            except Exception:
                source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options)

            def after_play(error):
                if error:
                    print(f"[{self.bot.token_name}] Error playing {title}: {error}")
                asyncio.run_coroutine_threadsafe(self.play_next_song(voice_client, guild_id, channel), self.bot.loop)

            voice_client.play(source, after=after_play)
            now_msg = self.bot.status_messages.get(guild_id)
            emb = discord.Embed(title="Now Playing", description=f"**{title}**", color=discord.Color(0x2F3136))
            thumb = self.bot.last_played.get(guild_id, {}).get("thumbnail")
            if thumb:
                emb.set_thumbnail(url=thumb)
            if now_msg:
                await now_msg.edit(content=None, embed=emb)
                self.bot.status_messages.pop(guild_id, None)
            else:
                target_channel = self.resolve_message_channel(channel)
                try:
                    await target_channel.send(embed=emb)
                except Exception:
                    await channel.send(embed=emb)
            await self.maybe_send_campaign(self.resolve_message_channel(channel))
            self.cancel_idle(guild_id)
        else:
            if self.bot.autoplay_status.get(guild_id):
                seed = self.bot.last_played.get(guild_id, {})
                seed_id = seed.get("id")
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
                    guild_history = self.bot.play_history.get(guild_id, [])
                    
                    for e in entries:
                        eid = e.get("id")
                        etitle = (e.get("title") or "")
                        etitle_lower = etitle.lower()
                        dur = e.get("duration") or 0
                        if not eid or eid == seed_id:
                            continue
                        if normalize_title(etitle) in guild_history:
                            continue
                        if any(p in etitle_lower for p in patterns):
                            continue
                        if "official video" in etitle_lower and dur > 900:
                            continue
                        filtered.append(e)
                    
                    if filtered:
                        choice = random.choice(filtered[:10])
                        cid = choice.get("id")
                        curl = choice.get("url")
                        ctitle = choice.get("title", "Untitled")
                        if not curl or "videoplayback" not in str(curl):
                            info = await search_ytdlp_async(f"https://www.youtube.com/watch?v={cid}", ydl_options)
                            thumb = (info or {}).get("thumbnail") or (info or {}).get("thumbnails", [{}])[0].get("url")
                            self.bot.song_queues[guild_id].append((curl, ctitle, cid, thumb))
                            await self.play_next_song(voice_client, guild_id, channel)
                            return
                    f_results = await search_ytdlp_async("ytsearch1: " + (seed.get("title") or ""), ydl_options)
                    f_tracks = f_results.get("entries", []) if f_results else []
                    if f_tracks:
                        f_item = f_tracks[0]
                        fid = f_item.get("id")
                        if not any(p in (f_item.get("title") or "").lower() for p in patterns) and fid and fid != seed_id:
                            f_thumb = f_item.get("thumbnail") or (f_item.get("thumbnails") or [{}])[0].get("url")
                            self.bot.song_queues[guild_id].append((f_item["url"], f_item.get("title", "Untitled"), fid, f_thumb))
                            await self.play_next_song(voice_client, guild_id, channel)
                            return
            emb = discord.Embed(title="No More Tracks", description="There are no more tracks.", color=discord.Color(0x2F3136))
            target_channel = self.resolve_message_channel(channel)
            try:
                await target_channel.send(embed=emb)
            except Exception:
                await channel.send(embed=emb)
            self.schedule_idle_disconnect(voice_client, guild_id)

async def main():
    tokens = []
    # Find base token first for backwards compatibility
    if "DISCORD_TOKEN" in os.environ:
        tokens.append(("BOT_1", os.environ["DISCORD_TOKEN"]))
    
    # Read other specific tokens like DISCORD_TOKEN_2, DISCORD_TOKEN_3
    for key, value in os.environ.items():
        if key.startswith("DISCORD_TOKEN_") and value:
            tokens.append((key, value))

    if not tokens:
        raise ValueError("No DISCORD_TOKEN found in environment (.env).")

    print(f"Starting {len(tokens)} discord bot(s)...")
    
    tasks = []
    for token_name, tk in tokens:
        print(f"Initializing {token_name}...")
        b = MusicBot(token_name=token_name)
        task = asyncio.create_task(b.start(tk))
        tasks.append((b, task))
        
    await asyncio.gather(*(t[1] for t in tasks))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bots shutting down...")
