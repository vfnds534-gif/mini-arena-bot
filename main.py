import asyncio
import logging
import os

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

GAME_PATH = os.path.join(os.path.dirname(__file__), "game.html")


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


def create_web_app():
    app = web.Application()
    app.router.add_get("/", serve_game)
    app.router.add_get("/game", serve_game)
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


async def cmd_start(message: Message):
    web_url = os.getenv("WEB_URL", "").strip()
    if web_url and web_url.startswith("https://"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎮 Грати", web_app=WebAppInfo(url=web_url))
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
