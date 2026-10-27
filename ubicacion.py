import asyncio
import csv
import io
import logging
import os
import re
import unicodedata
import uuid
import urllib.request

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
    UB_ESPERANDO_CAJON,
    UB_ESPERANDO_ID,  # <--- Estado para solicitar el ID de operario
) = 40, 41, 42, 43, 44, 45, 46, 47

URL_UBICACIONES = os.environ.get("URL_UBICACIONES", "").strip()
TOKEN_UBICACIONES = os.environ.get("TOKEN_UBICACIONES", "").strip()

# Google Sheet AUDITORIA_BOT, pestaña ETIQUETAS (columnas: OPERARIO | ID)
SHEET_ID_OPERARIOS = os.environ.get(
    "SHEET_ID_AUDITORIA", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
)
PESTANA_OPERARIOS = "ETIQUETAS"

MODOS = ("consultar", "reemplazar", "agregar", "cajon")
MODOS_MODIFICAR = ("reemplazar", "agregar")
PERMISO_MODIFICAR = "ubicaciones_modificar"
MENSAJE_SOLO_ADMIN = "Solo los administradores pueden modificar ubicaciones."
MAX_REFS = 20
MAX_COLORES = 60

# Segundos que tiene el usuario para leer el siguiente rollo después de decir "Sí"
SEGUNDOS_TOMA = 30

COLUMNAS_CODIGO = ("articulo", "codigo articulo", "codigo del articulo", "itemcode", "item code")
COLUMNAS_DESCRIPCION = ("descripcion", "descripcion articulo", "nombre articulo", "itemname", "item name")

_PATRON_UBICACION = re.compile(r"^[A-Z0-9Ñ][A-Z0-9Ñ \-./#]{0,29}$")


# --------------------------------------------------------------------------- #
# Utilidades y Carga de Operarios (Pestaña ETIQUETAS)
# --------------------------------------------------------------------------- #
def _cargar_operarios() -> dict:
    """Lee la pestaña UBICACIONES y devuelve {id: nombre}. Bloqueante: usar con to_thread."""
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
    for fila in filas[1:]:  # salta el encabezado OPERARIO | ID
        if len(fila) < 2:
            continue
        nombre = fila[0].strip()
        id_op = re.sub(r"\D", "", fila[1])
        if nombre and id_op:
            operarios[id_op] = nombre
    return operarios


def _sin_acentos(texto) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn"
    )


def _clave(codigo) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(codigo)).upper()


def _extraer_codigo(texto):
    digitos = re.sub(r"\D", "", str(texto))
    if len(digitos) == 12:
        return digitos[:7]
    if len(digitos) in (6, 7):
        return digitos
    return None


def _formatear(codigo: str) -> str:
    return f"{codigo[:4]}-{codigo[4:]}"


def _normalizar_ubicacion(texto):
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
    df = await datos.inventario.obtener()
    if df is None:
        raise RuntimeError(datos.inventario.ultimo_error or "No se pudo acceder al inventario")
    return _indice_articulos(df)


def _buscar_articulo(indice: dict, codigo: str):
    item, color = codigo[:4], codigo[4:]
    candidatos = [codigo]
    if color.startswith("0"):
        candidatos.append(item + color[1:])
        candidatos.append(item + color.lstrip("0"))
    for k in dict.fromkeys(candidatos):
        if k in indice:
            return indice[k]
    return None


def _similares(indice: dict, codigo: str, maximo: int = 8):
    prefijo = codigo[:4]
    coincidencias = sorted(
        (cod, desc) for k, (cod, desc) in indice.items() if k.startswith(prefijo)
    )
    return coincidencias[:maximo], len(coincidencias)


def _texto_no_encontrado(codigo: str, indice: dict) -> str:
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
    resp = await asyncio.to_thread(_llamar_ubicaciones, {"accion": "consultar", "codigo": codigo})
    return resp.get("ubicaciones", []), resp.get("descripcion", "")


