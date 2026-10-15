¡Claro que sí! Para que al presionar el botón el bot no te obligue a escribir el comando con barra (/in, /i o /PV), sino que directamente te pida el dato en el chat y al escribirlo te arroje el resultado, vamos a usar estados intermedios en los ConversationHandler de cada archivo.

Aquí tienes los códigos completos y ajustados para tus tres archivos (handlers.py, inventario_handlers.py e inventario_vendedor_handlers.py) junto con cómo debes actualizar los entry points en tu main.py.

1. Actualización en handlers.py (Pedidos - /PV)
Agregamos un estado nuevo llamado ESPERANDO_DOCUMENTO_PV para que capture el texto directamente cuando pulses el botón.

Python
import os
import logging
import io
import datetime
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# Estados para la conversación interactiva de búsqueda de pedidos (/PV)
SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR, ESPERANDO_DOCUMENTO_PV = range(3)

GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv"

URL_WEB_APP_REPORTE = os.environ.get(
    "URL_WEB_APP_REPORTE",
    "https://script.google.com/macros/s/AKfycbztHKV4C8hqcPUVHq71gFmRzPSbtz36NByAgPfR-JEyDlNRF-LkRj-g2lEWHrf42AaE/exec"
)

MI_TELEGRAM_ID = int(os.environ.get("MI_TELEGRAM_ID", "5655537446"))
RENDER_DEPLOY_HOOK_URL = os.environ.get("RENDER_DEPLOY_HOOK_URL", "")
SEGUNDOS_BORRADO = 24 * 60 * 60

async def _borrar_mensaje_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    datos = context.job.data
    try:
        await context.bot.delete_message(chat_id=datos["chat_id"], message_id=datos["message_id"])
    except Exception as e:
        logger.warning(f"No se pudo borrar el mensaje {datos['message_id']}: {e}")

def programar_borrado(context: ContextTypes.DEFAULT_TYPE, mensaje) -> None:
    if mensaje is None:
        return
    if context.job_queue is None:
        return
    context.job_queue.run_once(
        _borrar_mensaje_job,
        when=SEGUNDOS_BORRADO,
        data={"chat_id": mensaje.chat_id, "message_id": mensaje.message_id},
    )

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, **kwargs):
    msg = await update.message.reply_text(texto, **kwargs)
    programar_borrado(context, msg)
    return msg

