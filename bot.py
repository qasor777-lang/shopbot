import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import socket
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote

from aiohttp import ClientSession, ClientTimeout, web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo, MenuButtonDefault, MenuButtonWebApp, BotCommand, CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip().strip("'\"")
ADMIN_ID = int((os.getenv("ADMIN_ID", "0") or "0").strip() or 0)
DB = str(Path(os.getenv("SHOP_DB_PATH", str(BASE / "shop.db"))).expanduser())
Path(DB).parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = Path(os.getenv("SHOP_UPLOAD_DIR", str(Path(DB).parent / "uploads"))).expanduser()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")  # botning ochiq HTTPS manzili (katalog API uchun)
PLATFORM_PORT = os.getenv("PORT", "").strip()
API_PORT = int(PLATFORM_PORT or os.getenv("API_PORT", "8080") or 8080)


def choose_free_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return port
        except OSError:
            for candidate in range(port + 1, port + 50):
                try:
                    s.bind(("0.0.0.0", candidate))
                    return candidate
                except OSError:
                    continue
            return port


if not PLATFORM_PORT:
    API_PORT = choose_free_port(API_PORT)


def webapp_link():
    candidates = []
    for value in (WEBAPP_URL, PUBLIC_URL):
        cleaned = (value or "").strip().rstrip("/")
        if cleaned and "SIZNING-SAYT" not in cleaned and cleaned.startswith("https://"):
            candidates.append(cleaned)

    if not candidates:
        logging.warning(
            "Telegram WebApp uchun HTTPS public URL topilmadi. "
            "PUBLIC_URL/WEBAPP_URL ni .env ga aniq https link bilan yozing. "
            "localhost ishlamaydi."
        )
        return ""

    base = candidates[0]
    if PUBLIC_URL and base.rstrip("/") != PUBLIC_URL.rstrip("/"):
        return f"{base}?api={quote(PUBLIC_URL.rstrip('/'), safe='')}"
    return base

STATUS = {"p": ("🟡", "Jarayonda"), "d": ("🟢", "Yetkazilgan"), "c": ("🔴", "Bekor qilingan")}
POPULAR = ["iPhone", "Samsung", "Smart soat", "AirPods", "Noutbuk", "Aksessuar"]

r = Router()


# ---------------------------------------------------------------- database
# Turso (cloud SQLite): TURSO_DATABASE_URL va TURSO_AUTH_TOKEN berilsa baza bulutda saqlanadi
# (serverda doimiy disk bo'lmasa ham ma'lumot yo'qolmaydi). Bo'lmasa lokal shop.db ishlatiladi.
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
USE_TURSO = bool(TURSO_URL)


class Row:
    """sqlite3.Row ga o'xshash satr: row["name"], row[0] va unpack (a,b=row) ham ishlaydi."""

    __slots__ = ("_cols", "_vals", "_idx")

    def __init__(self, cols, vals):
        self._cols = tuple(cols)
        self._vals = tuple(vals)
        self._idx = {k: i for i, k in enumerate(self._cols)}

    def __getitem__(self, k):
        if isinstance(k, str):
            return self._vals[self._idx[k]]
        return self._vals[k]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __contains__(self, k):
        return k in self._idx

    def keys(self):
        return list(self._cols)


def _connect():
    if USE_TURSO:
        try:
            import libsql
        except ImportError:
            import libsql_experimental as libsql

        return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    return sqlite3.connect(DB)


def _row(cur, vals):
    return Row([d[0] for d in cur.description], vals)


def run(sql, args=(), fetch=None):
    conn = _connect()
    try:
        cur = conn.execute(sql, args)
        if fetch == "one":
            r = cur.fetchone()
            return None if r is None else _row(cur, r)
        if fetch == "all":
            return [_row(cur, r) for r in cur.fetchall()]
        conn.commit()
        lid = getattr(cur, "lastrowid", None)
        if lid is None and sql.lstrip()[:6].upper() == "INSERT":
            lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return lid
    finally:
        conn.close()


SCHEMA_PRODUCTS = """CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, emoji TEXT, image TEXT DEFAULT '',
    price INTEGER, old_price INTEGER DEFAULT 0, kw TEXT DEFAULT '', cat INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1)"""

CATS = [("Mevalar", "🍎"), ("Sabzavotlar", "🥕"), ("Kartoshka & Ko'katlar", "🥔"), ("Go'sht mahsulotlari", "🥩"),
        ("Sut mahsulotlari", "🥛"), ("Non mahsulotlari", "🍞"), ("Shirinliklar", "🍫"), ("Ichimliklar", "🥤")]


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, username TEXT,
    notif INTEGER DEFAULT 1, points INTEGER DEFAULT 0, phone TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS cart(
    user_id INTEGER, product_id INTEGER, qty INTEGER,
    PRIMARY KEY(user_id, product_id));
CREATE TABLE IF NOT EXISTS favorites(
    user_id INTEGER, product_id INTEGER, PRIMARY KEY(user_id, product_id));
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    created TEXT, status TEXT DEFAULT 'p', total INTEGER, note TEXT DEFAULT '',
    phone TEXT DEFAULT '', customer_name TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS order_items(
    order_id INTEGER, name TEXT, qty INTEGER, price INTEGER);
