import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import datos
from handlers import editar, esc, programar_borrado, recortar, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados únicos (no chocan con los de handlers.py ni con los del vendedor)
SELECCIONANDO_REF_SAP, SELECCIONANDO_COLOR_SAP, ESPERANDO_REF_SAP = 3, 4, 5

MAX_BOTONES = 20


def _num(valor, defecto):
    """Número con coma o punto decimal; si está vacío o inválido devuelve `defecto`."""
    texto = str(valor).strip().replace(",", ".")
    if not texto:
        return defecto
    try:
        return float(texto)
    except ValueError:
        return defecto


def _primer_valor(fila, columnas, defecto=""):
    for c in columnas:
        val = str(fila.get(c, "")).strip()
        if val and val != "nan":
            return val
    return defecto


def _texto_inventario(df, ref: str, color: str, filas) -> str:
    """Detalle final: artículo, descripción, desglose por almacén y totales generales."""
    cols_cod = [c for c in df.columns if any(k in c.upper() for k in ("CÓDIGO", "ALMACÉN", "ALMACEN"))]
    cols_nom = [c for c in df.columns if "NOMBRE" in c.upper() and "ALM" in c.upper()]

    primera = filas.iloc[0]
    codigo = esc(primera.get("Artículo", "N/A"))
    descripcion = esc(primera.get("Descripción", f"{ref} - {color}"))

    mensaje = (
        f"🟢 <b>Inventario</b>\n\n"
        f"• <b>Artículo:</b> <code>{codigo}</code>\n"
        f"• <b>Descripción:</b> {descripcion}\n\n"
        f"<b>Desglose por Almacén:</b>\n"
    )

    total_stock = total_comp = total_disp = 0.0
    for _, fila in filas.iterrows():
        cod_almacen = _primer_valor(fila, cols_cod, "1")
        nom_almacen = _primer_valor(fila, cols_nom, "")
        almacen_txt = f"{cod_almacen} - {nom_almacen}" if nom_almacen else cod_almacen

        stock = _num(fila.get("Stock", "0"), 0.0)
        comprometido = _num(fila.get("Comprometido", "0"), 0.0)
        disponible = _num(fila.get("Disponible", ""), None)
        if disponible is None:
            disponible = stock - comprometido

        total_stock += stock
        total_comp += comprometido
        total_disp += disponible

        mensaje += (
            f"• <b>Almacén {esc(almacen_txt)}:</b>\n"
            f"  - En stock: <code>{stock:,.2f}</code>\n"
            f"  - Comprometido: <code>{comprometido:,.2f}</code>\n"
            f"  - Disponible: <code>{disponible:,.2f}</code>\n\n"
        )

    mensaje += (
        f"-----------------------------------\n"
        f"📊 <b>Totales Generales:</b>\n"
        f"• <b>Stock Total:</b> <code>{total_stock:,.2f}</code>\n"
        f"• <b>Total Comprometido:</b> <code>{total_comp:,.2f}</code>\n"
        f"• <b>Total Disponible:</b> <code>{total_disp:,.2f}</code>"
    )
    return mensaje


def _partes_todo(df, ref: str, colores: list, limite: int = 3800) -> list:
    """Arma el detalle completo de TODOS los colores (mismo formato que un color individual)
    y lo divide en mensajes de hasta `limite` caracteres sin partir el bloque de un color."""
    titulo = f"📋 <b>Inventario completo</b>\nReferencia: <b>{esc(ref)}</b> ({len(colores)} colores)\n\n"

    bloques = []
    for color in colores:
        filas = df[(df["_ref"] == ref) & (df["_color"] == color)]
        if filas.empty:
            continue
        bloques.append(_texto_inventario(df, ref, color, filas) + "\n\n===================\n\n")

    if not bloques:
        return [titulo + "❌ No se encontró inventario para esta referencia."]

    partes, actual = [], titulo
    for b in bloques:
        if actual != titulo and len(actual) + len(b) > limite:
            partes.append(actual.rstrip())
            actual = ""
        actual += b
    partes.append(actual.rstrip())
    return partes


def _pantalla_refs(d: dict):
    texto = f"🔍 Encontré varias referencias para <b>{esc(d['termino'])}</b>.\nSelecciona una referencia:"
    if d["total"] > len(d["refs"]):
        texto += f"\n(Mostrando {len(d['refs'])} de {d['total']}; escribe una referencia más específica)"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(ref, callback_data=f"sap_ref_{i}")] for i, ref in enumerate(d["refs"])
    ])
    return texto, teclado


def _pantalla_colores(d: dict):
    filas = [[InlineKeyboardButton("📋 Ver todo", callback_data="sap_all")]]
    filas += [
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

    teclado_volver = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a colores", callback_data="sap_back_cols")]])

    if data == "sap_all":
        partes = _partes_todo(df, d["ref"], d["colores"])
        if len(partes) == 1:
            await editar(query, partes[0], teclado_volver)
        else:  # detalle largo: varios mensajes y el original queda con el botón de volver
            for parte in partes:
                msg = await query.message.reply_text(parte, parse_mode="HTML")
                programar_borrado(context, msg)
            await editar(query, "📌 Fin del detalle completo.", teclado_volver)
        return SELECCIONANDO_COLOR_SAP

    try:
        color = d["colores"][int(data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada(query)

    filas = df[(df["_ref"] == d["ref"]) & (df["_color"] == color)]
    if filas.empty:
        mensaje = f"❌ No se encontró inventario para la referencia <b>{esc(d['ref'])}</b> y color <b>{esc(color)}</b>."
    else:
        mensaje = _texto_inventario(df, d["ref"], color, filas)

    await editar(query, recortar(mensaje), teclado_volver)
    return SELECCIONANDO_COLOR_SAP
