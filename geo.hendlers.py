import asyncio
import logging
import os
import sys
from datetime import date
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BASE_URL = (
    "https://g6344c28e083a44-dbproyectos.adb.us-ashburn-1.oraclecloudapps.com/"
    "ords/multiplantas/noveltex/ntex_ws_trazabilidad"
)
PAGE_SIZE = 500          # registros por bloque (la guia propone maximo 500)
TIMEOUT = 60             # segundos
TELEGRAM_MAX = 4000      # Telegram permite 4096 caracteres por mensaje


# ---------------------------------------------------------------- servicio

def _id_llave() -> str:
    llave = os.environ.get("NTEX_WS_ID_LLAVE")
    if not llave:
        raise RuntimeError("Falta la variable de entorno NTEX_WS_ID_LLAVE")
    return llave


def _normalizar(item: dict) -> dict:
    """Llaves en minuscula y con espacios (acepta 'nombre_tercero' o 'nombre tercero')."""
    return {k.replace("_", " ").lower(): v for k, v in item.items() if k != "links"}


def consultar_pedido(documento: str, tipo: str = "PV") -> list:
    """Devuelve todas las lineas del pedido, pidiendo por bloques hasta que no haya mas."""
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


# ------------------------------------------------------------- formato texto

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
    if not items:
        return f"No encontré el pedido {tipo} {documento}."

    h = items[0]
    lineas = [
        f"Pedido {tipo} {documento}",
        f"Cliente: {_t(h.get('nombre tercero'))}",
        f"Vendedor: {_t(h.get('nombre vendedor'))}",
        f"Ubicación: {_t(h.get('ubicación del pedido'))}  |  Estado SAP: {_t(h.get('document status sap'))}",
        f"Proforma: {_t(h.get('numero proforma'))}  |  Valor: {_n(h.get('valor proforma'))}  |  Abonos: {_n(h.get('total abonos'))}",
        f"Vencimiento: {_fecha(h.get('fecha vencimiento'))}  |  Último mov.: {_fecha(h.get('fecha de ultimo movimiento'))}",
        f"Factura: {_t(h.get('numero factura'))}  |  Guía: {_t(h.get('numero guia'))}  |  Despacho: {_fecha(h.get('fecha despacho'))}",
        "",
        f"Líneas ({len(items)}):",
    ]
    for it in items:
        lineas.append(
            f"• {_t(it.get('id item'))} {_t(it.get('id refer'))} color {_t(it.get('color'))}: "
            f"pedido {_n(it.get('cantidad ped'))} / alistado {_n(it.get('cantidad alistada'))} "
            f"({_t(it.get('estado factura'))})"
        )
    return "\n".join(lineas)


def _partir(texto: str, limite: int = TELEGRAM_MAX) -> list:
    bloques, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > limite and actual:
            bloques.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        bloques.append(actual)
    return bloques


# ------------------------------------------------------------- handler Telegram

async def pedido_command(update, context):
    """/pedido 7374  -> trazabilidad del pedido PV 7374."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso: /pedido 7374")
        return

    doc = context.args[0]
    espera = await update.message.reply_text(f"Consultando pedido {doc}...")
    try:
        # requests es bloqueante: se corre en otro hilo para no frenar el bot
        items = await asyncio.to_thread(consultar_pedido, doc)
    except Exception:
        logger.exception("Error consultando trazabilidad del pedido %s", doc)
        await espera.edit_text("No pude consultar la trazabilidad ahora. Intenta de nuevo en un momento.")
        return

    bloques = _partir(formatear_pedido(doc, items))
    await espera.edit_text(bloques[0])
    for b in bloques[1:]:
        await update.message.reply_text(b)


if __name__ == "__main__":
    numero = sys.argv[1] if len(sys.argv) > 1 else input("Número de pedido PV: ").strip()
    print(formatear_pedido(numero, consultar_pedido(numero)))
