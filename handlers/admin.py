import re
from datetime import date, timedelta

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS, ROLES
from database import (
    get_user, get_user_by_username, all_users, add_user, set_admin, set_role, deactivate_user,
    add_invite, list_invites, delete_invite, add_points, add_penalty, clear_penalties, clear_penalty, active_penalties, add_note,
    add_announcement, employee_dashboard, team_overview, recent_reports, pending_reports,
    get_report, now, purge_user, get_request, set_request_status, set_nickname,
)
from handlers.common import (
    parse_points, fmt_points, chunk_text, photos_of, report_text, report_keyboard,
    send_report, apply_review, pen_line, penalty_kind_kb, PENALTY_KINDS,
)
from keyboards.menus import admin_menu, user_menu

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = await get_user(event.from_user.id)
        return bool(user and user["is_admin"])


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminFlow(StatesGroup):
    add_target = State()
    add_role = State()
    review_points = State()
    points_input = State()
    penalty_input = State()
    note_input = State()
    msg_input = State()
    broadcast = State()


NICK_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
TEXT_INPUT = (F.text, ~F.text.startswith("/"))


# ---------- клавиатуры ----------

def employees_kb(users, action):
    rows = [[InlineKeyboardButton(
        text=f"{u['full_name']} · {u['role']}"[:60], callback_data=f"{action}:{u['user_id']}"
    )] for u in users]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def roles_kb(template):
    """template, например 'addrole:{i}' или 'setrole:123:{i}'."""
    rows = [[InlineKeyboardButton(text=role, callback_data=template.format(i=i))]
            for i, role in enumerate(ROLES)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_kb(emp):
    uid = emp["user_id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Баллы", callback_data=f"act:points:{uid}"),
         InlineKeyboardButton(text="⚠️ Наказание", callback_data=f"act:penalty:{uid}")],
        [InlineKeyboardButton(text="🗒 Примечание", callback_data=f"act:note:{uid}"),
         InlineKeyboardButton(text="✉️ Написать", callback_data=f"act:msg:{uid}")],
        [InlineKeyboardButton(text="✅ Снять наказание", callback_data=f"act:clearpen:{uid}"),
         InlineKeyboardButton(text="🔁 Должность", callback_data=f"act:role:{uid}")],
        [InlineKeyboardButton(
            text="👑 Снять админа" if emp["is_admin"] else "👑 Сделать админом",
            callback_data=f"act:admin:{uid}"),
         InlineKeyboardButton(text="🗑 Убрать", callback_data=f"act:remove:{uid}")],
    ])


def format_employee_card(d):
    u = d["user"]
    username = f"@{u['username']}" if u.get("username") else "не указан"
    lines = [
        f"👤 {u['full_name']}",
        f"Должность: {u['role']}",
        f"Telegram: {username}",
        f"Админ: {'да' if u['is_admin'] else 'нет'}",
        "",
        "📊 СТАТИСТИКА",
        f"Отчётов: {d['reports_total']} (за неделю: {d['reports_week']}, на проверке: {d['pending']})",
        f"Баллов: {d['points_total']} (за неделю: {d['points_week']})",
        f"Последний отчёт: {d['last_report'] or 'не сдавал'}",
        "",
        "📝 ПОСЛЕДНИЕ ОТЧЁТЫ",
    ]
    if d["reports"]:
        for r in d["reports"]:
            status = f"✅ {fmt_points(r['points'] or 0)} б." if r.get("status") == "reviewed" else "⏳ на проверке"
            excerpt = r["work_text"].strip()
            excerpt = excerpt[:300] + "…" if len(excerpt) > 300 else excerpt
            lines.append(f"\n📅 {r['report_date']} · фото: {len(photos_of(r))} · {status}\n{excerpt}")
    else:
        lines.append("Отчётов пока нет.")
    lines += ["", "⭐ ОПЕРАЦИИ С БАЛЛАМИ"]
    if d["points_history"]:
        for p in d["points_history"]:
            lines.append(f"{fmt_points(p['amount'])} · {p['reason'] or 'Без причины'} · {p['created_at'][:10]}")
    else:
        lines.append("Операций пока нет.")
    lines += ["", "⚠️ ДЕЙСТВУЮЩИЕ НАКАЗАНИЯ"]
    if d["penalties"]:
        lines += [f"• {pen_line(p)} · {p['created_at'][:10]}" for p in d["penalties"]]
    else:
        lines.append("Нет действующих наказаний.")
    lines += ["", "🗒 ПРИМЕЧАНИЯ"]
    if d["notes"]:
        lines += [f"• {n['created_at'][:10]} · {n['note']}" for n in d["notes"]]
    else:
        lines.append("Примечаний нет.")
    return "\n".join(lines)


