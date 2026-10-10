import discord
from discord.ext import commands
from discord import app_commands
from datetime import timezone, timedelta, datetime
import asyncio
import json
import traceback

from cogs.economy import _award_gg
from utils.emojis import emoji as _emoji

SPRINT_GG = 50
LOG_WINDOW_MINUTES = 7
RACE_COLOR = discord.Color(0x26878D)

active_sprints: dict[int, dict] = {}

# Audio-length tiers -> pages-per-minute rate, used to convert an audiobook's
# %-complete into a page-equivalent for a unified (pages-only) leaderboard.
_AUDIO_RATE_TIERS = ((300, 0.5), (720, 0.45), (990, 0.35))
_AUDIO_RATE_DEFAULT = 0.3


def _audio_page_rate(total_minutes: int) -> float:
    for max_minutes, rate in _AUDIO_RATE_TIERS:
        if total_minutes <= max_minutes:
            return rate
    return _AUDIO_RATE_DEFAULT


def _audio_pct_to_pages(pct: float, total_minutes: int) -> int:
    effective_total_pages = total_minutes * _audio_page_rate(total_minutes)
    return round(effective_total_pages * pct / 100)


def _format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minutes"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minutes"


async def _award_sprint_gg(bot, guild: discord.Guild, user_id: int, amount: int):
    member = guild.get_member(user_id)
    if member:
        await _award_gg(bot.db, guild, member, amount)
    else:
        await bot.db.add_gg(guild.id, user_id, amount)


def _safe_name(member: discord.Member | None, user_id: int) -> str:
    return member.display_name if member else f"User {user_id}"


def _lookup_participant_type(cog, guild_id: int, uid: int) -> str | None:
    """In-memory lookup of a participant's tracking type — checks the active
    sprint, then (during the post-sprint log window) the finishing sprint."""
    sprint = active_sprints.get(guild_id) or cog._logging_sprints.get(guild_id)
    if sprint and uid in sprint["participants"]:
        return sprint["participants"][uid]["type"]
    return None


async def _get_channel(guild, channel_id):
    ch = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if ch is None:
        try:
            ch = await guild.fetch_channel(channel_id)
        except Exception:
            pass
    return ch


def _deserialize_start(ptype, val):
    if ptype == "audio_pct":
        return float(val)
    return int(val)


def _deserialize_end(ptype, val):
    if val is None:
        return None
    if ptype == "audio_pct":
        return float(val)
    return int(val)


def _parse_segments(raw) -> list[tuple[str, int]]:
    if not raw:
        return []
    try:
        return [(t, p) for t, p in json.loads(raw)]
    except (ValueError, TypeError):
        return []


def _progress_message(bot, member: discord.Member, pages_delta: int, extra: str = "") -> str:
    stamp = _emoji(bot, "librarystamp")
    book = _emoji(bot, "blankbook")
    extra_str = f" {extra}" if extra else ""
    return (
        f"{stamp} {member.mention}, Griffin has recorded sprint progress!\n"
        f"{book} +{pages_delta} pages{extra_str}, has been added to the ledger."
    )


def _leg_pages(data: dict, end_val) -> int:
    """Pages gained for the participant's current book leg, given an end value."""
    if data["type"] == "audio_pct":
        gained_pct = end_val - data["start"]
        total_minutes = data.get("total_minutes") or 0
        return _audio_pct_to_pages(gained_pct, total_minutes) if total_minutes else 0
    return end_val - data["start"]


def _combined_title_and_pages(data: dict, end_val) -> tuple[str | None, int]:
    """Folds in any Switch Books segments to produce one final title + page total."""
    leg_pages = _leg_pages(data, end_val)
    segments = data.get("segments") or []
    total_pages = leg_pages + sum(p for _, p in segments)
    titles = [t for t, _ in segments if t] + ([data["title"]] if data.get("title") else [])
    title = " & ".join(dict.fromkeys(titles)) if titles else None
    return title, total_pages


class SafeView(discord.ui.View):
    """Base for every sprint view — without this, a failed button callback
    leaves the interaction unresponded and Discord just shows "didn't
    respond in time" with zero information about what actually broke."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(f"View error on {item!r}:")
        traceback.print_exc()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong — please try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong — please try again.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class SafeModal(discord.ui.Modal):
    """Base for every sprint modal — without this, a failed on_submit leaves
    the interaction unresponded and Discord just shows "didn't respond in
    time" with zero information about what actually broke."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        print(f"Modal error in {self.title!r}:")
        traceback.print_exc()
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong submitting that — please try again.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong submitting that — please try again.", ephemeral=True
                )
        except discord.HTTPException:
            pass


# ── Pagination ───────────────────────────────────────────────────────────────

class EmbedPaginatorView(SafeView):
    def __init__(self, embeds: list[discord.Embed], *, timeout=180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.index = 0
        if len(embeds) > 1:
            for i, e in enumerate(embeds):
                page_note = f"Page {i + 1}/{len(embeds)}"
                e.set_footer(text=f"{e.footer.text} · {page_note}" if e.footer.text else page_note)
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.embeds) - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.embeds) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)


