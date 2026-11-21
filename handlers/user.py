import asyncio
from collections import defaultdict

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from database import (
    get_user, add_user, pop_invite, update_user_profile, has_report_today, save_report,
    employee_dashboard, user_reports, last_announcements, today_str,
    set_nickname, nickname_taken, get_request, upsert_request,
)
from handlers.common import (
    fmt_points, photos_of, notify_admins_new_report, chunk_text, clean_nick, NICK_HINT,
    notify_admins_request, pen_line,
)
from keyboards.menus import menu_for

router = Router()
MAX_PHOTOS = 10
_locks = defaultdict(asyncio.Lock)  # чтобы альбом фото не терял снимки при одновременной обработке


class NickForm(StatesGroup):
    nick = State()


class AccessRequest(StatesGroup):
    nick = State()


class ReportForm(StatesGroup):
    work = State()
    photos = State()


def status_line(r):
    if r.get("status") == "reviewed":
        line = f"✅ Проверен: {fmt_points(r.get('points') or 0)} б."
        return line + (f"\n💬 {r['review_comment']}" if r.get("review_comment") else "")
    return "⏳ Ожидает проверки"


# ---------- старт и доступ ----------

@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    tg = message.from_user
    user = await get_user(tg.id)
    just_added = False
    if not user and tg.username:
        invite = await pop_invite(tg.username)
        if invite:
            await add_user(tg.id, tg.username, tg.full_name, invite["role"], invite["is_admin"])
            user = await get_user(tg.id)
            just_added = True
    if not user:
        req = await get_request(tg.id)
        if req and req["status"] == "pending":
            return await message.answer("⏳ Ваша заявка уже отправлена администратору. "
                                        "Когда её одобрят, придёт сообщение.")
        if req and req["status"] == "rejected":
            return await message.answer("⛔ Администратор отклонил вашу заявку. Если это ошибка — свяжитесь с ним.")
        await state.set_state(AccessRequest.nick)
        return await message.answer(
            "⛔ У вас пока нет доступа.\nВведите ваш ник — я отправлю заявку администратору.\n"
            "Для отмены: /cancel", reply_markup=ReplyKeyboardRemove())
    await update_user_profile(tg.id, tg.username, tg.full_name)
    greeting = "✅ Доступ открыт!\n" if just_added else ""
    if user.get("nickname") is None:
        await state.set_state(NickForm.nick)
        return await message.answer(
            f"{greeting}Здравствуйте! Должность: {user['role']}\n\n"
            "Введите ваш ник — так вас будут видеть в списках и отчётах.",
            reply_markup=ReplyKeyboardRemove())
    await message.answer(
        f"{greeting}Здравствуйте, {user['full_name']}!\nДолжность: {user['role']}",
        reply_markup=menu_for(user),
    )


@router.message(Command("nick"))
async def change_nick(message: Message, state: FSMContext):
    if not await get_user(message.from_user.id):
        return
    await state.set_state(NickForm.nick)
    await message.answer("Введите новый ник. Для отмены: /cancel")


