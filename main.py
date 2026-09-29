import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================ SOZLAMALAR
BOT_TOKEN = os.getenv("bott", "")
SUPER_ADMIN_ID = int(os.getenv("id", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "data/pro_uc_bot.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
ORDER_EXPIRE_MIN = int(os.getenv("ORDER_EXPIRE_MIN", "30"))

TZ = timezone(timedelta(hours=5))  # Toshkent vaqti
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pro_uc_bot")

DEFAULT_SETTINGS = {
    "card_number": "", "card_owner": "",
    "coin_value": "50",          # 1 tanga = 50 so'm chegirma
    "coin_buy_price": "100",     # 1 tanga sotib olish = 100 so'm (arbitrajga yo'l qo'ymaydi)
    "coin_max_percent": "30",    # narxning maks. necha foizini tanga bilan yopish mumkin
    "welcome_bonus": "5",
    "ref_bonus": "20",
    "ref_purchase_bonus": "20",
    "daily_bonus": "1",
    "cashback_per_100uc": "2",
    "vip_threshold": "600",
    "vip_cashback_mult": "2",
    "topup_min": "5000",
    "support": "@admin",
}
SETTING_META = {
    "coin_value": ("1 tanga = necha so'm chegirma", "int"),
    "coin_buy_price": ("1 tanga sotib olish narxi (so'm)", "int"),
    "coin_max_percent": ("Tanga bilan maks. chegirma (%)", "int"),
    "welcome_bonus": ("Yangi foydalanuvchiga tanga", "int"),
    "ref_bonus": ("Do'st qo'shgani uchun tanga", "int"),
    "ref_purchase_bonus": ("Do'st 1-xaridida referalga tanga", "int"),
    "daily_bonus": ("Kunlik bonus (tanga)", "int"),
    "cashback_per_100uc": ("Har 100 UC uchun keshbek (tanga)", "int"),
    "vip_threshold": ("VIP uchun UC chegarasi", "int"),
    "vip_cashback_mult": ("VIP keshbek ko'paytmasi", "int"),
    "topup_min": ("Minimal hisob to'ldirish (so'm)", "int"),
    "support": ("Yordam kontakti (@username)", "str"),
}

STATUS_EMOJI = {"awaiting_check": "⏳", "checking": "🔎", "done": "✅", "cancelled": "❌"}
STATUS_TEXT = {"awaiting_check": "To'lov kutilmoqda", "checking": "Tekshirilmoqda",
               "done": "Bajarildi", "cancelled": "Bekor qilindi"}


# ============================================================ MA'LUMOTLAR BAZASI
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, full_name TEXT, username TEXT,
    balance INTEGER NOT NULL DEFAULT 0, coins INTEGER NOT NULL DEFAULT 0,
    total_uc INTEGER NOT NULL DEFAULT 0, is_vip INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER, banned INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
    last_daily TEXT, joined_at TEXT DEFAULT (datetime('now','+5 hours'))
);
CREATE TABLE IF NOT EXISTS admins(
    id INTEGER PRIMARY KEY, added_at TEXT DEFAULT (datetime('now','+5 hours'))
);
CREATE TABLE IF NOT EXISTS packages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, uc INTEGER NOT NULL,
    price INTEGER NOT NULL, cost INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, user_id INTEGER NOT NULL,
    pkg_id INTEGER, pkg_name TEXT, uc INTEGER NOT NULL, price INTEGER NOT NULL,
    final INTEGER NOT NULL, cost INTEGER NOT NULL,
    coupon_id INTEGER, coupon_code TEXT, coupon_disc INTEGER NOT NULL DEFAULT 0,
    coin_used INTEGER NOT NULL DEFAULT 0, coin_disc INTEGER NOT NULL DEFAULT 0,
    pubg_id TEXT NOT NULL, pay_method TEXT NOT NULL, status TEXT NOT NULL,
    check_file_id TEXT, admin_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','+5 hours')), done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS topups(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL, status TEXT NOT NULL, check_file_id TEXT, admin_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','+5 hours')), done_at TEXT
);
CREATE TABLE IF NOT EXISTS coupons(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    discount INTEGER NOT NULL, min_uc INTEGER NOT NULL DEFAULT 0,
    limit_count INTEGER NOT NULL, used_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS coupon_uses(
    coupon_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(coupon_id, user_id)
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE, title TEXT, link TEXT
);
CREATE TABLE IF NOT EXISTS lottery_log(
    month TEXT PRIMARY KEY, user_id INTEGER, uc INTEGER, cost INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','+5 hours'))
);
CREATE TABLE IF NOT EXISTS fsm(key TEXT PRIMARY KEY, state TEXT, data TEXT);
"""


class Fail(Exception):
    """Tranzaksiyani bekor qilish uchun."""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        if SUPER_ADMIN_ID:
            await self.conn.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (SUPER_ADMIN_ID,))

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def fetchone(self, q, args=()):
        cur = await self.conn.execute(q, args)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, q, args=()):
        cur = await self.conn.execute(q, args)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def execute(self, q, args=()):
        """(lastrowid, rowcount) qaytaradi."""
        async with self.lock:
            cur = await self.conn.execute(q, args)
            res = (cur.lastrowid, cur.rowcount)
            await cur.close()
            return res

    @asynccontextmanager
    async def tx(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")


db = Database(DB_PATH)


async def _one(conn, q, args=()):
    cur = await conn.execute(q, args)
    row = await cur.fetchone()
    await cur.close()
    return row


class SQLiteStorage(BaseStorage):
    """FSM holatlari ham bazada saqlanadi — bot qayta ishga tushsa ham yo'qolmaydi."""

    def __init__(self, database: Database):
        self.db = database

    @staticmethod
    def _k(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id}:{key.destiny}"

    async def set_state(self, key: StorageKey, state=None) -> None:
        s = state.state if isinstance(state, State) else state
        await self.db.execute(
            "INSERT INTO fsm(key,state) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
            (self._k(key), s))

    async def get_state(self, key: StorageKey) -> Optional[str]:
        row = await self.db.fetchone("SELECT state FROM fsm WHERE key=?", (self._k(key),))
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data) -> None:
        await self.db.execute(
            "INSERT INTO fsm(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (self._k(key), json.dumps(dict(data))))

    async def get_data(self, key: StorageKey) -> dict:
        row = await self.db.fetchone("SELECT data FROM fsm WHERE key=?", (self._k(key),))
        return json.loads(row["data"]) if row and row["data"] else {}

    async def update_data(self, key: StorageKey, data) -> dict:
        cur = await self.get_data(key)
        cur.update(data)
        await self.set_data(key, cur)
        return cur.copy()

    async def close(self) -> None:
        return None


# ============================================================ YORDAMCHI FUNKSIYALAR
def now_tz() -> datetime:
    return datetime.now(TZ)


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def esc(s) -> str:
    return html.escape(str(s or ""))


def parse_int(text: str) -> Optional[int]:
    t = re.sub(r"[\s,._]", "", (text or ""))
    if t.lstrip("-").isdigit():
        return int(t)
    return None


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name) or uid}</a>'


async def S(key: str) -> str:
    row = await db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


async def Si(key: str) -> int:
    try:
        return int(await S(key))
    except ValueError:
        return int(DEFAULT_SETTINGS.get(key, "0") or 0)


async def _seti(conn, key: str) -> int:
    row = await _one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    try:
        return int(row["value"]) if row else int(DEFAULT_SETTINGS[key])
    except ValueError:
        return int(DEFAULT_SETTINGS[key])


async def is_admin(uid: int) -> bool:
    return bool(await db.fetchone("SELECT 1 FROM admins WHERE id=?", (uid,)))


async def admin_ids() -> list:
    return [r["id"] for r in await db.fetchall("SELECT id FROM admins")]


async def get_user(uid: int):
    return await db.fetchone("SELECT * FROM users WHERE id=?", (uid,))


async def register_user(tg_user, ref_id: Optional[int] = None):
    """(user, yangi_mi, referal_ishladimi)"""
    u = await get_user(tg_user.id)
    name = tg_user.full_name or ""
    if u:
        await db.execute("UPDATE users SET full_name=?, username=?, blocked=0 WHERE id=?",
                         (name, tg_user.username, tg_user.id))
        return await get_user(tg_user.id), False, False
    if ref_id == tg_user.id or (ref_id and not await get_user(ref_id)):
        ref_id = None
    welcome = await Si("welcome_bonus")
    _, rc = await db.execute(
        "INSERT OR IGNORE INTO users(id,full_name,username,coins,referrer_id) VALUES(?,?,?,?,?)",
        (tg_user.id, name, tg_user.username, welcome, ref_id))
    if not rc:
        return await get_user(tg_user.id), False, False
    if ref_id:
        await db.execute("UPDATE users SET coins=coins+? WHERE id=?", (await Si("ref_bonus"), ref_id))
    return await get_user(tg_user.id), True, bool(ref_id)


async def gen_code() -> str:
    for _ in range(60):
        code = f"#{random.randint(1000, 9999)}"
        row = await db.fetchone(
            "SELECT 1 FROM orders WHERE code=? AND status IN ('awaiting_check','checking') "
            "UNION SELECT 1 FROM topups WHERE code=? AND status IN ('awaiting_check','checking')",
            (code, code))
        if not row:
            return code
    return f"#{random.randint(100000, 999999)}"


