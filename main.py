import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import datos
from config import load_settings
from logging_config import configure_logging

from handlers import (
    start, help_command, order_command,
    iniciar_busqueda, seleccionar_referencia, seleccionar_color,
    cancelar, reiniciar_render, limpiar_cache_y_deploy, refrescar_datos,
    menu_inicio, recibir_texto_documento_pv, callback_expirado, manejar_error,
    SELECCIONANDO_REFERENCIA, SELECCIONANDO_COLOR, ESPERANDO_DOCUMENTO_PV,
)

from inventario_handlers import (
    iniciar_busqueda_sap, seleccionar_referencia_sap, seleccionar_color_sap,
    recibir_texto_ref_sap,
    SELECCIONANDO_REF_SAP, SELECCIONANDO_COLOR_SAP, ESPERANDO_REF_SAP,
)

from inventario_vendedor_handlers import (
    iniciar_busqueda_vendedor, seleccionar_referencia_vendedor, seleccionar_color_vendedor,
    recibir_texto_ref_vendedor,
    SELECCIONANDO_REF_VEND, SELECCIONANDO_COLOR_VEND, ESPERANDO_REF_VEND,
)

logger = logging.getLogger(__name__)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):  # algunos monitores (UptimeRobot) usan HEAD
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # evita llenar los logs con cada ping
        pass


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


async def _post_init(application) -> None:
    await datos.iniciar(application)  # precarga las hojas y las refresca en segundo plano


async def _post_shutdown(application) -> None:
    await datos.detener(application)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.telegram_bot_token)

    threading.Thread(target=run_dummy_server, daemon=True).start()

    logger.info("Iniciando el bot de Telegram...")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .connect_timeout(10)
        .read_timeout(15)
        .write_timeout(15)
        .pool_timeout(10)
        .get_updates_read_timeout(30)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Un solo ConversationHandler para las 3 consultas. Antes eran tres separados y,
    # si el usuario dejaba uno abierto, el texto de la siguiente búsqueda lo atendía
    # el handler equivocado (por eso "a veces no buscaba" o mostraba otra cosa).
    entradas = [
        CommandHandler("pv", iniciar_busqueda),
        CommandHandler("in", iniciar_busqueda_sap),
        CommandHandler("i", iniciar_busqueda_vendedor),
        CallbackQueryHandler(iniciar_busqueda, pattern="^start_pv$"),
        CallbackQueryHandler(iniciar_busqueda_sap, pattern="^start_sap$"),
        CallbackQueryHandler(iniciar_busqueda_vendedor, pattern="^start_vend$"),
    ]

    texto_pv = MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_documento_pv)
    texto_sap = MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_ref_sap)
    texto_vend = MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_texto_ref_vendedor)

    estados = {
        # PEDIDOS (PV). Escribir texto en cualquier pantalla lanza una nueva búsqueda.
        ESPERANDO_DOCUMENTO_PV: [texto_pv],
        SELECCIONANDO_REFERENCIA: [
            CallbackQueryHandler(seleccionar_referencia, pattern=r"^pv_ref_\d+$"),
            texto_pv,
        ],
        SELECCIONANDO_COLOR: [
            CallbackQueryHandler(seleccionar_color, pattern=r"^pv_(col_\d+|all|back_refs|back_cols)$"),
            texto_pv,
        ],
        # INVENTARIO SAP
        ESPERANDO_REF_SAP: [texto_sap],
        SELECCIONANDO_REF_SAP: [
            CallbackQueryHandler(seleccionar_referencia_sap, pattern=r"^sap_ref_\d+$"),
            texto_sap,
        ],
        SELECCIONANDO_COLOR_SAP: [
            CallbackQueryHandler(seleccionar_color_sap, pattern=r"^sap_(col_\d+|back_refs|back_cols)$"),
            texto_sap,
        ],
        # INVENTARIO VENDEDOR
        ESPERANDO_REF_VEND: [texto_vend],
        SELECCIONANDO_REF_VEND: [
            CallbackQueryHandler(seleccionar_referencia_vendedor, pattern=r"^vend_ref_\d+$"),
            texto_vend,
        ],
        SELECCIONANDO_COLOR_VEND: [
            CallbackQueryHandler(seleccionar_color_vendedor, pattern=r"^vend_(col_\d+|back_refs|share)$"),
            texto_vend,
        ],
    }

    fallbacks = entradas + [
        CommandHandler("cancelar", cancelar),
        CommandHandler(["inicio", "menu"], menu_inicio),
    ]

    application.add_handler(ConversationHandler(
        entry_points=entradas,
        states=estados,
        fallbacks=fallbacks,
        conversation_timeout=900 if application.job_queue else None,  # 15 min sin uso: se cierra
    ))

    # Comandos generales
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pedido", order_command))
    application.add_handler(CommandHandler("reiniciar", reiniciar_render))
    application.add_handler(CommandHandler("actualizar", limpiar_cache_y_deploy))
    application.add_handler(CommandHandler("refrescar", refrescar_datos))
    application.add_handler(CommandHandler(["inicio", "menu"], menu_inicio))

    # Botones viejos (p. ej. tras reiniciar el bot): responde en vez de quedarse "cargando"
    application.add_handler(CallbackQueryHandler(callback_expirado))

    application.add_error_handler(manejar_error)

    application.run_polling(
        drop_pending_updates=True,  # no procesa mensajes acumulados mientras el bot estuvo apagado
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
