import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google import genai
from google.genai import types

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
    SELECCIONANDO_COLOR,
    SELECCIONANDO_REFERENCIA,
    ESPERANDO_DOCUMENTO_PV,
    callback_expirado,
    cancelar,
    esc,
    help_command,
    iniciar_busqueda,
    limpiar_cache_y_deploy,
    manejar_error,
    menu_inicio,
    order_command,
    recibir_texto_documento_pv,
    refrescar_datos,
    reiniciar_render,
    seleccionar_color,
    seleccionar_referencia,
    start,
)

from inventario_handlers import (
    ESPERANDO_REF_SAP,
    SELECCIONANDO_COLOR_SAP,
    SELECCIONANDO_REF_SAP,
    _num,
    _primer_valor,  # <--- Corregido aquí
    iniciar_busqueda_sap,
    recibir_texto_ref_sap,
    seleccionar_color_sap,
    seleccionar_referencia_sap,
)

from inventario_vendedor_handlers import (
    ESPERANDO_REF_VEND,
    SELECCIONANDO_COLOR_VEND,
    SELECCIONANDO_REF_VEND,
    iniciar_busqueda_vendedor,
    recibir_texto_ref_vendedor,
    seleccionar_color_vendedor,
    seleccionar_referencia_vendedor,
)

from geo_handlers import (
    ESPERANDO_DOCUMENTO_PG,
    iniciar_pg,
    recibir_texto_pg,
)

logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN DE GEMINI AGENTE Y HERRAMIENTAS ---
ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))


def consultar_inventario_sap(termino_busqueda: str) -> str:
  """Busca el stock e inventario SAP general de una referencia o tela y devuelve el desglose por almacén con sus totales."""
  try:
    df = datos.inventario.df
    if df is None or df.empty:
      return "El inventario aún no se ha cargado o no está disponible."

    termino = termino_busqueda.strip().upper()
    refs = datos.buscar_referencias(df, termino)
    if not refs:
      return (
          f"No se encontraron referencias que coincidan con '{termino_busqueda}'."
      )

    ref_encontrada = refs[0]
    filas_ref = df[df["_ref"] == ref_encontrada]

    if filas_ref.empty:
      return f"No hay existencias para la referencia {ref_encontrada}."

    codigo_articulo = str(filas_ref.iloc[0].get("Artículo", "N/A"))
    descripcion = str(
        filas_ref.iloc[0].get("Descripción", f"{ref_encontrada}")
    )

    resultado_texto = (
        f"🟢 Inventario SAP\n\n"
        f"• Artículo: {codigo_articulo}\n"
        f"• Descripción: {descripcion}\n\n"
        f"Desglose por Almacén:\n"
    )

    total_stock = total_comp = total_disp = 0.0
    cols_cod = [
        c
        for c in df.columns
        if any(k in c.upper() for k in ("CÓDIGO", "ALMACÉN", "ALMACEN"))
    ]
    cols_nom = [
        c for c in df.columns if "NOMBRE" in c.upper() and "ALM" in c.upper()
    ]

    for _, fila in filas_ref.iterrows():
      cod_almacen = _primer_valor(fila, cols_cod, "1")
      nom_almacen = _primer_valor(fila, cols_nom, "")
      almacen_txt = (
          f"{cod_almacen} - {nom_almacen}" if nom_almacen else cod_almacen
      )

      stock = _num(fila.get("Stock", "0"), 0.0)
      comprometido = _num(fila.get("Comprometido", "0"), 0.0)
      disponible = _num(fila.get("Disponible", ""), None)
      if disponible is None:
        disponible = stock - comprometido

      total_stock += stock
      total_comp += comprometido
      total_disp += disponible

      resultado_texto += (
          f"• Almacén {esc(almacen_txt)}:\n"
          f"  - En stock: {stock:,.2f}\n"
          f"  - Comprometido: {comprometido:,.2f}\n"
          f"  - Disponible: {disponible:,.2f}\n\n"
      )

    resultado_texto += (
        "-----------------------------------\n"
        "📊 Totales Generales:\n"
        f"• Stock Total: {total_stock:,.2f}\n"
        f"• Total Comprometido: {total_comp:,.2f}\n"
        f"• Total Disponible: {total_disp:,.2f}"
    )
    return resultado_texto

  except Exception as e:
    return f"Ocurrió un error consultando el inventario: {str(e)}"


SYSTEM_INSTRUCTION = """
Eres el Agente Inteligente y Asistente Virtual oficial de Noveltex en Telegram.
Tu objetivo principal es ayudar a los usuarios a consultar el inventario de telas y productos usando la herramienta 'consultar_inventario_sap' siempre que pidan ver el stock, disponibilidad o metros de una referencia (por ejemplo, 'Fit', 'Satin', etc.).
Cuando el usuario pregunte por un inventario, DEBES invocar la herramienta de inventario para darle el reporte exacto con el formato estructurado de almacenes y totales.
Mantén un trato profesional, cercano y servicial. Recuerda el contexto de la charla anterior.
"""

user_chats = {}


def get_or_create_chat(user_id):
  """Crea o recupera la sesión de chat conversacional con la herramienta de inventario habilitada."""
  if user_id not in user_chats:
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.3,
        tools=[consultar_inventario_sap],
    )
    user_chats[user_id] = ai_client.chats.create(
        model="gemini-3.8-flash", config=config
    )
  return user_chats[user_id]