def _paginate_lines(lines: list[str], title: str, per_page: int = 10, char_limit: int = 3500) -> list[discord.Embed]:
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if len(current) >= per_page or current_len + len(line) > char_limit:
            pages.append(current)
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        pages.append(current)
    if not pages:
        pages = [[]]
    return [
        discord.Embed(title=title, description="\n\n".join(p) or "Nothing here yet.", color=RACE_COLOR)
        for p in pages
    ]


# ── Modals ────────────────────────────────────────────────────────────────────

class JoinPagesModal(SafeModal, title="Join Sprint — Pages"):
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
        joined_at = datetime.utcnow()
        # Persist before touching memory: if the save fails, nothing is left behind, so the
        # user can simply retry instead of being stuck as "already joined" with no roster entry.
        await interaction.client.db.save_sprint_participant(guild_id, uid, "pages", title, str(start), joined_at=joined_at)
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        sprint["participants"][uid] = {
            "type": "pages", "title": title, "start": start, "end": None, "joined_at": joined_at,
            "gg_awarded": False, "segments": [],
        }
        suffix = f" — reading **{title}**" if title else ""
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 📖 **page {start}**{suffix}!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class JoinAudioPctModal(SafeModal, title="Join Sprint — Audio %"):
    listening = discord.ui.TextInput(label="Listening to", placeholder="What are you listening to?")
    total_hours = discord.ui.TextInput(label="Total audiobook length (hh:mm or hours)", placeholder="e.g. 12 or 12:30")
    start_pct = discord.ui.TextInput(label="Starting percentage", placeholder="e.g. 24")

    def __init__(self, cog, prefill_title: str | None = None, prefill_total_mins: int | None = None):
        super().__init__()
        self.cog = cog
        if prefill_title:
            self.listening.default = prefill_title
        if prefill_total_mins:
            self.total_hours.default = str(round(prefill_total_mins / 60, 2))

    @staticmethod
    def _parse_total_minutes(raw: str) -> int | None:
        raw = raw.strip()
        try:
            if ":" in raw:
                h, m = raw.split(":", 1)
                return round(float(h) * 60 + float(m))
            return round(float(raw) * 60)
        except ValueError:
            return None

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
        total_mins = self._parse_total_minutes(self.total_hours.value)
        if not total_mins:
            await interaction.response.send_message(
                "Please enter the total audiobook length as hours (e.g. `12`) or `hh:mm` (e.g. `12:30`).",
                ephemeral=True
            )
            return
        uid = interaction.user.id
        joined_at = datetime.utcnow()
        # Persist before touching memory (see JoinPagesModal) so a failed save leaves no ghost entry.
        await interaction.client.db.save_sprint_participant(guild_id, uid, "audio_pct", title, str(pct), joined_at=joined_at)
        if title:
            await interaction.client.db.save_sprint_book_meta(uid, guild_id, title, total_minutes=total_mins)
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        sprint["participants"][uid] = {
            "type": "audio_pct", "title": title, "start": pct,
            "end": None, "joined_at": joined_at, "gg_awarded": False,
            "total_minutes": total_mins, "segments": [],
        }
        start_str = f"{pct:.1f}%".rstrip("0").rstrip(".")
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} joined at 🎧 **{start_str}** — listening to **{title}**!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class UpdatePagesModal(SafeModal, title="Update Progress — Pages"):
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
        data = sprint["participants"][uid]
        if data["type"] != "pages":
            await interaction.response.send_message("You joined with audio — use the same update button.", ephemeral=True)
            return
        try:
            page = int(self.current_page.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        data["end"] = page
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(page))
        gained = max(0, page - data["start"])
        await interaction.response.send_message(
            _progress_message(interaction.client, interaction.user, gained), ephemeral=False
        )


class UpdateAudioPctModal(SafeModal, title="Update Progress — Audio %"):
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
        data = sprint["participants"][uid]
        if data["type"] != "audio_pct":
            await interaction.response.send_message("You joined with pages — use the same update button.", ephemeral=True)
            return
        try:
            pct = float(self.current_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        data["end"] = pct
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(pct))
        gained_pages = max(0, _leg_pages(data, pct))
        await interaction.response.send_message(
            _progress_message(interaction.client, interaction.user, gained_pages, extra=f"({pct:.1f}%)"),
            ephemeral=False
        )


class LogPagesModal(SafeModal, title="Log Sprint — Pages"):
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
            await interaction.response.send_message("You didn't join this sprint!", ephemeral=True)
            return
        try:
            end = int(self.end_page.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        data = sprint["participants"][uid]
        data["end"] = end
        data["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end))
        self.cog._check_all_logged(guild_id)
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        _, total_pages = _combined_title_and_pages(data, end)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 📖 **page {end}** (+{total_pages} pages total) — "
            f"**{SPRINT_GG} GG** earned!",
            ephemeral=False
        )
        await self.cog._update_end_embed(interaction.guild)


class LogAudioPctModal(SafeModal, title="Log Sprint — Audio %"):
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
        try:
            end_pct = float(self.end_pct.value.replace("%", "").strip())
        except ValueError:
            await interaction.response.send_message("Please enter a valid percentage.", ephemeral=True)
            return
        data = sprint["participants"][uid]
        data["end"] = end_pct
        data["gg_awarded"] = True
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end_pct))
        await _award_sprint_gg(interaction.client, interaction.guild, uid, SPRINT_GG)
        self.cog._check_all_logged(guild_id)
        _, total_pages = _combined_title_and_pages(data, end_pct)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} logged 🎧 **{end_pct:.1f}%** (+{total_pages} pages total) — "
            f"**{SPRINT_GG} GG** earned!",
            ephemeral=False
        )
        await self.cog._update_end_embed(interaction.guild)


