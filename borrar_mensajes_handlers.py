import logging
import os

import borrar_mensajes

logger = logging.getLogger(__name__)

# Opcional: "111,222". Si se define, solo esos usuarios pueden usar /limpiar
ADMIN_IDS = {
    int(x) for x in os.environ.get("BORRADO_ADMIN_IDS", "").split(",") if x.strip()
}


async def registrar_mensaje(update, context) -> None:
    """Anota el ID de cada mensaje y botón que pasa por el bot."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg and chat:
        borrar_mensajes.registrar(chat.id, msg.message_id)


async def limpiar_cmd(update, context) -> None:
    """/limpiar: deja limpio ahora mismo el chat donde se escribe."""
    user = update.effective_user
    chat = update.effective_chat
    if ADMIN_IDS and (not user or user.id not in ADMIN_IDS):
        await update.effective_message.reply_text("⛔ No tienes permiso para esto.")
        return
    borrar_mensajes.registrar(chat.id, update.effective_message.message_id)
    await borrar_mensajes.limpiar_chat(context.bot, chat.id)
