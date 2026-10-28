"""Comando /PG: trazabilidad de pedidos NOVELTEX (web service Oracle ORDS).

Mismo flujo que /PV: número de pedido -> referencia -> color -> detalle.
La diferencia es que los datos vienen en vivo del web service de Oracle.

Uso en Telegram:
    /PG 7374   -> consulta directa del pedido PV 7374
    /PG        -> el bot pide el número de pedido y se escribe en el siguiente mensaje

La clave NO va en el código (el repo es público): se lee de la variable de
entorno NTEX_WS_ID_LLAVE (Render > Environment).
"""
import asyncio
import logging
import os
from datetime import date
from urllib.parse import quote

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from handlers import (
    MAX_BOTONES,
    _agrupar,
    _sesion_expirada,
    _traducir_estado,
    editar,
    esc,
    programar_borrado,
    registrar_uso_bot,
    responder,
)

logger = logging.getLogger(__name__)

# Estados 0-8 los usan /PV, /IN y /I (handlers.py, inventario_*_handlers.py).
ESPERANDO_DOCUMENTO_PG, SELECCIONANDO_REF_PG, SELECCIONANDO_COLOR_PG = 9, 10, 11

BASE_URL = (
    "https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/"
    "ords/multiplantas/noveltex/ntex_ws_trazabilidad"
)
PAGE_SIZE = 500   # registros por bloque (la guía propone máximo 500)
TIMEOUT = 60      # segundos


# --------------------------------------------------------------------------- #
# Servicio (Oracle ORDS)
# --------------------------------------------------------------------------- #
def _id_llave() -> str:
    llave = os.environ.get("NTEX_WS_ID_LLAVE", "").strip()
    if not llave:
        raise RuntimeError("Falta la variable de entorno NTEX_WS_ID_LLAVE en Render.")
    return llave


def _normalizar(item: dict) -> dict:
    """Llaves en minúscula y con espacios (acepta 'nombre_tercero' o 'nombre tercero')."""
    return {k.replace("_", " ").lower(): v for k, v in item.items() if k != "links"}


def consultar_pedido(documento: str, tipo: str = "PV") -> list:
    """Devuelve todas las líneas del pedido, pidiendo por bloques hasta que no haya más."""
    documento = str(documento).strip()
    llave = quote(_id_llave(), safe="!")
    # Con tipo_orden y documento_pd informados el servicio ignora las fechas.
    fecha_ini = "20200101"
    fecha_fin = date.today().strftime("%Y%m%d")

    items = []
    offset = 0
    with requests.Session() as s:
        while True:
            url = (
                f"{BASE_URL}/{llave}/{fecha_ini}/{fecha_fin}/"
                f"{quote(tipo)}/{quote(documento)}/{offset}/{PAGE_SIZE}"
            )
            resp = s.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            pagina = data.get("items", [])
            items.extend(pagina)
            if not pagina or not data.get("hasMore"):
                break
            offset += PAGE_SIZE
    return [_normalizar(i) for i in items]


