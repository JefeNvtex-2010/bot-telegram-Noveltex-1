import asyncio
import io
import json
import logging
import os
import re
import threading
import time
from datetime import date, timedelta
from urllib.parse import quote

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from handlers import (
    MAX_BOTONES,
    MI_TELEGRAM_ID,
    _sesion_expirada,
    editar,
    esc,
    programar_borrado,
    registrar_uso_bot,
    responder,
)
from geo_handlers import BASE_URL, PAGE_SIZE, _id_llave, _normalizar, consultar_pedido

logger = logging.getLogger(__name__)

# Estados 0-11 ya están en uso (/PV, /IN, /I, /PG).
ESPERANDO_DOCUMENTO_GU, SELECCIONANDO_GUIA_GU, ACCIONES_GU = 12, 13, 14

BASE_GUIAS = "https://guias.coordinadora.com/site"
MAIN_URL = f"{BASE_GUIAS}/main.php"
SERVER_URL = f"{BASE_GUIAS}/srv/server.php"
IMAGEN_URL = f"{BASE_GUIAS}/getimagen.php?codigo_remision={{guia}}"
RASTREO_URL = (
    "https://coordinadora.com/rastreo/rastreo-de-guia/detalle-de-rastreo-de-guia/?guia={guia}"
)
TIMEOUT = 60
KEEPALIVE_SEGUNDOS = 8 * 60  # cada cuánto el bot "toca" Coordinadora para que no cierre la sesión
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
HEADERS_RPC = {
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://guias.coordinadora.com",
    "Referer": MAIN_URL,
    "Accept": "*/*",
}
MSG_CADUCADA = (
    "La sesión de Coordinadora caducó. Inicia sesión en el navegador, copia la cookie PHPSESSID "
    "y actualiza la variable COORD_PHPSESSID en Render."
)

# Búsqueda por factura (el servicio de trazabilidad se consulta por rango de fechas)
DIAS_FACTURA = int(os.environ.get("NTEX_WS_DIAS_FACTURA", "90"))
COMODIN = os.environ.get("NTEX_WS_COMODIN", "0")  # valor para tipo/documento cuando no se filtra por pedido
CACHE_SEGUNDOS = 10 * 60
MAX_PAGINAS = 80


# --------------------------------------------------------------------------- #
# Sesión de Coordinadora (cookie PHPSESSID copiada del navegador + keep-alive)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_sesion = None
_vivo = None  # None = desconocido, True = activa, False = caducada


def _cookie() -> str:
    valor = os.environ.get("COORD_PHPSESSID", "").strip()
    if not valor:
        raise RuntimeError("Falta la variable COORD_PHPSESSID en Render (cookie de la sesión de Coordinadora).")
    if valor.upper().startswith("PHPSESSID="):  # por si se pegó con el nombre
        valor = valor.split("=", 1)[1]
    return valor.strip()


def _obtener_sesion() -> requests.Session:
    global _sesion
    with _lock:
        if _sesion is None:
            s = requests.Session()
            s.headers.update(UA)
            s.cookies.set("PHPSESSID", _cookie(), domain="guias.coordinadora.com", path="/")
            _sesion = s
        return _sesion


def _sesion_activa(s: requests.Session):
    """True si Coordinadora reconoce la sesión, False si no, None si no se pudo comprobar (red)."""
    try:
        r = s.post(
            SERVER_URL,
            data=json.dumps({"method": "agwSettings.values", "params": {}}),
            headers=HEADERS_RPC,
            timeout=TIMEOUT,
        )
        data = r.json()
    except Exception as e:
        logger.warning("Keep-alive Coordinadora: no se pudo comprobar la sesión (%s: %s)", type(e).__name__, e)
        return None
    activa = (not data.get("error")) and bool(data.get("result"))
    if not activa:
        logger.warning("Keep-alive Coordinadora: sesión no válida. Respuesta: %s", str(data)[:300])
    return activa


def _avisar_dueno(texto: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": MI_TELEGRAM_ID, "text": texto},
            timeout=15,
        )
    except Exception as e:
        logger.warning("No se pudo avisar al dueño: %s", e)


