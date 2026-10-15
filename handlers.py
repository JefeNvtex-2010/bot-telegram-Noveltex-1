import asyncio
import datetime
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from html import escape

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

import datos

logger = logging.getLogger(__name__)

# Estados de la conversación de pedidos (/PV). Los estados de inventario usan 3-8
# (así todos pueden convivir en UN solo ConversationHandler sin chocar).
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR, ESPERANDO_DOCUMENTO_PV = 0, 1, 2

URL_WEB_APP_REPORTE = os.environ.get(
    "URL_WEB_APP_REPORTE",
    "https://script.google.com/macros/s/AKfycbztHKV4C8hqcPUVHq71gFmRzPSbtz36NByAgPfR-JEyDlNRF-LkRj-g2lEWHrf42AaE/exec",
)

MI_TELEGRAM_ID = int(os.environ.get("MI_TELEGRAM_ID", "5655537446"))
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
SEGUNDOS_BORRADO = 24 * 60 * 60
MAX_BOTONES = 60


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def esc(valor) -> str:
    """Escapa texto para parse_mode=HTML (no se rompe con _ * ` [ < > &)."""
    return escape(str(valor if valor is not None else "").strip())


escapar_markdown = esc  # alias por compatibilidad


def recortar(texto: str, limite: int = 3800) -> str:
    """Telegram permite 4096 caracteres por mensaje; se corta en un salto de línea."""
    if len(texto) <= limite:
        return texto
    return texto[:limite].rsplit("\n", 1)[0] + "\n… (resultado recortado)"


async def editar(query, texto: str, teclado=None) -> None:
    try:
        await query.edit_message_text(text=texto, reply_markup=teclado, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # doble clic en el mismo botón
            raise


async def _borrar_mensaje_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    datos_job = context.job.data
    try:
        await context.bot.delete_message(chat_id=datos_job["chat_id"], message_id=datos_job["message_id"])
    except Exception as e:
        logger.warning(f"No se pudo borrar el mensaje {datos_job['message_id']}: {e}")


def programar_borrado(context: ContextTypes.DEFAULT_TYPE, mensaje) -> None:
    if mensaje is None or context.job_queue is None:
        return
    context.job_queue.run_once(
        _borrar_mensaje_job,
        when=SEGUNDOS_BORRADO,
        data={"chat_id": mensaje.chat_id, "message_id": mensaje.message_id},
    )


async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    msg = await update.effective_message.reply_text(texto, **kwargs)
    programar_borrado(context, msg)
    return msg


# --------------------------------------------------------------------------- #
# Auditoría: se envía en un hilo aparte (ANTES bloqueaba el bot hasta 10 s)
# --------------------------------------------------------------------------- #
_pool_auditoria = ThreadPoolExecutor(max_workers=2, thread_name_prefix="auditoria")


def _enviar_auditoria(payload: dict) -> None:
    try:
        requests.post(URL_WEB_APP_REPORTE, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Error auditoría: {e}")


def registrar_uso_bot(update: Update, comando_usado: str) -> None:
    user = update.effective_user
    if not user:
        return
    nombre = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "Sin alias"
    zona_horaria = datetime.timezone(datetime.timedelta(hours=-5))
    hora_actual = datetime.datetime.now(zona_horaria).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"📊 [AUDITORÍA] {nombre} ({username}) [ID: {user.id}] -> {comando_usado} a las {hora_actual}")
    if not URL_WEB_APP_REPORTE:
        return
    payload = {
        "fecha": hora_actual,
        "usuario": nombre,
        "username": username,
        "id": str(user.id),
        "comando": comando_usado,
    }
    _pool_auditoria.submit(_enviar_auditoria, payload)  # no espera la respuesta


# --------------------------------------------------------------------------- #
# Menú y comandos generales
# --------------------------------------------------------------------------- #
def _teclado_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Inventario Vendedor", callback_data="start_vend")],
        [InlineKeyboardButton("📦 Inventario SAP", callback_data="start_sap")],
        [InlineKeyboardButton("📋 Pedidos (PV)", callback_data="start_pv")],
    ])