async def handle_gemini_agent(update, context):
  """Maneja las conversaciones y ejecución automática de herramientas (Function Calling)."""
  user_id = update.effective_user.id
  user_text = update.message.text

  await context.bot.send_chat_action(
      chat_id=update.effective_chat.id, action="typing"
  )

  try:
    chat_session = get_or_create_chat(user_id)
    response = await asyncio.to_thread(chat_session.send_message, user_text)

    if response.text:
      await update.message.reply_text(response.text, parse_mode="HTML")
    else:
      await update.message.reply_text(
          "Consulta procesada, pero no se generó respuesta.", parse_mode="HTML"
      )

  except Exception as e:
    logger.error(f"DETALLE DEL ERROR GEMINI: {e}", exc_info=True)
    await update.message.reply_text(f"⚠️ Error técnico: {str(e)}")


async def handle_voice_message(update, context):
  """Maneja las notas de voz, permitiendo que Gemini escuche la referencia y consulte el inventario."""
  user_id = update.effective_user.id

  await context.bot.send_chat_action(
      chat_id=update.effective_chat.id, action="typing"
  )

  try:
    voice_file = await update.message.voice.get_file()
    audio_bytes = await voice_file.download_as_bytearray()

    chat_session = get_or_create_chat(user_id)

    response = await asyncio.to_thread(
        chat_session.send_message,
        [
            types.Part.from_bytes(
                data=bytes(audio_bytes), mime_type="audio/ogg"
            ),
            (
                "Escucha esta nota de voz. Si el usuario está pidiendo consultar"
                " el inventario o stock de una referencia o tela, utiliza la"
                " herramienta de inventario para darle el reporte exacto."
            ),
        ],
    )

    await update.message.reply_text(response.text, parse_mode="HTML")

  except Exception as e:
    logger.error(f"Error procesando nota de voz en Gemini: {e}")
    await update.message.reply_text(f"⚠️ Error técnico voz: {str(e)}")


class SimpleHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"Bot is alive!")

  def do_HEAD(self):
    self.send_response(200)
    self.end_headers()

  def log_message(self, *args):
    pass


def run_dummy_server():
  port = int(os.environ.get("PORT", 10000))
  server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
  server.serve_forever()


async def _post_init(application) -> None:
  await datos.iniciar(application)


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

  entradas = [
      CommandHandler("pv", iniciar_busqueda),
      CommandHandler("in", iniciar_busqueda_sap),
      CommandHandler("i", iniciar_busqueda_vendedor),
      CommandHandler("pg", iniciar_pg),
      CallbackQueryHandler(iniciar_busqueda, pattern="^start_pv$"),
      CallbackQueryHandler(iniciar_busqueda_sap, pattern="^start_sap$"),
      CallbackQueryHandler(iniciar_busqueda_vendedor, pattern="^start_vend$"),
  ]

  texto_pv = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_documento_pv
  )
  texto_sap = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_ref_sap
  )
  texto_vend = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_ref_vendedor
  )
  texto_pg = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_pg
  )

  estados = {
      ESPERANDO_DOCUMENTO_PV: [texto_pv],
      SELECCIONANDO_REFERENCIA: [
          CallbackQueryHandler(seleccionar_referencia, pattern=r"^pv_ref_\d+$"),
          texto_pv,
      ],
      SELECCIONANDO_COLOR: [
          CallbackQueryHandler(
              seleccionar_color, pattern=r"^pv_(col_\d+|all|back_refs|back_cols)$"
          ),
          texto_pv,
      ],
      ESPERANDO_REF_SAP: [texto_sap],
      SELECCIONANDO_REF_SAP: [
          CallbackQueryHandler(
              seleccionar_referencia_sap, pattern=r"^sap_ref_\d+$"
          ),
          texto_sap,
      ],
      SELECCIONANDO_COLOR_SAP: [
          CallbackQueryHandler(
              seleccionar_color_sap,
              pattern=r"^sap_(col_\d+|back_refs|back_cols)$",
          ),
          texto_sap,
      ],
      ESPERANDO_REF_VEND: [texto_vend],
      SELECCIONANDO_REF_VEND: [
          CallbackQueryHandler(
              seleccionar_referencia_vendedor, pattern=r"^vend_ref_\d+$"
          ),
          texto_vend,
      ],
      SELECCIONANDO_COLOR_VEND: [
          CallbackQueryHandler(
              seleccionar_color_vendedor,
              pattern=r"^vend_(col_\d+|back_refs|share)$",
          ),
          texto_vend,
      ],
      ESPERANDO_DOCUMENTO_PG: [texto_pg],
  }

  fallbacks = entradas + [
      CommandHandler("cancelar", cancelar),
      CommandHandler(["inicio", "menu"], menu_inicio),
  ]

  application.add_handler(
      ConversationHandler(
          entry_points=entradas,
          states=estados,
          fallbacks=fallbacks,
          conversation_timeout=900 if application.job_queue else None,
      )
  )

  application.add_handler(CommandHandler("start", start))
  application.add_handler(CommandHandler("help", help_command))
  application.add_handler(CommandHandler("pedido", order_command))
  application.add_handler(CommandHandler("reiniciar", reiniciar_render))
  application.add_handler(CommandHandler("actualizar", limpiar_cache_y_deploy))
  application.add_handler(CommandHandler("refrescar", refrescar_datos))
  application.add_handler(CommandHandler(["inicio", "menu"], menu_inicio))

  application.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gemini_agent)
  )
  application.add_handler(
      MessageHandler(filters.VOICE, handle_voice_message)
  )

  application.add_handler(CallbackQueryHandler(callback_expirado))
  application.add_error_handler(manejar_error)

  application.run_polling(
      drop_pending_updates=True,
      allowed_updates=["message", "callback_query"],
  )


if __name__ == "__main__":
  main()
