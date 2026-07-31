import discord
from discord.ext import commands
from discord import app_commands
from datetime import timezone, timedelta
import asyncio
import json

from cogs.economy import _award_gg

SPRINT_GG = 50

active_sprints: dict[int, dict] = {}


async def _award_sprint_gg(bot, guild: discord.Guild, user_id: int, amount: int):
    member = guild.get_member(user_id)
    if member:
        await _award_gg(bot.db, guild, member, amount)
    else:
        await bot.db.add_gg(guild.id, user_id, amount)


def _safe_name(member: discord.Member | None, user_id: int) -> str:
    return member.display_name if member else f"User {user_id}"


async def _get_channel(guild, channel_id):
    ch = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            pass
    return ch


def _deserialize_start(ptype, val):
    if ptype == "audio":
        return tuple(json.loads(val))
    if ptype in ("ebook_pct", "audio_pct"):
        return float(val)
    return int(val)

def _deserialize_end(ptype, val):
    if val is None:
        return None
    if ptype == "audio":
        return tuple(json.loads(val))
    if ptype in ("ebook_pct", "audio_pct"):
        return float(val)
    return int(val)

def _pct_to_pages(pct: float, total_pages: int) -> int:
    return round(total_pages * pct / 100)

def _pct_to_minutes(pct: float, total_minutes: int) -> int:
    return round(total_minutes * pct / 100)


# ── Modals ────────────────────────────────────────────────────────────────────