@router.message(Command("cancel"))
async def cancel_user_action(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    had_state = await state.get_state() is not None
    await state.clear()
    if not user:
        return await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Действие отменено." if had_state else "Отменять нечего.", reply_markup=menu_for(user))


# ---------- сдача отчёта ----------

@router.message(F.text == "📝 Сдать отчёт")
async def report_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if not user:
        return await message.answer("⛔ Нет доступа.")
    if await has_report_today(message.from_user.id):
        today = [r for r in await user_reports(message.from_user.id, 1) if r["report_date"] == today_str()]
        extra = f"\n{status_line(today[0])}" if today else ""
        return await message.answer(f"Вы уже отправили отчёт за сегодня.{extra}", reply_markup=menu_for(user))
    await state.set_state(ReportForm.work)
    await message.answer("📝 Напишите, что вы сделали сегодня.\nДля отмены отправьте /cancel.")


@router.message(F.text == "📊 Моя статистика")
async def my_stats(message: Message, state: FSMContext):
    await state.clear()
    if not await get_user(message.from_user.id):
        return
    d = await employee_dashboard(message.from_user.id)
    u = d["user"]
    penalties = "; ".join(pen_line(p) for p in d["penalties"]) or "нет"
    notes = "\n".join(f"• {n['note']}" for n in d["notes"]) or "нет"
    await message.answer(
        f"📊 ВАША СТАТИСТИКА\n\nНик: @{u['username'] or 'не указан'}\nДолжность: {u['role']}\n\n"
        f"⭐ Баллы: {d['points_total']} всего, {d['points_week']} за неделю\n"
        f"📝 Отчётов: {d['reports_total']} всего, {d['reports_week']} за неделю\n"
        f"⏳ На проверке: {d['pending']}\n\n"
        f"⚠️ Наказания: {penalties}\n\n🗒 Примечания:\n{notes}"
    )


@router.message(F.text == "⚠️ Мои наказания")
async def my_penalties(message: Message, state: FSMContext):
    await state.clear()
    if not await get_user(message.from_user.id):
        return
    d = await employee_dashboard(message.from_user.id)
    await message.answer(
        "⚠️ Действующие наказания:\n" + (
            "\n".join(f"• {pen_line(p)} ({p['created_at'][:10]})" for p in d["penalties"])
            if d["penalties"] else "Нет действующих наказаний."))


@router.message(F.text == "📚 История отчётов")
async def history(message: Message, state: FSMContext):
    await state.clear()
    if not await get_user(message.from_user.id):
        return
    rows = await user_reports(message.from_user.id, 10)
    if not rows:
        return await message.answer("Отчётов пока нет.")
    for r in rows:
        text = f"📅 {r['report_date']} · фото: {len(photos_of(r))}\n{status_line(r)}\n\n{r['work_text']}"
        await message.answer(text[:4000])


@router.message(F.text == "📢 Объявления")
async def announcements(message: Message, state: FSMContext):
    await state.clear()
    if not await get_user(message.from_user.id):
        return
    rows = await last_announcements(5)
    if not rows:
        return await message.answer("Объявлений пока нет.")
    text = "\n\n".join(f"📅 {a['created_at'][:10]}\n{a['text']}" for a in reversed(rows))
    for part in chunk_text("📢 ПОСЛЕДНИЕ ОБЪЯВЛЕНИЯ\n\n" + text):
        await message.answer(part)


# ---------- шаги отчёта (после кнопок меню, чтобы меню работало в любом состоянии) ----------

@router.message(NickForm.nick, F.text, ~F.text.startswith("/"))
async def nick_save(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await get_user(uid):
        await state.clear()
        return
    nick = clean_nick(message.text)
    if not nick:
        return await message.answer(NICK_HINT)
    if await nickname_taken(nick, uid):
        return await message.answer("Такой ник уже занят. Введите другой.")
    await set_nickname(uid, nick)
    await state.clear()
    await message.answer(f"✅ Ник сохранён: {nick}", reply_markup=menu_for(await get_user(uid)))


@router.message(AccessRequest.nick, F.text, ~F.text.startswith("/"))
async def request_access(message: Message, state: FSMContext, bot: Bot):
    tg = message.from_user
    if await get_user(tg.id):
        await state.clear()
        return await message.answer("У вас уже есть доступ. Нажмите /start.")
    nick = clean_nick(message.text)
    if not nick:
        return await message.answer(NICK_HINT)
    await state.clear()
    await upsert_request(tg.id, nick, tg.username, tg.full_name)
    await message.answer("✅ Заявка отправлена администратору. Когда её одобрят, придёт сообщение.")
    await notify_admins_request(bot, tg.id)


@router.message(ReportForm.work, F.text, ~F.text.startswith("/"))
async def report_work(message: Message, state: FSMContext):
    work_text = message.text.strip()
    if not work_text:
        return await message.answer("Опишите выполненную работу текстом или отправьте /cancel.")
    await state.update_data(work_text=work_text, photos=[], last_group=None)
    await state.set_state(ReportForm.photos)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Завершить отправку")], [KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
    )
    await message.answer(
        "📷 Теперь отправьте фото-доказательства (можно несколько, до 10).\n"
        "Когда закончите — нажмите «✅ Завершить отправку». Если фото нет — тоже нажмите её.",
        reply_markup=keyboard,
    )


@router.message(ReportForm.work)
async def report_work_invalid(message: Message):
    await message.answer("Нужно отправить текстовое описание работы. Попробуйте ещё раз или /cancel.")


@router.message(ReportForm.photos, F.photo)
async def collect_photo(message: Message, state: FSMContext):
    async with _locks[message.from_user.id]:
        data = await state.get_data()
        photos = data.get("photos", [])
        if len(photos) >= MAX_PHOTOS:
            if not message.media_group_id or data.get("last_group") != message.media_group_id:
                await state.update_data(last_group=message.media_group_id)
                await message.answer(f"Максимум {MAX_PHOTOS} фото. Нажмите «✅ Завершить отправку».")
            return
        photos.append(message.photo[-1].file_id)
        group = message.media_group_id
        notify = not group or data.get("last_group") != group
        await state.update_data(photos=photos, last_group=group)
    if notify:
        await message.answer(f"✅ Фото добавлено ({len(photos)}). Можно отправить ещё или завершить.")


@router.message(ReportForm.photos, F.text == "❌ Отменить")
async def cancel_report(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отчёт отменён.", reply_markup=menu_for(await get_user(message.from_user.id)))


@router.message(ReportForm.photos, F.text == "✅ Завершить отправку")
async def finish_report(message: Message, state: FSMContext, bot: Bot):
    user = await get_user(message.from_user.id)
    if not user:
        await state.clear()
        return await message.answer("⛔ Нет доступа.")
    data = await state.get_data()
    if not data.get("work_text"):
        return await message.answer("Не найден текст отчёта. Отправьте /cancel и начните заново.")
    try:
        report_id = await save_report(message.from_user.id, data["work_text"], data.get("photos", []))
    except Exception:
        await state.clear()
        return await message.answer(
            "❌ Не удалось сохранить отчёт. Возможно, отчёт за сегодня уже существует — "
            "проверьте «📚 История отчётов».", reply_markup=menu_for(user))
    await state.clear()
    await message.answer("✅ Отчёт отправлен на проверку. Баллы выставит администратор — я пришлю уведомление.",
                         reply_markup=menu_for(user))
    await notify_admins_new_report(bot, report_id)


@router.message(ReportForm.photos)
async def photos_invalid(message: Message):
    await message.answer("Отправьте фото, нажмите «✅ Завершить отправку» или «❌ Отменить».")


@router.callback_query()
async def stray_callback(callback: CallbackQuery):
    await callback.answer("Нет доступа или кнопка устарела.", show_alert=True)
