async def seleccionar_referencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    referencia_elegida = query.data.replace("ref_", "").strip()
    resultado = context.user_data.get('df_pedido')
    doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')

    if resultado is None:
        await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
        return ConversationHandler.END

    if referencia_elegida == "ver_todo":
        mensaje = f"🔍 *Detalle Completo del Documento {doc_buscado}* (Total ítems: {len(resultado)}):\n"

        for index, fila in resultado.iterrows():
            id_referencia = str(fila.get('Id Refer', 'N/A')).strip()
            color = str(fila.get('Color', 'N/A')).strip()
            ubicacion = str(fila.get('Ubicación del Pedido', 'N/A')).strip()
            doc_status_sap = str(fila.get('Document Status SAP', 'N/A')).strip()
            line_status_sap = str(fila.get('Line Status Sap', 'N/A')).strip()
            cantidad_pedida = str(fila.get('Cantidad Ped', 'N/A')).strip()
            cantidad_alistada = str(fila.get('Cantidad Alistada', 'N/A')).strip()
            estado_factura = str(fila.get('Estado Factura', 'N/A')).strip()
            fecha_despacho = str(fila.get('Fecha Factura', 'N/A')).strip() 
            id_operario = str(fila.get('Id Operario Asignado', 'N/A')).strip() 
            estado_pedido = str(fila.get('Clasificacion Pedido', 'N/A')).strip()

            mensaje += (
                f"\n-----------------------------------\n"
                f"• *Estado factura:* {estado_factura}\n"
                f"• *Ubicación:* {ubicacion}\n"
                f"• *Id referencia:* {id_referencia}\n"
                f"• *Color:* {color}\n"            
                f"• *Document Status SAP:* {doc_status_sap}\n"
                f"• *Line Status Sap:* {line_status_sap}\n"
                f"• *Cantidad pedida:* {cantidad_pedida}\n"
                f"• *Cantidad alistada:* {cantidad_alistada}\n"            
                f"• *Fecha Despacho:* {fecha_despacho}\n"
                f"• *Id Operario Asignado:* {id_operario}\n"
                f"• *Estado del Pedido:* {estado_pedido}\n"
            )

        if len(mensaje) > 4000:
            for i in range(0, len(mensaje), 4000):
                await query.message.reply_text(mensaje[i:i+4000], parse_mode="Markdown")
        else:
            await query.edit_message_text(text=mensaje, parse_mode="Markdown")
        
        return ConversationHandler.END

    context.user_data['ref_elegida'] = referencia_elegida
    
    # Asegurarnos de limpiar espacios en la columna de referencias para comparar bien
    resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
    df_ref = resultado[resultado['Id Refer'] == referencia_elegida]
    
    colores = df_ref['Color'].astype(str).str.strip().unique()

    keyboard = []
    for color in colores:
        # Acortamos el callback_data si es muy largo para evitar que Telegram lo bloquee
        cb_data = f"col_{color}"
        if len(cb_data.encode('utf-8')) > 64:
            cb_data = cb_data[:64]
        keyboard.append([InlineKeyboardButton(str(color), callback_data=cb_data)])

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
        color_elegido = query.data.replace("col_", "").strip()
        doc_buscado = context.user_data.get('doc_buscado', 'Desconocido')
        ref_elegida = context.user_data.get('ref_elegida')
        resultado = context.user_data.get('df_pedido')

        if resultado is None or ref_elegida is None:
            await query.edit_message_text(text="⚠️ La sesión ha expirado o se reinició. Por favor, realiza la búsqueda de nuevo con `/PV [número]`.", parse_mode="Markdown")
            return ConversationHandler.END

        # Limpiar espacios en ambas columnas para garantizar la coincidencia exacta
        resultado['Id Refer'] = resultado['Id Refer'].astype(str).str.strip()
        resultado['Color'] = resultado['Color'].astype(str).str.strip()

        fila_match = resultado[(resultado['Id Refer'] == ref_elegida) & (resultado['Color'] == color_elegido)]

        if fila_match.empty:
            await query.edit_message_text(text=f"❌ No se encontró información para la referencia *{ref_elegida}* y color *{color_elegido}*.", parse_mode="Markdown")
            return ConversationHandler.END

        fila = fila_match.iloc[0]

        id_referencia = str(fila.get('Id Refer', 'N/A')).strip()
        color = str(fila.get('Color', 'N/A')).strip()
        ubicacion = str(fila.get('Ubicación del Pedido', 'N/A')).strip()
        doc_status_sap = str(fila.get('Document Status SAP', 'N/A')).strip()
        line_status_sap = str(fila.get('Line Status Sap', 'N/A')).strip()
        cantidad_pedida = str(fila.get('Cantidad Ped', 'N/A')).strip()
        cantidad_alistada = str(fila.get('Cantidad Alistada', 'N/A')).strip()
        estado_factura = str(fila.get('Estado Factura', 'N/A')).strip()
        fecha_despacho = str(fila.get('Fecha Factura', 'N/A')).strip() 
        id_operario = str(fila.get('Id Operario Asignado', 'N/A')).strip() 
        estado_pedido = str(fila.get('Clasificacion Pedido', 'N/A')).strip()

        mensaje = (
            f"🔍 *Detalle del Documento {doc_buscado}*:\n\n"
            f"• *Estado factura:* {estado_factura}\n"
            f"• *Ubicación:* {ubicacion}\n"
            f"• *Id referencia:* {id_referencia}\n"
            f"• *Color:* {color}\n"            
            f"• *Document Status SAP:* {doc_status_sap}\n"
            f"• *Line Status Sap:* {line_status_sap}\n"
            f"• *Cantidad pedida:* {cantidad_pedida}\n"
            f"• *Cantidad alistada:* {cantidad_alistada}\n"            
            f"• *Fecha Despacho:* {fecha_despacho}\n"
            f"• *Id Operario Asignado:* {id_operario}\n"
            f"• *Estado del Pedido:* {estado_pedido}"
        )

        await query.edit_message_text(text=mensaje, parse_mode="Markdown")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error en seleccionar_color: {e}")
        await query.edit_message_text(text=f"⚠️ Ocurrió un error al procesar el color: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END
