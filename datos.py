import asyncio
import hashlib
import io
import logging
import os
import re
import time
import unicodedata
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

URL_PEDIDOS = os.environ.get(
    "URL_PEDIDOS",
    "https://docs.google.com/spreadsheets/d/1EOGz7ix9Z1AufTJ-79TWHgiM9iN65LAf/export?format=csv",
)
URL_INVENTARIO = os.environ.get(
    "URL_INVENTARIO",
    "https://docs.google.com/spreadsheets/d/1FJdfaNhxcFFDVD_AV2lTITHw0f-mB0Y2/export?format=csv",
)

# --------------------------------------------------------------------------- #
# Catálogo de etiquetas: pestaña "Listado precios" del Google Sheet.
#
# Columnas que lee:
#   CODIGO       ej. 0018-200                  -> el item es lo que está antes del guion (0018)
#   REFERENCIA   ej. SATIN MARBELLA            -> nombre de la referencia (solo el nombre)
#   COLOR        ej. 200 ROJO                  -> código + nombre del color
#   DESCRIPCION  ej. SATIN MARBELLA - 200 ROJO -> solo se usa de respaldo
#
# Prioridad: si la hoja trae las columnas REFERENCIA y COLOR con valor en la fila, se
# usan tal cual. Si una fila las trae vacías (o con error, ej. #VALOR!), esa fila se
# separa a partir de la DESCRIPCION.
#
# Solo lo usan las ETIQUETAS. El resto de módulos (inventario SAP, vendedor, ubicaciones,
# transferencias) siguen usando el inventario de URL_INVENTARIO.
#
# - El archivo (ID) sale de URL_INVENTARIO, salvo que definas URL_CATALOGO en Render
#   (enlace CSV completo de la pestaña, si está en otro archivo).
# - El nombre de la pestaña sale de PESTANA_CATALOGO (por defecto "Listado precios");
#   debe escribirse exactamente igual que en el Google Sheet.
# --------------------------------------------------------------------------- #
PESTANA_CATALOGO = os.environ.get("PESTANA_CATALOGO", "Listado precios").strip()


def _url_de_pestana(url_base: str, pestana: str) -> str:
    """Arma el enlace CSV de una pestaña, tomando el ID del archivo de url_base."""
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url_base)
    if not m:
        return url_base
    return (
        f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
        f"/gviz/tq?tqx=out:csv&sheet={quote(pestana)}"
    )


URL_CATALOGO = os.environ.get("URL_CATALOGO", "").strip() or _url_de_pestana(
    URL_INVENTARIO, PESTANA_CATALOGO
)

INTERVALO_REFRESCO = int(os.environ.get("INTERVALO_REFRESCO", "120"))  # segundos
EDAD_MAXIMA = 600  # si los datos tienen más de esto, se fuerza un refresco en segundo plano

# Una sola sesión HTTP reutilizable (mantiene la conexión abierta) con reintentos.
_session = requests.Session()
_reintentos = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",),
)
_session.mount("https://", HTTPAdapter(max_retries=_reintentos, pool_connections=2, pool_maxsize=4))


# --------------------------------------------------------------------------- #
# Preparación de las hojas (se ejecuta una sola vez por descarga)
# --------------------------------------------------------------------------- #
def _a_numero(serie: pd.Series) -> pd.Series:
    return pd.to_numeric(
        serie.astype(str).str.strip().str.replace(",", ".", regex=False),
        errors="coerce",
    )


def normalizar_texto(texto: str) -> str:
    """Mayúsculas, sin tildes y con espacios simples (para comparar búsquedas)."""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t).strip().upper()


def _normalizar_serie(serie: pd.Series) -> pd.Series:
    """Igual que normalizar_texto, pero calculado una sola vez por cada valor distinto.

    La misma referencia se repite en muchas filas (un color por fila), así que
    normalizar solo los valores únicos es mucho más rápido.
    """
    s = serie.astype(str)
    mapa = {valor: normalizar_texto(valor) for valor in s.unique()}
    return s.map(mapa)


