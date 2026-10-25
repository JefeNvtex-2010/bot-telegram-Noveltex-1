"""Control de acceso a las funciones del bot (por ID de usuario de Telegram).

Los permisos salen de una hoja de Google Sheets (pestaña PERMISOS del archivo
AUDITORIA_BOT) y, opcionalmente, de variables de entorno. Ambas fuentes se suman.

  HOJA DE GOOGLE (recomendado)
    ACCESO_SHEET_URL     Enlace CSV de la pestaña PERMISOS publicada en la web
                         (Archivo > Compartir > Publicar en la web > PERMISOS > .csv).
    ACCESO_SHEET_ID      Solo si no usas ACCESO_SHEET_URL: ID del archivo de Google
                         (por defecto el de AUDITORIA_BOT). Requiere que el archivo
                         esté compartido como "Cualquier persona con el enlace".
    ACCESO_HOJA          Nombre de la pestaña (por defecto PERMISOS).
    ACCESO_REFRESCO_MIN  Cada cuántos minutos se vuelve a leer (por defecto 5).

    Columnas (se buscan por el nombre del encabezado, el orden no importa):
      grupo | ID Telegram | acceso a

    En "acceso a", separa con coma. Frases reconocidas:
      inventario sap (/in) | pedidos pg (/pg) | inventario vendedor (/iv)
      guias (/gu) | etiquetas (/etiqueta) | pedidos pv (/pv) | TODO

  VARIABLES DE ENTORNO (opcionales, respaldo fijo)
    ADMIN_IDS        IDs con acceso a TODO, separados por coma.
    ACCESO_USUARIOS  id:funcion1,funcion2;id:*;...

Nombres internos de función (los que usa @requiere_acceso):
  PV, PG, guias, etiquetas, inventario, inventario_vendedor

Lo que no está autorizado queda oculto: no aparece el botón en /i y, si lo escriben
a mano, el bot responde "no disponible".

La lista de comandos "/" de Telegram (el botón de menú) solo la ven los MASTER
(los que tienen TODO en la hoja, ADMIN_IDS y el dueño). Los demás usuarios no ven
ninguna lista: entran escribiendo /i y usan los botones que tengan autorizados.
"""
import asyncio
import csv
import functools
import io
import logging
import os
import re
import unicodedata

import requests
from telegram import BotCommand, BotCommandScopeChat, Update
from telegram.ext import Application, ContextTypes

log = logging.getLogger(__name__)

MENSAJE_SIN_ACCESO = "Esa opción no está disponible."

ACCESO_SHEET_URL = os.environ.get("ACCESO_SHEET_URL", "").strip()
ACCESO_SHEET_ID = os.environ.get(
    "ACCESO_SHEET_ID", "1Si6SMJ7p4DlDugBCxV6ATGicHWMFQNMZtCNQ409qppk"
).strip()
ACCESO_HOJA = os.environ.get("ACCESO_HOJA", "PERMISOS").strip() or "PERMISOS"
try:
    REFRESCO_MIN = max(1, int(os.environ.get("ACCESO_REFRESCO_MIN", "5")))
except ValueError:
    REFRESCO_MIN = 5

# Frase de la columna "acceso a" (sin tildes, minúsculas, sin "/") -> permiso interno.
ALIAS_FUNCIONES = {
    "todo": "*",
    "*": "*",
    "pv": "pv",
    "pedidos pv": "pv",
    "consulta pedidos": "pv",
    "pg": "pg",
    "pedidos pg": "pg",
    "pedidos": "pg",
    "in": "inventario",
    "inventario": "inventario",
    "inventario sap": "inventario",
    "sap": "inventario",
    "iv": "inventario_vendedor",
    "inventario vendedor": "inventario_vendedor",
    "vendedor": "inventario_vendedor",
    "gu": "guias",
    "guia": "guias",
    "guias": "guias",
    "et": "etiquetas",
    "etiqueta": "etiquetas",
    "etiquetas": "etiquetas",
}

