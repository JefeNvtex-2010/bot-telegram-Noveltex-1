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

# Estados para la conversación de inventario (/in)
SELECCIONANDO_REF_SAP = 2

# Enlace principal de Google Sheets (Pedidos)
GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"

# Enlace del Google Sheets para Inventario (usa la misma URL base y solo agregas el gid de tu pestaña "INVENTARIO BOT")
# Ejemplo: ".../export?format=csv&gid=TU_GID_AQUI"
INVENTARIO_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"

def escapar_markdown(texto: str) -> str:
    """Escapa caracteres especiales de Telegram para evitar errores de parseo."""
    if not isinstance(texto, str):
        texto = str(texto)
    caracteres = ['_', '*', '`', '[']
    for c in caracteres:
        texto = texto.replace(c, '')
    return texto.strip()

def _descargar_csv(url):
    """Función auxiliar robusta usando requests para descargar cualquier pestaña de Google Sheets en CSV."""
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
            logger.info(f"✅ ¡Catálogo de pedidos leído con éxito! ({len(df)} filas)")
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer Google Sheets de pedidos: {e}")
            return f"ERROR: {e}"

def cargar_inventario_drive():
    """Carga el inventario desde la pestaña de Google Sheets con exportación directa CSV."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv, INVENTARIO_SHEET_URL)
        try:
            df = future.result(timeout=20.0)
            logger.info(f"✅ ¡Inventario de Google Sheets leído con éxito! ({len(df)} filas)")
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer inventario de Google Sheets: {e}")
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
# FLUJO 1: BÚSQUEDA DE PEDIDOS (/PV)
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
            if catalogo_df == "TIMEOUT":
                await update.message.reply_text("⏱️ La consulta tardó demasiado. Intenta de nuevo.", parse_mode="Markdown")
                return ConversationHandler.END
            elif catalogo_df.startswith("ERROR"):
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


# ==========================================
# FLUJO 2: BÚSQUEDA DE INVENTARIO DESDE GOOGLE SHEETS (/in)
# ==========================================

async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca en la columna REFERENCIA de la pestaña de inventario y muestra los colores disponibles."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa la referencia a buscar.\nEjemplo: `/in FRESBURA`", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    msg = await update.message.reply_text("🔄 Buscando referencia en inventario...", parse_mode="Markdown")

    df_inv = cargar_inventario_drive()
    if isinstance(df_inv, str):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ No se pudo acceder a la pestaña de inventario: `{df_inv}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    try:
        cols_map = {str(c).upper().strip(): c for c in df_inv.columns}
        
        col_ref = cols_map.get('REFERENCIA')
        col_color = cols_map.get('COLOR')

        if not col_ref:
            col_ref = next((c for c in df_inv.columns if 'ref' in c.lower()), df_inv.columns[7] if len(df_inv.columns) > 7 else df_inv.columns[0])
        if not col_color:
            col_color = next((c for c in df_inv.columns if 'color' in c.lower()), df_inv.columns[8] if len(df_inv.columns) > 8 else df_inv.columns[1])

        df_filtrado = df_inv[df_inv[col_ref].astype(str).str.upper().str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron referencias coincidentes para: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        colores_unicos = df_filtrado[[col_color]].drop_duplicates().head(20).values

        keyboard = []
        for row_c in colores_unicos:
            color_val = str(row_c[0])
            cb_data = f"invcol_{color_val}"
            if len(cb_data.encode('utf-8')) > 64:
                cb_data = cb_data[:64]
            keyboard.append([InlineKeyboardButton(color_val, callback_data=cb_data)])

        context.user_data['df_inventario_drive'] = df_inv
        context.user_data['ref_buscada_inv'] = termino_busqueda
        context.user_data['col_ref'] = col_ref
        context.user_data['col_color'] = col_color

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"🔍 Referencia encontrada: *{termino_busqueda}*.\nSelecciona un color:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REF_SAP

    except Exception as e:
        logger.error(f"Error procesando inventario: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Ocurrió un error procesando los datos: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_ref_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("invcol_"):
        color_elegido = data.replace("invcol_", "").strip()
        df_inv = context.user_data.get('df_inventario_drive')
        ref_buscada = context.user_data.get('ref_buscada_inv')
        col_ref = context.user_data.get('col_ref', 'REFERENCIA')
        col_color = context.user_data.get('col_color', 'COLOR')

        if df_inv is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Busca de nuevo con `/in [referencia]`.", parse_mode="Markdown")
            return ConversationHandler.END

        filas_match = df_inv[
            (df_inv[col_ref].astype(str).str.upper().str.contains(ref_buscada, na=False)) & 
            (df_inv[col_color].astype(str).str.strip() == color_elegido)
        ]

        if filas_match.empty:
            await query.edit_message_text(text="❌ No se encontró información detallada para este color.", parse_mode="Markdown")
            return ConversationHandler.END

        detalle_texto = f"🟢 *Inventario - {ref_buscada}* / *{color_elegido}*\n"
        
        for idx, fila in filas_match.iterrows():
            detalle_texto += f"\n-----------------------------------\n"
            for col, val in fila.items():
                if val is not None and str(val).strip() != "":
                    detalle_texto += f"• *{escapar_markdown(col)}:* `{escapar_markdown(val)}`\n"

        if len(detalle_texto) > 4000:
            detalle_texto = detalle_texto[:4000]

        await query.edit_message_text(text=detalle_texto, parse_mode="Markdown")
    
    return ConversationHandler.END


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Búsqueda cancelada.")
    return ConversationHandler.END
