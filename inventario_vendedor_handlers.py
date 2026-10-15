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

# Enlace de tu nuevo libro/hoja de inventario (ajusta la URL si es necesario)
INVENTARIO_VENDEDOR_SHEET_URL = "https://docs.google.com/spreadsheets/d/1FJdfaNhxcFFDVD_AV2lTITHw0f-mB0Y2/export?format=csv"

def _descargar_csv_inventario_vendedor(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df

def cargar_inventario_vendedor_aislado():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv_inventario_vendedor, INVENTARIO_VENDEDOR_SHEET_URL)
        try:
            df = future.result(timeout=20.0)
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer inventario de vendedor: {e}")
            return f"ERROR: {e}"

async def volver_a_lista_referencias_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, query) -> int:
    termino = context.user_data.get('termino_busqueda_vend', '')
    df_inv = context.user_data.get('df_inventario_vendedor_aislado')
    col_ref = context.user_data.get('col_ref_vend', '__ref_limpia__')

    if df_inv is not None and termino:
        df_filtrado = df_inv[df_inv[col_ref].str.contains(termino, na=False)]
        referencias_unicas = df_filtrado[col_ref].unique()

        keyboard = []
        for ref_val in referencias_unicas[:20]:
            cb_data = f"vendref_{ref_val}"
            if len(cb_data.encode('utf-8')) > 64:
                cb_data = cb_data[:64]
            keyboard.append([InlineKeyboardButton(ref_val, callback_data=cb_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=f"🔍 Encontré varias referencias para *{termino}*.\nSelecciona una referencia:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    return SELECCIONANDO_REF_VEND

async def iniciar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    
    # Si viene del botón del menú principal
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_INVENTARIO_VENDEDOR")
        await query.edit_message_text(
            text="🛒 *Inventario Vendedor (Bodega 01)*\n\nPor favor, busca una referencia usando el comando, por ejemplo: `/i VERMONT`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # 📊 Registramos automáticamente el uso de /i en la auditoría
    registrar_uso_bot(update, "/i")

    # 🗑️ Programamos el borrado del comando del usuario (/i VERMONT) a las 24 h
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update, context,
            "⚠️ Por favor, ingresa la referencia a buscar.\nEjemplo: `/i VERMONT`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()

    # Este mensaje se edita después (mismo message_id), así que se programa una sola vez
    msg = await responder(update, context, "🔄 Buscando en inventario...", parse_mode="Markdown")

    df_inv = cargar_inventario_vendedor_aislado()
    if isinstance(df_inv, str):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ No se pudo acceder al inventario: `{df_inv}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    try:
        df_inv.columns = [str(c).strip() for c in df_inv.columns]
        
        col_ref = next((c for c in df_inv.columns if 'REF' in c.upper()), None)
        col_color = next((c for c in df_inv.columns if 'COLOR' in c.upper()), None)

        if not col_ref or not col_color:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="⚠️ No se encontraron las columnas REFERENCIA o COLOR en el inventario.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        df_inv['__ref_limpia__'] = df_inv[col_ref].astype(str).str.strip().str.upper()
        df_filtrado = df_inv[df_inv['__ref_limpia__'].str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron referencias para: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        referencias_unicas = df_filtrado['__ref_limpia__'].unique()

        context.user_data['df_inventario_vendedor_aislado'] = df_inv
        context.user_data['termino_busqueda_vend'] = termino_busqueda
        context.user_data['col_ref_vend'] = '__ref_limpia__'
        context.user_data['col_color_vend'] = col_color

        if len(referencias_unicas) > 1:
            keyboard = []
            for ref_val in referencias_unicas[:20]:
                cb_data = f"vendref_{ref_val}"
                if len(cb_data.encode('utf-8')) > 64:
                    cb_data = cb_data[:64]
                keyboard.append([InlineKeyboardButton(ref_val, callback_data=cb_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"🔍 Encontré varias referencias para *{termino_busqueda}*.\nSelecciona una referencia:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return SELECCIONANDO_REF_VEND

        ref_unica = referencias_unicas[0]
        context.user_data['ref_seleccionada_vend'] = ref_unica
        return await mostrar_colores_ref_vendedor(update, context, msg.message_id, edit=True)

    except Exception as e:
        logger.error(f"Error en inventario vendedor: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Error procesando datos: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_referencia_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "volver_vend_refs":
        return await volver_a_lista_referencias_vendedor(update, context, query)

    if data.startswith("vendref_"):
        ref_elegida = data.replace("vendref_", "").strip()
        context.user_data['ref_seleccionada_vend'] = ref_elegida
        return await mostrar_colores_ref_vendedor(update, context, query.message.message_id, edit=True)
    
    return SELECCIONANDO_REF_VEND

async def mostrar_colores_ref_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, edit=False) -> int:
    df_inv = context.user_data.get('df_inventario_vendedor_aislado')
    ref_seleccionada = context.user_data.get('ref_seleccionada_vend')
    col_ref = context.user_data.get('col_ref_vend', '__ref_limpia__')
    col_color = context.user_data.get('col_color_vend')

    df_filtrado = df_inv[df_inv[col_ref] == ref_seleccionada]
    
    colores_unicos = df_filtrado[col_color].astype(str).str.strip()
    colores_unicos = colores_unicos[colores_unicos != ''].unique()

    col_stock = 'Stock'
    col_comp = 'Comprometido'
    col_disp = 'Disponible'

    keyboard = []
    for color_val in colores_unicos[:20]:
        df_color = df_filtrado[df_filtrado[col_color].astype(str).str.strip() == color_val]
        
        disponible_bodega_01 = 0.0

        for _, fila in df_color.iterrows():
            cod_almacen = '1'
            for c in df_inv.columns:
                if 'CÓDIGO' in c.upper() or 'ALMACÉN' in c.upper() or 'ALMACEN' in c.upper():
                    val = str(fila.get(c, '')).strip()
                    if val and val != 'nan':
                        cod_almacen = val
                        break
            
            if cod_almacen in ['1', '01']:
                try:
                    raw_stock = str(fila.get(col_stock, '0')).strip().replace(',', '.')
                    stock = float(raw_stock) if raw_stock else 0.0
                except ValueError:
                    stock = 0.0

                try:
                    raw_comp = str(fila.get(col_comp, '0')).strip().replace(',', '.')
                    comprometido = float(raw_comp) if raw_comp else 0.0
                except ValueError:
                    comprometido = 0.0

                try:
                    raw_disp = str(fila.get(col_disp, '')).strip().replace(',', '.')
                    disponible = float(raw_disp) if raw_disp else (stock - comprometido)
                except ValueError:
                    disponible = stock - comprometido

                disponible_bodega_01 += disponible

        if disponible_bodega_01 < 0:
            disponible_bodega_01 = 0.0

        disp_str = f"{int(disponible_bodega_01)}" if disponible_bodega_01.is_integer() else f"{disponible_bodega_01:,.2f}"
        texto_boton = f"{color_val} - {disp_str} MTRS"
        
        cb_data = f"vend_{color_val}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
            
        keyboard.append([InlineKeyboardButton(texto_boton, callback_data=cb_data)])

    keyboard.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="volver_vend_refs")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    texto_msg = f"🔍 Referencia: *{ref_seleccionada}*.\nSelecciona un color:"

    if edit:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text=texto_msg,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.callback_query.edit_message_text(
            text=texto_msg,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    return SELECCIONANDO_COLOR_VEND

async def seleccionar_color_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "volver_vend_refs":
        return await volver_a_lista_referencias_vendedor(update, context, query)

    await query.answer(text="La disponibilidad de bodega 01 se muestra directamente en el botón.", show_alert=False)
    return SELECCIONANDO_COLOR_VEND