def _registrar_estado(activa) -> None:
    """Guarda el estado y avisa por Telegram una sola vez cuando la sesión se cae."""
    global _vivo
    if activa is None:
        return
    if activa is False and _vivo is not False:
        _avisar_dueno("⚠️ La sesión de Coordinadora caducó. " + MSG_CADUCADA)
    if activa is True and _vivo is False:
        _avisar_dueno("✅ La sesión de Coordinadora volvió a estar activa.")
    _vivo = activa


def _bucle_keepalive() -> None:
    time.sleep(30)  # deja arrancar el bot
    while True:
        try:
            if os.environ.get("COORD_PHPSESSID", "").strip():
                _registrar_estado(_sesion_activa(_obtener_sesion()))
        except Exception:
            logger.exception("Error en el keep-alive de Coordinadora")
        time.sleep(KEEPALIVE_SEGUNDOS)


threading.Thread(target=_bucle_keepalive, name="coord-keepalive", daemon=True).start()


def descargar_imagen_guia(guia: str):
    """Devuelve (bytes, content_type) usando la sesión guardada."""
    s = _obtener_sesion()
    r = s.get(IMAGEN_URL.format(guia=guia), timeout=TIMEOUT, headers={"Referer": MAIN_URL})
    tipo = r.headers.get("Content-Type", "").lower()
    if r.ok and (tipo.startswith("image/") or "pdf" in tipo) and r.content:
        _registrar_estado(True)
        return r.content, tipo

    logger.warning(
        "Imagen guía %s: HTTP %s, tipo %s, inicio: %s",
        guia, r.status_code, tipo, r.text[:300].replace("\n", " "),
    )
    activa = _sesion_activa(s)
    _registrar_estado(activa)
    if activa is False:
        raise RuntimeError(MSG_CADUCADA)
    raise RuntimeError("Coordinadora no devolvió la imagen de esta guía (puede no tener imagen disponible).")


# --------------------------------------------------------------------------- #
# Búsqueda por factura (rango de fechas del servicio de trazabilidad)
# --------------------------------------------------------------------------- #
def _solo_digitos(v) -> str:
    return re.sub(r"\D", "", str(v).split(".")[0]) if v not in (None, "") else ""


_cache_rango = {"t": 0.0, "items": []}
_lock_rango = threading.Lock()


def _traer_rango() -> list:
    """Líneas de los últimos DIAS_FACTURA días (con caché de unos minutos)."""
    with _lock_rango:
        if _cache_rango["items"] and time.time() - _cache_rango["t"] < CACHE_SEGUNDOS:
            return _cache_rango["items"]

        llave = quote(_id_llave(), safe="!")
        hoy = date.today()
        fecha_ini = (hoy - timedelta(days=DIAS_FACTURA)).strftime("%Y%m%d")
        fecha_fin = hoy.strftime("%Y%m%d")

        items, offset = [], 0
        with requests.Session() as s:
            for _ in range(MAX_PAGINAS):
                url = (
                    f"{BASE_URL}/{llave}/{fecha_ini}/{fecha_fin}/"
                    f"{quote(COMODIN)}/{quote(COMODIN)}/{offset}/{PAGE_SIZE}"
                )
                resp = s.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
                resp.raise_for_status()
                data = resp.json()
                pagina = data.get("items", [])
                items.extend(pagina)
                if not pagina or not data.get("hasMore"):
                    break
                offset += PAGE_SIZE

        items = [_normalizar(i) for i in items]
        logger.info("Rango %s-%s (comodín %r): %s líneas", fecha_ini, fecha_fin, COMODIN, len(items))
        if items:
            _cache_rango.update(t=time.time(), items=items)
        return items


def buscar_por_factura(numero: str):
    """Devuelve (líneas de esa factura, total de líneas del rango)."""
    items = _traer_rango()
    objetivo = _solo_digitos(numero)
    filtradas = [i for i in items if _solo_digitos(i.get("numero factura")) == objetivo]
    return filtradas, len(items)


def _interpretar(texto: str):
    """Devuelve (por_factura, numero). 'f 50460' / 'f50460' / 'factura 50460' fuerzan factura."""
    t = (texto or "").strip()
    m = re.match(r"^(?:factura|fac|f)[\s:#.\-]*(\d+)\s*$", t, re.I)
    if m:
        return True, m.group(1)
    doc = t.split(".")[0].strip()
    return False, (doc if doc.isdigit() else "")


