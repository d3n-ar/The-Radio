import asyncio
from datetime import datetime
import json
import os
import zoneinfo
import discord
import time
import random
from discord import app_commands
from discord.ext import commands, tasks

# Set up bot intents
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Configuration IDs
CHANNEL_ID = 1555193971347693633  # Replace with your actual Channel ID
ROLE_ID = 1555193970085068934     # Replace with your actual Role ID
GUILD_ID = 1555193970085068932    # Replace with your actual Server (Guild) ID for instant command sync
SAY_ALLOWED_USER_ID = 1066308834156625940

MAX_ATTACHMENT_BYTES = 128 * 1024 * 1024  # 128 MB
REPORT_CHANNEL_ID = 0
REPORT_COOLDOWN_SECONDS = 120
_report_last_used: dict[int, float] = {}

DATA_FILE = "data.json"

DEFAULT_CONFIG = {
    "Game_Event_Num": 13,
    "event_data": {
        "saturday_3pm": {
            "event_no": 6,
            "host_id": None,
            "host_name": "HostName1",
            "live_status": "non-live",
            "link": "to be filled",
        },
        "saturday_9pm": {
            "event_no": 7,
            "host_id": None,
            "host_name": "HostName2",
            "live_status": "non-live",
            "link": "to be filled",
        },
        "sunday_3pm": {
            "event_no": 8,
            "host_id": None,
            "host_name": "HostName3",
            "live_status": "non-live",
            "link": "to be filled",
        },
        "sunday_9pm": {
            "event_no": 9,
            "host_id": None,
            "host_name": "HostName4",
            "live_status": "non-live",
            "link": "to be filled",
        },
    },
    "host_event_counts": {},
    "last_posted_slot": "",
    "broadcast_channels": {},
}


def load_data():
    if not os.path.exists(DATA_FILE):
        return DEFAULT_CONFIG
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {DATA_FILE}: {e}. Falling back to default.")
        return DEFAULT_CONFIG


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


storage = load_data()
event_data = storage["event_data"]
game_event_num = storage.get("Game_Event_Num", 14)
host_event_counts = storage["host_event_counts"]
last_posted_slot = storage.get("last_posted_slot", "")
broadcast_channels: dict[str, int] = dict(storage.get("broadcast_channels", {}))


def find_member_by_name(guild: discord.Guild, name_query: str):
    query_lower = name_query.lower().replace("-", "_")
    for member in guild.members:
        member_username = member.name.lower().replace("-", "_")
        member_display = member.display_name.lower().replace("-", "_")
        if query_lower == member_username or query_lower == member_display or query_lower in member_username:
            return member
    return None


async def auto_configure_defaults():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel or not channel.guild:
        return

    guild = channel.guild

    # Loop through all 4 slots to check and bind initial default hosts if missing
    for slot_key in ["saturday_3pm", "saturday_9pm", "sunday_3pm", "sunday_9pm"]:
        fallback_name = event_data[slot_key].get("host_name", "Host")
        if not event_data[slot_key].get("host_id"):
            member = find_member_by_name(guild, fallback_name)
            if member:
                user_id_str = str(member.id)
                event_data[slot_key]["host_id"] = user_id_str
                if user_id_str not in host_event_counts:
                    host_event_counts[user_id_str] = 1

    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot,
        "broadcast_channels": broadcast_channels
    })


def build_event_message(slot_key: str) -> str:
    global game_event_num
    data = event_data[slot_key]
    host_id = data.get("host_id")
    fallback_name = data.get("host_name", "Unknown Host")

    host_text = f"<@{host_id}>" if host_id else fallback_name
    past_completed = host_event_counts.get(str(host_id), 0) if host_id else 0
    current_host_count = past_completed + 1

    message = (
        f"<@&{ROLE_ID}>\n"
        f"# Outcome Memories India - Weekly Game Event\n\n"
        f"**Game Event no. :- ** {game_event_num}\n\n"
        f"**Host:-** {host_text}\n"
        f"**Host Game Event Count:-** {current_host_count}\n\n"
        f"**Live Status:-** {data['live_status']}\n\n"
        f"**Game link:-** {data['link']}"
    )
    return message


def is_host_or_admin(interaction: discord.Interaction) -> bool:
    is_admin = interaction.permissions.administrator
    has_host_role = any(role.id == ROLE_ID for role in interaction.user.roles)
    return is_admin or has_host_role


def is_command_allowed_in_guild(interaction: discord.Interaction, *, allow_in_other_guilds: bool = False) -> bool:
    if interaction.guild_id == GUILD_ID:
        return True
    return allow_in_other_guilds

