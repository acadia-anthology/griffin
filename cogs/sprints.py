import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import timezone, timedelta, datetime
import asyncio
import json
import math
import re
import traceback
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from cogs.economy import _award_gg
from utils.emojis import emoji as _emoji, partial as _partial_emoji

LOG_WINDOW_MINUTES = 7
TEST_SPRINT_MINUTES = 5         # "Mod Test": sprint managers only, never awards (or deducts) GG
SCHEDULED_JOIN_MINUTES = 5   # joining window before a scheduled sprint's focus time
RACE_COLOR = discord.Color(0x26878D)

active_sprints: dict[int, dict] = {}

# LEGACY: audiobooks used to be converted from their length with these tiers. New joins give the book's
# total pages instead; this only keeps sprints that were already running at the switch-over correct.
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


async def _sprint_gg(db, guild_id: int) -> int:
    """GG awarded per completed sprint in this guild (0 = off). Set with /gg setrate sprint."""
    return (await db.get_guild_config(guild_id))["sprint_gg"]


async def _gg_for_duration(db, guild_id: int, duration) -> int:
    """GG for a sprint of this length: nothing for the Mod Test length, the guild's rate otherwise."""
    if duration == TEST_SPRINT_MINUTES:
        return 0
    return await _sprint_gg(db, guild_id)


async def _award_sprint_gg(bot, guild: discord.Guild, user_id: int, amount: int):
    if amount <= 0:
        return
    member = guild.get_member(user_id)
    if member:
        await _award_gg(bot.db, guild, member, amount)
    else:
        await bot.db.add_gg(guild.id, user_id, amount)


_ZWSP = "​"


def _add_chunked_field(embed: discord.Embed, header: str, lines: list[str], empty_text: str,
                       limit: int = 1000, budget: int = 4500) -> None:
    """Lists `lines` across as many fields as needed. Discord rejects a field over 1024 chars and an
    embed over 6000, and a rejected send/edit means the message silently never updates — so keep every
    field under `limit`, and if a huge group would blow past `budget` total, truncate with a count."""
    if not lines:
        embed.add_field(name=header, value=empty_text, inline=False)
        return
    used = 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > budget:
            lines = lines[:i] + [f"…and {len(lines) - i} more"]
            break
        used += len(line) + 1
    first, chunk = True, ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > limit:
            embed.add_field(name=header if first else _ZWSP, value=chunk, inline=False)
            first, chunk = False, line
        else:
            chunk = candidate
    if chunk:
        embed.add_field(name=header if first else _ZWSP, value=chunk, inline=False)


def _safe_name(member: discord.Member | None, user_id: int) -> str:
    return member.display_name if member else f"User {user_id}"


def _nm(member) -> str:
    """Plain display name for message text. A mention in message content pings the person, and the only
    pings we want are the sprint-created role ping and the focus start/end pings."""
    return discord.utils.escape_markdown(member.display_name)


def _uname(guild, user_id: int) -> str:
    member = guild.get_member(user_id) if guild else None
    return discord.utils.escape_markdown(_safe_name(member, user_id))


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


def _aware(dt):
    """DB timestamps are naive UTC; Discord timestamps / comparisons want aware."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _in_sprint_channel(interaction: discord.Interaction, sprint_channel_id: int) -> bool:
    """True if this is the configured channel, a thread/forum post inside it, or the configured thread itself
    (forum posts are threads, so setting a forum allows every post in it)."""
    channel = interaction.channel
    return channel.id == sprint_channel_id or getattr(channel, "parent_id", None) == sprint_channel_id


def _icon(bot, button: discord.ui.Button, name: str) -> None:
    """Gives a button one of Griffin's custom emojis (or the unicode stand-in) and drops the emoji that was
    typed into its label, so the label is just the words."""
    button.emoji = _partial_emoji(bot, name)
    label = button.label or ""
    if " " in label and not label[0].isalnum():
        button.label = label.split(" ", 1)[1]


_SHORTCODE = re.compile(r":([A-Za-z0-9_]{2,32}):")


def _expand_emoji(bot, text: str | None) -> str | None:
    """Turns :name: typed into a title/description into Griffin's own emoji, when it has one by that name."""
    if not text:
        return text
    emojis = getattr(bot, "app_emojis", {})
    return _SHORTCODE.sub(lambda m: str(emojis[m.group(1)]) if m.group(1) in emojis else m.group(0), text)


def _created_text(start_time, role_mention: str = "") -> str:
    """The line that goes with the Page Racers ping when a sprint opens for joining."""
    line = f"### A sprint has been created for {discord.utils.format_dt(start_time, 'F')}, time to focus!"
    return f"{role_mention}\n{line}" if role_mention else line


def _message_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _can_manage_sprints(interaction: discord.Interaction, settings: dict) -> bool:
    """Race librarian-role, griffin-wide manager-role, or Manage Server. If neither role has been
    configured yet, sprint management is open (that's the pre-existing behaviour)."""
    race_role = settings.get("sprint_manager_role_id")
    griffin_role = settings.get("griffin_manager_role_id")
    if not (race_role or griffin_role):
        return True
    user_roles = {r.id for r in getattr(interaction.user, "roles", [])}
    return (race_role in user_roles or griffin_role in user_roles
            or interaction.user.guild_permissions.manage_guild)


def _is_sprint_manager(interaction: discord.Interaction, settings: dict) -> bool:
    """Like _can_manage_sprints but never open-to-all: needs a configured role or Manage Server."""
    user_roles = {r.id for r in getattr(interaction.user, "roles", [])}
    return (settings.get("sprint_manager_role_id") in user_roles
            or settings.get("griffin_manager_role_id") in user_roles
            or interaction.user.guild_permissions.manage_guild)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y")


def _parse_schedule_when(date_raw: str, time_raw: str, tz: ZoneInfo):
    """Reads a typed date + clock time as being in `tz`. Returns (aware datetime, None) or (None, error)."""
    date_raw = date_raw.strip()
    d = None
    if not date_raw:                                    # no date typed -> today, in their timezone
        d = datetime.now(tz).date()
    for fmt in _DATE_FORMATS:
        if d is not None:
            break
        try:
            d = datetime.strptime(date_raw, fmt).date()
            break
        except ValueError:
            continue
    if d is None:
        try:  # month/day with no year -> this year
            d = datetime.strptime(date_raw, "%m/%d").replace(year=datetime.now(tz).year).date()
        except ValueError:
            pass
    if d is None:
        return None, "I couldn't read that date — use `YYYY-MM-DD` (e.g. `2026-10-24`) or `MM/DD/YYYY`."

    cleaned = time_raw.strip().replace(" ", "").upper()
    t = None
    formats = ["%H:%M", "%I:%M%p", "%I%p"]
    if cleaned.isdigit() and len(cleaned) == 4:        # "1430" — but never read "14" as 01:04
        formats.insert(1, "%H%M")
    for fmt in formats:
        try:
            t = datetime.strptime(cleaned, fmt).time()
            break
        except ValueError:
            continue
    if t is None:
        return None, "I couldn't read that time — use `14:30` or `2:30pm`, or a countdown like `in 1h30m`."
    return datetime.combine(d, t, tzinfo=tz), None


_RELATIVE_RE = re.compile(
    r"^(?:in|\+)?\s*"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h))?\s*"
    r"(?:(?P<m>\d+)\s*(?:minutes?|mins?|m))?$"
)
_MAX_START_AFTER = timedelta(days=30)


def _parse_start_after(raw: str):
    """Reads "in 1h30m" / "+90m" / "2 hours" as an offset from now. Returns (timedelta, None) when it is one,
    (None, None) when it's not a relative time at all (so a clock time like 2pm falls through), or
    (None, error) when it is relative but unusable. A bare number counts only with an "in"/"+" prefix."""
    text = (raw or "").strip().lower()
    if not text:
        return None, None
    m = _RELATIVE_RE.match(text)
    if not m or (m.group("h") is None and m.group("m") is None):
        bare = re.fullmatch(r"(?:in|\+)\s*(\d+)", text)      # "in 90" / "+45" -> minutes
        if not bare:
            return None, None
        delta = timedelta(minutes=int(bare.group(1)))
    else:
        if not (text.startswith(("in", "+")) or any(c.isalpha() for c in text)):
            return None, None
        delta = timedelta(hours=float(m.group("h") or 0), minutes=int(m.group("m") or 0))
    if delta < timedelta(minutes=2):
        return None, "That's too soon — pick a start at least 2 minutes from now (e.g. `in 30m`)."
    if delta > _MAX_START_AFTER:
        return None, "That's more than 30 days away — use a date and time for far-off sprints."
    return delta, None