async def send_long(message: Message, text, reply_markup=None):
    chunks = chunk_text(text)
    for i, part in enumerate(chunks):
        await message.answer(part, reply_markup=reply_markup if i == len(chunks) - 1 else None)


# ---------- общее ----------

@router.message(Command("cancel"))
async def cancel_admin_action(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return await message.answer("Отменять нечего.", reply_markup=admin_menu())
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=admin_menu())


@router.callback_query(F.data == "cancel_inline")
async def cancel_inline(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено")
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass


@router.message(F.text == "🛠 Админ-панель")
async def to_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-панель:", reply_markup=admin_menu())


@router.message(F.text == "🏠 Пользовательское меню")
async def to_user_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Пользовательское меню:", reply_markup=user_menu(is_admin=True))


# ---------- проверка отчётов ----------

@router.message(F.text == "📥 На проверке")
async def pending(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    rows = await pending_reports(20)
    if not rows:
        return await message.answer("Все отчёты проверены 🎉", reply_markup=admin_menu())
    await message.answer(f"📥 Ожидают проверки: {len(rows)}\nВыберите баллы под каждым отчётом.")
    for r in rows:
        await send_report(bot, message.chat.id, r)


@router.message(F.text == "📋 Последние отчёты")
async def last_reports(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    rows = await recent_reports(10)
    if not rows:
        return await message.answer("Отчётов пока нет.", reply_markup=admin_menu())
    for r in reversed(rows):
        await send_report(bot, message.chat.id, r)


async def refresh_report_message(message, r):
    try:
        await message.edit_text(report_text(r), reply_markup=report_keyboard(r))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(r"^rv:\d+:-?\d+$"))
async def review_quick(callback: CallbackQuery, bot: Bot):
    _, rid, pts = callback.data.split(":")
    r = await apply_review(bot, int(rid), int(pts), "", callback.from_user.id)
    if not r:
        return await callback.answer("Отчёт не найден.", show_alert=True)
    await callback.answer(f"Баллы: {fmt_points(int(pts))}. Сотрудник уведомлён.")
    await refresh_report_message(callback.message, r)


@router.callback_query(F.data.regexp(r"^rvc:\d+$"))
async def review_custom(callback: CallbackQuery, state: FSMContext):
    rid = int(callback.data.split(":")[1])
    r = await get_report(rid)
    if not r:
        return await callback.answer("Отчёт не найден.", show_alert=True)
    await callback.answer()
    await state.set_state(AdminFlow.review_points)
    await state.update_data(report_id=rid, chat_id=callback.message.chat.id, msg_id=callback.message.message_id)
    await callback.message.answer(
        f"✏️ Отчёт {r['full_name']} за {r['report_date']}.\n"
        "Отправьте количество баллов и, если нужно, комментарий для сотрудника.\n"
        "Примеры: 7  ·  7 Отличная работа  ·  -3 Мало доказательств\n"
        "/cancel — отмена"
    )


# ---------- сотрудники ----------

@router.message(F.text == "👥 Сотрудники")
async def employees(message: Message, state: FSMContext):
    await state.clear()
    team = await team_overview()
    invites = await list_invites()
    if not team and not invites:
        return await message.answer("Список сотрудников пока пуст.", reply_markup=admin_menu())
    lines = [f"👥 СОТРУДНИКИ ({len(team)})", "Нажмите на сотрудника, чтобы открыть карточку.", ""]
    for u in team:
        crown = "👑 " if u["is_admin"] else ""
        today = "✅" if u["today"] else "—"
        tg = f" (@{u['username']})" if u.get("username") else ""
        lines.append(
            f"{crown}{u['full_name']}{tg} — {u['role']}\n"
            f"   сегодня: {today} · неделя: {u['reports_week']} отч., {u['points_week']} б. · "
            f"на проверке: {u['pending']}"
        )
    chunks = chunk_text("\n".join(lines))
    for part in chunks[:-1]:
        await message.answer(part)
    await message.answer(chunks[-1], reply_markup=employees_kb(team, "employee") or admin_menu())
    if invites:
        rows = [[InlineKeyboardButton(text=f"❌ Отозвать @{i['username']}", callback_data=f"delinv:{i['username']}")]
                for i in invites]
        text = "⏳ Ждут первого входа (доступ откроется, когда человек нажмёт /start):\n" + "\n".join(
            f"• @{i['username']} — {i['role']}" for i in invites)
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("delinv:"))
async def invite_delete(callback: CallbackQuery):
    await delete_invite(callback.data.split(":", 1)[1])
    await callback.answer("Приглашение отозвано.")
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(r"^employee:\d+$"))
async def employee_detail(callback: CallbackQuery):
    data = await employee_dashboard(int(callback.data.split(":")[1]))
    if not data:
        return await callback.answer("Сотрудник не найден.", show_alert=True)
    await callback.answer()
    await send_long(callback.message, format_employee_card(data), card_kb(data["user"]))


