"""Módulo de ubicaciones de bodega (/ub).

Flujo:
  1) Menú: Consultar ubicación | Modificar ubicación.
  2) Modificar -> Registrar y borrar (deja solo la ubicación nueva)
               -> Agregar (suma otra ubicación sin borrar las que ya tiene).
  3) Se escanea (o se escribe) el código de barras del rollo, de 12 dígitos:
        001820005225  ->  artículo 0018-200  +  cantidad 05225 (se descarta)
     También se acepta el código del artículo solo (0018-200).
  4) El bot busca el artículo en el inventario SAP (código y descripción) y:
        - Consultar: muestra las ubicaciones registradas.
        - Registrar y borrar / Agregar: pide la ubicación y la guarda.

Las ubicaciones se guardan en la pestaña "UBICACIONES BODEGA" del Google Sheet
AUDITORIA_BOT (FECHA | UBICACIÓN | CODIGO | DESCRIPCIÓN | NOMBRE), una fila por
cada ubicación del artículo. La escritura/lectura se hace a través de un Apps Script
(ver ubicaciones_apps_script.gs).

Variables de entorno (Render):
  URL_UBICACIONES     URL /exec del Apps Script de ubicaciones.
  TOKEN_UBICACIONES   Token (el mismo que la propiedad TOKEN del Apps Script).

Permiso: "ubicaciones" (en la hoja PERMISOS: la frase "ubicaciones" o /ub).
"""
import asyncio
import logging
import os
import re
import unicodedata

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import datos
from acceso import requiere_acceso
from handlers import editar, esc, programar_borrado, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados únicos y altos para no chocar con los de los demás módulos
UB_MENU, UB_ESPERANDO_CODIGO, UB_ESPERANDO_UBICACION = 40, 41, 42

URL_UBICACIONES = os.environ.get("URL_UBICACIONES", "").strip()
TOKEN_UBICACIONES = os.environ.get("TOKEN_UBICACIONES", "").strip()

MODOS = ("consultar", "reemplazar", "agregar")

# Nombres posibles de las columnas del inventario SAP (sin tildes, minúsculas)
COLUMNAS_CODIGO = ("articulo", "codigo articulo", "codigo del articulo", "itemcode", "item code")
COLUMNAS_DESCRIPCION = ("descripcion", "descripcion articulo", "nombre articulo", "itemname", "item name")

_PATRON_UBICACION = re.compile(r"^[A-Z0-9Ñ][A-Z0-9Ñ \-./#]{0,29}$")


# --------------------------------------------------------------------------- #
# Utilidades de códigos
# --------------------------------------------------------------------------- #
def _sin_acentos(texto) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn"
    )


def _clave(codigo) -> str:
    """Código sin guiones ni espacios, en mayúsculas: '0018-200' -> '0018200'."""
    return re.sub(r"[^0-9A-Za-z]", "", str(codigo)).upper()


def _extraer_codigo(texto):
    """Devuelve los 7 dígitos del artículo o None si el texto no es válido.

    - 12 dígitos (código de barras del rollo): 7 del artículo + 5 de cantidad (se descartan).
    - 7 dígitos (código del artículo, con o sin guion): se usa tal cual.
    """
    digitos = re.sub(r"\D", "", str(texto))
    if len(digitos) == 12:
        return digitos[:7]
    if len(digitos) == 7:
        return digitos
    return None


def _formatear(codigo7: str) -> str:
    """'0018200' -> '0018-200'."""
    return f"{codigo7[:4]}-{codigo7[4:]}"


def _normalizar_ubicacion(texto):
    """Ubicación limpia en mayúsculas, o None si no es válida."""
    t = re.sub(r"\s+", " ", str(texto).strip().upper())
    return t if _PATRON_UBICACION.match(t) else None


# --------------------------------------------------------------------------- #
# Inventario SAP: índice de artículos (código -> descripción)
# --------------------------------------------------------------------------- #
_cache = {"firma": None, "indice": {}}


def _buscar_columna(df, candidatos):
    normales = {_sin_acentos(c).lower().strip(): c for c in df.columns}
    for cand in candidatos:
        if cand in normales:
            return normales[cand]
    return None


