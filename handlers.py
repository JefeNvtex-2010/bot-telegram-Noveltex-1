import logging
import io
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

# Estados para la conversación interactiva de búsqueda
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR = range(2)

# Enlace de tu Google Sheets adaptado a exportación CSV
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1vw8Vvane83LnGi8kLLznefY-9T3EZCJ6G8lI_wBdWK0/export?format=csv"

def escapar_markdown(texto: str) -> str:
    """Escapa caracteres especiales de Telegram para evitar errores de parseo."""
    if not isinstance(texto, str):
        texto = str(texto)
    caracteres = ['_', '*', '`', '[']
    for c in caracteres:
        texto = texto.replace(c, '')
    return texto.strip()

def _descargar_csv():
    """Función auxiliar robusta usando requests para manejar redirecciones de Google Sheets."""
    response = requests.get(GOOGLE_SHEET_URL, timeout=25)
    response.raise_for_status()
    
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    
    if 'Documento Pd' in df.columns:
        df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
    return df

def cargar_catalogo():
    """Carga el catálogo con un límite de 30 segundos de timeout para evitar bloqueos."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv)
        try:
            df = future.result(timeout=30.0)
            logger.info(f"✅ ¡Catálogo de Google Sheets leído con éxito! ({len(df)} filas)")
            return df
        except concurrent.futures.TimeoutError:
            logger.error("⚠️ Timeout: La conexión tardó más de 30 segundos en responder.")
            return "TIMEOUT"
        except Exception as e:
            logger.error(f"⚠️ Error al leer Google Sheets: {e}")
            return f"ERROR: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "allí"
    await update.message.reply_text(
        f"¡Hola {name}! Bienvenido al sistema de pedidos.\n\n"
        "Usa el comando /pedido para iniciar una nueva orden de compra o /PV [Nro_Documento]."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/reiniciar - Reiniciar servicio de Render (Solo autorizado)\n"
        "/actualizar - Limpiar caché y desplegar en Render (Solo autorizado)"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )

# --- FUNCIONES DE ADMINISTRACIÓN REMOTA DE RENDER ---

async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    MI_TELEGRAM_ID = 5655537446
    
    if update.effective_user.id != MI_TELEGRAM_ID:
        await update.message.reply_text("⛔ No tienes permisos para ejecutar este comando.")
        return

    RENDER_DEPLOY_HOOK_URL = "https://api.render.com/deploy/srv-db2ktt7avr4c73eet090?key=gstG3k654R4"

    try:
        response = requests.post(RENDER_DEPLOY_HOOK_URL)
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
        response = requests.post(RENDER_CACHE_HOOK_URL)
        if response.status_code == 200:
            await update.message.reply_text("🧹 ¡Orden enviada! Limpiando caché y desplegando nueva versión en Render...")
        else:
            await update.message.reply_text(f"⚠️ Error al conectar con Render. Código HTTP: {response.status_code}")
    except Exception as e:
        logger.error(f"Error al limpiar caché en Render: {e}")
        await update.message.reply_text(f"⚠️ Ocurrió un error inesperado: `{e}`", parse_mode="Markdown")

# --- FLUJO INTERACTIVO DE BÚSQUEDA ---

async def mostrar_ver_todo_referencia(query, context) -> int:
    """Muestra todos los colores/ítems de la referencia actualmente seleccionada."""
    resultado = context.user_data.get('df_pedido')
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
    ref_elegida = context.user_data.get('ref_elegida')

    if resultado is None or ref_elegida is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    resultado = resultado.replace('bost_Open', 'abierto')
    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == ref_elegida]

    mensaje = f"🔍 *Detalle Completo - Referencia {ref_elegida}* (Documento {doc_buscado}, Total ítems: {len(df_ref)}):\n"

    for index, fila in df_ref.iterrows():
        id_referencia = escapar_markdown(fila.get('Id Refer', 'N/A'))
        color = escapar_markdown(fila.get('Color', 'N/A'))
        ubicacion = escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))
        estado_de_pedido = escapar_markdown(fila.get('Document Status SAP', 'N/A'))
        line_status_sap = escapar_markdown(fila.get('Line Status Sap', 'N/A'))
        cantidad_pedida = escapar_markdown(fila.get('Cantidad Ped', 'N/A'))
        cantidad_alistada = escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))
        estado_factura = escapar_markdown(fila.get('Estado Factura', 'N/A'))
        fecha_despacho = escapar_markdown(fila.get('Fecha Factura', 'N/A')) 
        operario = escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))
        clasificacion = escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))
        observacion_adicional = escapar_markdown(fila.get('Observacion Adicional', 'N/A'))

        mensaje += (
            f"\n-----------------------------------\n"
            f"• *Estado factura:* {estado_factura}\n"
            f"• *Ubicación:* {ubicacion}\n"
            f"• *Id referencia:* {id_referencia}\n"
            f"• *Color:* {color}\n"                    
            f"• *Estado de Pedido:* {estado_de_pedido}\n"
            f"• *Estado Linea SAP:* {line_status_sap}\n"
            f"• *Cantidad pedida:* {cantidad_pedida}\n"
            f"• *Cantidad alistada:* {cantidad_alistada}\n"           
            f"• *Fecha Despacho:* {fecha_despacho}\n"
            f"• *Operario:* {operario}\n"
            f"• *Clasificación:* {clasificacion}\n"
            f"• *Observacion Adicional:* {observacion_adicional}\n"
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
            if catalogo_df == "TIMEOUT":
                await update.message.reply_text("⏱️ La consulta a Google Sheets tardó demasiado (más de 30s). Por favor, intenta de nuevo con `/PV [número]`.", parse_mode="Markdown")
                return ConversationHandler.END
            elif catalogo_df.startswith("ERROR"):
                await update.message.reply_text(f"⚠️ Error al conectar con Google Sheets:\n`{catalogo_df}`", parse_mode="Markdown")
                return ConversationHandler.END

        if catalogo_df is None or (isinstance(catalogo_df, pd.DataFrame) and catalogo_df.empty):
            await update.message.reply_text("⚠️ El catálogo de Google Sheets no está disponible o está vacío.")
            return ConversationHandler.END

        columna_doc = 'Documento Pd'

        if columna_doc not in catalogo_df.columns:
            await update.message.reply_text(f"⚠️ No se encontró la columna '{columna_doc}' en la hoja.")
            return ConversationHandler.END

        resultado = catalogo_df[catalogo_df[columna_doc] == doc_buscado]

        if resultado.empty:
            await update.message.reply_text(f"❌ No se encontró ningún registro con el documento: *{doc_buscado}*.", parse_mode="Markdown")
            return ConversationHandler.END

        context.user_data['df_pedido'] = resultado
        context.user_data['doc_buscado'] = doc_buscado

        referencias = resultado['Id Refer'].unique()

        # Usar la clasificación general pero con la etiqueta solicitada: Estado del Pedido
        estado_pedido_general = "N/A"
        if 'Clasificacion Pedido' in resultado.columns and not resultado.empty:
            val_estado = resultado.iloc[0].get('Clasificacion Pedido')
            if val_estado:
                estado_pedido_general = escapar_markdown(str(val_estado))

        keyboard = []
        for ref in referencias:
            keyboard.append([InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🔍 Documento *{doc_buscado}*.\n"
            f"• *Estado del Pedido:* {estado_pedido_general}\n\n"
            f"Selecciona una referencia:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    except Exception as e:
        logger.error(f"Excepción no controlada en iniciar_busqueda: {e}")
        await update.message.reply_text(f"⚠️ Ocurrió un error inesperado al procesar tu búsqueda: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END

async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    data_callback = query.data

    if data_callback == "volver_referencias":
        resultado = context.user_data.get('df_pedido')
        doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')

        if resultado is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
            return ConversationHandler.END

        referencias = resultado['Id Refer'].unique()
        
        estado_pedido_general = "N/A"
        if 'Clasificacion Pedido' in resultado.columns and not resultado.empty:
            val_estado = resultado.iloc[0].get('Clasificacion Pedido')
            if val_estado:
                estado_pedido_general = escapar_markdown(str(val_estado))

        keyboard = []
        for ref in referencias:
            keyboard.append([InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=f"🔍 Documento *{doc_buscado}*.\n"
                 f"• *Estado del Pedido:* {estado_pedido_general}\n\n"
                 f"Selecciona una referencia:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    referencia_elegida = data_callback.replace("ref_", "").strip()
    resultado = context.user_data.get('df_pedido')
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')

    if resultado is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data['ref_elegida'] = referencia_elegida
    
    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == referencia_elegida]
    
    colores = df_ref['Color'].astype(str).str.strip().unique()

    keyboard = []
    keyboard.append([InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")])

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
                await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
                return ConversationHandler.END

            resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
            df_ref = resultado[resultado['Id Refer'] == ref_elegida]
            colores = df_ref['Color'].astype(str).str.strip().unique()

            keyboard = []
            keyboard.append([InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")])
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
            await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
            return ConversationHandler.END

        resultado = resultado.replace('bost_Open', 'abierto')

        resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
        resultado['Color'] = resultado['Color'].astype(str).str.strip()

        fila_match = resultado[(resultado['Id Refer'] == ref_elegida) & (resultado['Color'] == color_elegido)]

        if fila_match.empty:
            await query.edit_message_text(text=f"❌ No se encontró información para la referencia *{ref_elegida}* y color *{color_elegido}*.", parse_mode="Markdown")
            return ConversationHandler.END

        fila = fila_match.iloc[0]
        
        id_referencia = escapar_markdown(fila.get('Id Refer', 'N/A'))
        color = escapar_markdown(fila.get('Color', 'N/A'))
        ubicacion = escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))
        estado_de_pedido = escapar_markdown(fila.get('Document Status SAP', 'N/A'))
        line_status_sap = escapar_markdown(fila.get('Line Status Sap', 'N/A'))
        cantidad_pedida = escapar_markdown(fila.get('Cantidad Ped', 'N/A'))
        cantidad_alistada = escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))
        estado_factura = escapar_markdown(fila.get('Estado Factura', 'N/A'))
        fecha_despacho = escapar_markdown(fila.get('Fecha Factura', 'N/A')) 
        operario = escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))
        clasificacion = escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))
        observacion_adicional = escapar_markdown(fila.get('Observacion Adicional', 'N/A'))

        mensaje = (
            f"🔍 *Detalle del Documento {doc_buscado}*:\n\n"
            f"• *Estado factura:* {estado_factura}\n"
            f"• *Ubicación:* {ubicacion}\n"
            f"• *Id referencia:* {id_referencia}\n"
            f"• *Color:* {color}\n"                    
            f"• *Estado de Pedido:* {estado_de_pedido}\n"
            f"• *Estado Linea SAP:* {line_status_sap}\n"
            f"• *Cantidad pedida:* {cantidad_pedida}\n"
            f"• *Cantidad alistada:* {cantidad_alistada}\n"           
            f"• *Fecha Despacho:* {fecha_despacho}\n"
            f"• *Operario:* {operario}\n"
            f"• *Clasificación:* {clasificacion}\n"
            f"• *Observacion Adicional:* {observacion_adicional}\n"
        )

        keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_colores")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")
        return SELECCIONANDO_COLOR

    except Exception as e:
        logger.error(f"Error en seleccionar_color: {e}")
        await query.edit_message_text(text=f"⚠️ Ocurrió un error al procesar el color: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Búsqueda cancelada.")
    return ConversationHandler.END
