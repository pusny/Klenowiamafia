import asyncio
import json
import logging
import os
import random
import re
import secrets
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

try:
    ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")
except ValueError:
    ADMIN_ID = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mafia")

DB_DIR = Path(os.getenv("DATA_DIR", "."))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "mafia.db")

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
            CREATE TABLE IF NOT EXISTS users_data (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                coins INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                title TEXT DEFAULT 'Новичок',
                wins INTEGER DEFAULT 0,
                games INTEGER DEFAULT 0,
                inventory TEXT DEFAULT '{}',
                achievements TEXT DEFAULT '[]',
                last_bonus REAL DEFAULT 0
            );
        """)
        conn.commit()

_init_db()

def db_get_user(user_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO users_data (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        d = dict(row)
        d['inventory'] = json.loads(d['inventory'])
        d['achievements'] = json.loads(d['achievements'])
        return d

def db_save_user(user_id, **kwargs):
    u = db_get_user(user_id)
    for k, v in kwargs.items():
        if k in u:
            u[k] = v
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("""
            UPDATE users_data SET 
                username=?, coins=?, level=?, title=?, wins=?, games=?, inventory=?, achievements=?, last_bonus=?
            WHERE user_id=?
        """, (
            u['username'], u['coins'], u['level'], u['title'], u['wins'], u['games'],
            json.dumps(u['inventory']), json.dumps(u['achievements']), u['last_bonus'], user_id
        ))
        conn.commit()

def is_chat_activated(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute("SELECT 1 FROM activations WHERE chat_id=?", (chat_id,)).fetchone()
        return row is not None

def activate_chat(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR IGNORE INTO activations (chat_id) VALUES (?)", (chat_id,))
        conn.commit()

def get_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute("SELECT key FROM pending_activations WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None

def set_pending_key(chat_id, key):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR REPLACE INTO pending_activations (chat_id, key) VALUES (?, ?)", (chat_id, key))
        conn.commit()

def clear_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("DELETE FROM pending_activations WHERE chat_id=?", (chat_id,))
        conn.commit()

def _gen_key():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))

def _log_activation_key(chat_id, chat_title, key):
    print(f"=== KEY: {key} for chat {chat_title} ({chat_id}) ===", flush=True)

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
    5:  "M C G G G", 6: "M C D G G G", 7: "M M C D G G G", 8: "M M C D B G G G",
    9:  "M M C D B W G G G", 10: "M M M C D B W G G G", 11: "M M M C D B W N G G G",
    12: "M M M C D B W N G G G G", 13: "M M M M C D B W N G G G G",
    14: "M M M M C D B W N G G G G G", 15: "M M M M C D B W N G G G G G G",
    16: "M M M M M C D B W N G G G G G G", 17: "M M M M M C D B W N G G G G G G G",
    18: "M M M M M C D B W N G G G G G G G G", 19: "M M M M M M C D B W N G G G G G G G G",
    20: "M M M M M M C D B W N G G G G G G G G G",
}

SHOP_ITEMS = {
    "amulet": {"name": "Оберег", "emoji": "🛡", "price": 30, "desc": "Защита от убийства", "modes": ("now", "match")},
    "blind": {"name": "Отвод глаз", "emoji": "👁‍🗨", "price": 25, "desc": "Защита от голосования", "modes": ("now", "match")},
    "fake": {"name": "Фальшивка", "emoji": "📄", "price": 40, "desc": "Смотритель видит вас как жителя", "modes": ("now", "match")},
    "cookie": {"name": "Печенье", "emoji": "🍪", "price": 15, "desc": "Узнать роль случайного игрока", "modes": ("now",)},
}

TITLES_SHOP = {
    "novice": {"name": "Новичок", "emoji": "🐣", "price": 0, "ach_req": 0},
    "leaf_collector": {"name": "Собиратель листвы", "emoji": "🍁", "price": 50, "ach_req": 0},
    "guardian": {"name": "Страж Клена", "emoji": "🌳", "price": 150, "ach_req": 0},
    "king": {"name": "Осенний Король", "emoji": "👑", "price": 500, "ach_req": 0},
    "detective": {"name": "Детектив", "emoji": "🕵️", "price": 0, "ach_req": 3},
    "veteran": {"name": "Ветеран", "emoji": "🎖", "price": 0, "ach_req": 6},
    "legend": {"name": "Легенда", "emoji": "🌟", "price": 0, "ach_req": 9},
}

ALL_ACHIEVEMENTS = {
    "first_win": "🍁 Первая победа",
    "rich": "💰 Богач (100 листиков)",
    "games_10": "🎮 10 сыгранных игр",
    "games_50": "🎮 50 сыгранных игр",
    "wins_5": "🏆 5 побед",
    "wins_20": "🏆 20 побед",
    "croc_master": "🐊 Крокодилий мастер",
    "mafia_boss": "🔪 Победа за Тень",
    "level_5": "⭐ Достигнут 5 уровень",
    "level_10": "🌟 Достигнут 10 уровень"
}

JOIN_TIMEOUT_SEC = 300
NIGHT_ACTION_TIMEOUT = 60
DAY_DISCUSSION_SEC = 180
VOTE_DURATION_SEC = 60
MUTE_DURATION_SEC = 5 * 60

ACTIVATION_REGEX = re.compile(r"(?i)^(?:/)?(?:активация|activate)(?:\s+\S+)?$")
MAFIA_START_REGEX = re.compile(r"(?i)^мафия\s+(?:начать|старт)$")
MAFIA_STOP_REGEX = re.compile(r"(?i)^мафия\s+(?:стоп|stop)$")

CROCODILE_REGEX = re.compile(r"(?i)^/?(?:крокодил|слова|crocodile|words)(?:\s+(?:начать|старт))?$")

CROC_GAME = {}
CROC_AWAITING = {}
ADMIN_STATE = {}

def get_roles(count):
    spec = ROLE_DISTRIBUTION.get(count)
    return [_ROLE_MAP[t] for t in spec.split()] if spec else None

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
    achievements: list = field(default_factory=list)
    wins: int = 0
    games: int = 0
    level: int = 1
    title: str = "Новичок"
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

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

    def alive_mafia(self):
        return [p for p in self.alive_players() if p.role == "mafia"]

    def alive_city(self):
        return [p for p in self.alive_players() if ROLE_TEAM[p.role] == "city"]

    def all_mafia(self):
        return [p for p in self.players.values() if p.role == "mafia"]

GAMES = {}

def _check_thread(update):
    if THREAD_ID <= 0: return True
    msg = update.effective_message
    if msg is None: return False
    if msg.chat.type == "private": return True
    return (msg.message_thread_id or 0) == THREAD_ID

async def _is_chat_admin(bot, chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("creator", "administrator")
    except: return False

async def _safe_send(bot, user_id, text, **kwargs):
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except: return False

async def _send_to_chat(bot, game, text, **kwargs):
    if game.thread_id:
        kwargs["message_thread_id"] = game.thread_id
    return await bot.send_message(game.chat_id, text, **kwargs)

async def activation_mw(update, ctx):
    msg = update.effective_message
    if msg is None or msg.chat.type == "private": return
    if is_chat_activated(msg.chat.id): return
    if ACTIVATION_REGEX.match((msg.text or "").strip()): return
    raise ApplicationHandlerStop

async def on_bot_added(update, ctx):
    cm = update.my_chat_member
    if cm is None or cm.new_chat_member.user.id != (await ctx.bot.get_me()).id: return
    if cm.new_chat_member.status not in ("member", "administrator") or cm.old_chat_member.status not in ("left", "kicked"): return
    if cm.chat.type == "private" or is_chat_activated(cm.chat.id) or get_pending_key(cm.chat.id): return
    key = _gen_key()
    set_pending_key(cm.chat.id, key)
    _log_activation_key(cm.chat.id, cm.chat.title or "", key)
    await ctx.bot.send_message(cm.chat.id, "🍁 Для активации введите:\n<code>активация КЛЮЧ</code>", parse_mode=ParseMode.HTML)

async def cmd_activate(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or msg.chat.type == "private" or is_chat_activated(msg.chat.id): return
    parts = (msg.text or "").lower().lstrip("/").split()
    if len(parts) < 2:
        if await _is_chat_admin(ctx.bot, msg.chat.id, user.id):
            key = get_pending_key(msg.chat.id) or _gen_key()
            set_pending_key(msg.chat.id, key)
            _log_activation_key(msg.chat.id, msg.chat.title or "", key)
        return
    if parts[1].strip().upper() == (get_pending_key(msg.chat.id) or "").upper():
        activate_chat(msg.chat.id)
        clear_pending_key(msg.chat.id)
        await msg.reply_text("✅ Чат активирован!")

async def guard_muted_messages(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    user = update.effective_user
    if not user or user.is_bot or not _check_thread(update): return
    game = GAMES.get(msg.chat.id)
    if not game or (msg.message_thread_id or 0) != game.thread_id: return
    if await _is_chat_admin(ctx.bot, msg.chat.id, user.id): return
    p = game.players.get(user.id)
    if not p: return
    if game.phase == "night" or not p.alive or p.is_muted():
        try: await msg.delete()
        except: pass
        raise ApplicationHandlerStop

def _menu_main_kb(user_id, chat_id=0):
    rows = [
        [InlineKeyboardButton("👤 Профиль", callback_data=f"menu:profile:{chat_id}"), InlineKeyboardButton("🛒 Магазин", callback_data=f"menu:shop:{chat_id}")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data=f"menu:inv:{chat_id}"), InlineKeyboardButton("🏆 Топ", callback_data=f"menu:top:{chat_id}")],
        [InlineKeyboardButton("🏅 Достижения", callback_data=f"menu:ach:{chat_id}"), InlineKeyboardButton("🍁 Бонус", callback_data=f"menu:bonus:{chat_id}")],
        [InlineKeyboardButton("👑 Титулы", callback_data=f"menu:titles:{chat_id}")]
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)

def _menu_back_kb(chat_id=0):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]])

def _menu_text(uid):
    u = db_get_user(uid)
    return f"🍁 *Меню*\n\nБаланс: *{u['coins']}🍁*\nУровень: *{u['level']}*\nТитул: *{u['title']}*"

async def cmd_dm_start(update, ctx):
    msg = update.effective_message
    if not msg: return
    await msg.reply_text(_menu_text(msg.from_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(msg.from_user.id))

async def cmd_dm_menu(update, ctx):
    msg = update.effective_message
    if not msg: return
    await msg.reply_text(_menu_text(msg.from_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(msg.from_user.id))

async def cmd_dm_other(update, ctx):
    msg = update.effective_message
    uid = update.effective_user.id
    db_get_user(uid)
    
    if uid in ADMIN_STATE:
        action = ADMIN_STATE.pop(uid)
        parts = msg.text.split(maxsplit=2)
        if action == "give_coins" and len(parts) >= 2:
            try:
                target, amount = int(parts[0].replace("@", "")), int(parts[1])
                u = db_get_user(target)
                db_save_user(target, coins=u['coins']+amount)
                await msg.reply_text(f"✅ Выдано {amount}🍁 юзеру {target}")
            except: await msg.reply_text("❌ Ошибка. Нужно: `ID количество`")
        elif action == "give_level" and len(parts) >= 2:
            try:
                target, lvl = int(parts[0].replace("@", "")), int(parts[1])
                db_save_user(target, level=lvl)
                await msg.reply_text(f"✅ Выдан уровень {lvl} юзеру {target}")
            except: await msg.reply_text("❌ Ошибка.")
        elif action == "give_title" and len(parts) >= 2:
            try:
                target, title = int(parts[0].replace("@", "")), " ".join(parts[1:])
                db_save_user(target, title=title)
                await msg.reply_text(f"✅ Выдан титул '{title}' юзеру {target}")
            except: await msg.reply_text("❌ Ошибка.")
        return

    if uid in CROC_AWAITING:
        chat_id = CROC_AWAITING.pop(uid)
        if "|" in msg.text:
            word, desc = msg.text.split("|", 1)
            CROC_GAME[chat_id] = {"word": word.strip(), "desc": desc.strip(), "author": uid}
            try:
                await ctx.bot.send_message(chat_id, f"🐊 *Крокодил!*\n\nОписание: {desc.strip()}\n\nКто угадает слово, пишите в чат!", message_thread_id=THREAD_ID or 0, parse_mode=ParseMode.MARKDOWN)
                await msg.reply_text("✅ Слово загадано!")
            except: pass
        else:
            await msg.reply_text("❌ Ошибка. Пиши: `Слово | Описание`")
        return
        
    await msg.reply_text(_menu_text(uid), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(uid))

async def cmd_mafia_start(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("⛔ Только администратор чата может запустить игру.")
    
    if msg.chat.id in GAMES: return await msg.reply_text("⚠️ Игра уже идёт.")
    buttons = [InlineKeyboardButton(str(i), callback_data=f"ms:{i}") for i in range(5, 21)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    await msg.reply_text("🍁 *Кленовая мафия*\nВыбери количество:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

async def cmd_mafia_stop(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id): return
    game = GAMES.pop(msg.chat.id, None)
    if game:
        if game.task and not game.task.done(): game.task.cancel()
        await msg.reply_text("🛑 Игра остановлена.")

async def cmd_crocodile(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("⛔ Только администратор чата может запустить Крокодила.")
        
    await msg.reply_text("🐊 *Крокодил*\nНажми кнопку, чтобы загадать слово.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Загадать слово", callback_data="croc:new")]]))

async def cb_start_count(update, ctx):
    q = update.callback_query
    if q is None or not _check_thread(update): return await q.answer("Не та ветка", show_alert=True)
    if q.message.chat.id in GAMES: return await q.answer("Игра уже идёт", show_alert=True)
    count = int(q.data.split(":")[1])
    game = Game(chat_id=q.message.chat.id, thread_id=q.message.message_thread_id or 0, total=count)
    game.join_message_id = q.message.message_id
    GAMES[game.chat_id] = game
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть (0/{count})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    await q.edit_message_text(f"🍁 *Набор*\nНужно: *{count}*\nНабрано: *0/{count}*\n\n❗ Каждому нажать /start в ЛС бота.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    game.task = asyncio.create_task(_join_timeout(ctx.bot, game.chat_id))

async def _join_timeout(bot, chat_id):
    await asyncio.sleep(JOIN_TIMEOUT_SEC)
    game = GAMES.get(chat_id)
    if game and game.phase == "joining":
        GAMES.pop(chat_id, None)
        await _send_to_chat(bot, game, "⏳ Набор отменён.")

async def cb_join(update, ctx):
    q = update.callback_query
    if q is None or not _check_thread(update): return await q.answer("Не та ветка", show_alert=True)
    chat = update.effective_chat
    user = q.from_user
    game = GAMES.get(chat.id)
    if not game or game.phase != "joining": return await q.answer("Набор закрыт", show_alert=True)
    if user.id in game.players: return await q.answer("Ты уже в игре")
    
    try: await ctx.bot.send_message(user.id, "🍁 Ты в игре!")
    except: return await q.answer("Сначала нажми /start в ЛС!", show_alert=True)
    
    u = db_get_user(user.id)
    p = Player(user_id=user.id, username=user.username or "", full_name=user.full_name)
    p.coins, p.items, p.achievements, p.wins, p.games = u['coins'], u['inventory'], u['achievements'], u['wins'], u['games']
    p.level, p.title = u['level'], u['title']
    game.players[user.id] = p
    
    n = len(game.players)
    if n >= game.total:
        if game.task and not game.task.done(): game.task.cancel()
        await q.answer("Поехали!")
        await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 *Набор окончен*\nИгроков: *{n}/{game.total}*", parse_mode=ParseMode.MARKDOWN)
        await _start_game(ctx.bot, game)
        return
    names = ", ".join([p.full_name for p in game.players.values()])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть ({n}/{game.total})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    try:
        await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 *Набор*\nНужно: *{game.total}*\nНабрано: *{n}/{game.total}*\n\n👥 {names}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except: pass
    await q.answer(f"В игре ({n}/{game.total})")

async def _start_game(bot, game):
    roles = get_roles(game.total)[:]
    random.shuffle(roles)
    uids = list(game.players.keys())
    random.shuffle(uids)
    for uid, role in zip(uids, roles):
        game.players[uid].role = role
    mafia_names = [p.full_name for p in game.all_mafia()]
    
    for p in game.players.values():
        teammates = [n for n in mafia_names if n != p.full_name]
        text = f"🍁 Твоя роль: *{ROLE_NAMES[p.role]}*\n\n{ROLE_DESCRIPTIONS[p.role]}"
        if p.role == "mafia" and teammates: text += f"\n\n🤝 Союзники: {', '.join(teammates)}"
        try: await bot.send_message(p.user_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data="openmenu:0")]]))
        except: pass

    await _send_to_chat(bot, game, f"🍁 *Игра началась!*\nИгроков: *{game.total}*\nТеней: *{len(game.all_mafia())}*", parse_mode=ParseMode.MARKDOWN)
    game.task = asyncio.create_task(_game_loop(bot, game))

async def _game_loop(bot, game):
    try:
        while True:
            game.round_num += 1
            await _run_night(bot, game)
            if _check_win(game): break
            await _run_day(bot, game)
            if _check_win(game): break
            await _run_vote(bot, game)
            if _check_win(game): break
    except: pass
    finally:
        await _announce_end(bot, game)
        GAMES.pop(game.chat_id, None)

async def _run_night(bot, game):
    game.phase = "night"
    game.night_actions.clear()
    await _send_to_chat(bot, game, f"🌙 *Ночь {game.round_num}*\nКленовый город засыпает…", parse_mode=ParseMode.MARKDOWN)
    await asyncio.sleep(2)
    
    for p in game.alive_players():
        if p.role == "citizen": continue
        targets = [op for op in game.alive_players() if op.user_id != p.user_id]
        if p.role == "mafia": targets = [op for op in game.alive_players() if ROLE_TEAM[op.role] != "mafia"]
        elif p.role == "doctor": targets = game.alive_players()
        elif p.role == "medium": targets = [op for op in game.players.values() if not op.alive]
        if not targets: continue
        rows = [[InlineKeyboardButton(t.full_name, callback_data=f"mn:{game.chat_id}:{t.user_id}")] for t in targets]
        try: await bot.send_message(p.user_id, f"🌙 *Ночь*\nВыбери цель:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
        except: pass
    
    await asyncio.sleep(NIGHT_ACTION_TIMEOUT)
    await _resolve_night(bot, game)

async def _resolve_night(bot, game):
    bart_t = whore_t = doc_t = cop_t = med_t = None
    m_votes = []
    for uid, tid in game.night_actions.items():
        actor = game.players.get(uid)
        if not actor or not actor.alive: continue
        if actor.role == "bartender": bart_t = tid
        elif actor.role == "whore": whore_t = tid
        elif actor.role == "doctor": doc_t = tid
        elif actor.role == "cop": cop_t = tid
        elif actor.role == "medium": med_t = tid
        elif actor.role == "mafia": m_votes.append(tid)
    
    msg = []
    for tid, src in [(bart_t, "Укрыватель"), (whore_t, "Очаровательница")]:
        if tid and tid in game.players:
            t = game.players[tid]
            if t.alive: t.muted_until = time.time() + MUTE_DURATION_SEC; msg.append(f"🔇 {t.full_name} замолчал.")
            
    if cop_t:
        c = next((p for p in game.alive_players() if p.role == "cop"), None)
        t = game.players[cop_t]
        is_m = ROLE_TEAM[t.role] == "mafia"
        if t.has_active("fake"): is_m = False; t.consume_active("fake")
        if c: await _safe_send(bot, c.user_id, f"🔍 {t.full_name} — {'ТЕНЬ' if is_m else 'Житель'}")
        
    if med_t:
        m = next((p for p in game.alive_players() if p.role == "medium"), None)
        t = game.players[med_t]
        if m: await _safe_send(bot, m.user_id, f"👻 {t.full_name} был: {ROLE_NAMES[t.role]}")
        
    if m_votes:
        c = Counter(m_votes).most_common()
        if c[0][1] > 1 or len(c) == 1:
            k = c[0][0]; v = game.players[k]
            if v.has_active("amulet"): v.consume_active("amulet"); msg.append(f"🛡 Оберег спас {v.full_name}!")
            elif k == doc_t: msg.append(f"💉 Лекарь спас {v.full_name}!")
            else: v.alive = False; msg.append(f"💀 Убит: {v.full_name}")
        else: msg.append("Тени не договорились.")
        
    if not msg: msg.append("Спокойная ночь.")
    await _send_to_chat(bot, game, "🌅 *Итоги ночи:*\n" + "\n".join(msg), parse_mode=ParseMode.MARKDOWN)

async def _run_day(bot, game):
    game.phase = "day"
    alive = ", ".join([p.full_name for p in game.alive_players()])
    await _send_to_chat(bot, game, f"☀️ *День {game.round_num}*\nЖивы: {alive}\nОбсуждение 3 минуты.", parse_mode=ParseMode.MARKDOWN)
    await asyncio.sleep(DAY_DISCUSSION_SEC)

async def _run_vote(bot, game):
    game.phase = "voting"
    game.votes.clear()
    alive = game.alive_players()
    if len(alive) <= 1: return
    rows = [[InlineKeyboardButton(p.full_name, callback_data=f"mv:{game.chat_id}:{p.user_id}")] for p in alive]
    await _send_to_chat(bot, game, f"🗳 *Голосование* 1 мин.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    await asyncio.sleep(VOTE_DURATION_SEC)
    
    valid = {}
    for v_id, t_id in game.votes.items():
        t = game.players.get(t_id)
        if t and t.has_active("blind"): t.consume_active("blind"); continue
        valid[v_id] = t_id
        
    if not valid: return await _send_to_chat(bot, game, "Никто не голосовал.")
    c = Counter(valid.values()).most_common()
    if c[0][1] >= 2 and (len(c) == 1 or c[0][1] > c[1][1]):
        p = game.players[c[0][0]]
        p.alive = False
        await _send_to_chat(bot, game, f"⚖️ Изгнан: {p.full_name} ({ROLE_NAMES[p.role]})")
    else: await _send_to_chat(bot, game, "⚖️ Ничья.")

def _check_win(game):
    m = len(game.alive_mafia()); c = len(game.alive_city())
    if m == 0: game.winner = "city"; return True
    if m >= c: game.winner = "mafia"; return True
    return False

async def _announce_end(bot, game):
    game.phase = "ended"
    header = "🏆 *Победа Жителей!*" if game.winner == "city" else "🏆 *Победа Теней!*"
    
    lines = [header, ""]
    for p in game.players.values():
        status = "жив" if p.alive else "мёртв"
        mark = " (Тень)" if ROLE_TEAM[p.role] == "mafia" else ""
        lines.append(f"• {p.full_name} — *{ROLE_NAMES[p.role]}*{mark} — {status}")
        
        u = db_get_user(p.user_id)
        u['games'] += 1
        reward = 2
        
        won = (game.winner == "city" and ROLE_TEAM[p.role] == "city") or (game.winner == "mafia" and ROLE_TEAM[p.role] == "mafia")
        if won:
            u['wins'] += 1
            reward = 10 if game.winner == "city" else 15
            if "first_win" not in u['achievements']: u['achievements'].append("first_win")
            if game.winner == "mafia" and "mafia_boss" not in u['achievements']: u['achievements'].append("mafia_boss")
            
        u['coins'] += reward
        if u['coins'] >= 100 and "rich" not in u['achievements']: u['achievements'].append("rich")
        if u['games'] >= 10 and "games_10" not in u['achievements']: u['achievements'].append("games_10")
        if u['games'] >= 50 and "games_50" not in u['achievements']: u['achievements'].append("games_50")
        if u['wins'] >= 5 and "wins_5" not in u['achievements']: u['achievements'].append("wins_5")
        if u['wins'] >= 20 and "wins_20" not in u['achievements']: u['achievements'].append("wins_20")
        
        u['level'] = (u['wins'] * 2 + u['games']) // 5 + 1
        if u['level'] >= 5 and "level_5" not in u['achievements']: u['achievements'].append("level_5")
        if u['level'] >= 10 and "level_10" not in u['achievements']: u['achievements'].append("level_10")
        
        db_save_user(p.user_id, **u)
        
        try: await _safe_send(bot, p.user_id, f"🍁 *Игра окончена!*\nТы {'выиграл' if won else 'проиграл'}.\nНаграда: *+{reward}🍁*", parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(p.user_id, game.chat_id))
        except: pass

    await _send_to_chat(bot, game, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data=f"openmenu:{game.chat_id}")]]))

async def cb_night_action(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, target_id = int(parts[1]), int(parts[2])
    game = GAMES.get(chat_id)
    if not game or game.phase != "night": return await q.answer("Не ночь", show_alert=True)
    if q.from_user.id in game.night_actions: return await q.answer("Ты уже ходил", show_alert=True)
    target = game.players.get(target_id)
    if not target: return await q.answer("Нет цели")
    game.night_actions[q.from_user.id] = target_id
    await q.answer(f"Выбран: {target.full_name}")
    try: await q.edit_message_text(f"🌙 Выбран: {target.full_name}", parse_mode=ParseMode.MARKDOWN)
    except: pass

async def cb_vote(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, target_id = int(parts[1]), int(parts[2])
    game = GAMES.get(chat_id)
    if not game or game.phase != "voting": return await q.answer("Не голосование", show_alert=True)
    p = game.players.get(q.from_user.id)
    if not p or not p.alive or p.is_muted() or q.from_user.id in game.votes: return await q.answer("Нельзя", show_alert=True)
    target = game.players.get(target_id)
    if not target or not target.alive: return await q.answer("Цель недоступна", show_alert=True)
    game.votes[q.from_user.id] = target_id
    await q.answer("Голос принят!")
    await _send_to_chat(ctx.bot, game, f"🗳 *{p.full_name}* голосует против *{target.full_name}*", parse_mode=ParseMode.MARKDOWN)

async def cb_open_menu(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[1])
    except: chat_id = 0
    await ctx.bot.send_message(q.from_user.id, _menu_text(q.from_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_main(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    await q.edit_message_text(_menu_text(q.from_user.id), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_profile(update, ctx):
    q = update.callback_query
    if not q: return
    uid = q.from_user.id
    u = db_get_user(uid)
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    text = f"👤 *Профиль*\n\n🍁 Листики: *{u['coins']}*\n⭐ Уровень: *{u['level']}*\n👑 Титул: *{u['title']}*\n🏆 Побед: *{u['wins']}* / Игр: *{u['games']}*\n\n🎒 Инвентарь: `{len(u['inventory'])}` предм.\n🏅 Достижений: `{len(u['achievements'])}`"
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_ach(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    u = db_get_user(q.from_user.id)
    lines = ["🏅 *Достижения*", ""]
    for k, v in ALL_ACHIEVEMENTS.items():
        lines.append(("✅ " if k in u['achievements'] else "❌ ") + v)
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_titles(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    u = db_get_user(q.from_user.id)
    current_title = u['title']
    ach_count = len(u['achievements'])
    
    text = f"👑 *Титулы*\n\nТекущий: *{current_title}*\nТвои достижения: `{ach_count}`\n\n"
    rows = []
    
    for tid, tdata in TITLES_SHOP.items():
        status = ""
        if tdata['name'] == current_title:
            status = " (Надет)"
        elif tdata['ach_req'] > 0:
            if ach_count >= tdata['ach_req']:
                status = f" (Доступно за {tdata['ach_req']} ачивок)"
            else:
                status = f" (Нужно {tdata['ach_req']} ачивок)"
        else:
            status = f" — {tdata['price']}🍁"
        
        rows.append([InlineKeyboardButton(f"{tdata['emoji']} {tdata['name']}{status}", callback_data=f"title:set:{chat_id}:{tid}")])
    
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_title_set(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, tid = int(parts[2]), parts[3]
    tdata = TITLES_SHOP.get(tid)
    if not tdata: return await q.answer("Нет такого титула")
    
    u = db_get_user(q.from_user.id)
    if tdata['name'] == u['title']: return await q.answer("У тебя уже надет этот титул")
    
    if tdata['ach_req'] > 0:
        if len(u['achievements']) < tdata['ach_req']:
            return await q.answer(f"Нужно {tdata['ach_req']} достижений!", show_alert=True)
    else:
        if u['coins'] < tdata['price']:
            return await q.answer("Недостаточно листиков!", show_alert=True)
        u['coins'] -= tdata['price']
        
    db_save_user(q.from_user.id, coins=u['coins'], title=tdata['name'])
    await q.answer(f"Титул изменён на {tdata['name']}!")
    await cb_menu_titles(update, ctx)

async def cb_menu_shop(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    u = db_get_user(q.from_user.id)
    rows = []
    for iid, item in SHOP_ITEMS.items():
        rows.append([InlineKeyboardButton(f"{item['emoji']} {item['name']} — {item['price']}🍁", callback_data=f"shop:item:{chat_id}:{iid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(f"🛒 *Магазин*\n\nБаланс: *{u['coins']}🍁*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_shop_item(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    item = SHOP_ITEMS.get(iid)
    if not item: return await q.answer("Нет предмета")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Купить", callback_data=f"shop:buy:{chat_id}:{iid}")], [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:shop:{chat_id}")]])
    await q.edit_message_text(f"{item['emoji']} *{item['name']}*\n{item['desc']}\nЦена: *{item['price']}🍁*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await q.answer()

async def cb_shop_buy(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    u = db_get_user(q.from_user.id)
    item = SHOP_ITEMS.get(iid)
    if u['coins'] < item['price']: return await q.answer("Мало листиков!", show_alert=True)
    u['coins'] -= item['price']
    u['inventory'][iid] = u['inventory'].get(iid, 0) + 1
    db_save_user(q.from_user.id, **u)
    await q.answer("Куплено!")
    try: await q.edit_message_text(f"✅ Куплено: {item['name']}\nОстаток: {u['coins']}🍁", reply_markup=_menu_back_kb(chat_id))
    except: pass

async def cb_menu_inv(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    u = db_get_user(q.from_user.id)
    rows = []
    for iid, cnt in u['inventory'].items():
        if iid in SHOP_ITEMS and cnt > 0:
            rows.append([InlineKeyboardButton(f"{SHOP_ITEMS[iid]['emoji']} {SHOP_ITEMS[iid]['name']} x{cnt}", callback_data=f"inv:item:{chat_id}:{iid}")])
    if not rows: rows.append([InlineKeyboardButton("— пусто —", callback_data="noop")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text("🎒 *Инвентарь*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_inv_item(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    item = SHOP_ITEMS.get(iid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Сейчас", callback_data=f"inv:use:{chat_id}:{iid}:now")],
        [InlineKeyboardButton("🎯 На матч", callback_data=f"inv:use:{chat_id}:{iid}:match")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:inv:{chat_id}")]
    ])
    await q.edit_message_text(f"{item['emoji']} *{item['name']}*\n\nИспользовать?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await q.answer()

async def cb_inv_use(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid, mode = int(parts[2]), parts[3], parts[4]
    u = db_get_user(q.from_user.id)
    if u['inventory'].get(iid, 0) <= 0: return await q.answer("Нет предмета")
    u['inventory'][iid] -= 1
    if u['inventory'][iid] == 0: del u['inventory'][iid]
    
    if iid == "cookie":
        game = GAMES.get(chat_id)
        if game:
            targets = [p for p in game.alive_players() if p.user_id != q.from_user.id]
            if targets:
                t = random.choice(targets)
                await _safe_send(ctx.bot, q.from_user.id, f"🍪 Предсказание: {t.full_name} — {ROLE_NAMES[t.role]}")
        db_save_user(q.from_user.id, **u)
        await q.answer("Готово")
        try: await q.edit_message_text("🍪 Предсказание отправлено.", reply_markup=_menu_back_kb(chat_id))
        except: pass
        return
        
    db_save_user(q.from_user.id, **u)
    
    game = GAMES.get(chat_id)
    if game and q.from_user.id in game.players:
        game.players[q.from_user.id].active_items[iid] = mode
        
    await q.answer("Активировано!")
    try: await q.edit_message_text(f"✅ Активировано: {SHOP_ITEMS[iid]['name']} ({mode})", reply_markup=_menu_back_kb(chat_id))
    except: pass

async def cb_menu_bonus(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    u = db_get_user(q.from_user.id)
    if time.time() - u['last_bonus'] < 86400: return await q.answer("Бонус раз в сутки!", show_alert=True)
    u['coins'] += 5
    u['last_bonus'] = time.time()
    db_save_user(q.from_user.id, **u)
    await q.answer("+5🍁")
    await q.edit_message_text(f"🍁 *Бонус* +5🍁\nБаланс: {u['coins']}", parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_back_kb(chat_id))

async def cb_menu_top(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT username, coins FROM users_data ORDER BY coins DESC LIMIT 10").fetchall()
    lines = ["🏆 *Топ богачей*:", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['username'] or 'Аноним'} — {r['coins']}🍁")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_admin_main(update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 Выдать листики", callback_data="admin:act:give_coins")],
        [InlineKeyboardButton("⭐ Выдать уровень", callback_data="admin:act:give_level")],
        [InlineKeyboardButton("👑 Выдать титул", callback_data="admin:act:give_title")],
        [InlineKeyboardButton("👥 Список юзеров", callback_data="admin:users")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main:0")]
    ])
    await q.edit_message_text("🛠 *Админ-панель*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await q.answer()

async def cb_admin_act(update, ctx):
    q = update.callback_query
    act = q.data.split(":")[2]
    ADMIN_STATE[q.from_user.id] = act
    prompts = {
        "give_coins": "Отправь: `ID количество`",
        "give_level": "Отправь: `ID уровень`",
        "give_title": "Отправь: `ID Титул`"
    }
    await q.edit_message_text(f"🛠 {prompts[act]}", parse_mode=ParseMode.MARKDOWN)
    await q.answer()

async def cb_admin_users(update, ctx):
    q = update.callback_query
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, level FROM users_data").fetchall()
    lines = ["👥 *Юзеры*:"]
    for r in rows[:50]:
        lines.append(f"`{r['user_id']}` {r['username']} (lvl {r['level']})")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=_menu_back_kb(0))
    await q.answer()

async def cb_croc_new(update, ctx):
    q = update.callback_query
    if q.message.chat.id in CROC_GAME: return await q.answer("Слово уже загадано!", show_alert=True)
    CROC_AWAITING[q.from_user.id] = q.message.chat.id
    await q.answer("Напиши мне в ЛС слово и описание!", show_alert=True)
    try: await ctx.bot.send_message(q.from_user.id, "🐊 Отправь: `Слово | Описание`", parse_mode=ParseMode.MARKDOWN)
    except: pass

async def croc_guess_mw(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type == "private" or not _check_thread(update): return
    game = CROC_GAME.get(msg.chat.id)
    if not game or not game.get("word") or not msg.text: return
    
    if msg.text.strip().lower() == game["word"].lower():
        CROC_GAME.pop(msg.chat.id, None)
        u = db_get_user(msg.from_user.id)
        u['coins'] += 10
        if "croc_master" not in u['achievements']: u['achievements'].append("croc_master")
        db_save_user(msg.from_user.id, **u)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Загадать слово", callback_data="croc:new")]])
        await msg.reply_text(f"🎉 *{msg.from_user.full_name}* угадал слово *{game['word']}*!\nНаграда: +10🍁\n\nЗагадай следующее:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        raise ApplicationHandlerStop

async def cb_noop(update, ctx):
    await update.callback_query.answer()

def _build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    g = filters.ChatType.GROUPS

    app.add_handler(MessageHandler(g, activation_mw), group=-300)
    app.add_handler(MessageHandler(g, guard_muted_messages), group=-100)
    app.add_handler(MessageHandler(g, croc_guess_mw), group=-50)

    app.add_handler(ChatMemberHandler(on_bot_added, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(g & filters.TEXT & filters.Regex(ACTIVATION_REGEX), cmd_activate))
    app.add_handler(CommandHandler("activate", cmd_activate, filters=g))

    app.add_handler(MessageHandler(g & filters.TEXT & filters.Regex(MAFIA_START_REGEX), cmd_mafia_start))
    app.add_handler(MessageHandler(g & filters.TEXT & filters.Regex(MAFIA_STOP_REGEX), cmd_mafia_stop))
    app.add_handler(CommandHandler("mafia", cmd_mafia_start, filters=g))
    app.add_handler(CommandHandler("mafia_stop", cmd_mafia_stop, filters=g))
    
    app.add_handler(CommandHandler("crocodile", cmd_crocodile, filters=g))
    app.add_handler(MessageHandler(g & filters.TEXT & filters.Regex(CROCODILE_REGEX), cmd_crocodile))

    app.add_handler(CommandHandler("start", cmd_dm_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("menu", cmd_dm_menu, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, cmd_dm_other))

    app.add_handler(CallbackQueryHandler(cb_start_count, pattern=r"^ms:"))
    app.add_handler(CallbackQueryHandler(cb_join, pattern=r"^mj$"))
    app.add_handler(CallbackQueryHandler(cb_night_action, pattern=r"^mn:"))
    app.add_handler(CallbackQueryHandler(cb_vote, pattern=r"^mv:"))
    
    app.add_handler(CallbackQueryHandler(cb_open_menu, pattern=r"^openmenu:"))
    app.add_handler(CallbackQueryHandler(cb_menu_main, pattern=r"^menu:main:"))
    app.add_handler(CallbackQueryHandler(cb_menu_profile, pattern=r"^menu:profile:"))
    app.add_handler(CallbackQueryHandler(cb_menu_ach, pattern=r"^menu:ach:"))
    app.add_handler(CallbackQueryHandler(cb_menu_titles, pattern=r"^menu:titles:"))
    app.add_handler(CallbackQueryHandler(cb_title_set, pattern=r"^title:set:"))
    app.add_handler(CallbackQueryHandler(cb_menu_shop, pattern=r"^menu:shop:"))
    app.add_handler(CallbackQueryHandler(cb_menu_inv, pattern=r"^menu:inv:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top, pattern=r"^menu:top:"))
    app.add_handler(CallbackQueryHandler(cb_menu_bonus, pattern=r"^menu:bonus:"))
    
    app.add_handler(CallbackQueryHandler(cb_shop_item, pattern=r"^shop:item:"))
    app.add_handler(CallbackQueryHandler(cb_shop_buy, pattern=r"^shop:buy:"))
    app.add_handler(CallbackQueryHandler(cb_inv_item, pattern=r"^inv:item:"))
    app.add_handler(CallbackQueryHandler(cb_inv_use, pattern=r"^inv:use:"))
    
    app.add_handler(CallbackQueryHandler(cb_admin_main, pattern=r"^admin:main$"))
    app.add_handler(CallbackQueryHandler(cb_admin_act, pattern=r"^admin:act:"))
    app.add_handler(CallbackQueryHandler(cb_admin_users, pattern=r"^admin:users$"))
    
    app.add_handler(CallbackQueryHandler(cb_croc_new, pattern=r"^croc:new$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    return app

async def _run_bot():
    app = _build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    await asyncio.Event().wait()

def main():
    try:
        asyncio.run(_run_bot())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