def _indice_articulos(df) -> dict:
    """{clave: (código SAP, descripción)}; se reconstruye solo si cambió el inventario."""
    firma = (id(df), len(df))
    if _cache["firma"] == firma:
        return _cache["indice"]

    col_cod = _buscar_columna(df, COLUMNAS_CODIGO)
    if col_cod is None:
        raise RuntimeError(
            "No encontré la columna del artículo en el inventario SAP "
            f"(columnas: {', '.join(map(str, df.columns))})"
        )
    col_desc = _buscar_columna(df, COLUMNAS_DESCRIPCION)

    codigos = df[col_cod].astype(str).str.strip().tolist()
    if col_desc:
        descripciones = df[col_desc].astype(str).str.strip().tolist()
    else:
        descripciones = [""] * len(codigos)

    indice = {}
    for cod, desc in zip(codigos, descripciones):
        if not cod or cod.lower() == "nan":
            continue
        k = _clave(cod)
        if k and k not in indice:
            indice[k] = (cod, "" if desc.lower() == "nan" else desc)

    _cache["firma"], _cache["indice"] = firma, indice
    return indice


async def _buscar_articulo(codigo7: str):
    """(código SAP, descripción) o None si el artículo no está en el inventario."""
    df = await datos.inventario.obtener()
    if df is None:
        raise RuntimeError(datos.inventario.ultimo_error or "No se pudo acceder al inventario")
    return _indice_articulos(df).get(codigo7)


# --------------------------------------------------------------------------- #
# Hoja de ubicaciones (vía Apps Script)
# --------------------------------------------------------------------------- #
def _llamar_ubicaciones(payload: dict) -> dict:
    """Llama al Apps Script. Bloqueante: usar con asyncio.to_thread."""
    if not URL_UBICACIONES:
        raise RuntimeError("Falta la variable URL_UBICACIONES")
    r = requests.post(
        URL_UBICACIONES,
        json={"token": TOKEN_UBICACIONES, **payload},
        timeout=25,
    )
    r.raise_for_status()
    try:
        resp = r.json()
    except ValueError:
        raise RuntimeError(
            "El Apps Script no devolvió JSON (revisa que esté publicado como "
            "aplicación web con acceso para cualquier persona)"
        )
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "error desconocido"))
    return resp


async def _consultar_ubicaciones(codigo: str):
    """(lista de ubicaciones, descripción guardada en la hoja)."""
    resp = await asyncio.to_thread(_llamar_ubicaciones, {"accion": "consultar", "codigo": codigo})
    return resp.get("ubicaciones", []), resp.get("descripcion", "")


# --------------------------------------------------------------------------- #
# Pantallas
# --------------------------------------------------------------------------- #
def _pantalla_menu():
    texto = "📍 <b>Ubicaciones de bodega</b>\n\n¿Qué deseas hacer?"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Consultar ubicación", callback_data="ub_consultar")],
        [InlineKeyboardButton("✏️ Modificar ubicación", callback_data="ub_modificar")],
    ])
    return texto, teclado


def _pantalla_modificar():
    texto = (
        "✏️ <b>Modificar ubicación</b>\n\nElige cómo registrar:\n\n"
        "🗑️ <b>Registrar y borrar:</b> elimina todas las ubicaciones actuales del "
        "artículo y deja solo la nueva.\n\n"
        "➕ <b>Agregar:</b> suma otra ubicación al artículo sin borrar las que ya tiene."
    )
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ Registrar y borrar", callback_data="ub_reemplazar")],
        [InlineKeyboardButton("➕ Agregar otra ubicación", callback_data="ub_agregar")],
        [InlineKeyboardButton("🔙 Volver", callback_data="ub_menu")],
    ])
    return texto, teclado


def _teclado_volver():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Volver al menú de ubicaciones", callback_data="ub_menu")]
    ])


def _texto_pedir_codigo(modo: str) -> str:
    titulo = {
        "consultar": "🔎 <b>Consultar ubicación</b>",
        "reemplazar": "🗑️ <b>Registrar y borrar</b>",
        "agregar": "➕ <b>Agregar ubicación</b>",
    }[modo]
    return (
        f"{titulo}\n\nEscanea el <b>código de barras</b> del rollo "
        "(o escribe el código del artículo, ej. <code>0018-200</code>):"
    )


