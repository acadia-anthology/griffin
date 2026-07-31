import aiopg
import os
from typing import Optional

DEFAULT_MESSAGE_RATE = 25
DEFAULT_MESSAGE_COOLDOWN = 30
DEFAULT_VOICE_RATE = 25
DEFAULT_VOICE_INTERVAL = 3600


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
                PRIMARY KEY (guild_id, user_id)
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
            "levelup_channel_id, assets_channel_id, sprint_channel_id, sprint_role_id "
            "FROM guild_config WHERE guild_id = %s",
            guild_id
        )
        if row:
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

    async def set_sprint_channel(self, guild_id: int, channel_id: int):
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
                             start_time, end_time, duration_minutes: int) -> int:
        row = await self._fetchone("""
            INSERT INTO sprints (guild_id, channel_id, host_id, start_time, end_time, duration_minutes)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, guild_id, channel_id, host_id, start_time, end_time, duration_minutes)
        return row["id"]

    async def add_sprint_participant(self, sprint_id: int, user_id: int, start_count: int, end_count,
                                      ptype: str = "pages", joined_at=None, sprint_end_time=None, title: str = None):
        await self._execute(
            "INSERT INTO sprint_participants (sprint_id, user_id, start_count, end_count, type, joined_at, sprint_end_time, title) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            sprint_id, user_id, start_count, end_count, ptype, joined_at, sprint_end_time, title
        )

    async def get_sprint_logs(self, guild_id: int, user_id: int, limit: int = 10) -> list:
        return await self._fetchall("""
            SELECT sp.sprint_id, sp.title, sp.type, sp.start_count, sp.end_count, sp.sprint_end_time
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

    async def get_sprint_stats(self, guild_id: int, user_id: int) -> dict:
        return await self._fetchone("""
            SELECT COUNT(*) FILTER (WHERE sp.end_count IS NOT NULL) AS sprints_completed,
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
        return await self._fetchall("""
            SELECT title, type, last_used FROM (
                SELECT sp.title,
                       (array_agg(sp.type ORDER BY sp.sprint_end_time DESC NULLS LAST))[1] AS type,
                       MAX(sp.sprint_end_time) AS last_used
                FROM sprint_participants sp
                JOIN sprints s ON s.id = sp.sprint_id
                WHERE s.guild_id=%s AND sp.user_id=%s AND sp.title IS NOT NULL
                GROUP BY sp.title
            ) sub
            ORDER BY last_used DESC NULLS LAST
            LIMIT %s
        """, guild_id, user_id, limit)

    async def get_sprint_book_meta(self, user_id: int, guild_id: int, title: str) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM sprint_book_meta WHERE user_id=%s AND guild_id=%s AND title=%s",
            user_id, guild_id, title
        )

    async def save_sprint_book_meta(self, user_id: int, guild_id: int, title: str,
                                     total_pages: Optional[int] = None, total_minutes: Optional[int] = None):
        await self._execute(
            """INSERT INTO sprint_book_meta (user_id, guild_id, title, total_pages, total_minutes)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (user_id, guild_id, title)
               DO UPDATE SET
                 total_pages = COALESCE(%s, sprint_book_meta.total_pages),
                 total_minutes = COALESCE(%s, sprint_book_meta.total_minutes)""",
            user_id, guild_id, title, total_pages, total_minutes, total_pages, total_minutes
        )

    async def get_sprint_leaderboard(self, guild_id: int, limit: int = 10) -> list:
        return await self._fetchall("""
            SELECT sp.user_id,
                   COUNT(*) AS sprints_completed,
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

    async def save_active_sprint(self, guild_id, host_id, channel_id, duration_minutes, start_time, end_time, role_id):
        await self._execute("DELETE FROM active_sprint WHERE guild_id=%s", guild_id)
        await self._execute("""
            INSERT INTO active_sprint (guild_id, host_id, channel_id, duration_minutes, start_time, end_time, role_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, guild_id, host_id, channel_id, duration_minutes, start_time, end_time, role_id)

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

    async def save_sprint_participant(self, guild_id, user_id, ptype, title, start_val, end_val=None, joined_at=None):
        from datetime import datetime
        joined_at = joined_at or datetime.utcnow()
        await self._execute("""
            INSERT INTO active_sprint_participants (guild_id, user_id, type, title, start_val, end_val, joined_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET type=%s, title=%s, start_val=%s, end_val=%s, joined_at=COALESCE(active_sprint_participants.joined_at, %s)
        """, guild_id, user_id, ptype, title, start_val, end_val, joined_at,
             ptype, title, start_val, end_val, joined_at)

    async def update_sprint_participant_end(self, guild_id, user_id, end_val):
        await self._execute(
            "UPDATE active_sprint_participants SET end_val=%s WHERE guild_id=%s AND user_id=%s",
            end_val, guild_id, user_id
        )

    async def get_sprint_participants(self, guild_id) -> list:
        return await self._fetchall("SELECT * FROM active_sprint_participants WHERE guild_id=%s", guild_id)
