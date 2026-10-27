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

# Estados 0-8 los usan /PV, /IN y /I; 9-11 los usa /PG. /PC usa 12-14.
ESPERANDO_DOCUMENTO_PC, SELECCIONANDO_REF_PC, SELECCIONANDO_COLOR_PC = 12, 13, 14

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


def consultar_pedido_pc(documento: str, tipo: str = "PC") -> list:
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

    campos_contado = ""
    if contado:
        campos_contado = "-----------------------------------\n"
        campos_contado += f"• <b>Fecha Vencimiento:</b> {esc(_fecha(it.get('fecha vencimiento'))) or 'N/A'}\n"
        num_proforma = esc(it.get("numero proforma"))
        if num_proforma:
            campos_contado += f"• <b>Número Proforma:</b> {num_proforma}\n"

    # Datos que solo trae el web service: se muestran únicamente si tienen valor
    extra = ""
    for etiqueta, clave in (("Número Factura", "numero factura"), ("Número Guía", "numero guia")):
        valor = esc(it.get(clave))
        if valor:
            extra += f"• <b>{etiqueta}:</b> {valor}\n"

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
        f"{extra}"
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
        [InlineKeyboardButton(str(ref) if ref else "(sin referencia)", callback_data=f"pc_ref_{i}")]
        for i, ref in enumerate(d["refs"][:MAX_BOTONES])
    ])
    return texto, teclado


def _pantalla_colores(d: dict):
    items_ref = [i for i in d["items"] if i.get("id refer") == d["ref"]]
    filas = [[InlineKeyboardButton("📄 Ver todo", callback_data="pc_all")]]
    for i, color in enumerate(d["colores"][:MAX_BOTONES]):
        etiqueta = str(color) if color not in (None, "") else "(sin color)"
        estados = list(dict.fromkeys(
            _traducir_estado(x.get("line status sap", "")) for x in items_ref if x.get("color") == color
        ))
        if estados:
            etiqueta += f" - {' / '.join(estados)}"
        filas.append([InlineKeyboardButton(etiqueta, callback_data=f"pc_col_{i}")])
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="pc_back_refs")])
    texto = f"Referencia seleccionada: <b>{esc(d['ref'])}</b>.\nSelecciona el color:"
    return texto, InlineKeyboardMarkup(filas)


# --------------------------------------------------------------------------- #
# Handlers de Telegram
# --------------------------------------------------------------------------- #
async def iniciar_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/PC [número] o botón del menú: punto de entrada de la conversación."""
    query = update.callback_query
    if query:  # botón "Pedidos (PC)" del menú /inicio
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_PC")
        await editar(query, "🚚 <b>Trazabilidad de pedidos</b>\n\nEscribe el <b>número de pedido</b> que deseas buscar:")
        return ESPERANDO_DOCUMENTO_PC

    registrar_uso_bot(update, "/PC")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "🚚 <b>Trazabilidad de pedidos</b>\n\nEscribe el <b>número de pedido</b> que deseas buscar "
            "(o usa <code>/PC 7374</code>):",
        )
        return ESPERANDO_DOCUMENTO_PC

    return await ejecutar_pc(update, context, context.args[0])


async def recibir_texto_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Número de pedido escrito en el mensaje siguiente a /PC (o para cambiar de pedido)."""
    programar_borrado(context, update.message)
    return await ejecutar_pc(update, context, update.message.text)


async def ejecutar_pc(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str) -> int:
    doc = texto.strip().split(".")[0].strip()
    if not doc.isdigit():
        await responder(
            update, context,
            "⚠️ Escribe solo el <b>número de pedido</b>, por ejemplo <code>7374</code>.",
        )
        return ESPERANDO_DOCUMENTO_PC

    espera = await responder(update, context, f"⏳ Consultando el pedido <b>{esc(doc)}</b>…")
    try:
        # requests es bloqueante: se corre en otro hilo para no frenar el bot
        items = await asyncio.to_thread(consultar_pedido_pc, doc)
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
        return ESPERANDO_DOCUMENTO_PC

    d = {
        "doc": doc,
        "items": items,
        "refs": list(dict.fromkeys(i.get("id refer") for i in items)),
    }
    context.user_data["pc"] = d

    if len(d["refs"]) == 1:  # una sola referencia: salta directo a los colores
        d["ref"] = d["refs"][0]
        d["colores"] = _colores(items, d["ref"])
        texto_msg, teclado = _pantalla_colores(d)
        await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
        return SELECCIONANDO_COLOR_PC

    texto_msg, teclado = _pantalla_refs(d)
    await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
    return SELECCIONANDO_REF_PC


async def seleccionar_referencia_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pc")
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
    return SELECCIONANDO_COLOR_PC


async def seleccionar_color_pc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pc")
    if not d or "ref" not in d:
        return await _sesion_expirada(query)
    data = query.data

    if data == "pc_back_refs":
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REF_PC

    if data == "pc_back_cols":
        texto, teclado = _pantalla_colores(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_COLOR_PC

    base = [i for i in d["items"] if i.get("id refer") == d["ref"]]
    if data == "pc_all":
        filas = base
        titulo = (
            f"🔍 <b>Detalle Completo - Referencia {esc(d['ref'])}</b> "
            f"(Documento {esc(d['doc'])}, Total ítems: {len(filas)}):\n"
        )
    else:
        try:
            color = d["colores"][int(data.rsplit("_", 1)[1])]
        except (ValueError, IndexError):
            return await _sesion_expirada(query)
        filas = [i for i in base if i.get("color") == color]
        titulo = (
            f"🔍 <b>Detalle del Documento {esc(d['doc'])}</b> "
            f"(Color {esc(color)}, Total ítems: {len(filas)}):\n"
        )

    bloques = ["\n-----------------------------------\n" + _texto_detalle(it) for it in filas]
    partes = _agrupar(titulo, bloques)

    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a colores", callback_data="pc_back_cols")]])
    if len(partes) == 1:
        await editar(query, partes[0], teclado)
    else:  # detalle largo: se envía en varios mensajes y el original queda con el botón de volver
        for parte in partes:
            msg = await query.message.reply_text(parte, parse_mode=ParseMode.HTML)
            programar_borrado(context, msg)
        await editar(query, "📌 Fin del detalle.", teclado)
    return SELECCIONANDO_COLOR_PC
