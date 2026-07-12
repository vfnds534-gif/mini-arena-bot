import asyncio
import hashlib
import json
import logging
import os

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

import db
import friends
from rooms import RoomError, RoomRegistry

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

GAME_PATH = os.path.join(os.path.dirname(__file__), "game.html")
room_registry = RoomRegistry()
presence_registry = friends.PresenceRegistry()
bot_instance = None
bot_username = None


def _compute_build_version():
    # Telegram's in-app WebView can keep serving a stale copy of the Mini
    # App across restarts unless the URL itself changes, so every deploy
    # needs a fresh cache-busting query param regardless of HTTP headers.
    with open(GAME_PATH, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:10]


BUILD_VERSION = _compute_build_version()


async def serve_game(request):
    with open(GAME_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(
        body=content.encode("utf-8"),
        content_type="text/html",
        charset="utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


async def send_json(ws, data):
    try:
        await ws.send_json(data)
    except ConnectionResetError:
        pass


async def broadcast(sockets, data):
    for ws in sockets:
        await send_json(ws, data)


async def handle_relay_message(ws, data):
    msg_type = data.get("type")

    if msg_type == "identify":
        telegram_id = str(data.get("telegramId") or data.get("userId") or "").strip()
        name = data.get("name") or "Гравець"
        if not telegram_id:
            return
        user = await friends.get_or_create_user(telegram_id, name)
        presence_registry.mark_online(telegram_id, ws)
        await send_json(ws, {"type": "identified", "tag": user["tag"], "telegramId": telegram_id})
        return

    if msg_type == "get_friends":
        telegram_id = presence_registry.ws_to_user.get(ws)
        if not telegram_id:
            return
        friends_list = await friends.list_friends(telegram_id)
        for f in friends_list:
            f["online"] = presence_registry.is_online(f["telegram_id"])
        incoming = await friends.list_incoming(telegram_id)
        outgoing = await friends.list_outgoing(telegram_id)
        await send_json(ws, {"type": "friends_data", "friends": friends_list, "incoming": incoming, "outgoing": outgoing})
        return

    if msg_type == "add_friend":
        telegram_id = presence_registry.ws_to_user.get(ws)
        if not telegram_id:
            return
        tag = (data.get("tag") or "").strip().upper()
        target_id = await friends.find_by_tag(tag)
        if not target_id:
            await send_json(ws, {"type": "add_friend_result", "ok": False, "reason": "not_found"})
            return
        result = await friends.send_friend_request(telegram_id, target_id)
        await send_json(ws, {"type": "add_friend_result", "ok": result == "ok", "reason": result})
        if result == "ok":
            for target_ws in presence_registry.get_sockets(target_id):
                await send_json(target_ws, {"type": "friend_request_received"})
        return

    if msg_type == "respond_friend_request":
        telegram_id = presence_registry.ws_to_user.get(ws)
        if not telegram_id:
            return
        request_id = data.get("requestId")
        accept = bool(data.get("accept"))
        row = await friends.respond_friend_request(request_id, telegram_id, accept)
        await send_json(ws, {"type": "respond_friend_request_result", "ok": row is not None})
        if row:
            for from_ws in presence_registry.get_sockets(row["from_id"]):
                await send_json(from_ws, {"type": "friend_request_responded", "accepted": accept})
        return

    if msg_type == "invite_friend":
        conn = room_registry.get_connection(ws)
        if not conn or conn.role != "host":
            await send_json(ws, {"type": "invite_result", "ok": False, "reason": "not_hosting"})
            return
        friend_id = data.get("friendTelegramId")
        if not friend_id or not bot_instance or not bot_username:
            await send_json(ws, {"type": "invite_result", "ok": False, "reason": "unavailable"})
            return
        inviter_name = data.get("inviterName") or "Друг"
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎮 Приєднатися",
                    url=f"https://t.me/{bot_username}?start={conn.room_code}",
                )
            ]])
            await bot_instance.send_message(
                int(friend_id),
                f"🎮 {inviter_name} запрошує тебе зіграти в Mini Arena!",
                reply_markup=kb,
            )
            await send_json(ws, {"type": "invite_result", "ok": True})
        except Exception as e:
            logger.warning(f"Не вдалося надіслати запрошення {friend_id}: {e}")
            await send_json(ws, {"type": "invite_result", "ok": False, "reason": "send_failed"})
        return

    if msg_type == "create_room":
        room = room_registry.create_room(ws, data.get("userId"), data.get("name", "Host"))
        await send_json(ws, {"type": "room_created", "code": room.code, "selfId": data.get("userId")})
        return

    if msg_type == "join_room":
        try:
            room, slot = room_registry.join_room(
                ws, data.get("code", ""), data.get("userId"), data.get("name", "Guest")
            )
        except RoomError as e:
            await send_json(ws, {"type": "join_error", "reason": e.reason})
            return
        await send_json(ws, {
            "type": "joined",
            "code": room.code,
            "selfId": data.get("userId"),
            "slot": slot,
            "roster": room.roster(),
            "hostName": room.host.name,
        })
        await broadcast(
            [room.host.ws] + [g.ws for g in room.guests.values()],
            {"type": "roster", "roster": room.roster(), "hostName": room.host.name},
        )
        return

    if msg_type == "leave_room":
        await handle_disconnect(ws)
        return

    if msg_type == "start_match":
        conn = room_registry.get_connection(ws)
        if not conn or conn.role != "host":
            return
        room = room_registry.get_room(conn.room_code)
        if not room:
            return
        room_registry.mark_started(room)
        await broadcast(
            [g.ws for g in room.guests.values()],
            {"type": "from_host", "payload": data.get("payload")},
        )
        return

    if msg_type == "to_guests":
        conn = room_registry.get_connection(ws)
        if not conn or conn.role != "host":
            return
        room = room_registry.get_room(conn.room_code)
        if not room:
            return
        await broadcast(
            [g.ws for g in room.guests.values()],
            {"type": "from_host", "payload": data.get("payload")},
        )
        return

    if msg_type == "to_host":
        conn = room_registry.get_connection(ws)
        if not conn or conn.role != "guest":
            return
        room = room_registry.get_room(conn.room_code)
        if not room:
            return
        await send_json(room.host.ws, {
            "type": "from_guest",
            "fromId": conn.user_id,
            "slot": conn.slot,
            "payload": data.get("payload"),
        })
        return


