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
from acceso import configurar_comandos, miid, recargar_accesos_cmd, requiere_acceso
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
    _primer_valor,
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
    SELECCIONANDO_COLOR_PG,
    SELECCIONANDO_REF_PG,
    iniciar_pg,
    recibir_texto_pg,
    seleccionar_color_pg,
    seleccionar_referencia_pg,
)

from guias_handlers import (
    ACCIONES_GU,
    ELIGIENDO_TIPO_GU,
    ESPERANDO_DOCUMENTO_GU,
    SELECCIONANDO_GUIA_GU,
    acciones_gu,
    elegir_tipo_gu,
    iniciar_gu,
    recibir_texto_gu,
    seleccionar_guia_gu,
)

from etiquetas_handlers import (  # impresión de etiquetas Zebra
    ESPERANDO_COPIAS_ET,
    ESPERANDO_METROS_ET,
    ESPERANDO_REF_ET,
    SELECCIONANDO_COLOR_ET,
    SELECCIONANDO_REF_ET,
    iniciar_etiqueta,
    recibir_copias_et,
    recibir_metros_et,
    recibir_texto_ref_et,
    seleccionar_color_et,
    seleccionar_referencia_et,
)

from ubicacion import (  # ubicaciones: consultar por nombre, consultar por cajón, modificar por código
    UB_ESPERANDO_CAJON,
    UB_ESPERANDO_CODIGO,
    UB_ESPERANDO_REF,
    UB_ESPERANDO_UBICACION,
    UB_MENU,
    UB_SELECCIONANDO_COLOR,
    UB_SELECCIONANDO_REF,
    botones_ub,
    iniciar_ubicaciones,
    recibir_cajon_ub,
    recibir_codigo_ub,
    recibir_ref_ub,
    recibir_ubicacion_ub,
    recordar_botones_ub,
    seguir_ub,
    seleccionar_color_ub,
    seleccionar_ref_ub,
)

from transferencias import (  # transferencias (Faltante, Retazos, Imperfecto)
    TR_ELEGIR_METODO,
    TR_ESPERANDO_CANTIDAD,
    TR_ESPERANDO_CODIGO,
    TR_ESPERANDO_REF,
    TR_MENU,
    TR_SELECCIONANDO_COLOR,
    TR_SELECCIONANDO_REF,
    botones_tr_menu,
    iniciar_transferencias,
    recibir_cantidad_tr,
    recibir_codigo_tr,
    recibir_ref_tr,
    seleccionar_color_tr,
    seleccionar_metodo_tr,
    seleccionar_ref_tr,
    seguir_tr,
)

logger = logging.getLogger(__name__)

# --- CONTROL DE ACCESO POR MÓDULO ---
iniciar_busqueda_sap = requiere_acceso("inventario")(iniciar_busqueda_sap)
iniciar_pg = requiere_acceso("PG")(iniciar_pg)
iniciar_gu = requiere_acceso("guias")(iniciar_gu)
iniciar_etiqueta = requiere_acceso("etiquetas")(iniciar_etiqueta)
iniciar_transferencias = requiere_acceso("transferencias")(iniciar_transferencias)

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


@requiere_acceso("inventario")
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


@requiere_acceso("inventario")
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
  application.bot_data["tarea_comandos"] = asyncio.create_task(
      configurar_comandos(application)
  )