"""


def _cols(conn, table):
    return [r[0] for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')").fetchall()]


def init_db():
    conn = _connect()
    try:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute(SCHEMA_PRODUCTS)
        if "cat" not in _cols(conn, "products"):  # eski (elektronika) bazani yangilash
            conn.execute("DROP TABLE products")
            conn.execute("DELETE FROM cart")
            conn.execute("DELETE FROM favorites")
            conn.execute(SCHEMA_PRODUCTS)
        if "active" not in _cols(conn, "products"):  # admin bot uchun
            conn.execute("ALTER TABLE products ADD COLUMN active INTEGER DEFAULT 1")
        if "image" not in _cols(conn, "products"):
            conn.execute("ALTER TABLE products ADD COLUMN image TEXT DEFAULT ''")
        if "note" not in _cols(conn, "orders"):
            conn.execute("ALTER TABLE orders ADD COLUMN note TEXT DEFAULT ''")
        if "phone" not in _cols(conn, "orders"):  # telefon majburiy
            conn.execute("ALTER TABLE orders ADD COLUMN phone TEXT DEFAULT ''")
        if "customer_name" not in _cols(conn, "orders"):
            conn.execute("ALTER TABLE orders ADD COLUMN customer_name TEXT DEFAULT ''")
        if "phone" not in _cols(conn, "users"):
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")
        conn.commit()
    finally:
        conn.close()
# ---------------------------------------------------------------- helpers
def money(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def esc(s):
    return html.escape(str(s or ""))


def discount(p):
    return round((1 - p["price"] / p["old_price"]) * 100) if p["old_price"] else 0


def price_line(p):
    if p["old_price"]:
        return f"<s>{money(p['old_price'])}</s>  <b>{money(p['price'])}</b>  🔴 -{discount(p)}%"
    return f"<b>{money(p['price'])}</b>"


def nav():
    return [
        B(text="🏠", callback_data="m:home"),
        B(text="🗂", callback_data="m:cats"),
        B(text="🛒", callback_data="m:cart"),
        B(text="📦", callback_data="m:orders:all"),
        B(text="👤", callback_data="m:profile"),
    ]


def kb(rows, with_nav=True):
    rows = list(rows)
    if with_nav:
        rows.append(nav())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def norm_phone(s):
    """O'zbekiston raqami: +998XXXXXXXXX ko'rinishiga keltiradi, noto'g'ri bo'lsa None."""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 9:
        d = "998" + d
    return "+" + d if re.fullmatch(r"998\d{9}", d) else None


def get_phone(uid):
    row = run("SELECT phone FROM users WHERE id=?", (uid,), fetch="one")
    return (row["phone"] or "") if row else ""


WAIT_PHONE = set()  # telefon raqamini kutayotgan foydalanuvchilar (botdagi "Buyurtma berish" uchun)


def home_kbd():
    link = webapp_link()
    if not link:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Web ilovani ochish", web_app=WebAppInfo(url=link))]],
        resize_keyboard=True,
    )


def save_user(u):
    run(
        "INSERT INTO users(id,name,username) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (u.id, u.full_name, u.username or ""),
    )


async def render(ev, text, markup):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
    else:
        await ev.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- screens
def s_home(u):
    link = webapp_link()
    text = (
        "🌿 <b>FreshMarket</b>\n<i>Siz uchun eng sifatli mahsulotlar!</i>\n\n"
        "🚚 Tez yetkazib berish  •  🛡 Sifat kafolati  •  💳 Xavfsiz to'lov  •  🎧 24/7 yordam\n\n"
        f"👋 <b>Salom, {esc(u.first_name)}!</b>\n"
        "FreshMarket botiga xush kelibsiz! Bu yerda siz meva, sabzavot, go'sht, sut mahsulotlari "
        "va boshqa oziq-ovqatlarni buyurtma qilishingiz mumkin.\n\n"
        "Sayt orqali buyurtma berish uchun chat pastidagi <b>Web ilovani ochish</b> tugmasini bosing."
    )
    rows = [
        [B(text="🗂 Kategoriyalar", callback_data="m:cats")],
        [B(text="🔥 Aksiyalar", callback_data="m:promo"), B(text="🛒 Savat", callback_data="m:cart")],
        [B(text="📦 Buyurtmalarim", callback_data="m:orders:all"), B(text="👤 Profil", callback_data="m:profile")],
        [B(text="🔍 Qidiruv", callback_data="m:search"), B(text="⚙️ Sozlamalar", callback_data="m:settings")],
    ]
    return text, kb(rows, with_nav=False)


def product_rows(products, back="m:home"):
    rows = []
    for p in products:
        label = f"{p['emoji']} {p['name']} — {money(p['price'])}"
        if p["old_price"]:
            label += f" (-{discount(p)}%)"
        rows.append([B(text=label, callback_data=f"p:{p['id']}")])
    return rows


