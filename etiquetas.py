"""Etiquetas Zebra Noveltex (10x5 cm, 203 dpi).

Dos modos, según las variables de entorno:
  * COLA    (si existe URL_COLA): el bot deja el trabajo en la cola (Apps Script) y un
            agente en la red del local lo imprime. Úsalo si la Zebra NO es alcanzable
            desde internet.
  * DIRECTO (si NO existe URL_COLA): el bot envía el ZPL por socket a la Zebra.
            Úsalo si la impresora es alcanzable desde donde corre el bot (por ejemplo,
            Render con la IP pública abierta en el puerto 9100).

Variables de entorno:
  ZEBRA_IP     IP de la impresora (por defecto 147.120.0.183)
  ZEBRA_PORT   puerto RAW (por defecto 9100)
  URL_COLA     URL /exec del Apps Script (solo modo cola)
  TOKEN_COLA   token del Apps Script (solo modo cola)
"""
import datetime
import os
import socket
import time

ZEBRA_IP = os.environ.get("ZEBRA_IP", "147.120.0.183")
ZEBRA_PORT = int(os.environ.get("ZEBRA_PORT", "9100"))
URL_COLA = os.environ.get("URL_COLA", "")
TOKEN_COLA = os.environ.get("TOKEN_COLA", "")

REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS = 2  # segundos


def _limpiar(texto) -> str:
    return str(texto).replace("^", " ").replace("~", " ").strip()


def generar_zpl_noveltex(item, ref, color, metros, centimetros, copias=1) -> str:
    """Etiqueta de rollo de tela: item, referencia, color, cantidad, fecha y lote con diseño idéntico al original."""
    tz = datetime.timezone(datetime.timedelta(hours=-5))  # Colombia (Render corre en UTC)
    ahora = datetime.datetime.now(tz)
    fecha = ahora.strftime("%d/%m/%Y %H:%M")
    lote = ahora.strftime("%d%m%Y")

    item, color = _limpiar(item), _limpiar(color)
    ref = _limpiar(ref)[:20]
    m, cm = int(metros), int(centimetros)
    cant = f"{m:03d}.{cm:02d}"
    codigo = f"{item}{color}{m:03d}{cm:02d}"  # ej. 026850205500

    # Retorno del bloque ZPL estructurado bajo la nueva plantilla
    return f"""^XA
^PW799
^LL408
^LH0,0
^CI28
^PQ{max(1, int(copias))}
^FO30,35^BQN,2,4^FDQA,{codigo}^FS
^FO305,30^GB20,20,20^FS
^FO317,42^GB20,20,20^FS
^FO329,54^GB20,20,20^FS
^FO365,37^A0N,36,36^FDNOVELTEX S.A.S^FS
^FO320,105^A0N,20,20^FDTel 6023798741 - 6023798742^FS
^FO50,165^A0N,38,38^FDITEM^FS
^FO220,165^A0N,38,38^FD{item}^FS
^FO50,210^A0N,38,38^FDREF.^FS
^FO220,210^A0N,40,40^FD{ref}^FS
^FO50,255^A0N,38,38^FDCOLOR^FS
^FO220,255^A0N,38,38^FD{color}^FS
^FO50,305^A0N,52,52^FDCANT.^FS
^FO240,305^A0N,52,52^FD{cant}^FS
^FO60,370^A0N,24,24^FD{fecha}^FS
^FO310,370^A0N,24,24^FDOPERARIO: ORIGINAL^FS
^FO690,370^A0B,46,26^FD{codigo}^FS
^FO750,370^A0B,44,28^FD{ref}^FS
^XZ"""


def enviar_a_zebra(zpl: str) -> None:
    """Envía ZPL a la impresora por red, con reintentos."""
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            with socket.create_connection((ZEBRA_IP, ZEBRA_PORT), timeout=10) as s:
                s.sendall(zpl.encode("utf-8"))
            return
        except OSError as e:
            ultimo_error = e
            if intento < REINTENTOS:
                time.sleep(ESPERA_ENTRE_REINTENTOS)
    raise ConnectionError(
        f"No se pudo conectar a la Zebra en {ZEBRA_IP}:{ZEBRA_PORT} "
        f"después de {REINTENTOS} intentos: {ultimo_error}"
    )


def _encolar(item, referencia, color, metros, centimetros, copias) -> None:
    import requests

    r = requests.post(
        URL_COLA,
        json={
            "token": TOKEN_COLA,
            "accion": "encolar",
            "item": str(item),
            "referencia": referencia,
            "color": str(color),
            "metros": int(metros),
            "centimetros": int(centimetros),
            "copias": int(copias),
        },
        timeout=20,
    )
    r.raise_for_status()
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"La cola rechazó el trabajo: {datos.get('error')}")


def imprimir_etiqueta(item, referencia, color, metros, centimetros, copias=1) -> None:
    """Imprime (o encola) la etiqueta. Bloqueante: llamar con asyncio.to_thread."""
    if URL_COLA:
        _encolar(item, referencia, color, metros, centimetros, copias)
    else:
        enviar_a_zebra(
            generar_zpl_noveltex(item, referencia, color, metros, centimetros, copias)
        )
