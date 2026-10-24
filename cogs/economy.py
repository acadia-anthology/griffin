import asyncio
import io
import json
import re
import time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import cards, levels
from utils.emoji_images import compress_emoji
from utils.emojis import _FALLBACKS, emoji as _emoji

VOICE_TICK_MINUTES = 5

# Rank card tip button — TIP_URL is None until a real Ko-fi/PayPal link
# exists, at which point the button just starts appearing automatically.
TIP_URL = "https://ko-fi.com/atmospherehq"


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


async def _get_curve(db, guild_id: int):
    """Merges a guild's edited level-1-30 thresholds and flat rate (31+) with the defaults."""
    config = await db.get_guild_config(guild_id)
    thresholds = dict(levels.LEVEL_THRESHOLDS)
    raw = config.get("level_thresholds")
    if raw:
        try:
            thresholds.update({int(k): v for k, v in json.loads(raw).items()})
        except (ValueError, TypeError):
            pass
    flat_rate = config.get("level_flat_rate") or levels.FLAT_RATE
    return thresholds, flat_rate


async def _get_tier_display(db, guild: discord.Guild, level: int) -> str:
    """The tier's display name for this level — a configured role's live name if set,
    else the built-in default tier name."""
    lo, _hi = levels.get_tier_range(level)
    tier_roles = await db.get_tier_roles(guild.id)
    role_id = tier_roles.get(lo)
    role = guild.get_role(role_id) if role_id else None
    return role.name if role else levels.get_tier_name(level)


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
    thresholds, flat_rate = await _get_curve(db, guild.id)
    before = await db.get_member(guild.id, member.id)
    old_level = levels.get_level(before["gg"], thresholds, flat_rate)
    new_total = await db.add_gg(guild.id, member.id, amount)
    new_level = levels.get_level(new_total, thresholds, flat_rate)
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
        thresholds, flat_rate = await _get_curve(self.db, interaction.guild.id)
        level = levels.get_level(stats["gg"], thresholds, flat_rate)
        tier_name = await _get_tier_display(self.db, interaction.guild, level)
        display_name = cards.sanitize_name(target.display_name, target.name)
        avatar_bytes = await target.display_avatar.read()
        background_bytes, accent_color = await _get_card_visuals(self.db, interaction.guild.id, stats)
        member_since = target.joined_at.strftime("%B %d, %Y") if target.joined_at else "Unknown"
        buf = cards.render_library_card(
            display_name, avatar_bytes, level, placement, stats["gg"], member_since,
            stats.get("bio"), stats.get("favorite_genres"), stats.get("books_checked_out"),
            birthday=None,  # not implemented yet — always hidden until that feature lands
            background_bytes=background_bytes, accent_color=accent_color, tier_name=tier_name
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
        thresholds, flat_rate = await _get_curve(self.db, interaction.guild.id)
        level, gg_into_level, gg_needed = levels.get_progress(stats["gg"], thresholds, flat_rate)
        tier_name = await _get_tier_display(self.db, interaction.guild, level)
        avatar_bytes = await target.display_avatar.read()
        display_name = cards.sanitize_name(target.display_name, target.name)
        background_bytes, accent_color = await _get_card_visuals(self.db, interaction.guild.id, stats)
        buf = cards.render_rank_card(
            display_name, avatar_bytes, level, placement,
            stats["gg"], gg_into_level, gg_needed, background_bytes, accent_color, tier_name=tier_name
        )
        view = None
        if TIP_URL:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="🧌 Tip Griffins Goblin King",
                style=discord.ButtonStyle.link, url=TIP_URL
            ))
        await interaction.followup.send(file=discord.File(buf, filename="rank.png"), view=view)


