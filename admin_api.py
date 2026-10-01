"""Admin Web App API: Telegram initData orqali tekshiriladi, faqat ADMIN_ID kira oladi."""
import base64, hashlib, hmac, json, os, re, time, uuid
from urllib.parse import parse_qsl
from aiohttp import web
from bot import ADMIN_ID, CATS, BASE, UPLOAD_DIR, run

IMAGE_TYPES = {
    "image/png": ("png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP"),
}

def _token():
    return os.getenv("ADMIN_BOT_TOKEN", "").strip().strip("'\"")

def auth(request):
    raw = request.headers.get("X-Init-Data", "")
    local_hosts = ("localhost", "127.0.0.1", "::1")
    local_mode = request.url.host in local_hosts and request.remote in ("127.0.0.1", "::1")
    if not raw and local_mode:
        if not ADMIN_ID:
            raise web.HTTPForbidden()
        return
    d = dict(parse_qsl(raw, keep_blank_values=True))
    h = d.pop("hash", "")
    if not h or not _token() or not ADMIN_ID:
        raise web.HTTPForbidden()
    check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", _token().encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h):
        raise web.HTTPForbidden()
    if time.time() - int(d.get("auth_date", 0)) > 86400:
        raise web.HTTPForbidden()
    if json.loads(d.get("user", "{}")).get("id") != ADMIN_ID:
        raise web.HTTPForbidden()

def ok(data): return web.json_response(data, headers={"Cache-Control": "no-store"})

async def page(request):
    return web.FileResponse(BASE / "admin.html")

async def data(request):
    auth(request)
    prods = []
    for product in run("SELECT * FROM products ORDER BY cat, id", fetch="all"):
        item = dict(product)
        item["image"] = f"/uploads/{item['image']}" if item.get("image") else ""
        prods.append(item)
    orders = []
    for o in run("SELECT o.*, COALESCE(NULLIF(o.customer_name,''),u.name) uname, u.username FROM orders o LEFT JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 50", fetch="all"):
        d = dict(o)
        d["items"] = [dict(i) for i in run("SELECT name,qty,price FROM order_items WHERE order_id=?", (o["id"],), fetch="all")]
        orders.append(d)
    st = run("SELECT COUNT(*) n, COALESCE(SUM(total),0) s FROM orders WHERE status!='c'", fetch="one")
    return ok({"cats": [list(c) for c in CATS], "products": prods, "orders": orders,
               "stats": {"orders": st["n"], "sum": st["s"], "users": run("SELECT COUNT(*) n FROM users", fetch="one")["n"],
                         "new": run("SELECT COUNT(*) n FROM orders WHERE status='p'", fetch="one")["n"]}})

async def save_product(request):
    auth(request)
    b = await request.json()
    name, emoji = str(b.get("name", "")).strip()[:80], "📦"
    price, old, cat = int(b.get("price") or 0), int(b.get("old_price") or 0), int(b.get("cat") or 0)
    if not name or price <= 0 or not 0 <= cat < len(CATS):
        raise web.HTTPBadRequest(text="Nom, narx yoki bo'lim noto'g'ri")
    active = 1 if b.get("active", 1) else 0
    image_name = ""
    image_data = b.get("image", "")
    if image_data:
        if not isinstance(image_data, str) or len(image_data) > 2_800_000:
            raise web.HTTPBadRequest(text="Rasm 2 MB dan kichik bo'lishi kerak")
        header, separator, encoded = image_data.partition(",")
        mime = header.removeprefix("data:").removesuffix(";base64") if separator else ""
        image_type = IMAGE_TYPES.get(mime)
        if not image_type:
            raise web.HTTPBadRequest(text="Faqat PNG, JPG yoki WebP rasm yuklang")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise web.HTTPBadRequest(text="Rasm faylini o'qib bo'lmadi")
        extension, matches = image_type
        if len(image_bytes) > 2 * 1024 * 1024 or not matches(image_bytes):
            raise web.HTTPBadRequest(text="Rasm hajmi yoki formati noto'g'ri")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        image_name = f"{uuid.uuid4().hex}.{extension}"
        (UPLOAD_DIR / image_name).write_bytes(image_bytes)
    elif not b.get("id"):
        raise web.HTTPBadRequest(text="Yangi mahsulot uchun rasm yuklang")
    if b.get("id"):
        product_id = int(b["id"])
        old_image = run("SELECT image FROM products WHERE id=?", (product_id,), fetch="one")
        if image_name:
            run("UPDATE products SET name=?,emoji=?,image=?,price=?,old_price=?,cat=?,active=? WHERE id=?", (name, emoji, image_name, price, old, cat, active, product_id))
        else:
            run("UPDATE products SET name=?,emoji=?,price=?,old_price=?,cat=?,active=? WHERE id=?", (name, emoji, price, old, cat, active, product_id))
        if image_name and old_image and old_image["image"]:
            _remove_image(old_image["image"])
    else:
        run("INSERT INTO products(name,emoji,image,price,old_price,cat,active) VALUES(?,?,?,?,?,?,?)", (name, emoji, image_name, price, old, cat, active))
    return ok({"ok": 1})


def _remove_image(name):
    if re.fullmatch(r"[0-9a-f]{32}\.(png|jpg|webp)", str(name or "")):
        try:
            (UPLOAD_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass

async def delete_product(request):
    auth(request)
    product_id = int((await request.json())["id"])
    product = run("SELECT image FROM products WHERE id=?", (product_id,), fetch="one")
    run("DELETE FROM products WHERE id=?", (product_id,))
    if product:
        _remove_image(product["image"])
    return ok({"ok": 1})

async def set_status(request):
    auth(request)
    b = await request.json()
    if b.get("status") not in ("p", "d", "c"):
        raise web.HTTPBadRequest()
    run("UPDATE orders SET status=? WHERE id=?", (b["status"], int(b["id"])))
    return ok({"ok": 1})

def add_routes(app):
    app.router.add_get("/admin", page)
    app.router.add_get("/admin/api/data", data)
    app.router.add_post("/admin/api/product", save_product)
    app.router.add_post("/admin/api/delete", delete_product)
    app.router.add_post("/admin/api/order", set_status)
