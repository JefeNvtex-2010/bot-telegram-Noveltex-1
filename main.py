import os  # Asegúrate de importar os si no está
from google import genai
from google.genai import types

# ... (todo tu código anterior de importaciones y configuración)

# --- CONFIGURACIÓN DE GEMINI AGENTE ---
# Inicializa el cliente de Gemini (tomará la API key de la variable de entorno GEMINI_API_KEY)
ai_client = genai.Client()

SYSTEM_INSTRUCTION = """
Eres el Agente Inteligente y Asistente Virtual oficial de Noveltex.
Tu objetivo es conversar fluidamente con los usuarios, ayudarles con información general, 
resolver dudas y guiarlos de forma amigable, profesional y precisa. 
Tienes un tono cercano y corporativo. Recuerda el contexto de la charla.
"""

# Diccionario para almacenar las sesiones de chat activas de cada usuario
user_chats = {}


def get_or_create_chat(user_id):
  if user_id not in user_chats:
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.7,
    )
    user_chats[user_id] = ai_client.chats.create(
        model="gemini-2.5-flash", config=config
    )
  return user_chats[user_id]


# Función que atenderá los mensajes libres del usuario como un agente
async def handle_gemini_agent(update, context):
  user_id = update.effective_user.id
  user_text = update.message.text

  # Mostrar la acción de "escribiendo..." para mayor fluidez
  await context.bot.chat_action(
      chat_id=update.effective_chat.id, action="typing"
  )

  try:
    chat_session = get_or_create_chat(user_id)
    response = chat_session.send_message(user_text)
    await update.message.reply_text(response.text)
  except Exception as e:
    logger.error(f"Error en agente Gemini: {e}")
    await update.message.reply_text(
        "Ups, tuve un pequeño problema procesando tu mensaje. Inténtalo de"
        " nuevo."
    )


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

  # ... (toda tu configuración de entradas, estados, fallbacks y ConversationHandler que ya tienes) ...
  # (Mantén tu ConversationHandler tal cual para que tus menús de inventario sigan funcionando perfecto)

  # Comandos generales
  application.add_handler(CommandHandler("start", start))
  application.add_handler(CommandHandler("help", help_command))
  application.add_handler(CommandHandler("pedido", order_command))
  application.add_handler(CommandHandler("reiniciar", reiniciar_render))
  application.add_handler(CommandHandler("actualizar", limpiar_cache_y_deploy))
  application.add_handler(CommandHandler("refrescar", refrescar_datos))
  application.add_handler(CommandHandler(["inicio", "menu"], menu_inicio))

  # --- AGREGAR EL MANEJADOR DEL AGENTE GEMINI ---
  # Este manejador capturará cualquier texto que NO esté dentro de un ConversationHandler activo
  application.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gemini_agent)
  )

  # Botones viejos
  application.add_handler(CallbackQueryHandler(callback_expirado))
  application.add_error_handler(manejar_error)

  application.run_polling(
      drop_pending_updates=True,
      allowed_updates=["message", "callback_query"],
  )


if __name__ == "__main__":
  main()
