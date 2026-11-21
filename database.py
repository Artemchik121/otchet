import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from config import TIMEZONE

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


# ---------- служебное ----------

def now():
    return datetime.now(ZoneInfo(TIMEZONE))


def today_str():
    return now().date().isoformat()


def week_start():
    today = now().date()
    return (today - timedelta(days=today.weekday())).isoformat()


async def _add_column(db, table, column, ddl):
    """Добавляет колонку, если её нет. Возвращает True, если колонка была добавлена."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    if column in [row[1] for row in await cur.fetchall()]:
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript('''
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            role TEXT NOT NULL, is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            report_date TEXT NOT NULL, work_text TEXT NOT NULL,
            photos TEXT DEFAULT '[]', created_at TEXT NOT NULL,
            UNIQUE(user_id, report_date)
        );
        CREATE TABLE IF NOT EXISTS points(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, reason TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS penalties(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            reason TEXT NOT NULL, until_date TEXT, active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            note TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS invites(
            username TEXT PRIMARY KEY, role TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests(
            user_id INTEGER PRIMARY KEY, nickname TEXT NOT NULL, username TEXT, full_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS announcements(
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_at TEXT NOT NULL
        );
        ''')

        # --- миграции старой базы ---
        await _add_column(db, "users", "nickname", "TEXT")
        await _add_column(db, "penalties", "kind", "TEXT")
        await _add_column(db, "points", "report_id", "INTEGER")
        await _add_column(db, "reports", "points", "INTEGER DEFAULT 0")
        await _add_column(db, "reports", "review_comment", "TEXT")
        await _add_column(db, "reports", "reviewed_by", "INTEGER")
        await _add_column(db, "reports", "reviewed_at", "TEXT")
        status_added = await _add_column(db, "reports", "status", "TEXT DEFAULT 'pending'")
        if status_added:
            # Все старые отчёты уже получили автоматические баллы — считаем их проверенными.
            await db.execute(
                "UPDATE points SET report_id=(SELECT r.id FROM reports r WHERE r.user_id=points.user_id "
                "AND r.report_date=substr(points.created_at,1,10)) WHERE reason LIKE 'Автоматически%'"
            )
            await db.execute(
                "UPDATE reports SET status='reviewed', "
                "points=COALESCE((SELECT SUM(amount) FROM points WHERE report_id=reports.id),0), "
                "review_comment='Автоначисление (до обновления бота)'"
            )
        await db.execute(
            "UPDATE users SET role='Куратор организаций' WHERE role='Заместитель куратора организаций'"
        )
        await db.commit()


# ---------- пользователи ----------

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=? AND active=1", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_by_username(username):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE lower(username)=lower(?) AND active=1", (username,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_user(user_id, username, full_name, role, is_admin=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO users(user_id,username,full_name,role,is_admin,active)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, full_name=excluded.full_name,
                role=excluded.role, is_admin=excluded.is_admin, active=1
        ''', (user_id, username, full_name, role, is_admin))
        await db.commit()


async def ensure_admin(user_id, default_role="Куратор организаций"):
    """Гарантирует, что пользователь из ADMIN_IDS существует и является админом."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO users(user_id,username,full_name,role,is_admin,active)
            VALUES(?,NULL,?,?,1,1)
            ON CONFLICT(user_id) DO UPDATE SET is_admin=1, active=1
        ''', (user_id, f"Администратор {user_id}", default_role))
        await db.commit()


async def update_user_profile(user_id, username, full_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            # Имя из Telegram подставляется, только пока сотрудник не ввёл свой ник.
            "UPDATE users SET username=?, full_name=CASE WHEN nickname IS NULL THEN ? ELSE full_name END "
            "WHERE user_id=? AND active=1",
            (username, full_name, user_id),
        )
        await db.commit()


async def set_nickname(user_id, nickname):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET nickname=?, full_name=? WHERE user_id=?",
                         (nickname, nickname, user_id))
        await db.commit()


async def nickname_taken(nickname, exclude_user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM users WHERE active=1 AND user_id<>? AND (lower(nickname)=lower(?) "
            "OR lower(full_name)=lower(?))", (exclude_user_id, nickname, nickname))
        return await cur.fetchone() is not None


async def purge_user(user_id):
    """Полное удаление сотрудника вместе с отчётами, баллами, наказаниями и примечаниями."""
    async with aiosqlite.connect(DB_PATH) as db:
        for table in ("reports", "points", "penalties", "notes", "requests", "users"):
            await db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        await db.commit()


# ---------- заявки на доступ ----------

async def upsert_request(user_id, nickname, username, full_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO requests(user_id,nickname,username,full_name,status,created_at) "
            "VALUES(?,?,?,?,'pending',?)",
            (user_id, nickname, username, full_name, now().isoformat()))
        await db.commit()


async def get_request(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM requests WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_request_status(user_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET status=? WHERE user_id=?", (status, user_id))
        await db.commit()


async def set_admin(user_id, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_admin=? WHERE user_id=?", (1 if value else 0, user_id))
        await db.commit()


async def set_role(user_id, role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
        await db.commit()


async def all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE active=1 ORDER BY full_name COLLATE NOCASE")
        return [dict(r) for r in await cur.fetchall()]


async def admin_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE active=1 AND is_admin=1")
        return [r[0] for r in await cur.fetchall()]


async def deactivate_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))
        await db.commit()


# ---------- приглашения по нику ----------

def _clean_nick(username):
    return (username or "").strip().lstrip("@").lower()


async def add_invite(username, role, is_admin=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO invites(username,role,is_admin,created_at) VALUES(?,?,?,?)",
            (_clean_nick(username), role, is_admin, now().isoformat()),
        )
        await db.commit()


async def list_invites():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM invites ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]


async def delete_invite(username):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM invites WHERE username=?", (_clean_nick(username),))
        await db.commit()


async def pop_invite(username):
    """Возвращает и удаляет приглашение для этого ника (или None)."""
    nick = _clean_nick(username)
    if not nick:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM invites WHERE username=?", (nick,))
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM invites WHERE username=?", (nick,))
        await db.commit()
        return dict(row)


# ---------- отчёты ----------

async def has_report_today(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM reports WHERE user_id=? AND report_date=?", (user_id, today_str())
        )
        return await cur.fetchone() is not None


async def save_report(user_id, work_text, photos):
    """Сохраняет отчёт со статусом «на проверке». Баллы выставляет администратор. Возвращает id."""
    stamp = now()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reports(user_id,report_date,work_text,photos,created_at,status) "
            "VALUES(?,?,?,?,?,'pending')",
            (user_id, stamp.date().isoformat(), work_text,
             json.dumps(photos, ensure_ascii=False), stamp.isoformat()),
        )
        await db.commit()
        return cur.lastrowid


_REPORT_SELECT = (
    "SELECT r.*, u.full_name, u.username, u.role FROM reports r "
    "JOIN users u ON u.user_id=r.user_id "
)


async def get_report(report_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_REPORT_SELECT + "WHERE r.id=?", (report_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def pending_reports(limit=20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            _REPORT_SELECT + "WHERE r.status='pending' ORDER BY r.report_date, r.id LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def pending_count():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM reports WHERE status='pending'")
        return (await cur.fetchone())[0]


async def recent_reports(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(_REPORT_SELECT + "ORDER BY r.report_date DESC, r.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def user_reports(user_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            _REPORT_SELECT + "WHERE r.user_id=? ORDER BY r.report_date DESC, r.id DESC LIMIT ?",
            (user_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def review_report(report_id, points, comment, admin_id):
    """Выставляет (или меняет) баллы за отчёт. Повторная проверка заменяет прежние баллы."""
    report = await get_report(report_id)
    if not report:
        return None
    stamp = now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM points WHERE report_id=?", (report_id,))
        if points != 0:
            reason = f"За отчёт от {report['report_date']}"
            if comment:
                reason += f": {comment}"
            await db.execute(
                "INSERT INTO points(user_id,amount,reason,created_at,report_id) VALUES(?,?,?,?,?)",
                (report["user_id"], points, reason, stamp, report_id),
            )
        await db.execute(
            "UPDATE reports SET status='reviewed', points=?, review_comment=?, reviewed_by=?, reviewed_at=? "
            "WHERE id=?",
            (points, comment or None, admin_id, stamp, report_id),
        )
        await db.commit()
    return await get_report(report_id)


# ---------- баллы / наказания / примечания ----------

async def add_points(user_id, amount, reason):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO points(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
            (user_id, amount, reason, now().isoformat()),
        )
        await db.commit()


async def add_penalty(user_id, kind, reason):
    """Наказание действует, пока его не снимет администратор (срока нет)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO penalties(user_id,kind,reason,created_at) VALUES(?,?,?,?)",
            (user_id, kind, reason, now().isoformat()),
        )
        await db.commit()


async def active_penalties(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM penalties WHERE user_id=? AND active=1 ORDER BY id DESC", (user_id,))
        return [dict(r) for r in await cur.fetchall()]


async def clear_penalty(penalty_id):
    """Снимает одно наказание. Возвращает его данные или None, если оно уже снято."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM penalties WHERE id=? AND active=1", (penalty_id,))
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute("UPDATE penalties SET active=0 WHERE id=?", (penalty_id,))
        await db.commit()
        return dict(row)


async def clear_penalties(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE penalties SET active=0 WHERE user_id=?", (user_id,))
        await db.commit()


async def add_note(user_id, note):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO notes(user_id,note,created_at) VALUES(?,?,?)",
            (user_id, note, now().isoformat()),
        )
        await db.commit()


# ---------- сводки ----------

async def employee_dashboard(user_id):
    monday, today = week_start(), today_str()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        user = await cur.fetchone()
        if not user:
            return None

        async def one(sql, params):
            c = await db.execute(sql, params)
            return (await c.fetchone())[0]

        async def many(sql, params):
            c = await db.execute(sql, params)
            return [dict(r) for r in await c.fetchall()]

        return {
            "user": dict(user),
            "reports_total": await one("SELECT COUNT(*) FROM reports WHERE user_id=?", (user_id,)),
            "last_report": await one("SELECT MAX(report_date) FROM reports WHERE user_id=?", (user_id,)),
            "reports_week": await one(
                "SELECT COUNT(*) FROM reports WHERE user_id=? AND report_date>=?", (user_id, monday)),
            "pending": await one(
                "SELECT COUNT(*) FROM reports WHERE user_id=? AND status='pending'", (user_id,)),
            "points_total": await one(
                "SELECT COALESCE(SUM(amount),0) FROM points WHERE user_id=?", (user_id,)),
            "points_week": await one(
                "SELECT COALESCE(SUM(amount),0) FROM points WHERE user_id=? AND substr(created_at,1,10)>=?",
                (user_id, monday)),
            "reports": await many(
                "SELECT * FROM reports WHERE user_id=? ORDER BY report_date DESC, id DESC LIMIT 5", (user_id,)),
            "points_history": await many(
                "SELECT * FROM points WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)),
            "penalties": await many(
                "SELECT * FROM penalties WHERE user_id=? AND active=1 ORDER BY id DESC", (user_id,)),
            "notes": await many(
                "SELECT * FROM notes WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)),
        }


async def team_overview():
    monday, today = week_start(), today_str()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('''
            SELECT u.*,
              (SELECT COUNT(*) FROM reports r WHERE r.user_id=u.user_id AND r.report_date>=?) reports_week,
              (SELECT COALESCE(SUM(amount),0) FROM points p
                 WHERE p.user_id=u.user_id AND substr(p.created_at,1,10)>=?) points_week,
              (SELECT COALESCE(SUM(amount),0) FROM points p WHERE p.user_id=u.user_id) points_total,
              (SELECT COUNT(*) FROM reports r WHERE r.user_id=u.user_id AND r.status='pending') pending,
              (SELECT COUNT(*) FROM penalties n WHERE n.user_id=u.user_id AND n.active=1) penalties_active,
              (SELECT COUNT(*) FROM reports r WHERE r.user_id=u.user_id AND r.report_date=?) today
            FROM users u WHERE u.active=1 ORDER BY u.full_name COLLATE NOCASE
        ''', (monday, monday, today))
        return [dict(r) for r in await cur.fetchall()]


async def users_without_report_today():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users u WHERE u.active=1 AND u.is_admin=0 AND NOT EXISTS "
            "(SELECT 1 FROM reports r WHERE r.user_id=u.user_id AND r.report_date=?) "
            "ORDER BY u.full_name COLLATE NOCASE", (today_str(),))
        return [dict(r) for r in await cur.fetchall()]


# ---------- объявления и настройки ----------

async def add_announcement(text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO announcements(text,created_at) VALUES(?,?)", (text, now().isoformat()))
        await db.commit()


async def last_announcements(limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        await db.commit()
