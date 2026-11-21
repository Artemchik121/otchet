import json
import re

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from config import QUICK_POINTS, ROLES
from database import admin_ids, get_report, review_report, get_request

POINTS_RE = re.compile(r"^([+-]?\d{1,3})(?:\s+(.+))?$", re.S)


def parse_points(text):
    """'7' / '7 молодец' / '-3 мало доказательств' -> (баллы, комментарий) или None."""
    match = POINTS_RE.match((text or "").strip())
    if not match:
        return None
    amount = int(match.group(1))
    if abs(amount) > 100:
        return None
    return amount, (match.group(2) or "").strip()


PENALTY_KINDS = {"w": "Предупреждение", "r": "Выговор"}


def pen_line(p):
    return f"{p.get('kind') or 'Наказание'}: {p['reason']}"


def penalty_kind_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Предупреждение", callback_data=f"pk:w:{uid}"),
         InlineKeyboardButton(text="🚫 Выговор", callback_data=f"pk:r:{uid}")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_inline")],
    ])


NICK_HINT = "Ник должен быть от 2 до 32 символов, одной строкой. Попробуйте ещё раз."


def clean_nick(text):
    nick = " ".join((text or "").split())
    return nick if 2 <= len(nick) <= 32 else None


async def notify_admins_request(bot: Bot, user_id: int):
    req = await get_request(user_id)
    if not req:
        return
    tg = f"@{req['username']}" if req.get("username") else "ник в Telegram не задан"
    rows = [[InlineKeyboardButton(text=role, callback_data=f"rqok:{user_id}:{i}")]
            for i, role in enumerate(ROLES)]
    rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rqno:{user_id}")])
    text = (f"🆕 Заявка на доступ\nНик: {req['nickname']}\nTelegram: {tg}\n\n"
            "Выберите должность, чтобы одобрить:")
    for admin_id in await admin_ids():
        try:
            await bot.send_message(admin_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass


def fmt_points(n):
    return "0" if n == 0 else f"{n:+d}"


def chunk_text(text, size=3900):
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [text]


def photos_of(report):
    try:
        return json.loads(report.get("photos") or "[]")
    except (ValueError, TypeError):
        return []


def report_text(r, show_author=True):
    lines = []
    if show_author:
        nick = f" (@{r['username']})" if r.get("username") else ""
        lines += [f"👤 {r['full_name']}{nick}", r["role"]]
    lines.append(f"📅 {r['report_date']} · фото: {len(photos_of(r))}")
    if r.get("status") == "reviewed":
        status = f"✅ Проверен: {fmt_points(r.get('points') or 0)} б."
        if r.get("review_comment"):
            status += f"\n💬 {r['review_comment']}"
    else:
        status = "⏳ Ожидает проверки"
    work = r["work_text"].strip()
    if len(work) > 3000:
        work = work[:3000] + "…"
    lines += [status, "", work]
    return "\n".join(lines)


def report_keyboard(r):
    rid = r["id"]
    if r.get("status") == "reviewed":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Изменить оценку", callback_data=f"rvc:{rid}")
        ]])
    quick = [
        InlineKeyboardButton(text=fmt_points(p), callback_data=f"rv:{rid}:{p}") for p in QUICK_POINTS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        quick,
        [InlineKeyboardButton(text="✏️ Своё число / комментарий", callback_data=f"rvc:{rid}")],
    ])


async def send_report(bot: Bot, chat_id: int, r, keyboard=True):
    """Фото отчёта (если есть), затем текст с кнопками выставления баллов."""
    photos = photos_of(r)
    try:
        for i in range(0, len(photos), 10):
            part = photos[i:i + 10]
            if len(part) == 1:
                await bot.send_photo(chat_id, part[0])
            else:
                await bot.send_media_group(chat_id, [InputMediaPhoto(media=f) for f in part])
    except Exception:
        await bot.send_message(chat_id, "⚠️ Не удалось загрузить фото отчёта.")
    await bot.send_message(chat_id, report_text(r), reply_markup=report_keyboard(r) if keyboard else None)


async def notify_admins_new_report(bot: Bot, report_id: int):
    r = await get_report(report_id)
    if not r:
        return
    for admin_id in await admin_ids():
        try:
            await bot.send_message(admin_id, "🔔 Новый отчёт на проверку")
            await send_report(bot, admin_id, r)
        except Exception:
            pass


async def apply_review(bot: Bot, report_id: int, points: int, comment: str, admin_id: int):
    """Выставляет баллы за отчёт и сообщает об этом сотруднику."""
    r = await review_report(report_id, points, comment or None, admin_id)
    if not r:
        return None
    text = f"📋 Ваш отчёт за {r['report_date']} проверен.\n⭐ Баллы: {fmt_points(points)}"
    if comment:
        text += f"\n💬 {comment}"
    try:
        await bot.send_message(r["user_id"], text)
    except Exception:
        pass
    return r
