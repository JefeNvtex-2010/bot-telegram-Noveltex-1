"""Etiquetas Zebra (10x5 cm, 203 dpi).

Dos modos, según las variables de entorno:
  * COLA    (si existe URL_COLA): el bot deja el trabajo en la cola (Apps Script) y el
            agente del local lo imprime. Úsalo si el bot corre en Render.
  * DIRECTO (si NO existe URL_COLA): el bot envía el ZPL por socket a la Zebra.
            Úsalo si el bot corre en un equipo de la misma red que la impresora.

Variables de entorno:
  ZEBRA_IP     IP de la impresora (por defecto 147.120.0.181)
  ZEBRA_PORT   puerto RAW (por defecto 6101)
  URL_COLA     URL /exec del Apps Script (solo modo cola)
  TOKEN_COLA   token del Apps Script (solo modo cola)
"""
import os
import socket

import requests

ZEBRA_IP = os.environ.get("ZEBRA_IP", "147.120.0.181")
ZEBRA_PORT = int(os.environ.get("ZEBRA_PORT", "6101"))
URL_COLA = os.environ.get("URL_COLA", "")
TOKEN_COLA = os.environ.get("TOKEN_COLA", "")


def _limpiar(texto: str) -> str:
    return str(texto).replace("^", " ").replace("~", " ").strip()


def generar_zpl(referencia: str, color: str, metros: str = "", copias: int = 1) -> str:
    ref = _limpiar(referencia)
    col = _limpiar(color)
    mts = _limpiar(metros)
    return f"""^XA
^PW799
^LL408
^LH0,0
^CI28
^PQ{max(1, int(copias))}
^FO30,20^A0N,80,80^FB740,1,0,L^FD{ref}^FS
^FO30,110^A0N,55,55^FB740,1,0,L^FD{col}^FS
^FO30,185^BY3^BCN,100,Y,N,N^FD{ref}^FS
^FO30,330^A0N,65,65^FB740,1,0,L^FD{mts}^FS
^XZ"""


def enviar_a_zebra(zpl: str) -> None:
    """Envía ZPL a la impresora por red. Lo usan el modo directo y el agente."""
    with socket.create_connection((ZEBRA_IP, ZEBRA_PORT), timeout=10) as s:
        s.sendall(zpl.encode("utf-8"))


def _encolar(referencia: str, color: str, metros: str, copias: int) -> None:
    r = requests.post(
        URL_COLA,
        json={
            "token": TOKEN_COLA,
            "accion": "encolar",
            "referencia": referencia,
            "color": color,
            "metros": metros,
            "copias": copias,
        },
        timeout=20,
    )
    r.raise_for_status()
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"La cola rechazó el trabajo: {datos.get('error')}")


def imprimir_etiqueta(referencia: str, color: str, metros: str = "", copias: int = 1) -> None:
    """Imprime (o encola) la etiqueta. Bloqueante: llamar con asyncio.to_thread."""
    if URL_COLA:
        _encolar(referencia, color, metros, copias)
    else:
        enviar_a_zebra(generar_zpl(referencia, color, metros, copias))