@router.message(F.text == "🏆 Рейтинг")
async def rating(message: Message, state: FSMContext):
    await state.clear()
    team = [u for u in await team_overview() if not u["is_admin"]]
    if not team:
        return await message.answer("Сотрудников для рейтинга пока нет.", reply_markup=admin_menu())
    team.sort(key=lambda u: (-u["points_week"], -u["reports_week"], u["full_name"].lower()))
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 РЕЙТИНГ ЗА НЕДЕЛЮ", ""]
    for i, u in enumerate(team):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {u['full_name']} — {u['points_week']} б. · отчётов: {u['reports_week']}")
    await send_long(message, "\n".join(lines), admin_menu())


# ---------- добавление сотрудника по нику ----------

@router.message(F.text == "➕ Добавить сотрудника")
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminFlow.add_target)
    await message.answer(
        "➕ Отправьте @ник сотрудника в Telegram (например, @ivan_petrov).\n\n"
        "💡 Можно вообще ничего не вводить: пусть человек сам нажмёт /start и введёт свой ник — "
        "вам придёт заявка с кнопками выбора должности.\n\n"
        "Дальше выберете должность — и всё. Когда человек откроет бота и нажмёт /start, "
        "доступ включится автоматически, ID вводить не нужно.\n\n"
        "(Если у человека нет ника, можно прислать его числовой Telegram ID.)\n"
        "/cancel — отмена"
    )


@router.message(AdminFlow.add_target, *TEXT_INPUT)
async def add_target(message: Message, state: FSMContext):
    text = re.sub(r"^(https?://)?t\.me/", "", message.text.strip(), flags=re.I)
    if text.isdigit() and int(text) > 0:
        target = {"id": int(text)}
    elif NICK_RE.match(text):
        target = {"nick": NICK_RE.match(text).group(1).lower()}
    else:
        return await message.answer(
            "❌ Не похоже на ник. Пришлите @ник (латиница, цифры, _; минимум 5 символов) или /cancel."
        )
    await state.update_data(target=target)
    await state.set_state(AdminFlow.add_role)
    await message.answer("Выберите должность:", reply_markup=roles_kb("addrole:{i}"))


