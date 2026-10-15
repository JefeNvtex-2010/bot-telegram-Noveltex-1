import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import datos
from handlers import editar, esc, programar_borrado, recortar, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados únicos (no chocan con los de handlers.py ni con los del vendedor)
SELECCIONANDO_REF_SAP, SELECCIONANDO_COLOR_SAP, ESPERANDO_REF_SAP = 3, 4, 5

MAX_BOTONES = 20


def _pantalla_refs(d: dict):
    texto = f"🔍 Varias referencias para <b>{esc(d['termino'])}</b>:"
    if d["total"] > len(d["refs"]):
        texto += f"\n(Mostrando {len(d['refs'])} de {d['total']}; escribe una referencia más específica)"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(ref, callback_data=f"sap_ref_{i}")] for i, ref in enumerate(d["refs"])
    ])
    return texto, teclado


def _pantalla_colores(d: dict):
    filas = [
        [InlineKeyboardButton(color, callback_data=f"sap_col_{i}")]
        for i, color in enumerate(d["colores"][:MAX_BOTONES])
    ]
    if len(d["refs"]) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="sap_back_refs")])
    texto = f"🔍 Referencia: <b>{esc(d['ref'])}</b>.\nSelecciona un color:"
    return texto, InlineKeyboardMarkup(filas)


async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_INVENTARIO_SAP")
        await editar(query, "📦 <b>Inventario SAP</b>\n\nEscribe la <b>referencia</b> que deseas buscar:")
        return ESPERANDO_REF_SAP

    registrar_uso_bot(update, "/in")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "📦 <b>Inventario SAP</b>\n\nEscribe la <b>referencia</b> que deseas buscar "
            "(o usa <code>/in SATIN</code>):",
        )
        return ESPERANDO_REF_SAP

    return await ejecutar_busqueda_sap(update, context, " ".join(context.args))


async def recibir_texto_ref_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_busqueda_sap(update, context, update.message.text)


async def ejecutar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE, termino: str) -> int:
    termino = termino.strip().upper()
    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.")
        return ESPERANDO_REF_SAP

    if datos.inventario.df is None:  # solo en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando inventario, un momento…")

    df = await datos.inventario.obtener()
    if df is None:
        detalle = f"\n<code>{esc(datos.inventario.ultimo_error)}</code>" if datos.inventario.ultimo_error else ""
        await responder(update, context, f"⚠️ No se pudo acceder al inventario.{detalle}")
        return ConversationHandler.END

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await responder(update, context, f"❌ No se encontró: <b>{esc(termino)}</b>.\nPuedes escribir otra referencia.")
        return ESPERANDO_REF_SAP

    d = {"termino": termino, "refs": refs[:MAX_BOTONES], "total": len(refs)}
    context.user_data["sap"] = d

    if len(refs) == 1:
        d["ref"] = refs[0]
        d["colores"] = datos.colores_de(df, refs[0])
        texto, teclado = _pantalla_colores(d)
        await responder(update, context, texto, reply_markup=teclado)
        return SELECCIONANDO_COLOR_SAP

    texto, teclado = _pantalla_refs(d)
    await responder(update, context, texto, reply_markup=teclado)
    return SELECCIONANDO_REF_SAP


async def _expirada(query) -> int:
    await editar(query, "⌛ Esta consulta expiró. Escribe /inicio para empezar de nuevo.")
    return ConversationHandler.END


async def seleccionar_referencia_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("sap")
    df = await datos.inventario.obtener()
    if not d or df is None:
        return await _expirada(query)
    try:
        ref = d["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada(query)

    d["ref"] = ref
    d["colores"] = datos.colores_de(df, ref)
    texto, teclado = _pantalla_colores(d)
    await editar(query, texto, teclado)
    return SELECCIONANDO_COLOR_SAP


async def seleccionar_color_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("sap")
    if not d or "ref" not in d:
        return await _expirada(query)
    data = query.data

    if data == "sap_back_refs":
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REF_SAP

    if data == "sap_back_cols":
        texto, teclado = _pantalla_colores(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_COLOR_SAP

    df = await datos.inventario.obtener()
    if df is None:
        return await _expirada(query)
    try:
        color = d["colores"][int(data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada(query)

    filas = df[(df["_ref"] == d["ref"]) & (df["_color"] == color)]
    mensaje = f"🟢 <b>Inventario SAP</b> ({esc(d['ref'])} - {esc(color)}):\n"
    if filas.empty:
        mensaje += "Sin registros."
    for _, fila in filas.iterrows():
        mensaje += (
            f"• Stock: <code>{esc(fila.get('Stock', '0'))}</code> | "
            f"Disponible: <code>{esc(fila.get('Disponible', '0'))}</code>\n"
        )

    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="sap_back_cols")]])
    await editar(query, recortar(mensaje), teclado)
    return SELECCIONANDO_COLOR_SAP