class GriffinModGroup(app_commands.Group):
    """Base for Griffin's mod-only command groups. Lets a configured
    griffin_manager_role use these commands without real Manage Server
    permission — every group in a nested chain needs this check, since
    discord.py only consults a command's *immediate* parent group."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        db = getattr(self, "db", None)
        if db is None:
            return False
        config = await db.get_guild_config(interaction.guild.id)
        manager_role_id = config.get("griffin_manager_role_id")
        return bool(manager_role_id) and any(
            r.id == manager_role_id for r in getattr(interaction.user, "roles", [])
        )


class AddGroup(GriffinModGroup):
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
            "valid long-term. Set one with `/staff channelset assets`."
        )
        await interaction.followup.send(f"✅ Added library card **{name}** to the catalog.{note}")


class SetRateGroup(GriffinModGroup):
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

    @app_commands.command(name="sprint", description="Set GG earned per completed sprint (0 turns it off).")
    @app_commands.describe(amount="GG awarded per completed sprint — 0 stops sprints from awarding GG")
    async def sprint(self, interaction: discord.Interaction, amount: app_commands.Range[int, 0]):
        await self.db.set_sprint_gg(interaction.guild.id, amount)
        if amount == 0:
            await interaction.response.send_message("✅ Sprints no longer award GG.")
        else:
            await interaction.response.send_message(f"✅ Completed sprints now earn **{amount} GG**.")


class LevelsEditModal(discord.ui.Modal, title="Edit Level Curve"):
    level_input = discord.ui.TextInput(
        label="Level to edit (1-30)", placeholder="e.g. 12", required=False
    )
    gg_input = discord.ui.TextInput(
        label="GG required for that level", placeholder="e.g. 29000", required=False
    )
    flat_rate_input = discord.ui.TextInput(
        label="New flat rate for levels 31-100", placeholder="e.g. 25000", required=False
    )

    def __init__(self, db):
        super().__init__()
        self.db = db

    async def on_submit(self, interaction: discord.Interaction):
        level_raw = self.level_input.value.strip()
        gg_raw = self.gg_input.value.strip()
        flat_raw = self.flat_rate_input.value.strip()

        if bool(level_raw) != bool(gg_raw):
            await interaction.response.send_message(
                "❌ To set a level's threshold, fill in both the level *and* the GG required.",
                ephemeral=True
            )
            return
        if not level_raw and not flat_raw:
            await interaction.response.send_message(
                "❌ Nothing to update — fill in a level + GG, a flat rate, or both.", ephemeral=True
            )
            return

        confirmations = []

        if level_raw:
            try:
                level = int(level_raw)
                gg_required = int(gg_raw)
            except ValueError:
                await interaction.response.send_message(
                    "❌ Level and GG required must both be whole numbers.", ephemeral=True
                )
                return
            if not 1 <= level <= 30:
                await interaction.response.send_message(
                    "❌ Level must be between 1 and 30.", ephemeral=True
                )
                return
            await self.db.set_level_threshold(interaction.guild.id, level, gg_required)
            confirmations.append(f"Level {level} now requires **{gg_required:,} GG**.")

        if flat_raw:
            try:
                flat_rate = int(flat_raw)
            except ValueError:
                await interaction.response.send_message(
                    "❌ Flat rate must be a whole number.", ephemeral=True
                )
                return
            await self.db.set_level_flat_rate(interaction.guild.id, flat_rate)
            confirmations.append(f"Levels 31-100 now cost **{flat_rate:,} GG** each.")

        await interaction.response.send_message("✅ " + " ".join(confirmations))


class LevelsGroup(GriffinModGroup):
    def __init__(self, db):
        super().__init__(name="levels", description="View and adjust Griffin's level/tier ladder")
        self.db = db

    @app_commands.command(name="view", description="Show the full level/tier ladder and GG requirements.")
    async def view(self, interaction: discord.Interaction):
        thresholds, flat_rate = await _get_curve(self.db, interaction.guild.id)
        tier_roles = await self.db.get_tier_roles(interaction.guild.id)
        lines = []
        for lo, hi, default_name in levels.TIER_NAMES:
            role_id = tier_roles.get(lo)
            role = interaction.guild.get_role(role_id) if role_id else None
            name = role.mention if role else default_name
            gg_required = levels.cumulative_for_level(lo, thresholds, flat_rate)
            range_text = f"{lo}+" if lo == levels.MAX_LEVEL else f"{lo}-{hi}"
            lines.append(f"**{name}** (Lvl {range_text}) — {gg_required:,} GG")
        embed = discord.Embed(
            title="📈 Level & Tier Ladder", description="\n".join(lines), color=discord.Color.gold()
        )
        embed.set_footer(text=f"Flat rate after level {levels.FLAT_START_LEVEL}: {flat_rate:,} GG/level")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="edit",
        description="Edit a level's GG threshold (1-30) and/or the flat rate for levels 31-100."
    )
    async def edit(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LevelsEditModal(self.db))

    @app_commands.command(name="set-tier", description="Set (or clear) the role tied to a tier.")
    @app_commands.describe(tier="Which tier to edit", role="Role to represent this tier — omit to clear")
    @app_commands.choices(tier=[
        app_commands.Choice(name=f"{name} ({lo}-{hi})" if lo != 100 else f"{name} (100+)", value=lo)
        for lo, hi, name in levels.TIER_NAMES
    ])
    async def set_tier(
        self, interaction: discord.Interaction,
        tier: app_commands.Choice[int], role: discord.Role = None
    ):
        await self.db.set_tier_role(interaction.guild.id, tier.value, role.id if role else None)
        if role:
            await interaction.response.send_message(f"✅ **{tier.name}** is now tied to {role.mention}.")
        else:
            await interaction.response.send_message(f"✅ **{tier.name}** cleared — back to the default name.")


_CHANNEL_REF = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)|<#(\d+)>|(\d{15,25})")


async def _resolve_channel_ref(guild: discord.Guild, raw: str):
    """A pasted channel / forum / thread ID, link or #mention -> the channel object, or None."""
    m = _CHANNEL_REF.search(raw or "")
    if not m:
        return None
    cid = int(next(g for g in m.groups() if g))
    channel = guild.get_channel_or_thread(cid)
    if channel is None:
        try:
            channel = await guild.fetch_channel(cid)
        except discord.HTTPException:
            return None
    return None if isinstance(channel, discord.CategoryChannel) else channel


