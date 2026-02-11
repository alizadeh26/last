from __future__ import annotations

import aiosqlite


class Storage:
    def __init__(self, db_path: str = "bot.db") -> None:
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    url TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await db.commit()

    async def add_subscription(self, url: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO subscriptions(url, enabled) VALUES(?, 1)",
                    (url,),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_subscription(self, url: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM subscriptions WHERE url = ?", (url,))
            await db.commit()
            return cur.rowcount > 0

    async def list_subscriptions(self) -> list[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT url FROM subscriptions WHERE enabled = 1 ORDER BY url"
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows]
