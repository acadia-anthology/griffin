import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

VOICE_TICK_MINUTES = 5


class PatronGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(name="patron", description="Goblin Gold & library card commands")
        self.db = db

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
            lines.append(f"**{i}.** {name} — {row['gg_earned']} GG")
        embed = discord.Embed(
            title="📚 Goblin Gold Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rank", description="Show a patron's Goblin Gold rank.")
    @app_commands.describe(member="Whose rank to check (defaults to you)")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        stats = await self.db.get_member(interaction.guild.id, target.id)
        placement = await self.db.get_rank(interaction.guild.id, target.id)
        embed = discord.Embed(
            title=f"{target.display_name}'s Library Record",
            color=discord.Color.gold()
        )
        embed.add_field(name="Rank", value=f"#{placement}" if placement else "Unranked", inline=True)
        embed.add_field(name="Goblin Gold Earned", value=str(stats["gg_earned"]), inline=True)
        embed.add_field(name="Spendable Balance", value=str(stats["gg_balance"]), inline=True)
        await interaction.response.send_message(embed=embed)

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
        embed.add_field(name="Goblin Gold", value=str(stats["gg_balance"]))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="library-card-shop", description="Browse library card backgrounds.")
    async def library_card_shop(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🖼️ Library Card Shop",
            description="The shop is still being stocked — check back soon!",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)


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


class GGGroup(app_commands.Group):
    def __init__(self, db):
        super().__init__(
            name="gg",
            description="Goblin Gold moderation tools",
            default_permissions=discord.Permissions(manage_guild=True)
        )
        self.db = db
        self.add_command(SetRateGroup(db))

    @app_commands.command(name="add", description="Give Goblin Gold to a member.")
    @app_commands.describe(member="Who to give GG to", amount="How much GG to give")
    async def add(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
        await self.db.add_gg(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(f"✅ Gave **{amount} GG** to {member.mention}.")

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
        if config["message_rate"] > 0:
            await self.db.add_gg(message.guild.id, message.author.id, config["message_rate"])

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
                    await self.db.add_gg(guild.id, member.id, amount)

    @voice_tick.before_loop
    async def before_voice_tick(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
    bot.tree.add_command(PatronGroup(bot.db))
    bot.tree.add_command(GGGroup(bot.db))
