import logging
import unicodedata

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import datos
from acceso import requiere_acceso
from handlers import editar, esc, programar_borrado, registrar_uso_bot, responder

logger = logging.getLogger(__name__)

# Estados únicos (no chocan con los de handlers.py ni con los de SAP)
SELECCIONANDO_REF_VEND, SELECCIONANDO_COLOR_VEND, ESPERANDO_REF_VEND = 6, 7, 8

MAX_BOTONES = 20   # máximo de referencias en botones
MAX_COLORES = 90   # máximo de colores en botones (Telegram permite 100 por teclado)
MAX_CHARS_MSG = 3500  # Telegram permite 4096; se deja margen para las etiquetas

# --- Alineación de los botones -------------------------------------------
# Espacios invisibles que Telegram no recorta:
ESP_DIGITO = "\u2007"   # ancho de un dígito
ESP_PUNTO = "\u2008"    # ancho de un punto (≈ medio dígito)
SEP_BTN = "\u2009·\u2009"  # separador entre columnas (espacios finos)
PLACEHOLDER_PRECIO = "—"   # lo que se muestra si el color no tiene precio

# Ancho estimado de cada carácter, tomando un dígito = 1.0
# (si los precios quedan corridos, ajusta estos valores)
_ANCHO_MAYUS = {
    "A": 1.16, "B": 1.14, "C": 1.18, "D": 1.16, "E": 1.02, "F": 0.98,
    "G": 1.20, "H": 1.27, "I": 0.48, "J": 0.96, "K": 1.13, "L": 0.96,
    "M": 1.55, "N": 1.27, "O": 1.23, "P": 1.11, "Q": 1.23, "R": 1.13,
    "S": 1.07, "T": 1.07, "U": 1.25, "V": 1.13, "W": 1.61, "X": 1.11,
    "Y": 1.05, "Z": 1.07,
}
_ANCHO_OTROS = {
    " ": 0.45, ",": 0.45, ".": 0.47, "-": 0.50, "·": 0.47,
    "$": 1.00, "—": 1.60, "/": 0.60,
}


def _fmt(valor: float) -> str:
    return f"{int(valor)}" if float(valor).is_integer() else f"{valor:,.2f}"


def _fmt_metros(valor: float) -> str:
    """Siempre con 2 decimales: 7,941.88 / 378.00 / 0.00"""
    return f"{valor:,.2f}"


# ---------------------------------------------------------------------------
# PRECIO CARTON (por color)
# ---------------------------------------------------------------------------
def _norm(texto) -> str:
    """Mayúsculas, sin tildes y sin espacios repetidos, para comparar textos."""
    s = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def _col_precio(df):
    """Busca la columna 'Precio Carton' (tolera tildes y mayúsculas)."""
    for c in df.columns:
        n = _norm(c)
        if "PRECIO" in n and "CARTON" in n:
            return c
    return None


def _a_numero(valor) -> float:
    """Convierte 2990, '2990', '$2,990' o '$ 2.990' en número."""
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor) if valor == valor else 0.0  # descarta NaN
    s = str(valor).replace("$", "").replace(" ", "").strip()
    if not s:
        return 0.0
    if "," in s and "." in s:
        # El último separador es el decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        partes = s.split(sep)
        # '2,990' o '2.990' = miles; '2,5' = decimal
        if len(partes) > 1 and len(partes[-1]) == 3:
            s = s.replace(sep, "")
        else:
            s = s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_precio(valor: float) -> str:
    """2990 -> $2.990"""
    return "$" + f"{valor:,.0f}".replace(",", ".")