# --------------------------------------------------------------------------- #
# Comandos que aparecen en la lista "/" de Telegram.
# Los nombres de comando van en minúsculas (Telegram no admite mayúsculas aquí;
# escribir /PV igual funciona).
# --------------------------------------------------------------------------- #
COMANDOS_BASE = [
    BotCommand("i", "Menú principal"),
    BotCommand("miid", "Ver mi ID de Telegram"),
]

COMANDOS_POR_FUNCION = {
    "PV": [BotCommand("pv", "Consultar pedidos")],
    "PG": [BotCommand("pg", "Pedidos")],
    "guias": [BotCommand("gu", "Guías")],
    "inventario": [BotCommand("in", "Inventario SAP")],
    "inventario_vendedor": [BotCommand("iv", "Inventario vendedor")],
    "etiquetas": [BotCommand("etiqueta", "Etiquetas")],
}

COMANDOS_ADMIN = [
    BotCommand("refrescar", "Recargar datos de Google Sheets"),
    BotCommand("accesos", "Recargar la hoja de accesos"),
]


# --------------------------------------------------------------------------- #
# Carga de permisos
# --------------------------------------------------------------------------- #
def _cargar_admins() -> set:
    ids = set()
    for parte in os.environ.get("ADMIN_IDS", "").split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.add(int(parte))
    return ids


def _cargar_usuarios_env() -> dict:
    """Devuelve {user_id: set(funciones)}; el set puede contener '*'."""
    usuarios = {}
    for bloque in os.environ.get("ACCESO_USUARIOS", "").split(";"):
        bloque = bloque.strip()
        if not bloque or ":" not in bloque:
            continue
        uid, funciones = bloque.split(":", 1)
        uid = uid.strip()
        if not uid.isdigit():
            continue
        usuarios[int(uid)] = {f.strip().lower() for f in funciones.split(",") if f.strip()}
    return usuarios


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn"
    )


def _permiso_de(item: str):
    """Convierte una frase ('inventario vendedor', '/pg', ...) en un permiso interno."""
    t = _sin_acentos(item).lower().strip().lstrip("/").strip()
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    if t in ALIAS_FUNCIONES:
        return ALIAS_FUNCIONES[t]

    # Respaldo por palabras clave (el orden importa: "vendedor" antes que "sap")
    palabras = t.split()
    if "todo" in palabras:
        return "*"
    if "vendedor" in t:
        return "inventario_vendedor"
    if "sap" in palabras or t.startswith("inventario"):
        return "inventario"
    if "guia" in t:
        return "guias"
    if "etiqueta" in t:
        return "etiquetas"
    if "pv" in palabras:
        return "pv"
    if "pg" in palabras or t.startswith("pedido"):
        return "pg"
    return None


def _parsear_permisos(texto: str) -> set:
    """Convierte 'pedidos pg, inventario sap, guias' o '/pg - /in' en un set de permisos."""
    texto = (texto or "").strip()
    if not texto:
        return set()
    permisos = set()
    for item in re.split(r"\s*[,;\n|]\s*|\s+-\s+|\s+y\s+", texto, flags=re.IGNORECASE):
        item = item.strip()
        if not item:
            continue
        permiso = _permiso_de(item)
        if permiso:
            permisos.add(permiso)
        else:
            log.warning("Acceso: no reconozco '%s' en la hoja de accesos", item)
    return permisos


def _a_id(texto: str):
    digitos = re.sub(r"\D", "", str(texto))
    return int(digitos) if len(digitos) >= 5 else None


def _fuente():
    """(url, params) de donde se descarga la hoja en CSV."""
    if ACCESO_SHEET_URL:
        return ACCESO_SHEET_URL, None
    return (
        f"https://docs.google.com/spreadsheets/d/{ACCESO_SHEET_ID}/gviz/tq",
        {"tqx": "out:csv", "sheet": ACCESO_HOJA, "headers": "1"},
    )