async def menu_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sirve como comando suelto y como fallback dentro de la conversación (cierra la conversación)."""
    texto = "👋 ¡Hola! Selecciona una opción del menú principal para comenzar:"
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await editar(query, texto, _teclado_menu())
    else:
        programar_borrado(context, update.message)
        msg = await update.message.reply_text(texto, reply_markup=_teclado_menu())
        programar_borrado(context, msg)
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    user = update.effective_user
    name = user.first_name if user else "allí"
    await responder(update, context, f"¡Hola {esc(name)}! Escribe /inicio para ver el menú principal con botones.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(update, context, "Usa /inicio para desplegar el menú de opciones.")


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(update, context, "📦 Escribe el nombre del producto:")


async def _llamar_hook(url: str) -> None:
    respuesta = await asyncio.to_thread(requests.post, url, timeout=10)
    respuesta.raise_for_status()


async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ Sin permisos.")
        return
    if not RENDER_DEPLOY_HOOK_URL:
        await responder(update, context, "⚠️ Falta la variable RENDER_DEPLOY_HOOK_URL.")
        return
    try:
        await _llamar_hook(RENDER_DEPLOY_HOOK_URL)
        await responder(update, context, "🔄 Reiniciando Render...")
    except Exception as e:
        await responder(update, context, f"⚠️ No se pudo reiniciar: <code>{esc(e)}</code>")


async def limpiar_cache_y_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ Sin permisos.")
        return
    datos.invalidar_todo()
    if not RENDER_DEPLOY_HOOK_URL:
        await responder(update, context, "🧹 Caché limpiada, pero falta RENDER_DEPLOY_HOOK_URL para desplegar.")
        return
    separador = "&" if "?" in RENDER_DEPLOY_HOOK_URL else "?"
    try:
        await _llamar_hook(f"{RENDER_DEPLOY_HOOK_URL}{separador}clearCache=true")
        await responder(update, context, "🧹 Caché limpiada localmente y desplegando en Render...")
    except Exception as e:
        await responder(update, context, f"⚠️ No se pudo desplegar: <code>{esc(e)}</code>")


async def refrescar_datos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refrescar: recarga las hojas de Google ya mismo (sin redeploy). Solo el dueño."""
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ Sin permisos.")
        return
    await datos.refrescar_todo()
    errores = [f for f in (datos.pedidos, datos.inventario) if f.ultimo_error]
    if errores:
        detalle = "\n".join(f"• {f.nombre}: <code>{esc(f.ultimo_error)}</code>" for f in errores)
        await responder(update, context, f"⚠️ Recarga con errores:\n{detalle}")
    else:
        await responder(update, context, "✅ Datos recargados desde Google Sheets.")