def s_promo():
    items = run("SELECT * FROM products WHERE old_price>0 AND active=1 ORDER BY id", fetch="all")
    text = "🎁 <b>Maxsus Aksiyalar!</b>\nSevimli mahsulotlaringiz endi yanada arzon!\n\n"
    if not items:
        text += "Hozircha aksiyalar yo'q."
    return text + "Mahsulotni tanlang 👇", kb(product_rows(items))


def s_cats():
    counts = {x["cat"]: x["n"] for x in run("SELECT cat, COUNT(*) n FROM products WHERE active=1 GROUP BY cat", fetch="all")}
    rows = [
        [B(text=f"{CATS[k][1]} {CATS[k][0]} ({counts.get(k, 0)})", callback_data=f"c:{k}") for k in (j, j + 1)]
        for j in range(0, len(CATS), 2)
    ]
    return "🗂 <b>Kategoriyalar</b>\n\nKerakli bo'limni tanlang 👇", kb(rows)


def s_cat(k):
    items = run("SELECT * FROM products WHERE cat=? AND active=1 ORDER BY id", (k,), fetch="all")
    rows = product_rows(items) + [[B(text="⬅️ Kategoriyalar", callback_data="m:cats")]]
    return f"{CATS[k][1]} <b>{CATS[k][0]}</b>\n\nMahsulotni tanlang 👇", kb(rows)


def s_search(query=None):
    if query is None:
        text = (
            "🔍 <b>Qidiruv</b>\n\nMahsulot nomini yozing...\n\n<b>Mashhur qidiruvlar</b>"
        )
        rows = [
            [B(text=POPULAR[i], callback_data=f"q:{POPULAR[i]}") for i in range(j, j + 3)]
            for j in (0, 3)
        ]
        return text, kb(rows)
    like = f"%{query.lower()}%"
    items = run(
        "SELECT * FROM products WHERE active=1 AND (lower(name) LIKE ? OR lower(kw) LIKE ?)", (like, like), fetch="all"
    )
    if not items:
        text = f"😕 «{esc(query)}» bo'yicha hech narsa topilmadi.\nBoshqa nom bilan urinib ko'ring."
        return text, kb([[B(text="🔍 Qayta qidirish", callback_data="m:search")]])
    return f"🔍 «{esc(query)}» natijalari:", kb(product_rows(items) + [[B(text="🔍 Qayta qidirish", callback_data="m:search")]])


def s_product(uid, pid):
    p = run("SELECT * FROM products WHERE id=? AND active=1", (pid,), fetch="one")
    if not p:
        return "Mahsulot topilmadi.", kb([])
    fav = run("SELECT 1 FROM favorites WHERE user_id=? AND product_id=?", (uid, pid), fetch="one")
    text = f"{p['emoji']} <b>{esc(p['name'])}</b>\n\n{price_line(p)}"
    rows = [
        [B(text="🛒 Savatga qo'shish", callback_data=f"add:{pid}")],
        [B(text="💔 Sevimlidan olish" if fav else "❤️ Sevimlilarga", callback_data=f"fav:{pid}")],
        [B(text="⬅️ Orqaga", callback_data=f"c:{p['cat']}")],
    ]
    return text, kb(rows)


def cart_items(uid):
    return run(
        "SELECT p.*, c.qty FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND p.active=1 ORDER BY p.id",
        (uid,),
        fetch="all",
    )


def s_cart(uid):
    items = cart_items(uid)
    if not items:
        text = "🛒 <b>Savat bo'sh</b>\n\nHozircha sizning savatingizda mahsulot yo'q."
        return text, kb([[B(text="🛍 Mahsulotlarni ko'rish", callback_data="m:cats")]])
    total = sum(i["price"] * i["qty"] for i in items)
    lines = [f"{i['emoji']} {esc(i['name'])} × {i['qty']} = {money(i['price'] * i['qty'])}" for i in items]
    text = "🛒 <b>Savat</b>\n\n" + "\n".join(lines) + f"\n\n💰 Jami: <b>{money(total)}</b>"
    rows = []
    for i in items:
        rows.append(
            [
                B(text="➖", callback_data=f"dec:{i['id']}"),
                B(text=f"{i['name']} ({i['qty']})", callback_data=f"p:{i['id']}"),
                B(text="➕", callback_data=f"inc:{i['id']}"),
            ]
        )
    rows.append(
        [B(text="🗑 Tozalash", callback_data="clear"), B(text="✅ Buyurtma berish", callback_data="checkout")]
    )
    return text, kb(rows)


def s_orders(uid, flt):
    sql, args = "SELECT * FROM orders WHERE user_id=?", [uid]
    if flt in ("p", "d"):
        sql += " AND status=?"
        args.append(flt)
    orders = run(sql + " ORDER BY id DESC LIMIT 10", tuple(args), fetch="all")
    text = "📦 <b>Buyurtmalarim</b>\n\n"
    if not orders:
        text += "Hozircha buyurtmalar yo'q."
    for o in orders:
        icon, name = STATUS[o["status"]]
        items = run("SELECT * FROM order_items WHERE order_id=?", (o["id"],), fetch="all")
        names = ", ".join(f"{esc(i['name'])} ×{i['qty']}" for i in items)
        text += f"<b>#{o['id']}</b>  {icon} {name}\n🕒 {o['created']}\n{names}\n💰 {money(o['total'])}\n\n"
    mark = lambda k, t: f"• {t}" if flt == k else t
    rows = [
        [
            B(text=mark("all", "Barchasi"), callback_data="m:orders:all"),
            B(text=mark("p", "Jarayonda"), callback_data="m:orders:p"),
            B(text=mark("d", "Yetkazilgan"), callback_data="m:orders:d"),
        ]
    ]
    return text, kb(rows)


