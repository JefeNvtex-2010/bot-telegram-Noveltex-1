"""Flujo guiado /etiqueta: ID operario -> referencia -> color -> cantidad (metros.centímetros) -> nº de etiquetas -> Selección Impresora."""
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
from etiquetas import imprimir_etiqueta, IMPRESORAS_ZEBRA
from handlers import registrar_uso_bot

logger = logging.getLogger(__name__)

# Estados como texto para no chocar con los de los demás módulos
ESPERANDO_REF_ET = "et_esperando_ref"
SELECCIONANDO_REF_ET = "et_seleccionando_ref"
SELECCIONANDO_COLOR_ET = "et_seleccionando_color"
ESPERANDO_METROS_ET = "et_esperando_metros"
ESPERANDO_COPIAS_ET = "et_esperando_copias"
SELECCIONANDO_IMPRESORAS_ET = "et_seleccionando_impresora"
PREGUNTANDO_OTRA_ET = "et_preguntando_otra"

MAX_BOTONES = 40
MAX_METROS = 999
MAX_COPIAS = 50

SHEET_ID_OPERARIOS = os.environ.get(
    "SHEET_ID_AUDITORIA", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
)
PESTANA_OPERARIOS = "ETIQUETAS"


def _cargar_operarios() -> dict:
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID_OPERARIOS}"
        f"/gviz/tq?tqx=out:csv&sheet={PESTANA_OPERARIOS}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        texto = r.read().decode("utf-8-sig")

    if texto.lstrip().lower().startswith("<"):
        raise RuntimeError("La hoja no es accesible (revisa que esté compartida con el enlace).")

    operarios = {}
    filas = list(csv.reader(io.StringIO(texto)))
    for fila in filas[1:]:
        if len(fila) < 2:
            continue
        nombre = fila[0].strip()
        id_op = re.sub(r"\D", "", fila[1])
        if nombre and id_op:
            operarios[id_op] = nombre
    return operarios


def _operario_actual(context: ContextTypes.DEFAULT_TYPE) -> dict:
    et = context.user_data.get("et", {})
    return {k: et[k] for k in ("operario", "id_operario") if k in et}


async def _responder(update: Update, texto: str, markup=None) -> None:
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_text(texto, reply_markup=markup)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(texto, reply_markup=markup)


def _entero(texto: str, minimo: int, maximo: int):
    t = texto.strip().lower()
    for sufijo in ("mts", "cm", "m"):
        t = t.removesuffix(sufijo).strip()
    if not t.isdigit():
        return None
    n = int(t)
    return n if minimo <= n <= maximo else None


def _formato_color(color: str) -> str:
    c = str(color).strip()
    m = re.match(r"^(\d+)", c)
    return m.group(1).zfill(3) if m else c


def _item_de(df, ref: str, color: str) -> str:
    if "Artículo" not in df.columns:
        return ""
    filas = df[(df["_ref"] == ref) & (df["_color"] == color)]
    if filas.empty:
        filas = df[df["_ref"] == ref]
    if filas.empty:
        return ""
    valor = str(filas.iloc[0]["Artículo"]).strip()
    valor = valor.split("-")[0].strip()
    return valor.zfill(4) if valor.isdigit() else valor


async def iniciar_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    registrar_uso_bot(update, "BOTÓN_ETIQUETA" if update.callback_query else "/ETIQUETA")
    context.user_data["et"] = {}
    await _responder(update, """🏷️ Etiqueta Zebra

Escribe tu ID de operario:""")
    return ESPERANDO_REF_ET


async def _validar_id_operario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    id_txt = re.sub(r"\D", "", update.message.text)
    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception:
        logger.exception("No se pudo leer la lista de operarios de etiquetas")
        await update.message.reply_text("⚠️ No pude consultar la lista de operarios. Intenta de nuevo en un momento:")
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

    await _responder(update, f"Referencia: {ref}\n\ Elige el color:", InlineKeyboardMarkup(botones))
    return SELECCIONANDO_COLOR_ET


async def recibir_texto_ref_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
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
        context.user_data["et"] = _operario_actual(context)
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
        f"Referencia: {et['ref']}\nItem: {item}\nColor: {color}\n\nIntroduce la cantidad:",
    )
    return ESPERANDO_METROS_ET


