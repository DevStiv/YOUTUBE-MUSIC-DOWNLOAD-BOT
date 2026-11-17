import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", 4))
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", 5))
FREE_MAX_DURATION = int(os.getenv("FREE_MAX_DURATION", 600))
VIP_MAX_DURATION = int(os.getenv("VIP_MAX_DURATION", 7200))

VIP_PRICE_STARS_7D = int(os.getenv("VIP_PRICE_STARS_7D", 50))
VIP_PRICE_STARS_30D = int(os.getenv("VIP_PRICE_STARS_30D", 150))

FORCE_CHANNEL_ID = os.getenv("FORCE_CHANNEL_ID", "").strip()
FORCE_CHANNEL_URL = os.getenv("FORCE_CHANNEL_URL", "").strip()
REFERRALS_FOR_VIP = int(os.getenv("REFERRALS_FOR_VIP", 3))
REFERRAL_REWARD_DAYS = int(os.getenv("REFERRAL_REWARD_DAYS", 3))
SUPPORT_USER = os.getenv("SUPPORT_USER", "@admin")

DOWNLOAD_DIR = "downloads"
DB_PATH = "omniaudio_saas.db"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