def s_profile(u):
    row = run("SELECT * FROM users WHERE id=?", (u.id,), fetch="one")
    orders = run("SELECT COUNT(*) n FROM orders WHERE user_id=?", (u.id,), fetch="one")["n"]
    favs = run("SELECT COUNT(*) n FROM favorites WHERE user_id=?", (u.id,), fetch="one")["n"]
    uname = f"@{esc(u.username)}" if u.username else "—"
    text = (
        f"👤 <b>{esc(u.full_name)}</b>\n{uname}\n\n"
        f"📦 Buyurtmalar: <b>{orders}</b>\n❤️ Sevimlilar: <b>{favs}</b>\n⭐ Ballar: <b>{row['points']}</b>"
    )
    rows = [
        [B(text="❤️ Sevimlilarim", callback_data="favs")],
        [B(text="📦 Buyurtmalarim", callback_data="m:orders:all")],
        [B(text="⚙️ Sozlamalar", callback_data="m:settings")],
        [B(text="❓ Yordam", callback_data="m:about")],
    ]
    return text, kb(rows)


def s_settings(uid):
    n = run("SELECT notif FROM users WHERE id=?", (uid,), fetch="one")["notif"]
    text = "⚙️ <b>Sozlamalar</b>"
    rows = [
        [B(text="🌐 Til: O'zbekcha", callback_data="noop")],
        [B(text=f"🔔 Bildirishnomalar: {'Yoqilgan ✅' if n else 'O‘chirilgan ❌'}", callback_data="notif")],
        [B(text="ℹ️ Bot haqida", callback_data="m:about")],
    ]
    return text, kb(rows)


def s_about():
    text = (
        "🌿 <b>FreshMarket</b>\n<i>Siz bilan har doim!</i>\n\n"
        "Sifatli mahsulotlar, tez yetkazib berish va 24/7 yordam.\n"
        "Savollar bo'lsa, do'kon administratoriga murojaat qiling."
    )
    return text, kb([])


# ---------------------------------------------------------------- handlers

async def notify_admin(bot, oid, text, markup):
    """Yangi buyurtma: avval admin botga (to'liq ma'lumot + tugmalar), bo'lmasa do'kon boti orqali."""
    try:
        import admin_bot
        if await admin_bot.notify_new_order(oid):
            return
    except Exception:
        logging.exception("admin bot orqali xabar yuborilmadi")
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=markup)
    except Exception:
        logging.exception("admin notify failed")


@r.message(CommandStart())
async def start(m: Message):
    save_user(m.from_user)
    await m.answer(
        "🛒 Web ilovada mahsulotlarni ko'ring va buyurtma bering.",
        reply_markup=home_kbd(),
    )
    text, markup = s_home(m.from_user)
    await m.answer(text, reply_markup=markup)