def _is_home_or_dm(interaction: discord.Interaction) -> bool:
    return interaction.guild_id is None or interaction.guild_id == GUILD_ID


def _find_writable_channel(
    guild: discord.Guild,
    *,
    need_attach_files: bool = False,
) -> discord.TextChannel | None:
    me = guild.me or guild.get_member(bot.user.id) if bot.user else None
    if me is None:
        return None

    for channel in sorted(guild.text_channels, key=lambda c: c.position):
        perms = channel.permissions_for(me)
        if perms.view_channel and perms.send_messages:
            if need_attach_files and not perms.attach_files:
                continue
            return channel
    return None

def _can_send_to_channel(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel | None,
    *,
    need_attach_files: bool = False,
) -> bool:
    if channel is None or getattr(channel, "guild", None) != guild:
        return False
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False

    me = guild.me or (guild.get_member(bot.user.id) if bot.user else None)
    if me is None:
        return False

    perms = channel.permissions_for(me)
    if not perms.view_channel or not perms.send_messages:
        return False
    if need_attach_files and not perms.attach_files:
        return False
    return True


def _pick_random_writable_channel(
    guild: discord.Guild,
    *,
    need_attach_files: bool = False,
) -> discord.TextChannel | None:
    writable: list[discord.TextChannel] = [
        ch
        for ch in guild.text_channels
        if _can_send_to_channel(guild, ch, need_attach_files=need_attach_files)
    ]
    if not writable:
        return None
    return random.choice(writable)


def _resolve_broadcast_channel(
    guild: discord.Guild,
    explicit_channel_id: int | None = None,
    *,
    need_attach_files: bool = False,
) -> tuple[discord.TextChannel | discord.Thread | None, str | None]:
    if explicit_channel_id is not None:
        ch = guild.get_channel(explicit_channel_id)
        if ch is None:
            return None, f"❌ Channel `{explicit_channel_id}` was not found in **{guild.name}**."
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return None, "❌ Target channel is not a supported text or thread channel."
        if not _can_send_to_channel(guild, ch, need_attach_files=need_attach_files):
            return None, f"❌ Missing required permissions in <#{ch.id}> (**{guild.name}**)."
        return ch, None

    saved_id = broadcast_channels.get(str(guild.id))
    if saved_id is not None:
        saved_ch = guild.get_channel(int(saved_id))
        if _can_send_to_channel(guild, saved_ch, need_attach_files=need_attach_files):
            return saved_ch, None

    random_ch = _pick_random_writable_channel(guild, need_attach_files=need_attach_files)
    if random_ch is None:
        return None, f"❌ No writable channel with required permissions found in **{guild.name}**."
    return random_ch, None


SLOT_CHOICES = [
    app_commands.Choice(name="Saturday 3:00 PM IST", value="saturday_3pm"),
    app_commands.Choice(name="Saturday 9:00 PM IST", value="saturday_9pm"),
    app_commands.Choice(name="Sunday 3:00 PM IST", value="sunday_3pm"),
    app_commands.Choice(name="Sunday 9:00 PM IST", value="sunday_9pm"),
]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    await auto_configure_defaults()

    try:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        synced = await bot.tree.sync()
        print(f"Cleared guild duplicates and synced {len(synced)} global command(s).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

    if not scheduled_event_checker.is_running():
        scheduled_event_checker.start()


# ---------------------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------------------

@bot.tree.command(name="host", description="Set the event host for a specific time slot")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(day=SLOT_CHOICES)
async def set_host(interaction: discord.Interaction, day: app_commands.Choice[str], user: discord.User):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    slot_key = day.value
    user_id_str = str(user.id)

    event_data[slot_key]["host_id"] = user_id_str
    event_data[slot_key]["host_name"] = user.display_name

    if user_id_str not in host_event_counts:
        host_event_counts[user_id_str] = 0

    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot
    })

    past_count = host_event_counts[user_id_str]
    upcoming_count = past_count + 1

    await interaction.response.send_message(
        f"✅ Host for **{day.name}** set to {user.mention} (This will be their **#{upcoming_count}** event).",
        ephemeral=True,
    )


