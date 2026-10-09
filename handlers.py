import logging
import io
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "allí"
    await update.message.reply_text(
        f"¡Hola {name}! Bienvenido al sistema de pedidos.\n\n"
        "Usa los comandos:\n"
        "• /pedido - Registrar una nueva orden de compra\n"
        "• /PV [Nro_Documento] - Consultar estatus de pedidos\n"
        "• /in [Referencia] - Consultar inventario por referencia"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/in [Referencia] - Consultar inventario por referencia\n"
        "/reiniciar - Reiniciar servicio de Render\n"
        "/actualizar - Limpiar caché y desplegar en Render"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )

# --- ADMINISTRACIÓN REMOTA DE RENDER ---

async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    MI_TELEGRAM_ID = 5655537446
    if update.effective_user.id != MI_TELEGRAM_ID:
        await update.message.reply_text("⛔ No tienes permisos para ejecutar este comando.")
        return

    RENDER_DEPLOY_HOOK_URL = "https://api.render.com/deploy/srv-db2ktt7avr4c73eet090?key=gstG3k654R4"
    try:
        response = requests.post(RENDER_DEPLOY_HOOK_URL, timeout=10)
        if response.status_code == 200:
            await update.message.reply_text("🔄 ¡Orden enviada con éxito! Reiniciando el servicio en Render...")
        else:
            await update.message.reply_text(f"⚠️ Error al conectar con Render. Código HTTP: {response.status_code}")
    except Exception as e:
        logger.error(f"Error al reiniciar Render: {e}")
        await update.message.reply_text(f"⚠️ Ocurrió un error inesperado: `{e}`", parse_mode="Markdown")

async def limpiar_cache_y_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    MI_TELEGRAM_ID = 5655537446
    if update.effective_user.id != MI_TELEGRAM_ID:
        await update.message.reply_text("⛔ No tienes permisos para ejecutar este comando.")
        return

    RENDER_CACHE_HOOK_URL = "https://api.render.com/deploy/srv-db2ktt7avr4c73eet090?key=gstG3k654R4&clearCache=true"
    try:
        response = requests.post(RENDER_CACHE_HOOK_URL, timeout=10)
        if response.status_code == 200:
            await update.message.reply_text("🧹 ¡Orden enviada! Limpiando caché y desplegando nueva versión en Render...")
        else:
            await update.message.reply_text(f"⚠️ Error al conectar con Render. Código HTTP: {response.status_code}")
    except Exception as e:
        logger.error(f"Error al limpiar caché en Render: {e}")
        await update.message.reply_text(f"⚠️ Ocurrió un error inesperado: `{e}`", parse_mode="Markdown")