def _texto_articulo(codigo: str, descripcion: str) -> str:
    texto = f"📦 <b>Artículo:</b> <code>{esc(codigo)}</code>\n"
    if descripcion:
        texto += f"📝 <b>Descripción:</b> {esc(descripcion)}\n"
    return texto


def _nombres(ubicaciones: list) -> str:
    return ", ".join(esc(u.get("ubicacion", "")) for u in ubicaciones)


def _detalle_ubicaciones(ubicaciones: list) -> str:
    if not ubicaciones:
        return "📍 <b>Sin ubicación registrada.</b>"
    lineas = []
    for u in ubicaciones:
        extra = " · ".join(x for x in (esc(u.get("fecha", "")), esc(u.get("nombre", ""))) if x)
        lineas.append(f"• <b>{esc(u.get('ubicacion', ''))}</b>" + (f" — {extra}" if extra else ""))
    return "📍 <b>Ubicaciones:</b>\n" + "\n".join(lineas)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


async def _sesion_expirada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu()
    await responder(update, context, "⌛ La sesión expiró.\n\n" + texto, reply_markup=teclado)
    return UB_MENU


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
@requiere_acceso("ubicaciones")
async def iniciar_ubicaciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["ub"] = {}
    texto, teclado = _pantalla_menu()

    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_UBICACIONES")
        await editar(query, texto, teclado)
        return UB_MENU

    registrar_uso_bot(update, "/ub")
    programar_borrado(context, update.message)
    await responder(update, context, texto, reply_markup=teclado)
    return UB_MENU


async def botones_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botones del módulo: ub_menu | ub_consultar | ub_modificar | ub_reemplazar | ub_agregar."""
    query = update.callback_query
    await query.answer()
    accion = query.data.split("_", 1)[1]
    ub = context.user_data.setdefault("ub", {})
    ub.clear()

    if accion == "menu":
        texto, teclado = _pantalla_menu()
        await editar(query, texto, teclado)
        return UB_MENU

    if accion == "modificar":
        texto, teclado = _pantalla_modificar()
        await editar(query, texto, teclado)
        return UB_MENU

    if accion in MODOS:  # consultar | reemplazar | agregar
        ub["modo"] = accion
        await editar(query, _texto_pedir_codigo(accion), _teclado_volver())
        return UB_ESPERANDO_CODIGO

    texto, teclado = _pantalla_menu()
    await editar(query, texto, teclado)
    return UB_MENU


async def recordar_botones_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Texto escrito mientras se espera elegir una opción del menú."""
    programar_borrado(context, update.message)
    await responder(update, context, "☝️ Elige una opción con los botones del menú de ubicaciones.")
    return UB_MENU


