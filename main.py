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

from rooms import RoomError, RoomRegistry

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

GAME_PATH = os.path.join(os.path.dirname(__file__), "game.html")
room_registry = RoomRegistry()


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
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("❌ BOT_TOKEN не знайдено! Задай його в змінних середовища.")

    bot = Bot(token=bot_token)
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())

    await run_web_server()

    logger.info("🤖 Бот запускається...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
