import logging
import io
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from handlers import escapar_markdown

logger = logging.getLogger(__name__)

# Definición de estados del flujo de inventario
SELECCIONANDO_REF_SAP = 1
SELECCIONANDO_COLOR_SAP = 2

INVENTARIO_SHEET_URL = "https://drive.google.com/file/d/1FJdfaNhxcFFDVD_AV2lTITHw0f-mB0Y2/export?format=csv"

def _descargar_csv_inventario(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    return df

def cargar_inventario_aislado():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_descargar_csv_inventario, INVENTARIO_SHEET_URL)
        try:
            df = future.result(timeout=20.0)
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer inventario: {e}")
            return f"ERROR: {e}"

async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa la referencia a buscar.\nEjemplo: `/in AMALFI`", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    msg = await update.message.reply_text("🔄 Buscando en inventario...", parse_mode="Markdown")

    df_inv = cargar_inventario_aislado()
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

        context.user_data['df_inventario_aislado'] = df_inv
        context.user_data['termino_busqueda_inv'] = termino_busqueda
        context.user_data['col_ref_inv'] = '__ref_limpia__'
        context.user_data['col_color_inv'] = col_color

        if len(referencias_unicas) > 1:
            keyboard = []
            for ref_val in referencias_unicas[:20]:
                cb_data = f"invref_{ref_val}"
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
            return SELECCIONANDO_REF_SAP

        ref_unica = referencias_unicas[0]
        context.user_data['ref_seleccionada_inv'] = ref_unica
        return await mostrar_colores_ref(update, context, msg.message_id, edit=True)

    except Exception as e:
        logger.error(f"Error en inventario: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Error procesando datos: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_referencia_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("invref_"):
        ref_elegida = data.replace("invref_", "").strip()
        context.user_data['ref_seleccionada_inv'] = ref_elegida
        return await mostrar_colores_ref(update, context, query.message.message_id, edit=True)
    
    return SELECCIONANDO_REF_SAP

async def mostrar_colores_ref(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, edit=False) -> int:
    df_inv = context.user_data.get('df_inventario_aislado')
    ref_seleccionada = context.user_data.get('ref_seleccionada_inv')
    col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')
    col_color = context.user_data.get('col_color_inv')

    df_filtrado = df_inv[df_inv[col_ref] == ref_seleccionada]
    colores_unicos = df_filtrado[col_color].astype(str).str.strip()
    colores_unicos = colores_unicos[colores_unicos != ''].unique()

    keyboard = []
    for color_val in colores_unicos[:20]:
        cb_data = f"inv_{color_val}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
        keyboard.append([InlineKeyboardButton(color_val, callback_data=cb_data)])

    keyboard.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="volver_inv_refs")])
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
    return SELECCIONANDO_COLOR_SAP

async def seleccionar_color_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data == "volver_inv_refs":
            termino = context.user_data.get('termino_busqueda_inv')
            df_inv = context.user_data.get('df_inventario_aislado')
            col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')

            df_filtrado = df_inv[df_inv[col_ref].str.contains(termino, na=False)]
            referencias_unicas = df_filtrado[col_ref].unique()

            keyboard = []
            for ref_val in referencias_unicas[:20]:
                cb_data = f"invref_{ref_val}"
                if len(cb_data.encode('utf-8')) > 64:
                    cb_data = cb_data[:64]
                keyboard.append([InlineKeyboardButton(ref_val, callback_data=cb_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                text=f"🔍 Encontré varias referencias para *{termino}*.\nSelecciona una referencia:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return SELECCIONANDO_REF_SAP

        if data == "volver_inv_colores":
            return await mostrar_colores_ref(update, context, query.message.message_id, edit=False)

        color_elegido = data.replace("inv_", "").strip()
        df_inv = context.user_data.get('df_inventario_aislado')
        ref_seleccionada = context.user_data.get('ref_seleccionada_inv')
        col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')
        col_color = context.user_data.get('col_color_inv')

        if df_inv is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Busca de nuevo con `/in [referencia]`.", parse_mode="Markdown")
            return ConversationHandler.END

        filas_match = df_inv[
            (df_inv[col_ref] == ref_seleccionada) & 
            (df_inv[col_color].astype(str).str.strip() == color_elegido)
        ]

        if filas_match.empty:
            await query.edit_message_text(text=f"❌ No se encontró inventario para la referencia *{ref_seleccionada}* y color *{color_elegido}*.", parse_mode="Markdown")
            return ConversationHandler.END

        col_art = 'Artículo'
        col_desc = 'Descripción'
        col_cod_alm = 'Código de Almacén'
        col_nom_alm = 'Nombre de Almacén'
        col_stock = 'Stock'
        col_comp = 'Comprometido'
        col_disp = 'Disponible'

        primera_fila = filas_match.iloc[0]
        codigo_articulo = escapar_markdown(str(primera_fila.get(col_art, 'N/A')))
        descripcion_articulo = escapar_markdown(str(primera_fila.get(col_desc, f"{ref_seleccionada} - {color_elegido}")))

        mensaje = (
            f"🟢 *Inventario*\n\n"
            f"• *Artículo:* `{codigo_articulo}`\n"
            f"• *Descripción:* {descripcion_articulo}\n\n"
            f"*Desglose por Almacén:*\n"
        )

        total_stock = 0.0
        total_comprometido = 0.0
        total_disponible = 0.0

        for _, fila in filas_match.iterrows():
            cod_almacen = str(fila.get(col_cod_alm, '1')).strip()
            if not cod_almacen or cod_almacen == 'nan':
                cod_almacen = '1'

            nom_almacen = str(fila.get(col_nom_alm, '')).strip()
            if nom_almacen and nom_almacen != 'nan' and nom_almacen != '':
                almacen_txt = f"{cod_almacen} - {nom_almacen}"
            else:
                almacen_txt = cod_almacen

            try:
                raw_stock = str(fila.get(col_stock, '0')).strip().replace(',', '')
                stock = float(raw_stock) if raw_stock else 0.0
            except ValueError:
                stock = 0.0

            try:
                raw_comp = str(fila.get(col_comp, '0')).strip().replace(',', '')
                comprometido = float(raw_comp) if raw_comp else 0.0
            except ValueError:
                comprometido = 0.0

            try:
                raw_disp = str(fila.get(col_disp, '')).strip().replace(',', '')
                disponible = float(raw_disp) if raw_disp else (stock - comprometido)
            except ValueError:
                disponible = stock - comprometido

            total_stock += stock
            total_comprometido += comprometido
            total_disponible += disponible

            mensaje += (
                f"• *Almacén {almacen_txt}:*\n"
                f"  - En stock: `{stock:,.2f}`\n"
                f"  - Comprometido: `{comprometido:,.2f}`\n"
                f"  - Disponible: `{disponible:,.2f}`\n\n"
            )

        mensaje += (
            f"-----------------------------------\n"
            f"📊 *Totales Generales:*\n"
            f"• *Stock Total:* `{total_stock:,.2f}`\n"
            f"• *Total Comprometido:* `{total_comprometido:,.2f}`\n"
            f"• *Total Disponible:* `{total_disponible:,.2f}`"
        )

        keyboard = [[InlineKeyboardButton("🔙 Volver a colores", callback_data="volver_inv_colores")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if len(mensaje) > 4000:
            mensaje = mensaje[:4000]

        await query.edit_message_text(text=mensaje, reply_markup=reply_markup, parse_mode="Markdown")
        return SELECCIONANDO_COLOR_SAP

    except Exception as e:
        logger.error(f"Error seleccionando color de inventario: {e}")
        await query.edit_message_text(text=f"⚠️ Error al procesar: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END
