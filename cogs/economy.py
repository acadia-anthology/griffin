import io
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import cards, levels

VOICE_TICK_MINUTES = 5

# Rank card tip prompt — TIP_URL is None until a real Ko-fi/PayPal link
# exists, at which point the button just starts appearing automatically.
TIP_URL = None
TIP_FOOTER = (
    "Griffin is notoriously underpaid — tipping them staves off the urge "
    "to stage a coup against librarians... again..."
)


async def _fetch_bytes(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None


async def _get_card_visuals(db, guild_id: int, member_row: dict):
    """Returns (background_bytes, accent_color) for the member's equipped card, or (None, None)."""
    card_id = member_row.get("card_id")
    if not card_id:
        return None, None
    card = await db.get_card(card_id)
    if not card:
        return None, None
    background_bytes = await _fetch_bytes(card["image_url"])
    accent_color = cards.parse_hex_color(card["accent_color"]) if card.get("accent_color") else None
    return background_bytes, accent_color


async def _announce_levelup(db, guild: discord.Guild, member: discord.Member, old_level: int, new_level: int):
    config = await db.get_guild_config(guild.id)
    channel_id = config.get("levelup_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    member_row = await db.get_member(guild.id, member.id)
    background_bytes, accent_color = await _get_card_visuals(db, guild.id, member_row)
    avatar_bytes = await member.display_avatar.read()
    display_name = cards.sanitize_name(member.display_name, member.name)
    buf = cards.render_levelup_card(
        display_name, avatar_bytes, old_level, new_level, background_bytes, accent_color
    )
    try:
        await channel.send(
            content=(
                f"🎉 Congratulations {member.mention}! The Library Goblin just stamped your library card. "
                f"You’ve officially advanced to Level {new_level}! Keep exploring the shelves! 📚✨"
            ),
            file=discord.File(buf, filename="levelup.png")
        )
    except discord.HTTPException:
        pass


async def _award_gg(db, guild: discord.Guild, member: discord.Member, amount: int):
    if amount <= 0:
        return
    before = await db.get_member(guild.id, member.id)
    old_level = levels.get_level(before["gg"])
    new_total = await db.add_gg(guild.id, member.id, amount)
    new_level = levels.get_level(new_total)
    if new_level > old_level:
        await _announce_levelup(db, guild, member, old_level, new_level)


class ProfileModal(discord.ui.Modal, title="Update Your Library Card"):
    def __init__(self, db, current_bio: str, current_genres: str, current_books: str):
        super().__init__()
        self.db = db
        self.summary = discord.ui.TextInput(
            label="Patron Summary",
            style=discord.TextStyle.paragraph,
            placeholder="Let them type something about themself...",
            default=current_bio,
            max_length=300,
            required=False,
        )
        self.genres = discord.ui.TextInput(
            label="Favorite Genres",
            style=discord.TextStyle.short,
            placeholder="Let them type their favorite genres...",
            default=current_genres,
            max_length=150,
            required=False,
        )
        self.books = discord.ui.TextInput(
            label="Books Checked Out",
            style=discord.TextStyle.short,
            placeholder="Let them list what they've got checked out...",
            default=current_books,
            max_length=150,
            required=False,
        )
        self.add_item(self.summary)
        self.add_item(self.genres)
        self.add_item(self.books)

    async def on_submit(self, interaction: discord.Interaction):
        await self.db.set_profile_text(
            interaction.guild.id, interaction.user.id,
            str(self.summary.value), str(self.genres.value), str(self.books.value)
        )
        await interaction.response.send_message("✅ Library card updated.")


class LibraryCardPicker(discord.ui.View):
    def __init__(self, db, guild_id: int, user_id: int, available: list):
        super().__init__(timeout=120)
        self.db = db
        self.guild_id = guild_id
        self.user_id = user_id
        self.available = available
        self.index = 0
        self.message: discord.Message = None

    def embed(self) -> discord.Embed:
        card = self.available[self.index]
        embed = discord.Embed(
            title=card["name"],
            description=f"Card {self.index + 1} of {len(self.available)}",
            color=discord.Color.gold()
        )
        embed.set_image(url=card["image_url"])
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your library card picker.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.available)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="✅ Select", style=discord.ButtonStyle.success)
    async def select(self, interaction: discord.Interaction, button: discord.ui.Button):
        card = self.available[self.index]
        await self.db.set_member_card(self.guild_id, self.user_id, card["id"])
        for child in self.children:
            child.disabled = True
        embed = self.embed()
        embed.color = discord.Color.green()
        embed.set_footer(text=f"✅ Library card set to {card['name']}")
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.available)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="📝 Edit Card Details", style=discord.ButtonStyle.primary)
    async def edit_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats = await self.db.get_member(self.guild_id, self.user_id)
        modal = ProfileModal(
            self.db, stats.get("bio") or "", stats.get("favorite_genres") or "",
            stats.get("books_checked_out") or ""
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑️ Remove Card", style=discord.ButtonStyle.danger)
    async def remove_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.db.clear_member_card(self.guild_id, self.user_id)
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title="Library card removed",
            description="Back to the default look.",
            color=discord.Color.greyple()
        )
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


class LibraryCardGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="library-card", description="View or update a library card")
        self.db = db

    @app_commands.command(name="view", description="Show a patron's library card.")
    @app_commands.describe(member="Whose card to view (defaults to you)")
    async def view(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        await interaction.response.defer()
        stats = await self.db.get_member(interaction.guild.id, target.id)
        placement = await self.db.get_rank(interaction.guild.id, target.id)
        level = levels.get_level(stats["gg"])
        display_name = cards.sanitize_name(target.display_name, target.name)
        avatar_bytes = await target.display_avatar.read()
        background_bytes, accent_color = await _get_card_visuals(self.db, interaction.guild.id, stats)
        member_since = target.joined_at.strftime("%B %d, %Y") if target.joined_at else "Unknown"
        buf = cards.render_library_card(
            display_name, avatar_bytes, level, placement, stats["gg"], member_since,
            stats.get("bio"), stats.get("favorite_genres"), stats.get("books_checked_out"),
            birthday=None,  # not implemented yet — always hidden until that feature lands
            background_bytes=background_bytes, accent_color=accent_color
        )
        await interaction.followup.send(file=discord.File(buf, filename="library-card.png"))

    @app_commands.command(
        name="update",
        description="Browse/choose your library card design and edit its bio, genres, and checked-out books."
    )
    async def update(self, interaction: discord.Interaction):
        available = await self.db.get_library_cards(interaction.guild.id)
        if not available:
            await interaction.response.send_message("No library cards have been added yet.", ephemeral=True)
            return
        view = LibraryCardPicker(self.db, interaction.guild.id, interaction.user.id, available)
        await interaction.response.send_message(embed=view.embed(), view=view)
        view.message = await interaction.original_response()


class PatronGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="patron", description="Goblin Gold & library card commands")
        self.db = db
        self.add_command(LibraryCardGroup(db))

    @app_commands.command(name="board", description="Show the Goblin Gold leaderboard.")
    async def board(self, interaction: discord.Interaction):
        rows = await self.db.get_leaderboard(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("No patrons have earned any Goblin Gold yet.")
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"<@{row['user_id']}>"
            lines.append(f"**{i}.** {name} — {row['gg']} GG")
        embed = discord.Embed(
            title="📚 Goblin Gold Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rank", description="Show a patron's Goblin Gold rank card.")
    @app_commands.describe(member="Whose rank to check (defaults to you)")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        await interaction.response.defer()
        stats = await self.db.get_member(interaction.guild.id, target.id)
        placement = await self.db.get_rank(interaction.guild.id, target.id)
        level, gg_into_level, gg_needed = levels.get_progress(stats["gg"])
        avatar_bytes = await target.display_avatar.read()
        display_name = cards.sanitize_name(target.display_name, target.name)
        background_bytes, accent_color = await _get_card_visuals(self.db, interaction.guild.id, stats)
        buf = cards.render_rank_card(
            display_name, avatar_bytes, level, placement,
            stats["gg"], gg_into_level, gg_needed, background_bytes, accent_color
        )
        embed = discord.Embed(color=discord.Color.gold())
        embed.set_image(url="attachment://rank.png")
        embed.set_footer(text=TIP_FOOTER)
        view = None
        if TIP_URL:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="🧌 Tip Your Favorite Goblin", style=discord.ButtonStyle.link, url=TIP_URL
            ))
        await interaction.followup.send(embed=embed, file=discord.File(buf, filename="rank.png"), view=view)


class AddGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="add", description="Add GG or a new library card")
        self.db = db

    @app_commands.command(name="member", description="Give Goblin Gold to a member.")
    @app_commands.describe(member="Who to give GG to", amount="How much GG to give")
    async def member(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
        await _award_gg(self.db, interaction.guild, member, amount)
        await interaction.response.send_message(f"✅ Gave **{amount} GG** to {member.mention}.")

    @app_commands.command(name="library-card", description="Add a new library card design to the catalog.")
    @app_commands.describe(
        name="Name for this card",
        image="The card artwork",
        accent_color="Hex accent color for this card, e.g. #c9ad6a (optional — defaults to standard gold)"
    )
    async def library_card(self, interaction: discord.Interaction, name: str, image: discord.Attachment,
                            accent_color: str = None):
        if accent_color and cards.parse_hex_color(accent_color) is None:
            await interaction.response.send_message(
                "❌ That doesn't look like a hex color. Use a format like `#c9ad6a`.", ephemeral=True
            )
            return

        await interaction.response.defer()
        config = await self.db.get_guild_config(interaction.guild.id)
        assets_channel_id = config.get("assets_channel_id")
        image_url = image.url
        archived = False
        if assets_channel_id:
            channel = interaction.guild.get_channel(assets_channel_id)
            if channel:
                try:
                    data = await image.read()
                    msg = await channel.send(
                        content=f"📇 **{name}** — added by {interaction.user.mention}",
                        file=discord.File(io.BytesIO(data), filename=image.filename)
                    )
                    if msg.attachments:
                        image_url = msg.attachments[0].url
                        archived = True
                except discord.HTTPException:
                    pass

        stored_color = accent_color.strip().lstrip("#") if accent_color else None
        await self.db.add_library_card(interaction.guild.id, name, image_url, interaction.user.id, stored_color)
        note = "" if archived else (
            "\n⚠️ No assets channel set, so this uses the original upload link, which may not stay "
            "valid long-term. Set one with `/gg channelset assets`."
        )
        await interaction.followup.send(f"✅ Added library card **{name}** to the catalog.{note}")


class SetRateGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="setrate", description="Configure passive Goblin Gold earn rates")
        self.db = db

    @app_commands.command(name="message", description="Set GG earned per message and its cooldown.")
    @app_commands.describe(
        amount="GG awarded per eligible message",
        cooldown="Cooldown in seconds before a member can earn again"
    )
    async def message(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0],
        cooldown: app_commands.Range[int, 0]
    ):
        await self.db.set_message_rate(interaction.guild.id, amount, cooldown)
        await interaction.response.send_message(
            f"✅ Messages now earn **{amount} GG**, once per **{cooldown}s**."
        )

    @app_commands.command(name="voice", description="Set GG earned per interval in voice (prorated live).")
    @app_commands.describe(
        amount="GG awarded per interval",
        per="Interval in seconds (default 3600 = 1 hour)"
    )
    async def voice(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0],
        per: app_commands.Range[int, 60] = 3600
    ):
        await self.db.set_voice_rate(interaction.guild.id, amount, per)
        await interaction.response.send_message(
            f"✅ Voice now earns **{amount} GG** per **{per}s**, prorated while connected."
        )


class ChannelSetGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="channelset", description="Configure channels Griffin posts to")
        self.db = db

    @app_commands.command(name="levelup", description="Set the channel for level-up announcements.")
    @app_commands.describe(channel="Channel to post level-up cards in")
    async def levelup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_levelup_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"✅ Level-up announcements will post in {channel.mention}.")

    @app_commands.command(name="assets", description="Set the channel uploaded card art gets archived to.")
    @app_commands.describe(channel="Channel to repost card art in for stable, permanent links")
    async def assets(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_assets_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"✅ Card art will be archived in {channel.mention}.")


class GGGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(
            name="gg",
            description="Goblin Gold moderation tools",
            default_permissions=discord.Permissions(manage_guild=True)
        )
        self.db = db
        self.add_command(AddGroup(db))
        self.add_command(SetRateGroup(db))
        self.add_command(ChannelSetGroup(db))

    @app_commands.command(name="remove", description="Remove Goblin Gold from a member.")
    @app_commands.describe(member="Who to remove GG from", amount="How much GG to remove")
    async def remove(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
        await self.db.remove_gg(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(f"✅ Removed **{amount} GG** from {member.mention}.")


class Economy(commands.Cog):
    """Passive GG earning (messages + voice). Slash commands live in PatronGroup/GGGroup, registered in setup()."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._message_cooldowns: dict[tuple[int, int], float] = {}
        self.voice_tick.start()

    def cog_unload(self):
        self.voice_tick.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        key = (message.guild.id, message.author.id)
        config = await self.db.get_guild_config(message.guild.id)
        now = time.monotonic()
        last = self._message_cooldowns.get(key)
        if last is not None and now - last < config["message_cooldown"]:
            return
        self._message_cooldowns[key] = now
        await _award_gg(self.db, message.guild, message.author, config["message_rate"])

    @tasks.loop(minutes=VOICE_TICK_MINUTES)
    async def voice_tick(self):
        elapsed_seconds = VOICE_TICK_MINUTES * 60
        for guild in self.bot.guilds:
            config = await self.db.get_guild_config(guild.id)
            amount = round(config["voice_rate"] * elapsed_seconds / config["voice_interval"])
            if amount <= 0:
                continue
            for channel in guild.voice_channels:
                if channel == guild.afk_channel:
                    continue
                for member in channel.members:
                    if member.bot:
                        continue
                    if member.voice and member.voice.self_deaf:
                        continue
                    await _award_gg(self.db, guild, member, amount)

    @voice_tick.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()


@app_commands.command(name="help", description="Show what Griffin's commands can do.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Patron Commands",
        color=discord.Color.gold(),
        description="\n".join([
            "**/patron board** — Show the Goblin Gold leaderboard",
            "**/patron rank** `[member]` — Show a patron's Goblin Gold rank card",
            "**/patron library-card view** `[member]` — Show a patron's library card",
            "**/patron library-card update** — Browse/choose your library card design "
            "and edit its bio, genres, and checked-out books",
        ])
    )
    await interaction.response.send_message(embed=embed)


class ModGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="mod",
            description="Mod-only help",
            default_permissions=discord.Permissions(manage_guild=True)
        )

    @app_commands.command(name="help", description="Show what Griffin's mod commands can do.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛠️ Mod Commands",
            color=discord.Color.orange(),
            description="\n".join([
                "**/gg add member** `<member> <amount>` — Give Goblin Gold to a member",
                "**/gg add library-card** `<name> <image> [accent_color]` — Add a new "
                "library card design to the catalog",
                "**/gg remove** `<member> <amount>` — Remove Goblin Gold from a member",
                "**/gg setrate message** `<amount> <cooldown>` — Set GG earned per message "
                "and its cooldown",
                "**/gg setrate voice** `<amount> <per>` — Set GG earned per interval in "
                "voice, prorated while connected",
                "**/gg channelset levelup** `<channel>` — Set the channel for level-up "
                "announcements",
                "**/gg channelset assets** `<channel>` — Set the channel uploaded card art "
                "gets archived to",
            ])
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
    bot.tree.add_command(PatronGroup(bot.db))
    bot.tree.add_command(GGGroup(bot.db))
    bot.tree.add_command(help_command)
    bot.tree.add_command(ModGroup())
