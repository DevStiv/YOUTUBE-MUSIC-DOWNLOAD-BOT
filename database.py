import aiosqlite
from datetime import datetime, timedelta
from config import DB_PATH, FREE_DAILY_LIMIT, REFERRALS_FOR_VIP, REFERRAL_REWARD_DAYS


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                plan TEXT DEFAULT 'free',
                vip_until TEXT,
                daily_downloads INTEGER DEFAULT 0,
                last_reset TEXT,
                total_downloads INTEGER DEFAULT 0,
                referred_by INTEGER,
                referrals_count INTEGER DEFAULT 0,
                quick_mode TEXT DEFAULT 'ask',
                joined_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_cache (
                cache_key TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                platform TEXT,
                title TEXT,
                artist TEXT,
                duration INTEGER,
                created_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                charge_id TEXT UNIQUE,
                amount_stars INTEGER,
                days_granted INTEGER,
                created_at TEXT
            )
        """)
        await db.commit()


async def get_or_create_user(user_id: int, username: str = None, referred_by: int = None) -> tuple[dict, bool]:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    is_new = False

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()

        if not row:
            is_new = True
            now = datetime.utcnow().isoformat()
            valid_ref = referred_by if (referred_by and referred_by != user_id) else None
            await db.execute(
                """INSERT INTO users 
                   (user_id, username, plan, daily_downloads, last_reset, referred_by, quick_mode, joined_at)
                   VALUES (?, ?, 'free', 0, ?, ?, 'ask', ?)""",
                (user_id, username, today, valid_ref, now)
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()

        user = dict(row)

        if user["plan"] == "vip" and user["vip_until"]:
            if datetime.utcnow() > datetime.fromisoformat(user["vip_until"]):
                await db.execute(
                    "UPDATE users SET plan = 'free', vip_until = NULL WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()
                user["plan"] = "free"
                user["vip_until"] = None

        if user["last_reset"] != today:
            await db.execute(
                "UPDATE users SET daily_downloads = 0, last_reset = ? WHERE user_id = ?",
                (today, user_id)
            )
            await db.commit()
            user["daily_downloads"] = 0
            user["last_reset"] = today

        return user, is_new


async def cycle_quick_mode(user_id: int) -> str:
    """Alterna entre: ask (preguntar con botones) -> auto_audio -> auto_video."""
    modes = ["ask", "auto_audio", "auto_video"]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT quick_mode FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        current = row[0] if (row and row[0] in modes) else "ask"
        next_mode = modes[(modes.index(current) + 1) % len(modes)]
        await db.execute("UPDATE users SET quick_mode = ? WHERE user_id = ?", (next_mode, user_id))
        await db.commit()
        return next_mode


async def process_referral(referrer_id: int) -> tuple[int, bool]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (referrer_id,))
        row = await cursor.fetchone()
        if not row:
            return 0, False

        new_count = row["referrals_count"] + 1
        reward_granted = False

        if new_count % REFERRALS_FOR_VIP == 0:
            reward_granted = True
            current_until = datetime.utcnow()
            if row["plan"] == "vip" and row["vip_until"]:
                parsed = datetime.fromisoformat(row["vip_until"])
                if parsed > current_until:
                    current_until = parsed
            new_until = (current_until + timedelta(days=REFERRAL_REWARD_DAYS)).isoformat()
            await db.execute(
                "UPDATE users SET referrals_count = ?, plan = 'vip', vip_until = ? WHERE user_id = ?",
                (new_count, new_until, referrer_id)
            )
        else:
            await db.execute(
                "UPDATE users SET referrals_count = ? WHERE user_id = ?",
                (new_count, referrer_id)
            )
        await db.commit()
        return new_count, reward_granted


async def set_vip(user_id: int, days: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT plan, vip_until FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()

        base_time = datetime.utcnow()
        if row and row["plan"] == "vip" and row["vip_until"]:
            existing = datetime.fromisoformat(row["vip_until"])
            if existing > base_time:
                base_time = existing

        vip_until = (base_time + timedelta(days=days)).isoformat()
        await db.execute(
            "UPDATE users SET plan = 'vip', vip_until = ? WHERE user_id = ?",
            (vip_until, user_id)
        )
        await db.commit()
    return vip_until


async def record_star_payment(user_id: int, charge_id: str, stars: int, days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO payments (user_id, charge_id, amount_stars, days_granted, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, charge_id, stars, days, datetime.utcnow().isoformat())
        )
        await db.commit()


async def can_download(user: dict) -> tuple[bool, str]:
    if user["plan"] == "vip":
        return True, "OK"
    if user["daily_downloads"] >= FREE_DAILY_LIMIT:
        return False, f"Alcanzaste tu límite de {FREE_DAILY_LIMIT} descargas diarias en el plan FREE."
    return True, "OK"


async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE users 
               SET daily_downloads = daily_downloads + 1,
                   total_downloads = total_downloads + 1
               WHERE user_id = ?""",
            (user_id,)
        )
        await db.commit()


async def get_cached_media(uid: str, variant: str) -> dict | None:
    cache_key = f"{uid}_{variant}"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM media_cache WHERE cache_key = ?", (cache_key,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_cached_media(uid: str, variant: str, file_id: str, media_type: str, platform: str, title: str, artist: str, duration: int):
    cache_key = f"{uid}_{variant}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO media_cache 
               (cache_key, file_id, media_type, platform, title, artist, duration, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cache_key, file_id, media_type, platform, title, artist, duration, datetime.utcnow().isoformat())
        )
        await db.commit()


async def clear_all_cache() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM media_cache")
        count = (await cursor.fetchone())[0]
        await db.execute("DELETE FROM media_cache")
        await db.commit()
        return count


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        vip_users = (await (await db.execute("SELECT COUNT(*) FROM users WHERE plan = 'vip'")).fetchone())[0]
        total_dl = (await (await db.execute("SELECT SUM(total_downloads) FROM users")).fetchone())[0] or 0
        cached = (await (await db.execute("SELECT COUNT(*) FROM media_cache")).fetchone())[0]
        total_stars = (await (await db.execute("SELECT SUM(amount_stars) FROM payments")).fetchone())[0] or 0
        return {
            "total_users": total_users,
            "vip_users": vip_users,
            "total_downloads": total_dl,
            "cached_items": cached,
            "total_stars": total_stars
        }


async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cursor.fetchall()]