_RE_CANTIDAD = re.compile(r"^(\d{1,3})(?:[.,](\d{1,2}))?$")


def _parsear_cantidad(texto: str):
    t = texto.strip().lower()
    for sufijo in ("mts", "m"):
        t = t.removesuffix(sufijo).strip()
    m = _RE_CANTIDAD.match(t)
    if not m:
        return None
    metros = int(m.group(1))
    cm = int((m.group(2) or "0").ljust(2, "0"))
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
    await update.message.reply_text(f"Cantidad: {metros:03d}.{cm:02d}\n\nIntroduce el número de etiquetas:")
    return ESPERANDO_COPIAS_ET


async def recibir_copias_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    copias = _entero(update.message.text, 1, MAX_COPIAS)
    if copias is None:
        await update.message.reply_text(f"⚠️ Escribe un número de etiquetas entre 1 y {MAX_COPIAS}:")
        return ESPERANDO_COPIAS_ET
    
    et = context.user_data.get("et", {})
    et["copias"] = copias
    
    botones_impresoras = []
    for llave, info in IMPRESORAS_ZEBRA.items():
        botones_impresoras.append([InlineKeyboardButton(info["nombre"], callback_data=f"et_imp_{llave}")])
        
    reply_markup = InlineKeyboardMarkup(botones_impresoras)
    await update.message.reply_text("🖥️ Selecciona la impresora Zebra más cercana:", reply_markup=reply_markup)
    return SELECCIONANDO_IMPRESORAS_ET


async def procesar_impresion_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Captura el botón de la impresora seleccionada y ejecuta el envío físico."""
    q = update.callback_query
    await q.answer()
    
    id_impresora = q.data.rsplit("_", 1)[1]
    et = context.user_data.get("et", {})
    campos = ("operario", "ref", "item", "color", "metros", "cm", "copias")
    
    if any(k not in et for k in campos):
        await q.edit_message_text("⚠️ Se perdió la selección de los datos. Usa /etiqueta de nuevo.")
        return ConversationHandler.END
        
    nombre_impresora = IMPRESORAS_ZEBRA.get(id_impresora, {}).get("nombre", "Zebra")
    
    try:
        await asyncio.to_thread(
            imprimir_etiqueta, et["item"], et["ref"], et["color"], et["metros"], et["cm"], id_impresora, et["copias"], et["operario"]
        )
    except Exception:
        logger.exception("No se pudo imprimir la etiqueta")
        await q.edit_message_text("⚠️ No se pudo conectar con la impresora física. Reintenta con /etiqueta.")
        return ConversationHandler.END
        
    logger.info(
        "Etiqueta enviada a %s por %s (ID %s): item %s ref %s color %s cant %03d.%02d copias %d",
        nombre_impresora, et["operario"], et.get("id_operario"), et["item"], et["ref"], et["color"],
        et["metros"], et["cm"], et["copias"],
    )
    
    cant = f"{et['metros']:03d}.{et['cm']:02d}"
    botones_otra = [
        [
            InlineKeyboardButton("🔄 Sí, generar otra", callback_data="et_otra_si"),
            InlineKeyboardButton("❌ No, terminar", callback_data="et_otra_no"),
        ]
    ]
    
    await q.edit_message_text(
        f"✅ Etiqueta enviada exitosamente a:\n👉 *{nombre_impresora}*\n\n"
        f"Item {et['item']} · {et['ref']} · Color {et.get('color_nombre', et['color'])}\n"
        f"Cantidad {cant} · Hojas: {et['copias']}\n"
        f"Operario: {et['operario']}\n\n"
        "¿Deseas generar otra etiqueta?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(botones_otra),
    )
    return PREGUNTANDO_OTRA_ET


async def manejar_otra_etiqueta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    q = update.callback_query
    await q.answer()
    if q.data == "et_otra_si":
        context.user_data["et"] = _operario_actual(context)
        await q.edit_message_text("🏷️ Etiqueta Zebra\n\nEscribe la referencia:")
        return ESPERANDO_REF_ET
    else:
        context.user_data.pop("et", None)
        await q.edit_message_text("👍 Entendido. Proceso de etiquetado cerrado correctamente.")
        return ConversationHandler.END
