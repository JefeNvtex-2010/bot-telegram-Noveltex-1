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

def _descargar_csv():
    """Función auxiliar robusta usando requests para manejar redirecciones de Google Sheets."""
    response = requests.get(GOOGLE_SHEET_URL, timeout=25)
    response.raise_for_status()
    
    # Leer el texto descargado con pandas usando StringIO
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    
    # Limpiar la columna de documentos para evitar errores de espacios o decimales (.0)
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
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )

# --- FLUJO INTERACTIVO DE BÚSQUEDA ---

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

        # Validación corregida para evitar conflictos con DataFrames de Pandas
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

        keyboard = []
        keyboard.append([InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")])

        for ref in referencias:
            keyboard.append([InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🔍 Documento *{doc_buscado}*.\nSelecciona una referencia o elige 'Ver todo':",
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

    referencia_elegida = query.data.replace("ref_", "")
    resultado = context.user_data.get('df_pedido')
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')

    if resultado is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    if referencia_elegida == "ver_todo":
        mensaje = f"🔍 *Detalle Completo del Documento {doc_buscado}* (Total ítems: {len(resultado)}):\n"

        for index, fila in resultado.iterrows():
            id_referencia = fila.get('Id Refer', 'N/A')
            color = fila.get('Color', 'N/A')
            ubicacion = fila.get('Ubicación del Pedido', 'N/A')
            doc_status_sap = fila.get('Document Status SAP', 'N/A')
            line_status_sap = fila.get('Line Status Sap', 'N/A')
            cantidad_pedida = fila.get('Cantidad Ped', 'N/A')
            cantidad_alistada = fila.get('Cantidad Alistada', 'N/A')
            estado_factura = fila.get('Estado Factura', 'N/A')
            fecha_despacho = fila.get('Fecha Factura', 'N/A') 
            id_operario = fila.get('Id Operario Asignado', 'N/A') 
            estado_pedido = fila.get('Clasificacion Pedido', 'N/A')

            mensaje += (
                f"\n-----------------------------------\n"
                f"• *Estado factura:* {estado_factura}\n"
                f"• *Ubicación:* {ubicacion}\n"
                f"• *Id referencia:* {id_referencia}\n"
                f"• *Color:* {color}\n"            
                f"• *Document Status SAP:* {doc_status_sap}\n"
                f"• *Line Status Sap:* {line_status_sap}\n"
                f"• *Cantidad pedida:* {cantidad_pedida}\n"
                f"• *Cantidad alistada:* {cantidad_alistada}\n"            
                f"• *Fecha Despacho:* {fecha_despacho}\n"
                f"• *Id Operario Asignado:* {id_operario}\n"
                f"• *Estado del Pedido:* {estado_pedido}\n"
            )

        if len(mensaje) > 4000:
            for i in range(0, len(mensaje), 4000):
                await query.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
        else:
            await query.edit_message_text(text=mensaje, parse_mode="Markdown")
        
        return ConversationHandler.END

    context.user_data['ref_elegida'] = referencia_elegida
    df_ref = resultado[resultado['Id Refer'] == referencia_elegida]
    colores = df_ref['Color'].unique()

    keyboard = []
    for color in colores:
        keyboard.append([InlineKeyboardButton(str(color), callback_data=f"col_{color}")])

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

    color_elegido = query.data.replace("col_", "")
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
    ref_elegida = context.user_data.get('ref_elegida')
    resultado = context.user_data.get('df_pedido')

    if resultado is None or ref_elegida is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    fila_match = resultado[(resultado['Id Refer'] == ref_elegida) & (resultado['Color'] == color_elegido)]

    if fila_match.empty:
        await query.edit_message_text(text="❌ No se encontró información para esa combinación.")
        return ConversationHandler.END

    fila = fila_match.iloc[0]

    id_referencia = fila.get('Id Refer', 'N/A')
    color = fila.get('Color', 'N/A')
    ubicacion = fila.get('Ubicación del Pedido', 'N/A')
    doc_status_sap = fila.get('Document Status SAP', 'N/A')
    line_status_sap = fila.get('Line Status Sap', 'N/A')
    cantidad_pedida = fila.get('Cantidad Ped', 'N/A')
    cantidad_alistada = fila.get('Cantidad Alistada', 'N/A')
    estado_factura = fila.get('Estado Factura', 'N/A')
    fecha_despacho = fila.get('Fecha Factura', 'N/A') 
    id_operario = fila.get('Id Operario Asignado', 'N/A') 
    estado_pedido = fila.get('Clasificacion Pedido', 'N/A')

    mensaje = (
        f"🔍 *Detalle del Documento {doc_buscado}*:\n\n"
        f"• *Estado factura:* {estado_factura}\n"
        f"• *Ubicación:* {ubicacion}\n"
        f"• *Id referencia:* {id_referencia}\n"
        f"• *Color:* {color}\n"            
        f"• *Document Status SAP:* {doc_status_sap}\n"
        f"• *Line Status Sap:* {line_status_sap}\n"
        f"• *Cantidad pedida:* {cantidad_pedida}\n"
        f"• *Cantidad alistada:* {cantidad_alistada}\n"            
        f"• *Fecha Despacho:* {fecha_despacho}\n"
        f"• *Id Operario Asignado:* {id_operario}\n"
        f"• *Estado del Pedido:* {estado_pedido}"
    )

    await query.edit_message_text(text=mensaje, parse_mode="Markdown")
    return ConversationHandler.END

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Búsqueda cancelada.")
    return ConversationHandler.END