async def safe_edit(msg: Message, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            await msg.answer(text, reply_markup=kb)


async def send_media(bot: Bot, chat_id: int, fid: Optional[str], text: str, kb=None):
    if not fid:
        return await bot.send_message(chat_id, text, reply_markup=kb)
    if fid.startswith("d:"):
        return await bot.send_document(chat_id, fid[2:], caption=text, reply_markup=kb)
    return await bot.send_photo(chat_id, fid[2:], caption=text, reply_markup=kb)


async def tell(bot: Bot, uid: int, text: str, **kw):
    try:
        await bot.send_message(uid, text, **kw)
    except (TelegramForbiddenError, TelegramBadRequest):
        await db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
    except Exception:
        log.exception("tell xatosi")


# ============================================================ TUGMA MATNLARI VA KLAVIATURALAR
class T:
    BUY = "🛒 UC sotib olish"
    TOPUP = "💰 Hisobni to'ldirish"
    PROFILE = "👤 Profil"
    ORDERS = "📦 Buyurtmalarim"
    COINS = "🪙 Tangalar"
    DAILY = "🎁 Kunlik bonus"
    REF = "👥 Referal"
    RATING = "🏆 Reyting"
    HELP = "☎️ Yordam"
    ADMIN = "⚙️ Admin Panel"
    CANCEL = "❌ Bekor qilish"
    BACK = "🔙 Bosh menyu"
    A_ADMIN = "➕ Admin qo'shish"
    A_CHANNEL = "📢 Majburiy obuna"
    A_BCAST = "📣 Ommaviy xabar"
    A_PKG = "➕ UC paket qo'shish"
    A_PKGS = "📋 Paketlar"
    A_CARD = "💳 Karta ma'lumoti"
    A_STATS = "📊 Statistika"
    A_COUPON = "🎟 Kupon yaratish"
    A_COUPONS = "🎫 Kuponlar"
    A_PENDING = "📥 Kutilayotganlar"
    A_USER = "👤 Foydalanuvchi boshqaruvi"
    A_LOTTERY = "🎰 VIP o'yini"
    A_SETTINGS = "🛠 Tizim sozlamalari"
    A_BACKUP = "💾 Zaxira (Backup)"


USER_TEXTS = [T.BUY, T.TOPUP, T.PROFILE, T.ORDERS, T.COINS, T.DAILY, T.REF, T.RATING, T.HELP]
ADMIN_TEXTS = [T.A_ADMIN, T.A_CHANNEL, T.A_BCAST, T.A_PKG, T.A_PKGS, T.A_CARD, T.A_STATS, T.A_COUPON,
               T.A_COUPONS, T.A_PENDING, T.A_USER, T.A_LOTTERY, T.A_SETTINGS, T.A_BACKUP]
MENU_TEXTS = set(USER_TEXTS + ADMIN_TEXTS + [T.ADMIN, T.CANCEL, T.BACK])


def menu_kb(admin: bool = False):
    b = ReplyKeyboardBuilder()
    for t in USER_TEXTS:
        b.button(text=t)
    if admin:
        b.button(text=T.ADMIN)
    b.adjust(2, 2, 2, 2, 1, 1)
    return b.as_markup(resize_keyboard=True)


def admin_kb():
    b = ReplyKeyboardBuilder()
    for t in ADMIN_TEXTS:
        b.button(text=t)
    b.button(text=T.BACK)
    b.adjust(2, 2, 2, 2, 2, 2, 2, 1)
    return b.as_markup(resize_keyboard=True)


def cancel_kb():
    b = ReplyKeyboardBuilder()
    b.button(text=T.CANCEL)
    return b.as_markup(resize_keyboard=True)


async def main_menu(uid: int):
    return menu_kb(await is_admin(uid))


# ============================================================ FSM HOLATLAR
class Buy(StatesGroup):
    pubg_id = State()
    confirm = State()
    coupon = State()


class Pay(StatesGroup):
    check = State()


class TopUp(StatesGroup):
    amount = State()


class CoinS(StatesGroup):
    exch = State()
    buy = State()


class Adm(StatesGroup):
    add_admin = State()
    add_channel = State()
    bcast_msg = State()
    bcast_confirm = State()
    pkg_name = State()
    pkg_uc = State()
    pkg_price = State()
    pkg_cost = State()
    card_number = State()
    card_owner = State()
    cp_code = State()
    cp_disc = State()
    cp_min = State()
    cp_limit = State()
    user_id = State()
    user_bal = State()
    user_coin = State()
    setting_val = State()


# ============================================================ BIZNES MANTIQ
async def calc_price(conn, uid: int, pkg, coupon_code: Optional[str], use_coins: bool) -> dict:
    price = pkg["price"]
    res = dict(price=price, coupon_id=None, coupon_code=None, coupon_disc=0, coupon_error=None,
               coin_used=0, coin_disc=0, final=price)
    if coupon_code:
        cp = await _one(conn, "SELECT * FROM coupons WHERE code=? COLLATE NOCASE", (coupon_code,))
        if not cp or not cp["active"]:
            res["coupon_error"] = "Kupon topilmadi yoki faol emas."
        elif cp["used_count"] >= cp["limit_count"]:
            res["coupon_error"] = "Kupon limiti tugagan."
        elif pkg["uc"] < cp["min_uc"]:
            res["coupon_error"] = f"Bu kupon kamida {cp['min_uc']} UC xaridda amal qiladi."
        elif await _one(conn, "SELECT 1 FROM coupon_uses WHERE coupon_id=? AND user_id=?", (cp["id"], uid)):
            res["coupon_error"] = "Siz bu kupondan avval foydalangansiz."
        else:
            cap = max(price - 1000, 0)
            res.update(coupon_id=cp["id"], coupon_code=cp["code"], coupon_disc=min(cp["discount"], cap))
    if use_coins:
        u = await _one(conn, "SELECT coins FROM users WHERE id=?", (uid,))
        coin_value = max(await _seti(conn, "coin_value"), 1)
        pct = min(max(await _seti(conn, "coin_max_percent"), 0), 100)
        remaining = price - res["coupon_disc"]
        max_coins = (remaining * pct // 100) // coin_value
        used = min(u["coins"] if u else 0, max_coins)
        res.update(coin_used=used, coin_disc=used * coin_value)
    res["final"] = max(price - res["coupon_disc"] - res["coin_disc"], 0)
    return res


async def create_order(uid: int, pkg_id: int, pubg_id: str, coupon: Optional[str],
                       use_coins: bool, method: str):
    code = await gen_code()
    try:
        async with db.tx() as c:
            pkg = await _one(c, "SELECT * FROM packages WHERE id=? AND active=1", (pkg_id,))
            if not pkg:
                raise Fail("Paket topilmadi yoki o'chirilgan.")
            pr = await calc_price(c, uid, pkg, coupon, use_coins)
            if pr["coupon_error"]:
                raise Fail(pr["coupon_error"])
            pay_bal = pr["final"] if method == "balance" else 0
            cur = await c.execute(
                "UPDATE users SET balance=balance-?, coins=coins-? WHERE id=? AND balance>=? AND coins>=?",
                (pay_bal, pr["coin_used"], uid, pay_bal, pr["coin_used"]))
            if cur.rowcount == 0:
                raise Fail("Balansingiz yetarli emas. Avval hisobni to'ldiring.")
            if pr["coupon_id"]:
                cur = await c.execute(
                    "UPDATE coupons SET used_count=used_count+1 "
                    "WHERE id=? AND active=1 AND used_count<limit_count", (pr["coupon_id"],))
                if cur.rowcount == 0:
                    raise Fail("Kupon limiti tugagan.")
                try:
                    await c.execute("INSERT INTO coupon_uses(coupon_id,user_id) VALUES(?,?)",
                                    (pr["coupon_id"], uid))
                except aiosqlite.IntegrityError:
                    raise Fail("Siz bu kupondan avval foydalangansiz.")
            status = "checking" if method == "balance" else "awaiting_check"
            cur = await c.execute(
                "INSERT INTO orders(code,user_id,pkg_id,pkg_name,uc,price,final,cost,coupon_id,coupon_code,"
                "coupon_disc,coin_used,coin_disc,pubg_id,pay_method,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, uid, pkg["id"], pkg["name"], pkg["uc"], pr["price"], pr["final"], pkg["cost"],
                 pr["coupon_id"], pr["coupon_code"], pr["coupon_disc"], pr["coin_used"], pr["coin_disc"],
                 pubg_id, method, status))
            return cur.lastrowid, code
    except Fail as e:
        return None, str(e)


async def cancel_order(oid: int, statuses=("awaiting_check", "checking")):
    """Buyurtmani bekor qiladi va tanga/kupon/balansni qaytaradi."""
    async with db.tx() as c:
        o = await _one(c, "SELECT * FROM orders WHERE id=?", (oid,))
        if not o or o["status"] not in statuses:
            return None
        await c.execute("UPDATE orders SET status='cancelled', done_at=datetime('now','+5 hours') WHERE id=?",
                        (oid,))
        refund = o["final"] if o["pay_method"] == "balance" else 0
        await c.execute("UPDATE users SET balance=balance+?, coins=coins+? WHERE id=?",
                        (refund, o["coin_used"], o["user_id"]))
        if o["coupon_id"]:
            await c.execute("DELETE FROM coupon_uses WHERE coupon_id=? AND user_id=?",
                            (o["coupon_id"], o["user_id"]))
            await c.execute("UPDATE coupons SET used_count=MAX(used_count-1,0) WHERE id=?", (o["coupon_id"],))
        return o


async def complete_order(oid: int, admin_id: int):
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE orders SET status='done', done_at=datetime('now','+5 hours'), admin_id=? "
            "WHERE id=? AND status='checking'", (admin_id, oid))
        if cur.rowcount == 0:
            return None
        o = await _one(c, "SELECT * FROM orders WHERE id=?", (oid,))
        u = await _one(c, "SELECT * FROM users WHERE id=?", (o["user_id"],))
        per100 = await _seti(c, "cashback_per_100uc")
        mult = max(await _seti(c, "vip_cashback_mult"), 1) if u["is_vip"] else 1
        cashback = o["uc"] * per100 // 100 * mult
        new_total = u["total_uc"] + o["uc"]
        threshold = await _seti(c, "vip_threshold")
        became_vip = (not u["is_vip"]) and new_total >= threshold
        await c.execute("UPDATE users SET total_uc=?, coins=coins+?, is_vip=? WHERE id=?",
                        (new_total, cashback, 1 if (u["is_vip"] or became_vip) else 0, u["id"]))
        ref_id, ref_bonus = None, 0
        cnt = await _one(c, "SELECT COUNT(*) n FROM orders WHERE user_id=? AND status='done'", (u["id"],))
        if u["referrer_id"] and cnt["n"] == 1:
            ref_id, ref_bonus = u["referrer_id"], await _seti(c, "ref_purchase_bonus")
            await c.execute("UPDATE users SET coins=coins+? WHERE id=?", (ref_bonus, ref_id))
        return dict(order=o, cashback=cashback, became_vip=became_vip, total_uc=new_total,
                    ref_id=ref_id, ref_bonus=ref_bonus)


async def complete_topup(tid: int, admin_id: int):
    async with db.tx() as c:
        cur = await c.execute(
            "UPDATE topups SET status='done', done_at=datetime('now','+5 hours'), admin_id=? "
            "WHERE id=? AND status='checking'", (admin_id, tid))
        if cur.rowcount == 0:
            return None
        t = await _one(c, "SELECT * FROM topups WHERE id=?", (tid,))
        await c.execute("UPDATE users SET balance=balance+? WHERE id=?", (t["amount"], t["user_id"]))
        return t


