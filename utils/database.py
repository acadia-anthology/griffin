import aiopg
import json
import os
from datetime import timezone
from typing import Optional

DEFAULT_MESSAGE_RATE = 25
DEFAULT_MESSAGE_COOLDOWN = 30
DEFAULT_VOICE_RATE = 25
DEFAULT_VOICE_INTERVAL = 3600
DEFAULT_SPRINT_GG = 50


def _utc_naive(dt):
    """Naive-UTC for TIMESTAMP columns. An aware datetime assigned to a TIMESTAMP column is converted
    using the DB *session's* timezone, which is only UTC if the server happens to be — so normalise here."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class Database:
    def __init__(self):
        self.pool = None

    async def initialize(self):
        self.pool = await aiopg.create_pool(os.getenv("DATABASE_URL"))
        await self._create_tables()
        print("✅ Database connected")

    async def _execute(self, query: str, *args):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)

    async def _fetchone(self, query: str, *args) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                row = await cur.fetchone()
                if row is None:
                    return None
                colnames = [desc[0] for desc in cur.description]
                return dict(zip(colnames, row))

    async def _fetchall(self, query: str, *args):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                rows = await cur.fetchall()
                colnames = [desc[0] for desc in cur.description]
                return [dict(zip(colnames, row)) for row in rows]

    async def _create_tables(self):
        await self._execute("""
            CREATE TABLE IF NOT EXISTS members (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                gg INTEGER NOT NULL DEFAULT 0,
                card_id INTEGER,
                bio TEXT,
                favorite_genres TEXT,
                books_checked_out TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await self._execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS bio TEXT;")
        await self._execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS favorite_genres TEXT;")
        await self._execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS books_checked_out TEXT;")
        await self._execute("""
            CREATE TABLE IF NOT EXISTS library_cards (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                image_url TEXT NOT NULL,
                accent_color TEXT,
                added_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # library_cards/guild_config may already exist from an earlier deploy
        # without these columns — CREATE TABLE IF NOT EXISTS won't add them
        # to a table that's already there, so migrate explicitly.
        await self._execute("ALTER TABLE library_cards ADD COLUMN IF NOT EXISTS accent_color TEXT;")
        await self._execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id BIGINT PRIMARY KEY,
                message_rate INTEGER NOT NULL DEFAULT 25,
                message_cooldown INTEGER NOT NULL DEFAULT 30,
                voice_rate INTEGER NOT NULL DEFAULT 25,
                voice_interval INTEGER NOT NULL DEFAULT 3600,
                levelup_channel_id BIGINT,
                assets_channel_id BIGINT
            )
        """)
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS assets_channel_id BIGINT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_channel_id BIGINT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_role_id BIGINT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_manager_role_id BIGINT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS griffin_manager_role_id BIGINT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS level_thresholds TEXT;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS level_flat_rate INTEGER;")
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_gg INTEGER;")

        await self._execute("""
            CREATE TABLE IF NOT EXISTS level_tier_roles (
                guild_id BIGINT NOT NULL,
                lo_level INTEGER NOT NULL,
                role_id BIGINT,
                PRIMARY KEY (guild_id, lo_level)
            )
        """)
        await self._execute("ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_timezone TEXT;")
        await self._execute(
            "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS sprint_librarian_ping BOOLEAN DEFAULT FALSE;"
        )

        await self._execute("""
            CREATE TABLE IF NOT EXISTS sprints (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                channel_id BIGINT,
                host_id BIGINT,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                duration_minutes INTEGER
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS sprint_participants (
                sprint_id INTEGER REFERENCES sprints(id),
                user_id BIGINT,
                start_count INTEGER DEFAULT 0,
                end_count INTEGER,
                type TEXT DEFAULT 'pages',
                title TEXT,
                joined_at TIMESTAMP,
                sprint_end_time TIMESTAMP,
                PRIMARY KEY (sprint_id, user_id)
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS active_sprint (
                guild_id BIGINT PRIMARY KEY,
                host_id BIGINT,
                channel_id BIGINT,
                duration_minutes INTEGER,
                start_time TIMESTAMP,
                end_time TIMESTAMP,
                role_id BIGINT,
                join_message_id BIGINT
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS active_sprint_participants (
                guild_id BIGINT,
                user_id BIGINT,
                type TEXT,
                title TEXT,
                start_val TEXT,
                end_val TEXT,
                joined_at TIMESTAMP,
                active_seconds INTEGER DEFAULT 0,
                resumed_at TIMESTAMP,
                segments TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        # CREATE TABLE IF NOT EXISTS never alters a table that's already deployed, so every
        # column added after a table first shipped needs its own ALTER or the live DB never gets it.
        await self._execute("ALTER TABLE active_sprint_participants ADD COLUMN IF NOT EXISTS active_seconds INTEGER DEFAULT 0;")
        await self._execute("ALTER TABLE active_sprint_participants ADD COLUMN IF NOT EXISTS resumed_at TIMESTAMP;")
        await self._execute("ALTER TABLE active_sprint_participants ADD COLUMN IF NOT EXISTS segments TEXT;")
        await self._execute("ALTER TABLE active_sprint_participants ADD COLUMN IF NOT EXISTS total_minutes INTEGER;")
        await self._execute("ALTER TABLE active_sprint_participants ADD COLUMN IF NOT EXISTS total_pages INTEGER;")
        await self._execute("ALTER TABLE active_sprint ADD COLUMN IF NOT EXISTS title TEXT;")
        await self._execute("ALTER TABLE active_sprint ADD COLUMN IF NOT EXISTS description TEXT;")
        await self._execute("ALTER TABLE active_sprint ADD COLUMN IF NOT EXISTS scheduled_id INTEGER;")
        await self._execute("ALTER TABLE sprints ADD COLUMN IF NOT EXISTS title TEXT;")
        await self._execute("ALTER TABLE sprints ADD COLUMN IF NOT EXISTS is_scheduled BOOLEAN DEFAULT FALSE;")
        await self._execute("""
            CREATE TABLE IF NOT EXISTS scheduled_sprints (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                host_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                start_time TIMESTAMP NOT NULL,
                duration_minutes INTEGER NOT NULL,
                message_id BIGINT,
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC')
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS scheduled_sprint_participants (
                scheduled_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                title TEXT,
                start_val TEXT NOT NULL,
                total_minutes INTEGER,
                PRIMARY KEY (scheduled_id, user_id)
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS user_timezones (
                user_id BIGINT PRIMARY KEY,
                tz_name TEXT NOT NULL
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS sprint_book_meta (
                user_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                total_pages INTEGER,
                total_minutes INTEGER,
                PRIMARY KEY (user_id, guild_id, title)
            )
        """)
        await self._execute("ALTER TABLE scheduled_sprint_participants ADD COLUMN IF NOT EXISTS total_pages INTEGER;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS last_used TIMESTAMP;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS finished BOOLEAN DEFAULT FALSE;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS last_type TEXT;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS last_pos DOUBLE PRECISION;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS status TEXT;")
        await self._execute("ALTER TABLE sprint_book_meta ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP;")

    # --- members / GG ---
    # GG is purely cosmetic (rank + bragging rights) — nothing spends it, so
    # it's a single running total, not a balance/earned split.

    async def get_member(self, guild_id: int, user_id: int) -> dict:
        row = await self._fetchone(
            "SELECT gg, card_id, bio, favorite_genres, books_checked_out "
            "FROM members WHERE guild_id = %s AND user_id = %s",
            guild_id, user_id
        )
        return row or {
            "gg": 0, "card_id": None, "bio": None,
            "favorite_genres": None, "books_checked_out": None,
        }

    async def set_profile_text(self, guild_id: int, user_id: int, bio: str, favorite_genres: str,
                                books_checked_out: str):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, bio, favorite_genres, books_checked_out)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET bio = %s, favorite_genres = %s, books_checked_out = %s
        """, guild_id, user_id, bio, favorite_genres, books_checked_out,
             bio, favorite_genres, books_checked_out)

    async def add_gg(self, guild_id: int, user_id: int, amount: int) -> int:
        row = await self._fetchone("""
            INSERT INTO members (guild_id, user_id, gg)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET gg = members.gg + %s
            RETURNING gg
        """, guild_id, user_id, amount, amount)
        return row["gg"]

    async def remove_gg(self, guild_id: int, user_id: int, amount: int):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, gg)
            VALUES (%s, %s, 0)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET gg = GREATEST(members.gg - %s, 0)
        """, guild_id, user_id, amount)

    async def get_leaderboard(self, guild_id: int) -> list:
        return await self._fetchall("""
            SELECT user_id, gg
            FROM members
            WHERE guild_id = %s AND gg > 0
            ORDER BY gg DESC
        """, guild_id)

    async def get_rank(self, guild_id: int, user_id: int) -> Optional[int]:
        rows = await self.get_leaderboard(guild_id)
        for i, row in enumerate(rows, start=1):
            if row["user_id"] == user_id:
                return i
        return None

    # --- library cards ---

    async def add_library_card(self, guild_id: int, name: str, image_url: str, added_by: int,
                                accent_color: Optional[str] = None) -> int:
        row = await self._fetchone("""
            INSERT INTO library_cards (guild_id, name, image_url, added_by, accent_color)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, guild_id, name, image_url, added_by, accent_color)
        return row["id"]

    async def get_library_cards(self, guild_id: int) -> list:
        return await self._fetchall("""
            SELECT id, name, image_url, accent_color FROM library_cards
            WHERE guild_id = %s ORDER BY name
        """, guild_id)

    async def get_card(self, card_id: int) -> Optional[dict]:
        return await self._fetchone(
            "SELECT id, name, image_url, accent_color FROM library_cards WHERE id = %s",
            card_id
        )

    async def set_member_card(self, guild_id: int, user_id: int, card_id: int):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, card_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET card_id = %s
        """, guild_id, user_id, card_id, card_id)

    async def clear_member_card(self, guild_id: int, user_id: int):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, card_id)
            VALUES (%s, %s, NULL)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET card_id = NULL
        """, guild_id, user_id)

    # --- guild config / earn rates ---

    async def get_guild_config(self, guild_id: int) -> dict:
        row = await self._fetchone(
            "SELECT message_rate, message_cooldown, voice_rate, voice_interval, "
            "levelup_channel_id, assets_channel_id, sprint_channel_id, sprint_role_id, "
            "sprint_manager_role_id, sprint_timezone, griffin_manager_role_id, "
            "level_thresholds, level_flat_rate, sprint_gg, sprint_librarian_ping "
            "FROM guild_config WHERE guild_id = %s",
            guild_id
        )
        if row:
            # NULL means "never configured" -> the default; an explicit 0 must stay 0 (GG off).
            if row.get("sprint_gg") is None:
                row["sprint_gg"] = DEFAULT_SPRINT_GG
            row["sprint_librarian_ping"] = bool(row.get("sprint_librarian_ping"))
            return row
        return {
            "message_rate": DEFAULT_MESSAGE_RATE,
            "message_cooldown": DEFAULT_MESSAGE_COOLDOWN,
            "voice_rate": DEFAULT_VOICE_RATE,
            "voice_interval": DEFAULT_VOICE_INTERVAL,
            "levelup_channel_id": None,
            "assets_channel_id": None,
            "sprint_channel_id": None,
            "sprint_role_id": None,
            "sprint_manager_role_id": None,
            "sprint_timezone": None,
            "griffin_manager_role_id": None,
            "sprint_librarian_ping": False,
            "level_thresholds": None,
            "level_flat_rate": None,
            "sprint_gg": DEFAULT_SPRINT_GG,
        }

    async def set_levelup_channel(self, guild_id: int, channel_id: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, levelup_channel_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET levelup_channel_id = %s
        """, guild_id, channel_id, channel_id)

    async def set_assets_channel(self, guild_id: int, channel_id: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, assets_channel_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET assets_channel_id = %s
        """, guild_id, channel_id, channel_id)

    async def set_sprint_channel(self, guild_id: int, channel_id):          # None clears the restriction
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_channel_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_channel_id = %s
        """, guild_id, channel_id, channel_id)

    async def set_sprint_role(self, guild_id: int, role_id: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_role_id = %s
        """, guild_id, role_id, role_id)

    async def set_sprint_manager_role(self, guild_id: int, role_id: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_manager_role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_manager_role_id = %s
        """, guild_id, role_id, role_id)

    async def set_griffin_manager_role(self, guild_id: int, role_id: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, griffin_manager_role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET griffin_manager_role_id = %s
        """, guild_id, role_id, role_id)

    # --- level curve (levels 1-30 individually editable, flat rate for 31+) ---

    async def set_level_threshold(self, guild_id: int, level: int, gg_required: int):
        config = await self.get_guild_config(guild_id)
        raw = config.get("level_thresholds")
        thresholds = json.loads(raw) if raw else {}
        thresholds[str(level)] = gg_required
        payload = json.dumps(thresholds)
        await self._execute("""
            INSERT INTO guild_config (guild_id, level_thresholds)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET level_thresholds = %s
        """, guild_id, payload, payload)

    async def set_sprint_gg(self, guild_id: int, amount: int):
        """GG awarded per completed sprint; 0 turns it off. NULL (never set) means the default."""
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_gg)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_gg = %s
        """, guild_id, amount, amount)

    async def set_level_flat_rate(self, guild_id: int, rate: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, level_flat_rate)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET level_flat_rate = %s
        """, guild_id, rate, rate)

    # --- level tier -> role mapping (per guild, falls back to default names) ---

    async def set_tier_role(self, guild_id: int, lo_level: int, role_id: Optional[int]):
        await self._execute("""
            INSERT INTO level_tier_roles (guild_id, lo_level, role_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id, lo_level) DO UPDATE SET role_id = %s
        """, guild_id, lo_level, role_id, role_id)

    async def get_tier_roles(self, guild_id: int) -> dict:
        rows = await self._fetchall(
            "SELECT lo_level, role_id FROM level_tier_roles WHERE guild_id=%s", guild_id
        )
        return {r["lo_level"]: r["role_id"] for r in rows if r["role_id"]}

    async def set_sprint_timezone(self, guild_id: int, tz_name: str):
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_timezone)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_timezone = %s
        """, guild_id, tz_name, tz_name)

    async def get_user_timezone(self, user_id: int):
        row = await self._fetchone("SELECT tz_name FROM user_timezones WHERE user_id=%s", user_id)
        return row["tz_name"] if row else None

    async def set_user_timezone(self, user_id: int, tz_name: str):
        await self._execute("""
            INSERT INTO user_timezones (user_id, tz_name)
            VALUES (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET tz_name = %s
        """, user_id, tz_name, tz_name)

    async def set_sprint_librarian_ping(self, guild_id: int, enabled: bool):
        await self._execute("""
            INSERT INTO guild_config (guild_id, sprint_librarian_ping)
            VALUES (%s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET sprint_librarian_ping = %s
        """, guild_id, enabled, enabled)

    async def set_message_rate(self, guild_id: int, amount: int, cooldown: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, message_rate, message_cooldown)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET message_rate = %s, message_cooldown = %s
        """, guild_id, amount, cooldown, amount, cooldown)

    async def set_voice_rate(self, guild_id: int, amount: int, interval: int):
        await self._execute("""
            INSERT INTO guild_config (guild_id, voice_rate, voice_interval)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id)
            DO UPDATE SET voice_rate = %s, voice_interval = %s
        """, guild_id, amount, interval, amount, interval)

    # --- sprints ---

    async def create_sprint(self, guild_id: int, channel_id: int, host_id: int,
                             start_time, end_time, duration_minutes: int,
                             title: Optional[str] = None, is_scheduled: bool = False) -> int:
        row = await self._fetchone("""
            INSERT INTO sprints (guild_id, channel_id, host_id, start_time, end_time, duration_minutes, title, is_scheduled)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, guild_id, channel_id, host_id, _utc_naive(start_time), _utc_naive(end_time),
             duration_minutes, title, is_scheduled)
        return row["id"]

    async def add_sprint_participant(self, sprint_id: int, user_id: int, start_count: int, end_count,
                                      ptype: str = "pages", joined_at=None, sprint_end_time=None, title: str = None):
        await self._execute(
            "INSERT INTO sprint_participants (sprint_id, user_id, start_count, end_count, type, joined_at, sprint_end_time, title) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            sprint_id, user_id, start_count, end_count, ptype,
            _utc_naive(joined_at), _utc_naive(sprint_end_time), title
        )

    async def get_sprint_logs(self, guild_id: int, user_id: int, limit: int = 10) -> list:
        return await self._fetchall("""
            SELECT sp.sprint_id, sp.title, sp.type, sp.start_count, sp.end_count, sp.sprint_end_time, s.duration_minutes
            FROM sprint_participants sp
            JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND sp.user_id=%s AND sp.end_count IS NOT NULL
            ORDER BY sp.sprint_end_time DESC NULLS LAST
            LIMIT %s
        """, guild_id, user_id, limit)

    async def update_sprint_log(self, sprint_id: int, user_id: int, start_count: int, end_count: int, title: Optional[str] = None):
        await self._execute(
            "UPDATE sprint_participants SET start_count=%s, end_count=%s, title=COALESCE(%s, title) WHERE sprint_id=%s AND user_id=%s",
            start_count, end_count, title, sprint_id, user_id
        )

    async def delete_sprint_log(self, sprint_id: int, user_id: int):
        await self._execute(
            "DELETE FROM sprint_participants WHERE sprint_id=%s AND user_id=%s",
            sprint_id, user_id
        )

    async def sprint_reset_preview(self, guild_id: int, user_ids=None, include_books: bool = False) -> dict:
        """What a stats reset would delete (nothing is changed). user_ids None = everyone in the server."""
        everyone, ids = user_ids is None, list(user_ids or [])
        row = await self._fetchone("""
            SELECT COUNT(*) AS logs, COUNT(DISTINCT sp.user_id) AS members, COUNT(DISTINCT sp.sprint_id) AS sprints
            FROM sprint_participants sp JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND (%s OR sp.user_id = ANY(%s))
        """, guild_id, everyone, ids)
        who = await self._fetchall("""
            SELECT DISTINCT sp.user_id FROM sprint_participants sp JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND (%s OR sp.user_id = ANY(%s)) ORDER BY sp.user_id
        """, guild_id, everyone, ids)
        books = 0
        if include_books:
            b = await self._fetchone(
                "SELECT COUNT(*) AS n FROM sprint_book_meta WHERE guild_id=%s AND (%s OR user_id = ANY(%s))",
                guild_id, everyone, ids)
            books = b["n"]
        return {"logs": row["logs"], "members": row["members"], "sprints": row["sprints"],
                "books": books, "user_ids": [r[0] if not isinstance(r, dict) else r["user_id"] for r in who]}

    async def reset_sprint_stats(self, guild_id: int, user_ids=None, include_books: bool = False) -> dict:
        """Deletes sprint history (the logs behind /race stats and the leaderboard) for the given people, or for
        everyone when user_ids is None. Sprints left with no logs are removed too. One statement, so it either
        all happens or none of it does. Goblin Gold, level and scheduled sprints are never touched."""
        everyone, ids = user_ids is None, list(user_ids or [])
        row = await self._fetchone("""
            WITH gone AS (
                DELETE FROM sprint_participants sp USING sprints s
                WHERE sp.sprint_id = s.id AND s.guild_id=%s AND (%s OR sp.user_id = ANY(%s))
                RETURNING sp.sprint_id, sp.user_id
            ), emptied AS (
                DELETE FROM sprints s
                WHERE s.guild_id=%s AND s.id IN (SELECT sprint_id FROM gone)
                  AND NOT EXISTS (
                      SELECT 1 FROM sprint_participants p
                      WHERE p.sprint_id = s.id
                        AND NOT EXISTS (SELECT 1 FROM gone g WHERE g.sprint_id = p.sprint_id AND g.user_id = p.user_id))
                RETURNING s.id
            ), books AS (
                DELETE FROM sprint_book_meta
                WHERE %s AND guild_id=%s AND (%s OR user_id = ANY(%s))
                RETURNING 1
            )
            SELECT (SELECT COUNT(*) FROM gone) AS logs,
                   (SELECT COUNT(DISTINCT user_id) FROM gone) AS members,
                   (SELECT COUNT(*) FROM emptied) AS sprints,
                   (SELECT COUNT(*) FROM books) AS books
        """, guild_id, everyone, ids, guild_id, include_books, guild_id, everyone, ids)
        return dict(row)

    async def get_sprint_stats(self, guild_id: int, user_id: int) -> dict:
        return await self._fetchone("""
            SELECT COUNT(*) FILTER (WHERE sp.end_count IS NOT NULL) AS sprints_completed,
                   COUNT(*) FILTER (WHERE sp.end_count IS NOT NULL AND s.duration_minutes <> 5) AS gg_sprints,
                   COALESCE(SUM(CASE WHEN sp.type='pages' AND sp.end_count IS NOT NULL THEN (sp.end_count - sp.start_count) ELSE 0 END), 0) AS total_pages,
                   COALESCE(SUM(CASE WHEN sp.type='audio' AND sp.end_count IS NOT NULL THEN (sp.end_count - sp.start_count) ELSE 0 END), 0) AS total_audio_minutes,
                   COUNT(*) FILTER (WHERE sp.end_count IS NULL) AS unlogged_sprints
            FROM sprint_participants sp
            JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND sp.user_id=%s
        """, guild_id, user_id)

    async def get_sprint_history(self, guild_id: int, user_id: int, limit: int = 20) -> list:
        return await self._fetchall("""
            SELECT sp.title, sp.type,
                   SUM(sp.end_count - sp.start_count) AS progress,
                   MIN(sp.sprint_end_time) AS first_date,
                   MAX(sp.sprint_end_time) AS last_date,
                   COUNT(*) AS session_count
            FROM sprint_participants sp
            JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND sp.user_id=%s AND sp.end_count IS NOT NULL AND sp.title IS NOT NULL
            GROUP BY sp.title, sp.type
            ORDER BY MAX(sp.sprint_end_time) DESC NULLS LAST
            LIMIT %s
        """, guild_id, user_id, limit)

    async def get_recent_sprint_titles(self, guild_id: int, user_id: int, limit: int = 5) -> list:
        """The person's most recent books (individual titles), newest first, minus any marked finished, each
        with how it was last tracked ('pages' / 'audio_pct'). Joined/switched books live in sprint_book_meta;
        older logged sprints fill in the history from before that (combined 'A & B' log titles are skipped,
        they aren't one book)."""
        return await self._fetchall("""
            SELECT title, last_used, last_type FROM (
                SELECT DISTINCT ON (LOWER(title)) title, last_used, last_type FROM (
                    SELECT m.title, m.last_used, m.last_type
                    FROM sprint_book_meta m
                    WHERE m.guild_id=%s AND m.user_id=%s
                    UNION ALL
                    SELECT sp.title, sp.sprint_end_time, sp.type
                    FROM sprint_participants sp
                    JOIN sprints s ON s.id = sp.sprint_id
                    WHERE s.guild_id=%s AND sp.user_id=%s AND sp.title IS NOT NULL AND sp.title NOT LIKE '%% & %%'
                ) u
                ORDER BY LOWER(title), last_used DESC NULLS LAST
            ) d
            WHERE LOWER(title) NOT IN (
                SELECT LOWER(title) FROM sprint_book_meta WHERE guild_id=%s AND user_id=%s AND finished
            )
            ORDER BY last_used DESC NULLS LAST
            LIMIT %s
        """, guild_id, user_id, guild_id, user_id, guild_id, user_id, limit)

    async def get_sprint_book_meta(self, user_id: int, guild_id: int, title: str) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM sprint_book_meta WHERE user_id=%s AND guild_id=%s AND LOWER(title)=LOWER(%s) "
            "ORDER BY last_used DESC NULLS LAST LIMIT 1",
            user_id, guild_id, title
        )

    async def _book_title(self, user_id: int, guild_id: int, title: str) -> str:
        """Typing 'dune' for a book saved as 'Dune' is the same book: reuse the saved spelling."""
        row = await self.get_sprint_book_meta(user_id, guild_id, title)
        return row["title"] if row else title

    async def set_book_progress(self, user_id: int, guild_id: int, title: str, ptype: str, position):
        """Where they got to in this book and whether they tracked it by pages or audio %, so the next
        sprint can pick up from there."""
        title = await self._book_title(user_id, guild_id, title)
        await self._execute(
            """INSERT INTO sprint_book_meta (user_id, guild_id, title, last_type, last_pos, last_used, finished)
               VALUES (%s,%s,%s,%s,%s,(NOW() AT TIME ZONE 'UTC'),FALSE)
               ON CONFLICT (user_id, guild_id, title)
               DO UPDATE SET last_type = %s, last_pos = %s""",
            user_id, guild_id, title, ptype, float(position), ptype, float(position)
        )

    async def save_sprint_book_meta(self, user_id: int, guild_id: int, title: str,
                                     total_pages: Optional[int] = None):
        """Remembers a book (and its total pages, if known) and that it was just used. Using a book again
        also brings it back if it had been marked finished."""
        await self._execute(
            """INSERT INTO sprint_book_meta (user_id, guild_id, title, total_pages, last_used, finished)
               VALUES (%s,%s,%s,%s,(NOW() AT TIME ZONE 'UTC'),FALSE)
               ON CONFLICT (user_id, guild_id, title)
               DO UPDATE SET
                 total_pages = COALESCE(%s, sprint_book_meta.total_pages),
                 last_used = (NOW() AT TIME ZONE 'UTC'),
                 finished = FALSE, status = NULL, finished_at = NULL""",
            user_id, guild_id, title, total_pages, total_pages
        )

    async def set_book_status(self, user_id: int, guild_id: int, title: str, status):
        """status: 'completed', 'dnf', or None to put the book back to 'currently reading'. Completed and DNF
        books drop off the recent-books list; none of this touches any sprint's page totals."""
        if status not in (None, "completed", "dnf"):
            raise ValueError(f"unknown book status {status!r}")
        title = await self._book_title(user_id, guild_id, title)
        finished = status is not None
        await self._execute(
            """INSERT INTO sprint_book_meta (user_id, guild_id, title, finished, status, finished_at)
               VALUES (%s,%s,%s,%s,%s,CASE WHEN %s THEN (NOW() AT TIME ZONE 'UTC') END)
               ON CONFLICT (user_id, guild_id, title)
               DO UPDATE SET finished = %s, status = %s,
                             finished_at = CASE WHEN %s THEN (NOW() AT TIME ZONE 'UTC') END""",
            user_id, guild_id, title, finished, status, finished, finished, status, finished
        )

    async def set_book_finished(self, user_id: int, guild_id: int, title: str, finished: bool = True):
        await self.set_book_status(user_id, guild_id, title, "completed" if finished else None)

    async def get_user_books(self, guild_id: int, user_id: int) -> list:
        """Every book this person has sprinted with: title, status ('completed' / 'dnf' / None = reading),
        and where they last got to. Older logged sprints fill in the books from before this was tracked
        (combined 'A & B' log titles are skipped, they aren't one book)."""
        return await self._fetchall("""
            SELECT title, status, last_used, last_type, last_pos, finished_at FROM (
                SELECT DISTINCT ON (LOWER(title)) title, status, last_used, last_type, last_pos, finished_at FROM (
                    SELECT m.title,
                           CASE WHEN m.finished THEN COALESCE(m.status, 'completed') END AS status,
                           m.last_used, m.last_type, m.last_pos, m.finished_at, 1 AS src
                    FROM sprint_book_meta m
                    WHERE m.guild_id=%s AND m.user_id=%s
                    UNION ALL
                    SELECT sp.title, NULL::text, sp.sprint_end_time, sp.type, NULL::double precision,
                           NULL::timestamp, 0
                    FROM sprint_participants sp
                    JOIN sprints s ON s.id = sp.sprint_id
                    WHERE s.guild_id=%s AND sp.user_id=%s AND sp.title IS NOT NULL AND sp.title NOT LIKE '%% & %%'
                ) u
                ORDER BY LOWER(title), src DESC, last_used DESC NULLS LAST
            ) d
            ORDER BY last_used DESC NULLS LAST
        """, guild_id, user_id, guild_id, user_id)

    async def get_sprint_leaderboard(self, guild_id: int, limit: int = 10) -> list:
        return await self._fetchall("""
            SELECT sp.user_id,
                   COUNT(*) AS sprints_completed,
                   COUNT(*) FILTER (WHERE s.duration_minutes <> 5) AS gg_sprints,
                   COALESCE(SUM(CASE WHEN sp.type='pages' THEN (sp.end_count - sp.start_count) ELSE 0 END), 0) AS total_pages,
                   COALESCE(SUM(CASE WHEN sp.type='audio' THEN (sp.end_count - sp.start_count) ELSE 0 END), 0) AS total_audio_minutes
            FROM sprint_participants sp
            JOIN sprints s ON s.id = sp.sprint_id
            WHERE s.guild_id=%s AND sp.end_count IS NOT NULL
            GROUP BY sp.user_id
            ORDER BY sprints_completed DESC, total_pages DESC
            LIMIT %s
        """, guild_id, limit)

    # --- active sprint persistence ---

    async def save_active_sprint(self, guild_id, host_id, channel_id, duration_minutes, start_time, end_time, role_id,
                                  title=None, description=None, scheduled_id=None, join_message_id=None):
        await self._execute("DELETE FROM active_sprint WHERE guild_id=%s", guild_id)
        await self._execute("""
            INSERT INTO active_sprint
                (guild_id, host_id, channel_id, duration_minutes, start_time, end_time, role_id,
                 title, description, scheduled_id, join_message_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, guild_id, host_id, channel_id, duration_minutes, _utc_naive(start_time), _utc_naive(end_time),
             role_id, title, description, scheduled_id, join_message_id)

    async def update_sprint_message_id(self, guild_id, message_id):
        await self._execute(
            "UPDATE active_sprint SET join_message_id=%s WHERE guild_id=%s",
            message_id, guild_id
        )

    async def get_active_sprint(self, guild_id) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM active_sprint WHERE guild_id=%s", guild_id)

    async def delete_active_sprint(self, guild_id):
        await self._execute("DELETE FROM active_sprint WHERE guild_id=%s", guild_id)
        await self._execute("DELETE FROM active_sprint_participants WHERE guild_id=%s", guild_id)

    async def save_sprint_participant(self, guild_id, user_id, ptype, title, start_val, end_val=None, joined_at=None,
                                       total_pages=None, segments_json: str = "[]", carry_active_seconds: int = 0):
        """Starts (or restarts, e.g. after starting fresh with a new book) the active-time clock. When starting
        fresh, the earlier books' segments and already-banked active seconds are carried in, not dropped."""
        from datetime import datetime
        joined_at = _utc_naive(joined_at) or datetime.utcnow()
        await self._execute("""
            INSERT INTO active_sprint_participants
                (guild_id, user_id, type, title, start_val, end_val, joined_at, active_seconds, resumed_at,
                 total_pages, segments)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                type=%s, title=%s, start_val=%s, end_val=%s,
                joined_at=COALESCE(active_sprint_participants.joined_at, %s),
                active_seconds=%s, resumed_at=%s, total_pages=%s, total_minutes=NULL, segments=%s
        """, guild_id, user_id, ptype, title, start_val, end_val, joined_at, carry_active_seconds, joined_at,
             total_pages, segments_json,
             ptype, title, start_val, end_val, joined_at, carry_active_seconds, joined_at, total_pages,
             segments_json)

    async def update_sprint_participant_end(self, guild_id, user_id, end_val):
        await self._execute(
            "UPDATE active_sprint_participants SET end_val=%s WHERE guild_id=%s AND user_id=%s",
            end_val, guild_id, user_id
        )

    # resumed_at / joined_at are naive-UTC (Python's datetime.utcnow()), so the clock queries
    # below compare against NOW() AT TIME ZONE 'UTC' rather than bare NOW(), which would be
    # read in whatever timezone the database server happens to run in.
    async def leave_sprint_participant(self, guild_id, user_id):
        """Banks elapsed active time and pauses the clock."""
        await self._execute("""
            UPDATE active_sprint_participants
            SET active_seconds = active_seconds
                    + GREATEST(0, EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'UTC') - resumed_at))::INTEGER),
                resumed_at = NULL
            WHERE guild_id=%s AND user_id=%s AND resumed_at IS NOT NULL
        """, guild_id, user_id)

    async def resume_sprint_participant(self, guild_id, user_id):
        """Resumes a paused clock without touching progress/title."""
        await self._execute("""
            UPDATE active_sprint_participants
            SET resumed_at = (NOW() AT TIME ZONE 'UTC')
            WHERE guild_id=%s AND user_id=%s AND resumed_at IS NULL
        """, guild_id, user_id)

    async def switch_sprint_participant_book(self, guild_id, user_id, ptype, title, start_val,
                                              segments_json: str, joined_at=None, total_pages=None):
        """Starts a participant's next book in one statement: new leg + saved segments together,
        leaving the active-time clock (joined_at / active_seconds / resumed_at) untouched."""
        from datetime import datetime
        joined_at = _utc_naive(joined_at) or datetime.utcnow()
        await self._execute("""
            INSERT INTO active_sprint_participants
                (guild_id, user_id, type, title, start_val, end_val, joined_at, active_seconds, resumed_at,
                 segments, total_pages)
            VALUES (%s,%s,%s,%s,%s,NULL,%s,0,%s,%s,%s)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                type=EXCLUDED.type, title=EXCLUDED.title, start_val=EXCLUDED.start_val,
                end_val=NULL, segments=EXCLUDED.segments, total_pages=EXCLUDED.total_pages,
                total_minutes=NULL
        """, guild_id, user_id, ptype, title, start_val, joined_at, joined_at, segments_json, total_pages)

    # --- scheduled sprints ---------------------------------------------------------------
    # start_time is naive UTC. status: scheduled -> active -> done, or cancelled / missed.

    async def create_scheduled_sprint(self, guild_id, channel_id, host_id, title, description,
                                       start_time, duration_minutes) -> int:
        row = await self._fetchone("""
            INSERT INTO scheduled_sprints (guild_id, channel_id, host_id, title, description, start_time, duration_minutes)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, guild_id, channel_id, host_id, title, description, _utc_naive(start_time), duration_minutes)
        return row["id"]

    async def set_scheduled_message_id(self, scheduled_id: int, message_id: int):
        await self._execute("UPDATE scheduled_sprints SET message_id=%s WHERE id=%s", message_id, scheduled_id)

    async def get_scheduled_sprint(self, scheduled_id: int) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM scheduled_sprints WHERE id=%s", scheduled_id)

    async def list_scheduled_sprints(self, guild_id: int, statuses=("scheduled",)) -> list:
        return await self._fetchall("""
            SELECT * FROM scheduled_sprints
            WHERE guild_id=%s AND status = ANY(%s)
            ORDER BY start_time ASC, id ASC
        """, guild_id, list(statuses))

    async def get_due_scheduled_sprints(self, cutoff) -> list:
        """Still-scheduled sprints whose start falls on/before `cutoff` (i.e. whose join window has opened)."""
        return await self._fetchall("""
            SELECT * FROM scheduled_sprints
            WHERE status='scheduled' AND start_time <= %s
            ORDER BY start_time ASC, id ASC
        """, _utc_naive(cutoff))

    async def set_scheduled_status(self, scheduled_id: int, status: str, expected: Optional[str] = None) -> bool:
        """Moves a scheduled sprint to `status`. With `expected`, only if it's currently in that state — a
        compare-and-set, so two activations/cancellations can never both win. True if a row changed."""
        if expected is None:
            row = await self._fetchone(
                "UPDATE scheduled_sprints SET status=%s WHERE id=%s RETURNING id", status, scheduled_id)
        else:
            row = await self._fetchone(
                "UPDATE scheduled_sprints SET status=%s WHERE id=%s AND status=%s RETURNING id",
                status, scheduled_id, expected)
        return row is not None

    async def get_scheduled_participants(self, scheduled_id: int) -> list:
        return await self._fetchall(
            "SELECT * FROM scheduled_sprint_participants WHERE scheduled_id=%s ORDER BY user_id", scheduled_id)

    async def get_sprint_participants(self, guild_id) -> list:
        return await self._fetchall("SELECT * FROM active_sprint_participants WHERE guild_id=%s", guild_id)
