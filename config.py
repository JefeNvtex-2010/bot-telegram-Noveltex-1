import os
from dataclasses import dataclass
from dotenv import load_dotenv

class ConfigurationError(ValueError):
    """Raised when required bot configuration is missing."""

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str

def load_settings() -> Settings:
    """Load bot settings from the environment and optional local .env file."""
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError(
            "Falta TELEGRAM_BOT_TOKEN. Agrégalo en las variables de entorno de Render "
            "o en un archivo .env local."
        )
    
    return Settings(telegram_bot_token=token)
