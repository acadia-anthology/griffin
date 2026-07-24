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
                gg_balance INTEGER NOT NULL DEFAULT 0,
                gg_earned INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await self._execute("""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id BIGINT PRIMARY KEY,
                message_rate INTEGER NOT NULL DEFAULT 25,
                message_cooldown INTEGER NOT NULL DEFAULT 30,
                voice_rate INTEGER NOT NULL DEFAULT 25,
                voice_interval INTEGER NOT NULL DEFAULT 3600
            )
        """)

    # --- members / GG ---
    # gg_earned is lifetime GG earned (never decreases from spending) and drives
    # rank/leaderboard. gg_balance is spendable and drops when a patron buys
    # something. /gg add and /gg remove touch both, since those are corrections
    # to how much GG someone has actually earned, not purchases.

    async def get_member(self, guild_id: int, user_id: int) -> dict:
        row = await self._fetchone(
            "SELECT gg_balance, gg_earned FROM members WHERE guild_id = %s AND user_id = %s",
            guild_id, user_id
        )
        return row or {"gg_balance": 0, "gg_earned": 0}

    async def add_gg(self, guild_id: int, user_id: int, amount: int):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, gg_balance, gg_earned)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET gg_balance = members.gg_balance + %s,
                          gg_earned = members.gg_earned + %s
        """, guild_id, user_id, amount, amount, amount, amount)

    async def remove_gg(self, guild_id: int, user_id: int, amount: int):
        await self._execute("""
            INSERT INTO members (guild_id, user_id, gg_balance, gg_earned)
            VALUES (%s, %s, 0, 0)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET gg_balance = GREATEST(members.gg_balance - %s, 0),
                          gg_earned = GREATEST(members.gg_earned - %s, 0)
        """, guild_id, user_id, amount, amount)

    async def spend_gg(self, guild_id: int, user_id: int, amount: int) -> bool:
        row = await self._fetchone("""
            UPDATE members SET gg_balance = gg_balance - %s
            WHERE guild_id = %s AND user_id = %s AND gg_balance >= %s
            RETURNING gg_balance
        """, amount, guild_id, user_id, amount)
        return row is not None

    async def get_leaderboard(self, guild_id: int) -> list:
        return await self._fetchall("""
            SELECT user_id, gg_balance, gg_earned
            FROM members
            WHERE guild_id = %s AND gg_earned > 0
            ORDER BY gg_earned DESC
        """, guild_id)

    async def get_rank(self, guild_id: int, user_id: int) -> Optional[int]:
        rows = await self.get_leaderboard(guild_id)
        for i, row in enumerate(rows, start=1):
            if row["user_id"] == user_id:
                return i
        return None

    # --- guild config / earn rates ---

    async def get_guild_config(self, guild_id: int) -> dict:
        row = await self._fetchone(
            "SELECT message_rate, message_cooldown, voice_rate, voice_interval "
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
        }

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