def _leer_sheet():
    """Descarga el CSV y devuelve ({user_id: permisos}, {user_id con TODO})."""
    url, params = _fuente()
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    texto = r.content.decode("utf-8-sig")

    if texto.lstrip().lower().startswith(("<!doctype", "<html")):
        raise RuntimeError(
            "La hoja de accesos no es pública: publica la pestaña PERMISOS en la web "
            "(.csv) y usa ese enlace en ACCESO_SHEET_URL."
        )

    filas = list(csv.reader(io.StringIO(texto)))
    if not filas:
        return {}, set()

    # Columnas por nombre de encabezado (si no se encuentran: grupo, ID, acceso)
    encabezado = [_sin_acentos(c).lower().strip() for c in filas[0]]

    def _indice(clave: str, defecto: int) -> int:
        for i, nombre in enumerate(encabezado):
            if clave in nombre:
                return i
        return defecto

    i_id = _indice("id", 1)
    i_acceso = _indice("acceso", 2)

    usuarios, masters = {}, set()
    for fila in filas:
        if len(fila) <= max(i_id, i_acceso):
            continue
        uid = _a_id(fila[i_id])
        if uid is None:  # encabezado o fila vacía
            continue
        permisos = _parsear_permisos(fila[i_acceso])
        if not permisos:
            continue
        if "*" in permisos:
            masters.add(uid)
        usuarios.setdefault(uid, set()).update(permisos)
    return usuarios, masters


ADMIN_IDS = _cargar_admins()  # no se reasigna: handlers.py le agrega al dueño
USUARIOS_ENV = _cargar_usuarios_env()
USUARIOS = {u: set(p) for u, p in USUARIOS_ENV.items()}  # env + hoja
MASTERS_SHEET = set()
_CONFIGURADOS = set()
_ESTADO = {"firma": None}


async def recargar_accesos() -> bool:
    """Vuelve a leer la hoja de accesos. Devuelve True si pudo."""
    try:
        usuarios, masters = await asyncio.to_thread(_leer_sheet)
    except Exception as e:
        log.error("No se pudo leer la hoja de accesos: %s", e)
        return False

    nuevo = {u: set(p) for u, p in USUARIOS_ENV.items()}
    for uid, permisos in usuarios.items():
        nuevo.setdefault(uid, set()).update(permisos)

    USUARIOS.clear()
    USUARIOS.update(nuevo)
    MASTERS_SHEET.clear()
    MASTERS_SHEET.update(masters)
    log.info("Accesos cargados: %s usuarios (%s con acceso total)", len(USUARIOS), len(MASTERS_SHEET))
    return True


# --------------------------------------------------------------------------- #
# Consultas de permisos
# --------------------------------------------------------------------------- #
def es_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in MASTERS_SHEET


def tiene_acceso(user_id: int, funcion: str) -> bool:
    if es_admin(user_id):
        return True
    permisos = USUARIOS.get(user_id, set())
    return "*" in permisos or funcion.lower() in permisos