async def handle_disconnect(ws):
    telegram_id = presence_registry.mark_offline(ws)
    if telegram_id:
        await friends.touch_last_seen(telegram_id)

    kind, room, targets = room_registry.remove_connection(ws)
    if not kind:
        return
    if kind == "room_closed":
        await broadcast(targets, {"type": "room_closed"})
    else:
        await broadcast(targets, {"type": "roster", "roster": room.roster(), "hostName": room.host.name})


async def ws_relay(request):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            await handle_relay_message(ws, data)
    finally:
        await handle_disconnect(ws)
    return ws


def create_web_app():
    app = web.Application()
    app.router.add_get("/", serve_game)
    app.router.add_get("/game", serve_game)
    app.router.add_get("/ws", ws_relay)
    return app


async def run_web_server():
    port = int(os.getenv("PORT", "8080"))
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Веб-сервер запущено на порту {port}")

    if not os.getenv("WEB_URL"):
        logger.warning("⚠️ WEB_URL не задано. Постав WEB_URL в Railway Variables.")


async def cmd_start(message: Message, command: CommandObject):
    web_url = os.getenv("WEB_URL", "").strip()
    if web_url and web_url.startswith("https://"):
        room_code = command.args
        url = f"{web_url}?room={room_code}&v={BUILD_VERSION}" if room_code else f"{web_url}?v={BUILD_VERSION}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎮 Грати", web_app=WebAppInfo(url=url))
        ]])
    else:
        kb = None
        logger.warning("WEB_URL відсутній або не https — кнопка гри не показана")

    await message.answer(
        "🎮 <b>Mini Arena</b>\n\nТисни кнопку нижче, щоб зіграти!",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def main():
    global bot_instance, bot_username

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("❌ BOT_TOKEN не знайдено! Задай його в змінних середовища.")

    await db.init_db()

    bot = Bot(token=bot_token)
    bot_instance = bot
    me = await bot.get_me()
    bot_username = me.username

    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())

    await run_web_server()

    logger.info("🤖 Бот запускається...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
