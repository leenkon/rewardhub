from __future__ import annotations

import base64
import binascii
import hashlib
import io
import math
import os
import re
import secrets
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
from xml.sax.saxutils import escape

from flask import Flask, Response, abort, jsonify, render_template, request, send_file, session


BASE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
if getattr(sys, "frozen", False):
    DEFAULT_DATA_DIR = Path(sys.executable).resolve().parent / "data"
else:
    DEFAULT_DATA_DIR = BASE_DIR / "data"
DATA_DIR = Path(os.getenv("DATA_DIR") or DEFAULT_DATA_DIR)
DATABASE_PATH = Path(os.getenv("DATABASE_PATH") or DATA_DIR / "points.db")


def generated_secret_key() -> str:
    """密钥一旦写进源码，任何人都能伪造管理员 cookie，所以缺失时只在首次启动生成一份，和数据放在一起。"""
    key_file = DATA_DIR / "secret_key"
    try:
        stored = key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""
        if stored:
            return stored
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fresh = secrets.token_hex(32)
        key_file.write_text(fresh, encoding="utf-8")
        key_file.chmod(0o600)
        return fresh
    except OSError:
        # 数据目录只读不该挡住启动：这种安装只是每次启动重新发一次登录态。
        return secrets.token_hex(32)


# Compose 转发的是 "${SECRET_KEY:-}"，所以未设置时传来的是空串，而不是根本没有这个变量。
SECRET_KEY = os.getenv("SECRET_KEY") or generated_secret_key()
PASSWORD_ITERATIONS = 240_000
APP_VERSION = "0.8.16"
CHILD_AVATARS = {"boy", "girl"}
ADULT_AVATARS = {"adult-male", "adult-female"}


def avatar_choices(role: str) -> set[str]:
    """头像分集只有孩子/成人两套，创建和改账号都走这里。"""
    return CHILD_AVATARS if role == "child" else ADULT_AVATARS
PROJECT_ICONS = {
    "television.svg", "book.svg", "homework.svg", "chore.svg", "sport.svg",
    "bedtime.svg", "snack.svg", "game.svg", "outing.svg", "points.svg",
    "gift.svg", "warning.svg", "computer.svg", "desktop.svg", "money.svg",
    "phone.svg", "cooking.svg", "cleaning.svg", "school.svg", "toothbrush.svg",
    "bath.svg", "pencil.svg", "clothes.svg", "laundry.svg", "dishes.svg",
    "pet.svg", "walk.svg", "shopping.svg", "backpack.svg", "handwash.svg",
    "water.svg", "plant.svg", "tidy.svg", "trash.svg", "lunch.svg", "delivery.svg",
}
BUILTIN_ICON_KEYS = set(
    """
    home broom vacuum iron bed sleep alarm shower repair hammer screwdriver
    meal breakfast dinner bread rice noodles apple banana orange strawberry cake cookie milk coffee tea juice icecream
    reading graduation math science language art music idea microscope ruler notebook library exam medal target lightbulb
    run bike swim football basketball baseball tennis badminton yoga weight hiking mountain trophy whistle skate climbing fitness stretch
    car bus train airplane rocket ship taxi bicycle map location suitcase passport ticket traffic fuel travel compass road parking
    family child baby adult dog cat flower birthday heart homekey door sofa camera calendar couple
    server harddrive folder cloud download upload wifi network database terminal code keyboard printer tablet
    office briefcase clock chart mail meeting call checklist pin note moneybag contract build manager megaphone search settings shield lock
    diamond crown badge star fire bolt gem treasure giftbox fireworks crown2 medal2
    doctor medicine hospital mask bandage thermometer apple2 water2 heart2 brain lungs health firstaid rest
    sun moon cloud2 rain snow rainbow wind leaf tree flower2 season umbrella temperature earth
    party confetti balloon cake2 music2 flag community handshake speech message announcement megaphone2 group friend smile package
    """.split()
)
ITEM_DEFAULT_ICONS = {"earn": "points.svg", "deduct": "warning.svg", "reward": "gift.svg"}
DEFAULT_SETTINGS = {"points_per_yuan": 100}
TASK_TYPES = {"daily", "epic"}
TASK_DIFFICULTIES = {"easy", "normal", "hard", "legendary"}
# 可编辑列只有一份：新建与编辑共用它的顺序，INSERT 与 UPDATE 不再各写一遍字段名。
TASK_COLUMNS = ("title", "description", "category", "task_type", "difficulty", "reward_coins", "reward_exp", "icon", "due_date")
DEFAULT_ACHIEVEMENTS = [
    ("first-quest", "初次出征", "完成第一个现实任务", "points.svg", "completed_tasks", 1),
    ("habit-builder", "习惯养成", "完成 3 个任务，建立自己的节奏", "bedtime.svg", "completed_tasks", 3),
    ("quest-ten", "十次出征", "完成 10 个任务，成为可靠的冒险者", "backpack.svg", "completed_tasks", 10),
    ("quest-twenty-five", "任务老手", "完成 25 个任务，持续兑现自己的目标", "points.svg", "completed_tasks", 25),
    ("epic-clear", "史诗征服者", "完成第一个史诗悬赏", "gift.svg", "epic_tasks", 1),
    ("epic-trio", "史诗远征队", "完成 3 个史诗悬赏", "gift.svg", "epic_tasks", 3),
    ("epic-five", "传奇开拓者", "完成 5 个史诗悬赏", "gift.svg", "epic_tasks", 5),
    ("coin-hoard", "积分收藏家", "累计赚取 100 积分", "money.svg", "earned_coins", 100),
    ("coin-five-hundred", "积分宝库", "累计赚取 500 积分", "money.svg", "earned_coins", 500),
    ("coin-thousand", "财富领航员", "累计赚取 1000 积分", "money.svg", "earned_coins", 1000),
    ("daily-five", "日常坚持者", "完成 5 个日常任务", "bedtime.svg", "daily_tasks", 5),
]


def valid_project_icon(icon: str | None) -> bool:
    if not isinstance(icon, str):
        return False
    if icon in PROJECT_ICONS:
        return True
    return icon.startswith("emoji:") and icon[6:] in BUILTIN_ICON_KEYS

ITEM_TABLES = {"earn": "earn_items", "deduct": "deduct_items", "reward": "rewards"}
SCORING_TABLES = ("earn_items", "deduct_items")
CAP_PERIOD_LABELS = {
    "day": "每天",
    "week": "每周",
    "month": "每月",
    "quarter": "每季度",
    "half_year": "每半年",
    "year": "每年",
}
CAP_PERIOD_MONTHS = {"month": 1, "quarter": 3, "half_year": 6, "year": 12}
STREAK_PERIOD_TEXT = {"day": "天", "week": "周", "month": "个月", "quarter": "个季度", "year": "年"}
DEDUCT_STREAK_SOURCES = {
    "day": "deduct_day_streak",
    "week": "deduct_week_streak",
    "month": "deduct_month_streak",
    "quarter": "deduct_quarter_streak",
    "year": "deduct_year_streak",
}
# 加分连续链按项目自己的目标窗口判定，所以一个 source 就覆盖所有窗口单位。
TARGET_STREAK_SOURCE = "target_streak"
# 防重复发放是按 source 认的，所以「连续未达标扣」不能共用 target_streak。
TARGET_STREAK_PENALTY_SOURCE = "target_streak_penalty"
ANALYTICS_MAX_DAYS = 365
RECENT_WINDOW_DAYS = 7
ITEM_PERIOD_UNIT = "week"
ITEM_PERIOD_COUNT = 8
ITEM_PERIOD_MAX = 31
TARGET_SHEET_UNITS = ("week", "month", "quarter")
TARGET_SHEET_MAX_CHILDREN = 12
TARGET_SHEET_MAX_ROWS = 60
TARGET_SHEET_MAX_COLUMNS = 64
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# 所有结算路径共用同一份回看长度：手动结算列出的期次，正好是引擎地板还够得到的那些。
SETTLE_LOOKBACK_PERIODS = {"day": 84, "week": 12, "month": 12, "quarter": 4, "year": 2}
RULE_UNITS = ("day", "week", "month", "quarter", "year")
RULE_UNIT_SHORT_TEXT = {"day": "天", "week": "周", "month": "月", "quarter": "季", "year": "年"}
AUTO_SETTLE_UNITS = ("day",)
MANUAL_SETTLE_UNITS = tuple(unit for unit in RULE_UNITS if unit not in AUTO_SETTLE_UNITS)
SETTLE_UNIT_ALL = "all"
TARGET_MEASURES = ("count", "quantity")
# 比一个单位更长的窗口都从这个固定的周一起算，于是所有账号的窗口边界一致。
WINDOW_EPOCH = date(2000, 1, 3)
MONTH_EPOCH_INDEX = WINDOW_EPOCH.year * 12
# 数量录入在窗口结算前不带分值，所以统计时按 quantity 计入。
# 带项目的补录是家长代填的过去日期记录，和孩子自己的提交一样计入。
EARN_COMPLETION_FILTER = (
    "item_kind = 'earn' AND item_id IS NOT NULL AND (amount > 0 OR quantity > 0) AND COALESCE(source, 'earn') IN ('earn', 'manual')"
)
DEDUCT_COMPLETION_FILTER = (
    "item_kind = 'deduct' AND item_id IS NOT NULL AND amount < 0 AND COALESCE(source, 'deduct') IN ('deduct', 'manual')"
)
# 目标窗口只统计真实完成的工作，所以上面那些发放/罚分 source 都不进这两个过滤条件。
COMPLETION_FILTERS = {"earn": EARN_COMPLETION_FILTER, "deduct": DEDUCT_COMPLETION_FILTER}
# 结算链重新锚定时要读的规则列，以及单个孩子的快照能够整体替换的全部列。
RULE_ANCHOR_FIELDS = (
    "week_target",
    "week_bonus",
    "week_penalty",
    "target_measure",
    "target_unit",
    "target_every",
    "bonus_every",
    "streak_weeks",
    "streak_bonus",
    "streak_unit",
    "streak_penalty_weeks",
    "streak_penalty",
)
RULE_OVERRIDE_FIELDS = (*RULE_ANCHOR_FIELDS, "cap_period", "cap_count", "count_days_once", "rule_set_at")
TARGET_BONUS_SOURCES = {"earn": "weekly_bonus", "deduct": "over_cap_penalty"}
# 每一族结算会写入的 source：重算只清自己那几行，不误伤别的——加分连续链就挂在目标窗口上。
TARGET_WINDOW_SOURCES = {
    "earn": (TARGET_BONUS_SOURCES["earn"], "weekly_penalty", TARGET_STREAK_SOURCE, TARGET_STREAK_PENALTY_SOURCE),
    "deduct": (TARGET_BONUS_SOURCES["deduct"],),
}
# 结算自己写出的 source：撤销这几类记录不重算窗口，只要水位行还在，发放就维持被撤销的状态。
AUTO_SETTLEMENT_SOURCES = (
    *TARGET_WINDOW_SOURCES["earn"],
    *TARGET_WINDOW_SOURCES["deduct"],
    *DEDUCT_STREAK_SOURCES.values(),
)
# 连续发放不占水位：记录本身就是唯一的去重依据，删掉一条，下一趟遍历就会原样补发。
# 按天的连续链在撤销请求里当场就回来了——它会先跑一次惰性结算再应答。
STREAK_SOURCES = (
    TARGET_STREAK_SOURCE,
    TARGET_STREAK_PENALTY_SOURCE,
    *DEDUCT_STREAK_SOURCES.values(),
)
# 发放索引和 INSERT 的冲突目标必须拼出完全相同的谓词，否则冲突目标就不匹配了。
SETTLEMENT_SOURCE_PREDICATE = "source IN (" + ", ".join(f"'{source}'" for source in AUTO_SETTLEMENT_SOURCES) + ")"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
CUSTOM_ASSET_KEYS = {
    "child-boy", "child-girl", "adult-male", "adult-female",
}
CUSTOM_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}

app = Flask(
    __name__,
    template_folder=str(RESOURCE_DIR / "templates"),
    static_folder=str(RESOURCE_DIR / "static"),
)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 这个应用不上传文件，这么大的请求体早就超出任何正常表单的范围了。
    MAX_CONTENT_LENGTH=1024 * 1024,
)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations)
        ).hex()
        return secrets.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def environment_value(name: str) -> str:
    """先读可选的 base64 值，再回落到普通环境变量。

    FlyNAS 把安装向导的密码存在 env 文件里。base64 能让 `$`、`#`、引号、空格
    这些字符不被 Compose 重新解释。
    """
    encoded = os.getenv(f"{name}_B64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return ""
    return os.getenv(name, "")


def admin_credentials_from_env() -> tuple[str, str] | None:
    username = environment_value("ADMIN_USERNAME").strip()
    password = environment_value("ADMIN_PASSWORD")
    if not username and not password:
        return None
    if not USERNAME_PATTERN.fullmatch(username) or len(password) < 6:
        return None
    return username, password


def admin_credentials_fingerprint(credentials: tuple[str, str]) -> str:
    """给出最后一次生效的安装凭据的不可逆标记。"""
    username, password = credentials
    payload = f"{username}\0{password}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def admin_credentials_marker_path() -> Path:
    return DATA_DIR / ".admin-credentials.applied"


def install_credentials_are_applied(credentials: tuple[str, str]) -> bool:
    marker = admin_credentials_marker_path()
    try:
        return marker.read_text(encoding="ascii").strip() == admin_credentials_fingerprint(credentials)
    except (OSError, UnicodeError):
        return False


def mark_install_credentials_applied(credentials: tuple[str, str]) -> None:
    marker = admin_credentials_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    temp_marker = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    temp_marker.write_text(admin_credentials_fingerprint(credentials), encoding="ascii")
    temp_marker.replace(marker)


def first_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE role = 'admin' AND active = 1 ORDER BY id LIMIT 1"
    ).fetchone()


def create_admin_account(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row:
    now = now_iso()
    conn.execute(
        "INSERT INTO accounts(username, password_hash, display_name, role, avatar, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, password_hash(password), "管理员", "admin", "adult-male", now),
    )
    return conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()


def apply_install_admin_credentials(conn: sqlite3.Connection, credentials: tuple[str, str]) -> bool:
    """只应用一次安装向导的凭据，不动应用数据。"""
    username, password = credentials
    requested = conn.execute(
        "SELECT * FROM accounts WHERE username = ? AND active = 1", (username,)
    ).fetchone()
    if requested is not None and requested["role"] != "admin":
        return False

    if requested is not None:
        conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE id = ?",
            (password_hash(password), requested["id"]),
        )
        return True

    admin = first_admin(conn)
    if admin is None:
        create_admin_account(conn, username, password)
        return True

    try:
        conn.execute(
            "UPDATE accounts SET username = ?, password_hash = ? WHERE id = ?",
            (username, password_hash(password), admin["id"]),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def migrate_point_requests(conn: sqlite3.Connection) -> None:
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'point_requests'"
    ).fetchone()
    if schema is None or "cash_exchange" in (schema["sql"] or ""):
        return
    conn.execute("ALTER TABLE point_requests RENAME TO point_requests_legacy")
    conn.execute(
        """
        CREATE TABLE point_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('earn', 'deduct', 'exchange', 'cash_exchange', 'manual')),
            title TEXT NOT NULL,
            amount INTEGER NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_id INTEGER,
            reject_reason TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO point_requests(
            id, account_id, requester_id, kind, title, amount, type, date, time,
            status, created_at, reviewed_at, reviewer_id, reject_reason
        )
        SELECT id, account_id, requester_id, kind, title, amount, type, date, time,
               status, created_at, reviewed_at, reviewer_id, reject_reason
        FROM point_requests_legacy
        """
    )
    conn.execute("DROP TABLE point_requests_legacy")


