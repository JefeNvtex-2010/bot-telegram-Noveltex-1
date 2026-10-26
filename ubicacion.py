"""Módulo de ubicaciones de bodega (/ub).

Flujo:
  1) Menú: Consultar ubicación | Modificar ubicación.

  2) CONSULTAR (por nombre): se escribe la REFERENCIA (ej. SATIN), se elige la
     referencia si hay varias y luego el COLOR, igual que en Inventario SAP.
     Muestra las ubicaciones registradas del artículo.

  3) MODIFICAR (siempre por código): Registrar y borrar | Agregar.
     Se escanea (o se escribe) el código de barras del rollo, de 12 dígitos:
        001820005225  ->  artículo 0018-200  +  cantidad 05225 (se descarta)
     También se acepta el código del artículo solo (0018-200).
     Si el color en SAP no lleva el cero inicial (0018042 -> 0018-42), el bot
     prueba ambas formas al buscar el artículo.
     El bot busca el artículo en el inventario SAP (código y descripción), pide la
     ubicación y la guarda.
        - Registrar y borrar: elimina todas las ubicaciones del artículo y deja la nueva.
        - Agregar: suma otra ubicación sin borrar las que ya tiene.

     Después de cada rollo el bot pregunta "¿Continuar con la siguiente toma?":
        - Sí: queda esperando el siguiente rollo durante SEGUNDOS_TOMA segundos.
              Si no hay lectura en ese tiempo, vuelve a hacer la pregunta.
        - No: vuelve al menú principal.

Las ubicaciones se guardan en la pestaña "UBICACIONES BODEGA" del Google Sheet
AUDITORIA_BOT (FECHA | UBICACIÓN | CODIGO | DESCRIPCIÓN | NOMBRE), una fila por
cada ubicación del artículo. La escritura/lectura se hace a través de un Apps Script
(ver ubicaciones_apps_script.gs).

Variables de entorno (Render):
  URL_UBICACIONES     URL /exec del Apps Script de ubicaciones.
  TOKEN_UBICACIONES   Token (el mismo que la propiedad TOKEN del Apps Script).

Permisos (en la hoja PERMISOS, columna "acceso a"):
  "ubicaciones"            -> solo CONSULTAR ubicaciones.
  "modificar ubicaciones"  -> además puede Registrar y borrar / Agregar (administradores).
  "TODO"                   -> todo.
"""
import asyncio
import logging
import os
import re
import unicodedata
import uuid

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import datos
from acceso import requiere_acceso, tiene_acceso
from handlers import (
    _teclado_menu,
    editar,
    esc,
    programar_borrado,
    registrar_uso_bot,
    responder,
)

logger = logging.getLogger(__name__)

# Estados únicos y altos para no chocar con los de los demás módulos
(
    UB_MENU,
    UB_ESPERANDO_REF,
    UB_SELECCIONANDO_REF,
    UB_SELECCIONANDO_COLOR,
    UB_ESPERANDO_CODIGO,
    UB_ESPERANDO_UBICACION,
) = 40, 41, 42, 43, 44, 45

URL_UBICACIONES = os.environ.get("URL_UBICACIONES", "").strip()
TOKEN_UBICACIONES = os.environ.get("TOKEN_UBICACIONES", "").strip()

MODOS = ("consultar", "reemplazar", "agregar")
MODOS_MODIFICAR = ("reemplazar", "agregar")
PERMISO_MODIFICAR = "ubicaciones_modificar"
MENSAJE_SOLO_ADMIN = "Solo los administradores pueden modificar ubicaciones."
MAX_REFS = 20
MAX_COLORES = 60

# Segundos que tiene el usuario para leer el siguiente rollo después de decir "Sí"
SEGUNDOS_TOMA = 30

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


def _clave(codigo) -> str:
    """Código sin guiones ni espacios, en mayúsculas: '0018-200' -> '0018200'."""
    return re.sub(r"[^0-9A-Za-z]", "", str(codigo)).upper()


def _extraer_codigo(texto):
    """Devuelve los dígitos del artículo o None si el texto no es válido.

    - 12 dígitos (código de barras del rollo): 7 del artículo + 5 de cantidad (se descartan).
    - 7 dígitos (código del artículo, con o sin guion): se usa tal cual.
    - 6 dígitos (código SAP sin el cero del color, ej. 0018-42): se usa tal cual.
    """
    digitos = re.sub(r"\D", "", str(texto))
    if len(digitos) == 12:
        return digitos[:7]
    if len(digitos) in (6, 7):
        return digitos
    return None


