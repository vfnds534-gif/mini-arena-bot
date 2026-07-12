import random
from datetime import datetime, timezone

from db import get_db

TAG_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TAG_LENGTH = 4


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _generate_unique_tag(db):
    for _ in range(50):
        tag = "".join(random.choice(TAG_ALPHABET) for _ in range(TAG_LENGTH))
        async with db.execute("SELECT 1 FROM users WHERE tag = ?", (tag,)) as cur:
            if not await cur.fetchone():
                return tag
    raise RuntimeError("tag_generation_failed")


async def get_or_create_user(telegram_id, name):
    db = await get_db()
    now = _now()
    async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        row = await cur.fetchone()
    if row:
        await db.execute(
            "UPDATE users SET name = ?, last_seen = ? WHERE telegram_id = ?",
            (name, now, telegram_id),
        )
        await db.commit()
        return {"telegram_id": telegram_id, "tag": row["tag"], "name": name, "last_seen": now}

    tag = await _generate_unique_tag(db)
    await db.execute(
        "INSERT INTO users (telegram_id, tag, name, last_seen) VALUES (?, ?, ?, ?)",
        (telegram_id, tag, name, now),
    )
    await db.commit()
    return {"telegram_id": telegram_id, "tag": tag, "name": name, "last_seen": now}


async def touch_last_seen(telegram_id):
    db = await get_db()
    await db.execute(
        "UPDATE users SET last_seen = ? WHERE telegram_id = ?",
        (_now(), telegram_id),
    )
    await db.commit()


async def find_by_tag(tag):
    db = await get_db()
    async with db.execute("SELECT telegram_id FROM users WHERE tag = ?", (tag.upper(),)) as cur:
        row = await cur.fetchone()
    return row["telegram_id"] if row else None


async def send_friend_request(from_id, to_id):
    if from_id == to_id:
        return "self"

    db = await get_db()
    async with db.execute("SELECT 1 FROM users WHERE telegram_id = ?", (to_id,)) as cur:
        if not await cur.fetchone():
            return "not_found"

    async with db.execute(
        "SELECT status FROM friend_requests WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
        (from_id, to_id, to_id, from_id),
    ) as cur:
        existing = await cur.fetchone()

    if existing:
        if existing["status"] == "accepted":
            return "already_friends"
        if existing["status"] == "pending":
            return "already_pending"
        # previously declined — clear it out so a fresh request can be sent
        await db.execute(
            "DELETE FROM friend_requests WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
            (from_id, to_id, to_id, from_id),
        )

    await db.execute(
        "INSERT INTO friend_requests (from_id, to_id, status, created_at) VALUES (?, ?, 'pending', ?)",
        (from_id, to_id, _now()),
    )
    await db.commit()
    return "ok"


async def respond_friend_request(request_id, to_id, accept):
    db = await get_db()
    async with db.execute("SELECT * FROM friend_requests WHERE id = ?", (request_id,)) as cur:
        row = await cur.fetchone()
    if not row or row["to_id"] != to_id or row["status"] != "pending":
        return None
    new_status = "accepted" if accept else "declined"
    await db.execute("UPDATE friend_requests SET status = ? WHERE id = ?", (new_status, request_id))
    await db.commit()
    return dict(row)


async def list_friends(telegram_id):
    db = await get_db()
    async with db.execute(
        """
        SELECT u.telegram_id, u.name, u.tag, u.last_seen
        FROM friend_requests fr
        JOIN users u ON u.telegram_id = (CASE WHEN fr.from_id = ? THEN fr.to_id ELSE fr.from_id END)
        WHERE fr.status = 'accepted' AND (fr.from_id = ? OR fr.to_id = ?)
        """,
        (telegram_id, telegram_id, telegram_id),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_incoming(telegram_id):
    db = await get_db()
    async with db.execute(
        """
        SELECT fr.id, fr.from_id, u.name, u.tag
        FROM friend_requests fr
        JOIN users u ON u.telegram_id = fr.from_id
        WHERE fr.to_id = ? AND fr.status = 'pending'
        """,
        (telegram_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_outgoing(telegram_id):
    db = await get_db()
    async with db.execute(
        """
        SELECT fr.id, fr.to_id, u.name, u.tag
        FROM friend_requests fr
        JOIN users u ON u.telegram_id = fr.to_id
        WHERE fr.from_id = ? AND fr.status = 'pending'
        """,
        (telegram_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


class PresenceRegistry:
    def __init__(self):
        self.online = {}      # telegram_id -> set[ws]
        self.ws_to_user = {}  # ws -> telegram_id

    def mark_online(self, telegram_id, ws):
        self.online.setdefault(telegram_id, set()).add(ws)
        self.ws_to_user[ws] = telegram_id

    def mark_offline(self, ws):
        telegram_id = self.ws_to_user.pop(ws, None)
        if telegram_id is None:
            return None
        conns = self.online.get(telegram_id)
        if conns:
            conns.discard(ws)
            if not conns:
                del self.online[telegram_id]
        return telegram_id

    def is_online(self, telegram_id):
        return bool(self.online.get(telegram_id))

    def get_sockets(self, telegram_id):
        return list(self.online.get(telegram_id, ()))
