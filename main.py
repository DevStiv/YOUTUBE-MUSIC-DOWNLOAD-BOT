import os
import asyncio
import logging
from typing import Dict

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus

from config import (
    BOT_TOKEN,
    ADMIN_IDS,
    FREE_DAILY_LIMIT,
    FREE_MAX_DURATION,
    VIP_MAX_DURATION,
    VIP_PRICE_STARS_7D,
    VIP_PRICE_STARS_30D,
    FORCE_CHANNEL_ID,
    FORCE_CHANNEL_URL,
    REFERRALS_FOR_VIP,
    REFERRAL_REWARD_DAYS,
    SUPPORT_USER,
)
from database import (
    init_db,
    get_or_create_user,
    cycle_quick_mode,
    process_referral,
    set_vip,
    record_star_payment,
    can_download,
    increment_download,
    get_cached_media,
    save_cached_media,
    clear_all_cache,
    get_stats,
    get_all_user_ids,
)
from extractors import (
    get_media_metadata,
    download_media_audio,
    download_media_video,
    download_image_file,
    search_tracks_inline,
    cleanup_files,
    janitor_cleanup_loop,
)
from middlewares import RateLimitMiddleware

logging.basicConfig(level=logging.INFO)
router = Router()

# Memoria temporal para selecciones de botones
PENDING_MEDIA: Dict[str, dict] = {}


def format_duration(seconds: int) -> str:
    if not seconds:
        return "N/A"
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def quick_mode_label(mode: str) -> str:
    labels = {
        "ask": "🎛 Panel Interactivo (Audio / Video / Foto)",
        "auto_audio": "🎵 Descarga Directa MP3",
        "auto_video": "🎬 Descarga Directa Video MP4",
    }
    return labels.get(mode, labels["ask"])