@router.callback_query(AdminFlow.add_role, F.data.regexp(r"^addrole:\d+$"))
async def add_role(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    target = data.get("target")
    if not target or idx >= len(ROLES):
        await state.clear()
        return await callback.answer("Что-то пошло не так, начните заново.", show_alert=True)
    role = ROLES[idx]
    await callback.answer()
    await state.clear()
    if "id" in target:
        uid = target["id"]
        if await get_user(uid):
            await set_role(uid, role)
            text = f"✅ Сотрудник с ID {uid} уже был в списке — должность обновлена: {role}."
        else:
            await add_user(uid, None, f"Сотрудник {uid}", role, 0)
            text = f"✅ Добавлен сотрудник ID {uid}.\nДолжность: {role}\nПопросите его нажать /start."
    else:
        nick = target["nick"]
        existing = await get_user_by_username(nick)
        if existing:
            await set_role(existing["user_id"], role)
            text = f"✅ @{nick} уже в списке — должность обновлена: {role}."
        else:
            await add_invite(nick, role, 0)
            text = (f"✅ Приглашение для @{nick} создано.\nДолжность: {role}\n\n"
                    f"Пусть @{nick} откроет бота и нажмёт /start — доступ включится сам.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text, reply_markup=admin_menu())


# ---------- действия над сотрудником (баллы, наказание, примечание ...) ----------

ACTION_TITLES = {"points": "⭐ Кому изменить баллы?", "penalty": "⚠️ Кому выдать наказание?",
                 "note": "🗒 Кому добавить примечание?", "remove": "🗑 Кого удалить?",
                 "clearpen": "✅ У кого снять наказание?"}


async def pick_employee(message: Message, state: FSMContext, action):
    await state.clear()
    users = await all_users()
    if action == "clearpen":
        users = [u for u in await team_overview() if u["penalties_active"] > 0]
        if not users:
            return await message.answer("Действующих наказаний ни у кого нет 👍", reply_markup=admin_menu())
    if action == "remove":
        users = [u for u in users if u["user_id"] != message.from_user.id and u["user_id"] not in ADMIN_IDS]
    if not users:
        return await message.answer("Подходящих сотрудников нет.", reply_markup=admin_menu())
    await message.answer(ACTION_TITLES[action], reply_markup=employees_kb(users, f"act:{action}"))


@router.message(F.text == "⭐ Баллы")
async def menu_points(message: Message, state: FSMContext):
    await pick_employee(message, state, "points")


@router.message(F.text == "⚠️ Наказание")
async def menu_penalty(message: Message, state: FSMContext):
    await pick_employee(message, state, "penalty")


@router.message(F.text == "🗒 Примечание")
async def menu_note(message: Message, state: FSMContext):
    await pick_employee(message, state, "note")


@router.message(F.text == "✅ Снять наказание")
async def menu_clearpen(message: Message, state: FSMContext):
    await pick_employee(message, state, "clearpen")


@router.message(F.text == "🗑 Удалить сотрудника")
async def menu_remove(message: Message, state: FSMContext):
    await pick_employee(message, state, "remove")


@router.callback_query(F.data.regexp(r"^act:[a-z]+:\d+$"))
async def action_router(callback: CallbackQuery, state: FSMContext, bot: Bot):
    _, action, uid_text = callback.data.split(":")
    uid = int(uid_text)
    emp = await get_user(uid)
    if not emp:
        return await callback.answer("Сотрудник не найден или уже убран.", show_alert=True)
    name = emp["full_name"]
    me = callback.from_user.id

    if action == "penalty":
        await callback.answer()
        return await callback.message.answer(
            f"⚠️ {name}\nВыберите вид наказания:", reply_markup=penalty_kind_kb(uid))

    if action in ("points", "penalty", "note", "msg"):
        await callback.answer()
        await state.set_state({
            "points": AdminFlow.points_input, "penalty": AdminFlow.penalty_input,
            "note": AdminFlow.note_input, "msg": AdminFlow.msg_input,
        }[action])
        await state.update_data(uid=uid)
        prompts = {
            "points": f"⭐ {name}\nОтправьте баллы и причину.\nПримеры: 10 Хорошая работа  ·  -5 Опоздание",
            "penalty": (f"⚠️ {name}\nОтправьте причину. Срок — после «|» (число дней или дата ГГГГ-ММ-ДД), "
                        "можно без срока.\nПримеры: Не сдан отчёт | 7  ·  Грубость | 2026-12-01  ·  Нарушение"),
            "note": f"🗒 {name}\nОтправьте текст примечания.",
            "msg": f"✉️ {name}\nОтправьте сообщение — оно придёт сотруднику от имени бота.",
        }
        return await callback.message.answer(prompts[action] + "\n/cancel — отмена")

    if action == "clearpen":
        if not await active_penalties(uid):
            return await callback.answer("У сотрудника нет действующих наказаний.", show_alert=True)
        await callback.answer()
        return await callback.message.answer(
            f"✅ {name}: какое наказание снять?", reply_markup=await penalties_kb(uid))

    if action == "admin":
        if uid == me or uid in ADMIN_IDS:
            return await callback.answer("Этому пользователю нельзя менять права.", show_alert=True)
        new_value = not emp["is_admin"]
        await set_admin(uid, new_value)
        await callback.answer()
        return await callback.message.answer(
            f"👑 {name}: права администратора {'выданы' if new_value else 'сняты'}.")

    if action == "role":
        await callback.answer()
        return await callback.message.answer(
            f"Новая должность для {name}:", reply_markup=roles_kb(f"setrole:{uid}:{{i}}"))

    if action == "remove":
        if uid == me or uid in ADMIN_IDS:
            return await callback.answer("Этого пользователя убрать нельзя.", show_alert=True)
        await callback.answer()
        return await callback.message.answer(
            f"🗑 Удалить {name}?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Убрать доступ (история останется)", callback_data=f"rmyes:{uid}")],
                [InlineKeyboardButton(text="💥 Удалить навсегда вместе с отчётами", callback_data=f"rmall:{uid}")],
                [InlineKeyboardButton(text="Отмена", callback_data="cancel_inline")],
            ]))
    await callback.answer()