class ChannelSetGroup(GriffinModGroup):
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

    @app_commands.command(
        name="sprint", description="Choose where sprints can run: a channel, forum or thread (optional)."
    )
    @app_commands.describe(channel="Paste a channel, forum or thread ID/link (blank = allow anywhere)")
    async def sprint(self, interaction: discord.Interaction, channel: str = None):
        if not (channel or "").strip():
            await self.db.set_sprint_channel(interaction.guild.id, None)
            await interaction.response.send_message("✅ Sprints can now be started in any channel.")
            return
        target = await _resolve_channel_ref(interaction.guild, channel)
        if target is None:
            await interaction.response.send_message(
                "❌ I couldn't find that. Paste a channel, forum or thread **ID**, a message/channel **link**, or "
                "a #mention (in Discord: right-click it → Copy ID, with Developer Mode on).", ephemeral=True
            )
            return
        await self.db.set_sprint_channel(interaction.guild.id, target.id)
        if isinstance(target, discord.ForumChannel):
            where = f"in any post in the {target.mention} forum"
        elif isinstance(target, discord.Thread):
            where = f"only in the thread {target.mention}"
        else:
            where = f"only in {target.mention} (and threads inside it)"
        await interaction.response.send_message(f"✅ Sprints can now be started and scheduled {where}.")