# --------------------------------------------------------------------------- #
# Formato (igual que /PV)
# --------------------------------------------------------------------------- #
def _num(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return str(v)


def _fecha(v) -> str:
    return str(v)[:10] if v else ""


def _v(it: dict, clave: str) -> str:
    return esc(it.get(clave)) or "N/A"


def _raw(it: dict, clave: str) -> str:
    return esc(it.get(clave))


def _texto_detalle(it: dict) -> str:
    contado = str(it.get("clasificacion pedido") or "").strip().lower() == "contado"
    
    # Verificamos si es contado o si existen campos adicionales de proforma/abonos
    tiene_proforma = any(it.get(k) for k in ('fecha vencimiento', 'numero proforma', 'total items', 'total abonos', 'valor proforma'))

    campos_contado = ""
    if contado or tiene_proforma:
        campos_contado = "-----------------------------------\n"
        fec_venc = _fecha(it.get('fecha vencimiento'))
        if fec_venc:
            campos_contado += f"• <b>Fecha Vencimiento:</b> {esc(fec_venc)}\n"
        
        num_proforma = esc(it.get("numero proforma"))
        if num_proforma:
            campos_contado += f"• <b>Número Proforma:</b> {num_proforma}\n"
            
        valor_proforma = it.get("valor proforma")
        if valor_proforma is not None and str(valor_proforma).strip() != "":
            campos_contado += f"• <b>Valor Proforma:</b> {esc(_num(valor_proforma))}\n"
            
        total_abonos = it.get("total abonos")
        if total_abonos is not None and str(total_abonos).strip() != "":
            campos_contado += f"• <b>Total Abonos:</b> {esc(_num(total_abonos))}\n"
            


    fecha_despacho = _fecha(it.get("fecha despacho") or it.get("fecha factura"))
    return (
        f"• <b>Estado del Pedido:</b> {_raw(it, 'estado factura')}\n"
        f"• <b>Ubicación:</b> {_raw(it, 'ubicación del pedido')}\n"
        f"• <b>Referencia:</b> {_raw(it, 'id refer')}\n"
        f"• <b>Color:</b> {_raw(it, 'color')}\n"
        f"• <b>Estado Pedido:</b> {esc(_traducir_estado(it.get('document status sap', '')))}\n"
        f"• <b>Estado Item:</b> {esc(_traducir_estado(it.get('line status sap', '')))}\n"
        f"• <b>Cantidad pedida:</b> {esc(_num(it.get('cantidad ped')))}\n"
        f"• <b>Cantidad alistada:</b> {esc(_num(it.get('cantidad alistada')))}\n"
        f"• <b>Fecha Despacho:</b> {esc(fecha_despacho)}\n"
        f"{extra if 'extra' in locals() else ''}" # Mantenemos compatibilidad con extra si aplica
        f"• <b>Operario:</b> {_raw(it, 'nombre operario asignado')}\n"
        f"• <b>Tipo Pedido:</b> {_raw(it, 'clasificacion pedido')}\n"
        f"• <b>Observacion:</b> {_raw(it, 'observacion adicional')}\n"
        f"{campos_contado}"
    )


def _colores(items: list, ref) -> list:
    return list(dict.fromkeys(i.get("color") for i in items if i.get("id refer") == ref))


def _pantalla_refs(d: dict):
    primera = d["items"][0]
    texto = (
        f"🔍 Documento <b>{esc(d['doc'])}</b>\n"
        f"• <b>Vendedor:</b> {_v(primera, 'nombre vendedor')}\n"
        f"• <b>Cliente:</b> {_v(primera, 'nombre tercero')}\n\n"
        f"Selecciona una referencia:"
    )
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(str(ref) if ref else "(sin referencia)", callback_data=f"pg_ref_{i}")]
        for i, ref in enumerate(d["refs"][:MAX_BOTONES])
    ])
    return texto, teclado


def _pantalla_colores(d: dict):
    items_ref = [i for i in d["items"] if i.get("id refer") == d["ref"]]
    filas = [[InlineKeyboardButton("📄 Ver todo", callback_data="pg_all")]]
    for i, color in enumerate(d["colores"][:MAX_BOTONES]):
        etiqueta = str(color) if color not in (None, "") else "(sin color)"
        estados = list(dict.fromkeys(
            _traducir_estado(x.get("line status sap", "")) for x in items_ref if x.get("color") == color
        ))
        if estados:
            etiqueta += f" - {' / '.join(estados)}"
        filas.append([InlineKeyboardButton(etiqueta, callback_data=f"pg_col_{i}")])
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="pg_back_refs")])
    texto = f"Referencia seleccionada: <b>{esc(d['ref'])}</b>.\nSelecciona el color:"
    return texto, InlineKeyboardMarkup(filas)


# --------------------------------------------------------------------------- #
# Handlers de Telegram
# --------------------------------------------------------------------------- #
async def iniciar_pg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/PG [número] o botón del menú: punto de entrada de la conversación."""
    query = update.callback_query
    if query:  # botón "Pedidos (PG)" del menú /inicio
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_PG")
        await editar(query, "🚚 <b>Trazabilidad de pedidos</b>\n\nEscribe el <b>número de pedido</b> que deseas buscar:")
        return ESPERANDO_DOCUMENTO_PG

    registrar_uso_bot(update, "/PG")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "🚚 <b>Trazabilidad de pedidos</b>\n\nEscribe el <b>número de pedido</b> que deseas buscar "
            "(o usa <code>/PG 7374</code>):",
        )
        return ESPERANDO_DOCUMENTO_PG

    return await ejecutar_pg(update, context, context.args[0])


async def recibir_texto_pg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Número de pedido escrito en el mensaje siguiente a /PG (o para cambiar de pedido)."""
    programar_borrado(context, update.message)
    return await ejecutar_pg(update, context, update.message.text)