def _formatear(codigo: str) -> str:
    """'0018200' -> '0018-200'  |  '001842' -> '0018-42'."""
    return f"{codigo[:4]}-{codigo[4:]}"


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


def _columna_codigo(df) -> str:
    col = _buscar_columna(df, COLUMNAS_CODIGO)
    if col is None:
        raise RuntimeError(
            "No encontré la columna del artículo en el inventario SAP "
            f"(columnas: {', '.join(map(str, df.columns))})"
        )
    return col


# --------------------------------------------------------------------------- #
# Inventario SAP
# --------------------------------------------------------------------------- #
_cache = {"firma": None, "indice": {}}


def _indice_articulos(df) -> dict:
    """{clave: (código SAP, descripción)}; se reconstruye solo si cambió el inventario."""
    firma = (id(df), len(df))
    if _cache["firma"] == firma:
        return _cache["indice"]

    col_cod = _columna_codigo(df)
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


async def _indice_inventario() -> dict:
    """Índice {clave: (código SAP, descripción)} del inventario SAP actual."""
    df = await datos.inventario.obtener()
    if df is None:
        raise RuntimeError(datos.inventario.ultimo_error or "No se pudo acceder al inventario")
    return _indice_articulos(df)


def _buscar_articulo(indice: dict, codigo: str):
    """Busca el artículo probando el código tal cual y sin el cero delante del color.

    0018042 -> prueba 0018042, luego 001842 (0018-42) y luego sin todos los ceros.
    """
    item, color = codigo[:4], codigo[4:]
    candidatos = [codigo]
    if color.startswith("0"):
        candidatos.append(item + color[1:])
        candidatos.append(item + color.lstrip("0"))
    for k in dict.fromkeys(candidatos):  # sin repetidos, conservando el orden
        if k in indice:
            return indice[k]
    return None


def _similares(indice: dict, codigo: str, maximo: int = 8):
    """Artículos del mismo ítem (primeros 4 dígitos): (lista limitada, total)."""
    prefijo = codigo[:4]
    coincidencias = sorted(
        (cod, desc) for k, (cod, desc) in indice.items() if k.startswith(prefijo)
    )
    return coincidencias[:maximo], len(coincidencias)


def _texto_no_encontrado(codigo: str, indice: dict) -> str:
    """Mensaje cuando el código no está en SAP, con pistas de qué sí hay del mismo ítem."""
    texto = f"❌ No encontré el artículo <b>{_formatear(codigo)}</b> en el inventario SAP."
    similares, total = _similares(indice, codigo)
    if similares:
        lineas = []
        for cod, desc in similares:
            lineas.append(f"• <code>{esc(cod)}</code>" + (f" — {esc(desc)}" if desc else ""))
        texto += f"\n\nDel ítem <b>{codigo[:4]}</b> sí hay en SAP:\n" + "\n".join(lineas)
        if total > len(similares):
            texto += f"\n… y {total - len(similares)} más"
    else:
        texto += f"\n\nNo hay ningún artículo del ítem <b>{codigo[:4]}</b> en el inventario SAP."
    return texto + "\n\nEscanea otro código."


def _articulo_de(df, ref: str, color: str):
    """Por referencia y color: (código SAP, descripción) o None."""
    filas = df[(df["_ref"] == ref) & (df["_color"] == color)]
    if filas.empty:
        return None
    codigo = _primer_texto(filas[_columna_codigo(df)])
    if not codigo:
        return None
    col_desc = _buscar_columna(df, COLUMNAS_DESCRIPCION)
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


def _uid(update: Update) -> int:
    usuario = update.effective_user
    return usuario.id if usuario else 0


def _puede_modificar(user_id: int) -> bool:
    return tiene_acceso(user_id, PERMISO_MODIFICAR)