# --------------------------------------------------------------------------- #
# Extraer guías únicas
# --------------------------------------------------------------------------- #
def _fecha(v) -> str:
    return str(v)[:10] if v else ""


def guias_unicas(items: list) -> list:
    """Una sola fila por guía; si se repite, queda la de fecha de factura más reciente."""
    por_guia = {}
    for it in items:
        guia = _solo_digitos(it.get("numero guia"))
        if not guia:
            continue
        fecha = _fecha(it.get("fecha factura") or it.get("fecha dcto") or it.get("fecha despacho"))
        actual = por_guia.get(guia)
        if actual is None or fecha > actual["fecha"]:
            por_guia[guia] = {
                "guia": guia,
                "fecha": fecha,
                "factura": str(it.get("numero factura") or "").strip(),
                "pedido": str(it.get("documento pd") or "").strip(),
                "cliente": it.get("nombre tercero") or "",
                "vendedor": it.get("nombre vendedor") or "",
            }
    return sorted(por_guia.values(), key=lambda g: g["fecha"], reverse=True)


# --------------------------------------------------------------------------- #
# Pantallas
# --------------------------------------------------------------------------- #
def _tipo(d: dict) -> str:
    return "Factura" if d.get("por") == "factura" else "Pedido"


def _etiqueta(g: dict) -> str:
    partes = [f"Guía {g['guia']}"]
    if g["factura"]:
        partes.append(f"Fact. {g['factura']}")
    if g["fecha"]:
        partes.append(g["fecha"])
    return " - ".join(partes)


def _pantalla_guias(d: dict):
    texto = f"📦 {_tipo(d)} <b>{esc(d['doc'])}</b> tiene varias guías.\nSelecciona una:"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(_etiqueta(g), callback_data=f"gu_sel_{i}")]
        for i, g in enumerate(d["guias"][:MAX_BOTONES])
    ])
    return texto, teclado


def _pantalla_acciones(d: dict, g: dict):
    texto = f"📦 {_tipo(d)} <b>{esc(d['doc'])}</b>\n"
    if d.get("por") == "factura":
        texto += f"• <b>Pedido:</b> {esc(g['pedido']) or 'N/A'}\n"
    texto += (
        f"• <b>Cliente:</b> {esc(g['cliente']) or 'N/A'}\n"
        f"• <b>Vendedor:</b> {esc(g['vendedor']) or 'N/A'}\n"
        f"• <b>Factura:</b> {esc(g['factura']) or 'N/A'}\n"
        f"• <b>Fecha factura:</b> {esc(g['fecha']) or 'N/A'}\n"
        f"• <b>Guía:</b> <code>{esc(g['guia'])}</code>"
    )
    filas = [
        [InlineKeyboardButton("📍 Trazabilidad de la guía", url=RASTREO_URL.format(guia=g["guia"]))],
        [InlineKeyboardButton("🖼️ Descargar imagen de la guía", callback_data="gu_img")],
    ]
    if len(d["guias"]) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a guías", callback_data="gu_back")])
    return texto, InlineKeyboardMarkup(filas)


# --------------------------------------------------------------------------- #
# Handlers de Telegram
# --------------------------------------------------------------------------- #
TEXTO_PEDIR = (
    "📦 <b>Guías Coordinadora</b>\n\n"
    "Escribe el <b>número de pedido</b> o de <b>factura</b>.\n"
    "Para buscar solo por factura escribe, por ejemplo, <code>f 50460</code>."
)