_TZ_ALIASES = {
    "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago", "CT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver", "MT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "AKST": "America/Anchorage", "AKDT": "America/Anchorage", "HST": "Pacific/Honolulu",
    "GMT": "Europe/London", "BST": "Europe/London", "UTC": "UTC",
    "CET": "Europe/Paris", "CEST": "Europe/Paris", "EET": "Europe/Athens", "EEST": "Europe/Athens",
    "IST": "Asia/Kolkata", "JST": "Asia/Tokyo", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
    "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
}
_tz_lookup: dict[str, str] | None = None


def _resolve_timezone(raw: str):
    """Typed timezone -> a proper IANA name, or None. Accepts US/EU abbreviations (EST, PST, ...) and any
    capitalisation of an IANA name (America/New_York, america/new_york)."""
    global _tz_lookup
    text = (raw or "").strip().replace(" ", "_")
    if not text:
        return None
    if text.upper() in _TZ_ALIASES:
        return _TZ_ALIASES[text.upper()]
    if _tz_lookup is None:
        _tz_lookup = {n.lower(): n for n in available_timezones()}
    name = _tz_lookup.get(text.lower())
    if name is None:
        try:                                    # tz data present but not enumerable
            ZoneInfo(text)
            name = text
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return name


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
        f"{stamp} {_nm(member)}, Griffin has recorded sprint progress!\n"
        f"{book} +{pages_delta} pages{extra_str}, has been added to the ledger."
    )


def _leg_pages(data: dict, end_val) -> int:
    """Pages gained for the participant's current book leg, given an end value."""
    if data["type"] == "audio_pct":
        gained_pct = end_val - data["start"]
        total_pages = data.get("total_pages") or 0
        if total_pages:
            return math.floor(total_pages * gained_pct / 100 + 0.5)       # whole pages, halves round up
        total_minutes = data.get("total_minutes") or 0                      # sprints already running before
        return _audio_pct_to_pages(gained_pct, total_minutes) if total_minutes else 0   # this change shipped
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
    def __init__(self, embeds: list[discord.Embed], *, timeout=900):
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


def _paginate_lines(lines: list[str], title: str, per_page: int = 40, char_limit: int = 3500,
                    header: str | None = None) -> list[discord.Embed]:
    """One embed holds as many lines as fit under the description cap (4096); only start a new page
    when it's genuinely full. `per_page` is just a sanity ceiling, not a readability cap. `header`
    (e.g. a sprint's title) is repeated at the top of every page."""
    if header:
        char_limit -= len(header) + 2
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current and (len(current) >= per_page or current_len + len(line) + 2 > char_limit):
            pages.append(current)
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + 2    # +2 for the "\n\n" the lines are joined with
    if current:
        pages.append(current)
    if not pages:
        pages = [[]]
    return [
        discord.Embed(
            title=title,
            description=(f"{header}\n\n" if header else "") + ("\n\n".join(p) or "Nothing here yet."),
            color=RACE_COLOR,
        )
        for p in pages
    ]


# ── Modals ────────────────────────────────────────────────────────────────────