class JoinPagesModal(discord.ui.Modal, title="Join Sprint — Pages"):
    reading = discord.ui.TextInput(label="Reading title", placeholder="What are you reading?", required=False)
    start_page = discord.ui.TextInput(label="Starting page", placeholder="e.g. 42", default="0")

    def __init__(self, cog, prefill_title: str | None = None):
        super().__init__()
        self.cog = cog
        if prefill_title:
            self.reading.default = prefill_title

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in active_sprints:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        if interaction.user.id in active_sprints[guild_id].get("participants", {}):
            await interaction.response.send_message("You've already joined this sprint!", ephemeral=True)
            return
        try:
            start = int(self.start_page.value)
        except ValueError:
            start = 0
        title = self.reading.value or None
        uid = interaction.user.id
        from datetime import datetime
        joined_at = datetime.utcnow()
        active_sprints[guild_id]["participants"][uid] = {
            "type": "pages", "title": title, "start": start, "end": None, "joined_at": joined_at, "gg_awarded": False
        }
        await interaction.client.db.save_sprint_participant(guild_id, uid, "pages", title, str(start), joined_at=joined_at)
        suffix = f" — reading **{title}**" if title else ""
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 📖 **page {start}**{suffix}!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class JoinAudioModal(discord.ui.Modal, title="Join Sprint — Audiobook"):
    listening = discord.ui.TextInput(label="Listening to", placeholder="What are you listening to?", required=False)
    start_hours = discord.ui.TextInput(label="Start time — hours", placeholder="e.g. 3", default="0")
    start_minutes = discord.ui.TextInput(label="Start time — minutes", placeholder="e.g. 45", default="0")

    def __init__(self, cog, prefill_title: str | None = None):
        super().__init__()
        self.cog = cog
        if prefill_title:
            self.listening.default = prefill_title

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in active_sprints:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        if interaction.user.id in active_sprints[guild_id].get("participants", {}):
            await interaction.response.send_message("You've already joined this sprint!", ephemeral=True)
            return
        try:
            h = int(self.start_hours.value)
        except ValueError:
            h = 0
        try:
            m = int(self.start_minutes.value)
        except ValueError:
            m = 0
        title = self.listening.value or None
        uid = interaction.user.id
        from datetime import datetime
        joined_at = datetime.utcnow()
        active_sprints[guild_id]["participants"][uid] = {
            "type": "audio", "title": title, "start": (h, m), "end": None, "joined_at": joined_at, "gg_awarded": False
        }
        await interaction.client.db.save_sprint_participant(
            guild_id, uid, "audio", title, json.dumps([h, m]), joined_at=joined_at
        )
        time_str = f"{h}h {m}m" if h else f"{m}m"
        suffix = f" — listening to **{title}**" if title else ""
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 🎧 **{time_str}**{suffix}!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class JoinEbookPctModal(discord.ui.Modal, title="Join Sprint — eBook %"):
    reading   = discord.ui.TextInput(label="Reading title", placeholder="What are you reading?")
    total_pages = discord.ui.TextInput(label="Total pages in book", placeholder="e.g. 400", required=False)
    start_pct = discord.ui.TextInput(label="Starting percentage", placeholder="e.g. 24")

    def __init__(self, cog, prefill_title: str | None = None, prefill_total: int | None = None):
        super().__init__()
        self.cog = cog
        if prefill_title:
            self.reading.default = prefill_title
        if prefill_total:
            self.total_pages.default = str(prefill_total)
            self.total_pages.required = False

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in active_sprints:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        if interaction.user.id in active_sprints[guild_id].get("participants", {}):
            await interaction.response.send_message("You've already joined this sprint!", ephemeral=True)
            return
        title = self.reading.value.strip()
        try:
            pct = float(self.start_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        total = None
        if self.total_pages.value.strip():
            try:
                total = int(self.total_pages.value.strip())
            except ValueError:
                await interaction.response.send_message("Please enter a valid number for total pages.", ephemeral=True)
                return
        uid = interaction.user.id
        from datetime import datetime
        joined_at = datetime.utcnow()
        active_sprints[guild_id]["participants"][uid] = {
            "type": "ebook_pct", "title": title, "start": pct,
            "end": None, "joined_at": joined_at, "gg_awarded": False,
            "total_pages": total,
        }
        await interaction.client.db.save_sprint_participant(guild_id, uid, "ebook_pct", title, str(pct), joined_at=joined_at)
        if title and total:
            await interaction.client.db.save_sprint_book_meta(uid, guild_id, title, total_pages=total)
        start_str = f"{pct:.1f}%".rstrip("0").rstrip(".")
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 📱 **{start_str}** — reading **{title}**!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class JoinAudioPctModal(discord.ui.Modal, title="Join Sprint — Audio %"):
    listening     = discord.ui.TextInput(label="Listening to", placeholder="What are you listening to?")
    total_hours   = discord.ui.TextInput(label="Total audiobook hours", placeholder="e.g. 12", required=False)
    start_pct     = discord.ui.TextInput(label="Starting percentage", placeholder="e.g. 24")

    def __init__(self, cog, prefill_title: str | None = None, prefill_total_mins: int | None = None):
        super().__init__()
        self.cog = cog
        if prefill_title:
            self.listening.default = prefill_title
        if prefill_total_mins:
            self.total_hours.default = str(round(prefill_total_mins / 60, 1))
            self.total_hours.required = False

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        if guild_id not in active_sprints:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        if interaction.user.id in active_sprints[guild_id].get("participants", {}):
            await interaction.response.send_message("You've already joined this sprint!", ephemeral=True)
            return
        title = self.listening.value.strip()
        try:
            pct = float(self.start_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        total_mins = None
        if self.total_hours.value.strip():
            try:
                total_mins = round(float(self.total_hours.value.strip()) * 60)
            except ValueError:
                await interaction.response.send_message("Please enter a valid number for total hours.", ephemeral=True)
                return
        uid = interaction.user.id
        from datetime import datetime
        joined_at = datetime.utcnow()
        active_sprints[guild_id]["participants"][uid] = {
            "type": "audio_pct", "title": title, "start": pct,
            "end": None, "joined_at": joined_at, "gg_awarded": False,
            "total_minutes": total_mins,
        }
        await interaction.client.db.save_sprint_participant(guild_id, uid, "audio_pct", title, str(pct), joined_at=joined_at)
        if title and total_mins:
            await interaction.client.db.save_sprint_book_meta(uid, guild_id, title, total_minutes=total_mins)
        start_str = f"{pct:.1f}%".rstrip("0").rstrip(".")
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 🎙️ **{start_str}** — listening to **{title}**!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class UpdatePagesModal(discord.ui.Modal, title="Update Progress — Pages"):
    current_page = discord.ui.TextInput(label="Current page", placeholder="e.g. 87")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No active sprint to update.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You haven't joined this sprint yet!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "pages":
            await interaction.response.send_message("You joined with audio — use the audio update button.", ephemeral=True)
            return
        try:
            page = int(self.current_page.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        sprint["participants"][uid]["end"] = page
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(page))
        await interaction.response.send_message(
            f"✅ Progress saved — you're at page **{page}**!", ephemeral=True
        )


class UpdateAudioModal(discord.ui.Modal, title="Update Progress — Audiobook"):
    current_hours   = discord.ui.TextInput(label="Current time — hours",   placeholder="e.g. 4", default="0")
    current_minutes = discord.ui.TextInput(label="Current time — minutes", placeholder="e.g. 10", default="0")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No active sprint to update.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You haven't joined this sprint yet!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "audio":
            await interaction.response.send_message("You joined with pages — use the pages update button.", ephemeral=True)
            return
        try:
            h = int(self.current_hours.value)
        except ValueError:
            h = 0
        try:
            m = int(self.current_minutes.value)
        except ValueError:
            m = 0
        sprint["participants"][uid]["end"] = (h, m)
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, json.dumps([h, m]))
        time_str = f"{h}h {m}m" if h else f"{m}m"
        await interaction.response.send_message(
            f"✅ Progress saved — you're at **{time_str}**!", ephemeral=True
        )


class UpdateEbookPctModal(discord.ui.Modal, title="Update Progress — eBook %"):
    current_pct = discord.ui.TextInput(label="Current percentage", placeholder="e.g. 38")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No active sprint to update.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You haven't joined this sprint yet!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "ebook_pct":
            await interaction.response.send_message("You didn't join with eBook % — use the correct update button.", ephemeral=True)
            return
        try:
            pct = float(self.current_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        sprint["participants"][uid]["end"] = pct
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(pct))
        await interaction.response.send_message(f"✅ Progress saved — you're at **{pct:.1f}%**!", ephemeral=True)


class UpdateAudioPctModal(discord.ui.Modal, title="Update Progress — Audio %"):
    current_pct = discord.ui.TextInput(label="Current percentage", placeholder="e.g. 38")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No active sprint to update.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You haven't joined this sprint yet!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "audio_pct":
            await interaction.response.send_message("You didn't join with Audio % — use the correct update button.", ephemeral=True)
            return
        try:
            pct = float(self.current_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        sprint["participants"][uid]["end"] = pct
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(pct))
        await interaction.response.send_message(f"✅ Progress saved — you're at **{pct:.1f}%**!", ephemeral=True)


class LogPagesModal(discord.ui.Modal, title="Log Sprint — Pages"):
    end_page = discord.ui.TextInput(label="Final page", placeholder="e.g. 87")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id) or self.cog._logging_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No sprint to log for.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message(
                "You didn't join this sprint!", ephemeral=True
            )
            return
        try:
            end = int(self.end_page.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        sprint["participants"][uid]["end"] = end
        sprint["participants"][uid]["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end))
        self.cog._check_all_logged(guild_id)
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        start = sprint["participants"][uid]["start"]
        total = end - start
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 📖 **page {end}** (+{total} pages) — **{SPRINT_GG} GG** earned!",
            ephemeral=False
        )


class LogAudioModal(discord.ui.Modal, title="Log Sprint — Audiobook"):
    end_hours   = discord.ui.TextInput(label="End time — hours",   placeholder="e.g. 4", default="0")
    end_minutes = discord.ui.TextInput(label="End time — minutes", placeholder="e.g. 10", default="0")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id) or self.cog._logging_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No sprint to log for.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message(
                "You didn't join this sprint!", ephemeral=True
            )
            return
        try:
            h = int(self.end_hours.value)
        except ValueError:
            h = 0
        try:
            m = int(self.end_minutes.value)
        except ValueError:
            m = 0
        sprint["participants"][uid]["end"] = (h, m)
        sprint["participants"][uid]["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, json.dumps([h, m]))
        self.cog._check_all_logged(guild_id)
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        start_h, start_m = sprint["participants"][uid]["start"]
        gained = (h * 60 + m) - (start_h * 60 + start_m)
        gained_str = f"{gained // 60}h {gained % 60}m" if gained >= 60 else f"{gained}m"
        end_str = f"{h}h {m}m" if h else f"{m}m"
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 🎧 **{end_str}** (+{gained_str}) — **{SPRINT_GG} GG** earned!",
            ephemeral=False
        )


