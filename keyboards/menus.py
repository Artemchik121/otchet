from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def user_menu(is_admin: bool = False):
    rows = [
        [KeyboardButton(text="📝 Сдать отчёт"), KeyboardButton(text="📊 Моя статистика")],
        [KeyboardButton(text="⚠️ Мои наказания"), KeyboardButton(text="📚 История отчётов")],
        [KeyboardButton(text="📢 Объявления")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📥 На проверке"), KeyboardButton(text="📋 Последние отчёты")],
        [KeyboardButton(text="👥 Сотрудники"), KeyboardButton(text="➕ Добавить сотрудника")],
        [KeyboardButton(text="⭐ Баллы"), KeyboardButton(text="⚠️ Наказание")],
        [KeyboardButton(text="✅ Снять наказание"), KeyboardButton(text="🗒 Примечание")],
        [KeyboardButton(text="🗑 Удалить сотрудника"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="🏆 Рейтинг"), KeyboardButton(text="🏠 Пользовательское меню")],
    ], resize_keyboard=True)


def menu_for(user):
    return admin_menu() if user and user["is_admin"] else user_menu()