async def notify_admins(bot: Bot, kind: str, rid: int):
    if kind == "order":
        o = await db.fetchone("SELECT * FROM orders WHERE id=?", (rid,))
        u = await get_user(o["user_id"])
        extra = ""
        if o["coupon_code"]:
            extra += f"\n🎟 Kupon: {esc(o['coupon_code'])} (−{fmt(o['coupon_disc'])})"
        if o["coin_used"]:
            extra += f"\n🪙 Tanga: {o['coin_used']} ta (−{fmt(o['coin_disc'])})"
        text = (f"🧾 <b>Yangi buyurtma {o['code']}</b>\n"
                f"👤 {mention(u['id'], u['full_name'])} (<code>{u['id']}</code>)"
                f"{' 💎VIP' if u['is_vip'] else ''}\n"
                f"🎮 PUBG ID: <code>{esc(o['pubg_id'])}</code>\n"
                f"💎 Paket: {esc(o['pkg_name'])} — <b>{o['uc']} UC</b>\n"
                f"💵 To'lov: <b>{fmt(o['final'])} so'm</b> (asl narx {fmt(o['price'])}){extra}\n"
                f"💳 Usul: {'Karta (chek)' if o['pay_method'] == 'card' else 'Balansdan (to`langan)'}\n\n"
                f"👉 Pulni tekshiring (izoh: <b>{o['code']}</b>), UC ni PUBG ID ga yuboring, so'ng tasdiqlang.")
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ UC yuborildi — Tasdiqlash", callback_data=f"oa:{rid}")
        kb.button(text="❌ Rad etish", callback_data=f"or:{rid}")
        kb.adjust(1)
        fid = o["check_file_id"]
    else:
        t = await db.fetchone("SELECT * FROM topups WHERE id=?", (rid,))
        u = await get_user(t["user_id"])
        text = (f"💰 <b>Hisob to'ldirish {t['code']}</b>\n"
                f"👤 {mention(u['id'], u['full_name'])} (<code>{u['id']}</code>)\n"
                f"💵 Summa: <b>{fmt(t['amount'])} so'm</b>\n\n"
                f"👉 Pul tushganini va izoh <b>{t['code']}</b> ekanini tekshiring.")
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Tasdiqlash", callback_data=f"ta:{rid}")
        kb.button(text="❌ Rad etish", callback_data=f"tr:{rid}")
        kb.adjust(1)
        fid = t["check_file_id"]
    for aid in await admin_ids():
        try:
            await send_media(bot, aid, fid, text, kb.as_markup())
        except Exception:
            log.warning("Adminga xabar yuborilmadi: %s", aid)


async def run_lottery(bot: Bot, month: str) -> str:
    if await db.fetchone("SELECT 1 FROM lottery_log WHERE month=?", (month,)):
        return f"ℹ️ {month} oyi uchun o'yin allaqachon o'tkazilgan."
    row = await db.fetchone(
        "SELECT user_id, SUM(uc) s FROM orders WHERE status='done' AND substr(done_at,1,7)=? "
        "AND user_id IN (SELECT id FROM users WHERE is_vip=1) "
        "GROUP BY user_id ORDER BY s DESC LIMIT 1", (month,))
    if not row:
        return f"ℹ️ {month} oyida VIP xaridorlar topilmadi."
    prize = random.choice([30, 60])
    avg = await db.fetchone("SELECT AVG(cost*1.0/uc) a FROM packages WHERE active=1 AND uc>0")
    cost = int(prize * (avg["a"] or 0))
    await db.execute("INSERT OR IGNORE INTO lottery_log(month,user_id,uc,cost) VALUES(?,?,?,?)",
                     (month, row["user_id"], prize, cost))
    u = await get_user(row["user_id"])
    last = await db.fetchone("SELECT pubg_id FROM orders WHERE user_id=? AND status='done' "
                             "ORDER BY id DESC LIMIT 1", (row["user_id"],))
    pubg = last["pubg_id"] if last else "—"
    await tell(bot, row["user_id"],
               f"🎉 <b>TABRIKLAYMIZ!</b>\n{month} oyining TOP-1 VIP xaridori siz bo'ldingiz "
               f"({row['s']} UC)!\n🎰 O'yin natijasi: <b>{prize} UC</b> yutdingiz!\n"
               f"UC tez orada PUBG ID <code>{esc(pubg)}</code> ga yuboriladi.")
    for aid in await admin_ids():
        await tell(bot, aid,
                   f"🎰 <b>VIP o'yini ({month})</b>\nG'olib: {mention(u['id'], u['full_name'])} "
                   f"(<code>{u['id']}</code>)\nSotib olgan: {row['s']} UC\n🎁 Sovrin: <b>{prize} UC</b>\n"
                   f"🎮 PUBG ID: <code>{esc(pubg)}</code>\n👉 Sovrinni qo'lda yuboring.")
    return f"✅ O'yin o'tkazildi: g'olib {esc(u['full_name'])}, sovrin {prize} UC."


