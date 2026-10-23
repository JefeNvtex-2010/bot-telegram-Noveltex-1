"""Comando /etiqueta REFERENCIA | COLOR | COPIAS   (COPIAS es opcional)
Ejemplo: /etiqueta ABC123 | AZUL MARINO | 2
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import datos
from etiquetas import imprimir_etiqueta

logger = logging.getLogger(__name__)

MAX_COPIAS = 50
AYUDA = (
    "Uso: /etiqueta REFERENCIA | COLOR | COPIAS\n"
    "Ejemplo: /etiqueta ABC123 | AZUL MARINO | 2"
)


async def etiqueta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = " ".join(context.args) if context.args else ""
    partes = [p.strip() for p in texto.split("|")]
    if len(partes) < 2 or not partes[0] or not partes[1]:
        await update.message.reply_text(AYUDA)
        return

    termino, color_in = partes[0].upper(), partes[1]
    try:
        copias = int(partes[2]) if len(partes) > 2 and partes[2] else 1
    except ValueError:
        copias = 1
    copias = min(max(copias, 1), MAX_COPIAS)

    df = await datos.inventario.obtener()
    if df is None:
        await update.message.reply_text("⚠️ Inventario no disponible, intenta en un momento.")
        return

    # Referencia: exacta o única coincidencia
    refs = datos.buscar_referencias(df, termino)
    if termino in refs:
        ref = termino
    elif len(refs) == 1:
        ref = refs[0]
    elif not refs:
        await update.message.reply_text(f"❌ No encontré la referencia {termino}.")
        return
    else:
        await update.message.reply_text(
            "Hay varias coincidencias, sé más específico:\n" + "\n".join(refs[:15])
        )
        return

    # Color: sin importar mayúsculas
    colores = datos.colores_de(df, ref)
    color = next((c for c in colores if c.upper() == color_in.upper()), None)
    if color is None:
        await update.message.reply_text(
            f"❌ {ref} no tiene el color «{color_in}». Colores disponibles:\n" + "\n".join(colores[:20])
        )
        return

    try:
        await asyncio.to_thread(imprimir_etiqueta, ref, color, copias)
        await update.message.reply_text(f"🏷️ Enviada a la impresora: {ref} / {color} x{copias}")
    except Exception:
        logger.exception("No se pudo imprimir la etiqueta")
        await update.message.reply_text("⚠️ No se pudo conectar con la impresora.")