@bot.tree.command(name="livestatus", description="Set the live status for a specific time slot")
@app_commands.choices(
    day=SLOT_CHOICES,
    status=[
        app_commands.Choice(name="live", value="live"),
        app_commands.Choice(name="non-live", value="non-live"),
    ],
)
async def set_live_status(interaction: discord.Interaction, day: app_commands.Choice[str], status: app_commands.Choice[str]):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ You must have the Event Host role or Administrator permissions to use this command.", ephemeral=True)
        return

    event_data[day.value]["live_status"] = status.value
    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot
    })

    await interaction.response.send_message(
        f"✅ Live status for **{day.name}** updated to **{status.value}**.", ephemeral=True
    )


@bot.tree.command(name="arsay", description="Send a message and/or attachment as the bot")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    text="The message content to send",
    attachment="An optional file or image to send (max 128 MB)",
    message_id="Optional message ID in this channel to reply to (requires bot in server)",
)
async def arsay_command(
    interaction: discord.Interaction,
    text: str | None = None,
    attachment: discord.Attachment | None = None,
    message_id: str | None = None,
):
    if not is_command_allowed_in_guild(interaction, allow_in_other_guilds=True):
        await interaction.response.send_message(
            "❌ This command is not available here.",
            ephemeral=True,
        )
        return

    if interaction.user.id != SAY_ALLOWED_USER_ID:
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    if not text and attachment is None:
        await interaction.response.send_message(
            "❌ Provide at least some text or an attachment to send.",
            ephemeral=True,
        )
        return

    if attachment is not None and attachment.size > MAX_ATTACHMENT_BYTES:
        await interaction.response.send_message(
            "❌ Attachment exceeds the 128 MB limit.",
            ephemeral=True,
        )
        return

    bot_in_guild = (
        interaction.guild is not None
        and interaction.guild.get_member(bot.user.id) is not None
    )

    if not bot_in_guild:
        if message_id is not None:
            await interaction.response.send_message(
                "❌ Replying to a message ID requires the bot to be invited to this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)
        try:
            discord_file = await attachment.to_file() if attachment is not None else None
            if discord_file is not None:
                await interaction.followup.send(content=text, file=discord_file)
            else:
                await interaction.followup.send(content=text)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to send: {e}")
        return

    target_channel = interaction.channel
    if not target_channel:
        await interaction.response.send_message(
            "❌ Could not access this channel.",
            ephemeral=True,
        )
        return

    parsed_message_id: int | None = None
    if message_id is not None:
        cleaned_id = message_id.strip()
        if not cleaned_id.isdigit():
            await interaction.response.send_message(
                "❌ Invalid message ID. Provide a numeric message ID.",
                ephemeral=True,
            )
            return
        parsed_message_id = int(cleaned_id)

    await interaction.response.defer(ephemeral=True)

    try:
        reply_target = None
        if parsed_message_id is not None:
            reply_target = await target_channel.fetch_message(parsed_message_id)

        discord_file = await attachment.to_file() if attachment is not None else None
        await target_channel.send(
            content=text,
            file=discord_file,
            reference=reply_target,
        )
        await interaction.followup.send("✅ Sent.", ephemeral=True)
    except discord.NotFound:
        await interaction.followup.send("message deleted", ephemeral=True)
    except discord.Forbidden as e:
        await interaction.followup.send(
            f"❌ Missing channel permission here (`{e}`).",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Failed to send message: {e}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)


@bot.tree.command(name="arreact", description="Add up to 6 reactions to a message in this channel")
@app_commands.describe(
    message_id="The message ID in this channel to react with",
    emoji_1="First emoji to react with",
    emoji_2="Second emoji to react with (optional)",
    emoji_3="Third emoji to react with (optional)",
    emoji_4="Fourth emoji to react with (optional)",
    emoji_5="Fifth emoji to react with (optional)",
    emoji_6="Sixth emoji to react with (optional)",
)
async def arreact_command(
    interaction: discord.Interaction,
    message_id: str,
    emoji_1: str,
    emoji_2: str | None = None,
    emoji_3: str | None = None,
    emoji_4: str | None = None,
    emoji_5: str | None = None,
    emoji_6: str | None = None,
):
    if not is_command_allowed_in_guild(interaction, allow_in_other_guilds=True):
        await interaction.response.send_message(
            "❌ This command is not available here.",
            ephemeral=True,
        )
        return

    if interaction.user.id != SAY_ALLOWED_USER_ID:
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    target_channel = interaction.channel
    if not target_channel:
        await interaction.response.send_message(
            "❌ Could not access this channel.",
            ephemeral=True,
        )
        return

    cleaned_id = message_id.strip()
    if not cleaned_id.isdigit():
        await interaction.response.send_message(
            "❌ Invalid message ID. Provide a numeric message ID.",
            ephemeral=True,
        )
        return

    raw_emojis = [emoji_1, emoji_2, emoji_3, emoji_4, emoji_5, emoji_6]
    emojis_to_add = [e.strip() for e in raw_emojis if e is not None and e.strip()]

    if not emojis_to_add:
        await interaction.response.send_message(
            "❌ Provide at least one valid emoji.",
            ephemeral=True,
        )
        return

    if len(emojis_to_add) > 6:
        await interaction.response.send_message(
            "❌ You can add at most 6 reactions at a time.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_message = await target_channel.fetch_message(int(cleaned_id))
        for emoji in emojis_to_add:
            await target_message.add_reaction(emoji)

        await interaction.followup.send(
            f"✅ Added {len(emojis_to_add)} reaction(s).",
            ephemeral=True,
        )
    except discord.NotFound:
        await interaction.followup.send(
            "message deleted",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Missing permission to view message history or add reactions in this channel.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Failed to add reaction (check that all emojis are valid): {e}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)


@bot.tree.command(name="eventlink", description="Set the game link for a specific time slot")
@app_commands.choices(day=SLOT_CHOICES)
async def set_event_link(interaction: discord.Interaction, day: app_commands.Choice[str], link_here: str):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ You must have the Event Host role or Administrator permissions to use this command.", ephemeral=True)
        return

    event_data[day.value]["link"] = link_here
    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot
    })

    await interaction.response.send_message(
        f"✅ Game link for **{day.name}** set to: {link_here}", ephemeral=True
    )


