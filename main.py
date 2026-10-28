import asyncio
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
from acceso import configurar_comandos, miid, recargar_accesos_cmd, requiere_acceso
from config import load_settings
from logging_config import configure_logging

from handlers import (
    SELECCIONANDO_COLOR,
    SELECCIONANDO_REFERENCIA,
    ESPERANDO_DOCUMENTO_PV,
    callback_expirado,
    cancelar,
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

from pedidos_vendedor_handlers import (
    ESPERANDO_DOCUMENTO_PC,
    SELECCIONANDO_COLOR_PC,
    SELECCIONANDO_REF_PC,
    iniciar_pc,
    recibir_texto_pc,
    seleccionar_color_pc,
    seleccionar_referencia_pc,
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

from etiquetas_handlers import (
    ESPERANDO_COPIAS_ET,
    ESPERANDO_METROS_ET,
    ESPERANDO_REF_ET,
    SELECCIONANDO_COLOR_ET,
    SELECCIONANDO_REF_ET,
    SELECCIONANDO_IMPRESORAS_ET,
    PREGUNTANDO_OTRA_ET,
    iniciar_etiqueta,
    recibir_copias_et,
    recibir_metros_et,
    recibir_texto_ref_et,
    seleccionar_color_et,
    seleccionar_referencia_et,
    procesar_impresion_et,
    manejar_otra_etiqueta,
)

from ubicacion import (
    UB_ESPERANDO_CAJON,
    UB_ESPERANDO_CODIGO,
    UB_ESPERANDO_ID,
    UB_ESPERANDO_REF,
    UB_ESPERANDO_UBICACION,
    UB_MENU,
    UB_SELECCIONANDO_COLOR,
    UB_SELECCIONANDO_REF,
    botones_ub,
    iniciar_ubicaciones,
    recibir_cajon_ub,
    recibir_codigo_ub,
    recibir_id_ub,
    recibir_ref_ub,
    recibir_ubicacion_ub,
    recordar_botones_ub,
    seguir_ub,
    seleccionar_color_ub,
    seleccionar_ref_ub,
)

from transferencias import (
    TR_ELEGIR_METODO,
    TR_ESPERANDO_CANTIDAD,
    TR_ESPERANDO_CODIGO,
    TR_ESPERANDO_ID,
    TR_ESPERANDO_REF,
    TR_MENU,
    TR_SELECCIONANDO_COLOR,
    TR_SELECCIONANDO_REF,
    botones_tr_menu,
    iniciar_transferencias,
    recibir_cantidad_tr,
    recibir_codigo_tr,
    recibir_id_tr,
    recibir_ref_tr,
    seleccionar_color_tr,
    seleccionar_metodo_tr,
    seleccionar_ref_tr,
    seguir_tr,
)

logger = logging.getLogger(__name__)

# Control de acceso por función (permisos leídos de la pestaña PERMISOS).
iniciar_busqueda_sap = requiere_acceso("inventario")(iniciar_busqueda_sap)
iniciar_pg = requiere_acceso("PG")(iniciar_pg)
iniciar_pc = requiere_acceso("PC")(iniciar_pc)
iniciar_gu = requiere_acceso("guias")(iniciar_gu)
iniciar_etiqueta = requiere_acceso("etiquetas")(iniciar_etiqueta)
iniciar_transferencias = requiere_acceso("transferencias")(iniciar_transferencias)


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
    application.bot_data["tarea_comandos"] = asyncio.create_task(configurar_comandos(application))


async def _post_shutdown(application) -> None:
    tarea = application.bot_data.get("tarea_comandos")
    if tarea and not tarea.done():
        tarea.cancel()
    await datos.detener(application)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.telegram_bot_token)
    threading.Thread(target=run_dummy_server, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .connect_timeout(10)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # ------------------------------------------------------------------ #
    # Comandos que arrancan un flujo (conversación)
    # ------------------------------------------------------------------ #
    entradas = [
        CommandHandler("pv", iniciar_busqueda),
        CommandHandler("in", iniciar_busqueda_sap),
        CommandHandler("iv", iniciar_busqueda_vendedor),
        CommandHandler("pg", iniciar_pg),
        CommandHandler("pc", iniciar_pc),
        CommandHandler("gu", iniciar_gu),
        CommandHandler("etiqueta", iniciar_etiqueta),
        CommandHandler("ub", iniciar_ubicaciones),
        CommandHandler("tr", iniciar_transferencias),
    ]

    texto = filters.TEXT & ~filters.COMMAND

    estados = {
        # /pv
        ESPERANDO_DOCUMENTO_PV: [MessageHandler(texto, recibir_texto_documento_pv)],
        SELECCIONANDO_REFERENCIA: [CallbackQueryHandler(seleccionar_referencia, pattern=r"^pv_ref_\d+$")],
        SELECCIONANDO_COLOR: [CallbackQueryHandler(seleccionar_color, pattern=r"^pv_(col_\d+|all|back_refs|back_cols)$")],

        # /in
        ESPERANDO_REF_SAP: [MessageHandler(texto, recibir_texto_ref_sap)],
        SELECCIONANDO_REF_SAP: [CallbackQueryHandler(seleccionar_referencia_sap, pattern=r"^sap_ref_\d+$")],
        SELECCIONANDO_COLOR_SAP: [CallbackQueryHandler(seleccionar_color_sap, pattern=r"^sap_(col_\d+|back_refs|back_cols)$")],

        # /etiqueta
        ESPERANDO_REF_ET: [MessageHandler(texto, recibir_texto_ref_et)],
        SELECCIONANDO_REF_ET: [CallbackQueryHandler(seleccionar_referencia_et, pattern=r"^et_ref_\d+$")],
        SELECCIONANDO_COLOR_ET: [CallbackQueryHandler(seleccionar_color_et, pattern=r"^et_(col_\d+|back)$")],
        ESPERANDO_METROS_ET: [MessageHandler(texto, recibir_metros_et)],
        ESPERANDO_COPIAS_ET: [MessageHandler(texto, recibir_copias_et)],
        SELECCIONANDO_IMPRESORAS_ET: [CallbackQueryHandler(procesar_impresion_et, pattern=r"^et_imp_.+$")],
        PREGUNTANDO_OTRA_ET: [CallbackQueryHandler(manejar_otra_etiqueta, pattern=r"^et_otra_(si|no)$")],

        # /iv
        ESPERANDO_REF_VEND: [MessageHandler(texto, recibir_texto_ref_vendedor)],
        SELECCIONANDO_REF_VEND: [CallbackQueryHandler(seleccionar_referencia_vendedor)],
        SELECCIONANDO_COLOR_VEND: [CallbackQueryHandler(seleccionar_color_vendedor)],

        # /pg
        ESPERANDO_DOCUMENTO_PG: [MessageHandler(texto, recibir_texto_pg)],
        SELECCIONANDO_REF_PG: [CallbackQueryHandler(seleccionar_referencia_pg)],
        SELECCIONANDO_COLOR_PG: [CallbackQueryHandler(seleccionar_color_pg)],

        # /pc
        ESPERANDO_DOCUMENTO_PC: [MessageHandler(texto, recibir_texto_pc)],
        SELECCIONANDO_REF_PC: [CallbackQueryHandler(seleccionar_referencia_pc)],
        SELECCIONANDO_COLOR_PC: [CallbackQueryHandler(seleccionar_color_pc)],

        # /gu
        ELIGIENDO_TIPO_GU: [CallbackQueryHandler(elegir_tipo_gu)],
        ESPERANDO_DOCUMENTO_GU: [MessageHandler(texto, recibir_texto_gu)],
        SELECCIONANDO_GUIA_GU: [CallbackQueryHandler(seleccionar_guia_gu)],
        ACCIONES_GU: [CallbackQueryHandler(acciones_gu)],

        # /ub
        UB_MENU: [CallbackQueryHandler(seguir_ub)],
        UB_ESPERANDO_ID: [MessageHandler(texto, recibir_id_ub)],
        UB_ESPERANDO_REF: [MessageHandler(texto, recibir_ref_ub)],
        UB_SELECCIONANDO_REF: [CallbackQueryHandler(seleccionar_ref_ub)],
        UB_SELECCIONANDO_COLOR: [CallbackQueryHandler(seleccionar_color_ub)],
        UB_ESPERANDO_CODIGO: [MessageHandler(texto, recibir_codigo_ub)],
        UB_ESPERANDO_CAJON: [MessageHandler(texto, recibir_cajon_ub)],
        UB_ESPERANDO_UBICACION: [MessageHandler(texto, recibir_ubicacion_ub)],

        # /tr
        TR_MENU: [CallbackQueryHandler(seguir_tr)],
        TR_ELEGIR_METODO: [CallbackQueryHandler(seleccionar_metodo_tr)],
        TR_ESPERANDO_ID: [MessageHandler(texto, recibir_id_tr)],
        TR_ESPERANDO_REF: [MessageHandler(texto, recibir_ref_tr)],
        TR_SELECCIONANDO_REF: [CallbackQueryHandler(seleccionar_ref_tr)],
        TR_SELECCIONANDO_COLOR: [CallbackQueryHandler(seleccionar_color_tr)],
        TR_ESPERANDO_CODIGO: [MessageHandler(texto, recibir_codigo_tr)],
        TR_ESPERANDO_CANTIDAD: [MessageHandler(texto, recibir_cantidad_tr)],
    }

    # ------------------------------------------------------------------ #
    # Comandos sueltos (fuera de la conversación)
    # ------------------------------------------------------------------ #
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("i", menu_inicio))
    application.add_handler(CommandHandler("miid", miid))
    application.add_handler(CommandHandler("accesos", recargar_accesos_cmd))
    application.add_handler(CommandHandler("refrescar", refrescar_datos))
    application.add_handler(CommandHandler("pedido", order_command))
    application.add_handler(CommandHandler("reiniciar", reiniciar_render))
    application.add_handler(CommandHandler("actualizar", limpiar_cache_y_deploy))

    application.add_handler(ConversationHandler(
        entry_points=entradas,
        states=estados,
        fallbacks=entradas + [CommandHandler("cancelar", cancelar)],
    ))

    # Botones viejos o sin conversación activa
    application.add_handler(CallbackQueryHandler(callback_expirado))
    application.add_error_handler(manejar_error)

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
