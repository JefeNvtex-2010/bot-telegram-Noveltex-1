import logging
import io
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from handlers import escapar_markdown, registrar_uso_bot, programar_borrado, responder

logger = logging.getLogger(__name__)

SELECCIONANDO_REF_VEND = 1
SELECCIONANDO_COLOR_VEND = 2
ESPERANDO_REF_VEND = 3

INVENTARIO_VENDEDOR_SHEET_URL = "https://docs.google.com/spreadsheets/d/1FJdfaNhxcFFDVD_AV2lTITHw0f-mB0Y2/export?format=csv"

def _descargar_csv_vendedor(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df

def cargar_inventario_vendedor_aislado():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv_vendedor, INVENTARIO_VENDEDOR_SHEET_URL)
        try:
            return future.result(timeout=20.0)
        except Exception as e:
            return f"ERROR: {e}"

async def iniciar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_INVENTARIO_VENDEDOR")
        await query.edit_message_text(
            text="🛒 *Inventario Vendedor (Bodega 01)*\n\nPor favor, escribe la **referencia** que deseas buscar:",
            parse_mode="Markdown"
        )
        return ESPERANDO_REF_VEND

    registrar_uso_bot(update, "/i")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(update, context, "⚠️ Ingresa la referencia. Ejemplo: `/i VERMONT`", parse_mode="Markdown")
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    return await ejecutar_busqueda_vendedor(update, context, termino_busqueda)

async def recibir_texto_ref_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    termino_busqueda = update.message.text.strip().upper()
    return await ejecutar_busqueda_vendedor(update, context, termino_busqueda)

async def ejecutar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, termino_busqueda: str) -> int:
    msg = await update.message.reply_text("🔄 Buscando inventario vendedor...", parse_mode="Markdown")
    programar_borrado(context, msg)

    df_inv = cargar_inventario_vendedor_aislado()
    if isinstance(df_inv, str):
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"⚠️ Error: `{df_inv}`", parse_mode="Markdown")
        return ConversationHandler.END

    df_inv.columns = [str(c).strip() for c in df_inv.columns]
    col_ref = next((c for c in df_inv.columns if 'REF' in c.upper()), None)
    col_color = next((c for c in df_inv.columns if 'COLOR' in c.upper()), None)

    if not col_ref or not col_color:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text="⚠️ Columnas no encontradas.", parse_mode="Markdown")
        return ConversationHandler.END

    df_inv['__ref_limpia__'] = df_inv[col_ref].astype(str).str.strip().str.upper()
    df_filtrado = df_inv[df_inv['__ref_limpia__'].str.contains(termino_busqueda, na=False)]

    if df_filtrado.empty:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"❌ No se encontró: *{termino_busqueda}*.", parse_mode="Markdown")
        return ConversationHandler.END

    referencias_unicas = df_filtrado['__ref_limpia__'].unique()
    context.user_data['df_inventario_vendedor_aislado'] = df_inv
    context.user_data['col_ref_vend'] = '__ref_limpia__'
    context.user_data['col_color_vend'] = col_color

    if len(referencias_unicas) > 1:
        keyboard = [[InlineKeyboardButton(r, callback_data=f"vendref_{r}")] for r in referencias_unicas[:20]]
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=f"🔍 Varias referencias para *{termino_busqueda}*:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return SELECCIONANDO_REF_VEND

    context.user_data['ref_seleccionada_vend'] = referencias_unicas[0]
    return await mostrar_colores_ref_vendedor(update, context, msg.message_id, edit=True)

async def seleccionar_referencia_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("vendref_"):
        context.user_data['ref_seleccionada_vend'] = query.data.replace("vendref_", "").strip()
        return await mostrar_colores_ref_vendedor(update, context, query.message.message_id, edit=True)
    return SELECCIONANDO_REF_VEND

async def mostrar_colores_ref_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, edit=False) -> int:
    df_inv = context.user_data.get('df_inventario_vendedor_aislado')
    ref_sel = context.user_data.get('ref_seleccionada_vend')
    col_color = context.user_data.get('col_color_vend')
    colores = df_inv[df_inv['__ref_limpia__'] == ref_sel][col_color].astype(str).str.strip().unique()

    keyboard = [[InlineKeyboardButton(c, callback_data=f"vend_{c}")] for c in colores[:20]]
    text = f"🔍 Referencia: *{ref_sel}*.\nSelecciona un color:"

    if edit:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=message_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return SELECCIONANDO_COLOR_VEND

async def seleccionar_color_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="🛒 Has seleccionado el color del inventario de vendedor.", parse_mode="Markdown")
    return SELECCIONANDO_COLOR_VEND
