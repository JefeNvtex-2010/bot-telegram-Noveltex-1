import asyncio
import io
import logging
import os
import re
import threading
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from handlers import (
    MAX_BOTONES,
    _sesion_expirada,
    editar,
    esc,
    programar_borrado,
    registrar_uso_bot,
    responder,
)
# Ajusta el nombre del módulo al archivo donde tienes consultar_pedido (el de /PG)
from pg_handlers import consultar_pedido

logger = logging.getLogger(__name__)

# Estados 0-11 ya están en uso (/PV, /IN, /I, /PG).
ESPERANDO_DOCUMENTO_GU, SELECCIONANDO_GUIA_GU, ACCIONES_GU = 12, 13, 14

LOGIN_URL = "https://guias.coordinadora.com/site/index.php"
IMAGEN_URL = "https://guias.coordinadora.com/site/getimagen.php?codigo_remision={guia}"
RASTREO_URL = (
    "https://coordinadora.com/rastreo/rastreo-de-guia/detalle-de-rastreo-de-guia/?guia={guia}"
)
TIMEOUT = 60
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


# --------------------------------------------------------------------------- #
# Login en guias.coordinadora.com (sesión reutilizable)
# --------------------------------------------------------------------------- #
class _FormParser(HTMLParser):
    """Recoge los formularios de la página de login con sus inputs."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._actual = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._actual = {"action": a.get("action", ""), "inputs": []}
            self.forms.append(self._actual)
        elif tag == "input" and self._actual is not None:
            self._actual["inputs"].append(
                {"name": a.get("name"), "type": (a.get("type") or "text").lower(), "value": a.get("value", "")}
            )

    def handle_endtag(self, tag):
        if tag == "form":
            self._actual = None


_lock = threading.Lock()
_sesion = None


def _credenciales() -> tuple:
    usuario = os.environ.get("COORD_USUARIO", "").strip()
    clave = os.environ.get("COORD_CLAVE", "").strip()
    if not usuario or not clave:
        raise RuntimeError("Faltan las variables de entorno COORD_USUARIO y COORD_CLAVE en Render.")
    return usuario, clave


def _login() -> requests.Session:
    usuario, clave = _credenciales()
    s = requests.Session()
    s.headers.update(UA)
    r = s.get(LOGIN_URL, timeout=TIMEOUT)
    r.raise_for_status()

    parser = _FormParser()
    parser.feed(r.text)
    form = next((f for f in parser.forms if any(i["type"] == "password" for i in f["inputs"])), None)
    if not form:
        raise RuntimeError("No encontré el formulario de login de Coordinadora.")

    datos, campo_user = {}, None
    for i in form["inputs"]:
        if not i["name"]:
            continue
        if i["type"] == "password":
            datos[i["name"]] = clave
        elif i["type"] == "text" and campo_user is None:
            campo_user = i["name"]
            datos[i["name"]] = usuario
        elif i["type"] in ("hidden",):
            datos[i["name"]] = i["value"]

    destino = urljoin(r.url, form["action"]) if form["action"] else r.url
    resp = s.post(destino, data=datos, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    logger.info("Login Coordinadora: HTTP %s, url final %s", resp.status_code, resp.url)
    return s


def _obtener_sesion(forzar: bool = False) -> requests.Session:
    global _sesion
    with _lock:
        if _sesion is None or forzar:
            _sesion = _login()
        return _sesion


def descargar_imagen_guia(guia: str):
    """Devuelve (bytes, content_type). Reintenta una vez con login nuevo si la sesión expiró."""
    for intento in (0, 1):
        s = _obtener_sesion(forzar=bool(intento))
        r = s.get(IMAGEN_URL.format(guia=guia), timeout=TIMEOUT)
        tipo = r.headers.get("Content-Type", "").lower()
        if r.ok and (tipo.startswith("image/") or "pdf" in tipo) and r.content:
            return r.content, tipo
        logger.warning("Imagen guía %s intento %s: HTTP %s, tipo %s", guia, intento, r.status_code, tipo)
    raise RuntimeError("Coordinadora no devolvió la imagen (login rechazado o guía sin imagen).")


# --------------------------------------------------------------------------- #
# Extraer guías únicas del pedido
# --------------------------------------------------------------------------- #
def _limpiar_guia(v) -> str:
    return re.sub(r"\D", "", str(v).split(".")[0]) if v not in (None, "") else ""


def _fecha(v) -> str:
    return str(v)[:10] if v else ""


def guias_unicas(items: list) -> list:
    """Una sola fila por guía; si se repite, queda la de fecha de factura más reciente."""
    por_guia = {}
    for it in items:
        guia = _limpiar_guia(it.get("numero guia"))
        if not guia:
            continue
        fecha = _fecha(it.get("fecha factura") or it.get("fecha dcto") or it.get("fecha despacho"))
        actual = por_guia.get(guia)
        if actual is None or fecha > actual["fecha"]:
            por_guia[guia] = {
                "guia": guia,
                "fecha": fecha,
                "factura": str(it.get("numero factura") or "").strip(),
                "cliente": it.get("nombre tercero") or "",
                "vendedor": it.get("nombre vendedor") or "",
            }
    return sorted(por_guia.values(), key=lambda g: g["fecha"], reverse=True)


# --------------------------------------------------------------------------- #
# Pantallas
# --------------------------------------------------------------------------- #
def _etiqueta(g: dict) -> str:
    partes = [f"Guía {g['guia']}"]
    if g["factura"]:
        partes.append(f"Fact. {g['factura']}")
    if g["fecha"]:
        partes.append(g["fecha"])
    return " - ".join(partes)


def _pantalla_guias(d: dict):
    texto = f"📦 Pedido <b>{esc(d['doc'])}</b> tiene varias guías.\nSelecciona una:"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(_etiqueta(g), callback_data=f"gu_sel_{i}")]
        for i, g in enumerate(d["guias"][:MAX_BOTONES])
    ])
    return texto, teclado


def _pantalla_acciones(d: dict, g: dict):
    texto = (
        f"📦 Pedido <b>{esc(d['doc'])}</b>\n"
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
async def iniciar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/GU [número] o botón del menú."""
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_GU")
        await editar(query, "📦 <b>Guías Coordinadora</b>\n\nEscribe el <b>número de pedido</b>:")
        return ESPERANDO_DOCUMENTO_GU

    registrar_uso_bot(update, "/GU")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "📦 <b>Guías Coordinadora</b>\n\nEscribe el <b>número de pedido</b> (o usa <code>/GU 7374</code>):",
        )
        return ESPERANDO_DOCUMENTO_GU
    return await ejecutar_gu(update, context, context.args[0])


