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


def generar_zpl_noveltex(item, ref, color, metros, centimetros, copias=1, operario="ORIGINAL") -> str:
    """Etiqueta de rollo de tela: item, referencia, color, cantidad, fecha, lote y operario dinámico."""
    tz = datetime.timezone(datetime.timedelta(hours=-5))  # Colombia
    ahora = datetime.datetime.now(tz)
    fecha = ahora.strftime("%d/%m/%Y %H:%M")
    lote = ahora.strftime("%d%m%Y")

    item, color = _limpiar(item), _limpiar(color)
    ref = _limpiar(ref)[:20]
    nombre_operario = _limpiar(operario).upper()[:25]
    
    m, cm = int(metros), int(centimetros)
    cant = f"{m:03d}.{cm:02d}"
    codigo = f"{item}{color}{m:03d}{cm:02d}"  # ej. 026869509999

    # Bloque ZPL con las coordenadas Y cambiadas a 60 para centrado vertical perfecto a la derecha
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
^FO605,60^A0R,74,38^FD{codigo}^FS
^FO675,60^A0R,68,40^FD{ref}^FS
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


def _encolar(item, referencia, color, metros, centimetros, copias, operario) -> None:
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
            "operario": operario,
        },
        timeout=20,
    )
    r.raise_for_status()
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"La cola rechazó el trabajo: {datos.get('error')}")


def imprimir_etiqueta(item, referencia, color, metros, centimetros, copias=1, operario="ORIGINAL") -> None:
    """Imprime (o encola) la etiqueta agregando soporte para operario dinámico."""
    if URL_COLA:
        _encolar(item, referencia, color, metros, centimetros, copias, operario)
    else:
        enviar_a_zebra(
            generar_zpl_noveltex(item, referencia, color, metros, centimetros, copias, operario)
        )
