import asyncio
import logging
import os
from datetime import date
from urllib.parse import quote

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from handlers import esc, programar_borrado, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados 0-8 los usan /PV, /IN y /I (handlers.py, inventario_*_handlers.py).
ESPERANDO_DOCUMENTO_PG = 9

BASE_URL = (
    "https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/"
    "ords/multiplantas/noveltex/ntex_ws_trazabilidad"
)
PAGE_SIZE = 500       # registros por bloque (la guía propone máximo 500)
TIMEOUT = 60          # segundos
TELEGRAM_MAX = 3800   # Telegram permite 4096 caracteres por mensaje


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
# Formato del mensaje (HTML)
# --------------------------------------------------------------------------- #
def _n(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return str(v)


def _t(v) -> str:
    return "-" if v in (None, "") else str(v)


def _fecha(v) -> str:
    return "-" if not v else str(v)[:10]


def formatear_pedido(documento: str, items: list, tipo: str = "PV") -> str:
    h = items[0]
    lineas = [
        f"🚚 <b>Trazabilidad {esc(tipo)} {esc(documento)}</b>",
        "",
        f"👤 <b>Cliente:</b> {esc(_t(h.get('nombre tercero')))}",
        f"🧑‍💼 <b>Vendedor:</b> {esc(_t(h.get('nombre vendedor')))}",
        f"📍 <b>Ubicación:</b> {esc(_t(h.get('ubicación del pedido')))}  |  "
        f"<b>Estado SAP:</b> {esc(_t(h.get('document status sap')))}",
        f"🧾 <b>Proforma:</b> {esc(_t(h.get('numero proforma')))}  |  "
        f"<b>Valor:</b> {esc(_n(h.get('valor proforma')))}  |  "
        f"<b>Abonos:</b> {esc(_n(h.get('total abonos')))}",
        f"📅 <b>Vence:</b> {esc(_fecha(h.get('fecha vencimiento')))}  |  "
        f"<b>Último mov.:</b> {esc(_fecha(h.get('fecha de ultimo movimiento')))}",
        f"📦 <b>Factura:</b> {esc(_t(h.get('numero factura')))}  |  "
        f"<b>Guía:</b> {esc(_t(h.get('numero guia')))}  |  "
        f"<b>Despacho:</b> {esc(_fecha(h.get('fecha despacho')))}",
        "",
        f"<b>Líneas ({len(items)}):</b>",
    ]
    for it in items:
        lineas.append(
            f"• <code>{esc(_t(it.get('id item')))}</code> {esc(_t(it.get('id refer')))} "
            f"· color {esc(_t(it.get('color')))} · "
            f"pedido {esc(_n(it.get('cantidad ped')))} / alistado {esc(_n(it.get('cantidad alistada')))} "
            f"({esc(_t(it.get('estado factura')))})"
        )
    return "\n".join(lineas)


def _partir(texto: str, limite: int = TELEGRAM_MAX) -> list:
    """Parte el texto en mensajes por saltos de línea (cada línea trae sus etiquetas completas)."""
    bloques, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > limite and actual:
            bloques.append(actual.rstrip("\n"))
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        bloques.append(actual.rstrip("\n"))
    return bloques


# --------------------------------------------------------------------------- #
# Handlers de Telegram
# --------------------------------------------------------------------------- #
async def iniciar_pg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/PG [número]: punto de entrada de la conversación."""
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
    """Número de pedido escrito en el mensaje siguiente a /PG."""
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
            f"❌ No se encontró el pedido <b>{esc(doc)}</b>.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
        )
        return ESPERANDO_DOCUMENTO_PG

    bloques = _partir(formatear_pedido(doc, items))
    await espera.edit_text(bloques[0], parse_mode=ParseMode.HTML)
    for b in bloques[1:]:
        await responder(update, context, b)
    return ConversationHandler.END