def migrate_items_to_library(conn: sqlite3.Connection) -> None:
    """把 0.8.0 之前每个孩子在项目库里各自的副本，合并成一份系统库。"""
    if not table_has_column(conn, "earn_items", "account_id"):
        return
    children = {int(row["id"]) for row in conn.execute("SELECT id FROM accounts WHERE role = 'child'").fetchall()}
    now = now_iso()
    for kind, table in ITEM_TABLES.items():
        owned: dict[int, set[int]] = {child_id: set() for child_id in children}
        default_icon = ITEM_DEFAULT_ICONS[kind]
        legacy_rows = conn.execute(
            f"SELECT id, account_id, name, points, icon FROM {table} ORDER BY id"
        ).fetchall()
        conn.execute(
            f"""
            CREATE TABLE {table}_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK (points > 0),
                icon TEXT NOT NULL DEFAULT '{default_icon}'
            )
            """
        )
        merged: dict[tuple[str, int, str], int] = {}
        library_ids: list[int] = []
        for row in legacy_rows:
            key = (row["name"], int(row["points"]), row["icon"] or default_icon)
            item_id = merged.get(key)
            if item_id is None:
                cursor = conn.execute(
                    f"INSERT INTO {table}_library(name, points, icon) VALUES (?, ?, ?)", key
                )
                item_id = int(cursor.lastrowid)
                merged[key] = item_id
                library_ids.append(item_id)
            if row["account_id"] is None:
                for child_id in children:
                    owned[child_id].add(item_id)
            elif int(row["account_id"]) in owned:
                owned[int(row["account_id"])].add(item_id)
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_library RENAME TO {table}")
        for child_id, child_items in owned.items():
            for item_id in set(library_ids) - child_items:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO item_bindings(item_kind, item_id, account_id, enabled, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    """,
                    (kind, item_id, child_id, now),
                )
    for table in ("records", "point_requests"):
        rows = conn.execute(
            f"SELECT id, title, amount, item_kind, item_id, source FROM {table} WHERE item_kind IS NULL OR source IS NULL"
        ).fetchall()
        for row in rows:
            tag = match_record_item(conn, row["title"], int(row["amount"]))
            item_kind = row["item_kind"] or (tag[0] if tag else None)
            item_id = row["item_id"] or (tag[1] if tag else None)
            source = row["source"] or infer_record_source(row["title"], item_kind)
            conn.execute(
                f"UPDATE {table} SET item_kind = ?, item_id = ?, source = ? WHERE id = ?",
                (item_kind, item_id, source, row["id"]),
            )


def remove_legacy_default_child(conn: sqlite3.Connection) -> None:
    """删掉 0.6.2 之前版本自动创建、且从未被改动过的那个孩子。"""
    child = conn.execute(
        "SELECT id FROM accounts WHERE username = 'child' AND role = 'child' AND display_name = '小朋友'"
    ).fetchone()
    if child is None:
        return
    child_id = int(child["id"])
    activity = conn.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM records WHERE account_id = ?
            UNION ALL SELECT 1 FROM point_requests WHERE account_id = ?
            UNION ALL SELECT 1 FROM account_logs WHERE target_id = ? OR actor_id = ?
        )
        """,
        (child_id, child_id, child_id, child_id),
    ).fetchone()[0]
    if activity:
        return
    for table in ("earn_items", "deduct_items", "rewards", "records", "point_requests"):
        if table in ITEM_TABLES.values() and not table_has_column(conn, table, "account_id"):
            continue
        conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (child_id,))
    conn.execute("DELETE FROM item_bindings WHERE account_id = ?", (child_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (child_id,))


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'child')),
                avatar TEXT NOT NULL DEFAULT 'boy',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS earn_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK (points > 0),
                icon TEXT NOT NULL DEFAULT 'points.svg',
                sort_order INTEGER NOT NULL DEFAULT 0,
                week_target INTEGER NOT NULL DEFAULT 0,
                week_bonus INTEGER NOT NULL DEFAULT 0,
                week_penalty INTEGER NOT NULL DEFAULT 0,
                target_measure TEXT NOT NULL DEFAULT 'count',
                target_unit TEXT NOT NULL DEFAULT 'week',
                target_every INTEGER NOT NULL DEFAULT 1,
                bonus_every INTEGER NOT NULL DEFAULT 1,
                streak_weeks INTEGER NOT NULL DEFAULT 0,
                streak_bonus INTEGER NOT NULL DEFAULT 0,
                streak_unit TEXT NOT NULL DEFAULT 'week',
                streak_penalty_weeks INTEGER NOT NULL DEFAULT 0,
                streak_penalty INTEGER NOT NULL DEFAULT 0,
                cap_period TEXT NOT NULL DEFAULT '',
                cap_count INTEGER NOT NULL DEFAULT 1,
                count_days_once INTEGER NOT NULL DEFAULT 0,
                rule_set_at TEXT
            );
            CREATE TABLE IF NOT EXISTS deduct_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK (points > 0),
                icon TEXT NOT NULL DEFAULT 'warning.svg',
                sort_order INTEGER NOT NULL DEFAULT 0,
                week_target INTEGER NOT NULL DEFAULT 0,
                week_bonus INTEGER NOT NULL DEFAULT 0,
                target_unit TEXT NOT NULL DEFAULT 'week',
                target_every INTEGER NOT NULL DEFAULT 1,
                bonus_every INTEGER NOT NULL DEFAULT 1,
                count_days_once INTEGER NOT NULL DEFAULT 0,
                streak_weeks INTEGER NOT NULL DEFAULT 0,
                streak_bonus INTEGER NOT NULL DEFAULT 0,
                streak_unit TEXT NOT NULL DEFAULT 'week',
                rule_set_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                points INTEGER NOT NULL CHECK (points > 0),
                icon TEXT NOT NULL DEFAULT 'gift.svg',
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS weekly_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                item_kind TEXT NOT NULL DEFAULT 'earn',
                item_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                completions INTEGER NOT NULL,
                target INTEGER NOT NULL,
                bonus INTEGER NOT NULL DEFAULT 0,
                penalty INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, item_kind, item_id, week_start)
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS point_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('earn', 'deduct', 'exchange', 'cash_exchange', 'manual')),
                title TEXT NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewer_id INTEGER,
                reject_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS account_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                actor_username TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                actor_avatar TEXT NOT NULL DEFAULT 'adult-male',
                target_id INTEGER,
                target_username TEXT NOT NULL,
                target_name TEXT NOT NULL,
                target_role TEXT NOT NULL,
                target_avatar TEXT NOT NULL DEFAULT 'boy',
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS item_bindings (
                item_kind TEXT NOT NULL CHECK (item_kind IN ('earn', 'deduct', 'reward')),
                item_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (item_kind, item_id, account_id)
            );
            CREATE TABLE IF NOT EXISTS item_rule_overrides (
                item_kind TEXT NOT NULL CHECK (item_kind IN ('earn', 'deduct')),
                item_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                week_target INTEGER NOT NULL DEFAULT 0,
                week_bonus INTEGER NOT NULL DEFAULT 0,
                week_penalty INTEGER NOT NULL DEFAULT 0,
                target_measure TEXT NOT NULL DEFAULT 'count',
                target_unit TEXT NOT NULL DEFAULT 'week',
                target_every INTEGER NOT NULL DEFAULT 1,
                bonus_every INTEGER NOT NULL DEFAULT 1,
                streak_weeks INTEGER NOT NULL DEFAULT 0,
                streak_bonus INTEGER NOT NULL DEFAULT 0,
                streak_unit TEXT NOT NULL DEFAULT 'week',
                streak_penalty_weeks INTEGER NOT NULL DEFAULT 0,
                streak_penalty INTEGER NOT NULL DEFAULT 0,
                cap_period TEXT NOT NULL DEFAULT '',
                cap_count INTEGER NOT NULL DEFAULT 1,
                count_days_once INTEGER NOT NULL DEFAULT 0,
                rule_set_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (item_kind, item_id, account_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '生活',
                task_type TEXT NOT NULL CHECK (task_type IN ('daily', 'epic')),
                difficulty TEXT NOT NULL DEFAULT 'normal' CHECK (difficulty IN ('easy', 'normal', 'hard', 'legendary')),
                reward_coins INTEGER NOT NULL CHECK (reward_coins > 0),
                reward_exp INTEGER NOT NULL CHECK (reward_exp > 0),
                icon TEXT NOT NULL DEFAULT 'points.svg',
                due_date TEXT,
                created_by INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed', 'submitted', 'completed', 'rejected')),
                claim_date TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                submitted_at TEXT,
                completed_at TEXT,
                reviewer_id INTEGER,
                review_note TEXT,
                UNIQUE(task_id, account_id, claim_date)
            );
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                achievement_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT 'gift.svg',
                requirement_type TEXT NOT NULL,
                requirement_value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                achievement_id INTEGER NOT NULL,
                unlocked_at TEXT NOT NULL,
                UNIQUE(account_id, achievement_id)
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                audience TEXT NOT NULL DEFAULT 'all' CHECK (audience IN ('all', 'children')),
                created_by INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        migrate_point_requests(conn)
        if not table_has_column(conn, "accounts", "avatar"):
            conn.execute("ALTER TABLE accounts ADD COLUMN avatar TEXT NOT NULL DEFAULT 'boy'")
        for column, default in (("actor_avatar", "adult-male"), ("target_avatar", "boy")):
            if not table_has_column(conn, "account_logs", column):
                conn.execute(f"ALTER TABLE account_logs ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        for table in ("records", "point_requests"):
            if not table_has_column(conn, table, "account_id"):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER")
            for column, clause in (
                ("item_kind", "TEXT"),
                ("source", "TEXT"),
                ("item_id", "INTEGER"),
                ("quantity", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if not table_has_column(conn, table, column):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {clause}")
        for table, default_icon in ITEM_DEFAULT_ICONS.items():
            if not table_has_column(conn, ITEM_TABLES[table], "icon"):
                conn.execute(
                    f"ALTER TABLE {ITEM_TABLES[table]} ADD COLUMN icon TEXT NOT NULL DEFAULT '{default_icon}'"
                )
            for row in conn.execute(f"SELECT id, icon FROM {ITEM_TABLES[table]}").fetchall():
                if not valid_project_icon(row["icon"]):
                    conn.execute(
                        f"UPDATE {ITEM_TABLES[table]} SET icon = ? WHERE id = ?",
                        (default_icon, row["id"]),
                    )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
            ("points_per_yuan", str(DEFAULT_SETTINGS["points_per_yuan"]), now_iso()),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO achievements(
                achievement_key, title, description, icon, requirement_type, requirement_value
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            DEFAULT_ACHIEVEMENTS,
        )
        for achievement_key, title, description, icon, requirement_type, requirement_value in DEFAULT_ACHIEVEMENTS:
            conn.execute(
                """
                UPDATE achievements
                SET title = ?, description = ?, icon = ?, requirement_type = ?, requirement_value = ?
                WHERE achievement_key = ?
                """,
                (title, description, icon, requirement_type, requirement_value, achievement_key),
            )

        credentials = admin_credentials_from_env()
        if credentials and not install_credentials_are_applied(credentials):
            if apply_install_admin_credentials(conn, credentials):
                mark_install_credentials_applied(credentials)
        conn.execute("UPDATE accounts SET avatar = 'adult-male' WHERE username = 'admin' AND avatar = 'boy'")
        remove_legacy_default_child(conn)
        migrate_items_to_library(conn)
        ensure_item_schema(conn)
        ensure_settlement_uniqueness(conn)


def ensure_item_schema(conn: sqlite3.Connection) -> None:
    """在旧库重建之后运行——那一步重建的项目表没有这些列。"""
    all_tables = tuple(ITEM_TABLES.values())
    for column, clause, tables in (
        ("sort_order", "INTEGER NOT NULL DEFAULT 0", all_tables),
        ("week_target", "INTEGER NOT NULL DEFAULT 0", SCORING_TABLES),
        ("week_bonus", "INTEGER NOT NULL DEFAULT 0", SCORING_TABLES),
        ("week_penalty", "INTEGER NOT NULL DEFAULT 0", ("earn_items",)),
        ("target_measure", "TEXT NOT NULL DEFAULT 'count'", ("earn_items",)),
        ("target_unit", "TEXT NOT NULL DEFAULT 'week'", SCORING_TABLES),
        ("target_every", "INTEGER NOT NULL DEFAULT 1", SCORING_TABLES),
        ("bonus_every", "INTEGER NOT NULL DEFAULT 1", SCORING_TABLES),
        ("cap_period", "TEXT NOT NULL DEFAULT ''", ("earn_items",)),
        ("cap_count", "INTEGER NOT NULL DEFAULT 1", ("earn_items",)),
        ("count_days_once", "INTEGER NOT NULL DEFAULT 0", SCORING_TABLES),
        ("streak_weeks", "INTEGER NOT NULL DEFAULT 0", SCORING_TABLES),
        ("streak_bonus", "INTEGER NOT NULL DEFAULT 0", SCORING_TABLES),
        ("streak_unit", "TEXT NOT NULL DEFAULT 'week'", SCORING_TABLES),
        ("streak_penalty_weeks", "INTEGER NOT NULL DEFAULT 0", ("earn_items",)),
        ("streak_penalty", "INTEGER NOT NULL DEFAULT 0", ("earn_items",)),
        ("rule_set_at", "TEXT", SCORING_TABLES),
    ):
        for table in tables:
            if not table_has_column(conn, table, column):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {clause}")
    # 停用开关已取消：老库 active = 0 的项目会重新回到列表，历史账目不变。
    for table in all_tables:
        for column in ("active", "active_from"):
            if table_has_column(conn, table, column):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    # 把 streak_bonus 写成负数，曾经是罚「连续未达标」的唯一办法。
    conn.execute(
        """
        UPDATE earn_items
        SET streak_penalty_weeks = streak_weeks, streak_penalty = -streak_bonus, streak_bonus = 0
        WHERE streak_bonus < 0
        """
    )
    ensure_settlement_kind(conn)
    if table_has_column(conn, "earn_items", "once_per_day"):
        conn.execute("ALTER TABLE earn_items DROP COLUMN once_per_day")
    if table_has_column(conn, "weekly_settlements", "streak"):
        conn.execute("ALTER TABLE weekly_settlements DROP COLUMN streak")


def ensure_settlement_kind(conn: sqlite3.Connection) -> None:
    """扣分窗口和加分窗口共用结算水位，所以项目 id 必须先带上一_kind 才能并存。"""
    if table_has_column(conn, "weekly_settlements", "item_kind"):
        return
    conn.execute(
        """
        CREATE TABLE weekly_settlements_by_kind (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            item_kind TEXT NOT NULL DEFAULT 'earn',
            item_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            completions INTEGER NOT NULL,
            target INTEGER NOT NULL,
            bonus INTEGER NOT NULL DEFAULT 0,
            penalty INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, item_kind, item_id, week_start)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO weekly_settlements_by_kind(
            account_id, item_kind, item_id, week_start, completions, target, bonus, penalty, created_at
        )
        SELECT account_id, 'earn', item_id, week_start, completions, target, bonus, penalty, created_at
        FROM weekly_settlements
        """
    )
    conn.execute("DROP TABLE weekly_settlements")
    conn.execute("ALTER TABLE weekly_settlements_by_kind RENAME TO weekly_settlements")


def ensure_settlement_uniqueness(conn: sqlite3.Connection) -> None:
    """连续发放没有自己的水位表，所以只能用记录本身的唯一键来挡住第二次写入。"""
    # 早于这个索引建立的实例，每档只保留第一条记录；NULL 的 key 不受约束，因为索引把它们排除在外。
    keyed = f"{SETTLEMENT_SOURCE_PREDICATE} AND account_id IS NOT NULL AND item_kind IS NOT NULL AND item_id IS NOT NULL"
    conn.execute(
        f"""
        DELETE FROM records
        WHERE {keyed} AND id NOT IN (
            SELECT MIN(id) FROM records WHERE {keyed}
            GROUP BY account_id, item_kind, item_id, date, source
        )
        """
    )
    index_sql = (
        "CREATE UNIQUE INDEX records_settlement_once "
        f"ON records(account_id, item_kind, item_id, date, source) WHERE {SETTLEMENT_SOURCE_PREDICATE}"
    )
    stored = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'records_settlement_once'"
    ).fetchone()
    if stored is None or " ".join(stored["sql"].split()) != " ".join(index_sql.split()):
        conn.execute("DROP INDEX IF EXISTS records_settlement_once")
        conn.execute(index_sql)


def current_date() -> str:
    return datetime.now().astimezone().date().isoformat()


def current_time() -> str:
    return datetime.now().astimezone().strftime("%H:%M")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def valid_date(value: Any) -> str:
    if value in (None, ""):
        return current_date()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError("日期格式必须为 YYYY-MM-DD") from exc


# SQLite 按 64 位整数绑定；更宽的值不会返回错误提示，而是直接在查询里抛 OverflowError。
INT_FIELD_LIMIT = 2 ** 63 - 1


def int_field(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if abs(number) > INT_FIELD_LIMIT:
        raise ValueError(f"{label}超出可填写范围")
    return number


def positive_int(value: Any, label: str = "积分") -> int:
    number = int_field(value, label)
    if number <= 0:
        raise ValueError(f"{label}必须大于 0")
    return number


def non_negative_int(value: Any, label: str) -> int:
    if value in (None, ""):
        return 0
    number = int_field(value, label)
    if number < 0:
        raise ValueError(f"{label}不能为负数")
    return number


def rule_flag(value: Any) -> int:
    return 1 if value not in (None, "", 0, False, "false", "0") else 0


def unit_choice_text(units: tuple[str, ...]) -> str:
    names = [RULE_UNIT_SHORT_TEXT[unit] for unit in units]
    return names[0] if len(names) < 2 else "、".join(names[:-1]) + "或" + names[-1]


def streak_rule_payload(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """streak_weeks 对扣分按 streak_unit 数周期，对加分却数目标窗口，所以按天的规则复用这个「周」命名的列。"""
    bonus = non_negative_int(payload.get("streak_bonus"), "连续扣分分值" if kind == "deduct" else "连续达标分值")
    periods = non_negative_int(payload.get("streak_weeks"), "连续期数")
    if bonus and periods <= 0:
        raise ValueError("连续奖惩需要大于 0 的连续期数")
    if kind == "deduct":
        unit = str(payload.get("streak_unit") or "week").strip().lower()
        if unit not in RULE_UNITS:
            raise ValueError(f"连续周期只能选{unit_choice_text(RULE_UNITS)}")
        return {"streak_weeks": periods, "streak_bonus": bonus, "streak_unit": unit}
    penalty = non_negative_int(payload.get("streak_penalty"), "连续未达标分值")
    penalty_periods = non_negative_int(payload.get("streak_penalty_weeks"), "连续未达标期数")
    if penalty and penalty_periods <= 0:
        raise ValueError("连续未达标扣分需要大于 0 的连续期数")
    if (bonus or penalty) and non_negative_int(payload.get("week_target") or 0, "目标数值") <= 0:
        raise ValueError("连续奖惩需要先设目标")
    return {
        "streak_weeks": periods,
        "streak_bonus": bonus,
        "streak_unit": "week",
        "streak_penalty_weeks": penalty_periods,
        "streak_penalty": penalty,
    }


def target_rule_payload(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """目标窗口的各列；那些以「周」命名的计数器，装的是项目自选的窗口长度和口径。"""
    unit = str(payload.get("target_unit") or "week").strip().lower()
    if unit not in RULE_UNITS:
        raise ValueError(f"目标周期只能选{unit_choice_text(RULE_UNITS)}")
    rule = {
        "week_target": non_negative_int(payload.get("week_target"), "目标数值"),
        "week_bonus": non_negative_int(payload.get("week_bonus"), "超额分值"),
        "target_unit": unit,
        "target_every": positive_int(payload.get("target_every") or 1, "目标周期长度"),
        "bonus_every": positive_int(payload.get("bonus_every") or 1, "奖励间隔"),
    }
    if kind == "deduct":
        # 扣分目标是「计数的提交不能超」的上限：不看数量口径，也不给未达标发奖励。
        return rule
    measure = str(payload.get("target_measure") or "count").strip().lower()
    if measure not in TARGET_MEASURES:
        raise ValueError("目标口径只能选次数或数量")
    # 数量口径的提交本身不积分；没有目标就没有可结算的依据。
    if measure == "quantity" and rule["week_target"] <= 0:
        raise ValueError("数量口径需要先设目标")
    rule["target_measure"] = measure
    rule["week_penalty"] = non_negative_int(payload.get("week_penalty"), "未达标扣分")
    return rule


def item_rule_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """一种项目类型的规则列；兑换品没有计分规则。"""
    if kind == "reward":
        return None
    target = target_rule_payload(payload, kind)
    result = {
        **target,
        **streak_rule_payload(payload, kind),
        # 数量在窗口里是累加的，所以「一天只算一次」这种只数次数的口径用不上。
        "count_days_once": 0 if target.get("target_measure") == "quantity" else rule_flag(payload.get("count_days_once")),
    }
    if kind == "earn":
        cap_period = str(payload.get("cap_period") or "").strip().lower()
        if cap_period and cap_period not in CAP_PERIOD_LABELS:
            raise ValueError("提交上限周期只能选每天、每周、每月、每季度、每半年或每年")
        result["cap_period"] = cap_period
        result["cap_count"] = positive_int(payload.get("cap_count") or 1, "上限次数") if cap_period else 1
    return result


def rule_is_active(rule: dict[str, Any]) -> bool:
    fields = ("week_target", "streak_weeks", "streak_bonus", "streak_penalty_weeks", "streak_penalty")
    return any(rule.get(field) for field in fields)


def points_per_yuan(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'points_per_yuan'").fetchone()
    try:
        rate = int(row["value"]) if row else DEFAULT_SETTINGS["points_per_yuan"]
    except (TypeError, ValueError):
        rate = DEFAULT_SETTINGS["points_per_yuan"]
    return rate if rate > 0 else DEFAULT_SETTINGS["points_per_yuan"]


def adventure_level_key(account_id: int) -> str:
    return f"adventure_level:{int(account_id)}"


def manual_adventure_level(conn: sqlite3.Connection, account_id: int | None) -> int | None:
    if account_id is None:
        return None
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (adventure_level_key(account_id),)).fetchone()
    try:
        level = int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return None
    return level if 1 <= level <= 99 else None


def total_experience(conn: sqlite3.Connection, account_id: int) -> int:
    return int(
        conn.execute(
            """
            SELECT COALESCE(SUM(t.reward_exp), 0)
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.account_id = ? AND ta.status = 'completed'
            """,
            (account_id,),
        ).fetchone()[0]
    )


def completed_task_count(conn: sqlite3.Connection, account_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM task_assignments WHERE account_id = ? AND status = 'completed'",
            (account_id,),
        ).fetchone()[0]
    )


def calculated_adventure_level(conn: sqlite3.Connection, account_id: int | None) -> int:
    if account_id is None:
        return 1
    return total_experience(conn, account_id) // 100 + 1


def adventure_level_for(conn: sqlite3.Connection, account_id: int | None) -> int:
    return manual_adventure_level(conn, account_id) or calculated_adventure_level(conn, account_id)


def adventure_benefits(conn: sqlite3.Connection, account_id: int | None) -> dict[str, Any]:
    level = adventure_level_for(conn, account_id)
    steps = min(max(level - 1, 0), 20)
    earn_bonus_percent = steps * 5
    exchange_discount_percent = steps * 2.5
    return {
        "level": level,
        "earn_bonus_percent": earn_bonus_percent,
        "exchange_discount_percent": exchange_discount_percent,
        "earn_multiplier": 1 + earn_bonus_percent / 100,
        "exchange_multiplier": 1 - exchange_discount_percent / 100,
    }


def adjusted_earn_coins(conn: sqlite3.Connection, account_id: int, coins: int) -> int:
    benefits = adventure_benefits(conn, account_id)
    return max(1, int(math.ceil(coins * benefits["earn_multiplier"])))


def adjusted_exchange_coins(conn: sqlite3.Connection, account_id: int, coins: int) -> int:
    benefits = adventure_benefits(conn, account_id)
    return max(1, int(math.floor(coins * benefits["exchange_multiplier"])))


def settings_payload(conn: sqlite3.Connection, account_id: int | None = None) -> dict[str, Any]:
    manual_level = manual_adventure_level(conn, account_id)
    return {
        "points_per_yuan": points_per_yuan(conn),
        "manual_adventure_level": manual_level,
        "adventure_level_mode": "manual" if manual_level is not None else "auto",
    }


def safe_account(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "avatar": row["avatar"] or ("adult-male" if row["role"] == "admin" else "boy"),
        "active": bool(row["active"]),
    }


def current_user(conn: sqlite3.Connection) -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = conn.execute("SELECT * FROM accounts WHERE id = ? AND active = 1", (user_id,)).fetchone()
    if user is None:
        session.clear()
    return user


def first_child_id(conn: sqlite3.Connection) -> int | None:
    """还没选中孩子时的兜底：在册的第一个孩子，与统计遍历的顺序一致。"""
    row = conn.execute("SELECT id FROM accounts WHERE role = 'child' AND active = 1 ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def active_child_id(conn: sqlite3.Connection, user: sqlite3.Row) -> int | None:
    if user["role"] == "child":
        return int(user["id"])
    selected = session.get("selected_child_id")
    if selected:
        child = conn.execute(
            "SELECT id FROM accounts WHERE id = ? AND role = 'child' AND active = 1", (selected,)
        ).fetchone()
        if child:
            return int(child["id"])
    fallback = first_child_id(conn)
    if fallback is not None:
        session["selected_child_id"] = fallback
    return fallback


def require_user(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        with connection() as conn:
            user = current_user(conn)
            if user is None:
                return api_error("请先登录", 401)
        return handler(*args, **kwargs)

    return wrapped


def require_admin(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        with connection() as conn:
            user = current_user(conn)
            if user is None:
                return api_error("请先登录", 401)
            if user["role"] != "admin":
                return api_error("只有管理员可以执行此操作", 403)
        return handler(*args, **kwargs)

    return wrapped


def row_item(row: sqlite3.Row, kind: str) -> dict[str, Any]:
    points = int(row["points"])
    if kind == "deduct":
        points = -points
    item = {
        "id": row["id"],
        "name": row["name"],
        "points": points,
        "icon": row["icon"] or ITEM_DEFAULT_ICONS[kind],
    }
    if kind == "reward":
        return item
    item.update(
        {
            "streak_weeks": int(row["streak_weeks"] or 0),
            "streak_bonus": int(row["streak_bonus"] or 0),
            "streak_unit": row["streak_unit"] or "week",
            "rule_set_at": row["rule_set_at"],
            "week_target": int(row["week_target"] or 0),
            "week_bonus": int(row["week_bonus"] or 0),
            "target_unit": row["target_unit"] or "week",
            "target_every": int(row["target_every"] or 1),
            "bonus_every": int(row["bonus_every"] or 1),
            "count_days_once": int(row["count_days_once"] or 0),
        }
    )
    if kind == "earn":
        item.update(
            {
                "week_penalty": int(row["week_penalty"] or 0),
                "streak_penalty_weeks": int(row["streak_penalty_weeks"] or 0),
                "streak_penalty": int(row["streak_penalty"] or 0),
                "target_measure": row["target_measure"] or "count",
                "cap_period": row["cap_period"] or "",
                "cap_count": int(row["cap_count"] or 1),
            }
        )
    else:
        # 扣分项目只按次数计分，目标又是上限口径，所以未达标奖励那几项保持默认值。
        item.update({"week_penalty": 0, "streak_penalty_weeks": 0, "streak_penalty": 0, "target_measure": "count"})
    return item


def rule_overrides(conn: sqlite3.Connection, account_id: int) -> dict[tuple[str, int], dict[str, Any]]:
    """一次查询取回某个孩子的全部覆盖；没有这个 key 就说明用的是项目自身的规则。"""
    rows = conn.execute("SELECT * FROM item_rule_overrides WHERE account_id = ?", (account_id,)).fetchall()
    return {(str(row["item_kind"]), int(row["item_id"])): dict(row) for row in rows}


def apply_rule_override(item: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """把孩子的规则快照盖在项目行上；覆盖并入规则只有这一处。"""
    if override is not None:
        item.update({field: override[field] for field in RULE_OVERRIDE_FIELDS})
    return item


def effective_item(
    conn: sqlite3.Connection,
    kind: str,
    row: sqlite3.Row,
    account_id: int | None,
    overrides: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """引擎和界面消费的那份项目字典，已经带上这个孩子的规则。"""
    if account_id is None or kind == "reward":
        return row_item(row, kind)
    if overrides is None:
        overrides = rule_overrides(conn, account_id)
    return apply_rule_override(row_item(row, kind), overrides.get((kind, int(row["id"]))))


def visible_item(item: dict[str, Any], kind: str, conn: sqlite3.Connection, user: sqlite3.Row) -> dict[str, Any]:
    if user["role"] != "child":
        return item
    base_points = abs(int(item["points"]))
    if kind == "earn":
        item["base_points"] = base_points
        item["points"] = adjusted_earn_coins(conn, int(user["id"]), base_points)
    elif kind == "reward":
        item["base_points"] = base_points
        item["points"] = adjusted_exchange_coins(conn, int(user["id"]), base_points)
    return item


def get_balance(conn: sqlite3.Connection, account_id: int) -> int:
    return int(
        conn.execute("SELECT COALESCE(SUM(amount), 0) FROM records WHERE account_id = ?", (account_id,)).fetchone()[0]
    )


def child_account_ids(conn: sqlite3.Connection) -> list[int]:
    """所有在册的孩子，顺序与结算和统计遍历他们时一致。"""
    rows = conn.execute("SELECT id FROM accounts WHERE role = 'child' AND active = 1 ORDER BY id").fetchall()
    return [int(row["id"]) for row in rows]


def excluded_item_ids(conn: sqlite3.Connection, kind: str, account_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT item_id FROM item_bindings WHERE item_kind = ? AND account_id = ? AND enabled = 0",
        (kind, account_id),
    ).fetchall()
    return {int(row["item_id"]) for row in rows}


def item_rows(conn: sqlite3.Connection, kind: str, account_id: int | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {ITEM_TABLES[kind]} ORDER BY sort_order, id").fetchall()
    overrides = rule_overrides(conn, account_id) if account_id is not None else None
    items = [effective_item(conn, kind, row, account_id, overrides) for row in rows]
    if account_id is None:
        return items
    excluded = excluded_item_ids(conn, kind, account_id)
    return [item for item in items if item["id"] not in excluded]


def scored_items(
    conn: sqlite3.Connection, kind: str, account_id: int | None = None
) -> list[dict[str, Any]]:
    """某个计分类型下的全部项目，已套用这个孩子的规则，按 id 排序。

    这里不筛绑定：结算遍历自己会跳过未绑定的项目，
    这样才能找到并重算某个项目解绑之前占过的窗口。
    """
    rows = conn.execute(f"SELECT * FROM {ITEM_TABLES[kind]} ORDER BY id").fetchall()
    overrides = rule_overrides(conn, account_id) if account_id is not None else None
    return [effective_item(conn, kind, row, account_id, overrides) for row in rows]


def binding_state(conn: sqlite3.Connection, child_ids: list[int]) -> dict[str, dict[str, list[int]]]:
    """每个孩子被排除的项目 id；没有排除行的孩子等于绑定整个库。"""
    state = {str(child_id): {kind: [] for kind in ITEM_TABLES} for child_id in child_ids}
    rows = conn.execute("SELECT item_kind, item_id, account_id FROM item_bindings WHERE enabled = 0").fetchall()
    for row in rows:
        bucket = state.setdefault(str(int(row["account_id"])), {kind: [] for kind in ITEM_TABLES})
        if row["item_kind"] in bucket:
            bucket[row["item_kind"]].append(int(row["item_id"]))
    for bucket in state.values():
        for ids in bucket.values():
            ids.sort()
    return state


def rule_override_state(conn: sqlite3.Connection, child_ids: list[int]) -> dict[str, dict[str, dict[str, Any]]]:
    """每个孩子自带规则快照的那些项目；孩子或项目不在这里，就跟随库里的规则。"""
    state = {str(child_id): {kind: {} for kind in ("earn", "deduct")} for child_id in child_ids}
    for row in conn.execute("SELECT * FROM item_rule_overrides"):
        bucket = state.get(str(int(row["account_id"])))
        if bucket is not None and row["item_kind"] in bucket:
            bucket[row["item_kind"]][str(int(row["item_id"]))] = {field: row[field] for field in RULE_OVERRIDE_FIELDS}
    return state


def set_item_binding(conn: sqlite3.Connection, kind: str, item_id: int, account_id: int, enabled: bool) -> None:
    if enabled:
        conn.execute(
            "DELETE FROM item_bindings WHERE item_kind = ? AND item_id = ? AND account_id = ?",
            (kind, item_id, account_id),
        )
        return
    conn.execute(
        """
        INSERT INTO item_bindings(item_kind, item_id, account_id, enabled, updated_at) VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(item_kind, item_id, account_id) DO UPDATE SET enabled = 0, updated_at = excluded.updated_at
        """,
        (kind, item_id, account_id, now_iso()),
    )


def reorder_items(conn: sqlite3.Connection, kind: str, ids: list[int]) -> None:
    """保存拖拽后的顺序；请求里没列出的 id 保持相对顺序排在最后。"""
    table = ITEM_TABLES[kind]
    existing = [int(row["id"]) for row in conn.execute(f"SELECT id FROM {table} ORDER BY sort_order, id").fetchall()]
    known = set(existing)
    ordered = [item_id for item_id in ids if item_id in known]
    ordered += [item_id for item_id in existing if item_id not in ordered]
    for position, moved_id in enumerate(ordered):
        conn.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (position, moved_id))


def load_bound_item(
    conn: sqlite3.Connection, kind: str, item_id: int | None, account_id: int, label: str
) -> dict[str, Any] | None:
    if item_id is None:
        return None
    row = conn.execute(f"SELECT * FROM {ITEM_TABLES[kind]} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError(f"{label}不存在")
    if item_id in excluded_item_ids(conn, kind, account_id):
        raise ValueError(f"{label}未绑定给当前孩子")
    # 存储的分值符号保持不变：调用方自己给扣分取负，这一点和 UI 序列化器不同。
    return apply_rule_override(dict(row), rule_overrides(conn, account_id).get((kind, item_id)))


def window_unit(unit: str, every: int = 1) -> tuple[str, int]:
    """季和年复用「月」锚点——它本来就始于一月，以及每隔 12 个月的一月。"""
    if unit == "quarter":
        return ("month", every * 3)
    if unit == "year":
        return ("month", every * 12)
    return (unit, every)


def period_start(unit: str, day: str, every: int = 1) -> str:
    """自然日历锚点：周从周一开始，月从 1 号开始；every>1 从 WINDOW_EPOCH 起数整段窗口。"""
    unit, every = window_unit(unit, every)
    current = date.fromisoformat(day[:10])
    if unit == "month":
        index = current.year * 12 + current.month - 1
        index -= (index - MONTH_EPOCH_INDEX) % every
        return date(index // 12, index % 12 + 1, 1).isoformat()
    if unit == "week":
        monday = current - timedelta(days=current.weekday())
        return (monday - timedelta(days=7 * ((monday - WINDOW_EPOCH).days // 7 % every))).isoformat()
    return (current - timedelta(days=(current - WINDOW_EPOCH).days % every)).isoformat()


def period_shift(unit: str, every: int, start: str, steps: int) -> str:
    """从一个对齐的窗口起点往后推 windows 个窗口的起点；按月则直接按序号推进。"""
    unit, every = window_unit(unit, every)
    day = date.fromisoformat(start[:10])
    if unit == "month":
        index = day.year * 12 + day.month - 1 + every * steps
        return date(index // 12, index % 12 + 1, 1).isoformat()
    return (day + timedelta(days={"week": 7, "day": 1}[unit] * every * steps)).isoformat()


def period_end(unit: str, start: str, every: int = 1) -> str:
    """窗口内最后一天，含当天。"""
    return (date.fromisoformat(period_shift(unit, every, start, 1)) - timedelta(days=1)).isoformat()


def period_range(unit: str, start: str, end: str, every: int = 1) -> Iterator[str]:
    """从 start 开始、到 end 之前（不含）的所有窗口起点。"""
    period = start
    while period < end:
        yield period
        period = period_shift(unit, every, period, 1)


def target_lookback_steps(unit: str, every: int) -> int:
    return max(2, -(-SETTLE_LOOKBACK_PERIODS[unit] // every))


def period_label(unit: str, start: str) -> str:
    """手动结算下拉里的期次名称；季和年挂在「月」锚点上，所以需要自己的文案。"""
    day = date.fromisoformat(start)
    if unit == "week":
        return f"{day.month}月{day.day}日那周"
    if unit == "month":
        return f"{day.year}年{day.month}月"
    if unit == "quarter":
        return f"{day.year}年第{(day.month - 1) // 3 + 1}季度"
    if unit == "year":
        return f"{day.year}年"
    return start


def settled_window_starts(conn: sqlite3.Connection, unit: str) -> set[str]:
    """这个单位已经判定过的期次。按天的规则共用水位表，所以由项目自己的目标单位决定。"""
    units: dict[tuple[str, int], set[str]] = {}
    for account_id in child_account_ids(conn):
        for kind, item in rule_items(conn, account_id):
            units.setdefault((kind, int(item["id"])), set()).add(item_target(item)[0])
    return {
        row["week_start"]
        for row in conn.execute("SELECT item_kind, item_id, week_start FROM weekly_settlements")
        if unit in units.get((row["item_kind"], int(row["item_id"])), set())
    }


def settlement_periods(unit: str, today: str, settled: set[str]) -> list[dict[str, Any]]:
    """当前期次，加上手动结算还够得着的所有更早期次，新的在前。"""
    current = period_start(unit, today)
    periods = []
    for steps in range(SETTLE_LOOKBACK_PERIODS[unit] + 1):
        start = period_shift(unit, 1, current, -steps)
        end = period_end(unit, start)
        periods.append(
            {
                "start": start,
                "end": end,
                "label": period_label(unit, start),
                "closed": end < today,
                "settled": start in settled,
            }
        )
    return periods


def completion_counts(
    conn: sqlite3.Connection, account_id: int, kind: str, since_date: str | None = None
) -> dict[int, dict[str, list[int]]]:
    """按项目和日期给出 [发放次数, 数量合计]。结算记录一律不计入。"""
    clause = "account_id = ? AND " + COMPLETION_FILTERS[kind] + (" AND date >= ?" if since_date else "")
    args: list[Any] = [account_id, since_date] if since_date else [account_id]
    rows = conn.execute(f"SELECT item_id, date, quantity FROM records WHERE {clause}", args).fetchall()
    counts: dict[int, dict[str, list[int]]] = {}
    for row in rows:
        per_date = counts.setdefault(int(row["item_id"]), {})
        tally = per_date.setdefault(row["date"], [0, 0])
        tally[0] += 1
        tally[1] += int(row["quantity"] or 0)
    return counts


def dates_within(per_date: dict[str, list[int]], start: str, until: str) -> dict[str, list[int]]:
    return {day: tally for day, tally in per_date.items() if start <= day < until}


def item_target(item: sqlite3.Row | dict[str, Any]) -> tuple[str, int]:
    """窗口的单位和长度；那些以「周」命名的列装的是项目自选单位下的数值。"""
    unit = item["target_unit"] or "week"
    return unit, max(int(item["target_every"] or 1), 1)


def completion_count(item: sqlite3.Row | dict[str, Any], per_date: dict[str, list[int]]) -> int:
    """按项目自己的口径统计窗口：数量合计、不同天数，还是发放次数。"""
    if not per_date:
        return 0
    if is_quantity(item):
        return sum(tally[1] for tally in per_date.values())
    if int(item["count_days_once"] or 0):
        return len(per_date)
    return sum(tally[0] for tally in per_date.values())


def is_quantity(item: sqlite3.Row | dict[str, Any]) -> bool:
    return str(item["target_measure"] or "count") == "quantity"


def window_count(item: sqlite3.Row | dict[str, Any], per_date: dict[str, list[int]], start: str) -> int:
    unit, every = item_target(item)
    return completion_count(item, dates_within(per_date, start, period_shift(unit, every, start, 1)))


def deduct_completion_dates(conn: sqlite3.Connection, account_id: int, since_date: str) -> dict[int, set[str]]:
    """每个孩子给每个扣分项目打分的日期，所以连续链只需要知道每一期有没有出现。"""
    rows = conn.execute(
        f"SELECT item_id, date FROM records WHERE account_id = ? AND {DEDUCT_COMPLETION_FILTER} AND date >= ?",
        (account_id, since_date),
    ).fetchall()
    dates: dict[int, set[str]] = {}
    for row in rows:
        dates.setdefault(int(row["item_id"]), set()).add(row["date"])
    return dates


def cap_window(period: str, day: str) -> tuple[str, str]:
    """提交上限的日历窗口：周窗口以周一为界，月/季/半年/年窗口从各自周期首月的 1 号起算。"""
    if period == "day":
        return day, day
    if period == "week":
        start = period_start("week", day)
        return start, (date.fromisoformat(start) + timedelta(days=6)).isoformat()
    months = CAP_PERIOD_MONTHS[period]
    current = date.fromisoformat(day)
    start = current.replace(month=(current.month - 1) // months * months + 1, day=1)
    next_index = start.year * 12 + start.month - 1 + months
    end = date(next_index // 12, next_index % 12 + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def ensure_submit_cap(
    conn: sqlite3.Connection,
    item_id: int | None,
    account_id: int,
    day: str,
    exclude_request_id: int | None = None,
) -> None:
    """对设了上限的项目限制每个窗口的提交次数；待审请求已经占掉一个名额。"""
    if item_id is None:
        return
    row = conn.execute("SELECT * FROM earn_items WHERE id = ?", (item_id,)).fetchone()
    item = effective_item(conn, "earn", row, account_id) if row is not None else None
    period = (item["cap_period"] or "") if item is not None else ""
    if period not in CAP_PERIOD_LABELS:
        return
    limit = int(item["cap_count"] or 1)
    start, end = cap_window(period, day)
    used = conn.execute(
        f"""
        SELECT COUNT(*) FROM records
        WHERE account_id = ? AND item_id = ? AND date BETWEEN ? AND ? AND {EARN_COMPLETION_FILTER}
        """,
        (account_id, item_id, start, end),
    ).fetchone()[0]
    sql = """
        SELECT COUNT(*) FROM point_requests
        WHERE account_id = ? AND item_id = ? AND item_kind = 'earn' AND status = 'pending'
          AND date BETWEEN ? AND ?
        """
    args: list[Any] = [account_id, item_id, start, end]
    if exclude_request_id is not None:
        sql += " AND id <> ?"
        args.append(int(exclude_request_id))
    used += conn.execute(sql, args).fetchone()[0]
    if used >= limit:
        raise ValueError(f"该项目{CAP_PERIOD_LABELS[period]}只能提交 {limit} 次")


def attach_target_stats(
    conn: sqlite3.Connection, items: list[dict[str, Any]], account_id: int | None, kind: str
) -> None:
    if not items or account_id is None:
        return
    today = current_date()
    windows = {}
    for item in items:
        unit, every = item_target(item)
        start = period_start(unit, today, every)
        windows[item["id"]] = (start, period_shift(unit, every, start, 1))
    counts = completion_counts(conn, int(account_id), kind)
    for item in items:
        per_date = counts.get(item["id"], {})
        start, until = windows[item["id"]]
        measure = 1 if is_quantity(item) else 0
        # 角标统计的是列表上方选定的日期，而补录可以指定任意过去某天，所以每天都留一份计数。
        item["day_counts"] = {day: tally[measure] for day, tally in per_date.items()}
        item["week_count"] = completion_count(item, dates_within(per_date, start, until))


def target_rule_active(item: dict[str, Any], kind: str) -> bool:
    """这个孩子的生效规则在窗口结束时发奖还是罚分：加分目标看超额、未达标、数量和连续；
    扣分目标是上限，只罚超额部分。"""
    if int(item["week_target"] or 0) <= 0:
        return False
    if kind == "deduct":
        return int(item["week_bonus"] or 0) > 0
    return (
        int(item["week_bonus"] or 0) > 0
        or int(item["week_penalty"] or 0) > 0
        or is_quantity(item)
        or int(item["streak_bonus"] or 0) > 0
        or int(item["streak_penalty"] or 0) > 0
    )


def rule_items(conn: sqlite3.Connection, account_id: int | None = None) -> list[tuple[str, dict[str, Any]]]:
    """某个孩子名下、生效的目标窗口结束时会被结算的项目；不指定孩子时，就是库自己的规则筛出来的项目。"""
    return [
        (kind, item)
        for kind in ("earn", "deduct")
        for item in scored_items(conn, kind, account_id)
        if target_rule_active(item, kind)
    ]


def target_bonus(item: dict[str, Any], tally: int) -> tuple[str, int] | None:
    """数量口径的项目达标后先付一次分值，超出目标每满 bonus_every 再加一份奖励；次数口径只付超额部分。"""
    every = max(int(item["bonus_every"] or 1), 1)
    bonus = int(item["week_bonus"] or 0)
    target = int(item["week_target"])
    over = tally - target
    # 具体是哪条规则发的钱由记录的 source 承担，标题只写项目和它相对目标的计数。
    title = f"{item['name']} {tally}/{target}"
    if is_quantity(item):
        payout = int(item["points"]) + (over // every) * bonus if over >= 0 else 0
        return (title, payout) if payout else None
    times = over // every if bonus and over > 0 else 0
    return (title, times * bonus) if times else None


def add_settlement_record(
    conn: sqlite3.Connection,
    account_id: int,
    title: str,
    amount: int,
    item_kind: str,
    item_id: int,
    source: str,
    day: str,
    timestamp: str,
) -> None:
    conn.execute(
        f"""
        INSERT INTO records(account_id, title, amount, type, date, time, created_at, item_kind, item_id, source)
        VALUES (?, ?, ?, ?, ?, '23:59', ?, ?, ?, ?)
        ON CONFLICT(account_id, item_kind, item_id, date, source) WHERE {SETTLEMENT_SOURCE_PREDICATE} DO NOTHING
        """,
        (
            account_id,
            title,
            amount,
            "income" if amount > 0 else "expense",
            day,
            timestamp,
            item_kind,
            item_id,
            source,
        ),
    )


def full_window_start(unit: str, every: int, anchor: str) -> str:
    """项目从第一天起就完整经历的第一个窗口：规则定在半途，就从下一个窗口开始计数。"""
    start = period_start(unit, anchor, every)
    return period_shift(unit, every, start, 1) if anchor > start else start


def item_window_floor(item: sqlite3.Row | dict[str, Any], today: str) -> str:
    """惰性结算能判定的最早窗口：受回看长度约束，且绝不早于项目自己的规则。"""
    unit, every = item_target(item)
    current = period_start(unit, today, every)
    floor = period_shift(unit, every, current, -target_lookback_steps(unit, every))
    anchor = str(item["rule_set_at"] or "")[:10]
    return max(floor, full_window_start(unit, every, anchor)) if anchor else floor


def settled_floor(
    conn: sqlite3.Connection,
    account_id: int,
    kind: str,
    item_id: int,
    unit: str,
    every: int,
    sources: tuple[str, ...],
    audited: bool,
) -> str:
    """这个项目还能被发钱的第一个窗口：结算只从它最新的标记往前推，不倒回去。"""
    marks: list[str] = []
    row = conn.execute(
        f"""
        SELECT MAX(date) AS day FROM records
        WHERE account_id = ? AND item_kind = ? AND item_id = ? AND source IN ({", ".join("?" * len(sources))})
        """,
        (account_id, kind, item_id, *sources),
    ).fetchone()
    if row["day"]:
        marks.append(str(row["day"])[:10])
    if audited:
        row = conn.execute(
            """
            SELECT MAX(week_start) AS day FROM weekly_settlements
            WHERE account_id = ? AND item_kind = ? AND item_id = ?
            """,
            (account_id, kind, item_id),
        ).fetchone()
        if row["day"]:
            marks.append(str(row["day"])[:10])
    if not marks:
        return ""
    return period_shift(unit, every, period_start(unit, max(marks), every), 1)


def settlement_families(item: dict[str, Any], kind: str, today: str) -> list[tuple[str, int, tuple[str, ...], str, bool]]:
    """这个项目会结算的窗口族：(单位, 长度, source, 最早可重算, 是否拥有水位行)。"""
    unit, every = item_target(item)
    families = [(unit, every, TARGET_WINDOW_SOURCES[kind], item_window_floor(item, today), True)]
    if kind == "deduct" and int(item["streak_weeks"] or 0) and int(item["streak_bonus"] or 0):
        streak_unit = item["streak_unit"]
        start = streak_start(streak_unit, today, item["rule_set_at"])
        families.append((streak_unit, 1, (DEDUCT_STREAK_SOURCES[streak_unit],), start, False))
    return families


def resettle_item_windows(
    conn: sqlite3.Connection,
    account_id: int,
    kind: Any,
    item_id: Any,
    since_date: str,
    unit: str | None = None,
) -> tuple[str, ...]:
    """把某个项目的这些族从给定日期起回退，返回尾部被清掉的那些单位。"""
    if kind not in ("earn", "deduct") or not item_id:
        return ()
    row = conn.execute(f"SELECT * FROM {ITEM_TABLES[kind]} WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return ()
    item = effective_item(conn, kind, row, account_id)
    item_id = int(item["id"])
    cleared: list[str] = []
    for family_unit, every, sources, floor, audited in settlement_families(item, kind, current_date()):
        if unit and family_unit != unit:
            continue
        # 每一族按自己的单位定位：清了别族的尾巴，只会删掉一批永远不会重算的行。
        start = max(period_start(family_unit, since_date, every), floor)
        if audited:
            conn.execute(
                "DELETE FROM weekly_settlements WHERE account_id = ? AND item_kind = ? AND item_id = ? AND week_start >= ?",
                (account_id, kind, item_id, start),
            )
        conn.execute(
            f"""
            DELETE FROM records
            WHERE account_id = ? AND item_kind = ? AND item_id = ? AND date >= ?
              AND source IN ({", ".join("?" * len(sources))})
            """,
            (account_id, kind, item_id, start, *sources),
        )
        cleared.append(family_unit)
    return tuple(cleared)


def regrade_after_log(
    conn: sqlite3.Connection, account_id: int, kind: Any, item_id: Any, log_date: str
) -> None:
    """从这条记录的日期起清掉该项目的所有窗口并重算，让调用方自己的应答就是最新的。"""
    units = resettle_item_windows(conn, account_id, kind, item_id, log_date)
    if units:
        settle_target_rules(conn, [account_id], units, log_date, int(item_id))
        settle_streaks(conn, [account_id], units, log_date, int(item_id))


def regrade_windows(conn: sqlite3.Connection, account_ids: list[int], unit: str, since: str) -> None:
    """从选定那一期起撤销某个单位已经发放的部分，让结算遍历把整条链重算一遍。"""
    for raw_account_id in account_ids:
        account_id = int(raw_account_id)
        # 每个孩子只重算他自己的规则在这个单位下会结算的项目，覆盖行一起算。
        item_ids: dict[str, list[int]] = {"earn": [], "deduct": []}
        for kind, item in rule_items(conn, account_id):
            if item_target(item)[0] == unit:
                item_ids[kind].append(int(item["id"]))
        for item in streak_rule_items(conn, account_id):
            item_id = int(item["id"])
            if item["streak_unit"] == unit and item_id not in item_ids["deduct"]:
                item_ids["deduct"].append(item_id)
        for kind, ids in item_ids.items():
            for item_id in ids:
                resettle_item_windows(conn, account_id, kind, item_id, since, unit)


def settle_target_window(
    conn: sqlite3.Connection,
    account_id: int,
    kind: str,
    item: dict[str, Any],
    per_date: dict[str, list[int]],
    start: str,
    timestamp: str,
) -> None:
    """结算一个已经闭合的目标窗口；week_start 保存窗口的第一天。"""
    # 扣分超额是一笔账单：它以负分记录落账，并落在审计行的罚分一侧。
    charge = kind == "deduct"
    item_id = int(item["id"])
    unit, every = item_target(item)
    tally = window_count(item, per_date, start)
    quota = int(item["week_target"])
    bonus = target_bonus(item, tally)
    shortfall = int(item["week_penalty"]) if tally < quota else 0
    if not bonus and not shortfall:
        return
    payout = bonus[1] if bonus else 0
    # 钱还没动之前先抢下这行唯一的窗口记录，所以两趟结算撞同一窗口也只发一次。
    claimed = conn.execute(
        """
        INSERT INTO weekly_settlements(
            account_id, item_kind, item_id, week_start, completions, target, bonus, penalty, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, item_kind, item_id, week_start) DO NOTHING
        """,
        (
            account_id,
            kind,
            item_id,
            start,
            tally,
            quota,
            0 if charge else payout,
            payout if charge else shortfall,
            timestamp,
        ),
    )
    if not claimed.rowcount:
        return
    settle_date = period_end(unit, start, every)
    if bonus:
        add_settlement_record(
            conn,
            account_id,
            bonus[0],
            -payout if charge else payout,
            kind,
            item_id,
            TARGET_BONUS_SOURCES[kind],
            settle_date,
            timestamp,
        )
    if shortfall:
        add_settlement_record(
            conn,
            account_id,
            f"{item['name']} {tally}/{quota}",
            -shortfall,
            kind,
            item_id,
            "weekly_penalty",
            settle_date,
            timestamp,
        )


def settle_earn_streak(
    conn: sqlite3.Connection,
    account_id: int,
    item: dict[str, Any],
    per_date: dict[str, list[int]],
    windows: list[str],
    timestamp: str,
    only_from: str = "",
) -> None:
    """奖励链按连续达标的整段发放，每满一段发一次；罚分链按连续未达标发放，每漏一段扣一次。"""
    chains = (
        (int(item["streak_weeks"] or 0), int(item["streak_bonus"] or 0), TARGET_STREAK_SOURCE),
        (int(item["streak_penalty_weeks"] or 0), -int(item["streak_penalty"] or 0), TARGET_STREAK_PENALTY_SOURCE),
    )
    item_id = int(item["id"])
    unit, every = item_target(item)
    # 就算只发尾巴那一段，段数也要按整条链来数，所以这个过滤条件要落在写入上。
    only_from = period_start(unit, only_from, every) if only_from else ""
    quota = int(item["week_target"])
    for target, amount, source in chains:
        if not target or not amount:
            continue
        on_target = amount > 0
        run = 0
        for start in windows:
            run = run + 1 if (window_count(item, per_date, start) >= quota) == on_target else 0
            if not run or run % target or start < only_from:
                continue
            settle_date = period_end(unit, start, every)
            add_settlement_record(
                conn,
                account_id,
                f"{item['name']} 连续 {target} 期",
                amount,
                "earn",
                item_id,
                source,
                settle_date,
                timestamp,
            )


def settles_item(item: dict[str, Any], only_item_id: int | None) -> bool:
    """这一趟遍历是否覆盖这个项目：某条记录的重算只点名自己那个项目，别的项目不受影响。"""
    return only_item_id is None or int(item["id"]) == only_item_id


def settle_target_rules(
    conn: sqlite3.Connection,
    account_ids: list[int],
    units: tuple[str, ...] = RULE_UNITS,
    only_from: str = "",
    only_item_id: int | None = None,
) -> None:
    """两个计分方向的目标窗口惰性结算：逐孩子进行，且只算他们绑定的项目。"""
    if not account_ids:
        return
    today = current_date()
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for raw_account_id in account_ids:
        account_id = int(raw_account_id)
        # 规则逐孩子读取，所以同一个项目可以只为某一个孩子在某个窗口结算。
        plans = []
        for kind, item in rule_items(conn, account_id):
            unit, every = item_target(item)
            if unit not in units or not settles_item(item, only_item_id):
                continue
            plans.append(
                (
                    kind,
                    item,
                    unit,
                    every,
                    period_start(unit, today, every),
                    item_window_floor(item, today),
                    period_start(unit, only_from, every) if only_from else "",
                )
            )
        if not plans:
            continue
        kinds = {plan[0] for plan in plans}
        since = min(plan[5] for plan in plans)
        counts = {kind: completion_counts(conn, account_id, kind, since) for kind in kinds}
        blocked = {kind: excluded_item_ids(conn, kind, account_id) for kind in kinds}
        for kind, item, unit, every, current, floor, from_start in plans:
            item_id = int(item["id"])
            per_date = counts[kind].get(item_id) or {}
            # 孩子从没记录过的项目，既没有可奖励的也没有可罚的。
            if not per_date or item_id in blocked[kind]:
                continue
            first = max(floor, period_start(unit, min(per_date), every))
            windows = list(period_range(unit, first, current, every))
            # 连续链依旧从整个回看区间开始数，但钱只落在项目最新标记之后，
            # 所以常规遍历不会重发已经判定过的窗口；某条记录自己的重算会先清掉这些标记。
            write_from = max(
                from_start,
                settled_floor(conn, account_id, kind, item_id, unit, every, TARGET_WINDOW_SOURCES[kind], True),
            )
            for start in windows:
                if start >= write_from:
                    settle_target_window(conn, account_id, kind, item, per_date, start, timestamp)
            if kind == "earn":
                settle_earn_streak(conn, account_id, item, per_date, windows, timestamp, write_from)


def streak_start(unit: str, today: str, rule_set_at: str | None) -> str:
    """连续规则还会判定的最早窗口：有界的回看，且绝不越过规则设定日。"""
    start = period_shift(unit, 1, period_start(unit, today), -SETTLE_LOOKBACK_PERIODS[unit])
    anchor = str(rule_set_at or "")[:10]
    return max(start, full_window_start(unit, 1, anchor)) if anchor else start


def streak_rule_items(conn: sqlite3.Connection, account_id: int | None = None) -> list[dict[str, Any]]:
    """这个孩子名下、生效规则会发连续罚分的扣分项目。"""
    return [
        item
        for item in scored_items(conn, "deduct", account_id)
        if int(item["streak_weeks"] or 0) > 0 and int(item["streak_bonus"] or 0) > 0
    ]


def settle_streaks(
    conn: sqlite3.Connection,
    account_ids: list[int],
    units: tuple[str, ...] = RULE_UNITS,
    only_from: str = "",
    only_item_id: int | None = None,
) -> None:
    """扣分项目每连续 N 个周期被打分，就额外多扣一次。"""
    if not account_ids:
        return
    today = current_date()
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for raw_account_id in account_ids:
        account_id = int(raw_account_id)
        items = [
            item
            for item in streak_rule_items(conn, account_id)
            if item["streak_unit"] in units and settles_item(item, only_item_id)
        ]
        if not items:
            continue
        starts = {int(item["id"]): streak_start(item["streak_unit"], today, item["rule_set_at"]) for item in items}
        since = min(starts.values())
        blocked = excluded_item_ids(conn, "deduct", account_id)
        scored = deduct_completion_dates(conn, account_id, since)
        for item in items:
            item_id = int(item["id"])
            dates = scored.get(item_id)
            if not dates or item_id in blocked:
                continue
            unit = item["streak_unit"]
            start = starts[item_id]
            from_start = max(
                period_start(unit, only_from) if only_from else "",
                settled_floor(conn, account_id, "deduct", item_id, unit, 1, (DEDUCT_STREAK_SOURCES[unit],), False),
            )
            periods = {period_start(unit, day) for day in dates}
            target = int(item["streak_weeks"])
            source = DEDUCT_STREAK_SOURCES[unit]
            streak = 0
            for period in period_range(unit, start, period_start(unit, today)):
                streak = streak + 1 if period in periods else 0
                # 连续段从整个回看区间开始数；只有选中的尾巴那一段可以写入。
                if not streak or streak % target or period < from_start:
                    continue
                settle_date = period_end(unit, period)
                add_settlement_record(
                    conn,
                    account_id,
                    f"{item['name']} 连续 {target} {STREAK_PERIOD_TEXT[unit]}",
                    -int(item["streak_bonus"]),
                    "deduct",
                    item_id,
                    source,
                    settle_date,
                    timestamp,
                )


def infer_record_source(title: str, item_kind: str | None) -> str:
    text = title or ""
    if text.startswith("兑换现金"):
        return "cash_exchange"
    if text.startswith("任务完成"):
        return "task"
    if text.startswith("补积分"):
        return "manual"
    if item_kind == "earn":
        return "earn"
    if item_kind == "deduct":
        return "deduct"
    if item_kind == "reward":
        return "exchange"
    return "manual"


def match_record_item(conn: sqlite3.Connection, title: str, amount: int) -> tuple[str, int] | None:
    for kind in ("earn",) if amount > 0 else ("deduct", "reward"):
        row = conn.execute(
            f"SELECT id FROM {ITEM_TABLES[kind]} WHERE name = ? LIMIT 1", (title,)
        ).fetchone()
        if row is not None:
            return kind, int(row["id"])
    return None


def record_label(row: sqlite3.Row, item_names: dict[tuple[str, int], str]) -> str:
    key = (row["item_kind"], int(row["item_id"])) if row["item_kind"] and row["item_id"] else None
    return item_names.get(key, row["title"])


def item_name_map(conn: sqlite3.Connection) -> dict[tuple[str, int], str]:
    mapping: dict[tuple[str, int], str] = {}
    for kind, table in ITEM_TABLES.items():
        for row in conn.execute(f"SELECT id, name FROM {table}").fetchall():
            mapping[(kind, int(row["id"]))] = row["name"]
    return mapping


def has_duplicate_item_name(conn: sqlite3.Connection, table: str, name: str, item_id: int | None = None) -> bool:
    """同名项目会把同一个习惯的历史拆到两行上，所以落笔时就拦住。"""
    sql = f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE"
    params: list[Any] = [name]
    if item_id is not None:
        sql += " AND id <> ?"
        params.append(item_id)
    return conn.execute(sql, params).fetchone() is not None


def scope_children(conn: sqlite3.Connection, account_ids: list[int]) -> list[dict[str, Any]]:
    if not account_ids:
        return []
    marks = ",".join("?" * len(account_ids))
    rows = conn.execute(
        f"SELECT id, display_name, username, avatar FROM accounts WHERE id IN ({marks}) ORDER BY id",
        account_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def daily_series(conn: sqlite3.Connection, account_id: int, dates: list[str]) -> dict[str, list[int]]:
    rows = conn.execute(
        """
        SELECT date,
               SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS gained,
               SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS lost
        FROM records WHERE account_id = ? AND date >= ? AND date <= ?
        GROUP BY date
        """,
        (account_id, dates[0], dates[-1]),
    ).fetchall()
    by_date = {row["date"]: row for row in rows}
    opening = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM records WHERE account_id = ? AND date < ?",
            (account_id, dates[0]),
        ).fetchone()[0]
    )
    gained: list[int] = []
    lost: list[int] = []
    net: list[int] = []
    balance: list[int] = []
    running = opening
    for day in dates:
        row = by_date.get(day)
        day_gained = int(row["gained"]) if row else 0
        day_lost = int(row["lost"]) if row else 0
        gained.append(day_gained)
        lost.append(day_lost)
        net.append(day_gained - day_lost)
        running += day_gained - day_lost
        balance.append(running)
    return {"gained": gained, "lost": lost, "net": net, "balance": balance}


def flow_groups(conn: sqlite3.Connection, account_ids: list[int], start_date: str | None) -> dict[str, Any]:
    """按项目和方向给记录分组，供饼图和最近的收支列表使用。"""
    empty = {"projects": {"earn": [], "deduct": [], "reward": [], "other": []}, "gained": [], "lost": []}
    if not account_ids:
        return empty
    marks = ",".join("?" * len(account_ids))
    where = f"WHERE account_id IN ({marks})"
    params: list[Any] = list(account_ids)
    if start_date:
        where += " AND date >= ?"
        params.append(start_date)
    rows = conn.execute(
        f"SELECT account_id, title, amount, item_kind, item_id, source FROM records {where} ORDER BY date, id",
        params,
    ).fetchall()
    item_names = item_name_map(conn)
    entries: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        amount = int(row["amount"])
        if not amount:
            # 数量录入还没有分值，所以它们属于项目的进度，不进收支图。
            continue
        sign = 1 if amount > 0 else -1
        kind = row["item_kind"] or ""
        group = kind if kind in ITEM_TABLES else "other"
        label = record_label(row, item_names)
        entry = entries.setdefault(
            (group, label, sign),
            {"name": label, "total": 0, "count": 0, "children": {}},
        )
        entry["total"] += abs(amount)
        entry["count"] += 1
        child_key = str(int(row["account_id"]))
        child_stat = entry["children"].setdefault(child_key, {"count": 0, "total": 0})
        child_stat["count"] += 1
        child_stat["total"] += amount
    buckets: dict[str, list[dict[str, Any]]] = {"earn": [], "deduct": [], "reward": [], "other": []}
    gained: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    for (group, _label, sign), entry in entries.items():
        buckets[group].append(entry)
        (gained if sign > 0 else lost).append(entry)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (-item["total"], item["name"]))
    gained.sort(key=lambda item: (-item["total"], item["name"]))
    lost.sort(key=lambda item: (-item["total"], item["name"]))
    return {"projects": buckets, "gained": gained, "lost": lost}


def item_period_stats(conn: sqlite3.Connection, account_ids: list[int], unit: str, period_count: int) -> dict[str, Any]:
    """近 N 个窗口里，每个孩子 × 加/扣分项目一行：记录了多少天，并把积分拆成完成与结算两部分。"""
    current = period_start(unit, current_date())
    periods = []
    start = period_shift(unit, 1, current, 1 - period_count)
    while start <= current:
        periods.append({"start": start, "end": period_end(unit, start), "label": period_label(unit, start)})
        start = period_shift(unit, 1, start, 1)
    window_of = {entry["start"]: position for position, entry in enumerate(periods)}
    other: dict[str, int] = {"count": 0, "gained": 0, "lost": 0}
    result: dict[str, Any] = {"unit": unit, "periods": periods, "items": {"earn": [], "deduct": []}, "other": other}
    if not account_ids:
        return result
    marks = ",".join("?" * len(account_ids))
    where = f"account_id IN ({marks}) AND date >= ? AND date <= ?"
    params: list[Any] = [*account_ids, periods[0]["start"], periods[-1]["end"]]
    items_of: dict[str, list[dict[str, Any]]] = {}
    for kind in ("earn", "deduct"):
        # 只有加分项目带数量口径；扣分项目一律按次数统计。
        measure = "target_measure" if kind == "earn" else "NULL AS target_measure"
        items_of[kind] = [
            dict(row) for row in conn.execute(f"SELECT id, name, {measure} FROM {ITEM_TABLES[kind]}").fetchall()
        ]
    library: dict[tuple[int, str], dict[int, dict[str, Any]]] = {}
    for raw_scope_id in account_ids:
        scope_id = int(raw_scope_id)
        overrides = rule_overrides(conn, scope_id)
        for kind, rows in items_of.items():
            library[(scope_id, kind)] = {
                int(row["id"]): apply_rule_override(dict(row), overrides.get((kind, int(row["id"]))))
                for row in rows
            }
    entries: dict[tuple[str, int, int], dict[str, Any]] = {}

    def tally_for(kind: str, row: sqlite3.Row) -> dict[str, int] | None:
        position = window_of.get(period_start(unit, row["date"]))
        if position is None:
            return None
        account_id = int(row["account_id"])
        item_id = int(row["item_id"])
        key = (kind, account_id, item_id)
        entry = entries.get(key)
        if entry is None:
            item = library[(account_id, kind)].get(item_id)
            entry = entries[key] = {
                "kind": kind,
                "account_id": account_id,
                "item_id": item_id,
                "name": item["name"] if item else str(row["title"]),
                "measure": "quantity" if item and str(item["target_measure"] or "count") == "quantity" else "count",
                "cells": [None] * len(periods),
            }
        tally = entry["cells"][position]
        if tally is None:
            tally = entry["cells"][position] = {"count": 0, "quantity": 0, "coins": 0, "payout": 0, "detail": []}
        return tally

    for kind in ("earn", "deduct"):
        for row in conn.execute(
            f"SELECT account_id, item_id, title, date, amount, quantity FROM records WHERE {COMPLETION_FILTERS[kind]} AND {where}",
            params,
        ).fetchall():
            tally = tally_for(kind, row)
            if tally is None:
                continue
            tally["count"] += 1
            tally["quantity"] += int(row["quantity"] or 0)
            tally["coins"] += int(row["amount"])
    # 结算记录挂在同一个项目上，所以它们的积分单独占一列，不掺进完成所得里。
    for row in conn.execute(
        f"""
        SELECT account_id, item_kind, item_id, title, date, amount, source FROM records
        WHERE {where} AND item_kind IN ('earn', 'deduct') AND item_id IS NOT NULL AND {SETTLEMENT_SOURCE_PREDICATE}
        """,
        params,
    ).fetchall():
        tally = tally_for(row["item_kind"], row)
        if tally is None:
            continue
        tally["payout"] += int(row["amount"])
        tally["detail"].append({"title": str(row["title"]), "amount": int(row["amount"]), "source": str(row["source"])})

    for row in conn.execute(
        f"""
        SELECT amount FROM records
        WHERE {where} AND (item_kind IS NULL OR item_kind NOT IN ('earn', 'deduct') OR item_id IS NULL)
        """,
        params,
    ).fetchall():
        amount = int(row["amount"])
        other["count"] += 1
        other["gained"] += amount if amount > 0 else 0
        other["lost"] += -amount if amount < 0 else 0

    def rows_of(kind: str) -> list[dict[str, Any]]:
        found = []
        for entry in entries.values():
            if entry["kind"] != kind:
                continue
            totals = {key: 0 for key in ("count", "quantity", "coins", "payout")}
            for tally in entry["cells"]:
                if not tally:
                    continue
                for key in totals:
                    totals[key] += tally[key]
            found.append({**entry, "total": totals})
        # 先按孩子排，再按分值排：家长是一张表按孩子逐个读的。
        found.sort(
            key=lambda item: (
                item["account_id"],
                -abs(item["total"]["coins"] + item["total"]["payout"]),
                -item["total"]["count"],
                item["name"],
            )
        )
        return found

    result["items"] = {"earn": rows_of("earn"), "deduct": rows_of("deduct")}
    return result


def recent_flow_payload(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    start_date = (date.fromisoformat(current_date()) - timedelta(days=RECENT_WINDOW_DAYS - 1)).isoformat()
    flows = flow_groups(conn, [account_id], start_date)
    rows = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS gained,
               COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS lost
        FROM records WHERE account_id = ? AND date >= ?
        """,
        (account_id, start_date),
    ).fetchone()
    return {
        "gained": int(rows["gained"]),
        "lost": int(rows["lost"]),
        "net": int(rows["gained"]) - int(rows["lost"]),
        "top_gained": flows["gained"][:5],
        "top_lost": flows["lost"][:5],
    }


def analytics_payload(conn: sqlite3.Connection, account_ids: list[int], days: int, unit: str, period_count: int) -> dict[str, Any]:
    today = date.fromisoformat(current_date())
    dates = [(today - timedelta(days=days - 1 - index)).isoformat() for index in range(days)]
    start_date = dates[0]
    children = scope_children(conn, account_ids)
    series: list[dict[str, Any]] = []
    aggregate = {"gained": [0] * days, "lost": [0] * days, "net": [0] * days, "balance": [0] * days}
    for child in children:
        daily = daily_series(conn, int(child["id"]), dates)
        for key in aggregate:
            for index, value in enumerate(daily[key]):
                aggregate[key][index] += value
        series.append(
            {
                "account_id": int(child["id"]),
                "name": child["display_name"],
                "avatar": child["avatar"],
                **daily,
                "total": {
                    "gained": sum(daily["gained"]),
                    "lost": sum(daily["lost"]),
                    "net": sum(daily["net"]),
                    "balance": daily["balance"][-1],
                },
            }
        )
    flows = flow_groups(conn, account_ids, start_date)
    pending_counts = {
        int(row["account_id"]): int(row["count"])
        for row in conn.execute(
            "SELECT account_id, COUNT(*) AS count FROM point_requests WHERE status = 'pending' GROUP BY account_id"
        ).fetchall()
    }
    comparison = [
        {
            "account_id": item["account_id"],
            "name": item["name"],
            "avatar": item["avatar"],
            "gained": item["total"]["gained"],
            "lost": item["total"]["lost"],
            "net": item["total"]["net"],
            "balance": item["total"]["balance"],
            "pending": pending_counts.get(item["account_id"], 0),
        }
        for item in series
    ]
    return {
        "days": days,
        "dates": dates,
        "children": children,
        "series": series,
        "aggregate": aggregate,
        "projects": flows["projects"],
        "item_periods": item_period_stats(conn, account_ids, unit, period_count),
        "comparison": comparison,
        "summary": {
            "gained": sum(aggregate["gained"]),
            "lost": sum(aggregate["lost"]),
            "net": sum(aggregate["net"]),
            "balance": sum(item["balance"] for item in comparison),
        },
    }


XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
QUOTE_ATTRS = {'"': "&quot;"}
# escape() 只处理尖括号那类字符；XML 1.0 另外禁止下面这些控制码，而粘贴进来的项目名可能带着它们。
XML_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# 目标表用到的样式槽位：0 默认、1 大标题、2 表头单元格、3 项目名单元格、4 带边框的格子。
XLSX_STYLES = (
    f'{XML_HEADER}<styleSheet xmlns="{SHEET_NS}">'
    '<fonts count="3">'
    '<font><sz val="11"/><color theme="1"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="12"/><color theme="1"/><name val="Calibri"/></font>'
    '<font><b/><sz val="10"/><color rgb="FF55655C"/><name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="3">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE7F1EA"/><bgColor indexed="64"/></patternFill></fill>'
    "</fills>"
    '<borders count="2">'
    "<border><left/><right/><top/><bottom/><diagonal/></border>"
    '<border><left style="thin"><color rgb="FFA8BCB2"/></left><right style="thin"><color rgb="FFA8BCB2"/></right>'
    '<top style="thin"><color rgb="FFA8BCB2"/></top><bottom style="thin"><color rgb="FFA8BCB2"/></bottom><diagonal/></border>'
    "</borders>"
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="5">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
    '<alignment horizontal="center" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
    '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
    '<alignment horizontal="left" vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
    '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def xlsx_column(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def xlsx_text_cell(ref: str, text: str, style: int) -> str:
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(XML_CONTROL_CHARS.sub("", text))}</t></is></c>'


XLSX_CELL_PADDING = 1.5  # 边框和光标占的宽度，Excel 会把这些留在文字之外
# 下限保证格子还能手写，上限防止一个过长的项目名把整张表拉宽。
XLSX_NAME_BOUNDS = (18.0, 34.0)
XLSX_GOAL_BOUNDS = (9.0, 16.0)
XLSX_BOX_BOUNDS = (6.0, 8.0)
XLSX_BODY_HEIGHT_BASE = 26.0
XLSX_BODY_LINE_HEIGHT = 15.0


def xlsx_display_width(text: str) -> float:
    """Excel 按默认字体的「0」字宽量列宽，所以中日韩、谚文和全角标点都算两个字。"""
    return sum(2 if 0x2E80 <= ord(ch) <= 0xFF60 else 1 for ch in text)


def xlsx_auto_width(texts: list[str], bounds: tuple[float, float]) -> float:
    widest = max((xlsx_display_width(text) for text in texts), default=0) + XLSX_CELL_PADDING
    return round(min(max(widest, bounds[0]), bounds[1]), 1)


def xlsx_line_count(text: str, width: float) -> int:
    return max(1, math.ceil(xlsx_display_width(text) / max(width - XLSX_CELL_PADDING, 1)))


def xlsx_worksheet(title: str, heads: list[list[str]], rows: list[list[str]]) -> str:
    """合并的大标题带、冻结的两行表头，然后每一期一个带边框的格子，供手写填写。"""
    columns = len(heads[0])
    last_column = xlsx_column(columns)
    body_start = len(heads) + 2
    name_width = xlsx_auto_width([row[0] for row in rows], XLSX_NAME_BOUNDS)
    goal_width = xlsx_auto_width([row[1] for row in rows], XLSX_GOAL_BOUNDS)
    box_width = xlsx_auto_width([cell for head in heads for cell in head[2:]], XLSX_BOX_BOUNDS)
    lines = [f'<row r="1" ht="22" customHeight="1">{xlsx_text_cell("A1", title, 1)}</row>']
    for index, head in enumerate(heads, start=2):
        cells = "".join(xlsx_text_cell(f"{xlsx_column(column)}{index}", text, 2) for column, text in enumerate(head, start=1))
        lines.append(f'<row r="{index}" ht="16" customHeight="1">{cells}</row>')
    for offset, row in enumerate(rows):
        index = body_start + offset
        wrapped_lines = max(xlsx_line_count(row[0], name_width), xlsx_line_count(row[1], goal_width))
        height = XLSX_BODY_HEIGHT_BASE + XLSX_BODY_LINE_HEIGHT * (wrapped_lines - 1)
        boxes = "".join(f'<c r="{xlsx_column(column)}{index}" s="4"/>' for column in range(3, columns + 1))
        lines.append(
            f'<row r="{index}" ht="{height:g}" customHeight="1">'
            f'{xlsx_text_cell(f"A{index}", row[0], 3)}{xlsx_text_cell(f"B{index}", row[1], 4)}{boxes}</row>'
        )
    return (
        f"{XML_HEADER}<worksheet xmlns=\"{SHEET_NS}\">"
        '<sheetPr><tabColor rgb="FF24A37B"/><pageSetUpPr fitToPage="1"/></sheetPr>'
        f'<dimension ref="A1:{last_column}{body_start + len(rows) - 1}"/>'
        f'<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
        f'<pane xSplit="2" ySplit="{body_start - 1}" topLeftCell="C{body_start}" activePane="bottomRight" state="frozen"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<cols><col min="1" max="1" width="{name_width:g}" customWidth="1"/><col min="2" max="2" width="{goal_width:g}" customWidth="1"/>'
        f'<col min="3" max="{columns}" width="{box_width:g}" customWidth="1"/></cols>'
        f'<sheetData>{"".join(lines)}</sheetData>'
        f'<mergeCells count="1"><mergeCell ref="A1:{last_column}1"/></mergeCells>'
        '<printOptions horizontalCentered="1"/>'
        '<pageMargins left="0.4" right="0.4" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
        '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        "</worksheet>"
    )


def xlsx_tab_name(raw: str, index: int, used: set[str]) -> str:
    """Excel 在标签名里禁用若干字符、长度上限 31，并把只差大小写的两个名字当成同一个工作表。"""
    name = re.sub(r"[\\/*?:\[\]]", "_", XML_CONTROL_CHARS.sub("_", raw)).strip().strip(".")[:31] or f"孩子{index}"
    base, step = name, 1
    while name.lower() in used:
        # 计数后缀必须和它装饰的项目名一起塞进同样的 31 个字符里。
        name = f"{base[:29 - len(str(step))]}({step})"
        step += 1
    used.add(name.lower())
    return name


def xlsx_workbook(sheets: list[tuple[str, str]]) -> bytes:
    """Excel 需要的五个部件；单元格用内联字符串，这样就不必再维护一张同步的共享字符串表。"""
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    content_types = (
        f'{XML_HEADER}<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>'
    )
    root_rels = (
        f'{XML_HEADER}<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    book_rels = (
        f'{XML_HEADER}<Relationships xmlns="{PKG_REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="{REL_NS}/styles" Target="styles.xml"/></Relationships>'
    )
    workbook = (
        f'{XML_HEADER}<workbook xmlns="{SHEET_NS}" xmlns:r="{REL_NS}"><sheets>'
        + "".join(
            f'<sheet name="{escape(name, QUOTE_ATTRS)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (name, _) in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", book_rels)
        archive.writestr("xl/styles.xml", XLSX_STYLES)
        for index, (name, sheet_xml) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", sheet_xml)
    return buffer.getvalue()


def target_sheet_request(payload: dict[str, Any]) -> dict[str, Any]:
    """浏览器本来就在渲染这张表，所以导出复用它的话术；这里的上限只为约束工作簿体积。"""
    unit = str(payload.get("unit") or "").strip().lower()
    if unit not in TARGET_SHEET_UNITS:
        raise ValueError(f"导出周期只能选{unit_choice_text(TARGET_SHEET_UNITS)}")
    day = str(payload.get("date") or "").strip()
    try:
        date.fromisoformat(day)
    except ValueError:
        raise ValueError("导出日期无效")
    raw_heads = payload.get("heads")
    if not isinstance(raw_heads, list) or len(raw_heads) != 2:
        raise ValueError("导出表头需要两行")
    heads = [[str(cell)[:12] for cell in head[:TARGET_SHEET_MAX_COLUMNS]] for head in raw_heads if isinstance(head, list)]
    if len(heads) != 2 or len(heads[0]) < 3 or len(heads[1]) != len(heads[0]):
        raise ValueError("导出表头列数不符")
    sheets = []
    for entry in (payload.get("sheets") or [])[:TARGET_SHEET_MAX_CHILDREN]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:40]
        rows = []
        for row in (entry.get("rows") or [])[:TARGET_SHEET_MAX_ROWS]:
            if isinstance(row, list) and len(row) >= 2 and str(row[0]).strip():
                rows.append([str(row[0])[:40], str(row[1])[:24]])
        if name and rows:
            sheets.append({"name": name, "rows": rows})
    if not sheets:
        raise ValueError("没有可导出的项目")
    return {"unit": unit, "date": day, "range": str(payload.get("range") or "").strip()[:80], "heads": heads, "sheets": sheets}


def request_rows(conn: sqlite3.Connection, user: sqlite3.Row) -> list[dict[str, Any]]:
    if user["role"] == "admin":
        rows = conn.execute(
            """
            SELECT pr.*, a.display_name AS child_name, a.avatar AS child_avatar
            FROM point_requests pr
            JOIN accounts a ON a.id = pr.account_id
            ORDER BY CASE pr.status WHEN 'pending' THEN 0 ELSE 1 END, pr.id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT pr.*, a.display_name AS child_name, a.avatar AS child_avatar
            FROM point_requests pr
            JOIN accounts a ON a.id = pr.account_id
            WHERE pr.account_id = ?
            ORDER BY pr.id DESC
            """,
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


def account_overview_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    today = current_date()
    rows = conn.execute(
        """
        SELECT
            a.id, a.username, a.display_name, a.role, a.avatar, a.active, a.created_at,
            COALESCE((SELECT SUM(r.amount) FROM records r WHERE r.account_id = a.id), 0) AS total_points,
            COALESCE((SELECT SUM(r.amount) FROM records r WHERE r.account_id = a.id AND r.date = ?), 0) AS today_net,
            (SELECT COUNT(*) FROM point_requests pr WHERE pr.account_id = a.id AND pr.status = 'pending') AS pending_count
        FROM accounts a
        WHERE a.active = 1
        ORDER BY CASE a.role WHEN 'child' THEN 0 ELSE 1 END, a.id
        """,
        (today,),
    ).fetchall()
    return [dict(row) for row in rows]


def account_log_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, actor_id, actor_username, actor_name, actor_avatar, target_id, target_username,
               target_name, target_role, target_avatar, action, created_at
        FROM account_logs
        ORDER BY id DESC
        LIMIT 200
        """
    ).fetchall()
    return [dict(row) for row in rows]


def gamification_payload(conn: sqlite3.Connection, account_id: int | None) -> dict[str, Any]:
    if account_id is None:
        return {
            "coins": 0,
            "experience": 0,
            "level": 1,
            "next_level_exp": 100,
            "completed_tasks": 0,
            "unlocked_achievements": 0,
            "level_mode": "auto",
            "earn_bonus_percent": 0,
            "exchange_discount_percent": 0,
        }
    completed_tasks = completed_task_count(conn, account_id)
    experience = total_experience(conn, account_id)
    unlocked = int(
        conn.execute(
            "SELECT COUNT(*) FROM account_achievements WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
    )
    benefits = adventure_benefits(conn, account_id)
    level = benefits["level"]
    return {
        "coins": get_balance(conn, account_id),
        "experience": experience,
        "level": level,
        "next_level_exp": level * 100,
        "completed_tasks": completed_tasks,
        "unlocked_achievements": unlocked,
        "level_mode": "manual" if manual_adventure_level(conn, account_id) is not None else "auto",
        "earn_bonus_percent": benefits["earn_bonus_percent"],
        "exchange_discount_percent": benefits["exchange_discount_percent"],
    }


def achievement_rows(conn: sqlite3.Connection, account_id: int | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.achievement_key, a.title, a.description, a.icon,
               a.requirement_type, a.requirement_value,
               aa.unlocked_at
        FROM achievements a
        LEFT JOIN account_achievements aa
          ON aa.achievement_id = a.id AND aa.account_id = ?
        ORDER BY CASE WHEN aa.unlocked_at IS NULL THEN 1 ELSE 0 END, a.id
        """,
        (account_id or 0,),
    ).fetchall()
    return [
        {
            **dict(row),
            "unlocked": row["unlocked_at"] is not None,
        }
        for row in rows
    ]


def task_assignment_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "account_name": row["account_name"],
        "status": row["status"],
        "claim_date": row["claim_date"],
        "review_note": row["review_note"],
    }


def task_rows(conn: sqlite3.Connection, user: sqlite3.Row) -> list[dict[str, Any]]:
    tasks = conn.execute(
        """
        SELECT t.*
        FROM tasks t
        WHERE t.is_active = 1
        ORDER BY CASE t.task_type WHEN 'epic' THEN 0 ELSE 1 END, t.id DESC
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for task in tasks:
        assignments = conn.execute(
            """
            SELECT ta.*, a.display_name AS account_name
            FROM task_assignments ta
            JOIN accounts a ON a.id = ta.account_id
            WHERE ta.task_id = ?
            ORDER BY ta.id DESC
            """,
            (task["id"],),
        ).fetchall()
        own = next(
            (row for row in assignments if int(row["account_id"]) == int(user["id"])),
            None,
        )
        result.append(
            {
                "id": task["id"],
                "title": task["title"],
                "description": task["description"],
                "category": task["category"],
                "task_type": task["task_type"],
                "difficulty": task["difficulty"],
                "reward_coins": adjusted_earn_coins(conn, int(user["id"]), int(task["reward_coins"])) if user["role"] == "child" else task["reward_coins"],
                "base_reward_coins": task["reward_coins"],
                "reward_exp": task["reward_exp"],
                "icon": task["icon"],
                "due_date": task["due_date"],
                "participant_count": len(assignments),
                "assignments": [task_assignment_row(row) for row in assignments] if user["role"] == "admin" else [],
                "my_assignment": task_assignment_row(own) if own is not None else None,
            }
        )
    return result


def announcement_rows(conn: sqlite3.Connection, user: sqlite3.Row) -> list[dict[str, Any]]:
    if user["role"] == "admin":
        rows = conn.execute(
            "SELECT * FROM announcements WHERE is_active = 1 ORDER BY id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM announcements WHERE is_active = 1 AND audience IN ('all', 'children') ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def unlock_achievements(conn: sqlite3.Connection, account_id: int) -> None:
    completed_tasks = completed_task_count(conn, account_id)
    epic_tasks = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.account_id = ? AND ta.status = 'completed' AND t.task_type = 'epic'
            """,
            (account_id,),
        ).fetchone()[0]
    )
    earned_coins = int(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM records WHERE account_id = ? AND amount > 0",
            (account_id,),
        ).fetchone()[0]
    )
    daily_tasks = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            WHERE ta.account_id = ? AND ta.status = 'completed' AND t.task_type = 'daily'
            """,
            (account_id,),
        ).fetchone()[0]
    )
    progress = {
        "completed_tasks": completed_tasks,
        "epic_tasks": epic_tasks,
        "earned_coins": earned_coins,
        "daily_tasks": daily_tasks,
    }
    definitions = conn.execute("SELECT * FROM achievements").fetchall()
    now = now_iso()
    for achievement in definitions:
        if progress.get(achievement["requirement_type"], 0) < achievement["requirement_value"]:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO account_achievements(account_id, achievement_id, unlocked_at) VALUES (?, ?, ?)",
            (account_id, achievement["id"], now),
        )


def state_payload(conn: sqlite3.Connection, user: sqlite3.Row) -> dict[str, Any]:
    account_id = active_child_id(conn, user)
    children = conn.execute("SELECT * FROM accounts WHERE role = 'child' AND active = 1 ORDER BY id").fetchall()
    is_admin = user["role"] == "admin"
    child_ids = [int(row["id"]) for row in children]
    settle_ids = child_ids if is_admin else [account_id] if account_id else []
    settle_target_rules(conn, settle_ids, AUTO_SETTLE_UNITS)
    settle_streaks(conn, settle_ids, AUTO_SETTLE_UNITS)
    records = conn.execute(
        "SELECT id, title, amount, type, date, time, source, quantity, item_kind, item_id FROM records WHERE account_id = ? ORDER BY id DESC",
        (account_id,),
    ).fetchall() if account_id is not None else []
    earn_items = [
        visible_item(item, "earn", conn, user)
        for item in (item_rows(conn, "earn", account_id) if account_id else [])
    ]
    attach_target_stats(conn, earn_items, account_id, "earn")
    deduct_items = [
        visible_item(item, "deduct", conn, user)
        for item in (item_rows(conn, "deduct", account_id) if account_id else [])
    ]
    attach_target_stats(conn, deduct_items, account_id, "deduct")
    payload = {
        "version": APP_VERSION,
        "settings": settings_payload(conn, account_id),
        "user": safe_account(user),
        "children": [safe_account(row) for row in children],
        "active_child": safe_account(conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()) if account_id else None,
        "active_child_id": account_id,
        "total_points": get_balance(conn, account_id) if account_id else 0,
        "earn_items": earn_items,
        "deduct_items": deduct_items,
        "rewards": [visible_item(item, "reward", conn, user) for item in (item_rows(conn, "reward", account_id) if account_id else [])],
        "records": [dict(row) for row in records],
        "requests": request_rows(conn, user),
        "tasks": task_rows(conn, user),
        "announcements": announcement_rows(conn, user),
        "achievements": achievement_rows(conn, account_id),
        "gamification": gamification_payload(conn, account_id),
        "account_overview": account_overview_rows(conn) if is_admin else [],
        "account_logs": account_log_rows(conn) if is_admin else [],
        "item_library": {kind: item_rows(conn, kind) for kind in ITEM_TABLES} if is_admin else None,
        "item_bindings": binding_state(conn, child_ids) if is_admin else None,
        "item_rules": rule_override_state(conn, child_ids) if is_admin else None,
        "recent_flow": recent_flow_payload(conn, account_id) if account_id else None,
    }
    return payload


def api_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def find_custom_asset(asset_key: str) -> Path | None:
    """无论用户图片是平铺的还是上传工具嵌套过的，都能找到。"""
    if asset_key not in CUSTOM_ASSET_KEYS:
        return None
    custom_dir = RESOURCE_DIR / "static" / "custom"
    for entry in sorted(custom_dir.glob(f"{asset_key}.*"), key=lambda item: item.name.lower()):
        if entry.is_file() and entry.suffix.lower() in CUSTOM_IMAGE_SUFFIXES:
            return entry
        if entry.is_dir():
            nested = sorted(
                (child for child in entry.rglob("*") if child.is_file() and child.suffix.lower() in CUSTOM_IMAGE_SUFFIXES),
                key=lambda item: str(item).lower(),
            )
            if nested:
                return nested[0]
    return None


@app.after_request
def cache_shipped_images(response: Response) -> Response:
    # 随包发布的图片只在换图时变动；脚本和用户上传的资源必须每次重新校验，升级才不会留下过期界面。
    if response.status_code == 200 and request.path.startswith(("/static/illustrations/", "/static/avatars/")):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/custom-assets/<asset_key>")
def custom_asset(asset_key: str):
    asset = find_custom_asset(asset_key)
    if asset is None:
        abort(404)
    return send_file(asset)


@app.get("/")
def index():
    return render_template("index.html", app_version=APP_VERSION)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION})


def has_admin(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM accounts WHERE role = 'admin' AND active = 1 LIMIT 1").fetchone() is not None


def log_account_action(conn: sqlite3.Connection, actor: Any, target: Any, action: str) -> None:
    conn.execute(
        """
        INSERT INTO account_logs(
            actor_id, actor_username, actor_name, actor_avatar, target_id, target_username,
            target_name, target_role, target_avatar, action, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor["id"], actor["username"], actor["display_name"], actor["avatar"],
            target["id"], target["username"], target["display_name"], target["role"], target["avatar"],
            action, now_iso(),
        ),
    )


@app.get("/api/setup/status")
def setup_status():
    with connection() as conn:
        return jsonify({"configured": has_admin(conn)})


@app.post("/api/setup/admin")
def setup_admin():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    password_confirm = str(payload.get("password_confirm", ""))
    if not USERNAME_PATTERN.fullmatch(username):
        return api_error("管理员账号需为 3-32 位字母、数字、下划线、点或短横线")
    if len(password) < 6:
        return api_error("管理员密码至少需要 6 位")
    if password != password_confirm:
        return api_error("两次输入的密码不一致")
    with connection() as conn:
        if has_admin(conn):
            return api_error("管理员已经设置完成，请直接登录", 409)
        try:
            user = create_admin_account(conn, username, password)
        except sqlite3.IntegrityError:
            return api_error("账号名已存在")
        session.clear()
        session["user_id"] = int(user["id"])
        return jsonify(state_payload(conn, user)), 201


@app.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    with connection() as conn:
        user = conn.execute("SELECT * FROM accounts WHERE username = ? AND active = 1", (username,)).fetchone()
        if user is None or not password_matches(password, user["password_hash"]):
            return api_error("账号或密码错误", 401)
        session.clear()
        session["user_id"] = int(user["id"])
        if user["role"] == "admin":
            default_child = first_child_id(conn)
            if default_child is not None:
                session["selected_child_id"] = default_child
        else:
            session["selected_child_id"] = int(user["id"])
        return jsonify(state_payload(conn, user))


@app.post("/api/auth/register-child")
def register_child():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    display_name = str(payload.get("display_name", "")).strip()
    password = str(payload.get("password", ""))
    password_confirm = str(payload.get("password_confirm", ""))
    avatar = str(payload.get("avatar") or "boy")
    if not USERNAME_PATTERN.fullmatch(username):
        return api_error("孩子账号需为 3-32 位字母、数字、下划线、点或短横线")
    if len(password) < 6:
        return api_error("孩子密码至少需要 6 位")
    if password != password_confirm:
        return api_error("两次输入的密码不一致")
    if avatar not in CHILD_AVATARS:
        return api_error("孩子头像类型无效")
    if not display_name:
        display_name = username
    with connection() as conn:
        if not has_admin(conn):
            return api_error("请先完成管理员首次设置", 409)
        try:
            cursor = conn.execute(
                "INSERT INTO accounts(username, password_hash, display_name, role, avatar, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    username,
                    password_hash(password),
                    display_name,
                    "child",
                    avatar,
                    now_iso(),
                ),
            )
        except sqlite3.IntegrityError:
            return api_error("账号名已存在")
        user = conn.execute("SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
        log_account_action(conn, user, user, "register_child")
        session.clear()
        session["user_id"] = int(user["id"])
        session["selected_child_id"] = int(user["id"])
        return jsonify(state_payload(conn, user)), 201


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.put("/api/auth/password")
@require_user
def change_own_password():
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("password", ""))
    password_confirm = str(payload.get("password_confirm", ""))
    if len(new_password) < 6:
        return api_error("新密码至少需要 6 位")
    if new_password != password_confirm:
        return api_error("两次输入的新密码不一致")
    with connection() as conn:
        user = current_user(conn)
        if user["role"] != "child":
            return api_error("管理账号请在账号管理中修改密码", 403)
        if not password_matches(current_password, user["password_hash"]):
            return api_error("当前密码错误", 401)
        conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE id = ?",
            (password_hash(new_password), user["id"]),
        )
        log_account_action(conn, user, user, "change_password")
        return jsonify({"ok": True})


@app.get("/api/auth/me")
def auth_me():
    with connection() as conn:
        user = current_user(conn)
        if user is None:
            return jsonify({"authenticated": False}), 401
        return jsonify({"authenticated": True, "user": safe_account(user)})


@app.post("/api/auth/select-child")
@require_admin
def select_child():
    payload = request.get_json(silent=True) or {}
    try:
        child_id = int(payload.get("child_id"))
    except (TypeError, ValueError):
        return api_error("孩子账号无效")
    with connection() as conn:
        user = current_user(conn)
        child = conn.execute("SELECT * FROM accounts WHERE id = ? AND role = 'child' AND active = 1", (child_id,)).fetchone()
        if child is None:
            return api_error("孩子账号不存在", 404)
        session["selected_child_id"] = child_id
        return jsonify(state_payload(conn, user))


@app.get("/api/accounts")
@require_admin
def list_accounts():
    with connection() as conn:
        return jsonify({"accounts": [safe_account(row) for row in conn.execute("SELECT * FROM accounts ORDER BY role, id").fetchall()]})


@app.post("/api/accounts")
@require_admin
def create_account():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    display_name = str(payload.get("display_name", "")).strip()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "child")).lower()
    avatar = str(payload.get("avatar") or ("adult-male" if role == "admin" else "boy"))
    if not USERNAME_PATTERN.fullmatch(username):
        return api_error("账号需为 3-32 位字母、数字、下划线、点或短横线")
    if len(password) < 6:
        return api_error("密码至少需要 6 位")
    if role not in ("admin", "child"):
        return api_error("账号类型无效")
    if avatar not in avatar_choices(role):
        return api_error("头像类型无效")
    if not display_name:
        display_name = username
    with connection() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO accounts(username, password_hash, display_name, role, avatar, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (username, password_hash(password), display_name, role, avatar, now_iso()),
            )
        except sqlite3.IntegrityError:
            return api_error("账号名已存在")
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
        log_account_action(conn, current_user(conn), row, "create_child" if role == "child" else "create_admin")
        return jsonify(safe_account(row)), 201


CHILD_HISTORY_TABLES = ("records", "point_requests", "task_assignments", "account_achievements", "weekly_settlements")


def clear_child_history(conn: sqlite3.Connection, account_id: int) -> None:
    """清掉某个孩子的活动和等级，保留账号行和它的项目绑定。"""
    for table in CHILD_HISTORY_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM app_settings WHERE key = ?", (adventure_level_key(account_id),))


@app.delete("/api/accounts/<int:account_id>")
@require_admin
def delete_account(account_id: int):
    with connection() as conn:
        user = current_user(conn)
        target = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if target is None:
            return api_error("账号不存在", 404)
        if int(user["id"]) == account_id:
            return api_error("不能删除当前登录账号")
        if target["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM accounts WHERE role = 'admin' AND active = 1").fetchone()[0] <= 1:
            return api_error("至少需要保留一个管理员账号")
        log_account_action(conn, user, target, "delete_child" if target["role"] == "child" else "delete_admin")
        clear_child_history(conn, account_id)
        conn.execute("DELETE FROM item_bindings WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM item_rule_overrides WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        if session.get("selected_child_id") == account_id:
            session.pop("selected_child_id", None)
        return jsonify({"deleted": True})


@app.route("/api/accounts/<int:account_id>", methods=["PUT", "PATCH"])
@require_admin
def update_account(account_id: int):
    payload = request.get_json(silent=True) or {}
    display_name = str(payload.get("display_name", "")).strip()
    password = str(payload.get("password", ""))
    avatar = str(payload.get("avatar", ""))
    if not display_name:
        return api_error("显示名称不能为空")
    if password and len(password) < 6:
        return api_error("密码至少需要 6 位")
    with connection() as conn:
        user = current_user(conn)
        target = conn.execute("SELECT * FROM accounts WHERE id = ? AND active = 1", (account_id,)).fetchone()
        if target is None:
            return api_error("账号不存在", 404)
        allowed_avatars = avatar_choices(target["role"])
        if not avatar:
            avatar = target["avatar"]
        if avatar not in allowed_avatars:
            return api_error("头像类型无效")
        assignments = ["display_name = ?", "avatar = ?"]
        values: list[Any] = [display_name, avatar]
        if password:
            assignments.append("password_hash = ?")
            values.append(password_hash(password))
        values.append(account_id)
        conn.execute(f"UPDATE accounts SET {', '.join(assignments)} WHERE id = ?", values)
        target = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        log_account_action(conn, user, target, "update_child" if target["role"] == "child" else "update_admin")
        return jsonify(safe_account(target))


@app.get("/api/state")
@require_user
def get_state():
    with connection() as conn:
        user = current_user(conn)
        return jsonify(state_payload(conn, user))


@app.route("/api/settings", methods=["PUT", "PATCH"])
@require_admin
def update_settings():
    payload = request.get_json(silent=True) or {}
    rate = None
    if "points_per_yuan" in payload:
        try:
            rate = positive_int(payload.get("points_per_yuan"), "换算比例")
        except ValueError as exc:
            return api_error(str(exc))
        if rate > 1_000_000:
            return api_error("换算比例不能超过 1000000")
    if rate is None and "adventure_level" not in payload:
        return api_error("没有需要保存的设置")
    with connection() as conn:
        user = current_user(conn)
        account_id = active_child_id(conn, user)
        if "adventure_level" in payload:
            requested_level = str(payload.get("adventure_level") or "").strip().lower()
            level_key = adventure_level_key(account_id) if account_id is not None else None
            if requested_level in ("", "auto"):
                if level_key:
                    conn.execute("DELETE FROM app_settings WHERE key = ?", (level_key,))
            else:
                try:
                    level = int(requested_level)
                except ValueError:
                    return api_error("冒险等级必须是 1 到 99 的整数，或选择自动计算")
                if not 1 <= level <= 99:
                    return api_error("冒险等级必须在 1 到 99 之间")
                if level_key is None:
                    return api_error("请先创建并选择孩子账号")
                conn.execute(
                    """
                    INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (level_key, str(level), now_iso()),
                )
        if rate is not None:
            now = now_iso()
            updated = conn.execute(
                "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'points_per_yuan'",
                (str(rate), now),
            )
            if updated.rowcount == 0:
                conn.execute(
                    "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    ("points_per_yuan", str(rate), now),
                )
        return jsonify(state_payload(conn, user))


def task_payload(payload: dict[str, Any], task: sqlite3.Row | None) -> dict[str, Any]:
    """悬赏任务的可编辑字段：新建缺省用默认值，编辑时没带上的列沿用库里那份。"""

    def fallback(key: str, default: Any) -> Any:
        return payload.get(key, task[key] if task is not None else default)

    due_date_value = str(payload.get("due_date") or "").strip()
    result = {
        "title": str(fallback("title", "")).strip()[:80],
        "description": str(fallback("description", "")).strip()[:240],
        "category": str(fallback("category", "生活")).strip()[:30] or "生活",
        "task_type": str(fallback("task_type", "daily")).lower(),
        "difficulty": str(fallback("difficulty", "normal")).lower(),
        "reward_coins": positive_int(fallback("reward_coins", None), "积分奖励"),
        "reward_exp": positive_int(fallback("reward_exp", None), "经验奖励"),
        "icon": str(payload.get("icon") or (task["icon"] if task is not None else ITEM_DEFAULT_ICONS["earn"])),
        "due_date": valid_date(due_date_value) if due_date_value else None,
    }
    if not result["title"]:
        raise ValueError("任务标题不能为空")
    if result["task_type"] not in TASK_TYPES:
        raise ValueError("任务类型无效")
    if result["difficulty"] not in TASK_DIFFICULTIES:
        raise ValueError("任务难度无效")
    if not valid_project_icon(result["icon"]):
        raise ValueError("任务图标无效")
    return result


@app.post("/api/tasks")
@require_admin
def create_task():
    payload = request.get_json(silent=True) or {}
    try:
        fields = task_payload(payload, None)
    except ValueError as exc:
        return api_error(str(exc))
    columns = ", ".join(TASK_COLUMNS)
    marks = ", ".join("?" for _ in TASK_COLUMNS)
    with connection() as conn:
        user = current_user(conn)
        cursor = conn.execute(
            f"INSERT INTO tasks({columns}, created_by, created_at) VALUES ({marks}, ?, ?)",
            (*[fields[column] for column in TASK_COLUMNS], user["id"], now_iso()),
        )
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify({"task": dict(task), "state": state_payload(conn, user)}), 201


@app.route("/api/tasks/<int:task_id>", methods=["PUT", "DELETE"])
@require_admin
def task_detail(task_id: int):
    payload = request.get_json(silent=True) or {}
    with connection() as conn:
        user = current_user(conn)
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND is_active = 1", (task_id,)).fetchone()
        if task is None:
            return api_error("任务不存在", 404)
        if request.method == "DELETE":
            # 下架而非物理删除：孩子已领取或已提交的进度要留在账上，只是不再出现在任务大厅。
            conn.execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
            return jsonify({"deleted": True, "state": state_payload(conn, user)})
        try:
            fields = task_payload(payload, task)
        except ValueError as exc:
            return api_error(str(exc))
        assignments = ", ".join(f"{column} = ?" for column in TASK_COLUMNS)
        values = [fields[column] for column in TASK_COLUMNS]
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values, task_id))
        updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return jsonify({"task": dict(updated), "state": state_payload(conn, user)})


@app.post("/api/tasks/<int:task_id>/claim")
@require_user
def claim_task(task_id: int):
    with connection() as conn:
        user = current_user(conn)
        if user["role"] != "child":
            return api_error("只有孩子账号可以领取任务", 403)
        task = conn.execute("SELECT * FROM tasks WHERE id = ? AND is_active = 1", (task_id,)).fetchone()
        if task is None:
            return api_error("任务不存在", 404)
        today = current_date()
        if task["due_date"] and task["due_date"] < today:
            return api_error("这个任务已经过期", 409)
        if task["task_type"] == "epic":
            existing = conn.execute(
                "SELECT 1 FROM task_assignments WHERE task_id = ? AND account_id = ? AND status IN ('claimed', 'submitted', 'completed')",
                (task_id, user["id"]),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT 1 FROM task_assignments WHERE task_id = ? AND account_id = ? AND claim_date = ? AND status IN ('claimed', 'submitted', 'completed')",
                (task_id, user["id"], today),
            ).fetchone()
        if existing is not None:
            return api_error("你已经领取过这个任务", 409)
        now = now_iso()
        try:
            cursor = conn.execute(
                """
                INSERT INTO task_assignments(task_id, account_id, status, claim_date, claimed_at)
                VALUES (?, ?, 'claimed', ?, ?)
                """,
                (task_id, user["id"], today, now),
            )
        except sqlite3.IntegrityError:
            # (任务, 孩子, 领取日) 是唯一键：同一天被退回的那行还占着它，重领只能走「重新提交」。
            return api_error("你已经领取过这个任务", 409)
        return jsonify({"assignment_id": cursor.lastrowid, **state_payload(conn, user)}), 201


@app.post("/api/task-assignments/<int:assignment_id>/submit")
@require_user
def submit_task(assignment_id: int):
    with connection() as conn:
        user = current_user(conn)
        assignment = conn.execute(
            "SELECT * FROM task_assignments WHERE id = ? AND account_id = ?",
            (assignment_id, user["id"]),
        ).fetchone()
        if user["role"] != "child" or assignment is None:
            return api_error("任务领取记录不存在", 404)
        if assignment["status"] not in ("claimed", "rejected"):
            return api_error("当前任务状态不能提交", 409)
        conn.execute(
            "UPDATE task_assignments SET status = 'submitted', submitted_at = ?, review_note = NULL WHERE id = ?",
            (now_iso(), assignment_id),
        )
        return jsonify(state_payload(conn, user))


@app.post("/api/task-assignments/<int:assignment_id>/approve")
@require_admin
def approve_task(assignment_id: int):
    with connection() as conn:
        user = current_user(conn)
        assignment = conn.execute(
            """
            SELECT ta.*, t.title, t.reward_coins, t.reward_exp, t.task_type,
                   a.display_name AS account_name
            FROM task_assignments ta
            JOIN tasks t ON t.id = ta.task_id
            JOIN accounts a ON a.id = ta.account_id
            WHERE ta.id = ?
            """,
            (assignment_id,),
        ).fetchone()
        if assignment is None:
            return api_error("待审核的任务提交不存在", 404)
        now = now_iso()
        # 先把状态抢下来再发钱：两次「通过」同时到达时，只有一条 UPDATE 能命中 submitted。
        reviewed = conn.execute(
            "UPDATE task_assignments SET status = 'completed', completed_at = ?, reviewer_id = ?, review_note = NULL WHERE id = ? AND status = 'submitted'",
            (now, user["id"], assignment_id),
        )
        if reviewed.rowcount != 1:
            return api_error("待审核的任务提交不存在", 404)
        reward_coins = adjusted_earn_coins(conn, int(assignment["account_id"]), int(assignment["reward_coins"]))
        conn.execute(
            """
            INSERT INTO records(account_id, title, amount, type, date, time, created_at, source)
            VALUES (?, ?, ?, 'income', ?, ?, ?, 'task')
            """,
            (
                assignment["account_id"],
                f"任务完成：{assignment['title']}",
                reward_coins,
                current_date(),
                current_time(),
                now,
            ),
        )
        unlock_achievements(conn, int(assignment["account_id"]))
        return jsonify(state_payload(conn, user))


@app.post("/api/task-assignments/<int:assignment_id>/reject")
@require_admin
def reject_task(assignment_id: int):
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason", "请完成任务后重新提交")).strip()[:200] or "请完成任务后重新提交"
    with connection() as conn:
        user = current_user(conn)
        reviewed = conn.execute(
            "UPDATE task_assignments SET status = 'rejected', reviewer_id = ?, review_note = ? WHERE id = ? AND status = 'submitted'",
            (user["id"], reason, assignment_id),
        )
        if reviewed.rowcount != 1:
            return api_error("待审核的任务提交不存在", 404)
        return jsonify(state_payload(conn, user))


@app.post("/api/announcements")
@require_admin
def create_announcement():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()[:80]
    content = str(payload.get("content", "")).strip()[:500]
    audience = str(payload.get("audience", "all")).lower()
    if not title or not content:
        return api_error("公告标题和内容不能为空")
    if audience not in {"all", "children"}:
        return api_error("公告范围无效")
    with connection() as conn:
        user = current_user(conn)
        now = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO announcements(title, content, audience, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, content, audience, user["id"], now, now),
        )
        return jsonify({"announcement_id": cursor.lastrowid, **state_payload(conn, user)}), 201


@app.route("/api/announcements/<int:announcement_id>", methods=["PUT", "DELETE"])
@require_admin
def announcement_detail(announcement_id: int):
    with connection() as conn:
        user = current_user(conn)
        existing = conn.execute(
            "SELECT * FROM announcements WHERE id = ? AND is_active = 1",
            (announcement_id,),
        ).fetchone()
        if existing is None:
            return api_error("公告不存在", 404)
        if request.method == "DELETE":
            conn.execute("UPDATE announcements SET is_active = 0 WHERE id = ?", (announcement_id,))
            return jsonify({"deleted": True, **state_payload(conn, user)})
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title", "")).strip()[:80]
        content = str(payload.get("content", "")).strip()[:500]
        audience = str(payload.get("audience", existing["audience"])).lower()
        if not title or not content:
            return api_error("公告标题和内容不能为空")
        if audience not in {"all", "children"}:
            return api_error("公告范围无效")
        conn.execute(
            "UPDATE announcements SET title = ?, content = ?, audience = ?, updated_at = ? WHERE id = ?",
            (title, content, audience, now_iso(), announcement_id),
        )
        return jsonify(state_payload(conn, user))


def noted_title(note: str, name: str) -> str:
    """手写的记录把备注放在项目名旁边；结算记录的标题保持干净，因为它们的 source 已经说明了来由。"""
    return f"{name}（{note}）" if note else name


@app.post("/api/transactions")
@require_user
def create_transaction():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind", "")).lower()
    try:
        with connection() as conn:
            user = current_user(conn)
            if user["role"] == "child" and kind == "deduct":
                return api_error("孩子账号不能直接扣取积分，请使用兑换奖励", 403)
            if kind == "cash_exchange" and user["role"] != "child":
                return api_error("只有孩子账号可以兑换现金", 403)
            account_id = active_child_id(conn, user)
            if account_id is None:
                return api_error("当前没有可用的孩子账号")
            name = str(payload.get("name", "")).strip()
            points_value = payload.get("points")
            transaction_type = "income"
            item_kind = None
            quantity = 0
            raw_item_id = payload.get("item_id")
            if raw_item_id is None:
                raw_item_id = payload.get("reward_id")
            item_id = positive_int(raw_item_id, "项目") if raw_item_id not in (None, "") else None
            if kind == "earn":
                item = load_bound_item(conn, "earn", item_id, account_id, "赚取项目")
                if item is not None:
                    name, points_value = noted_title(name, item["name"]), item["points"]
                    item_kind = "earn"
                if item is not None and is_quantity(item):
                    # 数量口径的项目只记录做了多少；分值要等窗口结算才到账。
                    quantity = positive_int(payload.get("quantity") or 0, "数量")
                    amount = 0
                else:
                    points = positive_int(points_value)
                    amount = adjusted_earn_coins(conn, account_id, points) if user["role"] == "child" else points
            elif kind == "deduct":
                item = load_bound_item(conn, "deduct", item_id, account_id, "扣取项目")
                if item is not None:
                    name, points_value = noted_title(name, item["name"]), item["points"]
                    item_kind = "deduct"
                points = positive_int(points_value)
                amount = -points
                transaction_type = "expense"
            elif kind == "exchange":
                item = load_bound_item(conn, "reward", item_id, account_id, "兑换奖励")
                if item is not None:
                    name, points_value = noted_title(name, item["name"]), item["points"]
                    item_kind = "reward"
                points = positive_int(points_value)
                charged_points = adjusted_exchange_coins(conn, account_id, points) if user["role"] == "child" else points
                if get_balance(conn, account_id) < charged_points:
                    return api_error("积分不足", 409)
                amount = -charged_points
                transaction_type = "expense"
            elif kind == "cash_exchange":
                points = positive_int(points_value, "兑换积分")
                charged_points = adjusted_exchange_coins(conn, account_id, points) if user["role"] == "child" else points
                if get_balance(conn, account_id) < charged_points:
                    return api_error("积分不足", 409)
                amount = -charged_points
                name = f"兑换现金 ¥{points / points_per_yuan(conn):.2f}"
                transaction_type = "expense"
            elif kind == "manual":
                manual_kind = str(payload.get("item_kind", "")).lower()
                if manual_kind in ("earn", "deduct"):
                    item = load_bound_item(conn, manual_kind, item_id, account_id, "补录项目")
                    if item is None:
                        raise ValueError("请选择补录项目")
                    item_kind = manual_kind
                    name = f"补录：{noted_title(name, item['name'])}"
                    if manual_kind == "earn" and is_quantity(item):
                        quantity = positive_int(payload.get("quantity"), "数量")
                        amount = 0
                    else:
                        amount = int(item["points"]) if manual_kind == "earn" else -int(item["points"])
                        transaction_type = "income" if amount > 0 else "expense"
                else:
                    try:
                        amount = int(points_value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("积分数值必须是整数") from exc
                    if amount == 0:
                        raise ValueError("积分数值不能为 0")
                    name = f"补积分：{name}" if name else "补积分"
                    transaction_type = "income" if amount > 0 else "expense"
            else:
                return api_error("不支持的交易类型")
            transaction_date = valid_date(payload.get("date"))
            if transaction_date > current_date():
                return api_error("发生日期不能晚于今天")
            transaction_time = str(payload.get("time") or current_time())[:5]
            if not name:
                name = transaction_date
            if user["role"] == "child" and amount < 0 and kind not in ("exchange", "cash_exchange"):
                return api_error("孩子账号不能扣除积分", 403)
            if item_kind == "earn":
                ensure_submit_cap(conn, item_id, account_id, transaction_date)
            source = kind
            if user["role"] == "child" and kind in ("earn", "exchange", "manual", "cash_exchange"):
                cursor = conn.execute(
                    """
                    INSERT INTO point_requests(
                        account_id, requester_id, kind, title, amount, type, date, time, created_at,
                        item_kind, item_id, source, quantity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        user["id"],
                        kind,
                        name,
                        amount,
                        transaction_type,
                        transaction_date,
                        transaction_time,
                        now_iso(),
                        item_kind,
                        item_id if item_kind else None,
                        source,
                        quantity,
                    ),
                )
                return jsonify({"request_submitted": True, "request_id": cursor.lastrowid, **state_payload(conn, user)}), 202
            cursor = conn.execute(
                """
                INSERT INTO records(account_id, title, amount, type, date, time, created_at, item_kind, item_id, source, quantity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    name,
                    amount,
                    transaction_type,
                    transaction_date,
                    transaction_time,
                    now_iso(),
                    item_kind,
                    item_id if item_kind else None,
                    source,
                    quantity,
                ),
            )
            # 在下面这串应答之前先重算，让应答里的角标和发放已经包含这条记录。
            regrade_after_log(conn, account_id, item_kind, item_id, transaction_date)
            return jsonify({"record_id": cursor.lastrowid, **state_payload(conn, user)}), 201
    except (TypeError, ValueError) as exc:
        return api_error(str(exc))


@app.post("/api/requests/<int:request_id>/approve")
@require_admin
def approve_request(request_id: int):
    with connection() as conn:
        user = current_user(conn)
        point_request = conn.execute(
            "SELECT * FROM point_requests WHERE id = ? AND status = 'pending'", (request_id,)
        ).fetchone()
        if point_request is None:
            return api_error("待审核申请不存在或已经处理", 404)
        if point_request["amount"] < 0 and get_balance(conn, point_request["account_id"]) < abs(point_request["amount"]):
            return api_error("当前积分不足，无法通过兑换申请", 409)
        if point_request["item_kind"] == "earn" and point_request["item_id"]:
            try:
                ensure_submit_cap(
                    conn,
                    int(point_request["item_id"]),
                    int(point_request["account_id"]),
                    point_request["date"],
                    request_id,
                )
            except ValueError as exc:
                return api_error(str(exc))
        # 先占住申请再落账，同一笔申请才不会因为两个管理员同时点「通过」发两次积分。
        reviewed = conn.execute(
            "UPDATE point_requests SET status = 'approved', reviewed_at = ?, reviewer_id = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), user["id"], request_id),
        )
        if reviewed.rowcount != 1:
            return api_error("待审核申请不存在或已经处理", 404)
        conn.execute(
            """
            INSERT INTO records(account_id, title, amount, type, date, time, created_at, item_kind, item_id, source, quantity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                point_request["account_id"],
                point_request["title"],
                point_request["amount"],
                point_request["type"],
                point_request["date"],
                point_request["time"],
                now_iso(),
                point_request["item_kind"],
                point_request["item_id"],
                point_request["source"] or point_request["kind"],
                point_request["quantity"] or 0,
            ),
        )
        # 审批通过的记录可能落在一个已经闭合的窗口里，所以它和别的记录一样重算自己的项目。
        regrade_after_log(
            conn,
            int(point_request["account_id"]),
            point_request["item_kind"],
            point_request["item_id"],
            point_request["date"],
        )
        return jsonify(state_payload(conn, user))


@app.post("/api/requests/<int:request_id>/reject")
@require_admin
def reject_request(request_id: int):
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason", "管理员拒绝了这条申请")).strip()[:200]
    with connection() as conn:
        user = current_user(conn)
        reviewed = conn.execute(
            "UPDATE point_requests SET status = 'rejected', reviewed_at = ?, reviewer_id = ?, reject_reason = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), user["id"], reason or "管理员拒绝了这条申请", request_id),
        )
        if reviewed.rowcount != 1:
            return api_error("待审核申请不存在或已经处理", 404)
        return jsonify(state_payload(conn, user))


@app.post("/api/transactions/<int:record_id>/undo")
@require_user
def undo_transaction(record_id: int):
    with connection() as conn:
        user = current_user(conn)
        if user["role"] == "child":
            return api_error("孩子账号不能撤销积分记录", 403)
        account_id = active_child_id(conn, user)
        removed = conn.execute(
            "SELECT source, item_kind, item_id, date FROM records WHERE id = ? AND account_id = ?",
            (record_id, account_id),
        ).fetchone()
        if removed is None:
            return api_error("积分记录不存在", 404)
        if removed["source"] in STREAK_SOURCES:
            return api_error("连续奖惩记录不能单独撤销，请用首页的重新核算调整", 409)
        conn.execute("DELETE FROM records WHERE id = ? AND account_id = ?", (record_id, account_id))
        # 撤掉结算发的那笔不重算——重算会照水位原样补回；撤掉普通发放才要重算它所在的窗口。
        if removed["source"] not in AUTO_SETTLEMENT_SOURCES:
            regrade_after_log(conn, account_id, removed["item_kind"], removed["item_id"], removed["date"])
        return jsonify(state_payload(conn, user))


@app.post("/api/items/<kind>")
@require_admin
def create_item(kind: str):
    if kind not in ITEM_TABLES:
        return api_error("不支持的项目类型", 404)
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    icon = str(payload.get("icon") or ITEM_DEFAULT_ICONS[kind])
    try:
        points = positive_int(payload.get("points"))
        rule = item_rule_payload(kind, payload)
    except ValueError as exc:
        return api_error(str(exc))
    if not name:
        return api_error("项目名称不能为空")
    if not valid_project_icon(icon):
        return api_error("项目图片类型无效")
    with connection() as conn:
        table = ITEM_TABLES[kind]
        if has_duplicate_item_name(conn, table, name):
            return api_error("已有同名项目，请换个名称")
        columns = ["name", "points", "icon"]
        values: list[Any] = [name, points, icon]
        columns.append("sort_order")
        values.append(conn.execute(f"SELECT COALESCE(MAX(sort_order), 0) + 1 FROM {table}").fetchone()[0])
        if rule is not None:
            columns += list(rule)
            values += list(rule.values())
            columns.append("rule_set_at")
            values.append(now_iso() if rule_is_active(rule) else None)
        cursor = conn.execute(
            f"INSERT INTO {table}({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})",
            values,
        )
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_item(row, kind)), 201


@app.route("/api/items/<kind>/<int:item_id>", methods=["PUT", "DELETE"])
@require_admin
def item_detail(kind: str, item_id: int):
    if kind not in ITEM_TABLES:
        return api_error("不支持的项目类型", 404)
    table = ITEM_TABLES[kind]
    with connection() as conn:
        existing = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if existing is None:
            return api_error("项目不存在", 404)
        if request.method == "DELETE":
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
            conn.execute("DELETE FROM item_bindings WHERE item_kind = ? AND item_id = ?", (kind, item_id))
            conn.execute("DELETE FROM item_rule_overrides WHERE item_kind = ? AND item_id = ?", (kind, item_id))
            # 水位标记和发放记录都保留：删除项目不该改写已经判定过的历史。
            return jsonify({"deleted": True})
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        icon = str(payload.get("icon") or existing["icon"] or ITEM_DEFAULT_ICONS[kind])
        if not name:
            return api_error("项目名称不能为空")
        if not valid_project_icon(icon):
            return api_error("项目图片类型无效")
        try:
            points = positive_int(payload.get("points"))
            rule = item_rule_payload(kind, payload)
        except ValueError as exc:
            return api_error(str(exc))
        if has_duplicate_item_name(conn, table, name, item_id):
            return api_error("已有同名项目，请换个名称")
        if rule is None:
            conn.execute(f"UPDATE {table} SET name = ?, points = ?, icon = ? WHERE id = ?", (name, points, icon, item_id))
        else:
            # 改计分规则会重新锚定连续历史；只改提交上限不该抹掉往期。
            changed = any(existing[column] != value for column, value in rule.items() if column in RULE_ANCHOR_FIELDS)
            rule_set_at = existing["rule_set_at"]
            if changed or (rule_is_active(rule) and not rule_set_at):
                rule_set_at = now_iso()
            assignments = ", ".join(f"{column} = ?" for column in rule)
            conn.execute(
                f"UPDATE {table} SET name = ?, points = ?, icon = ?, {assignments}, rule_set_at = ? WHERE id = ?",
                [name, points, icon, *rule.values(), rule_set_at, item_id],
            )
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        return jsonify(row_item(row, kind))


@app.post("/api/items/<kind>/reorder")
@require_admin
def reorder_items_endpoint(kind: str):
    if kind not in ITEM_TABLES:
        return api_error("不支持的项目类型", 404)
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if not isinstance(ids, list):
        return api_error("需要项目顺序列表")
    try:
        ordered_ids = [int(value) for value in ids]
    except (TypeError, ValueError):
        return api_error("项目 id 无效")
    with connection() as conn:
        user = current_user(conn)
        reorder_items(conn, kind, ordered_ids)
        return jsonify(state_payload(conn, user))


@app.route("/api/item-bindings/<int:account_id>", methods=["PUT", "PATCH"])
@require_admin
def update_item_bindings(account_id: int):
    payload = request.get_json(silent=True) or {}
    with connection() as conn:
        user = current_user(conn)
        child = conn.execute("SELECT id FROM accounts WHERE id = ? AND role = 'child'", (account_id,)).fetchone()
        if child is None:
            return api_error("孩子账号不存在", 404)
        if str(payload.get("action") or "").lower() == "reset":
            conn.execute("DELETE FROM item_bindings WHERE account_id = ?", (account_id,))
            return jsonify(state_payload(conn, user))
        bindings = payload.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            return api_error("没有需要保存的绑定")
        # 先校验整批再动表：connection() 在返回时就提交，提前退出会留下半套绑定。
        validated = []
        for entry in bindings:
            kind = str(entry.get("kind") or "") if isinstance(entry, dict) else ""
            if kind not in ITEM_TABLES:
                return api_error("不支持的项目类型", 404)
            try:
                item_id = positive_int(entry.get("item_id"), "项目")
            except ValueError as exc:
                return api_error(str(exc))
            if conn.execute(f"SELECT 1 FROM {ITEM_TABLES[kind]} WHERE id = ?", (item_id,)).fetchone() is None:
                return api_error("项目不存在", 404)
            validated.append((kind, item_id, bool(entry.get("enabled", True))))
        for kind, item_id, enabled in validated:
            set_item_binding(conn, kind, item_id, account_id, enabled)
        return jsonify(state_payload(conn, user))


@app.route("/api/item-rules/<int:account_id>", methods=["PUT", "DELETE"])
@require_admin
def update_item_rule(account_id: int):
    """某个孩子在单个项目上的规则快照：直接写、抄另一个孩子的，或者退回库里的规则。"""
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "")
    if kind not in ("earn", "deduct"):
        return api_error("不支持的项目类型", 404)
    try:
        item_id = positive_int(payload.get("item_id"), "项目")
    except ValueError as exc:
        return api_error(str(exc))
    with connection() as conn:
        user = current_user(conn)
        if conn.execute("SELECT 1 FROM accounts WHERE id = ? AND role = 'child'", (account_id,)).fetchone() is None:
            return api_error("孩子账号不存在", 404)
        row = conn.execute(f"SELECT * FROM {ITEM_TABLES[kind]} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return api_error("项目不存在", 404)
        if request.method == "DELETE":
            conn.execute(
                "DELETE FROM item_rule_overrides WHERE item_kind = ? AND item_id = ? AND account_id = ?",
                (kind, item_id, account_id),
            )
            return jsonify(state_payload(conn, user))
        existing = conn.execute(
            "SELECT * FROM item_rule_overrides WHERE item_kind = ? AND item_id = ? AND account_id = ?",
            (kind, item_id, account_id),
        ).fetchone()
        body = payload
        if str(payload.get("action") or "").lower() == "copy":
            try:
                source_id = positive_int(payload.get("copy_from"), "来源账号")
            except ValueError as exc:
                return api_error(str(exc))
            if source_id == account_id:
                return api_error("来源不能是当前孩子")
            if conn.execute("SELECT 1 FROM accounts WHERE id = ? AND role = 'child'", (source_id,)).fetchone() is None:
                return api_error("来源孩子不存在", 404)
            # 源孩子的规则也走同一套覆盖合并再读出来，抄过来的才是他实际计分的口径。
            body = effective_item(conn, kind, row, source_id)
        try:
            rule = item_rule_payload(kind, body)
        except ValueError as exc:
            return api_error(str(exc))
        # 像库里的编辑那样锚定：内容没变的复制保留旧锚点，真改了才重新锚定，没有规则的行本来就没锚点可继承。
        base = existing if existing is not None else row
        changed = any(base[column] != value for column, value in rule.items() if column in RULE_ANCHOR_FIELDS)
        anchor = base["rule_set_at"] if base["rule_set_at"] and not changed else now_iso()
        columns = ", ".join(rule)
        conn.execute(
            f"""
            INSERT INTO item_rule_overrides(item_kind, item_id, account_id, {columns}, rule_set_at, updated_at)
            VALUES (?, ?, ?, {", ".join("?" * len(rule))}, ?, ?)
            ON CONFLICT(item_kind, item_id, account_id) DO UPDATE SET
                {", ".join(f"{column} = excluded.{column}" for column in rule)},
                rule_set_at = excluded.rule_set_at, updated_at = excluded.updated_at
            """,
            [kind, item_id, account_id, *rule.values(), anchor, now_iso()],
        )
        return jsonify(state_payload(conn, user))


@app.get("/api/analytics")
@require_user
def get_analytics():
    try:
        days = positive_int(request.args.get("days"), "统计天数")
        requested = request.args.get("account_id")
        scoped_account_id = positive_int(requested, "账号") if requested else None
        unit = str(request.args.get("unit") or ITEM_PERIOD_UNIT).strip().lower()
        if unit not in RULE_UNITS:
            raise ValueError(f"统计周期只能选{unit_choice_text(RULE_UNITS)}")
        period_count = min(positive_int(request.args.get("periods") or ITEM_PERIOD_COUNT, "统计期数"), ITEM_PERIOD_MAX)
    except ValueError as exc:
        return api_error(str(exc))
    days = min(days, ANALYTICS_MAX_DAYS)
    with connection() as conn:
        user = current_user(conn)
        if user["role"] == "child":
            account_ids = [int(user["id"])]
        elif scoped_account_id is not None:
            scoped = conn.execute(
                "SELECT id FROM accounts WHERE id = ? AND role = 'child' AND active = 1", (scoped_account_id,)
            ).fetchone()
            if scoped is None:
                return api_error("孩子账号不存在", 404)
            account_ids = [int(scoped["id"])]
        else:
            account_ids = child_account_ids(conn)
        return jsonify(analytics_payload(conn, account_ids, days, unit, period_count))


@app.post("/api/targets/export")
@require_admin
def export_target_sheet():
    """把管理员正在看的这张目标表变成工作簿；话术仍然由渲染它的那个页面持有。"""
    try:
        data = target_sheet_request(request.get_json(silent=True) or {})
    except ValueError as exc:
        return api_error(str(exc))
    used: set[str] = set()
    sheets = [
        (
            xlsx_tab_name(sheet["name"], index, used),
            xlsx_worksheet(f"{sheet['name']} · {data['range']}", data["heads"], sheet["rows"]),
        )
        for index, sheet in enumerate(data["sheets"], start=1)
    ]
    label = f"目标任务表_{data['date']}{RULE_UNIT_SHORT_TEXT[data['unit']]}.xlsx"
    fallback = f"targets_{data['date']}_{data['unit']}.xlsx"
    return Response(
        xlsx_workbook(sheets),
        mimetype=XLSX_MIME,
        headers={"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(label)}"},
    )


@app.get("/api/settlements/periods")
@require_admin
def list_settlement_periods():
    """手动结算栏针对某个单位列出的期次；最新那一期还没结束，不能判定。"""
    unit = str(request.args.get("unit") or "").strip().lower()
    if unit == SETTLE_UNIT_ALL:
        # 补结会横跨所有单位里未认领的窗口，所以它没有单一期次可以命名，也不能显示为已结算。
        periods = [{"start": "", "end": current_date(), "label": "所有未结算期次", "closed": True, "settled": False}]
        return jsonify({"unit": unit, "periods": periods})
    if unit not in MANUAL_SETTLE_UNITS:
        return api_error(f"结算周期只能选{unit_choice_text(MANUAL_SETTLE_UNITS)}或全部周期")
    with connection() as conn:
        settled = settled_window_starts(conn, unit)
    return jsonify({"unit": unit, "periods": settlement_periods(unit, current_date(), settled)})


@app.post("/api/settlements/run")
@require_admin
def run_settlement():
    """按需判定某个期次单位下所有已闭合的窗口；按天的规则仍然在每次页面加载时继续结算。"""
    payload = request.get_json(silent=True) or {}
    unit = str(payload.get("unit") or "").strip().lower()
    if unit != SETTLE_UNIT_ALL and unit not in MANUAL_SETTLE_UNITS:
        return api_error(f"结算周期只能选{unit_choice_text(MANUAL_SETTLE_UNITS)}或全部周期")
    period = str(payload.get("period") or "").strip()[:10]
    force = bool(payload.get("force"))
    if force and not period:
        return api_error("强制重算需要指定期次")
    if unit == SETTLE_UNIT_ALL and period:
        return api_error("全部周期只补结未结算期次，重算请逐档选择")
    if period:
        try:
            date.fromisoformat(period)
        except ValueError:
            return api_error("结算期次日期无效")
        if period_end(unit, period_start(unit, period)) >= current_date():
            return api_error(f"{period_label(unit, period_start(unit, period))}尚未结束，不能结算")
    units = RULE_UNITS if unit == SETTLE_UNIT_ALL else (unit,)
    with connection() as conn:
        user = current_user(conn)
        account_ids = child_account_ids(conn)
        newest = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM records").fetchone()["id"])
        if force:
            regrade_windows(conn, account_ids, unit, period)
        settle_target_rules(conn, account_ids, units, period)
        settle_streaks(conn, account_ids, units, period)
        written = conn.execute(
            f"""
            SELECT COUNT(*) AS entries, COALESCE(SUM(amount), 0) AS points FROM records
            WHERE id > ? AND source IN ({", ".join("?" * len(AUTO_SETTLEMENT_SOURCES))})
            """,
            (newest, *AUTO_SETTLEMENT_SOURCES),
        ).fetchone()
        result = state_payload(conn, user)
        result["settlement"] = {"unit": unit, "entries": int(written["entries"]), "points": int(written["points"])}
        return jsonify(result)


@app.post("/api/system/clear")
@require_admin
def clear_points():
    with connection() as conn:
        user = current_user(conn)
        account_id = active_child_id(conn, user)
        if account_id is None:
            return api_error("当前没有可用的孩子账号")
        clear_child_history(conn, account_id)
        return jsonify(state_payload(conn, user))


@app.post("/api/system/reset")
@require_admin
def reset_system():
    with connection() as conn:
        user = current_user(conn)
        account_id = active_child_id(conn, user)
        if account_id is None:
            return api_error("当前没有可用的孩子账号")
        clear_child_history(conn, account_id)
        conn.execute("DELETE FROM item_bindings WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM item_rule_overrides WHERE account_id = ?", (account_id,))
        return jsonify(state_payload(conn, user))


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return api_error("接口不存在", 404)
    return error


@app.errorhandler(413)
def payload_too_large(error):
    # Flask 在进视图之前就抛这个异常；没有下面的处理器，客户端会在 SPA 期望 JSON 的地方收到 HTML。
    return api_error("提交内容过大，请减少后重试", 413)


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT") or "9696"), debug=False)
