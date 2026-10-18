import asyncio
import html
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
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

SELLER_USERNAME = (os.getenv("SELLER_USERNAME") or "@admin").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mafia")

DB_DIR = Path(os.getenv("DATA_DIR", "."))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "mafia.db")

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
    "novice": {"name": "Новичок", "emoji": "🐣", "price": 0, "ach_req": 0, "daily_bonus": 5},
    "leaf_collector": {"name": "Собиратель листвы", "emoji": "🍁", "price": 50, "ach_req": 0, "daily_bonus": 7},
    "guardian": {"name": "Страж Клена", "emoji": "🌳", "price": 150, "ach_req": 0, "daily_bonus": 12},
    "king": {"name": "Осенний Король", "emoji": "👑", "price": 400, "ach_req": 0, "daily_bonus": 25},
    "donater": {"name": "Меценат", "emoji": "💎", "price": 700, "ach_req": 0, "daily_bonus": 40},
    "rich_boy": {"name": "Золотой Скрудж", "emoji": "💰", "price": 1200, "ach_req": 0, "daily_bonus": 70},
    "detective": {"name": "Детектив", "emoji": "🕵️", "price": 0, "ach_req": 5, "daily_bonus": 15},
    "veteran": {"name": "Ветеран", "emoji": "🎖", "price": 0, "ach_req": 10, "daily_bonus": 25},
    "legend": {"name": "Легенда", "emoji": "🌟", "price": 0, "ach_req": 15, "daily_bonus": 45},
    "god": {"name": "Божество Клена", "emoji": "🌌", "price": 0, "ach_req": 20, "daily_bonus": 80},
}

ALL_ACHIEVEMENTS = {
    "first_win": "🍁 Первая победа",
    "rich_100": "💰 Богач (100🍁)",
    "rich_1000": "💎 Олигарх (1000🍁)",
    "games_10": "🎮 10 сыгранных игр",
    "games_50": "🎮 50 сыгранных игр",
    "games_100": "🎮 100 сыгранных игр",
    "wins_5": "🏆 5 побед",
    "wins_20": "🏆 20 побед",
    "wins_50": "🏆 50 побед",
    "croc_1": "🐊 Первый крокодил",
    "croc_10": "🐊 Знаток слов (10)",
    "croc_50": "🐊 Крокодилий бог (50)",
    "mafia_1": "🔪 Первая тень",
    "mafia_10": "🔪 Босс мафии (10)",
    "city_1": "🕵️ Первый мирный",
    "city_10": "🕵️ Защитник города (10)",
    "level_5": "⭐ Достигнут 5 уровень",
    "level_10": "🌟 Достигнут 10 уровень",
    "level_20": "✨ Достигнут 20 уровень"
}

ACTIVATION_REGEX = re.compile(r"(?i)^(?:/)?(?:активация|activate)(?:\s+\S+)?$")
MAFIA_START_REGEX = re.compile(r"(?i)^мафия\s+(?:начать|старт)$")
MAFIA_STOP_REGEX = re.compile(r"(?i)^мафия\s+(?:стоп|stop)$")
CROCODILE_REGEX = re.compile(r"(?i)^/?(?:крокодил|слова|crocodile|words)(?:\s+(?:начать|старт))?$")
CROCODILE_STOP_REGEX = re.compile(r"(?i)^/?(?:крокодил|слова|crocodile|words)\s+(?:стоп|stop)$")

CROC_GAME = {}
CROC_AWAITING = {}
CROC_ACTIVE = set()
ADMIN_STATE = {}
CASINO_AWAITING = set()
GAMES = {}


