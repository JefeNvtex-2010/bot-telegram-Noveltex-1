"""Flujo guiado /etiqueta: ID operario -> referencia -> color -> cantidad (metros.centímetros) -> nº de etiquetas."""
import asyncio
import csv
import io
import logging
import os
import re
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

import datos
from etiquetas import imprimir_etiqueta
from handlers import registrar_uso_bot

logger = logging.getLogger(__name__)

# Estados como texto para no chocar con los de los demás módulos
ESPERANDO_REF_ET = "et_esperando_ref"
SELECCIONANDO_REF_ET = "et_seleccionando_ref"
SELECCIONANDO_COLOR_ET = "et_seleccionando_color"
ESPERANDO_METROS_ET = "et_esperando_metros"
ESPERANDO_COPIAS_ET = "et_esperando_copias"
PREGUNTANDO_OTRA_ET = "et_preguntando_otra"  # estado para el bucle de "otra etiqueta"

MAX_BOTONES = 40
MAX_METROS = 999
MAX_COPIAS = 50

# Google Sheet AUDITORIA_BOT, pestaña ETIQUETAS (columnas: OPERARIO | ID)
SHEET_ID_OPERARIOS = os.environ.get(
    "SHEET_ID_AUDITORIA", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
)
PESTANA_OPERARIOS = "ETIQUETAS"


def _cargar_operarios() -> dict:
    """Lee la pestaña ETIQUETAS y devuelve {id: nombre}. Bloqueante: usar con to_thread."""
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
    for fila in filas[1:]:  # se salta el encabezado OPERARIO | ID
        if len(fila) < 2:
            continue
        nombre = fila[0].strip()
        id_op = re.sub(r"\D", "", fila[1])
        if nombre and id_op:
            operarios[id_op] = nombre
    return operarios


def _operario_actual(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Devuelve solo los datos del operario ya validado (para conservarlos entre pasos)."""
    et = context.user_data.get("et", {})
    return {k: et[k] for k in ("operario", "id_operario") if k in et}


async def _responder(update: Update, texto: str, markup=None) -> None:
    """Edita el mensaje si viene de un botón; si no, responde con uno nuevo."""
    q = update.callback_query
    if q:
        await q.answer()
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
    """Código de artículo (item) de la referencia, tomado del inventario."""
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


async def iniciar_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    registrar_uso_bot(update, "BOTÓN_ETIQUETA" if update.callback_query else "/ETIQUETA")
    context.user_data["et"] = {}  # sin operario: se pedirá el ID
    await _responder(update, "🏷️ Etiqueta Zebra\n\nEscribe tu ID de operario:")
    return ESPERANDO_REF_ET


async def _validar_id_operario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Valida el ID escrito contra la pestaña ETIQUETAS del Google Sheet."""
    id_txt = re.sub(r"\D", "", update.message.text)

    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception:
        logger.exception("No se pudo leer la lista de operarios de etiquetas")
        await update.message.reply_text(
            "⚠️ No pude consultar la lista de operarios. Intenta de nuevo en un momento:"
        )
        return ESPERANDO_REF_ET

    nombre = operarios.get(id_txt) if id_txt else None
    if not nombre:
        await update.message.reply_text("❌ ID no autorizado. Escribe tu ID de operario:")
        return ESPERANDO_REF_ET

    et = context.user_data.setdefault("et", {})
    et["operario"] = nombre
    et["id_operario"] = id_txt
    await update.message.reply_text(f"✅ Hola, {nombre}.\n\nEscribe la referencia:")
    return ESPERANDO_REF_ET


async def _mostrar_colores(update: Update, context: ContextTypes.DEFAULT_TYPE, ref: str, df) -> str:
    colores = datos.colores_de(df, ref)
    if not colores:
        await _responder(update, f"❌ {ref} no tiene colores registrados.\n\nEscribe otra referencia:")
        return ESPERANDO_REF_ET

    colores = colores[:MAX_BOTONES]
    context.user_data["et"] = {**_operario_actual(context), "ref": ref, "colores": colores}

    botones, fila = [], []
    for i, c in enumerate(colores):
        fila.append(InlineKeyboardButton(c, callback_data=f"et_col_{i}"))
        if len(fila) == 2:
            botones.append(fila)
            fila = []
    if fila:
        botones.append(fila)
    botones.append([InlineKeyboardButton("↩️ Cambiar referencia", callback_data="et_back")])

    await _responder(update, f"Referencia: {ref}\n\nElige el color:", InlineKeyboardMarkup(botones))
    return SELECCIONANDO_COLOR_ET


async def recibir_texto_ref_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    # Mientras no haya un operario validado, el texto recibido es el ID
    if "operario" not in context.user_data.get("et", {}):
        return await _validar_id_operario(update, context)

    termino = update.message.text.strip().upper()
    df = await datos.inventario.obtener()
    if df is None:
        await update.message.reply_text("⚠️ Inventario no disponible, intenta en un momento.")
        return ESPERANDO_REF_ET

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await update.message.reply_text(f"❌ No encontré la referencia «{termino}». Intenta de nuevo:")
        return ESPERANDO_REF_ET

    if termino in refs:
        return await _mostrar_colores(update, context, termino, df)
    if len(refs) == 1:
        return await _mostrar_colores(update, context, refs[0], df)

    mostradas = refs[:MAX_BOTONES]
    context.user_data["et"] = {**_operario_actual(context), "refs": mostradas}
    botones = [[InlineKeyboardButton(r, callback_data=f"et_ref_{i}")] for i, r in enumerate(mostradas)]
    texto = f"Encontré {len(refs)} referencias, elige una:"
    if len(refs) > len(mostradas):
        texto += f"\nSe muestran las primeras {len(mostradas)}, sé más específico para filtrar."
    await update.message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botones))
    return SELECCIONANDO_REF_ET


