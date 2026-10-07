import logging
import threading
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ConversationHandler
from config import load_settings
from logging_config import configure_logging
from handlers import (
    start, help_command, order_command, 
    iniciar_busqueda, seleccionar_referencia, seleccionar_color, 
    cancelar, SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR
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
    
    # Iniciar servidor web en segundo plano para cumplir con Render
    server_thread = threading.Thread(target=run_dummy_server, daemon=True)
    server_thread.start()
    
    logger.info("Iniciando el bot de Telegram...")
    
    application = ApplicationBuilder().token(settings.telegram_bot_token).build()
    
    # Configurar el ConversationHandler para el flujo interactivo de /buscar
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("buscar", iniciar_busqueda),
            CommandHandler("BUSCAR", iniciar_busqueda)
        ],
        states={
            # Usamos patrones flexibles para capturar cualquier callback de referencia o color
            SELECCIONANDO_REFERENCIA: [CallbackQueryHandler(seleccionar_referencia, pattern="^(ref_|ver_todo)")],
            SELECCIONANDO_COLOR: [CallbackQueryHandler(seleccionar_color)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
    )

    # Registrar manejadores
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pedido", order_command))
    
    application.run_polling()

if __name__ == "__main__":
    main()