@bot.tree.command(name="addlink", description="Allows the current event host to update the game link in the latest event message")
@app_commands.describe(link_here="The new game link to set")
async def add_link(interaction: discord.Interaction, link_here: str):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ Only assigned event hosts or administrators can use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_message = None

    async for message in channel.history(limit=50):
        if message.author.id == bot.user.id and "# Outcome Memories India - Weekly Game Event" in message.content:
            target_message = message
            break

    if not target_message:
        await interaction.followup.send("no messages found", ephemeral=True)
        return

    content = target_message.content
    if "**Game link:-**" in content:
        base_part = content.split("**Game link:-**")[0]
        updated_content = f"{base_part}**Game link:-** {link_here}"
        await target_message.edit(content=updated_content)
        await interaction.followup.send(f"✅ Game link updated successfully to: {link_here}", ephemeral=True)
    else:
        await interaction.followup.send("no messages found", ephemeral=True)


@bot.tree.command(name="endevent", description="Replaces the game link in the last event message with 'event over'")
async def end_event(interaction: discord.Interaction):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ Only assigned event hosts or administrators can use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_message = None

    async for message in channel.history(limit=50):
        if message.author.id == bot.user.id and "# Outcome Memories India - Weekly Game Event" in message.content:
            target_message = message
            break

    if not target_message:
        await interaction.followup.send("no messages found", ephemeral=True)
        return

    content = target_message.content
    if "**Game link:-**" in content:
        base_part = content.split("**Game link:-**")[0]
        updated_content = f"{base_part}**Game link:-** event over"
        await target_message.edit(content=updated_content)
        await interaction.followup.send("✅ Event link updated to **event over** in the latest event post.", ephemeral=True)
    else:
        await interaction.followup.send("no messages found", ephemeral=True)


@bot.tree.command(name="testevent", description="Preview the exact event message without waiting for schedule")
@app_commands.choices(day=SLOT_CHOICES)
async def test_event(interaction: discord.Interaction, day: app_commands.Choice[str]):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    preview_message = build_event_message(day.value)
    await interaction.response.send_message(
        f"**[Preview for {day.name}]**\n\n{preview_message}", ephemeral=True
    )


@bot.tree.command(name="postevent", description="Forces the bot to immediately send the scheduled event message for a slot")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(day=SLOT_CHOICES)
async def post_event(interaction: discord.Interaction, day: app_commands.Choice[str]):
    global game_event_num, last_posted_slot
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Channel not found or invalid CHANNEL_ID.", ephemeral=True)
        return

    slot_key = day.value
    message_text = build_event_message(slot_key)

    try:
        await channel.send(message_text)

        host_id = event_data[slot_key].get("host_id")
        if host_id:
            host_event_counts[host_id] = host_event_counts.get(host_id, 0) + 1

        game_event_num += 1
        event_data[slot_key]["live_status"] = "non-live"
        event_data[slot_key]["link"] = "to be filled"

        ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        last_posted_slot = f"{today_str}_{slot_key}"

        save_data({
            "Game_Event_Num": game_event_num,
            "event_data": event_data,
            "host_event_counts": host_event_counts,
            "last_posted_slot": last_posted_slot
        })

        await interaction.response.send_message(f"✅ Successfully posted the **{day.name}** event message to <#{CHANNEL_ID}>!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to post event message: {e}", ephemeral=True)