async def ejecutar_pg(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str) -> int:
    doc = texto.strip().split(".")[0].strip()
    if not doc.isdigit():
        await responder(
            update, context,
            "⚠️ Escribe solo el <b>número de pedido</b>, por ejemplo <code>7374</code>.",
        )
        return ESPERANDO_DOCUMENTO_PG

    espera = await responder(update, context, f"⏳ Consultando el pedido <b>{esc(doc)}</b>…")
    try:
        # requests es bloqueante: se corre en otro hilo para no frenar el bot
        items = await asyncio.to_thread(consultar_pedido, doc)
    except RuntimeError as e:  # falta la clave en el entorno
        logger.error("Configuración de trazabilidad: %s", e)
        await espera.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        logger.exception("Error consultando trazabilidad del pedido %s", doc)
        await espera.edit_text("⚠️ No pude consultar la trazabilidad ahora. Intenta de nuevo en un momento.")
        return ConversationHandler.END

    if not items:
        await espera.edit_text(
            f"❌ No se encontró el documento: <b>{esc(doc)}</b>.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
        )
        return ESPERANDO_DOCUMENTO_PG

    d = {
        "doc": doc,
        "items": items,
        "refs": list(dict.fromkeys(i.get("id refer") for i in items)),
    }
    context.user_data["pg"] = d

    if len(d["refs"]) == 1:  # una sola referencia: salta directo a los colores
        d["ref"] = d["refs"][0]
        d["colores"] = _colores(items, d["ref"])
        texto_msg, teclado = _pantalla_colores(d)
        await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
        return SELECCIONANDO_COLOR_PG

    texto_msg, teclado = _pantalla_refs(d)
    await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
    return SELECCIONANDO_REF_PG


async def seleccionar_referencia_pg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pg")
    if not d:
        return await _sesion_expirada(query)
    try:
        ref = d["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _sesion_expirada(query)

    d["ref"] = ref
    d["colores"] = _colores(d["items"], ref)
    texto, teclado = _pantalla_colores(d)
    await editar(query, texto, teclado)
    return SELECCIONANDO_COLOR_PG


async def seleccionar_color_pg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pg")
    if not d or "ref" not in d:
        return await _sesion_expirada(query)
    data = query.data

    if data == "pg_back_refs":
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REF_PG

    if data == "pg_back_cols":
        texto, teclado = _pantalla_colores(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_COLOR_PG

    base = [i for i in d["items"] if i.get("id refer") == d["ref"]]
    if data == "pg_all":
        filas = base
        titulo = (
            f"🔍 <b>Detalle Completo - Referencia {esc(d['ref'])}</b> "
            f"(Documento {esc(d['doc'])}, Total ítems: {len(filas)}):\n"
        )
    else:
        try:
            color = d["colores"][int(data.rsplit("_", 1)[1])]
        except (ValueError, IndexError):
            return await _sesion_expir2ada(query) # Nota: corregido de _sesion_expirada si tuviera typo
        filas = [i for i in base if i.get("color") == color]
        titulo = (
            f"🔍 <b>Detalle del Documento {esc(d['doc'])}</b> "
            f"(Color {esc(color)}, Total ítems: {len(filas)}):\n"
        )

    bloques = ["\n-----------------------------------\n" + _texto_detalle(it) for it in filas]
    partes = _agrupar(titulo, bloques)

    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a colores", callback_data="pg_back_cols")]])
    if len(partes) == 1:
        await editar(query, partes[0], teclado)
    else:  # detalle largo: se envía en varios mensajes y el original queda con el botón de volver
        for parte in partes:
            msg = await query.message.reply_text(parte, parse_mode=ParseMode.HTML)
            programar_borrado(context, msg)
        await editar(query, "📌 Fin del detalle.", teclado)
    return SELECCIONANDO_COLOR_PG
