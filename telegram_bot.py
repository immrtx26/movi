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
    add_credits,
    admin_generate_key,
    ban_user,
    consume_credit,
    get_credits,
    get_user_info,
    is_banned,
    list_keys,
    list_users,
    redeem_key,
    refund_credit,
    set_credits,
    unban_user,
)
from enroll_automation import FlowError, NetworkError, complete_enroll, start_and_send_otp
from profile_pool import (
    prepare_pool,
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
from profile_upload import (
    MAX_PROFILES_PER_USER,
    cleanup_upload_dir,
    commit_validated_profile,
    count_user_profiles,
    delete_user_profile,
    list_user_profiles,
    maybe_purge_exhausted_profile,
    user_can_add_profile,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

MENU, PHONE, OTP, REDEEM_KEY, ADD_FRONT, ADD_BACK, ADD_SELFIE = range(7)
FLOW_TIMEOUT = 600  # 10 min inactividad
UPLOAD_TMP = ROOT / "profiles" / "_upload_tmp"

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


def ensure_not_banned(uid: int) -> str | None:
    """Devuelve mensaje de error si está baneado, si no None."""
    if is_banned(uid):
        info = get_user_info(uid)
        reason = info.get("ban_reason") or "bloqueado por admin"
        return f"🚫 Tu cuenta está bloqueada.\nMotivo: {reason}\nContacta al administrador."
    return None


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
    cleanup_upload_dir(context.user_data.get("upload_dir"))

    if refund_if_charged and context.user_data.pop("credit_charged", False):
        refund_credit(uid, "reembolso_cancel")

    context.user_data.clear()


def _upload_staging_dir(uid: int) -> Path:
    path = UPLOAD_TMP / str(uid)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- PATCH: _save_user_image con preferencia por document y aviso de compresión ---
async def _save_user_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    dest: Path,
) -> Path | None:
    """Guarda foto o documento de imagen enviada por el usuario.

    IMPORTANTE: Preferimos 'document' (sin compresión de Telegram).
    Si llega como 'photo' (comprimida), se guarda pero se marca advertencia
    porque Hubox puede rechazarla por isFake alto.
    """
    msg = update.effective_message
    if not msg:
        return None

    file_id = None
    is_compressed = False

    # 1. Preferir documento (sin compresión)
    if msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
    # 2. Foto comprimida por Telegram: usar la de mayor resolución disponible
    elif msg.photo:
        file_id = msg.photo[-1].file_id
        is_compressed = True

    if not file_id:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await context.bot.get_file(file_id)
    await tg_file.download_to_drive(custom_path=str(dest))

    if is_compressed:
        log.warning(
            "Usuario %s envió foto comprimida (%s). Hubox podría rechazarla (isFake). "
            "Recomendar 'Enviar como archivo'.",
            user_id(update),
            dest.name,
        )
        context.user_data["compressed_upload_warning"] = True

    return dest
# --- FIN PATCH ---


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Agregar perfiles", callback_data="act:add_profile")],
            [InlineKeyboardButton("🗂 Mis perfiles", callback_data="act:my_profiles")],
            [InlineKeyboardButton("📱 Nueva vinculación", callback_data="act:link")],
            [InlineKeyboardButton("🔑 Canjear key", callback_data="act:redeem")],
            [InlineKeyboardButton("💳 Comprar key", callback_data="act:buy")],
        ]
    )


def after_upload_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    can, _ = user_can_add_profile(uid)
    if can:
        rows.append([InlineKeyboardButton("➕ Agregar otro", callback_data="act:add_profile")])
    rows.append([InlineKeyboardButton("🗂 Mis perfiles", callback_data="act:my_profiles")])
    rows.append([InlineKeyboardButton("« Menú", callback_data="act:menu")])
    return InlineKeyboardMarkup(rows)