@bot.tree.command(name="tophosts", description="Displays the top 10 event hosts leaderboard")
async def top_hosts(interaction: discord.Interaction):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    if not host_event_counts:
        await interaction.response.send_message("❌ No host data recorded yet.", ephemeral=True)
        return

    sorted_hosts = sorted(host_event_counts.items(), key=lambda item: item[1], reverse=True)
    top_10 = sorted_hosts[:10]

    leaderboard_lines = []
    for rank, (host_key, count) in enumerate(top_10, start=1):
        host_display = f"<@{host_key}>" if host_key.isdigit() else host_key
        medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"`#{rank}`"
        leaderboard_lines.append(f"{medal} **{host_display}** — {count} event(s)")

    leaderboard_text = "\n".join(leaderboard_lines)

    embed = discord.Embed(
        title="🏆 Outcome Memories India - Top Event Hosts",
        description=leaderboard_text,
        color=discord.Color.gold()
    )
    embed.set_footer(text="Keep hosting to climb the leaderboard!")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="cancelevent", description="Cancels an upcoming scheduled slot for this occurrence")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(day=SLOT_CHOICES)
async def cancel_event(interaction: discord.Interaction, day: app_commands.Choice[str]):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    slot_key = day.value
    event_data[slot_key]["live_status"] = "canceled"
    event_data[slot_key]["link"] = "event canceled"

    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot
    })

    await interaction.response.send_message(
        f"🚫 Upcoming **{day.name}** event has been marked as **canceled** for this occurrence and will be skipped.",
        ephemeral=False
    )


@bot.tree.command(name="abandonevent", description="Abandons the current running event and reverts host and event count")
async def abandon_event(interaction: discord.Interaction):
    global game_event_num, last_posted_slot
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ Only assigned event hosts or administrators can use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_message = None

    async for message in channel.history(limit=50):
        if message.author.id == bot.user.id and "# Outcome Memories India - Weekly Game Event" in message.content:
            target_message = message
            break

    if not target_message:
        await interaction.followup.send("❌ No recent event message found.", ephemeral=True)
        return

    content = target_message.content
    if "**Game link:-**" in content:
        base_part = content.split("**Game link:-**")[0]
        updated_content = f"{base_part}**Game link:-** event abandoned"
        await target_message.edit(content=updated_content)

        ist = zoneinfo.ZoneInfo("Asia/Kolkata")
        now = datetime.now(ist)

        if now.weekday() == 5:
            slot_key = "saturday_3pm" if now.hour < 18 else "saturday_9pm"
        else:
            slot_key = "sunday_3pm" if now.hour < 18 else "sunday_9pm"

        host_id = event_data[slot_key].get("host_id")
        if host_id and host_id in host_event_counts and host_event_counts[host_id] > 0:
            host_event_counts[host_id] -= 1

        # Decrement global event number back down so the next event reuses it
        if game_event_num > 1:
            game_event_num -= 1

        save_data({
            "Game_Event_Num": game_event_num,
            "event_data": event_data,
            "host_event_counts": host_event_counts,
            "last_posted_slot": last_posted_slot
        })

        await interaction.followup.send(
            "⚠️ Event updated to **event abandoned**. Host event count has been reverted.",
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ Could not parse event link format in target message.", ephemeral=True)


@bot.tree.command(name="removelinkforce", description="Forcefully removes a game link from a specific event message ID")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message_id="The message ID of the event post to clean up")
async def remove_link_force(interaction: discord.Interaction, message_id: str):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ Channel not found or invalid CHANNEL_ID.", ephemeral=True)
        return

    if not message_id.isdigit():
        await interaction.response.send_message("❌ Invalid Message ID provided. Please provide a valid numeric ID.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        target_message = await channel.fetch_message(int(message_id))
    except Exception as e:
        await interaction.followup.send(f"❌ Error retrieving message: {e}", ephemeral=True)
        return

    if target_message.author.id != bot.user.id or "# Outcome Memories India - Weekly Game Event" not in target_message.content:
        await interaction.followup.send("❌ Security Check Failed: Message not posted by this bot or not a valid event post.", ephemeral=True)
        return

    content = target_message.content
    if "**Game link:-**" in content:
        base_part = content.split("**Game link:-**")[0]
        updated_content = f"{base_part}**Game link:-** event over"
        await target_message.edit(content=updated_content)
        await interaction.followup.send(f"🔒 Game link removed and set to **event over** for message `{message_id}`.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Could not parse link section.", ephemeral=True)


@bot.tree.command(name="revertcancel", description="Reverts a canceled slot back to non-live")
@app_commands.choices(day=SLOT_CHOICES)
async def revert_cancel(interaction: discord.Interaction, day: app_commands.Choice[str]):
    if not is_command_allowed_in_guild(interaction):
        await interaction.response.send_message("❌ This command can only be used in the main server.", ephemeral=True)
        return

    if not is_host_or_admin(interaction):
        await interaction.response.send_message("❌ Only assigned event hosts or administrators can use this command.", ephemeral=True)
        return

    slot_key = day.value

    if event_data[slot_key].get("live_status") != "canceled":
        await interaction.response.send_message(
            f"⚠️ The **{day.name}** event is not currently marked as canceled.",
            ephemeral=True
        )
        return

    event_data[slot_key]["live_status"] = "non-live"
    event_data[slot_key]["link"] = "to be filled"

    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot
    })

    await interaction.response.send_message(
        f"✅ Cancellation reverted for **{day.name}** event! Live status is set to **non-live**.",
        ephemeral=False
    )