async def iniciar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/GU [número] o botón del menú."""
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_GU")
        await editar(query, TEXTO_PEDIR)
        return ESPERANDO_DOCUMENTO_GU

    registrar_uso_bot(update, "/GU")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(update, context, TEXTO_PEDIR + "\nTambién puedes usar <code>/GU 7374</code>.")
        return ESPERANDO_DOCUMENTO_GU
    return await ejecutar_gu(update, context, " ".join(context.args))


async def recibir_texto_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_gu(update, context, update.message.text)


async def ejecutar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str) -> int:
    por_factura, doc = _interpretar(texto)
    if not doc:
        await responder(
            update, context,
            "⚠️ Escribe solo el número, por ejemplo <code>7374</code> (pedido) o <code>f 50460</code> (factura).",
        )
        return ESPERANDO_DOCUMENTO_GU

    espera = await responder(
        update, context,
        f"⏳ Buscando {'la factura' if por_factura else 'el pedido'} <b>{esc(doc)}</b>…",
    )

    items, por, total_rango = [], "pedido", None
    try:
        if not por_factura:
            items = await asyncio.to_thread(consultar_pedido, doc)
        if not items:
            por = "factura"
            items, total_rango = await asyncio.to_thread(buscar_por_factura, doc)
    except RuntimeError as e:
        logger.error("Configuración: %s", e)
        await espera.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        logger.exception("Error consultando %s para guías", doc)
        await espera.edit_text("⚠️ No pude consultar el servicio ahora. Intenta de nuevo en un momento.")
        return ConversationHandler.END

    if not items:
        if total_rango == 0:
            msg = (
                "⚠️ El servicio no devolvió datos para buscar por factura. "
                "Revisa la configuración (variable NTEX_WS_COMODIN)."
            )
        elif por_factura:
            msg = (
                f"❌ No encontré la factura <b>{esc(doc)}</b> en los últimos {DIAS_FACTURA} días.\n"
                "Puedes escribir otro número."
            )
        else:
            msg = (
                f"❌ No encontré el pedido ni la factura <b>{esc(doc)}</b> "
                f"(facturas de los últimos {DIAS_FACTURA} días).\nPuedes escribir otro número."
            )
        await espera.edit_text(msg, parse_mode=ParseMode.HTML)
        return ESPERANDO_DOCUMENTO_GU

    guias = guias_unicas(items)
    if not guias:
        await espera.edit_text(
            f"ℹ️ {'La factura' if por == 'factura' else 'El pedido'} <b>{esc(doc)}</b> "
            "todavía no tiene guía asignada.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
        )
        return ESPERANDO_DOCUMENTO_GU

    d = {"doc": doc, "por": por, "guias": guias}
    context.user_data["gu"] = d

    if len(guias) == 1:
        d["sel"] = 0
        texto_msg, teclado = _pantalla_acciones(d, guias[0])
        await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
        return ACCIONES_GU

    texto_msg, teclado = _pantalla_guias(d)
    await espera.edit_text(texto_msg, reply_markup=teclado, parse_mode=ParseMode.HTML)
    return SELECCIONANDO_GUIA_GU


async def seleccionar_guia_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("gu")
    if not d:
        return await _sesion_expirada(query)
    try:
        d["sel"] = int(query.data.rsplit("_", 1)[1])
        g = d["guias"][d["sel"]]
    except (ValueError, IndexError):
        return await _sesion_expirada(query)
    texto, teclado = _pantalla_acciones(d, g)
    await editar(query, texto, teclado)
    return ACCIONES_GU


async def acciones_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.get("gu")
    if not d or "sel" not in d:
        await query.answer()
        return await _sesion_expirada(query)

    if query.data == "gu_back":
        await query.answer()
        texto, teclado = _pantalla_guias(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_GUIA_GU

    # gu_img: descargar y enviar la imagen
    g = d["guias"][d["sel"]]
    await query.answer("Descargando imagen…")
    try:
        contenido, tipo = await asyncio.to_thread(descargar_imagen_guia, g["guia"])
    except RuntimeError as e:
        logger.error("Imagen guía %s: %s", g["guia"], e)
        await query.message.reply_text(f"⚠️ {e}")
        return ACCIONES_GU
    except Exception as e:
        logger.exception("Error descargando imagen de la guía %s", g["guia"])
        detalle = f"{type(e).__name__}: {e}"[:300]
        await query.message.reply_text(f"⚠️ No pude descargar la imagen.\nDetalle: {detalle}")
        return ACCIONES_GU

    ext = "pdf" if "pdf" in tipo else ("png" if "png" in tipo else "jpg")
    archivo = io.BytesIO(contenido)
    msg = await query.message.reply_document(
        document=archivo,
        filename=f"guia_{g['guia']}.{ext}",
        caption=f"📦 Guía {g['guia']} - {_tipo(d)} {d['doc']}",
    )
    programar_borrado(context, msg)
    return ACCIONES_GU