def main_menu_kb(user_id: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="👤 Mi Cuenta", callback_data="nav:account"),
            InlineKeyboardButton(text="💎 Tienda VIP", callback_data="nav:vip"),
        ],
        [
            InlineKeyboardButton(text="🎁 Ganar VIP Gratis", callback_data="nav:ref"),
            InlineKeyboardButton(text="⚙️ Modo de Descarga", callback_data="nav:settings"),
        ],
        [
            InlineKeyboardButton(text="🌐 Plataformas Soportadas", callback_data="nav:platforms"),
            InlineKeyboardButton(text="🔎 Buscar en Línea", switch_inline_query_current_chat=""),
        ],
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(text="🛡️ Panel Admin SaaS", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def media_selector_kb(token: str, meta: dict) -> InlineKeyboardMarkup:
    rows = []
    if not meta.get("is_image"):
        rows.append([
            InlineKeyboardButton(text="🎵 MP3 128k", callback_data=f"dl:{token}:audio:mp3:128"),
            InlineKeyboardButton(text="💎 MP3 320k", callback_data=f"dl:{token}:audio:mp3:320"),
            InlineKeyboardButton(text="🎼 M4A VIP", callback_data=f"dl:{token}:audio:m4a:256"),
        ])
    if meta.get("has_video"):
        rows.append([
            InlineKeyboardButton(text="🎬 Video 480p", callback_data=f"dl:{token}:video:mp4:480"),
            InlineKeyboardButton(text="💎 HD 720p", callback_data=f"dl:{token}:video:mp4:720"),
            InlineKeyboardButton(text="💎 1080p", callback_data=f"dl:{token}:video:mp4:1080"),
        ])
    if meta.get("thumbnail"):
        btn_label = "🖼️ Descargar Foto Original HD" if meta.get("is_image") else "🖼️ Descargar Portada / Foto HD"
        rows.append([
            InlineKeyboardButton(text=btn_label, callback_data=f"dl:{token}:photo:jpg:hd")
        ])
    rows.append([
        InlineKeyboardButton(text="❌ Cancelar", callback_data=f"dl:{token}:cancel:0:0")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def check_force_sub(bot: Bot, user_id: int, plan: str) -> bool:
    if not FORCE_CHANNEL_ID or plan == "vip" or user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id=FORCE_CHANNEL_ID, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception:
        return True


async def safe_ui_update(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup):
    """Edita mensaje de texto o reemplaza si el mensaje previo tenía foto/media."""
    if callback.message.text:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    else:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=reply_markup)


# --- COMANDO /START Y MENÚS DE BOTONES ---

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.startswith("ref_"):
        raw = command.args.replace("ref_", "")
        if raw.isdigit():
            ref_id = int(raw)

    user, is_new = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        referred_by=ref_id
    )

    if is_new and user.get("referred_by"):
        count, rewarded = await process_referral(user["referred_by"])
        try:
            alert = f"🎉 ¡Nuevo referido registrado! Total invitados: <b>{count}</b>."
            if rewarded:
                alert += f"\n💎 <b>¡Felicidades!</b> Ganaste <b>+{REFERRAL_REWARD_DAYS} días VIP</b>."
            await message.bot.send_message(user["referred_by"], alert)
        except Exception:
            pass

    plan_badge = "💎 VIP" if user["plan"] == "vip" else "🆓 FREE"
    quota = "∞ Ilimitadas" if user["plan"] == "vip" else f"{user['daily_downloads']}/{FREE_DAILY_LIMIT}"

    text = (
        f"⚡ <b>OmniMedia SaaS — Suite de Descargas Todo-en-Uno</b>\n\n"
        f"Envía cualquier enlace o escribe el nombre de una canción/video para descargar <b>Videos MP4, Música MP3/M4A y Fotos HD</b> con botones interactivos.\n\n"
        f"• <b>Plan Actual:</b> {plan_badge}\n"
        f"• <b>Cuota Hoy:</b> {quota}\n"
        f"• <b>Modo Activo:</b> {quick_mode_label(user['quick_mode'])}\n\n"
        f"👇 <i>Usa los botones de abajo o pega un enlace directamente:</i>"
    )
    await message.answer(text, reply_markup=main_menu_kb(message.from_user.id))


@router.callback_query(F.data == "nav:home")
async def cb_home(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    plan_badge = "💎 VIP" if user["plan"] == "vip" else "🆓 FREE"
    quota = "∞ Ilimitadas" if user["plan"] == "vip" else f"{user['daily_downloads']}/{FREE_DAILY_LIMIT}"

    text = (
        f"⚡ <b>OmniMedia SaaS — Panel Principal</b>\n\n"
        f"• <b>Plan:</b> {plan_badge}\n"
        f"• <b>Cuota Hoy:</b> {quota}\n"
        f"• <b>Modo Activo:</b> {quick_mode_label(user['quick_mode'])}\n\n"
        f"<i>Pega cualquier enlace en el chat o navega con los botones:</i>"
    )
    await safe_ui_update(callback, text, main_menu_kb(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "nav:account")
async def cb_account(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    plan = "💎 VIP Activo" if user["plan"] == "vip" else "🆓 Plan Gratuito"
    expiry = f"\n• <b>Vence:</b> {user['vip_until'][:10]}" if user["vip_until"] else ""
    quota = "Ilimitadas" if user["plan"] == "vip" else f"{user['daily_downloads']}/{FREE_DAILY_LIMIT}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Mejorar a VIP", callback_data="nav:vip")],
        [InlineKeyboardButton(text="🔙 Menú Principal", callback_data="nav:home")],
    ])
    text = (
        f"👤 <b>Tu Cuenta SaaS</b>\n\n"
        f"• <b>ID de Usuario:</b> <code>{user['user_id']}</code>\n"
        f"• <b>Suscripción:</b> {plan}{expiry}\n"
        f"• <b>Descargas Hoy:</b> {quota}\n"
        f"• <b>Total Descargado:</b> {user['total_downloads']} archivos\n"
        f"• <b>Amigos Invitados:</b> {user['referrals_count']}"
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "nav:platforms")
async def cb_platforms(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Buscar Ahora", switch_inline_query_current_chat="")],
        [InlineKeyboardButton(text="🔙 Menú Principal", callback_data="nav:home")],
    ])
    text = (
        f"🌐 <b>Plataformas y Formatos Compatibles</b>\n\n"
        f"🎬 <b>Videos (MP4 480p / 720p / 1080p):</b>\n"
        f"YouTube, Shorts, TikTok (sin marca de agua), Instagram Reels, Facebook, X/Twitter, Reddit, Pinterest.\n\n"
        f"🎵 <b>Música y Audio (MP3 128k / 320k / M4A):</b>\n"
        f"YouTube Music, Spotify (Tracks), SoundCloud, Bandcamp y extracción de audio de cualquier video.\n\n"
        f"🖼️ <b>Fotos y Portadas HD:</b>\n"
        f"Miniaturas de alta resolución, portadas de álbumes y enlaces de imágenes directas."
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "nav:vip")
async def cb_vip_store(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ VIP 7 Días — {VIP_PRICE_STARS_7D} Stars", callback_data="buy_vip:7")],
        [InlineKeyboardButton(text=f"🌟 VIP 30 Días — {VIP_PRICE_STARS_30D} Stars", callback_data="buy_vip:30")],
        [InlineKeyboardButton(text="🔙 Menú Principal", callback_data="nav:home")],
    ])
    text = (
        f"💎 <b>Membresía OmniMedia VIP</b>\n\n"
        f"✅ <b>Descargas Ilimitadas</b> todos los días\n"
        f"✅ <b>Videos en HD 720p y Full HD 1080p</b>\n"
        f"✅ <b>Audio MP3 320 kbps y M4A Original</b>\n"
        f"✅ Duración extendida hasta <b>2 horas</b>\n"
        f"✅ Sin canales obligatorios y prioridad de velocidad\n\n"
        f"<i>Elige tu plan para activar al instante con Telegram Stars:</i>"
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "nav:ref")
async def cb_ref(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id)
    bot_username = (await callback.bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user['user_id']}"
    restantes = REFERRALS_FOR_VIP - (user["referrals_count"] % REFERRALS_FOR_VIP)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📲 Compartir mi Enlace con Amigos",
            url=f"https://t.me/share/url?url={ref_link}&text=Descarga%20Videos%2C%20M%C3%BAsica%20y%20Fotos%20en%20HD%20con%20este%20bot!"
        )],
        [InlineKeyboardButton(text="🔙 Menú Principal", callback_data="nav:home")],
    ])
    text = (
        f"🎁 <b>Gana VIP Gratis Invitando Amigos</b>\n\n"
        f"Recibe <b>{REFERRAL_REWARD_DAYS} días de VIP automático</b> por cada <b>{REFERRALS_FOR_VIP} amigos</b> que inicien el bot con tu enlace.\n\n"
        f"• <b>Tus invitados:</b> {user['referrals_count']}\n"
        f"• <b>Te faltan:</b> {restantes} amigos para el próximo premio\n\n"
        f"🔗 <b>Tu enlace:</b>\n<code>{ref_link}</code>"
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "nav:settings")
async def cb_settings(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Cambiar Modo de Descarga", callback_data="toggle:mode")],
        [InlineKeyboardButton(text="🔙 Menú Principal", callback_data="nav:home")],
    ])
    text = (
        f"⚙️ <b>Configuración de Comodidad</b>\n\n"
        f"• <b>Modo Actual:</b> {quick_mode_label(user['quick_mode'])}\n\n"
        f"🔹 <b>Panel Interactivo:</b> Te muestra foto, datos y botones para elegir Video, Audio o Foto en cada link.\n"
        f"🔹 <b>Descarga Directa MP3:</b> Envía el audio inmediatamente al pegar un link.\n"
        f"🔹 <b>Descarga Directa Video:</b> Envía el video MP4 inmediatamente al pegar un link."
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "toggle:mode")
async def cb_toggle_mode(callback: CallbackQuery):
    await cycle_quick_mode(callback.from_user.id)
    await cb_settings(callback)


