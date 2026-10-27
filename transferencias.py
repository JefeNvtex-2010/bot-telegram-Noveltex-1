import asyncio
import csv
import io
import logging
import os
import re
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

# Estados del módulo Transferencias
(
    TR_MENU,
    TR_ESPERANDO_ID,      # <--- 1. Solicitamos el ID primero al entrar al módulo
    TR_ELEGIR_METODO,
    TR_ESPERANDO_CODIGO,
    TR_ESPERANDO_REF,
    TR_SELECCIONANDO_REF,
    TR_SELECCIONANDO_COLOR,
    TR_ESPERANDO_CANTIDAD,
) = 60, 61, 62, 63, 64, 65, 66, 67

URL_TRANSFERENCIAS = os.environ.get("URL_TRANSFERENCIAS", "").strip()
TOKEN_TRANSFERENCIAS = os.environ.get("TOKEN_TRANSFERENCIAS", "").strip()

# Google Sheet AUDITORIA_BOT, pestaña ETIQUETAS (columnas: OPERARIO | ID)
SHEET_ID_OPERARIOS = os.environ.get(
    "SHEET_ID_AUDITORIA", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
)
PESTANA_OPERARIOS = "ETIQUETAS"

TIPOS_MOVIMIENTO = ("FALTANTE", "RETAZOS", "IMPERFECTO")
SEGUNDOS_TOMA = 30
MAX_REFS = 20
MAX_COLORES = 60


def _cargar_operarios() -> dict:
    """Lee la pestaña TRASLADOS y devuelve {id: nombre}. Bloqueante: usar con to_thread."""
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


def _extraer_codigo_y_cantidad(texto: str):
    digitos = re.sub(r"\D", "", str(texto))
    if len(digitos) != 12:
        return None, None

    cod7 = digitos[:7]
    cant_str = digitos[7:]

    try:
        cant = float(cant_str) / 100.0
    except ValueError:
        cant = 0.0

    return cod7, cant


def _formatear_codigo(cod7: str) -> str:
    return f"{cod7[:4]}-{cod7[4:]}"


def _llamar_transferencias(payload: dict) -> dict:
    if not URL_TRANSFERENCIAS:
        raise RuntimeError("Falta configurar la variable de entorno URL_TRANSFERENCIAS")
    r = requests.post(
        URL_TRANSFERENCIAS,
        json={"token": TOKEN_TRANSFERENCIAS, **payload},
        timeout=25,
    )
    r.raise_for_status()
    resp = r.json()
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "Error desconocido en el script"))
    return resp


def _boton_menu():
    return [InlineKeyboardButton("🔙 Menú Principal", callback_data="tr_menu")]


def _pantalla_menu():
    texto = "📋 <b>Registro de Transferencias</b>\n\nSelecciona el tipo de movimiento:"
    filas = [
        [InlineKeyboardButton("⚠️ Faltante", callback_data="tr_tipo_FALTANTE")],
        [InlineKeyboardButton("✂️ Retazos", callback_data="tr_tipo_RETAZOS")],
        [InlineKeyboardButton("❌ Imperfecto", callback_data="tr_tipo_IMPERFECTO")],
    ]
    return texto, InlineKeyboardMarkup(filas)


def _pantalla_metodo(tipo: str):
    texto = (
        f"📌 Tipo seleccionado: <b>{esc(tipo)}</b>\n\n"
        "¿Cómo deseas realizar el ingreso?"
    )
    filas = [
        [InlineKeyboardButton("📷 Código de barras (Scan)", callback_data="tr_metodo_barras")],
        [InlineKeyboardButton("🔎 Por Nombre / Referencia", callback_data="tr_metodo_nombre")],
        _boton_menu(),
    ]
    return texto, InlineKeyboardMarkup(filas)


def _pantalla_continuar(resumen: str = ""):
    texto = (resumen + "\n\n" if resumen else "") + "🔁 <b>¿Continuar con el siguiente registro?</b>"
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data="tr_seguir_si"),
        InlineKeyboardButton("🏁 No", callback_data="tr_seguir_no"),
    ]])
    return texto, teclado


# --- TEMPORIZADOR ---
def _cancelar_temporizador(tr: dict) -> None:
    job = tr.pop("job", None)
    tr["token"] = None
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass


async def _vencio_toma(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    tr = d["tr"]
    actual = context.application.user_data.get(d["user_id"], {}).get("tr")
    if actual is not tr or tr.get("token") != d["token"]:
        return
    tr["pausado"] = True
    tr["token"] = None
    tr.pop("job", None)
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
        logger.warning(f"Transferencias: error al re-preguntar por tiempo: {e}")


def _iniciar_temporizador(context: ContextTypes.DEFAULT_TYPE, tr: dict, mensaje, user_id: int) -> None:
    _cancelar_temporizador(tr)
    if context.job_queue is None or mensaje is None:
        return
    token = uuid.uuid4().hex
    tr["token"] = token
    tr["job"] = context.job_queue.run_once(
        _vencio_toma,
        SEGUNDOS_TOMA,
        data={
            "tr": tr,
            "token": token,
            "user_id": user_id,
            "chat_id": mensaje.chat_id,
            "message_id": mensaje.message_id,
        },
    )


# --- HANDLERS ---
@requiere_acceso("transferencias")
async def iniciar_transferencias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cancelar_temporizador(context.user_data.get("tr") or {})
    context.user_data["tr"] = {}

    # Al entrar al módulo, pedimos directamente el ID de operario
    texto = (
        "🔐 <b>Validación de Operario</b>\n\n"
        "Por favor, escribe tu <b>ID de operario</b> registrado en el sistema para continuar:"
    )

    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_TRANSFERENCIAS")
        await editar(query, texto, InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_ID

    registrar_uso_bot(update, "/tr")
    programar_borrado(context, update.message)
    await responder(update, context, texto, reply_markup=InlineKeyboardMarkup([_boton_menu()]))
    return TR_ESPERANDO_ID


async def recibir_id_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Valida el ID contra la pestaña TRASLADOS al ingresar al módulo."""
    programar_borrado(context, update.message)
    tr = context.user_data.setdefault("tr", {})
    id_txt = re.sub(r"\D", "", update.message.text or "")

    if not id_txt:
        await responder(update, context, "⚠️ Por favor, escribe un ID válido:", reply_markup=InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_ID

    try:
        operarios = await asyncio.to_thread(_cargar_operarios)
    except Exception as e:
        logger.error(f"Transferencias: no se pudo leer la lista de operarios: {e}", exc_info=True)
        await responder(update, context, "⚠️ No pude consultar la lista de operarios. Intenta de nuevo:", reply_markup=InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_ID

    nombre = operarios.get(id_txt)
    if not nombre:
        await responder(
            update, context,
            "❌ <b>ID no autorizado.</b> El ID ingresado no está registrado en la hoja. Intenta de nuevo:",
            reply_markup=InlineKeyboardMarkup([_boton_menu()])
        )
        return TR_ESPERANDO_ID

    # Guardamos los datos del operario verificado
    tr["operario"] = nombre
    tr["id_operario"] = id_txt

    # Una vez validado el ID, mostramos el menú de tipos de movimiento
    texto, teclado = _pantalla_menu()
    await responder(update, context, f"✅ ¡Hola, <b>{esc(nombre)}</b>! ID verificado.\n\n{texto}", reply_markup=teclado)
    return TR_MENU


async def botones_tr_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    tr = context.user_data.setdefault("tr", {})

    if data == "tr_menu":
        _cancelar_temporizador(tr)
        tr.clear()
        texto, teclado = _pantalla_menu()
        await editar(query, texto, teclado)
        return TR_MENU

    if data.startswith("tr_tipo_"):
        tipo = data.replace("tr_tipo_", "")
        tr["tipo"] = tipo
        texto, teclado = _pantalla_metodo(tipo)
        await editar(query, texto, teclado)
        return TR_ELEGIR_METODO

    return TR_MENU


async def seleccionar_metodo_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    tr = context.user_data.get("tr") or {}

    if data == "tr_metodo_barras":
        tr["metodo"] = "barras"
        texto = (
            f"📌 Tipo: <b>{esc(tr.get('tipo', ''))}</b> | Operario: <b>{esc(tr.get('operario', ''))}</b>\n\n"
            "📷 Escanea el <b>código de barras</b> de 12 dígitos del rollo:"
        )
        await editar(query, texto, InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_CODIGO

    if data == "tr_metodo_nombre":
        tr["metodo"] = "nombre"
        texto = (
            f"📌 Tipo: <b>{esc(tr.get('tipo', ''))}</b> | Operario: <b>{esc(tr.get('operario', ''))}</b>\n\n"
            "🔎 Escribe la <b>referencia</b> a buscar (ej. <code>SATIN</code>):"
        )
        await editar(query, texto, InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_REF

    return TR_MENU


# --- FLUJO BARRAS ---
async def recibir_codigo_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    tr = context.user_data.setdefault("tr", {})

    if tr.get("pausado"):
        texto, teclado = _pantalla_continuar()
        await responder(update, context, "⏸️ Primero responde:\n\n" + texto, reply_markup=teclado)
        return TR_ESPERANDO_CODIGO

    cod7, cantidad = _extraer_codigo_y_cantidad(update.message.text)
    if cod7 is None:
        await responder(
            update, context,
            "⚠️ No reconozco el código. Debe tener 12 dígitos (ej. <code>001820005230</code>).",
            reply_markup=InlineKeyboardMarkup([_boton_menu()]),
        )
        return TR_ESPERANDO_CODIGO

    try:
        from ubicacion import _buscar_articulo, _indice_inventario
        indice = await _indice_inventario()
        articulo = _buscar_articulo(indice, cod7)
    except Exception as e:
        logger.error(f"Transferencias: error consultando SAP: {e}")
        articulo = None

    if articulo:
        codigo_sap, descripcion = articulo
    else:
        codigo_sap = _formatear_codigo(cod7)
        descripcion = f"Artículo {codigo_sap}"

    nombre_user = tr.get("operario") or (update.effective_user.full_name if update.effective_user else "Desconocido")

    try:
        resp = await asyncio.to_thread(
            _llamar_transferencias,
            {
                "accion": "guardar_transferencia",
                "articulo": codigo_sap,
                "descripcion": descripcion,
                "cantidad": cantidad,
                "tipo": tr.get("tipo", ""),
                "nombre": nombre_user,
            },
        )
    except Exception as e:
        logger.error(f"Error guardando transferencia: {e}", exc_info=True)
        await responder(
            update, context,
            f"⚠️ No pude registrar en la hoja.\n<code>{esc(e)}</code>",
            reply_markup=InlineKeyboardMarkup([_boton_menu()]),
        )
        return TR_ESPERANDO_CODIGO

    registrar_uso_bot(update, f"TRANSFERENCIA_{tr.get('tipo')} {codigo_sap} - {cantidad}m")

    _cancelar_temporizador(tr)
    tr["pausado"] = True

    resumen = (
        f"✅ <b>Registrado con éxito</b>\n\n"
        f"📌 <b>Tipo:</b> {esc(tr.get('tipo', ''))}\n"
        f"👤 <b>Operario:</b> {esc(nombre_user)}\n"
        f"📦 <b>Artículo:</b> <code>{esc(codigo_sap)}</code>\n"
        f"📝 <b>Descripción:</b> {esc(descripcion)}\n"
        f"📏 <b>Cantidad:</b> <code>{cantidad:,.2f} m</code>"
    )
    texto, teclado = _pantalla_continuar(resumen)
    await responder(update, context, texto, reply_markup=teclado)
    return TR_ESPERANDO_CODIGO


async def seguir_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tr = context.user_data.get("tr") or {}

    if query.data == "tr_seguir_no":
        _cancelar_temporizador(tr)
        tr.clear()
        texto, teclado = _pantalla_menu()
        await editar(query, texto, teclado)
        return TR_MENU

    tr["pausado"] = False
    _iniciar_temporizador(context, tr, query.message, update.effective_user.id)

    if tr.get("metodo") == "barras":
        await editar(
            query,
            f"📷 Escanea el siguiente rollo (tienes {SEGUNDOS_TOMA} segundos).",
            InlineKeyboardMarkup([_boton_menu()]),
        )
        return TR_ESPERANDO_CODIGO
    else:
        await editar(
            query,
            "🔎 Escribe la siguiente <b>referencia</b> a buscar:",
            InlineKeyboardMarkup([_boton_menu()]),
        )
        return TR_ESPERANDO_REF


# --- FLUJO POR NOMBRE ---
async def recibir_ref_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    tr = context.user_data.setdefault("tr", {})
    termino = (update.message.text or "").strip().upper()

    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.", reply_markup=InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_REF

    df = await datos.inventario.obtener()
    if df is None:
        await responder(update, context, "⚠️ No se pudo acceder al inventario.", reply_markup=InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_REF

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await responder(update, context, f"❌ No se encontró: <b>{esc(termino)}</b>.", reply_markup=InlineKeyboardMarkup([_boton_menu()]))
        return TR_ESPERANDO_REF

    tr.update({"termino": termino, "refs": refs[:MAX_REFS]})

    if len(refs) == 1:
        tr["ref"] = refs[0]
        tr["colores"] = datos.colores_de(df, refs[0])
        filas = [[InlineKeyboardButton(c, callback_data=f"tr_col_{i}")] for i, c in enumerate(tr["colores"][:MAX_COLORES])]
        filas.append(_boton_menu())
        await responder(update, context, f"Referencia: <b>{esc(refs[0])}</b>. Selecciona color:", reply_markup=InlineKeyboardMarkup(filas))
        return TR_SELECCIONANDO_COLOR

    filas = [[InlineKeyboardButton(r, callback_data=f"tr_ref_{i}")] for i, r in enumerate(refs[:MAX_REFS])]
    filas.append(_boton_menu())
    await responder(update, context, "Selecciona una referencia:", reply_markup=InlineKeyboardMarkup(filas))
    return TR_SELECCIONANDO_REF


async def seleccionar_ref_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tr = context.user_data.get("tr") or {}
    idx = int(query.data.split("_")[-1])
    ref = tr["refs"][idx]

    df = await datos.inventario.obtener()
    tr["ref"] = ref
    tr["colores"] = datos.colores_de(df, ref)

    filas = [[InlineKeyboardButton(c, callback_data=f"tr_col_{i}")] for i, c in enumerate(tr["colores"][:MAX_COLORES])]
    filas.append(_boton_menu())
    await editar(query, f"Referencia: <b>{esc(ref)}</b>. Selecciona color:", InlineKeyboardMarkup(filas))
    return TR_SELECCIONANDO_COLOR


async def seleccionar_color_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tr = context.user_data.get("tr") or {}
    idx = int(query.data.split("_")[-1])
    color = tr["colores"][idx]

    df = await datos.inventario.obtener()
    from ubicacion import _articulo_de
    articulo = _articulo_de(df, tr["ref"], color)

    if not articulo:
        await editar(query, "❌ No se encontró el artículo.", InlineKeyboardMarkup([_boton_menu()]))
        return TR_MENU

    tr["codigo_sap"], tr["descripcion"] = articulo

    await editar(
        query,
        f"📦 <b>Artículo:</b> <code>{esc(tr['codigo_sap'])}</code>\n"
        f"📝 <b>Descripción:</b> {esc(tr['descripcion'])}\n\n"
        "📏 Escribe la <b>cantidad de metros</b> (ej. <code>45.50</code>):",
        InlineKeyboardMarkup([_boton_menu()]),
    )
    return TR_ESPERANDO_CANTIDAD


async def recibir_cantidad_tr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    tr = context.user_data.get("tr") or {}

    try:
        cant_str = update.message.text.replace(",", ".")
        cantidad = float(cant_str)
        if cantidad <= 0:
            raise ValueError
    except ValueError:
        await responder(update, context, "⚠️ Ingresa un número válido de metros (ej. <code>25.5</code>).")
        return TR_ESPERANDO_CANTIDAD

    nombre_user = tr.get("operario") or (update.effective_user.full_name if update.effective_user else "Desconocido")

    try:
        await asyncio.to_thread(
            _llamar_transferencias,
            {
                "accion": "guardar_transferencia",
                "articulo": tr.get("codigo_sap", ""),
                "descripcion": tr.get("descripcion", ""),
                "cantidad": cantidad,
                "tipo": tr.get("tipo", ""),
                "nombre": nombre_user,
            },
        )
    except Exception as e:
        await responder(update, context, f"⚠️ Error al guardar: <code>{esc(e)}</code>")
        return TR_ESPERANDO_CANTIDAD

    resumen = (
        f"✅ <b>Registrado con éxito</b>\n\n"
        f"📌 <b>Tipo:</b> {esc(tr.get('tipo', ''))}\n"
        f"👤 <b>Operario:</b> {esc(nombre_user)}\n"
        f"📦 <b>Artículo:</b> <code>{esc(tr.get('codigo_sap', ''))}</code>\n"
        f"📝 <b>Descripción:</b> {esc(tr.get('descripcion', ''))}\n"
        f"📏 <b>Cantidad:</b> <code>{cantidad:,.2f} m</code>"
    )
    tr["pausado"] = True
    texto, teclado = _pantalla_continuar(resumen)
    await responder(update, context, texto, reply_markup=teclado)
    return TR_ESPERANDO_CODIGO
