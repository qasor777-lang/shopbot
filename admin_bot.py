"""FreshMarket ADMIN bot.
Do'kon boti bilan BIR XIL baza (shop.db) bilan ishlaydi: bu yerda qilingan har bir
o'zgarish (yangi mahsulot, narx, aksiya, yashirish, o'chirish) do'kon botida darhol ko'rinadi.
"""
import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, MenuButtonWebApp, WebAppInfo
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup, Message

from bot import ADMIN_ID, CATS, PUBLIC_URL, STATUS, init_db, money, esc, run
from bot import TOKEN as SHOP_TOKEN

shop_bot = None  # do'kon boti (mijozlarga xabar yuborish uchun)
abot_inst = None  # admin bot (adminga yangi buyurtma xabari yuborish uchun)

ar = Router()  # faqat admin
ar.message.filter(F.from_user.id == ADMIN_ID)
ar.callback_query.filter(F.from_user.id == ADMIN_ID)
fb = Router()  # begonalar uchun


class Edit(StatesGroup):
    value = State()


class Add(StatesGroup):
    name = State()
    price = State()
    old = State()
    emoji = State()


PROMPT = {
    "price": "💰 Yangi narxni yozing (so'mda), masalan: <code>12000</code>",
    "old": "🏷 Aksiya uchun <b>eski (chizilgan) narxni</b> yozing. Aksiyani o'chirish uchun <code>0</code> yozing.",
    "name": "✏️ Yangi nomni yozing (birligi bilan), masalan: <code>Olma (Golden), 1 kg</code>",
    "emoji": "😀 Yangi emoji yuboring, masalan: 🍎",
}


# ---------------------------------------------------------------- helpers
def btn(t, d):
    return B(text=t, callback_data=d)


