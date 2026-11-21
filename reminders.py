import asyncio
import logging

from aiogram import Bot

from config import REMINDER_TIME, REPORT_DEADLINE
from database import (
    now, get_setting, set_setting, users_without_report_today, admin_ids,
)


async def reminder_loop(bot: Bot):
    """Раз в полминуты проверяет время: напоминает сотрудникам и сообщает админам о должниках."""
    while True:
        try:
            t = now()
            hm, day = t.strftime("%H:%M"), t.date().isoformat()

            if REMINDER_TIME and REMINDER_TIME <= hm < (REPORT_DEADLINE or "24:00") \
                    and not await get_setting(f"reminder:{day}"):
                await set_setting(f"reminder:{day}", "1")
                for u in await users_without_report_today():
                    try:
                        await bot.send_message(
                            u["user_id"],
                            f"⏰ Напоминание: вы ещё не сдали отчёт за сегодня. "
                            f"Крайний срок — {REPORT_DEADLINE or '23:59'}. Нажмите «📝 Сдать отчёт».")
                    except Exception:
                        pass

            if REPORT_DEADLINE and hm >= REPORT_DEADLINE and not await get_setting(f"deadline:{day}"):
                await set_setting(f"deadline:{day}", "1")
                missing = await users_without_report_today()
                text = (f"⏰ Не сдали отчёт за {day}:\n" + "\n".join(
                    f"• {u['full_name']}" + (f" (@{u['username']})" if u['username'] else "") for u in missing)
                ) if missing else f"✅ За {day} отчёты сдали все."
                for admin_id in await admin_ids():
                    try:
                        await bot.send_message(admin_id, text)
                    except Exception:
                        pass
        except Exception:
            logging.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(30)
