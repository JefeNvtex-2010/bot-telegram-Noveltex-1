import logging
import pandas as pd
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Enlace de tu Google Sheets adaptado a exportación CSV
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1vw8Vvane83LnGi8kLLznefY-9T3EZCJ6G8lI_wBdWK0/export?format=csv"

def cargar_catalogo():
    try:
        df = pd.read_csv(GOOGLE_SHEET_URL, dtype=str, keep_default_na=False)
        df.columns = df.columns.str.strip()
        
        # Limpiar la columna de documentos para evitar errores de espacios o decimales (.0)
        if 'Documento Pd' in df.columns:
            df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
            
        logger.info(f"✅ ¡Catálogo de Google Sheets leído con éxito! ({len(df)} filas)")
        return df
    except Exception as e:
        logger.error(f"⚠️ Error al leer Google Sheets: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "allí"
    await update.message.reply_text(
        f"¡Hola {name}! Bienvenido al sistema de pedidos.\n\n"
        "Usa el comando /pedido para iniciar una nueva orden de compra o /buscar [Nro_Documento]."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/buscar [Nro_Documento] - Consultar estatus en Google Sheets"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )

async def buscar_pedido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa el número de documento a buscar.\n"
            "Ejemplo: `/buscar 6530`", parse_mode="Markdown"
        )
        return

    doc_buscado = context.args[0].strip()

    # CARGAR SIEMPRE FRESCO DE GOOGLE SHEETS EN CADA BÚSQUEDA (Adiós a la memoria estática)
    catalogo_df = cargar_catalogo()

    if catalogo_df is None:
        await update.message.reply_text("⚠️ El catálogo de Google Sheets no está disponible.")
        return

    columna_doc = 'Documento Pd'

    if columna_doc not in catalogo_df.columns:
        await update.message.reply_text(f"⚠️ No se encontró la columna '{columna_doc}' en la hoja.")
        return

    # Filtramos todas las filas que coincidan con el Documento Pd
    resultado = catalogo_df[catalogo_df[columna_doc] == doc_buscado]

    if resultado.empty:
        await update.message.reply_text(f"❌ No se encontró ningún registro con el documento: *{doc_buscado}*.", parse_mode="Markdown")
        return

    # Encabezado del mensaje
    mensaje = f"🔍 *Detalle del Documento {doc_buscado}* (Total ítems: {len(resultado)}):\n"

    # Iteramos por cada fila/referencia encontrada
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
            f"• *Id referencia:* {id_referencia}\n"
            f"• *Color:* {color}\n"
            f"• *Ubicación:* {ubicacion}\n"
            f"• *Document Status SAP:* {doc_status_sap}\n"
            f"• *Line Status Sap:* {line_status_sap}\n"
            f"• *Cantidad pedida:* {cantidad_pedida}\n"
            f"• *Cantidad alistada:* {cantidad_alistada}\n"
            f"• *Estado factura:* {estado_factura}\n"
            f"• *Fecha Despacho:* {fecha_despacho}\n"
            f"• *Id Operario Asignado:* {id_operario}\n"
            f"• *Estado del Pedido:* {estado_pedido}\n"
        )

    if len(mensaje) > 4000:
        for i in range(0, len(mensaje), 4000):
            await update.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
    else:
        await update.message.reply_text(mensaje, parse_mode="Markdown")
