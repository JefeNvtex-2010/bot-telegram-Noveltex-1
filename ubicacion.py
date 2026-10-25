"""Módulo de ubicaciones de bodega (/ub).

Flujo:
  1) Menú: Consultar ubicación | Modificar ubicación.
  2) Modificar -> Registrar y borrar (deja solo la ubicación nueva)
               -> Agregar (suma otra ubicación sin borrar las que ya tiene).
  3) Se escribe la REFERENCIA (ej. SATIN), se elige la referencia si hay varias y
     luego el COLOR (igual que en Inventario SAP). El bot toma del inventario SAP el
     código y la descripción del artículo de esa referencia y color.
  4) Consultar: muestra las ubicaciones registradas.
     Registrar y borrar / Agregar: pide la ubicación y la guarda; después deja
     elegir otro color de la misma referencia (o escribir otra referencia).

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
(
    UB_MENU,
    UB_ESPERANDO_REF,
    UB_SELECCIONANDO_REF,
    UB_SELECCIONANDO_COLOR,
    UB_ESPERANDO_UBICACION,
) = 40, 41, 42, 43, 44

URL_UBICACIONES = os.environ.get("URL_UBICACIONES", "").strip()
TOKEN_UBICACIONES = os.environ.get("TOKEN_UBICACIONES", "").strip()

MODOS = ("consultar", "reemplazar", "agregar")
MAX_REFS = 20
MAX_COLORES = 60

# Nombres posibles de las columnas del inventario SAP (sin tildes, minúsculas)
COLUMNAS_CODIGO = ("articulo", "codigo articulo", "codigo del articulo", "itemcode", "item code")
COLUMNAS_DESCRIPCION = ("descripcion", "descripcion articulo", "nombre articulo", "itemname", "item name")

_PATRON_UBICACION = re.compile(r"^[A-Z0-9Ñ][A-Z0-9Ñ \-./#]{0,29}$")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _sin_acentos(texto) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn"
    )


def _normalizar_ubicacion(texto):
    """Ubicación limpia en mayúsculas, o None si no es válida."""
    t = re.sub(r"\s+", " ", str(texto).strip().upper())
    return t if _PATRON_UBICACION.match(t) else None


def _buscar_columna(df, candidatos):
    normales = {_sin_acentos(c).lower().strip(): c for c in df.columns}
    for cand in candidatos:
        if cand in normales:
            return normales[cand]
    return None


def _primer_texto(serie) -> str:
    for v in serie.astype(str):
        v = v.strip()
        if v and v.lower() != "nan":
            return v
    return ""


def _articulo_de(df, ref: str, color: str):
    """(código SAP, descripción) del artículo de esa referencia y color, o None."""
    filas = df[(df["_ref"] == ref) & (df["_color"] == color)]
    if filas.empty:
        return None
    col_cod = _buscar_columna(df, COLUMNAS_CODIGO)
    if col_cod is None:
        raise RuntimeError(
            "No encontré la columna del artículo en el inventario SAP "
            f"(columnas: {', '.join(map(str, df.columns))})"
        )
    col_desc = _buscar_columna(df, COLUMNAS_DESCRIPCION)

    codigo = _primer_texto(filas[col_cod])
    if not codigo:
        return None
    descripcion = _primer_texto(filas[col_desc]) if col_desc else ""
    return codigo, descripcion or f"{ref} - {color}"


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
def _boton_menu():
    return [InlineKeyboardButton("🔙 Menú de ubicaciones", callback_data="ub_menu")]


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
    return InlineKeyboardMarkup([_boton_menu()])


def _teclado_resultado():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Volver a colores", callback_data="ub_back_cols")],
        _boton_menu(),
    ])


def _texto_pedir_ref(modo: str) -> str:
    titulo = {
        "consultar": "🔎 <b>Consultar ubicación</b>",
        "reemplazar": "🗑️ <b>Registrar y borrar</b>",
        "agregar": "➕ <b>Agregar ubicación</b>",
    }[modo]
    return f"{titulo}\n\nEscribe la <b>referencia</b> que deseas buscar (ej. <code>SATIN</code>):"


def _pantalla_refs(ub: dict):
    texto = (
        f"🔍 Encontré varias referencias para <b>{esc(ub['termino'])}</b>.\n"
        "Selecciona una referencia:"
    )
    if ub["total"] > len(ub["refs"]):
        texto += (
            f"\n(Mostrando {len(ub['refs'])} de {ub['total']}; "
            "escribe una referencia más específica)"
        )
    filas = [
        [InlineKeyboardButton(ref or "(sin referencia)", callback_data=f"ub_ref_{i}")]
        for i, ref in enumerate(ub["refs"])
    ]
    filas.append(_boton_menu())
    return texto, InlineKeyboardMarkup(filas)


def _pantalla_colores(ub: dict, continuar: bool = False):
    colores = ub["colores"][:MAX_COLORES]
    filas = [
        [InlineKeyboardButton(color or "(sin color)", callback_data=f"ub_col_{i}")]
        for i, color in enumerate(colores)
    ]
    if len(ub.get("refs", [])) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="ub_back_refs")])
    filas.append(_boton_menu())

    if continuar:
        texto = (
            f"Selecciona otro color de <b>{esc(ub['ref'])}</b> "
            "o escribe otra referencia:"
        )
    else:
        texto = f"🔍 Referencia: <b>{esc(ub['ref'])}</b>.\nSelecciona un color:"
    if len(ub["colores"]) > MAX_COLORES:
        texto += f"\n(Mostrando {MAX_COLORES} de {len(ub['colores'])} colores)"
    return texto, InlineKeyboardMarkup(filas)


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
    """Sesión perdida (por ejemplo tras un reinicio) al recibir un texto."""
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu()
    await responder(update, context, "⌛ La sesión expiró.\n\n" + texto, reply_markup=teclado)
    return UB_MENU


async def _expirada_cb(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sesión perdida al pulsar un botón."""
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu()
    await editar(query, "⌛ La sesión expiró.\n\n" + texto, teclado)
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
    """Botones: ub_menu | ub_consultar | ub_modificar | ub_reemplazar | ub_agregar."""
    query = update.callback_query
    await query.answer()
    accion = query.data.split("_", 1)[1]
    ub = context.user_data.setdefault("ub", {})
    ub.clear()

    if accion == "modificar":
        texto, teclado = _pantalla_modificar()
        await editar(query, texto, teclado)
        return UB_MENU

    if accion in MODOS:  # consultar | reemplazar | agregar
        ub["modo"] = accion
        await editar(query, _texto_pedir_ref(accion), _teclado_volver())
        return UB_ESPERANDO_REF

    texto, teclado = _pantalla_menu()  # menu (y cualquier otra acción)
    await editar(query, texto, teclado)
    return UB_MENU