@r.message(F.web_app_data)
async def webapp_order(m: Message):
    # Narxlar HECH QACHON mijozdan olinmaydi: faqat bazadagi (admin belgilagan) narx ishlatiladi.
    try:
        o = json.loads(m.web_app_data.data)
        req = [
            (i.get("id"), str(i.get("n", ""))[:60], str(i.get("u", ""))[:20], min(100, max(1, int(i["q"]))), i.get("p"))
            for i in o["items"]
        ][:50]
        if not req:
            raise ValueError
    except Exception:
        await m.answer("❌ Buyurtmani o'qib bo'lmadi, qayta urinib ko'ring.")
        return
    phone = norm_phone(o.get("phone"))
    if not phone:
        await m.answer("❌ Telefon raqami kiritilmagan yoki noto'g'ri. Saytni qayta oching va raqamni <b>+998 XX XXX XX XX</b> ko'rinishida kiriting.")
        return
    items, missing, changed = [], [], False
    for pid, n, u, q, cp in req:
        p = None
        try:
            if pid is not None:
                p = run("SELECT * FROM products WHERE id=? AND active=1", (int(pid),), fetch="one")
            else:
                p = run("SELECT * FROM products WHERE active=1 AND (name=? OR name=?)", (f"{n}, {u}", n), fetch="one")
        except (TypeError, ValueError):
            p = None
        if p is None:
            missing.append(n or "?")
            continue
        if cp is not None and str(cp) != str(p["price"]):
            changed = True
        items.append((p["name"], q, p["price"]))
    if missing:
        await m.answer(
            "⚠️ Quyidagi mahsulotlar hozir do'konda mavjud emas:\n• " + "\n• ".join(esc(x) for x in missing)
            + "\n\nSaytni qayta oching va buyurtmani yangilang."
        )
        return
    save_user(m.from_user)
    run("UPDATE users SET phone=? WHERE id=?", (phone, m.from_user.id))
    total = sum(q * p for _, q, p in items)
    note = f"{str(o.get('addr', ''))[:200]} | {str(o.get('pay', ''))[:50]} | {str(o.get('slot', ''))[:50]}"
    oid = run(
        "INSERT INTO orders(user_id,created,status,total,note,phone) VALUES(?,?,?,?,?,?)",
        (m.from_user.id, datetime.now().strftime("%d.%m.%Y, %H:%M"), "p", total, note, phone),
    )
    for n, q, p in items:
        run("INSERT INTO order_items VALUES(?,?,?,?)", (oid, n, q, p))
    lines = "\n".join(f"• {esc(n)} × {q} = {money(q * p)}" for n, q, p in items)
    info = f"📞 {esc(phone)}\n📍 {esc(str(o.get('addr', ''))[:200])}\n💳 {esc(str(o.get('pay', ''))[:50])}\n🕒 {esc(str(o.get('slot', ''))[:50])}"
    extra = "\nℹ️ Ba'zi narxlar yangilangan — hisob joriy narxlar bo'yicha." if changed else ""
    await m.answer(f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n{lines}\n\n{info}\n💰 Jami: <b>{money(total)}</b>{extra}")
    if ADMIN_ID:
        uname = f"@{esc(m.from_user.username)}" if m.from_user.username else "—"
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[[B(text="🟢 Yetkazilgan", callback_data=f"os:{oid}:d"), B(text="🔴 Bekor qilish", callback_data=f"os:{oid}:c")]]
        )
        await notify_admin(
            m.bot,
            oid,
            f"🆕 <b>Yangi buyurtma #{oid}</b>\n👤 {esc(m.from_user.full_name)} {uname}\n{lines}\n{info}\n💰 {money(total)}",
            admin_kb,
        )


@r.message(Command("menu"))
async def menu_cmd(m: Message):
    save_user(m.from_user)
    text, markup = s_home(m.from_user)
    await m.answer(text, reply_markup=markup)


@r.message(Command("addproduct"))
async def add_product(m: Message):
    # /addproduct Nomi | narx | eski_narx | kalit so'zlar   (faqat admin)
    if m.from_user.id != ADMIN_ID:
        return
    try:
        name, price, old, kw = [x.strip() for x in m.text.split(" ", 1)[1].split("|")]
        run(
            "INSERT INTO products(name,emoji,price,old_price,kw) VALUES(?,?,?,?,?)",
            (name, "🛍", int(price), int(old or 0), kw.lower()),
        )
        await m.answer("✅ Mahsulot qo'shildi.")
    except Exception:
        await m.answer("Format: <code>/addproduct Nomi | narx | eski narx (0 bo'lsa aksiyasiz) | kalit so'zlar</code>")


