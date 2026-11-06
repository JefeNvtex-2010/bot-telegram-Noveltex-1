"""Flujo guiado /etiqueta: ID operario (+ PIN opcional) -> impresora -> referencia -> color -> cantidad (metros.centímetros) -> nº de etiquetas.

Referencias, colores e item salen del catálogo (pestaña "Listado precios"), ver datos.catalogo.
"""

import asyncio
import csv
import io
import logging
import os
import re
import time
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import datos
from etiquetas import IMPRESORAS_ZEBRA, imprimir_etiqueta
from handlers import _teclado_menu, menu_inicio, registrar_uso_bot

logger = logging.getLogger(__name__)

# Estados como texto para no chocar con los de los demás módulos
ESPERANDO_REF_ET = "et_esperando_ref"
ELIGIENDO_IMPRESORA_ET = "et_eligiendo_impresora"
SELECCIONANDO_REF_ET = "et_seleccionando_ref"
SELECCIONANDO_COLOR_ET = "et_seleccionando_color"
ESPERANDO_METROS_ET = "et_esperando_metros"
ESPERANDO_COPIAS_ET = "et_esperando_copias"
PREGUNTANDO_OTRA_ET = "et_preguntando_otra"  # estado para el bucle de "otra etiqueta"

MAX_BOTONES = 40
MAX_METROS = 999
MAX_COPIAS = 50

# Protección contra adivinar ID/PIN: tras MAX_FALLOS errores seguidos se bloquea el acceso
MAX_FALLOS = 5
SEGUNDOS_BLOQUEO = 300

# Impresora de respaldo si por alguna razón no hay una elegida (IDs: "81", "82", "83")
IMPRESORA_DEFECTO = os.environ.get("IMPRESORA_DEFECTO", "83").strip()
if IMPRESORA_DEFECTO not in IMPRESORAS_ZEBRA:
    IMPRESORA_DEFECTO = "83"

# Google Sheet AUDITORIA_BOT, pestaña ETIQUETAS (columnas: OPERARIO | ID | PIN opcional)
SHEET_ID_OPERARIOS = os.environ.get(
    "SHEET_ID_AUDITORIA", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
)
PESTANA_OPERARIOS = "ETIQUETAS"


def _cargar_operarios() -> dict:
    """Lee la pestaña ETIQUETAS y devuelve {id: (nombre, pin)}. Bloqueante: usar con to_thread.

    El PIN es la tercera columna y es opcional: si está vacío (o no existe la columna)
    solo se pide el ID.
    """
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID_OPERARIOS}"
        f"/gviz/tq?tqx=out:csv&sheet={PESTANA_OPERARIOS}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        texto = r.read().decode("utf-8-sig")

    # Si la hoja no es accesible, Google devuelve una página HTML de login
    if texto.lstrip().lower().startswith("<"):
        raise RuntimeError("La hoja no es accesible (revisa que esté compartida con el enlace).")

    operarios = {}
    filas = list(csv.reader(io.StringIO(texto)))
    for fila in filas[1:]:  # se salta el encabezado OPERARIO | ID | PIN
        if len(fila) < 2:
            continue
        nombre = fila[0].strip()
        id_op = re.sub(r"\D", "", fila[1])
        pin = re.sub(r"\D", "", fila[2]) if len(fila) > 2 else ""
        if nombre and id_op:
            operarios[id_op] = (nombre, pin)
    return operarios