def _pantalla_menu(puede_modificar: bool):
    """Menú principal; el botón de modificar solo aparece a quien tiene el permiso."""
    texto = "📍 <b>Ubicaciones de bodega</b>\n\n¿Qué deseas hacer?"
    filas = [[InlineKeyboardButton("🔎 Consultar ubicación", callback_data="ub_consultar")]]
    if puede_modificar:
        filas.append([InlineKeyboardButton("✏️ Modificar ubicación", callback_data="ub_modificar")])
    return texto, InlineKeyboardMarkup(filas)


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
    """Resultado de una consulta: volver a la lista de colores o al menú."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Volver a colores", callback_data="ub_back_cols")],
        _boton_menu(),
    ])


def _texto_pedir(modo: str) -> str:
    if modo == "consultar":
        return (
            "🔎 <b>Consultar ubicación</b>\n\n"
            "Escribe la <b>referencia</b> que deseas buscar (ej. <code>SATIN</code>):"
        )
    titulo = "🗑️ <b>Registrar y borrar</b>" if modo == "reemplazar" else "➕ <b>Agregar ubicación</b>"
    return (
        f"{titulo}\n\nEscanea el <b>código de barras</b> del rollo "
        "(o escribe el código del artículo, ej. <code>0018-200</code>):"
    )


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


def _pantalla_colores(ub: dict):
    colores = ub["colores"][:MAX_COLORES]
    filas = [
        [InlineKeyboardButton(color or "(sin color)", callback_data=f"ub_col_{i}")]
        for i, color in enumerate(colores)
    ]
    if len(ub.get("refs", [])) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="ub_back_refs")])
    filas.append(_boton_menu())

    texto = f"🔍 Referencia: <b>{esc(ub['ref'])}</b>.\nSelecciona un color:"
    if len(ub["colores"]) > MAX_COLORES:
        texto += f"\n(Mostrando {MAX_COLORES} de {len(ub['colores'])} colores)"
    return texto, InlineKeyboardMarkup(filas)


def _pantalla_continuar(resumen: str = ""):
    """Pregunta tras cada rollo: seguir con la siguiente toma o volver al menú."""
    texto = (resumen + "\n\n" if resumen else "") + "🔁 <b>¿Continuar con la siguiente toma?</b>"
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data="ub_seguir_si"),
        InlineKeyboardButton("🏁 No", callback_data="ub_seguir_no"),
    ]])
    return texto, teclado


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


# --------------------------------------------------------------------------- #
# Temporizador de la toma (30 s para leer el siguiente rollo)
# --------------------------------------------------------------------------- #
def _cancelar_temporizador(ub: dict) -> None:
    job = ub.pop("job", None)
    ub["token"] = None
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass  # el job ya había corrido o fue eliminado


async def _vencio_toma(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pasaron SEGUNDOS_TOMA sin lectura: se vuelve a hacer la pregunta."""
    d = context.job.data
    ub = d["ub"]
    actual = context.application.user_data.get(d["user_id"], {}).get("ub")
    if actual is not ub or ub.get("token") != d["token"]:
        return  # ya leyó un rollo, salió o empezó otra sesión
    ub["pausado"] = True
    ub["token"] = None
    ub.pop("job", None)
    texto, teclado = _pantalla_continuar(f"⏱️ Pasaron {SEGUNDOS_TOMA} segundos sin lectura.")
    try:
        await context.bot.edit_message_text(
            chat_id=d["chat_id"],
            message_id=d["message_id"],
            text=texto,
            reply_markup=teclado,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"Ubicaciones: no pude re-preguntar tras el tiempo de espera: {e}")


def _iniciar_temporizador(context: ContextTypes.DEFAULT_TYPE, ub: dict, mensaje, user_id: int) -> None:
    _cancelar_temporizador(ub)
    if context.job_queue is None or mensaje is None:
        return
    token = uuid.uuid4().hex
    ub["token"] = token
    ub["job"] = context.job_queue.run_once(
        _vencio_toma,
        SEGUNDOS_TOMA,
        data={
            "ub": ub,
            "token": token,
            "user_id": user_id,
            "chat_id": mensaje.chat_id,
            "message_id": mensaje.message_id,
        },
    )


# --------------------------------------------------------------------------- #
# Sesión expirada / sin permiso
# --------------------------------------------------------------------------- #
async def _sesion_expirada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sesión perdida (por ejemplo tras un reinicio) al recibir un texto."""
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu(_puede_modificar(_uid(update)))
    await responder(update, context, "⌛ La sesión expiró.\n\n" + texto, reply_markup=teclado)
    return UB_MENU


async def _expirada_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sesión perdida al pulsar un botón."""
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu(_puede_modificar(_uid(update)))
    await editar(update.callback_query, "⌛ La sesión expiró.\n\n" + texto, teclado)
    return UB_MENU


