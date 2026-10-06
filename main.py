import logging
from telegram.ext import ApplicationBuilder, CommandHandler
from telegram_bot.config import load_settings
from telegram_bot.logging_config import configure_logging
from telegram_bot.handlers import start, help_command, order_command, buscar_pedido

logger = logging.getLogger(__name__)

def main() -> None:
    settings = load_settings()
    configure_logging(settings.telegram_bot_token)

    logger.info("Iniciando el bot de Telegram...")

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Registrar todos los comandos (incluyendo /buscar)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pedido", order_command))
    application.add_handler(CommandHandler("buscar", buscar_pedido))
    application.add_handler(CommandHandler("BUSCAR", buscar_pedido))

    application.run_polling()

if __name__ == "__main__":
    main()