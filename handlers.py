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

# Estados para la conversación interactiva de búsqueda de pedidos (/PV)
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR = range(2)

# Estados para la conversación interactiva de inventario (/in)
SELECCIONANDO_REF_SAP = 0

# Enlaces de Google Sheets / Drive adaptados
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"
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
    
    try:
        # Intentamos leer de forma estándar pero tolerante a filas con errores de columnas
        df = pd.read_csv(
            io.StringIO(response.text), 
            dtype=str, 
            keep_default_na=False, 
            on_bad_lines='skip'
        )
    except Exception:
        # Si falla el motor C, usamos el motor de python que es más flexible con archivos irregulares
        df = pd.read_csv(
            io.StringIO(response.text), 
            dtype=str, 
            keep_default_na=False, 
            engine='python',
            on_bad_lines='skip'
        )
        
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
        "Usa los comandos:\n"
        "• /pedido - Registrar o consultar pedidos\n"
        "• /PV [Nro_Documento] - Búsqueda de pedidos\n"
        "• /in [Nombre_o_Descripcion] - Consultar inventario desde Drive\n"
        "• /help - Ayuda"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/in [Descripcion] - Consultar inventario desde Google Drive"
    )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📦 ¡Perfecto! Vamos a gestionar tu pedido.\n\n"
        "Usa /PV seguido del número de documento para consultar el estatus.",
        parse_mode="Markdown"
    )


# ==========================================
# FLUJO 1: BÚSQUEDA /PV (Google Sheets Pedidos)
# ==========================================

async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Punto de entrada para la búsqueda interactiva de pedidos con /PV."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, indica el número de documento o término a buscar.\nEjemplo: `/PV 12345`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino = " ".join(context.args).strip()
    msg = await update.message.reply_text("🔄 Buscando pedido en Google Sheets...", parse_mode="Markdown")

    df = cargar_catalogo()
    if isinstance(df, str):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Error al acceder al archivo de pedidos: `{df}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    try:
        # Filtrado rápido de ejemplo por Documento Pd
        col_doc = next((c for c in df.columns if 'documento' in c.lower() or 'doc' in c.lower()), df.columns[0])
        df_filtrado = df[df[col_doc].astype(str).str.contains(termino, case=False, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron registros para: *{termino}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        # Mostrar resultados principales con botones
        resultados = df_filtrado.head(10).to_dict(orient="records")
        keyboard = []
        for idx, row in enumerate(resultados):
            doc_val = str(row.get(col_doc, f"Opción {idx}"))
            keyboard.append([InlineKeyboardButton(f"Doc: {doc_val}", callback_data=f"ref_{doc_val}")])

        context.user_data['df_pedidos'] = df
        context.user_data['col_doc'] = col_doc

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"🔍 Se encontraron {len(df_filtrado)} registros. Selecciona uno:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REFERENCIA

    except Exception as e:
        logger.error(f"Error en búsqueda de pedidos: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Ocurrió un error procesando la consulta: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("ref_"):
        seleccion = data.replace("ref_", "").strip()
        df = context.user_data.get('df_pedidos')
        col_doc = context.user_data.get('col_doc', 'Documento Pd')

        if df is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Realiza la búsqueda de nuevo con `/PV`.", parse_mode="Markdown")
            return ConversationHandler.END

        fila_match = df[df[col_doc].astype(str).str.strip() == seleccion]
        if fila_match.empty:
            await query.edit_message_text(text="❌ No se encontró la información detallada.", parse_mode="Markdown")
            return ConversationHandler.END

        fila = fila_match.iloc[0]
        texto_res = f"📄 *Detalle del Pedido*\n\n"
        for col, val in fila.items():
            if val and str(val).strip() != "":
                texto_res += f"• *{escapar_markdown(col)}:* `{escapar_markdown(val)}`\n"

        if len(texto_res) > 4000:
            texto_res = texto_res[:4000]

        await query.edit_message_text(text=texto_res, parse_mode="Markdown")

    return ConversationHandler.END

async def seleccionar_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Manejador auxiliar para estados de color si se requiere en el flujo."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="Proceso completado.")
    return ConversationHandler.END


# ==========================================
# FLUJO 2: BÚSQUEDA /in (Inventario Google Drive)
# ==========================================

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
        col_nombre = next((col for col in df_inv.columns if 'name' in col.lower() or 'desc' in col.lower() or 'articulo' in col.lower()), df_inv.columns[1])
        col_codigo = next((col for col in df_inv.columns if 'code' in col.lower() or 'ref' in col.lower() or 'codigo' in col.lower()), df_inv.columns[0])

        df_filtrado = df_inv[df_inv[col_nombre].astype(str).str.upper().str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron artículos con la descripción: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        resultados = df_filtrado.head(15).to_dict(orient="records")
        keyboard = []
        for row in resultados:
            codigo = str(row.get(col_codigo, 'N/A'))
            nombre = str(row.get(col_nombre, 'N/A'))
            keyboard.append([InlineKeyboardButton(f"{codigo} - {nombre[:35]}", callback_data=f"sapref_{codigo}")])

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
        detalle_texto = f"🟢 *Detalle de Inventario (Drive)*\n\n"
        for col, val in fila.items():
            if val and str(val).strip() != "":
                detalle_texto += f"• *{escapar_markdown(col)}:* `{escapar_markdown(val)}`\n"

        if len(detalle_texto) > 4000:
            detalle_texto = detalle_texto[:4000]

        await query.edit_message_text(text=detalle_texto, parse_mode="Markdown")
    
    return ConversationHandler.END


# ==========================================
# UTILIDADES Y COMANDOS DE CONTROL
# ==========================================

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela la conversación actual."""
    if update.message:
        await update.message.reply_text("Operación cancelada.")
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text="Operación cancelada.")
    return ConversationHandler.END

async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando para reiniciar."""
    await update.message.reply_text("Reiniciando servicio...")

async def limpiar_cache_y_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando para limpiar caché."""
    await update.message.reply_text("Limpiando caché y actualizando...")