def _precios_por_color(df, ref: str, colores) -> dict:
    """Devuelve {color: precio} para los colores de la referencia."""
    col_precio = _col_precio(df)
    if col_precio is None:
        logger.warning(
            "Precio Carton: el inventario no tiene esa columna. Columnas actuales: %s",
            list(df.columns),
        )
        return {}

    filas = df[df["_ref"] == ref]
    if filas.empty:
        return {}

    # Columna que identifica el color de cada fila
    col_color = next((c for c in ("_color", "Color", "COLOR") if c in filas.columns), None)
    col_desc = next(
        (c for c in filas.columns if _norm(c) == "DESCRIPCION"), None
    )

    pares = []  # (clave_color, precio)
    for _, fila in filas.iterrows():
        precio = _a_numero(fila[col_precio])
        if precio <= 0:
            continue
        if col_color:
            clave = _norm(fila[col_color])
        elif col_desc:
            clave = _norm(fila[col_desc])  # ej. "SUPERNOVA - 21 CRUDO"
        else:
            clave = ""
        pares.append((clave, precio))

    resultado = {}
    for color, _disp in colores:
        c = _norm(color)
        precios = [p for k, p in pares if k == c]
        if not precios:
            # La descripción termina en el color: "SUPERNOVA - 21 CRUDO"
            precios = [p for k, p in pares if k.endswith(" " + c)]
        if precios:
            resultado[color] = precios[0]

    sin_precio = [c for c, _ in colores if c not in resultado]
    if sin_precio:
        logger.warning(
            "Precio Carton: %s sin precio en %s (columna de color: %s)",
            ref, sin_precio, col_color or col_desc,
        )
    return resultado


# ---------------------------------------------------------------------------
# TEXTOS (compartir y botones)
# ---------------------------------------------------------------------------
def _texto_color(d: dict, color: str, disp: float) -> str:
    """Texto para compartir: '600 NEGRO - 7,941.88 MTRS - $2.990' (sin relleno)."""
    texto = f"{color} - {_fmt_metros(disp)} MTRS"
    precio = d.get("precios", {}).get(color)
    if precio:
        texto += f" - {_fmt_precio(precio)}"
    return texto


def _ancho(texto: str) -> float:
    """Ancho estimado del texto, en 'dígitos' (1.0 = lo que mide un dígito)."""
    total = 0.0
    for ch in str(texto):
        if ch.isdigit():
            total += 1.0
        elif ch in _ANCHO_OTROS:
            total += _ANCHO_OTROS[ch]
        else:
            base = unicodedata.normalize("NFKD", ch)[:1].upper()
            total += _ANCHO_MAYUS.get(base, 1.10)
    return total


def _relleno(unidades: float) -> str:
    """Espacios invisibles que suman aproximadamente 'unidades' dígitos de ancho."""
    if unidades <= 0:
        return ""
    n = int(unidades)
    resto = unidades - n
    if resto >= 0.72:
        n += 1
    elif resto >= 0.24:
        return ESP_DIGITO * n + ESP_PUNTO
    return ESP_DIGITO * n


def _calcular_anchos(d: dict) -> None:
    """Calcula el ancho de cada columna (en 'dígitos') para alinear los botones."""
    colores = d["colores"]
    precios = d.get("precios", {})
    d["w_color"] = max((_ancho(c) for c, _ in colores), default=0)
    d["w_metros"] = max((_ancho(_fmt_metros(v)) for _, v in colores), default=0)
    if precios:
        d["w_precio"] = max(
            max(_ancho(_fmt_precio(p)) for p in precios.values()),
            _ancho(PLACEHOLDER_PRECIO),
        )
    else:
        d["w_precio"] = 0  # ningún precio: se omite la columna


def _texto_boton(d: dict, color: str, disp: float) -> str:
    """'600 NEGRO · 7,941.88 MTRS · $2.990' con las columnas alineadas."""
    col = f"{color}{_relleno(d['w_color'] - _ancho(color))}"

    m = _fmt_metros(disp)
    met = f"{_relleno(d['w_metros'] - _ancho(m))}{m} MTRS"

    texto = f"{col}{SEP_BTN}{met}"

    if d["w_precio"]:
        precio = d.get("precios", {}).get(color)
        p = _fmt_precio(precio) if precio else PLACEHOLDER_PRECIO
        texto += f"{SEP_BTN}{_relleno(d['w_precio'] - _ancho(p))}{p}"
    return texto


