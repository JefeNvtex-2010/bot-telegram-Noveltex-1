¡Claro que sí, Diego! Le acabo de agregar la línea divisoria justo antes de la fecha de vencimiento para que quede ordenado y visualmente separado como me indicaste en la imagen.

Aquí tienes el código completo y actualizado:

Python
import os
import logging
import io
import datetime
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

# Estados para la conversación interactiva de búsqueda de pedidos (/PV)
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR = range(2)

# Enlace de Google Sheets para Pedidos
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"

# ==========================================
# CONFIGURACIÓN
# ==========================================
# 📊 URL de la Aplicación Web de Google Apps Script para la Auditoría.
URL_WEB_APP_REPORTE = os.environ.get(
    "URL_WEB_APP_REPORTE",
    "https://script.google.com/macros/s/AKfycbztHKV4C8hqcPUVHq71gFmRzPSbtz36NByAgPfR-JEyDlNRF-LkRj-g2lEWHrf42AaE/exec"
)

# Tu ID de Telegram (variable de entorno MI_TELEGRAM_ID, con respaldo)
MI_TELEGRAM_ID = int(os.environ.get("MI_TELEGRAM_ID", "5655537446"))

# Deploy hook de Render: SOLO por variable de entorno RENDER_DEPLOY_HOOK_URL
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")

# ⏳ Tiempo de vida de los mensajes (24 horas)
SEGUNDOS_BORRADO = 24 * 60 * 60


# ==========================================
# BORRADO AUTOMÁTICO DE MENSAJES
# ==========================================

async def _borrar_mensaje_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que se ejecuta a las 24 h y borra el mensaje."""
    datos = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=datos["chat_id"],
            message_id=datos["message_id"]
        )
    except Exception as e:
        logger.warning(f"No se pudo borrar el mensaje {datos['message_id']}: {e}")


def programar_borrado(context: ContextTypes.DEFAULT_TYPE, mensaje) -> None:
    """Programa el borrado de un mensaje dentro de SEGUNDOS_BORRADO."""
    if mensaje is None:
        return
    if context.job_queue is None:
        logger.warning("JobQueue no disponible. Instala: python-telegram-bot[job-queue]")
        return
    context.job_queue.run_once(
        _borrar_mensaje_job,
        when=SEGUNDOS_BORRADO,
        data={"chat_id": mensaje.chat_id, "message_id": mensaje.message_id},
    )


async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, **kwargs):
    """Envía una respuesta y programa su borrado automático."""
    msg = await update.message.reply_text(texto, **kwargs)
    programar_borrado(context, msg)
    return msg


# ==========================================
# AUDITORÍA
# ==========================================

def registrar_uso_bot(update: Update, comando_usado: str):
    """Registra de forma automática quién consultó el bot, la hora local ajustada y el comando ejecutado."""
    user = update.effective_user
    if not user:
        return

    user_id = user.id
    nombre = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "Sin alias"

    # 🕒 Ajuste de zona horaria (-5 horas)
    zona_horaria = datetime.timezone(datetime.timedelta(hours=-5))
    hora_actual = datetime.datetime.now(zona_horaria).strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"📊 [AUDITORÍA] {nombre} ({username}) [ID: {user_id}] -> {comando_usado} a las {hora_actual}")

    if not URL_WEB_APP_REPORTE:
        logger.warning("⚠️ URL_WEB_APP_REPORTE no está configurada; no se envía la auditoría.")
        return

    try:
        payload = {
            "fecha": hora_actual,
            "usuario": nombre,
            "username": username,
            "id": str(user_id),
            "comando": comando_usado
        }
        r = requests.post(URL_WEB_APP_REPORTE, json=payload, timeout=10)
        logger.info(f"📨 Auditoría enviada, respuesta HTTP: {r.status_code}")
        if r.status_code >= 400:
            logger.error(f"⚠️ El Apps Script respondió con error: {r.text[:200]}")
    except Exception as e:
        logger.error(f"⚠️ Error al enviar reporte al Google Sheet de auditoría: {e}")


def escapar_markdown(texto: str) -> str:
    """Escapa caracteres especiales de Telegram para evitar errores de parseo."""
    if not isinstance(texto, str):
        texto = str(texto)
    caracteres = ['_', '*', '`', '[']
    for c in caracteres:
        texto = texto.replace(c, '')
    return texto.strip()


def _descargar_csv(url):
    """Función auxiliar robusta usando requests para descargar pestañas de Google Sheets en CSV."""
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df


def cargar_catalogo():
    """Carga el catálogo de pedidos."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv, GOOGLE_SHEET_URL)
        try:
            df = future.result(timeout=20.0)
            if 'Documento Pd' in df.columns:
                df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer Google Sheets de pedidos: {e}")
            return f"ERROR: {e}"