class JoinSprintModal(SafeModal):
    """The one form for every way into a book: Join, Start Fresh and Switch Books. Tracking type,
    a recent book (or a new one), where you're starting, and — for audiobooks — the book's total pages
    (remembered per book, so it's only asked once)."""

    def __init__(self, cog, recent_titles: list[str], *, switch: bool = False, default_type: str = "pages"):
        super().__init__(title="Switch Books — Next Book" if switch else "Join Sprint")
        self.cog = cog
        self.switch = switch
        self.recent = list(recent_titles)[:3]
        self.type_select = discord.ui.Select(options=[
            discord.SelectOption(label="Pages", value="pages", emoji="📖", default=default_type != "audio_pct"),
            discord.SelectOption(label="Audio %", value="audio_pct", emoji="🎧", default=default_type == "audio_pct"),
        ])
        self.add_item(discord.ui.Label(text="Tracking by", component=self.type_select))
        self.book_select = None
        if self.recent:
            self.book_select = discord.ui.Select(
                min_values=0, required=False, placeholder="Pick one of your recent books",
                options=[discord.SelectOption(label=t[:100], value=str(i)) for i, t in enumerate(self.recent)],
            )
            self.add_item(discord.ui.Label(text="Recent book", component=self.book_select))
        self.new_title = discord.ui.TextInput(
            required=False, max_length=100,
            placeholder="Only if it isn't in the list above" if self.recent else "What are you reading?",
        )
        self.add_item(discord.ui.Label(text="New book name" if self.recent else "Book name", component=self.new_title))
        self.start_input = discord.ui.TextInput(required=False, max_length=10, placeholder="e.g. 42, or 24 for 24%")
        self.add_item(discord.ui.Label(text="Start page or % (blank = where you left off)", component=self.start_input))
        self.total_pages = discord.ui.TextInput(required=False, max_length=6, placeholder="Audiobooks only, e.g. 352")
        self.add_item(discord.ui.Label(text="Book's total pages (for audio %)", component=self.total_pages))

    async def _fail(self, interaction: discord.Interaction, message: str):
        await interaction.response.send_message(f"❌ {message}", ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        guild_id, uid = interaction.guild.id, interaction.user.id
        ptype = (self.type_select.values or ["pages"])[0]
        chosen = self.recent[int(self.book_select.values[0])] if self.book_select and self.book_select.values else None
        typed = (self.new_title.value or "").strip()
        if chosen and typed:
            await self._fail(interaction, "Pick one of your recent books **or** type a new one — not both.")
            return
        title = chosen or typed or None
        if ptype == "audio_pct" and not title:
            await self._fail(interaction, "Pick a recent book or type the audiobook's name.")
            return
        meta = await db.get_sprint_book_meta(uid, guild_id, title) if title else None
        raw_start = (self.start_input.value or "").strip()
        resumed = False
        if raw_start:
            try:
                start = int(raw_start) if ptype == "pages" else float(raw_start.replace("%", "").strip())
            except ValueError:
                await self._fail(interaction, "Your starting " + ("page" if ptype == "pages" else "percentage")
                                 + " needs to be a number.")
                return
            if start < 0 or (ptype == "audio_pct" and start > 100):
                await self._fail(interaction, "That starting " + ("page" if ptype == "pages" else "percentage")
                                 + " doesn't look right.")
                return
        elif meta and meta.get("last_pos") is not None:         # blank: pick up where they left off
            was_audio = str(meta.get("last_type") or ptype).startswith("audio")
            if was_audio != (ptype == "audio_pct"):
                await self._fail(
                    interaction,
                    f"Last time you tracked **{meta['title']}** by {'audio %' if was_audio else 'pages'}. Switch "
                    f"**Tracking by** to match, or type where you're starting."
                )
                return
            start = float(meta["last_pos"]) if ptype == "audio_pct" else int(round(meta["last_pos"]))
            resumed = True
        else:
            start = 0 if ptype == "pages" else 0.0
        total_pages = None
        if ptype == "audio_pct":
            raw_total = (self.total_pages.value or "").strip()
            if raw_total:
                try:
                    total_pages = int(raw_total)
                except ValueError:
                    total_pages = 0
                if total_pages <= 0:
                    await self._fail(interaction, "The book's total pages needs to be a whole number, like `352`.")
                    return
            else:
                total_pages = (meta or {}).get("total_pages")
                if not total_pages:
                    await self._fail(
                        interaction,
                        "For audiobooks I need the book's **total pages** (e.g. `352`) so I can turn your % into "
                        "pages. I'll remember it for next time."
                    )
                    return
        start_str = f"{start:.1f}%".rstrip("0").rstrip(".") if ptype == "audio_pct" else f"page {start}"
        icon = _emoji(interaction.client, "headphones" if ptype == "audio_pct" else "blankbook")
        stamp = _emoji(interaction.client, "librarystamp")
        what = (f" — listening to **{title}**" if ptype == "audio_pct" else (f" — reading **{title}**" if title else ""))
        left_off = " *(where you left off)*" if resumed else ""

        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return

        if self.switch:
            data = sprint["participants"].get(uid)
            if not data:
                await interaction.response.send_message("You're not in this sprint.", ephemeral=True)
                return
            pending = data.get("pending_segment")
            if not pending:
                await interaction.response.send_message(
                    "Start from the **Switch Books** button first, so your current book's pages get saved.",
                    ephemeral=True
                )
                return
            # One statement saves the finished book as a segment AND starts the new one (and leaves the
            # active-time clock alone), persisted before touching memory so a failed save can't desync them.
            segments = list(data.get("segments") or []) + [pending]
            await db.switch_sprint_participant_book(
                guild_id, uid, ptype, title, str(start), json.dumps(segments),
                joined_at=data.get("joined_at"), total_pages=total_pages)
            data["segments"] = segments
            data.pop("pending_segment", None)
            data["type"], data["title"], data["start"], data["end"] = ptype, title, start, None
            data["total_pages"] = total_pages
            data.pop("total_minutes", None)
            if title:
                await db.save_sprint_book_meta(uid, guild_id, title, total_pages=total_pages)
            await interaction.response.send_message(
                f"{_emoji(interaction.client, 'bookswap')} {_nm(interaction.user)} switched to "
                f"**{title or 'a new book'}** — tracking continues!{left_off}", ephemeral=False)
            await self.cog._update_join_embed(interaction.guild)
            return

        if uid in sprint.get("participants", {}):
            await interaction.response.send_message("You've already joined this sprint!", ephemeral=True)
            return
        joined_at = datetime.utcnow()
        # Persist before touching memory: if the save fails, nothing is left behind, so the user can
        # simply retry instead of being stuck as "already joined" with no roster entry.
        carry = (sprint.get("carry") or {}).get(uid) or {}
        await db.save_sprint_participant(
            guild_id, uid, ptype, title, str(start), joined_at=joined_at, total_pages=total_pages,
            segments_json=json.dumps(carry.get("segments", [])), carry_active_seconds=carry.get("active_seconds", 0))
        if title:
            await db.save_sprint_book_meta(uid, guild_id, title, total_pages=total_pages)
        sprint = active_sprints.get(guild_id)
        if not sprint:
            await interaction.response.send_message("The sprint has already ended!", ephemeral=True)
            return
        entry = {"type": ptype, "title": title, "start": start, "end": None, "joined_at": joined_at,
                 "gg_awarded": False, "segments": list(carry.get("segments", []))}
        if total_pages:
            entry["total_pages"] = total_pages
        sprint["participants"][uid] = entry
        (sprint.get("carry") or {}).pop(uid, None)
        await interaction.response.send_message(
            f"{stamp} {_nm(interaction.user)} joined at {icon} **{start_str}**{what}!{left_off}", ephemeral=False)
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
        gg = await _gg_for_duration(interaction.client.db, guild_id, sprint["duration"])
        await _award_sprint_gg(interaction.client, interaction.guild, uid, gg)
        _, total_pages = _combined_title_and_pages(data, end)
        gg_note = f" — **{gg} GG** earned!" if gg > 0 else "!"
        await interaction.response.send_message(
            f"{_emoji(interaction.client, 'librarystamp')} {_nm(interaction.user)} logged "
            f"{_emoji(interaction.client, 'blankbook')} **page {end}** (+{total_pages} pages total){gg_note}",
            ephemeral=False
        )
        await self.cog._update_end_embed(interaction.guild)
        self.cog._check_all_logged(guild_id)         # last: the board must come after everyone's confirmation


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
        gg = await _gg_for_duration(interaction.client.db, guild_id, sprint["duration"])
        await _award_sprint_gg(interaction.client, interaction.guild, uid, gg)
        _, total_pages = _combined_title_and_pages(data, end_pct)
        gg_note = f" — **{gg} GG** earned!" if gg > 0 else "!"
        await interaction.response.send_message(
            f"{_emoji(interaction.client, 'librarystamp')} {_nm(interaction.user)} logged "
            f"{_emoji(interaction.client, 'headphones')} **{end_pct:.1f}%** (+{total_pages} pages total){gg_note}",
            ephemeral=False
        )
        await self.cog._update_end_embed(interaction.guild)
        self.cog._check_all_logged(guild_id)


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
        # Held as *pending* until the next book is entered. Committing it now would count these pages
        # twice if the switch is abandoned (the current leg still runs from its original start).
        data["pending_segment"] = (old_title, gained)
        data["end"] = end_val
        await interaction.client.db.update_sprint_participant_end(guild_id, uid, str(end_val))
        if data.get("title"):
            await interaction.client.db.set_book_progress(uid, guild_id, data["title"], self.ptype, end_val)
        await interaction.response.send_message(
            f"{_progress_message(interaction.client, interaction.user, gained)}\n"
            f"{_emoji(interaction.client, 'books')} **{old_title}** gets saved at +{gained} pages once you enter your next book — "
            f"hit the button below.",
            view=SwitchBookContinueView(self.cog, self.ptype, uid),
            ephemeral=False
        )


class SwitchBookContinueView(SafeView):
    def __init__(self, cog, ptype: str, owner_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.ptype = ptype
        self.owner_id = owner_id
        _icon(cog.bot, self.continue_btn, "books")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This book switch belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="📚 Enter New Book", style=discord.ButtonStyle.success)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._open_join_modal(interaction, switch=True, default_type=self.ptype)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass


# ── Views ─────────────────────────────────────────────────────────────────────

class ResumeOrFreshView(SafeView):
    """Shown when a paused (left) entry already exists for this participant."""
    def __init__(self, cog):
        super().__init__(timeout=60)
        self.cog = cog
        _icon(cog.bot, self.fresh_btn, "books")

    @discord.ui.button(label="▶️ Resume (same book)", style=discord.ButtonStyle.success)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id, uid = interaction.guild.id, interaction.user.id
        await interaction.client.db.resume_sprint_participant(guild_id, uid)
        sprint = active_sprints.get(guild_id)
        if sprint and uid in sprint["participants"]:
            sprint["participants"][uid]["resumed_at"] = datetime.utcnow()
            sprint["participants"][uid]["left"] = False
        await interaction.response.edit_message(content="▶️ Resumed — clock's running again.", view=None)
        await interaction.channel.send(f"{_emoji(interaction.client, 'griffinreading')} {_nm(interaction.user)} has resumed the sprint!")
        await self.cog._update_join_embed(interaction.guild)

    @discord.ui.button(label="📚 Start Fresh (new book)", style=discord.ButtonStyle.secondary)
    async def fresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        sprint = active_sprints.get(guild_id)
        uid = interaction.user.id
        if sprint:
            data = sprint["participants"].pop(uid, None)
            if data:
                # Starting fresh must not throw away the book they just left: bank its pages as a segment
                # (like Switch Books does) and keep the active time they've already put in.
                segments = list(data.get("segments") or [])
                if data.get("end") is not None:
                    gained = _leg_pages(data, data["end"])
                    if gained > 0:
                        segments.append((data.get("title") or "", gained))
                    if data.get("title"):
                        await interaction.client.db.set_book_progress(
                            uid, guild_id, data["title"], data["type"], data["end"])
                banked = 0
                for row in await interaction.client.db.get_sprint_participants(guild_id):
                    if row["user_id"] == uid:
                        banked = row.get("active_seconds") or 0
                sprint.setdefault("carry", {})[uid] = {"segments": segments, "active_seconds": banked}
        await self.cog._open_join_modal(interaction)


class JoinSprintView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        _icon(cog.bot, self.join_btn, "joinsprint")
        _icon(cog.bot, self.update_btn, "update")
        _icon(cog.bot, self.switch_btn, "books")
        _icon(cog.bot, self.leave_btn, "leavesprint")

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
        await self.cog._open_join_modal(interaction)

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
            f"{_emoji(interaction.client, 'griffinsleepy')} {_nm(interaction.user)} has left the sprint — the clock's paused. "
            f"Hit **Join Sprint** any time to resume.", ephemeral=False
        )
        await self.cog._update_join_embed(interaction.guild)


class LogProgressView(SafeView):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
        _icon(cog.bot, self.log_btn, "logpages")

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


# ── Scheduling UI ─────────────────────────────────────────────────────────────

# Common zones for the first-time picker (IANA name -> label). Anything else goes through "Other…".
_COMMON_ZONES = [
    ("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"),
    ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "Pacific"), ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"), ("America/Halifax", "Atlantic Canada"), ("America/Mexico_City", "Mexico City"),
    ("America/Sao_Paulo", "Brazil"), ("Europe/London", "UK"), ("Europe/Paris", "Central Europe"),
    ("Europe/Athens", "Eastern Europe"), ("Europe/Moscow", "Moscow"), ("Africa/Johannesburg", "South Africa"),
    ("Asia/Dubai", "Gulf"), ("Asia/Kolkata", "India"), ("Asia/Singapore", "Singapore"), ("Asia/Tokyo", "Japan"),
    ("Australia/Perth", "Perth"), ("Australia/Sydney", "Sydney"), ("Pacific/Auckland", "New Zealand"),
]


def _utc_offset_label(tz_name: str) -> str:
    off = datetime.now(ZoneInfo(tz_name)).utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