# ---------------------------------------------------------------------------
# PANTALLAS
# ---------------------------------------------------------------------------
def _pantalla_refs(d: dict):
    texto = f"🔍 Encontré varias referencias para <b>{esc(d['termino'])}</b>.\nSelecciona una referencia:"
    if d["total"] > len(d["refs"]):
        texto += f"\n(Mostrando {len(d['refs'])} de {d['total']}; escribe una referencia más específica)"
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton(ref, callback_data=f"vend_ref_{i}")] for i, ref in enumerate(d["refs"])
    ])
    return texto, teclado


def _pantalla_colores(d: dict, df):
    # (color, disponible en bodega 01) calculado de forma vectorizada
    d["colores"] = datos.disponibilidad_bodega01(df, d["ref"])[:MAX_COLORES]
    # {color: precio carton}
    d["precios"] = _precios_por_color(df, d["ref"], d["colores"])
    _calcular_anchos(d)

    filas = [
        [InlineKeyboardButton(_texto_boton(d, color, disp), callback_data=f"vend_col_{i}")]
        for i, (color, disp) in enumerate(d["colores"])
    ]
    if len(d["refs"]) > 1:
        filas.append([InlineKeyboardButton("🔙 Volver a referencias", callback_data="vend_back_refs")])
    # Botón final: convierte la lista a texto para copiar/reenviar
    filas.append([InlineKeyboardButton("📋 Clic para compartir", callback_data="vend_share")])

    texto = (
        f"🔍 Referencia: <b>{esc(d['ref'])}</b>.\n"
        "Selecciona un color:\n"
        "<i>Color · Metros · Precio Carton</i>"
    )
    return texto, InlineKeyboardMarkup(filas)


def _lineas_compartir(d: dict) -> list:
    """Una línea por color, en texto plano."""
    return [_texto_color(d, color, disp) for color, disp in d["colores"]]


def _mensajes_compartir(d: dict) -> list:
    """Texto plano listo para copiar/reenviar. Si es muy largo, se divide en varios mensajes."""
    encabezado = f"🔍 Referencia: <b>{esc(d['ref'])}</b> (Bodega 01)\n\n"
    lineas = _lineas_compartir(d)

    partes, actual, largo = [], [], 0
    for linea in lineas:
        if actual and largo + len(linea) + 1 > MAX_CHARS_MSG:
            partes.append(actual)
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + 1
    if actual:
        partes.append(actual)

    mensajes = []
    for i, bloque in enumerate(partes):
        cuerpo = esc("\n".join(bloque))
        cabecera = encabezado if i == 0 else f"🔍 <b>{esc(d['ref'])}</b> (continuación)\n\n"
        mensajes.append(cabecera + cuerpo)
    return mensajes


# ---------------------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------------------
@requiere_acceso("inventario_vendedor")
async def iniciar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        registrar_uso_bot(update, "BOTÓN_INVENTARIO_VENDEDOR")
        await editar(
            query,
            "🛒 <b>Inventario Vendedor (Bodega 01)</b>\n\nEscribe la <b>referencia</b> que deseas buscar:",
        )
        return ESPERANDO_REF_VEND

    registrar_uso_bot(update, "/iv")
    programar_borrado(context, update.message)

    if not context.args:
        await responder(
            update,
            context,
            "🛒 <b>Inventario Vendedor (Bodega 01)</b>\n\nEscribe la <b>referencia</b> que deseas buscar "
            "(o usa <code>/iv VERMONT</code>):",
        )
        return ESPERANDO_REF_VEND

    return await ejecutar_busqueda_vendedor(update, context, " ".join(context.args))


async def recibir_texto_ref_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    programar_borrado(context, update.message)
    return await ejecutar_busqueda_vendedor(update, context, update.message.text)


