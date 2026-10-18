import asyncio
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import string
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler,
    ChatMemberHandler, CommandHandler, MessageHandler, filters, ContextTypes
)

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")

try:
    THREAD_ID = int((os.getenv("THREAD_ID") or "0").strip() or "0")
except ValueError:
    THREAD_ID = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mafia")
log.info("THREAD_ID mode: %s", THREAD_ID if THREAD_ID else "ALL THREADS")

DB_DIR = Path(os.getenv("DATA_DIR", "."))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "mafia.db")

_old_db = Path("mafia.db")
if _old_db.exists() and _old_db.absolute() != Path(DB_PATH).absolute():
    if not Path(DB_PATH).exists():
        shutil.copy2(_old_db, DB_PATH)
        for suffix in ("-wal", "-shm"):
            old_f = Path(str(_old_db) + suffix)
            if old_f.exists():
                shutil.copy2(old_f, str(DB_PATH) + suffix)

log.info("DB path: %s", DB_PATH)


def _init_db():
    with sqlite3.connect(DB_PATH, timeout=15.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activations (
                chat_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS pending_activations (
                chat_id INTEGER PRIMARY KEY,
                key TEXT NOT NULL
            );
        """)
        conn.commit()


def is_chat_activated(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute(
            "SELECT 1 FROM activations WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row is not None


def activate_chat(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO activations (chat_id) VALUES (?)", (chat_id,)
        )
        conn.commit()


def get_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute(
            "SELECT key FROM pending_activations WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return row[0] if row else None


def set_pending_key(chat_id, key):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_activations (chat_id, key) VALUES (?, ?)",
            (chat_id, key),
        )
        conn.commit()


def clear_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute(
            "DELETE FROM pending_activations WHERE chat_id=?", (chat_id,)
        )
        conn.commit()


def _gen_key():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _log_activation_key(chat_id, chat_title, key):
    bar = "=" * 60
    log.warning(bar)
    log.warning("  KEY ACTIVATION")
    log.warning("  Chat: %s (%s)", chat_title, chat_id)
    log.warning("  Key:  %s", key)
    log.warning("  Usage in chat: активация %s", key)
    log.warning(bar)
    try:
        print(bar, flush=True)
        print("  KEY ACTIVATION", flush=True)
        print(f"  Chat: {chat_title} ({chat_id})", flush=True)
        print(f"  Key:  {key}", flush=True)
        print(f"  Usage in chat: активация {key}", flush=True)
        print(bar, flush=True)
    except Exception:
        pass


_init_db()

ROLE_NAMES = {
    "mafia": "Тень", "cop": "Смотритель", "doctor": "Лекарь",
    "bartender": "Укрыватель", "whore": "Очаровательница",
    "medium": "Шептун", "citizen": "Житель",
}

ROLE_DESCRIPTIONS = {
    "mafia": "Ты — Тень. Ночью вместе с союзниками выбираешь жертву.",
    "cop": "Ты — Смотритель. Ночью проверяешь одного жителя — Тень он или нет.",
    "doctor": "Ты — Лекарь. Ночью лечишь одного жителя. Если его убьют этой ночью — он выживет.",
    "bartender": "Ты — Укрыватель. Ночью поишь одного жителя — он молчит 5 мин.",
    "whore": "Ты — Очаровательница. Ночью отвлекаешь одного жителя — он молчит 5 мин.",
    "medium": "Ты — Шептун. Ночью узнаёшь роль одного павшего жителя.",
    "citizen": "Ты — Житель. Днём вычисляй Теней и голосуй против них.",
}

ROLE_TEAM = {
    "mafia": "mafia", "cop": "city", "doctor": "city", "bartender": "city",
    "whore": "city", "medium": "city", "citizen": "city",
}

_ROLE_MAP = {"M": "mafia", "C": "cop", "D": "doctor", "B": "bartender",
             "W": "whore", "N": "medium", "G": "citizen"}

ROLE_DISTRIBUTION = {
    5:  "M C G G G",
    6:  "M C D G G G",
    7:  "M M C D G G G",
    8:  "M M C D B G G G",
    9:  "M M C D B W G G G",
    10: "M M M C D B W G G G",
    11: "M M M C D B W N G G G",
    12: "M M M C D B W N G G G G",
    13: "M M M M C D B W N G G G G",
    14: "M M M M C D B W N G G G G G",
    15: "M M M M C D B W N G G G G G G",
    16: "M M M M M C D B W N G G G G G G",
    17: "M M M M M C D B W N G G G G G G G",
    18: "M M M M M C D B W N G G G G G G G G",
    19: "M M M M M M C D B W N G G G G G G G G",
    20: "M M M M M M C D B W N G G G G G G G G G",
}

SHOP_ITEMS = {
    "amulet": {"name": "Оберег", "emoji": "🛡", "price": 30,
               "desc": "Защита от убийства Теней", "modes": ("now", "match")},
    "blind": {"name": "Отвод глаз", "emoji": "👁‍🗨", "price": 25,
              "desc": "Защита от голосования", "modes": ("now", "match")},
    "fake": {"name": "Фальшивка", "emoji": "📄", "price": 40,
             "desc": "Смотритель видит вас как жителя", "modes": ("now", "match")},
    "cookie": {"name": "Печенье с предсказанием", "emoji": "🍪", "price": 15,
               "desc": "Узнать роль случайного игрока", "modes": ("now",)},
}

JOIN_TIMEOUT_SEC = 300
NIGHT_ACTION_TIMEOUT = 60
DAY_DISCUSSION_SEC = 180
VOTE_DURATION_SEC = 60
MUTE_DURATION_SEC = 5 * 60

ACTIVATION_REGEX = re.compile(r"(?i)^(?:/)?(?:активация|activate)(?:\s+\S+)?$")
MAFIA_START_REGEX = re.compile(r"(?i)^мафия\s+(?:начать|старт)$")
MAFIA_STOP_REGEX = re.compile(r"(?i)^мафия\s+(?:стоп|stop)$")


def get_roles(count):
    spec = ROLE_DISTRIBUTION.get(count)
    if not spec:
        return None
    return [_ROLE_MAP[t] for t in spec.split()]


@dataclass
class Player:
    user_id: int
    username: str
    full_name: str
    role: str = "citizen"
    alive: bool = True
    muted_until: float = 0.0
    coins: int = 0
    items: dict = field(default_factory=dict)
    active_items: dict = field(default_factory=dict)
    achievements: set = field(default_factory=set)
    wins: int = 0
    games: int = 0
    bonus_claimed: bool = False

    def is_muted(self):
        return time.time() < self.muted_until

    def has_active(self, item_id):
        return item_id in self.active_items

    def consume_active(self, item_id):
        self.active_items.pop(item_id, None)


@dataclass
class Game:
    chat_id: int
    thread_id: int
    total: int
    players: dict = field(default_factory=dict)
    phase: str = "joining"
    round_num: int = 0
    join_message_id: Optional[int] = None
    night_actions: dict = field(default_factory=dict)
    votes: dict = field(default_factory=dict)
    dm_failed: set = field(default_factory=set)
    winner: Optional[str] = None
    task: Optional[asyncio.Task] = None
    start_message_id: Optional[int] = None

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

    def alive_mafia(self):
        return [p for p in self.alive_players() if p.role == "mafia"]

    def alive_city(self):
        return [p for p in self.alive_players() if ROLE_TEAM[p.role] == "city"]

    def all_mafia(self):
        return [p for p in self.players.values() if p.role == "mafia"]


GAMES = {}
USER_GAMES = {}


def _register_user_game(user_id, chat_id):
    USER_GAMES.setdefault(user_id, set()).add(chat_id)


def _unregister_user_game(user_id, chat_id):
    s = USER_GAMES.get(user_id)
    if s:
        s.discard(chat_id)
        if not s:
            USER_GAMES.pop(user_id, None)


def _unregister_all_in_chat(chat_id):
    for uid in list(USER_GAMES.keys()):
        _unregister_user_game(uid, chat_id)


def _check_thread(update):
    if THREAD_ID <= 0:
        return True
    msg = update.effective_message
    if msg is None:
        return False
    if msg.chat.type == "private":
        return True
    return (msg.message_thread_id or 0) == THREAD_ID


async def _is_chat_admin(bot, chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except Exception:
        return False


async def _safe_send(bot, user_id, text, **kwargs):
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except Exception as e:
        log.warning("send to %s failed: %s", user_id, e)
        return False


async def _send_to_chat(bot, game, text, **kwargs):
    if game.thread_id:
        kwargs["message_thread_id"] = game.thread_id
    return await bot.send_message(game.chat_id, text, **kwargs)


async def activation_mw(update, ctx):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    if is_chat_activated(msg.chat.id):
        return
    text = (msg.text or "").strip()
    if ACTIVATION_REGEX.match(text):
        return
    if text.lower().startswith("/threadid"):
        return
    raise ApplicationHandlerStop


async def on_bot_added(update, ctx):
    cm = update.my_chat_member
    if cm is None:
        return
    me = await ctx.bot.get_me()
    if cm.new_chat_member.user.id != me.id:
        return
    old_s = cm.old_chat_member.status
    new_s = cm.new_chat_member.status
    if not (old_s in ("left", "kicked")
            and new_s in ("member", "administrator", "restricted")):
        return
    if cm.chat.type == "private":
        return
    chat_id = cm.chat.id
    if is_chat_activated(chat_id):
        try:
            await ctx.bot.send_message(
                chat_id, "🍁 Этот чат уже активирован. Пиши «мафия начать»."
            )
        except Exception:
            pass
        return
    existing = get_pending_key(chat_id)
    if existing:
        return
    key = _gen_key()
    set_pending_key(chat_id, key)
    title = cm.chat.title or cm.chat.full_name or str(chat_id)
    _log_activation_key(chat_id, title, key)
    try:
        await ctx.bot.send_message(
            chat_id,
            "🍁 Спасибо за добавление!\n\n"
            "Чтобы активировать бота:\n"
            "1. Зайди в консоль хостинга — там будет ключ из 12 символов\n"
            "2. Отправь в чат: <code>активация КЛЮЧ</code>\n\n"
            "Без активации бот не реагирует ни на что.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


async def cmd_activate(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None or msg.chat.type == "private":
        return
    chat_id = msg.chat.id
    if is_chat_activated(chat_id):
        try:
            await msg.delete()
        except Exception:
            pass
        return
    text = (msg.text or "").strip()
    low = text.lower().lstrip("/")
    parts = low.split()
    if len(parts) < 2:
        is_admin = await _is_chat_admin(ctx.bot, chat_id, user.id)
        if not is_admin:
            try:
                await msg.delete()
            except Exception:
                pass
            return
        existing = get_pending_key(chat_id)
        if existing:
            key = existing
        else:
            key = _gen_key()
            set_pending_key(chat_id, key)
        title = msg.chat.title or msg.chat.full_name or str(chat_id)
        _log_activation_key(chat_id, title, key)
        try:
            await msg.delete()
        except Exception:
            pass
        return
    entered = parts[1].strip().upper()
    pending = get_pending_key(chat_id)
    if not pending:
        try:
            await msg.delete()
        except Exception:
            pass
        return
    if entered != pending.upper():
        try:
            await msg.delete()
        except Exception:
            pass
        return
    activate_chat(chat_id)
    clear_pending_key(chat_id)
    try:
        await msg.delete()
    except Exception:
        pass
    log.info("Чат %s (%s) активирован пользователем %s",
             chat_id, msg.chat.title or "?", user.id)
    note = await ctx.bot.send_message(
        chat_id,
        "✅ <b>Чат активирован!</b>\n\n"
        "🍁 <i>Кленовая мафия</i>\n"
        "Запуск: <code>мафия начать</code>\n"
        "ID ветки: <code>/threadid</code>",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(30)
    try:
        await note.delete()
    except Exception:
        pass


async def guard_muted_messages(update, ctx):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    user = update.effective_user
    if user is None or user.is_bot:
        return
    if not _check_thread(update):
        return
    game = GAMES.get(msg.chat.id)
    if game is None:
        return
    if (msg.message_thread_id or 0) != game.thread_id:
        return
    p = game.players.get(user.id)
    if p is None or not p.alive:
        return
    if not p.is_muted():
        return
    if await _is_chat_admin(ctx.bot, msg.chat.id, user.id):
        return
    try:
        await msg.delete()
    except Exception:
        pass
    raise ApplicationHandlerStop


def _menu_main_kb(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Профиль", callback_data=f"menu:profile:{chat_id}"),
         InlineKeyboardButton("🛒 Магазин", callback_data=f"menu:shop:{chat_id}")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data=f"menu:inv:{chat_id}"),
         InlineKeyboardButton("🏆 Топ", callback_data=f"menu:top:{chat_id}")],
        [InlineKeyboardButton("🍁 Бонус", callback_data=f"menu:bonus:{chat_id}")],
    ])


def _menu_back_kb(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")],
    ])


def _menu_text(p):
    return f"🍁 *Меню* {p.full_name}\n\nБаланс: *{p.coins}🍁*"


def _menu_open_kb(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 Меню", callback_data=f"openmenu:{chat_id}")],
    ])


def _make_count_kb():
    buttons = [InlineKeyboardButton(str(i), callback_data=f"ms:{i}")
               for i in range(5, 21)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    return InlineKeyboardMarkup(rows)


async def cmd_threadid(update, ctx):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private":
        return
    tid = msg.message_thread_id or 0
    note = await msg.reply_text(
        f"chat_id: <code>{msg.chat.id}</code>\nthread_id: <code>{tid}</code>",
        parse_mode=ParseMode.HTML,
    )
    await asyncio.sleep(30)
    try:
        await note.delete()
        await msg.delete()
    except Exception:
        pass


async def cmd_menu(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type == "private":
        return
    if not _check_thread(update):
        return
    game = GAMES.get(chat.id)
    if not game:
        await msg.reply_text("🍁 Меню доступно только во время игры.")
        return
    p = game.players.get(user.id)
    if not p:
        await msg.reply_text("🍁 Ты не в игре.")
        return
    _register_user_game(user.id, chat.id)
    try:
        await ctx.bot.send_message(
            user.id, _menu_text(p),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_main_kb(chat.id),
        )
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception:
        await msg.reply_text("⚠️ Сначала нажми /start в личке с ботом!")


async def cmd_dm_start(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    chat_ids = USER_GAMES.get(user.id, set())
    active = [cid for cid in chat_ids if cid in GAMES]
    if active:
        await _open_menu_for_user(ctx.bot, msg, user.id, active)
        return
    await msg.reply_text(
        "🍁 Это бот «Кленовая мафия».\n\n"
        "Как начать:\n"
        "• В группе попроси админа написать <code>мафия начать</code>\n"
        "• Нажми «🎮 Играть»\n"
        "• Роль и меню придут сюда\n\n"
        "Если ты уже в игре — /menu.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_dm_menu(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    chat_ids = USER_GAMES.get(user.id, set())
    active = [cid for cid in chat_ids if cid in GAMES]
    if not active:
        await msg.reply_text(
            "🍁 Ты не в активной игре.\n\n"
            "Зайди в чат, нажми «🎮 Играть» — и меню откроется здесь."
        )
        return
    await _open_menu_for_user(ctx.bot, msg, user.id, active)


async def _open_menu_for_user(bot, msg, user_id, chat_ids):
    if len(chat_ids) == 1:
        cid = chat_ids[0]
        game = GAMES.get(cid)
        p = game.players.get(user_id) if game else None
        if not p:
            await msg.reply_text("🍁 Ты не в этой игре.")
            return
        try:
            await bot.send_message(
                user_id, _menu_text(p),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_menu_main_kb(cid),
            )
        except Exception:
            await msg.reply_text("⚠️ Не смог открыть меню.")
        return
    rows = []
    for cid in chat_ids:
        game = GAMES.get(cid)
        if not game:
            continue
        p = game.players.get(user_id)
        if not p:
            continue
        rows.append([InlineKeyboardButton(
            f"🍁 Игра в чате {cid} ({ROLE_NAMES[p.role]})",
            callback_data=f"dmpick:{cid}")])
    if not rows:
        await msg.reply_text("🍁 Нет активных игр.")
        return
    await msg.reply_text(
        "🍁 Ты в нескольких играх. Выбери меню:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_dm_other(update, ctx):
    msg = update.effective_message
    if not msg:
        return
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    await msg.reply_text(
        "🍁 Управление кнопками.\n\n"
        "/menu — меню (если ты в игре)\n"
        "/start — инструкция",
    )


async def cmd_mafia_start(update, ctx):
    if not _check_thread(update):
        return
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type == "private":
        return
    if not await _is_chat_admin(ctx.bot, chat.id, user.id):
        await msg.reply_text("⛔ Только администратор чата.")
        return
    if chat.id in GAMES:
        await msg.reply_text("⚠️ В этом чате уже идёт игра.")
        return
    await msg.reply_text(
        "🍁 *Кленовая мафия* — выберите количество игроков (5–20):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_make_count_kb(),
    )


async def cmd_mafia_stop(update, ctx):
    if not _check_thread(update):
        return
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type == "private":
        return
    if not await _is_chat_admin(ctx.bot, chat.id, user.id):
        await msg.reply_text("⛔ Только администратор.")
        return
    game = GAMES.pop(chat.id, None)
    if game is None:
        await msg.reply_text("Нет активной игры.")
        return
    if game.task and not game.task.done():
        game.task.cancel()
    _unregister_all_in_chat(chat.id)
    await msg.reply_text("🛑 Игра остановлена.")


async def cb_start_count(update, ctx):
    q = update.callback_query
    if q is None or q.data is None or q.message is None:
        return
    if not _check_thread(update):
        await q.answer("Не та ветка", show_alert=True)
        return
    chat = update.effective_chat
    user = q.from_user
    if not await _is_chat_admin(ctx.bot, chat.id, user.id):
        await q.answer("Только администратор", show_alert=True)
        return
    if chat.id in GAMES:
        await q.answer("Игра уже идёт", show_alert=True)
        return
    try:
        count = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    if count < 5 or count > 20:
        await q.answer("От 5 до 20", show_alert=True)
        return
    thread_id = q.message.message_thread_id or 0
    game = Game(chat_id=chat.id, thread_id=thread_id, total=count)
    game.join_message_id = q.message.message_id
    GAMES[chat.id] = game
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎮 Играть (0/{count})", callback_data="mj")],
        [InlineKeyboardButton("❌ Отменить", callback_data="mc")],
    ])
    try:
        await q.edit_message_text(
            f"🍁 *Кленовая мафия* — набор\n\n"
            f"Нужно: *{count}*\nНабрано: *0/{count}*\n\n"
            f"❗ Каждому нажать /start в личке с ботом.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    except Exception as e:
        log.warning("edit start msg fail: %s", e)
    game.task = asyncio.create_task(_join_timeout(ctx.bot, chat.id, JOIN_TIMEOUT_SEC))
    await q.answer()


async def _join_timeout(bot, chat_id, secs):
    try:
        await asyncio.sleep(secs)
    except asyncio.CancelledError:
        return
    game = GAMES.get(chat_id)
    if game is None or game.phase != "joining":
        return
    GAMES.pop(chat_id, None)
    _unregister_all_in_chat(chat_id)
    try:
        await _send_to_chat(bot, game,
            f"⏳ Набор не завершён за {secs // 60} мин. Игра отменена.")
    except Exception:
        pass


async def cb_join(update, ctx):
    q = update.callback_query
    if q is None or q.message is None:
        return
    if not _check_thread(update):
        await q.answer("Не та ветка", show_alert=True)
        return
    chat = update.effective_chat
    user = q.from_user
    if user is None or user.is_bot:
        await q.answer("Боты не играют", show_alert=True)
        return
    game = GAMES.get(chat.id)
    if game is None or game.phase != "joining":
        await q.answer("Набор уже закрыт", show_alert=True)
        return
    if user.id in game.players:
        await q.answer("Ты уже в игре")
        return
    if len(game.players) >= game.total:
        await q.answer("Мест нет", show_alert=True)
        return
    try:
        await ctx.bot.send_message(
            user.id,
            "🍁 Ты в игре «Кленовая мафия». Роль придёт после набора.\n\n"
            "Когда игра начнётся, здесь появится кнопка «🍁 Меню».",
            disable_notification=True,
        )
    except Exception:
        await q.answer("Сначала нажми /start в личке с ботом!", show_alert=True)
        return
    game.players[user.id] = Player(user_id=user.id,
                                   username=user.username or "",
                                   full_name=user.full_name)
    _register_user_game(user.id, chat.id)
    n = len(game.players)
    if n >= game.total:
        if game.task and not game.task.done():
            game.task.cancel()
        await q.answer("Поехали!")
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat.id, message_id=game.join_message_id,
                text=f"🍁 *Набор окончен*\n\nИгроков: *{n}/{game.total}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await _start_game(ctx.bot, game)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎮 Играть ({n}/{game.total})", callback_data="mj")],
        [InlineKeyboardButton("❌ Отменить", callback_data="mc")],
    ])
    try:
        await ctx.bot.edit_message_text(
            chat_id=chat.id,
            message_id=game.join_message_id,
            text=f"🍁 *Кленовая мафия* — набор\n\n"
                 f"Нужно: *{game.total}*\nНабрано: *{n}/{game.total}*\n\n"
                 f"❗ Каждому нажать /start в личке с ботом.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except Exception as e:
        log.debug("edit join msg fail: %s", e)
    await q.answer(f"Ты в игре ({n}/{game.total})")


async def cb_cancel(update, ctx):
    q = update.callback_query
    if q is None:
        return
    chat = update.effective_chat
    user = q.from_user
    if not await _is_chat_admin(ctx.bot, chat.id, user.id):
        await q.answer("Только администратор", show_alert=True)
        return
    game = GAMES.pop(chat.id, None)
    if game is None:
        await q.answer()
        return
    if game.task and not game.task.done():
        game.task.cancel()
    _unregister_all_in_chat(chat.id)
    await q.answer("Отменено")
    try:
        await _send_to_chat(ctx.bot, game, "🍁 Игра отменена.")
    except Exception:
        pass


async def _start_game(bot, game):
    try:
        roles = get_roles(game.total)
        if roles is None:
            await _send_to_chat(bot, game, "❌ Нет ролей для такого числа игроков.")
            GAMES.pop(game.chat_id, None)
            _unregister_all_in_chat(game.chat_id)
            return
        roles = roles[:]
        random.shuffle(roles)
        uids = list(game.players.keys())
        random.shuffle(uids)
        for uid, role in zip(uids, roles):
            game.players[uid].role = role
            game.players[uid].games += 1
        mafia_names = [p.full_name for p in game.all_mafia()]
        for p in game.players.values():
            teammates = []
            if p.role == "mafia":
                teammates = [n for n in mafia_names if n != p.full_name]
            text = (f"🍁 Твоя роль: *{ROLE_NAMES[p.role]}*\n\n"
                    f"{ROLE_DESCRIPTIONS[p.role]}")
            if p.role == "mafia":
                text += (f"\n\n🤝 Союзники: {', '.join(teammates)}"
                         if teammates else "\n\n🤝 Ты единственная Тень.")
            ok = await _safe_send(bot, p.user_id, text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_menu_open_kb(game.chat_id))
            if not ok:
                game.dm_failed.add(p.user_id)
        sent = await _send_to_chat(
            bot, game,
            f"🍁 *Игра началась!*\n\n"
            f"Игроков: *{game.total}*\nТеней: *{len(game.all_mafia())}*\n\n"
            f"Роли отправлены в личку.\n"
            f"Команда /menu — магазин, профиль, инвентарь.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_open_kb(game.chat_id),
        )
        if sent:
            game.start_message_id = sent.message_id
        if game.dm_failed:
            names = [game.players[uid].full_name for uid in game.dm_failed
                     if uid in game.players]
            if names:
                await _send_to_chat(bot, game,
                    "⚠️ Не смог отправить роль в ЛС: " + ", ".join(names) +
                    "\n\nЭти игроки не нажали /start боту.")
        game.task = asyncio.create_task(_game_loop(bot, game))
    except Exception as e:
        log.exception("start_game error: %s", e)
        GAMES.pop(game.chat_id, None)
        _unregister_all_in_chat(game.chat_id)
        try:
            await _send_to_chat(bot, game, f"⚠️ Ошибка запуска: {e}")
        except Exception:
            pass


async def _game_loop(bot, game):
    try:
        while True:
            game.round_num += 1
            await _run_night(bot, game)
            if _check_win(game):
                await _announce_end(bot, game)
                return
            await _run_day(bot, game)
            if _check_win(game):
                await _announce_end(bot, game)
                return
            await _run_vote(bot, game)
            if _check_win(game):
                await _announce_end(bot, game)
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("game loop error: %s", e)
        try:
            await _send_to_chat(bot, game, f"⚠️ Ошибка в игре: {e}")
        except Exception:
            pass
    finally:
        GAMES.pop(game.chat_id, None)
        _unregister_all_in_chat(game.chat_id)


async def _run_night(bot, game):
    game.phase = "night"
    game.night_actions.clear()
    game.dm_failed.clear()
    for p in game.players.values():
        for iid in list(p.active_items.keys()):
            if p.active_items[iid] == "now":
                del p.active_items[iid]
    await _send_to_chat(
        bot, game,
        f"🌙 *Ночь {game.round_num}*\n\nКленовый город засыпает…",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_menu_open_kb(game.chat_id),
    )
    await asyncio.sleep(2)
    dead_players = [p for p in game.players.values() if not p.alive]
    for p in game.alive_players():
        if p.role == "citizen":
            continue
        if p.role == "mafia":
            targets = [op for op in game.alive_players()
                       if ROLE_TEAM[op.role] != "mafia"]
        elif p.role == "doctor":
            targets = game.alive_players()
        elif p.role in ("cop", "bartender", "whore"):
            targets = [op for op in game.alive_players() if op.user_id != p.user_id]
        elif p.role == "medium":
            targets = dead_players
        else:
            targets = []
        if not targets:
            game.dm_failed.add(p.user_id)
            continue
        rows = [[InlineKeyboardButton(t.full_name, callback_data=f"mn:{t.user_id}")]
                for t in targets]
        ok = await _safe_send(
            bot, p.user_id,
            f"🌙 *Ночь {game.round_num}*\n\n"
            f"Твоя роль: *{ROLE_NAMES[p.role]}*\n\nВыбери цель:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        if not ok:
            game.dm_failed.add(p.user_id)
    await _wait_all_actions(game, NIGHT_ACTION_TIMEOUT)
    await _resolve_night(bot, game)


async def _wait_all_actions(game, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_done = True
        for p in game.alive_players():
            if p.role == "citizen":
                continue
            if p.user_id not in game.night_actions and p.user_id not in game.dm_failed:
                all_done = False
                break
        if all_done:
            return
        await asyncio.sleep(0.5)


async def _resolve_night(bot, game):
    bartender_target = None
    whore_target = None
    doctor_target = None
    cop_target = None
    medium_target = None
    mafia_votes = []
    for uid, tid in game.night_actions.items():
        actor = game.players.get(uid)
        if actor is None or not actor.alive:
            continue
        if actor.role == "bartender":
            bartender_target = tid
        elif actor.role == "whore":
            whore_target = tid
        elif actor.role == "doctor":
            doctor_target = tid
        elif actor.role == "cop":
            cop_target = tid
        elif actor.role == "medium":
            medium_target = tid
        elif actor.role == "mafia":
            mafia_votes.append(tid)
    messages = []
    for target_id, src_name in [(bartender_target, "Укрыватель"),
                                (whore_target, "Очаровательница")]:
        if target_id and target_id in game.players:
            t = game.players[target_id]
            if t.alive:
                t.muted_until = time.time() + MUTE_DURATION_SEC
                messages.append(f"🔇 {t.full_name} замолчал на 5 мин ({src_name}).")
    if cop_target and cop_target in game.players:
        cop_player = next((p for p in game.alive_players() if p.role == "cop"), None)
        t = game.players[cop_target]
        is_mafia = ROLE_TEAM[t.role] == "mafia"
        if t.has_active("fake"):
            is_mafia = False
            if t.active_items["fake"] == "match":
                t.consume_active("fake")
        if cop_player:
            await _safe_send(
                bot, cop_player.user_id,
                f"🔍 Проверка: *{t.full_name}* — "
                f"{'ТЕНЬ' if is_mafia else 'Житель'}",
                parse_mode=ParseMode.MARKDOWN,
            )
    if medium_target and medium_target in game.players:
        med_player = next((p for p in game.alive_players() if p.role == "medium"), None)
        t = game.players[medium_target]
        if med_player:
            await _safe_send(
                bot, med_player.user_id,
                f"👻 Призрак *{t.full_name}* был ролью: *{ROLE_NAMES[t.role]}*",
                parse_mode=ParseMode.MARKDOWN,
            )
    if mafia_votes:
        c = Counter(mafia_votes)
        top_count = c.most_common(1)[0][1]
        candidates = [tid for tid, cnt in c.items() if cnt == top_count]
        kill_id = random.choice(candidates)
        victim = game.players[kill_id]
        if victim.has_active("amulet"):
            if victim.active_items["amulet"] == "match":
                victim.consume_active("amulet")
            messages.append(f"🛡 Оберег спас *{victim.full_name}*!")
        elif kill_id == doctor_target:
            messages.append(f"💉 Лекарь спас *{victim.full_name}*!")
        else:
            victim.alive = False
            messages.append(f"💀 Убит: *{victim.full_name}*")
    if not messages:
        messages.append("🌅 Ночь прошла спокойно.")
    await _send_to_chat(
        bot, game,
        "🌅 *Ночь закончилась*\n\n" + "\n".join(messages),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_menu_open_kb(game.chat_id),
    )


async def _run_day(bot, game):
    game.phase = "day"
    await _send_to_chat(
        bot, game,
        f"☀️ *День {game.round_num}*\n\nОбсуждение — 3 минуты.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_menu_open_kb(game.chat_id),
    )
    await asyncio.sleep(DAY_DISCUSSION_SEC)


async def _run_vote(bot, game):
    game.phase = "voting"
    game.votes.clear()
    alive = game.alive_players()
    if len(alive) <= 1:
        await _send_to_chat(bot, game, "⚖️ Остался один игрок — голосовать не с кем.")
        return
    rows = [[InlineKeyboardButton(p.full_name, callback_data=f"mv:{p.user_id}")]
            for p in alive]
    await _send_to_chat(
        bot, game,
        f"🗳 *Голосование* — 1 минута\n\nКто Тень?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await asyncio.sleep(VOTE_DURATION_SEC)
    valid_votes = {}
    for voter_id, target_id in game.votes.items():
        target = game.players.get(target_id)
        if target and target.has_active("blind"):
            if target.active_items["blind"] == "match":
                target.consume_active("blind")
            continue
        valid_votes[voter_id] = target_id
    if not valid_votes:
        await _send_to_chat(bot, game,
            "⚖️ *Голосование окончено*\n\nВсе голоса сгорели.",
            parse_mode=ParseMode.MARKDOWN)
        return
    c = Counter(valid_votes.values())
    top = c.most_common()
    if len(top) == 1 or top[0][1] > top[1][1]:
        eliminated_id = top[0][0]
        p = game.players[eliminated_id]
        p.alive = False
        await _send_to_chat(
            bot, game,
            f"⚖️ *Голосование окончено*\n\n"
            f"Изгнан: *{p.full_name}*\nРоль: *{ROLE_NAMES[p.role]}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await _send_to_chat(bot, game,
            "⚖️ *Голосование окончено*\n\nНичья.",
            parse_mode=ParseMode.MARKDOWN)


def _check_win(game):
    mafia = len(game.alive_mafia())
    city = len(game.alive_city())
    if mafia == 0:
        game.winner = "city"
        return True
    if mafia >= city:
        game.winner = "mafia"
        return True
    return False


async def _announce_end(bot, game):
    game.phase = "ended"
    if game.winner == "city":
        header = "🏆 *Победа Жителей!*\n\nВсе Тени устранены."
    else:
        header = "🏆 *Победа Теней!*\n\nТени контролируют город."
    lines = [header, "", "*Роли игроков:*"]
    for p in game.players.values():
        status = "жив" if p.alive else "мёртв"
        mark = " (Тень)" if ROLE_TEAM[p.role] == "mafia" else ""
        lines.append(f"• {p.full_name} — *{ROLE_NAMES[p.role]}*{mark} — {status}")
        if game.winner == "city" and ROLE_TEAM[p.role] == "city" and p.alive:
            p.coins += 10
            p.wins += 1
            if "first_win" not in p.achievements:
                p.achievements.add("first_win")
                lines.append("  └ 🏆 Первая кровь!")
        elif game.winner == "mafia" and ROLE_TEAM[p.role] == "mafia":
            p.coins += 15
            p.wins += 1
            if "first_win" not in p.achievements:
                p.achievements.add("first_win")
                lines.append("  └ 🏆 Первая кровь!")
        else:
            p.coins += 2
        if p.coins >= 100 and "rich" not in p.achievements:
            p.achievements.add("rich")
            lines.append("  └ 🏆 Богач!")
    lines.append("\n🍁 Спасибо за игру!")
    await _send_to_chat(bot, game, "\n".join(lines),
                        parse_mode=ParseMode.MARKDOWN)


async def cb_night_action(update, ctx):
    q = update.callback_query
    if q is None:
        return
    chat = update.effective_chat
    user = q.from_user
    game = GAMES.get(chat.id)
    if game is None or game.phase != "night":
        await q.answer("Сейчас не ночь", show_alert=True)
        return
    if user.id not in game.players:
        await q.answer("Ты не в игре", show_alert=True)
        return
    p = game.players[user.id]
    if not p.alive:
        await q.answer("Ты мёртв", show_alert=True)
        return
    if user.id in game.night_actions:
        await q.answer("Ты уже сделал ход", show_alert=True)
        return
    try:
        target_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    target = game.players.get(target_id)
    if target is None:
        await q.answer("Не найдено", show_alert=True)
        return
    if p.role == "mafia" and ROLE_TEAM[target.role] == "mafia":
        await q.answer("Нельзя выбрать Тень", show_alert=True)
        return
    if p.role == "medium" and target.alive:
        await q.answer("Только мёртвые", show_alert=True)
        return
    game.night_actions[user.id] = target_id
    await q.answer(f"Ты выбрал: {target.full_name}")
    try:
        await q.edit_message_text(
            f"🌙 *Ночь {game.round_num}*\n\nТы выбрал: *{target.full_name}*",
            parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def cb_vote(update, ctx):
    q = update.callback_query
    if q is None:
        return
    chat = update.effective_chat
    user = q.from_user
    game = GAMES.get(chat.id)
    if game is None or game.phase != "voting":
        await q.answer("Сейчас не голосование", show_alert=True)
        return
    if user.id not in game.players:
        await q.answer("Ты не в игре", show_alert=True)
        return
    p = game.players[user.id]
    if not p.alive:
        await q.answer("Ты мёртв", show_alert=True)
        return
    if p.is_muted():
        await q.answer("Не можешь голосовать (замолчал)", show_alert=True)
        return
    if user.id in game.votes:
        await q.answer("Ты уже голосовал", show_alert=True)
        return
    try:
        target_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    if target_id == user.id:
        await q.answer("Нельзя голосовать за себя", show_alert=True)
        return
    target = game.players.get(target_id)
    if target is None or not target.alive:
        await q.answer("Цель недоступна", show_alert=True)
        return
    game.votes[user.id] = target_id
    await q.answer(f"Голос за {target.full_name}")
    try:
        await q.edit_message_text(
            f"🗳 Твой голос: *{target.full_name}*",
            parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def cb_open_menu(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p:
        await q.answer("Ты не в игре", show_alert=True)
        return
    try:
        await ctx.bot.send_message(
            q.from_user.id, _menu_text(p),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_main_kb(chat_id),
        )
        await q.answer("Меню открыто в ЛС")
    except Exception:
        await q.answer("Нажми /start в личке с ботом!", show_alert=True)


async def cb_dm_pick(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[1])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p:
        await q.answer("Ты не в игре", show_alert=True)
        return
    try:
        await ctx.bot.send_message(
            q.from_user.id, _menu_text(p),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_main_kb(chat_id),
        )
        await q.answer("Меню открыто")
    except Exception:
        await q.answer("Не смог открыть меню", show_alert=True)


async def cb_menu_main(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p:
        await q.answer("Ты не в игре", show_alert=True)
        return
    try:
        await q.edit_message_text(_menu_text(p),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_main_kb(chat_id))
    except Exception:
        pass
    await q.answer()


async def cb_menu_profile(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p:
        await q.answer("Ты не в игре", show_alert=True)
        return
    items_text = ", ".join(f"{SHOP_ITEMS[k]['emoji']} x{v}"
                           for k, v in p.items.items()) or "пусто"
    active_text = ", ".join(f"{SHOP_ITEMS[k]['emoji']} ({'сейчас' if v == 'now' else 'матч'})"
                            for k, v in p.active_items.items()) or "нет"
    ach_text = ", ".join(p.achievements) or "нет"
    text = (
        f"👤 *Профиль* {p.full_name}\n\n"
        f"🍁 Листики: *{p.coins}*\n"
        f"🏆 Побед: *{p.wins}* / Игр: *{p.games}*\n"
        f"🎭 Роль: *{ROLE_NAMES[p.role]}* ({'жив' if p.alive else 'мёртв'})\n\n"
        f"🎒 Инвентарь: {items_text}\n"
        f"⚡ Активно: {active_text}\n"
        f"🏅 Достижения: {ach_text}"
    )
    try:
        await q.edit_message_text(text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_back_kb(chat_id))
    except Exception:
        pass
    await q.answer()


async def cb_menu_shop(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    rows = []
    for iid, item in SHOP_ITEMS.items():
        rows.append([InlineKeyboardButton(
            f"{item['emoji']} {item['name']} — {item['price']}🍁",
            callback_data=f"shop:item:{chat_id}:{iid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    try:
        await q.edit_message_text(
            f"🛒 *Магазин*\n\nБаланс: *{p.coins}🍁*\n\nВыбери предмет:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        pass
    await q.answer()


async def cb_shop_item(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        parts = q.data.split(":")
        chat_id = int(parts[2])
        iid = parts[3]
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    item = SHOP_ITEMS.get(iid)
    if not item:
        await q.answer("Нет предмета", show_alert=True)
        return
    text = (f"{item['emoji']} *{item['name']}*\n\n"
            f"{item['desc']}\n\nЦена: *{item['price']}🍁*\n"
            f"Твой баланс: *{p.coins}🍁*")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Купить",
            callback_data=f"shop:confirm:{chat_id}:{iid}")],
        [InlineKeyboardButton("◀️ Назад",
            callback_data=f"menu:shop:{chat_id}")],
    ])
    try:
        await q.edit_message_text(text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass
    await q.answer()


async def cb_shop_confirm(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        parts = q.data.split(":")
        chat_id = int(parts[2])
        iid = parts[3]
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    item = SHOP_ITEMS.get(iid)
    if not item:
        await q.answer("Нет предмета", show_alert=True)
        return
    if p.coins < item["price"]:
        await q.answer("Недостаточно листиков!", show_alert=True)
        return
    p.coins -= item["price"]
    p.items[iid] = p.items.get(iid, 0) + 1
    await q.answer("Куплено!")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎒 Инвентарь",
            callback_data=f"menu:inv:{chat_id}")],
        [InlineKeyboardButton("◀️ В магазин",
            callback_data=f"menu:shop:{chat_id}")],
    ])
    try:
        await q.edit_message_text(
            f"✅ Куплено: {item['emoji']} *{item['name']}*\n\n"
            f"Остаток: *{p.coins}🍁*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass


async def cb_menu_inv(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    rows = []
    for iid, cnt in p.items.items():
        if iid not in SHOP_ITEMS:
            continue
        item = SHOP_ITEMS[iid]
        active = " ⚡" if iid in p.active_items else ""
        rows.append([InlineKeyboardButton(
            f"{item['emoji']} {item['name']} x{cnt}{active}",
            callback_data=f"inv:item:{chat_id}:{iid}")])
    if not rows:
        rows.append([InlineKeyboardButton("— пусто —", callback_data="noop")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    try:
        await q.edit_message_text(
            "🎒 *Инвентарь*\n\nВыбери предмет:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        pass
    await q.answer()


async def cb_inv_item(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        parts = q.data.split(":")
        chat_id = int(parts[2])
        iid = parts[3]
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    item = SHOP_ITEMS.get(iid)
    if not item:
        await q.answer("Нет предмета", show_alert=True)
        return
    in_inv = p.items.get(iid, 0) > 0
    active_mode = p.active_items.get(iid)
    text = f"{item['emoji']} *{item['name']}*\n\n{item['desc']}\n\n"
    if in_inv:
        text += f"В инвентаре: *{p.items[iid]}* шт.\n"
    if active_mode:
        text += f"⚡ Активировано: *{'сейчас' if active_mode == 'now' else 'на матч'}*\n"
    text += "\nКак использовать?"
    buttons = []
    if in_inv and not active_mode:
        if "now" in item["modes"]:
            if game.phase == "night" or iid == "cookie":
                buttons.append([InlineKeyboardButton(
                    "⚡ Использовать сейчас",
                    callback_data=f"inv:ask:{chat_id}:{iid}:now")])
            else:
                buttons.append([InlineKeyboardButton(
                    "⚡ Сейчас (только ночью)",
                    callback_data="noop")])
        if "match" in item["modes"]:
            buttons.append([InlineKeyboardButton(
                "🎯 Активировать на матч",
                callback_data=f"inv:ask:{chat_id}:{iid}:match")])
    buttons.append([InlineKeyboardButton(
        "◀️ Назад", callback_data=f"menu:inv:{chat_id}")])
    try:
        await q.edit_message_text(text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        pass
    await q.answer()


async def cb_inv_ask(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        parts = q.data.split(":")
        chat_id = int(parts[2])
        iid = parts[3]
        mode = parts[4]
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    item = SHOP_ITEMS.get(iid)
    if not item:
        await q.answer("Нет предмета", show_alert=True)
        return
    if p.items.get(iid, 0) <= 0:
        await q.answer("У тебя нет этого предмета", show_alert=True)
        return
    if iid in p.active_items:
        await q.answer("Этот предмет уже активен", show_alert=True)
        return
    mode_label = "сейчас" if mode == "now" else "на весь матч"
    text = (f"⚠️ Подтвердить использование?\n\n"
            f"{item['emoji']} *{item['name']}*\n"
            f"Режим: *{mode_label}*\n\n"
            f"{item['desc']}\n\n"
            f"После подтверждения предмет будет потрачен.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить",
            callback_data=f"inv:do:{chat_id}:{iid}:{mode}")],
        [InlineKeyboardButton("◀️ Отмена",
            callback_data=f"inv:item:{chat_id}:{iid}")],
    ])
    try:
        await q.edit_message_text(text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        pass
    await q.answer()


async def cb_inv_do(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        parts = q.data.split(":")
        chat_id = int(parts[2])
        iid = parts[3]
        mode = parts[4]
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    item = SHOP_ITEMS.get(iid)
    if not item:
        await q.answer("Нет предмета", show_alert=True)
        return
    if p.items.get(iid, 0) <= 0:
        await q.answer("У тебя нет этого предмета", show_alert=True)
        return
    if iid in p.active_items:
        await q.answer("Уже активен", show_alert=True)
        return
    if mode not in item["modes"]:
        await q.answer("Этот режим недоступен", show_alert=True)
        return
    if mode == "now" and iid != "cookie" and game.phase != "night":
        await q.answer("«Сейчас» доступно только ночью", show_alert=True)
        return
    p.items[iid] -= 1
    if p.items[iid] == 0:
        del p.items[iid]
    if iid == "cookie":
        targets = [op for op in game.alive_players() if op.user_id != p.user_id]
        if targets:
            target = random.choice(targets)
            await _safe_send(ctx.bot, p.user_id,
                f"🍪 Предсказание: *{target.full_name}* — *{ROLE_NAMES[target.role]}*",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await _safe_send(ctx.bot, p.user_id, "🍪 Нет других игроков.")
        await q.answer("Готово")
        try:
            await q.edit_message_text(
                "🍪 Предсказание получено!\n\nПроверь ЛС.",
                reply_markup=_menu_back_kb(chat_id))
        except Exception:
            pass
        return
    p.active_items[iid] = mode
    label = "сейчас" if mode == "now" else "на матч"
    await q.answer(f"Активировано: {item['name']} ({label})")
    try:
        await q.edit_message_text(
            f"✅ Активировано: {item['emoji']} *{item['name']}* ({label})",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_back_kb(chat_id))
    except Exception:
        pass


async def cb_menu_top(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    players_sorted = sorted(game.players.values(), key=lambda x: x.coins, reverse=True)
    lines = ["🏆 *Топ богачей*:", ""]
    for i, p in enumerate(players_sorted[:10], 1):
        lines.append(f"{i}. {p.full_name} — *{p.coins}🍁*")
    try:
        await q.edit_message_text("\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_back_kb(chat_id))
    except Exception:
        pass
    await q.answer()


async def cb_menu_bonus(update, ctx):
    q = update.callback_query
    if not q:
        return
    try:
        chat_id = int(q.data.split(":")[2])
    except (IndexError, ValueError):
        await q.answer("Ошибка", show_alert=True)
        return
    game = GAMES.get(chat_id)
    if not game:
        await q.answer("Игра завершена", show_alert=True)
        return
    p = game.players.get(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Ты не в игре", show_alert=True)
        return
    if p.bonus_claimed:
        await q.answer("Ты уже получил бонус в этом матче", show_alert=True)
        return
    p.bonus_claimed = True
    p.coins += 5
    await q.answer("Бонус получен!")
    try:
        await q.edit_message_text(
            f"🍁 *Бонус* +5🍁\n\nБаланс: *{p.coins}🍁*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_menu_back_kb(chat_id))
    except Exception:
        pass


async def cb_noop(update, ctx):
    q = update.callback_query
    if q:
        await q.answer()


async def on_member_left(update, ctx):
    cm = update.chat_member
    if cm is None:
        return
    u = cm.new_chat_member.user
    old_s = cm.old_chat_member.status
    new_s = cm.new_chat_member.status
    if new_s not in ("left", "kicked"):
        return
    if old_s in ("left", "kicked"):
        return
    game = GAMES.get(cm.chat.id)
    if game is None:
        return
    p = game.players.get(u.id)
    if p is None or not p.alive:
        return
    p.alive = False
    _unregister_user_game(u.id, cm.chat.id)
    try:
        await _send_to_chat(ctx.bot, game,
            f"🚪 *{p.full_name}* покинул чат и выбывает.",
            parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    if _check_win(game):
        await _announce_end(ctx.bot, game)


def _build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    g = filters.ChatType.GROUPS

    app.add_handler(MessageHandler(g, activation_mw), group=-300)
    app.add_handler(MessageHandler(g, guard_muted_messages), group=-100)

    app.add_handler(ChatMemberHandler(
        on_bot_added, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(
        on_member_left, chat_member_types=ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(MessageHandler(
        g & filters.TEXT & filters.Regex(ACTIVATION_REGEX), cmd_activate))
    app.add_handler(CommandHandler("activate", cmd_activate, filters=g))

    app.add_handler(MessageHandler(
        g & filters.TEXT & filters.Regex(MAFIA_START_REGEX), cmd_mafia_start))
    app.add_handler(MessageHandler(
        g & filters.TEXT & filters.Regex(MAFIA_STOP_REGEX), cmd_mafia_stop))
    app.add_handler(CommandHandler("mafia", cmd_mafia_start, filters=g))
    app.add_handler(CommandHandler("mafia_stop", cmd_mafia_stop, filters=g))
    app.add_handler(CommandHandler("threadid", cmd_threadid, filters=g))
    app.add_handler(CommandHandler("menu", cmd_menu, filters=g))

    app.add_handler(CommandHandler("start", cmd_dm_start,
                                   filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("menu", cmd_dm_menu,
                                   filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        cmd_dm_other,
    ))

    app.add_handler(CallbackQueryHandler(cb_start_count, pattern=r"^ms:"))
    app.add_handler(CallbackQueryHandler(cb_join, pattern=r"^mj$"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern=r"^mc$"))
    app.add_handler(CallbackQueryHandler(cb_night_action, pattern=r"^mn:"))
    app.add_handler(CallbackQueryHandler(cb_vote, pattern=r"^mv:"))
    app.add_handler(CallbackQueryHandler(cb_open_menu, pattern=r"^openmenu:"))
    app.add_handler(CallbackQueryHandler(cb_dm_pick, pattern=r"^dmpick:"))
    app.add_handler(CallbackQueryHandler(cb_menu_main, pattern=r"^menu:main:"))
    app.add_handler(CallbackQueryHandler(cb_menu_profile, pattern=r"^menu:profile:"))
    app.add_handler(CallbackQueryHandler(cb_menu_shop, pattern=r"^menu:shop:"))
    app.add_handler(CallbackQueryHandler(cb_menu_inv, pattern=r"^menu:inv:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top, pattern=r"^menu:top:"))
    app.add_handler(CallbackQueryHandler(cb_menu_bonus, pattern=r"^menu:bonus:"))
    app.add_handler(CallbackQueryHandler(cb_shop_item, pattern=r"^shop:item:"))
    app.add_handler(CallbackQueryHandler(cb_shop_confirm, pattern=r"^shop:confirm:"))
    app.add_handler(CallbackQueryHandler(cb_inv_item, pattern=r"^inv:item:"))
    app.add_handler(CallbackQueryHandler(cb_inv_ask, pattern=r"^inv:ask:"))
    app.add_handler(CallbackQueryHandler(cb_inv_do, pattern=r"^inv:do:"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    return app


async def _shutdown_app(app):
    log.info("shutting down app…")
    if app.updater is not None:
        try:
            await app.updater.stop()
            await asyncio.sleep(2)
        except Exception as e:
            log.debug("updater.stop: %s", e)
    for step in (app.stop, app.shutdown):
        try:
            await step()
        except Exception as e:
            log.debug("shutdown step: %s", e)
    log.info("app shut down.")


async def _run_bot():
    cycle = 0
    while True:
        cycle += 1
        log.info("=== app cycle #%d ===", cycle)
        app = None
        try:
            app = _build_app()
            log.info("Подключение к Telegram…")
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=30,
            )
            log.info("Bot running.")
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            log.info("Bot stopped by user.")
            if app is not None:
                await _shutdown_app(app)
            break
        except Conflict as e:
            log.warning("Conflict: %s. Жду 45с", e)
            if app is not None:
                await _shutdown_app(app)
            await asyncio.sleep(45)
        except (TimedOut, NetworkError) as e:
            log.warning("Сеть: %s. Жду 10с", type(e).__name__)
            if app is not None:
                await _shutdown_app(app)
            await asyncio.sleep(10)
        except Exception as e:
            log.exception("Ошибка цикла #%d: %s", cycle, e)
            if app is not None:
                await _shutdown_app(app)
            await asyncio.sleep(15)


def main():
    log.info("Bot starting…")
    while True:
        try:
            asyncio.run(_run_bot())
            break
        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except SystemExit:
            break
        except Exception as e:
            log.exception("Фатальный краш. Рестарт через 15 сек: %s", e)
            try:
                time.sleep(15)
            except KeyboardInterrupt:
                break


if __name__ == "__main__":
    main()