def my_profiles_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in list_user_profiles(uid):
        short = (p["label"] or p["id"])[:18]
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗑 {p['id']} {short} ({p['successes']}/{p['max_successes']})",
                    callback_data=f"delask:{p['id']}",
                )
            ]
        )
    can, _ = user_can_add_profile(uid)
    if can:
        rows.append([InlineKeyboardButton("➕ Agregar perfil", callback_data="act:add_profile")])
    rows.append([InlineKeyboardButton("« Menú", callback_data="act:menu")])
    return InlineKeyboardMarkup(rows)


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
    mine = count_user_profiles(uid)
    text = (
        "📱 *Bot Vinculación Movistar*\n\n"
        f"Activaciones disponibles: *{credits}*\n"
        f"Costo por vinculación: *${ACTIVATION_COST_MXN} MXN*\n"
        f"Tus perfiles: *{mine}/{MAX_PROFILES_PER_USER}* "
        f"(máx {MAX_SUCCESSES_PER_PROFILE} usos c/u)\n\n"
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



async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: lista usuarios conocidos (créditos / ban)."""
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    items = list_users(40)
    if not items:
        await update.effective_message.reply_text("Sin usuarios registrados aún.")
        return
    lines = ["*Usuarios* (máx 40):"]
    for u in items:
        flag = "🚫" if u.get("banned") else "✅"
        reason = u.get("ban_reason") or ""
        extra = f" — _{reason}_" if u.get("banned") and reason else ""
        lines.append(
            f"{flag} `{u['user_id']}` · créditos *{u['credits']}*{extra}"
        )
    lines.append(
        "\nComandos:\n"
        "`/creditos <id> +N` o `-N` o `=N`\n"
        "`/ban <id> [motivo]`\n"
        "`/unban <id>`\n"
        "`/user <id>`"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: detalle de un usuario."""
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Uso: `/user <telegram_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    target = int(args[0])
    info = get_user_info(target)
    lines = [
        f"*Usuario* `{info['user_id']}`",
        f"Créditos: *{info['credits']}*",
        f"Ban: *{'sí' if info['banned'] else 'no'}*",
    ]
    if info.get("ban_reason"):
        lines.append(f"Motivo ban: {info['ban_reason']}")
    hist = info.get("history") or []
    if hist:
        lines.append("\n*Últimos movimientos:*")
        for h in reversed(hist[-10:]):
            lines.append(
                f"`{h.get('ts','')}` {h.get('delta',0):+d} · {h.get('reason','')} · bal {h.get('balance','')}"
            )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_creditos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /creditos <id> +5 | -2 | =10"""
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.effective_message.reply_text(
            "Uso:\n"
            "`/creditos <id> +5`  sumar\n"
            "`/creditos <id> -2`  restar\n"
            "`/creditos <id> =10` fijar saldo",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    target = int(args[0])
    raw = (args[1] or "").strip().replace(" ", "")
    try:
        if raw.startswith("="):
            amount = int(raw[1:] or "0")
            bal = set_credits(target, amount, reason=f"admin_set_by:{uid}")
            await update.effective_message.reply_text(
                f"✅ Usuario `{target}` saldo fijado a *{bal}* créditos.",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif raw.startswith("+") or raw.startswith("-"):
            delta = int(raw)
            if delta == 0:
                await update.effective_message.reply_text("Delta 0, sin cambios.")
                return
            bal = add_credits(target, delta, reason=f"admin_adjust_by:{uid}")
            await update.effective_message.reply_text(
                f"✅ Usuario `{target}` {delta:+d} → saldo *{bal}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            delta = int(raw)
            bal = add_credits(target, delta, reason=f"admin_adjust_by:{uid}")
            await update.effective_message.reply_text(
                f"✅ Usuario `{target}` {delta:+d} → saldo *{bal}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
    except ValueError:
        await update.effective_message.reply_text("Cantidad inválida. Ej: `+5`, `-2`, `=10`.", parse_mode=ParseMode.MARKDOWN)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /ban <id> [motivo]"""
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text(
            "Uso: `/ban <telegram_id> [motivo]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    target = int(args[0])
    if target == uid:
        await update.effective_message.reply_text("No puedes banearte a ti mismo.")
        return
    if target in ADMIN_IDS:
        await update.effective_message.reply_text("No se puede banear a un admin.")
        return
    reason = " ".join(args[1:]).strip() or "ban_admin"
    ban_user(target, reason=reason, banned_by=uid)
    await update.effective_message.reply_text(
        f"🚫 Usuario `{target}` baneado.\nMotivo: {reason}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /unban <id>"""
    uid = user_id(update)
    if not is_admin(uid):
        await update.effective_message.reply_text("⛔ Solo admin.")
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text(
            "Uso: `/unban <telegram_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    target = int(args[0])
    unban_user(target, reason=f"unban_by:{uid}")
    await update.effective_message.reply_text(
        f"✅ Usuario `{target}` desbloqueado.",
        parse_mode=ParseMode.MARKDOWN,
    )


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
        banned_msg = ensure_not_banned(uid)
        if banned_msg:
            await query.edit_message_text(banned_msg, reply_markup=main_menu_keyboard())
            return MENU
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

    if action == "add_profile":
        cleanup_user_flow(context, uid, refund_if_charged=True)
        can, detail = user_can_add_profile(uid)
        if not can:
            await query.edit_message_text(
                f"⚠️ {detail}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🗂 Mis perfiles", callback_data="act:my_profiles")],
                        [InlineKeyboardButton("« Menú", callback_data="act:menu")],
                    ]
                ),
            )
            return MENU
        staging = _upload_staging_dir(uid)
        cleanup_upload_dir(staging)
        staging = _upload_staging_dir(uid)
        context.user_data["upload_dir"] = staging
        context.user_data["flow_uid"] = uid
        context.user_data["add_step"] = "frente"
        await query.edit_message_text(
            "➕ *Agregar perfil*\n\n"
            f"Cupo: *{detail}*\n\n"
            "Envía la foto del *frente* (anverso) de la INE.\n\n"
            "⚠️ *Importante:* envíala como *ARCHIVO* (clip → Archivo), no como foto. "
            "Así evitamos que Telegram la comprima y Hubox no la rechace por isFake.\n\n"
            "_Orden: frente → reverso → selfie._\n"
            "Puedes agregar varios (máx 10). Solo entran al pool si pasan validación.\n"
            f"Cada perfil se borra solo al llegar a {MAX_SUCCESSES_PER_PROFILE} activaciones "
            "o cuando tú lo elimines.\n\n"
            "Usa /cancel para salir.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_FRONT

    if action == "my_profiles":
        return await show_my_profiles(update, context, edit=True)

    return MENU


async def show_my_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> int:
    uid = user_id(update)
    items = list_user_profiles(uid)
    if not items:
        text = (
            "🗂 *Mis perfiles*\n\n"
            "No tienes perfiles cargados.\n"
            f"Puedes agregar hasta *{MAX_PROFILES_PER_USER}*."
        )
    else:
        lines = [
            "🗂 *Mis perfiles*\n",
            f"Total: *{len(items)}/{MAX_PROFILES_PER_USER}*\n"
            f"Toque 🗑 para borrar. Se eliminan solos tras "
            f"{MAX_SUCCESSES_PER_PROFILE} activaciones.\n",
        ]
        for p in items:
            st = "agotado" if p["discarded"] else f"{p['successes']}/{p['max_successes']} usos"
            lines.append(f"• `{p['id']}` — {p['label'][:40]} ({st})")
        text = "\n".join(lines)
    kb = my_profiles_keyboard(uid)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
        )
    return MENU