class LogEbookPctModal(discord.ui.Modal, title="Log Sprint — eBook %"):
    end_pct = discord.ui.TextInput(label="Final percentage", placeholder="e.g. 52")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id) or self.cog._logging_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No sprint to log for.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You didn't join this sprint!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "ebook_pct":
            await interaction.response.send_message("You didn't join with eBook % — use the correct log button.", ephemeral=True)
            return
        try:
            end_pct = float(self.end_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        start_pct = sprint["participants"][uid]["start"]
        gained_pct = end_pct - start_pct
        title = sprint["participants"][uid].get("title")
        total_pages = sprint["participants"][uid].get("total_pages")
        if not total_pages and title:
            meta = await interaction.client.db.get_sprint_book_meta(uid, guild_id, title)
            if meta:
                total_pages = meta.get("total_pages")
        sprint["participants"][uid]["end"] = end_pct
        sprint["participants"][uid]["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end_pct))
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        self.cog._check_all_logged(guild_id)
        if total_pages:
            pages_gained = _pct_to_pages(gained_pct, total_pages)
            progress_str = f"+{pages_gained} pages ({gained_pct:.1f}%)"
        else:
            progress_str = f"+{gained_pct:.1f}%"
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 📱 **{end_pct:.1f}%** ({progress_str}) — **{SPRINT_GG} GG** earned!",
            ephemeral=False
        )


class LogAudioPctModal(discord.ui.Modal, title="Log Sprint — Audio %"):
    end_pct = discord.ui.TextInput(label="Final percentage", placeholder="e.g. 52")

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id) or self.cog._logging_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("No sprint to log for.", ephemeral=True)
            return
        if uid not in sprint["participants"]:
            await interaction.response.send_message("You didn't join this sprint!", ephemeral=True)
            return
        if sprint["participants"][uid]["type"] != "audio_pct":
            await interaction.response.send_message("You didn't join with Audio % — use the correct log button.", ephemeral=True)
            return
        try:
            end_pct = float(self.end_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        start_pct = sprint["participants"][uid]["start"]
        gained_pct = end_pct - start_pct
        title = sprint["participants"][uid].get("title")
        total_minutes = sprint["participants"][uid].get("total_minutes")
        if not total_minutes and title:
            meta = await interaction.client.db.get_sprint_book_meta(uid, guild_id, title)
            if meta:
                total_minutes = meta.get("total_minutes")
        sprint["participants"][uid]["end"] = end_pct
        sprint["participants"][uid]["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end_pct))
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        self.cog._check_all_logged(guild_id)
        if total_minutes:
            mins_gained = _pct_to_minutes(gained_pct, total_minutes)
            gained_str = f"{mins_gained // 60}h {mins_gained % 60}m" if mins_gained >= 60 else f"{mins_gained}m"
            progress_str = f"+{gained_str} ({gained_pct:.1f}%)"
        else:
            progress_str = f"+{gained_pct:.1f}%"
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 🎙️ **{end_pct:.1f}%** ({progress_str}) — **{SPRINT_GG} GG** earned!",
            ephemeral=False
        )


# ── Views ─────────────────────────────────────────────────────────────────────

class BookSelectView(discord.ui.View):
    def __init__(self, cog, titles: list[str], modal_type: str):
        super().__init__(timeout=60)
        self.cog = cog
        options = [discord.SelectOption(label=t[:100], value=t[:100]) for t in titles]
        options.append(discord.SelectOption(label="Enter a different title...", value="__new__"))
        select = discord.ui.Select(placeholder="Pick a recent book or enter a new one...", options=options)
        select.callback = self._make_callback(modal_type)
        self.add_item(select)

    def _make_callback(self, modal_type: str):
        async def callback(interaction: discord.Interaction):
            chosen = interaction.data["values"][0]
            uid = interaction.user.id
            guild_id = interaction.guild.id
            meta = None
            if chosen != "__new__":
                meta = await interaction.client.db.get_sprint_book_meta(uid, guild_id, chosen)
            if modal_type == "pages":
                modal = JoinPagesModal(self.cog, prefill_title=chosen if chosen != "__new__" else None)
            elif modal_type == "audio":
                modal = JoinAudioModal(self.cog, prefill_title=chosen if chosen != "__new__" else None)
            elif modal_type == "ebook_pct":
                total_pages = meta.get("total_pages") if meta else None
                modal = JoinEbookPctModal(self.cog,
                    prefill_title=chosen if chosen != "__new__" else None,
                    prefill_total=total_pages)
            else:  # audio_pct
                total_mins = meta.get("total_minutes") if meta else None
                modal = JoinAudioPctModal(self.cog,
                    prefill_title=chosen if chosen != "__new__" else None,
                    prefill_total_mins=total_mins)
            await interaction.response.send_modal(modal)
        return callback


class JoinTypeView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.select(
        placeholder="How are you tracking?",
        options=[
            discord.SelectOption(label="Pages", emoji="📖", value="pages"),
            discord.SelectOption(label="Audio (minutes)", emoji="🎧", value="audio"),
            discord.SelectOption(label="eBook %", emoji="📱", value="ebook_pct"),
            discord.SelectOption(label="Audio %", emoji="🎙️", value="audio_pct"),
        ]
    )
    async def type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        ptype = select.values[0]
        titles = await interaction.client.db.get_recent_sprint_titles(interaction.guild.id, interaction.user.id)
        if titles:
            view = BookSelectView(self.cog, [r["title"] for r in titles], ptype)
            await interaction.response.edit_message(content="📚 Pick your book:", view=view)
        else:
            modal_map = {
                "pages": JoinPagesModal,
                "audio": JoinAudioModal,
                "ebook_pct": JoinEbookPctModal,
                "audio_pct": JoinAudioPctModal,
            }
            await interaction.response.send_modal(modal_map[ptype](self.cog))


class JoinSprintView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📖 Join Sprint", style=discord.ButtonStyle.success, custom_id="sprint_join_btn", row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "How would you like to track your sprint?", view=JoinTypeView(self.cog), ephemeral=True
        )

    @discord.ui.button(label="🔄 Update Progress", style=discord.ButtonStyle.success, custom_id="sprint_update_btn", row=0)
    async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await interaction.client.db._fetchone(
            "SELECT type FROM active_sprint_participants WHERE guild_id=%s AND user_id=%s",
            interaction.guild.id, interaction.user.id
        )
        if not row:
            await interaction.response.send_message(
                "❌ You haven't joined this sprint yet — pick a join type first!", ephemeral=True
            )
            return
        modal_map = {
            "pages": UpdatePagesModal,
            "audio": UpdateAudioModal,
            "ebook_pct": UpdateEbookPctModal,
            "audio_pct": UpdateAudioPctModal,
        }
        modal_cls = modal_map.get(row["type"])
        if not modal_cls:
            await interaction.response.send_message("❌ Unknown sprint type. Please rejoin.", ephemeral=True)
            return
        await interaction.response.send_modal(modal_cls(self.cog))