async def recibir_codigo_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    modo = ub.get("modo")
    if modo not in MODOS:
        return await _sesion_expirada(update, context)

    codigo7 = _extraer_codigo(update.message.text)
    if codigo7 is None:
        await responder(
            update, context,
            "⚠️ No reconozco ese código.\nDebe tener 12 dígitos (ej. <code>001820005225</code>) "
            "o ser el código del artículo (ej. <code>0018-200</code>).",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    if datos.inventario.df is None:  # solo en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando inventario, un momento…")

    try:
        articulo = await _buscar_articulo(codigo7)
    except Exception as e:
        logger.error(f"Ubicaciones: error buscando el artículo {codigo7}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude consultar el inventario SAP.\n<code>{esc(e)}</code>",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    if articulo is None and modo != "consultar":
        await responder(
            update, context,
            f"❌ No encontré el artículo <b>{_formatear(codigo7)}</b> en el inventario SAP.\n"
            "Escanea otro código.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    codigo = articulo[0] if articulo else _formatear(codigo7)
    descripcion = articulo[1] if articulo else ""

    try:
        ubicaciones, descripcion_hoja = await _consultar_ubicaciones(codigo)
    except Exception as e:
        logger.error(f"Ubicaciones: error consultando la hoja para {codigo}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude consultar la hoja de ubicaciones.\n<code>{esc(e)}</code>",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    if articulo is None and not ubicaciones:  # consulta de un artículo que no existe
        await responder(
            update, context,
            f"❌ No encontré el artículo <b>{_formatear(codigo7)}</b> en el inventario SAP "
            "ni ubicaciones registradas.\nEscanea otro código.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    descripcion = descripcion or descripcion_hoja
    cabecera = _texto_articulo(codigo, descripcion)

    if modo == "consultar":
        await responder(
            update, context,
            f"{cabecera}\n{_detalle_ubicaciones(ubicaciones)}\n\nEscanea otro rollo o toca Volver.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    # Modificar: guarda el artículo y pide la ubicación
    ub["articulo"] = (codigo, descripcion)
    if modo == "reemplazar":
        if ubicaciones:
            aviso = f"⚠️ Se borrarán las ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
        else:
            aviso = "Aún no tiene ubicaciones registradas."
    else:
        if ubicaciones:
            aviso = f"Ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
        else:
            aviso = "Aún no tiene ubicaciones registradas."

    await responder(
        update, context,
        f"{cabecera}\n{aviso}\n\n📍 Escribe la <b>ubicación</b> (ej. <code>A-03-2</code>):",
        reply_markup=_teclado_volver(),
    )
    return UB_ESPERANDO_UBICACION


async def recibir_ubicacion_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.get("ub") or {}
    modo = ub.get("modo")
    articulo = ub.get("articulo")
    if modo not in ("reemplazar", "agregar") or not articulo:
        return await _sesion_expirada(update, context)

    entrada = update.message.text or ""
    if re.fullmatch(r"\d{12}", re.sub(r"\s", "", entrada)):
        await responder(
            update, context,
            "⚠️ Eso parece un código de barras, no una ubicación.\n"
            "Escribe la ubicación (ej. <code>A-03-2</code>).",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_UBICACION

    ubicacion = _normalizar_ubicacion(entrada)
    if ubicacion is None:
        await responder(
            update, context,
            "⚠️ Ubicación no válida. Usa letras, números, espacios, guion, punto, "
            "<code>/</code> o <code>#</code> (máximo 30 caracteres), por ejemplo <code>A-03-2</code>.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_UBICACION

    codigo, descripcion = articulo
    usuario = update.effective_user
    nombre = ""
    if usuario:
        nombre = usuario.full_name or (f"@{usuario.username}" if usuario.username else str(usuario.id))

    try:
        resp = await asyncio.to_thread(
            _llamar_ubicaciones,
            {
                "accion": "guardar",
                "modo": modo,
                "codigo": codigo,
                "descripcion": descripcion,
                "ubicacion": ubicacion,
                "nombre": nombre,
            },
        )
    except Exception as e:
        logger.error(f"Ubicaciones: error guardando {codigo} -> {ubicacion}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude guardar la ubicación.\n<code>{esc(e)}</code>\nPuedes intentar de nuevo.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_UBICACION

    registrar_uso_bot(update, f"UBICACION_{modo.upper()} {codigo} -> {ubicacion}")

    ubicaciones = resp.get("ubicaciones", [])
    borradas = int(resp.get("borradas", 0) or 0)

    if resp.get("duplicada"):
        titulo = f"ℹ️ <b>{esc(ubicacion)}</b> ya estaba registrada para este artículo."
    elif modo == "reemplazar":
        titulo = f"✅ <b>Ubicación registrada:</b> {esc(ubicacion)}"
        if borradas:
            titulo += f"\n🗑️ Se borraron {_plural(borradas, 'ubicación anterior', 'ubicaciones anteriores')}."
    else:
        titulo = f"✅ <b>Ubicación agregada:</b> {esc(ubicacion)}"

    ub.pop("articulo", None)
    await responder(
        update, context,
        f"{titulo}\n\n{_texto_articulo(codigo, descripcion)}"
        f"📍 <b>Ubicaciones:</b> {_nombres(ubicaciones)}\n\n"
        "Escanea el siguiente rollo o toca Volver.",
        reply_markup=_teclado_volver(),
    )
    return UB_ESPERANDO_CODIGO