# --- PANEL DE ADMINISTRACIÓN INTERACTIVO ---

@router.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    s = await get_stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Actualizar Métricas", callback_data="admin:home"),
            InlineKeyboardButton(text="🧹 Vaciar Caché", callback_data="admin:clear_cache"),
        ],
        [InlineKeyboardButton(text="🔙 Menú Usuario", callback_data="nav:home")],
    ])
    text = (
        f"🛡️ <b>Centro de Control Admin SaaS</b>\n\n"
        f"👥 <b>Usuarios Totales:</b> {s['total_users']}\n"
        f"💎 <b>Usuarios VIP Activos:</b> {s['vip_users']}\n"
        f"📥 <b>Descargas Entregadas:</b> {s['total_downloads']}\n"
        f"⚡ <b>Archivos en Caché:</b> {s['cached_items']}\n"
        f"⭐ <b>Ingresos Stars:</b> {s['total_stars']} XTR\n\n"
        f"<i>Comandos rápidos:</i>\n"
        f"• <code>/addvip ID DIAS</code>\n"
        f"• <code>/broadcast Mensaje</code>"
    )
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    s = await get_stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Actualizar Métricas", callback_data="admin:home"),
            InlineKeyboardButton(text="🧹 Vaciar Caché", callback_data="admin:clear_cache"),
        ],
        [InlineKeyboardButton(text="🔙 Menú Usuario", callback_data="nav:home")],
    ])
    text = (
        f"🛡️ <b>Centro de Control Admin SaaS</b>\n\n"
        f"👥 <b>Usuarios Totales:</b> {s['total_users']}\n"
        f"💎 <b>Usuarios VIP Activos:</b> {s['vip_users']}\n"
        f"📥 <b>Descargas Entregadas:</b> {s['total_downloads']}\n"
        f"⚡ <b>Archivos en Caché:</b> {s['cached_items']}\n"
        f"⭐ <b>Ingresos Stars:</b> {s['total_stars']} XTR\n\n"
        f"• Dar VIP: <code>/addvip ID DIAS</code>\n"
        f"• Anuncio: <code>/broadcast Mensaje</code>"
    )
    await safe_ui_update(callback, text, kb)
    await callback.answer("Métricas actualizadas")