class ScheduleSprintModal(SafeModal, title="Schedule a Sprint"):
    sprint_title = discord.ui.TextInput(label="Sprint title", placeholder="e.g. Block 1/7 Sprint 2/5", max_length=80)
    blurb = discord.ui.TextInput(
        label="Description (optional)", style=discord.TextStyle.paragraph, required=False, max_length=500,
        placeholder="Anything patrons should know about this sprint",
    )
    date_input = discord.ui.TextInput(
        label="Date (YYYY-MM-DD, blank = today)", placeholder="e.g. 2026-10-24", required=False, max_length=12
    )
    time_input = discord.ui.TextInput(label="Start (14:30, 2:30pm, or in 1h30m)", placeholder="14:30  or  in 1h30m", max_length=16)

    def __init__(self, cog, tz_name: str, prefill_date: str | None = None):
        super().__init__()
        self.cog = cog
        self.tz_name = tz_name
        if prefill_date:
            self.date_input.default = prefill_date

    async def on_submit(self, interaction: discord.Interaction):
        title = self.sprint_title.value.strip()
        if not title:
            await interaction.response.send_message("❌ A sprint needs a title.", ephemeral=True)
            return
        date_text = (self.date_input.value or "").strip()
        delta, err = _parse_start_after(self.time_input.value)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return
        if delta is not None:                       # "in 1h30m": counted from now, no timezone involved
            start_utc = (discord.utils.utcnow() + delta).replace(second=0, microsecond=0)
            entered = f"in {_format_duration(int(delta.total_seconds() // 60))}"
        else:
            try:
                tz = ZoneInfo(self.tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                await interaction.response.send_message(
                    "❌ Your saved timezone isn't valid — set it again with `/race timezone`.", ephemeral=True
                )
                return
            when, err = _parse_schedule_when(date_text, self.time_input.value, tz)
            if err:
                await interaction.response.send_message(f"❌ {err}", ephemeral=True)
                return
            start_utc = when.astimezone(timezone.utc)
            entered = f"{when.strftime('%a %b %d, %H:%M')} ({self.tz_name})"
        if start_utc <= discord.utils.utcnow() + timedelta(minutes=1):
            hint = "" if date_text else " Add a date to schedule it for another day."
            await interaction.response.send_message(
                f"❌ That start time is already past (or less than a minute away).{hint}", ephemeral=True
            )
            return
        settings = await self.cog.bot.db.get_guild_config(interaction.guild.id)
        view = ScheduleDurationView(
            self.cog, interaction.user.id, interaction.channel.id, title,
            (self.blurb.value or "").strip() or None, start_utc, self.tz_name, date_text,
            allow_test=_is_sprint_manager(interaction, settings),
        )
        await interaction.response.send_message(
            f"⏱️ How long should **{title}** run?\n"
            f"Starts {discord.utils.format_dt(start_utc, 'F')} ({discord.utils.format_dt(start_utc, 'R')}) — "
            f"you entered **{entered}**.",
            view=view, ephemeral=True
        )


class TimezoneModal(SafeModal, title="Your timezone"):
    zone = discord.ui.TextInput(
        label="Timezone", placeholder="e.g. America/Toronto, Europe/Berlin, EST, PST", max_length=40
    )

    def __init__(self, cog, then_schedule: bool):
        super().__init__()
        self.cog, self.then_schedule = cog, then_schedule

    async def on_submit(self, interaction: discord.Interaction):
        name = _resolve_timezone(self.zone.value)
        if not name:
            await interaction.response.send_message(
                f"❌ I don't know a timezone called `{self.zone.value.strip()[:40]}`. Use a city-style name like "
                "`America/Toronto` or `Europe/Berlin`, or an abbreviation like `EST`.", ephemeral=True
            )
            return
        await self.cog.bot.db.set_user_timezone(interaction.user.id, name)
        text = (f"✅ Saved — I'll read your times as **{name}** ({_utc_offset_label(name)}). "
                "Change it any time with `/race timezone`.")
        if self.then_schedule:
            await interaction.response.send_message(
                text, view=ContinueScheduleView(self.cog, interaction.user.id, name), ephemeral=True
            )
        else:
            await interaction.response.send_message(text, ephemeral=True)


class ContinueScheduleView(SafeView):
    """Modals can't open modals, so after typing a timezone the scheduler continues with a button."""

    def __init__(self, cog, owner_id: int, tz_name: str):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.tz_name = cog, owner_id, tz_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run `/race schedule` to schedule your own.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Continue scheduling", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScheduleSprintModal(self.cog, self.tz_name))


class TimezonePickView(SafeView):
    """One-time (and /race timezone) picker. Picking a zone saves it; from /race schedule it then opens
    the schedule form straight away."""

    def __init__(self, cog, owner_id: int, then_schedule: bool, server_tz: str | None = None):
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.then_schedule = cog, owner_id, then_schedule
        options = []
        if server_tz:
            options.append(discord.SelectOption(
                label=f"Use the server's timezone ({server_tz})"[:100], value=f"server|{server_tz}"
            ))
        for name, label in _COMMON_ZONES:
            try:
                options.append(discord.SelectOption(label=f"{label} ({_utc_offset_label(name)})", value=name,
                                                    description=name))
            except (ZoneInfoNotFoundError, ValueError):
                continue                        # tz data missing on this host; skip rather than crash
        options.append(discord.SelectOption(label="Other… (type it in)", value="__other__"))
        select = discord.ui.Select(placeholder="Pick your timezone", options=options[:25])
        select.callback = self._picked
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This picker belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        if value == "__other__":
            await interaction.response.send_modal(TimezoneModal(self.cog, self.then_schedule))
            return
        name = value.split("|", 1)[1] if value.startswith("server|") else value
        await self.cog.bot.db.set_user_timezone(interaction.user.id, name)
        if self.then_schedule:
            await interaction.response.send_modal(ScheduleSprintModal(self.cog, name))
        else:
            await interaction.response.edit_message(
                content=f"✅ Saved — I'll read your times as **{name}** ({_utc_offset_label(name)}).", view=None
            )


class ScheduleDurationView(SafeView):
    def __init__(self, cog, owner_id: int, channel_id: int, title: str, description: str | None,
                 start_utc, tz_name: str, date_text: str, allow_test: bool = False):
        super().__init__(timeout=300)
        if allow_test:                  # only offered to sprint managers; _finalize_schedule re-checks
            self.pick.add_option(label="Mod Test (5 min, no GG)", value=str(TEST_SPRINT_MINUTES))
        self.cog, self.owner_id, self.channel_id = cog, owner_id, channel_id
        self.title, self.description = title, description
        self.start_utc, self.tz_name, self.date_text = start_utc, tz_name, date_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This sprint is being scheduled by someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="How long is the sprint?",
        options=[
            discord.SelectOption(label="30 minutes", value="30"),
            discord.SelectOption(label="1 hour", value="60"),
            discord.SelectOption(label="1 hour 30 minutes", value="90"),
            discord.SelectOption(label="2 hours", value="120"),
        ]
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self.cog._finalize_schedule(interaction, self, int(select.values[0]))


class ScheduleAnotherView(SafeView):
    def __init__(self, cog, owner_id: int, tz_name: str, prefill_date: str):
        super().__init__(timeout=600)
        self.cog, self.owner_id, self.tz_name, self.prefill_date = cog, owner_id, tz_name, prefill_date

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run `/race schedule` to schedule your own.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Schedule another", style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScheduleSprintModal(self.cog, self.tz_name, self.prefill_date))


# ── Cog ───────────────────────────────────────────────────────────────────────

class Sprints(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._logging_sprints: dict[int, dict] = {}
        self._logging_tasks: dict[int, asyncio.Task] = {}
        self._end_message: dict[int, tuple[int, int]] = {}  # guild_id -> (channel_id, message_id)
        self._tasks: set = set()                 # strong refs to background tasks so they aren't GC'd
        self._restored = asyncio.Event()         # set once startup recovery has run (the scheduler waits on it)

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
        self._spawn(self._restore_sprints())
        self.scheduler_tick.start()

    def cog_unload(self):
        self.scheduler_tick.cancel()

    def _spawn(self, coro):
        task = self.bot.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _restore_sprints(self):
        try:
            await self.bot.wait_until_ready()
            for guild in self.bot.guilds:
                row = await self.bot.db.get_active_sprint(guild.id)
                if not row:
                    continue
                now = discord.utils.utcnow()
                end_time = _aware(row["end_time"])
                # The row's start_time is when the sprint was *created*; focus time began a join-window later,
                # which is exactly end_time minus the duration.
                start_time = end_time - timedelta(minutes=row["duration_minutes"])

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
                        "total_minutes": p.get("total_minutes"),
                        "total_pages": p.get("total_pages"),
                        "left": p.get("resumed_at") is None,
                    }
                sprint = {
                    "host": row["host_id"], "channel": row["channel_id"], "duration": row["duration_minutes"],
                    "started_at": _aware(row["start_time"]), "start_time": start_time, "end_time": end_time,
                    "role_id": row["role_id"], "participants": participants,
                    "join_message_id": row.get("join_message_id"), "started": now >= start_time,
                    "title": row.get("title"), "description": row.get("description"),
                    "scheduled_id": row.get("scheduled_id"),
                }
                active_sprints[guild.id] = sprint
                self._spawn(self._run_sprint_lifecycle(guild, sprint))
            for guild in self.bot.guilds:
                try:
                    await self._refresh_scheduled_messages(guild)
                except Exception:
                    traceback.print_exc()
        finally:
            self._restored.set()

    # ── Sprint lifecycle (shared by /race start, scheduled sprints and restart recovery) ──────────

    async def _announce_start(self, guild: discord.Guild, sprint: dict):
        channel = await _get_channel(guild, sprint["channel"])
        if not channel:
            return
        participant_ids = [uid for uid, d in sprint["participants"].items() if not d.get("left")]
        mentions = " ".join(f"<@{uid}>" for uid in participant_ids) or None
        title_line = f"### {_expand_emoji(self.bot, sprint['title'])}\n" if sprint.get("title") else ""
        end_time = sprint["end_time"]
        start_embed = discord.Embed(
            description=(
                f"## {_emoji(self.bot, 'litcandle')} Focus Time Started!\n{title_line}\n"
                f"{_emoji(self.bot, 'griffinreading')} *Griffin has lit the candle and commands all patrons "
                f"to open their tomes, focus time has begun!*\n\n"
                f"{_emoji(self.bot, 'hourglass')} The sprint ends at "
                f"{discord.utils.format_dt(end_time, 'F')} ({discord.utils.format_dt(end_time, 'R')})."
            ),
            color=RACE_COLOR
        )
        await channel.send(
            content=mentions, embed=start_embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def _run_sprint_lifecycle(self, guild: discord.Guild, sprint: dict):
        """Waits for focus time, announces it, waits for the end, then finishes. After every wait it checks
        this is still the guild's current sprint — otherwise a cancelled sprint's old timer would wake up
        and finish whatever sprint had been started since."""
        gid = guild.id
        try:
            if not sprint.get("started"):
                delay = (sprint["start_time"] - discord.utils.utcnow()).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                if active_sprints.get(gid) is not sprint:
                    return
                sprint["started"] = True
                await self._announce_start(guild, sprint)
            remaining = (sprint["end_time"] - discord.utils.utcnow()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            if active_sprints.get(gid) is not sprint:
                return
            await self._finish_sprint(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            print(f"sprint lifecycle failed for guild {gid}:")
            traceback.print_exc()

    # ── Roster embed (creation + live) ────────────────────────────────────────

    _FIELD_LIMIT = 1000

    def _build_sprint_embeds(self, guild: discord.Guild, start_time, end_time, host_name, duration,
                             title: str | None = None, description: str | None = None) -> list[discord.Embed]:
        sprint = active_sprints.get(guild.id)
        participants = sprint["participants"] if sprint else {}

        head = [f"## {_emoji(self.bot, 'announcement')} The Stacks Go Silent…"]
        if title:
            head.append(f"### {_expand_emoji(self.bot, title)}")
        if description:
            head.append(_expand_emoji(self.bot, description))
        lines = [
            *head,
            "",
            f"{_emoji(self.bot, 'litcandle')} **Start Time:** {discord.utils.format_dt(start_time, 'F')} "
            f"*({discord.utils.format_dt(start_time, 'R')})*",
            f"{_emoji(self.bot, 'snuffedcandle')} **End Time:** {discord.utils.format_dt(end_time, 'F')} "
            f"*({discord.utils.format_dt(end_time, 'R')})*",
            f"{_emoji(self.bot, 'hourglass')} **Duration:** {_format_duration(duration)}",
            f"{_emoji(self.bot, 'griffinlurk')} **Host:** {host_name}",
        ]
        if duration == TEST_SPRINT_MINUTES:
            lines.append("🧪 **Mod Test sprint — no GG is awarded.**")
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
                p_lines.append(f"{bookmark} {_uname(guild, uid)} - {data['start']:.1f}%{suffix}")
            else:
                p_lines.append(f"{bookmark} {_uname(guild, uid)} - page {data['start']}{suffix}")

        _add_chunked_field(main_embed, header, p_lines, "No participants yet", limit=self._FIELD_LIMIT)

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
            embeds = self._build_sprint_embeds(
                guild, start_time, end_time, host_name, sprint["duration"],
                title=sprint.get("title"), description=sprint.get("description")
            )
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
            lines.append(f"{ink} {_uname(self.bot.get_guild(guild_id), uid)}{mark}")
        embed = discord.Embed(
            description=(
                f"## {_emoji(self.bot, 'snuffedcandle')} Focus Time Ended!\n\n"
                f"{_emoji(self.bot, 'griffinsleepy')} *Griffin snuffs out the candle and demands all patrons "
                f"close tomes and report pages immediately!*\n\n"
                f"Patrons have {LOG_WINDOW_MINUTES} minutes to update progress, "
                f"until {discord.utils.format_dt(deadline, 'R')}."
            ),
            color=RACE_COLOR
        )
        # Every patron is listed (with a stamp once logged) however many there are, so this has to
        # be chunked: one field caps at 1024 chars, which custom-emoji lines hit at ~12 patrons.
        _add_chunked_field(
            embed, f"{_emoji(self.bot, 'blankbook')} Sprint Patrons:", lines, "No participants."
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
        app_commands.Choice(name="Mod Test (5 min, no GG)", value=TEST_SPRINT_MINUTES),
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
        if sprint_channel_id and not _in_sprint_channel(interaction, sprint_channel_id):
            await interaction.response.send_message(
                f"❌ Sprints can only be started in <#{sprint_channel_id}>.", ephemeral=True
            )
            return

        if not _can_manage_sprints(interaction, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to start sprints.", ephemeral=True
            )
            return

        if duration_minutes == TEST_SPRINT_MINUTES and not _is_sprint_manager(interaction, settings):
            await interaction.response.send_message(
                "❌ Mod Test sprints are for sprint managers only.", ephemeral=True
            )
            return

        if guild_id in active_sprints:
            await interaction.response.send_message("A sprint is already running!", ephemeral=True)
            return
        if guild_id in self._logging_sprints:
            # The finishing sprint still owns this guild's active_sprint rows until its log window closes;
            # starting another now would let the old sprint's cleanup wipe the new one's data.
            await interaction.response.send_message(
                "⏳ The last sprint is still collecting logs — try again in a few minutes.", ephemeral=True
            )
            return

        role_id = settings.get("sprint_role_id")
        role_mention = f"<@&{role_id}>" if role_id else ""

        now = discord.utils.utcnow()
        start_time = now + timedelta(seconds=countdown_seconds)
        end_time = start_time + timedelta(minutes=duration_minutes)

        sprint = {
            "host": interaction.user.id, "channel": interaction.channel.id, "duration": duration_minutes,
            "started_at": now, "start_time": start_time, "end_time": end_time, "role_id": role_id,
            "participants": {}, "join_message_id": None, "started": False,
            "title": None, "description": None, "scheduled_id": None,
        }
        active_sprints[guild_id] = sprint

        embeds = self._build_sprint_embeds(interaction.guild, start_time, end_time,
                                           interaction.user.display_name, duration_minutes)
        view = JoinSprintView(self)

        await interaction.response.send_message(
            content=_created_text(start_time, role_mention),
            embeds=embeds, view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        msg = await interaction.original_response()
        sprint["join_message_id"] = msg.id

        await self.bot.db.save_active_sprint(
            guild_id, interaction.user.id, interaction.channel.id, duration_minutes, now, end_time, role_id
        )
        await self.bot.db.update_sprint_message_id(guild_id, msg.id)

        await self._run_sprint_lifecycle(interaction.guild, sprint)

    # ── /race cancel ──────────────────────────────────────────────────────────

    @race.command(name="cancel", description="Force-cancel the current sprint (running or awaiting logs).")
    async def sprint_cancel(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        settings = await self.bot.db.get_guild_config(guild_id)
        if not _can_manage_sprints(interaction, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to cancel sprints.", ephemeral=True
            )
            return

        cancelled_something = False
        scheduled_ids = set()

        sprint = active_sprints.pop(guild_id, None)
        if sprint is not None:
            cancelled_something = True
            if sprint.get("scheduled_id"):
                scheduled_ids.add(sprint["scheduled_id"])

        logging_sprint = self._logging_sprints.pop(guild_id, None)
        if logging_sprint is not None:
            # Flag it before waking the wait, so _finish_sprint knows to stop instead of posting results.
            logging_sprint["cancelled"] = True
            self._end_message.pop(guild_id, None)
            task = self._logging_tasks.pop(guild_id, None)
            if task and not task.done():
                task.cancel()
            cancelled_something = True
            if logging_sprint.get("scheduled_id"):
                scheduled_ids.add(logging_sprint["scheduled_id"])

        await self.bot.db.delete_active_sprint(guild_id)
        for sid in scheduled_ids:
            await self.bot.db.set_scheduled_status(sid, "cancelled")

        if cancelled_something:
            await interaction.response.send_message("🛑 The current sprint has been cancelled.")
        else:
            await interaction.response.send_message(
                "There's no sprint running right now, but I cleared any leftover state just in case.",
                ephemeral=True
            )

    # ── Scheduling ────────────────────────────────────────────────────────────────────────────

    def _build_scheduled_embed(self, guild: discord.Guild, row: dict) -> discord.Embed:
        """The small 'a sprint is scheduled' notice: title, when, how long. No buttons and no ping; the
        join card (with the Page Racers ping) goes up as its own message when the join window opens."""
        start = _aware(row["start_time"])
        lines = [f"## {_emoji(self.bot, 'announcement')} A Community Sprint has been scheduled…",
                 f"### {_expand_emoji(self.bot, row['title'])}"]
        if row.get("description"):
            lines.append(_expand_emoji(self.bot, row["description"]))
        lines += [
            "",
            f"{_emoji(self.bot, 'litcandle')} **Starts:** {discord.utils.format_dt(start, 'F')} "
            f"*({discord.utils.format_dt(start, 'R')})*",
            f"{_emoji(self.bot, 'hourglass')} **Duration:** {_format_duration(row['duration_minutes'])}",
            *(["🧪 **Mod Test sprint — no GG is awarded.**"] if row["duration_minutes"] == TEST_SPRINT_MINUTES else []),
            "",
            f"*A join card goes up {SCHEDULED_JOIN_MINUTES} minutes before focus time — watch for the ping!*",
        ]
        return discord.Embed(description="\n".join(lines), color=RACE_COLOR)

    def _build_scheduled_closed_embed(self, row: dict, reason: str) -> discord.Embed:
        start = _aware(row["start_time"])
        label = {
            "cancelled": "🛑 Cancelled",
            "started": "✅ Open for joining — the join card is below",
        }.get(reason, "⚠️ Missed — Griffin was busy or offline")
        struck = f"~~**{row['title']}**~~" if reason != "started" else f"**{row['title']}**"
        return discord.Embed(
            description=f"{struck}\n{label}\n*Scheduled for {discord.utils.format_dt(start, 'F')}*",
            color=RACE_COLOR,
        )

    async def _close_scheduled_message(self, guild: discord.Guild, row: dict, reason: str):
        if not row.get("message_id"):
            return
        channel = await _get_channel(guild, row["channel_id"])
        if not channel:
            return
        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.edit(embeds=[self._build_scheduled_closed_embed(row, reason)], view=None)
        except Exception:
            print(f"_close_scheduled_message failed for scheduled sprint {row['id']}:")
            traceback.print_exc()

    async def _refresh_scheduled_messages(self, guild: discord.Guild):
        """Brings still-waiting scheduled announcements up to the small, button-free format (ones posted by an
        earlier version carried a big card and Pre-Join buttons that no longer do anything)."""
        for row in await self.bot.db.list_scheduled_sprints(guild.id, ("scheduled",)):
            if not row.get("message_id"):
                continue
            try:
                channel = await _get_channel(guild, row["channel_id"])
                msg = await channel.fetch_message(row["message_id"]) if channel else None
                if msg is not None:
                    await msg.edit(content=None, embeds=[self._build_scheduled_embed(guild, row)], view=None)
            except discord.NotFound:
                continue                 # someone deleted the announcement; nothing to tidy
            except Exception:
                print(f"_refresh_scheduled_messages failed for scheduled sprint {row['id']}:")
                traceback.print_exc()

    async def _open_join_modal(self, interaction: discord.Interaction, *, switch: bool = False,
                               default_type: str = "pages"):
        """Opens the one-step join form straight from a button, with the person's last 3 books to pick from."""
        rows = await self.bot.db.get_recent_sprint_titles(interaction.guild.id, interaction.user.id, limit=3)
        if rows and not switch:         # start on however they tracked their latest book (pages or audio %)
            default_type = "audio_pct" if str(rows[0].get("last_type") or "").startswith("audio") else "pages"
        await interaction.response.send_modal(JoinSprintModal(
            self, [r["title"] for r in rows], switch=switch, default_type=default_type))

    async def _find_schedule_conflict(self, guild_id: int, start_utc, duration: int):
        """Another scheduled/active sprint whose window — joining time before it through log time after
        it — overlaps the proposed one. Sprints in a guild share state, so they can't overlap."""
        join = timedelta(minutes=SCHEDULED_JOIN_MINUTES)
        log = timedelta(minutes=LOG_WINDOW_MINUTES)
        new_open, new_close = start_utc - join, start_utc + timedelta(minutes=duration) + log
        for r in await self.bot.db.list_scheduled_sprints(guild_id, ("scheduled", "active")):
            r_start = _aware(r["start_time"])
            r_open, r_close = r_start - join, r_start + timedelta(minutes=r["duration_minutes"]) + log
            if new_open < r_close and r_open < new_close:
                return r
        return None

    async def _finalize_schedule(self, interaction: discord.Interaction, view: "ScheduleDurationView", duration: int):
        guild, db = interaction.guild, self.bot.db
        if view.start_utc <= discord.utils.utcnow() + timedelta(minutes=1):
            await interaction.response.edit_message(
                content="❌ That start time passed while you were choosing. Run `/race schedule` again.", view=None
            )
            return
        if duration == TEST_SPRINT_MINUTES and not _is_sprint_manager(interaction, await db.get_guild_config(guild.id)):
            await interaction.response.edit_message(
                content="❌ Mod Test sprints are for sprint managers only.", view=None
            )
            return
        conflict = await self._find_schedule_conflict(guild.id, view.start_utc, duration)
        if conflict:
            await interaction.response.edit_message(
                content=(
                    f"❌ That overlaps **{conflict['title']}** "
                    f"({discord.utils.format_dt(_aware(conflict['start_time']), 'F')}). Sprints need "
                    f"{SCHEDULED_JOIN_MINUTES} min of joining time before them and {LOG_WINDOW_MINUTES} min of "
                    f"logging time after. Pick a shorter length, or run `/race schedule` with a different time."
                ),
                view=view,
            )
            return
        channel = await _get_channel(guild, view.channel_id)
        if not channel:
            await interaction.response.edit_message(content="❌ I can't find that channel anymore.", view=None)
            return
        sid = await db.create_scheduled_sprint(
            guild.id, channel.id, interaction.user.id, view.title, view.description, view.start_utc, duration
        )
        try:
            row = await db.get_scheduled_sprint(sid)
            msg = await channel.send(
                embeds=[self._build_scheduled_embed(guild, row)]
            )
            await db.set_scheduled_message_id(sid, msg.id)
        except Exception:
            await db.set_scheduled_status(sid, "cancelled")
            raise
        await interaction.response.edit_message(
            content=(f"✅ Scheduled **{view.title}** for {discord.utils.format_dt(view.start_utc, 'F')} "
                     f"({_format_duration(duration)})."),
            view=ScheduleAnotherView(self, interaction.user.id, view.tz_name, view.date_text),
        )

    @race.command(name="schedule", description="Schedule a sprint ahead of time (one at a time).")
    async def sprint_schedule(self, interaction: discord.Interaction):
        settings = await self.bot.db.get_guild_config(interaction.guild.id)
        sprint_channel_id = settings.get("sprint_channel_id")
        if sprint_channel_id and not _in_sprint_channel(interaction, sprint_channel_id):
            await interaction.response.send_message(
                f"❌ Sprints can only be scheduled in <#{sprint_channel_id}>.", ephemeral=True
            )
            return
        if not _can_manage_sprints(interaction, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to schedule sprints.", ephemeral=True
            )
            return
        tz_name = await self.bot.db.get_user_timezone(interaction.user.id)
        if tz_name:
            try:
                ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError):
                tz_name = None
        if not tz_name:                             # first time: ask once, remember it
            await interaction.response.send_message(
                "🌍 First time scheduling — what's your timezone? I'll remember it, and use it to read the "
                "start times you type. (Starting with a countdown like `in 1h30m` needs no timezone.)",
                view=TimezonePickView(self, interaction.user.id, True, settings.get("sprint_timezone")),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ScheduleSprintModal(self, tz_name))

    @race.command(name="timezone", description="Set or check the timezone I use to read your typed times.")
    @app_commands.describe(zone="Optional: a timezone to save, e.g. America/Toronto or EST. Leave blank to pick from a list.")
    async def sprint_timezone(self, interaction: discord.Interaction, zone: str | None = None):
        db = self.bot.db
        if zone:
            name = _resolve_timezone(zone)
            if not name:
                await interaction.response.send_message(
                    f"❌ I don't know a timezone called `{zone[:40]}`. Use a city-style name like "
                    "`America/Toronto` or `Europe/Berlin`, or an abbreviation like `EST`.", ephemeral=True
                )
                return
            await db.set_user_timezone(interaction.user.id, name)
            await interaction.response.send_message(
                f"✅ Saved — I'll read your times as **{name}** ({_utc_offset_label(name)}).", ephemeral=True
            )
            return
        current = await db.get_user_timezone(interaction.user.id)
        settings = await db.get_guild_config(interaction.guild.id)
        head = (f"🌍 Your timezone is **{current}** ({_utc_offset_label(current)}). Pick another to change it:"
                if current else "🌍 You haven't set a timezone yet. Pick yours:")
        await interaction.response.send_message(
            head, view=TimezonePickView(self, interaction.user.id, False, settings.get("sprint_timezone")),
            ephemeral=True,
        )

    @race.command(name="scheduled", description="See the upcoming scheduled sprints.")
    async def sprint_scheduled(self, interaction: discord.Interaction):
        db = self.bot.db
        rows = await db.list_scheduled_sprints(interaction.guild.id, ("scheduled",))
        if not rows:
            await interaction.response.send_message("📜 Nothing is scheduled right now.")
            return
        lines = []
        for r in rows:
            start = _aware(r["start_time"])
            link = (f" · [Details]({_message_url(interaction.guild.id, r['channel_id'], r['message_id'])})"
                    if r.get("message_id") else "")
            lines.append(
                f"**{r['title']}**\n{discord.utils.format_dt(start, 'F')} ({discord.utils.format_dt(start, 'R')}) · "
                f"{_format_duration(r['duration_minutes'])}{link}"
            )
        embeds = _paginate_lines(lines, "📜 Scheduled Sprints")
        kwargs = {"view": EmbedPaginatorView(embeds)} if len(embeds) > 1 else {}   # response.send_message rejects view=None
        await interaction.response.send_message(embed=embeds[0], **kwargs)

    @race.command(name="unschedule", description="Cancel a scheduled sprint.")
    @app_commands.describe(sprint="Which scheduled sprint to cancel")
    async def sprint_unschedule(self, interaction: discord.Interaction, sprint: str):
        db = self.bot.db
        settings = await db.get_guild_config(interaction.guild.id)
        if not _can_manage_sprints(interaction, settings):
            await interaction.response.send_message(
                "❌ You don't have permission to cancel scheduled sprints.", ephemeral=True
            )
            return
        try:
            sid = int(sprint)
        except ValueError:
            await interaction.response.send_message("❌ Pick a sprint from the list.", ephemeral=True)
            return
        row = await db.get_scheduled_sprint(sid)
        if not row or row["guild_id"] != interaction.guild.id:
            await interaction.response.send_message("❌ I couldn't find that scheduled sprint.", ephemeral=True)
            return
        if not await db.set_scheduled_status(sid, "cancelled", expected="scheduled"):
            await interaction.response.send_message(
                "❌ That sprint has already started or was already cancelled — use `/race cancel` for a running sprint.",
                ephemeral=True
            )
            return
        await self._close_scheduled_message(interaction.guild, row, "cancelled")
        await interaction.response.send_message(
            f"{_emoji(self.bot, 'snuffedcandle')} {_nm(interaction.user)} cancelled the scheduled sprint **{row['title']}**."
        )

    @sprint_unschedule.autocomplete("sprint")
    async def _unschedule_autocomplete(self, interaction: discord.Interaction, current: str):
        db = self.bot.db
        settings = await db.get_guild_config(interaction.guild.id)
        try:
            tz = ZoneInfo(settings.get("sprint_timezone") or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc
        out = []
        for r in await db.list_scheduled_sprints(interaction.guild.id, ("scheduled",)):
            local = _aware(r["start_time"]).astimezone(tz).strftime("%b %d %H:%M")
            label = f"{r['title']} — {local}"[:100]
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label, value=str(r["id"])))
        return out[:25]

    # The scheduler: opens a scheduled sprint's join window (a fresh join card + ping, like /race start) and
    # hands the sprint to the same lifecycle.

    @tasks.loop(seconds=30)
    async def scheduler_tick(self):
        await self._scheduler_pass()

    @scheduler_tick.before_loop
    async def _before_scheduler_tick(self):
        await self.bot.wait_until_ready()
        await self._restored.wait()      # don't race startup recovery for ownership of a guild

    async def _scheduler_pass(self):
        cutoff = datetime.utcnow() + timedelta(minutes=SCHEDULED_JOIN_MINUTES)
        for row in await self.bot.db.get_due_scheduled_sprints(cutoff):
            try:
                await self._try_activate_scheduled(row)
            except Exception:
                print(f"scheduled sprint {row['id']} failed to activate:")
                traceback.print_exc()

    async def _try_activate_scheduled(self, row: dict):
        db = self.bot.db
        guild = self.bot.get_guild(row["guild_id"])
        channel = await _get_channel(guild, row["channel_id"]) if guild else None
        if not guild or not channel:
            await db.set_scheduled_status(row["id"], "missed", expected="scheduled")
            return
        if datetime.utcnow() >= row["start_time"]:
            if await db.set_scheduled_status(row["id"], "missed", expected="scheduled"):
                await self._post_missed(guild, channel, row)
            return
        if guild.id in active_sprints or guild.id in self._logging_sprints:
            return                       # another sprint owns the guild right now — retry next pass
        if not await db.set_scheduled_status(row["id"], "active", expected="scheduled"):
            return                       # already claimed by another pass
        try:
            await self._activate_scheduled(guild, channel, row)
        except Exception:
            active_sprints.pop(guild.id, None)
            await db.delete_active_sprint(guild.id)
            await db.set_scheduled_status(row["id"], "missed")
            raise

    async def _post_missed(self, guild: discord.Guild, channel, row: dict):
        await self._close_scheduled_message(guild, row, "missed")
        await channel.send(
            f"⚠️ **{row['title']}** was scheduled for "
            f"{discord.utils.format_dt(_aware(row['start_time']), 'F')} but couldn't start — Griffin was busy or "
            f"offline. Reschedule it with `/race schedule`."
        )

    async def _activate_scheduled(self, guild: discord.Guild, channel, row: dict):
        db = self.bot.db
        settings = await db.get_guild_config(guild.id)
        role_id = settings.get("sprint_role_id")
        start_time = _aware(row["start_time"])
        end_time = start_time + timedelta(minutes=row["duration_minutes"])
        now = discord.utils.utcnow()

        # Pre-joining is gone, but a sprint scheduled by an earlier version may still carry a pre-join list.
        prejoin_rows = await db.get_scheduled_participants(row["id"])
        participants = {}
        for p in prejoin_rows:
            entry = {
                "type": p["type"], "title": p["title"], "start": _deserialize_start(p["type"], p["start_val"]),
                "end": None, "joined_at": row["start_time"], "gg_awarded": False, "segments": [],
            }
            if p.get("total_pages"):
                entry["total_pages"] = p["total_pages"]
            participants[p["user_id"]] = entry

        sprint = {
            "host": row["host_id"], "channel": channel.id, "duration": row["duration_minutes"],
            "started_at": now, "start_time": start_time, "end_time": end_time, "role_id": role_id,
            "participants": participants, "join_message_id": None, "started": False,
            "title": row["title"], "description": row["description"], "scheduled_id": row["id"],
        }
        active_sprints[guild.id] = sprint        # claim the guild before any await below can be interleaved

        await db.save_active_sprint(
            guild.id, row["host_id"], channel.id, row["duration_minutes"], now, end_time, role_id,
            title=row["title"], description=row["description"], scheduled_id=row["id"],
        )
        for p in prejoin_rows:
            await db.save_sprint_participant(
                guild.id, p["user_id"], p["type"], p["title"], p["start_val"],
                joined_at=row["start_time"], total_pages=p.get("total_pages"),
            )

        host = guild.get_member(row["host_id"])
        embeds = self._build_sprint_embeds(
            guild, start_time, end_time, host.display_name if host else "Unknown", row["duration_minutes"],
            title=row["title"], description=row["description"],
        )
        # The join card goes up as its own *new* message, exactly like /race start, with the Page Racers ping
        # on top (an edit would never notify). The small "scheduled" notice is then marked as open.
        role_mention = f"<@&{role_id}>" if role_id else ""
        msg = await channel.send(
            content=_created_text(start_time, role_mention), embeds=embeds, view=JoinSprintView(self),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        sprint["join_message_id"] = msg.id
        await db.update_sprint_message_id(guild.id, msg.id)
        await self._close_scheduled_message(guild, row, "started")
        self._spawn(self._run_sprint_lifecycle(guild, sprint))

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
        gg_rate = await _sprint_gg(self.bot.db, interaction.guild.id)
        gg_earned = (stats["gg_sprints"] or 0) * gg_rate
        pages = stats["total_pages"] or 0
        unlogged = int(stats["unlogged_sprints"] or 0)

        embed = discord.Embed(title=f"{_emoji(self.bot, 'bargraph')} Sprint Stats — {member.display_name}", color=RACE_COLOR)
        embed.add_field(name="Sprints Completed", value=str(stats["sprints_completed"] or 0))
        if pages:
            embed.add_field(name=f"{_emoji(self.bot, 'blankbook')} Pages Read", value=f"{pages:,}")
        if gg_rate > 0:
            embed.add_field(name="GG Earned from Sprints", value=f"{gg_earned} GG")
        if unlogged:
            embed.add_field(name=f"{_emoji(self.bot, 'griffinlurk')} Unlogged Sprints", value=str(unlogged))
        await interaction.followup.send(embed=embed)

    # ── /race leaderboard ────────────────────────────────────────────────────

    @race.command(name="leaderboard", description="View the sprint leaderboard")
    async def sprint_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = await self.bot.db.get_sprint_leaderboard(interaction.guild.id, limit=1000)
        if not rows:
            await interaction.followup.send("No sprint data yet!")
            return

        gg_rate = await _sprint_gg(self.bot.db, interaction.guild.id)
        ordinals = ["1ST", "2ND", "3RD"] + [f"{i}TH" for i in range(4, len(rows) + 1)]
        lines = []
        for i, row in enumerate(rows):
            member = interaction.guild.get_member(row["user_id"])
            name = _safe_name(member, row["user_id"])
            sprints = row["sprints_completed"] or 0
            total_pages = row["total_pages"] or 0
            gg_part = f" · Total Sprint GG Earned: {(row['gg_sprints'] or 0) * gg_rate:,} GG" if gg_rate > 0 else ""
            lines.append(
                f"**{ordinals[i]} - {name.upper()}**\n"
                f"{sprints} Sprint{'s' if sprints != 1 else ''} | "
                f"Total Pages Logged: {total_pages:,}{gg_part}"
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
                    "total_minutes": p.get("total_minutes"),
                    "total_pages": p.get("total_pages"),
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
        if sprint.get("cancelled"):
            return                       # /race cancel during the log window: don't post results

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
                "total_minutes": p.get("total_minutes"),
                "total_pages": p.get("total_pages"),
            }
        for uid, data in sprint["participants"].items():
            if data["end"] is not None:
                final_participants.setdefault(uid, data)
                final_participants[uid]["end"] = data["end"]
                final_participants[uid]["segments"] = data.get("segments", [])
                if data.get("total_minutes"):
                    final_participants[uid]["total_minutes"] = data["total_minutes"]
                if data.get("total_pages"):
                    final_participants[uid]["total_pages"] = data["total_pages"]
                if data.get("joined_at") and not final_participants[uid].get("joined_at"):
                    final_participants[uid]["joined_at"] = data["joined_at"]
                if data.get("gg_awarded"):
                    final_participants[uid]["gg_awarded"] = True

        await self.bot.db.delete_active_sprint(guild_id)

        gg = await _gg_for_duration(self.bot.db, guild_id, sprint["duration"])
        silent_gg_uids = []
        for uid, data in final_participants.items():
            if data["end"] is not None and not data.get("gg_awarded"):
                await _award_sprint_gg(self.bot, guild, uid, gg)
                data["gg_awarded"] = True
                silent_gg_uids.append(uid)

        for uid, data in final_participants.items():      # remember where each person got to in their book
            if data["end"] is not None and data.get("title"):
                try:
                    await self.bot.db.set_book_progress(uid, guild_id, data["title"], data["type"], data["end"])
                except Exception:
                    traceback.print_exc()

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

        cfg = await self.bot.db.get_guild_config(guild_id)
        librarian_role_id = cfg.get("sprint_manager_role_id")
        # Pinging the librarian role on every results board is opt-in (/staff race librarian-ping), e.g. readathons.
        librarian_mention = f"<@&{librarian_role_id}> " if librarian_role_id and cfg.get("sprint_librarian_ping") else ""

        if silent_gg_uids and gg > 0:       # said before the board so the results are always the last word
            names = ", ".join(_uname(guild, uid) for uid in silent_gg_uids)
            await channel.send(
                f"{names} — you updated your progress during the sprint and earned **{gg} GG**! {_emoji(self.bot, 'books')}"
            )

        if results:
            medals = [_emoji(self.bot, "first_place"), _emoji(self.bot, "second_place"), _emoji(self.bot, "third_place")]
            lines = []
            for i, (name, pages, title, duration) in enumerate(results):
                suffix = f" — *{title}*" if title else ""
                prefix = medals[i] if i < 3 else f"`#{i + 1}`"
                dur = f" ({duration})" if duration else ""
                lines.append(f"{prefix} **{name}** — +{pages} pages{suffix}{dur}")
            embeds = _paginate_lines(
                lines, f"{_emoji(self.bot, 'checkered_flag')} Griffin has recorded the sprint results…",
                header=f"### {sprint['title']}" if sprint.get("title") else None,
            )
            if gg > 0:
                for e in embeds:
                    e.set_footer(text=f"Each participant earned {gg} GG!")
            view = EmbedPaginatorView(embeds) if len(embeds) > 1 else None
            await channel.send(content=librarian_mention or None, embed=embeds[0], view=view)
        else:
            await channel.send(f"{librarian_mention}No one logged their results. 📭")

        sprint_id = await self.bot.db.create_sprint(
            guild_id, sprint["channel"], sprint["host"], sprint["started_at"], sprint_end_time, sprint["duration"],
            title=sprint.get("title"), is_scheduled=bool(sprint.get("scheduled_id")),
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

        if sprint.get("scheduled_id"):
            await self.bot.db.set_scheduled_status(sprint["scheduled_id"], "done")


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
    def __init__(self, bot, sprint_id: int, guild_id: int, is_test: bool = False):
        super().__init__(timeout=30)
        self.bot = bot
        self.sprint_id = sprint_id
        self.guild_id = guild_id
        self.is_test = is_test

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.db.delete_sprint_log(self.sprint_id, interaction.user.id)
        gg = 0 if self.is_test else await _sprint_gg(self.bot.db, self.guild_id)
        if gg > 0:
            await self.bot.db.remove_gg(self.guild_id, interaction.user.id, gg)
        for item in self.children:
            item.disabled = True
        note = f" and **{gg} GG** deducted" if gg > 0 else ""
        await interaction.response.edit_message(content=f"🗑️ Log deleted{note}.", view=self)
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

        self.finish_btn = discord.ui.Button(label="✅ Mark book finished", style=discord.ButtonStyle.success,
                                            disabled=True)
        self.finish_btn.callback = self._on_finish
        self.add_item(self.finish_btn)

    async def _on_select(self, interaction: discord.Interaction):
        self._selected = interaction.data["values"][0]
        self.edit_btn.disabled = False
        self.delete_btn.disabled = False
        self.finish_btn.disabled = not self.logs[self._selected]["title"]
        await interaction.response.edit_message(view=self)

    async def _on_edit(self, interaction: discord.Interaction):
        row = self.logs[self._selected]
        modal = SprintEditModal(
            self.bot, row["sprint_id"], row["title"] or "Untitled",
            row["start_count"] or 0, row["end_count"] or 0, row["type"], self.guild_id
        )
        await interaction.response.send_modal(modal)

    async def _on_finish(self, interaction: discord.Interaction):
        row = self.logs[self._selected]
        # A log that spanned a Switch Books is titled "A & B": each is its own book.
        books = [t.strip() for t in (row["title"] or "").split(" & ") if t.strip()]
        for book in books:
            await self.bot.db.set_book_finished(interaction.user.id, self.guild_id, book, True)
        names = ", ".join(f"**{b}**" for b in books)
        await interaction.response.send_message(
            f"✅ Marked {names} as finished — it won't show in your list of recent books. "
            f"Join with the title again any time to bring it back.", ephemeral=True
        )

    async def _on_delete(self, interaction: discord.Interaction):
        row = self.logs[self._selected]
        date = row["sprint_end_time"].strftime("%b %d") if row["sprint_end_time"] else "?"
        is_test = row["duration_minutes"] == TEST_SPRINT_MINUTES
        gg = 0 if is_test else await _sprint_gg(self.bot.db, self.guild_id)
        deduct_note = f" This will deduct **{gg} GG**." if gg > 0 else ""
        await interaction.response.send_message(
            f"Delete **{row['title'] or 'Untitled'}** ({date})?{deduct_note}",
            view=SprintDeleteConfirmView(self.bot, row["sprint_id"], self.guild_id, is_test),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Sprints(bot))
