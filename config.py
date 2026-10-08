import os
from dataclasses import dataclass
from dotenv import load_dotenv

class ConfigurationError(ValueError):
    """Raised when required bot configuration is missing."""

@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    sap_url: str
    sap_company_db: str
    sap_user: str
    sap_password: str

def load_settings() -> Settings:
    """Load bot settings from the environment and optional local .env file."""
    load_dotenv()
    
    # 1. Validación de Telegram
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError(
            "Falta TELEGRAM_BOT_TOKEN. Agrégalo en las variables de entorno de Render "
            "o en un archivo .env local."
        )

    # 2. Configuración de SAP Service Layer
    sap_url = os.getenv("SAP_URL", "").strip()
    sap_company_db = os.getenv("SAP_COMPANY_DB", "").strip()
    sap_user = os.getenv("SAP_USER", "").strip()
    sap_password = os.getenv("SAP_PASSWORD", "").strip()

    # Opcional: Validar si falta alguna credencial clave de SAP para que avise en los logs
    if not all([sap_url, sap_company_db, sap_user, sap_password]):
        print("⚠️ Advertencia: Faltan algunas variables de SAP en el entorno (SAP_URL, SAP_COMPANY_DB, SAP_USER o SAP_PASSWORD).")

    return Settings(
        telegram_bot_token=token,
        sap_url=sap_url,
        sap_company_db=sap_company_db,
        sap_user=sap_user,
        sap_password=sap_password
    )
