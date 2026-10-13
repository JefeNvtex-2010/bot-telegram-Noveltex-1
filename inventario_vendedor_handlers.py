async def mostrar_colores_ref(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int, edit=False) -> int:
    df_inv = context.user_data.get('df_inventario_aislado')
    ref_seleccionada = context.user_data.get('ref_seleccionada_inv')
    col_ref = context.user_data.get('col_ref_inv', '__ref_limpia__')
    col_color = context.user_data.get('col_color_inv')

    # Filtrar por la referencia seleccionada
    df_filtrado = df_inv[df_inv[col_ref] == ref_seleccionada]
    
    colores_unicos = df_filtrado[col_color].astype(str).str.strip()
    colores_unicos = colores_unicos[colores_unicos != ''].unique()

    # Columnas necesarias para calcular el disponible de la bodega 01
    col_stock = 'Stock'
    col_comp = 'Comprometido'
    col_disp = 'Disponible'

    keyboard = []
    for color_val in colores_unicos[:20]:
        # Filtrar las filas que corresponden a esta referencia y este color
        df_color = df_filtrado[df_filtrado[col_color].astype(str).str.strip() == color_val]
        
        disponible_bodega_01 = 0.0

        for _, fila in df_color.iterrows():
            # Identificar si la fila pertenece a la bodega 01
            cod_almacen = '1'
            for c in df_inv.columns:
                if 'CÓDIGO' in c.upper() or 'ALMACÉN' in c.upper() or 'ALMACEN' in c.upper():
                    val = str(fila.get(c, '')).strip()
                    if val and val != 'nan':
                        cod_almacen = val
                        break
            
            # Verificamos si es la bodega '01' o '1' (puedes ajustar según cómo venga en tu CSV, ej. '01' o '1')
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

        # Texto del botón mostrando el color y su disponible en bodega 01 (ej. "10 OPTICAL WHITE - 5000")
        texto_boton = f"{color_val} - {int(disponible_bodega_01) if disponible_bodega_01.is_integer() else disponible_bodega_01}"
        
        # Como ya no se requiere "entrar" al color para ver el stock, el callback data puede mantener
        # una acción informativa o simplemente un callback que indique la selección si lo requieres luego, 
        # o puedes deshabilitarlo/ajustarlo según tu flujo. Aquí se mantiene el patrón de callback.
        cb_data = f"inv_{color_val}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
            
        keyboard.append([InlineKeyboardButton(texto_boton, callback_data=cb_data)])

    # Únicamente queda habilitado el botón de volver a referencias
    keyboard.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="volver_inv_refs")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    texto_msg = f"🔍 Referencia: *{ref_seleccionada}*.\nSelecciona un color (Disponible Bodega 01):"

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