# ── Switch Books ─────────────────────────────────────────────────────────────

class SwitchBookOldModal(SafeModal):
    def __init__(self, cog, ptype: str):
        super().__init__(title="Switch Books — Current Book's Progress")
        self.cog = cog
        self.ptype = ptype
        label = "Current page" if ptype == "pages" else "Current percentage"
        placeholder = "e.g. 87" if ptype == "pages" else "e.g. 52"
        self.value_input = discord.ui.TextInput(label=label, placeholder=placeholder)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint or uid not in sprint["participants"]:
            await interaction.response.send_message("You're not in this sprint.", ephemeral=True)
            return
        data = sprint["participants"][uid]
        try:
            end_val = int(self.value_input.value) if self.ptype == "pages" else float(self.value_input.value.replace("%", ""))
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        gained = max(0, _leg_pages(data, end_val))
        old_title = data.get("title") or "Untitled"
        data.setdefault("segments", []).append((old_title, gained))
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end_val))
        await interaction.client.db.update_sprint_participant_segments(
            guild_id, uid, json.dumps(data["segments"])
        )
        await interaction.response.send_message(
            f"{_progress_message(interaction.client, interaction.user, gained)}\n"
            f"📚 **{old_title}** locked in at +{gained} pages. Ready for your next book?",
            view=SwitchBookContinueView(self.cog, self.ptype),
            ephemeral=False
        )


class SwitchBookContinueView(SafeView):
    def __init__(self, cog, ptype: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.ptype = ptype

    @discord.ui.button(label="📚 Enter New Book", style=discord.ButtonStyle.success)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SwitchBookNewModal(self.cog, self.ptype))
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass


class SwitchBookNewModal(SafeModal):
    def __init__(self, cog, ptype: str):
        super().__init__(title="Switch Books — New Book")
        self.cog = cog
        self.ptype = ptype
        self.title_input = discord.ui.TextInput(label="New book title", placeholder="What are you starting?")
        self.add_item(self.title_input)
        if ptype == "pages":
            self.start_input = discord.ui.TextInput(label="Starting page", placeholder="e.g. 0", default="0")
            self.add_item(self.start_input)
        else:
            self.start_input = discord.ui.TextInput(label="Starting percentage", placeholder="e.g. 0", default="0")
            self.total_input = discord.ui.TextInput(
                label="Total audiobook length (hh:mm or hours)", placeholder="e.g. 12 or 12:30"
            )
            self.add_item(self.start_input)
            self.add_item(self.total_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        uid = interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint or uid not in sprint["participants"]:
            await interaction.response.send_message("You're not in this sprint.", ephemeral=True)
            return
        data = sprint["participants"][uid]
        title = self.title_input.value.strip()
        total_mins = None
        try:
            if self.ptype == "pages":
                start = int(self.start_input.value)
            else:
                start = float(self.start_input.value.replace("%", ""))
                total_mins = JoinAudioPctModal._parse_total_minutes(self.total_input.value)
                if not total_mins:
                    await interaction.response.send_message(
                        "Please enter a valid total length as hours or `hh:mm`.", ephemeral=True
                    )
                    return
        except ValueError:
            await interaction.response.send_message("Please enter valid numbers.", ephemeral=True)
            return
        # Persist before touching memory so a failed save doesn't leave the two out of sync.
        await interaction.client.db.save_sprint_participant(
            guild_id, uid, self.ptype, title, str(start), joined_at=data.get("joined_at")
        )
        if total_mins:
            data["total_minutes"] = total_mins
        data["title"] = title
        data["start"] = start
        data["end"] = None
        await interaction.response.send_message(
            f"📖 {interaction.user.mention} switched to **{title}** — tracking continues!", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


# ── Views ─────────────────────────────────────────────────────────────────────

class BookSelectView(SafeView):
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
            else:
                total_mins = meta.get("total_minutes") if meta else None
                modal = JoinAudioPctModal(
                    self.cog,
                    prefill_title=chosen if chosen != "__new__" else None,
                    prefill_total_mins=total_mins
                )
            await interaction.response.send_modal(modal)
        return callback


class JoinTypeView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.select(
        placeholder="How are you tracking?",
        options=[
            discord.SelectOption(label="Pages", emoji="📖", value="pages"),
            discord.SelectOption(label="Audio %", emoji="🎧", value="audio_pct"),
        ]
    )
    async def type_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        ptype = select.values[0]
        titles = await interaction.client.db.get_recent_sprint_titles(interaction.guild.id, interaction.user.id, limit=3)
        if titles:
            view = BookSelectView(self.cog, [r["title"] for r in titles], ptype)
            await interaction.response.edit_message(content="📚 Pick your book:", view=view)
        else:
            modal_cls = JoinPagesModal if ptype == "pages" else JoinAudioPctModal
            await interaction.response.send_modal(modal_cls(self.cog))


class ResumeOrFreshView(SafeView):
    """Shown when a paused (left) entry already exists for this participant."""
    def __init__(self, cog):
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="▶️ Resume (same book)", style=discord.ButtonStyle.success)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id, uid = interaction.guild.id, interaction.user.id
        await interaction.client.db.resume_sprint_participant(guild_id, uid)
        sprint = active_sprints.get(guild_id)
        if sprint and uid in sprint["participants"]:
            sprint["participants"][uid]["resumed_at"] = datetime.utcnow()
            sprint["participants"][uid]["left"] = False
        await interaction.response.edit_message(content="▶️ Resumed — clock's running again.", view=None)
        await interaction.channel.send(f"▶️ {interaction.user.mention} has resumed the sprint!")
        await self.cog._update_join_embed(interaction.guild)

    @discord.ui.button(label="📚 Start Fresh (new book)", style=discord.ButtonStyle.secondary)
    async def fresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        sprint = active_sprints.get(guild_id)
        if sprint:
            sprint["participants"].pop(interaction.user.id, None)
        await interaction.response.edit_message(
            content="How would you like to track your sprint?", view=JoinTypeView(self.cog)
        )


class JoinSprintView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📖 Join Sprint", style=discord.ButtonStyle.success, custom_id="sprint_join_btn", row=0)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        sprint = active_sprints.get(guild_id)
        existing = sprint["participants"].get(interaction.user.id) if sprint else None
        if existing and existing.get("left"):
            await interaction.response.send_message(
                "You paused this sprint earlier — resume where you left off, or start fresh with a new book?",
                view=ResumeOrFreshView(self.cog), ephemeral=True
            )
            return
        if existing:
            await interaction.response.send_message(
                "❌ You're already in this sprint — use **Update Progress** or **Switch Books** instead.",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            "How would you like to track your sprint?", view=JoinTypeView(self.cog), ephemeral=True
        )

    @discord.ui.button(label="🔄 Update Progress", style=discord.ButtonStyle.success, custom_id="sprint_update_btn", row=0)
    async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ptype = _lookup_participant_type(self.cog, interaction.guild.id, interaction.user.id)
        if not ptype:
            row = await interaction.client.db._fetchone(
                "SELECT type FROM active_sprint_participants WHERE guild_id=%s AND user_id=%s",
                interaction.guild.id, interaction.user.id
            )
            ptype = row["type"] if row else None
        if not ptype:
            await interaction.response.send_message(
                "❌ You haven't joined this sprint yet — pick a join type first!", ephemeral=True
            )
            return
        modal_cls = UpdatePagesModal if ptype == "pages" else UpdateAudioPctModal
        await interaction.response.send_modal(modal_cls(self.cog))

    @discord.ui.button(label="📚 Switch Books", style=discord.ButtonStyle.success, custom_id="sprint_switch_btn", row=1)
    async def switch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ptype = _lookup_participant_type(self.cog, interaction.guild.id, interaction.user.id)
        if not ptype:
            row = await interaction.client.db._fetchone(
                "SELECT type FROM active_sprint_participants WHERE guild_id=%s AND user_id=%s",
                interaction.guild.id, interaction.user.id
            )
            ptype = row["type"] if row else None
        if not ptype:
            await interaction.response.send_message(
                "❌ You haven't joined this sprint yet.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SwitchBookOldModal(self.cog, ptype))

    @discord.ui.button(label="🚪 Leave Sprint", style=discord.ButtonStyle.danger, custom_id="sprint_leave_btn", row=1)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id, uid = interaction.guild.id, interaction.user.id
        sprint = active_sprints.get(guild_id)
        if not sprint or uid not in sprint["participants"]:
            await interaction.response.send_message("You haven't joined this sprint.", ephemeral=True)
            return
        if sprint["participants"][uid].get("left"):
            await interaction.response.send_message("You've already left — hit Join to resume.", ephemeral=True)
            return
        sprint["participants"][uid]["left"] = True
        await interaction.client.db.leave_sprint_participant(guild_id, uid)
        await interaction.response.send_message(
            f"⏸️ {interaction.user.mention} has left the sprint — the clock's paused. "
            f"Hit **Join Sprint** any time to resume.", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class LogProgressView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📝 Log Sprint", style=discord.ButtonStyle.success, custom_id="sprint_log_btn", row=0)
    async def log_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ptype = _lookup_participant_type(self.cog, interaction.guild.id, interaction.user.id)
        if not ptype:
            row = await interaction.client.db._fetchone(
                "SELECT type FROM active_sprint_participants WHERE guild_id=%s AND user_id=%s",
                interaction.guild.id, interaction.user.id
            )
            ptype = row["type"] if row else None
        if not ptype:
            await interaction.response.send_message(
                "❌ You don't have a sprint entry to log — you may not have joined this sprint.", ephemeral=True
            )
            return
        modal_cls = LogPagesModal if ptype == "pages" else LogAudioPctModal
        await interaction.response.send_modal(modal_cls(self.cog))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Sprints(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._logging_sprints: dict[int, dict] = {}
        self._logging_tasks: dict[int, asyncio.Task] = {}
        self._end_message: dict[int, tuple[int, int]] = {}  # guild_id -> (channel_id, message_id)

    def _check_all_logged(self, guild_id: int):
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
                    "segments": _parse_segments(p.get("segments")),
                    "left": p.get("resumed_at") is None,
                }
            sprint = {
                "host": row["host_id"], "channel": row["channel_id"], "duration": row["duration_minutes"],
                "started_at": row["start_time"], "start_time": start_time, "end_time": end_time,
                "role_id": row["role_id"], "participants": participants,
                "join_message_id": row.get("join_message_id"), "started": now >= start_time,
            }
            active_sprints[guild.id] = sprint
            remaining = (end_time - now).total_seconds()
            self.bot.loop.create_task(self._wait_and_finish(guild, max(remaining, 0)))

    async def _wait_and_finish(self, guild, seconds):
        await asyncio.sleep(seconds)
        await self._finish_sprint(guild)

    # ── Roster embed (creation + live) ────────────────────────────────────────

    _FIELD_LIMIT = 1000

    def _build_sprint_embeds(self, guild: discord.Guild, start_time, end_time, host_name, duration) -> list[discord.Embed]:
        sprint = active_sprints.get(guild.id)
        participants = sprint["participants"] if sprint else {}

        lines = [
            f"{_emoji(self.bot, 'announcement')} **__The Stacks Go Silent…__**",
            "",
            f"{_emoji(self.bot, 'litcandle')} **Start Time:** {discord.utils.format_dt(start_time, 'F')} "
            f"*({discord.utils.format_dt(start_time, 'R')})*",
            f"{_emoji(self.bot, 'snuffedcandle')} **End Time:** {discord.utils.format_dt(end_time, 'F')} "
            f"*({discord.utils.format_dt(end_time, 'R')})*",
            f"{_emoji(self.bot, 'hourglass')} **Duration:** {_format_duration(duration)}",
            f"{_emoji(self.bot, 'griffinlurk')} **Host:** {host_name}",
        ]
        main_embed = discord.Embed(description="\n".join(lines), color=RACE_COLOR)

        server_icon = guild.icon.url if guild.icon else None
        if server_icon:
            main_embed.set_thumbnail(url=server_icon)

        header = f"{_emoji(self.bot, 'blankbook')} Sprint Patrons:"
        if not participants:
            main_embed.add_field(name=header, value="No participants yet", inline=False)
            return [main_embed]

        p_lines = []
        for uid, data in participants.items():
            if data.get("left"):
                continue
            title = data.get("title") or ""
            suffix = f" of **{title}**" if title else ""
            bookmark = _emoji(self.bot, "bookmark")
            if data["type"] == "audio_pct":
                p_lines.append(f"{bookmark} <@{uid}> - {data['start']:.1f}%{suffix}")
            else:
                p_lines.append(f"{bookmark} <@{uid}> - page {data['start']}{suffix}")

        first, chunk = True, ""
        for line in p_lines:
            candidate = f"{chunk}\n{line}" if chunk else line
            if len(candidate) > self._FIELD_LIMIT:
                main_embed.add_field(name=header if first else "​", value=chunk, inline=False)
                first, chunk = False, line
            else:
                chunk = candidate
        if chunk:
            main_embed.add_field(name=header if first else "​", value=chunk, inline=False)
        elif not p_lines:
            main_embed.add_field(name=header, value="No participants yet", inline=False)

        return [main_embed]

    async def _update_join_embed(self, guild):
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
            embeds = self._build_sprint_embeds(guild, start_time, end_time, host_name, sprint["duration"])
            await msg.edit(embeds=embeds)
        except Exception:
            print(f"_update_join_embed failed for guild {guild.id}:")
            traceback.print_exc()

    # ── End-of-sprint checklist ───────────────────────────────────────────────

    def _build_end_embed(self, guild_id: int, deadline) -> discord.Embed:
        sprint = self._logging_sprints.get(guild_id, {})
        participants = sprint.get("participants", {})
        lines = []
        ink = _emoji(self.bot, "inkandquill")
        stamp = _emoji(self.bot, "librarystamp")
        for uid, data in participants.items():
            mark = f" {stamp}" if data.get("end") is not None else ""
            lines.append(f"{ink} <@{uid}>{mark}")
        embed = discord.Embed(
            description=(
                f"{_emoji(self.bot, 'snuffedcandle')} **__Focus Time Ended!__**\n\n"
                f"{_emoji(self.bot, 'griffinsleepy')} Griffin snuffs out the candles and demands all patrons "
                f"close tomes and report pages immediately!\n\n"
                f"Patrons have {LOG_WINDOW_MINUTES} minutes to update progress, "
                f"until {discord.utils.format_dt(deadline, 'R')}."
            ),
            color=RACE_COLOR
        )
        embed.add_field(
            name=f"{_emoji(self.bot, 'blankbook')} Sprint Patrons:",
            value="\n".join(lines) if lines else "No participants.",
            inline=False
        )
        return embed

    async def _update_end_embed(self, guild: discord.Guild):
        ref = self._end_message.get(guild.id)
        if not ref:
            return
        sprint = self._logging_sprints.get(guild.id)
        deadline = sprint.get("log_deadline") if sprint else None
        if not deadline:
            return
        channel_id, message_id = ref
        channel = await _get_channel(guild, channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=self._build_end_embed(guild.id, deadline))
        except Exception:
            print(f"_update_end_embed failed for guild {guild.id}:")
            traceback.print_exc()

    # ── /race start ───────────────────────────────────────────────────────────

    race = app_commands.Group(name="race", description="Sprint race commands")

    @race.command(name="start", description="Start a reading/writing sprint!")
    @app_commands.describe(
        duration_minutes="How long the sprint lasts",
        countdown_seconds="Join window before the sprint begins (default 3 minutes)"
    )
    @app_commands.choices(duration_minutes=[
        app_commands.Choice(name="30 minutes", value=30),
        app_commands.Choice(name="1 hour", value=60),
        app_commands.Choice(name="1 hour 30 minutes", value=90),
        app_commands.Choice(name="2 hours", value=120),
    ])
    @app_commands.choices(countdown_seconds=[
        app_commands.Choice(name="3 minutes", value=180),
        app_commands.Choice(name="4 minutes", value=240),
        app_commands.Choice(name="5 minutes", value=300),
    ])
    async def sprint_start(self, interaction: discord.Interaction,
                            duration_minutes: app_commands.Choice[int],
                            countdown_seconds: app_commands.Choice[int] = None):
        guild_id = interaction.guild.id
        duration_minutes = duration_minutes.value
        countdown_seconds = countdown_seconds.value if countdown_seconds else 180

        settings = await self.bot.db.get_guild_config(guild_id)
        sprint_channel_id = settings.get("sprint_channel_id")
        if sprint_channel_id and interaction.channel.id != sprint_channel_id:
            await interaction.response.send_message(
                f"❌ Sprints can only be started in <#{sprint_channel_id}>.", ephemeral=True
            )
            return

        manager_role_id = settings.get("sprint_manager_role_id")
        griffin_manager_role_id = settings.get("griffin_manager_role_id")
        if manager_role_id or griffin_manager_role_id:
            user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
            has_role = manager_role_id in user_role_ids or griffin_manager_role_id in user_role_ids
            if not has_role and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "❌ You don't have permission to start sprints.", ephemeral=True
                )
                return

        if guild_id in active_sprints:
            await interaction.response.send_message("A sprint is already running!", ephemeral=True)
            return

        role_id = settings.get("sprint_role_id")
        role_mention = f"<@&{role_id}>" if role_id else ""

        now = discord.utils.utcnow()
        start_time = now + timedelta(seconds=countdown_seconds)
        end_time = start_time + timedelta(minutes=duration_minutes)

        active_sprints[guild_id] = {
            "host": interaction.user.id, "channel": interaction.channel.id, "duration": duration_minutes,
            "started_at": now, "start_time": start_time, "end_time": end_time, "role_id": role_id,
            "participants": {}, "join_message_id": None, "started": False,
        }

        embeds = self._build_sprint_embeds(interaction.guild, start_time, end_time, interaction.user.display_name, duration_minutes)
        view = JoinSprintView(self)

        await interaction.response.send_message(
            content=role_mention if role_mention else None,
            embeds=embeds, view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        msg = await interaction.original_response()
        active_sprints[guild_id]["join_message_id"] = msg.id

        await self.bot.db.save_active_sprint(guild_id, interaction.user.id, interaction.channel.id, duration_minutes, now, end_time, role_id)
        await self.bot.db.update_sprint_message_id(guild_id, msg.id)

        await asyncio.sleep(countdown_seconds)
        if guild_id not in active_sprints:
            return

        active_sprints[guild_id]["started"] = True
        channel = await _get_channel(interaction.guild, interaction.channel.id)
        if channel:
            participant_ids = [uid for uid, d in active_sprints.get(guild_id, {}).get("participants", {}).items() if not d.get("left")]
            participant_mentions = " ".join(f"<@{uid}>" for uid in participant_ids) if participant_ids else None
            start_embed = discord.Embed(
                description=(
                    f"{_emoji(self.bot, 'litcandle')} **__Focus Time Started!__**\n\n"
                    f"{_emoji(self.bot, 'griffinreading')} Griffin has lit the candle and commands all patrons "
                    f"to open their tomes, focus time has begun!\n\n"
                    f"{_emoji(self.bot, 'hourglass')} The sprint ends at "
                    f"{discord.utils.format_dt(end_time, 'F')} ({discord.utils.format_dt(end_time, 'R')})."
                ),
                color=RACE_COLOR
            )
            await channel.send(
                content=participant_mentions, embed=start_embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )

        await asyncio.sleep(duration_minutes * 60)
        await self._finish_sprint(interaction.guild)

    # ── /race cancel ──────────────────────────────────────────────────────────

    @race.command(name="cancel", description="Force-cancel the current sprint (running or awaiting logs).")
    async def sprint_cancel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = await self.bot.db.get_guild_config(guild_id)
        manager_role_id = settings.get("sprint_manager_role_id")
        griffin_manager_role_id = settings.get("griffin_manager_role_id")
        if manager_role_id or griffin_manager_role_id:
            user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
            has_role = manager_role_id in user_role_ids or griffin_manager_role_id in user_role_ids
            if not has_role and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "❌ You don't have permission to cancel sprints.", ephemeral=True
                )
                return

        cancelled_something = False

        if guild_id in active_sprints:
            active_sprints.pop(guild_id, None)
            cancelled_something = True

        if guild_id in self._logging_sprints:
            self._logging_sprints.pop(guild_id, None)
            self._end_message.pop(guild_id, None)
            task = self._logging_tasks.pop(guild_id, None)
            if task and not task.done():
                task.cancel()
            cancelled_something = True

        await self.bot.db.delete_active_sprint(guild_id)

        if cancelled_something:
            await interaction.response.send_message("🛑 The current sprint has been cancelled.")
        else:
            await interaction.response.send_message(
                "There's no sprint running right now, but I cleared any leftover state just in case.",
                ephemeral=True
            )

    # ── /race edit ────────────────────────────────────────────────────────────

    @race.command(name="edit", description="Edit or delete one of your sprint logs")
    async def sprint_edit(self, interaction: discord.Interaction):
        logs = await self.bot.db.get_sprint_logs(interaction.guild.id, interaction.user.id, limit=10)
        if not logs:
            await interaction.response.send_message("You don't have any logged sprints yet.", ephemeral=True)
            return
        view = SprintLogSelectView(self.bot, logs, interaction.guild.id)
        await interaction.response.send_message("Select a sprint log to edit or delete:", view=view, ephemeral=True)

    # ── /race stats ───────────────────────────────────────────────────────────

    @race.command(name="stats", description="View sprint stats for yourself or another member")
    @app_commands.describe(member="Whose stats to view (defaults to you)")
    async def sprint_stats(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        member = member or interaction.user
        stats = await self.bot.db.get_sprint_stats(interaction.guild.id, member.id)
        gg_earned = (stats["sprints_completed"] or 0) * SPRINT_GG
        pages = stats["total_pages"] or 0
        unlogged = int(stats["unlogged_sprints"] or 0)

        embed = discord.Embed(title=f"📊 Sprint Stats — {member.display_name}", color=RACE_COLOR)
        embed.add_field(name="Sprints Completed", value=str(stats["sprints_completed"] or 0))
        if pages:
            embed.add_field(name="📖 Pages Read", value=f"{pages:,}")
        embed.add_field(name="GG Earned from Sprints", value=f"{gg_earned} GG")
        if unlogged:
            embed.add_field(name="🫣 Unlogged Sprints", value=str(unlogged))
        await interaction.followup.send(embed=embed)

    # ── /race leaderboard ────────────────────────────────────────────────────

    @race.command(name="leaderboard", description="View the sprint leaderboard")
    async def sprint_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.db.get_sprint_leaderboard(interaction.guild.id, limit=1000)
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
            gg = sprints * SPRINT_GG
            lines.append(
                f"**{ordinals[i]} - {name.upper()}**\n"
                f"{sprints} Sprint{'s' if sprints != 1 else ''} | "
                f"Total Pages Logged: {total_pages:,} · Total Sprint GG Earned: {gg:,} GG"
            )
        embeds = _paginate_lines(lines, "🏆 Sprint Leaderboard")
        view = EmbedPaginatorView(embeds) if len(embeds) > 1 else None
        await interaction.followup.send(embed=embeds[0], view=view)

    # ── Internal: finish sprint ──────────────────────────────────────────────

    async def _finish_sprint(self, guild: discord.Guild):
        guild_id = guild.id
        sprint = active_sprints.pop(guild_id, None)
        if not sprint:
            return

        for p in await self.bot.db.get_sprint_participants(guild_id):
            uid = p["user_id"]
            if uid not in sprint["participants"]:
                sprint["participants"][uid] = {
                    "type": p["type"], "title": p["title"],
                    "start": _deserialize_start(p["type"], p["start_val"]),
                    "end": _deserialize_end(p["type"], p["end_val"]),
                    "segments": _parse_segments(p.get("segments")),
                }

        deadline = discord.utils.utcnow() + timedelta(minutes=LOG_WINDOW_MINUTES)
        sprint["log_deadline"] = deadline
        self._logging_sprints[guild_id] = sprint

        channel = await _get_channel(guild, sprint["channel"])
        if not channel:
            await self.bot.db.delete_active_sprint(guild_id)
            return

        view = LogProgressView(self)
        end_embed = self._build_end_embed(guild_id, deadline)
        # Mentions inside an embed render but never notify — the ping has to be message content.
        ping_ids = [uid for uid, d in sprint["participants"].items() if not d.get("left")]
        ping_mentions = " ".join(f"<@{uid}>" for uid in ping_ids) or None
        end_msg = await channel.send(
            content=ping_mentions, embed=end_embed, view=view,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        self._end_message[guild_id] = (channel.id, end_msg.id)

        task = asyncio.ensure_future(asyncio.sleep(LOG_WINDOW_MINUTES * 60))
        self._logging_tasks[guild_id] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._logging_tasks.pop(guild_id, None)

        self._logging_sprints.pop(guild_id, None)
        self._end_message.pop(guild_id, None)

        sprint_end_time = discord.utils.utcnow()

        final_participants = {}
        for p in await self.bot.db.get_sprint_participants(guild_id):
            uid = p["user_id"]
            final_participants[uid] = {
                "type": p["type"], "title": p["title"],
                "start": _deserialize_start(p["type"], p["start_val"]),
                "end": _deserialize_end(p["type"], p["end_val"]),
                "joined_at": p.get("joined_at"),
                "active_seconds": p.get("active_seconds") or 0,
                "resumed_at": p.get("resumed_at"),
                "segments": _parse_segments(p.get("segments")),
            }
        for uid, data in sprint["participants"].items():
            if data["end"] is not None:
                final_participants.setdefault(uid, data)
                final_participants[uid]["end"] = data["end"]
                final_participants[uid]["segments"] = data.get("segments", [])
                if data.get("joined_at") and not final_participants[uid].get("joined_at"):
                    final_participants[uid]["joined_at"] = data["joined_at"]
                if data.get("gg_awarded"):
                    final_participants[uid]["gg_awarded"] = True

        await self.bot.db.delete_active_sprint(guild_id)

        silent_gg_uids = []
        for uid, data in final_participants.items():
            if data["end"] is not None and not data.get("gg_awarded"):
                await _award_sprint_gg(self.bot, guild, uid, SPRINT_GG)
                data["gg_awarded"] = True
                silent_gg_uids.append(uid)

        def _duration_str(active_seconds, resumed_at, end_time):
            total_seconds = active_seconds or 0
            if resumed_at:
                from datetime import timezone as _tz
                r = resumed_at.replace(tzinfo=_tz.utc) if resumed_at.tzinfo is None else resumed_at
                e = end_time.replace(tzinfo=_tz.utc) if end_time.tzinfo is None else end_time
                total_seconds += max(0, (e - r).total_seconds())
            mins = max(0, int(total_seconds / 60))
            return f"{mins}m" if mins < 60 else f"{mins // 60}h {mins % 60}m"

        results = []
        for uid, data in final_participants.items():
            if data["end"] is None:
                continue
            member = guild.get_member(uid)
            name = _safe_name(member, uid)
            duration = _duration_str(data.get("active_seconds"), data.get("resumed_at"), sprint_end_time)
            title, pages = _combined_title_and_pages(data, data["end"])
            results.append((name, pages, title, duration))

        results.sort(key=lambda r: r[1], reverse=True)

        librarian_role_id = (await self.bot.db.get_guild_config(guild_id)).get("sprint_manager_role_id")
        librarian_mention = f"<@&{librarian_role_id}> " if librarian_role_id else ""

        if results:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, (name, pages, title, duration) in enumerate(results):
                suffix = f" — *{title}*" if title else ""
                prefix = medals[i] if i < 3 else f"`#{i + 1}`"
                dur = f" ({duration})" if duration else ""
                lines.append(f"{prefix} **{name}** — +{pages} pages{suffix}{dur}")
            embeds = _paginate_lines(lines, "🏁 Griffin has recorded the sprint results…")
            for e in embeds:
                e.set_footer(text=f"Each participant earned {SPRINT_GG} GG!")
            view = EmbedPaginatorView(embeds) if len(embeds) > 1 else None
            await channel.send(content=librarian_mention or None, embed=embeds[0], view=view)
        else:
            await channel.send(f"{librarian_mention}No one logged their results. 📭")

        if silent_gg_uids:
            mentions = " ".join(f"<@{uid}>" for uid in silent_gg_uids)
            await channel.send(
                f"{mentions} — you updated your progress during the sprint and earned **{SPRINT_GG} GG**! 📚"
            )

        sprint_id = await self.bot.db.create_sprint(
            guild_id, sprint["channel"], sprint["host"], sprint["started_at"], sprint_end_time, sprint["duration"]
        )
        for uid, data in final_participants.items():
            if data["end"] is not None:
                title, total_pages = _combined_title_and_pages(data, data["end"])
                start_val, end_val = 0, total_pages
            else:
                title = data.get("title")
                start_val, end_val = 0, None
            await self.bot.db.add_sprint_participant(
                sprint_id, uid, start_val, end_val, "pages",
                joined_at=data.get("joined_at"), sprint_end_time=sprint_end_time, title=title,
            )


# ── Sprint Edit Views ─────────────────────────────────────────────────────────

class SprintEditModal(SafeModal):
    def __init__(self, bot, sprint_id: int, title: str, start: int, end: int, log_type: str, guild_id: int):
        super().__init__(title="Edit Sprint Log")
        self.bot = bot
        self.sprint_id = sprint_id
        self.log_type = log_type
        self.guild_id = guild_id

        self.title_input = discord.ui.TextInput(label="Title", default=title, required=False, max_length=100)
        self.start_input = discord.ui.TextInput(label="Start (pages)", default=str(start), required=True, max_length=6)
        self.end_input = discord.ui.TextInput(label="End (pages)", default=str(end), required=True, max_length=6)
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


class SprintDeleteConfirmView(SafeView):
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
        await interaction.response.edit_message(content=f"🗑️ Log deleted and **{SPRINT_GG} GG** deducted.", view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()


class SprintLogSelectView(SafeView):
    def __init__(self, bot, logs: list, guild_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.logs = {str(row["sprint_id"]): row for row in logs}

        options = []
        for row in logs:
            date = row["sprint_end_time"].strftime("%b %d") if row["sprint_end_time"] else "?"
            progress = (row["end_count"] or 0) - (row["start_count"] or 0)
            label = (row["title"] or "Untitled")[:80]
            desc = f"{date} · {row['start_count']} → {row['end_count']} (+{progress} pg)"
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
            row["start_count"] or 0, row["end_count"] or 0, row["type"], self.guild_id
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
