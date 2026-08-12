#!/usr/bin/env python3
"""
Bot Telegram — vinculación Movistar con pool de perfiles y sistema de keys.

Cada usuario tiene contexto aislado (user_data de Telegram).
Los perfiles se bloquean exclusivamente por user_id mientras dura la vinculación.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot_store import (
    ACTIVATION_COST_MXN,
    StoreError,
    admin_generate_key,
    consume_credit,
    get_credits,
    list_keys,
    redeem_key,
    refund_credit,
)
from enroll_automation import FlowError, NetworkError, complete_enroll, start_and_send_otp
from profile_pool import (
    MAX_SUCCESSES_PER_PROFILE,
    acquire_next_profile,
    discard_profile,
    get_profile,
    profile_status,
    record_profile_success,
    release_all_for_user,
    release_profile,
    startup_report,
    user_holds_profile,
)
from profile_prepare import prepare_all

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

MENU, PHONE, OTP, REDEEM_KEY = range(4)
FLOW_TIMEOUT = 600  # 10 min inactividad

ADMIN_IDS: set[int] = set()
_owner = int(os.getenv("TELEGRAM_OWNER_ID", "0") or "0")
if _owner:
    ADMIN_IDS.add(_owner)
for part in (os.getenv("TELEGRAM_ADMIN_IDS", "") or "").split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_IDS.add(int(part))

HUBOX_USER = os.getenv("HUBOX_USER", "")
HUBOX_PASSWORD = os.getenv("HUBOX_PASSWORD", "")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("movistar-bot")


def user_id(update: Update) -> int:
    user = update.effective_user
    return user.id if user else 0


def is_admin(uid: int) -> bool:
    if not ADMIN_IDS:
        return True
    return uid in ADMIN_IDS


def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 12 and digits.startswith("52"):
        digits = digits[2:]
    if len(digits) == 10:
        return digits
    return None


def close_hubox_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.user_data.pop("hubox_client", None)
    if client is not None:
        try:
            client.session.close()
        except Exception:
            pass
    context.user_data.pop("hubox_session", None)


def cleanup_user_flow(
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    *,
    refund_if_charged: bool = False,
) -> None:
    """Libera perfil, cierra sesión Hubox y opcionalmente reembolsa. Solo afecta a `uid`."""
    profile_id = context.user_data.get("profile_id")
    if profile_id:
        release_profile(profile_id, uid)
    else:
        release_all_for_user(uid)

    close_hubox_session(context)

    if refund_if_charged and context.user_data.pop("credit_charged", False):
        refund_credit(uid, "reembolso_cancel")

    context.user_data.clear()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📱 Nueva vinculación", callback_data="act:link")],
            [InlineKeyboardButton("🔑 Canjear key", callback_data="act:redeem")],
            [InlineKeyboardButton("💳 Comprar key", callback_data="act:buy")],
        ]
    )


def format_success(result: dict) -> str:
    nombre = result.get("nombre_bd") or result.get("ocr_nombre") or "—"
    curp = result.get("curp") or "—"
    sim = result.get("similarity") or "—"
    pred = result.get("prediction") or "—"
    return (
        "🎉 *¡Vinculación exitosa!*\n\n"
        f"👤 `{nombre}`\n"
        f"🪪 CURP: `{curp}`\n"
        f"📊 Similitud: `{sim}` · `{pred}`\n\n"
        "Listo. Usa el menú para otra activación."
    )


def format_fail(msg: str, *, refunded: bool = False) -> str:
    text = f"❌ {msg}"
    if refunded:
        text += f"\n\n💰 Se reembolsó 1 activación (${ACTIVATION_COST_MXN} MXN)."
    text += "\n\nUsa el menú para reintentar."
    return text


async def reply_fail(update: Update, msg: str, *, refunded: bool = False) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(format_fail(msg, refunded=refunded))


async def show_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
    cleanup: bool = False,
    refund_on_cleanup: bool = False,
) -> int:
    uid = user_id(update)
    if cleanup:
        cleanup_user_flow(context, uid, refund_if_charged=refund_on_cleanup)
    else:
        context.user_data.clear()

    credits = get_credits(uid)
    text = (
        "📱 *Bot Vinculación Movistar*\n\n"
        f"Activaciones disponibles: *{credits}*\n"
        f"Costo por vinculación: *${ACTIVATION_COST_MXN} MXN*\n\n"
        "Elige una opción:"
    )
    kb = main_menu_keyboard()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return MENU


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    cleanup_user_flow(context, uid, refund_if_charged=True)
    return await show_menu(update, context, cleanup=False)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    cleanup_user_flow(context, uid, refund_if_charged=True)
    if update.effective_message:
        await update.effective_message.reply_text("Cancelado.")
    return await show_menu(update, context, cleanup=False)


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    cleanup_user_flow(context, uid, refund_if_charged=True)
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            "⏱ Sesión expirada por inactividad.\n"
            f"Si se había cobrado, se reembolsó la activación (${ACTIVATION_COST_MXN} MXN).",
        )
    return await show_menu(update, context, cleanup=False)


async def cmd_genkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Uso: /genkey <usos>\nEj: /genkey 5")
        return
    uses = int(args[0])
    try:
        code = admin_generate_key(uses, uid)
    except StoreError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"✅ Key generada ({uses} activación/es):\n\n`{code}`\n\n"
        "Solo se puede canjear *una vez*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    items = list_keys(15)
    if not items:
        await update.effective_message.reply_text("Sin keys.")
        return
    lines = ["*Keys recientes:*"]
    for k in items:
        st = k.get("status", "?")
        uses = k.get("uses_total", "?")
        by = k.get("redeemed_by") or "—"
        lines.append(f"`{k['code']}` · {uses} usos · {st} · user {by}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_perfiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    lines = ["*Pool* — `profiles/movistar_perfiles`"]
    for p in profile_status():
        usage = f"{p.get('successes', 0)}/{MAX_SUCCESSES_PER_PROFILE}"
        if p.get("discarded"):
            lines.append(f"🗑 `{p['id']}` {p['label']} — descartado ({usage})")
        elif p["ready"]:
            estado = "ocupado" if p["busy"] else "libre"
            holder = p.get("held_by") or "—"
            lines.append(f"✅ `{p['id']}` {p['label']} — {estado} ({usage}) user {holder}")
        else:
            errs = "; ".join(p.get("errors") or ["?"])
            lines.append(f"❌ `{p['id']}` {p['label']} — *no listo*\n   _{errs}_")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_validar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    await update.effective_message.reply_text(f"```\n{startup_report()}\n```", parse_mode=ParseMode.MARKDOWN)


def _run_profile_prep(*, notify_log: bool = True) -> None:
    try:
        report = prepare_all(
            hubox_user=HUBOX_USER or None,
            hubox_password=HUBOX_PASSWORD or None,
        )
        if notify_log:
            log.info("Preparación de perfiles:\n%s", report.summary())
    except Exception:
        log.exception("Error preparando perfiles")


async def cmd_preparar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    await update.effective_message.reply_text("⏳ Preparando perfiles desde el catálogo…")
    report = await asyncio.to_thread(
        prepare_all,
        hubox_user=HUBOX_USER or None,
        hubox_password=HUBOX_PASSWORD or None,
    )
    await update.effective_message.reply_text(f"```\n{report.summary()}\n```", parse_mode=ParseMode.MARKDOWN)


async def on_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]
    uid = query.from_user.id

    if action == "menu":
        return await show_menu(update, context, edit=True, cleanup=True, refund_on_cleanup=True)

    if action == "buy":
        await query.edit_message_text(
            f"💳 *Comprar activaciones*\n\n"
            f"Cada vinculación cuesta *${ACTIVATION_COST_MXN} MXN*.\n"
            "Contacta al admin para obtener una key de pago.\n\n"
            "Si el proceso falla por red, se reembolsa la activación automáticamente.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Menú", callback_data="act:menu")]]),
        )
        return MENU

    if action == "redeem":
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await query.edit_message_text(
            "🔑 *Canjear key*\n\nEnvía el código de activación.\n"
            "_Solo se puede canjear una vez por key._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return REDEEM_KEY

    if action == "link":
        cleanup_user_flow(context, uid, refund_if_charged=True)
        credits = get_credits(uid)
        if credits < 1:
            await query.edit_message_text(
                "⚠️ No tienes activaciones.\n\n"
                "Canjea una key o compra una activación.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=main_menu_keyboard(),
            )
            return MENU
        await query.edit_message_text(
            "📱 *Nueva vinculación*\n\n"
            "Escribe tu número celular a *10 dígitos*\n"
            "(ejemplo: `5512345678`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return PHONE

    return MENU


async def on_redeem_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    code = (update.effective_message.text or "").strip()
    try:
        uses = redeem_key(uid, code)
    except StoreError as exc:
        await update.effective_message.reply_text(f"❌ {exc}")
        return REDEEM_KEY
    balance = get_credits(uid)
    await update.effective_message.reply_text(
        f"✅ Key canjeada: +{uses} activación/es.\n"
        f"Saldo actual: *{balance}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    return await show_menu(update, context, cleanup=False)


async def on_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    phone = normalize_phone(update.effective_message.text or "")
    if not phone:
        await update.effective_message.reply_text("Número inválido. Envía 10 dígitos.")
        return PHONE

    if user_holds_profile(uid):
        await update.effective_message.reply_text(
            "Ya tienes una vinculación en curso. Termínala o usa /cancel."
        )
        return PHONE

    try:
        profile = acquire_next_profile(uid)
    except RuntimeError as exc:
        await update.effective_message.reply_text(
            f"⚠️ {exc}\n\nIntenta más tarde o contacta al admin.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard(),
        )
        return MENU

    if not consume_credit(uid, reason=f"vinculacion:{phone}"):
        release_profile(profile.id, uid)
        await update.effective_message.reply_text(
            "⚠️ Sin activaciones disponibles.",
            reply_markup=main_menu_keyboard(),
        )
        return MENU

    context.user_data["phone"] = phone
    context.user_data["profile_id"] = profile.id
    context.user_data["credit_charged"] = True
    context.user_data["flow_uid"] = uid

    await update.effective_message.reply_text(
        f"📱 Número `{phone}` recibido.\n\n⏳ Enviando OTP…",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        client, track_id = await asyncio.to_thread(start_and_send_otp, profile, phone)
        context.user_data["track_id"] = track_id
        context.user_data["hubox_client"] = client

        await update.effective_message.reply_text(
            "✅ OTP enviado por SMS.\n\n"
            "✏️ Escribe el *código de 4 dígitos*:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return OTP

    except NetworkError as exc:
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await reply_fail(update, f"Error de red: {exc}", refunded=True)
        return await show_menu(update, context, cleanup=False)

    except FlowError as exc:
        cleanup_user_flow(context, uid, refund_if_charged=exc.refundable)
        await reply_fail(update, str(exc), refunded=exc.refundable)
        return await show_menu(update, context, cleanup=False)

    except Exception as exc:
        log.exception("inicio/otp failed")
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await reply_fail(update, f"Error: {exc}", refunded=True)
        return await show_menu(update, context, cleanup=False)


async def on_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    flow_uid = context.user_data.get("flow_uid")
    if flow_uid and flow_uid != uid:
        await update.effective_message.reply_text("Esta sesión no te pertenece. Usa /start.")
        return ConversationHandler.END

    otp = re.sub(r"\D", "", update.effective_message.text or "")
    if len(otp) < 4:
        await update.effective_message.reply_text("OTP inválido. Envía 4 dígitos.")
        return OTP

    profile_id = context.user_data.get("profile_id")
    track_id = context.user_data.get("track_id")
    client = context.user_data.get("hubox_client")

    try:
        profile = get_profile(profile_id) if profile_id else None
        if not profile or not track_id or not client:
            raise FlowError("Sesión expirada. Inicia de nuevo.", refundable=True)

        if user_holds_profile(uid) != profile_id:
            raise FlowError("El perfil ya no está asignado a tu sesión.", refundable=True)

        await update.effective_chat.send_action(ChatAction.TYPING)
        await update.effective_message.reply_text("⏳ Procesando vinculación…")

        result = await asyncio.to_thread(complete_enroll, profile, client, track_id, otp)

        count = record_profile_success(profile_id)
        release_profile(profile_id, uid)
        close_hubox_session(context)
        context.user_data.clear()
        if count >= MAX_SUCCESSES_PER_PROFILE:
            log.info("Perfil %s descartado tras %s/%s éxitos", profile_id, count, MAX_SUCCESSES_PER_PROFILE)
        await update.effective_message.reply_text(format_success(result), parse_mode=ParseMode.MARKDOWN)
        return await show_menu(update, context, cleanup=False)

    except NetworkError as exc:
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await reply_fail(update, f"Error de red: {exc}", refunded=True)
        return await show_menu(update, context, cleanup=False)

    except FlowError as exc:
        if exc.retry_otp:
            await update.effective_message.reply_text(
                "❌ Código incorrecto o expirado.\nEnvía otro OTP o /cancel para salir (con reembolso)."
            )
            return OTP
        if exc.discard_profile and profile_id:
            discard_profile(profile_id, str(exc))
            log.info("Perfil %s descartado: %s", profile_id, exc)
        cleanup_user_flow(context, uid, refund_if_charged=exc.refundable)
        await reply_fail(update, str(exc), refunded=exc.refundable)
        return await show_menu(update, context, cleanup=False)

    except Exception as exc:
        log.exception("flow failed")
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await reply_fail(update, f"Error: {exc}", refunded=True)
        return await show_menu(update, context, cleanup=False)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MENU: [CallbackQueryHandler(on_menu_action, pattern=r"^act:")],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_otp)],
            REDEEM_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_redeem_key)],
            ConversationHandler.TIMEOUT: [
                CallbackQueryHandler(on_timeout),
                MessageHandler(filters.ALL, on_timeout),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(on_menu_action, pattern=r"^act:menu$"),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
        name="movistar_enroll",
        conversation_timeout=FLOW_TIMEOUT,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("perfiles", cmd_perfiles))
    app.add_handler(CommandHandler("validar", cmd_validar))
    app.add_handler(CommandHandler("preparar", cmd_preparar))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    log.info("Preparando perfiles…")
    _run_profile_prep(notify_log=True)
    log.info("Bot arrancando\n%s", startup_report())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
