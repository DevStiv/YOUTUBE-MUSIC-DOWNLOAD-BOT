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
    process_referral,
    set_vip,
    record_star_payment,
    toggle_auto_download,
    can_download,
    increment_download,
    get_cached_audio,
    save_cached_audio,
    get_stats,
    get_all_user_ids,
)
from extractors import (
    get_media_metadata,
    download_media_audio,
    search_tracks_inline,
    cleanup_files,
    janitor_cleanup_loop,
)
from middlewares import RateLimitMiddleware

logging.basicConfig(level=logging.INFO)
router = Router()

# Almacén temporal en memoria para metadatos pendientes de selección de calidad
PENDING_TRACKS: Dict[str, dict] = {}


def format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Mi Cuenta", callback_data="menu:account"),
            InlineKeyboardButton(text="💎 Hazte VIP (Stars)", callback_data="menu:vip"),
        ],
        [
            InlineKeyboardButton(text="🎁 Invitar Amigos", callback_data="menu:ref"),
            InlineKeyboardButton(text="⚙️ Ajustes", callback_data="menu:settings"),
        ]
    ])


async def check_force_sub(bot: Bot, user_id: int, plan: str) -> bool:
    """Verifica si el usuario está suscrito al canal obligatorio (VIP exentos)."""
    if not FORCE_CHANNEL_ID or plan == "vip" or user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id=FORCE_CHANNEL_ID, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logging.warning(f"No se pudo verificar canal obligatorio: {e}")
        return True


# --- COMANDO /START Y REFERIDOS ---

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.startswith("ref_"):
        raw_ref = command.args.replace("ref_", "")
        if raw_ref.isdigit():
            ref_id = int(raw_ref)

    user, is_new = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        referred_by=ref_id
    )

    if is_new and user.get("referred_by"):
        count, rewarded = await process_referral(user["referred_by"])
        try:
            msg_ref = f"🎉 ¡Un amigo se unió con tu enlace! Llevas <b>{count}</b> invitados."
            if rewarded:
                msg_ref += f"\n💎 <b>¡Meta alcanzada!</b> Se han acreditado <b>+{REFERRAL_REWARD_DAYS} días VIP</b> a tu cuenta."
            await message.bot.send_message(user["referred_by"], msg_ref)
        except Exception:
            pass

    plan_badge = "💎 VIP" if user["plan"] == "vip" else "🆓 FREE"
    quota = "∞ Ilimitadas" if user["plan"] == "vip" else f"{user['daily_downloads']}/{FREE_DAILY_LIMIT}"

    text = (
        f"🎧 <b>OmniAudio SaaS — Extractor Universal</b>\n\n"
        f"Envíame un enlace de <b>YouTube, Spotify, SoundCloud, TikTok, Instagram, Facebook o X</b>, o escribe directamente el <b>nombre de una canción</b>.\n\n"
        f"• <b>Plan Actual:</b> {plan_badge}\n"
        f"• <b>Cuota Diaria:</b> {quota}\n\n"
        f"💡 <i>También puedes escribir</i> <code>@{(await message.bot.me()).username} canción</code> <i>en cualquier chat.</i>"
    )
    await message.answer(text, reply_markup=main_menu_kb())


# --- MENÚS INTERACTIVOS ---

