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

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("mafia")

DB_DIR = Path(os.getenv("DATA_DIR", "."))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(DB_DIR / "mafia.db")

ROLE_NAMES = {"mafia":"Тень","cop":"Смотритель","doctor":"Лекарь","bartender":"Укрыватель","whore":"Очаровательница","medium":"Шептун","citizen":"Житель"}
ROLE_DESCRIPTIONS = {
    "mafia": "Ты — Тень. Ночью вместе с союзниками выбираешь жертву.",
    "cop": "Ты — Смотритель. Ночью проверяешь одного жителя.",
    "doctor": "Ты — Лекарь. Ночью лечишь одного жителя.",
    "bartender": "Ты — Укрыватель. Ночью поишь одного жителя.",
    "whore": "Ты — Очаровательница. Ночью отвлекаешь одного жителя.",
    "medium": "Ты — Шептун. Узнаёшь роль павшего.",
    "citizen": "Ты — Житель. Днём голосуй против Теней.",
}
ROLE_TEAM = {k: ("mafia" if k=="mafia" else "city") for k in ROLE_NAMES}
_ROLE_MAP = {"M":"mafia","C":"cop","D":"doctor","B":"bartender","W":"whore","N":"medium","G":"citizen"}
ROLE_DISTRIBUTION = {
    5:"M C G G G",6:"M C D G G G",7:"M M C D G G G",8:"M M C D B G G G",
    9:"M M C D B W G G G",10:"M M M C D B W G G G",11:"M M M C D B W N G G G",
    12:"M M M C D B W N G G G G",13:"M M M M C D B W N G G G G",
    14:"M M M M C D B W N G G G G G",15:"M M M M C D B W N G G G G G G",
    16:"M M M M M C D B W N G G G G G G",17:"M M M M M C D B W N G G G G G G G",
    18:"M M M M M C D B W N G G G G G G G G",19:"M M M M M M C D B W N G G G G G G G G",
    20:"M M M M M M C D B W N G G G G G G G G G",
}

SHOP_ITEMS = {
    "amulet":{"name":"Оберег","emoji":"🛡","price":75,"desc":"Защита от убийства ночью","modes":("now","match"),"cat":"game"},
    "blind":{"name":"Отвод глаз","emoji":"👁‍🗨","price":60,"desc":"Защита от голосования днём","modes":("now","match"),"cat":"game"},
    "fake":{"name":"Фальшивка","emoji":"📄","price":100,"desc":"Смотритель видит вас как жителя","modes":("now","match"),"cat":"game"},
    "cookie":{"name":"Печенье","emoji":"🍪","price":40,"desc":"Узнать роль случайного игрока","modes":("now",),"cat":"game"},
    "double_vote":{"name":"Двойной голос","emoji":"🎯","price":200,"desc":"Твой голос на дневном голосовании считается за два","modes":("now","match"),"cat":"active"},
    "bomb":{"name":"Бомба","emoji":"💣","price":250,"desc":"Если тебя казнят, заберёшь с собой случайного голосовавшего против тебя","modes":("now","match"),"cat":"active"},
    "antidote":{"name":"Противоядие","emoji":"💊","price":50,"desc":"Мгновенно снимает эффект молчания","modes":("now",),"cat":"active"},
    "case_wood":{"name":"Деревянный кейс","emoji":"📦","price":50,"desc":"Шанс 45%. Выигрыш до 80🍁!","modes":("now",),"cat":"misc"},
    "case_silver":{"name":"Серебряный кейс","emoji":"🎁","price":200,"desc":"Шанс 45%. Выигрыш до 300🍁!","modes":("now",),"cat":"misc"},
    "case_gold":{"name":"Золотой кейс","emoji":"💎","price":1000,"desc":"Шанс 45%. Выигрыш до 2000🍁!","modes":("now",),"cat":"misc"},
    "custom_title":{"name":"Свой титул","emoji":"🎨","price":2000,"desc":"Свой текст в титул (до 15 символов)","modes":("now",),"cat":"misc"},
    "bait_advanced":{"name":"Улучшенная наживка x10","emoji":"🐛","price":300,"desc":"10 шт. Сапог реже (15%), редкая рыба чаще","modes":("now",),"cat":"fish"},
    "bait_luxury":{"name":"Премиум наживка x10","emoji":"🦐","price":800,"desc":"10 шт. Сапог 5%, редкий улов чаще","modes":("now",),"cat":"fish"},
    "dungeon_potion":{"name":"Зелье удачи","emoji":"🧪","price":150,"desc":"+25% к успеху в следующем походе","modes":("now",),"cat":"dungeon"},
    "dungeon_charm":{"name":"Амулет охотника","emoji":"🧿","price":400,"desc":"+50% к успеху в следующем походе","modes":("now",),"cat":"dungeon"},
}

TITLES_SHOP = {
    "novice":{"name":"Новичок","emoji":"🐣","price":0,"ach_req":0,"daily_bonus":1},
    "leaf_collector":{"name":"Собиратель листвы","emoji":"🍁","price":100,"ach_req":0,"daily_bonus":5},
    "guardian":{"name":"Страж Клена","emoji":"🌳","price":350,"ach_req":0,"daily_bonus":15},
    "king":{"name":"Осенний Король","emoji":"👑","price":800,"ach_req":0,"daily_bonus":40},
    "donater":{"name":"Меценат","emoji":"💎","price":1500,"ach_req":0,"daily_bonus":100},
    "godfather":{"name":"Крестный отец","emoji":"🎩","price":2000,"ach_req":0,"daily_bonus":150},
    "rich_boy":{"name":"Золотой Скрудж","emoji":"💰","price":3000,"ach_req":0,"daily_bonus":250},
    "farmer":{"name":"Фермер","emoji":"🌾","price":0,"ach_req":8,"daily_bonus":3},
    "fisher":{"name":"Рыбак","emoji":"🎣","price":0,"ach_req":12,"daily_bonus":3},
    "dungeoner":{"name":"Странник","emoji":"⚔️","price":0,"ach_req":16,"daily_bonus":3},
    "detective":{"name":"Детектив","emoji":"🕵️","price":0,"ach_req":5,"daily_bonus":2},
    "sheriff":{"name":"Шериф","emoji":"🤠","price":0,"ach_req":8,"daily_bonus":2},
    "veteran":{"name":"Ветеран","emoji":"🎖","price":0,"ach_req":10,"daily_bonus":2},
    "legend":{"name":"Легенда","emoji":"🌟","price":0,"ach_req":15,"daily_bonus":2},
    "god":{"name":"Божество Клена","emoji":"🌌","price":0,"ach_req":20,"daily_bonus":5},
    "emperor":{"name":"Император","emoji":"🏛","price":0,"ach_req":25,"daily_bonus":5},
    "absolute":{"name":"Абсолют","emoji":"🌠","price":0,"ach_req":30,"daily_bonus":5},
}

ALL_ACHIEVEMENTS = {
    "first_win":"🍁 Первая победа","rich_100":"💰 Богач (100🍁)","rich_1000":"💎 Олигарх (1000🍁)",
    "rich_5000":"🏦 Миллионер (5000🍁)","rich_10000":"🌍 Хозяин мира (10000🍁)",
    "games_10":"🎮 10 игр","games_50":"🎮 50 игр","games_100":"🎮 100 игр","games_500":"🎮 500 игр",
    "wins_5":"🏆 5 побед","wins_20":"🏆 20 побед","wins_50":"🏆 50 побед","wins_100":"🏆 100 побед",
    "croc_1":"🐊 Первый крокодил","croc_10":"🐊 10 слов","croc_50":"🐊 50 слов","croc_100":"🐊 100 слов",
    "mafia_1":"🔪 Первая тень","mafia_10":"🔪 Босс мафии","mafia_50":"🔪 Дон мафии",
    "city_1":"🕵️ Первый мирный","city_10":"🕵️ Защитник","city_50":"🕵️ Мэр",
    "level_5":"⭐ 5 ур.","level_10":"🌟 10 ур.","level_20":"✨ 20 ур.","level_30":"💫 30 ур.","level_50":"🌌 50 ур.",
    "farm_1":"🌾 Первый урожай","farm_10":"🌾 10 урожаев","farm_50":"🌾 50 урожаев",
    "fish_1":"🐟 Первая рыбка","fish_10":"🐟 10 рыбок","fish_50":"🐟 50 рыбок",
    "dung_1":"⚔️ Первый поход","dung_10":"⚔️ 10 походов","dung_50":"⚔️ 50 походов",
    "ach_10":"🎖 10 ачивок","ach_20":"🏅 20 ачивок",
}

ACTIVATION_REGEX = re.compile(r"(?i)^(?:/)?(?:активация|activate)(?:\s+\S+)?$")
MAFIA_START_REGEX = re.compile(r"(?i)^мафия\s+(?:начать|старт)$")
MAFIA_STOP_REGEX = re.compile(r"(?i)^мафия\s+(?:стоп|stop)$")
CROCODILE_REGEX = re.compile(r"(?i)^/?(?:крокодил|слова|crocodile|words)(?:\s+(?:начать|старт))?$")
CROCODILE_STOP_REGEX = re.compile(r"(?i)^/?(?:крокодил|слова|crocodile|words)\s+(?:стоп|stop)$")

CROC_GAME, CROC_AWAITING, CROC_ACTIVE, ADMIN_STATE, CUSTOM_TITLE_AWAITING, GAMES = {}, {}, set(), {}, {}, {}


def _init_db():
    with sqlite3.connect(DB_PATH, timeout=15.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activations (chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS pending_activations (chat_id INTEGER PRIMARY KEY, key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS bans (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS users_data (
                user_id INTEGER PRIMARY KEY, username TEXT, coins INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1, title TEXT DEFAULT 'Новичок',
                wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0,
                inventory TEXT DEFAULT '{}', achievements TEXT DEFAULT '[]',
                unlocked_titles TEXT DEFAULT '["Новичок"]', last_bonus REAL DEFAULT 0,
                croc_wins INTEGER DEFAULT 0, mafia_wins INTEGER DEFAULT 0, city_wins INTEGER DEFAULT 0,
                farm_slots INTEGER DEFAULT 1, farm_data TEXT DEFAULT '[]',
                fish_rod INTEGER DEFAULT 1, fish_bait INTEGER DEFAULT 0, fish_cd REAL DEFAULT 0,
                dungeon_weapon INTEGER DEFAULT 1, dungeon_armor INTEGER DEFAULT 1,
                dungeon_cd REAL DEFAULT 0, dungeon_bonus INTEGER DEFAULT 0,
                pet_name TEXT DEFAULT '', pet_level INTEGER DEFAULT 1,
                pet_hunger INTEGER DEFAULT 100, pet_happy INTEGER DEFAULT 100,
                pet_last_time REAL DEFAULT 0, pet_sick INTEGER DEFAULT 0,
                farm_harvests INTEGER DEFAULT 0, fish_caught INTEGER DEFAULT 0, dungeon_runs INTEGER DEFAULT 0
            );
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users_data)")}
        for col, ddl in [("dungeon_bonus","INTEGER DEFAULT 0")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE users_data ADD COLUMN {col} {ddl}")
        defaults = [
            ("case_wood_chance","45"),("case_wood_max_win","80"),
            ("case_silver_chance","45"),("case_silver_max_win","300"),
            ("case_gold_chance","45"),("case_gold_max_win","2000"),
            ("farm_acorn_time","60"),("farm_acorn_max","15"),
            ("farm_mushroom_time","300"),("farm_mushroom_max","70"),
            ("farm_pumpkin_time","900"),("farm_pumpkin_max","180"),
            ("farm_syrup_time","1800"),("farm_syrup_max","400"),
            ("farm_sunflower_time","3600"),("farm_sunflower_max","1000"),
            ("fish_chance_boot","30"),("fish_chance_leaf","30"),
            ("fish_chance_carp","20"),("fish_chance_perch","12"),
            ("fish_chance_chest","5"),("fish_chance_key","2"),("fish_chance_pearl","1"),
            ("fish_reward_leaf","1"),("fish_reward_carp","3"),
            ("fish_reward_perch","6"),("fish_reward_chest","15"),
            ("fish_reward_key","40"),("fish_reward_pearl","100"),
            ("dungeon_entry_cost","50"),("dungeon_base_chance","40"),
            ("dungeon_mult_chance","3"),("dungeon_reward_min","10"),("dungeon_reward_max","50"),
            ("pet_cost","500"),("pet_feed_cost","30"),("pet_feed_cost_per_level","15"),
            ("pet_play_cost","20"),("pet_play_cost_per_level","10"),
            ("pet_upgrade_cost_base","500"),("pet_decay_time","600"),
            ("pet_base_yield","25"),("pet_yield_per_level","8"),
            ("pet_heal_cost_per_level","300"),
            ("pet_feed_restore","60"),("pet_play_restore","60"),
        ]
        for k, v in defaults:
            conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (k, v))
        conn.execute("UPDATE bot_settings SET value='45' WHERE key='case_wood_chance' AND value IN ('40','15','60')")
        conn.execute("UPDATE bot_settings SET value='80' WHERE key='case_wood_max_win' AND value IN ('30','200','100')")
        conn.execute("UPDATE bot_settings SET value='45' WHERE key='case_silver_chance' AND value IN ('30','10','50')")
        conn.execute("UPDATE bot_settings SET value='300' WHERE key='case_silver_max_win' AND value IN ('100','800','500')")
        conn.execute("UPDATE bot_settings SET value='45' WHERE key='case_gold_chance' AND value IN ('20','5','40')")
        conn.execute("UPDATE bot_settings SET value='2000' WHERE key='case_gold_max_win' AND value IN ('500','5000','3000')")
        conn.execute("UPDATE bot_settings SET value='0' WHERE key LIKE 'farm_%_min' AND CAST(value AS INTEGER) < 0")
        conn.commit()

_init_db()

def is_user_banned(uid):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        return conn.execute("SELECT 1 FROM bans WHERE user_id=?", (uid,)).fetchone() is not None
def ban_user(uid):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR IGNORE INTO bans (user_id) VALUES (?)", (uid,)); conn.commit()
def unban_user(uid):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("DELETE FROM bans WHERE user_id=?", (uid,)); conn.commit()

def db_get_setting(key, default=""):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
def db_set_setting(key, value):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value))); conn.commit()