async def _sin_permiso_modificar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Texto recibido de alguien que ya no puede modificar (permiso quitado a mitad de camino)."""
    logger.warning(f"Ubicaciones: modificación denegada a {_uid(update)}")
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu(False)
    await responder(update, context, f"⛔ {MENSAJE_SOLO_ADMIN}\n\n{texto}", reply_markup=teclado)
    return UB_MENU


# --------------------------------------------------------------------------- #
# Handlers: menú
# --------------------------------------------------------------------------- #
@requiere_acceso("ubicaciones")
async def iniciar_ubicaciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data["ub"] = {}
    texto, teclado = _pantalla_menu(_puede_modificar(_uid(update)))

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
    accion = query.data.split("_", 1)[1]
    puede_modificar = _puede_modificar(_uid(update))
    ub = context.user_data.setdefault("ub", {})

    # Modificar solo para administradores (también si pulsan un botón viejo)
    if accion == "modificar" or accion in MODOS_MODIFICAR:
        if not puede_modificar:
            logger.warning(f"Ubicaciones: modificación denegada a {_uid(update)}")
            await query.answer(MENSAJE_SOLO_ADMIN, show_alert=True)
            _cancelar_temporizador(ub)
            ub.clear()
            texto, teclado = _pantalla_menu(False)
            await editar(query, texto, teclado)
            return UB_MENU

    await query.answer()
    _cancelar_temporizador(ub)  # cualquier botón del menú corta la espera del rollo
    ub.clear()

    if accion == "modificar":
        texto, teclado = _pantalla_modificar()
        await editar(query, texto, teclado)
        return UB_MENU

    if accion == "consultar":  # por nombre: referencia y color
        ub["modo"] = accion
        await editar(query, _texto_pedir(accion), _teclado_volver())
        return UB_ESPERANDO_REF

    if accion in MODOS_MODIFICAR:  # por código de barras
        ub["modo"] = accion
        await editar(query, _texto_pedir(accion), _teclado_volver())
        return UB_ESPERANDO_CODIGO

    texto, teclado = _pantalla_menu(puede_modificar)  # menu (y cualquier otra acción)
    await editar(query, texto, teclado)
    return UB_MENU


async def recordar_botones_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Texto escrito mientras se espera elegir una opción del menú."""
    programar_borrado(context, update.message)
    await responder(update, context, "☝️ Elige una opción con los botones del menú de ubicaciones.")
    return UB_MENU


