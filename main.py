import logging
import threading
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
from config import load_settings
from logging_config import configure_logging

from handlers import (
    start, help_command, order_command, 
    iniciar_busqueda, seleccionar_referencia, seleccionar_color, 
    cancelar, reiniciar_render, limpiar_cache_y_deploy,
    menu_inicio, recibir_texto_documento_pv,
    SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR, ESPERANDO_DOCUMENTO_PV
)

from inventario_handlers import (
    iniciar_busqueda_sap, seleccionar_referencia_sap, seleccionar_color_sap,
    recibir_texto_ref_sap,
    SELECCIONANDO_REF_SAP, SELECCIONANDO_COLOR_SAP, ESPERANDO_REF_SAP
)

from inventario_vendedor_handlers import (
    iniciar_busqueda_vendedor, seleccionar_referencia_vendedor, seleccionar_color_vendedor,
    recibir_texto_ref_vendedor,
    SELECCIONANDO_REF_VEND, SELECCIONANDO_COLOR_VEND, ESPERANDO_REF_VEND
)

logger = logging.getLogger(__name__)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

def main() -> None:
    settings = load_settings()
    configure_logging(settings.telegram_bot_token)
    
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()
    
    logger.info("Iniciando el bot de Telegram...")
    
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    
    # 1. Manejador para Pedidos (/PV o botón start_pv)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("PV", iniciar_busqueda),
            CommandHandler("pv", iniciar_busqueda),
            CallbackQueryHandler(iniciar_busqueda, pattern="^start_pv$")
        ],
        states={
            ESPERANDO_DOCUMENTO_PV: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_documento_pv)
            ],
            SELECCIONANDO_REFERENCIA: [
                CallbackQueryHandler(seleccionar_referencia, pattern="^(ref_|ver_todo)")
            ],
            SELECCIONANDO_COLOR: [
                CallbackQueryHandler(seleccionar_color, pattern="^(col_|ref_|volver_colores|volver_referencias)")
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("PV", iniciar_busqueda),
            CommandHandler("pv", iniciar_busqueda)
        ],
    )

    # 2. Manejador para Inventario SAP original (/in o botón start_sap)
    inventario_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("in", iniciar_busqueda_sap),
            CommandHandler("IN", iniciar_busqueda_sap),
            CallbackQueryHandler(iniciar_busqueda_sap, pattern="^start_sap$")
        ],
        states={
            ESPERANDO_REF_SAP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_ref_sap)
            ],
            SELECCIONANDO_REF_SAP: [
                CallbackQueryHandler(seleccionar_referencia_sap, pattern="^(invref_|volver_inv_refs)")
            ],
            SELECCIONANDO_COLOR_SAP: [
                CallbackQueryHandler(seleccionar_color_sap, pattern="^(inv_|volver_inv_colores|volver_inv_refs)")
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("in", iniciar_busqueda_sap),
            CommandHandler("IN", iniciar_busqueda_sap)
        ],
    )

    # 3. Nuevo Manejador para el Inventario de Vendedor (/i o botón start_vend)
    vendedor_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("i", iniciar_busqueda_vendedor),
            CommandHandler("I", iniciar_busqueda_vendedor),
            CallbackQueryHandler(iniciar_busqueda_vendedor, pattern="^start_vend$")
        ],
        states={
            ESPERANDO_REF_VEND: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_ref_vendedor)
            ],
            SELECCIONANDO_REF_VEND: [
                CallbackQueryHandler(seleccionar_referencia_vendedor, pattern="^(vendref_|volver_vend_refs)")
            ],
            SELECCIONANDO_COLOR_VEND: [
                CallbackQueryHandler(seleccionar_color_vendedor, pattern="^(vend_|volver_vend_refs)")
            ],
        },
        fallbacks=[
            CommandHandler("cancelar", cancelar),
            CommandHandler("i", iniciar_busqueda_vendedor),
            CommandHandler("I", iniciar_busqueda_vendedor)
        ],
    )

    application.add_handler(conv_handler)
    application.add_handler(inventario_conv_handler)
    application.add_handler(vendedor_conv_handler)
    
    # Comandos generales
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pedido", order_command))
    application.add_handler(CommandHandler("reiniciar", reiniciar_render))
    application.add_handler(CommandHandler("actualizar", limpiar_cache_y_deploy))
    
    # Comando para desplegar el menú interactivo con botones
    application.add_handler(CommandHandler("inicio", menu_inicio))
    application.add_handler(CommandHandler("menu", menu_inicio))
    
    application.run_polling()

if __name__ == "__main__":
    main()