async def _post_shutdown(application) -> None:
  tarea = application.bot_data.get("tarea_comandos")
  if tarea and not tarea.done():
    tarea.cancel()
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
      CommandHandler("iv", iniciar_busqueda_vendedor),
      CommandHandler("pg", iniciar_pg),
      CommandHandler("gu", iniciar_gu),
      CommandHandler("etiqueta", iniciar_etiqueta),
      CommandHandler("ub", iniciar_ubicaciones),
      CommandHandler("tr", iniciar_transferencias),
      CallbackQueryHandler(iniciar_busqueda, pattern="^start_pv$"),
      CallbackQueryHandler(iniciar_busqueda_sap, pattern="^start_sap$"),
      CallbackQueryHandler(iniciar_busqueda_vendedor, pattern="^start_vend$"),
      CallbackQueryHandler(iniciar_pg, pattern="^start_pg$"),
      CallbackQueryHandler(iniciar_gu, pattern="^start_gu$"),
      CallbackQueryHandler(iniciar_etiqueta, pattern="^start_et$"),
      CallbackQueryHandler(iniciar_ubicaciones, pattern="^start_ub$"),
      CallbackQueryHandler(iniciar_transferencias, pattern="^start_tr$"),
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
  texto_gu = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_gu
  )
  texto_ref_et = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_texto_ref_et
  )
  texto_metros_et = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_metros_et
  )
  texto_copias_et = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_copias_et
  )
  botones_tipo_gu = CallbackQueryHandler(
      elegir_tipo_gu, pattern=r"^gu_tipo_(pedido|factura|menu)$"
  )
  # Ubicaciones de bodega
  botones_ub_cb = CallbackQueryHandler(
      botones_ub,
      pattern=r"^ub_(menu|consultar|cajon|modificar|reemplazar|agregar)$",
  )
  ub_ref_cb = CallbackQueryHandler(
      seleccionar_ref_ub, pattern=r"^ub_ref_\d+$"
  )
  ub_color_cb = CallbackQueryHandler(
      seleccionar_color_ub, pattern=r"^ub_(col_\d+|back_refs|back_cols)$"
  )
  ub_seguir_cb = CallbackQueryHandler(
      seguir_ub, pattern=r"^ub_seguir_(si|no)$"
  )
  texto_ub_menu = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recordar_botones_ub
  )
  texto_ub_ref = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_ref_ub
  )
  texto_ub_cajon = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_cajon_ub
  )
  texto_ub_codigo = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_codigo_ub
  )
  texto_ub_ubicacion = MessageHandler(
      filters.TEXT & ~filters.COMMAND, recibir_ubicacion_ub
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
      SELECCIONANDO_REF_PG: [
          CallbackQueryHandler(
              seleccionar_referencia_pg, pattern=r"^pg_ref_\d+$"
          ),
          texto_pg,
      ],
      SELECCIONANDO_COLOR_PG: [
          CallbackQueryHandler(
              seleccionar_color_pg,
              pattern=r"^pg_(col_\d+|all|back_refs|back_cols)$",
          ),
          texto_pg,
      ],
      ELIGIENDO_TIPO_GU: [botones_tipo_gu, texto_gu],
      ESPERANDO_DOCUMENTO_GU: [botones_tipo_gu, texto_gu],
      SELECCIONANDO_GUIA_GU: [
          CallbackQueryHandler(
              seleccionar_guia_gu, pattern=r"^gu_sel_\d+$"
          ),
          texto_gu,
      ],
      ACCIONES_GU: [
          CallbackQueryHandler(acciones_gu, pattern=r"^gu_(img|back|nuevo)$"),
          texto_gu,
      ],
      # Flujo de etiquetas (referencia -> color -> metros)
      ESPERANDO_REF_ET: [texto_ref_et],
      SELECCIONANDO_REF_ET: [
          CallbackQueryHandler(
              seleccionar_referencia_et, pattern=r"^et_ref_\d+$"
          ),
          texto_ref_et,
      ],
      SELECCIONANDO_COLOR_ET: [
          CallbackQueryHandler(
              seleccionar_color_et, pattern=r"^et_(col_\d+|back)$"
          ),
          texto_ref_et,
      ],
      ESPERANDO_METROS_ET: [texto_metros_et],
      ESPERANDO_COPIAS_ET: [texto_copias_et],
      # Ubicaciones de bodega
      UB_MENU: [botones_ub_cb, texto_ub_menu],
      UB_ESPERANDO_REF: [botones_ub_cb, texto_ub_ref],
      UB_SELECCIONANDO_REF: [ub_ref_cb, botones_ub_cb, texto_ub_ref],
      UB_SELECCIONANDO_COLOR: [ub_color_cb, botones_ub_cb, texto_ub_ref],
      UB_ESPERANDO_CAJON: [botones_ub_cb, texto_ub_cajon],
      UB_ESPERANDO_CODIGO: [ub_seguir_cb, botones_ub_cb, texto_ub_codigo],
      UB_ESPERANDO_UBICACION: [botones_ub_cb, texto_ub_ubicacion],
      # Transferencias (Faltante, Retazos, Imperfecto)
      TR_MENU: [CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_(menu|tipo_.+)$")],
      TR_ELEGIR_METODO: [
          CallbackQueryHandler(seleccionar_metodo_tr, pattern=r"^tr_metodo_.+$"),
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
      ],
      TR_ESPERANDO_CODIGO: [
          CallbackQueryHandler(seguir_tr, pattern=r"^tr_seguir_(si|no)$"),
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
          MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_codigo_tr),
      ],
      TR_ESPERANDO_REF: [
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
          MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_ref_tr),
      ],
      TR_SELECCIONANDO_REF: [
          CallbackQueryHandler(seleccionar_ref_tr, pattern=r"^tr_ref_\d+$"),
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
      ],
      TR_SELECCIONANDO_COLOR: [
          CallbackQueryHandler(seleccionar_color_tr, pattern=r"^tr_col_\d+$"),
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
      ],
      TR_ESPERANDO_CANTIDAD: [
          CallbackQueryHandler(botones_tr_menu, pattern=r"^tr_menu$"),
          MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cantidad_tr),
      ],
  }

  fallbacks = entradas + [
      CommandHandler("cancelar", cancelar),
      CommandHandler(["i", "inicio", "menu"], menu_inicio),
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
  application.add_handler(CommandHandler("miid", miid))
  application.add_handler(CommandHandler("accesos", recargar_accesos_cmd))
  application.add_handler(CommandHandler(["i", "inicio", "menu"], menu_inicio))

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
