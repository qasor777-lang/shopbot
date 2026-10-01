"""Do'kon boti + Admin bot + katalog API — bitta start kommandasi bilan ishlaydi."""
import asyncio
import os
from pathlib import Path

import admin_bot
import bot

BASE = Path(__file__).resolve().parent
LOCK_FILE = BASE / ".shopbot.lock"


def acquire_lock():
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
                if pid <= 0:
                    raise ValueError("invalid PID")
                os.kill(pid, 0)
            except PermissionError:
                pass
            except (OSError, ValueError):
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                continue
            print("⚠️ Bitta bot instance allaqachon ishlayapti.")
            print(f"   Agar kerak bo'lsa, avval eski jarayonni yopib, keyin qayta urinib ko'ring.")
            print(f"   Lock fayli: {LOCK_FILE}  pid={pid}")
            raise SystemExit(1)
    raise SystemExit("Bot lock faylini olishning iloji bo'lmadi.")


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


async def main():
    print("\n=== Shop + Admin bot start ===")
    print(f"SHOP BOT TOKEN: {'OK' if bot.TOKEN else 'MISSING'}")
    print(f"ADMIN BOT TOKEN: {'OK' if admin_bot.os.getenv('ADMIN_BOT_TOKEN', '').strip() else 'MISSING'}")
    print(f"ADMIN_ID: {bot.ADMIN_ID}")
    await asyncio.gather(bot.main(), admin_bot.main())


if __name__ == "__main__":
    acquire_lock()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTo'xtatildi.")
    finally:
        release_lock()
