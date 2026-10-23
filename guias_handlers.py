import asyncio
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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

# Índice de facturas (se arma leyendo pedidos con el web service de trazabilidad)
PEDIDO_DESDE = int(os.environ.get("NTEX_PEDIDO_DESDE", "4000"))
PEDIDO_TOPE = int(os.environ.get("NTEX_PEDIDO_TOPE", "9000"))
HILOS = int(os.environ.get("NTEX_SCAN_HILOS", "6"))
BLOQUE = 200               # pedidos por bloque al armar el índice
COLA_ATRAS = 400           # al refrescar se vuelven a leer los últimos pedidos...
COLA_ADELANTE = 150        # ...y estos números nuevos por encima del máximo
REFRESCO_SEGUNDOS = 20 * 60
ESPERA_ENTRE_COLAS = 60    # mínimo entre lecturas rápidas disparadas por una búsqueda sin resultado


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
# Búsqueda por factura: índice factura -> pedido -> guía armado con el web service
# --------------------------------------------------------------------------- #
def _solo_digitos(v) -> str:
    return re.sub(r"\D", "", str(v).split(".")[0]) if v not in (None, "") else ""


CAMPOS = (
    "numero factura", "numero guia", "fecha factura", "fecha dcto",
    "fecha despacho", "nombre tercero", "nombre vendedor",
)

INDICE = {
    "por_pedido": {},   # pedido(int) -> lista de líneas con factura
    "listo": False,     # primera construcción terminada
    "error": "",
    "progreso": 0,      # último pedido leído en la construcción inicial
    "max": 0,           # pedido más alto con datos
    "t_cola": 0.0,      # última lectura rápida de pedidos recientes
    "t_listo": 0.0,
    "errores": 0,
}
_lock_ind = threading.Lock()
_lock_escaneo = threading.Lock()  # una sola lectura masiva a la vez


def _leer_pedido(n: int):
    """Devuelve (n, None) si falló, o (n, (total_lineas, lineas_con_factura))."""
    try:
        items = consultar_pedido(str(n))
    except Exception as e:
        logger.debug("Pedido %s: error %s", n, e)
        return n, None
    filas = []
    for it in items:
        fac = _solo_digitos(it.get("numero factura"))
        if not fac:
            continue
        fila = {k: it.get(k) for k in CAMPOS}
        fila["numero factura"] = fac
        fila["documento pd"] = str(n)
        filas.append(fila)
    return n, (len(items), filas)


def _escanear_rango(desde: int, hasta: int):
    """Lee los pedidos desde..hasta. Devuelve (máximo con datos, cantidad de errores)."""
    nuevos, max_enc, errores = {}, 0, 0
    with ThreadPoolExecutor(max_workers=max(1, HILOS)) as ex:
        for n, res in ex.map(_leer_pedido, range(desde, hasta + 1)):
            if res is None:
                errores += 1
                continue
            total, filas = res
            if total:
                max_enc = max(max_enc, n)
            nuevos[n] = filas
    with _lock_ind:
        INDICE["por_pedido"].update(nuevos)
        INDICE["errores"] += errores
    return max_enc, errores


def _construir_indice() -> None:
    """Construcción inicial: bloques desde PEDIDO_DESDE hasta que dejan de aparecer pedidos."""
    inicio = time.time()
    n, vacios, max_total, fallos_seguidos = PEDIDO_DESDE, 0, 0, 0
    while n <= PEDIDO_TOPE:
        fin = min(n + BLOQUE - 1, PEDIDO_TOPE)
        mx, err = _escanear_rango(n, fin)
        INDICE["progreso"] = fin
        if err >= (fin - n + 1):
            fallos_seguidos += 1
            if fallos_seguidos >= 2:
                INDICE["error"] = "el servicio de trazabilidad no respondió"
                logger.error("Índice de facturas: el servicio no respondió, se cancela")
                return
        else:
            fallos_seguidos = 0
        if mx:
            max_total = max(max_total, mx)
            vacios = 0
        else:
            vacios += 1
        if max_total and vacios >= 2:
            break
        n = fin + 1

    with _lock_ind:
        INDICE["max"] = max_total
        INDICE["listo"] = True
        INDICE["error"] = ""
        INDICE["t_listo"] = time.time()
        facturas = sum(len(v) for v in INDICE["por_pedido"].values())
    logger.info(
        "Índice de facturas listo: pedidos %s-%s, %s líneas con factura, %.0f s",
        PEDIDO_DESDE, max_total, facturas, time.time() - inicio,
    )