async def on_delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data or ""

    if data.startswith("delask:"):
        pid = data.split(":", 1)[1]
        await query.edit_message_text(
            f"¿Borrar el perfil `{pid}`?\nEsta acción no se puede deshacer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Sí, borrar", callback_data=f"delok:{pid}"),
                        InlineKeyboardButton("❌ No", callback_data="act:my_profiles"),
                    ]
                ]
            ),
        )
        return MENU

    if data.startswith("delok:"):
        pid = data.split(":", 1)[1]
        ok, msg = await asyncio.to_thread(delete_user_profile, uid, pid)
        prefix = "✅ " if ok else "❌ "
        await query.edit_message_text(
            prefix + msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🗂 Mis perfiles", callback_data="act:my_profiles")],
                    [InlineKeyboardButton("« Menú", callback_data="act:menu")],
                ]
            ),
        )
        return MENU

    return MENU


async def on_add_expect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recuerda enviar imagen durante el flujo de alta."""
    state = context.user_data.get("add_step") or "frente"
    await update.effective_message.reply_text(
        f"Necesito una *imagen* del {state}. Envía una foto (no texto).",
        parse_mode=ParseMode.MARKDOWN,
    )
    step = context.user_data.get("add_step")
    if step == "reverso":
        return ADD_BACK
    if step == "selfie":
        return ADD_SELFIE
    return ADD_FRONT


async def on_add_front(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    staging = context.user_data.get("upload_dir") or _upload_staging_dir(uid)
    context.user_data["upload_dir"] = staging
    dest = staging / "front.jpg"
    saved = await _save_user_image(update, context, dest)
    if not saved:
        await update.effective_message.reply_text(
            "Envía una *foto* del frente de la INE (imagen).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_FRONT
    context.user_data["upload_front"] = str(dest)
    context.user_data["add_step"] = "reverso"
    await update.effective_message.reply_text(
        "✅ Frente recibido.\n\nAhora envía la foto del *reverso* de la INE "
        "(debe verse nítido, con los 2 códigos QR).\n\n"
        "Recuerda: *envíala como archivo* para evitar compresión.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_BACK


async def on_add_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    staging = context.user_data.get("upload_dir") or _upload_staging_dir(uid)
    dest = staging / "back.jpg"
    saved = await _save_user_image(update, context, dest)
    if not saved:
        await update.effective_message.reply_text(
            "Envía una *foto* del reverso de la INE (imagen).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_BACK
    context.user_data["upload_back"] = str(dest)
    context.user_data["add_step"] = "selfie"
    await update.effective_message.reply_text(
        "✅ Reverso recibido.\n\nAhora envía la *selfie* (cara frontal, buena luz).",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_SELFIE


async def on_add_selfie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = user_id(update)
    staging = context.user_data.get("upload_dir") or _upload_staging_dir(uid)
    dest = staging / "selfie.jpg"
    saved = await _save_user_image(update, context, dest)
    if not saved:
        await update.effective_message.reply_text(
            "Envía una *foto* selfie (imagen).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_SELFIE

    front = Path(context.user_data.get("upload_front") or staging / "front.jpg")
    back = Path(context.user_data.get("upload_back") or staging / "back.jpg")
    if not front.is_file() or not back.is_file():
        cleanup_upload_dir(staging)
        context.user_data.clear()
        await update.effective_message.reply_text(
            "❌ Faltan fotos del proceso. Empieza de nuevo desde el menú."
        )
        return await show_menu(update, context, cleanup=False)

    # --- PATCH: aviso si alguna imagen llegó comprimida ---
    if context.user_data.pop("compressed_upload_warning", False):
        await update.effective_message.reply_text(
            "⚠️ *Advertencia de calidad*\n\n"
            "Alguna de las imágenes se envió como *foto* (comprimida por Telegram). "
            "Hubox podría rechazarla por `isFake` alto.\n\n"
            "**Recomendación para futuras subidas:**\n"
            "1. Abre Telegram → clip 📎 → *Archivo*\n"
            "2. Selecciona la foto desde tu galería\n"
            "3. Así se envía sin comprimir y con metadatos intactos.\n\n"
            "El perfil se validará igual, pero si Hubox lo rechaza, reintenta con este método.",
            parse_mode=ParseMode.MARKDOWN,
        )
    # --- FIN PATCH ---

    await update.effective_message.reply_text(
        "⏳ Validando perfil y preparando far/close face (480×640)…"
    )
    await update.effective_chat.send_action(ChatAction.TYPING)

    result = await asyncio.to_thread(
        commit_validated_profile,
        front,
        back,
        dest,
        label=f"User {uid}",
        uploaded_by=uid,
    )
    cleanup_upload_dir(staging)
    context.user_data.clear()

    if not result.ok:
        detail = "\n".join(f"• {e}" for e in (result.errors or ["validación fallida"]))
        await update.effective_message.reply_text(
            "❌ *Perfil no apto — no se agregó al pool.*\n\n"
            f"{detail}\n\n"
            "Corrige las fotos e intenta de nuevo con *Agregar perfiles*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=after_upload_keyboard(uid),
        )
        return MENU

    await asyncio.to_thread(_run_profile_prep, notify_log=False)
    prepare_pool()

    total = count_user_profiles(uid)
    await update.effective_message.reply_text(
        "✅ *Perfil agregado y listo*\n\n"
        f"ID: `{result.profile_id}`\n"
        f"Label: {result.label}\n"
        f"Tus perfiles: *{total}/{MAX_PROFILES_PER_USER}*\n"
        f"Usos máximos: {MAX_SUCCESSES_PER_PROFILE} (luego se elimina solo).\n"
        "frente + reverso (2 QR) + selfie + far/close 480×640.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=after_upload_keyboard(uid),
    )
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

    banned_msg = ensure_not_banned(uid)
    if banned_msg:
        await update.effective_message.reply_text(banned_msg)
        return await show_menu(update, context, cleanup=False)

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

        if not user_holds_profile(uid):
            raise FlowError("El perfil ya no está asignado a tu sesión.", refundable=True)

        await update.effective_chat.send_action(ChatAction.TYPING)
        await update.effective_message.reply_text("⏳ Procesando vinculación…")

        result = await asyncio.to_thread(complete_enroll, profile, client, track_id, otp)

        count = record_profile_success(profile_id)
        release_profile(profile_id, uid)
        close_hubox_session(context)
        context.user_data.clear()
        purged = False
        if count >= MAX_SUCCESSES_PER_PROFILE:
            purged = maybe_purge_exhausted_profile(profile_id)
            log.info(
                "Perfil %s agotado tras %s/%s éxitos (purgado=%s)",
                profile_id,
                count,
                MAX_SUCCESSES_PER_PROFILE,
                purged,
            )
        msg = format_success(result)
        if purged:
            msg += (
                f"\n\n🗑 Perfil `{profile_id}` alcanzó "
                f"{MAX_SUCCESSES_PER_PROFILE} activaciones y fue eliminado."
            )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
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
            if maybe_purge_exhausted_profile(profile_id):
                log.info("Perfil %s descartado y eliminado: %s", profile_id, exc)
            else:
                log.info("Perfil %s descartado: %s", profile_id, exc)
        cleanup_user_flow(context, uid, refund_if_charged=exc.refundable)
        await reply_fail(update, str(exc), refunded=exc.refundable)
        return await show_menu(update, context, cleanup=False)

    except Exception as exc:
        log.exception("flow failed")
        cleanup_user_flow(context, uid, refund_if_charged=True)
        await reply_fail(update, f"Error: {exc}", refunded=True)
        return await show_menu(update, context, cleanup=False)


# --- PATCH: main() con chequeo defensivo de JobQueue ---
def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en .env")

    app = Application.builder().token(token).build()

    # Chequeo defensivo: sin JobQueue los timeouts de ConversationHandler no funcionan.
    if app.job_queue is None:
        raise SystemExit(
            "CRÍTICO: JobQueue no está activo.\n"
            "Instala 'python-telegram-bot[job-queue]' y reinicia el bot.\n"
            "Verifica requirements.txt y el Dockerfile."
        )
    log.info("JobQueue activo - los timeouts de conversación funcionarán.")

    photo_or_image = (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MENU: [
                CallbackQueryHandler(on_menu_action, pattern=r"^act:"),
                CallbackQueryHandler(on_delete_profile, pattern=r"^del(ask|ok):"),
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_phone)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_otp)],
            REDEEM_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_redeem_key)],
            ADD_FRONT: [
                MessageHandler(photo_or_image, on_add_front),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_add_expect_photo),
            ],
            ADD_BACK: [
                MessageHandler(photo_or_image, on_add_back),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_add_expect_photo),
            ],
            ADD_SELFIE: [
                MessageHandler(photo_or_image, on_add_selfie),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_add_expect_photo),
            ],
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
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CommandHandler("creditos", cmd_creditos))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("perfiles", cmd_perfiles))
    app.add_handler(CommandHandler("validar", cmd_validar))
    app.add_handler(CommandHandler("preparar", cmd_preparar))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    log.info("Preparando perfiles…")
    _run_profile_prep(notify_log=True)
    prepare_pool()
    log.info("Bot arrancando\n%s", startup_report())
    app.run_polling(allowed_updates=Update.ALL_TYPES)
# --- FIN PATCH ---


if __name__ == "__main__":
    main()