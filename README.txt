SHOPBOT + ADMIN BOT

Ishga tushirish (Windows): run.bat ni ikki marta bosing. Bitta oynada ikkala bot ham ishlaydi.

.env fayl:
  BOT_TOKEN        - do'kon boti tokeni (@BotFather)
  ADMIN_BOT_TOKEN  - ADMIN uchun ALOHIDA bot tokeni (@BotFather'da yangi bot oching)
  ADMIN_ID         - sizning Telegram ID'ingiz (@userinfobot). Admin botga faqat shu ID kira oladi.
  PUBLIC_URL       - botning ochiq HTTPS manzili (pastga qarang)
  WEBAPP_URL       - bo'sh qoldirsangiz PUBLIC_URL ishlatiladi (sayt botning o'zidan beriladi)
  API_PORT         - 8080 (o'zgartirish shart emas)

Admin bot: /start -> Mahsulotlar / Qo'shish / Buyurtmalar / Statistika.
Narx, aksiya, nom, emoji, yashirish, o'chirish - hammasi do'konda va saytda darhol yangilanadi.

PUBLIC_URL qanday olinadi (kompyuteringiz ochiq tursa):
  cloudflared tunnel --url http://localhost:8080   -> https://xxxx.trycloudflare.com
  shu manzilni PUBLIC_URL= ga yozing. (Har safar yangilanadi; doimiy manzil uchun VPS yoki
  nomlangan Cloudflare tunnel kerak.)

Muhim: narxlar serverda tekshiriladi - saytdan kelgan narx hisobga olinmaydi, faqat admin qo'ygan narx.

Telefon raqam: buyurtma berishda MAJBURIY (+998 XX XXX XX XX).
  - Saytda: raqam kiritilmasa "Buyurtmani tasdiqlash" ishlamaydi.
  - Botda ("Buyurtma berish" tugmasi): raqam yo'q bo'lsa, bot kontakt tugmasini yuboradi.
  - Server ham tekshiradi. Raqam admin botda va admin panelda ko'rinadi.

Internetga joylash (Docker bilan ishlaydigan hosting):
  - Loyihani GitHub'ga yuborishdan oldin .env va shop.db yuborilmasligini tekshiring.
    .gitignore va .dockerignore bu fayllarni chiqarib tashlaydi.
  - Hostingda Dockerfile orqali ishga tushiring. Bitta nusxa/replica ishlating.
  - Doimiy diskni /data ga ulang va SHOP_DB_PATH=/data/shop.db o'zgaruvchisini kiriting.
  - Hostingda BOT_TOKEN, ADMIN_BOT_TOKEN va ADMIN_ID ni maxfiy environment variable sifatida kiriting.
  - Hosting HTTPS domen bergach, PUBLIC_URL va WEBAPP_URL ga shu https:// manzilni yozing.
  - PORT o'zgaruvchisini hosting belgilaydi. Sayt API va ikkala bot python start_all.py orqali ishlaydi.
  - Bot tokenlari chatga yuborilgan bo'lsa, BotFather'da yangilang va yangi tokenlarni faqat hosting sozlamalariga kiriting.

24/7 ISHLASHI UCHUN (noutbuk o'chiq bo'lsa ham bot to'xtamaydi):

A) Railway (eng oson yo'l):
  1. Loyihani GitHub'ga yuboring (git init, add, commit, push). .env yuborilmaydi.
  2. railway.app -> New Project -> Deploy from GitHub repo (railway.toml Dockerfile'dan build qiladi).
  3. Variables bo'limiga yozing: BOT_TOKEN, ADMIN_BOT_TOKEN, ADMIN_ID=6089586932,
     SHOP_DB_PATH=/data/shop.db, SHOP_UPLOAD_DIR=/data/uploads
  4. Volumes -> Mount path: /data (baza serverda saqlanib qolishi uchun).
  5. Settings -> Networking -> Generate Domain -> chiqqan https://... manzilni
     PUBLIC_URL va WEBAPP_URL ga yozing. PORT ni o'zi belgilaydi.

B) VPS (Ubuntu server):
  1. Serverga Docker o'rnating va loyiha fayllarini ko'chiring (scp yoki git clone).
  2. .env faylni serverga ko'chiring yoki qaytadan yarating.
  3. PUBLIC_URL ga server domeningizni yozing (masalan https://shop.sizningdomen.uz).
  4. Ishga tushirish: docker compose up -d --build
     (restart: always bor — server qayta yonsa ham bot avtomatik ishga tushadi.)
  5. Loglar: docker compose logs -f

Muhim: botni ikki joyda (server + noutbuk) bir vaqtda ishga tushirmang —
Telegram faqat bitta polling'ga ruxsat beradi va .shopbot.lock ham to'xtatadi.
