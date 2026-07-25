import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import cards, levels

VOICE_TICK_MINUTES = 5


async def _fetch_bytes(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None


async def _get_background(db, guild_id: int, member_row: dict) -> Optional[bytes]:
    card_id = member_row.get("card_id")
    if not card_id:
        return None
    card = await db.get_card(card_id)
    if not card:
        return None
    return await _fetch_bytes(card["image_url"])


async def _announce_levelup(db, guild: discord.Guild, member: discord.Member, old_level: int, new_level: int):
    config = await db.get_guild_config(guild.id)
    channel_id = config.get("levelup_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    member_row = await db.get_member(guild.id, member.id)
    background_bytes = await _get_background(db, guild.id, member_row)
    avatar_bytes = await member.display_avatar.read()
    buf = cards.render_levelup_card(
        member.display_name, avatar_bytes, old_level, new_level, background_bytes
    )
    try:
        await channel.send(
            content=f"🎉 {member.mention} just reached **Level {new_level}**!",
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


class UpdateGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="update", description="Update your own profile")
        self.db = db

    async def card_autocomplete(self, interaction: discord.Interaction, current: str):
        available = await self.db.get_library_cards(interaction.guild.id)
        current = current.lower()
        matches = [c for c in available if current in c["name"].lower()]
        return [
            app_commands.Choice(name=c["name"], value=str(c["id"]))
            for c in matches[:25]
        ]

    @app_commands.command(name="library-card", description="Choose your library card design.")
    @app_commands.describe(card="Which card to use")
    @app_commands.autocomplete(card=card_autocomplete)
    async def library_card(self, interaction: discord.Interaction, card: str):
        card_row = await self.db.get_card(int(card))
        if card_row is None:
            await interaction.response.send_message("❌ That card doesn't exist anymore. Pick another.", ephemeral=True)
            return
        await self.db.set_member_card(interaction.guild.id, interaction.user.id, card_row["id"])
        await interaction.response.send_message(f"✅ Library card set to **{card_row['name']}**.")


class PatronGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="patron", description="Goblin Gold & library card commands")
        self.db = db
        self.add_command(UpdateGroup(db))

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
        background_bytes = await _get_background(self.db, interaction.guild.id, stats)
        buf = cards.render_rank_card(
            target.display_name, avatar_bytes, level, placement,
            gg_into_level, gg_needed, background_bytes
        )
        await interaction.followup.send(file=discord.File(buf, filename="rank.png"))

    @app_commands.command(name="library-card", description="Show a patron's library card.")
    @app_commands.describe(member="Whose card to view (defaults to you)")
    async def library_card(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        stats = await self.db.get_member(interaction.guild.id, target.id)
        embed = discord.Embed(
            title=f"{target.display_name}'s Library Card",
            description="🚧 Placeholder card — full design coming soon.",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Goblin Gold", value=str(stats["gg"]))
        if stats["card_id"]:
            card = await self.db.get_card(stats["card_id"])
            if card:
                embed.set_image(url=card["image_url"])
                embed.add_field(name="Card", value=card["name"])
        await interaction.response.send_message(embed=embed)


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
    @app_commands.describe(name="Name for this card", image="The card artwork")
    async def library_card(self, interaction: discord.Interaction, name: str, image: discord.Attachment):
        await self.db.add_library_card(interaction.guild.id, name, image.url, interaction.user.id)
        await interaction.response.send_message(f"✅ Added library card **{name}** to the catalog.")


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


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
    bot.tree.add_command(PatronGroup(bot.db))
    bot.tree.add_command(GGGroup(bot.db))
