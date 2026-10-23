import asyncio
import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import datos
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
from geo_handlers import consultar_pedido

logger = logging.getLogger(__name__)

# Estados 0-11 ya están en uso (/PV, /IN, /I, /PG).
ESPERANDO_DOCUMENTO_GU, SELECCIONANDO_GUIA_GU, ACCIONES_GU, ELIGIENDO_TIPO_GU = 12, 13, 14, 15

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

# Valores de "tipo de documento" que se prueban en el servicio poniendo la factura como documento
TIPOS_FACTURA = ["FV", "FA", "FC", "FT", "FE", "F", "FACTURA", "13"]


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
# Búsqueda por factura
#   1) hoja de pedidos (Google Sheets): factura -> pedido, y luego se consulta el pedido
#   2) servicio de trazabilidad probando tipos de documento con la factura como documento
# --------------------------------------------------------------------------- #
def _solo_digitos(v) -> str:
    return re.sub(r"\D", "", str(v).split(".")[0]) if v not in (None, "") else ""


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower().replace("_", " ")).strip()


def _col_factura(df):
    """Busca en la hoja la columna con el número de factura."""
    norm = {c: _norm(c) for c in df.columns}
    for p in ("numero factura", "numero de factura", "nro factura", "num factura", "no factura",
              "n factura", "documento factura", "factura"):
        for c, n in norm.items():
            if n == p:
                return c
    for c, n in norm.items():
        if "factura" in n and not any(x in n for x in ("fecha", "estado", "valor", "total", "url", "tipo")):
            return c
    return None


async def _pedidos_por_factura_hoja(doc: str):
    """Devuelve (lista de pedidos que tienen esa factura en la hoja, notas de diagnóstico)."""
    try:
        df = await datos.pedidos.obtener()
    except Exception as e:
        return [], [f"hoja: error {type(e).__name__}"]
    if df is None or df.empty:
        return [], ["hoja: sin datos"]
    col = _col_factura(df)
    if not col:
        cands = [c for c in df.columns if "factura" in _norm(c)]
        return [], [f"hoja: no hay columna con el número de factura (columnas con 'factura': {cands or 'ninguna'})"]
    if "Documento Pd" not in df.columns:
        return [], ["hoja: falta la columna Documento Pd"]

    serie = df[col].astype(str).str.split(".").str[0].str.replace(r"\D", "", regex=True)
    peds = df.loc[serie == _solo_digitos(doc), "Documento Pd"].astype(str).str.split(".").str[0].str.strip()
    peds = [p for p in dict.fromkeys(peds) if p.isdigit()]
    return peds, [f"hoja: columna {col!r}, {len(peds)} pedido(s) con esa factura"]


_tipo_factura_ok = None


def _probar_tipos_factura(doc: str):
    """Prueba tipos de documento del servicio con la factura como documento (en paralelo)."""
    global _tipo_factura_ok
    objetivo = _solo_digitos(doc)
    diag = []
    tipos = [_tipo_factura_ok] if _tipo_factura_ok else TIPOS_FACTURA

    ex = ThreadPoolExecutor(max_workers=len(tipos))
    futs = {ex.submit(consultar_pedido, doc, t): t for t in tipos}
    hechos, _ = wait(futs, timeout=30)
    ex.shutdown(wait=False)

    for f in hechos:
        t = futs[f]
        try:
            its = f.result()
        except Exception as e:
            diag.append(f"servicio tipo {t!r}: error {type(e).__name__}")
            continue
        coincide = [i for i in its if _solo_digitos(i.get("numero factura")) == objetivo]
        diag.append(f"servicio tipo {t!r}: {len(its)} líneas, {len(coincide)} de esa factura")
        if coincide:
            _tipo_factura_ok = t
            return coincide, diag
    sin_resp = [futs[f] for f in futs if f not in hechos]
    if sin_resp:
        diag.append(f"servicio sin respuesta a tiempo: {sin_resp}")
    return [], diag


async def buscar_por_factura(doc: str):
    """Devuelve (líneas de esa factura, notas de diagnóstico)."""
    objetivo = _solo_digitos(doc)
    peds, diag = await _pedidos_por_factura_hoja(doc)

    items = []
    for ped in peds[:5]:
        try:
            its = await asyncio.to_thread(consultar_pedido, ped)
        except RuntimeError:
            raise
        except Exception as e:
            diag.append(f"pedido {ped}: error {type(e).__name__}")
            continue
        propios = [i for i in its if _solo_digitos(i.get("numero factura")) == objetivo]
        items.extend(propios or its)
    if items:
        return items, diag

    its, d2 = await asyncio.to_thread(_probar_tipos_factura, doc)
    return its, diag + d2


def _bloque_diag(update: Update, diag: list) -> str:
    """El detalle técnico solo lo ve el dueño del bot."""
    if diag and update.effective_user and update.effective_user.id == MI_TELEGRAM_ID:
        return f"\n\n<b>Diagnóstico:</b>\n<pre>{esc(chr(10).join(diag))[:1500]}</pre>"
    return ""


def _interpretar(texto: str, modo=None):
    """Devuelve (por_factura, numero).
    modo = 'pedido' / 'factura' (elegido con los botones) o None (atajo /gu 7374: automático).
    En automático, 'f 50460' / 'f50460' / 'factura 50460' fuerzan factura."""
    t = (texto or "").strip()
    m = re.match(r"^(?:factura|fac|f)[\s:#.\-]*(\d+)\s*$", t, re.I)
    if m and modo != "pedido":
        return True, m.group(1)
    doc = t.split(".")[0].strip()
    return modo == "factura", (doc if doc.isdigit() else "")


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
TEXTO_TIPO = "📦 <b>Guías Coordinadora</b>\n\n¿Cómo quieres buscar la guía?"