async def seleccionar_referencia_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    idx = int(q.data.rsplit("_", 1)[1])
    refs = context.user_data.get("et", {}).get("refs", [])
    if idx >= len(refs):
        await q.answer("Opción no válida. Escribe la referencia de nuevo.", show_alert=True)
        return ESPERANDO_REF_ET

    df = await datos.inventario.obtener()
    if df is None:
        await q.answer("Inventario no disponible, intenta en un momento.", show_alert=True)
        return ESPERANDO_REF_ET
    return await _mostrar_colores(update, context, refs[idx], df)


async def seleccionar_color_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    if q.data == "et_back":
        context.user_data["et"] = _operario_actual(context)  # conserva al operario
        await _responder(update, "Escribe la referencia:")
        return ESPERANDO_REF_ET

    et = context.user_data.get("et", {})
    colores = et.get("colores", [])
    idx = int(q.data.rsplit("_", 1)[1])
    if "ref" not in et or idx >= len(colores):
        await q.answer("Opción no válida. Escribe la referencia de nuevo.", show_alert=True)
        return ESPERANDO_REF_ET

    df = await datos.inventario.obtener()
    if df is None:
        await q.answer("Inventario no disponible, intenta en un momento.", show_alert=True)
        return ESPERANDO_REF_ET

    color = colores[idx]
    item = _item_de(df, et["ref"], color)
    if not item:
        await q.answer("No encontré el código de artículo de esta referencia.", show_alert=True)
        return SELECCIONANDO_COLOR_ET

    et["color"] = _formato_color(color)
    et["color_nombre"] = color
    et["item"] = item
    await _responder(
        update,
        f"Referencia: {et['ref']}\nItem: {item}\nColor: {color}\n\n"
        "Introduce la cantidad:",
    )
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
        await update.message.reply_text("⚠️ Cantidad no válida. Introduce la cantidad:")
        return ESPERANDO_METROS_ET

    metros, cm = cantidad
    context.user_data.setdefault("et", {}).update({"metros": metros, "cm": cm})
    await update.message.reply_text(
        f"Cantidad: {metros:03d}.{cm:02d}\n\nIntroduce el número de etiquetas:"
    )
    return ESPERANDO_COPIAS_ET


async def recibir_copias_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    copias = _entero(update.message.text, 1, MAX_COPIAS)
    if copias is None:
        await update.message.reply_text(f"⚠️ Escribe un número de etiquetas entre 1 y {MAX_COPIAS}:")
        return ESPERANDO_COPIAS_ET

    et = context.user_data.get("et", {})
    campos = ("operario", "ref", "item", "color", "metros", "cm")
    if any(k not in et for k in campos):
        await update.message.reply_text("⚠️ Se perdió la selección. Escribe /etiqueta para empezar de nuevo.")
        return ConversationHandler.END

    try:
        await asyncio.to_thread(
            imprimir_etiqueta, et["item"], et["ref"], et["color"], et["metros"], et["cm"], copias
        )
    except Exception:
        logger.exception("No se pudo imprimir la etiqueta")
        await update.message.reply_text(
            "⚠️ No se pudo enviar la etiqueta a la impresora. Escribe /etiqueta para reintentar."
        )
        return ConversationHandler.END

    logger.info(
        "Etiqueta impresa por %s (ID %s): item %s ref %s color %s cant %03d.%02d copias %d",
        et["operario"], et.get("id_operario"), et["item"], et["ref"], et["color"],
        et["metros"], et["cm"], copias,
    )

    cant = f"{et['metros']:03d}.{et['cm']:02d}"

    botones_otra = [
        [
            InlineKeyboardButton("🔄 Sí, generar otra", callback_data="et_otra_si"),
            InlineKeyboardButton("❌ No, terminar", callback_data="et_otra_no"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(botones_otra)

    await update.message.reply_text(
        f"✅ Etiqueta enviada:\nItem {et['item']} · {et['ref']} · Color {et.get('color_nombre', et['color'])}\n"
        f"Cantidad {cant} · Etiquetas {copias}\n"
        f"Operario: {et['operario']}\n\n"
        "¿Deseas generar otra etiqueta?",
        reply_markup=reply_markup,
    )
    return PREGUNTANDO_OTRA_ET


async def manejar_otra_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Evalúa la acción elegida por el operador (generar otra o cerrar)."""
    q = update.callback_query
    await q.answer()

    if q.data == "et_otra_si":
        context.user_data["et"] = _operario_actual(context)  # limpia todo menos al operario
        await q.edit_message_text("🏷️ Etiqueta Zebra\n\nEscribe la referencia:")
        return ESPERANDO_REF_ET
    else:
        context.user_data.pop("et", None)
        await q.edit_message_text("👍 Entendido. Proceso de etiquetado cerrado correctamente.")
        return ConversationHandler.END