@bot.tree.command(
    name="setbroadcastchannel",
    description="Set the default channel for broadcast messages in this server",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(channel_id="Channel ID in this server to use for broadcasts")
async def set_broadcast_channel_command(interaction: discord.Interaction, channel_id: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "❌ This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Only server administrators can use `/setbroadcastchannel`.",
            ephemeral=True,
        )
        return

    cleaned_id = channel_id.strip()
    if not cleaned_id.isdigit():
        await interaction.response.send_message(
            "❌ Invalid channel ID. Provide a numeric channel ID.",
            ephemeral=True,
        )
        return

    target_id = int(cleaned_id)
    target_channel = interaction.guild.get_channel(target_id)
    if target_channel is None:
        await interaction.response.send_message(
            f"❌ Channel `{target_id}` was not found in **{interaction.guild.name}**.",
            ephemeral=True,
        )
        return

    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "❌ Please select a text channel or thread.",
            ephemeral=True,
        )
        return

    broadcast_channels[str(interaction.guild.id)] = target_id
    save_data({
        "Game_Event_Num": game_event_num,
        "event_data": event_data,
        "host_event_counts": host_event_counts,
        "last_posted_slot": last_posted_slot,
        "broadcast_channels": broadcast_channels,
    })

    await interaction.response.send_message(
        f"✅ Saved <#{target_channel.id}> as the default broadcast channel for **{interaction.guild.name}**.",
        ephemeral=True,
    )


