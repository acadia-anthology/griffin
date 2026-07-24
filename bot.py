import discord
from discord.ext import commands
import asyncio
import os
from dotenv import load_dotenv
from utils.database import Database

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class Griffin(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # fallback prefix, mainly using slash commands
            intents=intents,
            help_command=None
        )
        self.db = Database()
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        if isinstance(error, discord.app_commands.CheckFailure):
            msg = "❌ You don't have permission to use this command."
        else:
            msg = "❌ Something went wrong running that command."
            print(f"App command error in /{interaction.command.qualified_name if interaction.command else '?'}: {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def setup_hook(self):
        await self.db.initialize()
        cogs = [
            "cogs.core",
        ]
        for cog in cogs:
            await self.load_extension(cog)
            print(f"✅ Loaded {cog}")
        await self.tree.sync()
        print("✅ Slash commands synced")

    async def on_ready(self):
        print(f"✨ Griffin is online as {self.user} (ID: {self.user.id})", flush=True)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="the stacks 📚"
            )
        )


bot = Griffin()

if __name__ == "__main__":
    asyncio.run(bot.start(os.getenv("DISCORD_TOKEN")))
