import logging
import io
import requests
import pandas as pd
import concurrent.futures
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from handlers import escapar_markdown

logger = logging.getLogger(__name__)

SELECCIONANDO_REF_SAP = 2

INVENTARIO_SHEET_URL = "https://docs.google.com/spreadsheets/d/1SoK0_f6YFB36cAzjG8UOvFqELx3qwtgU/export?format=csv"

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
            logger.info(f"✅ ¡Inventario aislado leído con éxito! ({len(df)} filas)")
            return df
        except Exception as e:
            logger.error(f"⚠️ Error al leer inventario: {e}")
            return f"ERROR: {e}"

async def iniciar_busqueda_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.args:
        await update.message.reply_text(
            "⚠️ Por favor, ingresa la referencia a buscar.\nEjemplo: `/in CAMILA`", 
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    termino_busqueda = " ".join(context.args).strip().upper()
    msg = await update.message.reply_text("🔄 Buscando referencia en inventario...", parse_mode="Markdown")

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
        
        col_ref = 'REFERENCIA' if 'REFERENCIA' in df_inv.columns else next((c for c in df_inv.columns if 'REF' in c.upper()), None)
        col_color = 'COLOR' if 'COLOR' in df_inv.columns else next((c for c in df_inv.columns if 'COLOR' in c.upper()), None)

        if not col_ref or not col_color:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text="⚠️ No se encontraron las columnas REFERENCIA o COLOR en el inventario.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        df_inv['__ref_limpia__'] = df_inv[col_ref].astype(str).str.strip().str.upper()
        df_filtrado = df_inv[df_inv['__ref_limpia__'] == termino_busqueda]

        if df_filtrado.empty:
            df_filtrado = df_inv[df_inv['__ref_limpia__'].str.contains(termino_busqueda, na=False)]

        if df_filtrado.empty:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                text=f"❌ No se encontraron referencias para: *{termino_busqueda}*.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        colores_unicos = df_filtrado[[col_color]].drop_duplicates().head(20).values

        keyboard = []
        for row_c in colores_unicos:
            color_val = str(row_c[0]).strip()
            if not color_val:
                continue
            cb_data = f"inv_{color_val}"
            if len(cb_data.encode('utf-8')) > 64:
                cb_data = cb_data[:64]
            keyboard.append([InlineKeyboardButton(color_val, callback_data=cb_data)])

        context.user_data['df_inventario_aislado'] = df_inv
        context.user_data['ref_buscada_inv'] = termino_busqueda
        context.user_data['col_ref_inv'] = '__ref_limpia__'
        context.user_data['col_color_inv'] = col_color

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"🔍 Referencia: *{termino_busqueda}*.\nSelecciona un color:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return SELECCIONANDO_REF_SAP

    except Exception as e:
        logger.error(f"Error en inventario: {e}")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Error procesando datos: `{e}`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

async def seleccionar_color_sap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data == "volver_inv_colores":
            ref_buscada = context.user_data.get('ref_buscada_inv')
            df_inv = context.user_data.get('df_inventario_aislado')
            col_color = context.user_data.get('col_color_inv')
            col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')
            
            if df_inv is None:
                await query.edit_message_text(text="⚠️ La sesión ha expirado.", parse_mode="Markdown")
                return ConversationHandler.END

            df_filtrado = df_inv[df_inv[col_ref] == ref_buscada]
            colores_unicos = df_filtrado[[col_color]].drop_duplicates().head(20).values

            keyboard = []
            for row_c in colores_unicos:
                color_val = str(row_c[0]).strip()
                if not color_val:
                    continue
                cb_data = f"inv_{color_val}"
                if len(cb_data.encode('utf-8')) > 64:
                    cb_data = cb_data[:64]
                keyboard.append([InlineKeyboardButton(color_val, callback_data=cb_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                text=f"🔍 Referencia: *{ref_buscada}*.\nSelecciona un color:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            return SELECCIONANDO_REF_SAP

        color_elegido = data.replace("inv_", "").strip().upper()
        df_inv = context.user_data.get('df_inventario_aislado')
        ref_buscada = context.user_data.get('ref_buscada_inv')
        col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')
        col_color = context.user_data.get('col_color_inv')

        if df_inv is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado. Busca de nuevo con `/in [referencia]`.", parse_mode="Markdown")
            return ConversationHandler.END

        df_inv['__color_limpio__'] = df_inv[col_color].astype(str).str.strip().str.upper()

        filas_match = df_inv[
            (df_inv[col_ref] == ref_buscada) & 
            (df_inv['__color_limpio__'] == color_elegido)
        ]

        if filas_match.empty:
            await query.edit_message_text(text=f"❌ No se encontró inventario para el color *{color_elegido}*.", parse_mode="Markdown")
            return ConversationHandler.END

        col_cod_alm = 'Código de Almacén' if 'Código de Almacén' in df_inv.columns else 'Codigo de Almacen'
        col_nom_alm = 'Nombre de Almacén' if 'Nombre de Almacén' in df_inv.columns else 'Nombre de Almacen'
        col_stock = 'Stock'
        col_comp = 'Comprometido'
        col_disp = 'Disponible'
        col_art = 'Artículo' if 'Artículo' in df_inv.columns else 'Articulo'
        col_desc = 'Descripción' if 'Descripción' in df_inv.columns else 'Descripcion'

        # LIMPIEZA RIGUROSA DE DUPLICADOS (Evita filas idénticas o almacenes repetidos vacíos)
        subset_cols = [c for c in [col_cod_alm, col_stock, col_comp, col_disp] if c in df_inv.columns]
        if subset_cols:
            filas_match = filas_match.drop_duplicates(subset=subset_cols)

        primera_fila = filas_match.iloc[0]
        codigo_articulo = escapar_markdown(str(primera_fila.get(col_art, 'N/A')))
        descripcion_articulo = escapar_markdown(str(primera_fila.get(col_desc, f"{ref_buscada} - {color_elegido}")))

        mensaje = (
            f"🟢 *Inventario SAP en Tiempo Real*\n\n"
            f"• *Código:* `{codigo_articulo}`\n"
            f"• *Artículo:* {descripcion_articulo}\n\n"
            f"*Desglose por Almacén:*\n"
        )

        total_stock = 0.0
        total_comprometido = 0.0
        total_disponible = 0.0

        for _, fila in filas_match.iterrows():
            cod_almacen = str(fila.get(col_cod_alm, 'Principal')).strip() if col_cod_alm in df_inv.columns else 'Principal'
            if not cod_almacen or cod_almacen == 'nan':
                cod_almacen = 'Principal'

            nom_almacen = str(fila.get(col_nom_alm, '')).strip() if col_nom_alm in df_inv.columns else ''

            if nom_almacen and nom_almacen != 'nan' and nom_almacen != '':
                almacen_txt = f"{cod_almacen} - {nom_almacen}"
            else:
                almacen_txt = cod_almacen

            try:
                stock = float(str(fila.get(col_stock, '0')).replace(',', '').strip() or '0')
            except ValueError:
                stock = 0.0

            try:
                comprometido = float(str(fila.get(col_comp, '0')).replace(',', '').strip() or '0')
            except ValueError:
                comprometido = 0.0

            try:
                disponible = float(str(fila.get(col_disp, str(stock - comprometido))).replace(',', '').strip() or str(stock - comprometido))
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
        return SELECCIONANDO_REF_SAP

    except Exception as e:
        logger.error(f"Error seleccionando color de inventario: {e}")
        await query.edit_message_text(text=f"⚠️ Error al procesar: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END