async def recibir_texto_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_gu(update, context, update.message.text)


async def ejecutar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str) -> int:
    doc = texto.strip().split(".")[0].strip()
    if not doc.isdigit():
        await responder(update, context, "⚠️ Escribe solo el <b>número de pedido</b>, por ejemplo <code>7374</code>.")
        return ESPERANDO_DOCUMENTO_GU

    espera = await responder(update, context, f"⏳ Buscando la guía del pedido <b>{esc(doc)}</b>…")
    try:
        items = await asyncio.to_thread(consultar_pedido, doc)
    except RuntimeError as e:
        logger.error("Configuración: %s", e)
        await espera.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        logger.exception("Error consultando pedido %s para guías", doc)
        await espera.edit_text("⚠️ No pude consultar el pedido ahora. Intenta de nuevo en un momento.")
        return ConversationHandler.END

    if not items:
        await espera.edit_text(
            f"❌ No se encontró el documento: <b>{esc(doc)}</b>.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
        )
        return ESPERANDO_DOCUMENTO_GU

    guias = guias_unicas(items)
    if not guias:
        await espera.edit_text(
            f"ℹ️ El pedido <b>{esc(doc)}</b> todavía no tiene guía asignada.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
        )
        return ESPERANDO_DOCUMENTO_GU

    d = {"doc": doc, "guias": guias}
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
        await query.message.reply_text(f"⚠️ {e}")
        return ACCIONES_GU
    except Exception:
        logger.exception("Error descargando imagen de la guía %s", g["guia"])
        await query.message.reply_text("⚠️ No pude descargar la imagen ahora. Intenta de nuevo en un momento.")
        return ACCIONES_GU

    ext = "pdf" if "pdf" in tipo else ("png" if "png" in tipo else "jpg")
    archivo = io.BytesIO(contenido)
    msg = await query.message.reply_document(
        document=archivo,
        filename=f"guia_{g['guia']}.{ext}",
        caption=f"📦 Guía {g['guia']} - Pedido {d['doc']}",
    )
    programar_borrado(context, msg)
    return ACCIONES_GU