async def callback_expirado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botones viejos (por ejemplo, tras un reinicio del bot): avisa en vez de quedarse 'cargando'."""
    query = update.callback_query
    if query:
        await query.answer("Esta opción ya no está disponible. Escribe /inicio.", show_alert=True)


async def manejar_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción no controlada", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Ocurrió un error inesperado. Intenta de nuevo o escribe /inicio.",
            )
        except Exception:
            pass


async def _sesion_expirada(query) -> int:
    await editar(query, "⌛ Esta consulta expiró. Escribe /inicio para empezar de nuevo.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# FLUJO PEDIDOS (PV)
# --------------------------------------------------------------------------- #
def _v(fila, columna: str) -> str:
    return esc(fila.get(columna, "")) or "N/A"


def _raw(fila, columna: str) -> str:
    """Valor tal cual (en blanco si la celda está vacía), como se mostraba antes."""
    return esc(fila.get(columna, "N/A"))


def _texto_detalle(fila) -> str:
    tipo_pedido = str(fila.get("Clasificacion Pedido", "")).strip().lower()
    campos_contado = ""
    if tipo_pedido == "contado":
        campos_contado = "-----------------------------------\n"
        campos_contado += f"• <b>Fecha Vencimiento:</b> {_v(fila, 'Fecha Vencimiento')}\n"
        num_proforma = esc(fila.get("Numero Proforma", ""))
        if num_proforma:
            campos_contado += f"• <b>Número Proforma:</b> {num_proforma}\n"
    return (
        f"• <b>Estado del Pedido:</b> {_raw(fila, 'Estado Factura')}\n"
        f"• <b>Ubicación:</b> {_raw(fila, 'Ubicación del Pedido')}\n"
        f"• <b>Referencia:</b> {_raw(fila, 'Id Refer')}\n"
        f"• <b>Color:</b> {_raw(fila, 'Color')}\n"
        f"• <b>Estado Pedido:</b> {esc(_traducir_estado(fila.get('Document Status SAP', '')))}\n"
        f"• <b>Estado Item:</b> {esc(_traducir_estado(fila.get('Line Status Sap', '')))}\n"
        f"• <b>Cantidad pedida:</b> {_raw(fila, 'Cantidad Ped')}\n"
        f"• <b>Cantidad alistada:</b> {_raw(fila, 'Cantidad Alistada')}\n"
        f"• <b>Fecha Despacho:</b> {_raw(fila, 'Fecha Factura')}\n"
        f"• <b>Operario:</b> {_raw(fila, 'Nombre Operario Asignado')}\n"
        f"• <b>Tipo Pedido:</b> {_raw(fila, 'Clasificacion Pedido')}\n"
        f"• <b>Observacion:</b> {_raw(fila, 'Observacion Adicional')}\n"
        f"{campos_contado}"
    )


def _agrupar(titulo: str, bloques: list, limite: int = 3800) -> list:
    """Junta el título y los bloques en mensajes de hasta `limite` caracteres sin partir un bloque."""
    partes, actual = [], titulo
    for b in bloques:
        if actual != titulo and len(actual) + len(b) > limite:
            partes.append(actual)
            actual = ""
        actual += b
    partes.append(actual)
    return partes


def _traducir_estado(valor) -> str:
    return {"bost_Open": "Abierto", "bost_Close": "Cerrado"}.get(str(valor).strip(), str(valor).strip() or "N/A")


def _pantalla_refs(d: dict):
    primera = d["df"].iloc[0]
    texto = (
        f"🔍 Documento <b>{esc(d['doc'])}</b>\n"
        f"• <b>Vendedor:</b> {_v(primera, 'Nombre Vendedor')}\n"
        f"• <b>Cliente:</b> {_v(primera, 'Nombre Tercero')}\n\n"
        f"Selecciona una referencia:"
    )
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(ref or "(sin referencia)", callback_data=f"pv_ref_{i}")]
        for i, ref in enumerate(d["refs"][:MAX_BOTONES])
    ])
    return texto, teclado


def _pantalla_colores(d: dict):
    df = d["df"]
    df_ref = df[df["Id Refer"] == d["ref"]]
    tiene_estado = "Line Status Sap" in df_ref.columns
    filas = [[InlineKeyboardButton("📄 Ver todo", callback_data="pv_all")]]
    for i, color in enumerate(d["colores"][:MAX_BOTONES]):
        etiqueta = color or "(sin color)"
        if tiene_estado:
            estados = df_ref.loc[df_ref["Color"] == color, "Line Status Sap"].map(_traducir_estado).unique()
            etiqueta += f" - {' / '.join(estados)}"
        filas.append([InlineKeyboardButton(etiqueta, callback_data=f"pv_col_{i}")])
    filas.append([InlineKeyboardButton("🔙 Volver", callback_data="pv_back_refs")])
    texto = f"Referencia seleccionada: <b>{esc(d['ref'])}</b>.\nSelecciona el color:"
    return texto, InlineKeyboardMarkup(filas)


async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_PV")
        await editar(query, "📋 <b>Pedidos (PV)</b>\n\nEscribe el <b>número de documento</b> que deseas buscar:")
        return ESPERANDO_DOCUMENTO_PV

    registrar_uso_bot(update, "/PV")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "📋 <b>Pedidos (PV)</b>\n\nEscribe el <b>número de documento</b> que deseas buscar "
            "(o usa <code>/PV 5270</code>):",
        )
        return ESPERANDO_DOCUMENTO_PV

    return await ejecutar_busqueda_documento(update, context, context.args[0])


async def recibir_texto_documento_pv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_busqueda_documento(update, context, update.message.text)


async def ejecutar_busqueda_documento(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_buscado: str) -> int:
    doc_buscado = doc_buscado.strip().split(".")[0].strip()
    if not doc_buscado:
        await responder(update, context, "⚠️ Escribe el número de documento.")
        return ESPERANDO_DOCUMENTO_PV

    if datos.pedidos.df is None:  # solo ocurre en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando datos, un momento…")

    df = await datos.pedidos.obtener()
    if df is None or df.empty:
        detalle = f"\n<code>{esc(datos.pedidos.ultimo_error)}</code>" if datos.pedidos.ultimo_error else ""
        await responder(update, context, f"⚠️ Error al conectar con Google Sheets o catálogo vacío.{detalle}")
        return ConversationHandler.END

    faltan = [c for c in ("Documento Pd", "Id Refer", "Color") if c not in df.columns]
    if faltan:
        await responder(update, context, f"⚠️ No se encontraron las columnas: {esc(', '.join(faltan))}.")
        return ConversationHandler.END

    resultado = df[df["Documento Pd"] == doc_buscado]
    if resultado.empty:
        await responder(update, context, f"❌ No se encontró el documento: <b>{esc(doc_buscado)}</b>.\nPuedes escribir otro número.")
        return ESPERANDO_DOCUMENTO_PV

    d = {
        "doc": doc_buscado,
        "df": resultado,
        "refs": resultado["Id Refer"].unique().tolist(),
    }
    context.user_data["pv"] = d

    if len(d["refs"]) == 1:  # una sola referencia: salta directo a los colores
        d["ref"] = d["refs"][0]
        d["colores"] = resultado.loc[resultado["Id Refer"] == d["ref"], "Color"].unique().tolist()
        texto, teclado = _pantalla_colores(d)
        await responder(update, context, texto, reply_markup=teclado)
        return SELECCIONANDO_COLOR

    texto, teclado = _pantalla_refs(d)
    await responder(update, context, texto, reply_markup=teclado)
    return SELECCIONANDO_REFERENCIA


async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pv")
    if not d:
        return await _sesion_expirada(query)
    try:
        ref = d["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _sesion_expirada(query)

    d["ref"] = ref
    d["colores"] = d["df"].loc[d["df"]["Id Refer"] == ref, "Color"].unique().tolist()
    texto, teclado = _pantalla_colores(d)
    await editar(query, texto, teclado)
    return SELECCIONANDO_COLOR


async def seleccionar_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("pv")
    if not d or "ref" not in d:
        return await _sesion_expirada(query)
    data = query.data

    if data == "pv_back_refs":
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REFERENCIA

    if data == "pv_back_cols":
        texto, teclado = _pantalla_colores(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_COLOR

    df = d["df"]
    base = df[df["Id Refer"] == d["ref"]]
    if data == "pv_all":
        filas = base
        titulo = (
            f"🔍 <b>Detalle Completo - Referencia {esc(d['ref'])}</b> "
            f"(Documento {esc(d['doc'])}, Total ítems: {len(filas)}):\n"
        )
    else:
        try:
            color = d["colores"][int(data.rsplit("_", 1)[1])]
        except (ValueError, IndexError):
            return await _sesion_expirada(query)
        filas = base[base["Color"] == color]
        titulo = (
            f"🔍 <b>Detalle del Documento {esc(d['doc'])}</b> "
            f"(Color {esc(color)}, Total ítems: {len(filas)}):\n"
        )

    bloques = ["\n-----------------------------------\n" + _texto_detalle(fila) for _, fila in filas.iterrows()]
    partes = _agrupar(titulo, bloques)

    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver a colores", callback_data="pv_back_cols")]])
    if len(partes) == 1:
        await editar(query, partes[0], teclado)
    else:  # detalle largo: se envía en varios mensajes y el original queda con el botón de volver
        for parte in partes:
            msg = await query.message.reply_text(parte, parse_mode=ParseMode.HTML)
            programar_borrado(context, msg)
        await editar(query, "📌 Fin del detalle.", teclado)
    return SELECCIONANDO_COLOR


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    await responder(update, context, "❌ Búsqueda cancelada.")
    return ConversationHandler.END