async def penalties_kb(uid):
    pens = await active_penalties(uid)
    rows = [[InlineKeyboardButton(text=f"{pen_line(p)} · {p['created_at'][:10]}"[:60],
                                  callback_data=f"pdel:{p['id']}")] for p in pens]
    if len(pens) > 1:
        rows.append([InlineKeyboardButton(text="🧹 Снять все", callback_data=f"pdelall:{uid}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_inline")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^pk:[wr]:\d+$"))
async def penalty_kind(callback: CallbackQuery, state: FSMContext):
    _, code, uid_text = callback.data.split(":")
    emp = await get_user(int(uid_text))
    if not emp:
        return await callback.answer("Сотрудник не найден.", show_alert=True)
    await callback.answer()
    await state.set_state(AdminFlow.penalty_input)
    await state.update_data(uid=int(uid_text), kind=PENALTY_KINDS[code])
    await callback.message.edit_text(
        f"⚠️ {PENALTY_KINDS[code]} для {emp['full_name']}\nНапишите причину одним сообщением.\n/cancel — отмена")


@router.callback_query(F.data.regexp(r"^pdel:\d+$"))
async def penalty_remove(callback: CallbackQuery, bot: Bot):
    p = await clear_penalty(int(callback.data.split(":")[1]))
    if not p:
        return await callback.answer("Это наказание уже снято.", show_alert=True)
    await callback.answer("Снято.")
    await callback.message.edit_text(f"✅ Снято: {pen_line(p)}")
    try:
        await bot.send_message(p["user_id"], f"✅ С вас снято: {pen_line(p)}")
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^pdelall:\d+$"))
async def penalties_remove_all(callback: CallbackQuery, bot: Bot):
    uid = int(callback.data.split(":")[1])
    await clear_penalties(uid)
    await callback.answer("Сняты все.")
    await callback.message.edit_text("✅ Все наказания сняты.")
    try:
        await bot.send_message(uid, "✅ С вас сняты все наказания.")
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^setrole:\d+:\d+$"))
async def set_role_cb(callback: CallbackQuery):
    _, uid, idx = callback.data.split(":")
    if int(idx) >= len(ROLES) or not await get_user(int(uid)):
        return await callback.answer("Не получилось.", show_alert=True)
    await set_role(int(uid), ROLES[int(idx)])
    await callback.answer("Должность обновлена.")
    await callback.message.edit_text(f"✅ Должность изменена: {ROLES[int(idx)]}")


@router.callback_query(F.data.regexp(r"^rmyes:\d+$"))
async def remove_confirm(callback: CallbackQuery):
    uid = int(callback.data.split(":")[1])
    if uid == callback.from_user.id or uid in ADMIN_IDS:
        return await callback.answer("Этого пользователя убрать нельзя.", show_alert=True)
    await deactivate_user(uid)
    await callback.answer("Сотрудник убран.")
    await callback.message.edit_text("🗑 Доступ закрыт, история сохранена. Чтобы вернуть — добавьте его заново.")


@router.callback_query(F.data.regexp(r"^rmall:\d+$"))
async def remove_forever(callback: CallbackQuery):
    uid = int(callback.data.split(":")[1])
    if uid == callback.from_user.id or uid in ADMIN_IDS:
        return await callback.answer("Этого пользователя удалить нельзя.", show_alert=True)
    await purge_user(uid)
    await callback.answer("Удалён.")
    await callback.message.edit_text("💥 Сотрудник удалён навсегда вместе со всей историей.")


# ---------- заявки на доступ ----------

@router.callback_query(F.data.regexp(r"^rqok:\d+:\d+$"))
async def request_approve(callback: CallbackQuery, bot: Bot):
    _, uid_text, idx_text = callback.data.split(":")
    uid, idx = int(uid_text), int(idx_text)
    req = await get_request(uid)
    if not req or req["status"] != "pending" or idx >= len(ROLES):
        return await callback.answer("Заявка уже обработана.", show_alert=True)
    role = ROLES[idx]
    await add_user(uid, req["username"], req["nickname"], role, 0)
    await set_nickname(uid, req["nickname"])
    await set_request_status(uid, "approved")
    await callback.answer("Доступ выдан.")
    await callback.message.edit_text(f"✅ Заявка одобрена: {req['nickname']} — {role}")
    try:
        await bot.send_message(uid, f"✅ Доступ открыт!\nДолжность: {role}", reply_markup=user_menu())
    except Exception:
        pass


@router.callback_query(F.data.regexp(r"^rqno:\d+$"))
async def request_reject(callback: CallbackQuery, bot: Bot):
    uid = int(callback.data.split(":")[1])
    req = await get_request(uid)
    if not req or req["status"] != "pending":
        return await callback.answer("Заявка уже обработана.", show_alert=True)
    await set_request_status(uid, "rejected")
    await callback.answer("Отклонено.")
    await callback.message.edit_text(f"❌ Заявка отклонена: {req['nickname']}")
    try:
        await bot.send_message(uid, "⛔ Администратор отклонил вашу заявку.")
    except Exception:
        pass


# ---------- ввод данных (состояния) ----------
# Эти обработчики объявлены ПОСЛЕ кнопок меню, поэтому нажатие любой кнопки меню прерывает ввод.

@router.message(AdminFlow.review_points, *TEXT_INPUT)
async def review_points_finish(message: Message, state: FSMContext, bot: Bot):
    parsed = parse_points(message.text)
    if not parsed:
        return await message.answer("❌ Формат: число от -100 до 100 и (по желанию) комментарий.\n"
                                    "Пример: 7 Отличная работа. Или /cancel.")
    amount, comment = parsed
    data = await state.get_data()
    r = await apply_review(bot, data["report_id"], amount, comment, message.from_user.id)
    await state.clear()
    if not r:
        return await message.answer("Отчёт не найден.", reply_markup=admin_menu())
    try:
        await bot.edit_message_text(report_text(r), chat_id=data["chat_id"], message_id=data["msg_id"],
                                    reply_markup=report_keyboard(r))
    except TelegramBadRequest:
        pass
    await message.answer(f"✅ {r['full_name']}: {fmt_points(amount)} б. за отчёт от {r['report_date']}. "
                         "Сотрудник уведомлён.", reply_markup=admin_menu())


@router.message(AdminFlow.points_input, *TEXT_INPUT)
async def points_finish(message: Message, state: FSMContext, bot: Bot):
    parsed = parse_points(message.text)
    if not parsed or parsed[0] == 0:
        return await message.answer("❌ Формат: 10 Хорошая работа  или  -5 Опоздание. Или /cancel.")
    amount, reason = parsed
    uid = (await state.get_data())["uid"]
    emp = await get_user(uid)
    if not emp:
        await state.clear()
        return await message.answer("Сотрудник не найден.", reply_markup=admin_menu())
    await add_points(uid, amount, reason or "Корректировка баллов")
    await state.clear()
    try:
        await bot.send_message(uid, f"⭐ Вам изменили баллы: {fmt_points(amount)}"
                                    + (f"\n💬 {reason}" if reason else ""))
    except Exception:
        pass
    await message.answer(f"✅ {emp['full_name']}: {fmt_points(amount)} б.", reply_markup=admin_menu())


@router.message(AdminFlow.penalty_input, *TEXT_INPUT)
async def penalty_finish(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    uid, kind = data["uid"], data.get("kind", "Наказание")
    emp = await get_user(uid)
    if not emp:
        await state.clear()
        return await message.answer("Сотрудник не найден.", reply_markup=admin_menu())
    reason = message.text.strip()
    await add_penalty(uid, kind, reason)
    await state.clear()
    try:
        await bot.send_message(uid, f"⚠️ Вам выдано: {kind}\nПричина: {reason}\n"
                                    "Снять его может только администратор.")
    except Exception:
        pass
    await message.answer(f"✅ {kind} выдано: {emp['full_name']}.", reply_markup=admin_menu())


@router.message(AdminFlow.note_input, *TEXT_INPUT)
async def note_finish(message: Message, state: FSMContext):
    uid = (await state.get_data())["uid"]
    emp = await get_user(uid)
    if not emp:
        await state.clear()
        return await message.answer("Сотрудник не найден.", reply_markup=admin_menu())
    await add_note(uid, message.text.strip())
    await state.clear()
    await message.answer(f"✅ Примечание добавлено: {emp['full_name']}.", reply_markup=admin_menu())


@router.message(AdminFlow.msg_input, *TEXT_INPUT)
async def msg_finish(message: Message, state: FSMContext, bot: Bot):
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await bot.send_message(uid, f"✉️ Сообщение от администратора:\n\n{message.text}")
        await message.answer("✅ Отправлено.", reply_markup=admin_menu())
    except Exception:
        await message.answer("❌ Не удалось доставить — сотрудник, возможно, ещё не запускал бота.",
                             reply_markup=admin_menu())


# ---------- рассылка ----------

@router.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminFlow.broadcast)
    await message.answer("Напишите сообщение для всех сотрудников. Оно также сохранится в разделе "
                         "«📢 Объявления». /cancel — отмена")


@router.message(AdminFlow.broadcast, *TEXT_INPUT)
async def broadcast_finish(message: Message, state: FSMContext, bot: Bot):
    text = message.text.strip()
    await add_announcement(text)
    sent = failed = 0
    for u in await all_users():
        if u["user_id"] == message.from_user.id:
            continue
        try:
            await bot.send_message(u["user_id"], f"📢 Объявление\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    await state.clear()
    await message.answer(f"📢 Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.",
                         reply_markup=admin_menu())