# ==========================================
# FLUJO DE BÚSQUEDA DE PEDIDOS (/PV)
# ==========================================

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
        mensaje += (
            f"\n-----------------------------------\n"
            f"• *Estado factura:* {escapar_markdown(fila.get('Estado Factura', 'N/A'))}\n"
            f"• *Ubicación:* {escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))}\n"
            f"• *Id referencia:* {escapar_markdown(fila.get('Id Refer', 'N/A'))}\n"
            f"• *Color:* {escapar_markdown(fila.get('Color', 'N/A'))}\n"                    
            f"• *Estado Pedido:* {escapar_markdown(fila.get('Document Status SAP', 'N/A'))}\n"
            f"• *Estado Item:* {escapar_markdown(fila.get('Line Status Sap', 'N/A'))}\n"
            f"• *Cantidad pedida:* {escapar_markdown(fila.get('Cantidad Ped', 'N/A'))}\n"
            f"• *Cantidad alistada:* {escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))}\n"           
            f"• *Fecha Despacho:* {escapar_markdown(fila.get('Fecha Factura', 'N/A'))}\n"
            f"• *Nombre Operario Asignado:* {escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))}\n"
            f"• *Estado del Pedido:* {escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))}\n"
            f"• *Observacion Adicional:* {escapar_markdown(fila.get('Observacion Adicional', 'N/A'))}\n"
        )

    keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_colores")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if len(mensaje) > 4000:
        for i in range(0, len(mensaje), 4000):
            await query.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
        await query.edit_message_text(text="📌 Fin del detalle completo.", reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")
    
    return SELECCIONANDO_COLOR

async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        if not context.args:
            await update.message.reply_text(
                "⚠️ Por favor, ingresa el número de documento a buscar.\n"
                "Ejemplo: `/PV 5270`", parse_mode="Markdown"
            )
            return ConversationHandler.END

        doc_buscado = context.args[0].strip()
        catalogo_df = cargar_catalogo()

        if isinstance(catalogo_df, str):
            if catalogo_df.startswith("ERROR"):
                await update.message.reply_text(f"⚠️ Error al conectar con Google Sheets:\n`{catalogo_df}`", parse_mode="Markdown")
                return ConversationHandler.END

        if catalogo_df is None or (isinstance(catalogo_df, pd.DataFrame) and catalogo_df.empty):
            await update.message.reply_text("⚠️ El catálogo está vacío.")
            return ConversationHandler.END

        columna_doc = 'Documento Pd'
        if columna_doc not in catalogo_df.columns:
            await update.message.reply_text(f"⚠️ No se encontró la columna '{columna_doc}'.")
            return ConversationHandler.END

        resultado = catalogo_df[catalogo_df[columna_doc].astype(str).str.strip() == doc_buscado]

        if resultado.empty:
            await update.message.reply_text(f"❌ No se encontró ningún registro con el documento: *{doc_buscado}*.", parse_mode="Markdown")
            return ConversationHandler.END

        context.user_data['df_pedido'] = resultado
        context.user_data['doc_buscado'] = doc_buscado

        referencias = resultado['Id Refer'].unique()
        keyboard = [[InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")] for ref in referencias]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🔍 Documento *{doc_buscado}*.\n\nSelecciona una referencia:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    except Exception as e:
        logger.error(f"Error en iniciar_busqueda: {e}")
        await update.message.reply_text(f"⚠️ Error inesperado: `{e}`", parse_mode="Markdown")
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
            text=f"🔍 Documento *{doc_buscado}*.\n\nSelecciona una referencia:",
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
    colores = df_ref['Color'].astype(str).str.strip().unique()

    keyboard = [[InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")]]
    for color in colores:
        cb_data = f"col_{color}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
        keyboard.append([InlineKeyboardButton(str(color), callback_data=cb_data)])
    keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_referencias")])

    reply_markup = InlineKeyboardMarkup(keyboard)
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
            colores = df_ref['Color'].astype(str).str.strip().unique()

            keyboard = [[InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")]]
            for color in colores:
                cb_data = f"col_{color}"
                if len(cb_data.encode('utf-8')) > 64:
                    cb_data = cb_data[:64]
                keyboard.append([InlineKeyboardButton(str(color), callback_data=cb_data)])
            keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_referencias")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                text=f"Referencia seleccionada: *{ref_elegida}*.\nAhora selecciona el color:",
                reply_markup=reply_markup,
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

        fila = fila_match.iloc[0]
        mensaje = (
            f"🔍 *Detalle del Documento {doc_buscado}*:\n\n"
            f"• *Estado factura:* {escapar_markdown(fila.get('Estado Factura', 'N/A'))}\n"
            f"• *Ubicación:* {escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))}\n"
            f"• *Id referencia:* {escapar_markdown(fila.get('Id Refer', 'N/A'))}\n"
            f"• *Color:* {escapar_markdown(fila.get('Color', 'N/A'))}\n"                    
            f"• *Estado Pedido:* {escapar_markdown(fila.get('Document Status SAP', 'N/A'))}\n"
            f"• *Estado Item:* {escapar_markdown(fila.get('Line Status Sap', 'N/A'))}\n"
            f"• *Cantidad pedida:* {escapar_markdown(fila.get('Cantidad Ped', 'N/A'))}\n"
            f"• *Cantidad alistada:* {escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))}\n"           
            f"• *Fecha Despacho:* {escapar_markdown(fila.get('Fecha Factura', 'N/A'))}\n"
            f"• *Nombre Operario Asignado:* {escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))}\n"
            f"• *Estado del Pedido:* {escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))}\n"
            f"• *Observacion Adicional:* {escapar_markdown(fila.get('Observacion Adicional', 'N/A'))}\n"
        )

        keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_colores")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")
        return SELECCIONANDO_COLOR

    except Exception as e:
        logger.error(f"Error en seleccionar_color: {e}")
        await query.edit_message_text(text=f"⚠️ Error al procesar el color: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Búsqueda cancelada.")
    return ConversationHandler.END