async def recordar_botones_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Texto escrito mientras se espera elegir una opción del menú."""
    programar_borrado(context, update.message)
    await responder(update, context, "☝️ Elige una opción con los botones del menú de ubicaciones.")
    return UB_MENU


async def recibir_ref_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Referencia escrita: busca en el inventario SAP y muestra referencias o colores."""
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    modo = ub.get("modo")
    if modo not in MODOS:
        return await _sesion_expirada(update, context)

    termino = (update.message.text or "").strip().upper()
    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.", reply_markup=_teclado_volver())
        return UB_ESPERANDO_REF

    if datos.inventario.df is None:  # solo en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando inventario, un momento…")

    df = await datos.inventario.obtener()
    if df is None:
        detalle = f"\n<code>{esc(datos.inventario.ultimo_error)}</code>" if datos.inventario.ultimo_error else ""
        await responder(
            update, context, f"⚠️ No se pudo acceder al inventario.{detalle}",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_REF

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await responder(
            update, context,
            f"❌ No se encontró: <b>{esc(termino)}</b>.\nPuedes escribir otra referencia.",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_REF

    # Búsqueda nueva: se conserva solo el modo
    ub.clear()
    ub.update({"modo": modo, "termino": termino, "refs": refs[:MAX_REFS], "total": len(refs)})

    if len(refs) == 1:
        ub["ref"] = refs[0]
        ub["colores"] = datos.colores_de(df, refs[0])
        texto, teclado = _pantalla_colores(ub)
        await responder(update, context, texto, reply_markup=teclado)
        return UB_SELECCIONANDO_COLOR

    texto, teclado = _pantalla_refs(ub)
    await responder(update, context, texto, reply_markup=teclado)
    return UB_SELECCIONANDO_REF


async def seleccionar_ref_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ub = context.user_data.get("ub") or {}
    df = await datos.inventario.obtener()
    if ub.get("modo") not in MODOS or not ub.get("refs") or df is None:
        return await _expirada_cb(query, context)
    try:
        ref = ub["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada_cb(query, context)

    ub["ref"] = ref
    ub["colores"] = datos.colores_de(df, ref)
    texto, teclado = _pantalla_colores(ub)
    await editar(query, texto, teclado)
    return UB_SELECCIONANDO_COLOR


async def seleccionar_color_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botones: ub_col_N | ub_back_refs | ub_back_cols."""
    query = update.callback_query
    await query.answer()
    ub = context.user_data.get("ub") or {}
    modo = ub.get("modo")
    if modo not in MODOS or not ub.get("ref") or not ub.get("colores"):
        return await _expirada_cb(query, context)
    data = query.data

    if data == "ub_back_refs":
        if not ub.get("refs"):
            return await _expirada_cb(query, context)
        ub.pop("articulo", None)
        texto, teclado = _pantalla_refs(ub)
        await editar(query, texto, teclado)
        return UB_SELECCIONANDO_REF

    if data == "ub_back_cols":
        ub.pop("articulo", None)
        texto, teclado = _pantalla_colores(ub)
        await editar(query, texto, teclado)
        return UB_SELECCIONANDO_COLOR

    try:
        color = ub["colores"][int(data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada_cb(query, context)

    df = await datos.inventario.obtener()
    if df is None:
        return await _expirada_cb(query, context)

    try:
        articulo = _articulo_de(df, ub["ref"], color)
    except Exception as e:
        logger.error(f"Ubicaciones: error resolviendo el artículo: {e}", exc_info=True)
        await editar(query, f"⚠️ No pude identificar el artículo.\n<code>{esc(e)}</code>", _teclado_resultado())
        return UB_SELECCIONANDO_COLOR

    if articulo is None:
        await editar(
            query,
            f"❌ No encontré el artículo de <b>{esc(ub['ref'])}</b> color <b>{esc(color)}</b> "
            "en el inventario SAP.",
            _teclado_resultado(),
        )
        return UB_SELECCIONANDO_COLOR

    codigo, descripcion = articulo
    try:
        ubicaciones, _ = await _consultar_ubicaciones(codigo)
    except Exception as e:
        logger.error(f"Ubicaciones: error consultando la hoja para {codigo}: {e}", exc_info=True)
        await editar(
            query,
            f"⚠️ No pude consultar la hoja de ubicaciones.\n<code>{esc(e)}</code>",
            _teclado_resultado(),
        )
        return UB_SELECCIONANDO_COLOR

    cabecera = _texto_articulo(codigo, descripcion)

    if modo == "consultar":
        await editar(query, f"{cabecera}\n{_detalle_ubicaciones(ubicaciones)}", _teclado_resultado())
        return UB_SELECCIONANDO_COLOR

    # Modificar: guarda el artículo y pide la ubicación
    ub["articulo"] = (codigo, descripcion)
    if ubicaciones:
        if modo == "reemplazar":
            aviso = f"⚠️ Se borrarán las ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
        else:
            aviso = f"Ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
    else:
        aviso = "Aún no tiene ubicaciones registradas."

    await editar(
        query,
        f"{cabecera}\n{aviso}\n\n📍 Escribe la <b>ubicación</b> (ej. <code>A-03-2</code>):",
        _teclado_resultado(),
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
            reply_markup=_teclado_resultado(),
        )
        return UB_ESPERANDO_UBICACION

    ubicacion = _normalizar_ubicacion(entrada)
    if ubicacion is None:
        await responder(
            update, context,
            "⚠️ Ubicación no válida. Usa letras, números, espacios, guion, punto, "
            "<code>/</code> o <code>#</code> (máximo 30 caracteres), por ejemplo <code>A-03-2</code>.",
            reply_markup=_teclado_resultado(),
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
            reply_markup=_teclado_resultado(),
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
    confirmacion = (
        f"{titulo}\n\n{_texto_articulo(codigo, descripcion)}"
        f"📍 <b>Ubicaciones:</b> {_nombres(ubicaciones)}"
    )

    # Sigue con otro color de la misma referencia (o una referencia nueva escrita)
    if ub.get("ref") and ub.get("colores"):
        texto_colores, teclado = _pantalla_colores(ub, continuar=True)
        await responder(update, context, f"{confirmacion}\n\n{texto_colores}", reply_markup=teclado)
        return UB_SELECCIONANDO_COLOR

    await responder(
        update, context,
        f"{confirmacion}\n\nEscribe otra referencia o toca Volver.",
        reply_markup=_teclado_volver(),
    )
    return UB_ESPERANDO_REF