def requiere_acceso(funcion: str):
    """Decorador para handlers async de python-telegram-bot.

    Uso:
        @requiere_acceso("etiquetas")
        async def cmd_etiqueta(update, context): ...

    Si no hay acceso devuelve None: dentro de un ConversationHandler eso significa
    que la conversación no arranca (o se queda en el estado en que estaba).
    """

    def decorador(handler):
        @functools.wraps(handler)
        async def envoltura(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            usuario = update.effective_user
            uid = usuario.id if usuario else 0

            if usuario and tiene_acceso(uid, funcion):
                return await handler(update, context, *args, **kwargs)

            log.warning(
                "Acceso denegado: user_id=%s nombre=%s funcion=%s",
                uid,
                usuario.full_name if usuario else "?",
                funcion,
            )
            if update.callback_query:
                await update.callback_query.answer(MENSAJE_SIN_ACCESO, show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(MENSAJE_SIN_ACCESO)
            return None

        return envoltura

    return decorador


# --------------------------------------------------------------------------- #
# Lista "/" de Telegram por persona
# --------------------------------------------------------------------------- #
def comandos_de(user_id: int) -> list:
    """Lista de comandos que debe ver esta persona en el menú "/" de Telegram.

    Solo los MASTER ven la lista (todos los comandos). Para el resto devuelve [],
    lo que en Telegram significa "sin lista de comandos".
    """
    if not es_admin(user_id):
        return []

    comandos = list(COMANDOS_BASE)
    for lista in COMANDOS_POR_FUNCION.values():
        comandos.extend(lista)
    comandos.extend(COMANDOS_ADMIN)

    vistos, unicos = set(), []
    for c in comandos:
        if c.command not in vistos:
            vistos.add(c.command)
            unicos.append(c)
    return unicos


def _firma():
    return (
        frozenset((u, frozenset(p)) for u, p in USUARIOS.items()),
        frozenset(MASTERS_SHEET),
        frozenset(ADMIN_IDS),
    )


async def _aplicar_comandos(bot, forzar: bool = False) -> None:
    """Fija la lista "/" de cada persona. Solo si los permisos cambiaron (o forzar)."""
    firma = _firma()
    if not forzar and firma == _ESTADO["firma"]:
        return

    objetivos = set(USUARIOS) | set(ADMIN_IDS) | set(MASTERS_SHEET)
    for uid in sorted(objetivos):
        try:
            scope = BotCommandScopeChat(chat_id=uid)
            lista = comandos_de(uid)
            if lista:
                await bot.set_my_commands(lista, scope=scope)
            else:
                # Sin lista: se borra la que tuviera (queda sin botón de menú)
                await bot.delete_my_commands(scope=scope)
        except Exception as e:
            # Pasa si esa persona aún no ha escrito nunca al bot
            log.warning("No se pudo fijar los comandos de %s: %s", uid, e)

    # Personas que ya no están en la hoja: se les borra la lista que tuvieran
    for uid in sorted(_CONFIGURADOS - objetivos):
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
        except Exception as e:
            log.warning("No se pudo restablecer los comandos de %s: %s", uid, e)

    _CONFIGURADOS.clear()
    _CONFIGURADOS.update(objetivos)
    _ESTADO["firma"] = firma


async def _job_recarga(context: ContextTypes.DEFAULT_TYPE) -> None:
    if await recargar_accesos():
        await _aplicar_comandos(context.bot)


async def configurar_comandos(application: Application) -> None:
    """Úsalo como post_init del Application.

    Lee la hoja de accesos, fija la lista "/" de cada persona y programa la
    recarga automática cada ACCESO_REFRESCO_MIN minutos.
    """
    await recargar_accesos()

    try:
        # Lista por defecto vacía: quien no es MASTER no ve lista de comandos
        await application.bot.delete_my_commands()
    except Exception as e:
        log.error("No se pudo vaciar la lista de comandos por defecto: %s", e)

    await _aplicar_comandos(application.bot, forzar=True)

    if application.job_queue:
        application.job_queue.run_repeating(
            _job_recarga,
            interval=REFRESCO_MIN * 60,
            first=REFRESCO_MIN * 60,
            name="recarga_accesos",
        )
    else:
        log.warning("No hay job_queue: la hoja de accesos solo se lee al arrancar (usa /accesos).")


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
async def recargar_accesos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/accesos: relee la hoja de accesos ya mismo. Solo administradores."""
    usuario = update.effective_user
    mensaje = update.effective_message
    if not usuario or not mensaje:
        return
    if not es_admin(usuario.id):
        await mensaje.reply_text(MENSAJE_SIN_ACCESO)
        return
    if await recargar_accesos():
        await _aplicar_comandos(context.bot, forzar=True)
        await mensaje.reply_text(f"✅ Accesos recargados: {len(USUARIOS)} usuarios.")
    else:
        await mensaje.reply_text("⚠️ No se pudo leer la hoja de accesos. Revisa el log.")


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /miid: cada persona ve su ID para pasárselo al administrador.
    No lleva restricción a propósito."""
    usuario = update.effective_user
    if update.effective_message and usuario:
        await update.effective_message.reply_text(f"Tu ID de Telegram es: {usuario.id}")