# ============================================================ FILTR VA MIDDLEWARE
class AdminFilter(BaseFilter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        return await is_admin(event.from_user.id)


_sub_cache: dict = {}


async def missing_channels(bot: Bot, uid: int, use_cache: bool = True) -> list:
    now = time.time()
    if use_cache and _sub_cache.get(uid, 0) > now:
        return []
    miss = []
    for ch in await db.fetchall("SELECT * FROM channels"):
        try:
            mb = await bot.get_chat_member(ch["chat_id"], uid)
            if mb.status in ("left", "kicked") or getattr(mb, "is_member", True) is False:
                miss.append(ch)
        except Exception as e:
            log.warning("Kanal tekshirilmadi %s: %s", ch["chat_id"], e)
    if not miss:
        _sub_cache[uid] = now + 60
    return miss


def sub_prompt(miss: list):
    b = InlineKeyboardBuilder()
    for ch in miss:
        b.button(text=f"📢 {ch['title']}", url=ch["link"])
    b.button(text="✅ Obunani tekshirish", callback_data="chk_sub")
    b.adjust(1)
    return "❗️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:", b.as_markup()


class StateInterruptMiddleware(BaseMiddleware):
    """Menyu tugmasi yoki buyruq bosilsa, ochiq FSM holatini tozalaydi."""

    async def __call__(self, handler, event: Message, data):
        text = event.text or ""
        if text in MENU_TEXTS or text.startswith("/"):
            state: Optional[FSMContext] = data.get("state")
            if state:
                await state.clear()
                data["raw_state"] = None
        return await handler(event, data)


class GateMiddleware(BaseMiddleware):
    """Ro'yxatga olish, ban va majburiy obuna tekshiruvi."""

    async def __call__(self, handler, event, data):
        tg = data.get("event_from_user")
        bot: Bot = data["bot"]
        if not tg or tg.is_bot:
            return await handler(event, data)
        is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
        if not is_start:
            await register_user(tg)
        if await is_admin(tg.id):
            return await handler(event, data)
        u = await get_user(tg.id)
        if u and u["banned"]:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Siz bloklangansiz.", show_alert=True)
            elif not is_start:
                await event.answer("🚫 Siz bloklangansiz.")
            return
        if is_start or (isinstance(event, CallbackQuery) and event.data == "chk_sub"):
            return await handler(event, data)
        miss = await missing_channels(bot, tg.id)
        if miss:
            text, kb = sub_prompt(miss)
            if isinstance(event, CallbackQuery):
                await event.answer("Avval kanallarga obuna bo'ling!", show_alert=True)
                await event.message.answer(text, reply_markup=kb)
            else:
                await event.answer(text, reply_markup=kb)
            return
        return await handler(event, data)


# ============================================================ ROUTERLAR
common_r = Router()
admin_r = Router()
user_r = Router()
admin_r.message.filter(AdminFilter())
admin_r.callback_query.filter(AdminFilter())


WELCOME = ("👋 Assalomu alaykum, <b>{name}</b>!\n\n💎 <b>Pro UC Bot</b> — PUBG Mobile UC ni ishonchli, "
           "tez va arzon narxlarda sotib oling.\n\n🎁 Har xarid uchun tanga keshbek, VIP maqom, "
           "kuponlar va oylik o'yinlar sizni kutmoqda!")


@common_r.message(F.text == T.CANCEL)
@common_r.message(Command("cancel"))
async def cancel_any(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("❌ Bekor qilindi.", reply_markup=await main_menu(m.from_user.id))


@common_r.message(F.text == T.BACK)
async def back_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🏠 Bosh menyu", reply_markup=await main_menu(m.from_user.id))


# ------------------------------------------------------------ /start va obuna
@user_r.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, command: CommandObject, bot: Bot):
    await state.clear()
    ref = None
    if command.args and command.args.startswith("ref_"):
        ref = parse_int(command.args[4:])
    u, new, ref_ok = await register_user(m.from_user, ref)
    if u["banned"]:
        return await m.answer("🚫 Siz bloklangansiz.")
    if new and ref_ok:
        await tell(bot, ref, f"🎉 Sizning havolangiz orqali <b>{esc(m.from_user.full_name)}</b> qo'shildi! "
                             f"+{await Si('ref_bonus')} 🪙 tanga berildi.")
    admin = await is_admin(m.from_user.id)
    if not admin:
        miss = await missing_channels(bot, m.from_user.id, use_cache=False)
        if miss:
            text, kb = sub_prompt(miss)
            return await m.answer(text, reply_markup=kb)
    await m.answer(WELCOME.format(name=esc(m.from_user.full_name)), reply_markup=menu_kb(admin))


@user_r.callback_query(F.data == "chk_sub")
async def chk_sub(c: CallbackQuery, bot: Bot):
    miss = await missing_channels(bot, c.from_user.id, use_cache=False)
    if miss:
        return await c.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    await c.answer("✅ Rahmat!")
    try:
        await c.message.delete()
    except Exception:
        pass
    await c.message.answer(WELCOME.format(name=esc(c.from_user.full_name)),
                           reply_markup=await main_menu(c.from_user.id))


# ------------------------------------------------------------ Profil, reyting, referal, yordam
@user_r.message(F.text == T.PROFILE)
async def profile(m: Message):
    u = await get_user(m.from_user.id)
    thr = await Si("vip_threshold")
    vip = "💎 <b>VIP</b>" if u["is_vip"] else f"Oddiy (VIP uchun yana <b>{max(thr - u['total_uc'], 0)} UC</b>)"
    refs = await db.fetchone("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (u["id"],))
    await m.answer(
        f"👤 <b>Profil</b>\n\n🆔 ID: <code>{u['id']}</code>\n📛 Ism: {esc(u['full_name'])}\n"
        f"💰 Balans: <b>{fmt(u['balance'])} so'm</b>\n🪙 Tangalar: <b>{fmt(u['coins'])}</b>\n"
        f"💎 Jami xarid: <b>{fmt(u['total_uc'])} UC</b>\n🏅 Maqom: {vip}\n"
        f"👥 Takliflar: {refs['n']} ta\n📅 Ro'yxatdan: {u['joined_at'][:10]}")


@user_r.message(F.text == T.RATING)
async def rating(m: Message):
    rows = await db.fetchall("SELECT full_name,total_uc,is_vip FROM users WHERE total_uc>0 "
                             "ORDER BY total_uc DESC LIMIT 10")
    if not rows:
        return await m.answer("🏆 Reyting hozircha bo'sh. Birinchi bo'ling!")
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines = [f"{medals[i]} {esc((r['full_name'] or '—')[:14])} — <b>{fmt(r['total_uc'])} UC</b>"
             f"{' 💎' if r['is_vip'] else ''}" for i, r in enumerate(rows)]
    await m.answer("🏆 <b>TOP-10 xaridorlar</b>\n\n" + "\n".join(lines) +
                   "\n\n🎰 Har oy VIP'lar orasidan TOP-1 ga <b>30 yoki 60 UC</b> sovg'a o'yini!")


@user_r.message(F.text == T.REF)
async def referral(m: Message, bot: Bot):
    me = await bot.get_me()
    cnt = await db.fetchone("SELECT COUNT(*) n FROM users WHERE referrer_id=?", (m.from_user.id,))
    await m.answer(
        f"👥 <b>Do'stlarni taklif qiling — tanga oling!</b>\n\n"
        f"🔗 Sizning havolangiz:\n<code>https://t.me/{me.username}?start=ref_{m.from_user.id}</code>\n\n"
        f"🎁 Har bir do'st uchun: <b>+{await Si('ref_bonus')} 🪙</b>\n"
        f"🛍 Do'stingiz 1-xaridida: <b>+{await Si('ref_purchase_bonus')} 🪙</b>\n"
        f"👤 Takliflaringiz: <b>{cnt['n']}</b> ta")


@user_r.message(F.text == T.HELP)
async def help_(m: Message):
    await m.answer(f"☎️ <b>Yordam</b>\n\nSavol va muammolar uchun: {esc(await S('support'))}\n\n"
                   f"ℹ️ To'lov paytida izohga faqat <b>bot bergan kodni</b> (masalan #7492) yozing.")


@user_r.message(F.text == T.DAILY)
async def daily(m: Message):
    today = now_tz().strftime("%Y-%m-%d")
    bonus = await Si("daily_bonus")
    _, rc = await db.execute("UPDATE users SET coins=coins+?, last_daily=? WHERE id=? "
                             "AND (last_daily IS NULL OR last_daily<>?)",
                             (bonus, today, m.from_user.id, today))
    if not rc:
        return await m.answer("⏳ Bugungi bonusni olgansiz. Ertaga qaytib keling!")
    await m.answer(f"🎁 Kunlik bonus: <b>+{bonus} 🪙</b> tanga!")


# ------------------------------------------------------------ Tangalar
@user_r.message(F.text == T.COINS)
async def coins_menu(m: Message):
    u = await get_user(m.from_user.id)
    cv, bp = await Si("coin_value"), await Si("coin_buy_price")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Tangani so'mga almashtirish", callback_data="coin:exch")
    kb.button(text="🛍 Tanga sotib olish", callback_data="coin:buy")
    kb.adjust(1)
    await m.answer(
        f"🪙 <b>Tangalar</b>\n\nSizda: <b>{fmt(u['coins'])} 🪙</b>\n\n"
        f"• 1 🪙 = <b>{fmt(cv)} so'm</b> chegirma (xarid paytida ishlatiladi, "
        f"narxning {await Si('coin_max_percent')}% gacha)\n"
        f"• 1 🪙 sotib olish narxi: <b>{fmt(bp)} so'm</b>\n\n"
        f"<b>Tanga qanday olinadi?</b>\n🛒 Har 100 UC uchun +{await Si('cashback_per_100uc')} 🪙 "
        f"(VIP'ga ×{await Si('vip_cashback_mult')})\n👥 Do'st taklif qilish\n🎁 Kunlik bonus", reply_markup=kb.as_markup())


@user_r.callback_query(F.data == "coin:exch")
async def coin_exch(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(CoinS.exch)
    u = await get_user(c.from_user.id)
    await c.message.answer(f"🔄 Nechta tangani so'mga almashtirasiz? (Sizda {u['coins']} 🪙)\n"
                           f"1 🪙 = {fmt(await Si('coin_value'))} so'm", reply_markup=cancel_kb())


@user_r.message(StateFilter(CoinS.exch))
async def coin_exch_amount(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    val = n * await Si("coin_value")
    _, rc = await db.execute("UPDATE users SET coins=coins-?, balance=balance+? WHERE id=? AND coins>=?",
                             (n, val, m.from_user.id, n))
    if not rc:
        return await m.answer("❌ Tangangiz yetarli emas. Boshqa son kiriting.")
    await state.clear()
    await m.answer(f"✅ {n} 🪙 → <b>{fmt(val)} so'm</b> balansga qo'shildi.",
                   reply_markup=await main_menu(m.from_user.id))


@user_r.callback_query(F.data == "coin:buy")
async def coin_buy(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(CoinS.buy)
    u = await get_user(c.from_user.id)
    await c.message.answer(f"🛍 Nechta tanga sotib olasiz?\n1 🪙 = {fmt(await Si('coin_buy_price'))} so'm\n"
                           f"💰 Balansingiz: {fmt(u['balance'])} so'm", reply_markup=cancel_kb())


@user_r.message(StateFilter(CoinS.buy))
async def coin_buy_amount(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    cost = n * await Si("coin_buy_price")
    _, rc = await db.execute("UPDATE users SET balance=balance-?, coins=coins+? WHERE id=? AND balance>=?",
                             (cost, n, m.from_user.id, cost))
    if not rc:
        return await m.answer("❌ Balans yetarli emas. Avval hisobni to'ldiring yoki kamroq kiriting.")
    await state.clear()
    await m.answer(f"✅ {n} 🪙 sotib olindi (−{fmt(cost)} so'm).", reply_markup=await main_menu(m.from_user.id))


# ------------------------------------------------------------ UC sotib olish
@user_r.message(F.text == T.BUY)
async def buy_list(m: Message):
    pk = await db.fetchall("SELECT * FROM packages WHERE active=1 ORDER BY uc")
    if not pk:
        return await m.answer("😔 Hozircha paketlar mavjud emas. Keyinroq urinib ko'ring.")
    b = InlineKeyboardBuilder()
    for p in pk:
        b.button(text=f"💎 {p['name']} • {fmt(p['price'])} so'm", callback_data=f"buy:{p['id']}")
    b.adjust(1)
    await m.answer("🛒 <b>UC paketini tanlang:</b>\n\n💎 Jami 600 UC dan oshsa — avtomatik <b>VIP</b>!",
                   reply_markup=b.as_markup())


@user_r.callback_query(F.data.startswith("buy:"))
async def buy_pick(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    p = await db.fetchone("SELECT * FROM packages WHERE id=? AND active=1", (pid,))
    if not p:
        return await c.answer("Paket topilmadi", show_alert=True)
    await c.answer()
    await state.clear()
    await state.update_data(pid=pid, coupon=None, use_coins=False)
    await state.set_state(Buy.pubg_id)
    await c.message.answer(f"✅ Tanlandi: <b>{esc(p['name'])}</b> — {fmt(p['price'])} so'm\n\n"
                           f"🎮 Endi <b>PUBG Mobile ID</b> raqamingizni yuboring:", reply_markup=cancel_kb())


async def render_summary(uid: int, data: dict):
    pkg = await db.fetchone("SELECT * FROM packages WHERE id=? AND active=1", (data["pid"],))
    if not pkg:
        return None, None
    pr = await calc_price(db.conn, uid, pkg, data.get("coupon"), data.get("use_coins", False))
    u = await get_user(uid)
    lines = [f"🧾 <b>Buyurtma</b>\n", f"🎮 PUBG ID: <code>{esc(data['pubg_id'])}</code>",
             f"💎 Paket: {esc(pkg['name'])} — {pkg['uc']} UC", f"💵 Narx: {fmt(pr['price'])} so'm"]
    if pr["coupon_id"]:
        lines.append(f"🎟 Kupon ({esc(pr['coupon_code'])}): −{fmt(pr['coupon_disc'])} so'm")
    if pr["coin_used"]:
        lines.append(f"🪙 Tanga ({pr['coin_used']} ta): −{fmt(pr['coin_disc'])} so'm")
    lines += ["━━━━━━━━━━", f"✅ <b>To'lov: {fmt(pr['final'])} so'm</b>", f"💰 Balansingiz: {fmt(u['balance'])} so'm"]
    kb = InlineKeyboardBuilder()
    kb.button(text="🎟 Kupon kiritish", callback_data="b:coupon")
    if u["coins"] > 0:
        kb.button(text=f"🪙 Tanga ishlatish: {'✅ Yoqilgan' if data.get('use_coins') else '❌ O`chiq'}",
                  callback_data="b:coins")
    kb.button(text="💳 Karta orqali to'lash", callback_data="b:card")
    kb.button(text="💰 Balansdan to'lash", callback_data="b:bal")
    kb.button(text="✖️ Bekor qilish", callback_data="b:cancel")
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@user_r.message(StateFilter(Buy.pubg_id))
async def buy_pubg(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not (t.isdigit() and 7 <= len(t) <= 12):
        return await m.answer("❗️ PUBG ID faqat raqamlardan iborat bo'lishi kerak (7–12 xona). Qayta yuboring:")
    await state.update_data(pubg_id=t)
    await state.set_state(Buy.confirm)
    text, kb = await render_summary(m.from_user.id, await state.get_data())
    if not text:
        await state.clear()
        return await m.answer("Paket topilmadi.", reply_markup=await main_menu(m.from_user.id))
    await m.answer(text, reply_markup=kb)


@user_r.callback_query(StateFilter(Buy.confirm), F.data == "b:coupon")
async def buy_coupon_ask(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Buy.coupon)
    await c.message.answer("🎟 Kupon (promokod) kodini yuboring:", reply_markup=cancel_kb())


@user_r.message(StateFilter(Buy.coupon))
async def buy_coupon_set(m: Message, state: FSMContext):
    code = (m.text or "").strip()
    data = await state.get_data()
    pkg = await db.fetchone("SELECT * FROM packages WHERE id=?", (data["pid"],))
    pr = await calc_price(db.conn, m.from_user.id, pkg, code, False)
    if pr["coupon_error"]:
        return await m.answer(f"❌ {pr['coupon_error']}\nBoshqa kod yuboring yoki bekor qiling.")
    await state.update_data(coupon=code)
    await state.set_state(Buy.confirm)
    text, kb = await render_summary(m.from_user.id, await state.get_data())
    await m.answer("✅ Kupon qo'llandi!")
    await m.answer(text, reply_markup=kb)


@user_r.callback_query(StateFilter(Buy.confirm), F.data == "b:coins")
async def buy_toggle_coins(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(use_coins=not d.get("use_coins", False))
    text, kb = await render_summary(c.from_user.id, await state.get_data())
    await c.answer()
    await safe_edit(c.message, text, kb)


@user_r.callback_query(F.data == "b:cancel")
async def buy_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Bekor qilindi")
    await safe_edit(c.message, "❌ Buyurtma bekor qilindi.")
    await c.message.answer("🏠 Bosh menyu", reply_markup=await main_menu(c.from_user.id))


def payment_text(card: str, owner: str, amount: int, code: str) -> str:
    return (f"💳 <b>To'lov ma'lumotlari</b>\n\nKarta: <code>{esc(card)}</code>\nEgasi: <b>{esc(owner)}</b>\n"
            f"Summa: <b>{fmt(amount)} so'm</b>\n\n"
            f"🔑 To'lov izohiga shu kodni yozing: <code>{code}</code>\n"
            f"⚠️ Kod yozilmasa to'lov tasdiqlanmasligi mumkin!\n"
            f"⏳ {ORDER_EXPIRE_MIN} daqiqa ichida to'lang.\n\n📸 To'lagach, <b>chek (skrinshot)</b>ni shu yerga yuboring.")


@user_r.callback_query(StateFilter(Buy.confirm), F.data.in_({"b:card", "b:bal"}))
async def buy_pay(c: CallbackQuery, state: FSMContext, bot: Bot):
    method = "card" if c.data == "b:card" else "balance"
    card, owner = await S("card_number"), await S("card_owner")
    if method == "card" and not card:
        return await c.answer("Karta ma'lumoti kiritilmagan. Admin bilan bog'laning.", show_alert=True)
    d = await state.get_data()
    oid, res = await create_order(c.from_user.id, d["pid"], d["pubg_id"], d.get("coupon"),
                                  d.get("use_coins", False), method)
    if oid is None:
        return await c.answer(res, show_alert=True)
    await c.answer()
    o = await db.fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    if method == "balance":
        await state.clear()
        await safe_edit(c.message, f"✅ <b>Buyurtma {o['code']} qabul qilindi!</b>\nTo'lov balansdan olindi. "
                                   f"UC tez orada PUBG ID <code>{esc(o['pubg_id'])}</code> ga yuboriladi.")
        await c.message.answer("🏠 Bosh menyu", reply_markup=await main_menu(c.from_user.id))
        await notify_admins(bot, "order", oid)
    else:
        await state.set_state(Pay.check)
        await state.update_data(kind="order", ref_id=oid)
        await safe_edit(c.message, payment_text(card, owner, o["final"], o["code"]))
        await c.message.answer("📸 Chekni yuboring (rasm yoki fayl):", reply_markup=cancel_kb())


@user_r.message(StateFilter(Pay.check), F.photo | F.document)
async def got_check(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    kind, rid = d.get("kind"), d.get("ref_id")
    fid = ("p:" + m.photo[-1].file_id) if m.photo else ("d:" + m.document.file_id)
    table = "orders" if kind == "order" else "topups"
    _, rc = await db.execute(
        f"UPDATE {table} SET status='checking', check_file_id=? WHERE id=? AND user_id=? "
        f"AND status='awaiting_check'", (fid, rid, m.from_user.id))
    await state.clear()
    if not rc:
        return await m.answer("⚠️ Bu so'rov muddati tugagan yoki allaqachon yuborilgan.",
                              reply_markup=await main_menu(m.from_user.id))
    await m.answer("✅ Chek qabul qilindi! Admin tekshirib, tez orada tasdiqlaydi.",
                   reply_markup=await main_menu(m.from_user.id))
    await notify_admins(bot, kind, rid)


@user_r.message(StateFilter(Pay.check))
async def check_not_photo(m: Message):
    await m.answer("📸 Iltimos, to'lov chekini <b>rasm</b> yoki <b>fayl</b> ko'rinishida yuboring.")


# ------------------------------------------------------------ Hisobni to'ldirish
@user_r.message(F.text == T.TOPUP)
async def topup_start(m: Message, state: FSMContext):
    if not await S("card_number"):
        return await m.answer("😔 Hozircha to'lov qabul qilinmayapti. Admin bilan bog'laning.")
    await state.set_state(TopUp.amount)
    await m.answer(f"💰 Qancha so'm to'ldirmoqchisiz?\nMinimal: <b>{fmt(await Si('topup_min'))} so'm</b>",
                   reply_markup=cancel_kb())


@user_r.message(StateFilter(TopUp.amount))
async def topup_amount(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    mn = await Si("topup_min")
    if not n or n < mn or n > 100_000_000:
        return await m.answer(f"❗️ Summa {fmt(mn)} dan 100 000 000 gacha bo'lishi kerak.")
    code = await gen_code()
    tid, _ = await db.execute("INSERT INTO topups(code,user_id,amount,status) VALUES(?,?,?,'awaiting_check')",
                              (code, m.from_user.id, n))
    await state.set_state(Pay.check)
    await state.update_data(kind="topup", ref_id=tid)
    await m.answer(payment_text(await S("card_number"), await S("card_owner"), n, code))


# ------------------------------------------------------------ Buyurtmalarim
@user_r.message(F.text == T.ORDERS)
async def my_orders(m: Message):
    rows = await db.fetchall("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 8", (m.from_user.id,))
    if not rows:
        return await m.answer("📦 Sizda hali buyurtmalar yo'q.")
    lines, kb = [], InlineKeyboardBuilder()
    for o in rows:
        lines.append(f"{STATUS_EMOJI[o['status']]} <b>{o['code']}</b> • {o['uc']} UC • {fmt(o['final'])} so'm • "
                     f"{STATUS_TEXT[o['status']]}\n    🕒 {o['created_at'][:16]}")
        if o["status"] == "awaiting_check":
            kb.button(text=f"📸 Chek yuborish {o['code']}", callback_data=f"oc_send:{o['id']}")
            kb.button(text=f"✖️ Bekor {o['code']}", callback_data=f"oc_cancel:{o['id']}")
    kb.adjust(2)
    await m.answer("📦 <b>So'nggi buyurtmalaringiz</b>\n\n" + "\n".join(lines), reply_markup=kb.as_markup())


@user_r.callback_query(F.data.startswith("oc_send:"))
async def oc_send(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1])
    o = await db.fetchone("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id))
    if not o or o["status"] != "awaiting_check":
        return await c.answer("Bu buyurtma endi faol emas.", show_alert=True)
    await c.answer()
    await state.set_state(Pay.check)
    await state.update_data(kind="order", ref_id=oid)
    await c.message.answer(payment_text(await S("card_number"), await S("card_owner"), o["final"], o["code"]),
                           reply_markup=cancel_kb())


@user_r.callback_query(F.data.startswith("oc_cancel:"))
async def oc_cancel(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await db.fetchone("SELECT user_id FROM orders WHERE id=?", (oid,))
    if not o or o["user_id"] != c.from_user.id:
        return await c.answer("Topilmadi", show_alert=True)
    if await cancel_order(oid, ("awaiting_check",)):
        await c.answer("Bekor qilindi")
        await safe_edit(c.message, "✅ Buyurtma bekor qilindi (tanga/kupon qaytarildi).")
    else:
        await c.answer("Bu buyurtmani bekor qilib bo'lmaydi.", show_alert=True)


# ============================================================ ADMIN PANEL
@admin_r.message(F.text == T.ADMIN)
@admin_r.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Admin Panel</b>", reply_markup=admin_kb())


# ------------------------------------------------------------ Admin qo'shish
async def admins_view():
    rows = await db.fetchall("SELECT id FROM admins ORDER BY added_at")
    b = InlineKeyboardBuilder()
    lines = []
    for r in rows:
        lines.append(f"• <code>{r['id']}</code>{' (asosiy)' if r['id'] == SUPER_ADMIN_ID else ''}")
        if r["id"] != SUPER_ADMIN_ID:
            b.button(text=f"🗑 {r['id']}", callback_data=f"ad:{r['id']}")
    b.adjust(2)
    return "👥 <b>Adminlar:</b>\n" + "\n".join(lines) + "\n\n➕ Yangi admin <b>Telegram ID</b> sini yuboring:", b.as_markup()


@admin_r.message(F.text == T.A_ADMIN)
async def add_admin_start(m: Message, state: FSMContext):
    text, kb = await admins_view()
    await state.set_state(Adm.add_admin)
    await m.answer(text, reply_markup=kb)
    await m.answer("ID yuboring yoki bekor qiling 👇", reply_markup=cancel_kb())


@admin_r.callback_query(F.data.startswith("ad:"))
async def del_admin(c: CallbackQuery):
    aid = int(c.data.split(":")[1])
    if aid == SUPER_ADMIN_ID or aid == c.from_user.id:
        return await c.answer("Bu adminni o'chirib bo'lmaydi.", show_alert=True)
    await db.execute("DELETE FROM admins WHERE id=?", (aid,))
    await c.answer("O'chirildi")
    text, kb = await admins_view()
    await safe_edit(c.message, text, kb)


@admin_r.message(StateFilter(Adm.add_admin))
async def add_admin_id(m: Message, state: FSMContext, bot: Bot):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ To'g'ri Telegram ID kiriting (faqat raqam).")
    await db.execute("INSERT OR IGNORE INTO admins(id) VALUES(?)", (n,))
    await state.clear()
    await m.answer(f"✅ <code>{n}</code> admin qilindi.", reply_markup=admin_kb())
    await tell(bot, n, "🎉 Siz Pro UC Bot adminisiz! /start ni bosing.")


# ------------------------------------------------------------ Majburiy obuna
async def channels_view():
    rows = await db.fetchall("SELECT * FROM channels")
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"🗑 {r['title']}", callback_data=f"chd:{r['id']}")
    b.button(text="➕ Kanal qo'shish", callback_data="cha")
    b.adjust(1)
    text = "📢 <b>Majburiy obuna kanallari</b>\n\n" + (
        "\n".join(f"• {esc(r['title'])} — {esc(r['link'])}" for r in rows) if rows else "Hozircha kanal yo'q.")
    return text + "\n\n⚠️ Bot kanalda <b>admin</b> bo'lishi shart.", b.as_markup()


@admin_r.message(F.text == T.A_CHANNEL)
async def channels_menu(m: Message):
    text, kb = await channels_view()
    await m.answer(text, reply_markup=kb, disable_web_page_preview=True)


@admin_r.callback_query(F.data == "cha")
async def channel_add_ask(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(Adm.add_channel)
    await c.message.answer("📢 Kanal <b>@username</b> yoki ID sini (-100...) yuboring:", reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.add_channel))
async def channel_add(m: Message, state: FSMContext, bot: Bot):
    raw = (m.text or "").strip()
    ident = raw if raw.startswith("@") else (parse_int(raw) if parse_int(raw) else "@" + raw.lstrip("@"))
    try:
        chat = await bot.get_chat(ident)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in ("administrator", "creator"):
            return await m.answer("❌ Bot bu kanalda admin emas. Avval botni admin qiling.")
        link = f"https://t.me/{chat.username}" if chat.username else (
            chat.invite_link or await bot.export_chat_invite_link(chat.id))
    except Exception as e:
        return await m.answer(f"❌ Kanal topilmadi yoki bot ruxsati yo'q.\n<code>{esc(e)}</code>")
    await db.execute("INSERT OR REPLACE INTO channels(chat_id,title,link) VALUES(?,?,?)",
                     (chat.id, chat.title or str(chat.id), link))
    await state.clear()
    await m.answer(f"✅ Kanal qo'shildi: {esc(chat.title)}", reply_markup=admin_kb())


@admin_r.callback_query(F.data.startswith("chd:"))
async def channel_del(c: CallbackQuery):
    await db.execute("DELETE FROM channels WHERE id=?", (int(c.data.split(":")[1]),))
    _sub_cache.clear()
    await c.answer("O'chirildi")
    text, kb = await channels_view()
    await safe_edit(c.message, text, kb)


# ------------------------------------------------------------ Broadcast
@admin_r.message(F.text == T.A_BCAST)
async def bcast_start(m: Message, state: FSMContext):
    await state.set_state(Adm.bcast_msg)
    await m.answer("📣 Yuboriladigan xabarni jo'nating (matn, rasm, video — istalgan format):",
                   reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.bcast_msg))
async def bcast_preview(m: Message, state: FSMContext, bot: Bot):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id)
    await state.set_state(Adm.bcast_confirm)
    n = await db.fetchone("SELECT COUNT(*) n FROM users WHERE banned=0 AND blocked=0")
    await bot.copy_message(m.chat.id, m.chat.id, m.message_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Yuborish ({n['n']} ta)", callback_data="bc:go")
    kb.button(text="❌ Bekor", callback_data="bc:no")
    kb.adjust(1)
    await m.answer("👆 Xabar shunday ko'rinadi. Yuborilsinmi?", reply_markup=kb.as_markup())


BG_TASKS: set = set()


async def do_broadcast(bot: Bot, admin_id: int, chat_id: int, msg_id: int):
    ids = [r["id"] for r in await db.fetchall("SELECT id FROM users WHERE banned=0 AND blocked=0")]
    ok = fail = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, chat_id, msg_id)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(uid, chat_id, msg_id)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            await db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await tell(bot, admin_id, f"📣 <b>Xabar yuborish yakunlandi</b>\n✅ Yetkazildi: {ok}\n❌ Xato/bloklagan: {fail}")


@admin_r.callback_query(StateFilter(Adm.bcast_confirm), F.data.in_({"bc:go", "bc:no"}))
async def bcast_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if c.data == "bc:no":
        await c.answer("Bekor qilindi")
        return await safe_edit(c.message, "❌ Bekor qilindi.")
    await c.answer("Yuborilmoqda...")
    await safe_edit(c.message, "⏳ Xabar yuborilmoqda... Tugagach hisobot keladi.")
    t = asyncio.create_task(do_broadcast(bot, c.from_user.id, d["src_chat"], d["src_msg"]))
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    await c.message.answer("Admin panel", reply_markup=admin_kb())


# ------------------------------------------------------------ UC paket qo'shish / boshqarish
@admin_r.message(F.text == T.A_PKG)
async def pkg_start(m: Message, state: FSMContext):
    await state.set_state(Adm.pkg_name)
    await m.answer("➕ Paket <b>nomini</b> yozing (masalan: 60 UC):", reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.pkg_name))
async def pkg_name(m: Message, state: FSMContext):
    await state.update_data(name=(m.text or "").strip()[:40])
    await state.set_state(Adm.pkg_uc)
    await m.answer("💎 UC miqdori (raqam):")


@admin_r.message(StateFilter(Adm.pkg_uc))
async def pkg_uc(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    await state.update_data(uc=n)
    await state.set_state(Adm.pkg_price)
    await m.answer("💵 <b>Sotuv narxi</b> (so'm):")


@admin_r.message(StateFilter(Adm.pkg_price))
async def pkg_price(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    await state.update_data(price=n)
    await state.set_state(Adm.pkg_cost)
    await m.answer("🏷 <b>Tannarx</b> (siz UC ni qanchaga olasiz, so'm):")


@admin_r.message(StateFilter(Adm.pkg_cost))
async def pkg_cost(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if n is None or n < 0:
        return await m.answer("❗️ Son kiriting.")
    d = await state.get_data()
    await db.execute("INSERT INTO packages(name,uc,price,cost) VALUES(?,?,?,?)",
                     (d["name"], d["uc"], d["price"], n))
    await state.clear()
    await m.answer(f"✅ Paket qo'shildi: <b>{esc(d['name'])}</b> — {d['uc']} UC\n"
                   f"Sotuv: {fmt(d['price'])} | Tannarx: {fmt(n)} | Foyda: <b>{fmt(d['price'] - n)} so'm</b>",
                   reply_markup=admin_kb())


async def packages_view():
    rows = await db.fetchall("SELECT * FROM packages ORDER BY uc")
    b = InlineKeyboardBuilder()
    lines = []
    for p in rows:
        lines.append(f"{'🟢' if p['active'] else '⚪️'} <b>{esc(p['name'])}</b> • {p['uc']} UC • "
                     f"{fmt(p['price'])} / tannarx {fmt(p['cost'])} (foyda {fmt(p['price'] - p['cost'])})")
        b.button(text=f"{'⏸' if p['active'] else '▶️'} {p['name']}", callback_data=f"pt:{p['id']}")
        b.button(text="🗑", callback_data=f"pd:{p['id']}")
    b.adjust(2)
    return "📋 <b>Paketlar</b>\n\n" + ("\n".join(lines) if lines else "Paket yo'q."), b.as_markup()


@admin_r.message(F.text == T.A_PKGS)
async def pkgs_menu(m: Message):
    text, kb = await packages_view()
    await m.answer(text, reply_markup=kb)


@admin_r.callback_query(F.data.startswith(("pt:", "pd:")))
async def pkg_manage(c: CallbackQuery):
    act, pid = c.data.split(":")
    if act == "pt":
        await db.execute("UPDATE packages SET active=1-active WHERE id=?", (int(pid),))
    else:
        await db.execute("DELETE FROM packages WHERE id=?", (int(pid),))
    await c.answer("Bajarildi")
    text, kb = await packages_view()
    await safe_edit(c.message, text, kb)


# ------------------------------------------------------------ Karta
@admin_r.message(F.text == T.A_CARD)
async def card_start(m: Message, state: FSMContext):
    await state.set_state(Adm.card_number)
    await m.answer(f"💳 Hozirgi karta: <code>{esc(await S('card_number')) or '—'}</code> "
                   f"({esc(await S('card_owner')) or '—'})\n\nYangi <b>karta raqamini</b> yuboring:",
                   reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.card_number))
async def card_number(m: Message, state: FSMContext):
    digits = re.sub(r"\D", "", m.text or "")
    if not 13 <= len(digits) <= 19:
        return await m.answer("❗️ Karta raqami 13–19 ta raqamdan iborat bo'lishi kerak.")
    await state.update_data(card=" ".join(digits[i:i + 4] for i in range(0, len(digits), 4)))
    await state.set_state(Adm.card_owner)
    await m.answer("👤 Karta egasining <b>F.I.O.</b> sini yuboring:")


@admin_r.message(StateFilter(Adm.card_owner))
async def card_owner(m: Message, state: FSMContext):
    d = await state.get_data()
    await db.execute("UPDATE settings SET value=? WHERE key='card_number'", (d["card"],))
    await db.execute("UPDATE settings SET value=? WHERE key='card_owner'", ((m.text or "").strip()[:60],))
    await state.clear()
    await m.answer(f"✅ Karta yangilandi:\n<code>{d['card']}</code>\n{esc(m.text)}", reply_markup=admin_kb())


# ------------------------------------------------------------ Statistika va Avto-Hisobchi
async def stats_text() -> str:
    today = now_tz().strftime("%Y-%m-%d")
    month = now_tz().strftime("%Y-%m")

    async def agg(where="", args=()):
        r = await db.fetchone(
            "SELECT COUNT(*) n, COALESCE(SUM(uc),0) uc, COALESCE(SUM(final),0) rev, COALESCE(SUM(cost),0) cost "
            f"FROM orders WHERE status='done' {where}", args)
        return r

    total, td, mo = await agg(), await agg("AND substr(done_at,1,10)=?", (today,)), \
        await agg("AND substr(done_at,1,7)=?", (month,))
    users = await db.fetchone("SELECT COUNT(*) n, COALESCE(SUM(is_vip),0) v, COALESCE(SUM(balance),0) b, "
                              "COALESCE(SUM(coins),0) c FROM users")
    today_users = await db.fetchone("SELECT COUNT(*) n FROM users WHERE substr(joined_at,1,10)=?", (today,))
    lot = await db.fetchone("SELECT COALESCE(SUM(cost),0) c FROM lottery_log")
    pend = await db.fetchone("SELECT (SELECT COUNT(*) FROM orders WHERE status='checking') o, "
                             "(SELECT COUNT(*) FROM topups WHERE status='checking') t")
    profit = total["rev"] - total["cost"]
    net = profit - lot["c"]
    return (f"📊 <b>Statistika & Avto-Hisobchi</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{fmt(users['n'])}</b> (bugun +{today_users['n']})\n"
            f"💎 VIP: <b>{users['v']}</b>\n"
            f"💰 Foydalanuvchilar balansi: {fmt(users['b'])} so'm | 🪙 {fmt(users['c'])}\n\n"
            f"<b>📦 Jami savdo</b>\nBuyurtmalar: {fmt(total['n'])} | UC: {fmt(total['uc'])}\n"
            f"Tushum: {fmt(total['rev'])} so'm\nTannarx: {fmt(total['cost'])} so'm\n"
            f"Sovrinlar xarajati: {fmt(lot['c'])} so'm\n"
            f"✅ <b>SOF FOYDA: {fmt(net)} so'm</b>\n\n"
            f"<b>📅 Bugun:</b> {td['n']} ta • {fmt(td['rev'])} so'm • foyda {fmt(td['rev'] - td['cost'])}\n"
            f"<b>🗓 Shu oy:</b> {mo['n']} ta • {fmt(mo['rev'])} so'm • foyda {fmt(mo['rev'] - mo['cost'])}\n\n"
            f"⏳ Kutilayotgan: {pend['o']} buyurtma, {pend['t']} to'ldirish")


@admin_r.message(F.text == T.A_STATS)
async def stats(m: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Foydalanuvchilar ro'yxati (CSV)", callback_data="st:users")
    kb.button(text="🧾 Buyurtmalar (CSV)", callback_data="st:orders")
    kb.adjust(1)
    await m.answer(await stats_text(), reply_markup=kb.as_markup())


@admin_r.callback_query(F.data.in_({"st:users", "st:orders"}))
async def stats_export(c: CallbackQuery):
    await c.answer("Tayyorlanmoqda...")
    buf = io.StringIO()
    w = csv.writer(buf)
    if c.data == "st:users":
        w.writerow(["id", "ism", "username", "balans", "tanga", "jami_uc", "vip", "referal", "ban", "qo'shilgan"])
        for r in await db.fetchall("SELECT * FROM users ORDER BY joined_at"):
            w.writerow([r["id"], r["full_name"], r["username"], r["balance"], r["coins"], r["total_uc"],
                        r["is_vip"], r["referrer_id"], r["banned"], r["joined_at"]])
        name = "users.csv"
    else:
        w.writerow(["id", "kod", "user_id", "pubg_id", "paket", "uc", "narx", "to'langan", "tannarx",
                    "foyda", "holat", "usul", "sana"])
        for r in await db.fetchall("SELECT * FROM orders ORDER BY id"):
            w.writerow([r["id"], r["code"], r["user_id"], r["pubg_id"], r["pkg_name"], r["uc"], r["price"],
                        r["final"], r["cost"], r["final"] - r["cost"], r["status"], r["pay_method"],
                        r["created_at"]])
        name = "orders.csv"
    data = buf.getvalue().encode("utf-8-sig")
    await c.message.answer_document(BufferedInputFile(data, filename=name))


# ------------------------------------------------------------ Kuponlar
@admin_r.message(F.text == T.A_COUPON)
async def cp_start(m: Message, state: FSMContext):
    await state.set_state(Adm.cp_code)
    await m.answer("🎟 Kupon <b>kodini</b> yozing (lotin harf/raqam, masalan: PUBG2026):", reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.cp_code))
async def cp_code(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{3,20}", code):
        return await m.answer("❗️ Kod 3–20 ta lotin harf/raqamdan iborat bo'lsin.")
    if await db.fetchone("SELECT 1 FROM coupons WHERE code=?", (code,)):
        return await m.answer("❗️ Bu kod mavjud. Boshqa kod yozing.")
    await state.update_data(code=code)
    await state.set_state(Adm.cp_disc)
    await m.answer("💵 Chegirma summasi (so'mda):")


@admin_r.message(StateFilter(Adm.cp_disc))
async def cp_disc(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    await state.update_data(disc=n)
    await state.set_state(Adm.cp_min)
    await m.answer("💎 Kamida necha UC dan boshlab amal qilsin? (0 — cheklovsiz):")


@admin_r.message(StateFilter(Adm.cp_min))
async def cp_min(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if n is None or n < 0:
        return await m.answer("❗️ 0 yoki musbat son kiriting.")
    await state.update_data(min_uc=n)
    await state.set_state(Adm.cp_limit)
    await m.answer("👥 Necha kishiga mo'ljallangan? (limit):")


@admin_r.message(StateFilter(Adm.cp_limit))
async def cp_limit(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    if not n or n <= 0:
        return await m.answer("❗️ Musbat son kiriting.")
    d = await state.get_data()
    await db.execute("INSERT INTO coupons(code,discount,min_uc,limit_count) VALUES(?,?,?,?)",
                     (d["code"], d["disc"], d["min_uc"], n))
    await state.clear()
    await m.answer(f"✅ Kupon yaratildi!\n🎟 <code>{d['code']}</code>\n💵 Chegirma: {fmt(d['disc'])} so'm\n"
                   f"💎 Minimal: {d['min_uc']} UC\n👥 Limit: {n} kishi", reply_markup=admin_kb())


async def coupons_view():
    rows = await db.fetchall("SELECT * FROM coupons ORDER BY id DESC LIMIT 20")
    b = InlineKeyboardBuilder()
    lines = []
    for r in rows:
        lines.append(f"{'🟢' if r['active'] else '⚪️'} <code>{esc(r['code'])}</code> • −{fmt(r['discount'])} • "
                     f"≥{r['min_uc']} UC • {r['used_count']}/{r['limit_count']}")
        b.button(text=f"{'⏸' if r['active'] else '▶️'} {r['code']}", callback_data=f"cpt:{r['id']}")
        b.button(text="🗑", callback_data=f"cpd:{r['id']}")
    b.adjust(2)
    return "🎫 <b>Kuponlar</b>\n\n" + ("\n".join(lines) if lines else "Kupon yo'q."), b.as_markup()


@admin_r.message(F.text == T.A_COUPONS)
async def coupons_menu(m: Message):
    text, kb = await coupons_view()
    await m.answer(text, reply_markup=kb)


@admin_r.callback_query(F.data.startswith(("cpt:", "cpd:")))
async def coupon_manage(c: CallbackQuery):
    act, cid = c.data.split(":")
    if act == "cpt":
        await db.execute("UPDATE coupons SET active=1-active WHERE id=?", (int(cid),))
    else:
        await db.execute("DELETE FROM coupons WHERE id=?", (int(cid),))
    await c.answer("Bajarildi")
    text, kb = await coupons_view()
    await safe_edit(c.message, text, kb)


# ------------------------------------------------------------ Buyurtma / to'ldirishni tasdiqlash
async def mark_msg(c: CallbackQuery, note: str):
    try:
        body = c.message.html_text or ""
        if c.message.photo or c.message.document:
            await c.message.edit_caption(caption=f"{body}\n\n{note}", reply_markup=None)
        else:
            await c.message.edit_text(f"{body}\n\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


@admin_r.callback_query(F.data.startswith("oa:"))
async def order_approve(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[1])
    info = await complete_order(oid, c.from_user.id)
    if not info:
        await c.answer("Bu buyurtma allaqachon ishlangan.", show_alert=True)
        return await mark_msg(c, "ℹ️ Allaqachon ishlangan.")
    await c.answer("✅ Tasdiqlandi")
    o = info["order"]
    await mark_msg(c, f"✅ Tasdiqladi: {esc(c.from_user.full_name)}")
    txt = (f"✅ <b>Buyurtma {o['code']} bajarildi!</b>\n💎 {o['uc']} UC PUBG ID <code>{esc(o['pubg_id'])}</code> "
           f"ga yuborildi.\n🪙 Keshbek: <b>+{info['cashback']}</b> tanga\n💎 Jami xaridingiz: {info['total_uc']} UC")
    if info["became_vip"]:
        txt += "\n\n🎉 <b>Tabriklaymiz! Siz endi VIP maqomdasiz!</b> Keshbek ×2 va oylik TOP-1 o'yinida ishtirok."
    await tell(bot, o["user_id"], txt)
    if info["ref_id"]:
        await tell(bot, info["ref_id"], f"🎁 Taklif qilgan do'stingiz 1-xaridini qildi! +{info['ref_bonus']} 🪙")


@admin_r.callback_query(F.data.startswith("or:"))
async def order_reject(c: CallbackQuery, bot: Bot):
    o = await cancel_order(int(c.data.split(":")[1]))
    if not o:
        await c.answer("Bu buyurtma allaqachon ishlangan.", show_alert=True)
        return await mark_msg(c, "ℹ️ Allaqachon ishlangan.")
    await c.answer("Rad etildi")
    await mark_msg(c, f"❌ Rad etdi: {esc(c.from_user.full_name)}")
    await tell(bot, o["user_id"], f"❌ <b>Buyurtma {o['code']} rad etildi.</b>\nTo'lov tasdiqlanmadi. "
                                  f"Tanga/kupon/balans qaytarildi. Savol bo'lsa: {esc(await S('support'))}")


@admin_r.callback_query(F.data.startswith("ta:"))
async def topup_approve(c: CallbackQuery, bot: Bot):
    t = await complete_topup(int(c.data.split(":")[1]), c.from_user.id)
    if not t:
        await c.answer("Allaqachon ishlangan.", show_alert=True)
        return await mark_msg(c, "ℹ️ Allaqachon ishlangan.")
    await c.answer("✅ Tasdiqlandi")
    await mark_msg(c, f"✅ Tasdiqladi: {esc(c.from_user.full_name)}")
    await tell(bot, t["user_id"], f"✅ Hisobingiz <b>{fmt(t['amount'])} so'm</b> ga to'ldirildi!")


@admin_r.callback_query(F.data.startswith("tr:"))
async def topup_reject(c: CallbackQuery, bot: Bot):
    tid = int(c.data.split(":")[1])
    _, rc = await db.execute("UPDATE topups SET status='cancelled', done_at=datetime('now','+5 hours') "
                             "WHERE id=? AND status IN ('checking','awaiting_check')", (tid,))
    if not rc:
        await c.answer("Allaqachon ishlangan.", show_alert=True)
        return await mark_msg(c, "ℹ️ Allaqachon ishlangan.")
    t = await db.fetchone("SELECT * FROM topups WHERE id=?", (tid,))
    await c.answer("Rad etildi")
    await mark_msg(c, f"❌ Rad etdi: {esc(c.from_user.full_name)}")
    await tell(bot, t["user_id"], f"❌ To'ldirish {t['code']} tasdiqlanmadi. Yordam: {esc(await S('support'))}")


@admin_r.message(F.text == T.A_PENDING)
async def pending(m: Message, bot: Bot):
    os_ = await db.fetchall("SELECT id FROM orders WHERE status='checking' ORDER BY id LIMIT 15")
    ts = await db.fetchall("SELECT id FROM topups WHERE status='checking' ORDER BY id LIMIT 15")
    if not os_ and not ts:
        return await m.answer("✅ Kutilayotgan so'rovlar yo'q.")
    for r in os_:
        o = await db.fetchone("SELECT check_file_id FROM orders WHERE id=?", (r["id"],))
        await _resend(bot, m.from_user.id, "order", r["id"])
    for r in ts:
        await _resend(bot, m.from_user.id, "topup", r["id"])


async def _resend(bot: Bot, admin_id: int, kind: str, rid: int):
    # Faqat shu adminga qayta yuborish uchun vaqtincha admin ro'yxatini almashtiramiz
    global admin_ids
    orig = admin_ids

    async def only_me():
        return [admin_id]
    admin_ids = only_me
    try:
        await notify_admins(bot, kind, rid)
    finally:
        admin_ids = orig


# ------------------------------------------------------------ Foydalanuvchi boshqaruvi
def user_card(u) -> tuple:
    text = (f"👤 <b>{esc(u['full_name'])}</b> (<code>{u['id']}</code>)\n@{esc(u['username'] or '—')}\n"
            f"💰 Balans: {fmt(u['balance'])} so'm | 🪙 {fmt(u['coins'])}\n"
            f"💎 Jami: {fmt(u['total_uc'])} UC | {'VIP' if u['is_vip'] else 'Oddiy'}\n"
            f"🚫 Ban: {'ha' if u['banned'] else 'yo`q'} | 📅 {u['joined_at'][:10]}")
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Balans ±", callback_data=f"um:bal:{u['id']}")
    kb.button(text="🪙 Tanga ±", callback_data=f"um:coin:{u['id']}")
    kb.button(text="✅ Unban" if u["banned"] else "🚫 Ban", callback_data=f"um:ban:{u['id']}")
    kb.button(text="💎 VIP almashtirish", callback_data=f"um:vip:{u['id']}")
    kb.adjust(2)
    return text, kb.as_markup()


@admin_r.message(F.text == T.A_USER)
async def user_start(m: Message, state: FSMContext):
    await state.set_state(Adm.user_id)
    await m.answer("👤 Foydalanuvchi <b>ID</b> sini yuboring:", reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.user_id))
async def user_find(m: Message, state: FSMContext):
    n = parse_int(m.text or "")
    u = await get_user(n) if n else None
    if not u:
        return await m.answer("❌ Foydalanuvchi topilmadi. ID ni tekshiring.")
    await state.clear()
    text, kb = user_card(u)
    await m.answer(text, reply_markup=kb)
    await m.answer("Admin panel", reply_markup=admin_kb())


@admin_r.callback_query(F.data.startswith("um:"))
async def user_action(c: CallbackQuery, state: FSMContext):
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if act in ("bal", "coin"):
        await c.answer()
        await state.set_state(Adm.user_bal if act == "bal" else Adm.user_coin)
        await state.update_data(uid=uid)
        return await c.message.answer("Qo'shish uchun musbat, ayirish uchun manfiy son yuboring (masalan 5000 yoki -2000):",
                                      reply_markup=cancel_kb())
    if act == "ban":
        await db.execute("UPDATE users SET banned=1-banned WHERE id=?", (uid,))
    elif act == "vip":
        await db.execute("UPDATE users SET is_vip=1-is_vip WHERE id=?", (uid,))
    await c.answer("Bajarildi")
    text, kb = user_card(await get_user(uid))
    await safe_edit(c.message, text, kb)


@admin_r.message(StateFilter(Adm.user_bal))
@admin_r.message(StateFilter(Adm.user_coin))
async def user_adjust(m: Message, state: FSMContext, bot: Bot):
    n = parse_int(m.text or "")
    if n is None:
        return await m.answer("❗️ Son kiriting.")
    d = await state.get_data()
    is_bal = (await state.get_state()) == Adm.user_bal.state
    col = "balance" if is_bal else "coins"
    await db.execute(f"UPDATE users SET {col}=MAX({col}+?,0) WHERE id=?", (n, d["uid"]))
    await state.clear()
    u = await get_user(d["uid"])
    text, kb = user_card(u)
    await m.answer("✅ Yangilandi.\n\n" + text, reply_markup=kb)
    await m.answer("Admin panel", reply_markup=admin_kb())
    await tell(bot, d["uid"], f"ℹ️ Hisobingiz o'zgartirildi: {'balans' if is_bal else 'tanga'} {n:+d}")


# ------------------------------------------------------------ VIP o'yini
@admin_r.message(F.text == T.A_LOTTERY)
async def lottery_menu(m: Message):
    cur = now_tz().strftime("%Y-%m")
    prev = (now_tz().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎰 Joriy oy ({cur})", callback_data=f"lot:{cur}")
    kb.button(text=f"🎰 O'tgan oy ({prev})", callback_data=f"lot:{prev}")
    kb.adjust(1)
    await m.answer("🎰 <b>VIP TOP-1 o'yini</b>\n\nTanlangan oyda eng ko'p UC olgan VIP g'olib bo'ladi va "
                   "tasodifiy <b>30 yoki 60 UC</b> yutadi.\n(Har oyning 1-kuni avtomatik ham o'tkaziladi.)",
                   reply_markup=kb.as_markup())


@admin_r.callback_query(F.data.startswith("lot:"))
async def lottery_run(c: CallbackQuery, bot: Bot):
    await c.answer("O'yin boshlandi...")
    await c.message.answer(await run_lottery(bot, c.data.split(":")[1]))


# ------------------------------------------------------------ Tizim sozlamalari
async def settings_view():
    b = InlineKeyboardBuilder()
    lines = []
    for k, (label, _) in SETTING_META.items():
        lines.append(f"• {label}: <b>{esc(await S(k))}</b>")
        b.button(text=label[:30], callback_data=f"set:{k}")
    b.adjust(1)
    return "🛠 <b>Tizim sozlamalari</b>\n\n" + "\n".join(lines) + "\n\nO'zgartirish uchun tanlang:", b.as_markup()


@admin_r.message(F.text == T.A_SETTINGS)
async def settings_menu(m: Message):
    text, kb = await settings_view()
    await m.answer(text, reply_markup=kb)


@admin_r.callback_query(F.data.startswith("set:"))
async def setting_pick(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in SETTING_META:
        return await c.answer("Noma'lum")
    await c.answer()
    await state.set_state(Adm.setting_val)
    await state.update_data(key=key)
    await c.message.answer(f"✏️ <b>{SETTING_META[key][0]}</b>\nHozirgi: <b>{esc(await S(key))}</b>\nYangi qiymat:",
                           reply_markup=cancel_kb())


@admin_r.message(StateFilter(Adm.setting_val))
async def setting_save(m: Message, state: FSMContext):
    d = await state.get_data()
    key = d["key"]
    val = (m.text or "").strip()
    if SETTING_META[key][1] == "int":
        n = parse_int(val)
        if n is None or n < 0:
            return await m.answer("❗️ 0 yoki musbat son kiriting.")
        val = str(n)
    await db.execute("UPDATE settings SET value=? WHERE key=?", (val, key))
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=admin_kb())


# ------------------------------------------------------------ Backup
@admin_r.message(F.text == T.A_BACKUP)
async def backup(m: Message):
    tmp = Path(DB_PATH).parent / f"backup_{int(time.time())}.db"
    try:
        async with db.lock:
            await db.conn.execute("VACUUM INTO ?", (str(tmp),))
        data = tmp.read_bytes()
        await m.answer_document(BufferedInputFile(data, filename=f"pro_uc_bot_{now_tz():%Y%m%d_%H%M}.db"),
                                caption="💾 Baza zaxira nusxasi. Yangi serverda DB_PATH ga qo'ying — hamma narsa saqlanadi.")
    except Exception as e:
        await m.answer(f"❌ Zaxira xatosi: {esc(e)}")
    finally:
        tmp.unlink(missing_ok=True)


# ============================================================ FON VAZIFALAR
async def expire_pending(bot: Bot):
    mod = f"-{ORDER_EXPIRE_MIN} minutes"
    for r in await db.fetchall("SELECT id,user_id,code FROM orders WHERE status='awaiting_check' "
                               "AND created_at < datetime('now','+5 hours',?)", (mod,)):
        if await cancel_order(r["id"], ("awaiting_check",)):
            await tell(bot, r["user_id"], f"⌛️ Buyurtma <b>{r['code']}</b> vaqt tugagani uchun bekor qilindi "
                                          f"(tanga/kupon qaytarildi).")
    await db.execute("UPDATE topups SET status='cancelled' WHERE status='awaiting_check' "
                     "AND created_at < datetime('now','+5 hours',?)", (mod,))


async def auto_lottery(bot: Bot):
    now = now_tz()
    if now.day != 1:
        return
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if not await db.fetchone("SELECT 1 FROM lottery_log WHERE month=?", (prev,)):
        log.info("Avto VIP o'yini: %s", await run_lottery(bot, prev))


async def maintenance(bot: Bot):
    tick = 0
    while True:
        try:
            await expire_pending(bot)
            if tick % 60 == 0:
                await auto_lottery(bot)
        except Exception:
            log.exception("maintenance xatosi")
        tick += 1
        await asyncio.sleep(60)


# ============================================================ ISHGA TUSHIRISH
async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN topilmadi (.env faylini tekshiring).")
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    await db.connect()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=SQLiteStorage(db))
    dp.message.filter(F.chat.type == "private")
    dp.message.outer_middleware(StateInterruptMiddleware())
    dp.message.outer_middleware(GateMiddleware())
    dp.callback_query.outer_middleware(GateMiddleware())
    dp.include_router(common_r)
    dp.include_router(admin_r)
    dp.include_router(user_r)

    maint = asyncio.create_task(maintenance(bot))
    try:
        if WEBHOOK_URL:
            from aiohttp import web
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

            app = web.Application()

            async def health(_):
                return web.json_response({"status": "ok", "bot": "Pro UC Bot"})

            app.router.add_get("/", health)
            SimpleRequestHandler(dp, bot, secret_token=WEBHOOK_SECRET or None).register(app, path=WEBHOOK_PATH)
            setup_application(app, dp, bot=bot)
            await bot.set_webhook(WEBHOOK_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET or None,
                                  allowed_updates=dp.resolve_used_update_types())
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
            log.info("Webhook rejimi: %s:%s", WEB_HOST, WEB_PORT)
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            log.info("Polling rejimi ishga tushdi")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        maint.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
