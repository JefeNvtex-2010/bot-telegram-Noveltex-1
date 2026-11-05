import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

TZ = ZoneInfo(os.environ.get("BORRADO_TZ", "America/Bogota"))
HORA = int(os.environ.get("BORRADO_HORA", "6"))
MINUTO = int(os.environ.get("BORRADO_MINUTO", "0"))
# IDs anteriores al primer mensaje registrado que también se intentan borrar
BARRIDO = int(os.environ.get("BORRADO_BARRIDO", "300"))
DB_PATH = os.environ.get("BORRADO_DB", "mensajes_chat.db")
# Opcional: "123,456". Vacío = limpia todos los chats que el bot conoce
CHATS_PERMITIDOS = {
    int(x) for x in os.environ.get("BORRADO_CHAT_IDS", "").split(",") if x.strip()
}

_lock = threading.Lock()


def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db() -> None:
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                min_id  INTEGER NOT NULL,
                max_id  INTEGER NOT NULL)"""
        )


def registrar(chat_id: int, message_id: int) -> None:
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO chats (chat_id, min_id, max_id) VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 min_id = MIN(min_id, excluded.min_id),
                 max_id = MAX(max_id, excluded.max_id)""",
            (chat_id, message_id, message_id),
        )


def _obtener(chat_id: int):
    with _lock, _conn() as c:
        return c.execute(
            "SELECT min_id, max_id FROM chats WHERE chat_id=?", (chat_id,)
        ).fetchone()


def _listar() -> list[int]:
    with _lock, _conn() as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM chats").fetchall()]


def _olvidar(chat_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))


async def _borrar_lote(bot, chat_id: int, ids: list[int]) -> None:
    """Borra hasta 100 IDs. Los que no existen, Telegram los ignora."""
    while True:
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=ids)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Forbidden:
            raise
        except (BadRequest, AttributeError, TypeError):
            break  # si el lote falla, se intenta uno por uno

    for mid in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError:
            pass
        await asyncio.sleep(0.03)


async def limpiar_chat(bot, chat_id: int) -> bool:
    """Deja el chat limpio. Devuelve True si hizo la limpieza."""
    fila = _obtener(chat_id)
    if not fila:
        return False
    min_id, max_id = fila

    # Mensaje marcador: su ID es el tope real e incluye lo que el bot envió
    # (esos mensajes no llegan como updates)
    try:
        marcador = await bot.send_message(
            chat_id, "🧹 Limpiando el chat...", disable_notification=True
        )
        tope = max(marcador.message_id, max_id)
    except (Forbidden, BadRequest):
        logger.warning("No se puede escribir en el chat %s; se descarta.", chat_id)
        _olvidar(chat_id)
        return False
    except TelegramError:
        tope = max_id

    inicio = max(1, min_id - BARRIDO)
    try:
        for i in range(inicio, tope + 1, 100):
            lote = list(range(i, min(i + 100, tope + 1)))
            await _borrar_lote(bot, chat_id, lote)
            await asyncio.sleep(0.4)
    except Forbidden:
        logger.warning(
            "Sin permiso para borrar en el chat %s (¿no es administrador?).", chat_id
        )
        _olvidar(chat_id)
        return False

    _olvidar(chat_id)
    logger.info("Chat %s limpiado (IDs %s a %s).", chat_id, inicio, tope)
    return True


async def limpiar_todos(bot) -> int:
    chats = _listar()
    if CHATS_PERMITIDOS:
        chats = [c for c in chats if c in CHATS_PERMITIDOS]
    limpios = 0
    for chat_id in chats:
        try:
            if await limpiar_chat(bot, chat_id):
                limpios += 1
        except Exception:
            logger.exception("Error limpiando el chat %s", chat_id)
    logger.info("Limpieza diaria terminada: %s chats.", limpios)
    return limpios


async def bucle_diario(application) -> None:
    """Revisa cada 30 s si ya pasó la hora de limpieza y no se ha hecho hoy."""
    ultima_fecha = None
    while True:
        try:
            ahora = datetime.now(TZ)
            objetivo = ahora.replace(
                hour=HORA, minute=MINUTO, second=0, microsecond=0
            )
            if ahora >= objetivo and ultima_fecha != ahora.date():
                ultima_fecha = ahora.date()
                await limpiar_todos(application.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en el bucle de limpieza diaria")
        await asyncio.sleep(30)