def _preparar_pedidos(df: pd.DataFrame) -> pd.DataFrame:
    if "Documento Pd" in df.columns:
        df["Documento Pd"] = df["Documento Pd"].astype(str).str.split(".").str[0].str.strip()
    for col in ("Id Refer", "Color"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    return df


def _preparar_inventario(df: pd.DataFrame) -> pd.DataFrame:
    col_ref = next((c for c in df.columns if "REF" in c.upper()), None)
    col_color = next((c for c in df.columns if "COLOR" in c.upper()), None)
    if not col_ref or not col_color:
        raise ValueError("No se encontraron las columnas REFERENCIA o COLOR en el inventario")

    df["_ref"] = df[col_ref].astype(str).str.strip().str.upper()
    df["_ref_norm"] = _normalizar_serie(df[col_ref])  # solo para buscar
    df["_color"] = df[col_color].astype(str).str.strip()

    sin_dato = pd.Series(float("nan"), index=df.index)
    stock = (_a_numero(df["Stock"]) if "Stock" in df.columns else sin_dato).fillna(0.0)
    comprometido = (_a_numero(df["Comprometido"]) if "Comprometido" in df.columns else sin_dato).fillna(0.0)
    disponible = _a_numero(df["Disponible"]) if "Disponible" in df.columns else sin_dato
    # Si "Disponible" viene vacío o inválido: stock - comprometido (misma lógica de antes)
    df["_disp"] = disponible.fillna(stock - comprometido)

    # Bodega 01: primera columna de código/almacén con valor; si no hay, se asume "1".
    cols_alm = [c for c in df.columns if any(k in c.upper() for k in ("CÓDIGO", "ALMACÉN", "ALMACEN"))]
    if cols_alm:
        alm = pd.Series([None] * len(df), index=df.index, dtype=object)
        for c in cols_alm:
            s = df[c].astype(str).str.strip()
            s = s.where(~(s.eq("") | s.str.lower().eq("nan")))
            alm = alm.fillna(s)
        alm = alm.fillna("1")
        df["_b01"] = alm.isin(["1", "01"])
    else:
        df["_b01"] = True
    return df


# DESCRIPCION: "SATIN MARBELLA - 200 ROJO" -> ref "SATIN MARBELLA", color "200 ROJO".
# Se prueba de lo más estricto a lo más flexible:
#   1) " - " con espacios a ambos lados, seguido de un número (el código del color)
#   2) cualquier guion (-, –, —) con o sin espacios, seguido de un número
#   3) sin guion: se corta donde empieza el número del color que indica el CODIGO
_RE_DESC_ESTRICTA = r"^(?P<ref>.+?)\s+[-–—]\s+(?P<color>\d.*)$"
_RE_DESC_SUELTA = r"^(?P<ref>.+?)\s*[-–—]\s*(?P<color>\d.*)$"


def _separar_por_sufijo(descripcion: str, sufijo: str):
    """Para descripciones sin guion: 'SATIN CIPRIANI 10 OPTICAL WHITE' + sufijo '10'."""
    m = re.match(r"^(\d+)", str(sufijo))
    if not m:
        return None
    num = m.group(1).lstrip("0") or "0"
    patron = re.compile(r"^(?P<ref>.+?)\s+(?P<color>0*" + re.escape(num) + r"\b.*)$")
    r = patron.match(str(descripcion))
    return (r.group("ref"), r.group("color")) if r else None


def _valida(serie: pd.Series) -> pd.Series:
    """Texto limpio; vacío, 'nan' o errores de fórmula (#VALOR!, #N/A...) pasan a NaN."""
    s = serie.astype(str).str.strip()
    return s.where(~(s.eq("") | s.str.startswith("#") | s.str.lower().eq("nan")))


def _preparar_catalogo(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte la pestaña 'Listado precios' en el mismo formato que usa el resto del
    bot: _ref, _ref_norm, _color y Artículo."""
    cols = {normalizar_texto(c): c for c in df.columns}
    col_cod = next((cols[k] for k in ("CODIGO", "ARTICULO", "CODIGO ARTICULO", "ITEM") if k in cols), None)
    col_desc = next((c for k, c in cols.items() if k.startswith("DESCRIP")), None)
    col_ref_h = cols.get("REFERENCIA")
    col_color_h = cols.get("COLOR")

    if not col_cod or not (col_desc or (col_ref_h and col_color_h)):
        raise ValueError(
            "En la pestaña del catálogo no encontré CODIGO junto con DESCRIPCION "
            "(o REFERENCIA y COLOR). "
            f"Columnas leídas: {', '.join(map(str, df.columns))}"
        )

    codigo = df[col_cod].astype(str).str.strip()
    desc = df[col_desc].astype(str).str.strip() if col_desc else pd.Series("", index=df.index)
    sufijo = codigo.str.split("-", n=1).str[1].fillna("").str.strip()  # 0018-200 -> 200

    # 1) Columnas REFERENCIA y COLOR de la hoja (prioridad)
    ref_h = _valida(df[col_ref_h]) if col_ref_h else pd.Series(float("nan"), index=df.index)
    color_h = _valida(df[col_color_h]) if col_color_h else pd.Series(float("nan"), index=df.index)

    # 2) Separar la DESCRIPCION (respaldo para filas sin REFERENCIA o COLOR)
    partes = desc.str.extract(_RE_DESC_ESTRICTA)
    suelta = desc.str.extract(_RE_DESC_SUELTA)
    ref_p = partes["ref"].fillna(suelta["ref"])
    color_p = partes["color"].fillna(suelta["color"])

    # 3) Descripciones sin guion: se corta por el número del color del CODIGO
    pendientes = ref_p.isna() & desc.ne("")
    for i in ref_p.index[pendientes]:
        r = _separar_por_sufijo(desc[i], sufijo[i])
        if r:
            ref_p[i], color_p[i] = r

    # Por fila: la columna de la hoja manda; si falta, lo separado de la descripción;
    # y si tampoco hay, la descripción completa / el sufijo del código.
    ref = ref_h.fillna(ref_p).fillna(desc)
    color = color_h.fillna(color_p).fillna(sufijo)

    out = pd.DataFrame(
        {
            "Artículo": codigo,
            "Descripción": desc,
            "_ref": ref.astype(str).str.strip().str.upper(),
            "_color": color.astype(str).str.strip(),
            "_suf": sufijo,
        }
    )
    out = out[(out["Artículo"] != "") & (out["_ref"] != "")].reset_index(drop=True)
    out["_ref_norm"] = _normalizar_serie(out["_ref"])

    # Avisos en el log (no detienen el bot)
    num_color = out["_color"].str.extract(r"^(\d+)")[0].str.lstrip("0")
    num_cod = out["_suf"].str.extract(r"^(\d+)")[0].str.lstrip("0")
    distintos = num_color.notna() & num_cod.notna() & (num_color != num_cod)
    if distintos.any():
        ejemplos = out.loc[distintos, ["Artículo", "_ref", "_color"]].head(5).values.tolist()
        logger.warning(
            f"⚠️ catálogo: {int(distintos.sum())} filas donde el número del COLOR no "
            f"coincide con el CODIGO. Ejemplos: {ejemplos}"
        )
    con_numero = out["_ref"].str.contains(r"\d{2,}", regex=True) & out["_color"].eq(out["_suf"])
    if con_numero.any():
        ejemplos = out.loc[con_numero, ["Artículo", "_ref"]].head(5).values.tolist()
        logger.warning(
            f"⚠️ catálogo: {int(con_numero.sum())} referencias parecen incluir el color en el "
            f"nombre (revisa REFERENCIA/DESCRIPCION). Ejemplos: {ejemplos}"
        )
    return out.drop(columns=["_suf"])


# --------------------------------------------------------------------------- #
# Fuente con caché "stale-while-revalidate"
# --------------------------------------------------------------------------- #
class FuenteCSV:
    def __init__(self, nombre: str, url: str, preparar):
        self.nombre = nombre
        self.url = url
        self._preparar = preparar
        self.df = None
        self.timestamp = 0.0
        self.ultimo_error = None
        self._huella = None  # huella del último archivo procesado (para saltar si no cambió)
        self._lock = None  # se crea perezosamente dentro del event loop

    def _candado(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _descargar(self):
        """Descarga la hoja. Devuelve None si el archivo es idéntico al anterior."""
        t0 = time.time()
        r = _session.get(self.url, timeout=(5, 25))
        r.raise_for_status()

        # Si la hoja no está compartida, Google devuelve una página HTML de login
        if r.content.lstrip()[:1] == b"<":
            raise RuntimeError(
                f"La hoja de {self.nombre} no es accesible (revisa que esté compartida "
                "con 'cualquier persona con el enlace' y que la pestaña exista)."
            )

        huella = hashlib.md5(r.content, usedforsecurity=False).hexdigest()
        if self.df is not None and huella == self._huella:
            logger.info(f"♻️ {self.nombre}: sin cambios ({time.time() - t0:.1f}s)")
            return None  # no se vuelve a leer ni a preparar

        df = pd.read_csv(io.BytesIO(r.content), dtype=str, keep_default_na=False, encoding="utf-8-sig")
        df.columns = df.columns.str.strip()
        df = self._preparar(df)
        self._huella = huella
        logger.info(f"📥 {self.nombre}: {len(df)} filas descargadas en {time.time() - t0:.1f}s")
        return df

    async def _cargar(self) -> bool:
        try:
            df = await asyncio.wait_for(asyncio.to_thread(self._descargar), timeout=45)
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"⚠️ Error cargando {self.nombre}: {self.ultimo_error}")
            return False  # si ya había datos, se conservan los anteriores
        if df is not None:  # None = la hoja no cambió: se conservan los datos actuales
            self.df = df
        self.timestamp = time.time()
        self.ultimo_error = None
        return True

    async def refrescar(self) -> bool:
        async with self._candado():
            return await self._cargar()

    async def obtener(self):
        """Devuelve el DataFrame al instante. Solo espera si todavía no hay datos."""
        if self.df is None:
            async with self._candado():
                if self.df is None:
                    await self._cargar()
        elif time.time() - self.timestamp > EDAD_MAXIMA and not self._candado().locked():
            _lanzar(self.refrescar())
        return self.df

    def invalidar(self) -> None:
        self.df = None
        self.timestamp = 0.0
        self._huella = None


pedidos = FuenteCSV("pedidos", URL_PEDIDOS, _preparar_pedidos)
inventario = FuenteCSV("inventario", URL_INVENTARIO, _preparar_inventario)
catalogo = FuenteCSV("catálogo", URL_CATALOGO, _preparar_catalogo)  # solo para etiquetas


# --------------------------------------------------------------------------- #
# Refresco en segundo plano
# --------------------------------------------------------------------------- #
_tareas = set()


def _lanzar(coro) -> asyncio.Task:
    tarea = asyncio.get_running_loop().create_task(coro)
    _tareas.add(tarea)
    tarea.add_done_callback(_tareas.discard)
    return tarea


async def refrescar_todo() -> None:
    await asyncio.gather(pedidos.refrescar(), inventario.refrescar(), catalogo.refrescar())


def invalidar_todo() -> None:
    pedidos.invalidar()
    inventario.invalidar()
    catalogo.invalidar()


async def _bucle_refresco() -> None:
    await refrescar_todo()  # precarga al arrancar
    while True:
        await asyncio.sleep(INTERVALO_REFRESCO)
        try:
            await refrescar_todo()
        except Exception as e:  # nunca debe morir el bucle
            logger.error(f"Error en el bucle de refresco: {e}")


_tarea_bucle = None


async def iniciar(application) -> None:
    """Usar como post_init de la aplicación."""
    global _tarea_bucle
    logger.info(f"Catálogo de etiquetas: pestaña '{PESTANA_CATALOGO}'")
    _tarea_bucle = _lanzar(_bucle_refresco())


async def detener(application) -> None:
    """Usar como post_shutdown de la aplicación."""
    if _tarea_bucle is not None:
        _tarea_bucle.cancel()


# --------------------------------------------------------------------------- #
# Consultas de inventario (comunes a SAP, vendedor y etiquetas)
# --------------------------------------------------------------------------- #
def buscar_referencias(df: pd.DataFrame, termino: str) -> list:
    """Referencias que contienen todas las palabras del término, sin importar
    tildes, mayúsculas ni espacios repetidos (regex=False: "(" o "+" no rompen)."""
    palabras = normalizar_texto(termino).split()
    if not palabras:
        return []
    columna = "_ref_norm" if "_ref_norm" in df.columns else "_ref"
    mask = pd.Series(True, index=df.index)
    for p in palabras:
        mask &= df[columna].str.contains(p, regex=False, na=False)
    return df.loc[mask, "_ref"].unique().tolist()


def colores_de(df: pd.DataFrame, ref: str) -> list:
    return [c for c in df.loc[df["_ref"] == ref, "_color"].unique().tolist() if c]


def disponibilidad_bodega01(df: pd.DataFrame, ref: str) -> list:
    """Lista de (color, metros disponibles en bodega 01) para una referencia."""
    df_ref = df[df["_ref"] == ref]
    colores = [c for c in df_ref["_color"].unique().tolist() if c]
    suma = df_ref[df_ref["_b01"]].groupby("_color")["_disp"].sum()
    return [(c, max(float(suma.get(c, 0.0)), 0.0)) for c in colores]