@bot.tree.command(
    name="broadcast",
    description="Send a message and/or attachment to a specific server",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    guild_id="Target server (guild) ID",
    channel_id="Optional target channel ID (uses saved default or random writable channel if omitted)",
    text="Message text to send",
    attachment="Optional attachment (max 128 MB)",
)
async def broadcast_command(
    interaction: discord.Interaction,
    guild_id: str,
    channel_id: str | None = None,
    text: str | None = None,
    attachment: discord.Attachment | None = None,
):
    #if not _is_home_or_dm(interaction):
    #    await interaction.response.send_message(
    #        "❌ `/broadcast` can only be used from the main server or in DMs.",
    #        ephemeral=True,
    #    )
    #    return

    if interaction.user.id != SAY_ALLOWED_USER_ID:
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    cleaned_guild_id = guild_id.strip()
    if not cleaned_guild_id.isdigit():
        await interaction.response.send_message(
            "❌ Invalid guild ID. Provide a numeric server ID.",
            ephemeral=True,
        )
        return

    parsed_channel_id: int | None = None
    if channel_id is not None:
        cleaned_channel_id = channel_id.strip()
        if not cleaned_channel_id.isdigit():
            await interaction.response.send_message(
                "❌ Invalid channel ID. Provide a numeric channel ID.",
                ephemeral=True,
            )
            return
        parsed_channel_id = int(cleaned_channel_id)

    if not text and attachment is None:
        await interaction.response.send_message(
            "❌ Provide at least some text or an attachment to send.",
            ephemeral=True,
        )
        return

    if attachment is not None and attachment.size > MAX_ATTACHMENT_BYTES:
        await interaction.response.send_message(
            "❌ Attachment exceeds the 128 MB limit.",
            ephemeral=True,
        )
        return

    target_guild = bot.get_guild(int(cleaned_guild_id))
    if target_guild is None:
        await interaction.response.send_message(
            "❌ Bot is not in a server with that guild ID or the ID is invalid.",
            ephemeral=True,
        )
        return

    target_channel, error_msg = _resolve_broadcast_channel(
        target_guild,
        parsed_channel_id,
        need_attach_files=(attachment is not None),
    )
    if target_channel is None:
        await interaction.response.send_message(
            error_msg or "❌ Could not resolve a target channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        discord_file = await attachment.to_file() if attachment is not None else None
        if discord_file is not None:
            await target_channel.send(content=text, file=discord_file)
        else:
            await target_channel.send(content=text)

        await interaction.followup.send(
            f"✅ Broadcast sent to <#{target_channel.id}> in **{target_guild.name}**.",
            ephemeral=True,
        )
    except discord.Forbidden as e:
        await interaction.followup.send(
            f"❌ Forbidden when sending to <#{target_channel.id}> in **{target_guild.name}**: `{e}`",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Failed to broadcast message: `{e}`",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)


@bot.tree.command(
    name="broadcastall",
    description="Send a message and/or attachment to all servers the bot is in",
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    text="Message text to send to all servers",
    attachment="Optional attachment (max 128 MB)",
)
async def broadcastall_command(
    interaction: discord.Interaction,
    text: str | None = None,
    attachment: discord.Attachment | None = None,
):
    #if not _is_home_or_dm(interaction):
    #    await interaction.response.send_message(
    #        "❌ `/broadcastall` can only be used from the main server or in DMs.",
    #        ephemeral=True,
    #    )
    #    return

    if interaction.user.id != SAY_ALLOWED_USER_ID:
        await interaction.response.send_message(
            "❌ You are not authorized to use this command.",
            ephemeral=True,
        )
        return

    if not text and attachment is None:
        await interaction.response.send_message(
            "❌ Provide at least some text or an attachment to send.",
            ephemeral=True,
        )
        return

    if attachment is not None and attachment.size > MAX_ATTACHMENT_BYTES:
        await interaction.response.send_message(
            "❌ Attachment exceeds the 128 MB limit.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    sent_count = 0
    skipped_count = 0
    failed_guilds: list[str] = []

    for guild in bot.guilds:
        channel, _ = _resolve_broadcast_channel(
            guild,
            explicit_channel_id=None,
            need_attach_files=(attachment is not None),
        )
        if channel is None:
            skipped_count += 1
            continue

        try:
            discord_file = await attachment.to_file() if attachment is not None else None
            if discord_file is not None:
                await channel.send(content=text, file=discord_file)
            else:
                await channel.send(content=text)
            sent_count += 1
        except Exception as e:
            failed_guilds.append(f"{guild.name} (`{guild.id}`): {e}")

    summary = (
        f"✅ Broadcast complete.\n"
        f"• Sent: **{sent_count}** server(s)\n"
        f"• Skipped (no writable channel/perms): **{skipped_count}**"
    )
    if failed_guilds:
        preview_errors = "\n".join(failed_guilds[:5])
        summary += f"\n• Failed (**{len(failed_guilds)}**):\n{preview_errors}"

    await interaction.followup.send(summary, ephemeral=True)


@bot.tree.command(name="report", description="Send a report to the designated report channel")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(
    text="Details of your report",
    attachment="Optional screenshot or file (max 128 MB)",
)
async def report_command(
    interaction: discord.Interaction,
    text: str | None = None,
    attachment: discord.Attachment | None = None,
):
    if not is_command_allowed_in_guild(interaction, allow_in_other_guilds=False) and interaction.guild_id is not None:
        await interaction.response.send_message(
            "❌ `/report` is not available in this server.",
            ephemeral=True,
        )
        return

    if not text and attachment is None:
        await interaction.response.send_message(
            "❌ Provide report details or an attachment.",
            ephemeral=True,
        )
        return

    if attachment is not None and attachment.size > MAX_ATTACHMENT_BYTES:
        await interaction.response.send_message(
            "❌ Attachment exceeds the 128 MB limit.",
            ephemeral=True,
        )
        return

    now = time.monotonic()
    last_used = _report_last_used.get(interaction.user.id)
    if last_used is not None:
        elapsed = now - last_used
        if elapsed < REPORT_COOLDOWN_SECONDS:
            remaining = int(REPORT_COOLDOWN_SECONDS - elapsed) + 1
            await interaction.response.send_message(
                f"⏳ Please wait **{remaining}s** before sending another report.",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True)

    try:
        report_channel = bot.get_channel(REPORT_CHANNEL_ID)
        if report_channel is None:
            report_channel = await bot.fetch_channel(REPORT_CHANNEL_ID)

        if not isinstance(report_channel, (discord.TextChannel, discord.Thread)):
            await interaction.followup.send(
                "❌ Configured report channel is not a valid text channel.",
                ephemeral=True,
            )
            return

        source_info = (
            f"**{interaction.guild.name}** (`{interaction.guild.id}`) / <#{interaction.channel_id}>"
            if interaction.guild is not None
            else "Direct Messages"
        )
        report_body = (
            f"📩 **New Report**\n"
            f"**From:** {interaction.user.mention} (`{interaction.user}` | ID: `{interaction.user.id}`)\n"
            f"**Source:** {source_info}\n\n"
            f"**Message:**\n{text if text else '*(No text provided)*'}"
        )

        discord_file = await attachment.to_file() if attachment is not None else None
        if discord_file is not None:
            await report_channel.send(content=report_body, file=discord_file)
        else:
            await report_channel.send(content=report_body)

        _report_last_used[interaction.user.id] = time.monotonic()
        await interaction.followup.send(
            "✅ Your report has been sent.",
            ephemeral=True,
        )
    except discord.NotFound:
        await interaction.followup.send(
            "❌ Report channel could not be found.",
            ephemeral=True,
        )
    except discord.Forbidden as e:
        await interaction.followup.send(
            f"❌ Bot does not have permission to post in the report channel (`{e}`).",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to send report: {e}",
            ephemeral=True,
        )

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Only administrators can use this command!", ephemeral=True)


# ---------------------------------------------------------------------------
# TIME CHECKER BACKGROUND TASK (IST)
# ---------------------------------------------------------------------------

@tasks.loop(minutes=1)
async def scheduled_event_checker():
    global last_posted_slot, game_event_num

    ist = zoneinfo.ZoneInfo("Asia/Kolkata")
    now = datetime.now(ist)
    today_str = now.strftime("%Y-%m-%d")

    target_slot = None

    # Saturday checks (weekday == 5)
    if now.weekday() == 5:
        if now.hour == 15:
            target_slot = "saturday_3pm"
        elif now.hour == 21:
            target_slot = "saturday_9pm"

    # Sunday checks (weekday == 6)
    elif now.weekday() == 6:
        if now.hour == 15:
            target_slot = "sunday_3pm"
        elif now.hour == 21:
            target_slot = "sunday_9pm"

    if target_slot:
        slot_identifier = f"{today_str}_{target_slot}"

        if last_posted_slot != slot_identifier:
            channel = bot.get_channel(CHANNEL_ID)
            if channel:
                # If canceled, skip posting for this occurrence and reset for future weeks
                if event_data[target_slot].get("live_status") == "canceled":
                    last_posted_slot = slot_identifier
                    event_data[target_slot]["live_status"] = "non-live"
                    event_data[target_slot]["link"] = "to be filled"
                    save_data({
                        "Game_Event_Num": game_event_num,
                        "event_data": event_data,
                        "host_event_counts": host_event_counts,
                        "last_posted_slot": last_posted_slot
                    })
                    print(f"[Auto-Post] Skipped canceled event for {target_slot} ({slot_identifier})")
                else:
                    message_text = build_event_message(target_slot)
                    try:
                        await channel.send(message_text)

                        host_id = event_data[target_slot].get("host_id")
                        if host_id:
                            host_event_counts[host_id] = host_event_counts.get(host_id, 0) + 1

                        game_event_num += 1
                        event_data[target_slot]["live_status"] = "non-live"
                        event_data[target_slot]["link"] = "to be filled"

                        last_posted_slot = slot_identifier

                        save_data({
                            "Game_Event_Num": game_event_num,
                            "event_data": event_data,
                            "host_event_counts": host_event_counts,
                            "last_posted_slot": last_posted_slot
                        })
                        print(f"[Auto-Post] Successfully posted event for {target_slot} ({slot_identifier})")
                    except discord.Forbidden:
                        print(f"[Auto-Post Error] Missing channel permissions in Channel ID {CHANNEL_ID}.")
                    except Exception as e:
                        print(f"[Auto-Post Error] Failed to send scheduled message: {e}")


@scheduled_event_checker.before_loop
async def before_checker():
    await bot.wait_until_ready()

bot.run("YOUR_BOT_TOKEN")