def _teclado_tipo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📦 Por pedido", callback_data="gu_tipo_pedido"),
        InlineKeyboardButton("🧾 Por factura", callback_data="gu_tipo_factura"),
    ]])


def _teclado_cambiar() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cambiar (pedido / factura)", callback_data="gu_tipo_menu")]])


def _texto_pedir(modo: str) -> str:
    if modo == "factura":
        return "🧾 <b>Búsqueda por factura</b>\n\nEscribe el <b>número de factura</b>:"
    return "📦 <b>Búsqueda por pedido</b>\n\nEscribe el <b>número de pedido</b>:"


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
    filas.append([InlineKeyboardButton("🔎 Nueva búsqueda", callback_data="gu_nuevo")])
    return texto, InlineKeyboardMarkup(filas)


# --------------------------------------------------------------------------- #
# Handlers de Telegram
# --------------------------------------------------------------------------- #
async def iniciar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/GU [número] o botón del menú. Sin número muestra los botones Pedido / Factura."""
    query = update.callback_query
    context.user_data["gu_modo"] = None
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_GU")
        await editar(query, TEXTO_TIPO, _teclado_tipo())
        return ELIGIENDO_TIPO_GU

    registrar_uso_bot(update, "/GU")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(update, context, TEXTO_TIPO, reply_markup=_teclado_tipo())
        return ELIGIENDO_TIPO_GU
    # Atajo: /GU 7374 o /GU f 50460 (búsqueda automática)
    return await ejecutar_gu(update, context, " ".join(context.args), None)


async def elegir_tipo_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botones 'Por pedido' / 'Por factura' / 'Cambiar'."""
    query = update.callback_query
    await query.answer()
    accion = query.data.rsplit("_", 1)[1]  # pedido | factura | menu

    if accion == "menu":
        context.user_data["gu_modo"] = None
        await editar(query, TEXTO_TIPO, _teclado_tipo())
        return ELIGIENDO_TIPO_GU

    context.user_data["gu_modo"] = accion
    await editar(query, _texto_pedir(accion), _teclado_cambiar())
    return ESPERANDO_DOCUMENTO_GU


async def recibir_texto_gu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    modo = context.user_data.get("gu_modo")
    return await ejecutar_gu(update, context, update.message.text, modo)


async def ejecutar_gu(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, modo=None) -> int:
    por_factura, doc = _interpretar(texto, modo)
    if not doc:
        await responder(
            update, context,
            "⚠️ Escribe solo el número, por ejemplo <code>7374</code>."
            if modo else
            "⚠️ Escribe solo el número, por ejemplo <code>7374</code> (pedido) o <code>f 50460</code> (factura).",
            reply_markup=_teclado_cambiar() if modo else None,
        )
        return ESPERANDO_DOCUMENTO_GU

    espera = await responder(
        update, context,
        f"⏳ Buscando {'la factura' if por_factura else 'el pedido'} <b>{esc(doc)}</b>…",
    )

    items, por, diag = [], "pedido", []
    try:
        if not por_factura:
            items = await asyncio.to_thread(consultar_pedido, doc)
        if not items and modo != "pedido":
            por = "factura"
            items, diag = await buscar_por_factura(doc)
    except RuntimeError as e:
        logger.error("Configuración: %s", e)
        await espera.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        logger.exception("Error consultando %s para guías", doc)
        await espera.edit_text("⚠️ No pude consultar el servicio ahora. Intenta de nuevo en un momento.")
        return ConversationHandler.END

    if diag:
        logger.info("Búsqueda de factura %s: %s", doc, " | ".join(diag))

    teclado_error = _teclado_cambiar() if modo else None
    if not items:
        if por_factura:
            msg = f"❌ No encontré la factura <b>{esc(doc)}</b>.\nPuedes escribir otro número."
        elif modo == "pedido":
            msg = f"❌ No encontré el pedido <b>{esc(doc)}</b>.\nPuedes escribir otro número."
        else:
            msg = f"❌ No encontré el pedido ni la factura <b>{esc(doc)}</b>.\nPuedes escribir otro número."
        msg += _bloque_diag(update, diag)
        await espera.edit_text(msg, parse_mode=ParseMode.HTML, reply_markup=teclado_error)
        return ESPERANDO_DOCUMENTO_GU

    guias = guias_unicas(items)
    if not guias:
        await espera.edit_text(
            f"ℹ️ {'La factura' if por == 'factura' else 'El pedido'} <b>{esc(doc)}</b> "
            "todavía no tiene guía asignada.\nPuedes escribir otro número.",
            parse_mode=ParseMode.HTML,
            reply_markup=teclado_error,
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

    if query.data == "gu_nuevo":
        await query.answer()
        context.user_data["gu_modo"] = None
        # El mensaje con la guía se conserva: la nueva búsqueda sale en un mensaje aparte
        msg = await query.message.reply_text(TEXTO_TIPO, reply_markup=_teclado_tipo(), parse_mode=ParseMode.HTML)
        programar_borrado(context, msg)
        return ELIGIENDO_TIPO_GU

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