@router.callback_query(F.data == "menu:account")
async def cb_account(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    plan = "💎 VIP Activo" if user["plan"] == "vip" else "🆓 Plan Gratuito"
    expiry = f"\n• <b>Expira:</b> {user['vip_until'][:10]}" if user["vip_until"] else ""
    quota = "Ilimitadas" if user["plan"] == "vip" else f"{user['daily_downloads']}/{FREE_DAILY_LIMIT}"

    text = (
        f"📊 <b>Perfil de Suscripción</b>\n\n"
        f"• <b>ID:</b> <code>{user['user_id']}</code>\n"
        f"• <b>Nivel:</b> {plan}{expiry}\n"
        f"• <b>Descargas Hoy:</b> {quota}\n"
        f"• <b>Total Histórico:</b> {user['total_downloads']}\n"
        f"• <b>Amigos Invitados:</b> {user['referrals_count']}"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:vip")
async def cb_vip_store(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ VIP 7 Días — {VIP_PRICE_STARS_7D} Stars", callback_data="buy_vip:7")],
        [InlineKeyboardButton(text=f"🌟 VIP 30 Días — {VIP_PRICE_STARS_30D} Stars", callback_data="buy_vip:30")],
        [InlineKeyboardButton(text="🔙 Volver", callback_data="menu:account")],
    ])
    text = (
        f"💎 <b>Membresía OmniAudio VIP</b>\n\n"
        f"✅ Descargas diarias <b>ilimitadas</b>\n"
        f"✅ Calidad máxima <b>MP3 320 kbps y M4A Lossless</b>\n"
        f"✅ Pistas, podcasts y sets de hasta <b>2 horas</b>\n"
        f"✅ Sin suscripción obligatoria a canales\n"
        f"✅ Prioridad máxima en servidores\n\n"
        f"<i>Activación instantánea mediante Telegram Stars:</i>"
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "menu:ref")
async def cb_referrals(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id)
    bot_username = (await callback.bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user['user_id']}"
    restantes = REFERRALS_FOR_VIP - (user["referrals_count"] % REFERRALS_FOR_VIP)

    text = (
        f"🎁 <b>Programa de Recompensas</b>\n\n"
        f"Invita a tus amigos y obtén <b>{REFERRAL_REWARD_DAYS} días de VIP GRATIS</b> cada vez que <b>{REFERRALS_FOR_VIP} usuarios</b> entren con tu enlace.\n\n"
        f"• <b>Invitados totales:</b> {user['referrals_count']}\n"
        f"• <b>Faltan para tu próximo premio:</b> {restantes} amigos\n\n"
        f"🔗 <b>Tu enlace personal:</b>\n<code>{ref_link}</code>"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery):
    user, _ = await get_or_create_user(callback.from_user.id)
    auto_status = "✅ ACTIVADO (Descarga al pegar link)" if user["auto_download"] else "❌ DESACTIVADO (Elegir calidad)"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Cambiar Modo Auto-Descarga", callback_data="toggle:auto")],
        [InlineKeyboardButton(text="🔙 Volver", callback_data="menu:account")],
    ])
    text = (
        f"⚙️ <b>Preferencias de Usuario</b>\n\n"
        f"• <b>Modo Descarga Directa:</b> {auto_status}\n\n"
        f"<i>Cuando está desactivado, podrás elegir formato (MP3 128k / 320k / M4A) antes de descargar.</i>"
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "toggle:auto")
async def cb_toggle_auto(callback: CallbackQuery):
    await toggle_auto_download(callback.from_user.id)
    await cb_settings(callback)


# --- SISTEMA DE PAGOS CON TELEGRAM STARS ---

@router.callback_query(F.data.startswith("buy_vip:"))
async def cb_buy_vip(callback: CallbackQuery):
    days = int(callback.data.split(":")[1])
    stars = VIP_PRICE_STARS_7D if days == 7 else VIP_PRICE_STARS_30D

    await callback.message.answer_invoice(
        title=f"OmniAudio VIP ({days} Días)",
        description=f"Acceso ilimitado, audio 320kbps / M4A y duración extendida durante {days} días.",
        payload=f"vip_{days}_{callback.from_user.id}",
        currency="XTR",  # Moneda oficial Telegram Stars
        prices=[LabeledPrice(label=f"Suscripción VIP {days}d", amount=stars)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    payload_parts = payment.invoice_payload.split("_")
    days = int(payload_parts[1])
    user_id = int(payload_parts[2])

    until = await set_vip(user_id, days)
    await record_star_payment(user_id, payment.telegram_payment_charge_id, payment.total_amount, days)

    await message.answer(
        f"🎉 <b>¡Pago confirmado! Bienvenido a VIP</b>\n\n"
        f"• <b>Duración:</b> {days} días\n"
        f"• <b>Válido hasta:</b> {until[:10]}\n\n"
        f"Ya puedes disfrutar de descargas ilimitadas en máxima calidad.",
        reply_markup=main_menu_kb()
    )


# --- BÚSQUEDA INLINE (@bot consulta) ---

@router.inline_query()
async def inline_music_search(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if len(query) < 2:
        return

    tracks = await search_tracks_inline(query, limit=6)
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
                                 