def db_get_user(user_id, tg_user=None):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO users_data (user_id) VALUES (?)", (user_id,)); conn.commit()
            row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        if tg_user:
            name = tg_user.username or tg_user.full_name or f"Игрок {user_id}"
            if row['username'] != name:
                conn.execute("UPDATE users_data SET username=? WHERE user_id=?", (name, user_id)); conn.commit()
                row = conn.execute("SELECT * FROM users_data WHERE user_id=?", (user_id,)).fetchone()
        d = dict(row)
        d['inventory'] = json.loads(d['inventory'] or '{}')
        d['achievements'] = json.loads(d['achievements'] or '[]')
        d['unlocked_titles'] = json.loads(d.get('unlocked_titles') or '["Новичок"]')
        d['farm_data'] = json.loads(d.get('farm_data') or '[]')
        for k in ('coins','level','wins','games','croc_wins','mafia_wins','city_wins','farm_slots','fish_rod','fish_bait','dungeon_weapon','dungeon_armor','dungeon_bonus','pet_level','pet_hunger','pet_happy','pet_sick','farm_harvests','fish_caught','dungeon_runs'):
            d[k] = int(d.get(k) or 0)
        d['level'] = max(1, d['level'])
        d['last_bonus'] = float(d.get('last_bonus') or 0.0)
        d['fish_cd'] = float(d.get('fish_cd') or 0)
        d['dungeon_cd'] = float(d.get('dungeon_cd') or 0)
        d['pet_last_time'] = float(d.get('pet_last_time') or 0.0)
        d['pet_name'] = d.get('pet_name') or ''
        calc_level = (d['wins'] * 2 + d['games'] + d['croc_wins']) // 5 + 1
        d['level'] = max(calc_level, d['level'])
        if d['title'] not in d['unlocked_titles']:
            d['unlocked_titles'].append(d['title'])
        return d

def db_save_user(uid, **kwargs):
    u = db_get_user(uid)
    for k, v in kwargs.items():
        if k in u: u[k] = v
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("""
            UPDATE users_data SET 
                username=?, coins=?, level=?, title=?, wins=?, games=?, inventory=?, achievements=?, unlocked_titles=?, last_bonus=?, croc_wins=?, mafia_wins=?, city_wins=?,
                farm_slots=?, farm_data=?, fish_rod=?, fish_bait=?, fish_cd=?, dungeon_weapon=?, dungeon_armor=?, dungeon_cd=?, dungeon_bonus=?,
                pet_name=?, pet_level=?, pet_hunger=?, pet_happy=?, pet_last_time=?, pet_sick=?,
                farm_harvests=?, fish_caught=?, dungeon_runs=?
            WHERE user_id=?
        """, (
            u['username'], u['coins'], u['level'], u['title'], u['wins'], u['games'],
            json.dumps(u['inventory']), json.dumps(u['achievements']), json.dumps(u['unlocked_titles']), u['last_bonus'],
            u['croc_wins'], u['mafia_wins'], u['city_wins'],
            u['farm_slots'], json.dumps(u['farm_data']), u['fish_rod'], u['fish_bait'], u['fish_cd'],
            u['dungeon_weapon'], u['dungeon_armor'], u['dungeon_cd'], u['dungeon_bonus'],
            u['pet_name'], u['pet_level'], u['pet_hunger'], u['pet_happy'], u['pet_last_time'], u['pet_sick'],
            u['farm_harvests'], u['fish_caught'], u['dungeon_runs'], uid
        )); conn.commit()

def _check_rich_achievements(uid):
    u = db_get_user(uid); changed = False
    for k, val in (("rich_100",100),("rich_1000",1000),("rich_5000",5000),("rich_10000",10000)):
        if u['coins'] >= val and k not in u['achievements']:
            u['achievements'].append(k); changed = True
    if changed: db_save_user(uid, achievements=u['achievements'])

def _check_mini_game_achievements(uid):
    u = db_get_user(uid); changed = False
    for field_key, thresholds in [
        ("farm_harvests",[(1,"farm_1"),(10,"farm_10"),(50,"farm_50")]),
        ("fish_caught",[(1,"fish_1"),(10,"fish_10"),(50,"fish_50")]),
        ("dungeon_runs",[(1,"dung_1"),(10,"dung_10"),(50,"dung_50")]),
    ]:
        for val, ach in thresholds:
            if u[field_key] >= val and ach not in u['achievements']:
                u['achievements'].append(ach); changed = True
    if len(u['achievements']) >= 10 and "ach_10" not in u['achievements']: u['achievements'].append("ach_10"); changed=True
    if len(u['achievements']) >= 20 and "ach_20" not in u['achievements']: u['achievements'].append("ach_20"); changed=True
    if changed: db_save_user(uid, achievements=u['achievements'])

def is_chat_activated(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        return conn.execute("SELECT 1 FROM activations WHERE chat_id=?", (chat_id,)).fetchone() is not None
def activate_chat(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR IGNORE INTO activations (chat_id) VALUES (?)", (chat_id,)); conn.commit()
def get_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        row = conn.execute("SELECT key FROM pending_activations WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None
def set_pending_key(chat_id, key):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("INSERT OR REPLACE INTO pending_activations (chat_id, key) VALUES (?, ?)", (chat_id, key)); conn.commit()
def clear_pending_key(chat_id):
    with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
        conn.execute("DELETE FROM pending_activations WHERE chat_id=?", (chat_id,)); conn.commit()

def get_safe_name(user_or_player):
    name = getattr(user_or_player, 'full_name', str(user_or_player))
    if not name or not str(name).strip(): return "Игрок"
    return html.escape(str(name))

def get_roles(count):
    spec = ROLE_DISTRIBUTION.get(count)
    return [_ROLE_MAP[t] for t in spec.split()] if spec else None

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
    except Exception: return False

async def _safe_send(bot, user_id, text, **kwargs):
    try: await bot.send_message(user_id, text, **kwargs); return True
    except Exception: return False

async def _send_to_chat(bot, game, text, **kwargs):
    if game.thread_id: kwargs["message_thread_id"] = game.thread_id
    return await bot.send_message(game.chat_id, text, **kwargs)

@dataclass
class Player:
    user_id: int; username: str; full_name: str
    role: str = "citizen"; alive: bool = True; muted_until: float = 0.0
    coins: int = 0; items: dict = field(default_factory=dict); active_items: dict = field(default_factory=dict)
    achievements: list = field(default_factory=list); wins: int = 0; games: int = 0; level: int = 1; title: str = "Новичок"
    def is_muted(self): return time.time() < self.muted_until
    def has_active(self, item_id): return item_id in self.active_items
    def consume_active(self, item_id): self.active_items.pop(item_id, None)

@dataclass
class Game:
    chat_id: int; thread_id: int; total: int
    players: dict = field(default_factory=dict); phase: str = "joining"; round_num: int = 0
    join_message_id: Optional[int] = None; night_actions: dict = field(default_factory=dict); votes: dict = field(default_factory=dict)
    dm_failed: set = field(default_factory=set); winner: Optional[str] = None; task: Optional[asyncio.Task] = None
    def alive_players(self): return [p for p in self.players.values() if p.alive]
    def alive_mafia(self): return [p for p in self.alive_players() if p.role == "mafia"]
    def alive_city(self): return [p for p in self.alive_players() if ROLE_TEAM[p.role] == "city"]
    def all_mafia(self): return [p for p in self.players.values() if p.role == "mafia"]

def _balance_str(coins): return f"<b>{int(coins)}🍁</b>"

def _menu_main_kb(user_id, chat_id=0):
    rows = [
        [InlineKeyboardButton("👤 Профиль", callback_data=f"menu:profile:{chat_id}"), InlineKeyboardButton("🛒 Магазин", callback_data=f"menu:shop:{chat_id}")],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data=f"menu:inv:{chat_id}"), InlineKeyboardButton("🏆 Топы", callback_data=f"menu:top_menu:{chat_id}")],
        [InlineKeyboardButton("🏅 Достижения", callback_data=f"menu:ach:{chat_id}"), InlineKeyboardButton("🍁 Бонус", callback_data=f"menu:bonus:{chat_id}")],
        [InlineKeyboardButton("👑 Титулы", callback_data=f"menu:titles:{chat_id}"), InlineKeyboardButton("🐾 Питомец", callback_data=f"pet:main:{chat_id}")],
        [InlineKeyboardButton("🌾 Ферма", callback_data=f"farm:main:{chat_id}"), InlineKeyboardButton("🎣 Рыбалка", callback_data=f"fish:main:{chat_id}")],
        [InlineKeyboardButton("⚔️ Подземелье", callback_data=f"dungeon:main:{chat_id}")],
        [InlineKeyboardButton("💎 Купить 🍁", callback_data=f"menu:buy:{chat_id}")]
    ]
    if user_id == ADMIN_ID: rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)

def _menu_back_kb(chat_id=0): return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]])

def _menu_text(uid, tg_user=None):
    u = db_get_user(uid, tg_user)
    return f"🍁 <b>Меню</b>\n\nБаланс: {_balance_str(u['coins'])}\nУровень: <b>{u['level']}</b>\nТитул: <b>{u['title']}</b>"


async def ban_middleware(update, ctx):
    user = update.effective_user
    if not user or user.id == ADMIN_ID: return
    if is_user_banned(user.id):
        try: await ctx.bot.send_message(user.id, f"⛔ Вы забанены. Разбан: {SELLER_USERNAME} (500₽).", parse_mode=ParseMode.HTML)
        except Exception: pass
        msg = update.effective_message
        if msg and msg.chat.type != "private":
            try: await msg.reply_text("⛔ Вы забанены.")
            except Exception: pass
        raise ApplicationHandlerStop

async def ban_callback_mw(update, ctx):
    q = update.callback_query
    if not q: return
    user = q.from_user
    if not user or user.id == ADMIN_ID: return
    if is_user_banned(user.id):
        await q.answer(f"Забанены. Разбан: 500₽ у {SELLER_USERNAME}", show_alert=True)
        raise ApplicationHandlerStop

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
    log.warning("=== KEY: %s for chat %s ===", key, cm.chat.id)
    await ctx.bot.send_message(cm.chat.id, "🍁 Для активации:\n<code>активация КЛЮЧ</code>", parse_mode=ParseMode.HTML)

async def cmd_activate(update, ctx):
    msg = update.effective_message; user = update.effective_user
    if not msg or not user or msg.chat.type == "private" or is_chat_activated(msg.chat.id): return
    parts = (msg.text or "").lower().lstrip("/").split()
    if len(parts) < 2:
        if await _is_chat_admin(ctx.bot, msg.chat.id, user.id):
            key = get_pending_key(msg.chat.id)
            if not key:
                alphabet = string.ascii_uppercase + string.digits
                key = "".join(secrets.choice(alphabet) for _ in range(12))
                set_pending_key(msg.chat.id, key)
            log.warning("=== KEY: %s ===", key)
        return
    if parts[1].strip().upper() == (get_pending_key(msg.chat.id) or "").upper():
        activate_chat(msg.chat.id); clear_pending_key(msg.chat.id)
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
        except Exception: pass
        raise ApplicationHandlerStop