class LogProgressView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📝 Log Sprint", style=discord.ButtonStyle.success, custom_id="sprint_log_btn", row=0)
    async def log_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await interaction.client.db._fetchone(
            "SELECT type FROM active_sprint_participants WHERE guild_id=%s AND user_id=%s",
            interaction.guild.id, interaction.user.id
        )
        if not row:
            await interaction.response.send_message(
                "❌ You don't have a sprint entry to log — you may not have joined this sprint.", ephemeral=True
            )
            return
        modal_map = {
            "pages": LogPagesModal,
            "audio": LogAudioModal,
            "ebook_pct": LogEbookPctModal,
            "audio_pct": LogAudioPctModal,
        }
        modal_cls = modal_map.get(row["type"])
        if not modal_cls:
            await interaction.response.send_message("❌ Unknown sprint type. Please rejoin.", ephemeral=True)
            return
        await interaction.response.send_modal(modal_cls(self.cog))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Sprints(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Sprints in the 5-min logging window (after timer ends, before results)
        self._logging_sprints: dict[int, dict] = {}
        self._logging_tasks: dict[int, asyncio.Task] = {}

    def _check_all_logged(self, guild_id: int):
        """Cancel the logging wait if every participant has submitted their end count."""
        sprint = self._logging_sprints.get(guild_id)
        if not sprint:
            return
        participants = sprint.get("participants", {})
        if participants and all(p.get("end") is not None for p in participants.values()):
            task = self._logging_tasks.get(guild_id)
            if task and not task.done():
                task.cancel()

    async def cog_load(self):
        self.bot.add_view(JoinSprintView(self))
        self.bot.add_view(LogProgressView(self))
        self.bot.loop.create_task(self._restore_sprints())

    async def _restore_sprints(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            row = await self.bot.db.get_active_sprint(guild.id)
            if not row:
                continue
            now = discord.utils.utcnow()
            end_time = row["end_time"]
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            start_time = row["start_time"]
            if hasattr(start_time, "tzinfo") and start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)

            participants = {}
            for p in await self.bot.db.get_sprint_participants(guild.id):
                end = _deserialize_end(p["type"], p["end_val"])
                participants[p["user_id"]] = {
                    "type": p["type"], "title": p["title"],
                    "start": _deserialize_start(p["type"], p["start_val"]),
                    "end": end,
                    "joined_at": p.get("joined_at"),
                    "gg_awarded": end is not None,
                }

            sprint = {
                "host": row["host_id"],
                "channel": row["channel_id"],
                "duration": row["duration_minutes"],
                "started_at": row["start_time"],
                "start_time": start_time,
                "end_time": end_time,
                "role_id": row["role_id"],
                "participants": participants,
                "join_message_id": row.get("join_message_id"),
                "started": now >= start_time,
            }
            active_sprints[guild.id] = sprint

            remaining = (end_time - now).total_seconds()
            self.bot.loop.create_task(self._wait_and_finish(guild, max(remaining, 0)))

    async def _wait_and_finish(self, guild, seconds):
        await asyncio.sleep(seconds)
        await self._finish_sprint(guild)

    # ── Build the sprint embed ────────────────────────────────────────────────

    # Discord's hard cap per embed field value.
    _FIELD_LIMIT = 1000

    def _build_sprint_embeds(self, guild: discord.Guild, start_time, end_time, host_name, duration,
                              started: bool = False) -> list[discord.Embed]:
        sprint = active_sprints.get(guild.id)
        participants = sprint["participants"] if sprint else {}

        title_line = (
            "📣 **__READING SPRINT STARTED!__**" if started
            else "📣 **__Reading Sprint Starting Soon!__**"
        )
        lines = [
            title_line,
            "",
            f"🧌 **Host:** {host_name}",
            f"⏳ **Start Time:** {discord.utils.format_dt(start_time, 'F')} "
            f"*({discord.utils.format_dt(start_time, 'R')})*",
            f"🏁 **End Time:** {discord.utils.format_dt(end_time, 'F')} "
            f"*({discord.utils.format_dt(end_time, 'R')})*",
        ]
        main_embed = discord.Embed(description="\n".join(lines), color=discord.Color.gold())

        server_icon = guild.icon.url if guild.icon else None
        if server_icon:
            main_embed.set_thumbnail(url=server_icon)

        if not participants:
            main_embed.add_field(name="📖 Sprint Participants:", value="No participants yet", inline=False)
            return [main_embed]

        p_lines = []
        for uid, data in participants.items():
            title = data.get("title") or ""
            ptype = data["type"]
            suffix = f" of **{title}**" if title else ""
            if ptype == "audio":
                h, m = data["start"]
                pos = f"{h}h {m}m" if h else f"{m}m"
                p_lines.append(f"📌 <@{uid}> - {pos}{suffix}")
            elif ptype in ("ebook_pct", "audio_pct"):
                p_lines.append(f"📌 <@{uid}> - {data['start']:.1f}%{suffix}")
            else:
                p_lines.append(f"📌 <@{uid}> - Page {data['start']}{suffix}")

        # Full roster always shows — chunk across fields since a single field
        # caps out at 1024 chars and groups can run 10-30+ deep.
        first = True
        chunk = ""
        for line in p_lines:
            candidate = f"{chunk}\n{line}" if chunk else line
            if len(candidate) > self._FIELD_LIMIT:
                main_embed.add_field(
                    name="📖 Sprint Participants:" if first else "​",
                    value=chunk, inline=False
                )
                first = False
                chunk = line
            else:
                chunk = candidate
        if chunk:
            main_embed.add_field(
                name="📖 Sprint Participants:" if first else "​",
                value=chunk, inline=False
            )

        return [main_embed]

    async def _update_join_embed(self, guild):
        """Edit the sprint announcement embed to show updated participants."""
        sprint = active_sprints.get(guild.id)
        if not sprint or not sprint.get("join_message_id"):
            return
        channel = await _get_channel(guild, sprint["channel"])
        if not channel:
            return
        start_time = sprint.get("start_time") or sprint.get("end_time")
        end_time = sprint.get("end_time")
        if not start_time or not end_time:
            return
        try:
            msg = await channel.fetch_message(sprint["join_message_id"])
            host = guild.get_member(sprint["host"])
            host_name = host.display_name if host else "Unknown"
            embeds = self._build_sprint_embeds(
                guild, start_time, end_time, host_name, sprint["duration"],
                started=sprint.get("started", False)
            )
            await msg.edit(embeds=embeds)
        except Exception:
            pass

    # ── /race start ─────────────────────────────────────────────────────────

    race = app_commands.Group(name="race", description="Sprint race commands")

    @race.command(name="start", description="Start a reading/writing sprint!")
    @app_commands.describe(
        duration_minutes="How many minutes the sprint lasts (max 120)",
        countdown_seconds="Seconds before the sprint begins (default 30)"
    )
    async def sprint_start(self, interaction: discord.Interaction,
                           duration_minutes: int, countdown_seconds: int = 30):
        guild_id = interaction.guild.id

        settings = await self.bot.db.get_guild_config(guild_id)
        sprint_channel_id = settings.get("sprint_channel_id")
        if sprint_channel_id and interaction.channel.id != sprint_channel_id:
            await interaction.response.send_message(
                f"❌ Sprints can only be started in <#{sprint_channel_id}>.", ephemeral=True
            )
            return

        if guild_id in active_sprints:
            await interaction.response.send_message(
                "A sprint is already running!", ephemeral=True
            )
            return

        duration_minutes = min(duration_minutes, 120)
        countdown_seconds = min(countdown_seconds, 300)

        role_id = settings.get("sprint_role_id")
        role_mention = f"<@&{role_id}>" if role_id else ""

        now = discord.utils.utcnow()
        start_time = now + timedelta(seconds=countdown_seconds)
        end_time   = start_time + timedelta(minutes=duration_minutes)

        sprint_data = {
            "host": interaction.user.id,
            "channel": interaction.channel.id,
            "duration": duration_minutes,
            "started_at": now,
            "start_time": start_time,
            "end_time": end_time,
            "role_id": role_id,
            "participants": {},
            "join_message_id": None,
            "started": False,
        }
        active_sprints[guild_id] = sprint_data

        embeds = self._build_sprint_embeds(
            interaction.guild, start_time, end_time, interaction.user.display_name, duration_minutes
        )
        view = JoinSprintView(self)

        await interaction.response.send_message(
            content=role_mention if role_mention else None,
            embeds=embeds,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        msg = await interaction.original_response()
        active_sprints[guild_id]["join_message_id"] = msg.id

        await self.bot.db.save_active_sprint(
            guild_id, interaction.user.id, interaction.channel.id,
            duration_minutes, now, end_time, role_id
        )
        await self.bot.db.update_sprint_message_id(guild_id, msg.id)

        await asyncio.sleep(countdown_seconds)
        if guild_id not in active_sprints:
            return

        active_sprints[guild_id]["started"] = True
        channel = await _get_channel(interaction.guild, interaction.channel.id)
        if channel:
            participant_ids = list(active_sprints.get(guild_id, {}).get("participants", {}).keys())
            participant_mentions = " ".join(f"<@{uid}>" for uid in participant_ids) if participant_ids else None
            started_embeds = self._build_sprint_embeds(
                interaction.guild, start_time, end_time, interaction.user.display_name, duration_minutes,
                started=True
            )
            try:
                msg = await channel.fetch_message(active_sprints[guild_id]["join_message_id"])
                await msg.edit(content=participant_mentions, embeds=started_embeds,
                                allowed_mentions=discord.AllowedMentions(users=True))
            except Exception:
                await channel.send(
                    f"{participant_mentions + ' ' if participant_mentions else ''}🏃 **Sprint started!** "
                    f"You have **{duration_minutes} minutes**. Go go go!",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )

        await asyncio.sleep(duration_minutes * 60)
        await self._finish_sprint(interaction.guild)


    # ── /race edit ──────────────────────────────────────────────────────────

    @race.command(name="edit", description="Edit or delete one of your sprint logs")
    async def sprint_edit(self, interaction: discord.Interaction):
        logs = await self.bot.db.get_sprint_logs(interaction.guild.id, interaction.user.id, limit=10)
        if not logs:
            await interaction.response.send_message("You don't have any logged sprints yet.", ephemeral=True)
            return
        view = SprintLogSelectView(self.bot, logs, interaction.guild.id)
        await interaction.response.send_message(
            "Select a sprint log to edit or delete:", view=view, ephemeral=True
        )

    # ── /race stats ─────────────────────────────────────────────────────────

    @race.command(name="stats", description="View sprint stats for yourself or another member")
    @app_commands.describe(member="Whose stats to view (defaults to you)")
    async def sprint_stats(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        member = member or interaction.user
        stats = await self.bot.db.get_sprint_stats(interaction.guild.id, member.id)
        gg_earned = (stats["sprints_completed"] or 0) * SPRINT_GG
        pages = stats["total_pages"] or 0
        audio_mins = int(stats["total_audio_minutes"] or 0)
        unlogged = int(stats["unlogged_sprints"] or 0)

        embed = discord.Embed(title=f"📊 Sprint Stats — {member.display_name}", color=discord.Color.teal())
        embed.add_field(name="Sprints Completed", value=str(stats["sprints_completed"] or 0))
        if pages:
            embed.add_field(name="📖 Pages Read", value=f"{pages:,}")
        if audio_mins:
            time_str = f"{audio_mins // 60}h {audio_mins % 60}m" if audio_mins >= 60 else f"{audio_mins}m"
            embed.add_field(name="🎧 Audio Time", value=time_str)
        embed.add_field(name="GG Earned from Sprints", value=f"{gg_earned} GG")
        if unlogged:
            embed.add_field(name="🫣 Unlogged Sprints", value=str(unlogged))
        await interaction.followup.send(embed=embed)

    # ── /race leaderboard ───────────────────────────────────────────────────

    @race.command(name="leaderboard", description="View the sprint leaderboard")
    async def sprint_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.db.get_sprint_leaderboard(interaction.guild.id)
        if not rows:
            await interaction.followup.send("No sprint data yet!")
            return

        ordinals = ["1ST", "2ND", "3RD"] + [f"{i}TH" for i in range(4, len(rows) + 1)]
        lines = []
        for i, row in enumerate(rows):
            member = interaction.guild.get_member(row["user_id"])
            name = _safe_name(member, row["user_id"])
            sprints = row["sprints_completed"] or 0
            total_pages = row["total_pages"] or 0
            audio_mins = int(row["total_audio_minutes"] or 0)
            time_display = (f"{audio_mins // 60}h {audio_mins % 60}m" if audio_mins >= 60 else f"{audio_mins}m") if audio_mins else "0m"
            gg = sprints * SPRINT_GG
            lines.append(
                f"**{ordinals[i]} - {name.upper()}**\n"
                f"{sprints} Sprint{'s' if sprints != 1 else ''} | "
                f"Total Pages Logged: {total_pages:,} · Total Audio Logged: {time_display} · "
                f"Total Sprint GG Earned: {gg:,} GG"
            )
        embed = discord.Embed(title="🏆 Sprint Leaderboard", description="\n\n".join(lines), color=discord.Color.gold())
        await interaction.followup.send(embed=embed)

    # ── Internal: finish sprint ───────────────────────────────────────────────

    async def _finish_sprint(self, guild: discord.Guild):
        guild_id = guild.id
        sprint = active_sprints.pop(guild_id, None)
        if not sprint:
            return

        # Reload participants from DB to pick up any late joins
        for p in await self.bot.db.get_sprint_participants(guild_id):
            uid = p["user_id"]
            if uid not in sprint["participants"]:
                sprint["participants"][uid] = {
                    "type": p["type"], "title": p["title"],
                    "start": _deserialize_start(p["type"], p["start_val"]),
                    "end": _deserialize_end(p["type"], p["end_val"]),
                }

        # Keep in logging window
        self._logging_sprints[guild_id] = sprint

        channel = await _get_channel(guild, sprint["channel"])
        if not channel:
            await self.bot.db.delete_active_sprint(guild_id)
            return

        participant_ids = list(sprint.get("participants", {}).keys())
        participant_mentions = " ".join(f"<@{uid}>" for uid in participant_ids) if participant_ids else None

        deadline = discord.utils.utcnow() + timedelta(minutes=5)
        view = LogProgressView(self)
        await channel.send(
            content=participant_mentions,
            embed=discord.Embed(
                title="⏰ Sprint Ended — Log Your Progress!",
                description=f"Log your final page or audio time by {discord.utils.format_dt(deadline, 'R')}!",
                color=discord.Color.orange()
            ),
            view=view,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        # Wait up to 5 minutes, but cancel early if everyone logs
        task = asyncio.ensure_future(asyncio.sleep(300))
        self._logging_tasks[guild_id] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._logging_tasks.pop(guild_id, None)

        self._logging_sprints.pop(guild_id, None)

        # Re-fetch final state from DB
        sprint_end_time = discord.utils.utcnow()

        final_participants = {}
        for p in await self.bot.db.get_sprint_participants(guild_id):
            uid = p["user_id"]
            final_participants[uid] = {
                "type": p["type"], "title": p["title"],
                "start": _deserialize_start(p["type"], p["start_val"]),
                "end": _deserialize_end(p["type"], p["end_val"]),
                "joined_at": p.get("joined_at"),
            }
        # Merge in-memory logged results (preserve joined_at and gg_awarded from memory)
        for uid, data in sprint["participants"].items():
            if data["end"] is not None:
                final_participants.setdefault(uid, data)
                final_participants[uid]["end"] = data["end"]
                if data.get("joined_at") and not final_participants[uid].get("joined_at"):
                    final_participants[uid]["joined_at"] = data["joined_at"]
                if data.get("gg_awarded"):
                    final_participants[uid]["gg_awarded"] = True

        await self.bot.db.delete_active_sprint(guild_id)

        # Award GG for anyone who updated mid-sprint but didn't log in the window
        silent_gg_uids = []
        for uid, data in final_participants.items():
            if data["end"] is not None and not data.get("gg_awarded"):
                await _award_sprint_gg(self.bot, guild, uid, SPRINT_GG)
                data["gg_awarded"] = True
                silent_gg_uids.append(uid)

        def _duration_str(joined_at, end_time):
            if not joined_at:
                return None
            from datetime import timezone as _tz
            j = joined_at.replace(tzinfo=_tz.utc) if joined_at.tzinfo is None else joined_at
            e = end_time.replace(tzinfo=_tz.utc) if end_time.tzinfo is None else end_time
            mins = max(0, int((e - j).total_seconds() / 60))
            return f"{mins}m" if mins < 60 else f"{mins // 60}h {mins % 60}m"

        results = []
        for uid, data in final_participants.items():
            if data["end"] is None:
                continue
            member = guild.get_member(uid)
            name = _safe_name(member, uid)
            title = data.get("title")
            duration = _duration_str(data.get("joined_at"), sprint_end_time)
            ptype = data["type"]
            if ptype == "audio":
                start_h, start_m = data["start"]
                end_h, end_m = data["end"]
                gained = (end_h * 60 + end_m) - (start_h * 60 + start_m)
                gained_str = f"{gained // 60}h {gained % 60}m" if gained >= 60 else f"{gained}m"
                results.append((name, f"+{gained_str}", title, duration))
            elif ptype == "ebook_pct":
                gained_pct = data["end"] - data["start"]
                meta = await self.bot.db.get_sprint_book_meta(uid, guild_id, title) if title else None
                total_pages = data.get("total_pages") or (meta.get("total_pages") if meta else None)
                if total_pages:
                    pages = _pct_to_pages(gained_pct, total_pages)
                    progress = f"+{pages} pages ({gained_pct:.1f}%)"
                else:
                    progress = f"+{gained_pct:.1f}%"
                results.append((name, progress, title, duration))
            elif ptype == "audio_pct":
                gained_pct = data["end"] - data["start"]
                meta = await self.bot.db.get_sprint_book_meta(uid, guild_id, title) if title else None
                total_mins = data.get("total_minutes") or (meta.get("total_minutes") if meta else None)
                if total_mins:
                    mins = _pct_to_minutes(gained_pct, total_mins)
                    gained_str = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
                    progress = f"+{gained_str} ({gained_pct:.1f}%)"
                else:
                    progress = f"+{gained_pct:.1f}%"
                results.append((name, progress, title, duration))
            else:
                total = data["end"] - data["start"]
                results.append((name, f"+{total} pages", title, duration))

        if results:
            embed = discord.Embed(title="🏁 Sprint Results!", color=discord.Color.gold())
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, (name, progress, title, duration) in enumerate(results):
                suffix = f" — *{title}*" if title else ""
                prefix = medals[i] if i < 3 else f"`#{i+1}`"
                lines.append(f"{prefix} **{name}** — {progress}{suffix}")
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Each participant earned {SPRINT_GG} GG!")
            await channel.send(embed=embed)
        else:
            await channel.send("No one logged their results. 📭")

        if silent_gg_uids:
            mentions = " ".join(f"<@{uid}>" for uid in silent_gg_uids)
            await channel.send(
                f"{mentions} — you updated your progress during the sprint and earned **{SPRINT_GG} GG**! 📚"
            )

        sprint_id = await self.bot.db.create_sprint(
            guild_id, sprint["channel"], sprint["host"],
            sprint["started_at"], sprint_end_time, sprint["duration"]
        )
        for uid, data in final_participants.items():
            ptype = data["type"]
            title = data.get("title")
            if data["end"] is not None:
                if ptype == "audio":
                    start_val = data["start"][0] * 60 + data["start"][1]
                    end_val = data["end"][0] * 60 + data["end"][1]
                    save_type = "audio"
                elif ptype == "ebook_pct":
                    gained_pct = data["end"] - data["start"]
                    meta = await self.bot.db.get_sprint_book_meta(uid, guild_id, title) if title else None
                    total_pages = data.get("total_pages") or (meta.get("total_pages") if meta else None)
                    start_val = _pct_to_pages(data["start"], total_pages) if total_pages else 0
                    end_val = _pct_to_pages(data["end"], total_pages) if total_pages else int(data["end"])
                    save_type = "pages"
                elif ptype == "audio_pct":
                    meta = await self.bot.db.get_sprint_book_meta(uid, guild_id, title) if title else None
                    total_mins = data.get("total_minutes") or (meta.get("total_minutes") if meta else None)
                    start_val = _pct_to_minutes(data["start"], total_mins) if total_mins else 0
                    end_val = _pct_to_minutes(data["end"], total_mins) if total_mins else int(data["end"])
                    save_type = "audio"
                else:
                    start_val = data["start"]
                    end_val = data["end"]
                    save_type = "pages"
            else:
                # Record unlogged participants so they appear in stats
                save_type = "audio" if ptype in ("audio", "audio_pct") else "pages"
                start_val = 0
                end_val = None
            await self.bot.db.add_sprint_participant(
                sprint_id, uid, start_val, end_val, save_type,
                joined_at=data.get("joined_at"), sprint_end_time=sprint_end_time,
                title=title,
            )


# ── Sprint Edit Views ──────────────────────────────────────────────────────────

class SprintEditModal(discord.ui.Modal):
    def __init__(self, bot, sprint_id: int, title: str, start: int, end: int, log_type: str, guild_id: int):
        super().__init__(title="Edit Sprint Log")
        self.bot = bot
        self.sprint_id = sprint_id
        self.log_type = log_type
        self.guild_id = guild_id

        unit = "page" if log_type == "pages" else ("%" if "pct" in log_type else "min")
        self.title_input = discord.ui.TextInput(
            label="Title", default=title, required=False, max_length=100
        )
        self.start_input = discord.ui.TextInput(
            label=f"Start ({unit})", default=str(start), required=True, max_length=6
        )
        self.end_input = discord.ui.TextInput(
            label=f"End ({unit})", default=str(end), required=True, max_length=6
        )
        self.add_item(self.title_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            start = int(self.start_input.value)
            end = int(self.end_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Start and end must be numbers.", ephemeral=True)
            return
        if end < start:
            await interaction.response.send_message("❌ End can't be less than start.", ephemeral=True)
            return
        new_title = self.title_input.value.strip() or None
        await self.bot.db.update_sprint_log(self.sprint_id, interaction.user.id, start, end, new_title)
        await interaction.response.send_message(
            f"✅ Log updated — **{new_title or 'Untitled'}** · {start} → {end}.", ephemeral=True
        )


class SprintDeleteConfirmView(discord.ui.View):
    def __init__(self, bot, sprint_id: int, guild_id: int):
        super().__init__(timeout=30)
        self.bot = bot
        self.sprint_id = sprint_id
        self.guild_id = guild_id

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.db.delete_sprint_log(self.sprint_id, interaction.user.id)
        await self.bot.db.remove_gg(self.guild_id, interaction.user.id, SPRINT_GG)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"🗑️ Log deleted and **{SPRINT_GG} GG** deducted.", view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()


class SprintLogSelectView(discord.ui.View):
    def __init__(self, bot, logs: list, guild_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.logs = {str(row["sprint_id"]): row for row in logs}

        options = []
        for row in logs:
            date = row["sprint_end_time"].strftime("%b %d") if row["sprint_end_time"] else "?"
            progress = (row["end_count"] or 0) - (row["start_count"] or 0)
            unit = "pg" if row["type"] == "pages" else ("min" if row["type"] == "audio" else "%")
            label = (row["title"] or "Untitled")[:80]
            desc = f"{date} · {row['start_count']} → {row['end_count']} (+{progress} {unit})"
            options.append(discord.SelectOption(label=label, value=str(row["sprint_id"]), description=desc))

        select = discord.ui.Select(placeholder="Choose a sprint log to edit or delete…", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._selected = None

        self.edit_btn = discord.ui.Button(label="✏️ Edit", style=discord.ButtonStyle.primary, disabled=True)
        self.edit_btn.callback = self._on_edit
        self.add_item(self.edit_btn)

        self.delete_btn = discord.ui.Button(label="🗑️ Delete", style=discord.ButtonStyle.danger, disabled=True)
        self.delete_btn.callback = self._on_delete
        self.add_item(self.delete_btn)

    async def _on_select(self, interaction: discord.Interaction):
        self._selected = interaction.data["values"][0]
        self.edit_btn.disabled = False
        self.delete_btn.disabled = False
        await interaction.response.edit_message(view=self)

    async def _on_edit(self, interaction: discord.Interaction):
        row = self.logs[self._selected]
        modal = SprintEditModal(
            self.bot, row["sprint_id"], row["title"] or "Untitled",
            row["start_count"] or 0, row["end_count"] or 0,
            row["type"], self.guild_id
        )
        await interaction.response.send_modal(modal)

    async def _on_delete(self, interaction: discord.Interaction):
        row = self.logs[self._selected]
        date = row["sprint_end_time"].strftime("%b %d") if row["sprint_end_time"] else "?"
        await interaction.response.send_message(
            f"Delete **{row['title'] or 'Untitled'}** ({date})? This will deduct **{SPRINT_GG} GG**.",
            view=SprintDeleteConfirmView(self.bot, row["sprint_id"], self.guild_id),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Sprints(bot))