def _operario_actual(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Devuelve los datos del operario validado y su impresora (para conservarlos entre pasos)."""
    et = context.user_data.get("et", {})
    return {k: et[k] for k in ("operario", "id_operario", "impresora") if k in et}


# --------------------------------------------------------------------------- #
# Teclados de navegación: "↩️ Atrás" y "🏠 Menú principal"
# --------------------------------------------------------------------------- #
def _boton_atras_et() -> InlineKeyboardButton:
    return InlineKeyboardButton("↩️ Atrás", callback_data="et_atras")


def _boton_menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton("🏠 Menú principal", callback_data="et_menu")


def _boton_menu_et():
    """Fila con solo el botón de menú principal."""
    return [_boton_menu_btn()]


def _fila_nav_et():
    """Fila con Atrás + Menú principal."""
    return [_boton_atras_et(), _boton_menu_btn()]


def _teclado_menu_et() -> InlineKeyboardMarkup:
    """Solo menú principal (para el primer paso, donde no hay nada atrás)."""
    return InlineKeyboardMarkup([_boton_menu_et()])


def _teclado_nav_et() -> InlineKeyboardMarkup:
    """Atrás + Menú principal."""
    return InlineKeyboardMarkup([_fila_nav_et()])


def _teclado_impresoras(atras: bool = True) -> InlineKeyboardMarkup:
    fila = [
        InlineKeyboardButton(f"🖨️ {id_imp}", callback_data=f"et_imp_{id_imp}")
        for id_imp in IMPRESORAS_ZEBRA
    ]
    return InlineKeyboardMarkup([fila, _fila_nav_et() if atras else _boton_menu_et()])


def _teclado_refs(refs: list) -> InlineKeyboardMarkup:
    botones = [[InlineKeyboardButton(r, callback_data=f"et_ref_{i}")] for i, r in enumerate(refs)]
    botones.append(_fila_nav_et())
    return InlineKeyboardMarkup(botones)


def _teclado_colores(colores: list) -> InlineKeyboardMarkup:
    botones, fila = [], []
    for i, c in enumerate(colores):
        fila.append(InlineKeyboardButton(c, callback_data=f"et_col_{i}"))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    botones.append(_fila_nav_et())
    return InlineKeyboardMarkup(botones)


def _texto_pedir_cantidad(et: dict) -> str:
    return (
        f"Referencia: {et['ref']}\nItem: {et['item']}\n"
        f"Color: {et.get('color_nombre', et['color'])}\n\n"
        "Introduce la cantidad:"
    )


async def volver_menu_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cierra el flujo de etiquetas y muestra el menú principal (equivale a /i).

    Funciona desde cualquier paso: en main.py se registra como fallback del
    ConversationHandler con el patrón ^et_menu$.
    """
    context.user_data.pop("et", None)  # se cierra la sesión del operario
    q = update.callback_query
    texto_actual = (q.message.text or "") if q and q.message else ""

    if texto_actual.startswith("✅ Etiqueta enviada"):
        # Se conserva el resumen de la etiqueta como registro (solo se quitan
        # los botones) y el menú sale en un mensaje nuevo debajo.
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        user = update.effective_user
        teclado = _teclado_menu(user.id if user else 0)
        if teclado is None:
            texto = "⛔ Todavía no tienes funciones habilitadas. Escribe /miid y pásale tu ID al administrador."
        else:
            texto = "👋 ¡Hola! Selecciona una opción del menú principal para comenzar:"
        await q.message.reply_text(texto, reply_markup=teclado)
        return ConversationHandler.END

    return await menu_inicio(update, context)


async def volver_paso_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Botón "↩️ Atrás": regresa al paso anterior según el punto donde va el operario.

    Funciona desde cualquier paso: en main.py se registra como fallback del
    ConversationHandler con el patrón ^et_atras$.
    El paso actual se deduce de lo que ya hay guardado en context.user_data["et"].
    """
    et = context.user_data.get("et", {})

    # 1) Pidiendo ID/PIN, o eligiendo impresora -> volver a pedir el ID (cierra la sesión)
    if "operario" not in et or "impresora" not in et:
        context.user_data["et"] = {}
        await _responder(
            update,
            "🏷️ Etiqueta Zebra\n\nEscribe tu ID de operario:",
            _teclado_menu_et(),
        )
        return ESPERANDO_REF_ET

    # 2) Pidiendo la referencia -> volver a la impresora
    if "ref" not in et and "refs" not in et:
        context.user_data["et"] = {k: et[k] for k in ("operario", "id_operario") if k in et}
        await _responder(
            update,
            "🖨️ ¿En qué impresora vas a imprimir?",
            _teclado_impresoras(),
        )
        return ELIGIENDO_IMPRESORA_ET

    # 3) En la lista de referencias -> volver a pedir la referencia
    if "ref" not in et:
        context.user_data["et"] = _operario_actual(context)
        await _responder(update, "Escribe la referencia:", _teclado_nav_et())
        return ESPERANDO_REF_ET

    # 4) Eligiendo color -> volver a la lista de referencias (si la había) o pedir la referencia
    if "color" not in et:
        refs = et.get("refs")
        if refs:
            context.user_data["et"] = {**_operario_actual(context), "refs": refs}
            await _responder(
                update,
                "Elige una de las referencias encontradas:",
                _teclado_refs(refs),
            )
            return SELECCIONANDO_REF_ET
        context.user_data["et"] = _operario_actual(context)
        await _responder(update, "Escribe la referencia:", _teclado_nav_et())
        return ESPERANDO_REF_ET

    # 5) Pidiendo la cantidad -> volver a los colores de la misma referencia
    if "metros" not in et:
        colores = et.get("colores")
        if not colores:
            context.user_data["et"] = _operario_actual(context)
            await _responder(update, "Escribe la referencia:", _teclado_nav_et())
            return ESPERANDO_REF_ET
        for k in ("color", "color_nombre", "item"):
            et.pop(k, None)
        await _responder(
            update,
            f"Referencia: {et['ref']}\n\nElige el color:",
            _teclado_colores(colores),
        )
        return SELECCIONANDO_COLOR_ET

    # 6) Pidiendo el número de etiquetas -> volver a la cantidad
    if "item" not in et:
        context.user_data["et"] = _operario_actual(context)
        await _responder(update, "Escribe la referencia:", _teclado_nav_et())
        return ESPERANDO_REF_ET
    et.pop("metros", None)
    et.pop("cm", None)
    await _responder(update, _texto_pedir_cantidad(et), _teclado_nav_et())
    return ESPERANDO_METROS_ET


def _nombre_impresora(id_imp: str) -> str:
    return IMPRESORAS_ZEBRA.get(id_imp, {}).get("nombre", id_imp)


async def _responder(update: Update, texto: str, markup=None, nuevo: bool = False) -> None:
    """Edita el mensaje si viene de un botón; si no (o si nuevo=True), responde con uno nuevo."""
    q = update.callback_query
    if q:
        await q.answer()
        if not nuevo:
            try:
                await q.edit_message_text(texto, reply_markup=markup)
                return
            except Exception:
                pass  # p. ej. mensaje idéntico: se envía uno nuevo
    await update.effective_message.reply_text(texto, reply_markup=markup)


def _entero(texto: str, minimo: int, maximo: int):
    """Convierte '61', '61 m' o '5 cm' en entero dentro del rango; None si no es válido."""
    t = texto.strip().lower()
    for sufijo in ("mts", "cm", "m"):
        t = t.removesuffix(sufijo).strip()
    if not t.isdigit():
        return None
    n = int(t)
    return n if minimo <= n <= maximo else None


def _formato_color(color: str) -> str:
    """Toma el código del color: '240 VINOTINTO' -> '240', '21' -> '021'.

    Si el color no empieza con un código numérico, se deja tal cual.
    """
    c = str(color).strip()
    m = re.match(r"^(\d+)", c)
    return m.group(1).zfill(3) if m else c


def _item_de(df, ref: str, color: str) -> str:
    """Código de artículo (item) de la referencia, tomado del catálogo."""
    if "Artículo" not in df.columns:
        return ""
    filas = df[(df["_ref"] == ref) & (df["_color"] == color)]
    if filas.empty:
        filas = df[df["_ref"] == ref]
    if filas.empty:
        return ""
    valor = str(filas.iloc[0]["Artículo"]).strip()
    valor = valor.split("-")[0].strip()  # '1554-240' -> '1554'
    return valor.zfill(4) if valor.isdigit() else valor


# --------------------------------------------------------------------------- #
# Seguridad: borrado de mensajes sensibles y bloqueo por intentos fallidos
# --------------------------------------------------------------------------- #
async def _borrar_mensaje_usuario(update: Update) -> None:
    """Borra el mensaje del usuario (para que el ID/PIN no queden visibles en el chat)."""
    try:
        await update.message.delete()
    except Exception as e:
        logger.warning("No se pudo borrar el mensaje con el ID/PIN: %s", e)


def _segundos_bloqueo(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Segundos que faltan para que termine el bloqueo (0 si no está bloqueado)."""
    hasta = context.user_data.get("et_bloqueo_hasta", 0)
    return max(0, int(hasta - time.time()))


def _registrar_fallo(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Suma un intento fallido. Devuelve True si con este fallo se activa el bloqueo."""
    fallos = context.user_data.get("et_fallos", 0) + 1
    if fallos >= MAX_FALLOS:
        context.user_data["et_bloqueo_hasta"] = time.time() + SEGUNDOS_BLOQUEO
        context.user_data["et_fallos"] = 0
        return True
    context.user_data["et_fallos"] = fallos
    return False


async def _avisar_bloqueo(chat, segundos: int) -> None:
    minutos = max(1, (segundos + 59) // 60)
    await chat.send_message(
        f"⛔ Demasiados intentos fallidos. Intenta de nuevo en {minutos} min.",
        reply_markup=_teclado_menu_et(),
    )


async def iniciar_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    registrar_uso_bot(update, "BOTÓN_ETIQUETA" if update.callback_query else "/ETIQUETA")
    context.user_data["et"] = {}  # sin operario: se pedirá el ID
    await _responder(
        update,
        "🏷️ Etiqueta Zebra\n\nEscribe tu ID de operario:",
        _teclado_menu_et(),
    )
    return ESPERANDO_REF_ET


async def _aceptar_operario(chat, context: ContextTypes.DEFAULT_TYPE, id_op: str, nombre: str) -> str:
    """Deja la sesión abierta a nombre del operario y pide la impresora."""
    context.user_data["et"] = {"operario": nombre, "id_operario": id_op}
    context.user_data["et_fallos"] = 0
    await chat.send_message(
        f"✅ Hola, {nombre}.\n\n🖨️ ¿En qué impresora vas a imprimir?",
        reply_markup=_teclado_impresoras(),
    )
    return ELIGIENDO_IMPRESORA_ET


async def _validar_id_operario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Valida el ID escrito contra la pestaña ETIQUETAS. Si el operario tiene PIN, lo pide."""
    id_txt = re.sub(r"\D", "", update.message.text)
    await _borrar_mensaje_usuario(update)  # el ID no queda visible en el chat
    chat = update.effective_chat

    espera = _segundos_bloqueo(context)
    if espera:
        await _avisar_bloqueo(chat, espera)
        return ESPERANDO_REF_ET

    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception:
        logger.exception("No se pudo leer la lista de operarios de etiquetas")
        await chat.send_message(
            "⚠️ No pude consultar la lista de operarios. Intenta de nuevo en un momento:",
            reply_markup=_teclado_menu_et(),
        )
        return ESPERANDO_REF_ET

    registro = operarios.get(id_txt) if id_txt else None
    if not registro:
        if _registrar_fallo(context):
            await _avisar_bloqueo(chat, SEGUNDOS_BLOQUEO)
        else:
            await chat.send_message(
                "❌ ID no autorizado. Escribe tu ID de operario:",
                reply_markup=_teclado_menu_et(),
            )
        return ESPERANDO_REF_ET

    nombre, pin = registro
    if pin:
        # El ID es válido pero falta el PIN: la sesión aún NO queda abierta
        context.user_data["et"] = {"id_pendiente": id_txt}
        await chat.send_message("🔒 Escribe tu PIN:", reply_markup=_teclado_nav_et())
        return ESPERANDO_REF_ET

    return await _aceptar_operario(chat, context, id_txt, nombre)


async def _validar_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Valida el PIN del operario cuyo ID ya fue aceptado."""
    pin_txt = re.sub(r"\D", "", update.message.text)
    await _borrar_mensaje_usuario(update)  # el PIN no queda visible en el chat
    chat = update.effective_chat
    id_pendiente = context.user_data.get("et", {}).get("id_pendiente", "")

    espera = _segundos_bloqueo(context)
    if espera:
        context.user_data["et"] = {}
        await _avisar_bloqueo(chat, espera)
        return ESPERANDO_REF_ET

    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception:
        logger.exception("No se pudo leer la lista de operarios de etiquetas")
        await chat.send_message(
            "⚠️ No pude consultar la lista de operarios. Intenta de nuevo:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_REF_ET

    registro = operarios.get(id_pendiente)
    if not registro or not pin_txt or pin_txt != registro[1]:
        bloqueado = _registrar_fallo(context)
        context.user_data["et"] = {}  # se vuelve a pedir el ID desde cero
        if bloqueado:
            await _avisar_bloqueo(chat, SEGUNDOS_BLOQUEO)
        else:
            await chat.send_message(
                "❌ PIN incorrecto. Escribe tu ID de operario:",
                reply_markup=_teclado_menu_et(),
            )
        return ESPERANDO_REF_ET

    return await _aceptar_operario(chat, context, id_pendiente, registro[0])


async def seleccionar_impresora_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    await q.answer()
    id_imp = q.data.rsplit("_", 1)[1]
    if id_imp not in IMPRESORAS_ZEBRA:
        return ELIGIENDO_IMPRESORA_ET

    et = context.user_data.setdefault("et", {})
    et["impresora"] = id_imp
    await _responder(
        update,
        f"🖨️ Impresora: {_nombre_impresora(id_imp)}\n\nEscribe la referencia:",
        _teclado_nav_et(),
    )
    return ESPERANDO_REF_ET


async def _mostrar_colores(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ref: str,
    df,
    nuevo: bool = False,
    refs=None,
) -> str:
    colores = datos.colores_de(df, ref)
    if not colores:
        await _responder(
            update,
            f"❌ {ref} no tiene colores registrados.\n\nEscribe otra referencia:",
            _teclado_nav_et(),
            nuevo=nuevo,
        )
        context.user_data["et"] = _operario_actual(context)
        return ESPERANDO_REF_ET

    colores = colores[:MAX_BOTONES]
    nuevo_et = {**_operario_actual(context), "ref": ref, "colores": colores}
    if refs:
        nuevo_et["refs"] = refs  # para poder volver a la lista de referencias con "Atrás"
    context.user_data["et"] = nuevo_et

    await _responder(
        update,
        f"Referencia: {ref}\n\nElige el color:",
        _teclado_colores(colores),
        nuevo=nuevo,
    )
    return SELECCIONANDO_COLOR_ET


async def recibir_texto_ref_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    et = context.user_data.get("et", {})
    # Mientras no haya un operario validado, el texto recibido es el ID (o el PIN)
    if "operario" not in et:
        if "id_pendiente" in et:
            return await _validar_pin(update, context)
        return await _validar_id_operario(update, context)

    termino = update.message.text.strip().upper()
    df = await datos.catalogo.obtener()
    if df is None:
        await update.message.reply_text(
            "⚠️ Inventario no disponible, intenta en un momento.",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_REF_ET

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await update.message.reply_text(
            f"❌ No encontré la referencia «{termino}». Intenta de nuevo:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_REF_ET

    if termino in refs:
        return await _mostrar_colores(update, context, termino, df)
    if len(refs) == 1:
        return await _mostrar_colores(update, context, refs[0], df)

    mostradas = refs[:MAX_BOTONES]
    context.user_data["et"] = {**_operario_actual(context), "refs": mostradas}
    texto = f"Encontré {len(refs)} referencias, elige una:"
    if len(refs) > len(mostradas):
        texto += f"\nSe muestran las primeras {len(mostradas)}, sé más específico para filtrar."
    await update.message.reply_text(texto, reply_markup=_teclado_refs(mostradas))
    return SELECCIONANDO_REF_ET


async def seleccionar_referencia_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    await q.answer()  # Evita expiración de botones de Telegram
    idx = int(q.data.rsplit("_", 1)[1])
    refs = context.user_data.get("et", {}).get("refs", [])
    if idx >= len(refs):
        return ESPERANDO_REF_ET

    df = await datos.catalogo.obtener()
    if df is None:
        return ESPERANDO_REF_ET
    return await _mostrar_colores(update, context, refs[idx], df, refs=refs)


async def seleccionar_color_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    await q.answer()  # Evita expiración de botones de Telegram
    if q.data == "et_back":
        # Botón antiguo "Cambiar referencia" (mensajes viejos): conserva operario e impresora
        context.user_data["et"] = _operario_actual(context)
        await _responder(update, "Escribe la referencia:", _teclado_nav_et())
        return ESPERANDO_REF_ET

    et = context.user_data.get("et", {})
    colores = et.get("colores", [])
    idx = int(q.data.rsplit("_", 1)[1])
    if "ref" not in et or idx >= len(colores):
        return ESPERANDO_REF_ET

    df = await datos.catalogo.obtener()
    if df is None:
        return ESPERANDO_REF_ET

    color = colores[idx]
    item = _item_de(df, et["ref"], color)
    if not item:
        return SELECCIONANDO_COLOR_ET

    et["color"] = _formato_color(color)
    et["color_nombre"] = color
    et["item"] = item
    await _responder(update, _texto_pedir_cantidad(et), _teclado_nav_et())
    return ESPERANDO_METROS_ET


_RE_CANTIDAD = re.compile(r"^(\d{1,3})(?:[.,](\d{1,2}))?$")


def _parsear_cantidad(texto: str):
    """'50' -> (50, 0) | '50.5' -> (50, 50) | '50,25' -> (50, 25). None si no es válido."""
    t = texto.strip().lower()
    for sufijo in ("mts", "m"):
        t = t.removesuffix(sufijo).strip()
    m = _RE_CANTIDAD.match(t)
    if not m:
        return None
    metros = int(m.group(1))
    cm = int((m.group(2) or "0").ljust(2, "0"))  # '5' -> 50 cm, '05' -> 5 cm
    if metros == 0 and cm == 0:
        return None
    return metros, cm


async def recibir_metros_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    cantidad = _parsear_cantidad(update.message.text)
    if cantidad is None:
        await update.message.reply_text(
            "⚠️ Cantidad no válida. Introduce la cantidad:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_METROS_ET
    metros, cm = cantidad
    context.user_data.setdefault("et", {}).update({"metros": metros, "cm": cm})
    await update.message.reply_text(
        f"Cantidad: {metros:03d}.{cm:02d}\n\nIntroduce el número de etiquetas:",
        reply_markup=_teclado_nav_et(),
    )
    return ESPERANDO_COPIAS_ET


async def recibir_copias_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    copias = _entero(update.message.text, 1, MAX_COPIAS)
    if copias is None:
        await update.message.reply_text(
            f"⚠️ Escribe un número de etiquetas entre 1 y {MAX_COPIAS}:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_COPIAS_ET
    et = context.user_data.get("et", {})
    campos = ("operario", "ref", "item", "color", "metros", "cm")
    if any(k not in et for k in campos):
        await update.message.reply_text("⚠️ Se perdió la selección. Escribe /etiqueta para empezar de nuevo.")
        return ConversationHandler.END

    id_imp = et.get("impresora") or IMPRESORA_DEFECTO
    nombre_imp = _nombre_impresora(id_imp)
    try:
        await asyncio.to_thread(
            imprimir_etiqueta,
            et["item"], et["ref"], et["color"], et["metros"], et["cm"],
            id_imp,  # id_impresora: va antes de copias en etiquetas.py
            copias, et["operario"],
        )
    except Exception:
        logger.exception("No se pudo imprimir la etiqueta en %s", nombre_imp)
        await update.message.reply_text(
            f"⚠️ No se pudo enviar la etiqueta a {nombre_imp}. "
            "Revisa que esté encendida y con red, o prueba con otra impresora.\n\n"
            "Escribe /etiqueta para reintentar."
        )
        return ConversationHandler.END
    logger.info(
        "Etiqueta impresa en %s por %s (ID %s): item %s ref %s color %s cant %03d.%02d copias %d",
        nombre_imp, et["operario"], et.get("id_operario"), et["item"], et["ref"], et["color"],
        et["metros"], et["cm"], copias,
    )
    cant = f"{et['metros']:03d}.{et['cm']:02d}"
    botones_otra = [
        [
            InlineKeyboardButton("🔄 Sí, generar otra", callback_data="et_otra_si"),
            InlineKeyboardButton("❌ No, terminar", callback_data="et_otra_no"),
        ],
        [InlineKeyboardButton("🎨 Misma referencia, otro color", callback_data="et_otra_color")],
        [InlineKeyboardButton("📏 Mismo color, otro metraje", callback_data="et_otra_metraje")],
        [InlineKeyboardButton("🖨️ Cambiar impresora", callback_data="et_otra_imp")],
        _boton_menu_et(),
    ]
    reply_markup = InlineKeyboardMarkup(botones_otra)
    await update.message.reply_text(
        f"✅ Etiqueta enviada:\nItem {et['item']} · {et['ref']} · Color {et.get('color_nombre', et['color'])}\n"
        f"Cantidad {cant} · Etiquetas {copias}\n"
        f"Operario: {et['operario']}\n"
        f"Impresora: {nombre_imp}\n\n"
        "¿Deseas generar otra etiqueta?",
        reply_markup=reply_markup,
    )
    return PREGUNTANDO_OTRA_ET


async def manejar_otra_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Evalúa la acción elegida por el operador (otra etiqueta, otro color, otro metraje, cambiar impresora o cerrar)."""
    q = update.callback_query
    await q.answer()  # Evita expiración de botones de Telegram

    if q.data == "et_otra_si":
        context.user_data["et"] = _operario_actual(context)  # limpia todo menos operario e impresora
        await q.edit_message_text(
            "🏷️ Etiqueta Zebra\n\nEscribe la referencia:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_REF_ET

    if q.data == "et_otra_color":
        # Misma referencia que se acaba de imprimir: solo se vuelve a elegir el color
        ref = context.user_data.get("et", {}).get("ref")
        if not ref:
            context.user_data["et"] = _operario_actual(context)
            await q.edit_message_text(
                "🏷️ Etiqueta Zebra\n\nEscribe la referencia:",
                reply_markup=_teclado_nav_et(),
            )
            return ESPERANDO_REF_ET

        df = await datos.catalogo.obtener()
        if df is None:
            await q.message.reply_text("⚠️ Inventario no disponible, intenta de nuevo en un momento.")
            return PREGUNTANDO_OTRA_ET

        # Se quitan los botones del resumen para conservarlo como registro,
        # y la lista de colores sale en un mensaje nuevo debajo.
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await _mostrar_colores(update, context, ref, df, nuevo=True)

    if q.data == "et_otra_metraje":
        # Misma referencia, item y color: solo se pide una nueva cantidad
        et = context.user_data.get("et", {})
        if any(k not in et for k in ("ref", "item", "color")):
            context.user_data["et"] = _operario_actual(context)
            await q.edit_message_text(
                "🏷️ Etiqueta Zebra\n\nEscribe la referencia:",
                reply_markup=_teclado_nav_et(),
            )
            return ESPERANDO_REF_ET

        et.pop("metros", None)
        et.pop("cm", None)
        # Se conserva el resumen anterior como registro (sin botones)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await q.message.reply_text(
            f"Referencia: {et['ref']}\nItem: {et['item']}\n"
            f"Color: {et.get('color_nombre', et['color'])}\n\n"
            "Introduce la nueva cantidad:",
            reply_markup=_teclado_nav_et(),
        )
        return ESPERANDO_METROS_ET

    if q.data == "et_otra_imp":
        et = _operario_actual(context)
        et.pop("impresora", None)
        context.user_data["et"] = et
        await q.edit_message_text(
            "🖨️ ¿En qué impresora vas a imprimir?",
            reply_markup=_teclado_impresoras(atras=False),  # el resumen ya se editó: no hay paso anterior
        )
        return ELIGIENDO_IMPRESORA_ET

    # "No, terminar": se cierra la sesión del operario para que nadie más use su nombre
    context.user_data.pop("et", None)
    await q.edit_message_text("👍 Entendido. Proceso de etiquetado cerrado correctamente.")
    return ConversationHandler.END
