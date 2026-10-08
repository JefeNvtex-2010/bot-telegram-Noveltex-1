import logging
import io
import time
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from config import load_settings

logger = logging.getLogger(__name__)

# --- CACHÉ DE INVENTARIO DRIVE ---
inventario_cache = {}
CACHE_TTL = 1800  # 1800 segundos = 30 minutos

# Estados para la conversación interactiva de búsqueda
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR = range(2)
SELECCIONANDO_REF_SAP = 2

# Enlaces de Google Sheets / Drive adaptados a exportación CSV
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"
# Nuevo enlace de inventario convertido para descarga directa CSV desde Google Drive
INVENTARIO_DRIVE_URL = "https://docs.google.com/uc?export=download&id=1FJdfaNhxcFFDVD_AV2lTITHw0f-mB0Y2"

def escapar_markdown(texto: str) -> str:
    """Escapa caracteres especiales de Telegram para evitar errores de parseo."""
    if not isinstance(texto, str):
        texto = str(texto)
    caracteres = ['_', '*', '`', '[']
    for c in caracteres:
        texto = texto.replace(c, '')
    return texto.strip()

def _descargar_csv(url):
    """Función auxiliar robusta usando requests para descargar CSVs de Google."""
    response = requests.get(url, timeout=25)
    response.raise_for_status()
    
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df

def cargar_catalogo():
    """Carga el catálogo de pedidos con un límite de 30 segundos de timeout."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv, GOOGLE_SHEET_URL)
        try:
            df = future.result(timeout=30.0)
            if 'Documento Pd' in df.columns:
                df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer Google Sheets de pedidos: {e}")
            return f"ERROR: {e}"

def cargar_inventario_drive():
    """Carga el archivo de inventario desde el nuevo enlace de Google Drive."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv, INVENTARIO_DRIVE_URL)
        try:
            df = future.result(timeout=30.0)
            logger.info(f"✅ ¡Inventario de Drive leído con éxito! ({len(df)} filas)")
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer el inventario de Google Drive: {e}")
            return f"ERROR: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "allí"
    await update.message.reply_text(
        f"¡Hola {name}! Bienvenido al sistema de pedidos.\n\n"
        "Usa el comando /pedido para iniciar una nueva orden, /PV [Nro_Documento] o /in [Nombre_o_Descripcion]."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/in [Descripcion] - Consultar inventario desde Google Drive por nombre"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a registrar tu pedido.\n\n"
        "Por favor, escribe el **nombre del producto** que necesitas:",
        parse_mode="Markdown"
    )

# --- BÚSQUEDA DE INVENTARIO DESDE EL NUEVO LINK DE DRIVE ---

async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca artículos en el inventario de Google Drive por coincidencia parcial en la descripción."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa el nombre o descripción a buscar.\nEjemplo: `/in CAPRI`", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    
    msg = await update.message.reply_text("🔄 Buscando en el archivo de inventario...", parse_mode="Markdown")

    df_inv = cargar_inventario_drive()
    if isinstance(df_inv, str):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ No se pudo acceder al archivo de inventario en Drive: `{df_inv}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    try:
        # Buscamos coincidencias flexibles en las columnas del dataframe de Drive
        # Ajusta 'ItemName' o 'Descripcion' según el nombre exacto de la columna de texto de tu archivo
        col_nombre = next((col for col in df_inv.columns if 'name' in col.lower() or 'desc' in col.lower() or 'articulo' in col.lower()), df_inv.columns[1])
        col_codigo = next((col for col in df_inv.columns if 'code' in col.lower() or 'ref' in col.lower() or 'codigo' in col.lower()), df_inv.columns[0])

        # Filtrar filas que contengan el término de búsqueda
        df_filtrado = df_inv[df_inv[col_nombre].astype(str).str.upper().str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron artículos con la descripción: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        # Convertir resultados a lista para mostrar botones
        resultados = df_filtrado.head(15).to_dict(orient="records")
        
        keyboard = []
        for row in resultados:
            codigo = str(row.get(col_codigo, 'N/A'))
            nombre = str(row.get(col_nombre, 'N/A'))
            keyboard.append([InlineKeyboardButton(f"{codigo} - {nombre[:35]}", callback_data=f"sapref_{codigo}")])

        # Guardar el dataframe temporalmente en el context del usuario
        context.user_data['df_inventario_drive'] = df_inv
        context.user_data['col_codigo'] = col_codigo
        context.user_data['col_nombre'] = col_nombre

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"🔍 Se encontraron {len(df_filtrado)} coincidencias para *{termino_busqueda}*.\nSelecciona una referencia:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REF_SAP

    except Exception as e:
        logger.error(f"Error procesando inventario de Drive: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Ocurrió un error procesando el archivo: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_ref_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("sapref_"):
        codigo_elegido = data.replace("sapref_", "").strip()
        df_inv = context.user_data.get('df_inventario_drive')
        col_codigo = context.user_data.get('col_codigo', 'ItemCode')

        if df_inv is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Realiza la búsqueda de nuevo con `/in [nombre]`.", parse_mode="Markdown")
            return ConversationHandler.END

        fila_match = df_inv[df_inv[col_codigo].astype(str).str.strip() == codigo_elegido]

        if fila_match.empty:
            await query.edit_message_text(text="❌ No se encontró información detallada para este artículo.", parse_mode="Markdown")
            return ConversationHandler.END

        fila = fila_match.iloc[0]
        
        # Construir el mensaje con todas las columnas disponibles de esa fila del archivo
        detalle_texto = f"🟢 *Detalle de Inventario (Drive)*\n\n"
        for col, val in fila.items():
            if val and str(val).strip() != "":
                detalle_texto += f"• *{escapar_markdown(col)}:* `{escapar_markdown(val)}`\n"

        if len(detalle_texto) > 4000:
            detalle_texto = detalle_texto[:4000]

        await query.edit_message_text(text=detalle_texto, parse_mode="Markdown")
    
    return ConversationHandler.END

# --- FLUJO INTERACTIVO DE BÚSQUEDA DE PEDIDOS (GOOGLE SHEETS) ---
# (Se conservan tus funciones estándar para /PV, iniciar_busqueda, seleccionar_referencia, seleccionar_color, etc.)
