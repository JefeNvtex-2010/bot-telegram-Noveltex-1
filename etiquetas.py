"""Etiquetas Zebra Noveltex (10x5 cm, 203 dpi).

Modificado para soportar múltiples impresoras en modo DIRECTO sin usar config.py
"""
import datetime
import os
import socket
import time
import requests

# Conexión directa por IP pública + puerto redirigido (variables de entorno en Render).
#  - ZEBRA 183: usa ZEBRA_IP_183 / ZEBRA_PORT_183 y, si no existen, ZEBRA_IP / ZEBRA_PORT.
#  - ZEBRA 181: requiere ZEBRA_IP_181 / ZEBRA_PORT_181 (cada impresora tiene su propio puerto).
def _puerto(*nombres, defecto=9100) -> int:
    for n in nombres:
        v = os.environ.get(n, "").strip()
        if v.isdigit():
            return int(v)
    return defecto


IMPRESORAS_ZEBRA = {
    "1": {
        "nombre": "ZEBRA 183",
        "ip": (os.environ.get("ZEBRA_IP_183") or os.environ.get("ZEBRA_IP") or "").strip(),
        "port": _puerto("ZEBRA_PORT_183", "ZEBRA_PORT"),
    },
    "2": {
        "nombre": "ZEBRA 181",
        "ip": os.environ.get("ZEBRA_IP_181", "").strip(),
        "port": _puerto("ZEBRA_PORT_181"),
    },
}

URL_COLA = os.environ.get("URL_COLA", "")
TOKEN_COLA = os.environ.get("TOKEN_COLA", "")

REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS = 2  # segundos


def _limpiar(texto) -> str:
    return str(texto).replace("^", " ").replace("~", " ").strip()


def generar_zpl_noveltex(item, ref, color, metros, centimetros, copias=1, operario="ORIGINAL") -> str:
    tz = datetime.timezone(datetime.timedelta(hours=-5))  # Colombia
    ahora = datetime.datetime.now(tz)
    fecha = ahora.strftime("%d/%m/%Y %H:%M")
    lote = ahora.strftime("%d%m%Y")

    item, color = _limpiar(item), _limpiar(color)
    ref = _limpiar(ref)[:20]
    nombre_operario = _limpiar(operario).upper()[:25]
    
    m, cm = int(metros), int(centimetros)
    cant = f"{m:03d}.{cm:02d}"
    codigo = f"{item}{color}{m:03d}{cm:02d}"

    return f"""^XA
^PW799
^LL408
^LH0,0
^CI28
^PQ{max(1, int(copias))}
^FO50,35^BQN,2,5^FDQA,{codigo}^FS
^FO220,40^A0N,44,44^FDNOVELTEX S.A.S^FS
^FO220,95^A0N,22,22^FDTel 6023798741 - 6023798742^FS
^FO50,165^A0N,38,38^FDITEM^FS
^FO220,165^A0N,38,38^FD{item}^FS
^FO50,210^A0N,38,38^FDREF.^FS
^FO220,210^A0N,40,40^FD{ref}^FS
^FO50,255^A0N,38,38^FDCOLOR^FS
^FO220,255^A0N,38,38^FD{color}^FS
^FO50,305^A0N,52,52^FDCANT.^FS
^FO220,305^A0N,52,52^FD{cant}^FS
^FO60,370^A0N,24,24^FD{fecha}^FS
^FO310,370^A0N,24,24^FDOPERARIO: {nombre_operario}^FS
^FO605,35^A0R,74,38^FD{codigo}^FS
^FO675,35^A0R,68,40^FD{ref}^FS
^XZ"""


def enviar_a_zebra(zpl: str, ip: str, puerto: int) -> None:
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            with socket.create_connection((ip, puerto), timeout=10) as s:
                s.sendall(zpl.encode("utf-8"))
            return
        except OSError as e:
            ultimo_error = e
            if intento < REINTENTOS:
                time.sleep(ESPERA_ENTRE_REINTENTOS)
    raise ConnectionError(
        f"No se pudo conectar a la Zebra en {ip}:{puerto} "
        f"después de {REINTENTOS} intentos: {ultimo_error}"
    )


def _encolar(item, referencia, color, metros, centimetros, copias, operario, id_impresora=None) -> None:
    payload = {
        "token": TOKEN_COLA,
        "accion": "encolar",
        "item": str(item),
        "referencia": referencia,
        "color": str(color),
        "metros": int(metros),
        "centimetros": int(centimetros),
        "copias": int(copias),
        "operario": operario,
    }
    if id_impresora:
        payload["impresora"] = id_impresora

    r = requests.post(URL_COLA, json=payload, timeout=20)
    r.raise_for_status()
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"La cola rechazó el trabajo: {datos.get('error')}")


def imprimir_etiqueta(item, referencia, color, metros, centimetros, id_impresora, copias=1, operario="ORIGINAL") -> None:
    if id_impresora not in IMPRESORAS_ZEBRA:
        raise ValueError(f"La impresora con ID '{id_impresora}' no está registrada.")
    config_zebra = IMPRESORAS_ZEBRA[id_impresora]
    if URL_COLA:
        _encolar(item, referencia, color, metros, centimetros, copias, operario, id_impresora)
    else:
        if not config_zebra["ip"]:
            raise ConnectionError(
                f"{config_zebra['nombre']} no tiene IP configurada en las variables de entorno."
            )
        zpl = generar_zpl_noveltex(item, referencia, color, metros, centimetros, copias, operario)
        enviar_a_zebra(zpl, config_zebra["ip"], config_zebra["port"])