class RemoveEmojiView(discord.ui.View):
    def __init__(self, owner_id: int, emoji: discord.Emoji, registry: dict, used_by_griffin: bool):
        super().__init__(timeout=60)
        self.owner_id, self.emoji, self.registry, self.used = owner_id, emoji, registry, used_by_griffin

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, remove it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.emoji.delete()
        except discord.HTTPException as e:
            await interaction.response.edit_message(content=f"❌ Discord wouldn't remove it: {e}", view=None)
            return
        self.registry.pop(self.emoji.name, None)
        note = (" Griffin will use a basic emoji in its place until you upload one with the same name."
                if self.used else "")
        await interaction.response.edit_message(content=f"🗑️ Removed `{self.emoji.name}`.{note}", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Kept it.", view=None)
        self.stop()


class EmojiGroup(GriffinModGroup):
    """Upload / remove the emojis Griffin itself owns (they're shared everywhere Griffin is)."""
    MAX_BYTES = 256 * 1024          # Discord's limit; bigger uploads are shrunk automatically
    MAX_INPUT_BYTES = 8 * 1024 * 1024

    def __init__(self, db):
        super().__init__(name="emoji", description="Upload or remove the emojis Griffin uses")
        self.db = db

    @app_commands.command(name="add", description="Upload a new emoji for Griffin to use.")
    @app_commands.describe(
        name="Letters, numbers and underscores. Use a name Griffin already expects to give it a new look",
        image="PNG, JPG, GIF or WebP; big files are shrunk to fit automatically",
    )
    async def add(self, interaction: discord.Interaction, name: str, image: discord.Attachment):
        registry = interaction.client.app_emojis
        name = name.strip().strip(":")
        if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", name):
            await interaction.response.send_message(
                "❌ The name needs 2–32 letters, numbers or underscores (no spaces).", ephemeral=True)
            return
        if name in registry:
            await interaction.response.send_message(
                f"❌ There's already an emoji called `{name}`. Remove it first with `/staff emoji remove`, "
                f"or pick another name.", ephemeral=True)
            return
        ctype = (image.content_type or "").split(";")[0]
        if ctype not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            await interaction.response.send_message("❌ That needs to be a PNG, JPG, GIF or WebP.", ephemeral=True)
            return
        if image.size > self.MAX_INPUT_BYTES:
            await interaction.response.send_message(
                f"❌ That file is {image.size / 1024 / 1024:.1f} MB — too big even to shrink. Try one under 8 MB.",
                ephemeral=True)
            return
        await interaction.response.defer()
        try:
            data = await image.read()
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ I couldn't download that file: {e}", ephemeral=True)
            return
        shrunk = ""
        if len(data) > self.MAX_BYTES or ctype == "image/webp":      # Discord takes PNG/JPG/GIF up to 256 KB
            before = len(data)
            try:
                data, _ = await asyncio.to_thread(compress_emoji, data)    # off the event loop: GIFs take a moment
            except ValueError as e:
                await interaction.followup.send(f"❌ I couldn't shrink that: {e}.", ephemeral=True)
                return
            if before > self.MAX_BYTES:
                shrunk = f" Shrunk from {before // 1024} KB to {max(1, len(data) // 1024)} KB."
        try:
            emoji = await interaction.client.create_application_emoji(name=name, image=data)
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Discord wouldn't take that emoji: {e}", ephemeral=True)
            return
        registry[emoji.name] = emoji
        note = "" if emoji.name in _FALLBACKS else " (Griffin doesn't use this name yet; it's stored for later.)"
        await interaction.followup.send(f"✅ Added {emoji} as `{emoji.name}`.{shrunk}{note}")

    @app_commands.command(name="remove", description="Remove one of Griffin's emojis.")
    @app_commands.describe(name="Which emoji to remove")
    async def remove(self, interaction: discord.Interaction, name: str):
        registry = interaction.client.app_emojis
        emoji = registry.get(name.strip().strip(":"))
        if emoji is None:
            await interaction.response.send_message(
                f"❌ I don't have an emoji called `{name}`. Use `/staff emoji list` to see them.", ephemeral=True)
            return
        used = emoji.name in _FALLBACKS
        warn = (" Griffin uses this one in its sprint messages, so it'll fall back to a basic emoji until you "
                "upload a replacement with the same name.") if used else ""
        await interaction.response.send_message(
            f"Remove {emoji} `{emoji.name}` for good?{warn}",
            view=RemoveEmojiView(interaction.user.id, emoji, registry, used), ephemeral=True)

    @remove.autocomplete("name")
    async def _remove_autocomplete(self, interaction: discord.Interaction, current: str):
        names = sorted(interaction.client.app_emojis)
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    @app_commands.command(name="list", description="See Griffin's emojis and which ones it's missing.")
    async def list_(self, interaction: discord.Interaction):
        registry = interaction.client.app_emojis
        lines = [f"{e} `{n}`" + (" — used by Griffin" if n in _FALLBACKS else "")
                 for n, e in sorted(registry.items())]
        missing = [n for n in _FALLBACKS if n not in registry]
        text = "\n".join(lines) or "No emojis uploaded yet."
        if len(text) > 3500:
            text = text[:3500].rsplit("\n", 1)[0] + "\n…and more"
        if missing:
            text += ("\n\n**Griffin expects these but doesn't have them** (using a basic emoji): "
                     + ", ".join(f"`{n}`" for n in missing))
        await interaction.response.send_message(
            embed=discord.Embed(title=f"Griffin's emojis ({len(registry)})", description=text,
                                color=discord.Color(0x26878D)), ephemeral=True)


class RaceConfigGroup(GriffinModGroup):
    def __init__(self, db):
        super().__init__(name="race", description="Configure sprint/race settings")
        self.db = db

    @app_commands.command(name="ping-role", description="Set the role pinged when a sprint/race starts.")
    @app_commands.describe(role="Role to ping for sprints (e.g. Page Racers)")
    async def ping_role(self, interaction: discord.Interaction, role: discord.Role):
        await self.db.set_sprint_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"✅ Sprints will now ping {role.mention}.")

    @app_commands.command(
        name="librarian-role",
        description="Set who can manage sprints (and who is pinged with results, if you turn that on)."
    )
    @app_commands.describe(role="Role that can create/manage sprints")
    async def librarian_role(self, interaction: discord.Interaction, role: discord.Role):
        await self.db.set_sprint_manager_role(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"✅ {role.mention} can now manage sprints. It is only pinged with results while "
            f"`/staff race librarian-ping` is on."
        )

    @app_commands.command(
        name="librarian-ping",
        description="Turn the librarian-role ping on sprint results on or off (e.g. on for readathons)."
    )
    @app_commands.describe(setting="On = ping the librarian role with every results board; off = never")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on", value=1),
        app_commands.Choice(name="off", value=0),
    ])
    async def librarian_ping(self, interaction: discord.Interaction, setting: app_commands.Choice[int]):
        await self.db.set_sprint_librarian_ping(interaction.guild.id, bool(setting.value))
        cfg = await self.db.get_guild_config(interaction.guild.id)
        role_id = cfg.get("sprint_manager_role_id")
        if setting.value and not role_id:
            note = " Set a role first with `/staff race librarian-role`, or there's no one to ping."
        else:
            note = ""
        await interaction.response.send_message(
            f"✅ The librarian role will {'now' if setting.value else 'no longer'} be pinged with sprint "
            f"results.{note}"
        )

    @app_commands.command(
        name="timezone",
        description="Server fallback timezone for scheduled sprint times (people can set their own)."
    )
    @app_commands.describe(timezone="IANA timezone name, e.g. America/New_York or Europe/London")
    async def timezone(self, interaction: discord.Interaction, timezone: str):
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            await interaction.response.send_message(
                f"❌ `{timezone}` isn't a recognized timezone. Use an IANA name like "
                f"`America/New_York` or `Europe/London`.",
                ephemeral=True
            )
            return
        await self.db.set_sprint_timezone(interaction.guild.id, timezone)
        await interaction.response.send_message(
            f"✅ Scheduled sprint times will be interpreted as **{timezone}**."
        )