async def cmd_dm_start(update, ctx):
    msg = update.effective_message
    if msg: await msg.reply_text(_menu_text(msg.from_user.id, msg.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(msg.from_user.id))

async def cmd_dm_other(update, ctx):
    msg = update.effective_message; uid = update.effective_user.id
    db_get_user(uid, update.effective_user)
    
    if uid in ADMIN_STATE:
        action = ADMIN_STATE.pop(uid)
        parts = msg.text.split(maxsplit=2)
        try:
            if action == "give_coins" and len(parts) >= 2:
                target, amount = int(parts[0].replace("@", "")), int(parts[1])
                u = db_get_user(target); new_balance = u['coins'] + amount
                if new_balance < 0: return await msg.reply_text(f"❌ У юзера {target} только {u['coins']}🍁.", reply_markup=_menu_main_kb(uid))
                db_save_user(target, coins=new_balance); _check_rich_achievements(target)
                await msg.reply_text(f"✅ {'Выдано' if amount>0 else 'Списано'} {abs(amount)}🍁 юзеру {target}", reply_markup=_menu_main_kb(uid))
            elif action == "give_level" and len(parts) >= 2:
                target, lvl = int(parts[0].replace("@", "")), int(parts[1])
                db_save_user(target, level=lvl)
                await msg.reply_text(f"✅ Уровень {lvl} юзеру {target}", reply_markup=_menu_main_kb(uid))
            elif action == "give_title" and len(parts) >= 2:
                target = int(parts[0].replace("@", "")); title = " ".join(parts[1:])
                u = db_get_user(target)
                if title not in u['unlocked_titles']: u['unlocked_titles'].append(title)
                db_save_user(target, title=title, unlocked_titles=u['unlocked_titles'])
                await msg.reply_text(f"✅ Титул '{title}'", reply_markup=_menu_main_kb(uid))
            elif action == "take_title" and len(parts) >= 2:
                target = int(parts[0].replace("@", "")); title = " ".join(parts[1:])
                u = db_get_user(target)
                if title in u['unlocked_titles']: u['unlocked_titles'].remove(title)
                if u['title'] == title: u['title'] = "Новичок"
                db_save_user(target, title=u['title'], unlocked_titles=u['unlocked_titles'])
                await msg.reply_text(f"✅ Титул '{title}' забран", reply_markup=_menu_main_kb(uid))
            elif action == "view_profile" and len(parts) >= 1:
                target = int(parts[0].replace("@", ""))
                u = db_get_user(target)
                inv = ", ".join([f"{SHOP_ITEMS[k]['emoji']} {SHOP_ITEMS[k]['name']} x{v}" for k, v in u['inventory'].items() if k in SHOP_ITEMS]) or "Пусто"
                text = f"👤 <code>{target}</code> {html.escape(u['username'] or 'Аноним')}\nБаланс: {_balance_str(u['coins'])}\nУр: {u['level']}\nТитул: {u['title']}\nИнвентарь: {inv}"
                await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(uid))
            elif action == "ban_user":
                target = int(parts[0].replace("@", "")); ban_user(target)
                await msg.reply_text(f"✅ {target} забанен", reply_markup=_menu_main_kb(uid))
            elif action == "unban_user":
                target = int(parts[0].replace("@", "")); unban_user(target)
                await msg.reply_text(f"✅ {target} разбанен", reply_markup=_menu_main_kb(uid))
            elif action.startswith(("case_","farm_","fish_","dungeon_","pet_")):
                val = int(parts[0])
                db_set_setting(action, str(val))
                await msg.reply_text(f"✅ {action} = {val}", reply_markup=_menu_main_kb(uid))
        except Exception:
            await msg.reply_text("❌ Ошибка.", reply_markup=_menu_main_kb(uid))
        return
    
    if uid in CUSTOM_TITLE_AWAITING:
        CUSTOM_TITLE_AWAITING.pop(uid)
        new_title = msg.text.strip().replace("\n", " ")[:15]
        if not new_title: return await msg.reply_text("❌ Пусто.", reply_markup=_menu_main_kb(uid))
        u = db_get_user(uid)
        if new_title not in u['unlocked_titles']: u['unlocked_titles'].append(new_title)
        db_save_user(uid, title=new_title, unlocked_titles=u['unlocked_titles'])
        await msg.reply_text(f"✅ Титул: <b>{html.escape(new_title)}</b>", parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(uid))
        return
    
    if uid in CROC_AWAITING:
        chat_id = CROC_AWAITING.pop(uid)
        if "|" in msg.text:
            word, desc = msg.text.split("|", 1)
            CROC_GAME[chat_id] = {"word": word.strip(), "desc": desc.strip(), "author": uid}
            try:
                await ctx.bot.send_message(chat_id, f"🐊 <b>Крокодил!</b>\n\n{html.escape(desc.strip())}\n\nУгадывайте!", message_thread_id=THREAD_ID or 0, parse_mode=ParseMode.HTML)
                await msg.reply_text("✅ Загадано!")
            except Exception: pass
        else: await msg.reply_text("❌ Слово | Описание")
        return
    await msg.reply_text(_menu_text(uid, update.effective_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(uid))


async def cmd_mafia_start(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id): return await msg.reply_text("⛔ Только админ.")
    if msg.chat.id in GAMES: return await msg.reply_text("⚠️ Игра идёт.")
    buttons = [InlineKeyboardButton(str(i), callback_data=f"ms:{i}") for i in range(5, 21)]
    rows = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
    await msg.reply_text("🍁 <b>Мафия</b>\nВыбери:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))

async def cmd_mafia_stop(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id): return
    game = GAMES.pop(msg.chat.id, None)
    if game:
        if game.task and not game.task.done(): game.task.cancel()
        await msg.reply_text("🛑 Остановлено.")

async def cmd_crocodile(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id): return await msg.reply_text("⛔ Только админ.")
    CROC_ACTIVE.add(msg.chat.id)
    await msg.reply_text("🐊 <b>Крокодил</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Загадать", callback_data="croc:new")]]))

async def cmd_crocodile_stop(update, ctx):
    if not _check_thread(update): return
    msg = update.effective_message
    if not msg or msg.chat.type == "private": return
    if not await _is_chat_admin(ctx.bot, msg.chat.id, msg.from_user.id): return
    CROC_ACTIVE.discard(msg.chat.id); CROC_GAME.pop(msg.chat.id, None)
    await msg.reply_text("🛑 Крокодил остановлен.")
    to_remove = [u for u, c in CROC_AWAITING.items() if c == msg.chat.id]
    for u in to_remove: del CROC_AWAITING[u]


async def cb_start_count(update, ctx):
    q = update.callback_query
    if q is None or not _check_thread(update): return await q.answer("Не та ветка", show_alert=True)
    if q.message.chat.id in GAMES: return await q.answer("Игра идёт", show_alert=True)
    count = int(q.data.split(":")[1])
    game = Game(chat_id=q.message.chat.id, thread_id=q.message.message_thread_id or 0, total=count)
    game.join_message_id = q.message.message_id; GAMES[game.chat_id] = game
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть (0/{count})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    await q.edit_message_text(f"🍁 <b>Набор</b>\n{count} игроков\n\n❗ Жмите /start в ЛС", parse_mode=ParseMode.HTML, reply_markup=kb)
    game.task = asyncio.create_task(_join_timeout(ctx.bot, game.chat_id))

async def _join_timeout(bot, chat_id):
    await asyncio.sleep(300)
    game = GAMES.get(chat_id)
    if game and game.phase == "joining": GAMES.pop(chat_id, None); await _send_to_chat(bot, game, "⏳ Набор отменён.")

async def cb_join(update, ctx):
    q = update.callback_query
    if q is None or not _check_thread(update): return await q.answer("Не та ветка", show_alert=True)
    chat = update.effective_chat; user = q.from_user
    game = GAMES.get(chat.id)
    if not game or game.phase != "joining": return await q.answer("Набор закрыт", show_alert=True)
    if user.id in game.players: return await q.answer("Уже в игре")
    try: await ctx.bot.send_message(user.id, "🍁 Ты в игре!")
    except Exception: return await q.answer("Сначала /start в ЛС!", show_alert=True)
    u = db_get_user(user.id, user)
    p = Player(user_id=user.id, username=user.username or "", full_name=user.full_name)
    p.coins, p.items, p.achievements, p.wins, p.games = u['coins'], u['inventory'], u['achievements'], u['wins'], u['games']
    p.level, p.title = u['level'], u['title']
    game.players[user.id] = p
    n = len(game.players)
    if n >= game.total:
        if game.task and not game.task.done(): game.task.cancel()
        await q.answer("Поехали!")
        await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 Игроков: <b>{n}/{game.total}</b>", parse_mode=ParseMode.HTML)
        await _start_game(ctx.bot, game)
        return
    names = ", ".join([get_safe_name(p) for p in game.players.values()])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🎮 Играть ({n}/{game.total})", callback_data="mj")], [InlineKeyboardButton("❌ Отменить", callback_data="mc")]])
    try: await ctx.bot.edit_message_text(chat_id=chat.id, message_id=game.join_message_id, text=f"🍁 <b>Набор</b>\n{n}/{game.total}\n\n👥 {names}", parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception: pass
    await q.answer(f"В игре ({n}/{game.total})")

async def cb_cancel(update, ctx):
    q = update.callback_query
    if q is None: return
    chat = update.effective_chat
    if not await _is_chat_admin(ctx.bot, chat.id, q.from_user.id): return await q.answer("Только админ", show_alert=True)
    game = GAMES.pop(chat.id, None)
    if game is None: return await q.answer()
    if game.task and not game.task.done(): game.task.cancel()
    await q.answer("Отменено")
    try: await q.edit_message_text("🍁 Отменено.")
    except Exception: await _send_to_chat(ctx.bot, game, "🍁 Отменено.")


async def _start_game(bot, game):
    roles = get_roles(game.total)[:]; random.shuffle(roles)
    uids = list(game.players.keys()); random.shuffle(uids)
    for uid, role in zip(uids, roles): game.players[uid].role = role
    mafia_names = [get_safe_name(p) for p in game.all_mafia()]
    for p in game.players.values():
        teammates = [n for n in mafia_names if n != get_safe_name(p)]
        text = f"🍁 Роль: <b>{ROLE_NAMES[p.role]}</b>\n\n{ROLE_DESCRIPTIONS[p.role]}"
        if p.role == "mafia" and teammates: text += f"\n\n🤝 {', '.join(teammates)}"
        try: await bot.send_message(p.user_id, text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data="openmenu:0")]]))
        except Exception: pass
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
    except Exception as e: log.error(f"Game loop: {e}")
    finally:
        try: await _announce_end(bot, game)
        except Exception as e: log.error(f"Announce: {e}")
        finally: GAMES.pop(game.chat_id, None)