def kb(rows, home=True):
    rows = list(rows)
    if home:
        rows.append([btn("🏠 Admin menyu", "a:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CANCEL = kb([], home=False)
CANCEL = InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Bekor qilish", "a:menu")]])


def num(txt):
    d = re.sub(r"\D", "", txt or "")
    return int(d) if d else None


def pct(p):
    return round((1 - p["price"] / p["old_price"]) * 100)


async def render(ev, text, markup):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
    else:
        await ev.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- screens
def s_menu():
    text = (
        "🛠 <b>FreshMarket — Admin panel</b>\n\n"
        "Mahsulot, narx va buyurtmalarni shu yerdan boshqaring.\n"
        "Qilgan o'zgarishlaringiz do'kon botida <b>darhol</b> ko'rinadi."
    )
    rows = [
        [btn("📦 Mahsulotlar", "a:cats"), btn("➕ Mahsulot qo'shish", "a:add")],
        [btn("🧾 Buyurtmalar", "a:ord:p"), btn("📊 Statistika", "a:stat")],
    ]
    return text, kb(rows, home=False)


def s_cats():
    st = {x["cat"]: x for x in run("SELECT cat, COUNT(*) n, SUM(active=0) h FROM products GROUP BY cat", fetch="all")}
    rows = []
    for k, (name, em) in enumerate(CATS):
        n = st[k]["n"] if k in st else 0
        h = (st[k]["h"] or 0) if k in st else 0
        rows.append([btn(f"{em} {name} ({n}{f', 🚫{h}' if h else ''})", f"a:cat:{k}")])
    return "📦 <b>Mahsulotlar</b>\n\nBo'limni tanlang:", kb(rows)


def s_cat(k):
    items = run("SELECT * FROM products WHERE cat=? ORDER BY id", (k,), fetch="all")
    rows = [
        [btn(f"{'🚫 ' if not p['active'] else ''}{p['emoji']} {p['name']} — {money(p['price'])}", f"a:p:{p['id']}")]
        for p in items
    ]
    rows.append([btn("➕ Shu bo'limga qo'shish", f"a:addc:{k}")])
    rows.append([btn("⬅️ Bo'limlar", "a:cats")])
    return f"{CATS[k][1]} <b>{CATS[k][0]}</b> — {len(items)} ta mahsulot\n(🚫 = yashirin)", kb(rows)


def s_prod(pid):
    p = run("SELECT * FROM products WHERE id=?", (pid,), fetch="one")
    if not p:
        return "Mahsulot topilmadi.", kb([[btn("⬅️ Bo'limlar", "a:cats")]])
    text = f"{p['emoji']} <b>{esc(p['name'])}</b>\n📂 {CATS[p['cat']][0]}\n💰 Narx: <b>{money(p['price'])}</b>\n"
    if p["old_price"]:
        text += f"🏷 Eski narx: <s>{money(p['old_price'])}</s> (aksiya -{pct(p)}%)\n"
    text += "👁 Holat: " + ("do'konda ko'rinadi ✅" if p["active"] else "yashirin 🚫")
    i = p["id"]
    rows = [
        [btn("💰 Narxni o'zgartirish", f"a:e:price:{i}"), btn("🏷 Aksiya narxi", f"a:e:old:{i}")],
        [btn("✏️ Nomi", f"a:e:name:{i}"), btn("😀 Emoji", f"a:e:emoji:{i}")],
        [btn("👁 Ko'rsatish" if not p["active"] else "🚫 Yashirish", f"a:tog:{i}"), btn("🗑 O'chirish", f"a:del:{i}")],
        [btn("⬅️ Orqaga", f"a:cat:{p['cat']}")],
    ]
    return text, kb(rows)


def s_orders(flt):
    sql = "SELECT o.*, COALESCE(NULLIF(o.customer_name,''),u.name) AS uname FROM orders o LEFT JOIN users u ON u.id=o.user_id"
    args = ()
    if flt in STATUS:
        sql += " WHERE o.status=?"
        args = (flt,)
    orders = run(sql + " ORDER BY o.id DESC LIMIT 15", args, fetch="all")
    rows = [
        [btn("🟡 Jarayonda", "a:ord:p"), btn("🟢 Yetkazilgan", "a:ord:d")],
        [btn("🔴 Bekor", "a:ord:c"), btn("Barchasi", "a:ord:all")],
    ]
    for o in orders:
        rows.append([btn(f"#{o['id']} {STATUS[o['status']][0]} {money(o['total'])} · {(o['uname'] or '')[:16]}", f"a:o:{o['id']}")])
    text = "🧾 <b>Buyurtmalar</b>" + ("" if orders else "\n\nBu bo'limda buyurtma yo'q.")
    return text, kb(rows)


def s_order(oid):
    o = run(
        "SELECT o.*, COALESCE(NULLIF(o.customer_name,''),u.name) AS uname, u.username AS uun FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?",
        (oid,),
        fetch="one",
    )
    if not o:
        return "Buyurtma topilmadi.", kb([[btn("⬅️ Buyurtmalar", "a:ord:p")]])
    items = run("SELECT * FROM order_items WHERE order_id=?", (oid,), fetch="all")
    lines = "\n".join(f"• {esc(i['name'])} × {i['qty']} = {money(i['qty'] * i['price'])}" for i in items)
    icon, name = STATUS[o["status"]]
    uname = f"@{esc(o['uun'])}" if o["uun"] else ""
    text = (
        f"🧾 <b>Buyurtma #{o['id']}</b>  {icon} {name}\n🕒 {esc(o['created'])}\n"
        f"👤 {esc(o['uname'] or '')} {uname}\n📞 {esc(o['phone'] or '—')}\n\n{lines}\n\n💰 Jami: <b>{money(o['total'])}</b>"
    )
    if o["note"]:
        text += f"\n📍 {esc(o['note'])}"
    rows = []
    if o["status"] == "p":
        rows.append([btn("🟢 Yetkazilgan", f"a:os:{oid}:d"), btn("🔴 Bekor qilish", f"a:os:{oid}:c")])
    rows.append([btn("⬅️ Buyurtmalar", "a:ord:p")])
    return text, kb(rows)


def s_stat():
    users = run("SELECT COUNT(*) n FROM users", fetch="one")["n"]
    pr = run("SELECT COUNT(*) n, SUM(active=0) h FROM products", fetch="one")
    by = {x["status"]: x for x in run("SELECT status, COUNT(*) n, SUM(total) s FROM orders GROUP BY status", fetch="all")}
    g = lambda k: by[k]["n"] if k in by else 0
    rev = (by["d"]["s"] or 0) if "d" in by else 0
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Mijozlar: <b>{users}</b>\n"
        f"📦 Mahsulotlar: <b>{pr['n']}</b> (yashirin: {pr['h'] or 0})\n\n"
        f"🟡 Jarayonda: <b>{g('p')}</b>\n🟢 Yetkazilgan: <b>{g('d')}</b>\n🔴 Bekor: <b>{g('c')}</b>\n\n"
        f"💰 Yetkazilgan buyurtmalar summasi: <b>{money(rev)}</b>"
    )
    return text, kb([])


def group_id():
    """Buyurtma xabarlari yuboriladigan guruh ID'si (/setgroup bilan o'rnatiladi)."""
    r = run("SELECT val FROM settings WHERE key='group_id'", fetch="one")
    try:
        return int(r["val"]) if r else 0
    except (TypeError, ValueError):
        return 0


async def notify_new_order(oid):
    """Yangi buyurtma adminga va (o'rnatilgan bo'lsa) guruhga keladi (to'liq ma'lumot + tugmalar)."""
    if not abot_inst:
        return False
    targets = [t for t in (ADMIN_ID, group_id()) if t]
    if not targets:
        return False
    text, markup = s_order(oid)
    for t in targets:
        try:
            await abot_inst.send_message(t, "🆕 " + text, reply_markup=markup)
        except Exception:
            logging.exception("buyurtma xabari yuborilmadi: %s", t)
    return True


# ---------------------------------------------------------------- handlers
@ar.message(CommandStart())
@ar.message(Command("menu"))
@ar.message(Command("cancel"))
async def menu_cmd(m: Message, state: FSMContext):
    await state.clear()
    await render(m, *s_menu())


@ar.message(Edit.value)
async def edit_value(m: Message, state: FSMContext):
    d = await state.get_data()
    pid, field = d.get("pid"), d.get("field")
    p = run("SELECT * FROM products WHERE id=?", (pid,), fetch="one")
    t = (m.text or "").strip()
    if not p:
        await state.clear()
        await m.answer("Mahsulot topilmadi.")
        return
    note = ""
    if field == "price":
        n = num(t)
        if not n:
            await m.answer("❌ Raqam yozing, masalan: <code>12000</code>", reply_markup=CANCEL)
            return
        if p["old_price"] and p["old_price"] <= n:
            run("UPDATE products SET old_price=0 WHERE id=?", (pid,))
            note = "\nℹ️ Yangi narx eski narxdan past emas, shuning uchun aksiya o'chirildi."
        run("UPDATE products SET price=? WHERE id=?", (n, pid))
    elif field == "old":
        n = 0 if t in ("-", "0") else num(t)
        if n is None or (n and n <= p["price"]):
            await m.answer(
                f"❌ Eski narx joriy narxdan ({money(p['price'])}) katta bo'lishi kerak. Yoki aksiyani o'chirish uchun 0 yozing.",
                reply_markup=CANCEL,
            )
            return
        run("UPDATE products SET old_price=? WHERE id=?", (n, pid))
    elif field == "name":
        if not 2 <= len(t) <= 60:
            await m.answer("❌ Nom 2–60 belgi bo'lsin. Qayta yozing:", reply_markup=CANCEL)
            return
        run("UPDATE products SET name=?, kw=? WHERE id=?", (t, t.lower(), pid))
    elif field == "emoji":
        if not t or len(t) > 8:
            await m.answer("❌ Bitta emoji yuboring, masalan: 🍎", reply_markup=CANCEL)
            return
        run("UPDATE products SET emoji=? WHERE id=?", (t, pid))
    await state.clear()
    text, markup = s_prod(pid)
    await m.answer("✅ Saqlandi — do'kon botida yangilandi." + note + "\n\n" + text, reply_markup=markup)


@ar.message(Add.name)
async def add_name(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not 2 <= len(t) <= 60:
        await m.answer("❌ Nom 2–60 belgi bo'lsin. Qayta yozing:", reply_markup=CANCEL)
        return
    await state.update_data(name=t)
    await state.set_state(Add.price)
    await m.answer("💰 Narxini yozing (so'mda), masalan: <code>12000</code>", reply_markup=CANCEL)


@ar.message(Add.price)
async def add_price(m: Message, state: FSMContext):
    n = num(m.text)
    if not n:
        await m.answer("❌ Raqam yozing, masalan: <code>12000</code>", reply_markup=CANCEL)
        return
    await state.update_data(price=n)
    await state.set_state(Add.old)
    await m.answer(
        "🏷 Aksiya bo'lsa <b>eski narxni</b> yozing (u yangi narxdan katta bo'lsin).\nAksiya yo'q bo'lsa <code>0</code> yozing.",
        reply_markup=CANCEL,
    )


@ar.message(Add.old)
async def add_old(m: Message, state: FSMContext):
    d = await state.get_data()
    t = (m.text or "").strip()
    n = 0 if t in ("-", "0") else num(t)
    if n is None or (n and n <= d["price"]):
        await m.answer("❌ Eski narx yangi narxdan katta bo'lsin yoki 0 yozing.", reply_markup=CANCEL)
        return
    await state.update_data(old=n)
    await state.set_state(Add.emoji)
    await m.answer("😀 Emoji yuboring (masalan 🍎) yoki standart uchun <code>-</code> yozing.", reply_markup=CANCEL)


@ar.message(Add.emoji)
async def add_emoji(m: Message, state: FSMContext):
    d = await state.get_data()
    t = (m.text or "").strip()
    em = "🛍" if t in ("", "-") else t[:8]
    pid = run(
        "INSERT INTO products(name,emoji,price,old_price,kw,cat,active) VALUES(?,?,?,?,?,?,1)",
        (d["name"], em, d["price"], d["old"], d["name"].lower(), d["cat"]),
    )
    await state.clear()
    text, markup = s_prod(pid)
    await m.answer("✅ Mahsulot qo'shildi — do'kon botida ko'rinadi.\n\n" + text, reply_markup=markup)


@ar.callback_query(F.data.startswith("a:"))
async def cb(c: CallbackQuery, state: FSMContext):
    p = c.data.split(":")
    act = p[1]
    if act == "menu":
        await state.clear()
        await render(c, *s_menu())
    elif act == "cats":
        await state.clear()
        await render(c, *s_cats())
    elif act == "cat":
        await state.clear()
        await render(c, *s_cat(int(p[2])))
    elif act == "p":
        await state.clear()
        await render(c, *s_prod(int(p[2])))
    elif act == "e":
        await state.set_state(Edit.value)
        await state.update_data(pid=int(p[3]), field=p[2])
        await c.message.answer(PROMPT[p[2]], reply_markup=CANCEL)
    elif act == "tog":
        run("UPDATE products SET active=1-active WHERE id=?", (int(p[2]),))
        await render(c, *s_prod(int(p[2])))
    elif act == "del":
        i = int(p[2])
        await render(
            c,
            "🗑 Bu mahsulot <b>butunlay o'chiriladi</b>. Rostdan ham o'chirilsinmi?\n(Vaqtincha olib qo'ymoqchi bo'lsangiz, \"Yashirish\" ni tanlang.)",
            kb([[btn("✅ Ha, o'chirilsin", f"a:delok:{i}"), btn("✖️ Yo'q", f"a:p:{i}")]], home=False),
        )
    elif act == "delok":
        i = int(p[2])
        row = run("SELECT cat FROM products WHERE id=?", (i,), fetch="one")
        run("DELETE FROM products WHERE id=?", (i,))
        run("DELETE FROM cart WHERE product_id=?", (i,))
        run("DELETE FROM favorites WHERE product_id=?", (i,))
        await render(c, *s_cat(row["cat"] if row else 0))
    elif act == "add":
        rows = [[btn(f"{em} {name}", f"a:addc:{k}")] for k, (name, em) in enumerate(CATS)]
        await render(c, "➕ <b>Yangi mahsulot</b>\n\nQaysi bo'limga qo'shamiz?", kb(rows))
    elif act == "addc":
        await state.set_state(Add.name)
        await state.update_data(cat=int(p[2]))
        await c.message.answer(
            f"{CATS[int(p[2])][1]} <b>{CATS[int(p[2])][0]}</b> bo'limiga qo'shamiz.\n\n"
            "✏️ Mahsulot nomini birligi bilan yozing, masalan: <code>Olma (Golden), 1 kg</code>",
            reply_markup=CANCEL,
        )
    elif act == "ord":
        await render(c, *s_orders(p[2]))
    elif act == "o":
        await render(c, *s_order(int(p[2])))
    elif act == "os":
        oid, code = int(p[2]), p[3]
        o = run("SELECT * FROM orders WHERE id=?", (oid,), fetch="one")
        if not o or o["status"] != "p" or code not in ("d", "c"):
            await c.answer("Bu buyurtma allaqachon o'zgartirilgan", show_alert=True)
            return
        run("UPDATE orders SET status=? WHERE id=?", (code, oid))
        if code == "d":
            run("UPDATE users SET points=points+? WHERE id=?", (o["total"] // 100000, o["user_id"]))
        user = run("SELECT notif FROM users WHERE id=?", (o["user_id"],), fetch="one")
        if shop_bot and user and user["notif"]:
            icon, name = STATUS[code]
            try:
                await shop_bot.send_message(o["user_id"], f"📦 Buyurtma #{oid}: {icon} <b>{name}</b>")
            except Exception:
                logging.exception("mijozga xabar yuborilmadi")
        await render(c, *s_order(oid))
    elif act == "stat":
        await render(c, *s_stat())
    await c.answer()


@ar.message(Command("setgroup"), F.chat.type.in_({"group", "supergroup"}))
async def set_group(m: Message):
    run("INSERT INTO settings(key,val) VALUES('group_id',?) "
        "ON CONFLICT(key) DO UPDATE SET val=excluded.val", (str(m.chat.id),))
    await m.answer("✅ Yangi buyurtma xabarlari endi shu guruhga yuboriladi.")


@fb.message()
async def deny_m(m: Message):
    if not ADMIN_ID:
        await m.answer(
            f"👋 Sizning Telegram ID'ingiz: <code>{m.from_user.id}</code>\n\n"
            "Shuni <b>.env</b> faylidagi <code>ADMIN_ID=</code> ga yozing va botlarni qayta ishga tushiring."
        )
    else:
        await m.answer("⛔ Bu bot faqat administrator uchun.")


@fb.callback_query()
async def deny_c(c: CallbackQuery):
    await c.answer("⛔ Ruxsat yo'q", show_alert=True)


# ---------------------------------------------------------------- main
async def main():
    global shop_bot, abot_inst
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("ADMIN_BOT_TOKEN", "").strip().strip("'\"")
    if not token:
        print("ADMIN_BOT_TOKEN topilmadi (.env) — admin bot ishga tushmadi.")
        return
    init_db()
    abot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await abot.get_me()
    except TelegramUnauthorizedError:
        await abot.session.close()
        print("ADMIN BOT TOKENI NOTO'G'RI (Unauthorized). @BotFather'dan tekshiring.")
        return
    abot_inst = abot
    shop_bot = Bot(SHOP_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if SHOP_TOKEN else None
    print(f"✅ Admin bot ishga tushdi: @{me.username}")
    if not ADMIN_ID:
        print("⚠️  ADMIN_ID yozilmagan: admin botga /start yozing, u sizning ID'ingizni aytadi, uni .env ga yozing.")
    await abot.delete_webhook(drop_pending_updates=True)
    await abot.set_my_commands([BotCommand(command="menu", description="Admin menyu"), BotCommand(command="cancel", description="Bekor qilish")])
    pub = PUBLIC_URL
    if pub and ADMIN_ID:
        try:
            await abot.set_chat_menu_button(chat_id=ADMIN_ID, menu_button=MenuButtonWebApp(text="🛠 Admin panel", web_app=WebAppInfo(url=pub + "/admin")))
        except Exception as e:
            print("Menu tugmasi o'rnatilmadi:", e)
    dp = Dispatcher()
    dp.include_router(ar)
    dp.include_router(fb)
    try:
        await dp.start_polling(abot, handle_signals=False)
    finally:
        await abot.session.close()
        if shop_bot:
            await shop_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
