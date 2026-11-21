import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN, ADMIN_IDS
from database import init_db, ensure_admin
from handlers import user, admin
from reminders import reminder_loop


async def main():
    logging.basicConfig(level=logging.INFO)
    if ":" not in BOT_TOKEN:
        raise RuntimeError("Открой config.py и вставь токен от BotFather в BOT_TOKEN.")
    await init_db()

    # ID из ADMIN_IDS всегда получают права администратора.
    for user_id in ADMIN_IDS:
        await ensure_admin(user_id)

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)
    dp.include_router(user.router)
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="nick", description="Сменить ник"),
        BotCommand(command="cancel", description="Отменить действие"),
    ])
    reminders = asyncio.create_task(reminder_loop(bot))
    print("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
