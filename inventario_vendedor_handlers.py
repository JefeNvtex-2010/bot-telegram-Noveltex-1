import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import datos
from acceso import requiere_acceso
from handlers import editar, esc, programar_borrado, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados únicos (no chocan con los de handlers.py ni con los de SAP)
SELECCIONANDO_REF_VEND, SELECCIONANDO_COLOR_VEND, ESPERANDO_REF_VEND = 6, 7, 8

MAX_BOTONES = 20


def _fmt(valor: float) -> str:
    return f"{int(valor)}" if float(valor).is_integer() else f"{valor:,.2f}"


def _pantalla_refs(d: dict):
    texto = f"🔍 Encontré varias referencias para <b>{esc(d['termino'])}</b>.\nSelecciona una referencia:"
    if d["total"] > len(d["refs"]):
        texto += f"\n(Mostrando {len(d['refs'])} de {d['total']}; escribe una referencia más específica)"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(ref, callback_data=f"vend_ref_{i}")] for i, ref in enumerate(d["refs"])
    ])
    return texto, teclado


def _pantalla_colores(d: dict, df):
    # (color, disponible en bodega 01) calculado de forma vectorizada
    d["colores"] = datos.disponibilidad_bodega01(df, d["ref"])[:MAX_BOTONES]
    filas = [
        [InlineKeyboardButton(f"{color} - {_fmt(disp)} MTRS", callback_data=f"vend_col_{i}")]
        for i, (color, disp) in enumerate(d["colores"])
    ]
    if len(d["refs"]) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="vend_back_refs")])
    # Botón final: convierte la lista a texto para copiar/reenviar
    filas.append([InlineKeyboardButton("📋 Clic para compartir", callback_data="vend_share")])

    texto = f"🔍 Referencia: <b>{esc(d['ref'])}</b>.\nSelecciona un color:"
    return texto, InlineKeyboardMarkup(filas)


def _texto_compartir(d: dict) -> str:
    """Lista en texto plano (sin botones) lista para copiar o reenviar."""
    lineas = [f"{esc(color)} - {_fmt(disp)} MTRS" for color, disp in d["colores"]]
    return f"🔍 Referencia: <b>{esc(d['ref'])}</b> (Bodega 01)\n\n" + "\n".join(lineas)


@requiere_acceso("inventario_vendedor")
async def iniciar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_INVENTARIO_VENDEDOR")
        await editar(
            query,
            "🛒 <b>Inventario Vendedor (Bodega 01)</b>\n\nEscribe la <b>referencia</b> que deseas buscar:",
        )
        return ESPERANDO_REF_VEND

    registrar_uso_bot(update, "/iv")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update,
            context,
            "🛒 <b>Inventario Vendedor (Bodega 01)</b>\n\nEscribe la <b>referencia</b> que deseas buscar "
            "(o usa <code>/iv VERMONT</code>):",
        )
        return ESPERANDO_REF_VEND

    return await ejecutar_busqueda_vendedor(update, context, " ".join(context.args))


async def recibir_texto_ref_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_busqueda_vendedor(update, context, update.message.text)


async def ejecutar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, termino: str) -> int:
    termino = termino.strip().upper()
    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.")
        return ESPERANDO_REF_VEND

    if datos.inventario.df is None:  # solo en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando inventario, un momento…")

    df = await datos.inventario.obtener()
    if df is None:
        detalle = f"\n<code>{esc(datos.inventario.ultimo_error)}</code>" if datos.inventario.ultimo_error else ""
        await responder(update, context, f"⚠️ No se pudo acceder al inventario.{detalle}")
        return ConversationHandler.END

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await responder(
            update,
            context,
            f"❌ No se encontraron referencias para: <b>{esc(termino)}</b>.\nPuedes escribir otra referencia.",
        )
        return ESPERANDO_REF_VEND

    d = {"termino": termino, "refs": refs[:MAX_BOTONES], "total": len(refs)}
    context.user_data["vend"] = d

    if len(refs) == 1:
        d["ref"] = refs[0]
        texto, teclado = _pantalla_colores(d, df)
        await responder(update, context, texto, reply_markup=teclado)
        return SELECCIONANDO_COLOR_VEND

    texto, teclado = _pantalla_refs(d)
    await responder(update, context, texto, reply_markup=teclado)
    return SELECCIONANDO_REF_VEND


async def _expirada(query) -> int:
    await editar(query, "⌛ Esta consulta expiró. Escribe /i para empezar de nuevo.")
    return ConversationHandler.END


async def seleccionar_referencia_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("vend")
    df = await datos.inventario.obtener()
    if not d or df is None:
        return await _expirada(query)
    try:
        d["ref"] = d["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada(query)

    texto, teclado = _pantalla_colores(d, df)
    await editar(query, texto, teclado)
    return SELECCIONANDO_COLOR_VEND


async def seleccionar_color_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.get("vend")
    if not d or "ref" not in d:
        await query.answer()
        return await _expirada(query)

    if query.data == "vend_back_refs":
        await query.answer()
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REF_VEND

    if query.data == "vend_share":
        await query.answer()
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=_texto_compartir(d),
            parse_mode=ParseMode.HTML,
        )
        programar_borrado(context, msg)  # se borra a las 24 h como los demás mensajes
        return SELECCIONANDO_COLOR_VEND

    try:
        color, disp = d["colores"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        await query.answer()
        return await _expirada(query)

    # Un solo query.answer() (antes se llamaba dos veces y el aviso nunca se mostraba)
    await query.answer(
        text=f"{d['ref']} - {color}\nDisponible en bodega 01: {_fmt(disp)} MTRS",
        show_alert=True,
    )
    return SELECCIONANDO_COLOR_VEND