class GGGroup(GriffinModGroup):
    def __init__(self, db):
        super().__init__(name="gg", description="Goblin Gold moderation tools")
        self.db = db
        self.add_command(AddGroup(db))
        self.add_command(SetRateGroup(db))
        self.add_command(LevelsGroup(db))

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
        title=f"{_emoji(interaction.client, 'books')} Patron Commands",
        color=discord.Color.gold(),
        description="\n".join([
            "**/patron board** — Show the Goblin Gold leaderboard",
            "**/patron rank** `[member]` — Show a patron's Goblin Gold rank card",
            "**/patron library-card view** `[member]` — Show a patron's library card",
            "**/patron library-card update** — Browse/choose your library card design "
            "and edit its bio, genres, and checked-out books",
            "**/race start** `<duration_minutes> [countdown_seconds]` — Start a reading/writing sprint",
            "**/race stats** `[member]` — View sprint stats for yourself or another member",
            "**/race leaderboard** — View the sprint leaderboard",
            "**/race edit** — Edit or delete one of your sprint logs",
        ])
    )
    await interaction.response.send_message(embed=embed)


class StaffGroup(GriffinModGroup):
    def __init__(self, db):
        super().__init__(name="staff", description="Staff-only tools")
        self.db = db
        self.add_command(ChannelSetGroup(db))
        self.add_command(RaceConfigGroup(db))
        self.add_command(EmojiGroup(db))

    @app_commands.command(
        name="manager-role",
        description="Set (or clear) the role that can use all of Griffin's staff commands without Manage Server."
    )
    @app_commands.describe(role="Role granted full Griffin staff access — omit to clear it")
    async def manager_role(self, interaction: discord.Interaction, role: discord.Role = None):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only someone with Manage Server can change Griffin's manager role.", ephemeral=True
            )
            return
        await self.db.set_griffin_manager_role(interaction.guild.id, role.id if role else None)
        if role:
            await interaction.response.send_message(
                f"✅ {role.mention} can now use every Griffin staff command — no Manage Server needed. "
                f"(This can be the same role as the race librarian, or a different one.)"
            )
        else:
            await interaction.response.send_message("✅ Griffin manager role cleared.")

    @app_commands.command(name="help", description="Show what Griffin's staff commands can do.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title=f"{_emoji(interaction.client, 'tools')} Staff Commands",
            color=discord.Color.orange(),
            description="\n".join([
                "**/staff manager-role** `<role>` — Set who can use every Griffin staff command "
                "without Manage Server (Manage Server required to run this one)",
                "**/gg add member** `<member> <amount>` — Give Goblin Gold to a member",
                "**/gg add library-card** `<name> <image> [accent_color]` — Add a new "
                "library card design to the catalog",
                "**/gg remove** `<member> <amount>` — Remove Goblin Gold from a member",
                "**/gg setrate message** `<amount> <cooldown>` — Set GG earned per message "
                "and its cooldown",
                "**/gg setrate voice** `<amount> <per>` — Set GG earned per interval in "
                "voice, prorated while connected",
                "**/gg setrate sprint** `<amount>` — Set GG earned per completed sprint "
                "(0 turns it off)",
                "**/gg levels view** — Show the full level/tier ladder and GG requirements",
                "**/gg levels edit** — Modal to set a level's GG threshold (1-30) and/or the "
                "flat rate for levels 31-100",
                "**/gg levels set-tier** `<tier> [role]` — Tie a tier to a role (omit role to clear)",
                "**/staff channelset levelup** `<channel>` — Set the channel for level-up "
                "announcements",
                "**/staff channelset assets** `<channel>` — Set the channel uploaded card art "
                "gets archived to",
                "**/staff channelset sprint** `<id or link>` — Restrict sprints to a channel, forum or "
                "thread (paste its ID); leave blank to allow anywhere",
                "**/staff emoji add|remove|list** — Upload, remove or review the emojis Griffin uses",
                "**/race schedule** — Schedule a sprint ahead of time (one at a time). Start with a time "
                "like `14:30` (your own timezone, asked once) or a countdown like `in 1h30m`. A small notice "
                "goes up now; the join card and Page Racers ping go up 5 minutes before it starts",
                "**/race scheduled** — See the upcoming scheduled sprints",
                "**/race timezone** — Set your timezone, used to read the times you type when scheduling",
                "**/race unschedule** `<sprint>` — Cancel a scheduled sprint",
                "**/race cancel** — Force-cancel the sprint that's running or collecting logs",
                "**/race start** `Mod Test (5 min, no GG)` — A 5-minute practice sprint for testing (also offered in `/race schedule`); never awards GG",
                "**/staff race ping-role** `<role>` — Set the role pinged when a sprint starts",
                "**/staff race librarian-role** `<role>` — Set who can manage sprints",
                "**/staff race librarian-ping** `<on/off>` — Ping the librarian role with sprint results "
                "(off by default; turn on for readathons)",
                "**/staff race timezone** `<timezone>` — Server fallback timezone (offered to people who haven't set their own)",
            ])
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
    bot.tree.add_command(PatronGroup(bot.db))
    bot.tree.add_command(GGGroup(bot.db))
    bot.tree.add_command(help_command)
    bot.tree.add_command(StaffGroup(bot.db))
