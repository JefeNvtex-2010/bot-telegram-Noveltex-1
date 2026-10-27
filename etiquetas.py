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
    codigo = f"{item}{color}{m:03d}{cm:02d}"  # ej. 001852202345

    # Cadena hexadecimal de la imagen indexada del logo Noveltex (64x64 pixeles, 512 bytes)
    logo_hex = (
        "0000000000000000000003FC00000000000007FE0000000000000FFF0000000000001FF"
        "F8000000000003FFF8000000000007FFF800000000000FFFF800000000001FFFF80000"
        "0000003F1FF800000000000701FF80000000000E01FF80000000001C01FF8000000000"
        "380FFF8000000000700FFF8000000000E00FFF8000000001C00FFF80003FC0380FFF80"
        "007FE07001FF8000FFF0E0001FF801FFF1C0001FF8003FFF380001FF8007FFF700001"
        "FF800FFFFE00001FF801FFFFF00001FF803F1FFF80001FF80701FFF80001FF80E01FFF"
        "80001FF81C01FFF80001FF8380FFF80000FF8700FFF80000FF8E00FFF80000FF8C00FF"
        "F80000FF80000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000"
    )

    # Valores centrados a la coordenada X=315 según marcas verticales en bolígrafo
    return f"""^XA
^PW799
^LL408
^LH0,0
^CI28
^PQ{max(1, int(copias))}
^FO50,35^BQN,2,5^FDQA,{codigo}^FS
^FO280,30^GFA,512,512,8,{logo_hex}^FS
^FO360,45^A0N,38,38^FDNOVELTEX S.A.S^FS
^FO320,110^A0N,22,22^FDTel 6023798741 - 6023798742^FS
^FO50,165^A0N,38,38^FDITEM^FS
^FO315,165^A0N,38,38^FD{item}^FS
^FO50,210^A0N,38,38^FDREF.^FS
^FO315,210^A0N,40,40^FD{ref}^FS
^FO50,255^A0N,38,38^FDCOLOR^FS
^FO315,255^A0N,38,38^FD{color}^FS
^FO50,305^A0N,52,52^FDCANT.^FS
^FO315,305^A0N,52,52^FD{cant}^FS
^FO60,370^A0N,24,24^FD{fecha}^FS
^FO310,370^A0N,24,24^FDOPERARIO: ORIGINAL^FS
^FO640,150^A0B,62,34^FD{codigo}^FS
^FO705,150^A0B,58,36^FD{ref}^FS
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
