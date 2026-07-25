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
                PRIMARY KEY (guild_id, user_id)
            )
        """)
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

    # --- members / GG ---
    # GG is purely cosmetic (rank + bragging rights) — nothing spends it, so
    # it's a single running total, not a balance/earned split.

    async def get_member(self, guild_id: int, user_id: int) -> dict:
        row = await self._fetchone(
            "SELECT gg, card_id FROM members WHERE guild_id = %s AND user_id = %s",
            guild_id, user_id
        )
        return row or {"gg": 0, "card_id": None}

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

    # --- guild config / earn rates ---

    async def get_guild_config(self, guild_id: int) -> dict:
        row = await self._fetchone(
            "SELECT message_rate, message_cooldown, voice_rate, voice_interval, "
            "levelup_channel_id, assets_channel_id "
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