def _init_db():
    with sqlite3.connect(DB_PATH, timeout=15.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activations (chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS pending_activations (chat_id INTEGER PRIMARY KEY, key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT);
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
                unlocked_titles TEXT DEFAULT '["Новичок"]',
                last_bonus REAL DEFAULT 0,
                croc_wins INTEGER DEFAULT 0,
                mafia_wins INTEGER DEFAULT 0,
                city_wins INTEGER DEFAULT 0
            );
        """)
        
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users_data)")}
        if "unlocked_titles" not in cols:
            conn.execute("ALTER TABLE users_data ADD COLUMN unlocked_titles TEXT DEFAULT '[\"Новичок\"]'")
        if "croc_wins" not in cols:
            conn.execute("ALTER TABLE users_data ADD COLUMN croc_wins INTEGER DEFAULT 0")
        if "mafia_wins" not in cols:
            conn.execute("ALTER TABLE users_data ADD COLUMN mafia_wins INTEGER DEFAULT 0")
        if "city_wins" not in cols:
            conn.execute("ALTER TABLE users_data ADD COLUMN city_wins INTEGER DEFAULT 0")
            
        conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('casino_chance', '45')")
        conn.commit()

_init_db()

def db_get_setting(key, default=""):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

def db_set_setting(key, value):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()

def db_get_user(user_id, tg_user=None):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO users_data (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
            
        if tg_user:
            name = tg_user.username or tg_user.full_name or f"Игрок {user_id}"
            if row['username'] != name:
                conn.execute("UPDATE users_data SET username=? WHERE user_id=?", (name, user_id))
                conn.commit()
                row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
                
        d = dict(row)
        d['inventory'] = json.loads(d['inventory'] or '{}')
        d['achievements'] = json.loads(d['achievements'] or '[]')
        d['unlocked_titles'] = json.loads(d.get('unlocked_titles') or '["Новичок"]')
        d['coins'] = int(d.get('coins') or 0)
        d['wins'] = int(d.get('wins') or 0)
        d['games'] = int(d.get('games') or 0)
        d['last_bonus'] = float(d.get('last_bonus') or 0.0)
        d['croc_wins'] = int(d.get('croc_wins') or 0)
        d['mafia_wins'] = int(d.get('mafia_wins') or 0)
        d['city_wins'] = int(d.get('city_wins') or 0)
        
        total_xp = d['wins'] * 2 + d['games'] + d['croc_wins']
        calc_level = total_xp // 5 + 1
        stored_level = int(d.get('level') or 1)
        d['level'] = max(calc_level, stored_level)
        
        if d['title'] not in d['unlocked_titles']:
            d['unlocked_titles'].append(d['title'])
        return d

def db_save_user(uid, **kwargs):
    u = db_get_user(uid)
    for k, v in kwargs.items():
        if k in u:
            u[k] = v
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("""
            UPDATE users_data SET 
                username=?, coins=?, level=?, title=?, wins=?, games=?, inventory=?, achievements=?, unlocked_titles=?, last_bonus=?, croc_wins=?, mafia_wins=?, city_wins=?
            WHERE user_id=?
        """, (
            u['username'], u['coins'], u['level'], u['title'], u['wins'], u['games'],
            json.dumps(u['inventory']), json.dumps(u['achievements']), json.dumps(u['unlocked_titles']), u['last_bonus'],
            u['croc_wins'], u['mafia_wins'], u['city_wins'], uid
        ))
        conn.commit()

def _check_rich_achievements(uid):
    u = db_get_user(uid)
    changed = False
    if u['coins'] >= 100 and "rich_100" not in u['achievements']:
        u['achievements'].append("rich_100")
        changed = True
    if u['coins'] >= 1000 and "rich_1000" not in u['achievements']:
        u['achievements'].append("rich_1000")
        changed = True
    if changed:
        db_save_user(uid, achievements=u['achievements'])

def _check_level_achievements(uid):
    u = db_get_user(uid)
    changed = False
    if u['level'] >= 5 and "level_5" not in u['achievements']:
        u['achievements'].append("level_5")
        changed = True
    if u['level'] >= 10 and "level_10" not in u['achievements']:
        u['achievements'].append("level_10")
        changed = True
    if u['level'] >= 20 and "level_20" not in u['achievements']:
        u['achievements'].append("level_20")
        changed = True
    if changed:
        db_save_user(uid, achievements=u['achievements'])

def is_chat_activated(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        return conn.execute("SELECT 1 FROM activations WHERE chat_id=?", (chat_id,)).fetchone() is not None

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

def get_safe_name(user_or_player):
    name = getattr(user_or_player, 'full_name', str(user_or_player))
    if not name or not str(name).strip():
        return "Игрок"
    return html.escape(str(name))

def get_roles(count):
    spec = ROLE_DISTRIBUTION.get(count)
    return [_ROLE_MAP[t] for t in spec.split()] if spec else None

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
    except Exception: 
        return False

async def _send_to_chat(bot, game, text, **kwargs):
    if game.thread_id:
        kwargs["message_thread_id"] = game.thread_id
    return await bot.send_message(game.chat_id, text, **kwargs)

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


def _menu_main_kb(user_id, chat_id=0):
    rows = [
        [InlineKeyboardButton("👤 Профиль", callback_data=f"menu:profile:{chat_id}"), InlineKeyboardButton("🛒 Магазин", callback_data=f"menu:shop:{chat_id}")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data=f"menu:inv:{chat_id}"), InlineKeyboardButton("🏆 Топ", callback_data=f"menu:top:{chat_id}")],
        [InlineKeyboardButton("🏅 Достижения", callback_data=f"menu:ach:{chat_id}"), InlineKeyboardButton("🍁 Бонус", callback_data=f"menu:bonus:{chat_id}")],
        [InlineKeyboardButton("👑 Титулы", callback_data=f"menu:titles:{chat_id}"), InlineKeyboardButton("🎰 Казино", callback_data=f"menu:casino:{chat_id}")],
        [InlineKeyboardButton("💎 Купить 🍁", callback_data=f"menu:buy:{chat_id}")]
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)

def _menu_back_kb(chat_id=0):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]])

def _menu_text(uid, tg_user=None):
    u = db_get_user(uid, tg_user)
    return f"🍁 <b>Меню</b>\n\nБаланс: <b>{u['coins']}🍁</b>\nУровень: <b>{u['level']}</b>\nТитул: <b>{u['title']}</b>"


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
    
    alphabet = string.ascii_uppercase + string.digits
    key = "".join(secrets.choice(alphabet) for _ in range(12))
    set_pending_key(cm.chat.id, key)
    
    log.warning("=== KEY: %s for chat %s (%s) ===", key, cm.chat.title, cm.chat.id)
    await ctx.bot.send_message(cm.chat.id, "🍁 Для активации введите:\n<code>активация КЛЮЧ</code>", parse_mode=ParseMode.HTML)

async def cmd_activate(update, ctx):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or msg.chat.type == "private" or is_chat_activated(msg.chat.id): return
    
    parts = (msg.text or "").lower().lstrip("/").split()
    if len(parts) < 2:
        if await _is_chat_admin(ctx.bot, msg.chat.id, user.id):
            key = get_pending_key(msg.chat.id)
            if not key:
                alphabet = string.ascii_uppercase + string.digits
                key = "".join(secrets.choice(alphabet) for _ in range(12))
                set_pending_key(msg.chat.id, key)
            log.warning("=== KEY: %s for chat %s (%s) ===", key, msg.chat.title, msg.chat.id)
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
        try: 
            await msg.delete()
        except Exception: 
            pass
        raise ApplicationHandlerStop


async def cmd_dm_start(update, ctx):
    msg = update.effective_message
    if msg:
        await msg.reply_text(_menu_text(msg.from_user.id, msg.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(msg.from_user.id))

async def cmd_dm_menu(update, ctx):
    msg = update.effective_message
    if msg:
        await msg.reply_text(_menu_text(msg.from_user.id, msg.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(msg.from_user.id))

async def cmd_dm_other(update, ctx):
    msg = update.effective_message
    uid = update.effective_user.id
    db_get_user(uid, update.effective_user)
    
    if uid in ADMIN_STATE:
        action = ADMIN_STATE.pop(uid)
        parts = msg.text.split(maxsplit=2)
        
        try:
            if action == "give_coins" and len(parts) >= 2:
                target, amount = int(parts[0].replace("@", "")), int(parts[1])
                u = db_get_user(target)
                db_save_user(target, coins=u['coins']+amount)
                _check_rich_achievements(target)
                await msg.reply_text(f"✅ Выдано {amount}🍁 юзеру {target}", reply_markup=_menu_main_kb(uid))
            elif action == "give_level" and len(parts) >= 2:
                target, lvl = int(parts[0].replace("@", "")), int(parts[1])
                db_save_user(target, level=lvl)
                _check_level_achievements(target)
                await msg.reply_text(f"✅ Выдан уровень {lvl} юзеру {target}", reply_markup=_menu_main_kb(uid))
            elif action == "give_title" and len(parts) >= 2:
                target = int(parts[0].replace("@", ""))
                title = " ".join(parts[1:])
                u = db_get_user(target)
                if title not in u['unlocked_titles']:
                    u['unlocked_titles'].append(title)
                db_save_user(target, title=title, unlocked_titles=u['unlocked_titles'])
                await msg.reply_text(f"✅ Выдан титул '{title}' юзеру {target}", reply_markup=_menu_main_kb(uid))
            elif action == "casino_chance" and len(parts) >= 1:
                chance = int(parts[0])
                if chance < 1 or chance > 99: raise ValueError
                db_set_setting("casino_chance", str(chance))
                await msg.reply_text(f"✅ Шанс победы в казино теперь: {chance}%", reply_markup=_menu_main_kb(uid))
        except Exception:
            await msg.reply_text("❌ Ошибка. Проверь формат ввода.", reply_markup=_menu_main_kb(uid))
        return
        
    if uid in CROC_AWAITING:
        chat_id = CROC_AWAITING.pop(uid)
        if "|" in msg.text:
            word, desc = msg.text.split("|", 1)
            CROC_GAME[chat_id] = {"word": word.strip(), "desc": desc.strip(), "author": uid}
            try:
                await ctx.bot.send_message(chat_id, f"🐊 <b>Крокодил!</b>\n\nОписание: {html.escape(desc.strip())}\n\nКто угадает слово, пишите в чат!", message_thread_id=THREAD_ID or 0, parse_mode=ParseMode.HTML)
                await msg.reply_text("✅ Слово загадано!")
            except Exception:
                pass
        else:
            await msg.reply_text("❌ Ошибка. Пиши: <code>Слово | Описание</code>", parse_mode=ParseMode.HTML)
        return

    if uid in CASINO_AWAITING:
        try:
            bet = int(msg.text.strip())
            if bet < 1: raise ValueError
        except ValueError:
            await msg.reply_text("❌ Неверная сумма. Введи целое число больше 0.", reply_markup=_menu_back_kb(0))
            return
            
        u = db_get_user(uid, msg.from_user)
        if u['coins'] < bet:
            await msg.reply_text(f"❌ У тебя только {u['coins']}🍁.", reply_markup=_menu_back_kb(0))
            return
            
        chance = int(db_get_setting("casino_chance", "45"))
        u['coins'] -= bet
        
        if random.randint(1, 100) <= chance:
            win = bet * 2
            u['coins'] += win
            db_save_user(uid, **u)
            _check_rich_achievements(uid)
            await msg.reply_text(f"🎆🎇🎉 <b>ПОБЕДА!</b> 🎉🎇🎆\n\nТы выиграл <b>{win}🍁</b>!\nТвой баланс: <b>{u['coins']}🍁</b>\n\n<i>Можешь ввести следующую ставку!</i>", parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(0))
        else:
            db_save_user(uid, **u)
            await msg.reply_text(f"💀 <b>Проигрыш...</b>\n\nТы потерял <b>{bet}🍁</b>.\nТвой баланс: <b>{u['coins']}🍁</b>\n\n<i>Можешь ввести следующую ставку!</i>", parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(0))
        return
        
    await msg.reply_text(_menu_text(uid, update.effective_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(uid))


async def cmd_mafia_start(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("⛔ Только администратор чата может запустить игру.")
    
    if msg.chat.id in GAMES: 
        return await msg.reply_text("⚠️ Игра уже идёт.")
        
    buttons = [InlineKeyboardButton(str(i), callback_data=f"ms:{i}") for i in range(5, 21)]
    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    await msg.reply_text("🍁 <b>Кленовая мафия</b>\nВыбери количество:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

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
        
    CROC_ACTIVE.add(msg.chat.id)
    await msg.reply_text("🐊 <b>Крокодил</b>\nНажми кнопку, чтобы загадать слово.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Загадать слово", callback_data="croc:new")]]))

async def cmd_crocodile_stop(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id):
        return await msg.reply_text("⛔ Только администратор чата может остановить игру.")
        
    if msg.chat.id in CROC_GAME or msg.chat.id in CROC_ACTIVE:
        CROC_ACTIVE.discard(msg.chat.id)
        CROC_GAME.pop(msg.chat.id, None)
        await msg.reply_text("🛑 Игра в Крокодила остановлена. Все старые кнопки теперь неактивны.")
    else:
        await msg.reply_text("ℹ️ В этом чате нет активной игры в Крокодила.")
        
    to_remove = [uid for uid, cid in CROC_AWAITING.items() if cid == msg.chat.id]
    for uid in to_remove:
        del CROC_AWAITING[uid]


async def cb_start_count(update, ctx):
    q = update.callback_query
    if q is None or not _check_thread(update): return await q.answer("Не та ветка", show_alert=True)
    if q.message.chat.id in GAMES: return await q.answer("Игра уже идёт", show_alert=True)
    
    count = int(q.data.split(":")[1])
    game = Game(chat_id=q.message.chat.id, thread_id=q.message.message_thread_id or 0, total=count)
    game.join_message_id = q.message.message_id
    GAMES[game.chat_id] = game
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть (0/{count})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    await q.edit_message_text(f"🍁 <b>Набор</b>\nНужно: <b>{count}</b>\nНабрано: <b>0/{count}</b>\n\n❗ Каждому нажать /start в ЛС бота.", parse_mode=ParseMode.HTML, reply_markup=kb)
    game.task = asyncio.create_task(_join_timeout(ctx.bot, game.chat_id))

async def _join_timeout(bot, chat_id):
    await asyncio.sleep(300)
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
    
    try: 
        await ctx.bot.send_message(user.id, "🍁 Ты в игре!")
    except Exception: 
        return await q.answer("Сначала нажми /start в ЛС!", show_alert=True)
    
    u = db_get_user(user.id, user)
    p = Player(user_id=user.id, username=user.username or "", full_name=user.full_name)
    p.coins, p.items, p.achievements, p.wins, p.games = u['coins'], u['inventory'], u['achievements'], u['wins'], u['games']
    p.level, p.title = u['level'], u['title']
    game.players[user.id] = p
    
    n = len(game.players)
    if n >= game.total:
        if game.task and not game.task.done(): game.task.cancel()
        await q.answer("Поехали!")
        await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 <b>Набор окончен</b>\nИгроков: <b>{n}/{game.total}</b>", parse_mode=ParseMode.HTML)
        await _start_game(ctx.bot, game)
        return
        
    names = ", ".join([get_safe_name(p) for p in game.players.values()])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть ({n}/{game.total})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    try:
        await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 <b>Набор</b>\nНужно: <b>{game.total}</b>\nНабрано: <b>{n}/{game.total}</b>\n\n👥 {names}", parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception: 
        pass
    await q.answer(f"В игре ({n}/{game.total})")

async def cb_cancel(update, ctx):
    q = update.callback_query
    if q is None: return
    chat = update.effective_chat
    if not await _is_chat_admin(ctx.bot, chat.id, q.from_user.id):
        return await q.answer("Только администратор", show_alert=True)
        
    game = GAMES.pop(chat.id, None)
    if game is None: return await q.answer()
    
    if game.task and not game.task.done(): game.task.cancel()
    await q.answer("Отменено")
    try:
        await q.edit_message_text("🍁 Игра отменена.")
    except Exception:
        await _send_to_chat(ctx.bot, game, "🍁 Игра отменена.")


async def _start_game(bot, game):
    roles = get_roles(game.total)[:]
    random.shuffle(roles)
    uids = list(game.players.keys())
    random.shuffle(uids)
    for uid, role in zip(uids, roles):
        game.players[uid].role = role
        
    mafia_names = [get_safe_name(p) for p in game.all_mafia()]
    for p in game.players.values():
        teammates = [n for n in mafia_names if n != get_safe_name(p)]
        text = f"🍁 Твоя роль: <b>{ROLE_NAMES[p.role]}</b>\n\n{ROLE_DESCRIPTIONS[p.role]}"
        if p.role == "mafia" and teammates: 
            text += f"\n\n🤝 Союзники: {', '.join(teammates)}"
        try: 
            await bot.send_message(p.user_id, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data="openmenu:0")]]))
        except Exception: 
            pass

    await _send_to_chat(bot, game, f"🍁 <b>Игра началась!</b>\nИгроков: <b>{game.total}</b>\nТеней: <b>{len(game.all_mafia())}</b>", parse_mode=ParseMode.HTML)
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
    except Exception as e:
        log.error(f"Game loop error: {e}")
    finally:
        try:
            await _announce_end(bot, game)
        except Exception as e:
            log.error(f"Announce end error: {e}")
        finally:
            GAMES.pop(game.chat_id, None)

async def _run_night(bot, game):
    game.phase = "night"
    game.night_actions.clear()
    await _send_to_chat(bot, game, f"🌙 <b>Ночь {game.round_num}</b>\nКленовый город засыпает…", parse_mode=ParseMode.HTML)
    await asyncio.sleep(2)
    
    for p in game.alive_players():
        if p.role == "citizen": continue
        targets = [op for op in game.alive_players() if op.user_id != p.user_id]
        if p.role == "mafia": targets = [op for op in game.alive_players() if ROLE_TEAM[op.role] != "mafia"]
        elif p.role == "doctor": targets = game.alive_players()
        elif p.role == "medium": targets = [op for op in game.players.values() if not op.alive]
        
        if not targets: continue
        rows = [[InlineKeyboardButton(get_safe_name(t), callback_data=f"mn:{game.chat_id}:{t.user_id}")] for t in targets]
        try: 
            await bot.send_message(p.user_id, f"🌙 <b>Ночь</b>\nВыбери цель:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: 
            pass
    
    await asyncio.sleep(60)
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
            if t.alive: 
                t.muted_until = time.time() + 300
                msg.append(f"🔇 {get_safe_name(t)} замолчал.")
            
    if cop_t:
        c = next((p for p in game.alive_players() if p.role == "cop"), None)
        t = game.players[cop_t]
        is_m = ROLE_TEAM[t.role] == "mafia"
        if t.has_active("fake"): 
            is_m = False
            t.consume_active("fake")
        if c: await _safe_send(bot, c.user_id, f"🔍 {get_safe_name(t)} — {'ТЕНЬ' if is_m else 'Житель'}")
        
    if med_t:
        m = next((p for p in game.alive_players() if p.role == "medium"), None)
        t = game.players[med_t]
        if m: await _safe_send(bot, m.user_id, f"👻 {get_safe_name(t)} был: {ROLE_NAMES[t.role]}")
        
    if m_votes:
        c = Counter(m_votes).most_common()
        if c[0][1] > 1 or len(c) == 1:
            k = c[0][0]
            v = game.players[k]
            if v.has_active("amulet"): 
                v.consume_active("amulet")
                msg.append(f"🛡 Оберег спас {get_safe_name(v)}!")
            elif k == doc_t: 
                msg.append(f"💉 Лекарь спас {get_safe_name(v)}!")
            else: 
                v.alive = False
                msg.append(f"💀 Убит: {get_safe_name(v)}")
        else: 
            msg.append("Тени не договорились.")
        
    if not msg: msg.append("Спокойная ночь.")
    await _send_to_chat(bot, game, "🌅 <b>Итоги ночи:</b>\n" + "\n".join(msg), parse_mode=ParseMode.HTML)

async def _run_day(bot, game):
    game.phase = "day"
    alive_str = ", ".join([get_safe_name(p) for p in game.alive_players()])
    
    total = game.total
    discuss_sec = 60 if total <= 6 else 90 if total <= 9 else 120 if total <= 13 else 150 if total <= 17 else 180
    m = discuss_sec // 60
    s = discuss_sec % 60
    time_str = f"{m} мин" if s == 0 else f"{m} мин {s} сек"
    
    await _send_to_chat(bot, game, f"☀️ <b>День {game.round_num}</b>\nЖивы: {alive_str}\nОбсуждение — {time_str}.", parse_mode=ParseMode.HTML)
    await asyncio.sleep(discuss_sec)

async def _run_vote(bot, game):
    game.phase = "voting"
    game.votes.clear()
    alive = game.alive_players()
    if len(alive) <= 1: return
    
    rows = [[InlineKeyboardButton(get_safe_name(p), callback_data=f"mv:{game.chat_id}:{p.user_id}")] for p in alive]
    await _send_to_chat(bot, game, f"🗳 <b>Голосование</b> 1 мин.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await asyncio.sleep(60)
    
    valid = {}
    for v_id, t_id in game.votes.items():
        t = game.players.get(t_id)
        if t and t.has_active("blind"): 
            t.consume_active("blind")
            continue
        valid[v_id] = t_id
        
    if not valid: return await _send_to_chat(bot, game, "Никто не голосовал.")
    c = Counter(valid.values()).most_common()
    if c[0][1] >= 2 and (len(c) == 1 or c[0][1] > c[1][1]):
        p = game.players[c[0][0]]
        p.alive = False
        await _send_to_chat(bot, game, f"⚖️ Изгнан: {get_safe_name(p)} (<b>{ROLE_NAMES[p.role]}</b>)", parse_mode=ParseMode.HTML)
    else: 
        await _send_to_chat(bot, game, "⚖️ Ничья.")

def _check_win(game):
    m = len(game.alive_mafia())
    c = len(game.alive_city())
    if m == 0: 
        game.winner = "city"
        return True
    if m >= c: 
        game.winner = "mafia"
        return True
    return False

async def _announce_end(bot, game):
    game.phase = "ended"
    header = "🏆 <b>Победа Жителей!</b>" if game.winner == "city" else "🏆 <b>Победа Теней!</b>"
    
    lines = [header, ""]
    for p in game.players.values():
        status = "жив" if p.alive else "мёртв"
        mark = " (Тень)" if ROLE_TEAM[p.role] == "mafia" else ""
        lines.append(f"• {get_safe_name(p)} — <b>{ROLE_NAMES[p.role]}</b>{mark} — {status}")
        
        u = db_get_user(p.user_id)
        old_lvl = (u['wins'] * 2 + u['games'] + u['croc_wins']) // 5 + 1
        u['games'] += 1
        reward = 1
        
        won = (game.winner == "city" and ROLE_TEAM[p.role] == "city") or (game.winner == "mafia" and ROLE_TEAM[p.role] == "mafia")
        if won:
            u['wins'] += 1
            reward = 5 if game.winner == "city" else 8
            if "first_win" not in u['achievements']: u['achievements'].append("first_win")
            
            if game.winner == "mafia":
                u['mafia_wins'] += 1
                if "mafia_1" not in u['achievements']: u['achievements'].append("mafia_1")
                if u['mafia_wins'] >= 10 and "mafia_10" not in u['achievements']: u['achievements'].append("mafia_10")
            else:
                u['city_wins'] += 1
                if "city_1" not in u['achievements']: u['achievements'].append("city_1")
                if u['city_wins'] >= 10 and "city_10" not in u['achievements']: u['achievements'].append("city_10")
            
        u['coins'] += reward
        xp_gained = 3 if won else 1
        
        calc = (u['wins'] * 2 + u['games'] + u['croc_wins']) // 5 + 1
        new_lvl = max(calc, int(u.get('level') or 1))
        u['level'] = new_lvl
        
        if u['coins'] >= 100 and "rich_100" not in u['achievements']: u['achievements'].append("rich_100")
        if u['coins'] >= 1000 and "rich_1000" not in u['achievements']: u['achievements'].append("rich_1000")
        if u['games'] >= 10 and "games_10" not in u['achievements']: u['achievements'].append("games_10")
        if u['games'] >= 50 and "games_50" not in u['achievements']: u['achievements'].append("games_50")
        if u['games'] >= 100 and "games_100" not in u['achievements']: u['achievements'].append("games_100")
        if u['wins'] >= 5 and "wins_5" not in u['achievements']: u['achievements'].append("wins_5")
        if u['wins'] >= 20 and "wins_20" not in u['achievements']: u['achievements'].append("wins_20")
        if u['wins'] >= 50 and "wins_50" not in u['achievements']: u['achievements'].append("wins_50")
        if new_lvl >= 5 and "level_5" not in u['achievements']: u['achievements'].append("level_5")
        if new_lvl >= 10 and "level_10" not in u['achievements']: u['achievements'].append("level_10")
        if new_lvl >= 20 and "level_20" not in u['achievements']: u['achievements'].append("level_20")
        
        db_save_user(p.user_id, **u)
        
        msg_text = f"🍁 <b>Игра окончена!</b>\nТы {'выиграл' if won else 'проиграл'}.\nНаграда: <b>+{reward}🍁</b>\nОпыт: <b>+{xp_gained} XP</b>"
        if new_lvl > old_lvl:
            msg_text += f"\n⭐ <b>Уровень повышен до {new_lvl}!</b>"

        try: 
            await _safe_send(bot, p.user_id, msg_text, parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(p.user_id, game.chat_id))
        except Exception: 
            pass

    await _send_to_chat(bot, game, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data=f"openmenu:{game.chat_id}")]]))


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
    await q.answer(f"Выбран: {get_safe_name(target)}")
    try: 
        await q.edit_message_text(f"🌙 Выбран: {get_safe_name(target)}", parse_mode=ParseMode.HTML)
    except Exception: 
        pass

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
    await _send_to_chat(ctx.bot, game, f"🗳 <b>{get_safe_name(p)}</b> голосует против <b>{get_safe_name(target)}</b>", parse_mode=ParseMode.HTML)


async def cb_open_menu(update, ctx):
    q = update.callback_query
    if not q: return
    CASINO_AWAITING.discard(q.from_user.id)
    ADMIN_STATE.pop(q.from_user.id, None)
    CROC_AWAITING.pop(q.from_user.id, None)
    try: 
        chat_id = int(q.data.split(":")[1])
    except Exception: 
        chat_id = 0
    await ctx.bot.send_message(q.from_user.id, _menu_text(q.from_user.id, q.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_main(update, ctx):
    q = update.callback_query
    if not q: return
    CASINO_AWAITING.discard(q.from_user.id)
    ADMIN_STATE.pop(q.from_user.id, None)
    CROC_AWAITING.pop(q.from_user.id, None)
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    await q.edit_message_text(_menu_text(q.from_user.id, q.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_profile(update, ctx):
    q = update.callback_query
    if not q: return
    u = db_get_user(q.from_user.id, q.from_user)
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    
    total_xp = u['wins'] * 2 + u['games'] + u['croc_wins']
    text = f"👤 <b>Профиль</b>\n\n🍁 Листики: <b>{u['coins']}</b>\n⭐ Уровень: <b>{u['level']}</b> (XP: {total_xp}/{u['level']*5})\n👑 Титул: <b>{u['title']}</b>\n🏆 Побед: <b>{u['wins']}</b> / Игр: <b>{u['games']}</b>\n\n🎒 Инвентарь: <code>{len(u['inventory'])}</code> предм.\n🏅 Достижений: <code>{len(u['achievements'])}</code>"
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_ach(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    lines = ["🏅 <b>Достижения</b>", ""]
    for k, v in ALL_ACHIEVEMENTS.items():
        lines.append(("✅ " if k in u['achievements'] else "❌ ") + v)
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_titles(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    
    text = f"👑 <b>Титулы</b>\n\nТекущий: <b>{u['title']}</b>\nТвои достижения: <code>{len(u['achievements'])}</code>\n\n"
    rows = []
    
    for tid, tdata in TITLES_SHOP.items():
        status = ""
        if tdata['name'] == u['title']:
            status = " (Надет)"
        elif tdata['name'] in u['unlocked_titles']:
            status = " (Доступен)"
        elif tdata['ach_req'] > 0:
            status = f" (За {tdata['ach_req']} ачивок)" if len(u['achievements']) >= tdata['ach_req'] else f" (Нужно {tdata['ach_req']} ачивок)"
        else:
            status = f" — {tdata['price']}🍁"
        
        rows.append([InlineKeyboardButton(f"{tdata['emoji']} {tdata['name']}{status} (+{tdata['daily_bonus']}🍁)", callback_data=f"title:set:{chat_id}:{tid}")])
    
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
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
    
    if tdata['name'] in u['unlocked_titles']:
        db_save_user(q.from_user.id, title=tdata['name'])
        await q.answer(f"Титул изменён на {tdata['name']}!")
        return await cb_menu_titles(update, ctx)
        
    if tdata['ach_req'] > 0:
        if len(u['achievements']) < tdata['ach_req']:
            return await q.answer(f"Нужно {tdata['ach_req']} достижений!", show_alert=True)
    else:
        if u['coins'] < tdata['price']:
            return await q.answer("Недостаточно листиков!", show_alert=True)
        u['coins'] -= tdata['price']
        
    u['unlocked_titles'].append(tdata['name'])
    db_save_user(q.from_user.id, coins=u['coins'], title=tdata['name'], unlocked_titles=u['unlocked_titles'])
    await q.answer(f"Титул куплен и надет!")
    await cb_menu_titles(update, ctx)

async def cb_menu_shop(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    rows = []
    for iid, item in SHOP_ITEMS.items():
        rows.append([InlineKeyboardButton(f"{item['emoji']} {item['name']} — {item['price']}🍁", callback_data=f"shop:item:{chat_id}:{iid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(f"🛒 <b>Магазин</b>\n\nБаланс: <b>{u['coins']}🍁</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_shop_item(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    item = SHOP_ITEMS.get(iid)
    if not item: return await q.answer("Нет предмета")
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Купить", callback_data=f"shop:buy:{chat_id}:{iid}")], [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:shop:{chat_id}")]])
    await q.edit_message_text(f"{item['emoji']} <b>{item['name']}</b>\n{item['desc']}\nЦена: <b>{item['price']}🍁</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_shop_buy(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    u = db_get_user(q.from_user.id, q.from_user)
    item = SHOP_ITEMS.get(iid)
    
    if u['coins'] < item['price']: return await q.answer("Мало листиков!", show_alert=True)
    u['coins'] -= item['price']
    u['inventory'][iid] = u['inventory'].get(iid, 0) + 1
    db_save_user(q.from_user.id, **u)
    
    await q.answer("Куплено!")
    try: 
        await q.edit_message_text(f"✅ Куплено: {item['name']}\nОстаток: {u['coins']}🍁", reply_markup=_menu_back_kb(chat_id))
    except Exception: 
        pass

async def cb_menu_inv(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    rows = []
    for iid, cnt in u['inventory'].items():
        if iid in SHOP_ITEMS and cnt > 0:
            rows.append([InlineKeyboardButton(f"{SHOP_ITEMS[iid]['emoji']} {SHOP_ITEMS[iid]['name']} x{cnt}", callback_data=f"inv:item:{chat_id}:{iid}")])
    if not rows: rows.append([InlineKeyboardButton("— пусто —", callback_data="noop")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text("🎒 <b>Инвентарь</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
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
    await q.edit_message_text(f"{item['emoji']} <b>{item['name']}</b>\n\nИспользовать?", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_inv_use(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid, mode = int(parts[2]), parts[3], parts[4]
    u = db_get_user(q.from_user.id, q.from_user)
    
    if u['inventory'].get(iid, 0) <= 0: return await q.answer("Нет предмета")
    u['inventory'][iid] -= 1
    if u['inventory'][iid] == 0: del u['inventory'][iid]
    
    if iid == "cookie":
        game = GAMES.get(chat_id)
        if game:
            targets = [p for p in game.alive_players() if p.user_id != q.from_user.id]
            if targets:
                t = random.choice(targets)
                await _safe_send(ctx.bot, q.from_user.id, f"🍪 Предсказание: {get_safe_name(t)} — {ROLE_NAMES[t.role]}")
        db_save_user(q.from_user.id, **u)
        await q.answer("Готово")
        try: 
            await q.edit_message_text("🍪 Предсказание отправлено.", reply_markup=_menu_back_kb(chat_id))
        except Exception: 
            pass
        return
        
    db_save_user(q.from_user.id, **u)
    
    game = GAMES.get(chat_id)
    if game and q.from_user.id in game.players:
        game.players[q.from_user.id].active_items[iid] = mode
        
    await q.answer("Активировано!")
    try: 
        await q.edit_message_text(f"✅ Активировано: {SHOP_ITEMS[iid]['name']} ({mode})", reply_markup=_menu_back_kb(chat_id))
    except Exception: 
        pass

async def cb_menu_bonus(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    
    if time.time() - u['last_bonus'] < 86400: return await q.answer("Бонус раз в сутки!", show_alert=True)
    
    bonus_amount = 5
    for tid, tdata in TITLES_SHOP.items():
        if tdata['name'] == u['title']:
            bonus_amount = tdata['daily_bonus']
            break
            
    u['coins'] += bonus_amount
    u['last_bonus'] = time.time()
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id)
    
    await q.answer(f"+{bonus_amount}🍁")
    await q.edit_message_text(f"🍁 <b>Бонус</b> +{bonus_amount}🍁\nБаланс: {u['coins']}", parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))

async def cb_menu_top(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows_coins = conn.execute("SELECT user_id, username, coins FROM users_data WHERE coins > 0 ORDER BY coins DESC LIMIT 5").fetchall()
        rows_level = conn.execute("SELECT user_id, username, level FROM users_data WHERE level > 1 ORDER BY level DESC LIMIT 5").fetchall()
        
    lines = ["🏆 <b>Топ по листикам:</b>", ""]
    if rows_coins:
        for i, r in enumerate(rows_coins, 1):
            uname = r['username']
            link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 Игрок {r["user_id"]}'
            lines.append(f"{i}. {link} — {r['coins']}🍁")
    else:
        lines.append("Пока пусто.")
        
    lines.append("\n⭐ <b>Топ по уровню:</b>")
    lines.append("")
    if rows_level:
        for i, r in enumerate(rows_level, 1):
            uname = r['username']
            link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 Игрок {r["user_id"]}'
            lines.append(f"{i}. {link} — {r['level']} lvl")
    else:
        lines.append("Пока пусто.")
        
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_buy(update, ctx):
    q = update.callback_query
    if not q: return
    try: 
        chat_id = int(q.data.split(":")[2])
    except Exception: 
        chat_id = 0
    
    text = f"💎 <b>Покупка листиков</b>\n\nАктуальный курс: <b>1 листик = 3 рубля</b>\n\nДля покупки напишите нашему менеджеру: {SELLER_USERNAME}\nПосле перевода он выдаст вам листики.\n\n<i>(Обязательно укажите ваш ID: <code>{q.from_user.id}</code>)</i>"
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_casino(update, ctx):
    q = update.callback_query
    if not q: return
    ADMIN_STATE.pop(q.from_user.id, None)
    CROC_AWAITING.pop(q.from_user.id, None)
    CASINO_AWAITING.add(q.from_user.id)
    
    u = db_get_user(q.from_user.id, q.from_user)
    
    text = f"🎰 <b>Казино</b>\n\nТвой баланс: <b>{u['coins']}🍁</b>\nВыплата при победе: <b>x2</b>\n\nВведи сумму ставки (целое число от 1🍁):"
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(0))
    await q.answer()


async def cb_admin_main(update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("Нет доступа", show_alert=True)
    CASINO_AWAITING.discard(q.from_user.id)
    CROC_AWAITING.pop(q.from_user.id, None)
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 Выдать листики", callback_data="admin:act:give_coins")],
        [InlineKeyboardButton("⭐ Выдать уровень", callback_data="admin:act:give_level")],
        [InlineKeyboardButton("👑 Выдать титул", callback_data="admin:act:give_title")],
        [InlineKeyboardButton("🎰 Настройки Казино", callback_data="admin:casino")],
        [InlineKeyboardButton("👥 Список юзеров", callback_data="admin:users")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main:0")]
    ])
    await q.edit_message_text("🛠 <b>Админ-панель</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_admin_act(update, ctx):
    q = update.callback_query
    act = q.data.split(":")[2]
    ADMIN_STATE[q.from_user.id] = act
    prompts = {
        "give_coins": "Отправь: <code>ID количество</code>",
        "give_level": "Отправь: <code>ID уровень</code>",
        "give_title": "Отправь: <code>ID Титул</code>"
    }
    await q.edit_message_text(f"🛠 {prompts[act]}", parse_mode=ParseMode.HTML)
    await q.answer()

async def cb_admin_casino(update, ctx):
    q = update.callback_query
    chance = db_get_setting("casino_chance", "45")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить шанс", callback_data="admin:casino_set")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin:main")]
    ])
    await q.edit_message_text(f"🎰 <b>Настройки Казино</b>\n\nТекущий шанс победы: <b>{chance}%</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_admin_casino_set(update, ctx):
    q = update.callback_query
    ADMIN_STATE[q.from_user.id] = "casino_chance"
    await q.edit_message_text("🛠 Отправь новый шанс победы в процентах (от 1 до 99):")
    await q.answer()

async def cb_admin_users(update, ctx):
    q = update.callback_query
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, level FROM users_data").fetchall()
    lines = ["👥 <b>Юзеры:</b>"]
    for r in rows[:50]:
        uname = html.escape(r['username'] or 'Аноним')
        lines.append(f"<code>{r['user_id']}</code> {uname} (lvl {r['level']})")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(0))
    await q.answer()


async def cb_croc_new(update, ctx):
    q = update.callback_query
    if q.message.chat.id not in CROC_ACTIVE:
        return await q.answer("Игра в Крокодила остановлена.", show_alert=True)
    if q.message.chat.id in CROC_GAME: 
        return await q.answer("Слово уже загадано!", show_alert=True)
        
    CROC_AWAITING[q.from_user.id] = q.message.chat.id
    await q.answer("Напиши мне в ЛС слово и описание!", show_alert=True)
    try: 
        await ctx.bot.send_message(q.from_user.id, "🐊 Отправь: <code>Слово | Описание</code>", parse_mode=ParseMode.HTML)
    except Exception: 
        pass

async def croc_guess_mw(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type == "private" or not _check_thread(update): return
    
    game = CROC_GAME.get(msg.chat.id)
    if not game or not game.get("word") or not msg.text: return
    
    if msg.text.strip().lower() == game["word"].lower():
        CROC_GAME.pop(msg.chat.id, None)
        try: 
            await msg.set_reaction("✅")
        except Exception: 
            pass
        
        u = db_get_user(msg.from_user.id, msg.from_user)
        old_lvl = u['level']
        
        u['coins'] += 5
        u['croc_wins'] += 1
        
        calc = (u['wins'] * 2 + u['games'] + u['croc_wins']) // 5 + 1
        new_lvl = max(calc, int(u.get('level') or 1))
        u['level'] = new_lvl
        
        if "croc_1" not in u['achievements']: u['achievements'].append("croc_1")
        if u['croc_wins'] >= 10 and "croc_10" not in u['achievements']: u['achievements'].append("croc_10")
        if u['croc_wins'] >= 50 and "croc_50" not in u['achievements']: u['achievements'].append("croc_50")
        if new_lvl >= 5 and "level_5" not in u['achievements']: u['achievements'].append("level_5")
        if new_lvl >= 10 and "level_10" not in u['achievements']: u['achievements'].append("level_10")
        if new_lvl >= 20 and "level_20" not in u['achievements']: u['achievements'].append("level_20")
        
        db_save_user(msg.from_user.id, **u)
        _check_rich_achievements(msg.from_user.id)
        
        msg_text = f"🐊 Ты угадал слово <b>{game['word']}</b>!\nНаграда: +5🍁\nОпыт: <b>+2 XP</b>"
        if new_lvl > old_lvl:
            msg_text += f"\n⭐ <b>Уровень повышен до {new_lvl}!</b>"
            
        try:
            await ctx.bot.send_message(msg.from_user.id, msg_text, parse_mode=ParseMode.HTML)
        except Exception: 
            pass
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Загадать слово", callback_data="croc:new")]])
        await msg.reply_text(f"🎉 <b>{get_safe_name(msg.from_user)}</b> угадал слово <b>{game['word']}</b>!\nНаграда: +5🍁\n\nЗагадай следующее:", parse_mode=ParseMode.HTML, reply_markup=kb)
        raise ApplicationHandlerStop
    else:
        try: 
            await msg.set_reaction("❌")
        except Exception: 
            pass

async def cb_noop(update, ctx):
    await update.callback_query.answer()

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception while handling an update:", exc_info=context.error)


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
    app.add_handler(MessageHandler(g & filters.TEXT & filters.Regex(CROCODILE_STOP_REGEX), cmd_crocodile_stop))

    app.add_handler(CommandHandler("start", cmd_dm_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("menu", cmd_dm_menu, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, cmd_dm_other))

    app.add_handler(CallbackQueryHandler(cb_start_count, pattern=r"^ms:"))
    app.add_handler(CallbackQueryHandler(cb_join, pattern=r"^mj$"))
    app.add_handler(CallbackQueryHandler(cb_cancel, pattern=r"^mc$"))
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
    app.add_handler(CallbackQueryHandler(cb_menu_buy, pattern=r"^menu:buy:"))
    app.add_handler(CallbackQueryHandler(cb_menu_casino, pattern=r"^menu:casino:"))
    
    app.add_handler(CallbackQueryHandler(cb_shop_item, pattern=r"^shop:item:"))
    app.add_handler(CallbackQueryHandler(cb_shop_buy, pattern=r"^shop:buy:"))
    app.add_handler(CallbackQueryHandler(cb_inv_item, pattern=r"^inv:item:"))
    app.add_handler(CallbackQueryHandler(cb_inv_use, pattern=r"^inv:use:"))
    
    app.add_handler(CallbackQueryHandler(cb_admin_main, pattern=r"^admin:main$"))
    app.add_handler(CallbackQueryHandler(cb_admin_act, pattern=r"^admin:act:"))
    app.add_handler(CallbackQueryHandler(cb_admin_casino, pattern=r"^admin:casino$"))
    app.add_handler(CallbackQueryHandler(cb_admin_casino_set, pattern=r"^admin:casino_set$"))
    app.add_handler(CallbackQueryHandler(cb_admin_users, pattern=r"^admin:users$"))
    
    app.add_handler(CallbackQueryHandler(cb_croc_new, pattern=r"^croc:new$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))

    app.add_error_handler(_error_handler)
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
