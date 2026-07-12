import os

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "mini_arena.db")

_connection = None


async def get_db():
    global _connection
    if _connection is None:
        _connection = await aiosqlite.connect(DB_PATH)
        _connection.row_factory = aiosqlite.Row
    return _connection


async def init_db():
    db = await get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            tag TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS friend_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id TEXT NOT NULL,
            to_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            UNIQUE(from_id, to_id)
        )
        """
    )
    await db.commit()