async def _phone_saved(m: Message, phone):
    WAIT_PHONE.discard(m.from_user.id)
    run("UPDATE users SET phone=? WHERE id=?", (phone, m.from_user.id))
    await m.answer(f"✅ Raqam saqlandi: <b>{esc(phone)}</b>", reply_markup=home_kbd())
    text, markup = s_cart(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@r.message(F.contact)
async def got_contact(m: Message):
    save_user(m.from_user)
    if m.contact.user_id != m.from_user.id:  # boshqa odamning kontakti qabul qilinmaydi
        await m.answer("❌ Iltimos, o'zingizning raqamingizni yuboring (pastdagi tugma orqali).")
        return
    phone = norm_phone(m.contact.phone_number)
    if not phone:
        await m.answer("❌ Faqat O'zbekiston raqami (+998...) qabul qilinadi. Raqamni qo'lda yozing.")
        return
    await _phone_saved(m, phone)


@r.message(F.text & ~F.text.startswith("/"))
async def text_search(m: Message):
    save_user(m.from_user)
    if m.from_user.id in WAIT_PHONE:  # telefon raqami kutilmoqda
        phone = norm_phone(m.text)
        if not phone:
            await m.answer("❌ Raqam noto'g'ri. Masalan: <b>+998 90 123 45 67</b>")
            return
        await _phone_saved(m, phone)
        return
    text, markup = s_search(m.text.strip())
    await m.answer(text, reply_markup=markup)


@r.callback_query()
async def cb(c: CallbackQuery):
    save_user(c.from_user)
    uid = c.from_user.id
    parts = c.data.split(":")
    act = parts[0]

    if act == "noop":
        await c.answer()
        return

    if act == "m":
        page = parts[1]
        if page == "home":
            text, markup = s_home(c.from_user)
        elif page == "promo":
            text, markup = s_promo()
        elif page == "cats":
            text, markup = s_cats()
        elif page == "search":
            text, markup = s_search()
        elif page == "cart":
            text, markup = s_cart(uid)
        elif page == "orders":
            text, markup = s_orders(uid, parts[2] if len(parts) > 2 else "all")
        elif page == "profile":
            text, markup = s_profile(c.from_user)
        elif page == "settings":
            text, markup = s_settings(uid)
        else:
            text, markup = s_about()
        await render(c, text, markup)
        await c.answer()
        return

    if act == "q":
        text, markup = s_search(parts[1])
        await render(c, text, markup)
    elif act == "c":
        text, markup = s_cat(int(parts[1]))
        await render(c, text, markup)
    elif act == "p":
        text, markup = s_product(uid, int(parts[1]))
        await render(c, text, markup)
    elif act == "add":
        run(
            "INSERT INTO cart(user_id,product_id,qty) VALUES(?,?,1) "
            "ON CONFLICT(user_id,product_id) DO UPDATE SET qty=qty+1",
            (uid, int(parts[1])),
        )
        await c.answer("🛒 Savatga qo'shildi!")
        return
    elif act in ("inc", "dec"):
        pid = int(parts[1])
        if act == "inc":
            run("UPDATE cart SET qty=qty+1 WHERE user_id=? AND product_id=?", (uid, pid))
        else:
            run("UPDATE cart SET qty=qty-1 WHERE user_id=? AND product_id=?", (uid, pid))
            run("DELETE FROM cart WHERE user_id=? AND qty<=0", (uid,))
        text, markup = s_cart(uid)
        await render(c, text, markup)
    elif act == "clear":
        run("DELETE FROM cart WHERE user_id=?", (uid,))
        text, markup = s_cart(uid)
        await render(c, text, markup)
    elif act == "fav":
        pid = int(parts[1])
        if run("SELECT 1 FROM favorites WHERE user_id=? AND product_id=?", (uid, pid), fetch="one"):
            run("DELETE FROM favorites WHERE user_id=? AND product_id=?", (uid, pid))
        else:
            run("INSERT INTO favorites VALUES(?,?)", (uid, pid))
        text, markup = s_product(uid, pid)
        await render(c, text, markup)
    elif act == "favs":
        items = run(
            "SELECT p.* FROM favorites f JOIN products p ON p.id=f.product_id WHERE f.user_id=? AND p.active=1",
            (uid,),
            fetch="all",
        )
        text = "❤️ <b>Sevimlilar</b>" if items else "❤️ Sevimlilar ro'yxati bo'sh."
        await render(c, text, kb(product_rows(items)))
    elif act == "notif":
        run("UPDATE users SET notif=1-notif WHERE id=?", (uid,))
        text, markup = s_settings(uid)
        await render(c, text, markup)
    elif act == "checkout":
        items = cart_items(uid)
        if not items:
            await c.answer("Savat bo'sh", show_alert=True)
            return
        phone = get_phone(uid)
        if not phone:  # telefon raqamisiz buyurtma berib bo'lmaydi
            WAIT_PHONE.add(uid)
            await c.answer()
            await c.message.answer(
                "📞 Buyurtma berish uchun <b>telefon raqamingiz</b> kerak.\n\n"
                "Pastdagi tugmani bosing yoki raqamni yozing (masalan: +998 90 123 45 67).",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="📞 Raqamni yuborish", request_contact=True)]],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
            return
        total = sum(i["price"] * i["qty"] for i in items)
        oid = run(
            "INSERT INTO orders(user_id,created,status,total,phone) VALUES(?,?,?,?,?)",
            (uid, datetime.now().strftime("%d.%m.%Y, %H:%M"), "p", total, phone),
        )
        for i in items:
            run("INSERT INTO order_items VALUES(?,?,?,?)", (oid, i["name"], i["qty"], i["price"]))
        run("DELETE FROM cart WHERE user_id=?", (uid,))
        await render(
            c,
            f"✅ <b>Buyurtma #{oid} qabul qilindi!</b>\n\n💰 Jami: <b>{money(total)}</b>\n"
            "Tez orada siz bilan bog'lanamiz.",
            kb([[B(text="📦 Buyurtmalarim", callback_data="m:orders:all")]]),
        )
        if ADMIN_ID:
            lines = "\n".join(f"• {esc(i['name'])} × {i['qty']}" for i in items)
            uname = f"@{esc(c.from_user.username)}" if c.from_user.username else "—"
            admin_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        B(text="🟢 Yetkazilgan", callback_data=f"os:{oid}:d"),
                        B(text="🔴 Bekor qilish", callback_data=f"os:{oid}:c"),
                    ]
                ]
            )
            await notify_admin(
                c.bot,
                oid,
                f"🆕 <b>Yangi buyurtma #{oid}</b>\n👤 {esc(c.from_user.full_name)} {uname}\n📞 {esc(phone)}\n"
                f"{lines}\n💰 {money(total)}",
                admin_kb,
            )
    elif act == "os":
        if uid != ADMIN_ID:
            await c.answer("Ruxsat yo'q", show_alert=True)
            return
        oid, code = int(parts[1]), parts[2]
        o = run("SELECT * FROM orders WHERE id=?", (oid,), fetch="one")
        if not o or o["status"] != "p":
            await c.answer("Allaqachon o'zgartirilgan")
            return
        run("UPDATE orders SET status=? WHERE id=?", (code, oid))
        if code == "d":
            run("UPDATE users SET points=points+? WHERE id=?", (o["total"] // 100000, o["user_id"]))
        icon, name = STATUS[code]
        try:
            await c.message.edit_text(c.message.html_text + f"\n\n{icon} <b>{name}</b>")
        except TelegramBadRequest:
            pass
        user = run("SELECT notif FROM users WHERE id=?", (o["user_id"],), fetch="one")
        if user and user["notif"]:
            try:
                await c.bot.send_message(o["user_id"], f"📦 Buyurtma #{oid}: {icon} <b>{name}</b>")
            except Exception:
                pass
    await c.answer()


# ---------------------------------------------------------------- katalog API (Mini App uchun)
CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
ADDRESS_CACHE = {}
ADDRESS_CACHE_LOCK = asyncio.Lock()
ADDRESS_LAST_REQUEST = 0.0


async def api_products(request):
    out = []
    for p in run("SELECT * FROM products WHERE active=1 ORDER BY cat, id", fetch="all"):
        name, unit = (x.strip() for x in p["name"].rsplit(",", 1)) if "," in p["name"] else (p["name"], "")
        group = re.split(r"[ (,]", name, 1)[0] if name else ""
        out.append({"id": p["id"], "cat": p["cat"], "name": name, "unit": unit, "price": p["price"],
                    "old": p["old_price"], "emoji": p["emoji"], "image": f"/uploads/{p['image']}" if p["image"] else "", "group": group})
    return web.json_response(out, headers=CORS)


async def product_image(request):
    filename = request.match_info["filename"]
    if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpg|webp)", filename):
        raise web.HTTPNotFound()
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def api_addresses(request):
    global ADDRESS_LAST_REQUEST
    query = " ".join(request.query.get("q", "").split())[:80]
    if len(query) < 3:
        return web.json_response([], headers=CORS)

    key = query.casefold()
    cached = ADDRESS_CACHE.get(key)
    if cached and time.time() - cached[0] < 3600:
        return web.json_response(cached[1], headers=CORS)

    params = {
        "q": f"{query}, Samarqand, Uzbekistan",
        "format": "jsonv2",
        "addressdetails": "1",
        "countrycodes": "uz",
        "viewbox": "66.70,39.90,67.20,39.45",
        "bounded": "1",
        "limit": "6",
        "accept-language": "uz,ru,en",
    }
    async with ADDRESS_CACHE_LOCK:
        cached = ADDRESS_CACHE.get(key)
        if cached and time.time() - cached[0] < 3600:
            return web.json_response(cached[1], headers=CORS)
        delay = 1.0 - (time.monotonic() - ADDRESS_LAST_REQUEST)
        if delay > 0:
            await asyncio.sleep(delay)
        ADDRESS_LAST_REQUEST = time.monotonic()
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=8),
                headers={"User-Agent": "FreshMarketShop/1.0 (Telegram Mini App address search)"},
            ) as session:
                async with session.get("https://nominatim.openstreetmap.org/search", params=params) as response:
                    if response.status != 200:
                        logging.warning("Address search provider returned HTTP %s", response.status)
                        return web.json_response([], headers=CORS)
                    raw_results = await response.json()
        except Exception:
            logging.exception("Samarqand address search failed")
            return web.json_response([], headers=CORS)

        results, seen = [], set()
        for result in raw_results:
            if result.get("address", {}).get("country_code") != "uz":
                continue
            label = str(result.get("display_name", "")).strip()
            if label and label.casefold() not in seen:
                results.append({"label": label})
                seen.add(label.casefold())
        if len(ADDRESS_CACHE) >= 256:
            ADDRESS_CACHE.pop(next(iter(ADDRESS_CACHE)))
        ADDRESS_CACHE[key] = (time.time(), results)
        return web.json_response(results, headers=CORS)


