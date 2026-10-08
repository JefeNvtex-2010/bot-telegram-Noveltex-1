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

# Enlaces de Google Sheets / Drive
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

def _descargar_csv():
    """Función auxiliar robusta usando requests para manejar redirecciones de Google Sheets (Pedidos)."""
    response = requests.get(GOOGLE_SHEET_URL, timeout=25)
    response.raise_for_status()
    
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    
    if 'Documento Pd' in df.columns:
        df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
    return df

def _descargar_inventario_csv():
    """Función auxiliar robusta con detección automática de separador para el inventario de Google Drive."""
    response = requests.get(INVENTARIO_DRIVE_URL, timeout=25)
    response.raise_for_status()
    
    try:
        # sep=None con engine='python' detecta automáticamente si el CSV usa comas, puntos y comas o tabuladores
        df = pd.read_csv(
            io.StringIO(response.text), 
            dtype=str, 
            keep_default_na=False, 
            sep=None, 
            engine='python',
            on_bad_lines='skip'
        )
    except Exception:
        df = pd.read_csv(
            io.StringIO(response.text), 
            dtype=str, 
            keep_default_na=False, 
            on_bad_lines='skip'
        )
        
    df.columns = df.columns.str.strip()
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

def cargar_inventario_drive():
    """Carga el inventario desde Google Drive con un límite de 30 segundos de timeout."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_inventario_csv)
        try:
            df = future.result(timeout=30.0)
            logger.info(f"✅ ¡Inventario de Drive leído con éxito! ({len(df)} filas - Columnas: {list(df.columns)})")
            return df
        except concurrent.futures.TimeoutError:
            logger.error("⚠️ Timeout en inventario Drive.")
            return "TIMEOUT"
        except Exception as e:
            logger.error(f"⚠️ Error al leer inventario de Google Drive: {e}")
            return f"ERROR: {e}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user else "allí"
    await update.message.reply_text(
        f"¡Hola {name}! Bienvenido al sistema de pedidos.\n\n"
        "Usa los comandos:\n"
        "• /pedido - Registrar una nueva orden de compra\n"
        "• /PV [Nro_Documento] - Consultar estatus de pedidos\n"
        "• /in [Descripcion] - Consultar inventario por descripción (tipo filtro Excel)"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos disponibles:\n"
        "/start - Iniciar el bot\n"
        "/pedido - Registrar un pedido\n"
        "/PV [Nro_Documento] - Consultar estatus en Google Sheets\n"
        "/in [Descripcion] - Filtrar inventario por descripción\n"
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

    resultado = resultado.replace('bost_Open', 'Abierto')
    resultado = resultado.replace('bost_Close', 'Cerrado')

    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == ref_elegida]

    mensaje = f"🔍 *Detalle Completo - Referencia {ref_elegida}* (Documento {doc_buscado}, Total ítems: {len(df_ref)}):\n"

    for index, fila in df_ref.iterrows():
        id_referencia = escapar_markdown(fila.get('Id Refer', 'N/A'))
        color = escapar_markdown(fila.get('Color', 'N/A'))
        ubicacion = escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))
        doc_status_sap = escapar_markdown(fila.get('Document Status SAP', 'N/A'))
        line_status_sap = escapar_markdown(fila.get('Line Status Sap', 'N/A'))
        cantidad_pedida = escapar_markdown(fila.get('Cantidad Ped', 'N/A'))
        cantidad_alistada = escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))
        estado_factura = escapar_markdown(fila.get('Estado Factura', 'N/A'))
        fecha_despacho = escapar_markdown(fila.get('Fecha Factura', 'N/A')) 
        nombre_operario = escapar_markdown(fila.get('Nombre Operario Asignado', 'N/A'))
        estado_pedido = escapar_markdown(fila.get('Clasificacion Pedido', 'N/A'))
        observacion_adicional = escapar_markdown(fila.get('Observacion Adicional', 'N/A'))

        mensaje += (
            f"\n-----------------------------------\n"
            f"• *Estado factura:* {estado_factura}\n"
            f"• *Ubicación:* {ubicacion}\n"
            f"• *Id referencia:* {id_referencia}\n"
            f"• *Color:* {color}\n"                    
            f"• *Estado Pedido:* {doc_status_sap}\n"
            f"• *Estado Item:* {line_status_sap}\n"
            f"• *Cantidad pedida:* {cantidad_pedida}\n"
            f"• *Cantidad alistada:* {cantidad_alistada}\n"           
            f"• *Fecha Despacho:* {fecha_despacho}\n"
            f"• *Nombre Operario Asignado:* {nombre_operario}\n"
            f"• *Estado del Pedido:* {estado_pedido}\n"
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
# FLUJO 2: BÚSQUEDA DE INVENTARIO DESDE DRIVE (/in) - FILTRO ESTILO EXCEL
# ==========================================

async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Busca y filtra artículos por coincidencia parcial en la columna Descripción."""
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa el texto a buscar en la descripción.\nEjemplo: `/in CAPRI`", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    msg = await update.message.reply_text("🔄 Filtrando inventario...", parse_mode="Markdown")

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
        # Detección segura de columnas basadas en las columnas reales del archivo
        cols = [c.strip() for c in df_inv.columns]
        
        # Buscar columna de descripción (contiene 'desc' o es la segunda columna)
        col_nombre = next((c for c in cols if 'desc' in c.lower()), cols[1] if len(cols) > 1 else cols[0])
        # Buscar columna de artículo/código (contiene 'art' o 'cod' o 'ref' o es la primera)
        col_codigo = next((c for c in cols if 'art' in c.lower() or 'cod' in c.lower() or 'ref' in c.lower()), cols[0])

        # FILTRAR ESTILO EXCEL: Buscar exclusivamente en la columna Descripción de forma tolerante a mayúsculas
        df_filtrado = df_inv[df_inv[col_nombre].astype(str).str.upper().str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron coincidencias en la descripción para: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        # Tomamos hasta 20 resultados para mostrar en los botones estilo lista de Excel
        resultados = df_filtrado.head(20).to_dict(orient="records")
        keyboard = []
        for row in resultados:
            codigo = str(row.get(col_codigo, 'N/A'))
            nombre = str(row.get(col_nombre, 'N/A'))
            
            # Buscar columna de almacén de forma flexible
            col_alm = next((c for c in cols if 'almacen' in c.lower() or 'alm' in c.lower()), None)
            almacen = str(row.get(col_alm, '')) if col_alm else ''
            
            # Texto visible en cada botón estilo filtro
            texto_boton = f"{nombre}" + (f" (Alm {almacen})" if almacen else "")
            if len(texto_boton.encode('utf-8')) > 64:
                texto_boton = texto_boton[:61] + "..."

            # Usamos el índice de la fila filtrada original
            row_idx = df_filtrado[df_filtrado[col_codigo].astype(str).str.strip() == codigo].index[0]
            cb_data = f"sapidx_{row_idx}"
            keyboard.append([InlineKeyboardButton(texto_boton, callback_data=cb_data)])

        context.user_data['df_inventario_drive'] = df_inv

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"🔍 Se encontraron *{len(df_filtrado)}* resultados para *{termino_busqueda}*:\nSelecciona una opción:",
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

    if data.startswith("sapidx_"):
        row_idx = int(data.replace("sapidx_", "").strip())
        df_inv = context.user_data.get('df_inventario_drive')

        if df_inv is None or row_idx not in df_inv.index:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Busca de nuevo con `/in [descripcion]`.", parse_mode="Markdown")
            return ConversationHandler.END

        fila = df_inv.loc[row_idx]
        
        # Muestra todas las columnas y valores de esa fila exacta seleccionada
        detalle_texto = f"🟢 *Detalle de Inventario*\n\n"
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