async def ejecutar_busqueda_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE, termino: str) -> int:
    termino = termino.strip().upper()
    if not termino:
        await responder(update, context, "⚠️ Escribe la referencia a buscar.")
        return ESPERANDO_REF_VEND

    if datos.inventario.df is None:  # solo en la primera carga tras arrancar
        await responder(update, context, "⏳ Cargando inventario, un momento…")

    df = await datos.inventario.obtener()
    if df is None:
        detalle = f"\n<code>{esc(datos.inventario.ultimo_error)}</code>" if datos.inventario.ultimo_error else ""
        await responder(update, context, f"⚠️ No se pudo acceder al inventario.{detalle}")
        return ConversationHandler.END

    refs = datos.buscar_referencias(df, termino)
    if not refs:
        await responder(
            update,
            context,
            f"❌ No se encontraron referencias para: <b>{esc(termino)}</b>.\nPuedes escribir otra referencia.",
        )
        return ESPERANDO_REF_VEND

    d = {"termino": termino, "refs": refs[:MAX_BOTONES], "total": len(refs)}
    context.user_data["vend"] = d

    if len(refs) == 1:
        d["ref"] = refs[0]
        texto, teclado = _pantalla_colores(d, df)
        await responder(update, context, texto, reply_markup=teclado)
        return SELECCIONANDO_COLOR_VEND

    texto, teclado = _pantalla_refs(d)
    await responder(update, context, texto, reply_markup=teclado)
    return SELECCIONANDO_REF_VEND


async def _expirada(query) -> int:
    await editar(query, "⌛ Esta consulta expiró. Escribe /i para empezar de nuevo.")
    return ConversationHandler.END


async def seleccionar_referencia_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data.get("vend")
    df = await datos.inventario.obtener()
    if not d or df is None:
        return await _expirada(query)
    try:
        d["ref"] = d["refs"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        return await _expirada(query)

    texto, teclado = _pantalla_colores(d, df)
    await editar(query, texto, teclado)
    return SELECCIONANDO_COLOR_VEND


async def _enviar_compartir(context, chat_id: int, d: dict) -> None:
    """Envía los botones convertidos en texto. Si Telegram rechaza el formato, lo manda en texto plano."""
    try:
        for texto in _mensajes_compartir(d):
            msg = await context.bot.send_message(
                chat_id=chat_id, text=texto, parse_mode=ParseMode.HTML
            )
            programar_borrado(context, msg)  # se borra a las 24 h como los demás mensajes
    except Exception:
        logger.exception("Falló el envío con formato de 'Clic para compartir'; se envía en texto plano.")
        plano = f"Referencia: {d['ref']} (Bodega 01)\n\n" + "\n".join(_lineas_compartir(d))
        for i in range(0, len(plano), 4000):
            msg = await context.bot.send_message(chat_id=chat_id, text=plano[i:i + 4000])
            programar_borrado(context, msg)


async def seleccionar_color_vendedor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    d = context.user_data.get("vend")
    if not d or "ref" not in d:
        await query.answer()
        return await _expirada(query)

    if query.data == "vend_back_refs":
        await query.answer()
        texto, teclado = _pantalla_refs(d)
        await editar(query, texto, teclado)
        return SELECCIONANDO_REF_VEND

    if query.data == "vend_share":
        await query.answer()
        await _enviar_compartir(context, query.message.chat_id, d)
        return SELECCIONANDO_COLOR_VEND

    try:
        color, disp = d["colores"][int(query.data.rsplit("_", 1)[1])]
    except (ValueError, IndexError):
        await query.answer()
        return await _expirada(query)

    precio = d.get("precios", {}).get(color)
    linea_precio = f"\nPrecio Carton: {_fmt_precio(precio)}" if precio else ""

    # Un solo query.answer() (antes se llamaba dos veces y el aviso nunca se mostraba)
    await query.answer(
        text=f"{d['ref']} - {color}\nDisponible en bodega 01: {_fmt_metros(disp)} MTRS{linea_precio}",
        show_alert=True,
    )
    return SELECCIONANDO_COLOR_VEND