# --------------------------------------------------------------------------- #
# Handlers: CONSULTAR (por referencia y color)
# --------------------------------------------------------------------------- #
async def recibir_ref_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Referencia escrita: busca en el inventario SAP y muestra referencias o colores."""
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    if ub.get("modo") != "consultar":
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
    ub.update({"modo": "consultar", "termino": termino, "refs": refs[:MAX_REFS], "total": len(refs)})

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
    if ub.get("modo") != "consultar" or not ub.get("refs") or df is None:
        return await _expirada_cb(update, context)
    try:
        ref = ub["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada_cb(update, context)

    ub["ref"] = ref
    ub["colores"] = datos.colores_de(df, ref)
    texto, teclado = _pantalla_colores(ub)
    await editar(query, texto, teclado)
    return UB_SELECCIONANDO_COLOR


async def seleccionar_color_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botones: ub_col_N | ub_back_refs | ub_back_cols (solo consulta)."""
    query = update.callback_query
    await query.answer()
    ub = context.user_data.get("ub") or {}
    if ub.get("modo") != "consultar" or not ub.get("ref") or not ub.get("colores"):
        return await _expirada_cb(update, context)
    data = query.data

    if data == "ub_back_refs":
        if not ub.get("refs"):
            return await _expirada_cb(update, context)
        texto, teclado = _pantalla_refs(ub)
        await editar(query, texto, teclado)
        return UB_SELECCIONANDO_REF

    if data == "ub_back_cols":
        texto, teclado = _pantalla_colores(ub)
        await editar(query, texto, teclado)
        return UB_SELECCIONANDO_COLOR

    try:
        color = ub["colores"][int(data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada_cb(update, context)

    df = await datos.inventario.obtener()
    if df is None:
        return await _expirada_cb(update, context)

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

    await editar(
        query,
        f"{_texto_articulo(codigo, descripcion)}\n{_detalle_ubicaciones(ubicaciones)}",
        _teclado_resultado(),
    )
    return UB_SELECCIONANDO_COLOR


# --------------------------------------------------------------------------- #
# Handlers: MODIFICAR (siempre por código de barras)
# --------------------------------------------------------------------------- #
async def seguir_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Botones: ub_seguir_si | ub_seguir_no."""
    query = update.callback_query
    await query.answer()
    ub = context.user_data.get("ub") or {}
    if ub.get("modo") not in MODOS_MODIFICAR:
        return await _expirada_cb(update, context)
    if not _puede_modificar(_uid(update)):
        logger.warning(f"Ubicaciones: modificación denegada a {_uid(update)}")
        await query.answer(MENSAJE_SOLO_ADMIN, show_alert=True)
        return await _expirada_cb(update, context)

    if query.data == "ub_seguir_no":  # terminar: volver al menú principal
        _cancelar_temporizador(ub)
        context.user_data.pop("ub", None)
        teclado = _teclado_menu(_uid(update))
        if teclado is None:
            texto = "⛔ Todavía no tienes funciones habilitadas."
        else:
            texto = "👋 ¡Hola! Selecciona una opción del menú principal para comenzar:"
        await editar(query, texto, teclado)
        return ConversationHandler.END

    # Sí: queda esperando el siguiente rollo, con límite de tiempo
    ub["pausado"] = False
    ub.pop("articulo", None)
    _iniciar_temporizador(context, ub, query.message, _uid(update))
    await editar(
        query,
        f"📷 Escanea el siguiente rollo (tienes {SEGUNDOS_TOMA} segundos).",
        _teclado_volver(),
    )
    return UB_ESPERANDO_CODIGO


async def recibir_codigo_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    modo = ub.get("modo")
    if modo not in MODOS_MODIFICAR:
        return await _sesion_expirada(update, context)
    if not _puede_modificar(_uid(update)):
        return await _sin_permiso_modificar(update, context)

    if ub.get("pausado"):  # hay una pregunta pendiente: no se lee hasta responder
        texto, teclado = _pantalla_continuar()
        await responder(update, context, "⏸️ Primero responde:\n\n" + texto, reply_markup=teclado)
        return UB_ESPERANDO_CODIGO

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
        indice = await _indice_inventario()
    except Exception as e:
        logger.error(f"Ubicaciones: error buscando el artículo {codigo7}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude consultar el inventario SAP.\n<code>{esc(e)}</code>",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    articulo = _buscar_articulo(indice, codigo7)  # prueba con y sin el cero del color
    if articulo is None:
        logger.warning(
            f"Ubicaciones: el artículo {_formatear(codigo7)} no está en el inventario SAP "
            f"({len(indice)} artículos cargados)"
        )
        await responder(
            update, context,
            _texto_no_encontrado(codigo7, indice),
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    _cancelar_temporizador(ub)  # leyó a tiempo: ya no hace falta el reloj

    codigo, descripcion = articulo
    try:
        ubicaciones, _ = await _consultar_ubicaciones(codigo)
    except Exception as e:
        logger.error(f"Ubicaciones: error consultando la hoja para {codigo}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude consultar la hoja de ubicaciones.\n<code>{esc(e)}</code>",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    ub["articulo"] = (codigo, descripcion)
    if ubicaciones:
        if modo == "reemplazar":
            aviso = f"⚠️ Se borrarán las ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
        else:
            aviso = f"Ubicaciones actuales: <b>{_nombres(ubicaciones)}</b>"
    else:
        aviso = "Aún no tiene ubicaciones registradas."

    await responder(
        update, context,
        f"{_texto_articulo(codigo, descripcion)}\n{aviso}\n\n"
        "📍 Escribe la <b>ubicación</b> (ej. <code>A-03-2</code>):",
        reply_markup=_teclado_volver(),
    )
    return UB_ESPERANDO_UBICACION


async def recibir_ubicacion_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.get("ub") or {}
    modo = ub.get("modo")
    articulo = ub.get("articulo")
    if modo not in MODOS_MODIFICAR or not articulo:
        return await _sesion_expirada(update, context)
    if not _puede_modificar(_uid(update)):
        return await _sin_permiso_modificar(update, context)

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
    ub["pausado"] = True  # queda la pregunta "¿Continuar?" pendiente
    resumen = (
        f"{titulo}\n\n{_texto_articulo(codigo, descripcion)}"
        f"📍 <b>Ubicaciones:</b> {_nombres(ubicaciones)}"
    )
    texto, teclado = _pantalla_continuar(resumen)
    await responder(update, context, texto, reply_markup=teclado)
    return UB_ESPERANDO_CODIGO
