"""Flujo guiado /etiqueta: referencia -> color -> metros -> imprime en la Zebra."""
import asyncio
import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import datos
from etiquetas import imprimir_etiqueta
from handlers import registrar_uso_bot

logger = logging.getLogger(__name__)

# Estados como texto para no chocar con los de los demás módulos
ESPERANDO_REF_ET = "et_esperando_ref"
SELECCIONANDO_REF_ET = "et_seleccionando_ref"
SELECCIONANDO_COLOR_ET = "et_seleccionando_color"
ESPERANDO_METROS_ET = "et_esperando_metros"

MAX_BOTONES = 40
MAX_METROS = 10000


async def _responder(update: Update, texto: str, markup=None) -> None:
    """Edita el mensaje si viene de un botón; si no, responde con uno nuevo."""
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(texto, reply_markup=markup)
            return
        except Exception:
            pass  # p. ej. mensaje idéntico: se envía uno nuevo
    await update.effective_message.reply_text(texto, reply_markup=markup)


def _formatear_metros(m: float) -> str:
    if m == int(m):
        return f"{int(m)} m"
    return f"{m:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " m"


async def iniciar_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    registrar_uso_bot(update, "BOTÓN_ETIQUETA" if update.callback_query else "/ETIQUETA")
    context.user_data["et"] = {}
    await _responder(update, "🏷️ Etiqueta Zebra\n\nEscribe la referencia (o parte de ella):")
    return ESPERANDO_REF_ET


async def _mostrar_colores(update: Update, context: ContextTypes.DEFAULT_TYPE, ref: str, df) -> str:
    colores = datos.colores_de(df, ref)
    if not colores:
        await _responder(update, f"❌ {ref} no tiene colores registrados.\n\nEscribe otra referencia:")
        return ESPERANDO_REF_ET

    colores = colores[:MAX_BOTONES]
    context.user_data["et"] = {"ref": ref, "colores": colores}

    botones, fila = [], []
    for i, c in enumerate(colores):
        fila.append(InlineKeyboardButton(c, callback_data=f"et_col_{i}"))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    botones.append([InlineKeyboardButton("↩️ Cambiar referencia", callback_data="et_back")])

    await _responder(update, f"Referencia: {ref}\n\nElige el color:", InlineKeyboardMarkup(botones))
    return SELECCIONANDO_COLOR_ET


async def recibir_texto_ref_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    termino = update.message.text.strip().upper()
    df = await datos.inventario.obtener()
    if df is None:
        await update.message.reply_text("⚠️ Inventario no disponible, intenta en un momento.")
        return ESPERANDO_REF_ET

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await update.message.reply_text(f"❌ No encontré la referencia «{termino}». Intenta de nuevo:")
        return ESPERANDO_REF_ET

    if termino in refs:
        return await _mostrar_colores(update, context, termino, df)
    if len(refs) == 1:
        return await _mostrar_colores(update, context, refs[0], df)

    mostradas = refs[:MAX_BOTONES]
    context.user_data["et"] = {"refs": mostradas}
    botones = [[InlineKeyboardButton(r, callback_data=f"et_ref_{i}")] for i, r in enumerate(mostradas)]
    texto = f"Encontré {len(refs)} referencias, elige una:"
    if len(refs) > len(mostradas):
        texto += f"\n(Se muestran las primeras {len(mostradas)}; sé más específico para filtrar.)"
    await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botones))
    return SELECCIONANDO_REF_ET


async def seleccionar_referencia_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    idx = int(q.data.rsplit("_", 1)[1])
    refs = context.user_data.get("et", {}).get("refs", [])
    if idx >= len(refs):
        await q.answer("Opción no válida. Escribe la referencia de nuevo.", show_alert=True)
        return ESPERANDO_REF_ET

    df = await datos.inventario.obtener()
    if df is None:
        await q.answer("Inventario no disponible, intenta en un momento.", show_alert=True)
        return ESPERANDO_REF_ET
    return await _mostrar_colores(update, context, refs[idx], df)


async def seleccionar_color_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    if q.data == "et_back":
        context.user_data["et"] = {}
        await _responder(update, "Escribe la referencia (o parte de ella):")
        return ESPERANDO_REF_ET

    et = context.user_data.get("et", {})
    colores = et.get("colores", [])
    idx = int(q.data.rsplit("_", 1)[1])
    if "ref" not in et or idx >= len(colores):
        await q.answer("Opción no válida. Escribe la referencia de nuevo.", show_alert=True)
        return ESPERANDO_REF_ET

    et["color"] = colores[idx]
    await _responder(
        update,
        f"Referencia: {et['ref']}\nColor: {et['color']}\n\n"
        "¿Cuántos metros lleva la etiqueta? (ejemplo: 50 o 48,5)",
    )
    return ESPERANDO_METROS_ET


async def recibir_metros_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    texto = update.message.text.strip().lower().replace(",", ".")
    texto = texto.removesuffix("m").removesuffix("mts").strip()
    try:
        metros = float(texto)
    except ValueError:
        metros = None

    if metros is None or not math.isfinite(metros) or not (0 < metros <= MAX_METROS):
        await update.message.reply_text("⚠️ Escribe un número de metros válido, por ejemplo 50 o 48,5:")
        return ESPERANDO_METROS_ET

    et = context.user_data.get("et", {})
    ref, color = et.get("ref"), et.get("color")
    if not ref or not color:
        await update.message.reply_text("⚠️ Se perdió la selección. Escribe /etiqueta para empezar de nuevo.")
        return ConversationHandler.END

    metros_txt = _formatear_metros(metros)
    try:
        await asyncio.to_thread(imprimir_etiqueta, ref, color, metros_txt)
    except Exception:
        logger.exception("No se pudo imprimir la etiqueta")
        await update.message.reply_text(
            "⚠️ No se pudo enviar la etiqueta a la impresora. Escribe /etiqueta para reintentar."
        )
        return ConversationHandler.END

    context.user_data.pop("et", None)
    await update.message.reply_text(f"✅ Etiqueta enviada:\n{ref} · {color} · {metros_txt}")
    return ConversationHandler.END
