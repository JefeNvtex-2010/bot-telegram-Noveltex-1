import logging
import traceback


class SecretRedactionFilter(logging.Filter):
    """Remove the bot token from log messages and exception tracebacks."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._secret in message:
            record.msg = message.replace(self._secret, "[REDACTED]")
            record.args = ()

        traceback_text = record.exc_text or ""
        if record.exc_info:
            traceback_text += "".join(traceback.format_exception(*record.exc_info))

        if self._secret in traceback_text:
            record.exc_info = None
            record.exc_text = None
            record.msg = (
                "Se omitieron los detalles del error para proteger "
                "el token de Telegram."
            )
            record.args = ()

        return True


def configure_logging(secret: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    redaction_filter = SecretRedactionFilter(secret)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction_filter)

    logging.getLogger("httpx").setLevel(logging.WARNING)