async def _consultar_por_ubicacion(ubicacion: str):
    resp = await asyncio.to_thread(
        _llamar_ubicaciones, {"accion": "consultar_ubicacion", "ubicacion": ubicacion}
    )
    return resp.get("articulos", [])


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
    texto = "📍 <b>Ubicaciones de bodega</b>\n\n¿Qué deseas hacer?"
    filas = [
        [InlineKeyboardButton("🔎 Consultar por Artículo", callback_data="ub_consultar")],
        [InlineKeyboardButton("📦 Consulta por Cajón", callback_data="ub_cajon")],
    ]
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Volver a colores", callback_data="ub_back_cols")],
        _boton_menu(),
    ])


def _texto_pedir(modo: str) -> str:
    if modo == "consultar":
        return (
            "🔎 <b>Consultar ubicación por Artículo</b>\n\n"
            "Escribe la <b>referencia</b> que deseas buscar (ej. <code>SATIN</code>):"
        )
    if modo == "cajon":
        return (
            "📦 <b>Consulta por Cajón</b>\n\n"
            "Escribe o ingresa la <b>ubicación/cajón</b> que deseas consultar "
            "(ej. <code>B/1 P/6 C/1</code>):"
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
# Temporizador
# --------------------------------------------------------------------------- #
def _cancelar_temporizador(ub: dict) -> None:
    job = ub.pop("job", None)
    ub["token"] = None
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass


async def _vencio_toma(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    ub = d["ub"]
    actual = context.application.user_data.get(d["user_id"], {}).get("ub")
    if actual is not ub or ub.get("token") != d["token"]:
        return
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
# Sesiones y Permisos
# --------------------------------------------------------------------------- #
async def _sesion_expirada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu(_puede_modificar(_uid(update)))
    await responder(update, context, "⌛ La sesión expiró.\n\n" + texto, reply_markup=teclado)
    return UB_MENU


async def _expirada_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cancelar_temporizador(context.user_data.get("ub") or {})
    context.user_data.pop("ub", None)
    texto, teclado = _pantalla_menu(_puede_modificar(_uid(update)))
    await editar(update.callback_query, "⌛ La sesión expiró.\n\n" + texto, teclado)
    return UB_MENU


# --------------------------------------------------------------------------- #
# Handlers: Menú e Identificación por ID en pestaña ETIQUETAS
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
    query = update.callback_query
    accion = query.data.split("_", 1)[1]
    puede_modificar = _puede_modificar(_uid(update))
    ub = context.user_data.setdefault("ub", {})

    await query.answer()
    _cancelar_temporizador(ub)
    ub.clear()

    # Al presionar modificar, solicitamos el ID de operario de la pestaña ETIQUETAS
    if accion == "modificar":
        ub["modo_pendiente"] = "modificar"
        await editar(
            query,
            "🔐 <b>Validación de Operario</b>\n\nPor favor, escribe tu <b>ID</b> (registrado en el sistema) para continuar:",
            _teclado_volver()
        )
        return UB_ESPERANDO_ID

    if accion == "consultar":
        ub["modo"] = accion
        await editar(query, _texto_pedir(accion), _teclado_volver())
        return UB_ESPERANDO_REF

    if accion == "cajon":
        ub["modo"] = accion
        await editar(query, _texto_pedir(accion), _teclado_volver())
        return UB_ESPERANDO_CAJON

    if accion in MODOS_MODIFICAR:
        ub["modo"] = accion
        await editar(query, _texto_pedir(accion), _teclado_volver())
        return UB_ESPERANDO_CODIGO

    texto, teclado = _pantalla_menu(puede_modificar)
    await editar(query, texto, teclado)
    return UB_MENU


async def recibir_id_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Valida el ID contra la pestaña ETIQUETAS usando la misma lógica de etiquetas.py."""
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    id_txt = re.sub(r"\D", "", update.message.text or "")

    if not id_txt:
        await responder(
            update, context,
            "⚠️ Por favor, escribe un ID válido:",
            reply_markup=_teclado_volver()
        )
        return UB_ESPERANDO_ID

    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception as e:
        logger.error(f"Ubicaciones: no se pudo leer la lista de operarios: {e}", exc_info=True)
        await responder(
            update, context,
            "⚠️ No pude consultar la lista de operarios. Intenta de nuevo:",
            reply_markup=_teclado_volver()
        )
        return UB_ESPERANDO_ID

    nombre = operarios.get(id_txt)
    if not nombre:
        await responder(
            update, context,
            "❌ <b>ID no autorizado.</b> El ID ingresado no está registrado en la pestaña ETIQUETAS. Intenta de nuevo:",
            reply_markup=_teclado_volver()
        )
        return UB_ESPERANDO_ID

    # Guardamos el nombre validado para registrarlo luego en las auditorías/modificaciones
    ub["operario_validado"] = nombre
    ub.pop("modo_pendiente", None)

    texto, teclado = _pantalla_modificar()
    await responder(
        update, context,
        f"✅ ¡Hola, <b>{esc(nombre)}</b>! ID verificado correctamente.\n\n{texto}",
        reply_markup=teclado
    )
    return UB_MENU


async def recordar_botones_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    await responder(update, context, "☝️ Elige una opción con los botones del menú de ubicaciones.")
    return UB_MENU


# --------------------------------------------------------------------------- #
# Consultar por Cajón / Referencia
# --------------------------------------------------------------------------- #
async def recibir_cajon_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    if ub.get("modo") != "cajon":
        return await _sesion_expirada(update, context)

    entrada = update.message.text or ""
    ubicacion = _normalizar_ubicacion(entrada)
    if not ubicacion:
        await responder(
            update, context,
            "⚠️ Ubicación no válida. Escribe la ubicación correctamente (ej. <code>B/1 P/6 C/1</code>).",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CAJON

    try:
        articulos = await _consultar_por_ubicacion(ubicacion)
    except Exception as e:
        logger.error(f"Ubicaciones: error consultando el cajón {ubicacion}: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude consultar la ubicación en la hoja.\n<code>{esc(e)}</code>",
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CAJON

    if not articulos:
        texto = f"❌ No se encontraron artículos registrados en la ubicación <b>{esc(ubicacion)}</b>."
    else:
        lineas = []
        for art in articulos:
            codigo = art.get("codigo", "")
            desc = art.get("descripcion", "")
            lineas.append(f"• <code>{esc(codigo)}</code>" + (f" — {esc(desc)}" if desc else ""))

        texto = (
            f"📦 <b>Artículos en la ubicación {esc(ubicacion)}:</b>\n\n"
            + "\n".join(lineas)
            + f"\n\nTotal de registros: <b>{len(articulos)}</b>"
        )

    await responder(update, context, texto, reply_markup=_teclado_volver())
    return UB_ESPERANDO_CAJON


async def recibir_ref_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    ub = context.user_data.setdefault("ub", {})
    if ub.get("modo") != "consultar":
        return await _sesion_expirada(update, context)

    termino = (update.message.text or "").strip().upper()
    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.", reply_markup=_teclado_volver())
        return UB_ESPERANDO_REF

    if datos.inventario.df is None:
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
# Modificar / Guardar ubicaciones
# --------------------------------------------------------------------------- #
async def seguir_ub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ub = context.user_data.get("ub") or {}
    if ub.get("modo") not in MODOS_MODIFICAR:
        return await _expirada_cb(update, context)

    if query.data == "ub_seguir_no":
        _cancelar_temporizador(ub)
        context.user_data.pop("ub", None)
        teclado = _teclado_menu(_uid(update))
        if teclado is None:
            texto = "⛔ Todavía no tienes funciones habilitadas."
        else:
            texto = "👋 ¡Hola! Selecciona una opción del menú principal para comenzar:"
        await editar(query, texto, teclado)
        return ConversationHandler.END

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

    if ub.get("pausado"):
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

    if datos.inventario.df is None:
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

    articulo = _buscar_articulo(indice, codigo7)
    if articulo is None:
        await responder(
            update, context,
            _texto_no_encontrado(codigo7, indice),
            reply_markup=_teclado_volver(),
        )
        return UB_ESPERANDO_CODIGO

    _cancelar_temporizador(ub)

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
    # Usamos el nombre validado previamente desde la pestaña ETIQUETAS
    nombre = ub.get("operario_validado", "")
    if not nombre:
        usuario = update.effective_user
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
    ub["pausado"] = True
    resumen = (
        f"{titulo}\n\n{_texto_articulo(codigo, descripcion)}"
        f"📍 <b>Ubicaciones:</b> {_nombres(ubicaciones)}"
    )
    texto, teclado = _pantalla_continuar(resumen)
    await responder(update, context, texto, reply_markup=teclado)
    return UB_ESPERANDO_CODIGO
