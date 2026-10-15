"""Capa de datos compartida para el bot.

Qué resuelve:
  * Las hojas de Google Sheets se descargan en SEGUNDO PLANO cada INTERVALO_REFRESCO
    segundos. Los handlers nunca esperan una descarga (salvo la primerísima carga).
  * Las hojas se preparan UNA sola vez al descargarlas (columnas limpias, números
    convertidos, bodega 01 marcada), no en cada búsqueda.
  * El inventario SAP y el de vendedor usan la misma hoja: se descarga una sola vez.
  * La descarga corre en un hilo (asyncio.to_thread) para no congelar el bot.
"""
import asyncio
import io
import logging
import os
import time

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
        self._lock = None  # se crea perezosamente dentro del event loop

    def _candado(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _descargar(self) -> pd.DataFrame:
        t0 = time.time()
        r = _session.get(self.url, timeout=(5, 25))
        r.raise_for_status()
        df = pd.read_csv(io.BytesIO(r.content), dtype=str, keep_default_na=False, encoding="utf-8-sig")
        df.columns = df.columns.str.strip()
        df = self._preparar(df)
        logger.info(f"📥 {self.nombre}: {len(df)} filas descargadas en {time.time() - t0:.1f}s")
        return df

    async def _cargar(self) -> bool:
        try:
            df = await asyncio.wait_for(asyncio.to_thread(self._descargar), timeout=45)
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"⚠️ Error cargando {self.nombre}: {self.ultimo_error}")
            return False  # si ya había datos, se conservan los anteriores
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


pedidos = FuenteCSV("pedidos", URL_PEDIDOS, _preparar_pedidos)
inventario = FuenteCSV("inventario", URL_INVENTARIO, _preparar_inventario)


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
    await asyncio.gather(pedidos.refrescar(), inventario.refrescar())


def invalidar_todo() -> None:
    pedidos.invalidar()
    inventario.invalidar()


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
    _tarea_bucle = _lanzar(_bucle_refresco())


async def detener(application) -> None:
    """Usar como post_shutdown de la aplicación."""
    if _tarea_bucle is not None:
        _tarea_bucle.cancel()


# --------------------------------------------------------------------------- #
# Consultas de inventario (comunes a SAP y vendedor)
# --------------------------------------------------------------------------- #
def buscar_referencias(df: pd.DataFrame, termino: str) -> list:
    mask = df["_ref"].str.contains(termino, regex=False, na=False)  # regex=False: "(" o "+" no rompen
    return df.loc[mask, "_ref"].unique().tolist()


def colores_de(df: pd.DataFrame, ref: str) -> list:
    return [c for c in df.loc[df["_ref"] == ref, "_color"].unique().tolist() if c]


def disponibilidad_bodega01(df: pd.DataFrame, ref: str) -> list:
    """Lista de (color, metros disponibles en bodega 01) para una referencia."""
    df_ref = df[df["_ref"] == ref]
    colores = [c for c in df_ref["_color"].unique().tolist() if c]
    suma = df_ref[df_ref["_b01"]].groupby("_color")["_disp"].sum()
    return [(c, max(float(suma.get(c, 0.0)), 0.0)) for c in colores]