def verify_webapp_user(init_data):
    if not init_data:
        return None
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    try:
        auth_date = int(values.get("auth_date", "0"))
        user = json.loads(values.get("user", "{}"))
        user_id = int(user.get("id", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not received_hash or not user_id or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
        return None
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(received_hash, expected) else None


async def api_order(request):
    try:
        data = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        return web.json_response({"error": "Buyurtma ma'lumotlari noto'g'ri."}, status=400, headers=CORS)
    if not isinstance(data, dict):
        return web.json_response({"error": "Buyurtma ma'lumotlari noto'g'ri."}, status=400, headers=CORS)

    raw_init_data = request.headers.get("X-Telegram-Init-Data", "")
    telegram_user = verify_webapp_user(raw_init_data)
    if raw_init_data and not telegram_user:
        return web.json_response({"error": "Telegram sessiyasi eskirgan. Web ilovani qayta oching."}, status=403, headers=CORS)

    phone = norm_phone(data.get("phone"))
    address = str(data.get("addr", "")).strip()[:200]
    payment = str(data.get("pay", ""))[:50]
    slot = str(data.get("slot", ""))[:50]
    if not phone:
        return web.json_response({"error": "Telefon raqamini +998 formatida kiriting."}, status=400, headers=CORS)
    if not address:
        return web.json_response({"error": "Yetkazib berish manzilini kiriting."}, status=400, headers=CORS)
    if payment not in ("Naqd (yetkazib berishda)", "Karta orqali"):
        return web.json_response({"error": "To'lov usulini tanlang."}, status=400, headers=CORS)

    requested = data.get("items")
    if not isinstance(requested, list) or not requested or len(requested) > 50:
        return web.json_response({"error": "Savatchada mahsulot yo'q yoki mahsulotlar soni noto'g'ri."}, status=400, headers=CORS)

    items, changed = [], False
    for item in requested:
        try:
            product_id = int(item["id"])
            quantity = int(item["q"])
        except (TypeError, ValueError, KeyError):
            return web.json_response({"error": "Mahsulot ma'lumotlari noto'g'ri."}, status=400, headers=CORS)
        if quantity < 1 or quantity > 100:
            return web.json_response({"error": "Mahsulot miqdori 1 dan 100 gacha bo'lishi kerak."}, status=400, headers=CORS)
        product = run("SELECT * FROM products WHERE id=? AND active=1", (product_id,), fetch="one")
        if not product:
            return web.json_response({"error": "Mahsulotlardan biri endi mavjud emas. Savatchani yangilang."}, status=409, headers=CORS)
        if item.get("p") is not None and str(item["p"]) != str(product["price"]):
            changed = True
        items.append({"name": product["name"], "qty": quantity, "price": product["price"]})

    total = sum(item["qty"] * item["price"] for item in items)
    customer_name = " ".join(str(data.get("name", "Web mijoz")).split())[:80] or "Web mijoz"
    user_id = None
    if telegram_user:
        user_id = int(telegram_user["id"])
        customer_name = " ".join(filter(None, (telegram_user.get("first_name", ""), telegram_user.get("last_name", ""))))[:80]
        username = str(telegram_user.get("username", ""))[:64]
        run(
            "INSERT INTO users(id,name,username,phone) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, phone=excluded.phone",
            (user_id, customer_name, username, phone),
        )

    note = f"{address} | {payment} | {slot}"
    order_id = run(
        "INSERT INTO orders(user_id,created,status,total,note,phone,customer_name) VALUES(?,?,?,?,?,?,?)",
        (user_id, datetime.now().strftime("%d.%m.%Y, %H:%M"), "p", total, note, phone, customer_name),
    )
    for item in items:
        run("INSERT INTO order_items VALUES(?,?,?,?)", (order_id, item["name"], item["qty"], item["price"]))

    lines = "\n".join(f"• {esc(item['name'])} × {item['qty']} = {money(item['qty'] * item['price'])}" for item in items)
    info = f"📞 {esc(phone)}\n📍 {esc(address)}\n💳 {esc(payment)}\n🕒 {esc(slot)}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[B(text="🟢 Yetkazilgan", callback_data=f"os:{order_id}:d"), B(text="🔴 Bekor qilish", callback_data=f"os:{order_id}:c")]]
    )
    await notify_admin(
        request.app["shop_bot"],
        order_id,
        f"🆕 <b>Yangi buyurtma #{order_id}</b>\n👤 {esc(customer_name)}\n{lines}\n{info}\n💰 {money(total)}",
        markup,
    )
    return web.json_response(
        {"ok": True, "order_id": order_id, "total": total, "changed": changed,
         "items": [{"n": item["name"], "q": item["qty"]} for item in items]},
        headers=CORS,
    )


async def start_api(shop_bot):
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app["shop_bot"] = shop_bot
    app.router.add_get("/api/products", api_products)
    app.router.add_get("/api/addresses", api_addresses)
    app.router.add_get("/uploads/{filename}", product_image)
    app.router.add_post("/api/order", api_order)
    app.router.add_get("/", lambda r: web.FileResponse(BASE / "index.html"))
    import admin_api
    admin_api.add_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
        print(f"🌐 Katalog API: http://localhost:{API_PORT}/api/products")
        print(f"🌐 Web shop: http://localhost:{API_PORT}/")
    except OSError:
        print(f"⚠️ {API_PORT}-port band — katalog API ishga tushmadi (.env: API_PORT)")
    return runner


# ---------------------------------------------------------------- main
async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylga yozing.")
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit("TOKEN NOTO'G'RI (Unauthorized). @BotFather'dan yangi tokenni .env ga yozing.")
    print(f"✅ Bot ishga tushdi: @{me.username}  (to'xtatish: Ctrl+C)")
    await bot.delete_webhook(drop_pending_updates=True)
    link = webapp_link()
    menu = MenuButtonWebApp(text="🛒 Magazin", web_app=WebAppInfo(url=link)) if link else MenuButtonDefault()
    await bot.set_chat_menu_button(menu_button=menu)
    if ADMIN_ID and link:
        await bot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=menu)
    api = await start_api(bot)
    dp = Dispatcher()
    dp.include_router(r)
    await bot.set_my_commands(
        [BotCommand(command="start", description="Botni ishga tushirish"), BotCommand(command="menu", description="Asosiy menyu")]
    )
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await api.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
