import aiopg
import os
from typing import Optional


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
                user_id BIGINT PRIMARY KEY,
                gg INTEGER NOT NULL DEFAULT 0
            )
        """)