def _refrescar_cola() -> None:
    """Vuelve a leer los pedidos más recientes (nuevas facturas o guías recién asignadas)."""
    if not _lock_escaneo.acquire(blocking=False):
        return
    try:
        INDICE["t_cola"] = time.time()
        desde = max(PEDIDO_DESDE, INDICE["max"] - COLA_ATRAS)
        hasta = min(PEDIDO_TOPE, INDICE["max"] + COLA_ADELANTE)
        mx, _ = _escanear_rango(desde, hasta)
        if mx > INDICE["max"]:
            INDICE["max"] = mx
    except Exception:
        logger.exception("Error refrescando el índice de facturas")
    finally:
        _lock_escaneo.release()


def _bucle_indice() -> None:
    time.sleep(45)  # deja arrancar el bot y los demás servicios
    while True:
        try:
            if not INDICE["listo"]:
                with _lock_escaneo:
                    _construir_indice()
                if not INDICE["listo"]:
                    time.sleep(120)  # reintenta más tarde
                    continue
            else:
                _refrescar_cola()
        except Exception:
            logger.exception("Error en el índice de facturas")
        time.sleep(REFRESCO_SEGUNDOS)


threading.Thread(target=_bucle_indice, name="indice-facturas", daemon=True).start()


def _filtrar_factura(objetivo: str) -> list:
    with _lock_ind:
        listas = list(INDICE["por_pedido"].values())
    return [f for filas in listas for f in filas if f["numero factura"] == objetivo]


def buscar_por_factura(doc: str):
    """Devuelve (líneas de esa factura, índice_listo)."""
    if not INDICE["listo"]:
        return [], False
    objetivo = _solo_digitos(doc)
    res = _filtrar_factura(objetivo)
    if not res and time.time() - INDICE["t_cola"] > ESPERA_ENTRE_COLAS:
        _refrescar_cola()  # puede ser una factura nueva
        res = _filtrar_factura(objetivo)
    return res, True


def _estado_indice() -> str:
    if INDICE["listo"]:
        hace = int((time.time() - INDICE["t_listo"]) / 60)
        return f"índice listo (pedidos {PEDIDO_DESDE}-{INDICE['max']}), armado hace {hace} min"
    if INDICE["error"]:
        return f"índice con error: {INDICE['error']}"
    return f"índice en construcción (voy en el pedido {INDICE['progreso'] or PEDIDO_DESDE})"


def _bloque_diag(update: Update) -> str:
    """El detalle técnico solo lo ve el dueño del bot."""
    if update.effective_user and update.effective_user.id == MI_TELEGRAM_ID:
        extra = f"\nconsultas fallidas: {INDICE['errores']}" if INDICE["errores"] else ""
        return f"\n\n<b>Diagnóstico:</b>\n<pre>{esc(_estado_indice() + extra)}</pre>"
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

    items, por, indice_listo = [], "pedido", True
    try:
        if not por_factura:
            items = await asyncio.to_thread(consultar_pedido, doc)
        if not items and modo != "pedido":
            por = "factura"
            items, indice_listo = await asyncio.to_thread(buscar_por_factura, doc)
    except RuntimeError as e:
        logger.error("Configuración: %s", e)
        await espera.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        logger.exception("Error consultando %s para guías", doc)
        await espera.edit_text("⚠️ No pude consultar el servicio ahora. Intenta de nuevo en un momento.")
        return ConversationHandler.END

    teclado_error = _teclado_cambiar() if modo else None
    if not items:
        if por == "factura" and not indice_listo:
            msg = (
                "⏳ Todavía estoy preparando el índice de facturas con el servicio de trazabilidad. "
                "Tarda unos minutos después de cada reinicio. Inténtalo de nuevo en un rato "
                "o busca por pedido."
            )
            if INDICE["error"]:
                msg = (
                    "⚠️ No pude armar el índice de facturas: el servicio de trazabilidad no respondió. "
                    "Busca por pedido por ahora."
                )
        elif por_factura:
            msg = (
                f"❌ No encontré la factura <b>{esc(doc)}</b> entre los pedidos "
                f"{PEDIDO_DESDE} a {INDICE['max']}.\nPuedes escribir otro número."
            )
        elif modo == "pedido":
            msg = f"❌ No encontré el pedido <b>{esc(doc)}</b>.\nPuedes escribir otro número."
        else:
            msg = f"❌ No encontré el pedido ni la factura <b>{esc(doc)}</b>.\nPuedes escribir otro número."
        if por == "factura":
            msg += _bloque_diag(update)
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