def registrar_uso_bot(update: Update, comando_usado: str):
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    nombre = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "Sin alias"
    zona_horaria = datetime.timezone(datetime.timedelta(hours=-5))
    hora_actual = datetime.datetime.now(zona_horaria).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"📊 [AUDITORÍA] {nombre} ({username}) [ID: {user_id}] -> {comando_usado} a las {hora_actual}")
    if not URL_WEB_APP_REPORTE:
        return
    try:
        payload = {"fecha": hora_actual, "usuario": nombre, "username": username, "id": str(user_id), "comando": comando_usado}
        requests.post(URL_WEB_APP_REPORTE, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Error auditoría: {e}")

def escapar_markdown(texto: str) -> str:
    if not isinstance(texto, str):
        texto = str(texto)
    for c in ['_', '*', '`', '[']:
        texto = texto.replace(c, '')
    return texto.strip()

def _descargar_csv(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df

def cargar_catalogo():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv, GOOGLE_SHEET_URL)
        try:
            df = future.result(timeout=20.0)
            if 'Documento Pd' in df.columns:
                df['Documento Pd'] = df['Documento Pd'].astype(str).str.split('.').str[0].str.strip()
            return df
        except Exception as e:
            return f"ERROR: {e}"

# MENÚ PRINCIPAL
async def menu_inicio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        programar_borrado(context, update.message)
        
    keyboard = [
        [InlineKeyboardButton("🛒 Inventario Vendedor", callback_data="start_vend")],
        [InlineKeyboardButton("📦 Inventario SAP", callback_data="start_sap")],
        [InlineKeyboardButton("📋 Pedidos (PV)", callback_data="start_pv")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    texto_bienvenida = "👋 ¡Hola! Selecciona una opción del menú principal para comenzar:"
    
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text=texto_bienvenida, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        msg = await update.message.reply_text(texto_bienvenida, reply_markup=reply_markup, parse_mode="Markdown")
        programar_borrado(context, msg)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    user = update.effective_user
    name = user.first_name if user else "allí"
    await responder(update, context, f"¡Hola {name}! Escribe /inicio para ver el menú principal con botones.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(update, context, "Usa /inicio para desplegar el menú de opciones.")

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    await responder(update, context, "📦 Escribe el nombre del producto:", parse_mode="Markdown")

async def reiniciar_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ Sin permisos.")
        return
    requests.post(RENDER_DEPLOY_HOOK_URL, timeout=10)
    await responder(update, context, "🔄 Reiniciando Render...")

async def limpiar_cache_y_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    programar_borrado(context, update.message)
    if update.effective_user.id != MI_TELEGRAM_ID:
        await responder(update, context, "⛔ Sin permisos.")
        return
    requests.post(f"{RENDER_DEPLOY_HOOK_URL}&clearCache=true", timeout=10)
    await responder(update, context, "🧹 Limpiando caché y desplegando...")

# FLUJO PEDIDOS
def _texto_detalle(fila, titulo=None) -> str:
    cabecera = f"{titulo}\n\n" if titulo else ""
    tipo_pedido = str(fila.get('Clasificacion Pedido', '')).strip().lower()
    campos_contado = ""
    if tipo_pedido == 'contado':
        campos_contado += "-----------------------------------\n"
        campos_contado += f"• *Fecha Vencimiento:* {escapar_markdown(fila.get('Fecha Vencimiento', 'N/A'))}\n"
    return (
        f"{cabecera}"
        f"• *Estado del Pedido:* {escapar_markdown(fila.get('Estado Factura', 'N/A'))}\n"
        f"• *Ubicación:* {escapar_markdown(fila.get('Ubicación del Pedido', 'N/A'))}\n"
        f"• *Referencia:* {escapar_markdown(fila.get('Id Refer', 'N/A'))}\n"
        f"• *Color:* {escapar_markdown(fila.get('Color', 'N/A'))}\n"
        f"• *Cantidad pedida:* {escapar_markdown(fila.get('Cantidad Ped', 'N/A'))}\n"
        f"• *Cantidad alistada:* {escapar_markdown(fila.get('Cantidad Alistada', 'N/A'))}\n"
        f"{campos_contado}"
    )

def _traducir_estado(valor) -> str:
    return {"bost_Open": "Abierto", "bost_Close": "Cerrado"}.get(str(valor).strip(), str(valor).strip() or "N/A")

def _construir_teclado_colores(df_ref) -> InlineKeyboardMarkup:
    df = df_ref.copy()
    df['Color'] = df['Color'].astype(str).str.strip()
    keyboard = [[InlineKeyboardButton("📄 Ver todo", callback_data="ref_ver_todo")]]
    for color in df['Color'].unique():
        estados = df.loc[df['Color'] == color, 'Line Status Sap'].map(_traducir_estado).unique()
        keyboard.append([InlineKeyboardButton(f"{color} - {' / '.join(estados)}", callback_data=f"col_{color}")])
    keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="volver_referencias")])
    return InlineKeyboardMarkup(keyboard)

def _texto_encabezado(resultado, doc_buscado) -> str:
    primera = resultado.iloc[0]
    return f"🔍 Documento *{doc_buscado}*\n• *Vendedor:* {escapar_markdown(primera.get('Nombre Vendedor', 'N/A'))}\n• *Cliente:* {escapar_markdown(primera.get('Nombre Tercero', 'N/A'))}\n\nSelecciona una referencia:"

async def iniciar_busqueda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_PV")
        await query.edit_message_text(
            text="📋 *Pedidos (PV)*\n\nPor favor, escribe el **número de documento** que deseas buscar:",
            parse_mode="Markdown"
        )
        return ESPERANDO_DOCUMENTO_PV

    registrar_uso_bot(update, "/PV")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(update, context, "⚠️ Ingresa el número de documento. Ejemplo: `/PV 5270`", parse_mode="Markdown")
        return ConversationHandler.END

    doc_buscado = context.args[0].strip()
    return await ejecutar_busqueda_documento(update, context, doc_buscado)

async def recibir_texto_documento_pv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    doc_buscado = update.message.text.strip()
    return await ejecutar_busqueda_documento(update, context, doc_buscado)

async def ejecutar_busqueda_documento(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_buscado: str) -> int:
    msg_buscando = await update.message.reply_text("🔍 Buscando pedido...")
    programar_borrado(context, msg_buscando)

    catalogo_df = cargar_catalogo()
    if isinstance(catalogo_df, str) or catalogo_df is None or catalogo_df.empty:
        await responder(update, context, "⚠️ Error al conectar con Google Sheets o catálogo vacío.")
        return ConversationHandler.END

    if 'Documento Pd' not in catalogo_df.columns:
        await responder(update, context, "⚠️ No se encontró la columna 'Documento Pd'.")
        return ConversationHandler.END

    resultado = catalogo_df[catalogo_df['Documento Pd'].astype(str).str.strip() == doc_buscado]
    if resultado.empty:
        await responder(update, context, f"❌ No se encontró el documento: *{doc_buscado}*.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data['df_pedido'] = resultado
    context.user_data['doc_buscado'] = doc_buscado

    referencias = resultado['Id Refer'].unique()
    keyboard = [[InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")] for ref in referencias]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await responder(update, context, _texto_encabezado(resultado, doc_buscado), reply_markup=reply_markup, parse_mode="Markdown")
    return SELECCIONANDO_REFERENCIA

async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data_callback = query.data

    if data_callback == "volver_referencias":
        resultado = context.user_data.get('df_pedido')
        doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
        referencias = resultado['Id Refer'].unique()
        keyboard = [[InlineKeyboardButton(str(ref), callback_data=f"ref_{ref}")] for ref in referencias]
        await query.edit_message_text(text=_texto_encabezado(resultado, doc_buscado), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return SELECCIONANDO_REFERENCIA

    referencia_elegida = data_callback.replace("ref_", "").strip()
    resultado = context.user_data.get('df_pedido')
    context.user_data['ref_elegida'] = referencia_elegida
    df_ref = resultado[resultado['Id Refer'].astype(str).str.strip() == referencia_elegida]

    await query.edit_message_text(text=f"Referencia seleccionada: *{referencia_elegida}*.\nSelecciona el color:", reply_markup=_construir_teclado_colores(df_ref), parse_mode="Markdown")
    return SELECCIONANDO_COLOR

async def seleccionar_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data_callback = query.data

    if data_callback == "volver_colores":
        ref_elegida = context.user_data.get('ref_elegida')
        resultado = context.user_data.get('df_pedido')
        df_ref = resultado[resultado['Id Refer'].astype(str).str.strip() == ref_elegida]
        await query.edit_message_text(text=f"Referencia seleccionada: *{ref_elegida}*.", reply_markup=_construir_teclado_colores(df_ref), parse_mode="Markdown")
        return SELECCIONANDO_COLOR

    color_elegido = data_callback.replace("col_", "").strip()
    resultado = context.user_data.get('df_pedido')
    ref_elegida = context.user_data.get('ref_elegida')
    
    fila_match = resultado[(resultado['Id Refer'].astype(str).str.strip() == ref_elegida) & (resultado['Color'].astype(str).str.strip() == color_elegido)]
    mensaje = f"🔍 *Detalle* (Color {color_elegido}):\n"
    for _, fila in fila_match.iterrows():
        mensaje += "\n-----------------------------------\n" + _texto_detalle(fila)

    await query.edit_message_text(text=mensaje, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver_colores")]]), parse_mode="Markdown")
    return SELECCIONANDO_COLOR

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    await responder(update, context, "❌ Búsqueda cancelada.")
    return ConversationHandler.END
