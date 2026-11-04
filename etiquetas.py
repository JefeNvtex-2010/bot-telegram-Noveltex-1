"""Etiquetas Zebra Noveltex (10x5 cm, 203 dpi).

Impresión directa por socket a la IP pública + puerto redirigido de cada Zebra.

Variables de entorno en Render (una pareja por impresora):
  ZEBRA_IP_81 / ZEBRA_PORT_81
  ZEBRA_IP_82 / ZEBRA_PORT_82
  ZEBRA_IP_83 / ZEBRA_PORT_83   (si no existen, usa ZEBRA_IP / ZEBRA_PORT)

- La IP puede ser un dominio (DDNS) y también se puede escribir como 'ip:puerto'.
- Si existe URL_COLA, se usa la cola (Apps Script) y se ignoran las IP. Bórrala para imprimir directo.
"""
import datetime
import logging
import os
import socket
import time
import requests

logger = logging.getLogger(__name__)

IMPRESORAS_ZEBRA = {
    "81": {"nombre": "ZEBRA 81"},
    "82": {"nombre": "ZEBRA 82"},
    "83": {"nombre": "ZEBRA 83"},
}

REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS = 2  # segundos


def _env(*nombres) -> str:
    for n in nombres:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


def destino_impresora(id_impresora) -> tuple:
    """Devuelve (ip, puerto) leyendo las variables de entorno en el momento de imprimir."""
    id_impresora = str(id_impresora)
    ip = _env(f"ZEBRA_IP_{id_impresora}")
    puerto_txt = _env(f"ZEBRA_PORT_{id_impresora}")

    # Compatibilidad: la 83 también puede usar las variables sin sufijo
    if id_impresora == "83":
        ip = ip or _env("ZEBRA_IP")
        puerto_txt = puerto_txt or _env("ZEBRA_PORT")

    # Permite escribir 'ip:puerto' en una sola variable
    if ":" in ip:
        ip, _, p = ip.rpartition(":")
        puerto_txt = p.strip()

    puerto = int(puerto_txt) if puerto_txt.isdigit() else 9100
    return ip.strip(), puerto


def _limpiar(texto) -> str:
    return str(texto).replace("^", " ").replace("~", " ").strip()


def generar_zpl_noveltex(item, ref, color, metros, centimetros, copias=1, operario="ORIGINAL") -> str:
    tz = datetime.timezone(datetime.timedelta(hours=-5))  # Colombia
    ahora = datetime.datetime.now(tz)
    fecha = ahora.strftime("%d/%m/%Y %H:%M")

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
            logger.warning("Intento %d/%d fallido hacia %s:%s -> %s", intento, REINTENTOS, ip, puerto, e)
            if intento < REINTENTOS:
                time.sleep(ESPERA_ENTRE_REINTENTOS)
    raise ConnectionError(
        f"No se pudo conectar a la Zebra en {ip}:{puerto} "
        f"después de {REINTENTOS} intentos: {ultimo_error}"
    )


def _encolar(item, referencia, color, metros, centimetros, copias, operario, id_impresora=None) -> None:
    url_cola = os.environ.get("URL_COLA", "").strip()
    token_cola = os.environ.get("TOKEN_COLA", "")
    payload = {
        "token": token_cola,
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

    r = requests.post(url_cola, json=payload, timeout=20)
    r.raise_for_status()
    datos = r.json()
    if not datos.get("ok"):
        raise RuntimeError(f"La cola rechazó el trabajo: {datos.get('error')}")


def imprimir_etiqueta(item, referencia, color, metros, centimetros, id_impresora, copias=1, operario="ORIGINAL") -> None:
    id_impresora = str(id_impresora)
    if id_impresora not in IMPRESORAS_ZEBRA:
        raise ValueError(f"La impresora con ID '{id_impresora}' no está registrada.")
    nombre = IMPRESORAS_ZEBRA[id_impresora]["nombre"]

    if os.environ.get("URL_COLA", "").strip():
        logger.info("Etiqueta por COLA (URL_COLA definida) para %s: las IP se ignoran", nombre)
        _encolar(item, referencia, color, metros, centimetros, copias, operario, id_impresora)
        return

    ip, puerto = destino_impresora(id_impresora)
    if not ip:
        raise ConnectionError(
            f"{nombre} no tiene IP configurada (define ZEBRA_IP_{id_impresora} en las variables de entorno)."
        )
    logger.info("Enviando etiqueta directa a %s (%s:%s)", nombre, ip, puerto)
    zpl = generar_zpl_noveltex(item, referencia, color, metros, centimetros, copias, operario)
    enviar_a_zebra(zpl, ip, puerto)