@router.callback_query(F.data == "admin:clear_cache")
async def cb_admin_clear_cache(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    removed = await clear_all_cache()
    await callback.answer(f"🧹 Se eliminaron {removed} registros del caché.", show_alert=True)
    await cb_admin_home(callback)


# --- PAGOS AUTOMÁTICOS TELEGRAM STARS ---

@router.callback_query(F.data.startswith("buy_vip:"))
async def cb_buy_vip(callback: CallbackQuery):
    days = int(callback.data.split(":")[1])
    stars = VIP_PRICE_STARS_7D if days == 7 else VIP_PRICE_STARS_30D

    await callback.message.answer_invoice(
        title=f"OmniMedia VIP ({days} Días)",
        description=f"Descargas ilimitadas de Videos Full HD, Audios 320kbps y Fotos durante {days} días.",
        payload=f"vip_{days}_{callback.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=f"VIP {days} Días", amount=stars)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    parts = payment.invoice_payload.split("_")
    days, user_id = int(parts[1]), int(parts[2])

    until = await set_vip(user_id, days)
    await record_star_payment(user_id, payment.telegram_payment_charge_id, payment.total_amount, days)

    await message.answer(
        f"🎉 <b>¡Suscripción VIP Activada!</b>\n\n"
        f"• <b>Plan:</b> +{days} días VIP\n"
        f"• <b>Activo hasta:</b> {until[:10]}\n\n"
        f"Disfruta de videos en 1080p y música en 320kbps sin límites.",
        reply_markup=main_menu_kb(user_id)
    )


# --- BÚSQUEDA INLINE ---

@router.inline_query()
async def inline_search(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if len(query) < 2:
        return
    tracks = await search_tracks_inline(query, limit=6)
    results = [
        InlineQueryResultArticle(
            id=f"{t['id']}_{i}",
            title=t["title"],
            description=f"⏱ {format_duration(t['duration'])} • {t['uploader']}",
            input_message_content=InputTextMessageContent(message_text=t["url"]),
        )
        for i, t in enumerate(tracks)
    ]
    await inline_query.answer(results, cache_time=60)


# --- COMANDOS RÁPIDOS DE ADMIN ---

@router.message(Command("addvip"))
async def cmd_addvip(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Uso: <code>/addvip USER_ID DIAS</code>")
        return
    uid, days = int(parts[1]), int(parts[2])
    await get_or_create_user(uid)
    until = await set_vip(uid, days)
    await message.answer(f"✅ Usuario <code>{uid}</code> ahora es VIP hasta {until[:10]}.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    body = message.text.replace("/broadcast", "", 1).strip()
    if not body:
        await message.answer("Uso: <code>/broadcast Mensaje</code>")
        return
    uids = await get_all_user_ids()
    sent, fail = 0, 0
    status = await message.answer(f"📣 Enviando a {len(uids)} usuarios...")
    for u in uids:
        try:
            await message.bot.send_message(u, f"📢 <b>Anuncio Oficial</b>\n\n{body}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await status.edit_text(f"✅ Difusión completada.\n• Enviados: {sent}\n• Fallidos: {fail}")


# --- MOTOR DE ENTREGA MULTIMODAL (AUDIO, VIDEO Y FOTO) ---

def after_download_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 Menú Principal", callback_data="nav:home"),
            InlineKeyboardButton(text="💎 Planes VIP", callback_data="nav:vip"),
        ]
    ])


async def deliver_media(
    chat_message: Message,
    user: dict,
    meta: dict,
    media_type: str,
    codec: str,
    quality: str,
    ui_msg: Message
):
    variant_key = f"{media_type}_{codec}_{quality}"
    cached = await get_cached_media(meta["uid"], variant_key)

    caption = (
        f"✨ <b>{meta['title']}</b>\n"
        f"🌐 {meta['platform']} • {media_type.upper()} ({quality})"
    )

    if cached:
        try:
            if media_type == "audio":
                await chat_message.answer_audio(
                    audio=cached["file_id"],
                    title=cached["title"],
                    performer=cached["artist"],
                    duration=cached["duration"],
                    caption=caption,
                    reply_markup=after_download_kb(),
                )
            elif meks_inline(query, limit=6)
    results = []
    for idx, t in enumerate(tracks):
        dur = format_duration(t["duration"])
        results.append(
            InlineQueryResultArticle(
                id=f"{t['id']}_{idx}",
                title=t["title"],
                description=f"⏱ {dur} • {t['uploader']}",
                input_message_content=InputTextMessageContent(message_text=t["url"]),
            )
        )
    await inline_query.answer(results, cache_time=60)


# --- PANEL DE ADMINISTRACIÓN ---

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    s = await get_stats()
    await message.answer(
        f"📈 <b>Dashboard Administrativo SaaS</b>\n\n"
        f"• <b>Usuarios Totales:</b> {s['total_users']}\n"
        f"• <b>Suscriptores VIP:</b> {s['vip_users']}\n"
        f"• <b>Descargas Servidas:</b> {s['total_downloads']}\n"
        f"• <b>Pistas en Caché Global:</b> {s['cached_songs']}\n"
        f"• <b>Ingresos Totales:</b> ⭐ {s['total_stars']} Stars"
    )


@router.message(Command("addvip"))
async def cmd_addvip(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Uso: <code>/addvip USER_ID DIAS</code>")
        return
    uid, days = int(parts[1]), int(parts[2])
    await get_or_create_user(uid)
    until = await set_vip(uid, days)
    await message.answer(f"✅ VIP asignado a <code>{uid}</code> hasta {until[:10]}.")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    body = message.text.replace("/broadcast", "", 1).strip()
    if not body:
        await message.answer("Uso: <code>/broadcast Tu mensaje</code>")
        return
    uids = await get_all_user_ids()
    sent, fail = 0, 0
    status = await message.answer(f"📣 Enviando difusión a {len(uids)} usuarios...")
    for u in uids:
        try:
            await message.bot.send_message(u, f"📢 <b>Comunicado Oficial</b>\n\n{body}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await status.edit_text(f"✅ Broadcast finalizado.\n• Entregados: {sent}\n• Fallidos: {fail}")


# --- PROCESAMIENTO MULTI-PLATAFORMA Y SELECTOR DE FORMATO ---

async def execute_audio_delivery(
    message: Message,
    user: dict,
    meta: dict,
    codec: str,
    bitrate: str,
    status_msg: Message
):
    fmt_key = f"{codec}_{bitrate}"
    cached = await get_cached_audio(meta["uid"], fmt_key)

    if cached:
        await status_msg.edit_text("⚡ <b>Entregando desde caché global...</b>")
        await message.answer_audio(
            audio=cached["file_id"],
            title=cached["title"],
            performer=cached["artist"],
            duration=cached["duration"],
            caption=(
                f"🎧 <b>{cached['title']}</b>\n"
                f"🌐 Fuente: {meta['platform']} • {codec.upper()} {bitrate}kbps"
            ),
        )
        await increment_download(user["user_id"])
        await status_msg.delete()
        return

    await status_msg.edit_text(
        f"⏳ <b>Extrayendo audio ({codec.upper()} {bitrate}kbps)...</b>\n"
        f"🌐 <b>Plataforma:</b> {meta['platform']}\n"
        f"🎵 <i>{meta['title']}</i>"
    )

    file_path = None
    try:
        file_path = await download_media_audio(meta["webpage_url"], codec=codec, bitrate=bitrate)
        if not os.path.exists(file_path):
            raise FileNotFoundError("Archivo de audio no generado.")

        await status_msg.edit_text("📤 <b>Subiendo a Telegram...</b>")
        sent_msg = await message.answer_audio(
            audio=FSInputFile(file_path),
            title=meta["title"],
            performer=meta["artist"],
            duration=meta["duration"],
            caption=(
                f"🎧 <b>{meta['title']}</b>\n"
                f"🌐 Fuente: {meta['platform']} • {codec.upper()} {bitrate}kbps"
            ),
        )

        if sent_msg.audio:
            await save_cached_audio(
                track_uid=meta["uid"],
                fmt_quality=fmt_key,
                file_id=sent_msg.audio.file_id,
                platform=meta["platform"],
                title=meta["title"],
                artist=meta["artist"],
                duration=meta["duration"],
            )

        await increment_download(user["user_id"])
        await status_msg.delete()

    except Exception as e:
        logging.error(f"Error procesando descarga: {e}")
        await status_msg.edit_text("❌ Error al descargar o convertir el audio. Intenta con otro enlace.")
    finally:
        if file_path:
            cleanup_files(file_path)


@router.message(F.text)
async def handle_incoming_request(message: Message):
    query = message.text.strip()
    if query.startswith("/"):
        return

    user, _ = await get_or_create_user(message.from_user.id, message.from_user.username)

    # 1. Verificar Sponsor Gate (Canal Obligatorio)
    if not await check_force_sub(message.bot, user["user_id"], user["plan"]):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Unirme al Canal Oficial", url=FORCE_CHANNEL_URL)],
        ])
        await message.answer(
            "🔒 <b>Acceso Restringido</b>\n\n"
            "Para usar el servicio gratuito debes unirte primero a nuestro canal oficial. "
            "Luego vuelve a enviar tu enlace.",
            reply_markup=kb
        )
        return

    # 2. Verificar Cuota Diaria
    allowed, reason = await can_download(user)
    if not allowed:
        await message.answer(f"⛔ <b>Cuota Agotada</b>\n\n{reason}", reply_markup=main_menu_kb())
        return

    status_msg = await message.answer("🔍 <b>Analizando enlace o búsqueda...</b>")

    try:
        meta = await get_media_metadata(query)
    except Exception as e:
        logging.error(f"Error analizando {query}: {e}")
        await status_msg.edit_text(
            "❌ No se encontró contenido descargable. Asegúrate de que el enlace sea público o intenta con otro término."
        )
        return

    # 3. Verificar Límite de Duración
    max_dur = VIP_MAX_DURATION if user["plan"] == "vip" else FREE_MAX_DURATION
    if meta["duration"] > max_dur:
        await status_msg.edit_text(
            f"⚠️ Esta pista dura <b>{format_duration(meta['duration'])}</b> y excede el límite de "
            f"<b>{max_dur // 60} minutos</b> de tu plan actual.",
            reply_markup=main_menu_kb()
        )
        return

    # 4. Modo Auto-Descarga Directa
    if user["auto_download"] == 1:
        bitrate = "320" if user["plan"] == "vip" else "128"
        await execute_audio_delivery(message, user, meta, "mp3", bitrate, status_msg)
        return

    # 5. Selector Interactivo de Calidad
    token = f"{user['user_id']}_{message.message_id}"
    PENDING_TRACKS[token] = meta

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎵 MP3 128k (Free)", callback_data=f"dl:{token}:mp3:128"),
        ],
        [
            InlineKeyboardButton(text="💎 MP3 320k (VIP)", callback_data=f"dl:{token}:mp3:320"),
            InlineKeyboardButton(text="🎼 M4A Original (VIP)", callback_data=f"dl:{token}:m4a:256"),
        ]
    ])

    await status_msg.edit_text(
        f"📀 <b>Pista Detectada</b>\n\n"
        f"• <b>Título:</b> {meta['title']}\n"
        f"• <b>Artista:</b> {meta['artist']}\n"
        f"• <b>Plataforma:</b> {meta['platform']}\n"
        f"• <b>Duración:</b> {format_duration(meta['duration'])}\n\n"
        f"<i>Selecciona el formato de salida:</i>",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("dl:"))
async def cb_select_quality(callback: CallbackQuery):
    _, token, codec, bitrate = callback.data.split(":")
    user, _ = await get_or_create_user(callback.from_user.id, callback.from_user.username)

    # Bloquear formatos VIP a usuarios Free
    if (bitrate in ("320", "256") or codec == "m4a") and user["plan"] != "vip":
        await callback.answer("💎 Este formato es exclusivo para usuarios VIP.", show_alert=True)
        return

    meta = PENDING_TRACKS.pop(token, None)
    if not meta:
        await callback.answer("⚠️ Esta solicitud expiró. Envía el enlace nuevamente.", show_alert=True)
        return

    allowed, reason = await can_download(user)
    if not allowed:
        await callback.answer(reason, show_alert=True)
        return

    await callback.answer()
    await execute_audio_delivery(callback.message, user, meta, codec, bitrate, callback.message)


async def main():
    if not BOT_TOKEN:
        raise ValueError("Falta BOT_TOKEN en .env")

    await init_db()
    asyncio.create_task(janitor_cleanup_loop())

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(RateLimitMiddleware(limit_seconds=2.5))
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
                                 