# ==========================================
# COMANDOS BÁSICOS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    user = update.effective_user
    name = user.first_name if user else "allí"
    await responder(
        update, context,
        f"¡Hola {name}! Bienvenido al Bot de Noveltex, estoy aqui para ayudarte en tu día, dime cual sera tu primera consulta\n\n"
        "Usa los comandos:\n"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(
        update, context,
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/in [Referencia] - Consultar inventario SAP\n"
        "/i [Referencia] - Consultar inventario vendedor (Bodega 01)\n"
        "/reiniciar - Reiniciar servicio de Render\n"
        "/actualizar - Limpiar caché y desplegar en Render"
    )


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(
        update, context,
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )


# --- ADMINISTRACIÓN REMOTA DE RENDER ---

async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ No tienes permisos para ejecutar este comando.")
        return

    if not RENDER_DEPLOY_HOOK_URL:
        await responder(update, context, "⚠️ Falta configurar la variable RENDER_DEPLOY_HOOK_URL en Render.")
        return

    try:
        response = requests.post(RENDER_DEPLOY_HOOK_URL, timeout=10)
        if response.status_code == 200:
            await responder(update, context, "🔄 ¡Orden enviada con éxito! Reiniciando el servicio en Render...")
        else:
            await responder(update, context, f"⚠️ Error al conectar con Render. Código HTTP: {response.status_code}")
    except Exception as e:
        logger.error(f"Error al reiniciar Render: {e}")
        await responder(update, context, f"⚠️ Ocurrió un error inesperado: `{e}`", parse_mode="Markdown")


async def limpiar_cache_y_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ No tienes permisos para ejecutar este comando.")
        return

    if not RENDER_DEPLOY_HOOK_URL:
        await responder(update, context, "⚠️ Falta configurar la variable RENDER_DEPLOY_HOOK_URL en Render.")
        return

    separador = "&" if "?" in RENDER_DEPLOY_HOOK_URL else "?"
    url_cache = f"{RENDER_DEPLOY_HOOK_URL}{separador}clearCache=true"

    try:
        response = requests.post(url_cache, timeout=10)
        if response.status_code == 200:
            await responder(update, context, "🧹 ¡Orden enviada! Limpiando caché y desplegando nueva versión en Render...")
        else:
            await responder(update, context, f"⚠️ Error al conectar con Render. Código HTTP: {response.status_code}")
    except Exception as e:
        logger.error(f"Error al limpiar caché en Render: {e}")
        await responder(update, context, f"⚠️ Ocurrió un error inesperado: `{e}`", parse_mode="Markdown")


# ==========================================
# FLUJO DE BÚSQUEDA DE PEDIDOS (/PV)
# ==========================================

def _texto_detalle(fila, titulo=None) -> str:
    """Construye el bloque de texto con el detalle de una fila de pedido."""
    cabecera = f"{titulo}\n\n" if titulo else ""
    
    # Validamos si la clasificación del pedido es de contado
    tipo_pedido = str(fila.get('Clasificacion Pedido', '')).strip().lower()
    
    campos_contado = ""
    if tipo_pedido == 'contado':
        fecha_vencimiento = escapar_markdown(fila.get('Fecha Vencimiento', 'N/A')) or 'N/A'
        num_proforma = escapar_markdown(fila.get('Numero Proforma', 'N/A')) or 'N/A'
        
        # Agregamos la línea separadora antes de la fecha de vencimiento
        campos_contado += "-----------------------------------\n"
        campos_contado += f"• *Fecha Vencimiento:* {fecha_vencimiento}\n"
        if num_proforma and num_proforma != 'N/A':
            campos_contado += f"• *Número Proforma:* {num_proforma}\n"

    return (
        f"{cabecera}"
        f"• *Estado del Pedido:* {escapar_markdown(fila.get('Estado Factura', 'N/A'))}\n"
        f"• *Ubicación:* {escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))}\n"
        f"• *Referencia:* {escapar_markdown(fila.get('Id Refer', 'N/A'))}\n"
        f"• *Color:* {escapar_markdown(fila.get('Color', 'N/A'))}\n"
        f"• *Estado Pedido:* {escapar_markdown(fila.get('Document Status SAP', 'N/A'))}\n"
        f"• *Estado Item:* {escapar_markdown(fila.get('Line Status Sap', 'N/A'))}\n"
        f"• *Cantidad pedida:* {escapar_markdown(fila.get('Cantidad Ped', 'N/A'))}\n"
        f"• *Cantidad alistada:* {escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))}\n"
        f"• *Fecha Despacho:* {escapar_markdown(fila.get('Fecha Factura', 'N/A'))}\n"
        f"• *Operario:* {escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))}\n"
        f"• *Tipo Pedido:* {escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))}\n"
        f"• *Observacion:* {escapar_markdown(fila.get('Observacion Adicional', 'N/A'))}\n"
        f"{campos_contado}"  # <--- Línea divisoria y campos de contado al final
    )


def _traducir_estado(valor) -> str:
    """Traduce el estado de SAP a texto legible."""
    v = str(valor).strip()
    return {"bost_Open": "Abierto", "bost_Close": "Cerrado"}.get(v, v or "N/A")


def _construir_teclado_colores(df_ref) -> InlineKeyboardMarkup:
    """Un botón por color, con el Estado Item al frente."""
    df = df_ref.copy()
    df['Color'] = df['Color'].astype(str).str.strip()

    keyboard = [[InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")]]
    for color in df['Color'].unique():
        estados = (
            df.loc[df['Color'] == color, 'Line Status Sap']
            .map(_traducir_estado)
            .unique()
        )
        etiqueta = f"{color} - {' / '.join(estados)}"

        cb_data = f"col_{color}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
        keyboard.append([InlineKeyboardButton(etiqueta, callback_data=cb_data)])
    keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_referencias")])
    return InlineKeyboardMarkup(keyboard)


def _texto_encabezado(resultado, doc_buscado) -> str:
    """Mensaje inicial del documento: número, vendedor y cliente (Nombre Tercero)."""
    primera = resultado.iloc[0]
    vendedor = escapar_markdown(primera.get('Nombre Vendedor', 'N/A')) or 'N/A'
    cliente = escapar_markdown(primera.get('Nombre Tercero', 'N/A')) or 'N/A'
    return (
        f"🔍 Documento *{doc_buscado}*\n"
        f"• *Vendedor:* {vendedor}\n"
        f"• *Cliente:* {cliente}\n\n"
        f"Selecciona una referencia:"
    )


async def mostrar_ver_todo_referencia(query, context) -> int:
    resultado = context.user_data.get('df_pedido')
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
    ref_elegida = context.user_data.get('ref_elegida')

    if resultado is None or ref_elegida is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado. Realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    resultado = resultado.replace('bost_Open', 'Abierto').replace('bost_Close', 'Cerrado')
    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == ref_elegida]

    mensaje = f"🔍 *Detalle Completo - Referencia {ref_elegida}* (Documento {doc_buscado}, Total ítems: {len(df_ref)}):\n"

    for index, fila in df_ref.iterrows():
        mensaje += "\n-----------------------------------\n" + _texto_detalle(fila)

    keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_colores")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if len(mensaje) > 4000:
        for i in range(0, len(mensaje), 4000):
            msg = await query.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
            programar_borrado(context, msg)
        await query.edit_message_text(text="📌 Fin del detalle completo.", reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")

    return SELECCIONANDO_COLOR


async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # 📊 Registramos automáticamente el uso de /PV
    registrar_uso_bot(update, "/PV")

    # 🗑️ Programamos el borrado del comando del usuario (/PV 5270)
    programar_borrado(context, update.message)

    try:
        if not context.args:
            await responder(
                update, context,
                "⚠️ Por favor, ingresa el número de documento a buscar.\n"
                "Ejemplo: `/PV 5270`", parse_mode="Markdown"
            )
            return ConversationHandler.END

        # 🔍 Mensaje temporal indicando que se está buscando el pedido
        msg_buscando = await update.message.reply_text("🔍 Buscando pedido...")
        programar_borrado(context, msg_buscando)

        doc_buscado = context.args[0].strip()
        catalogo_df = cargar_catalogo()

        if isinstance(catalogo_df, str):
            if catalogo_df.startswith("ERROR"):
                await responder(update, context, f"⚠️ Error al conectar con Google Sheets:\n`{catalogo_df}`", parse_mode="Markdown")
                return ConversationHandler.END

        if catalogo_df is None or (isinstance(catalogo_df, pd.DataFrame) and catalogo_df.empty):
            await responder(update, context, "⚠️ El catálogo está vacío.")
            return ConversationHandler.END

        columna_doc = 'Documento Pd'
        if columna_doc not in catalogo_df.columns:
            await responder(update, context, f"⚠️ No se encontró la columna '{columna_doc}'.")
            return ConversationHandler.END

        resultado = catalogo_df[catalogo_df[columna_doc].astype(str).str.strip() == doc_buscado]

        if resultado.empty:
            await responder(update, context, f"❌ No se encontró ningún registro con el documento: *{doc_buscado}*.", parse_mode="Markdown")
            return ConversationHandler.END

        context.user_data['df_pedido'] = resultado
        context.user_data['doc_buscado'] = doc_buscado

        referencias = resultado['Id Refer'].unique()
        keyboard = [[InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")] for ref in referencias]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await responder(
            update, context,
            _texto_encabezado(resultado, doc_buscado),
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    except Exception as e:
        logger.error(f"Error en iniciar_busqueda: {e}")
        await responder(update, context, f"⚠️ Error inesperado: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END


async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data_callback = query.data

    if data_callback == "volver_referencias":
        resultado = context.user_data.get('df_pedido')
        doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
        if resultado is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado.", parse_mode="Markdown")
            return ConversationHandler.END

        referencias = resultado['Id Refer'].unique()
        keyboard = [[InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")] for ref in referencias]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=_texto_encabezado(resultado, doc_buscado),
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    referencia_elegida = data_callback.replace("ref_", "").strip()
    resultado = context.user_data.get('df_pedido')
    if resultado is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data['ref_elegida'] = referencia_elegida
    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == referencia_elegida]

    reply_markup = _construir_teclado_colores(df_ref)
    await query.edit_message_text(
        text=f"Referencia seleccionada: *{referencia_elegida}*.\nAhora selecciona el color:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return SELECCIONANDO_COLOR


async def seleccionar_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    try:
        data_callback = query.data

        if data_callback == "ref_ver_todo":
            return await mostrar_ver_todo_referencia(query, context)

        if data_callback == "volver_referencias":
            return await seleccionar_referencia(update, context)

        if data_callback == "volver_colores":
            ref_elegida = context.user_data.get('ref_elegida')
            resultado = context.user_data.get('df_pedido')
            if resultado is None or ref_elegida is None:
                await query.edit_message_text(text="⚠️ La sesión ha expirado.", parse_mode="Markdown")
                return ConversationHandler.END

            resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
            df_ref = resultado[resultado['Id Refer'] == ref_elegida]

            await query.edit_message_text(
                text=f"Referencia seleccionada: *{ref_elegida}*.\nAhora selecciona el color:",
                reply_markup=_construir_teclado_colores(df_ref),
                parse_mode="Markdown"
            )
            return SELECCIONANDO_COLOR

        color_elegido = data_callback.replace("col_", "").strip()
        doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
        ref_elegida = context.user_data.get('ref_elegida')
        resultado = context.user_data.get('df_pedido')

        if resultado is None or ref_elegida is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado.", parse_mode="Markdown")
            return ConversationHandler.END

        resultado = resultado.replace('bost_Open', 'Abierto').replace('bost_Close', 'Cerrado')
        resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
        resultado['Color'] = resultado['Color'].astype(str).str.strip()

        fila_match = resultado[(resultado['Id Refer'] == ref_elegida) & (resultado['Color'] == color_elegido)]

        if fila_match.empty:
            await query.edit_message_text(text=f"❌ No se encontró información para la referencia *{ref_elegida}* y color *{color_elegido}*.", parse_mode="Markdown")
            return ConversationHandler.END

        mensaje = (
            f"🔍 *Detalle del Documento {doc_buscado}* "
            f"(Color {color_elegido}, Total ítems: {len(fila_match)}):\n"
        )
        for _, fila in fila_match.iterrows():
            mensaje += "\n-----------------------------------\n" + _texto_detalle(fila)

        keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_colores")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if len(mensaje) > 4000:
            for i in range(0, len(mensaje), 4000):
                msg = await query.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
                programar_borrado(context, msg)
            await query.edit_message_text(text="📌 Fin del detalle.", reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")
        return SELECCIONANDO_COLOR

    except Exception as e:
        logger.error(f"Error en seleccionar_color: {e}")
        await query.edit_message_text(text=f"⚠️ Error al procesar el color: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    await responder(update, context, "❌ Búsqueda cancelada.")
    return ConversationHandler.END