async def _run_night(bot, game):
    game.phase = "night"; game.night_actions.clear()
    await _send_to_chat(bot, game, f"🌙 <b>Ночь {game.round_num}</b>", parse_mode=ParseMode.HTML)
    await asyncio.sleep(2)
    for p in game.alive_players():
        if p.role == "citizen": continue
        targets = [op for op in game.alive_players() if op.user_id != p.user_id]
        if p.role == "mafia": targets = [op for op in game.alive_players() if ROLE_TEAM[op.role] != "mafia"]
        elif p.role == "doctor": targets = game.alive_players()
        elif p.role == "medium": targets = [op for op in game.players.values() if not op.alive]
        if not targets: continue
        rows = [[InlineKeyboardButton(get_safe_name(t), callback_data=f"mn:{game.chat_id}:{t.user_id}")] for t in targets]
        try: await bot.send_message(p.user_id, "🌙 Ночь. Цель:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
    await asyncio.sleep(60)
    await _resolve_night(bot, game)

async def _resolve_night(bot, game):
    bart_t = whore_t = doc_t = cop_t = med_t = None; m_votes = []
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
            if t.alive: t.muted_until = time.time() + 300; msg.append(f"🔇 {get_safe_name(t)} замолчал.")
    if cop_t:
        c = next((p for p in game.alive_players() if p.role == "cop"), None)
        t = game.players[cop_t]; is_m = ROLE_TEAM[t.role] == "mafia"
        if t.has_active("fake"): is_m = False; t.consume_active("fake")
        if c: await _safe_send(bot, c.user_id, f"🔍 {get_safe_name(t)} — {'ТЕНЬ' if is_m else 'Житель'}")
    if med_t:
        m = next((p for p in game.alive_players() if p.role == "medium"), None)
        t = game.players[med_t]
        if m: await _safe_send(bot, m.user_id, f"👻 {get_safe_name(t)}: {ROLE_NAMES[t.role]}")
    if m_votes:
        c = Counter(m_votes).most_common()
        if c[0][1] > 1 or len(c) == 1:
            k = c[0][0]; v = game.players[k]
            if v.has_active("amulet"): v.consume_active("amulet"); msg.append(f"🛡 Оберег спас {get_safe_name(v)}!")
            elif k == doc_t: msg.append(f"💉 Лекарь спас {get_safe_name(v)}!")
            else: v.alive = False; msg.append(f"💀 Убит: {get_safe_name(v)}")
        else: msg.append("Тени не договорились.")
    if not msg: msg.append("Спокойная ночь.")
    await _send_to_chat(bot, game, "🌅 <b>Итоги ночи:</b>\n" + "\n".join(msg), parse_mode=ParseMode.HTML)

async def _run_day(bot, game):
    game.phase = "day"
    alive_str = ", ".join([get_safe_name(p) for p in game.alive_players()])
    sec = 60 if game.total <= 6 else 90 if game.total <= 9 else 120
    await _send_to_chat(bot, game, f"☀️ <b>День {game.round_num}</b>\nЖивы: {alive_str}\n{sec} сек", parse_mode=ParseMode.HTML)
    await asyncio.sleep(sec)

async def _run_vote(bot, game):
    game.phase = "voting"; game.votes.clear()
    alive = game.alive_players()
    if len(alive) <= 1: return
    rows = [[InlineKeyboardButton(get_safe_name(p), callback_data=f"mv:{game.chat_id}:{p.user_id}")] for p in alive]
    await _send_to_chat(bot, game, "🗳 <b>Голосование</b> 1 мин.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await asyncio.sleep(60)
    votes_list = []
    for v_id, t_id in game.votes.items():
        t = game.players.get(t_id)
        if t and t.has_active("blind"): t.consume_active("blind"); continue
        votes_list.append(t_id)
        voter = game.players.get(v_id)
        if voter and voter.has_active("double_vote"):
            votes_list.append(t_id); voter.consume_active("double_vote")
            await _safe_send(bot, v_id, "🎯 Двойной голос!")
    if not votes_list: return await _send_to_chat(bot, game, "Никто не голосовал.")
    c = Counter(votes_list).most_common()
    if c[0][1] >= 2 and (len(c) == 1 or c[0][1] > c[1][1]):
        p = game.players[c[0][0]]; p.alive = False
        text = f"⚖️ Изгнан: {get_safe_name(p)} (<b>{ROLE_NAMES[p.role]}</b>)"
        if p.has_active("bomb"):
            voters = [v_id for v_id, t_id in game.votes.items() if t_id == p.user_id and v_id != p.user_id]
            if voters:
                victim = game.players[random.choice(voters)]; victim.alive = False
                text += f"\n💣 {get_safe_name(p)} взорвал <b>{get_safe_name(victim)}</b>!"
        await _send_to_chat(bot, game, text, parse_mode=ParseMode.HTML)
    else: await _send_to_chat(bot, game, "⚖️ Ничья.")

def _check_win(game):
    m = len(game.alive_mafia()); c = len(game.alive_city())
    if m == 0: game.winner = "city"; return True
    if m >= c: game.winner = "mafia"; return True
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
        u['games'] += 1; reward = 2
        won = (game.winner == "city" and ROLE_TEAM[p.role] == "city") or (game.winner == "mafia" and ROLE_TEAM[p.role] == "mafia")
        if won:
            u['wins'] += 1; reward = 5 if game.winner == "city" else 8
            if "first_win" not in u['achievements']: u['achievements'].append("first_win")
            if game.winner == "mafia":
                u['mafia_wins'] += 1
                if "mafia_1" not in u['achievements']: u['achievements'].append("mafia_1")
            else:
                u['city_wins'] += 1
                if "city_1" not in u['achievements']: u['achievements'].append("city_1")
        u['coins'] += reward
        calc = (u['wins']*2 + u['games'] + u['croc_wins']) // 5 + 1
        u['level'] = max(calc, int(u.get('level') or 1))
        db_save_user(p.user_id, **u)
        try: await _safe_send(bot, p.user_id, f"🍁 {'Выиграл' if won else 'Проиграл'}. +{reward}🍁", reply_markup=_menu_main_kb(p.user_id, game.chat_id))
        except Exception: pass
    await _send_to_chat(bot, game, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🍁 Меню", callback_data=f"openmenu:{game.chat_id}")]]))

async def cb_night_action(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, target_id = int(parts[1]), int(parts[2])
    game = GAMES.get(chat_id)
    if not game or game.phase != "night": return await q.answer("Не ночь", show_alert=True)
    if q.from_user.id in game.night_actions: return await q.answer("Уже ходил", show_alert=True)
    target = game.players.get(target_id)
    if not target: return await q.answer("Нет")
    game.night_actions[q.from_user.id] = target_id
    await q.answer(f"{get_safe_name(target)}")
    try: await q.edit_message_text(f"🌙 Выбран: {get_safe_name(target)}", parse_mode=ParseMode.HTML)
    except Exception: pass

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
    if not target or not target.alive: return await q.answer("Недоступно", show_alert=True)
    game.votes[q.from_user.id] = target_id
    await q.answer("Голос!")
    await _send_to_chat(ctx.bot, game, f"🗳 <b>{get_safe_name(p)}</b> → <b>{get_safe_name(target)}</b>", parse_mode=ParseMode.HTML)


async def cb_open_menu(update, ctx):
    q = update.callback_query
    if not q: return
    ADMIN_STATE.pop(q.from_user.id, None); CROC_AWAITING.pop(q.from_user.id, None); CUSTOM_TITLE_AWAITING.pop(q.from_user.id, None)
    try: chat_id = int(q.data.split(":")[1])
    except Exception: chat_id = 0
    await ctx.bot.send_message(q.from_user.id, _menu_text(q.from_user.id, q.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_main(update, ctx):
    q = update.callback_query
    if not q: return
    ADMIN_STATE.pop(q.from_user.id, None); CROC_AWAITING.pop(q.from_user.id, None); CUSTOM_TITLE_AWAITING.pop(q.from_user.id, None)
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    await q.edit_message_text(_menu_text(q.from_user.id, q.from_user), parse_mode=ParseMode.HTML, reply_markup=_menu_main_kb(q.from_user.id, chat_id))
    await q.answer()

async def cb_menu_profile(update, ctx):
    q = update.callback_query
    if not q: return
    u = db_get_user(q.from_user.id, q.from_user)
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    total_xp = u['wins'] * 2 + u['games'] + u['croc_wins']
    pet_str = f"{u['pet_name']} (Ур. {u['pet_level']})" if u['pet_name'] else "Нет"
    text = (f"👤 <b>Профиль</b>\n\n"
            f"🍁 {_balance_str(u['coins'])}\n"
            f"⭐ Ур. {u['level']} (XP: {total_xp}/{u['level']*5})\n"
            f"👑 {u['title']}\n"
            f"🐾 {pet_str}\n\n"
            f"🏆 Побед: {u['wins']} / Игр: {u['games']}\n"
            f"🌾 {u['farm_harvests']} / 🎣 {u['fish_caught']} / ⚔️ {u['dungeon_runs']}\n\n"
            f"🎒 {len(u['inventory'])} предм. | 🏅 {len(u['achievements'])}")
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_ach(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    lines = ["🏅 <b>Достижения</b>", ""]
    for k, v in ALL_ACHIEVEMENTS.items():
        lines.append(("✅ " if k in u['achievements'] else "❌ ") + v)
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()

async def cb_menu_titles(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    text = f"👑 <b>Титулы</b>\n\nТекущий: <b>{u['title']}</b>\nАчивок: {len(u['achievements'])}\n\n"
    rows = []
    for tid, tdata in TITLES_SHOP.items():
        if tdata['name'] == u['title']: status = " (Надет)"
        elif tdata['name'] in u['unlocked_titles']: status = " (Доступен)"
        elif tdata['ach_req'] > 0: status = f" ({tdata['ach_req']} ачивок)"
        else: status = f" — {tdata['price']}🍁"
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
    if not tdata: return await q.answer("Нет")
    u = db_get_user(q.from_user.id)
    if tdata['name'] == u['title']: return await q.answer("Уже надет")
    if tdata['name'] in u['unlocked_titles']:
        db_save_user(q.from_user.id, title=tdata['name'])
        await q.answer(f"{tdata['name']}")
        return await cb_menu_titles(update, ctx)
    if tdata['ach_req'] > 0:
        if len(u['achievements']) < tdata['ach_req']: return await q.answer(f"Нужно {tdata['ach_req']}", show_alert=True)
    else:
        if u['coins'] < tdata['price']: return await q.answer("Мало листиков", show_alert=True)
        u['coins'] -= tdata['price']
    u['unlocked_titles'].append(tdata['name'])
    db_save_user(q.from_user.id, coins=u['coins'], title=tdata['name'], unlocked_titles=u['unlocked_titles'])
    await q.answer("Куплено")
    await cb_menu_titles(update, ctx)

async def cb_menu_shop(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔪 Для мафии", callback_data=f"shop:cat:{chat_id}:game"), InlineKeyboardButton("🎯 Активные", callback_data=f"shop:cat:{chat_id}:active")],
        [InlineKeyboardButton("📦 Разное", callback_data=f"shop:cat:{chat_id}:misc")],
        [InlineKeyboardButton("🎣 Рыбалка", callback_data=f"shop:cat:{chat_id}:fish"), InlineKeyboardButton("⚔️ Данж", callback_data=f"shop:cat:{chat_id}:dungeon")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]
    ])
    await q.edit_message_text(f"🛒 <b>Магазин</b>\nБаланс: {_balance_str(u['coins'])}", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_shop_cat(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, cat = int(parts[2]), parts[3]
    u = db_get_user(q.from_user.id, q.from_user)
    titles = {"game":"🔪 Для мафии","active":"🎯 Активные","misc":"📦 Разное","fish":"🎣 Рыбалка","dungeon":"⚔️ Данж"}
    title = titles.get(cat, "📦")
    rows = []
    items = sorted([it for it in SHOP_ITEMS.items() if it[1].get("cat") == cat], key=lambda x: x[1]["price"])
    for iid, item in items:
        rows.append([InlineKeyboardButton(f"{item['emoji']} {item['name']} — {item['price']}🍁", callback_data=f"shop:item:{chat_id}:{iid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:shop:{chat_id}")])
    await q.edit_message_text(f"{title}\nБаланс: {_balance_str(u['coins'])}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_shop_item(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    item = SHOP_ITEMS.get(iid)
    if not item: return await q.answer("Нет")
    cat = item.get("cat", "game"); desc = item["desc"]
    if iid.startswith("case_"):
        chance = db_get_setting(f"{iid}_chance", "45"); max_win = db_get_setting(f"{iid}_max_win", "100")
        desc = f"Шанс {chance}%. Выигрыш до {max_win}🍁. Возможен дроп предметов."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Купить", callback_data=f"shop:buy:{chat_id}:{iid}")], [InlineKeyboardButton("◀️ Назад", callback_data=f"shop:cat:{chat_id}:{cat}")]])
    await q.edit_message_text(f"{item['emoji']} <b>{item['name']}</b>\n{desc}\nЦена: <b>{item['price']}🍁</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_shop_buy(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, iid = int(parts[2]), parts[3]
    u = db_get_user(q.from_user.id, q.from_user)
    item = SHOP_ITEMS.get(iid)
    if u['coins'] < item['price']: return await q.answer("Мало листиков", show_alert=True)
    u['coins'] -= item['price']
    qty = 10 if iid in ("bait_advanced", "bait_luxury") else 1
    u['inventory'][iid] = u['inventory'].get(iid, 0) + qty
    db_save_user(q.from_user.id, **u)
    cat = item.get("cat", "game")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В магазин", callback_data=f"shop:cat:{chat_id}:{cat}")]])
    await q.answer("Куплено!")
    try: await q.edit_message_text(f"✅ Куплено: {item['name']}\nОстаток: {_balance_str(u['coins'])}", parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception: pass

async def cb_menu_inv(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    rows = []
    if u['fish_bait'] > 0: rows.append([InlineKeyboardButton(f"🪱 Обычная наживка x{u['fish_bait']}", callback_data="noop")])
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
    if not item: return await q.answer("Нет")
    if iid.startswith("case_") or iid in ["custom_title","antidote","bait_advanced","bait_luxury","dungeon_potion","dungeon_charm"]:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Использовать", callback_data=f"inv:use:{chat_id}:{iid}:now")], [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:inv:{chat_id}")]])
        await q.edit_message_text(f"{item['emoji']} <b>{item['name']}</b>\n\n{item['desc']}", parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
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
    if u['inventory'].get(iid, 0) <= 0: return await q.answer("Нет")
    
    if iid.startswith("case_"):
        u['inventory'][iid] -= 1
        if u['inventory'][iid] == 0: del u['inventory'][iid]
        chance = int(db_get_setting(f"{iid}_chance", "45"))
        max_win = int(db_get_setting(f"{iid}_max_win", "100"))
        won = random.randint(1, 100) <= chance
        item_drop_chance = {"case_wood": 20, "case_silver": 25, "case_gold": 30}.get(iid, 0)
        if won:
            if random.randint(1, 100) <= item_drop_chance:
                drops = {
                    "case_wood": [("bait_advanced", 3, 40), ("bait_luxury", 1, 20), ("dungeon_potion", 1, 40)],
                    "case_silver": [("bait_advanced", 5, 30), ("bait_luxury", 3, 25), ("dungeon_potion", 2, 25), ("dungeon_charm", 1, 20)],
                    "case_gold": [("bait_advanced", 10, 25), ("bait_luxury", 5, 25), ("dungeon_potion", 3, 25), ("dungeon_charm", 2, 25)]
                }.get(iid, [])
                r = random.randint(1, 100); cum = 0; drop_str = ""
                for drop_id, qty, ch in drops:
                    cum += ch
                    if r <= cum:
                        u['inventory'][drop_id] = u['inventory'].get(drop_id, 0) + qty
                        drop_str = f"{SHOP_ITEMS[drop_id]['emoji']} {SHOP_ITEMS[drop_id]['name']} x{qty}"
                        break
                db_save_user(q.from_user.id, **u)
                await q.answer("🎁 Дроп!")
                text = f"{SHOP_ITEMS[iid]['emoji']} Открыт {SHOP_ITEMS[iid]['name']}: <b>{drop_str}</b>\nБаланс: {_balance_str(u['coins'])}"
            else:
                reward = random.randint(10, max_win) if max_win >= 10 else max_win
                u['coins'] += reward
                db_save_user(q.from_user.id, **u)
                _check_rich_achievements(q.from_user.id)
                await q.answer(f"🎉 +{reward}🍁")
                text = f"{SHOP_ITEMS[iid]['emoji']} {SHOP_ITEMS[iid]['name']}: <b>+{reward}🍁</b>\nБаланс: {_balance_str(u['coins'])}"
        else:
            db_save_user(q.from_user.id, **u)
            await q.answer("Пусто")
            text = f"{SHOP_ITEMS[iid]['emoji']} {SHOP_ITEMS[iid]['name']}: пусто.\nБаланс: {_balance_str(u['coins'])}"
        try: await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
        except Exception: pass
        return
    
    if iid == "custom_title":
        u['inventory'][iid] -= 1
        if u['inventory'][iid] == 0: del u['inventory'][iid]
        db_save_user(q.from_user.id, **u)
        CUSTOM_TITLE_AWAITING[q.from_user.id] = True
        await q.answer("Напиши титул в чат!")
        try: await q.edit_message_text("🎨 Напиши свой титул (до 15 символов):")
        except Exception: pass
        return
    
    if iid == "antidote":
        game = GAMES.get(chat_id)
        if not game or q.from_user.id not in game.players: return await q.answer("Ты не в игре", show_alert=True)
        p = game.players[q.from_user.id]
        if not p.is_muted(): return await q.answer("Ты и так не молчишь", show_alert=True)
        u['inventory'][iid] -= 1
        if u['inventory'][iid] == 0: del u['inventory'][iid]
        db_save_user(q.from_user.id, **u)
        p.muted_until = 0
        await q.answer("💊 Антидот!")
        return
    
    if iid in ("bait_advanced", "bait_luxury"):
        return await q.answer("Используй в рыбалке напрямую", show_alert=True)
    
    if iid in ("dungeon_potion", "dungeon_charm"):
        u['inventory'][iid] -= 1
        if u['inventory'][iid] == 0: del u['inventory'][iid]
        bonus = 25 if iid == "dungeon_potion" else 50
        u['dungeon_bonus'] = u.get('dungeon_bonus', 0) + bonus
        db_save_user(q.from_user.id, **u)
        await q.answer(f"✅ +{bonus}% (итого +{u['dungeon_bonus']}%)")
        try: await q.edit_message_text(f"✅ {SHOP_ITEMS[iid]['name']} активирован. Бафф: +{u['dungeon_bonus']}%", reply_markup=_menu_back_kb(chat_id))
        except Exception: pass
        return
    
    u['inventory'][iid] -= 1
    if u['inventory'][iid] == 0: del u['inventory'][iid]
    if iid == "cookie":
        game = GAMES.get(chat_id)
        if game:
            targets = [p for p in game.alive_players() if p.user_id != q.from_user.id]
            if targets:
                t = random.choice(targets)
                await _safe_send(ctx.bot, q.from_user.id, f"🍪 {get_safe_name(t)}: {ROLE_NAMES[t.role]}")
        db_save_user(q.from_user.id, **u)
        await q.answer("Готово")
        return
    db_save_user(q.from_user.id, **u)
    game = GAMES.get(chat_id)
    if game and q.from_user.id in game.players:
        game.players[q.from_user.id].active_items[iid] = mode
    await q.answer("Активировано!")
    try: await q.edit_message_text(f"✅ {SHOP_ITEMS[iid]['name']} ({mode})", reply_markup=_menu_back_kb(chat_id))
    except Exception: pass


async def cb_pet_main(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_name']:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🥚 Завести (500🍁)", callback_data=f"pet:adopt:{chat_id}")], [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]])
        return await q.edit_message_text("🐾 <b>Питомец</b>\n\nЗаведи за <b>500🍁</b>.", parse_mode=ParseMode.HTML, reply_markup=kb)
    now = time.time(); last = u['pet_last_time']; decay_time = int(db_get_setting("pet_decay_time", "600"))
    diff = now - last
    if diff >= decay_time:
        dec = int(diff // decay_time)
        u['pet_hunger'] = max(0, u['pet_hunger'] - dec)
        u['pet_happy'] = max(0, u['pet_happy'] - dec)
        u['pet_last_time'] = now - (diff % decay_time)
        if (u['pet_hunger'] < 30 or u['pet_happy'] < 30) and not u['pet_sick']: u['pet_sick'] = 1
        db_save_user(q.from_user.id, pet_hunger=u['pet_hunger'], pet_happy=u['pet_happy'], pet_last_time=u['pet_last_time'], pet_sick=u['pet_sick'])
    feed_cost = int(db_get_setting("pet_feed_cost", "30")) + (u['pet_level']-1) * int(db_get_setting("pet_feed_cost_per_level", "15"))
    play_cost = int(db_get_setting("pet_play_cost", "20")) + (u['pet_level']-1) * int(db_get_setting("pet_play_cost_per_level", "10"))
    upgrade_cost = u['pet_level'] * int(db_get_setting("pet_upgrade_cost_base", "500"))
    heal_cost = u['pet_level'] * int(db_get_setting("pet_heal_cost_per_level", "300"))
    collect_reward = int(db_get_setting("pet_base_yield", "25")) + (u['pet_level']-1) * int(db_get_setting("pet_yield_per_level", "8"))
    sick_marker = "\n🚨 БОЛЕН!" if u['pet_sick'] else ""
    text = f"🐾 <b>{u['pet_name']}</b>\n⭐ Ур. {u['pet_level']}{sick_marker}\n\n🍖 {u['pet_hunger']}/100\n🎾 {u['pet_happy']}/100"
    rows = []
    if u['pet_sick']: rows.append([InlineKeyboardButton(f"💊 Вылечить ({heal_cost}🍁)", callback_data=f"pet:heal:{chat_id}")])
    rows.append([InlineKeyboardButton(f"🍖 Корм ({feed_cost}🍁)", callback_data=f"pet:feed:{chat_id}"), InlineKeyboardButton(f"🎾 Игра ({play_cost}🍁)", callback_data=f"pet:play:{chat_id}")])
    rows.append([InlineKeyboardButton(f"⬆️ Прокачать ({upgrade_cost}🍁)", callback_data=f"pet:upgrade:{chat_id}")])
    rows.append([InlineKeyboardButton(f"🍁 Собрать (+{collect_reward})", callback_data=f"pet:collect:{chat_id}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_pet_adopt(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if u['pet_name']: return await q.answer("Уже есть", show_alert=True)
    cost = int(db_get_setting("pet_cost", "500"))
    if u['coins'] < cost: return await q.answer("Мало листиков", show_alert=True)
    u['coins'] -= cost
    u['pet_name'] = random.choice(["Кленовый Котенок","Осенний Лисенок","Лесной Ежик","Золотой Совенок","Красный Бельчонок"])
    u['pet_level'] = 1; u['pet_hunger'] = 100; u['pet_happy'] = 100; u['pet_last_time'] = time.time(); u['pet_sick'] = 0
    db_save_user(q.from_user.id, **u)
    await q.answer(f"Завел: {u['pet_name']}")
    await cb_pet_main(update, ctx)

async def cb_pet_feed(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_name']: return await q.answer("Нет питомца", show_alert=True)
    cost = int(db_get_setting("pet_feed_cost", "30")) + (u['pet_level']-1) * int(db_get_setting("pet_feed_cost_per_level", "15"))
    if u['coins'] < cost: return await q.answer("Мало", show_alert=True)
    u['coins'] -= cost
    u['pet_hunger'] = min(100, u['pet_hunger'] + int(db_get_setting("pet_feed_restore", "60")))
    db_save_user(q.from_user.id, **u)
    await q.answer("Покормлен")
    await cb_pet_main(update, ctx)

async def cb_pet_play(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_name']: return await q.answer("Нет", show_alert=True)
    cost = int(db_get_setting("pet_play_cost", "20")) + (u['pet_level']-1) * int(db_get_setting("pet_play_cost_per_level", "10"))
    if u['coins'] < cost: return await q.answer("Мало", show_alert=True)
    u['coins'] -= cost
    u['pet_happy'] = min(100, u['pet_happy'] + int(db_get_setting("pet_play_restore", "60")))
    db_save_user(q.from_user.id, **u)
    await q.answer("Поиграл")
    await cb_pet_main(update, ctx)

async def cb_pet_upgrade(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_name']: return await q.answer("Нет", show_alert=True)
    cost = u['pet_level'] * int(db_get_setting("pet_upgrade_cost_base", "500"))
    if u['coins'] < cost: return await q.answer("Мало", show_alert=True)
    u['coins'] -= cost; u['pet_level'] += 1
    db_save_user(q.from_user.id, **u)
    await q.answer(f"Ур. {u['pet_level']}")
    await cb_pet_main(update, ctx)

async def cb_pet_heal(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_sick']: return await q.answer("Не болен", show_alert=True)
    cost = u['pet_level'] * int(db_get_setting("pet_heal_cost_per_level", "300"))
    if u['coins'] < cost: return await q.answer("Мало", show_alert=True)
    u['coins'] -= cost; u['pet_sick'] = 0
    db_save_user(q.from_user.id, **u)
    await q.answer("Вылечен")
    await cb_pet_main(update, ctx)

async def cb_pet_collect(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if not u['pet_name']: return await q.answer("Нет", show_alert=True)
    if u['pet_sick']: return await q.answer("Болен!", show_alert=True)
    if u['pet_hunger'] < 50 or u['pet_happy'] < 50: return await q.answer("Голоден/грустит", show_alert=True)
    reward = int(db_get_setting("pet_base_yield", "25")) + (u['pet_level']-1) * int(db_get_setting("pet_yield_per_level", "8"))
    u['coins'] += reward
    u['pet_hunger'] = max(0, u['pet_hunger'] - 20); u['pet_happy'] = max(0, u['pet_happy'] - 20)
    if u['pet_hunger'] < 30 or u['pet_happy'] < 30: u['pet_sick'] = 1
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id)
    await q.answer(f"+{reward}🍁")
    await cb_pet_main(update, ctx)


async def cb_farm_main(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    now = time.time()
    text = f"🌾 <b>Ферма</b>\nГрядок: {u['farm_slots']}\n\n"
    rows = []
    for i, crop in enumerate(u['farm_data']):
        if crop['finish'] > now:
            text += f"🌱 {crop['name']} — {int(crop['finish']-now)} сек\n"
        else:
            text += f"🌟 {crop['name']} — готово\n"
            rows.append([InlineKeyboardButton(f"Собрать {crop['name']}", callback_data=f"farm:harvest:{chat_id}:{i}")])
    if len(u['farm_data']) < u['farm_slots']:
        rows.append([InlineKeyboardButton("🌱 Посадить", callback_data=f"farm:plant_menu:{chat_id}")])
    price = 100 * (u['farm_slots'] ** 2)
    rows.append([InlineKeyboardButton(f"🪵 Грядка ({price}🍁)", callback_data=f"farm:buy_plot:{chat_id}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_farm_plant_menu(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    seeds = [("acorn","🌰 Желудь",10,"farm_acorn_time",60),("mushroom","🍄 Гриб",50,"farm_mushroom_time",300),("pumpkin","🎃 Тыква",100,"farm_pumpkin_time",900),("syrup","🍁 Сироп",200,"farm_syrup_time",1800),("sunflower","🌻 Подсолнух",500,"farm_sunflower_time",3600)]
    rows = []
    for sid, name, cost, tk, d in seeds:
        t = int(db_get_setting(tk, str(d)))
        rows.append([InlineKeyboardButton(f"{name} ({cost}🍁) — {t//60} мин", callback_data=f"farm:plant:{chat_id}:{sid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"farm:main:{chat_id}")])
    await q.edit_message_text("🌱 Что посадить?", reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_farm_plant(update, ctx):
    q = update.callback_query
    if not q: return
    _, _, chat_id, seed = q.data.split(":")
    u = db_get_user(q.from_user.id, q.from_user)
    if len(u['farm_data']) >= u['farm_slots']: return await q.answer("Нет мест", show_alert=True)
    seeds = {
        "acorn":{"name":"Желудь","cost":10,"time":"farm_acorn_time","max":"farm_acorn_max","default":"60","maxdef":"15"},
        "mushroom":{"name":"Гриб","cost":50,"time":"farm_mushroom_time","max":"farm_mushroom_max","default":"300","maxdef":"70"},
        "pumpkin":{"name":"Тыква","cost":100,"time":"farm_pumpkin_time","max":"farm_pumpkin_max","default":"900","maxdef":"180"},
        "syrup":{"name":"Сироп","cost":200,"time":"farm_syrup_time","max":"farm_syrup_max","default":"1800","maxdef":"400"},
        "sunflower":{"name":"Подсолнух","cost":500,"time":"farm_sunflower_time","max":"farm_sunflower_max","default":"3600","maxdef":"1000"}}
    s = seeds.get(seed)
    if not s: return await q.answer("Неверный тип")
    if u['coins'] < s['cost']: return await q.answer("Мало", show_alert=True)
    u['coins'] -= s['cost']
    u['farm_data'].append({"name":s['name'],"finish":time.time()+int(db_get_setting(s['time'],s['default'])),"min_yield":0,"max_yield":int(db_get_setting(s['max'],s['maxdef']))})
    db_save_user(q.from_user.id, **u)
    await q.answer("Посажено")
    await cb_farm_main(update, ctx)

async def cb_farm_harvest(update, ctx):
    q = update.callback_query
    if not q: return
    _, _, chat_id, idx = q.data.split(":")
    idx = int(idx)
    u = db_get_user(q.from_user.id, q.from_user)
    if idx >= len(u['farm_data']): return await q.answer("Ошибка")
    crop = u['farm_data'][idx]
    if crop['finish'] > time.time(): return await q.answer("Не выросло", show_alert=True)
    reward = random.randint(crop['min_yield'], crop['max_yield'])
    u['coins'] += reward
    u['farm_data'].pop(idx); u['farm_harvests'] += 1
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id); _check_mini_game_achievements(q.from_user.id)
    if reward > 0:
        await q.answer(f"+{reward}🍁")
        text = f"🌾 {crop['name']}: <b>+{reward}🍁</b>\nБаланс: {_balance_str(u['coins'])}\nВсего: {u['farm_harvests']}"
    else:
        await q.answer("Неурожай")
        text = f"💀 {crop['name']}: неурожай.\nБаланс: {_balance_str(u['coins'])}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Ферма", callback_data=f"farm:main:{chat_id}")]])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def cb_farm_buy_plot(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    price = 100 * (u['farm_slots'] ** 2)
    if u['coins'] < price: return await q.answer("Мало", show_alert=True)
    u['coins'] -= price; u['farm_slots'] += 1
    db_save_user(q.from_user.id, **u)
    await q.answer("Куплено")
    await cb_farm_main(update, ctx)


async def cb_fish_main(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    now = time.time(); cd = max(0, u['fish_cd'] - now)
    adv = u['inventory'].get('bait_advanced', 0)
    lux = u['inventory'].get('bait_luxury', 0)
    text = (f"🎣 <b>Осеннее Озеро</b>\n\n"
            f"Удочка: <b>Ур. {u['fish_rod']}</b>\n"
            f"🪱 Обычная: <b>{u['fish_bait']}</b> шт.\n"
            f"🐛 Улучшенная: <b>{adv}</b> шт.\n"
            f"🦐 Премиум: <b>{lux}</b> шт.\n"
            f"Поймано рыбы: <b>{u['fish_caught']}</b> шт.\n\n")
    rows = []
    if cd > 0:
        text += f"⏳ Отдых после заброса: <b>{int(cd)} сек.</b>"
    else:
        text += "🌊 Озеро спокойно. Можно забрасывать."
        if u['fish_bait'] > 0:
            rows.append([InlineKeyboardButton(f"🎣 Заброс (🪱 обычная, x{u['fish_bait']})", callback_data=f"fish:cast:{chat_id}:basic")])
        if adv > 0:
            rows.append([InlineKeyboardButton(f"🎣 Заброс (🐛 улучшенная, x{adv})", callback_data=f"fish:cast:{chat_id}:advanced")])
        if lux > 0:
            rows.append([InlineKeyboardButton(f"🎣 Заброс (🦐 премиум, x{lux})", callback_data=f"fish:cast:{chat_id}:luxury")])
        if not (u['fish_bait'] or adv or lux):
            text += "\n\n❗ Купи наживку"
    rows.append([InlineKeyboardButton("🪱 Купить обычную (10 шт / 100🍁)", callback_data=f"fish:buy_bait:{chat_id}")])
    price = u['fish_rod'] * 500
    rows.append([InlineKeyboardButton(f"⚙️ Улучшить удочку (Ур. {u['fish_rod']+1} / {price}🍁)", callback_data=f"fish:upgrade:{chat_id}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_fish_cast(update, ctx):
    q = update.callback_query
    if not q: return
    parts = q.data.split(":")
    chat_id, bait_type = int(parts[2]), parts[3]
    u = db_get_user(q.from_user.id, q.from_user)
    if time.time() < u['fish_cd']: return await q.answer("Рано", show_alert=True)
    if bait_type == "basic":
        if u['fish_bait'] <= 0: return await q.answer("Нет обычной", show_alert=True)
        u['fish_bait'] -= 1
        c_boot = int(db_get_setting("fish_chance_boot", "30"))
        c_leaf = int(db_get_setting("fish_chance_leaf", "30"))
        c_carp = int(db_get_setting("fish_chance_carp", "20"))
        c_perch = int(db_get_setting("fish_chance_perch", "12"))
        c_chest = int(db_get_setting("fish_chance_chest", "5"))
        c_key = int(db_get_setting("fish_chance_key", "2"))
        c_pearl = int(db_get_setting("fish_chance_pearl", "1"))
    elif bait_type == "advanced":
        if u['inventory'].get('bait_advanced', 0) <= 0: return await q.answer("Нет улучшенной", show_alert=True)
        u['inventory']['bait_advanced'] -= 1
        if u['inventory']['bait_advanced'] == 0: del u['inventory']['bait_advanced']
        c_boot, c_leaf, c_carp, c_perch, c_chest, c_key, c_pearl = 15, 20, 30, 20, 10, 3, 2
    else:
        if u['inventory'].get('bait_luxury', 0) <= 0: return await q.answer("Нет премиум", show_alert=True)
        u['inventory']['bait_luxury'] -= 1
        if u['inventory']['bait_luxury'] == 0: del u['inventory']['bait_luxury']
        c_boot, c_leaf, c_carp, c_perch, c_chest, c_key, c_pearl = 5, 10, 30, 25, 15, 7, 8
    u['fish_cd'] = time.time() + 600
    roll = random.randint(1, 100)
    rod = u['fish_rod']
    r_leaf = int(db_get_setting("fish_reward_leaf", "1"))
    r_carp = int(db_get_setting("fish_reward_carp", "3"))
    r_perch = int(db_get_setting("fish_reward_perch", "6"))
    r_chest = int(db_get_setting("fish_reward_chest", "15"))
    r_key = int(db_get_setting("fish_reward_key", "40"))
    r_pearl = int(db_get_setting("fish_reward_pearl", "100"))
    if bait_type == "basic":
        rod_bonus = (rod - 1) * 3
        c_boot = max(10, c_boot - rod_bonus)
        c_leaf = max(10, c_leaf - rod_bonus//2)
    if roll <= c_boot: loot, reward = "🗑 Старый сапог", 0
    elif roll <= c_boot + c_leaf: loot, reward = "🍂 Осенний лист", r_leaf
    elif roll <= c_boot + c_leaf + c_carp: loot, reward = "🐟 Кленовый карась", r_carp
    elif roll <= c_boot + c_leaf + c_carp + c_perch: loot, reward = "🐠 Золотой окунь", r_perch
    elif roll <= c_boot + c_leaf + c_carp + c_perch + c_chest: loot, reward = "📦 Затонувший сундук", r_chest
    elif roll <= c_boot + c_leaf + c_carp + c_perch + c_chest + c_key: loot, reward = "🔑 Ржавый ключ", r_key
    else: loot, reward = "🦪 Жемчужина", r_pearl
    u['coins'] += reward
    if reward > 0: u['fish_caught'] += 1
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id); _check_mini_game_achievements(q.from_user.id)
    if reward > 0:
        await q.answer(f"{loot}!")
        text = f"🎣 <b>Улов:</b>\n\n{loot}\nНаграда: <b>+{reward}🍁</b>\nТвой баланс: {_balance_str(u['coins'])}\n\nВсего поймано: <b>{u['fish_caught']}</b>"
    else:
        await q.answer(f"{loot}")
        text = f"💀 <b>Неудача...</b>\n\nТы вытащил {loot}, но ничего не поймал.\n\nТвой баланс: {_balance_str(u['coins'])}\nВсего поймано: <b>{u['fish_caught']}</b>"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Рыбалка", callback_data=f"fish:main:{chat_id}")]])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def cb_fish_buy_bait(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if u['coins'] < 100: return await q.answer("Мало", show_alert=True)
    u['coins'] -= 100; u['fish_bait'] += 10
    db_save_user(q.from_user.id, **u)
    await q.answer("+10 наживок")
    await cb_fish_main(update, ctx)

async def cb_fish_upgrade(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    price = u['fish_rod'] * 500
    if u['coins'] < price: return await q.answer("Мало", show_alert=True)
    u['coins'] -= price; u['fish_rod'] += 1
    db_save_user(q.from_user.id, **u)
    await q.answer("Улучшено")
    await cb_fish_main(update, ctx)


async def cb_dungeon_main(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    now = time.time(); cd = max(0, u['dungeon_cd'] - now)
    entry_cost = int(db_get_setting("dungeon_entry_cost", "50"))
    potions = u['inventory'].get('dungeon_potion', 0); charms = u['inventory'].get('dungeon_charm', 0)
    text = f"⚔️ <b>Подземелье</b>\n\nОружие: Ур. {u['dungeon_weapon']}\nБроня: Ур. {u['dungeon_armor']}\nПоходов: {u['dungeon_runs']}\n"
    if u['dungeon_bonus'] > 0: text += f"🧪 Бафф: +{u['dungeon_bonus']}%\n"
    text += "\n"
    rows = []
    if cd > 0: text += f"⏳ Кд: {int(cd//3600)}ч {int((cd%3600)//60)}м"
    else:
        text += f"💰 Вход: {entry_cost}🍁"
        rows.append([InlineKeyboardButton(f"🚀 В подземелье ({entry_cost}🍁)", callback_data=f"dungeon:start:{chat_id}")])
    if potions > 0: rows.append([InlineKeyboardButton(f"🧪 Зелье +25% x{potions}", callback_data=f"dungeon:use_potion:{chat_id}")])
    if charms > 0: rows.append([InlineKeyboardButton(f"🧿 Амулет +50% x{charms}", callback_data=f"dungeon:use_charm:{chat_id}")])
    w_price = u['dungeon_weapon'] * 300; a_price = u['dungeon_armor'] * 300
    rows.append([InlineKeyboardButton(f"🗡 Оружие → Ур. {u['dungeon_weapon']+1} ({w_price}🍁)", callback_data=f"dungeon:up_w:{chat_id}")])
    rows.append([InlineKeyboardButton(f"🛡 Броня → Ур. {u['dungeon_armor']+1} ({a_price}🍁)", callback_data=f"dungeon:up_a:{chat_id}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_dungeon_use_potion(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if u['inventory'].get('dungeon_potion', 0) <= 0: return await q.answer("Нет зелий", show_alert=True)
    u['inventory']['dungeon_potion'] -= 1
    if u['inventory']['dungeon_potion'] == 0: del u['inventory']['dungeon_potion']
    u['dungeon_bonus'] += 25
    db_save_user(q.from_user.id, **u)
    await q.answer(f"+25% (итого +{u['dungeon_bonus']}%)")
    await cb_dungeon_main(update, ctx)

async def cb_dungeon_use_charm(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if u['inventory'].get('dungeon_charm', 0) <= 0: return await q.answer("Нет амулетов", show_alert=True)
    u['inventory']['dungeon_charm'] -= 1
    if u['inventory']['dungeon_charm'] == 0: del u['inventory']['dungeon_charm']
    u['dungeon_bonus'] += 50
    db_save_user(q.from_user.id, **u)
    await q.answer(f"+50% (итого +{u['dungeon_bonus']}%)")
    await cb_dungeon_main(update, ctx)

async def cb_dungeon_start(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if time.time() < u['dungeon_cd']: return await q.answer("Кд", show_alert=True)
    entry_cost = int(db_get_setting("dungeon_entry_cost", "50"))
    if u['coins'] < entry_cost: return await q.answer("Мало листиков", show_alert=True)
    u['coins'] -= entry_cost
    power = u['dungeon_weapon'] + u['dungeon_armor']
    base_chance = int(db_get_setting("dungeon_base_chance", "40"))
    mult_chance = int(db_get_setting("dungeon_mult_chance", "3"))
    reward_min = int(db_get_setting("dungeon_reward_min", "10"))
    reward_max = int(db_get_setting("dungeon_reward_max", "50"))
    bonus = u['dungeon_bonus']
    success_chance = min(95, base_chance + power * mult_chance + bonus)
    won = random.randint(1, 100) <= success_chance
    u['dungeon_cd'] = time.time() + 21600; u['dungeon_runs'] += 1; u['dungeon_bonus'] = 0
    if won:
        loot_table = [("🪵 Древесина",1),("⛓ Железо",2),("💎 Кристалл",3),("🏺 Древняя ваза",5),("🗡 Артефакт",10)]
        loot_item, loot_mult = random.choice(loot_table)
        reward = random.randint(reward_min, reward_max) * power * loot_mult
        u['coins'] += reward
        msg = f"⚔️ <b>Успех!</b>\n\nГерой вернулся с ресурсом: <b>{loot_item}</b>!\nДобыча: <b>+{reward}🍁</b>!\n\nТвой баланс: {_balance_str(u['coins'])}\nВсего походов: <b>{u['dungeon_runs']}</b>"
    else:
        msg = f"💀 <b>Поражение...</b>\n\nГерой не справился с монстрами. Вы ничего не получили.\n\nТвой баланс: {_balance_str(u['coins'])}\nВсего походов: <b>{u['dungeon_runs']}</b>"
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id); _check_mini_game_achievements(q.from_user.id)
    await q.answer("Готово")
    try: await q.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Данж", callback_data=f"dungeon:main:{chat_id}")]]))
    except Exception: pass

async def cb_dungeon_up_w(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    price = u['dungeon_weapon'] * 300
    if u['coins'] < price: return await q.answer("Мало", show_alert=True)
    u['coins'] -= price; u['dungeon_weapon'] += 1
    db_save_user(q.from_user.id, **u)
    await q.answer("Улучшено")
    await cb_dungeon_main(update, ctx)

async def cb_dungeon_up_a(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    price = u['dungeon_armor'] * 300
    if u['coins'] < price: return await q.answer("Мало", show_alert=True)
    u['coins'] -= price; u['dungeon_armor'] += 1
    db_save_user(q.from_user.id, **u)
    await q.answer("Улучшено")
    await cb_dungeon_main(update, ctx)


async def cb_menu_bonus(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    u = db_get_user(q.from_user.id, q.from_user)
    if time.time() - u['last_bonus'] < 86400: return await q.answer("Раз в сутки", show_alert=True)
    bonus = 1
    for tdata in TITLES_SHOP.values():
        if tdata['name'] == u['title']: bonus = tdata['daily_bonus']; break
    u['coins'] += bonus; u['last_bonus'] = time.time()
    db_save_user(q.from_user.id, **u)
    _check_rich_achievements(q.from_user.id)
    await q.answer(f"+{bonus}🍁")
    await q.edit_message_text(f"🍁 +{bonus}🍁\nБаланс: {_balance_str(u['coins'])}", parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))

async def cb_menu_top_menu(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 По листикам", callback_data=f"menu:top_coins:{chat_id}")],
        [InlineKeyboardButton("⭐ По уровню", callback_data=f"menu:top_level:{chat_id}")],
        [InlineKeyboardButton("🌾 Фермеры", callback_data=f"menu:top_farm:{chat_id}")],
        [InlineKeyboardButton("🎣 Рыбаки", callback_data=f"menu:top_fish:{chat_id}")],
        [InlineKeyboardButton("⚔️ Данж", callback_data=f"menu:top_dung:{chat_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"menu:main:{chat_id}")]
    ])
    await q.edit_message_text("🏆 <b>Топы</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_menu_top_coins(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, coins, title FROM users_data WHERE coins > 0 ORDER BY coins DESC LIMIT 5").fetchall()
    lines = ["🏆 <b>Топ по листикам</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = r['username']
        link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 {r["user_id"]}'
        lines.append(f"{i}. {link} — {r['coins']}🍁 | {r['title']}")
    if not rows: lines.append("Пусто")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️", callback_data=f"menu:top_menu:{chat_id}")]]), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_top_level(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, level, title FROM users_data WHERE level > 1 ORDER BY level DESC LIMIT 5").fetchall()
    lines = ["⭐ <b>Топ по уровню</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = r['username']
        link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 {r["user_id"]}'
        lines.append(f"{i}. {link} — {r['level']} lvl | {r['title']}")
    if not rows: lines.append("Пусто")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️", callback_data=f"menu:top_menu:{chat_id}")]]), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_top_farm(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, farm_harvests FROM users_data WHERE farm_harvests > 0 ORDER BY farm_harvests DESC LIMIT 5").fetchall()
    lines = ["🌾 <b>Топ Фермеров</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = r['username']
        link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 {r["user_id"]}'
        lines.append(f"{i}. {link} — {r['farm_harvests']}")
    if not rows: lines.append("Пусто")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️", callback_data=f"menu:top_menu:{chat_id}")]]), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_top_fish(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, fish_caught FROM users_data WHERE fish_caught > 0 ORDER BY fish_caught DESC LIMIT 5").fetchall()
    lines = ["🎣 <b>Топ Рыбаков</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = r['username']
        link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 {r["user_id"]}'
        lines.append(f"{i}. {link} — {r['fish_caught']}")
    if not rows: lines.append("Пусто")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️", callback_data=f"menu:top_menu:{chat_id}")]]), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_top_dung(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT user_id, username, dungeon_runs FROM users_data WHERE dungeon_runs > 0 ORDER BY dungeon_runs DESC LIMIT 5").fetchall()
    lines = ["⚔️ <b>Топ Подземельщиков</b>", ""]
    for i, r in enumerate(rows, 1):
        uname = r['username']
        link = f'<a href="https://t.me/{uname}">👤 {html.escape(uname)}</a>' if uname else f'👤 {r["user_id"]}'
        lines.append(f"{i}. {link} — {r['dungeon_runs']}")
    if not rows: lines.append("Пусто")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️", callback_data=f"menu:top_menu:{chat_id}")]]), link_preview_options=LinkPreviewOptions(is_disabled=True))
    await q.answer()

async def cb_menu_buy(update, ctx):
    q = update.callback_query
    if not q: return
    try: chat_id = int(q.data.split(":")[2])
    except Exception: chat_id = 0
    text = f"💎 <b>Покупка листиков</b>\n\n1 листик = 5₽\n\nМенеджер: {SELLER_USERNAME}\n\nID: <code>{q.from_user.id}</code>"
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_menu_back_kb(chat_id))
    await q.answer()


async def cb_admin_main(update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("Нет доступа", show_alert=True)
    CROC_AWAITING.pop(q.from_user.id, None); ADMIN_STATE.pop(q.from_user.id, None)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🍁 Листики", callback_data="admin:act:give_coins")],
        [InlineKeyboardButton("⭐ Уровень", callback_data="admin:act:give_level")],
        [InlineKeyboardButton("👑 Титул", callback_data="admin:act:give_title"), InlineKeyboardButton("🚫 Забрать", callback_data="admin:act:take_title")],
        [InlineKeyboardButton("🔍 Профиль", callback_data="admin:act:view_profile")],
        [InlineKeyboardButton("🚫 Бан", callback_data="admin:act:ban_user"), InlineKeyboardButton("✅ Разбан", callback_data="admin:act:unban_user")],
        [InlineKeyboardButton("📦 Кейсы", callback_data="admin:case")],
        [InlineKeyboardButton("⚙️ Экономика", callback_data="admin:econ")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main:0")]
    ])
    await q.edit_message_text("🛠 <b>Админ</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_admin_case(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 Wood", callback_data="admin:case_edit:wood")],[InlineKeyboardButton("🎁 Silver", callback_data="admin:case_edit:silver")],[InlineKeyboardButton("💎 Gold", callback_data="admin:case_edit:gold")],[InlineKeyboardButton("◀️", callback_data="admin:main")]])
    await q.edit_message_text("📦 Выбери кейс:", reply_markup=kb)
    await q.answer()

async def cb_admin_case_edit(update, ctx):
    q = update.callback_query
    c = q.data.split(":")[2]
    chance = db_get_setting(f"case_{c}_chance", "45"); max_win = db_get_setting(f"case_{c}_max_win", "100")
    text = f"📦 <b>{c}</b>\nШанс: {chance}%\nМакс: {max_win}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Шанс", callback_data=f"admin:case_set:{c}:chance")],[InlineKeyboardButton("📈 Макс", callback_data=f"admin:case_set:{c}:max_win")],[InlineKeyboardButton("◀️", callback_data="admin:case")]])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_admin_case_set(update, ctx):
    q = update.callback_query
    _, _, c, setting = q.data.split(":")
    ADMIN_STATE[q.from_user.id] = f"case_{c}_{setting}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin:cancel_input")]])
    await q.edit_message_text(f"🛠 Отправь {setting} для {c}:", reply_markup=kb)
    await q.answer()

async def cb_admin_econ(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌾 Ферма", callback_data="admin:econ_farm")],[InlineKeyboardButton("🎣 Рыбалка", callback_data="admin:econ_fish")],[InlineKeyboardButton("⚔️ Данж", callback_data="admin:econ_dungeon")],[InlineKeyboardButton("🐾 Питомец", callback_data="admin:econ_pet")],[InlineKeyboardButton("◀️", callback_data="admin:main")]])
    await q.edit_message_text("⚙️ Экономика:", reply_markup=kb)
    await q.answer()

async def cb_admin_econ_pet(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    items = [("pet_cost","Цена покупки"),("pet_feed_cost","Корм база"),("pet_feed_cost_per_level","Корм /ур"),("pet_play_cost","Игра база"),("pet_play_cost_per_level","Игра /ур"),("pet_upgrade_cost_base","Прокачка"),("pet_heal_cost_per_level","Лечение"),("pet_base_yield","Доход база"),("pet_yield_per_level","Доход /ур"),("pet_decay_time","Голодание")]
    rows = [[InlineKeyboardButton(name, callback_data=f"admin:econ_set:{key}")] for key, name in items]
    rows.append([InlineKeyboardButton("◀️", callback_data="admin:econ")])
    text = "🐾 <b>Питомец</b>\n"
    for k, name in items: text += f"{name}: {db_get_setting(k)}\n"
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_admin_econ_farm(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌰", callback_data="admin:econ_farm_edit:acorn")],[InlineKeyboardButton("🍄", callback_data="admin:econ_farm_edit:mushroom")],[InlineKeyboardButton("🎃", callback_data="admin:econ_farm_edit:pumpkin")],[InlineKeyboardButton("🍁", callback_data="admin:econ_farm_edit:syrup")],[InlineKeyboardButton("🌻", callback_data="admin:econ_farm_edit:sunflower")],[InlineKeyboardButton("◀️", callback_data="admin:econ")]])
    await q.edit_message_text("🌾 Ферма:", reply_markup=kb)
    await q.answer()

async def cb_admin_econ_farm_edit(update, ctx):
    q = update.callback_query
    c = q.data.split(":")[2]
    t = db_get_setting(f"farm_{c}_time", "60"); mx = db_get_setting(f"farm_{c}_max", "15")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏱ Время", callback_data=f"admin:econ_set:farm_{c}_time")],[InlineKeyboardButton("📈 Макс", callback_data=f"admin:econ_set:farm_{c}_max")],[InlineKeyboardButton("◀️", callback_data="admin:econ_farm")]])
    await q.edit_message_text(f"🌱 {c}\nВремя: {t}\nМакс: {mx}", reply_markup=kb)
    await q.answer()

async def cb_admin_econ_fish(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💵 Награды", callback_data="admin:econ_fish_rew")],[InlineKeyboardButton("◀️", callback_data="admin:econ")]])
    await q.edit_message_text("🎣 Рыбалка:", reply_markup=kb)
    await q.answer()

async def cb_admin_econ_fish_rew(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    items = [("fish_reward_leaf","Лист"),("fish_reward_carp","Карась"),("fish_reward_perch","Окунь"),("fish_reward_chest","Сундук"),("fish_reward_key","Ключ"),("fish_reward_pearl","Жемчуг")]
    rows = [[InlineKeyboardButton(f"{n}: {db_get_setting(k)}", callback_data=f"admin:econ_set:{k}")] for k,n in items]
    rows.append([InlineKeyboardButton("◀️", callback_data="admin:econ_fish")])
    await q.edit_message_text("💰 Награды:", reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_admin_econ_dungeon(update, ctx):
    q = update.callback_query
    ADMIN_STATE.pop(q.from_user.id, None)
    items = [("dungeon_entry_cost","Вход"),("dungeon_base_chance","Шанс база"),("dungeon_mult_chance","Бонус силы"),("dungeon_reward_min","Мин"),("dungeon_reward_max","Макс")]
    rows = [[InlineKeyboardButton(f"{n}: {db_get_setting(k)}", callback_data=f"admin:econ_set:{k}")] for k,n in items]
    rows.append([InlineKeyboardButton("◀️", callback_data="admin:econ")])
    await q.edit_message_text("⚔️ Данж:", reply_markup=InlineKeyboardMarkup(rows))
    await q.answer()

async def cb_admin_econ_set(update, ctx):
    q = update.callback_query
    setting = q.data.split(":")[2]
    ADMIN_STATE[q.from_user.id] = setting
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin:cancel_input")]])
    await q.edit_message_text(f"🛠 Отправь новое значение для <b>{setting}</b>:", parse_mode=ParseMode.HTML, reply_markup=kb)
    await q.answer()

async def cb_admin_act(update, ctx):
    q = update.callback_query
    act = q.data.split(":")[2]
    ADMIN_STATE[q.from_user.id] = act
    prompts = {
        "give_coins": "ID количество (с минусом — списать)",
        "give_level": "ID уровень",
        "give_title": "ID Титул",
        "take_title": "ID Титул",
        "view_profile": "ID",
        "ban_user": "ID",
        "unban_user": "ID"
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin:cancel_input")]])
    await q.edit_message_text(f"🛠 {prompts[act]}:", reply_markup=kb)
    await q.answer()

async def cb_admin_cancel_input(update, ctx):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return await q.answer("Нет доступа", show_alert=True)
    ADMIN_STATE.pop(q.from_user.id, None)
    await cb_admin_main(update, ctx)


async def cb_croc_new(update, ctx):
    q = update.callback_query
    if q.message.chat.id not in CROC_ACTIVE: return await q.answer("Остановлено", show_alert=True)
    if q.message.chat.id in CROC_GAME: return await q.answer("Уже загадано", show_alert=True)
    awaiting = [u for u, c in CROC_AWAITING.items() if c == q.message.chat.id]
    if awaiting: return await q.answer("Кто-то загадывает", show_alert=True)
    CROC_AWAITING[q.from_user.id] = q.message.chat.id
    try: await q.edit_message_text(f"🐊 <b>{get_safe_name(q.from_user)}</b> загадывает...")
    except Exception: pass
    await q.answer("Напиши в ЛС!", show_alert=True)
    try: await ctx.bot.send_message(q.from_user.id, "🐊 Отправь: <code>Слово | Описание</code>", parse_mode=ParseMode.HTML)
    except Exception:
        CROC_AWAITING.pop(q.from_user.id, None)
        await q.answer("Сначала /start в ЛС!", show_alert=True)

async def croc_guess_mw(update, ctx):
    msg = update.effective_message
    if not msg or msg.chat.type == "private" or not _check_thread(update): return
    game = CROC_GAME.get(msg.chat.id)
    if not game or not game.get("word") or not msg.text: return
    if msg.text.strip().lower() == game["word"].lower():
        CROC_GAME.pop(msg.chat.id, None)
        try: await msg.set_reaction("✅")
        except Exception: pass
        u = db_get_user(msg.from_user.id, msg.from_user)
        u['coins'] += 3; u['croc_wins'] += 1
        u['level'] = max((u['wins']*2 + u['games'] + u['croc_wins'])//5 + 1, u['level'])
        if "croc_1" not in u['achievements']: u['achievements'].append("croc_1")
        if u['croc_wins'] >= 10 and "croc_10" not in u['achievements']: u['achievements'].append("croc_10")
        if u['croc_wins'] >= 50 and "croc_50" not in u['achievements']: u['achievements'].append("croc_50")
        if u['croc_wins'] >= 100 and "croc_100" not in u['achievements']: u['achievements'].append("croc_100")
        db_save_user(msg.from_user.id, **u)
        _check_rich_achievements(msg.from_user.id)
        try: await ctx.bot.send_message(msg.from_user.id, f"🐊 +3🍁 за <b>{game['word']}</b>", parse_mode=ParseMode.HTML)
        except Exception: pass
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Загадать", callback_data="croc:new")]])
        await msg.reply_text(f"🎉 <b>{get_safe_name(msg.from_user)}</b> угадал: <b>{game['word']}</b>! +3🍁", parse_mode=ParseMode.HTML, reply_markup=kb)
        raise ApplicationHandlerStop
    else:
        try: await msg.set_reaction("❌")
        except Exception: pass

async def cb_noop(update, ctx):
    await update.callback_query.answer()

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception:", exc_info=context.error)


def _build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    g = filters.ChatType.GROUPS
    app.add_handler(MessageHandler(filters.ALL, ban_middleware), group=-500)
    app.add_handler(CallbackQueryHandler(ban_callback_mw), group=-500)
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
    app.add_handler(CallbackQueryHandler(cb_shop_cat, pattern=r"^shop:cat:"))
    app.add_handler(CallbackQueryHandler(cb_menu_inv, pattern=r"^menu:inv:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_menu, pattern=r"^menu:top_menu:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_coins, pattern=r"^menu:top_coins:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_level, pattern=r"^menu:top_level:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_farm, pattern=r"^menu:top_farm:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_fish, pattern=r"^menu:top_fish:"))
    app.add_handler(CallbackQueryHandler(cb_menu_top_dung, pattern=r"^menu:top_dung:"))
    app.add_handler(CallbackQueryHandler(cb_menu_bonus, pattern=r"^menu:bonus:"))
    app.add_handler(CallbackQueryHandler(cb_menu_buy, pattern=r"^menu:buy:"))
    app.add_handler(CallbackQueryHandler(cb_pet_main, pattern=r"^pet:main:"))
    app.add_handler(CallbackQueryHandler(cb_pet_adopt, pattern=r"^pet:adopt:"))
    app.add_handler(CallbackQueryHandler(cb_pet_feed, pattern=r"^pet:feed:"))
    app.add_handler(CallbackQueryHandler(cb_pet_play, pattern=r"^pet:play:"))
    app.add_handler(CallbackQueryHandler(cb_pet_upgrade, pattern=r"^pet:upgrade:"))
    app.add_handler(CallbackQueryHandler(cb_pet_heal, pattern=r"^pet:heal:"))
    app.add_handler(CallbackQueryHandler(cb_pet_collect, pattern=r"^pet:collect:"))
    app.add_handler(CallbackQueryHandler(cb_farm_main, pattern=r"^farm:main:"))
    app.add_handler(CallbackQueryHandler(cb_farm_plant_menu, pattern=r"^farm:plant_menu:"))
    app.add_handler(CallbackQueryHandler(cb_farm_plant, pattern=r"^farm:plant:"))
    app.add_handler(CallbackQueryHandler(cb_farm_harvest, pattern=r"^farm:harvest:"))
    app.add_handler(CallbackQueryHandler(cb_farm_buy_plot, pattern=r"^farm:buy_plot:"))
    app.add_handler(CallbackQueryHandler(cb_fish_main, pattern=r"^fish:main:"))
    app.add_handler(CallbackQueryHandler(cb_fish_cast, pattern=r"^fish:cast:"))
    app.add_handler(CallbackQueryHandler(cb_fish_buy_bait, pattern=r"^fish:buy_bait:"))
    app.add_handler(CallbackQueryHandler(cb_fish_upgrade, pattern=r"^fish:upgrade:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_main, pattern=r"^dungeon:main:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_use_potion, pattern=r"^dungeon:use_potion:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_use_charm, pattern=r"^dungeon:use_charm:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_start, pattern=r"^dungeon:start:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_up_w, pattern=r"^dungeon:up_w:"))
    app.add_handler(CallbackQueryHandler(cb_dungeon_up_a, pattern=r"^dungeon:up_a:"))
    app.add_handler(CallbackQueryHandler(cb_shop_item, pattern=r"^shop:item:"))
    app.add_handler(CallbackQueryHandler(cb_shop_buy, pattern=r"^shop:buy:"))
    app.add_handler(CallbackQueryHandler(cb_inv_item, pattern=r"^inv:item:"))
    app.add_handler(CallbackQueryHandler(cb_inv_use, pattern=r"^inv:use:"))
    app.add_handler(CallbackQueryHandler(cb_admin_main, pattern=r"^admin:main$"))
    app.add_handler(CallbackQueryHandler(cb_admin_act, pattern=r"^admin:act:"))
    app.add_handler(CallbackQueryHandler(cb_admin_case, pattern=r"^admin:case$"))
    app.add_handler(CallbackQueryHandler(cb_admin_case_edit, pattern=r"^admin:case_edit:"))
    app.add_handler(CallbackQueryHandler(cb_admin_case_set, pattern=r"^admin:case_set:"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ, pattern=r"^admin:econ$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_pet, pattern=r"^admin:econ_pet$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_farm, pattern=r"^admin:econ_farm$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_farm_edit, pattern=r"^admin:econ_farm_edit:"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_fish, pattern=r"^admin:econ_fish$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_fish_rew, pattern=r"^admin:econ_fish_rew$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_dungeon, pattern=r"^admin:econ_dungeon$"))
    app.add_handler(CallbackQueryHandler(cb_admin_econ_set, pattern=r"^admin:econ_set:"))
    app.add_handler(CallbackQueryHandler(cb_admin_cancel_input, pattern=r"^admin:cancel_input$"))
    app.add_handler(CallbackQueryHandler(cb_croc_new, pattern=r"^croc:new$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))
    app.add_error_handler(_error_handler)
    return app

async def _run_bot():
    app = _build_app()
    await app.initialize()
    await app.bot.set_my_commands([])
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    await asyncio.Event().wait()

def main():
    try: asyncio.run(_run_bot())
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
